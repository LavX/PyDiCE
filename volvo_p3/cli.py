"""Command-line Volvo P3 diagnostic tool."""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import math
import re
import sys
import time
import zipfile
from pathlib import Path

from .can_transport import CanBusConfig, PythonCanTransport
from .diagnostics import build_report, fault_trace, network_status
from .dice_commands import CATEGORIES, iter_commands
from .dice_installer import extract_setup_dice
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
from .tui import run_tui
from .diag_catalog import (
    SCHEMA_NAME as DIAG_CATALOG_SCHEMA,
    SCHEMA_VERSION as DIAG_CATALOG_VERSION,
    CatalogError,
    load_catalog,
    normalize_dtc_code,
)
from .script_runner import (
    STATUS_ERROR,
    STATUS_PLANNED,
    STATUS_RESPONSE,
    AddressMap,
    AddressMapError,
    NameMapReport,
    RunPlan,
    RunResult,
    build_run_plan,
    load_profile_name_map,
    parse_address_map,
    run_plan,
)
from .script_plan import (
    DiagnosticScriptError,
    classify_service,
    is_read_only_service,
    parse_script_xml,
    parse_script_zip,
)


def add_bus_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--interface", default="socketcan", help="python-can interface, e.g. socketcan, slcan, pcan")
    parser.add_argument("--channel", default="can0", help="python-can channel, e.g. can0 or /dev/tty.usbserial")
    parser.add_argument("--bitrate", type=int, default=None, help="CAN bitrate if required by the backend")


def open_transport(args: argparse.Namespace) -> PythonCanTransport:
    return PythonCanTransport(CanBusConfig(args.interface, args.channel, args.bitrate))


def cmd_probe_can(args: argparse.Namespace) -> int:
    try:
        import can
    except ImportError:
        print("python-can is not installed", file=sys.stderr)
        return 1

    print(f"python-can: {can.__version__}")
    print("backends installed in python-can:")
    for backend in sorted(can.interface.BACKENDS):
        print(f"  {backend}")

    if args.detect:
        print("detected configs on this machine:")
        try:
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                configs = can.detect_available_configs()
        except Exception as exc:
            print(f"  detection failed: {type(exc).__name__}: {exc}")
            return 1
        if not configs:
            print("  none")
        for config in configs:
            print(f"  {config}")
    return 0


def cmd_list_modules(args: argparse.Namespace) -> int:
    for name, ecu in sorted(KNOWN_MODULES.items(), key=lambda item: item[1]):
        print(f"{name:8s} {hex_byte(ecu)}")
    return 0


def cmd_list_dice_commands(args: argparse.Namespace) -> int:
    for command in iter_commands(args.category):
        line = (
            f"0x{command.command_id:02X}  {command.category:9s}  {command.direction:16s}  "
            f"{command.source:10s}  {command.name}"
        )
        if args.types:
            request = command.request_type or "-"
            reply = command.reply_type or "-"
            line += f"  {request} -> {reply}"
        print(line)
    return 0


def cmd_probe_dice(args: argparse.Namespace) -> int:
    try:
        devices = find_dice_system_profiler()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not devices:
        print("DiCE USB device not found")
        return 1

    for device in devices:
        print("\n".join(device.format_lines()))
    return 0


def cmd_probe_dice_raw(args: argparse.Namespace) -> int:
    print(f"pyusb libusb backend: {get_pyusb_backend_status()}")
    try:
        devices = find_dice_pyusb()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not devices:
        print("DiCE not visible through PyUSB")
        return 1

    for device in devices:
        print("\n".join(device.format_lines()))
    return 0


def cmd_dice_info(args: argparse.Namespace) -> int:
    import json
    from .dice_rpc import DiceRpc
    with DiceRpc() as device:
        print(json.dumps(device.system_info(), indent=2))
    return 0


