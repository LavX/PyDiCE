"""Curses TUI for Volvo P3 diagnostics."""

from __future__ import annotations

import curses
import contextlib
import io
import json
import os
import shlex
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from volvo_p3.can_transport import CanBusConfig, PythonCanTransport
    from volvo_p3.diagnostics import DiagnosticReport, build_report as build_diagnostic_report
    from volvo_p3.diagnostics import fault_trace as build_fault_trace
    from volvo_p3.diagnostics import network_status as compute_network_status
    from volvo_p3.dice_usb import find_dice_pyusb, find_dice_system_profiler, get_pyusb_backend_status
    from volvo_p3.modules import KNOWN_MODULES, parse_ecu
    from volvo_p3.protocol import (
        SERVICE_CLEAR_DTC,
        SERVICE_READ_DTC,
        build_clear_dtc_request,
        build_read_dtc_request,
        hex_byte,
        hex_id,
        is_response_to,
        parse_dtc_response,
    )
    from volvo_p3.diag_catalog import CatalogError, DiagnosticCatalog, load_catalog
    from volvo_p3.script_plan import (
        DiagnosticScript,
        DiagnosticScriptError,
        classify_service,
        parse_script_xml,
        parse_script_zip,
    )
else:
    from .can_transport import CanBusConfig, PythonCanTransport
    from .diagnostics import DiagnosticReport, build_report as build_diagnostic_report
    from .diagnostics import fault_trace as build_fault_trace
    from .diagnostics import network_status as compute_network_status
    from .dice_usb import find_dice_pyusb, find_dice_system_profiler, get_pyusb_backend_status
    from .modules import KNOWN_MODULES, parse_ecu
    from .protocol import (
        SERVICE_CLEAR_DTC,
        SERVICE_READ_DTC,
        build_clear_dtc_request,
        build_read_dtc_request,
        hex_byte,
        hex_id,
        is_response_to,
        parse_dtc_response,
    )
    from .diag_catalog import CatalogError, DiagnosticCatalog, load_catalog
    from .script_plan import (
        DiagnosticScript,
        DiagnosticScriptError,
        classify_service,
        parse_script_xml,
        parse_script_zip,
    )


@dataclass(frozen=True)
class FieldSpec:
    name: str
    prompt: str
    default: str = ""


@dataclass(frozen=True)
class ActionSpec:
    name: str
    description: str
    runner: Callable[[dict[str, str]], list[str]]
    fields: tuple[FieldSpec, ...] = ()


@dataclass(frozen=True)
class SectionSpec:
    title: str
    actions: tuple[ActionSpec, ...]


@dataclass(frozen=True)
class NativeScanResult:
    """Outcome of one native CLI scan: the saved report and its log lines."""

    report_path: Path
    profile_path: Path | None = None
    lines: tuple[str, ...] = ()


@dataclass
class VehicleState:
    vin: str = ""
    profile_created: bool = False
    detected_modules: list[int] = field(default_factory=list)
    last_dtc_modules: list[int] = field(default_factory=list)
    adapter_summary: list[str] = field(default_factory=list)


def _optional_int(value: str | None) -> int | None:
    if value is None or not value.strip():
        return None
    return int(value, 0)


def _parse_ecu_list(text: str) -> list[int]:
    values = [piece.strip() for piece in text.split(",")]
    return [parse_ecu(piece) for piece in values if piece]


# Diagnostic data views -------------------------------------------------------------

DEFAULT_DIAG_CATALOG = "data/diag_catalog.json.xz"
DEFAULT_DIAG_SCAN = "reports/my-car-complete.json"
DEFAULT_LIVE_TIMEOUT = "1.5"


def default_diag_catalog_path() -> str:
    """Return the conventional catalog path, or blank when it is absent."""

    return DEFAULT_DIAG_CATALOG if Path(DEFAULT_DIAG_CATALOG).is_file() else ""


def default_diag_scan_path() -> str:
    """Return the conventional saved scan path."""

    return DEFAULT_DIAG_SCAN


def default_live_diag_profile_path() -> str:
    """Return the conventional saved profile path, or blank when it is absent."""

    return DEFAULT_DIAG_SCAN if Path(DEFAULT_DIAG_SCAN).is_file() else ""


def _timeout_text(value: str | float | None) -> str:
    try:
        timeout = float(value)
    except (TypeError, ValueError):
        raise ValueError("timeout must be a positive finite number") from None
    if not 0 < timeout < float("inf"):
        raise ValueError("timeout must be a positive finite number")
    return str(timeout)


