"""python-can transport wrapper for Volvo P3 diagnostics."""

from __future__ import annotations

import platform
from dataclasses import dataclass
from typing import Iterator

from .protocol import VolvoFrame, parse_frame


@dataclass
class CanBusConfig:
    interface: str
    channel: str
    bitrate: int | None = None


class PythonCanTransport:
    def __init__(self, config: CanBusConfig):
        try:
            import can
        except ImportError as exc:
            raise RuntimeError("python-can is required for CAN access. Install the python-can package.") from exc

        kwargs = {
            "interface": config.interface,
            "channel": config.channel,
        }
        if config.bitrate:
            kwargs["bitrate"] = config.bitrate
        self._can = can
        try:
            self._bus = can.interface.Bus(**kwargs)
        except OSError as exc:
            if config.interface == "socketcan" and platform.system() != "Linux":
                raise RuntimeError(
                    "socketcan is only available on Linux. On macOS, use a different python-can backend "
                    "or test DiCE with 'probe-dice' and 'probe-dice-raw' first."
                ) from exc
            raise RuntimeError(
                f"failed to open CAN bus interface={config.interface!r} channel={config.channel!r}: {exc}"
            ) from exc

    def close(self) -> None:
        self._bus.shutdown()

    def send(self, frame: VolvoFrame) -> None:
        msg = self._can.Message(
            arbitration_id=frame.arbitration_id,
            data=frame.data,
            is_extended_id=frame.is_extended_id,
        )
        self._bus.send(msg)

    def recv(self, timeout: float = 0.1) -> VolvoFrame | None:
        msg = self._bus.recv(timeout=timeout)
        if msg is None:
            return None
        return parse_frame(msg.arbitration_id, bytes(msg.data), msg.is_extended_id)

    def read_until(self, duration: float, timeout: float = 0.1) -> Iterator[VolvoFrame]:
        import time

        deadline = time.monotonic() + duration
        while time.monotonic() < deadline:
            frame = self.recv(timeout=min(timeout, max(0.0, deadline - time.monotonic())))
            if frame is not None:
                yield frame
