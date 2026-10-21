"""Build read-only VIDA-style diagnostic views from saved scan reports.

This module is a pure data transform. It reads the JSON-shaped dictionaries
produced by ``dice-scan`` (and the discovery profiles accepted by
``--profile``), optionally merges descriptions from a code catalog, and builds
the three views VIDA presents for a readout: the module list, network status,
and fault trace.

It never talks to a vehicle, opens a CAN channel, imports a VIDA catalog
implementation, or changes a diagnostic session. Unknown report fields,
module records, and DTC records are kept in ``raw`` mappings so a view can be
round-tripped without losing the evidence it came from.

The accepted scan shapes are:

* a full ``dice-scan`` report with top-level ``modules`` entries carrying
  ``module`` or ``name``, CAN IDs, ``status``, and ``dtcs``;
* a discovery profile with ``name``, ``request_id``, and ``response_id`` but
  no read status or DTC list;
* a wrapper object with the report under a ``scan`` key.

The catalog is deliberately not imported. Any mapping with code keys works:
flat ``{code: description}``, ``{code: {"description": ...}}``, a nested
``dtcs``/``dtc``/``codes``/``descriptions`` mapping, or per-module mappings
under ``modules``. A description is used only when the catalog supplies one;
this module never derives or invents text for a code.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Optional, Tuple

# UDS status bit order, least significant bit first. The names match
# uds_read.py so a record can be read without translation.
DTC_STATUS_BIT_ORDER = (
    "testFailed",
    "testFailedThisOperationCycle",
    "pendingDTC",
    "confirmedDTC",
    "testNotCompletedSinceLastClear",
    "testFailedSinceLastClear",
    "testNotCompletedThisOperationCycle",
    "warningIndicatorRequested",
)

ACTIVE_STATUS_BITS = ("testFailed", "testFailedThisOperationCycle")
CONFIRMED_STATUS_BIT = "confirmedDTC"
PENDING_STATUS_BIT = "pendingDTC"
HISTORY_STATUS_BIT = "testFailedSinceLastClear"
INCOMPLETE_STATUS_BITS = (
    "testNotCompletedSinceLastClear",
    "testNotCompletedThisOperationCycle",
)

# Current faults are shown before stored or monitor history. The order is also
# the order used inside each module by fault_trace().
FAULT_STATUS_ORDER = (
    "active",
    "confirmed",
    "pending",
    "history",
    "incomplete",
    "unknown",
)
CURRENT_FAULT_STATUSES = ("active", "confirmed")

DEFAULT_COVERAGE_NOTE = (
    "Silence does not establish module absence. Other buses, protocols and "
    "response mappings are not automatically detected."
)

_CATALOG_CONTAINER_KEYS = ("dtcs", "dtc", "codes", "descriptions")
_CATALOG_TEXT_KEYS = ("description", "text", "name", "title", "label")
_MISSING = object()
_RESERVED_SCAN_KEYS = {
    "modules",
    "summary",
    "name",
    "title",
    "started_at",
    "finished_at",
    "status",
    "scope",
    "coverage_note",
}


@dataclass(frozen=True)
class DiagnosticDtc:
    """One DTC record with its status flags and optional catalog description.

    ``description`` is ``None`` when no catalog entry matched. The original
    record is retained in ``raw`` and is not modified when a description is
    merged. ``classification`` is a VIDA-style bucket computed from the
    effective status bits, or from the reported status bits when the report
    has no effective bits.
    """

    code: str
    description: Optional[str] = None
    description_source: Optional[str] = None
    module: str = ""
    failure_type: Optional[int] = None
    status_byte: Optional[int] = None
    status_bits: Mapping[str, bool] = field(default_factory=dict)
    set_status_bits: Tuple[str, ...] = ()
    effective_status_byte: Optional[int] = None
    effective_status_bits: Mapping[str, bool] = field(default_factory=dict)
    set_effective_status_bits: Tuple[str, ...] = ()
    classification: str = "unknown"
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def status(self) -> str:
        """Return the VIDA-style fault state (alias for ``classification``)."""
        return self.classification

    @property
    def is_active(self) -> bool:
        """Return whether a current test failure bit is set."""
        return any(self._effective_bits.get(name, False) for name in ACTIVE_STATUS_BITS)

    @property
    def is_confirmed(self) -> bool:
        """Return whether the confirmed DTC bit is set."""
        return self._effective_bits.get(CONFIRMED_STATUS_BIT, False)

    @property
    def is_pending(self) -> bool:
        """Return whether the pending DTC bit is set."""
        return self._effective_bits.get(PENDING_STATUS_BIT, False)

    @property
    def _effective_bits(self) -> Mapping[str, bool]:
        return self.effective_status_bits or self.status_bits

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        """Return a JSON-serializable mapping for this DTC."""
        result = {
            "code": self.code,
            "description": self.description,
            "description_source": self.description_source,
            "module": self.module,
            "failure_type": self.failure_type,
            "failure_type_hex": _hex_byte(self.failure_type),
            "status_byte": self.status_byte,
            "status_hex": _hex_byte(self.status_byte),
            "status_bits": _jsonable(self.status_bits),
            "set_status_bits": list(self.set_status_bits),
            "effective_status_byte": self.effective_status_byte,
            "effective_status_hex": _hex_byte(self.effective_status_byte),
            "effective_status_bits": _jsonable(self.effective_status_bits),
            "set_effective_status_bits": list(self.set_effective_status_bits),
            "status": self.classification,
            "classification": self.classification,
            "is_active": self.is_active,
            "is_confirmed": self.is_confirmed,
            "is_pending": self.is_pending,
        }
        if include_raw:
            result["raw"] = _jsonable(self.raw)
        return result


@dataclass(frozen=True)
class DiagnosticModule:
    """One ECU address from a scan report and the records read from it.

    ``dtc_count`` is the count reported by the scan when present, otherwise the
    number of parsed records. ``dtcs`` holds the parsed records and ``raw``
    holds the original module mapping, including evidence and any fields this
    view does not interpret.
    """

    name: str
    request_id: Optional[int] = None
    response_id: Optional[int] = None
    status: str = "unknown"
    dtcs: Tuple[DiagnosticDtc, ...] = ()
    dtc_count: int = 0
    fault_record_count: Optional[int] = None
    status_availability_mask: Optional[int] = None
    negative_response: Optional[Mapping[str, Any]] = None
    reason: Optional[str] = None
    raw: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)

    @property
    def record_count(self) -> int:
        """Return the number of parsed DTC records."""
        return len(self.dtcs)

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        """Return a JSON-serializable mapping for this module."""
        result = {
            "name": self.name,
            "request_id": self.request_id,
            "request_id_hex": _hex_id(self.request_id),
            "response_id": self.response_id,
            "response_id_hex": _hex_id(self.response_id),
            "status": self.status,
            "dtc_count": self.dtc_count,
            "record_count": self.record_count,
            "fault_record_count": self.fault_record_count,
            "status_availability_mask": self.status_availability_mask,
            "status_availability_mask_hex": _hex_byte(self.status_availability_mask),
            "negative_response": _jsonable(self.negative_response),
            "reason": self.reason,
            "dtcs": [dtc.to_dict(include_raw=include_raw) for dtc in self.dtcs],
        }
        if include_raw:
            result["raw"] = _jsonable(self.raw)
        return result


@dataclass(frozen=True)
class FaultTraceGroup:
    """DTCs from one module in one fault-state bucket.

    ``is_current`` is true for the ``active`` and ``confirmed`` buckets. The
    individual status flags remain on each :class:`DiagnosticDtc`; grouping
    does not rewrite them.
    """

    module: str
    status: str
    dtcs: Tuple[DiagnosticDtc, ...] = ()

    @property
    def count(self) -> int:
        """Return the number of DTCs in this group."""
        return len(self.dtcs)

    @property
    def is_current(self) -> bool:
        """Return whether this group is active or confirmed."""
        return self.status in CURRENT_FAULT_STATUSES

    def to_dict(self, *, include_raw: bool = False) -> dict[str, Any]:
        """Return a JSON-serializable mapping for this fault-trace group."""
        return {
            "module": self.module,
            "status": self.status,
            "is_current": self.is_current,
            "count": self.count,
            "dtcs": [dtc.to_dict(include_raw=include_raw) for dtc in self.dtcs],
        }


@dataclass(frozen=True)
class NetworkStatus:
    """Response summary for the modules in a report.

    A module that answered at all is listed in ``responders``. Modules with a
    negative response or an incomplete response therefore appear both in
    ``responders`` and in their specific bucket. ``other`` holds statuses this
    view does not recognise, including discovery-only entries that carry no
    read status.
    """

    total_modules: int
    responders: Tuple[str, ...] = ()
    no_response: Tuple[str, ...] = ()
    incomplete: Tuple[str, ...] = ()
    negative_response: Tuple[str, ...] = ()
    other: Tuple[str, ...] = ()
    coverage_note: str = DEFAULT_COVERAGE_NOTE

    @property
    def responder_count(self) -> int:
        """Return the number of modules that answered at all."""
        return len(self.responders)

    @property
    def coverage_ratio(self) -> float:
        """Return the responder fraction, or ``0.0`` for an empty report."""
        if self.total_modules == 0:
            return 0.0
        return self.responder_count / self.total_modules

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable mapping for the network status."""
        return {
            "total_modules": self.total_modules,
            "responder_count": self.responder_count,
            "responders": list(self.responders),
            "no_response": list(self.no_response),
            "no_response_count": len(self.no_response),
            "incomplete": list(self.incomplete),
            "incomplete_count": len(self.incomplete),
            "negative_response": list(self.negative_response),
            "negative_response_count": len(self.negative_response),
            "other": list(self.other),
            "other_count": len(self.other),
            "coverage_ratio": self.coverage_ratio,
            "coverage_note": self.coverage_note,
        }


