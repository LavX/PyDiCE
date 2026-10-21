"""Guarded, read-only execution of diagnostic scripts.

A diagnostic script names a logical ``EcuAddress`` or ``EcuType``; it does not carry
the physical CAN identifier of the module. This module never guesses that
translation. The caller supplies an :class:`AddressMap`, and a request whose
logical address has no entry is reported as ``skipped_unmapped``.

An :class:`AddressMap` can also hold name entries. They are only populated from
an explicit saved profile, matched against the script's ``EcuName`` or
``EcuIdentifier`` by exact normalized text. An ambiguous or absent name stays
unmapped and is reported; nothing is fuzzy-matched.

Safety rules enforced here, not trusted from the script:

* Only the four read services from :mod:`volvo_p3.script_plan` (0x01, 0x09,
  0x19, 0x22) can become frames. ``build_run_plan`` reads requests exclusively
  through ``DiagnosticScript.read_only_requests`` and ``run_plan`` re-checks every
  service byte immediately before ``send``.
* No diagnostic session control, security access, ECU reset, keepalive,
  communication control, routine control, clear or write service is ever sent.
* The only non-request frame this module can send is an ISO-TP flow-control
  frame (``30 00 00 00 00 00 00 00``) after a first-frame response, which is
  required to read a multi-frame answer.
* ``run_plan(..., dry_run=True)`` never touches the transport.
* The runner does not know or invent response identifiers. It accepts a
  response when its 11-bit CAN ID differs from the request ID and its UDS
  service bytes answer the request. The observed identifier is recorded.

The wire format for a request is a single ISO-TP frame on the mapped 11-bit
physical CAN identifier, for example ``03 19 02 FF 00 00 00 00``. It is not
the Volvo proprietary 0x000FFFFE format; :class:`~volvo_p3.protocol.VolvoFrame`
is reused only as a plain CAN frame container.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .protocol import VolvoFrame
from .script_plan import (
    NON_READ_SERVICES,
    READ_SERVICE_NAMES,
    ReadRequest,
    DiagnosticScript,
    is_read_only_service,
)


MAX_CAN_ID = 0x7FF
FUNCTIONAL_REQUEST_ID = 0x7DF
MAX_SINGLE_FRAME_BYTES = 7
MAX_ISOTP_PAYLOAD = 4095
MAX_RESPONSE_PENDING_WAIT = 5.0
MAX_TRANSFER_WAIT = 10.0
CONSECUTIVE_FRAME_TIMEOUT = 1.0
FLOW_CONTROL_DATA = bytes.fromhex("30 00 00 00 00 00 00 00")

STATUS_PLANNED = "planned"
STATUS_SENT = "sent"
STATUS_RESPONSE = "response"
STATUS_TIMEOUT = "timeout"
STATUS_SKIPPED_UNMAPPED = "skipped_unmapped"
STATUS_SKIPPED_NON_READ = "skipped_non_read"
STATUS_SKIPPED_UNSUPPORTED = "skipped_unsupported"
STATUS_ERROR = "error"

STATUSES = (
    STATUS_PLANNED,
    STATUS_SENT,
    STATUS_RESPONSE,
    STATUS_TIMEOUT,
    STATUS_SKIPPED_UNMAPPED,
    STATUS_SKIPPED_NON_READ,
    STATUS_SKIPPED_UNSUPPORTED,
    STATUS_ERROR,
)

NAME_MATCHED = "matched"
NAME_AMBIGUOUS = "ambiguous"
NAME_MISSING = "missing"
NAME_UNNAMED = "unnamed"

NAME_STATUSES = (NAME_MATCHED, NAME_AMBIGUOUS, NAME_MISSING, NAME_UNNAMED)

DTC_STATUS_BITS = (
    "testFailed",
    "testFailedThisOperationCycle",
    "pendingDTC",
    "confirmedDTC",
    "testNotCompletedSinceLastClear",
    "testFailedSinceLastClear",
    "testNotCompletedThisOperationCycle",
    "warningIndicatorRequested",
)

NRC_NAMES = {
    0x10: "generalReject",
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x21: "busyRepeatRequest",
    0x22: "conditionsNotCorrect",
    0x24: "requestSequenceError",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x35: "invalidKey",
    0x36: "exceedNumberOfAttempts",
    0x37: "requiredTimeDelayNotExpired",
    0x70: "uploadDownloadNotAccepted",
    0x71: "transferDataSuspended",
    0x72: "generalProgrammingFailure",
    0x78: "responsePending",
    0x7E: "subFunctionNotSupportedInActiveSession",
    0x7F: "serviceNotSupportedInActiveSession",
}


class AddressMapError(ValueError):
    """Raised when an address map cannot be parsed or is unsafe."""


@dataclass(frozen=True)
class AddressMatch:
    """Result of resolving one script node against an :class:`AddressMap`."""

    logical_address: Optional[int]
    logical_kind: Optional[str]
    can_id: Optional[int]

    @property
    def mapped(self) -> bool:
        return self.can_id is not None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "logical_address": self.logical_address,
            "logical_address_hex": _optional_hex(self.logical_address),
            "logical_kind": self.logical_kind,
            "can_id": self.can_id,
            "can_id_hex": _optional_can_id(self.can_id),
            "mapped": self.mapped,
        }


class AddressMap:
    """Explicit logical-address to 11-bit physical CAN request ID mapping.

    Keys are the integer values the source writes in an ``EcuAddress`` or ``EcuType``
    property. A script node that has both is resolved by ``EcuAddress`` first,
    then ``EcuType``. Nothing is derived from the script itself.

    ``names`` holds optional entries matched from a saved module profile. They
    are consulted only after both numeric keys miss, and only for an exact
    normalized match. The numeric entries always win.
    """

    def __init__(
        self,
        addresses: Optional[Mapping[Any, Any]] = None,
        names: Optional[Mapping[Any, Any]] = None,
    ):
        self._entries: Dict[int, int] = {}
        self._names: Dict[str, int] = {}
        if addresses is not None:
            self._load_addresses(addresses)
        if names is not None:
            self._load_names(names)

    def _load_addresses(self, addresses: Mapping[Any, Any]) -> None:
        if not isinstance(addresses, Mapping):
            raise AddressMapError(
                "address map must be a JSON object of logical address to CAN ID"
            )
        for logical, can_id in addresses.items():
            logical_value = _parse_integer(logical, "logical address")
            if logical_value < 0:
                raise AddressMapError(
                    "logical address must not be negative: {!r}".format(logical)
                )
            can_value = _parse_integer(
                can_id,
                "CAN request ID for logical address {}".format(logical_value),
            )
            self._check_can_id(can_value, "logical address {}".format(logical_value))
            if logical_value in self._entries and self._entries[logical_value] != can_value:
                raise AddressMapError(
                    "logical address {} is mapped to both 0x{:03X} and 0x{:03X}".format(
                        logical_value,
                        self._entries[logical_value],
                        can_value,
                    )
                )
            self._entries[logical_value] = can_value

    def _load_names(self, names: Mapping[Any, Any]) -> None:
        if not isinstance(names, Mapping):
            raise AddressMapError(
                "address names must be a JSON object of ECU name to CAN ID"
            )
        for name, can_id in names.items():
            if not isinstance(name, str):
                raise AddressMapError(
                    "ECU name must be a string, got {!r}".format(name)
                )
            key = normalize_ecu_name(name)
            if not key:
                raise AddressMapError("ECU name must not be empty")
            can_value = _parse_integer(
                can_id, "CAN request ID for ECU name {!r}".format(name)
            )
            self._check_can_id(can_value, "ECU name {!r}".format(name))
            if key in self._names and self._names[key] != can_value:
                raise AddressMapError(
                    "ECU name {!r} is mapped to both 0x{:03X} and 0x{:03X}".format(
                        name, self._names[key], can_value
                    )
                )
            self._names[key] = can_value

    @staticmethod
    def _check_can_id(can_value: int, what: str) -> None:
        if not 0 <= can_value <= MAX_CAN_ID:
            raise AddressMapError(
                "CAN request ID {:#05x} for {} is outside "
                "the 11-bit range 0x000..0x7FF".format(can_value, what)
            )
        if can_value == FUNCTIONAL_REQUEST_ID:
            raise AddressMapError(
                "CAN request ID 0x7DF is functional broadcast; {} needs a "
                "physical request ID".format(what)
            )

    @classmethod
    def from_mapping(cls, addresses: Mapping[Any, Any]) -> "AddressMap":
        return cls(addresses)

    def merged_with(self, other: "AddressMap") -> "AddressMap":
        """Return a new map; ``other`` fills gaps and conflicts raise."""

        if not isinstance(other, AddressMap):
            raise TypeError("other must be an AddressMap")
        addresses: Dict[int, int] = dict(self._entries)
        addresses.update(other._entries)
        names: Dict[str, int] = dict(self._names)
        names.update(other._names)
        return AddressMap(addresses, names)

    def resolve(
        self,
        ecu_address: Optional[int],
        ecu_type: Optional[int],
        ecu_name: Optional[str] = None,
        ecu_identifier: Optional[str] = None,
    ) -> AddressMatch:
        candidates: List[Tuple[int, str]] = []
        if ecu_address is not None:
            candidates.append((ecu_address, "ecu_address"))
        if ecu_type is not None:
            candidates.append((ecu_type, "ecu_type"))
        for logical, kind in candidates:
            if logical in self._entries:
                return AddressMatch(logical, kind, self._entries[logical])
        for value, kind in (
            (ecu_name, "ecu_name"),
            (ecu_identifier, "ecu_identifier"),
        ):
            key = normalize_ecu_name(value)
            if key and key in self._names:
                return AddressMatch(None, kind, self._names[key])
        if candidates:
            logical, kind = candidates[0]
            return AddressMatch(logical, kind, None)
        for value, kind in (
            (ecu_name, "ecu_name"),
            (ecu_identifier, "ecu_identifier"),
        ):
            if value and value.strip():
                return AddressMatch(None, kind, None)
        return AddressMatch(None, None, None)

    def get(self, logical_address: int) -> Optional[int]:
        return self._entries.get(logical_address)

    def get_name(self, name: str) -> Optional[int]:
        return self._names.get(normalize_ecu_name(name))

    def __contains__(self, logical_address: object) -> bool:
        return logical_address in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def __iter__(self):
        return iter(sorted(self._entries))

    def items(self) -> Tuple[Tuple[int, int], ...]:
        return tuple(sorted(self._entries.items()))

    def name_items(self) -> Tuple[Tuple[str, int], ...]:
        return tuple(sorted(self._names.items()))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "entries": [
                {
                    "logical_address": logical,
                    "logical_address_hex": _optional_hex(logical),
                    "can_id": can_id,
                    "can_id_hex": _optional_can_id(can_id),
                }
                for logical, can_id in self.items()
            ],
            "names": [
                {
                    "name": name,
                    "can_id": can_id,
                    "can_id_hex": _optional_can_id(can_id),
                }
                for name, can_id in self.name_items()
            ],
        }


def normalize_ecu_name(value: Optional[str]) -> str:
    """Fold an ECU name for exact comparison.

    Case is folded and every character that is not a letter or digit is
    removed; no aliases or partial matches are applied. ``"D.I.M."`` and
    ``"dim"`` both become ``"DIM"``, while ``"Driver Information Module"`` does
    not.
    """

    if not value:
        return ""
    return "".join(char for char in str(value).upper() if char.isalnum())


def parse_address_map(document: Any) -> AddressMap:
    """Parse the documented JSON shapes into an :class:`AddressMap`.

    Both ``{"addresses": {"0x11": "0x7E0"}}`` and a flat
    ``{"0x11": "0x7E0"}`` are accepted. Keys and values accept a ``0x``
    prefix, plain decimal, and unprefixed hexadecimal when they contain a
    letter (``7E0``). A key without a prefix is decimal, matching the way the source
    writes ``EcuAddress`` and ``EcuType``.
    """

    if not isinstance(document, Mapping):
        raise AddressMapError(
            "address map must be a JSON object, got {}".format(
                type(document).__name__
            )
        )
    if "addresses" in document:
        addresses = document["addresses"]
        if not isinstance(addresses, Mapping):
            raise AddressMapError('"addresses" must be a JSON object')
    else:
        addresses = document
    return AddressMap(addresses)


def _parse_integer(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AddressMapError(
            "{} must be an integer or a numeric string, got {!r}".format(
                what, value
            )
        )
    if isinstance(value, int):
        return value
    text = value.strip()
    if not text:
        raise AddressMapError("{} is empty".format(what))
    if text.startswith("-"):
        raise AddressMapError("{} must not be negative: {!r}".format(what, value))
    if text.startswith("+"):
        text = text[1:]
    lowered = text.lower()
    if lowered.startswith("0x"):
        digits = text[2:]
        base = 16
    elif all(char in "0123456789" for char in text):
        digits = text
        base = 10
    else:
        digits = text
        base = 16
    if not digits:
        raise AddressMapError("{} is empty".format(what))
    try:
        return int(digits, base)
    except ValueError as exc:
        raise AddressMapError(
            "{} is not a valid number: {!r}".format(what, value)
        ) from exc


@dataclass(frozen=True)
class NameMapEntry:
    """One script ECU considered against a saved profile's module names.

    ``status`` is one of :data:`NAME_STATUSES`. ``field`` names the script
    property the compared text came from (``ecu_name`` or ``ecu_identifier``),
    or None when the node carries neither.
    """

    status: str
    value: Optional[str]
    normalized: str
    field: Optional[str] = None
    can_id: Optional[int] = None
    profile_names: Tuple[str, ...] = ()
    detail: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "field": self.field,
            "value": self.value,
            "normalized": self.normalized,
            "can_id": self.can_id,
            "can_id_hex": _optional_can_id(self.can_id),
            "profile_names": list(self.profile_names),
            "detail": self.detail,
        }


@dataclass(frozen=True)
class NameMapReport:
    """Outcome of matching every script ECU name against one profile."""

    entries: Tuple[NameMapEntry, ...] = ()

    def _with_status(self, status: str) -> Tuple[NameMapEntry, ...]:
        return tuple(entry for entry in self.entries if entry.status == status)

    @property
    def matched(self) -> Tuple[NameMapEntry, ...]:
        return self._with_status(NAME_MATCHED)

    @property
    def ambiguous(self) -> Tuple[NameMapEntry, ...]:
        return self._with_status(NAME_AMBIGUOUS)

    @property
    def missing(self) -> Tuple[NameMapEntry, ...]:
        return self._with_status(NAME_MISSING)

    @property
    def unnamed(self) -> Tuple[NameMapEntry, ...]:
        return self._with_status(NAME_UNNAMED)

    def counts(self) -> Dict[str, int]:
        counts = {status: 0 for status in NAME_STATUSES}
        for entry in self.entries:
            counts[entry.status] = counts.get(entry.status, 0) + 1
        counts["ecus"] = len(self.entries)
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "counts": self.counts(),
            "entries": [entry.to_dict() for entry in self.entries],
        }


def _profile_name_index(
    targets: Iterable[Any],
) -> Dict[str, List[Tuple[str, int]]]:
    """Group profile modules by normalized name, keeping every original."""

    index: Dict[str, List[Tuple[str, int]]] = {}
    for target in targets:
        name = getattr(target, "name", None)
        request_id = getattr(target, "request_id", None)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("profile module names must not be empty")
        if isinstance(request_id, bool) or not isinstance(request_id, int):
            raise ValueError(
                "profile module {!r} has no integer request ID".format(name)
            )
        key = normalize_ecu_name(name)
        if not key:
            raise ValueError("profile module {!r} has an empty name".format(name))
        entries = index.setdefault(key, [])
        if all(entry[1] != request_id for entry in entries):
            entries.append((name.strip(), request_id))
    return index


def build_profile_name_map(
    script: DiagnosticScript, targets: Iterable[Any]
) -> Tuple[AddressMap, NameMapReport]:
    """Match script ECU names to saved-profile module names.

    Only exact normalized text counts. A name that two profile modules claim
    with different request IDs is ambiguous; it is not mapped. A name with no
    profile entry is reported as missing. ``EcuName`` is tried first, then
    ``EcuIdentifier``.

    Returns a name-only :class:`AddressMap` and the report. Merge the map into
    an explicit numeric map with :meth:`AddressMap.merged_with`; the numeric
    entries still resolve first.
    """

    if not isinstance(script, DiagnosticScript):
        raise TypeError("script must be a DiagnosticScript")

    index = _profile_name_index(targets)
    names: Dict[str, int] = {}
    entries: List[NameMapEntry] = []

    for plan in script.ecus:
        candidates: List[Tuple[str, str]] = []
        if plan.ecu_name and plan.ecu_name.strip():
            candidates.append((plan.ecu_name.strip(), "ecu_name"))
        if plan.ecu_identifier and plan.ecu_identifier.strip():
            candidates.append((plan.ecu_identifier.strip(), "ecu_identifier"))

        if not candidates:
            entries.append(
                NameMapEntry(
                    status=NAME_UNNAMED,
                    value=None,
                    normalized="",
                    detail="script node carries no EcuName or EcuIdentifier",
                )
            )
            continue

        matched: Optional[NameMapEntry] = None
        ambiguous: Optional[NameMapEntry] = None
        for value, field in candidates:
            key = normalize_ecu_name(value)
            profile_entries = index.get(key, [])
            if not profile_entries:
                continue
            unique_ids = {request_id for _, request_id in profile_entries}
            if len(unique_ids) == 1:
                can_id = next(iter(unique_ids))
                matched = NameMapEntry(
                    status=NAME_MATCHED,
                    value=value,
                    normalized=key,
                    field=field,
                    can_id=can_id,
                    profile_names=tuple(name for name, _ in profile_entries),
                    detail="matched {}".format(field),
                )
                break
            if ambiguous is None:
                ambiguous = NameMapEntry(
                    status=NAME_AMBIGUOUS,
                    value=value,
                    normalized=key,
                    field=field,
                    profile_names=tuple(name for name, _ in profile_entries),
                    detail="profile has {} modules named {!r} with different "
                    "request IDs".format(len(unique_ids), key),
                )

        if matched is not None and matched.can_id is not None:
            names[matched.normalized] = matched.can_id
            entries.append(matched)
        elif ambiguous is not None:
            entries.append(ambiguous)
        else:
            value, field = candidates[0]
            entries.append(
                NameMapEntry(
                    status=NAME_MISSING,
                    value=value,
                    normalized=normalize_ecu_name(value),
                    field=field,
                    detail="no profile module named {!r}".format(value),
                )
            )

    return AddressMap(names=names), NameMapReport(tuple(entries))


def load_profile_name_map(
    script: DiagnosticScript, profile_path: Any
) -> Tuple[AddressMap, NameMapReport]:
    """Load a saved module profile and match it to one script.

    A missing or unreadable profile raises ``ValueError`` with the path in the
    message, so the CLI and the TUI can report it the same way.
    """

    from .module_scan import load_module_profile

    path = Path(profile_path)
    try:
        targets = load_module_profile(path)
    except FileNotFoundError as exc:
        raise ValueError("module profile not found: {}".format(path)) from exc
    except (OSError, ValueError) as exc:
        raise ValueError("invalid module profile {}: {}".format(path, exc)) from exc
    return build_profile_name_map(script, targets)


@dataclass(frozen=True)
class LogicalAddress:
    """One unmapped script logical address, reported instead of guessed."""

    kind: str
    value: Optional[int]
    source_ecu: str
    name: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "value": self.value,
            "value_hex": _optional_hex(self.value),
            "source_ecu": self.source_ecu,
            "name": self.name,
        }


@dataclass(frozen=True)
class RequestRecord:
    """One request and, after a run, its outcome.

    ``request_bytes`` is the UDS payload, not the ISO-TP frame. ``status`` is
    one of :data:`STATUSES`. ``transmitted`` is True only after the transport
    accepted the frame.
    """

    index: int
    source_ecu: str
    service_id: int
    request_bytes: bytes
    status: str
    can_id: Optional[int] = None
    logical_address: Optional[int] = None
    logical_kind: Optional[str] = None
    ecu_address: Optional[int] = None
    ecu_type: Optional[int] = None
    tester_address: Optional[int] = None
    ecu_name: Optional[str] = None
    ecu_identifier: Optional[str] = None
    detail: Optional[str] = None
    transmitted: bool = False
    response_id: Optional[int] = None
    response_bytes: Optional[bytes] = None
    response_payload: Optional[bytes] = None
    decoded: Optional[Dict[str, Any]] = None

    @property
    def service_name(self) -> str:
        if self.service_id in READ_SERVICE_NAMES:
            return READ_SERVICE_NAMES[self.service_id]
        if self.service_id in NON_READ_SERVICES:
            return NON_READ_SERVICES[self.service_id]
        return "unknown"

    @property
    def request_hex(self) -> str:
        return _hex_bytes(self.request_bytes)

    @property
    def response_payload_hex(self) -> Optional[str]:
        if self.response_payload is None:
            return None
        return _hex_bytes(self.response_payload)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "source_ecu": self.source_ecu,
            "logical_address": self.logical_address,
            "logical_address_hex": _optional_hex(self.logical_address),
            "logical_kind": self.logical_kind,
            "ecu_address": self.ecu_address,
            "ecu_address_hex": _optional_hex(self.ecu_address),
            "ecu_type": self.ecu_type,
            "tester_address": self.tester_address,
            "tester_address_hex": _optional_hex(self.tester_address),
            "ecu_name": self.ecu_name,
            "ecu_identifier": self.ecu_identifier,
            "service_id": self.service_id,
            "service_hex": "0x{:02X}".format(self.service_id),
            "service_name": self.service_name,
            "request": self.request_hex,
            "request_bytes": list(self.request_bytes),
            "can_id": self.can_id,
            "can_id_hex": _optional_can_id(self.can_id),
            "status": self.status,
            "detail": self.detail,
            "transmitted": self.transmitted,
            "response_id": self.response_id,
            "response_id_hex": _optional_can_id(self.response_id),
            "response_bytes": (
                None if self.response_bytes is None else _hex_bytes(self.response_bytes)
            ),
            "response_payload": self.response_payload_hex,
            "decoded": self.decoded,
        }


@dataclass(frozen=True)
class RunPlan:
    """The read-only requests a script would produce, with their mapping."""

    docno: Optional[str]
    title: Optional[str]
    script_type: Optional[str]
    records: Tuple[RequestRecord, ...]
    unmapped: Tuple[LogicalAddress, ...] = ()

    @property
    def planned(self) -> Tuple[RequestRecord, ...]:
        return tuple(
            record for record in self.records if record.status == STATUS_PLANNED
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "docno": self.docno,
            "title": self.title,
            "script_type": self.script_type,
            "request_count": len(self.records),
            "counts": _status_counts(self.records),
            "records": [record.to_dict() for record in self.records],
            "unmapped": [item.to_dict() for item in self.unmapped],
        }


@dataclass(frozen=True)
class RunResult:
    """Per-request outcomes from :func:`run_plan`."""

    records: Tuple[RequestRecord, ...]
    dry_run: bool
    transport_touched: bool
    aborted: bool = False
    abort_reason: Optional[str] = None

    @property
    def counts(self) -> Dict[str, int]:
        counts = _status_counts(self.records)
        counts["records"] = len(self.records)
        counts["transmitted"] = sum(
            1 for record in self.records if record.transmitted
        )
        return counts

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "transport_touched": self.transport_touched,
            "aborted": self.aborted,
            "abort_reason": self.abort_reason,
            "counts": self.counts,
            "records": [record.to_dict() for record in self.records],
        }


def build_run_plan(script: DiagnosticScript, address_map: AddressMap) -> RunPlan:
    """Build a plan from ``script.read_only_requests()`` only.

    Every request keeps the script's logical address. A request with no
    address-map entry is marked ``skipped_unmapped`` and recorded once in
    ``RunPlan.unmapped``. Nothing is guessed.
    """

    if not isinstance(address_map, AddressMap):
        if isinstance(address_map, Mapping):
            address_map = AddressMap(address_map)
        else:
            raise TypeError("address_map must be an AddressMap or a mapping")

    records: List[RequestRecord] = []
    unmapped: List[LogicalAddress] = []
    seen_unmapped = set()

    for index, request in enumerate(script.read_only_requests(), 1):
        match = address_map.resolve(
            request.ecu_address,
            request.ecu_type,
            request.ecu_name,
            request.ecu_identifier,
        )
        request_bytes, byte_note = _request_bytes(request)
        source_ecu = _source_label(
            request.ecu_address,
            request.ecu_type,
            request.ecu_name,
            request.ecu_identifier,
        )
        detail_parts: List[str] = []

        if not request_bytes:
            status = STATUS_SKIPPED_UNSUPPORTED
            detail_parts.append(
                byte_note or "script supplied no request bytes"
            )
        elif not is_read_only_service(request.service_id):
            status = STATUS_SKIPPED_NON_READ
            detail_parts.append(
                "service 0x{:02X} is not a read service".format(
                    request.service_id
                )
            )
        elif not is_read_only_service(request_bytes[0]):
            status = STATUS_SKIPPED_NON_READ
            detail_parts.append(
                "request byte 0x{:02X} is not a read service".format(
                    request_bytes[0]
                )
            )
        elif request_bytes[0] != request.service_id:
            status = STATUS_SKIPPED_UNSUPPORTED
            detail_parts.append(
                "request service byte 0x{:02X} disagrees with declared "
                "service 0x{:02X}".format(request_bytes[0], request.service_id)
            )
        elif match.can_id is None:
            status = STATUS_SKIPPED_UNMAPPED
            detail_parts.append(
                "no address map entry for {}".format(source_ecu)
            )
            key = (
                match.logical_kind,
                match.logical_address,
                request.ecu_name,
                request.ecu_identifier,
            )
            if key not in seen_unmapped:
                seen_unmapped.add(key)
                unmapped.append(
                    LogicalAddress(
                        kind=match.logical_kind or "unknown",
                        value=match.logical_address,
                        source_ecu=source_ecu,
                        name=request.ecu_name or request.ecu_identifier,
                    )
                )
        elif len(request_bytes) > MAX_SINGLE_FRAME_BYTES:
            status = STATUS_SKIPPED_UNSUPPORTED
            detail_parts.append(
                "request is {} bytes; multi-frame transmit is not implemented".format(
                    len(request_bytes)
                )
            )
        else:
            status = STATUS_PLANNED

        if byte_note:
            detail_parts.append(byte_note)

        records.append(
            RequestRecord(
                index=index,
                source_ecu=source_ecu,
                service_id=request.service_id,
                request_bytes=request_bytes,
                status=status,
                can_id=match.can_id,
                logical_address=match.logical_address,
                logical_kind=match.logical_kind,
                ecu_address=request.ecu_address,
                ecu_type=request.ecu_type,
                tester_address=request.tester_address,
                ecu_name=request.ecu_name,
                ecu_identifier=request.ecu_identifier,
                detail="; ".join(detail_parts) or None,
            )
        )

    return RunPlan(
        docno=script.docno,
        title=script.title,
        script_type=script.script_type,
        records=tuple(records),
        unmapped=tuple(unmapped),
    )


def run_plan(
    plan: RunPlan,
    transport: Any,
    timeout: float = 1.5,
    dry_run: bool = False,
) -> RunResult:
    """Send mapped read requests one at a time and collect their responses.

    ``transport`` only needs ``send(frame)`` and ``recv(timeout)``. The dry-run
    path returns the plan's records without touching it. Timeouts are recorded
    per request and the run continues. A rejected frame (``ValueError`` from
    ``send``) is recorded and the run continues because nothing was sent; a
    broken transport (``OSError``/``RuntimeError``) aborts the remaining work.
    """

    if not isinstance(plan, RunPlan):
        raise TypeError("plan must be a RunPlan")
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive number of seconds")
    if dry_run:
        return RunResult(
            records=plan.records,
            dry_run=True,
            transport_touched=False,
        )
    if transport is None:
        raise ValueError("transport is required unless dry_run is true")

    records: List[RequestRecord] = []
    aborted = False
    abort_reason: Optional[str] = None
    touched = False

    for record in plan.records:
        if record.status != STATUS_PLANNED:
            records.append(record)
            continue
        if not is_read_only_service(record.service_id):
            records.append(
                replace(
                    record,
                    status=STATUS_SKIPPED_NON_READ,
                    detail="service 0x{:02X} is not a read service".format(
                        record.service_id
                    ),
                )
            )
            continue
        if (
            not record.request_bytes
            or not is_read_only_service(record.request_bytes[0])
        ):
            records.append(
                replace(
                    record,
                    status=STATUS_SKIPPED_NON_READ,
                    detail="request bytes are empty or are not a read service",
                )
            )
            continue
        if record.request_bytes[0] != record.service_id:
            records.append(
                replace(
                    record,
                    status=STATUS_SKIPPED_UNSUPPORTED,
                    detail="request service byte 0x{:02X} disagrees with "
                    "declared service 0x{:02X}".format(
                        record.request_bytes[0], record.service_id
                    ),
                )
            )
            continue
        if record.can_id is None:
            records.append(
                replace(
                    record,
                    status=STATUS_SKIPPED_UNMAPPED,
                    detail="no address map entry for {}".format(record.source_ecu),
                )
            )
            continue
        if not 0 <= record.can_id <= MAX_CAN_ID:
            records.append(
                replace(
                    record,
                    status=STATUS_SKIPPED_UNSUPPORTED,
                    detail="CAN request ID is outside the 11-bit range",
                )
            )
            continue
        if record.can_id == FUNCTIONAL_REQUEST_ID:
            records.append(
                replace(
                    record,
                    status=STATUS_SKIPPED_UNSUPPORTED,
                    detail="0x7DF is functional broadcast, not a physical "
                    "request ID",
                )
            )
            continue
        if len(record.request_bytes) > MAX_SINGLE_FRAME_BYTES:
            records.append(
                replace(
                    record,
                    status=STATUS_SKIPPED_UNSUPPORTED,
                    detail="request is {} bytes; multi-frame transmit is not "
                    "implemented".format(len(record.request_bytes)),
                )
            )
            continue

        frame = _build_request_frame(record.can_id, record.request_bytes)
        touched = True
        try:
            transport.send(frame)
        except TimeoutError as exc:
            records.append(
                replace(
                    record,
                    status=STATUS_TIMEOUT,
                    detail="transport send timed out: {}".format(exc),
                )
            )
            continue
        except ValueError as exc:
            records.append(
                replace(
                    record,
                    status=STATUS_ERROR,
                    detail="transport refused request: {}".format(exc),
                )
            )
            continue
        except (OSError, RuntimeError) as exc:
            records.append(
                replace(
                    record,
                    status=STATUS_ERROR,
                    detail="transport send failed: {}".format(exc),
                )
            )
            aborted = True
            abort_reason = "send failed: {}".format(exc)
            break

        sent = replace(record, status=STATUS_SENT, transmitted=True)
        try:
            response = _receive_response(
                transport,
                request_id=record.can_id,
                service_id=record.service_id,
                request_bytes=record.request_bytes,
                timeout=timeout,
            )
        except TimeoutError:
            response = None
        except ValueError as exc:
            records.append(
                replace(
                    sent,
                    status=STATUS_ERROR,
                    detail="response read rejected after transmit: {}".format(exc),
                )
            )
            aborted = True
            abort_reason = "receive rejected: {}".format(exc)
            break
        except (OSError, RuntimeError) as exc:
            records.append(
                replace(
                    sent,
                    detail="response read failed after transmit: {}".format(exc),
                )
            )
            aborted = True
            abort_reason = "receive failed: {}".format(exc)
            break

        if response is None:
            records.append(
                replace(
                    sent,
                    status=STATUS_TIMEOUT,
                    detail="no matching response within {:g}s".format(timeout),
                )
            )
            continue

        records.append(
            replace(
                sent,
                status=STATUS_RESPONSE,
                response_id=response.response_id,
                response_bytes=response.frame_data,
                response_payload=response.payload,
                decoded=decode_response_payload(
                    record.service_id, response.payload
                ),
            )
        )

    return RunResult(
        records=tuple(records),
        dry_run=False,
        transport_touched=touched,
        aborted=aborted,
        abort_reason=abort_reason,
    )


@dataclass(frozen=True)
class _Response:
    response_id: int
    frame_data: bytes
    payload: bytes


def _build_request_frame(can_id: int, request_bytes: bytes) -> VolvoFrame:
    if not request_bytes:
        raise ValueError("request bytes are empty")
    if not is_read_only_service(request_bytes[0]):
        raise ValueError(
            "refusing to send non-read service 0x{:02X}".format(
                request_bytes[0]
            )
        )
    if len(request_bytes) > MAX_SINGLE_FRAME_BYTES:
        raise ValueError("single-frame request cannot exceed 7 bytes")
    data = bytes([len(request_bytes)]) + bytes(request_bytes)
    return VolvoFrame(can_id, data.ljust(8, b"\x00"), False)


def _receive_response(
    transport: Any,
    *,
    request_id: int,
    service_id: int,
    request_bytes: bytes,
    timeout: float,
) -> Optional[_Response]:
    """Collect one response, ignoring unrelated frames and assembling ISO-TP.

    Responses are matched on UDS bytes, not on a guessed response identifier.
    A single frame or a first-frame/consecutive-frame transfer is supported.
    """

    started = time.monotonic()
    deadline = started + timeout
    pending_deadline = started + MAX_RESPONSE_PENDING_WAIT
    transfer_deadline: Optional[float] = None
    total_length: Optional[int] = None
    assembled = bytearray()
    expected_sequence = 1
    first_frame_data: Optional[bytes] = None

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        frame = transport.recv(timeout=remaining)
        if frame is None:
            time.sleep(min(0.001, max(0.0, deadline - time.monotonic())))
            continue

        data = bytes(frame.data)
        response_id = frame.arbitration_id
        if not data or response_id == request_id:
            continue
        if not 0 <= response_id <= MAX_CAN_ID:
            continue

        pci_type = data[0] >> 4
        if pci_type == 0:
            payload_length = data[0] & 0x0F
            if payload_length == 0 or payload_length > len(data) - 1:
                continue
            payload = data[1 : 1 + payload_length]
            if _is_response_pending(payload, service_id):
                deadline = min(pending_deadline, time.monotonic() + timeout)
                continue
            if _response_matches(service_id, request_bytes, payload):
                return _Response(response_id, data, payload)
            continue

        if pci_type == 1:
            if len(data) != 8:
                continue
            length = ((data[0] & 0x0F) << 8) | data[1]
            header_length = 2
            if length == 0:
                if len(data) < 6:
                    continue
                length = int.from_bytes(data[2:6], "big")
                header_length = 6
            if length == 0 or length > MAX_ISOTP_PAYLOAD:
                continue
            initial = data[header_length:]
            if length <= len(initial):
                continue
            total_length = length
            assembled = bytearray(initial)
            expected_sequence = 1
            first_frame_data = data
            transfer_deadline = time.monotonic() + MAX_TRANSFER_WAIT
            deadline = min(
                transfer_deadline,
                time.monotonic() + CONSECUTIVE_FRAME_TIMEOUT,
            )
            transport.send(
                VolvoFrame(request_id, FLOW_CONTROL_DATA, False)
            )
            continue

        if pci_type == 2:
            if total_length is None or first_frame_data is None:
                continue
            sequence = data[0] & 0x0F
            if sequence != expected_sequence:
                total_length = None
                assembled.clear()
                first_frame_data = None
                continue
            missing = total_length - len(assembled)
            required = min(7, missing)
            if len(data) - 1 < required:
                total_length = None
                assembled.clear()
                first_frame_data = None
                continue
            assembled.extend(data[1 : 1 + required])
            expected_sequence = (expected_sequence + 1) & 0x0F
            if len(assembled) < total_length:
                if transfer_deadline is not None:
                    deadline = min(
                        transfer_deadline,
                        time.monotonic() + CONSECUTIVE_FRAME_TIMEOUT,
                    )
                continue

            payload = bytes(assembled[:total_length])
            frame_data = first_frame_data
            total_length = None
            assembled.clear()
            first_frame_data = None
            if _is_response_pending(payload, service_id):
                deadline = min(pending_deadline, time.monotonic() + timeout)
                continue
            if _response_matches(service_id, request_bytes, payload):
                return _Response(response_id, frame_data, payload)
            continue

        # Flow-control frames from another tester are not answers; ignore them.
        continue


def _is_response_pending(payload: bytes, service_id: int) -> bool:
    return (
        len(payload) >= 3
        and payload[0] == 0x7F
        and payload[1] == service_id
        and payload[2] == 0x78
    )


def _response_matches(
    service_id: int, request_bytes: bytes, payload: bytes
) -> bool:
    if not payload:
        return False
    if payload[0] == 0x7F:
        return len(payload) >= 2 and payload[1] == service_id
    if payload[0] != ((service_id + 0x40) & 0xFF):
        return False
    request_tail = request_bytes[1:]
    if service_id in (0x01, 0x09, 0x19):
        if not request_tail:
            return True
        return len(payload) >= 2 and payload[1] == request_tail[0]
    if service_id == 0x22:
        requested_did = request_tail[:2]
        if len(requested_did) < 2:
            return True
        return len(payload) >= 3 and payload[1:3] == requested_did
    return True


def decode_response_payload(
    service_id: int, payload: bytes
) -> Dict[str, Any]:
    """Decode a response where the format is unambiguous.

    Service 0x19 subfunction 0x02 is decoded into UDS DTC records. Everything
    else, including other 0x19 subfunctions, is preserved as raw payload bytes
    under a clear ``kind``; no code or text is invented.
    """

    payload = bytes(payload)
    if not payload:
        return {"kind": "empty", "raw_payload_hex": ""}
    if payload[0] == 0x7F:
        request_service = payload[1] if len(payload) >= 2 else None
        nrc = payload[2] if len(payload) >= 3 else None
        decoded: Dict[str, Any] = {
            "kind": "negative_response",
            "request_service": request_service,
            "request_service_hex": (
                None if request_service is None else "0x{:02X}".format(request_service)
            ),
            "nrc": nrc,
            "nrc_hex": None if nrc is None else "0x{:02X}".format(nrc),
            "nrc_name": NRC_NAMES.get(nrc),
            "raw_payload_hex": _hex_bytes(payload),
        }
        return decoded
    if service_id == 0x19:
        return _decode_dtc_payload(payload)
    return {
        "kind": "raw",
        "service": service_id,
        "service_hex": "0x{:02X}".format(service_id),
        "reason": "no decoder for this service; raw payload preserved",
        "raw_payload_hex": _hex_bytes(payload),
    }


def _decode_dtc_payload(payload: bytes) -> Dict[str, Any]:
    subfunction = payload[1] if len(payload) >= 2 else None
    base: Dict[str, Any] = {
        "kind": "dtc_list",
        "service": 0x19,
        "subfunction": subfunction,
        "subfunction_hex": (
            None if subfunction is None else "0x{:02X}".format(subfunction)
        ),
        "raw_payload_hex": _hex_bytes(payload),
    }
    if subfunction is None:
        base.update({"status": "malformed", "reason": "missing_subfunction"})
        return base
    if subfunction != 0x02:
        base.update(
            {
                "status": "raw",
                "reason": "only subfunction 0x02 is decoded; raw payload preserved",
            }
        )
        return base
    if len(payload) < 3:
        base.update(
            {"status": "malformed", "reason": "missing_status_availability_mask"}
        )
        return base
    mask = payload[2]
    records_data = payload[3:]
    if len(records_data) % 4:
        base.update({"status": "malformed", "reason": "truncated_dtc_record"})
        return base

    dtcs: List[Dict[str, Any]] = []
    for offset in range(0, len(records_data), 4):
        record = records_data[offset : offset + 4]
        status_byte = record[3]
        status_bits = {
            name: bool(status_byte & (1 << bit))
            for bit, name in enumerate(DTC_STATUS_BITS)
        }
        dtcs.append(
            {
                "code": _decode_sae_dtc(record[0], record[1]),
                "raw_dtc_hex": _hex_bytes(record[:3]),
                "failure_type": record[2],
                "failure_type_hex": "0x{:02X}".format(record[2]),
                "status_byte": status_byte,
                "status_hex": "0x{:02X}".format(status_byte),
                "status_bits": status_bits,
                "set_status_bits": [
                    name for name, is_set in status_bits.items() if is_set
                ],
                "raw_record_hex": _hex_bytes(record),
            }
        )

    base.update(
        {
            "status": "ok",
            "status_availability_mask": mask,
            "status_availability_mask_hex": "0x{:02X}".format(mask),
            "dtc_count": len(dtcs),
            "dtcs": dtcs,
        }
    )
    return base


def _decode_sae_dtc(first: int, second: int) -> str:
    system = ("P", "C", "B", "U")[(first >> 6) & 0x03]
    return "{}{}{:X}{:X}{:X}".format(
        system,
        (first >> 4) & 0x03,
        first & 0x0F,
        (second >> 4) & 0x0F,
        second & 0x0F,
    )


def _request_bytes(request: ReadRequest) -> Tuple[bytes, Optional[str]]:
    """Return the request payload; fall back to the service byte when absent."""

    if request.request is None or not request.request.strip():
        return b"", "script supplied no request bytes"
    compact = re.sub(r"[\s:_-]+", "", request.request.strip())
    try:
        data = bytes.fromhex(compact)
    except ValueError:
        data = b""
    if not data:
        return b"", "script request is not valid hex"
    return data, None


def _source_label(
    ecu_address: Optional[int],
    ecu_type: Optional[int],
    ecu_name: Optional[str] = None,
    ecu_identifier: Optional[str] = None,
) -> str:
    labels: List[str] = []
    if ecu_address is not None:
        labels.append("EcuAddress 0x{:X}".format(ecu_address))
    if ecu_type is not None:
        labels.append("EcuType {}".format(ecu_type))
    if ecu_name:
        labels.append("EcuName {}".format(ecu_name))
    if ecu_identifier:
        labels.append("EcuIdentifier {}".format(ecu_identifier))
    return " / ".join(labels) if labels else "unknown ECU"


def _status_counts(records: Iterable[RequestRecord]) -> Dict[str, int]:
    counts = {status: 0 for status in STATUSES}
    for record in records:
        counts[record.status] = counts.get(record.status, 0) + 1
    return counts


def _hex_bytes(data: bytes) -> str:
    return bytes(data).hex(" ").upper()


def _optional_hex(value: Optional[int]) -> Optional[str]:
    return None if value is None else "0x{:X}".format(value)


def _optional_can_id(value: Optional[int]) -> Optional[str]:
    return None if value is None else "0x{:03X}".format(value)
