"""Volvo P3 diagnostic CAN frame helpers.

The frame format here follows the vendor reverse-engineering notes used by
Volvo enthusiast tools:

* Requests use extended CAN ID 0x000FFFFE.
* Byte 0 is 0xC8 plus the number of significant bytes that follow.
* Byte 1 is the ECU/module diagnostic address.
* Byte 2 is the Volvo diagnostic service.
"""

from dataclasses import dataclass
from typing import Iterable

REQUEST_CAN_ID = 0x000FFFFE

SERVICE_KEEPALIVE = 0xA1
SERVICE_READ_DTC = 0xAE
SERVICE_CLEAR_DTC = 0xAF

RESPONSE_OFFSET = 0x40


@dataclass(frozen=True)
class VolvoFrame:
    arbitration_id: int
    data: bytes
    is_extended_id: bool = True

    @property
    def ecu(self) -> int | None:
        return self.data[1] if len(self.data) > 1 else None

    @property
    def service(self) -> int | None:
        return self.data[2] if len(self.data) > 2 else None

    @property
    def payload(self) -> bytes:
        return self.data[3:]


@dataclass(frozen=True)
class DTCResponse:
    ecu: int
    response_id: int
    service: int
    raw_payload: bytes
    raw_frame: bytes

    @property
    def raw_payload_hex(self) -> str:
        return self.raw_payload.hex(" ").upper()

    @property
    def raw_frame_hex(self) -> str:
        return self.raw_frame.hex(" ").upper()


def build_request(ecu: int, service: int, payload: Iterable[int] = ()) -> VolvoFrame:
    body = bytes([ecu & 0xFF, service & 0xFF, *[byte & 0xFF for byte in payload]])
    if len(body) > 7:
        raise ValueError("single-frame Volvo diagnostic request body cannot exceed 7 bytes")

    data = bytes([0xC8 + len(body)]) + body
    return VolvoFrame(REQUEST_CAN_ID, data.ljust(8, b"\x00"), True)


def build_read_dtc_request(ecu: int, dtc_group: int = 0x1B) -> VolvoFrame:
    return build_request(ecu, SERVICE_READ_DTC, [dtc_group])


def build_clear_dtc_request(ecu: int, dtc_group: int = 0x1B) -> VolvoFrame:
    return build_request(ecu, SERVICE_CLEAR_DTC, [dtc_group])


def expected_response_service(request_service: int) -> int:
    return (request_service + RESPONSE_OFFSET) & 0xFF


def parse_frame(arbitration_id: int, data: bytes, is_extended_id: bool = True) -> VolvoFrame:
    return VolvoFrame(arbitration_id, bytes(data[:8]), is_extended_id)


def is_response_to(frame: VolvoFrame, ecu: int, request_service: int) -> bool:
    return (
        frame.is_extended_id
        and len(frame.data) >= 3
        and frame.ecu == (ecu & 0xFF)
        and frame.service == expected_response_service(request_service)
    )


def parse_dtc_response(frame: VolvoFrame) -> DTCResponse | None:
    if len(frame.data) < 3 or frame.ecu is None or frame.service is None:
        return None
    if frame.service != expected_response_service(SERVICE_READ_DTC):
        return None

    length = frame.data[0] - 0xC8
    if length < 2:
        payload = b""
    else:
        payload = frame.data[3 : 1 + length]

    return DTCResponse(
        ecu=frame.ecu,
        response_id=frame.arbitration_id,
        service=frame.service,
        raw_payload=payload,
        raw_frame=frame.data,
    )


def hex_byte(value: int) -> str:
    return f"0x{value & 0xFF:02X}"


def hex_id(value: int) -> str:
    return f"0x{value:08X}"
