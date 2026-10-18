"""Read DTCs from one physical ECU using UDS ReadDTCInformation 0x19/0x02.

This module does not change diagnostic sessions, request security access, or
clear/reset diagnostic information. The caller supplies the physical CAN IDs.
"""

from __future__ import annotations

import math
import time
from typing import Any, Dict, List, Optional, Tuple

from .protocol import VolvoFrame


SERVICE_READ_DTC_INFORMATION = 0x19
SUBFUNCTION_REPORT_DTC_BY_STATUS_MASK = 0x02
DTC_STATUS_MASK = 0xFF
MAX_ISOTP_PAYLOAD = 4095
MAX_RESPONSE_PENDING_WAIT = 5.0
MAX_EVIDENCE_FRAMES = 4096

_DTC_STATUS_BITS = (
    "testFailed",
    "testFailedThisOperationCycle",
    "pendingDTC",
    "confirmedDTC",
    "testNotCompletedSinceLastClear",
    "testFailedSinceLastClear",
    "testNotCompletedThisOperationCycle",
    "warningIndicatorRequested",
)

_NRC_NAMES = {
    0x11: "serviceNotSupported",
    0x12: "subFunctionNotSupported",
    0x13: "incorrectMessageLengthOrInvalidFormat",
    0x22: "conditionsNotCorrect",
    0x31: "requestOutOfRange",
    0x33: "securityAccessDenied",
    0x78: "responsePending",
}


def _hex_bytes(data: bytes) -> str:
    return bytes(data).hex(" ").upper()


def _hex_id(identifier: int) -> str:
    return "0x{:03X}".format(identifier)


def _frame_record(direction: str, kind: str, frame: VolvoFrame) -> Dict[str, Any]:
    return {
        "direction": direction,
        "kind": kind,
        "arbitration_id": frame.arbitration_id,
        "arbitration_id_hex": _hex_id(frame.arbitration_id),
        "is_extended_id": bool(frame.is_extended_id),
        "data_hex": _hex_bytes(frame.data),
    }


def _decode_sae_dtc(first_two_bytes: bytes) -> str:
    """Decode the SAE J2012 two-byte portion of a UDS DTC."""
    if len(first_two_bytes) != 2:
        raise ValueError("SAE DTC portion must be exactly two bytes")
    first, second = first_two_bytes
    system = ("P", "C", "B", "U")[(first >> 6) & 0x03]
    return "{}{}{:X}{:X}{:X}".format(
        system,
        (first >> 4) & 0x03,
        first & 0x0F,
        (second >> 4) & 0x0F,
        second & 0x0F,
    )


def _decode_dtc_record(record: bytes, status_availability_mask: int) -> Dict[str, Any]:
    raw_dtc = record[:3]
    status_byte = record[3]
    effective_status_byte = status_byte & status_availability_mask
    status_bits = {
        name: bool(status_byte & (1 << bit))
        for bit, name in enumerate(_DTC_STATUS_BITS)
    }
    status_bit_support = {
        name: bool(status_availability_mask & (1 << bit))
        for bit, name in enumerate(_DTC_STATUS_BITS)
    }
    effective_status_bits = {
        name: bool(effective_status_byte & (1 << bit))
        for bit, name in enumerate(_DTC_STATUS_BITS)
    }
    return {
        "code": _decode_sae_dtc(raw_dtc[:2]),
        "raw_dtc_hex": _hex_bytes(raw_dtc),
        "failure_type_byte": raw_dtc[2],
        "failure_type_hex": "0x{:02X}".format(raw_dtc[2]),
        "status_byte": status_byte,
        "status_hex": "0x{:02X}".format(status_byte),
        "status_bits": status_bits,
        "set_status_bits": [name for name, is_set in status_bits.items() if is_set],
        "status_bit_support": status_bit_support,
        "effective_status_byte": effective_status_byte,
        "effective_status_hex": "0x{:02X}".format(effective_status_byte),
        "effective_status_bits": effective_status_bits,
        "set_effective_status_bits": [
            name for name, is_set in effective_status_bits.items() if is_set
        ],
        "raw_record_hex": _hex_bytes(record),
    }


def _parse_positive_response(payload: bytes) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    if len(payload) < 3:
        return None, "missing_status_availability_mask"
    records_data = payload[3:]
    if len(records_data) % 4:
        return None, "truncated_dtc_record"
    status_availability_mask = payload[2]
    dtcs = [
        _decode_dtc_record(records_data[offset : offset + 4], status_availability_mask)
        for offset in range(0, len(records_data), 4)
    ]
    return {
        "status": "ok",
        "status_availability_mask": status_availability_mask,
        "status_availability_mask_hex": "0x{:02X}".format(status_availability_mask),
        "dtc_count": len(dtcs),
        "fault_record_count": sum(
            bool(dtc["effective_status_byte"] & 0xAF) for dtc in dtcs
        ),
        "dtcs": dtcs,
        "raw_payload_hex": _hex_bytes(payload),
    }, None


