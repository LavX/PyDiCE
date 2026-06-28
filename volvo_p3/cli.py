"""Command-line Volvo P3 diagnostic tool."""

from __future__ import annotations

import argparse
import contextlib
import io
import sys
import time
from pathlib import Path

from .can_transport import CanBusConfig, PythonCanTransport
from .dice_commands import iter_commands
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
        print(f"{command.command_id:3d}  {command.category:9s}  {command.direction:16s}  {command.name}")
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
    list_dice_commands.add_argument("--category", choices=("system", "raw_can", "iso15765"), default=None)
    list_dice_commands.set_defaults(func=cmd_list_dice_commands)

    probe_can = subparsers.add_parser("probe-can", help="show python-can backends and optional detected configs")
    probe_can.add_argument("--detect", action="store_true", help="run python-can backend detection")
    probe_can.set_defaults(func=cmd_probe_can)

    probe_dice = subparsers.add_parser("probe-dice", help="detect a connected DiCE USB adapter on macOS")
    probe_dice.set_defaults(func=cmd_probe_dice)

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
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