def cmd_dice_read_dtc(args: argparse.Namespace) -> int:
    import json
    from datetime import datetime, timezone
    from .dice_rpc import DiceRpc
    from .dice_can import DiceCanTransport
    from .obd_read import scan_obd

    if not 0.1 <= args.timeout <= 10:
        raise RuntimeError("timeout must be between 0.1 and 10 seconds")
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "generic emissions OBD, 500 kbit/s, 11-bit CAN",
              "usb_trace": [], "status": "incomplete"}

    class RecordingRpc(DiceRpc):
        def write_packet(self, payload, timeout=2.0):
            report["usb_trace"].append({"direction": "tx", "payload": payload.hex()})
            return super().write_packet(payload, timeout)
        def read_packet(self, timeout=2.0):
            payload = super().read_packet(timeout)
            if payload is not None:
                report["usb_trace"].append({"direction": "rx", "payload": payload.hex()})
            return payload

    try:
        with RecordingRpc() as rpc:
            report["adapter"] = rpc.system_info()
            with DiceCanTransport(rpc) as bus:
                report["scan"] = scan_obd(bus, discovery_timeout=args.timeout,
                                          response_timeout=args.timeout)
            report["adapter_after"] = rpc.system_info()
        modes = [mode for ecu in report["scan"]["ecus"] for mode in ecu["modes"].values()]
        report["status"] = "read" if any(mode["status"] == "ok" for mode in modes) else "no_dtc_readout"
    except (RuntimeError, OSError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "read" else 1


def cmd_dice_hybrid_dtc(args: argparse.Namespace) -> int:
    import json
    from datetime import datetime, timezone
    from .dice_rpc import DiceRpc
    from .dice_can import DiceCanTransport, P3_D6_MODULES
    from .uds_read import read_uds_dtcs

    if not 0.1 <= args.timeout <= 10:
        raise RuntimeError("timeout must be between 0.1 and 10 seconds")
    names = args.module or ["BECM", "IEM", "IGM", "DCDC", "OBC", "ACCM", "CEM", "ECM", "TCM", "DIM"]
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "P3 V60 D6 candidate module addresses, default-session UDS DTC read",
              "address_source": "https://www.elektroda.com/rtvforum/topic3626792.html#18318959",
              "identity_note": "Module labels from source, response IDs assumed request+8 until observed",
              "usb_trace": [], "modules": [], "status": "incomplete"}

    class RecordingRpc(DiceRpc):
        def write_packet(self, payload, timeout=2.0):
            report["usb_trace"].append({"direction": "tx", "payload": payload.hex()})
            return super().write_packet(payload, timeout)
        def read_packet(self, timeout=2.0):
            payload = super().read_packet(timeout)
            if payload is not None:
                report["usb_trace"].append({"direction": "rx", "payload": payload.hex()})
            return payload

    try:
        with RecordingRpc() as rpc:
            report["adapter"] = rpc.system_info()
            with DiceCanTransport(rpc, uds_requests=[P3_D6_MODULES[name] for name in names]) as bus:
                for name in names:
                    request_id = P3_D6_MODULES[name]
                    result = read_uds_dtcs(bus, request_id, request_id + 8, timeout=args.timeout)
                    report["modules"].append({"module": name, **result})
            report["adapter_after"] = rpc.system_info()
        report["status"] = "read" if any(m["status"] == "ok" for m in report["modules"]) else "no_dtc_readout"
    except (RuntimeError, OSError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "read" else 1


def cmd_dice_scan(args: argparse.Namespace) -> int:
    """Scan explicitly selected physical UDS targets without changing sessions."""
    import json
    from datetime import datetime, timezone
    from .dice_rpc import DiceRpc
    from .dice_can import DiceCanTransport
    from .module_scan import (default_module_targets, load_module_profile,
                              build_discovery_targets, scan_modules)

    if not 0.1 <= args.timeout <= 10:
        raise ValueError("timeout must be between 0.1 and 10 seconds")
    if args.profile:
        targets = load_module_profile(args.profile)
    elif args.discover:
        targets = build_discovery_targets(args.start, args.end, args.response_offset)
    else:
        targets = default_module_targets()
    output = args.output or Path("reports") / (datetime.now().strftime("dice-scan-%Y%m%d-%H%M%S-%f") + ".json")
    if output.exists():
        raise ValueError(f"output already exists: {output}")
    if args.save_profile and args.save_profile.resolve() == output.resolve():
        raise ValueError("profile and report must use different paths")
    if args.save_profile and args.save_profile.exists() and not args.replace_profile:
        raise ValueError(f"profile already exists: {args.save_profile}. Use --profile {args.save_profile} to read it, or --replace-profile with --save-profile to refresh it.")
    if args.replace_profile and not args.save_profile:
        raise ValueError("--replace-profile requires --save-profile")
    output.parent.mkdir(parents=True, exist_ok=True)
    report = {"started_at": datetime.now(timezone.utc).isoformat(),
              "scope": "11-bit UDS, 500 kbit/s, current DiCE CAN channel",
              "coverage_note": "Silence does not establish module absence. Other buses, protocols and response mappings are not automatically detected.",
              "status": "incomplete", "modules": []}

    def save():
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(report, indent=2) + "\n")
        temporary.replace(output)

    def progress(index, total, target, result):
        report["modules"].append(result)
        save()
        print(f"[{index}/{total}] {target.name} 0x{target.request_id:03X} -> 0x{target.response_id:03X}: {result['status']}", flush=True)
        availability = result.get("status_availability_mask", 0)
        for dtc in result.get("dtcs") or []:
            effective = dtc["status_byte"] & availability
            if args.all_records or effective & 0xAF:
                bits = dtc.get("set_effective_status_bits", [])
                print(f"  {dtc['code']}-{dtc['failure_type_byte']:02X} raw={dtc['status_hex']} effective=0x{effective:02X} {', '.join(bits)}", flush=True)

    save()
    scan_completed = False
    try:
        with DiceRpc() as rpc:
            report["adapter"] = rpc.system_info()
            with DiceCanTransport(rpc, uds_requests=[t.request_id for t in targets]) as bus:
                result = scan_modules(bus, targets, timeout=args.timeout, progress=progress)
                report.update(result)
                scan_completed = True
            report["adapter_after"] = rpc.system_info()
    except KeyboardInterrupt:
        report["status"] = "interrupted"
    except (RuntimeError, OSError, ValueError) as exc:
        report["status"] = "error"
        report["error"] = str(exc)
    finally:
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        catalog_path = getattr(args, "diag_catalog", None)
        if catalog_path:
            try:
                report["diagnostics"] = _diag_report_section(report, catalog_path)
            except (ValueError, OSError) as exc:
                report["diagnostics_error"] = str(exc)
        save()
    if args.save_profile and scan_completed and report["status"] not in ("error", "interrupted"):
        responded = {m["request_id"] for m in report["modules"]
                     if m.get("request_response_received") is True}
        profile = {"name": "Discovered UDS modules", "modules": [
            {"name": t.name, "request_id": f"0x{t.request_id:03X}", "response_id": f"0x{t.response_id:03X}"}
            for t in targets if t.request_id in responded]}
        if profile["modules"]:
            args.save_profile.parent.mkdir(parents=True, exist_ok=True)
            temporary_profile = args.save_profile.with_suffix(args.save_profile.suffix + ".tmp")
            temporary_profile.write_text(json.dumps(profile, indent=2) + "\n")
            temporary_profile.replace(args.save_profile)
            print(f"Profile: {args.save_profile} ({len(profile['modules'])} responding targets)")
        else:
            print("No matching diagnostic responses; no reusable profile written.")
    elif args.save_profile:
        print("Discovery was incomplete; the existing profile was left unchanged.")
    print(f"Report: {output}")
    print(f"Status: {report['status']}; summary: {report.get('summary', {})}")
    if report["status"] == "interrupted":
        return 130
    return 0 if any(m.get("status") == "ok" for m in report["modules"]) and report["status"] != "error" else 1


def cmd_extract_dice_installer(args: argparse.Namespace) -> int:
    extracted = extract_setup_dice(args.installer, args.output, args.arch, unpack=not args.no_unpack)
    for path in extracted:
        print(path)
    return 0


# Diagnostic data read-only commands ---------------------------------------------------
#
# These commands only read files that are already on disk. They do not open a
# CAN channel, contact a vehicle, execute a diagnostic script, or write to the source image
# image. The catalog and script modules own the parsing rules; this layer keeps
# the text output and the JSON envelope stable.


def _load_diag_catalog(path: Path):
    if not path.exists():
        raise ValueError(f"catalog not found: {path}")
    if not path.is_file():
        raise ValueError(f"catalog is not a file: {path}")
    try:
        return load_catalog(path)
    except CatalogError as exc:
        raise ValueError(f"invalid catalog {path}: {exc}") from exc
    except UnicodeError as exc:
        raise ValueError(f"catalog is not valid UTF-8: {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read catalog {path}: {exc}") from exc