@dataclass(frozen=True)
class DiagnosticReport:
    """A read-only VIDA-style view built from one scan report.

    ``summary`` and ``metadata`` keep the scan's own summary and any top-level
    fields the view does not interpret. ``vehicle`` is the caller's optional
    profile mapping and is copied verbatim.
    """

    name: Optional[str] = None
    vehicle: Mapping[str, Any] = field(default_factory=dict)
    modules: Tuple[DiagnosticModule, ...] = ()
    started_at: Optional[str] = None
    finished_at: Optional[str] = None
    scan_status: Optional[str] = None
    scope: Optional[str] = None
    coverage_note: str = DEFAULT_COVERAGE_NOTE
    summary: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @property
    def module_count(self) -> int:
        """Return the number of modules in the report."""
        return len(self.modules)

    @property
    def dtc_count(self) -> int:
        """Return the number of parsed DTC records across all modules."""
        return sum(len(module.dtcs) for module in self.modules)

    @property
    def described_dtc_count(self) -> int:
        """Return the number of DTC records with a catalog description."""
        return sum(
            1
            for module in self.modules
            for dtc in module.dtcs
            if dtc.description is not None
        )

    @property
    def current_fault_count(self) -> int:
        """Return the number of active or confirmed DTC records."""
        return sum(
            1
            for module in self.modules
            for dtc in module.dtcs
            if dtc.classification in CURRENT_FAULT_STATUSES
        )

    def to_dict(self, *, include_raw: bool = True) -> dict[str, Any]:
        """Return a JSON-serializable mapping of the full diagnostic view."""
        return {
            "name": self.name,
            "vehicle": _jsonable(self.vehicle),
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "scan_status": self.scan_status,
            "scope": self.scope,
            "coverage_note": self.coverage_note,
            "counts": {
                "modules": self.module_count,
                "dtcs": self.dtc_count,
                "described_dtcs": self.described_dtc_count,
                "current_faults": self.current_fault_count,
            },
            "modules": [
                module.to_dict(include_raw=include_raw) for module in self.modules
            ],
            "summary": _jsonable(self.summary),
            "metadata": _jsonable(self.metadata),
            "network": network_status(self).to_dict(),
            "fault_trace": [group.to_dict() for group in fault_trace(self)],
        }

    def to_json(
        self, *, indent: Optional[int] = 2, include_raw: bool = True
    ) -> str:
        """Return stable JSON for the full diagnostic view."""
        return json.dumps(
            self.to_dict(include_raw=include_raw),
            indent=indent,
            sort_keys=True,
            ensure_ascii=False,
        )