def _negative_response(payload: bytes) -> Optional[Dict[str, Any]]:
    if len(payload) < 2 or payload[:2] != bytes((0x7F, SERVICE_READ_DTC_INFORMATION)):
        return None
    if len(payload) < 3:
        return {
            "status": "malformed",
            "reason": "negative_response_missing_nrc",
            "raw_payload_hex": _hex_bytes(payload),
        }
    nrc = payload[2]
    result: Dict[str, Any] = {
        "status": "negative_response",
        "negative_response": {
            "request_service": SERVICE_READ_DTC_INFORMATION,
            "nrc": nrc,
            "nrc_hex": "0x{:02X}".format(nrc),
        },
        "raw_payload_hex": _hex_bytes(payload),
    }
    name = _NRC_NAMES.get(nrc)
    if name is not None:
        result["negative_response"]["name"] = name
    if nrc == 0x78:
        result["status"] = "response_pending"
    return result


def _response_prefix_matches(payload: bytes) -> Optional[bool]:
    """Classify whether an ISO-TP payload can answer this service request."""
    if not payload:
        return None
    if payload[0] == 0x59:
        if len(payload) < 2:
            return None
        return payload[1] == SUBFUNCTION_REPORT_DTC_BY_STATUS_MASK
    if payload[0] == 0x7F:
        if len(payload) < 2:
            return None
        return payload[1] == SERVICE_READ_DTC_INFORMATION
    return False


def _result_base(request_id: int, response_id: int, evidence: List[Dict[str, Any]]) -> Dict[str, Any]:
    return {
        "request_id": request_id,
        "request_id_hex": _hex_id(request_id),
        "response_id": response_id,
        "response_id_hex": _hex_id(response_id),
        "service": SERVICE_READ_DTC_INFORMATION,
        "service_hex": "0x19",
        "subfunction": SUBFUNCTION_REPORT_DTC_BY_STATUS_MASK,
        "subfunction_hex": "0x02",
        "request_status_mask": DTC_STATUS_MASK,
        "request_status_mask_hex": "0xFF",
        "dtcs": None,
        "evidence": evidence,
    }


