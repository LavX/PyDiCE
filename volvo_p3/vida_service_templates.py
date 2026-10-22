"""VIDA CarCom service request/response templates (``T111_Service``).

This module ports the mechanism that VIDA's managed diagnostic stack uses to
turn a numeric ``ServiceId`` into request bytes, recovered by static analysis
of the VIDA image (read only, nothing executed). It does not send anything to
a vehicle by itself.

## How VIDA builds a request

``BizServices.CarCom.VehicleServices.DLL`` has a ``VehicleService`` class
whose constructor calls ``IVehicleServiceDatabase.GetService`` (a stored
procedure wrapper, ``SPService.GetService``). That stored procedure is:

    CREATE PROCEDURE [dbo].[service_GetService]
    @service int
    AS
    select service, mode, [servicename] as [name], [description], definition
    from t111_service where id = @service

So the numeric id passed as ``@service`` selects one ``T111_Service`` row,
and the row's ``definition`` column is an XML template shaped like:

    <request><item name="Service" length="8" /><item name="Identifier"
    length="8" /></request><response>...</response>

``VehicleService`` stores that template in its ``_serviceXml`` field and
hands it to a ``RequestHandler``, which builds the actual bytes with a
``BinaryBuffer`` (fields ``_buf: byte[]``, ``_position: int``, methods
``AddBits``, ``GetBits``, ``AddHex``, ``ToArray``, ``ToHex``). ``AddBits``
takes a bit length, so fields are not always byte-aligned (one recovered
template packs two 4-bit sub-fields, ``Mode`` and ``IdentifierType``, into a
single byte). This confirms the template is interpreted generically: every
named ``<item>`` is packed into the output buffer at the bit length it
declares, in document order.

## What this module does and does not establish

``T111_Service`` has zero recoverable *data* rows in the imaged database
(confirmed independently by two passes, see ``docs/vida-service-templates.md``).
What survives is 189 ``definition`` values stored as LOB (text/image) pages,
carved by :func:`extract_service_templates`. SQL Server does not store a back
pointer from a LOB page to its owning row id, so **the numeric id that goes
with each recovered template is not recoverable from this image's data
pages**. :data:`INFERRED_SERVICE_ROLES` records a best-effort, only partly
confirmed hypothesis (treat it as exactly that): that the carved templates
are stored in ascending id order, which would make a template's position in
the list equal its ``T111_Service.id``. Cross-checking that hypothesis
against the recovered diagnostic-script corpus (which ``ServiceId`` scripts
actually use, and which extra fields they bind) gives 19 exact-set matches
out of 63 ids the corpus exercises, which is well above chance for a
~15-name vocabulary but far short of confirmation. Do not build a byte-exact
request sender on the index-equals-id guess without validating it against a
live VIDA session or hardware capture first.

What *is* established, independent of that open question: the shape grammar
(``<item name=... length=... type=.../>``, bit-packed in document order), and
that a template's ``Service`` item is filled from the row's own ``service``
column (the literal UDS/CarCom service byte), not from anything a script
supplies.
"""

from __future__ import annotations

import mmap
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Mapping, Optional, Sequence, Tuple
from xml.etree import ElementTree

PAGE_SIZE = 8192
PAGE_TYPE_OFFSET = 1
LOB_PAGE_TYPE = 3
OBJECT_ID_OFFSET = 24

# T111_Service's sysschobjs object id in the 2022 reference image. A
# different image can renumber it; callers that know a different id can pass
# it to extract_service_templates explicitly.
T111_SERVICE_OBJECT = 149_575_571

_TEMPLATE_RE = re.compile(rb"<request>.*?(?:</response>|<response\s*/>)", re.DOTALL)
_ASCII_TRIM_RE = re.compile(rb"[^\x09\x0a\x0d\x20-\x7e]")


class ServiceTemplateError(ValueError):
    """Raised when a template body cannot be parsed."""