def build_report(
    scan: Mapping[str, Any],
    catalog: Optional[Mapping[str, Any]] = None,
    vehicle: Optional[Mapping[str, Any]] = None,
) -> DiagnosticReport:
    """Build a :class:`DiagnosticReport` from a scan mapping.

    ``scan`` is not mutated. ``catalog`` may be any of the mapping shapes
    described in the module docstring. ``vehicle`` is optional profile data
    (for example a VIN, model year, or a VIDA profile title) and is included
    unchanged in the report.
    """
    if not isinstance(scan, Mapping):
        raise TypeError("scan must be a mapping")

    source = scan
    wrapped = source.get("scan")
    if "modules" not in source and isinstance(wrapped, Mapping):
        source = wrapped

    raw_modules = source.get("modules", ())
    if raw_modules is None:
        raw_modules = ()
    if isinstance(raw_modules, Mapping) or not isinstance(raw_modules, (list, tuple)):
        raise TypeError("scan modules must be a list or tuple")

    global_catalog, module_catalog = _build_catalog_index(catalog)
    modules = tuple(
        module
        for module in (
            _build_module(item, global_catalog, module_catalog)
            for item in raw_modules
        )
        if module is not None
    )

    raw_summary = source.get("summary")
    metadata = {
        key: _copy_raw(value)
        for key, value in source.items()
        if key not in _RESERVED_SCAN_KEYS
    }

    coverage_note = _text(source.get("coverage_note")) or DEFAULT_COVERAGE_NOTE
    return DiagnosticReport(
        name=_text(source.get("name")) or _text(source.get("title")),
        vehicle=_copy_raw(dict(vehicle)) if vehicle is not None else {},
        modules=modules,
        started_at=_text(source.get("started_at")),
        finished_at=_text(source.get("finished_at")),
        scan_status=_text(source.get("status")),
        scope=_text(source.get("scope")),
        coverage_note=coverage_note,
        summary=_copy_raw(raw_summary) if isinstance(raw_summary, Mapping) else {},
        metadata=metadata,
    )


