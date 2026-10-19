"""DiCE USB RPC framing recovered from DiCE32.dll and checked on hardware.

The two additive checksums start at 0x69. Length includes the payload checksum.
See docs/native-dice.md for binary addresses and captured replies.
"""
from __future__ import annotations

import time
from collections import deque


class DiceProtocolError(RuntimeError):
    pass


def checksum(data: bytes) -> int:
    return (0x69 + sum(data)) & 0xFF


def encode_packet(payload: bytes) -> bytes:
    if not 1 <= len(payload) <= 65534:
        raise ValueError('DiCE payload length must be 1..65534')
    header = b'\xb5' + (len(payload) + 1).to_bytes(2, 'little')
    return header + bytes([checksum(header)]) + payload + bytes([checksum(payload)])


class PacketDecoder:
    """Buffer USB fragments and reject corrupted frames before interpretation."""
    def __init__(self):
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[bytes]:
        self.buffer.extend(data)
        packets = []
        while len(self.buffer) >= 4:
            if self.buffer[0] != 0xB5:
                raise DiceProtocolError('invalid DiCE packet marker')
            if checksum(self.buffer[:3]) != self.buffer[3]:
                raise DiceProtocolError('invalid DiCE header checksum')
            length = int.from_bytes(self.buffer[1:3], 'little')
            if length < 2:
                raise DiceProtocolError('invalid DiCE packet length')
            if len(self.buffer) < 4 + length:
                break
            payload = bytes(self.buffer[4:3 + length])
            if checksum(payload) != self.buffer[3 + length]:
                raise DiceProtocolError('invalid DiCE payload checksum')
            del self.buffer[:4 + length]
            packets.append(payload)
        return packets


class DiceRpc:
    """One owner of the USB interface, with bounded request/reply timeouts.

    No reset, firmware download, kernel-driver detach, or USB reconfiguration.
    """
    def __init__(self):
        self.device = None
        self.decoder = PacketDecoder()
        self.pending = deque()
        self.events = deque()
        self.serial = None

    def open(self):
        import usb.core
        import usb.util
        if self.device is not None:
            raise RuntimeError('DiCE already open')
        devices = list(usb.core.find(find_all=True, idVendor=0x17AA, idProduct=0xD1CE))
        if len(devices) != 1:
            raise RuntimeError(f'expected one DiCE, found {len(devices)}')
        dev = devices[0]
        try:
            config = dev.get_active_configuration()
            interface = config[(0, 0)]
            endpoints = {ep.bEndpointAddress for ep in interface}
            if not {0x01, 0x81}.issubset(endpoints):
                raise DiceProtocolError('expected DiCE bulk endpoints 01/81')
            usb.util.claim_interface(dev, 0)
            self.serial = usb.util.get_string(dev, dev.iSerialNumber)
            self.device = dev
        except Exception:
            usb.util.dispose_resources(dev)
            raise
        return self

    def close(self):
        import usb.util
        if self.device is not None:
            usb.util.dispose_resources(self.device)
            self.device = None
        self.pending.clear()
        self.events.clear()
        self.decoder = PacketDecoder()

    def __enter__(self):
        return self.open()

    def __exit__(self, *_):
        self.close()

    def write_packet(self, payload: bytes, timeout: float = 2.0):
        if self.device is None:
            raise RuntimeError('DiCE is not open')
        packet = encode_packet(payload)
        if self.device.write(0x01, packet, timeout=max(1, int(timeout * 1000))) != len(packet):
            raise DiceProtocolError('short USB write')

    def read_packet(self, timeout: float = 2.0) -> bytes | None:
        import usb.core
        if self.device is None:
            raise RuntimeError('DiCE is not open')
        if self.pending:
            return self.pending.popleft()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                data = self.device.read(0x81, 65536, timeout=max(1, int((deadline-time.monotonic())*1000)))
            except usb.core.USBTimeoutError:
                return None
            self.pending.extend(self.decoder.feed(bytes(data)))
            if self.pending:
                return self.pending.popleft()
        return None

    def exchange(self, payload: bytes, timeout: float = 2.0) -> bytes:
        self.write_packet(payload, timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            reply = self.read_packet(max(0, deadline - time.monotonic()))
            if reply is None:
                break
            if reply[0] in (0x81, 0x88):
                if len(self.events) >= 4096:
                    raise DiceProtocolError('DiCE event queue overflow')
                self.events.append(reply)
                continue
            if reply[0] != payload[0]:
                raise DiceProtocolError(f'unexpected reply command 0x{reply[0]:02x}')
            if len(reply) < 2 or reply[1] != 0:
                raise DiceProtocolError(f'DiCE rejected command: {reply.hex()}')
            return reply
        raise TimeoutError(f'DiCE command 0x{payload[0]:02x} timed out')

    def system_info(self) -> dict:
        firmware = self.exchange(b'\x04')
        voltage = self.exchange(b'\x62')
        if len(firmware) != 35 or len(voltage) != 4:
            raise DiceProtocolError('unexpected system information reply size')
        return {
            'serial': self.serial,
            'firmware_reply': firmware.hex(),
            'firmware_build': firmware[4:34].split(b'\x00', 1)[0].decode('ascii', errors='replace'),
            'supply_voltage_raw': int.from_bytes(voltage[2:4], 'little'),
            'voltage_reply': voltage.hex(),
        }