@dataclass(frozen=True)
class TemplateItem:
    """One wire-format element inside a template's ``request`` or ``response``.

    ``tag`` keeps the element name (``item``, ``block``, ``fixedlist``,
    ``dynamiclist``), since the response side nests richer structures than a
    flat item list. ``length`` is in bits, matching ``BinaryBuffer.AddBits``;
    it is ``None`` for elements that carry no fixed length (for example a
    plain ``Data`` item, or a ``dynamiclist``).
    """

    tag: str
    name: Optional[str]
    length: Optional[int]
    item_type: Optional[str]
    attributes: Mapping[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, object]:
        return {
            "tag": self.tag,
            "name": self.name,
            "length": self.length,
            "type": self.item_type,
            "attributes": dict(self.attributes),
        }


@dataclass(frozen=True)
class ServiceTemplate:
    """A parsed ``T111_Service.definition`` template.

    ``index`` is the template's position in LOB storage order, carried
    verbatim; see the module docstring for why it is not treated as a
    confirmed ``T111_Service.id``.
    """

    index: int
    raw_xml: str
    request_items: Tuple[TemplateItem, ...]
    response_items: Tuple[TemplateItem, ...]

    def request_names(self, *, exclude: Sequence[str] = ("Service",)) -> Tuple[str, ...]:
        """Names of request items, in order, dropping ``exclude`` (default: ``Service``).

        ``Service`` is filled from the row's own column rather than a script
        value, so comparing the remaining names is the useful shape for
        matching a template against a corpus ``ServiceId``'s observed extra
        properties.
        """

        return tuple(item.name for item in self.request_items if item.name and item.name not in exclude)

    def to_dict(self) -> Dict[str, object]:
        return {
            "index": self.index,
            "request_items": [item.to_dict() for item in self.request_items],
            "response_items": [item.to_dict() for item in self.response_items],
        }


def _parse_int(value: Optional[str]) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value, 10)
    except ValueError:
        try:
            return int(value, 16)
        except ValueError:
            return None


def _collect_items(parent: ElementTree.Element) -> Tuple[TemplateItem, ...]:
    items: List[TemplateItem] = []
    for element in parent.iter():
        if element is parent:
            continue
        attrs = dict(element.attrib)
        name = attrs.pop("name", None)
        length = _parse_int(attrs.pop("length", None))
        item_type = attrs.pop("type", None)
        items.append(TemplateItem(tag=element.tag, name=name, length=length, item_type=item_type, attributes=attrs))
    return tuple(items)


def parse_template(raw_xml: str, index: int = -1) -> ServiceTemplate:
    """Parse one recovered ``<request>...</response>`` template body.

    The body is not a single well-formed document (it is two sibling
    elements, ``request`` and ``response``, with no shared root), so it is
    wrapped before parsing. Raises :class:`ServiceTemplateError` if it is
    not well-formed XML once wrapped.
    """

    try:
        root = ElementTree.fromstring(f"<t>{raw_xml}</t>")
    except ElementTree.ParseError as exc:
        raise ServiceTemplateError(f"template {index}: not well-formed XML: {exc}") from exc

    request = root.find("request")
    response = root.find("response")
    request_items = _collect_items(request) if request is not None else ()
    response_items = _collect_items(response) if response is not None else ()
    return ServiceTemplate(
        index=index,
        raw_xml=raw_xml,
        request_items=request_items,
        response_items=response_items,
    )


def iter_raw_templates(data: bytes) -> Iterator[str]:
    """Yield raw ``<request>...</response>`` text chunks found in ``data``.

    This is a text carve, not a row decode: SQL Server LOB pages hold the
    column bytes with no row header this module parses, so a chunk's order
    is page order, not necessarily insertion order (see the module
    docstring's caveat on id ordering).
    """

    for match in _TEMPLATE_RE.finditer(data):
        chunk = _ASCII_TRIM_RE.sub(b" ", match.group(0))
        yield chunk.decode("ascii", errors="replace")