def network_status(report: DiagnosticReport) -> NetworkStatus:
    """Summarize responders, silence, and partial or negative responses."""
    if not isinstance(report, DiagnosticReport):
        raise TypeError("report must be a DiagnosticReport")

    responders: list[str] = []
    no_response: list[str] = []
    incomplete: list[str] = []
    negative_response: list[str] = []
    other: list[str] = []

    for module in report.modules:
        status = (module.status or "").strip().casefold()
        if status == "ok":
            responders.append(module.name)
        elif status == "no_response":
            no_response.append(module.name)
        elif status == "incomplete":
            responders.append(module.name)
            incomplete.append(module.name)
        elif status == "negative_response":
            responders.append(module.name)
            negative_response.append(module.name)
        else:
            other.append(module.name)

    return NetworkStatus(
        total_modules=report.module_count,
        responders=tuple(responders),
        no_response=tuple(no_response),
        incomplete=tuple(incomplete),
        negative_response=tuple(negative_response),
        other=tuple(other),
        coverage_note=report.coverage_note,
    )


def fault_trace(report: DiagnosticReport) -> Tuple[FaultTraceGroup, ...]:
    """Group DTCs by module and fault-state bucket.

    Every group is non-empty. Within a module, active and confirmed records
    come first, followed by pending, history, incomplete, and unknown records.
    Records without a status byte are placed in ``unknown`` rather than being
    treated as cleared.
    """
    if not isinstance(report, DiagnosticReport):
        raise TypeError("report must be a DiagnosticReport")

    groups: list[FaultTraceGroup] = []
    for module in report.modules:
        buckets: dict[str, list[DiagnosticDtc]] = {
            status: [] for status in FAULT_STATUS_ORDER
        }
        for dtc in module.dtcs:
            status = dtc.classification
            if status not in buckets:
                status = "unknown"
            buckets[status].append(dtc)
        for status in FAULT_STATUS_ORDER:
            if buckets[status]:
                groups.append(
                    FaultTraceGroup(
                        module=module.name,
                        status=status,
                        dtcs=tuple(buckets[status]),
                    )
                )
    return tuple(groups)


