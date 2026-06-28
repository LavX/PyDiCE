"""Curses TUI for Volvo P3 diagnostics."""

from __future__ import annotations

import curses
import contextlib
import io
import os
import sys
import threading
import time
from dataclasses import dataclass, field
from typing import Callable

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    from volvo_p3.can_transport import CanBusConfig, PythonCanTransport
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
else:
    from .can_transport import CanBusConfig, PythonCanTransport
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


class VolvoTuiApp:
    def __init__(self) -> None:
        self.transport = CanBusConfig("socketcan", "can0", None)
        self.vehicle = VehicleState()
        self.sections = self._build_sections()
        self.section_index = 0
        self.action_index = 0
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

        row = 2
        for sidx, section in enumerate(self.sections):
            if row >= height - 5:
                break
            stdscr.addnstr(row, 1, section.title, max(0, left_width - 2), curses.A_UNDERLINE)
            row += 1
            for aidx, action in enumerate(section.actions):
                if row >= height - 5:
                    break
                active = sidx == self.section_index and aidx == self.action_index
                marker = ">" if active else " "
                label = f"{marker} {action.name}"
                attr = curses.A_REVERSE if active else curses.A_NORMAL
                stdscr.addnstr(row, 1, label, max(0, left_width - 2), attr)
                row += 1
            row += 1

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