def extract_service_templates(
    path: Path, *, object_id: int = T111_SERVICE_OBJECT
) -> List[ServiceTemplate]:
    """Carve every recovered service template from a CarComRT MDF image.

    Reads ``path`` read-only and scans only LOB pages whose header object id
    matches ``object_id``. Returns templates in page order with
    :attr:`ServiceTemplate.index` set to that order's position (0-based), not
    a confirmed database id. Templates that fail to parse are skipped and not
    counted as an index slot, so a caller using the index-as-id hypothesis
    should treat gaps as "unknown", not "id absent".
    """

    templates: List[ServiceTemplate] = []
    with path.open("rb") as handle:
        mapped = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        try:
            page_count = len(mapped) // PAGE_SIZE
            # A template can straddle a page boundary, so matching pages are
            # concatenated into contiguous runs before the regex scan, rather
            # than searched one page at a time (which would silently drop
            # every template that crosses a boundary).
            run = bytearray()
            index = 0

            def flush() -> None:
                nonlocal index
                for raw in iter_raw_templates(bytes(run)):
                    try:
                        templates.append(parse_template(raw, index=index))
                    except ServiceTemplateError:
                        continue
                    index += 1
                run.clear()

            for page_number in range(page_count):
                start = page_number * PAGE_SIZE
                page = mapped[start : start + PAGE_SIZE]
                owner = int.from_bytes(page[OBJECT_ID_OFFSET : OBJECT_ID_OFFSET + 4], "little")
                if page[PAGE_TYPE_OFFSET] == LOB_PAGE_TYPE and owner == object_id:
                    run.extend(page)
                else:
                    if run:
                        flush()
            if run:
                flush()
        finally:
            mapped.close()
    return templates


# ---------------------------------------------------------------------------
# Bit-level request/response coding
#
# BinaryBuffer.AddBits/GetBits are not decompiled (no managed decompiler is
# available in this pass, see docs/vida-service-templates.md), so the exact
# bit order is an assumption, not a confirmed fact: MSB-first within a field,
# fields packed contiguously with no padding between them, the final byte
# zero-padded on the low end. Validate against a live capture before trusting
# an encoded byte string against real hardware.
# ---------------------------------------------------------------------------


class BitWriter:
    """Packs integer fields MSB-first into a growing byte buffer.

    Mirrors ``BinaryBuffer.AddBits`` by name and by the bit-length contract
    every recovered template item uses. See the module-level note above on
    why the exact bit order is an assumption pending live validation.
    """

    def __init__(self) -> None:
        self._bits: List[int] = []

    def add_bits(self, value: int, length: int) -> None:
        if length <= 0:
            raise ValueError("length must be positive")
        if value < 0 or value >= (1 << length):
            raise ValueError(f"value {value} does not fit in {length} bits")
        for shift in range(length - 1, -1, -1):
            self._bits.append((value >> shift) & 1)

    def add_bytes(self, data: bytes) -> None:
        for byte in data:
            self.add_bits(byte, 8)

    def to_bytes(self) -> bytes:
        bits = list(self._bits)
        padding = (-len(bits)) % 8
        bits.extend([0] * padding)
        out = bytearray()
        for offset in range(0, len(bits), 8):
            byte = 0
            for bit in bits[offset : offset + 8]:
                byte = (byte << 1) | bit
            out.append(byte)
        return bytes(out)

    def __len__(self) -> int:
        return len(self._bits)


