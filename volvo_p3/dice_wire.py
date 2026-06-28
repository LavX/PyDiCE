"""Recovered DiCE wire-format helpers.

The reverse-engineered Windows DLL shows one concrete framing detail and one
concrete reply decoder:

* outbound command frames begin with ``0x1D``
* the second byte is the command ID
* reply status is a single byte decoded through the RFC status table

The payload layout after the prefix is still under investigation, so this
module stays intentionally narrow.
"""

from __future__ import annotations

from dataclasses import dataclass


DICE_COMMAND_PREFIX = 0x1D

DICE_REPLY_STATUS_MESSAGES: dict[int, str] = {
    0x00: "successfull.",
    0x01: "Failed, undefined error.",
    0x02: "Failed, a parameter was out of range.",
    0x03: "Failed, a buffer was empty.",
    0x04: "Failed, a buffer was full.",
    0x05: "Failed, could not generate correct baud rate.",
    0x06: "Failed, out of memory.",
    0x07: "Failed, wrong mode to execute.",
    0x08: "Failed, timeout.",
    0x09: "Failed, requested function is unavailable.",
    0x0A: "Failed, a parameter was of wrong alignment.",
    0x0B: "Failed, the message did not pass the filter.",
    0x0C: "Failed, busy..",
    0x63: "Failed, unimplemented function.",
    0xF7: "RFC not found.",
    0xF8: "RFC no connection.",
    0xF9: "RFC version missmatch.",
    0xFA: "RFC buffer overflow.",
    0xFB: "Failed, RFC message length exceeded.",
    0xFC: "Failed, RFC header checksum error.",
    0xFD: "Failed, RFC unrecognized function identifier.",
    0xFE: "Failed, RFC message checksum error.",
    0xFF: "Failed, RFC timeout.",
}


@dataclass(frozen=True)
class DiceWireFrame:
    """A raw DiCE command frame."""

    command_id: int
    payload: bytes = b""

    def encode(self) -> bytes:
        return bytes([DICE_COMMAND_PREFIX, self.command_id & 0xFF]) + self.payload


def decode_reply_status(status: int) -> str:
    return DICE_REPLY_STATUS_MESSAGES.get(status & 0xFF, "Failed, unknown RFC error.")
