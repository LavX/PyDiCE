"""Read-only OBD CAN channel on the native DiCE RPC transport.

Command layouts come from DiCE32_x64.dll. The channel setup and teardown
commands have also been accepted by a connected DiCE. Transmit is restricted
to OBD read requests and ISO-TP flow control.
"""
from __future__ import annotations

import time

from .dice_rpc import DiceProtocolError, DiceRpc
from .protocol import VolvoFrame


# Candidate P3 V60 D6 diagnostic request IDs reported from VIDA.
# Source: https://www.elektroda.com/rtvforum/topic3626792.html#18318959
# Module labels must be distinguished from live identity verification.
P3_D6_MODULES = {
    "ECM": 0x7E0, "TCM": 0x7E1, "BECM": 0x7E4,
    "IEM": 0x745, "IGM": 0x747, "DCDC": 0x746,
    "OBC": 0x753, "ACCM": 0x7C7, "CEM": 0x726, "DIM": 0x720,
}


class DiceCanTransport:
    """Own one standard-ID, 500 kbit/s CAN channel and its receive filter."""

    def __init__(self, rpc: DiceRpc, *, uds_requests=(), allow_read_services=False):
        self.rpc = rpc
        self.uds_requests = frozenset(uds_requests)
        self.allow_read_services = bool(allow_read_services)
        if any(type(identifier) is not int or not 0x700 <= identifier <= 0x7F7 or identifier == 0x7DF
               for identifier in self.uds_requests):
            raise ValueError("UDS requests require diagnostic IDs 0x700..0x7F7, excluding functional 0x7DF")
        self.channel_id: int | None = None
        self.filter_id: int | None = None
        self.started = False
        self._tx_sequence = 0

    @staticmethod
    def _check_size(reply: bytes, expected: int, command: int) -> None:
        if len(reply) != expected:
            raise DiceProtocolError(
                f'DiCE command 0x{command:02x} reply size {len(reply)}, expected {expected}'
            )

    def open(self) -> DiceCanTransport:
        if self.channel_id is not None:
            raise RuntimeError('CAN channel already open')
        try:
            # Init mode 1 is the standard CAN mode used for J2534 CAN protocol 5
            # with ConnectFlags=0. The DiCE returns the allocated channel ID.
            reply = self.rpc.exchange(b'\x10\x01')
            self._check_size(reply, 3, 0x10)
            self.channel_id = reply[2]
            channel = self.channel_id
            for key, value in ((0, 0), (1, 500_000), (7, 1), (3, 1)):
                reply = self.rpc.exchange(
                    bytes((0x14, channel, key)) + value.to_bytes(4, 'little')
                )
                self._check_size(reply, 2, 0x14)
            reply = self.rpc.exchange(bytes((0x12, channel)))
            self._check_size(reply, 2, 0x12)
            self.started = True

            # DiCE AddFilter record: flags u16, type u8, pattern ID u32,
            # pattern data[8], mask ID u32, mask data[8]. Type 1 is pass.
            # OBD admits 0x7e8..0x7ef. Explicit UDS targets can have any
            # standard response ID, so receive all and filter in the reader.
            record = (
                b'\x00\x00\x01'
                + (0 if self.uds_requests else 0x7E8).to_bytes(4, 'little')
                + bytes(8)
                + (0 if self.uds_requests else 0x7F8).to_bytes(4, 'little')
                + bytes(8)
            )
            reply = self.rpc.exchange(bytes((0x1A, channel)) + record)
            self._check_size(reply, 3, 0x1A)
            self.filter_id = reply[2]
        except Exception:
            try:
                self.close()
            except Exception:
                pass
            raise
        return self

    def close(self) -> None:
        if self.channel_id is None:
            return
        channel = self.channel_id
        first_error: Exception | None = None
        # The DLL's remove-filter request contains command, channel, filter ID.
        if self.filter_id is not None:
            try:
                reply = self.rpc.exchange(bytes((0x1B, channel, self.filter_id)))
                self._check_size(reply, 2, 0x1B)
            except Exception as exc:
                first_error = exc
            self.filter_id = None
        if self.started:
            try:
                reply = self.rpc.exchange(bytes((0x13, channel)))
                self._check_size(reply, 2, 0x13)
            except Exception as exc:
                if first_error is None:
                    first_error = exc
            self.started = False
        try:
            reply = self.rpc.exchange(bytes((0x11, channel)))
            self._check_size(reply, 2, 0x11)
        except Exception as exc:
            if first_error is None:
                first_error = exc
        self.channel_id = None
        if first_error is not None:
            raise first_error

    def __enter__(self) -> DiceCanTransport:
        return self.open()

    def __exit__(self, *_: object) -> None:
        self.close()

    def send(self, frame: VolvoFrame, timeout: float = 2.0) -> None:
        """Send only the OBD read requests and flow-control frames used here.

        The RawCAN 0x80 layout and 0x81 transmit confirm come from DiCE32_x64.dll.
        Receive events that arrive before the confirm stay queued for recv().
        """
        if self.channel_id is None or not self.started:
            raise RuntimeError('CAN channel is not open')
        if frame.is_extended_id or len(frame.data) != 8:
            raise ValueError('only eight-byte standard OBD read frames are allowed')
        data = bytes(frame.data)
        is_discovery = frame.arbitration_id == 0x7DF and data == bytes.fromhex(
            '02 01 00 00 00 00 00 00'
        )
        is_dtc_read = (
            frame.arbitration_id == 0x7DF
            and data[0] == 1
            and data[1] in (0x03, 0x07, 0x0A)
            and data[2:] == bytes(6)
        )
        is_flow_control = (
            (0x7E0 <= frame.arbitration_id <= 0x7E7 or frame.arbitration_id in self.uds_requests)
            and data == bytes.fromhex('30 00 00 00 00 00 00 00')
        )
        is_uds_read = (
            frame.arbitration_id in self.uds_requests
            and data == bytes.fromhex('03 19 02 FF 00 00 00 00')
        )
        is_vida_read = (
            self.allow_read_services
            and frame.arbitration_id in self.uds_requests
            and (data[0] & 0xF0) == 0
            and data[1] in (0x01, 0x09, 0x19, 0x22)
        )
        if not (is_discovery or is_dtc_read or is_flow_control or is_uds_read or is_vida_read):
            raise ValueError('DiCE transmit is limited to OBD read requests and flow control')
        if timeout <= 0:
            raise ValueError('timeout must be positive')

        sequence = self._tx_sequence
        self._tx_sequence = (sequence + 1) & 0xFFFF
        # RawCAN WriteMsgs packs one 16-byte record after command, channel,
        # and byte count. The record is length (DLC+8), flags, sequence,
        # CAN ID, then eight CAN data bytes. Standard ID uses flags zero.
        payload = (
            bytes((0x80, self.channel_id))
            + (16).to_bytes(2, 'little')
            + bytes((len(data) + 8, 0))
            + sequence.to_bytes(2, 'little')
            + frame.arbitration_id.to_bytes(4, 'little')
            + data
        )
        self.rpc.write_packet(payload, timeout=timeout)
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f'DiCE CAN transmit sequence {sequence} timed out')
            event = self.rpc.read_packet(remaining)
            if event is None:
                raise TimeoutError(f'DiCE CAN transmit sequence {sequence} timed out')
            if not event or event[0] != 0x81:
                raise DiceProtocolError(f'unexpected reply during DiCE CAN transmit: {event.hex()}')
            if len(event) < 8 or event[3] != self.channel_id:
                raise DiceProtocolError(f'invalid DiCE CAN event during transmit: {event.hex()}')
            if not (event[4] & 0x08):
                if len(self.rpc.events) >= 4096:
                    raise DiceProtocolError('DiCE event queue overflow')
                self.rpc.events.append(event)
                continue
            if len(event) != 16:
                raise DiceProtocolError(f'invalid DiCE CAN confirmation: {event.hex()}')
            confirmed_sequence = int.from_bytes(event[6:8], 'little')
            if confirmed_sequence != sequence:
                raise DiceProtocolError(
                    f'DiCE transmit confirmation sequence {confirmed_sequence}, expected {sequence}'
                )
            if event[1] != 0:
                raise DiceProtocolError(f'DiCE transmit failed: {event.hex()}')
            return

    def recv(self, timeout: float = 1.0) -> VolvoFrame | None:
        """Read one ordinary CAN frame, or return None at the deadline."""
        if self.channel_id is None or not self.started:
            raise RuntimeError('CAN channel is not open')
        deadline = time.monotonic() + timeout
        while True:
            if self.rpc.events:
                payload = self.rpc.events.popleft()
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                payload = self.rpc.read_packet(remaining)
                if payload is None:
                    return None
            if not payload or payload[0] != 0x81:
                raise DiceProtocolError(f'unexpected CAN event: {payload.hex()}')
            if len(payload) < 16:
                raise DiceProtocolError('short DiCE CAN event')
            if payload[3] != self.channel_id:
                raise DiceProtocolError('DiCE CAN event from another channel')
            if payload[1] != 0:
                raise DiceProtocolError(f'DiCE CAN event error: {payload.hex()}')
            flags = payload[4]
            if flags & 0x08:
                # RawCAN flags bit 3 denotes a transmit confirmation.
                continue
            length = payload[2] - 14
            if length > 8 or len(payload) != 16 + length:
                raise DiceProtocolError('invalid DiCE CAN frame length')
            return VolvoFrame(
                arbitration_id=int.from_bytes(payload[12:16], 'little'),
                data=payload[16:16 + length],
                is_extended_id=bool(flags & 0x01),
            )


# Preserve the first passive probe API while callers switch to the transport name.
DiceCan = DiceCanTransport
