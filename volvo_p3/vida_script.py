"""Parse VIDA diagnostic scripts into a read-only plan.

A VIDA script is a single-entry ZIP archive whose entry is an XML document
rooted at ``script``. The archive reaches the client as
``ScriptContent.XmlDataCompressed``; the entry name is a document id, not a
file name, so it is not used to select the body.

Only the addressing and DTC-list parts of the format are modelled here.
Parsing never executes a script and never talks to a car. ``read_only_requests``
is the only method that produces request data, and it drops every service that
is not a known read.

Service ids inside a script are written as hexadecimal bytes without a prefix:
a ClearDtc script carries ``14``, a ReadDTCs script carries ``19`` and ``22``.
``EcuAddress``, ``EcuType`` and ``TesterAddress`` are identifiers, so they are
read as decimal unless written with a ``0x`` prefix.
"""

from __future__ import annotations

import io
import re
import zipfile
import zlib
from dataclasses import dataclass
from typing import Callable, Iterable, List, Optional, Sequence, Tuple
from xml.etree import ElementTree


MAX_SCRIPT_BYTES = 8 * 1024 * 1024

READ_ONLY_SERVICES = frozenset({0x01, 0x09, 0x19, 0x22})

READ_SERVICE_NAMES = {
    0x01: "read_current_data",
    0x09: "read_vehicle_information",
    0x19: "read_dtc_information",
    0x22: "read_data_by_identifier",
}

NON_READ_SERVICES = {
    0x10: "diagnostic_session_control",
    0x11: "ecu_reset",
    0x14: "clear_diagnostic_information",
    0x27: "security_access",
    0x28: "communication_control",
    0x2E: "write_data_by_identifier",
    0x2F: "input_output_control",
    0x31: "routine_control",
    0x3D: "write_memory_by_address",
    0x3E: "tester_present",
    0x85: "control_dtc_setting",
}

_PROFILE_URN_RE = re.compile(r"^urn:x-nevis:profile:([0-9a-fA-F]{16})$")
_PROFILE_ID_RE = re.compile(r"^[0-9a-fA-F]{16}$")
_XML_ENCODING_RE = re.compile(br"""<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)["']""")
_ELEMENT_START_RE = re.compile(r"<[A-Za-z_:]")
_PATH_ATTRIBUTES = (
    "value",
    "name",
    "node",
    "nodeid",
    "property",
    "path",
    "source",
    "list",
    "class",
    "id",
)


class VidaScriptError(ValueError):
    """Raised when a script archive or document cannot be parsed."""


@dataclass(frozen=True)
class EcuPlan:
    """One ``Components.VehComm`` node.

    ``service_ids`` holds every service the node names, in document order and
    deduplicated. ``requests`` and ``responses`` keep the raw script values;
    they are often empty slots or bindings rather than bytes.
    """

    mode: Optional[str]
    ecu_address: Optional[int]
    ecu_type: Optional[int]
    tester_address: Optional[int]
    service_ids: Tuple[int, ...]
    requests: Tuple[str, ...]
    responses: Tuple[str, ...]


@dataclass(frozen=True)
class DtcPlan:
    """Paths the script binds to a DTC list.

    ``raw_nodes`` keeps the matched XML snippets so a caller can inspect the
    original binding without re-parsing ``VidaScript.raw_xml``.
    """

    list_path: Optional[str]
    value_path: Optional[str]
    text_path: Optional[str]
    raw_nodes: Tuple[str, ...]


@dataclass(frozen=True)
class ReadRequest:
    """A request that survived the read-only filter.

    ``request`` is None when the script carried no request bytes, or when the
    bytes could not be shown to belong to a read service.
    """

    ecu_address: Optional[int]
    ecu_type: Optional[int]
    tester_address: Optional[int]
    service_id: int
    request: Optional[str]

    @property
    def service_name(self) -> str:
        return READ_SERVICE_NAMES.get(self.service_id, "unknown")


@dataclass(frozen=True)
class VidaScript:
    """The addressing and DTC-list view of one VIDA script document."""

    docno: Optional[str]
    title: Optional[str]
    variant_id: Optional[str]
    language: Optional[str]
    profile_ids: Tuple[str, ...]
    script_type: Optional[str]
    ecus: Tuple[EcuPlan, ...]
    dtc_list: Optional[DtcPlan]
    raw_xml: str

    def read_only_requests(self) -> Tuple[ReadRequest, ...]:
        """Return requests for known read services only.

        Writes, clears, security access, session changes, resets, keepalives
        and unknown services are dropped, as are request byte strings whose
        leading service byte disagrees with the node or is not a read.
        """

        result: List[ReadRequest] = []
        for plan in self.ecus:
            for service_id in plan.service_ids:
                if not is_read_only_service(service_id):
                    continue
                values = _safe_request_values(plan, service_id)
                if not values:
                    values = (None,)
                for value in values:
                    result.append(
                        ReadRequest(
                            ecu_address=plan.ecu_address,
                            ecu_type=plan.ecu_type,
                            tester_address=plan.tester_address,
                            service_id=service_id,
                            request=value,
                        )
                    )
        return tuple(result)