def _load_json_object(path: Path, kind: str) -> dict:
    if not path.exists():
        raise ValueError(f"{kind} not found: {path}")
    if not path.is_file():
        raise ValueError(f"{kind} is not a file: {path}")
    try:
        with path.open("r", encoding="utf-8") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"{kind} is not valid JSON: {path}: {exc.msg} "
            f"(line {exc.lineno}, column {exc.colno})"
        ) from exc
    except UnicodeError as exc:
        raise ValueError(f"{kind} is not valid UTF-8: {path}: {exc}") from exc
    except OSError as exc:
        raise ValueError(f"cannot read {kind} {path}: {exc}") from exc
    if not isinstance(document, dict):
        raise ValueError(f"{kind} must be a JSON object: {path}")
    return document


def _parse_vehicle_profile(value):
    if value is None:
        return None
    text = value.strip()
    if not text:
        raise ValueError("vehicle profile is empty")
    if text.startswith("{"):
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "vehicle profile is not valid JSON: {} (line {}, column {})".format(
                    exc.msg, exc.lineno, exc.colno
                )
            ) from exc
        if not isinstance(document, dict):
            raise ValueError("vehicle profile must be a JSON object")
        return document
    path = Path(text)
    if not path.exists():
        raise ValueError(
            f"vehicle profile not found: {text} "
            "(pass inline JSON or a JSON file)"
        )
    return _load_json_object(path, "vehicle profile")


def _diag_report_section(scan, catalog_path) -> dict:
    """Build the optional diagnostic section attached to a live scan report."""
    catalog = _load_diag_catalog(Path(catalog_path))
    return build_report(scan, _catalog_report_mapping(catalog)).to_dict(
        include_raw=False
    )


def _catalog_report_mapping(catalog) -> dict:
    """Translate a service catalog into the shape diagnostics.build_report reads.

    Entries with an ECU label are scoped to that module. A seven-character
    DTC code carries the two-digit failure type after the five-character DTC,
    so the same text is also registered under the ``CODE-XX`` key that the
    diagnostic view looks up. No text is generated here; an ambiguous key is
    dropped instead of picking one of the catalog's rows.
    """

    global_dtcs: dict = {}
    module_dtcs: dict = {}
    conflicts = []

    def add(index, code, text):
        key = code.strip().upper()
        existing = index.get(key)
        if existing is not None and existing != text:
            conflicts.append((index, key))
            return
        index[key] = text

    for entry in catalog.dtc_descriptions:
        text = entry.long_text or entry.short_text
        if not text:
            continue
        index = (
            module_dtcs.setdefault(entry.ecu.strip().upper(), {})
            if entry.ecu
            else global_dtcs
        )
        codes = [entry.code]
        if len(entry.code) == 7:
            codes.append("{}-{}".format(entry.code[:5], entry.code[5:]))
        for code in codes:
            add(index, code, text)

    for index, key in conflicts:
        index.pop(key, None)

    return {
        "dtcs": global_dtcs,
        "modules": {
            name: index for name, index in module_dtcs.items() if index
        },
    }


def _catalog_source_dict(source) -> dict:
    return {
        "kind": source.kind,
        "generated_at": source.generated_at,
        "image": source.image,
        "extractor": source.extractor,
        "language": source.language,
        "notes": list(source.notes),
        "databases": [
            {"name": item.name, "path": item.path, "sha256": item.sha256}
            for item in source.databases
        ],
    }


def _catalog_summary(catalog, path: Path) -> dict:
    return {
        "path": str(path),
        "schema": DIAG_CATALOG_SCHEMA,
        "version": DIAG_CATALOG_VERSION,
        "source": _catalog_source_dict(catalog.source),
        "counts": {
            "dtc_descriptions": len(catalog.dtc_descriptions),
            "timing": len(catalog.timing),
            "profiles": len(catalog.profiles),
            "scripts": len(catalog.scripts),
            "symptom_definitions": len(catalog.symptom_definitions),
            "freeze_frames": len(catalog.freeze_frames),
        },
    }


def _dtc_description_dict(entry) -> dict:
    return {
        "code": entry.code,
        "ecu": entry.ecu,
        "long_text": entry.long_text,
        "short_text": entry.short_text,
        "symptoms": list(entry.symptoms),
        "causes": list(entry.causes),
        "language": entry.language,
        "source": entry.source,
    }


def _print_catalog(catalog, path: Path) -> None:
    source = catalog.source
    print(f"Catalog: {path}")
    print(f"Schema: {DIAG_CATALOG_SCHEMA} version {DIAG_CATALOG_VERSION}")
    print(f"Source kind: {source.kind}")
    if source.generated_at:
        print(f"Generated: {source.generated_at}")
    if source.image:
        print(f"Image: {source.image}")
    if source.extractor:
        print(f"Extractor: {source.extractor}")
    if source.language:
        print(f"Language: {source.language}")
    print(f"Databases: {len(source.databases)}")
    for database in source.databases:
        print(f"  {database.name}  {database.path}")
    if source.notes:
        print("Notes:")
        for note in source.notes:
            print(f"  - {note}")
    print("Counts:")
    print(f"  DTC descriptions: {len(catalog.dtc_descriptions)}")
    print(f"  Timing rows: {len(catalog.timing)}")
    print(f"  Profiles: {len(catalog.profiles)}")
    print(f"  Scripts: {len(catalog.scripts)}")
    print(f"  Symptom definitions: {len(catalog.symptom_definitions)}")
    print(f"  Freeze frames: {len(catalog.freeze_frames)}")


def _print_catalog_lookup(entry, code: str) -> None:
    print(f"DTC {code}:")
    print(f"  ECU: {entry.ecu or 'global'}")
    if entry.long_text:
        print(f"  Long: {entry.long_text}")
    if entry.short_text:
        print(f"  Short: {entry.short_text}")
    if entry.symptoms:
        print("  Symptoms:")
        for symptom in entry.symptoms:
            print(f"    - {symptom}")
    if entry.causes:
        print("  Causes:")
        for cause in entry.causes:
            print(f"    - {cause}")
    if entry.language:
        print(f"  Language: {entry.language}")
    if entry.source:
        print(f"  Source: {entry.source}")


def cmd_diag_catalog(args: argparse.Namespace) -> int:
    path = Path(args.catalog)
    catalog = _load_diag_catalog(path)

    normalized = None
    entry = None
    if args.code:
        normalized = normalize_dtc_code(args.code)
        if normalized is None:
            print(f"not a diagnostic trouble code: {args.code}", file=sys.stderr)
            return 1
        entry = catalog.lookup_dtc(normalized)

    if args.json:
        payload = _catalog_summary(catalog, path)
        if normalized is not None:
            payload["lookup"] = {
                "code": normalized,
                "found": entry is not None,
                "description": _dtc_description_dict(entry) if entry else None,
            }
        print(json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False))
    else:
        _print_catalog(catalog, path)
        if normalized is not None and entry is not None:
            print()
            _print_catalog_lookup(entry, normalized)

    if normalized is not None and entry is None:
        print(f"no catalog description for {normalized}", file=sys.stderr)
        return 1
    return 0