def _build_module(
    item: Any,
    global_catalog: Mapping[str, str],
    module_catalog: Mapping[str, Mapping[str, str]],
) -> Optional[DiagnosticModule]:
    if isinstance(item, Mapping):
        raw = item
        raw_copy = _copy_raw(item)
    elif isinstance(item, str):
        raw = {"name": item}
        raw_copy = {"name": item}
    else:
        return None

    name = _text(raw.get("module")) or _text(raw.get("name")) or ""
    request_id = _first_int(raw, "request_id", "request_id_hex")
    response_id = _first_int(raw, "response_id", "response_id_hex")
    status = _text(raw.get("status")) or "unknown"

    raw_dtcs = raw.get("dtcs")
    if raw_dtcs is None:
        raw_dtcs = ()
    if isinstance(raw_dtcs, Mapping) or not isinstance(raw_dtcs, (list, tuple)):
        raw_dtcs = ()

    dtcs = tuple(
        dtc
        for dtc in (
            _build_dtc(record, name, global_catalog, module_catalog)
            for record in raw_dtcs
        )
        if dtc is not None
    )

    reported_count = _as_int(raw.get("dtc_count"))
    dtc_count = reported_count if reported_count is not None else len(dtcs)
    raw_negative = raw.get("negative_response")
    negative_response = (
        _copy_raw(raw_negative) if isinstance(raw_negative, Mapping) else None
    )

    return DiagnosticModule(
        name=name,
        request_id=request_id,
        response_id=response_id,
        status=status,
        dtcs=dtcs,
        dtc_count=dtc_count,
        fault_record_count=_first_int(raw, "fault_record_count"),
        status_availability_mask=_first_int(
            raw, "status_availability_mask", "status_availability_mask_hex"
        ),
        negative_response=negative_response,
        reason=_text(raw.get("reason")) or _text(raw.get("error")),
        raw=raw_copy,
    )


def _build_dtc(
    record: Any,
    module_name: str,
    global_catalog: Mapping[str, str],
    module_catalog: Mapping[str, Mapping[str, str]],
) -> Optional[DiagnosticDtc]:
    if isinstance(record, Mapping):
        raw = record
        raw_copy = _copy_raw(record)
    elif isinstance(record, str):
        raw = {"code": record}
        raw_copy = {"code": record}
    else:
        return None

    code = _text(raw.get("code")) or ""
    failure_type = _first_int(raw, "failure_type_byte", "failure_type")
    status_byte = _first_int(raw, "status_byte", "status_hex")
    status_bits = _bool_map(raw.get("status_bits"))
    if not status_bits and status_byte is not None:
        status_bits = _bits_from_byte(status_byte)

    raw_set_bits = raw.get("set_status_bits", _MISSING)
    set_status_bits = (
        _bits_to_set(status_bits)
        if raw_set_bits is _MISSING
        else _string_tuple(raw_set_bits)
    )

    effective_byte = _first_int(raw, "effective_status_byte", "effective_status_hex")
    effective_bits = _bool_map(raw.get("effective_status_bits"))
    if not effective_bits and effective_byte is not None:
        effective_bits = _bits_from_byte(effective_byte)

    raw_set_effective = raw.get("set_effective_status_bits", _MISSING)
    set_effective_bits = (
        _bits_to_set(effective_bits)
        if raw_set_effective is _MISSING
        else _string_tuple(raw_set_effective)
    )

    description, source = _lookup_description(
        module_name,
        code,
        failure_type,
        global_catalog,
        module_catalog,
    )
    classification = _classify(effective_bits or status_bits)

    return DiagnosticDtc(
        code=code,
        description=description,
        description_source=source,
        module=module_name,
        failure_type=failure_type,
        status_byte=status_byte,
        status_bits=status_bits,
        set_status_bits=set_status_bits,
        effective_status_byte=effective_byte,
        effective_status_bits=effective_bits,
        set_effective_status_bits=set_effective_bits,
        classification=classification,
        raw=raw_copy,
    )


def _classify(bits: Mapping[str, bool]) -> str:
    if any(bits.get(name, False) for name in ACTIVE_STATUS_BITS):
        return "active"
    if bits.get(CONFIRMED_STATUS_BIT, False):
        return "confirmed"
    if bits.get(PENDING_STATUS_BIT, False):
        return "pending"
    if bits.get(HISTORY_STATUS_BIT, False):
        return "history"
    if any(bits.get(name, False) for name in INCOMPLETE_STATUS_BITS):
        return "incomplete"
    return "unknown"


def _build_catalog_index(
    catalog: Optional[Mapping[str, Any]],
) -> Tuple[Mapping[str, str], Mapping[str, Mapping[str, str]]]:
    if not isinstance(catalog, Mapping):
        return {}, {}

    global_index: dict[str, str] = {}
    for source in _catalog_sources(catalog):
        _index_catalog_mapping(source, global_index)

    module_index: dict[str, dict[str, str]] = {}
    raw_modules = catalog.get("modules")
    if isinstance(raw_modules, Mapping):
        for name, entry in raw_modules.items():
            if not isinstance(name, str) or not isinstance(entry, Mapping):
                continue
            scoped: dict[str, str] = {}
            for source in _catalog_sources(entry):
                _index_catalog_mapping(source, scoped)
            if scoped:
                module_index[name.strip().upper()] = scoped
    return global_index, module_index