def _extract_payload(
    bus: Any,
    *,
    request_id: int,
    response_id: int,
    start_time: float,
    timeout: float,
    evidence: List[Dict[str, Any]],
) -> Tuple[Optional[bytes], Optional[Dict[str, Any]], Optional[str], int]:
    """Collect one ISO-TP response, sending flow control for a first frame."""
    initial_deadline = start_time + timeout
    response_pending_deadline = start_time + MAX_RESPONSE_PENDING_WAIT
    deadline = initial_deadline
    expected_sequence = 1
    total_length: Optional[int] = None
    assembled = bytearray()
    response_pending_count = 0
    evidence_omitted = 0

    def record(frame: VolvoFrame, direction: str, kind: str, **extra: Any) -> None:
        nonlocal evidence_omitted
        if len(evidence) < MAX_EVIDENCE_FRAMES:
            item = _frame_record(direction, kind, frame)
            item.update(extra)
            evidence.append(item)
        else:
            evidence_omitted += 1

    def issue(status: str, reason: str) -> Tuple[None, Dict[str, Any], None, int]:
        detail: Dict[str, Any] = {"status": status, "reason": reason}
        if response_pending_count:
            detail["response_pending_count"] = response_pending_count
        return None, detail, None, evidence_omitted

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            if total_length is not None:
                return issue("incomplete", "timeout_waiting_for_consecutive_frame")
            if response_pending_count:
                return issue("response_pending_timeout", "response_pending_deadline_exceeded")
            return None, None, "no_response", evidence_omitted

        frame = bus.recv(timeout=remaining)
        if frame is None:
            continue
        matches = not frame.is_extended_id and frame.arbitration_id == response_id
        if not matches:
            evidence_omitted += 1
            continue
        record(frame, "rx", "response", matched=True)
        data = bytes(frame.data)
        if not data:
            return issue("malformed", "empty_can_frame")

        pci_type = data[0] >> 4
        if pci_type == 0:
            payload_length = data[0] & 0x0F
            if payload_length == 0 or payload_length > len(data) - 1:
                return issue("malformed", "invalid_single_frame_length")
            payload = data[1 : 1 + payload_length]
            if payload[:1] == b"\x59" and len(payload) < 2:
                return issue("malformed", "truncated_positive_response_header")
            if payload[:2] == bytes((0x59, SUBFUNCTION_REPORT_DTC_BY_STATUS_MASK)):
                return payload, None, None, evidence_omitted
            negative = _negative_response(payload)
            if negative is not None:
                if negative["status"] == "response_pending":
                    response_pending_count += 1
                    deadline = response_pending_deadline
                    continue
                return None, negative, None, evidence_omitted
            if payload[:1] == b"\x7F" and len(payload) < 2:
                return issue("malformed", "truncated_negative_response_header")
            continue

        if pci_type == 1:
            if len(data) < 2:
                return issue("malformed", "short_first_frame")
            frame_length = ((data[0] & 0x0F) << 8) | data[1]
            header_length = 2
            if frame_length == 0:
                if len(data) < 6:
                    return issue("malformed", "short_extended_first_frame")
                frame_length = int.from_bytes(data[2:6], "big")
                header_length = 6
            if frame_length == 0:
                return issue("malformed", "invalid_first_frame_length")
            if frame_length > MAX_ISOTP_PAYLOAD:
                return issue("malformed", "payload_exceeds_limit")
            initial = data[header_length:]
            if frame_length <= len(initial):
                return issue("malformed", "invalid_first_frame_length")
            if initial[:1] == b"\x59" and len(initial) < 2:
                return issue("malformed", "truncated_positive_response_header")
            if initial[:1] == b"\x7F" and len(initial) < 2:
                return issue("malformed", "truncated_negative_response_header")
            if _response_prefix_matches(initial) is False:
                continue
            total_length = frame_length
            assembled = bytearray(initial)
            expected_sequence = 1
            flow_control = VolvoFrame(
                request_id,
                bytes.fromhex("30 00 00 00 00 00 00 00"),
                False,
            )
            bus.send(flow_control)
            record(flow_control, "tx", "flow_control")
            continue

        if pci_type == 2:
            if total_length is None:
                return issue("malformed", "consecutive_frame_without_first_frame")
            sequence = data[0] & 0x0F
            if sequence != expected_sequence:
                return issue("malformed", "consecutive_frame_sequence_mismatch")
            remaining_payload = total_length - len(assembled)
            required_bytes = min(7, remaining_payload)
            if len(data) - 1 < required_bytes:
                return issue("malformed", "truncated_consecutive_frame")
            assembled.extend(data[1 : 1 + remaining_payload])
            expected_sequence = (expected_sequence + 1) & 0x0F
            if len(assembled) >= total_length:
                payload = bytes(assembled[:total_length])
                if payload[:1] == b"\x59" and len(payload) < 2:
                    return issue("malformed", "truncated_positive_response_header")
                if payload[:1] == b"\x7F" and len(payload) < 2:
                    return issue("malformed", "truncated_negative_response_header")
                if payload[:2] == bytes((0x59, SUBFUNCTION_REPORT_DTC_BY_STATUS_MASK)):
                    return payload, None, None, evidence_omitted
                negative = _negative_response(payload)
                if negative is not None:
                    if negative["status"] == "response_pending":
                        response_pending_count += 1
                        total_length = None
                        assembled.clear()
                        deadline = response_pending_deadline
                        continue
                    return None, negative, None, evidence_omitted
                continue
            continue

        if pci_type == 3:
            return issue("malformed", "unexpected_flow_control_frame")
        return issue("malformed", "unexpected_iso_tp_frame_type")


def read_uds_dtcs(
    bus: Any,
    request_id: int,
    response_id: int,
    timeout: float = 1.5,
) -> Dict[str, Any]:
    """Read DTCs from one ECU using physical 11-bit CAN IDs.

    ``bus`` must implement ``send(VolvoFrame)`` and ``recv(timeout)``. The only
    request sent is ``03 19 02 FF 00 00 00 00``. An NRC 0x78 extends the initial
    receive window, with total waiting from the request capped at five seconds.
    """
    if not 0 <= request_id <= 0x7FF or not 0 <= response_id <= 0x7FF:
        raise ValueError("request_id and response_id must be 11-bit CAN identifiers")
    if request_id == response_id:
        raise ValueError("request_id and response_id must differ")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be positive")

    evidence: List[Dict[str, Any]] = []
    result = _result_base(request_id, response_id, evidence)
    request = VolvoFrame(
        request_id,
        bytes.fromhex("03 19 02 FF 00 00 00 00"),
        False,
    )
    evidence.append(_frame_record("tx", "request", request))
    bus.send(request)
    start_time = time.monotonic()

    payload, issue, terminal_status, evidence_omitted = _extract_payload(
        bus,
        request_id=request_id,
        response_id=response_id,
        start_time=start_time,
        timeout=timeout,
        evidence=evidence,
    )
    if evidence_omitted:
        result["evidence_omitted"] = evidence_omitted
    if terminal_status is not None:
        result["status"] = terminal_status
        result["response_pending_count"] = 0
        return result
    if issue is not None:
        result.update(issue)
        result["dtcs"] = None
        return result
    if payload is None:
        result["status"] = "no_response"
        return result

    positive, parse_error = _parse_positive_response(payload)
    if parse_error is not None:
        result.update({"status": "malformed", "reason": parse_error})
        return result
    assert positive is not None
    result.update(positive)
    return result