def _format_id(value) -> str:
    return "-" if value is None else "0x{:X}".format(value)


def _format_byte(value) -> str:
    return "-" if value is None else "0x{:02X}".format(value)


def _format_dtc(code: str, failure_type) -> str:
    if failure_type is None:
        return code
    return f"{code}-{failure_type:02X}"


def _print_report_modules(report) -> None:
    if not report.modules:
        print("Modules: none")
        return
    print(f"Modules ({report.module_count}):")
    for module in report.modules:
        ids = "{} -> {}".format(
            _format_id(module.request_id), _format_id(module.response_id)
        )
        print(
            f"  {module.name:10s} {ids:17s} status={module.status:16s} "
            f"dtcs={module.dtc_count}"
        )


def _print_report_network(report) -> None:
    network = network_status(report)
    print(
        "Network: {}/{} responders ({:.0%})".format(
            network.responder_count, network.total_modules, network.coverage_ratio
        )
    )
    print(f"  responders: {', '.join(network.responders) or '-'}")
    print(
        "  no response ({}): {}".format(
            len(network.no_response), ", ".join(network.no_response) or "-"
        )
    )
    print(
        "  incomplete ({}): {}".format(
            len(network.incomplete), ", ".join(network.incomplete) or "-"
        )
    )
    print(
        "  negative response ({}): {}".format(
            len(network.negative_response),
            ", ".join(network.negative_response) or "-",
        )
    )
    print(
        "  other ({}): {}".format(
            len(network.other), ", ".join(network.other) or "-"
        )
    )
    print(f"  coverage: {network.coverage_note}")


def _print_report_faults(report) -> None:
    groups = fault_trace(report)
    if not groups:
        print("Fault trace: none")
        return
    print("Fault trace:")
    for group in groups:
        print(f"  {group.module}  {group.status} ({group.count})")
        for dtc in group.dtcs:
            status_byte = (
                dtc.effective_status_byte
                if dtc.effective_status_byte is not None
                else dtc.status_byte
            )
            line = "    {}  status={}".format(
                _format_dtc(dtc.code, dtc.failure_type),
                _format_byte(status_byte),
            )
            if dtc.description:
                line += f"  {dtc.description}"
            print(line)


def _print_report_dtcs(report) -> None:
    records = [
        (module, dtc) for module in report.modules for dtc in module.dtcs
    ]
    if not records:
        print("DTCs: none")
        return
    print(f"DTCs ({len(records)}):")
    for module, dtc in records:
        print(
            "  {:<10s} {:<12s} {:<10s} {}".format(
                module.name,
                _format_dtc(dtc.code, dtc.failure_type),
                dtc.classification,
                dtc.description or "-",
            )
        )


def _print_report(report, path: Path, view: str, catalog_loaded: bool) -> None:
    print(f"Scan: {report.name or path}")
    if report.scan_status:
        print(f"Status: {report.scan_status}")
    if report.scope:
        print(f"Scope: {report.scope}")
    if report.vehicle:
        vehicle = ", ".join(
            f"{key}={report.vehicle[key]}" for key in sorted(report.vehicle)
        )
        print(f"Vehicle: {vehicle}")
    print(
        "Modules: {}  DTCs: {}  Described: {}  Current faults: {}".format(
            report.module_count,
            report.dtc_count,
            report.described_dtc_count,
            report.current_fault_count,
        )
    )
    if not catalog_loaded:
        print("Catalog: not loaded; DTC descriptions are unavailable")
    if view == "all":
        print()
        _print_report_modules(report)
    if view in ("all", "network"):
        print()
        _print_report_network(report)
    if view in ("all", "faults"):
        print()
        _print_report_faults(report)
    if view == "dtcs":
        print()
        _print_report_dtcs(report)


def _report_payload(report, view: str) -> dict:
    full = report.to_dict(include_raw=False)
    payload = {
        "view": view,
        "name": report.name,
        "scan_status": report.scan_status,
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "scope": report.scope,
        "coverage_note": report.coverage_note,
        "vehicle": full["vehicle"],
        "counts": full["counts"],
    }
    if view in ("all", "network"):
        payload["network"] = network_status(report).to_dict()
    if view in ("all", "faults"):
        payload["fault_trace"] = [
            group.to_dict(include_raw=False) for group in fault_trace(report)
        ]
    if view == "all":
        payload["modules"] = [
            module.to_dict(include_raw=False) for module in report.modules
        ]
    if view == "dtcs":
        payload["dtcs"] = [
            dtc.to_dict(include_raw=False)
            for module in report.modules
            for dtc in module.dtcs
        ]
    return payload


