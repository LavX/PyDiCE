"""Command-line Volvo P3 diagnostic tool."""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import time
from pathlib import Path

from .can_transport import CanBusConfig, PythonCanTransport
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
