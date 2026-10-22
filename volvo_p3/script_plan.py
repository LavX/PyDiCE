"""Parse diagnostic scripts into a read-only plan.

A diagnostic script is a single-entry ZIP archive whose entry is an XML document
rooted at ``script``. The archive reaches the client as
``ScriptContent.XmlDataCompressed``; the entry name is a document id, not a
file name, so it is not used to select the body.

Only the addressing, ECU identity and DTC-list parts of the format are modelled
here. Parsing never executes a script and never talks to a car.
``read_only_requests`` is the only method that produces request data, and it
drops every service that is not a known read.

A VehComm node's ``ServiceId`` property is a decimal CarCom catalog key
(``T111_Service.id``), not a raw UDS service byte. This was confirmed by
decompiling ``BizServices.CarCom.VehicleServices.DLL``'s
``service_GetService`` stored-procedure call (see
``docs/vida-service-templates.md``), and independently by the recovered
script corpus: 4,025 of 4,029 real ``ServiceId`` values are plain decimal
digit strings (the remaining 4 are single-character text corruption of a
digit string, not hex letters), and the common values (106, 112, 101, 114,
119, ...) exceed the 0-255 range a raw byte would allow. An earlier pass's
claim that these values are "hexadecimal without a prefix" does not hold up
against the corpus and is corrected here; a hand-written test fixture using
hex-letter values such as ``3E``/``2E`` for this property was synthetic, not
drawn from the corpus.

A *literal* ``Request``/``Response`` field, when a script's body happens to
carry one, is a different thing: a raw wire byte string (first byte is the
actual UDS service byte), parsed by ``_request_bytes`` and classified with
``classify_service``/``is_read_only_service``/``READ_ONLY_SERVICES`` below.
No script in the committed corpus carries one (`data/diag_dump.tar.xz`
checked directly: 0 of 4,169 VehComm nodes), so this path has no live
coverage today, but it is kept because a differently-recovered script could
have one.

The ``ServiceId`` *property* value (the catalog key) is classified
separately, by ``volvo_p3.vida_service_templates.classify_catalog_service``/
``is_read_only_catalog_service``/``READ_ONLY_CATALOG_SERVICES``, which
``_parse_catalog_service_id`` below feeds. Do not use the raw-byte functions
on a catalog key, or vice versa; they classify different number spaces that
happen to share small values by coincidence.

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

from .vida_service_templates import INFERRED_SERVICE_ROLES, is_read_only_catalog_service


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


class DiagnosticScriptError(ValueError):
    """Raised when a script archive or document cannot be parsed."""


@dataclass(frozen=True)
class EcuPlan:
    """One ``Components.VehComm`` node.

    ``service_ids`` holds every service the node names, in document order and
    deduplicated. ``requests`` and ``responses`` keep the raw script values;
    they are often empty slots or bindings rather than bytes.

    ``ecu_name`` and ``ecu_identifier`` are the optional textual identities a
    script node can carry. They are kept verbatim; nothing is derived from them
    here.
    """

    mode: Optional[str]
    ecu_address: Optional[int]
    ecu_type: Optional[int]
    tester_address: Optional[int]
    service_ids: Tuple[int, ...]
    requests: Tuple[str, ...]
    responses: Tuple[str, ...]
    ecu_name: Optional[str] = None
    ecu_identifier: Optional[str] = None


@dataclass(frozen=True)
class DtcPlan:
    """Paths the script binds to a DTC list.

    ``raw_nodes`` keeps the matched XML snippets so a caller can inspect the
    original binding without re-parsing ``DiagnosticScript.raw_xml``.
    """

    list_path: Optional[str]
    value_path: Optional[str]
    text_path: Optional[str]
    raw_nodes: Tuple[str, ...]


@dataclass(frozen=True)
class ReadRequest:
    """A request that survived the read-only filter.

    ``service_id`` is a decimal CarCom catalog key, not a raw UDS byte; see
    the module docstring. ``request`` is None when the script carried no
    request bytes, or when the bytes could not be shown to belong to a read
    service.
    """

    ecu_address: Optional[int]
    ecu_type: Optional[int]
    tester_address: Optional[int]
    service_id: int
    request: Optional[str]
    ecu_name: Optional[str] = None
    ecu_identifier: Optional[str] = None

    @property
    def service_name(self) -> str:
        """A human-readable label for ``service_id`` (a catalog key).

        From ``vida_service_templates.INFERRED_SERVICE_ROLES``, a working
        hypothesis, so this name is advisory, not confirmed.
        """

        return INFERRED_SERVICE_ROLES.get(self.service_id, "unknown")


@dataclass(frozen=True)
class DiagnosticScript:
    """The addressing and DTC-list view of one diagnostic script document."""

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
        and unknown catalog services are dropped, as is a literal request
        byte string whose own leading byte is not a known read service.

        A catalog ``ServiceId`` and a literal ``Request`` byte string are
        different number spaces (see the module docstring), so there is no
        way to verify a specific pairing between them. With exactly one
        ``ServiceId`` on a node, every safe literal request is paired with
        it (one operation, one or more request variants). With more than
        one, each is paired positionally with the request at the same
        index, since that is the only correspondence a script's own
        document order offers; a missing or unsafe request at that
        position falls back to ``None`` rather than guessing.
        """

        result: List[ReadRequest] = []
        for plan in self.ecus:
            safe_values = _safe_request_values(plan)
            service_ids = plan.service_ids
            for position, service_id in enumerate(service_ids):
                if not is_read_only_catalog_service(service_id):
                    continue
                if len(service_ids) == 1:
                    candidates = [value for value in safe_values if value is not None]
                else:
                    candidate = safe_values[position] if position < len(safe_values) else None
                    candidates = [candidate] if candidate is not None else []
                for value in candidates or (None,):
                    result.append(
                        ReadRequest(
                            ecu_address=plan.ecu_address,
                            ecu_type=plan.ecu_type,
                            tester_address=plan.tester_address,
                            service_id=service_id,
                            request=value,
                            ecu_name=plan.ecu_name,
                            ecu_identifier=plan.ecu_identifier,
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


def parse_script_zip(data: bytes) -> DiagnosticScript:
    """Parse a single-entry ZIP archive holding one diagnostic XML script."""

    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise TypeError("script archive must be bytes")
    payload = bytes(data)
    try:
        archive = zipfile.ZipFile(io.BytesIO(payload))
    except zipfile.BadZipFile as exc:
        raise DiagnosticScriptError("not a valid ZIP archive: {}".format(exc)) from exc

    with archive:
        entries = [info for info in archive.infolist() if not info.is_dir()]
        if not entries:
            raise DiagnosticScriptError("script archive contains no file entries")
        if len(entries) > 1:
            names = ", ".join(info.filename for info in entries)
            raise DiagnosticScriptError(
                "script archive must contain exactly one entry, found {}: {}".format(
                    len(entries), names
                )
            )
        entry = entries[0]
        if entry.file_size > MAX_SCRIPT_BYTES:
            raise DiagnosticScriptError(
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
            raise DiagnosticScriptError(
                "cannot read script entry {!r}: {}".format(entry.filename, exc)
            ) from exc

    if len(body) > MAX_SCRIPT_BYTES:
        raise DiagnosticScriptError(
            "script entry {!r} is larger than {} bytes".format(
                entry.filename, MAX_SCRIPT_BYTES
            )
        )

    text = _decode_script(body, entry.filename)
    try:
        return parse_script_xml(text)
    except DiagnosticScriptError as exc:
        raise DiagnosticScriptError(
            "script entry {!r}: {}".format(entry.filename, exc)
        ) from exc


def parse_script_xml(text: str) -> DiagnosticScript:
    """Parse an uncompressed diagnostic script document."""

    if not isinstance(text, str):
        raise TypeError("script XML must be str")
    if not text.strip():
        raise DiagnosticScriptError("missing root element: script document is empty")
    if _ELEMENT_START_RE.search(text) is None:
        raise DiagnosticScriptError("missing root element: no XML element found")

    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError as exc:
        raise DiagnosticScriptError("malformed XML: {}".format(exc)) from exc

    root_name = _local_name(root.tag)
    if root_name != "script":
        raise DiagnosticScriptError(
            "missing <script> root element, found <{}>".format(root_name)
        )

    return DiagnosticScript(
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


#: A sanity bound on a parsed CarCom ServiceId catalog key, not a protocol
#: limit. The largest confirmed-real value seen in the recovered corpus is
#: 12,633; this is generous headroom, not a tight fit, so a parse failure
#: above it points at a corrupted value, not a legitimate one this bound
#: rejects.
MAX_CATALOG_SERVICE_ID = 1_000_000


def _parse_catalog_service_id(value: str) -> Optional[int]:
    """Parse a VehComm ``ServiceId`` property value.

    This is a decimal CarCom catalog key (``T111_Service.id``), not a raw
    UDS service byte; see the module docstring. A ``0x``-prefixed value is
    rejected rather than silently reinterpreted, since that prefix would
    mean the source actually wrote a hex byte, which is a different,
    unconfirmed situation this function does not guess at.
    """

    text = value.strip()
    if not text or not text.isdigit():
        return None
    service_id = int(text, 10)
    if 0 <= service_id <= MAX_CATALOG_SERVICE_ID:
        return service_id
    return None


def _ecu_plans(root: ElementTree.Element) -> Iterable[EcuPlan]:
    for element in root.iter():
        if not _is_vehcomm(element):
            continue
        service_ids: List[int] = []
        for value in _field_values(element, "ServiceId"):
            service_id = _parse_catalog_service_id(value)
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
            ecu_name=_first(_field_values(element, "EcuName")),
            ecu_identifier=_first(_field_values(element, "EcuIdentifier")),
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


def _safe_request_values(plan: EcuPlan) -> Tuple[Optional[str], ...]:
    """Classify each literal request by its own leading byte.

    Returns one entry per ``plan.requests`` value, in the same order: the
    stripped request text when its own leading byte is a known read
    service, or ``None`` otherwise. The position is kept (not compacted)
    so a caller pairing requests positionally against ``plan.service_ids``
    still lines up; see :meth:`DiagnosticScript.read_only_requests`.

    There is no catalog key to cross-check a literal request's leading
    byte against: the catalog key (``ServiceId``) and the raw wire byte a
    literal ``Request`` string starts with are different number spaces
    (see the module docstring), so the only valid check here is on the
    byte that would actually be sent.
    """

    values: List[Optional[str]] = []
    for value in plan.requests:
        data = _request_bytes(value)
        if data is not None and is_read_only_service(data[0]):
            values.append(value.strip())
        else:
            values.append(None)
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
        raise DiagnosticScriptError(
            "script entry {!r} is not valid {}: {}".format(source, encoding, exc)
        ) from exc