def cmd_diag_report(args: argparse.Namespace) -> int:
    path = Path(args.scan)
    scan = _load_json_object(path, "scan report")
    catalog = _load_diag_catalog(Path(args.catalog)) if args.catalog else None
    vehicle = _parse_vehicle_profile(args.vehicle)
    catalog_mapping = _catalog_report_mapping(catalog) if catalog else None
    try:
        report = build_report(scan, catalog=catalog_mapping, vehicle=vehicle)
    except TypeError as exc:
        raise ValueError(f"invalid scan report {path}: {exc}") from exc

    if args.json:
        print(
            json.dumps(
                _report_payload(report, args.view),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    else:
        _print_report(report, path, args.view, catalog is not None)
    return 0


def _read_file(path: Path, kind: str) -> bytes:
    if not path.exists():
        raise ValueError(f"{kind} not found: {path}")
    if not path.is_file():
        raise ValueError(f"{kind} is not a file: {path}")
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ValueError(f"cannot read {kind} {path}: {exc}") from exc


def _decode_script_text(data: bytes, path: Path) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        match = re.search(
            br"""<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)["']""", data[:200]
        )
        encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise ValueError(
            f"script file is not valid {encoding}: {path}: {exc}"
        ) from exc


def _blocked_services(script) -> list:
    service_ids = sorted(
        {
            service_id
            for ecu in script.ecus
            for service_id in ecu.service_ids
            if not is_read_only_service(service_id)
        }
    )
    return [
        {
            "service_id": service_id,
            "service_id_hex": "0x{:02X}".format(service_id),
            "classification": classify_service(service_id),
        }
        for service_id in service_ids
    ]


def _script_payload(script, path: Path) -> dict:
    plan = script.dtc_list
    return {
        "path": str(path),
        "docno": script.docno,
        "title": script.title,
        "variant_id": script.variant_id,
        "language": script.language,
        "script_type": script.script_type,
        "profiles": list(script.profile_ids),
        "ecus": [
            {
                "mode": ecu.mode,
                "ecu_address": ecu.ecu_address,
                "ecu_address_hex": _format_id(ecu.ecu_address),
                "ecu_type": ecu.ecu_type,
                "ecu_type_hex": _format_id(ecu.ecu_type),
                "tester_address": ecu.tester_address,
                "tester_address_hex": _format_id(ecu.tester_address),
                "ecu_name": ecu.ecu_name,
                "ecu_identifier": ecu.ecu_identifier,
                "service_ids": list(ecu.service_ids),
                "service_ids_hex": [
                    "0x{:02X}".format(service_id)
                    for service_id in ecu.service_ids
                ],
                "service_classifications": [
                    classify_service(service_id)
                    for service_id in ecu.service_ids
                ],
                "requests": list(ecu.requests),
                "responses": list(ecu.responses),
            }
            for ecu in script.ecus
        ],
        "dtc_plan": None
        if plan is None
        else {
            "list_path": plan.list_path,
            "value_path": plan.value_path,
            "text_path": plan.text_path,
            "raw_nodes": list(plan.raw_nodes),
        },
        "read_only_requests": [
            {
                "ecu_address": request.ecu_address,
                "ecu_address_hex": _format_id(request.ecu_address),
                "ecu_type": request.ecu_type,
                "tester_address": request.tester_address,
                "ecu_name": request.ecu_name,
                "ecu_identifier": request.ecu_identifier,
                "service_id": request.service_id,
                "service_id_hex": "0x{:02X}".format(request.service_id),
                "service_name": request.service_name,
                "request": request.request,
            }
            for request in script.read_only_requests()
        ],
        "blocked_services": _blocked_services(script),
        "executed": False,
    }


def _print_script(script, path: Path) -> None:
    print(f"Script: {path}")
    print(f"Docno: {script.docno or '-'}")
    print(f"Title: {script.title or '-'}")
    print(f"Variant: {script.variant_id or '-'}")
    print(f"Language: {script.language or '-'}")
    print(f"Type: {script.script_type or '-'}")
    print(f"Profiles ({len(script.profile_ids)}): {', '.join(script.profile_ids) or '-'}")
    print(f"ECUs ({len(script.ecus)}):")
    for index, ecu in enumerate(script.ecus, 1):
        print(
            f"  [{index}] mode={ecu.mode or '-'} ecu_address={_format_id(ecu.ecu_address)} "
            f"ecu_type={_format_id(ecu.ecu_type)} tester_address={_format_id(ecu.tester_address)}"
        )
        services = ", ".join(
            "0x{:02X} {}".format(service_id, classify_service(service_id))
            for service_id in ecu.service_ids
        )
        print(f"      services: {services or '-'}")
        if ecu.requests:
            requests = [value for value in ecu.requests if value]
            if requests:
                print(f"      raw requests: {' | '.join(requests)}")
        if ecu.responses:
            responses = [value for value in ecu.responses if value]
            if responses:
                print(f"      raw responses: {' | '.join(responses)}")
    plan = script.dtc_list
    if plan is None:
        print("DTC plan: none")
    else:
        print("DTC plan:")
        print(f"  list: {plan.list_path or '-'}")
        print(f"  value: {plan.value_path or '-'}")
        print(f"  text: {plan.text_path or '-'}")
    requests = script.read_only_requests()
    print(f"Read-only requests ({len(requests)}):")
    for request in requests:
        print(
            "  ECU {}  service 0x{:02X} {}  request={}".format(
                _format_id(request.ecu_address),
                request.service_id,
                request.service_name,
                request.request or "(none)",
            )
        )
    blocked = _blocked_services(script)
    print(f"Blocked services ({len(blocked)}, never executed):")
    for service in blocked:
        print(
            "  {} {}".format(
                service["service_id_hex"], service["classification"]
            )
        )
    print("Execution: none. This command only parses the script.")


def _load_script_plan(path: Path):
    data = _read_file(path, "script")
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            return parse_script_zip(data)
        return parse_script_xml(_decode_script_text(data, path))
    except DiagnosticScriptError as exc:
        raise ValueError(f"cannot parse script {path}: {exc}") from exc


def _load_address_map(path: Path):
    document = _load_json_object(path, "address map")
    try:
        return parse_address_map(document)
    except AddressMapError as exc:
        raise ValueError(f"invalid address map {path}: {exc}") from exc


def _build_script_address_map(script, map_path, profile_path):
    """Combine the explicit address map with optional profile name matches.

    The numeric map is loaded first, so a profile can only fill entries the
    explicit map does not already cover. Returns the merged map and the name
    report, or none when no profile was supplied.
    """

    if map_path is None and profile_path is None:
        raise ValueError("supply --address-map, --profile, or both")
    address_map = _load_address_map(map_path) if map_path is not None else AddressMap()
    name_report = None
    if profile_path is not None:
        name_map, name_report = load_profile_name_map(script, profile_path)
        address_map = address_map.merged_with(name_map)
    return address_map, name_report


def _record_target(record) -> str:
    return "unmapped" if record.can_id is None else "0x{:03X}".format(record.can_id)


def _record_line(record) -> str:
    return "[{}] {} -> {}  service 0x{:02X} {}  request={}  {}".format(
        record.index,
        record.source_ecu,
        _record_target(record),
        record.service_id,
        record.service_name,
        record.request_hex or "(none)",
        record.status,
    )


def _print_unmapped(plan) -> None:
    if not plan.unmapped:
        return
    print(f"Unmapped logical addresses ({len(plan.unmapped)}):")
    for item in plan.unmapped:
        if item.value is None:
            value = "unknown"
        elif item.kind == "ecu_address":
            value = "0x{:X}".format(item.value)
        else:
            value = str(item.value)
        print(f"  {item.kind} {value} ({item.source_ecu}): no address map entry")


def _print_name_report(report, profile_path: Path) -> None:
    counts = report.counts()
    print(f"Module profile: {profile_path}")
    print(
        "Name matches: {} matched, {} ambiguous, {} missing, {} unnamed "
        "({} script ECUs)".format(
            counts["matched"],
            counts["ambiguous"],
            counts["missing"],
            counts["unnamed"],
            counts["ecus"],
        )
    )
    for entry in report.matched:
        print(
            "  {} {} -> 0x{:03X}".format(
                entry.field, entry.value, entry.can_id
            )
        )
    for entry in report.ambiguous:
        print(
            "  ambiguous {} {}: profile names {}".format(
                entry.field,
                entry.value,
                ", ".join(entry.profile_names),
            )
        )
    for entry in report.missing:
        print(
            "  no profile module named {} ({})".format(
                entry.value, entry.field
            )
        )


def _print_blocked(blocked) -> None:
    if not blocked:
        return
    print(
        "Blocked services ({}): {}".format(
            len(blocked),
            ", ".join(
                "{} {}".format(item["service_id_hex"], item["classification"])
                for item in blocked
            ),
        )
    )


def _print_run_plan(
    plan,
    script_path: Path,
    map_path: Path | None,
    address_map: AddressMap,
    blocked,
    profile_path: Path | None = None,
    name_report: NameMapReport | None = None,
) -> None:
    print(f"Script: {script_path}")
    print(f"Docno: {plan.docno or '-'}")
    print(f"Title: {plan.title or '-'}")
    print(f"Type: {plan.script_type or '-'}")
    if map_path is not None:
        print(
            "Address map: {} ({} numeric entries)".format(
                map_path, len(address_map)
            )
        )
    if profile_path is not None and name_report is not None:
        _print_name_report(name_report, profile_path)
    print(f"Read-only requests ({len(plan.records)}):")
    for record in plan.records:
        line = "  " + _record_line(record)
        if record.detail:
            line += f"  ({record.detail})"
        print(line)
    _print_unmapped(plan)
    _print_blocked(blocked)
    print("Mode: plan only. No CAN frames were sent.")


def _print_run_result(result: RunResult) -> None:
    counts = result.counts
    print(
        "Run: transmitted={} responses={} timeouts={} errors={} skipped={}".format(
            counts["transmitted"],
            counts[STATUS_RESPONSE],
            counts["timeout"],
            counts[STATUS_ERROR],
            counts["skipped_unmapped"]
            + counts["skipped_non_read"]
            + counts["skipped_unsupported"],
        )
    )
    if result.aborted:
        print(f"Aborted: {result.abort_reason}")
    for record in result.records:
        line = "  " + _record_line(record)
        if record.detail and record.status != STATUS_RESPONSE:
            line += f"  ({record.detail})"
        print(line)
        if record.status != STATUS_RESPONSE:
            continue
        response_id = (
            "-"
            if record.response_id is None
            else "0x{:03X}".format(record.response_id)
        )
        print(
            "      response_id={} payload={}".format(
                response_id, record.response_payload_hex or "(empty)"
            )
        )
        decoded = record.decoded or {}
        if decoded.get("kind") == "dtc_list" and decoded.get("status") == "ok":
            for dtc in decoded["dtcs"]:
                print(
                    "      {}-{:02X} status=0x{:02X}".format(
                        dtc["code"], dtc["failure_type"], dtc["status_byte"]
                    )
                )
        elif decoded.get("kind") == "negative_response":
            print(
                "      negative response nrc={}{}".format(
                    decoded.get("nrc_hex") or "-",
                    " " + decoded["nrc_name"]
                    if decoded.get("nrc_name")
                    else "",
                )
            )


def _run_script_payload(
    script,
    script_path: Path,
    map_path: Path | None,
    address_map: AddressMap,
    plan,
    result,
    blocked,
    profile_path: Path | None = None,
    name_report: NameMapReport | None = None,
) -> dict:
    entries = address_map.to_dict()
    return {
        "script": {
            "path": str(script_path),
            "docno": script.docno,
            "title": script.title,
            "type": script.script_type,
        },
        "address_map": {
            "path": None if map_path is None else str(map_path),
            "entries": entries["entries"],
            "names": entries["names"],
        },
        "profile": None
        if profile_path is None or name_report is None
        else {
            "path": str(profile_path),
            "name_map": name_report.to_dict(),
        },
        "executed": result is not None,
        "dry_run": result is None or result.dry_run,
        "plan": plan.to_dict(),
        "result": None if result is None else result.to_dict(),
        "blocked_services": blocked,
    }


def native_request_ids(plan: RunPlan) -> list:
    """Return the sorted request IDs a native run would need.

    Raises ``ValueError`` when nothing is planned or an ID is outside the
    native diagnostic range.
    """

    planned = [
        record for record in plan.records if record.status == STATUS_PLANNED
    ]
    if not planned:
        raise ValueError(
            "nothing to execute: no read request has an address map entry"
        )
    request_ids = sorted(
        {record.can_id for record in planned if record.can_id is not None}
    )
    unsupported = [
        can_id for can_id in request_ids if not 0x700 <= can_id <= 0x7F7
    ]
    if unsupported:
        raise ValueError(
            "the native DiCE transport only accepts diagnostic request IDs "
            "0x700..0x7F7; address map has "
            + ", ".join("0x{:03X}".format(can_id) for can_id in unsupported)
        )
    return request_ids


def run_script_native(plan: RunPlan, timeout: float = 1.5) -> RunResult:
    """Send a planned read-only script through the native DiCE transport."""

    request_ids = native_request_ids(plan)
    from .dice_can import DiceCanTransport
    from .dice_rpc import DiceRpc

    try:
        with DiceRpc() as rpc:
            with DiceCanTransport(
                rpc, uds_requests=request_ids, allow_read_services=True
            ) as bus:
                return run_plan(plan, bus, timeout=timeout)
    except (RuntimeError, OSError) as exc:
        raise ValueError(f"DiCE run failed: {exc}") from exc


def cmd_diag_run_script(args: argparse.Namespace) -> int:
    script_path = Path(args.script)
    script = _load_script_plan(script_path)
    map_path = Path(args.address_map) if args.address_map is not None else None
    profile_path = Path(args.profile) if args.profile is not None else None
    address_map, name_report = _build_script_address_map(
        script, map_path, profile_path
    )
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise ValueError("timeout must be a positive number of seconds")

    plan = build_run_plan(script, address_map)
    blocked = _blocked_services(script)
    result = run_script_native(plan, args.timeout) if args.execute else None

    if args.json:
        print(
            json.dumps(
                _run_script_payload(
                    script,
                    script_path,
                    map_path,
                    address_map,
                    plan,
                    result,
                    blocked,
                    profile_path,
                    name_report,
                ),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    elif result is None:
        _print_run_plan(
            plan,
            script_path,
            map_path,
            address_map,
            blocked,
            profile_path,
            name_report,
        )
    else:
        print(f"Script: {script_path}")
        print(f"Docno: {plan.docno or '-'}")
        if map_path is not None:
            print(f"Address map: {map_path}")
        if profile_path is not None and name_report is not None:
            _print_name_report(name_report, profile_path)
        _print_unmapped(plan)
        _print_blocked(blocked)
        print()
        _print_run_result(result)

    if result is None:
        return 0
    if result.aborted or any(
        record.status == STATUS_ERROR for record in result.records
    ):
        return 1
    return 0


def cmd_script_plan(args: argparse.Namespace) -> int:
    path = Path(args.script)
    script = _load_script_plan(path)

    if args.json:
        print(
            json.dumps(
                _script_payload(script, path),
                indent=2,
                sort_keys=True,
                ensure_ascii=False,
            )
        )
    else:
        _print_script(script, path)
    return 0


def cmd_readout(args: argparse.Namespace) -> int:
    bus = open_transport(args)
    try:
        for frame in bus.read_until(args.duration):
            print(f"{hex_id(frame.arbitration_id)}  {frame.data.hex(' ').upper()}")
    finally:
        bus.close()
    return 0


def cmd_discover(args: argparse.Namespace) -> int:
    bus = open_transport(args)
    found: list[tuple[int, int, bytes]] = []
    try:
        for ecu in range(args.start, args.end + 1):
            request = build_read_dtc_request(ecu, args.group)
            bus.send(request)
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                frame = bus.recv(timeout=min(0.05, max(0.0, deadline - time.monotonic())))
                if frame and is_response_to(frame, ecu, SERVICE_READ_DTC):
                    found.append((ecu, frame.arbitration_id, frame.data))
                    print(f"found ecu={hex_byte(ecu)} response_id={hex_id(frame.arbitration_id)} frame={frame.data.hex(' ').upper()}")
                    break
            time.sleep(args.spacing)
    finally:
        bus.close()

    if not found:
        print("no modules responded")
    return 0


def cmd_read_dtc(args: argparse.Namespace) -> int:
    ecus = [parse_ecu(value) for value in args.ecu]
    bus = open_transport(args)
    try:
        for ecu in ecus:
            request = build_read_dtc_request(ecu, args.group)
            print(f"tx ecu={hex_byte(ecu)} service=AE frame={request.data.hex(' ').upper()}")
            bus.send(request)

            deadline = time.monotonic() + args.timeout
            got_response = False
            while time.monotonic() < deadline:
                frame = bus.recv(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
                if frame is None:
                    continue
                if not is_response_to(frame, ecu, SERVICE_READ_DTC):
                    if args.verbose:
                        print(f"rx other {hex_id(frame.arbitration_id)} {frame.data.hex(' ').upper()}")
                    continue

                got_response = True
                response = parse_dtc_response(frame)
                if response is None:
                    print(f"rx ecu={hex_byte(ecu)} response_id={hex_id(frame.arbitration_id)} frame={frame.data.hex(' ').upper()}")
                else:
                    print(
                        "rx "
                        f"ecu={hex_byte(response.ecu)} "
                        f"response_id={hex_id(response.response_id)} "
                        f"service={hex_byte(response.service)} "
                        f"payload={response.raw_payload_hex or '(empty)'} "
                        f"frame={response.raw_frame_hex}"
                    )
            if not got_response:
                print(f"no DTC response from ecu={hex_byte(ecu)}")
    finally:
        bus.close()
    return 0


def cmd_clear_dtc(args: argparse.Namespace) -> int:
    ecu = parse_ecu(args.ecu)
    if not args.yes:
        print("refusing to clear DTCs without --yes")
        return 2

    bus = open_transport(args)
    try:
        request = build_clear_dtc_request(ecu, args.group)
        print(f"tx ecu={hex_byte(ecu)} service=AF frame={request.data.hex(' ').upper()}")
        bus.send(request)

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            frame = bus.recv(timeout=min(0.1, max(0.0, deadline - time.monotonic())))
            if frame and is_response_to(frame, ecu, SERVICE_CLEAR_DTC):
                print(f"rx ecu={hex_byte(ecu)} response_id={hex_id(frame.arbitration_id)} frame={frame.data.hex(' ').upper()}")
                return 0
        print(f"no clear-DTC acknowledgement from ecu={hex_byte(ecu)}")
        return 1
    finally:
        bus.close()
    return 0


def cmd_tui(args: argparse.Namespace) -> int:
    run_tui()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="volvo-p3", description="Basic Volvo P3 CAN diagnostics")
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_modules = subparsers.add_parser("list-modules", help="show built-in ECU address hints")
    list_modules.set_defaults(func=cmd_list_modules)

    list_dice_commands = subparsers.add_parser("list-dice-commands", help="show recovered DiCE command IDs")
    list_dice_commands.add_argument("--category", choices=CATEGORIES, default=None)
    list_dice_commands.add_argument(
        "--types", action="store_true", help="also show the request/reply payload struct names"
    )
    list_dice_commands.set_defaults(func=cmd_list_dice_commands)

    probe_can = subparsers.add_parser("probe-can", help="show python-can backends and optional detected configs")
    probe_can.add_argument("--detect", action="store_true", help="run python-can backend detection")
    probe_can.set_defaults(func=cmd_probe_can)

    probe_dice = subparsers.add_parser("probe-dice", help="detect a connected DiCE USB adapter on macOS")
    probe_dice.set_defaults(func=cmd_probe_dice)

    dice_info = subparsers.add_parser("dice-info", help="read native DiCE firmware and supply voltage")
    dice_info.set_defaults(func=cmd_dice_info)

    dice_dtc = subparsers.add_parser("dice-read-dtc", help="read generic OBD DTCs over native DiCE USB")
    dice_dtc.add_argument("--timeout", type=float, default=1.5, help="response window per OBD service, seconds")
    dice_dtc.set_defaults(func=cmd_dice_read_dtc)

    hybrid_dtc = subparsers.add_parser("dice-hybrid-dtc", help="read P3 D6 module DTCs using default-session UDS")
    hybrid_dtc.add_argument("--timeout", type=float, default=1.5)
    hybrid_dtc.add_argument("--module", action="append", choices=("BECM", "IEM", "IGM", "DCDC", "OBC", "ACCM", "CEM", "ECM", "TCM", "DIM"))
    hybrid_dtc.set_defaults(func=cmd_dice_hybrid_dtc)

    scan = subparsers.add_parser("dice-scan", help="read physical UDS modules with discovery and reusable profiles")
    source = scan.add_mutually_exclusive_group()
    source.add_argument("--profile", type=Path, help="JSON module profile with explicit request/response IDs")
    source.add_argument("--discover", action="store_true", help="probe a diagnostic CAN ID range using read-DTC requests")
    scan.add_argument("--start", type=lambda v: int(v, 0), default=0x700)
    scan.add_argument("--end", type=lambda v: int(v, 0), default=0x7F7)
    scan.add_argument("--response-offset", type=lambda v: int(v, 0), default=8)
    scan.add_argument("--timeout", type=float, default=1.5)
    scan.add_argument("--output", type=Path, help="new JSON report path, default reports/dice-scan-TIMESTAMP.json")
    scan.add_argument("--save-profile", type=Path, help="save responding targets to a new reusable profile")
    scan.add_argument("--replace-profile", action="store_true", help="replace an existing saved profile after discovery")
    scan.add_argument("--all-records", action="store_true", help="also display monitors with no fault bits; JSON always includes every record")
    scan.add_argument(
        "--diag-catalog",
        type=Path,
        help=(
            "optional diagnostic catalog (.json or compressed); adds a "
            "fault-trace section"
        ),
    )
    scan.set_defaults(func=cmd_dice_scan)

    probe_dice_raw = subparsers.add_parser("probe-dice-raw", help="detect DiCE through PyUSB/libusb")
    probe_dice_raw.set_defaults(func=cmd_probe_dice_raw)

    extract_dice = subparsers.add_parser(
        "extract-dice-installer",
        help="extract DiCE driver/app MSI payloads from SetupDiCE.exe",
    )
    extract_dice.add_argument("installer", type=Path, help="path to SetupDiCE.exe")
    extract_dice.add_argument("--output", type=Path, default=Path("artifacts/setupdice_extracted"))
    extract_dice.add_argument("--arch", choices=("x64", "x86", "all"), default="x64")
    extract_dice.add_argument("--no-unpack", action="store_true", help="only recover MSI files")
    extract_dice.set_defaults(func=cmd_extract_dice_installer)

    diag_catalog = subparsers.add_parser(
        "diag-catalog", help="inspect a service catalog JSON export"
    )
    diag_catalog.add_argument(
        "--catalog",
        type=Path,
        required=True,
        help="catalog file: .json, .json.gz, .json.bz2 or .json.xz",
    )
    diag_catalog.add_argument(
        "--code", help="DTC code to look up, for example P034211"
    )
    diag_catalog.add_argument("--json", action="store_true", help="print stable JSON")
    diag_catalog.set_defaults(func=cmd_diag_catalog)

    diag_report = subparsers.add_parser(
        "diag-report", help="build read-only diagnostic views from a saved scan report"
    )
    diag_report.add_argument(
        "--scan", type=Path, required=True, help="saved dice-scan JSON report"
    )
    diag_report.add_argument(
        "--catalog",
        type=Path,
        help="optional service catalog for DTC descriptions (.json or compressed)",
    )
    diag_report.add_argument(
        "--vehicle", metavar="JSON", help="inline vehicle profile JSON or a JSON file"
    )
    diag_report.add_argument(
        "--view",
        choices=("all", "network", "faults", "dtcs"),
        default="all",
        help="section to print, default all",
    )
    diag_report.add_argument("--json", action="store_true", help="print stable JSON")
    diag_report.set_defaults(func=cmd_diag_report)

    script_plan = subparsers.add_parser(
        "diag-script", help="parse a diagnostic script ZIP or XML without executing it"
    )
    script_plan.add_argument(
        "--script", type=Path, required=True, help="diagnostic script ZIP or XML file"
    )
    script_plan.add_argument("--json", action="store_true", help="print stable JSON")
    script_plan.set_defaults(func=cmd_script_plan)

    diag_run = subparsers.add_parser(
        "diag-run-script",
        help="plan or run the read-only requests of a diagnostic script",
    )
    diag_run.add_argument(
        "--script", type=Path, required=True, help="diagnostic script ZIP or XML file"
    )
    diag_run.add_argument(
        "--address-map",
        type=Path,
        default=None,
        help=(
            "JSON map of logical ECU addresses to 11-bit physical CAN request "
            "IDs; wins over a profile name match"
        ),
    )
    diag_run.add_argument(
        "--profile",
        type=Path,
        default=None,
        metavar="PATH",
        help=(
            "saved module profile whose module names are matched to the "
            "script's EcuName or EcuIdentifier"
        ),
    )
    diag_run.add_argument(
        "--execute",
        action="store_true",
        help="send the planned reads through the native DiCE transport",
    )
    diag_run.add_argument(
        "--timeout",
        type=float,
        default=1.5,
        help="response window per request in seconds, default 1.5",
    )
    diag_run.add_argument("--json", action="store_true", help="print stable JSON")
    diag_run.set_defaults(func=cmd_diag_run_script)

    readout = subparsers.add_parser("readout", help="passively print CAN frames")
    add_bus_args(readout)
    readout.add_argument("--duration", type=float, default=10.0)
    readout.set_defaults(func=cmd_readout)

    discover = subparsers.add_parser("discover", help="probe ECU addresses with read-DTC requests")
    add_bus_args(discover)
    discover.add_argument("--start", type=lambda value: int(value, 0), default=0x00)
    discover.add_argument("--end", type=lambda value: int(value, 0), default=0xFF)
    discover.add_argument("--group", type=lambda value: int(value, 0), default=0x1B)
    discover.add_argument("--timeout", type=float, default=0.2)
    discover.add_argument("--spacing", type=float, default=0.02)
    discover.set_defaults(func=cmd_discover)

    read_dtc = subparsers.add_parser("read-dtc", help="read DTC bytes from one or more ECUs")
    add_bus_args(read_dtc)
    read_dtc.add_argument("--ecu", action="append", required=True, help="ECU name or byte, e.g. ECM or 0x7A")
    read_dtc.add_argument("--group", type=lambda value: int(value, 0), default=0x1B)
    read_dtc.add_argument("--timeout", type=float, default=1.0)
    read_dtc.add_argument("--verbose", action="store_true")
    read_dtc.set_defaults(func=cmd_read_dtc)

    clear_dtc = subparsers.add_parser("clear-dtc", help="clear DTCs for one ECU")
    add_bus_args(clear_dtc)
    clear_dtc.add_argument("--ecu", required=True, help="ECU name or byte, e.g. ECM or 0x7A")
    clear_dtc.add_argument("--group", type=lambda value: int(value, 0), default=0x1B)
    clear_dtc.add_argument("--timeout", type=float, default=1.0)
    clear_dtc.add_argument("--yes", action="store_true", help="required guard for clear operation")
    clear_dtc.set_defaults(func=cmd_clear_dtc)

    tui = subparsers.add_parser("tui", help="launch the interactive Volvo P3 terminal UI")
    tui.set_defaults(func=cmd_tui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        return 130
    except (RuntimeError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