def build_live_scan_options(
    catalog_path: str | os.PathLike[str],
    timeout: str | float | None = DEFAULT_LIVE_TIMEOUT,
    profile_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Return the read-only ``dice-scan`` options for one live DTC read.

    The catalog and the optional saved profile are checked before any scan
    starts, so a missing file becomes an error line instead of a failed
    adapter session. The profile is only read, never refreshed.
    """

    catalog_text = str(catalog_path).strip()
    if not catalog_text:
        raise ValueError("enter a service catalog JSON path")
    catalog_file = Path(catalog_text)
    if not catalog_file.is_file():
        raise FileNotFoundError("catalog not found: {}".format(catalog_file))

    options = [
        "--timeout",
        _timeout_text(timeout),
        "--diag-catalog",
        str(catalog_file),
    ]

    profile_text = str(profile_path).strip() if profile_path is not None else ""
    if profile_text:
        profile_file = Path(profile_text)
        if not profile_file.is_file():
            raise FileNotFoundError("profile not found: {}".format(profile_file))
        options.extend(("--profile", str(profile_file)))
    return options


def diag_catalog_mapping(catalog: DiagnosticCatalog) -> dict[str, Any]:
    """Flatten a loaded catalog into the mapping shape diagnostics accepts.

    Only text the catalog actually carries is copied. A code whose entries
    disagree is left out so no caller has to choose a description.
    """

    if not isinstance(catalog, DiagnosticCatalog):
        raise TypeError("catalog must be a DiagnosticCatalog")

    global_texts: dict[str, set[str]] = {}
    module_texts: dict[str, dict[str, set[str]]] = {}
    for entry in catalog.dtc_descriptions:
        text = (entry.short_text or entry.long_text or "").strip()
        if not text:
            continue
        if entry.ecu:
            module_texts.setdefault(entry.ecu.strip().upper(), {}).setdefault(
                entry.code, set()
            ).add(text)
        else:
            global_texts.setdefault(entry.code, set()).add(text)

    mapping: dict[str, Any] = {}
    for code, texts in global_texts.items():
        if len(texts) == 1:
            mapping[code] = {"description": next(iter(texts))}

    modules: dict[str, Any] = {}
    for ecu, codes in module_texts.items():
        scoped: dict[str, Any] = {}
        for code, texts in codes.items():
            if len(texts) == 1:
                scoped[code] = {"description": next(iter(texts))}
        if scoped:
            modules[ecu] = scoped
    if modules:
        mapping["modules"] = modules
    return mapping


def build_diag_report(
    scan_path: str | os.PathLike[str],
    catalog_path: str | os.PathLike[str] | None = None,
    vehicle: Mapping[str, Any] | None = None,
) -> DiagnosticReport:
    """Load a saved scan, optionally merge a catalog, and build the view data.

    Missing files raise ``FileNotFoundError`` and malformed input raises
    ``ValueError`` (``CatalogError`` included). The TUI renders those as an
    error line; callers that want that behaviour should use one of the
    ``diag_*_lines`` wrappers below.
    """

    report_text = str(scan_path).strip()
    if not report_text:
        raise FileNotFoundError("enter a scan report JSON path")
    report_path = Path(report_text)
    if not report_path.is_file():
        raise FileNotFoundError("scan report not found: {}".format(report_path))

    try:
        with report_path.open(encoding="utf-8") as handle:
            scan = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(
            "scan report is not valid JSON: {} (line {}, column {})".format(
                report_path, exc.lineno, exc.colno
            )
        ) from exc
    if not isinstance(scan, dict):
        raise ValueError(
            "scan report root must be a JSON object: {}".format(report_path)
        )

    catalog_mapping: Mapping[str, Any] | None = None
    catalog_text = str(catalog_path).strip() if catalog_path is not None else ""
    if catalog_text:
        catalog_file = Path(catalog_text)
        if not catalog_file.is_file():
            raise FileNotFoundError("catalog not found: {}".format(catalog_file))
        catalog_mapping = diag_catalog_mapping(load_catalog(catalog_file))

    return build_diagnostic_report(scan, catalog=catalog_mapping, vehicle=vehicle)


def _diag_hex(value: int | None, width: int = 2) -> str:
    if value is None:
        return "-"
    return "0x{:0{}X}".format(value, width)


def _diag_dtc_line(dtc: Any) -> str:
    line = "  {}  {}".format(dtc.code or "(no code)", dtc.classification)
    if dtc.description:
        line += "  " + dtc.description
    return line


def _diag_report_title(report: DiagnosticReport) -> str:
    return report.name or "saved scan"


def format_diag_dtc_lines(report: DiagnosticReport) -> list[str]:
    """Format the module and DTC overview for one report."""

    if not isinstance(report, DiagnosticReport):
        raise TypeError("report must be a DiagnosticReport")

    lines = ["Diagnostic DTC overview: {}".format(_diag_report_title(report))]
    vin = report.vehicle.get("vin") if isinstance(report.vehicle, Mapping) else None
    if isinstance(vin, str) and vin.strip():
        lines.append("vin: {}".format(vin.strip().upper()))
    lines.append(
        "modules: {} | DTC records: {} | described: {} | current faults: {}".format(
            report.module_count,
            report.dtc_count,
            report.described_dtc_count,
            report.current_fault_count,
        )
    )
    if not report.modules:
        lines.append("no modules in this report")
        return lines

    for module in report.modules:
        lines.append(
            "{}  request={}  response={}  status={}  dtcs={}".format(
                module.name or "(unnamed)",
                _diag_hex(module.request_id, 3),
                _diag_hex(module.response_id, 3),
                module.status or "unknown",
                module.dtc_count,
            )
        )
        lines.extend(_diag_dtc_line(dtc) for dtc in module.dtcs)
    return lines


def format_diag_network_lines(report: DiagnosticReport) -> list[str]:
    """Format the responder summary for one report."""

    if not isinstance(report, DiagnosticReport):
        raise TypeError("report must be a DiagnosticReport")

    network = compute_network_status(report)
    lines = [
        "Diagnostic network status: {}".format(_diag_report_title(report)),
        "total modules: {}".format(network.total_modules),
        "responders: {} ({:.0%})".format(
            network.responder_count, network.coverage_ratio
        ),
        "no response: {}".format(", ".join(network.no_response) or "(none)"),
        "incomplete: {}".format(", ".join(network.incomplete) or "(none)"),
        "negative response: {}".format(
            ", ".join(network.negative_response) or "(none)"
        ),
        "unclassified: {}".format(", ".join(network.other) or "(none)"),
        "coverage: {}".format(network.coverage_note),
    ]
    return lines


def format_diag_fault_lines(report: DiagnosticReport) -> list[str]:
    """Format DTCs grouped by module and fault state."""

    groups = build_fault_trace(report)
    lines = [
        "Diagnostic fault trace: {}".format(_diag_report_title(report)),
        "current faults: {} | groups: {}".format(
            report.current_fault_count, len(groups)
        ),
    ]
    if not groups:
        lines.append("no DTC records in this report")
        return lines

    for group in groups:
        lines.append(
            "{}  {}  ({})".format(
                group.module or "(unnamed)", group.status, group.count
            )
        )
        lines.extend(_diag_dtc_line(dtc) for dtc in group.dtcs)
    return lines


def _safe_diag_report_lines(
    scan_path: str | os.PathLike[str],
    catalog_path: str | os.PathLike[str] | None,
    vehicle: Mapping[str, Any] | None,
    formatter: Callable[[DiagnosticReport], list[str]],
) -> list[str]:
    try:
        report = build_diag_report(scan_path, catalog_path, vehicle)
    except (OSError, ValueError, TypeError) as exc:
        return ["error: {}".format(exc)]
    return formatter(report)


def diag_dtc_overview_lines(
    scan_path: str | os.PathLike[str],
    catalog_path: str | os.PathLike[str] | None = None,
    vehicle: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return DTC overview lines, with a single error line on bad input."""

    return _safe_diag_report_lines(
        scan_path, catalog_path, vehicle, format_diag_dtc_lines
    )


def diag_network_status_lines(
    scan_path: str | os.PathLike[str],
    catalog_path: str | os.PathLike[str] | None = None,
    vehicle: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return network status lines, with a single error line on bad input."""

    return _safe_diag_report_lines(
        scan_path, catalog_path, vehicle, format_diag_network_lines
    )


def diag_fault_trace_lines(
    scan_path: str | os.PathLike[str],
    catalog_path: str | os.PathLike[str] | None = None,
    vehicle: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return fault trace lines, with a single error line on bad input."""

    return _safe_diag_report_lines(
        scan_path, catalog_path, vehicle, format_diag_fault_lines
    )


def format_live_diag_lines(
    report: DiagnosticReport,
    *,
    report_path: str | os.PathLike[str] | None = None,
    catalog_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Format one live read as counts, fault trace, and network status."""

    if not isinstance(report, DiagnosticReport):
        raise TypeError("report must be a DiagnosticReport")

    lines = ["Live DTC read: {}".format(_diag_report_title(report))]
    if report_path is not None and str(report_path).strip():
        lines.append("saved report: {}".format(report_path))
    if catalog_path is not None and str(catalog_path).strip():
        lines.append("catalog: {}".format(catalog_path))
    if report.scan_status:
        lines.append("scan status: {}".format(report.scan_status))
    lines.append("")
    lines.extend(format_diag_dtc_lines(report))
    lines.append("")
    lines.extend(format_diag_fault_lines(report))
    lines.append("")
    lines.extend(format_diag_network_lines(report))
    return lines


def build_live_diag_lines(
    scan_path: str | os.PathLike[str],
    catalog_path: str | os.PathLike[str] | None = None,
    vehicle: Mapping[str, Any] | None = None,
    *,
    report_path: str | os.PathLike[str] | None = None,
) -> list[str]:
    """Load a just-saved scan and format it, or return a single error line."""

    try:
        report = build_diag_report(scan_path, catalog_path, vehicle)
    except (OSError, ValueError, TypeError) as exc:
        return ["error: {}".format(exc)]
    return format_live_diag_lines(
        report,
        report_path=report_path if report_path is not None else scan_path,
        catalog_path=catalog_path,
    )


def build_diag_catalog_view(path: str | os.PathLike[str]) -> list[str]:
    """Load a catalog and describe its sections, or return an error line."""

    text = str(path).strip()
    if not text:
        return ["error: enter a service catalog JSON path"]
    catalog_path = Path(text)
    if not catalog_path.is_file():
        return ["error: catalog not found: {}".format(catalog_path)]
    try:
        catalog = load_catalog(catalog_path)
    except (OSError, CatalogError) as exc:
        return ["error: {}".format(exc)]

    source = catalog.source
    lines = [
        "catalog: {}".format(catalog_path),
        "source: {}".format(source.kind),
    ]
    if source.generated_at:
        lines.append("generated: {}".format(source.generated_at))
    if source.image:
        lines.append("image: {}".format(source.image))
    if source.language:
        lines.append("language: {}".format(source.language))
    lines.append(
        "dtc descriptions: {} ({} codes)".format(
            len(catalog.dtc_descriptions),
            len({entry.code for entry in catalog.dtc_descriptions}),
        )
    )
    lines.append("timing entries: {}".format(len(catalog.timing)))
    lines.append("profiles: {}".format(len(catalog.profiles)))
    lines.append("scripts: {}".format(len(catalog.scripts)))
    return lines


def _parse_script_plan(data: bytes, suffix: str) -> DiagnosticScript:
    if suffix == ".zip" or data.startswith(b"PK\x03\x04"):
        return parse_script_zip(data)
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = data.decode("utf-16")
    else:
        text = data.decode("utf-8-sig")
    return parse_script_xml(text)


def _format_script_plan(script: DiagnosticScript, path: Path) -> list[str]:
    lines = [
        "script: {}".format(path),
        "docno: {}".format(script.docno or "(none)"),
        "title: {}".format(script.title or "(none)"),
        "type: {}".format(script.script_type or "(none)"),
    ]
    if script.variant_id:
        lines.append("variant: {}".format(script.variant_id))
    if script.language:
        lines.append("language: {}".format(script.language))
    if script.profile_ids:
        lines.append("profiles: {}".format(", ".join(script.profile_ids)))

    lines.append("ECUs: {}".format(len(script.ecus)))
    for plan in script.ecus:
        services = ", ".join(
            "0x{:02X}({})".format(service_id, classify_service(service_id))
            for service_id in plan.service_ids
        ) or "(none)"
        lines.append(
            "  ecu={} type={} tester={} mode={} services={}".format(
                _diag_hex(plan.ecu_address),
                _diag_hex(plan.ecu_type, 1),
                _diag_hex(plan.tester_address),
                plan.mode or "-",
                services,
            )
        )

    requests = script.read_only_requests()
    lines.append(
        "read-only plan: {} request{}".format(
            len(requests), "" if len(requests) == 1 else "s"
        )
    )
    for request in requests:
        lines.append(
            "  ecu={} service=0x{:02X} {} request={}".format(
                _diag_hex(request.ecu_address),
                request.service_id,
                request.service_name,
                request.request or "-",
            )
        )

    dropped = sorted(
        {
            service_id
            for plan in script.ecus
            for service_id in plan.service_ids
            if classify_service(service_id) != "read_only"
        }
    )
    if dropped:
        lines.append(
            "dropped services: {}".format(
                ", ".join(
                    "0x{:02X}({})".format(service_id, classify_service(service_id))
                    for service_id in dropped
                )
            )
        )

    dtc_plan = script.dtc_list
    if dtc_plan is None:
        lines.append("DTC list: (none)")
    else:
        parts = []
        if dtc_plan.list_path:
            parts.append("list={}".format(dtc_plan.list_path))
        if dtc_plan.value_path:
            parts.append("value={}".format(dtc_plan.value_path))
        if dtc_plan.text_path:
            parts.append("text={}".format(dtc_plan.text_path))
        lines.append("DTC list: {}".format(" ".join(parts) or "(none)"))

    lines.append("plan only: nothing was sent to a vehicle")
    return lines


def build_script_plan_view(path: str | os.PathLike[str]) -> list[str]:
    """Parse a diagnostic script ZIP or XML into a plan, or return an error line."""

    text = str(path).strip()
    if not text:
        return ["error: enter a diagnostic script ZIP or XML path"]
    script_path = Path(text)
    if not script_path.is_file():
        return ["error: script not found: {}".format(script_path)]
    try:
        data = script_path.read_bytes()
    except OSError as exc:
        return ["error: cannot read script {}: {}".format(script_path, exc)]

    try:
        script = _parse_script_plan(data, script_path.suffix.lower())
    except (OSError, ValueError, UnicodeDecodeError, DiagnosticScriptError) as exc:
        return ["error: {}".format(exc)]

    return _format_script_plan(script, script_path)


class VolvoTuiApp:
    def __init__(self) -> None:
        self.transport = CanBusConfig("socketcan", "can0", None)
        self.vehicle = VehicleState()
        self.sections = self._build_sections()
        self.section_index = 0
        self.action_index = 0
        self._menu_scroll = 0
        self.logs: list[str] = []
        self.status = "ready"
        self.running = False
        self._last_values: dict[str, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None

    def run(self) -> None:
        curses.wrapper(self._run)

    def _build_sections(self) -> list[SectionSpec]:
        return [
            SectionSpec(
                "Connection",
                (
                    ActionSpec("Probe DiCE", "Detect the adapter through macOS system profiling.", self._probe_dice_system),
                    ActionSpec("Probe DiCE Raw", "Detect the adapter through PyUSB/libusb.", self._probe_dice_raw),
                    ActionSpec("Probe CAN", "Inspect python-can backends and detected configs.", self._probe_can),
                    ActionSpec(
                        "Set Transport",
                        "Store the CAN backend settings used by diagnostic actions.",
                        self._set_transport,
                        (
                            FieldSpec("interface", "CAN interface", self.transport.interface),
                            FieldSpec("channel", "CAN channel", self.transport.channel),
                            FieldSpec("bitrate", "CAN bitrate", ""),
                        ),
                    ),
                ),
            ),
            SectionSpec(
                "Vehicle Profile",
                (
                    ActionSpec(
                        "Create Profile",
                        "Create or refresh the current vehicle profile from the session data.",
                        self._create_profile,
                        (
                            FieldSpec("vin", "VIN", self.vehicle.vin or ""),
                            FieldSpec("year", "Model year", "2013"),
                            FieldSpec("model", "Model", "XC60"),
                        ),
                    ),
                    ActionSpec("Set VIN", "Store a manual VIN for the current session.", self._set_vin, (FieldSpec("vin", "VIN", self.vehicle.vin or ""),)),
                    ActionSpec("List Modules", "Show the built-in Volvo ECU hints.", self._list_modules),
                    ActionSpec("Profile Summary", "Show the current manual vehicle profile state.", self._profile_summary),
                ),
            ),
            SectionSpec(
                "Native DiCE",
                (
                    ActionSpec(
                        "Scan known modules",
                        "Read DTCs from the documented V60 D6 modules on the native 500 kbit/s CAN channel.",
                        self._native_known_scan,
                        (FieldSpec("native_timeout", "Timeout per module in seconds", "1.5"),),
                    ),
                    ActionSpec(
                        "Discover module IDs",
                        "Probe read-only UDS DTC responses from 11-bit IDs on the native 500 kbit/s CAN channel.",
                        self._native_discover,
                        (
                            FieldSpec("native_start", "First request ID", "0x700"),
                            FieldSpec("native_end", "Last request ID", "0x7F7"),
                            FieldSpec("native_timeout", "Timeout per ID in seconds", "1.5"),
                        ),
                    ),
                    ActionSpec(
                        "Scan saved profile",
                        "Read DTCs from module IDs in a saved native DiCE profile.",
                        self._native_profile_scan,
                        (
                            FieldSpec("native_profile", "Profile JSON path", ""),
                            FieldSpec("native_timeout", "Timeout per module in seconds", "1.5"),
                        ),
                    ),
                ),
            ),
            SectionSpec(
                "Diagnostics",
                (
                    ActionSpec(
                        "Readout",
                        "Passively print frames from the configured CAN interface.",
                        self._readout,
                        (FieldSpec("duration", "Duration in seconds", "10"),),
                    ),
                    ActionSpec(
                        "Discover Modules",
                        "Probe ECU addresses with read-DTC requests.",
                        self._discover,
                        (
                            FieldSpec("start", "Start ECU byte", "0x00"),
                            FieldSpec("end", "End ECU byte", "0xFF"),
                            FieldSpec("group", "DTC group byte", "0x1B"),
                            FieldSpec("timeout", "Timeout per ECU in seconds", "0.2"),
                            FieldSpec("spacing", "Delay between probes in seconds", "0.02"),
                        ),
                    ),
                    ActionSpec(
                        "Read DTC",
                        "Read diagnostic trouble code bytes from one or more ECUs.",
                        self._read_dtc,
                        (
                            FieldSpec("ecu", "ECU name or byte list", "ECM"),
                            FieldSpec("group", "DTC group byte", "0x1B"),
                            FieldSpec("timeout", "Timeout in seconds", "1.0"),
                            FieldSpec("verbose", "Verbose other frames", "false"),
                        ),
                    ),
                    ActionSpec(
                        "Trace Faults",
                        "Summarize DTC activity for the known or discovered module set.",
                        self._trace_faults,
                        (
                            FieldSpec("group", "DTC group byte", "0x1B"),
                            FieldSpec("timeout", "Timeout per ECU in seconds", "0.75"),
                        ),
                    ),
                    ActionSpec(
                        "Clear DTC",
                        "Clear DTCs for one ECU after explicit confirmation.",
                        self._clear_dtc,
                        (
                            FieldSpec("ecu", "ECU name or byte", "ECM"),
                            FieldSpec("group", "DTC group byte", "0x1B"),
                            FieldSpec("timeout", "Timeout in seconds", "1.0"),
                            FieldSpec("confirm", "Type CLEAR to confirm", ""),
                        ),
                    ),
                ),
            ),
            SectionSpec(
                "Service Data",
                (
                    ActionSpec(
                        "Load Catalog",
                        "Load and validate a service catalog JSON export.",
                        self._diag_load_catalog,
                        (FieldSpec("diag_catalog", "Catalog JSON", default_diag_catalog_path()),),
                    ),
                    ActionSpec(
                        "Live DTC Read",
                        "Run one read-only scan with the service catalog and show DTC counts, fault trace, and network status.",
                        self._diag_live_read,
                        (
                            FieldSpec("live_catalog", "Catalog JSON", default_diag_catalog_path()),
                            FieldSpec("live_timeout", "Timeout per module in seconds", DEFAULT_LIVE_TIMEOUT),
                            FieldSpec("live_profile", "Saved profile JSON (optional)", default_live_diag_profile_path()),
                        ),
                    ),
                    ActionSpec(
                        "DTC Overview",
                        "List modules and DTCs from a saved scan report, with catalog descriptions when present.",
                        self._diag_dtc_overview,
                        (
                            FieldSpec("diag_scan", "Scan report JSON", default_diag_scan_path()),
                            FieldSpec("diag_catalog", "Catalog JSON (optional)", default_diag_catalog_path()),
                        ),
                    ),
                    ActionSpec(
                        "Fault Trace",
                        "Group saved DTCs by module and fault state.",
                        self._diag_fault_trace,
                        (
                            FieldSpec("diag_scan", "Scan report JSON", default_diag_scan_path()),
                            FieldSpec("diag_catalog", "Catalog JSON (optional)", default_diag_catalog_path()),
                        ),
                    ),
                    ActionSpec(
                        "Network Status",
                        "Summarize responders, silence, and partial responses in a saved scan.",
                        self._diag_network_status,
                        (FieldSpec("diag_scan", "Scan report JSON", default_diag_scan_path()),),
                    ),
                    ActionSpec(
                        "Script Plan",
                        "Parse a diagnostic script ZIP or XML into a read-only plan.",
                        self._script_plan_plan,
                        (FieldSpec("script_plan", "Script ZIP or XML", ""),),
                    ),
                ),
            ),
        ]

    @property
    def current_section(self) -> SectionSpec:
        return self.sections[self.section_index]

    @property
    def current_action(self) -> ActionSpec:
        return self.current_section.actions[self.action_index]

    def _set_status(self, text: str) -> None:
        with self._lock:
            self.status = text

    def _append_log(self, line: str) -> None:
        with self._lock:
            self.logs.append(line)
            if len(self.logs) > 600:
                self.logs[:] = self.logs[-600:]

    def _snapshot(self) -> tuple[str, list[str], VehicleState, CanBusConfig]:
        with self._lock:
            return self.status, list(self.logs), VehicleState(
                vin=self.vehicle.vin,
                profile_created=self.vehicle.profile_created,
                detected_modules=list(self.vehicle.detected_modules),
                last_dtc_modules=list(self.vehicle.last_dtc_modules),
                adapter_summary=list(self.vehicle.adapter_summary),
            ), CanBusConfig(self.transport.interface, self.transport.channel, self.transport.bitrate)

    def _prompt(self, stdscr: curses.window, prompt: str, default: str) -> str:
        height, width = stdscr.getmaxyx()
        row = max(0, height - 2)
        prompt_text = f"{prompt} [{default}]: " if default else f"{prompt}: "
        if len(prompt_text) >= width - 8:
            prompt_text = f"{prompt}: "
        stdscr.move(row, 0)
        stdscr.clrtoeol()
        stdscr.addnstr(row, 0, prompt_text, max(0, width - 1))
        stdscr.refresh()
        curses.echo()
        curses.curs_set(1)
        try:
            raw = stdscr.getstr(row, min(len(prompt_text), max(0, width - 1)), max(1, width - len(prompt_text) - 1))
        finally:
            curses.noecho()
            curses.curs_set(0)
        text = raw.decode(errors="ignore").strip() if raw else ""
        return text or default

    def _launch(self, stdscr: curses.window, action: ActionSpec) -> None:
        values = dict(self._last_values)
        for field in action.fields:
            default = values.get(field.name, field.default)
            values[field.name] = self._prompt(stdscr, field.prompt, default)

        self._last_values.update(values)
        self.running = True
        self._set_status(f"running: {action.name}")
        self._append_log(f"== {action.name} ==")

        def worker() -> None:
            try:
                lines = action.runner(values)
                for line in lines:
                    self._append_log(line)
                self._set_status(f"done: {action.name}")
            except Exception as exc:
                self._append_log(f"error: {type(exc).__name__}: {exc}")
                self._set_status(f"failed: {action.name}")
            finally:
                self.running = False

        self._worker = threading.Thread(target=worker, name=f"tui-{action.name}", daemon=True)
        self._worker.start()

    def _make_bus(self, values: dict[str, str]) -> PythonCanTransport:
        return PythonCanTransport(
            CanBusConfig(
                interface=values.get("interface", self.transport.interface),
                channel=values.get("channel", self.transport.channel),
                bitrate=_optional_int(values.get("bitrate", self.transport.bitrate and str(self.transport.bitrate))),
            )
        )

    def _run_native_scan_result(self, options: list[str], *, profile_path: Path | None = None) -> NativeScanResult:
        # Import only when selected: the CLI also imports this module for its TUI command.
        if __package__ in (None, ""):
            from volvo_p3.cli import main as cli_main
        else:
            from .cli import main as cli_main

        reports = Path.cwd() / "reports"
        reports.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        output = reports / f"dice-scan-{stamp}.json"
        argv = ["dice-scan", *options, "--output", str(output)]
        if profile_path is not None:
            argv.extend(("--save-profile", str(profile_path)))
        self._append_log(f"Command: {shlex.join(argv)}")

        class LiveOutput:
            def __init__(self, prefix: str = "") -> None:
                self.prefix = prefix
                self.pending = ""

            def write(self, text: str) -> int:
                self.pending += text
                while "\n" in self.pending:
                    line, self.pending = self.pending.split("\n", 1)
                    self._emit(line)
                return len(text)

            def flush(self) -> None:
                pass

            def finish(self) -> None:
                if self.pending:
                    self._emit(self.pending)
                    self.pending = ""

            def _emit(self, line: str) -> None:
                self_app._append_log(f"{self.prefix}{line}")

        self_app = self
        captured_stdout = LiveOutput()
        captured_stderr = LiveOutput("stderr: ")
        try:
            with contextlib.redirect_stdout(captured_stdout), contextlib.redirect_stderr(captured_stderr):
                exit_code = cli_main(argv)
        except SystemExit as exc:
            exit_code = exc.code if isinstance(exc.code, int) else 1
        finally:
            captured_stdout.finish()
            captured_stderr.finish()

        if exit_code:
            raise RuntimeError(f"native DiCE scan exited with code {exit_code}")
        lines = [f"Saved scan: {output}"]
        if profile_path is not None and profile_path.exists():
            lines.append(f"Saved module profile: {profile_path}")
            self._last_values["native_profile"] = str(profile_path)
        return NativeScanResult(output, profile_path, tuple(lines))

    def _run_native_scan(self, options: list[str], *, profile_path: Path | None = None) -> list[str]:
        """Run one native scan and return only its log lines."""

        return list(self._run_native_scan_result(options, profile_path=profile_path).lines)

    @staticmethod
    def _native_timeout(values: dict[str, str]) -> str:
        return _timeout_text(values["native_timeout"])

    def _native_known_scan(self, values: dict[str, str]) -> list[str]:
        return self._run_native_scan(["--timeout", self._native_timeout(values)])

    def _native_discover(self, values: dict[str, str]) -> list[str]:
        start = int(values["native_start"], 0)
        end = int(values["native_end"], 0)
        if not 0x700 <= start <= end <= 0x7F7:
            raise ValueError("request ID range must stay within 0x700..0x7F7")
        reports = Path.cwd() / "reports"
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        profile = reports / f"dice-profile-{stamp}.json"
        return self._run_native_scan(
            ["--discover", "--start", hex(start), "--end", hex(end), "--timeout", self._native_timeout(values)],
            profile_path=profile,
        )

    def _native_profile_scan(self, values: dict[str, str]) -> list[str]:
        profile = values["native_profile"].strip()
        if not profile:
            raise ValueError("enter a saved profile JSON path")
        return self._run_native_scan(["--profile", profile, "--timeout", self._native_timeout(values)])

    def _probe_dice_system(self, _values: dict[str, str]) -> list[str]:
        devices = find_dice_system_profiler()
        if not devices:
            self.vehicle.adapter_summary = ["DiCE USB device not found"]
            return ["DiCE USB device not found"]

        lines: list[str] = []
        for device in devices:
            lines.extend(device.format_lines())
            lines.append("")
        self.vehicle.adapter_summary = lines[:-1]
        return lines[:-1]

    def _probe_dice_raw(self, _values: dict[str, str]) -> list[str]:
        lines = [f"pyusb libusb backend: {get_pyusb_backend_status()}"]
        devices = find_dice_pyusb()
        if not devices:
            lines.append("DiCE not visible through PyUSB")
            self.vehicle.adapter_summary = lines[:]
            return lines

        for device in devices:
            lines.extend(device.format_lines())
            lines.append("")
        self.vehicle.adapter_summary = lines[:-1]
        return lines[:-1]

    def _probe_can(self, _values: dict[str, str]) -> list[str]:
        try:
            import can
        except ImportError:
            return ["python-can is not installed"]

        lines = [f"python-can: {can.__version__}", "backends installed in python-can:"]
        lines.extend(f"  {backend}" for backend in sorted(can.interface.BACKENDS))

        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                configs = can.detect_available_configs()
        except Exception as exc:
            lines.append(f"detection failed: {type(exc).__name__}: {exc}")
            return lines

        lines.append("detected configs on this machine:")
        if not configs:
            lines.append("  none")
        else:
            lines.extend(f"  {config}" for config in configs)
        return lines

    def _set_transport(self, values: dict[str, str]) -> list[str]:
        interface = values.get("interface", self.transport.interface)
        channel = values.get("channel", self.transport.channel)
        bitrate = _optional_int(values.get("bitrate"))
        self.transport = CanBusConfig(interface, channel, bitrate)
        return [
            f"transport set interface={interface!r} channel={channel!r} bitrate={bitrate if bitrate is not None else 'default'}",
        ]

    def _set_vin(self, values: dict[str, str]) -> list[str]:
        vin = values.get("vin", "").strip().upper()
        self.vehicle.vin = vin
        self.vehicle.profile_created = bool(vin)
        return [f"vin set to {vin or '(empty)'}"]

    def _create_profile(self, values: dict[str, str]) -> list[str]:
        vin = values.get("vin", "").strip().upper()
        year = values.get("year", "").strip()
        model = values.get("model", "").strip().upper()
        if vin:
            self.vehicle.vin = vin
        self.vehicle.profile_created = True
        modules = ", ".join(hex_byte(ecu) for ecu in self.vehicle.detected_modules) or "(none discovered yet)"
        return [
            f"profile created for {year or '2013'} {model or 'XC60'}",
            f"vin: {self.vehicle.vin or '(unset)'}",
            f"modules: {modules}",
        ]

    def _list_modules(self, _values: dict[str, str]) -> list[str]:
        lines = [f"{name:8s} {hex_byte(ecu)}" for name, ecu in sorted(KNOWN_MODULES.items(), key=lambda item: item[1])]
        self.vehicle.detected_modules = [ecu for ecu in KNOWN_MODULES.values()]
        return lines

    def _profile_summary(self, _values: dict[str, str]) -> list[str]:
        modules = ", ".join(hex_byte(ecu) for ecu in self.vehicle.detected_modules) or "(none)"
        adapter = " | ".join(self.vehicle.adapter_summary[-3:]) if self.vehicle.adapter_summary else "(not probed)"
        return [
            f"vin: {self.vehicle.vin or '(unset)'}",
            f"profile: {'created' if self.vehicle.profile_created else 'not created'}",
            f"transport: interface={self.transport.interface!r} channel={self.transport.channel!r} bitrate={self.transport.bitrate if self.transport.bitrate is not None else 'default'}",
            f"known modules: {modules}",
            f"adapter: {adapter}",
        ]

    def _readout(self, values: dict[str, str]) -> list[str]:
        bus = self._make_bus(values)
        try:
            duration = float(values["duration"])
            deadline = time.monotonic() + duration
            lines = [
                f"opened interface={bus._bus.channel_info if hasattr(bus._bus, 'channel_info') else self.transport.interface!r} "
                f"channel={self.transport.channel!r} duration={duration:g}s",
            ]
            while time.monotonic() < deadline:
                frame = bus.recv(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
                if frame is not None:
                    lines.append(f"{hex_id(frame.arbitration_id)}  {frame.data.hex(' ').upper()}")
            if len(lines) == 1:
                lines.append("no frames observed")
            return lines
        finally:
            bus.close()

    def _discover(self, values: dict[str, str]) -> list[str]:
        bus = self._make_bus(values)
        try:
            start = int(values["start"], 0)
            end = int(values["end"], 0)
            group = int(values["group"], 0)
            timeout = float(values["timeout"])
            spacing = float(values["spacing"])
            found: list[int] = []
            lines: list[str] = []
            for ecu in range(start, end + 1):
                request = build_read_dtc_request(ecu, group)
                bus.send(request)
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    frame = bus.recv(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
                    if frame and is_response_to(frame, ecu, SERVICE_READ_DTC):
                        found.append(ecu)
                        lines.append(
                            f"found ecu={hex_byte(ecu)} response_id={hex_id(frame.arbitration_id)} frame={frame.data.hex(' ').upper()}"
                        )
                        break
                time.sleep(spacing)
            self.vehicle.detected_modules = sorted(set(found) | set(self.vehicle.detected_modules))
            if not lines:
                lines.append("no modules responded")
            return lines
        finally:
            bus.close()

    def _read_dtc(self, values: dict[str, str]) -> list[str]:
        ecus = _parse_ecu_list(values["ecu"])
        bus = self._make_bus(values)
        try:
            group = int(values["group"], 0)
            timeout = float(values["timeout"])
            verbose = values.get("verbose", "").lower() in {"1", "true", "yes", "on"}
            self.vehicle.last_dtc_modules = list(ecus)
            lines: list[str] = []
            for ecu in ecus:
                request = build_read_dtc_request(ecu, group)
                lines.append(f"tx ecu={hex_byte(ecu)} service=AE frame={request.data.hex(' ').upper()}")
                bus.send(request)

                deadline = time.monotonic() + timeout
                got_response = False
                while time.monotonic() < deadline:
                    frame = bus.recv(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
                    if frame is None:
                        continue
                    if not is_response_to(frame, ecu, SERVICE_READ_DTC):
                        if verbose:
                            lines.append(f"rx other {hex_id(frame.arbitration_id)} {frame.data.hex(' ').upper()}")
                        continue

                    got_response = True
                    response = parse_dtc_response(frame)
                    if response is None:
                        lines.append(f"rx ecu={hex_byte(ecu)} response_id={hex_id(frame.arbitration_id)} frame={frame.data.hex(' ').upper()}")
                    else:
                        lines.append(
                            "rx "
                            f"ecu={hex_byte(response.ecu)} "
                            f"response_id={hex_id(response.response_id)} "
                            f"service={hex_byte(response.service)} "
                            f"payload={response.raw_payload_hex or '(empty)'} "
                            f"frame={response.raw_frame_hex}"
                        )
                if not got_response:
                    lines.append(f"no DTC response from ecu={hex_byte(ecu)}")
            return lines
        finally:
            bus.close()

    def _trace_faults(self, values: dict[str, str]) -> list[str]:
        ecus = self.vehicle.detected_modules or list(KNOWN_MODULES.values())
        bus = self._make_bus(values)
        try:
            group = int(values["group"], 0)
            timeout = float(values["timeout"])
            lines: list[str] = []
            for ecu in ecus:
                request = build_read_dtc_request(ecu, group)
                bus.send(request)
                deadline = time.monotonic() + timeout
                matched = False
                while time.monotonic() < deadline:
                    frame = bus.recv(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
                    if frame and is_response_to(frame, ecu, SERVICE_READ_DTC):
                        matched = True
                        response = parse_dtc_response(frame)
                        if response is None:
                            lines.append(f"{hex_byte(ecu)}: DTC response {frame.data.hex(' ').upper()}")
                        elif response.raw_payload:
                            lines.append(f"{hex_byte(ecu)}: fault data {response.raw_payload_hex}")
                        else:
                            lines.append(f"{hex_byte(ecu)}: no DTC payload")
                        break
                if not matched:
                    lines.append(f"{hex_byte(ecu)}: no response")
            return lines
        finally:
            bus.close()

    def _clear_dtc(self, values: dict[str, str]) -> list[str]:
        if values.get("confirm", "").strip().upper() != "CLEAR":
            return ["clear aborted: type CLEAR to confirm"]

        ecu = parse_ecu(values["ecu"])
        bus = self._make_bus(values)
        try:
            group = int(values["group"], 0)
            timeout = float(values["timeout"])
            request = build_clear_dtc_request(ecu, group)
            lines = [f"tx ecu={hex_byte(ecu)} service=AF frame={request.data.hex(' ').upper()}"]
            bus.send(request)

            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                frame = bus.recv(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
                if frame and is_response_to(frame, ecu, SERVICE_CLEAR_DTC):
                    lines.append(f"rx ecu={hex_byte(ecu)} response_id={hex_id(frame.arbitration_id)} frame={frame.data.hex(' ').upper()}")
                    return lines
            lines.append(f"no clear-DTC acknowledgement from ecu={hex_byte(ecu)}")
            return lines
        finally:
            bus.close()

    def _diag_vehicle(self) -> dict[str, str] | None:
        return {"vin": self.vehicle.vin} if self.vehicle.vin else None

    def _diag_load_catalog(self, values: dict[str, str]) -> list[str]:
        return build_diag_catalog_view(values.get("diag_catalog", ""))

    def _diag_live_read(self, values: dict[str, str]) -> list[str]:
        catalog = values.get("live_catalog", "")
        try:
            options = build_live_scan_options(
                catalog,
                values.get("live_timeout", DEFAULT_LIVE_TIMEOUT),
                values.get("live_profile", ""),
            )
        except (OSError, ValueError) as exc:
            return ["error: {}".format(exc)]

        try:
            result = self._run_native_scan_result(options)
        except (OSError, RuntimeError, ValueError) as exc:
            return ["error: live DTC read failed: {}".format(exc)]

        lines = list(result.lines)
        lines.extend(
            build_live_diag_lines(
                result.report_path,
                catalog,
                self._diag_vehicle(),
                report_path=result.report_path,
            )
        )
        return lines

    def _diag_dtc_overview(self, values: dict[str, str]) -> list[str]:
        return diag_dtc_overview_lines(
            values.get("diag_scan", ""),
            values.get("diag_catalog", ""),
            self._diag_vehicle(),
        )

    def _diag_fault_trace(self, values: dict[str, str]) -> list[str]:
        return diag_fault_trace_lines(
            values.get("diag_scan", ""),
            values.get("diag_catalog", ""),
            self._diag_vehicle(),
        )

    def _diag_network_status(self, values: dict[str, str]) -> list[str]:
        return diag_network_status_lines(
            values.get("diag_scan", ""),
            vehicle=self._diag_vehicle(),
        )

    def _script_plan_plan(self, values: dict[str, str]) -> list[str]:
        return build_script_plan_view(values.get("script_plan", ""))

    def _draw(self, stdscr: curses.window) -> None:
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        left_width = max(30, min(width // 3, 42))
        divider = left_width
        content_x = divider + 1

        for y in range(height):
            if divider < width:
                stdscr.addch(y, divider, curses.ACS_VLINE)

        status, logs, vehicle, transport = self._snapshot()
        stdscr.addnstr(0, 1, "Volvo P3 Diagnostics", max(0, left_width - 2), curses.A_BOLD)
        status_line = f"{status} | {transport.interface}:{transport.channel}"
        if transport.bitrate is not None:
            status_line += f" @{transport.bitrate}"
        if vehicle.vin:
            status_line += f" | VIN {vehicle.vin}"
        stdscr.addnstr(0, content_x + 1, status_line, max(0, width - content_x - 2), curses.A_BOLD)

        menu_rows: list[tuple[str, int, int, bool]] = []
        for sidx, section in enumerate(self.sections):
            menu_rows.append((section.title, sidx, -1, True))
            for aidx, action in enumerate(section.actions):
                menu_rows.append((action.name, sidx, aidx, False))
            menu_rows.append(("", sidx, -1, False))
        selected_row = next(
            index for index, (_, sidx, aidx, _) in enumerate(menu_rows)
            if sidx == self.section_index and aidx == self.action_index
        )
        menu_height = max(1, height - 7)
        if selected_row < self._menu_scroll:
            self._menu_scroll = selected_row
        elif selected_row >= self._menu_scroll + menu_height:
            self._menu_scroll = selected_row - menu_height + 1
        for row, (label, sidx, aidx, is_heading) in enumerate(
            menu_rows[self._menu_scroll : self._menu_scroll + menu_height], start=2
        ):
            if is_heading:
                stdscr.addnstr(row, 1, label, max(0, left_width - 2), curses.A_UNDERLINE)
            elif label:
                active = sidx == self.section_index and aidx == self.action_index
                marker = ">" if active else " "
                attr = curses.A_REVERSE if active else curses.A_NORMAL
                stdscr.addnstr(row, 1, f"{marker} {label}", max(0, left_width - 2), attr)

        selected = self.current_action
        detail_lines = [
            selected.description,
            "",
            "Fields:",
        ]
        if selected.fields:
            for field in selected.fields:
                value = self._last_values.get(field.name, field.default)
                detail_lines.append(f"  {field.prompt}: {value or '(blank)'}")
        else:
            detail_lines.append("  none")
        detail_lines.extend(
            [
                "",
                "Vehicle profile:",
                f"  VIN: {vehicle.vin or '(unset)'}",
                f"  Profile: {'created' if vehicle.profile_created else 'not created'}",
                f"  Known modules: {', '.join(hex_byte(ecu) for ecu in vehicle.detected_modules) or '(none)'}",
                "",
                "Recent log:",
            ]
        )

        preview = logs[-max(6, height // 3) :]
        if preview:
            detail_lines.extend(f"  {line}" for line in preview)
        else:
            detail_lines.append("  none")

        row = 2
        for line in detail_lines:
            if row >= height - 3:
                break
            stdscr.addnstr(row, content_x + 1, line, max(0, width - content_x - 2))
            row += 1

        footer = "Left/Right section  Up/Down action  Enter run  c clear log  q quit"
        stdscr.addnstr(height - 2, 1, footer, max(0, width - 2), curses.A_DIM)
        stdscr.refresh()

    def _wait_for_worker(self) -> None:
        worker = self._worker
        if worker and worker.is_alive():
            worker.join(timeout=0.1)

    def _run(self, stdscr: curses.window) -> None:
        curses.curs_set(0)
        stdscr.keypad(True)
        stdscr.nodelay(False)

        while not self._stop.is_set():
            self._draw(stdscr)
            key = stdscr.getch()
            if key in (ord("q"), ord("Q")):
                self._stop.set()
            elif key in (ord("c"), ord("C")):
                with self._lock:
                    self.logs.clear()
                    self.status = "log cleared"
            elif key in (curses.KEY_LEFT, ord("h")):
                self.section_index = (self.section_index - 1) % len(self.sections)
                self.action_index = 0
            elif key in (curses.KEY_RIGHT, ord("l")):
                self.section_index = (self.section_index + 1) % len(self.sections)
                self.action_index = 0
            elif key in (curses.KEY_UP, ord("k")):
                self.action_index = max(0, self.action_index - 1)
            elif key in (curses.KEY_DOWN, ord("j")):
                self.action_index = min(len(self.current_section.actions) - 1, self.action_index + 1)
            elif key in (curses.KEY_ENTER, 10, 13, ord("r"), ord("R")):
                if not self.running:
                    self._launch(stdscr, self.current_action)

        self._wait_for_worker()


def run_tui() -> None:
    VolvoTuiApp().run()