class BitReader:
    """Reads integer fields MSB-first from a byte buffer. See :class:`BitWriter`."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    @property
    def position(self) -> int:
        """Current bit position."""

        return self._position

    @property
    def remaining_bits(self) -> int:
        return len(self._data) * 8 - self._position

    def get_bits(self, length: int) -> int:
        if length <= 0:
            raise ValueError("length must be positive")
        if length > self.remaining_bits:
            raise ValueError(
                f"pos:{self._position} len:{length} value: not enough bits remaining"
            )
        value = 0
        for _ in range(length):
            byte_index, bit_index = divmod(self._position, 8)
            bit = (self._data[byte_index] >> (7 - bit_index)) & 1
            value = (value << 1) | bit
            self._position += 1
        return value


def build_request(
    template: ServiceTemplate,
    values: Mapping[str, object],
    *,
    service_byte: int,
) -> bytes:
    """Build request bytes from a template and a value for each named item.

    ``service_byte`` fills the template's own ``Service`` item (the row's
    ``service`` column in VIDA, not a script value). Every other named item
    with a fixed ``length`` is read from ``values`` by name and packed as an
    unsigned integer; an item with no fixed length (for example a bare
    ``Data`` item) is read from ``values`` as a ``bytes``-like object and
    packed byte for byte. Raises :class:`KeyError` for a missing value and
    :class:`ValueError` for a value that does not fit its declared length.

    This is the generic template interpreter VIDA's own ``RequestHandler``
    implements; it does not know which ``ServiceId`` maps to which template
    (see the module docstring), so callers must supply a template they have
    already matched to a service by other means.
    """

    writer = BitWriter()
    for item in template.request_items:
        if item.tag != "item":
            raise ServiceTemplateError(
                f"request item {item.tag!r} ({item.name!r}) is not a flat item; "
                "build_request only supports flat <item> request bodies"
            )
        if item.name == "Service":
            writer.add_bits(service_byte, item.length or 8)
            continue
        if item.name is None:
            continue
        if item.name not in values:
            raise KeyError(f"missing value for request item {item.name!r}")
        value = values[item.name]
        if item.length is None:
            writer.add_bytes(bytes(value))  # type: ignore[arg-type]
        else:
            writer.add_bits(int(value), item.length)  # type: ignore[arg-type]
    return writer.to_bytes()


# ---------------------------------------------------------------------------
# Hypothesis table: corpus-observed roles for low ServiceId values
# ---------------------------------------------------------------------------

#: Roles inferred for ServiceId values seen in the recovered diagnostic
#: script corpus, cross-referenced against the carved T111 templates and the
#: VehCom2/security-access static analysis. Every entry is a working
#: hypothesis, not a confirmed fact: see
#: ``docs/vida-service-templates.md`` for the evidence and the exact-match
#: statistic behind it. Do not use this table to build live requests without
#: independent validation.
INFERRED_SERVICE_ROLES: Dict[int, str] = {
    0: "data_record_output",
    1: "read_dtc_status",
    2: "read_ecu_identification",
    5: "erase_dtc_no_view",
    6: "erase_dtc_no_view",
    7: "unlock_request",
    8: "unlock_request",
    11: "activate_component_masked",
    13: "erase_dtc_no_view",
    14: "erase_dtc_no_view",
    17: "write_by_identifier",
    21: "relock",
    22: "disable_access",
    23: "calibration_lock_control",
    27: "session_close_or_erase_variant",
    29: "erase_dtc_no_view",
    100: "status_version_check",
    101: "read_dtc_status",
    102: "erase_dtc_no_view",
    105: "security_access_with_key",
    106: "extended_diagnostic_session",
    109: "io_control_reset_adaption",
    112: "default_diagnostic_session",
    114: "security_access_with_key",
    116: "activate_or_write_parameter",
    118: "security_access_with_key",
    119: "activate_or_write_parameter",
    12516: "freeze_frame_or_dro_readout",
}


#: Catalog ids (``T111_Service.id``, the ``ServiceId`` property value) with
#: strong, multi-source evidence of being a pure read: no vehicle state
#: change. This mirrors the narrow, conservative spirit of
#: ``volvo_p3.script_plan.READ_ONLY_SERVICES`` (the four canonical raw UDS
#: read SIDs), but for the catalog-key number space instead of a raw wire
#: byte; see the module docstring for why those are not the same thing.
#:
#: An id's absence here means "not confirmed safe", not "confirmed unsafe".
#: ``12516`` ("freeze_frame_or_dro_readout") is a plausible read too, but is
#: left out deliberately: its evidence is weaker (see
#: ``INFERRED_SERVICE_ROLES``) and the safe default when unsure is to
#: exclude, not include.
READ_ONLY_CATALOG_SERVICES: frozenset = frozenset({0, 1, 2, 101})


def classify_catalog_service(service_id: int) -> str:
    """Classify a CarCom ``ServiceId`` catalog key (``T111_Service.id``).

    Returns ``"read_only"`` for an id in :data:`READ_ONLY_CATALOG_SERVICES`,
    the matching label from :data:`INFERRED_SERVICE_ROLES` for a recognized
    non-read id, or ``"unknown"`` otherwise.

    This is the catalog-key counterpart to
    ``volvo_p3.script_plan.classify_service``, which classifies a raw UDS
    service byte (0-255), such as a literal request's own first byte. Do
    not use one in place of the other; see the module docstring.
    """

    if service_id in READ_ONLY_CATALOG_SERVICES:
        return "read_only"
    return INFERRED_SERVICE_ROLES.get(service_id, "unknown")


def is_read_only_catalog_service(service_id: int) -> bool:
    """Return True only for an id in :data:`READ_ONLY_CATALOG_SERVICES`."""

    return service_id in READ_ONLY_CATALOG_SERVICES