def _catalog_sources(catalog: Mapping[str, Any]) -> Tuple[Mapping[str, Any], ...]:
    sources: list[Mapping[str, Any]] = [catalog]
    for key in _CATALOG_CONTAINER_KEYS:
        nested = catalog.get(key)
        if isinstance(nested, Mapping) and nested is not catalog:
            sources.append(nested)
    return tuple(sources)


def _index_catalog_mapping(
    source: Mapping[str, Any], index: dict[str, str]
) -> None:
    for key, entry in source.items():
        if not isinstance(key, str):
            continue
        description = _entry_description(entry, key)
        if description is not None:
            index.setdefault(key.strip().upper(), description)


def _entry_description(entry: Any, key: str) -> Optional[str]:
    text: Optional[str] = None
    if isinstance(entry, str):
        text = entry
    elif isinstance(entry, Mapping):
        for field_name in _CATALOG_TEXT_KEYS:
            value = entry.get(field_name)
            if isinstance(value, str) and value.strip():
                text = value
                break
    if text is None:
        return None
    text = text.strip()
    if not text or text.casefold() == key.strip().casefold():
        # A code repeated as a label is not a description.
        return None
    return text


def _lookup_description(
    module_name: str,
    code: str,
    failure_type: Optional[int],
    global_catalog: Mapping[str, str],
    module_catalog: Mapping[str, Mapping[str, str]],
) -> Tuple[Optional[str], Optional[str]]:
    normalized = code.strip().upper()
    if not normalized:
        return None, None

    candidates = [normalized]
    if failure_type is not None:
        candidates.append("{}-{:02X}".format(normalized, failure_type))

    scoped = module_catalog.get((module_name or "").strip().upper(), {})
    for candidate in candidates:
        description = scoped.get(candidate)
        if description is not None:
            return description, "catalog"
    for candidate in candidates:
        description = global_catalog.get(candidate)
        if description is not None:
            return description, "catalog"
    return None, None


def _first_int(source: Mapping[str, Any], *keys: str) -> Optional[int]:
    for key in keys:
        value = _as_int(source.get(key))
        if value is not None:
            return value
    return None


def _as_int(value: Any) -> Optional[int]:
    if type(value) is int:
        return value
    if isinstance(value, str):
        text = value.strip()
        try:
            if text.lower().startswith("0x"):
                return int(text, 16)
            return int(text, 10)
        except ValueError:
            return None
    return None


def _text(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _bool_map(value: Any) -> dict[str, bool]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, bool] = {}
    for key, item in value.items():
        if not isinstance(key, str):
            continue
        if isinstance(item, bool):
            result[key] = item
        elif isinstance(item, str):
            lowered = item.strip().casefold()
            if lowered in ("true", "1", "yes"):
                result[key] = True
            elif lowered in ("false", "0", "no"):
                result[key] = False
        else:
            result[key] = bool(item)
    return result


def _string_tuple(value: Any) -> Tuple[str, ...]:
    if isinstance(value, str):
        text = value.strip()
        return (text,) if text else ()
    if isinstance(value, (list, tuple)):
        return tuple(
            text
            for text in (item.strip() for item in value if isinstance(item, str))
            if text
        )
    if isinstance(value, Mapping):
        return _bits_to_set(_bool_map(value))
    return ()


def _bits_from_byte(value: int) -> dict[str, bool]:
    return {
        name: bool(value & (1 << index))
        for index, name in enumerate(DTC_STATUS_BIT_ORDER)
    }


def _bits_to_set(bits: Mapping[str, bool]) -> Tuple[str, ...]:
    ordered = [name for name in DTC_STATUS_BIT_ORDER if bits.get(name, False)]
    extras = sorted(
        name
        for name, enabled in bits.items()
        if enabled and name not in DTC_STATUS_BIT_ORDER
    )
    return tuple(ordered + extras)


def _hex_id(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return "0x{:03X}".format(value)


def _hex_byte(value: Optional[int]) -> Optional[str]:
    if value is None:
        return None
    return "0x{:02X}".format(value)


def _copy_raw(value: Any) -> Any:
    try:
        return copy.deepcopy(value)
    except (TypeError, ValueError):
        if isinstance(value, Mapping):
            return dict(value)
        return value


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return [_jsonable(item) for item in sorted(value, key=repr)]
    if isinstance(value, bytes):
        return value.hex(" ").upper()
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