def classify_service(service_id: int) -> str:
    """Classify one service byte.

    Returns ``"read_only"`` for the four known read services, a stable
    lower-case tag for known non-read services, and ``"unknown"`` otherwise.
    """

    if service_id in READ_ONLY_SERVICES:
        return "read_only"
    return NON_READ_SERVICES.get(service_id, "unknown")


def is_read_only_service(service_id: int) -> bool:
    """Return True only for a service in ``READ_ONLY_SERVICES``."""

    return service_id in READ_ONLY_SERVICES


def parse_script_zip(data: bytes) -> VidaScript:
    """Parse a single-entry ZIP archive holding one VIDA XML script."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("script archive must be bytes")
    payload = bytes(data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise VidaScriptError("not a valid ZIP archive: {}".format(exc)) from exc

    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if not entries:
            raise VidaScriptError("script archive contains no file entries")
        if len(entries) > 1:
            names = ", ".join(info.filename for info in entries)
            raise VidaScriptError(
                "script archive must contain exactly one entry, found {}: {}".format(
                    len(entries), names
                )
            )
        entry = entries[0]
        if entry.file_size > MAX_SCRIPT_BYTES:
            raise VidaScriptError(
                "script entry {!r} is larger than {} bytes".format(
                    entry.filename, MAX_SCRIPT_BYTES
                )
            )
        try:
            body = archive.read(entry)
        except (
            zipfile.BadZipFile,
            NotImplementedError,
            RuntimeError,
            OSError,
            EOFError,
            zlib.error,
        ) as exc:
            raise VidaScriptError(
                "cannot read script entry {!r}: {}".format(entry.filename, exc)
            ) from exc

    if len(body) > MAX_SCRIPT_BYTES:
        raise VidaScriptError(
            "script entry {!r} is larger than {} bytes".format(
                entry.filename, MAX_SCRIPT_BYTES
            )
        )

    text = _decode_script(body, entry.filename)
    try:
        return parse_script_xml(text)
    except VidaScriptError as exc:
        raise VidaScriptError(
            "script entry {!r}: {}".format(entry.filename, exc)
        ) from exc


def parse_script_xml(text: str) -> VidaScript:
    """Parse an uncompressed VIDA script document."""

    if not isinstance(text, str):
        raise TypeError("script XML must be str")
    if not text.strip():
        raise VidaScriptError("missing root element: script document is empty")
    if _ELEMENT_START_RE.search(text) is None:
        raise VidaScriptError("missing root element: no XML element found")

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise VidaScriptError("malformed XML: {}".format(exc)) from exc

    root_name = _local_name(root.tag)
    if root_name != "script":
        raise VidaScriptError(
            "missing <script> root element, found <{}>".format(root_name)
        )

    return VidaScript(
        docno=_attribute(root, "docno"),
        title=_title(root),
        variant_id=_attribute(root, "VariantId"),
        language=_attribute(root, "lang"),
        profile_ids=_profile_ids(_attribute(root, "profile")),
        script_type=_attribute(root, "type"),
        ecus=tuple(_ecu_plans(root)),
        dtc_list=_dtc_plan(root),
        raw_xml=text,
    )


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _attribute(element: ElementTree.Element, name: str) -> Optional[str]:
    direct = element.get(name)
    if direct is not None:
        return direct.strip()
    for key, value in element.attrib.items():
        if _local_name(key) == name:
            return value.strip()
    return None


def _profile_ids(value: Optional[str]) -> Tuple[str, ...]:
    if not value:
        return ()
    ids: List[str] = []
    for token in value.split():
        match = _PROFILE_URN_RE.match(token)
        if match:
            ids.append(match.group(1).lower())
        elif _PROFILE_ID_RE.match(token):
            ids.append(token.lower())
    return tuple(dict.fromkeys(ids))


def _title(root: ElementTree.Element) -> Optional[str]:
    for element in root.iter():
        if _local_name(element.tag) == "title":
            text = "".join(element.itertext()).strip()
            if text:
                return text
    return None


def _is_vehcomm(element: ElementTree.Element) -> bool:
    name = _local_name(element.tag)
    if name == "VehComm":
        return True
    if name != "node":
        return False
    class_name = _attribute(element, "class") or ""
    return class_name.rsplit(".", 1)[-1] == "VehComm"


def _field_values(node: ElementTree.Element, field: str) -> List[str]:
    """Collect one VehComm field from property children or direct children."""

    values: List[str] = []
    for element in node.iter():
        name = _local_name(element.tag)
        if name == "property":
            names = {_attribute(element, "name"), _attribute(element, "class")}
            if field not in names:
                continue
        elif name != field:
            continue
        value = _attribute(element, "value")
        if value is None:
            value = "".join(element.itertext()).strip()
        values.append(value.strip())
    return values


def _first(values: Iterable[str]) -> Optional[str]:
    for value in values:
        if value:
            return value
    return None


def _parse_identifier(value: Optional[str]) -> Optional[int]:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        if text.isdigit():
            return int(text, 10)
        return int(text, 16)
    except ValueError:
        return None


def _parse_service_id(value: str) -> Optional[int]:
    text = value.strip().lower()
    if text.startswith("0x"):
        text = text[2:]
    if not text or any(char not in "0123456789abcdef" for char in text):
        return None
    service_id = int(text, 16)
    if 0 <= service_id <= 0xFF:
        return service_id
    return None


def _ecu_plans(root: ElementTree.Element) -> Iterable[EcuPlan]:
    for element in root.iter():
        if not _is_vehcomm(element):
            continue
        service_ids: List[int] = []
        for value in _field_values(element, "ServiceId"):
            service_id = _parse_service_id(value)
            if service_id is not None and service_id not in service_ids:
                service_ids.append(service_id)
        yield EcuPlan(
            mode=_first(_field_values(element, "Mode")),
            ecu_address=_parse_identifier(_first(_field_values(element, "EcuAddress"))),
            ecu_type=_parse_identifier(_first(_field_values(element, "EcuType"))),
            tester_address=_parse_identifier(
                _first(_field_values(element, "TesterAddress"))
            ),
            service_ids=tuple(service_ids),
            requests=tuple(_field_values(element, "Request")),
            responses=tuple(_field_values(element, "Response")),
        )


def _normalize_reference(value: str) -> str:
    text = value.strip().lower()
    text = re.sub(r"\s*([./:\\])\s*", ".", text)
    return re.sub(r"\s+", "", text)


def _path_candidates(
    root: ElementTree.Element,
) -> List[Tuple[str, ElementTree.Element]]:
    candidates: List[Tuple[str, ElementTree.Element]] = []
    for element in root.iter():
        for attribute in _PATH_ATTRIBUTES:
            value = _attribute(element, attribute)
            if value:
                candidates.append((value, element))
        node = _attribute(element, "node")
        prop = _attribute(element, "property")
        if node and prop:
            candidates.append(("{}.{}".format(node, prop), element))
    return candidates


def _best_match(
    candidates: Sequence[Tuple[str, ElementTree.Element]],
    predicate: Callable[[str], bool],
) -> Tuple[Optional[str], Tuple[ElementTree.Element, ...]]:
    matches = [
        (path, element)
        for path, element in candidates
        if predicate(_normalize_reference(path))
    ]
    if not matches:
        return None, ()
    path, _ = min(matches, key=lambda item: len(item[0].strip()))
    elements: List[ElementTree.Element] = []
    for _, element in matches:
        if all(element is not seen for seen in elements):
            elements.append(element)
    return path.strip(), tuple(elements)


def _is_list_reference(path: str) -> bool:
    return path == "dtclist" or path.endswith(".dtclist")


def _is_value_reference(path: str) -> bool:
    return path == "dtc.value" or path.endswith(".dtc.value")


def _is_text_reference(path: str) -> bool:
    return path == "dtc.text" or path.endswith(".dtc.text")


def _dtc_plan(root: ElementTree.Element) -> Optional[DtcPlan]:
    candidates = _path_candidates(root)
    list_path, _ = _best_match(candidates, _is_list_reference)
    value_path, _ = _best_match(candidates, _is_value_reference)
    text_path, _ = _best_match(candidates, _is_text_reference)
    if list_path is None and value_path is None and text_path is None:
        return None

    raw_nodes: List[str] = []
    for predicate in (_is_list_reference, _is_value_reference, _is_text_reference):
        _, elements = _best_match(candidates, predicate)
        for element in elements:
            snippet = ElementTree.tostring(element, encoding="unicode")
            if snippet not in raw_nodes:
                raw_nodes.append(snippet)
    return DtcPlan(
        list_path=list_path,
        value_path=value_path,
        text_path=text_path,
        raw_nodes=tuple(raw_nodes),
    )


def _request_bytes(value: str) -> Optional[bytes]:
    compact = re.sub(r"[\s:_-]+", "", value.strip())
    if not compact or len(compact) % 2:
        return None
    try:
        return bytes.fromhex(compact)
    except ValueError:
        return None


def _safe_request_values(plan: EcuPlan, service_id: int) -> Tuple[str, ...]:
    """Return requests whose leading byte is the same read service."""

    values: List[str] = []
    for value in plan.requests:
        data = _request_bytes(value)
        if data is None:
            continue
        if data[0] != service_id or not is_read_only_service(data[0]):
            continue
        stripped = value.strip()
        if stripped not in values:
            values.append(stripped)
    return tuple(values)


def _decode_script(data: bytes, source: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        match = _XML_ENCODING_RE.search(data[:200])
        encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise VidaScriptError(
            "script entry {!r} is not valid {}: {}".format(source, encoding, exc)
        ) from exc
