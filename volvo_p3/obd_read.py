"""Read generic OBD-II diagnostic trouble codes over ISO-TP on 11-bit CAN.

This module only sends the functional OBD requests for supported-PID discovery
and DTC services 03, 07, and 0A. It never sends a clear or reset request.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from .protocol import VolvoFrame


FUNCTIONAL_REQUEST_ID = 0x7DF
FIRST_RESPONSE_ID = 0x7E8
LAST_RESPONSE_ID = 0x7EF

SERVICE_CURRENT_DATA = 0x01
SERVICE_STORED_DTC = 0x03
SERVICE_PENDING_DTC = 0x07
SERVICE_PERMANENT_DTC = 0x0A

_POSITIVE_SERVICE = {
    SERVICE_CURRENT_DATA: 0x41,
    SERVICE_STORED_DTC: 0x43,
    SERVICE_PENDING_DTC: 0x47,
    SERVICE_PERMANENT_DTC: 0x4A,
}

_UNSUPPORTED_NRC = {0x11, 0x12, 0x7F}
_NRC_NAMES = {
    0x11: "service_not_supported",
    0x12: "subfunction_not_supported",
    0x13: "incorrect_message_length_or_invalid_format",
    0x22: "conditions_not_correct",
    0x31: "request_out_of_range",
    0x7F: "service_not_supported_in_active_session",
}


@dataclass
class _Assembly:
    total_length: int
    buffer: bytearray
    next_sequence: int = 1


@dataclass
class _Collected:
    payloads: Dict[int, bytes] = field(default_factory=dict)
    issues: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    evidence_indexes: List[int] = field(default_factory=list)


def _hex_bytes(data: bytes) -> str:
    return bytes(data).hex(" ").upper()


def _frame_record(phase: str, direction: str, kind: str, frame: VolvoFrame) -> Dict[str, Any]:
    return {
        "phase": phase,
        "direction": direction,
        "kind": kind,
        "arbitration_id": frame.arbitration_id,
        "arbitration_id_hex": "0x{:03X}".format(frame.arbitration_id),
        "is_extended_id": bool(frame.is_extended_id),
        "data_hex": _hex_bytes(frame.data),
    }


def _is_response_id(frame: VolvoFrame) -> bool:
    return (
        not frame.is_extended_id
        and FIRST_RESPONSE_ID <= frame.arbitration_id <= LAST_RESPONSE_ID
    )


def _status_for_nrc(nrc: int) -> str:
    return "unsupported" if nrc in _UNSUPPORTED_NRC else "negative_response"


def _negative_response(payload: bytes, request_service: int) -> Optional[Dict[str, Any]]:
    if len(payload) < 2 or payload[0] != 0x7F or payload[1] != request_service:
        return None
    if len(payload) < 3:
        return {"malformed": True, "raw_hex": _hex_bytes(payload)}
    nrc = payload[2]
    response: Dict[str, Any] = {
        "request_service": request_service,
        "nrc": nrc,
        "nrc_hex": "0x{:02X}".format(nrc),
    }
    name = _NRC_NAMES.get(nrc)
    if name is not None:
        response["name"] = name
    return response


def decode_dtc(raw: bytes) -> str:
    """Decode one SAE J2012 two-byte DTC such as ``P0133``."""
    if len(raw) != 2:
        raise ValueError("a DTC must be exactly two bytes")
    first, second = raw
    system = ("P", "C", "B", "U")[(first >> 6) & 0x03]
    return "{}{}{:X}{:X}{:X}".format(
        system,
        (first >> 4) & 0x03,
        first & 0x0F,
        (second >> 4) & 0x0F,
        second & 0x0F,
    )


def _decode_dtcs(payload: bytes) -> Tuple[Optional[List[str]], Optional[str]]:
    """Return decoded codes and an error when the service data is malformed."""
    if len(payload) < 2:
        return None, "missing_dtc_count"
    dtc_count = payload[1]
    dtc_bytes = payload[2:]
    if len(dtc_bytes) % 2:
        return None, "odd_number_of_dtc_bytes"
    codes: List[str] = []
    for offset in range(0, len(dtc_bytes), 2):
        pair = dtc_bytes[offset : offset + 2]
        if pair != b"\x00\x00":
            codes.append(decode_dtc(pair))
    if len(codes) != dtc_count:
        return None, "dtc_count_mismatch"
    return codes, None


def _supported_pids(payload: bytes) -> Tuple[List[int], bool]:
    """Decode PID 01 through 20 from a Mode 01 PID 00 response."""
    bitmap = payload[2:]
    pids: List[int] = []
    for byte_index, value in enumerate(bitmap[:4]):
        for bit in range(8):
            if value & (1 << (7 - bit)):
                pids.append(byte_index * 8 + bit + 1)
    return pids, len(bitmap) >= 4


def _request_frame(service: int, pid: Optional[int] = None) -> VolvoFrame:
    message = [service] if pid is None else [service, pid]
    data = bytes([len(message), *message]).ljust(8, b"\x00")
    return VolvoFrame(FUNCTIONAL_REQUEST_ID, data, False)


def _flow_control_frame(response_id: int) -> VolvoFrame:
    return VolvoFrame(response_id - 8, bytes.fromhex("30 00 00 00 00 00 00 00"), False)


def _payload_matches_request_prefix(
    payload: bytes, request_service: int, request_pid: Optional[int]
) -> Optional[bool]:
    """Return false for another service/PID, None while a prefix is incomplete."""
    if not payload:
        return None
    if payload[0] == 0x7F:
        if len(payload) < 2:
            return None
        return payload[1] == request_service
    if payload[0] != _POSITIVE_SERVICE[request_service]:
        return False
    if request_pid is None:
        return True
    if len(payload) < 2:
        return None
    return payload[1] == request_pid


def _collect(
    bus: Any,
    request: VolvoFrame,
    phase: str,
    timeout: float,
    recv_timeout: float,
    max_payload: int,
    evidence: List[Dict[str, Any]],
) -> _Collected:
    result = _Collected()
    start = len(evidence)
    request_length = request.data[0] & 0x0F
    request_payload = request.data[1 : 1 + request_length]
    request_service = request_payload[0]
    request_pid = request_payload[1] if len(request_payload) > 1 else None
    bus.send(request)
    evidence.append(_frame_record(phase, "tx", "request", request))

    active: Dict[int, _Assembly] = {}
    deadline = time.monotonic() + timeout

    def record_issue(response_id: int, reason: str, status: str = "incomplete") -> None:
        if response_id not in result.payloads:
            result.issues[response_id] = {"status": status, "reason": reason}

    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        frame = bus.recv(timeout=min(recv_timeout, remaining))
        if frame is None:
            continue

        evidence.append(_frame_record(phase, "rx", "response", frame))
        if not _is_response_id(frame):
            continue
        response_id = frame.arbitration_id
        data = bytes(frame.data)
        if not data:
            record_issue(response_id, "empty_can_frame", "malformed")
            continue

        pci_type = data[0] >> 4
        if pci_type == 0:
            payload_length = data[0] & 0x0F
            if payload_length == 0 or payload_length > len(data) - 1:
                record_issue(response_id, "invalid_single_frame_length", "malformed")
                continue
            payload = data[1 : 1 + payload_length]
            if _payload_matches_request_prefix(payload, request_service, request_pid) is False:
                continue
            result.payloads[response_id] = payload
            result.issues.pop(response_id, None)
            continue

        if pci_type == 1:
            if len(data) < 2:
                record_issue(response_id, "short_first_frame", "malformed")
                continue
            total_length = ((data[0] & 0x0F) << 8) | data[1]
            initial_payload = data[2:]
            if total_length <= len(initial_payload):
                record_issue(response_id, "invalid_first_frame_length", "malformed")
                continue
            if total_length > max_payload:
                record_issue(response_id, "payload_exceeds_limit", "incomplete")
                continue
            if _payload_matches_request_prefix(initial_payload, request_service, request_pid) is False:
                continue
            active[response_id] = _Assembly(total_length, bytearray(initial_payload))
            flow_control = _flow_control_frame(response_id)
            bus.send(flow_control)
            evidence.append(_frame_record(phase, "tx", "flow_control", flow_control))
            result.issues.pop(response_id, None)
            continue

        if pci_type == 2:
            assembly = active.get(response_id)
            if assembly is None:
                continue
            sequence = data[0] & 0x0F
            if sequence != assembly.next_sequence:
                active.pop(response_id, None)
                record_issue(response_id, "consecutive_frame_sequence_mismatch")
                continue
            assembly.buffer.extend(data[1 : 1 + assembly.total_length - len(assembly.buffer)])
            assembly.next_sequence = (assembly.next_sequence + 1) & 0x0F
            if len(assembly.buffer) >= assembly.total_length:
                payload = bytes(assembly.buffer[: assembly.total_length])
                if _payload_matches_request_prefix(payload, request_service, request_pid) is not False:
                    result.payloads[response_id] = payload
                    result.issues.pop(response_id, None)
                active.pop(response_id, None)
            continue

        record_issue(response_id, "unexpected_iso_tp_frame_type", "malformed")

    for response_id in active:
        record_issue(response_id, "timeout_waiting_for_consecutive_frame")
    result.evidence_indexes = list(range(start, len(evidence)))
    return result


def _parse_service_payload(payload: bytes, service: int) -> Optional[Dict[str, Any]]:
    negative = _negative_response(payload, service)
    if negative is not None:
        if negative.get("malformed"):
            return {"status": "malformed", "negative_response": negative}
        return {"status": _status_for_nrc(negative["nrc"]), "negative_response": negative}

    positive_service = _POSITIVE_SERVICE[service]
    if not payload or payload[0] != positive_service:
        return None
    if service == SERVICE_CURRENT_DATA:
        if len(payload) < 2 or payload[1] != 0x00:
            return None
        supported, complete = _supported_pids(payload)
        return {
            "status": "ok",
            "supported_pids": supported,
            "supported_pids_complete": complete,
        }

    dtcs, error = _decode_dtcs(payload)
    if error is not None:
        return {"status": "malformed", "reason": error}
    return {"status": "ok", "dtcs": dtcs}


def _result_for_payload(payload: bytes, service: int, evidence_indexes: List[int]) -> Optional[Dict[str, Any]]:
    parsed = _parse_service_payload(payload, service)
    if parsed is None:
        return None
    if service != SERVICE_CURRENT_DATA and parsed.get("status") != "ok":
        parsed["dtcs"] = None
    parsed["raw_payload_hex"] = _hex_bytes(payload)
    parsed["evidence_indexes"] = list(evidence_indexes)
    return parsed


def _result_for_issue(issue: Dict[str, Any], evidence_indexes: List[int]) -> Dict[str, Any]:
    result = dict(issue)
    result["dtcs"] = None
    result["evidence_indexes"] = list(evidence_indexes)
    return result


def _missing_result(evidence_indexes: List[int]) -> Dict[str, Any]:
    return {"status": "no_response", "dtcs": None, "evidence_indexes": list(evidence_indexes)}


def scan_obd(
    bus: Any,
    *,
    discovery_timeout: float = 0.25,
    response_timeout: float = 0.25,
    recv_timeout: float = 0.05,
    max_payload: int = 4095,
) -> Dict[str, Any]:
    """Discover OBD responders, then read stored, pending, and permanent DTCs.

    ``bus`` must provide ``send(VolvoFrame)`` and ``recv(timeout)`` methods.
    Receive windows are bounded by the supplied timeouts. All returned values,
    including the raw evidence records, are JSON serializable.
    """
    if discovery_timeout <= 0 or response_timeout <= 0 or recv_timeout <= 0:
        raise ValueError("timeouts must be positive")
    if max_payload < 1 or max_payload > 4095:
        raise ValueError("max_payload must be between 1 and 4095 bytes")

    evidence: List[Dict[str, Any]] = []
    discovery_request = _request_frame(SERVICE_CURRENT_DATA, 0x00)
    discovery = _collect(
        bus,
        discovery_request,
        "discovery",
        discovery_timeout,
        recv_timeout,
        max_payload,
        evidence,
    )

    ecu_results: Dict[int, Dict[str, Any]] = {}
    discovery_responders: List[Dict[str, Any]] = []
    discovery_frame_indexes = discovery.evidence_indexes
    for response_id, payload in sorted(discovery.payloads.items()):
        parsed = _result_for_payload(payload, SERVICE_CURRENT_DATA, discovery_frame_indexes)
        if parsed is None:
            continue
        ecu_results.setdefault(response_id, {"modes": {}})
        responder = {
            "response_id": response_id,
            "response_id_hex": "0x{:03X}".format(response_id),
            **parsed,
        }
        discovery_responders.append(responder)

    for response_id, issue in sorted(discovery.issues.items()):
        ecu_results.setdefault(response_id, {"modes": {}})
        discovery_responders.append(
            {
                "response_id": response_id,
                "response_id_hex": "0x{:03X}".format(response_id),
                **_result_for_issue(issue, discovery_frame_indexes),
            }
        )

    discovery_positive = any(item["status"] == "ok" for item in discovery_responders)
    discovery_status = "ok" if discovery_positive else "no_response"
    if not discovery_positive:
        statuses = {item["status"] for item in discovery_responders}
        if "unsupported" in statuses:
            discovery_status = "unsupported"
        elif "negative_response" in statuses:
            discovery_status = "negative_response"
        elif "incomplete" in statuses or "malformed" in statuses:
            discovery_status = "incomplete" if "incomplete" in statuses else "malformed"

    mode_transactions: Dict[int, _Collected] = {}
    mode_responses: Dict[int, Dict[int, Dict[str, Any]]] = {}
    for service in (SERVICE_STORED_DTC, SERVICE_PENDING_DTC, SERVICE_PERMANENT_DTC):
        phase = "{:02X}".format(service)
        collected = _collect(
            bus,
            _request_frame(service),
            phase,
            response_timeout,
            recv_timeout,
            max_payload,
            evidence,
        )
        mode_transactions[service] = collected
        parsed_by_id: Dict[int, Dict[str, Any]] = {}
        for response_id, payload in sorted(collected.payloads.items()):
            parsed = _result_for_payload(payload, service, collected.evidence_indexes)
            if parsed is not None:
                parsed_by_id[response_id] = parsed
                ecu_results.setdefault(response_id, {"modes": {}})
        for response_id in collected.issues:
            ecu_results.setdefault(response_id, {"modes": {}})
        mode_responses[service] = parsed_by_id

    for response_id, ecu in sorted(ecu_results.items()):
        for service in (SERVICE_STORED_DTC, SERVICE_PENDING_DTC, SERVICE_PERMANENT_DTC):
            collected = mode_transactions[service]
            parsed = mode_responses[service].get(response_id)
            if parsed is not None:
                ecu["modes"]["{:02X}".format(service)] = parsed
            elif response_id in collected.issues:
                ecu["modes"]["{:02X}".format(service)] = _result_for_issue(
                    collected.issues[response_id], collected.evidence_indexes
                )
            else:
                ecu["modes"]["{:02X}".format(service)] = _missing_result(
                    collected.evidence_indexes
                )

    return {
        "protocol": "ISO-TP over 11-bit CAN",
        "discovery": {
            "status": discovery_status,
            "request_id": FUNCTIONAL_REQUEST_ID,
            "request_id_hex": "0x{:03X}".format(FUNCTIONAL_REQUEST_ID),
            "responders": discovery_responders,
        },
        "ecus": [
            {
                "response_id": response_id,
                "response_id_hex": "0x{:03X}".format(response_id),
                "modes": ecu["modes"],
            }
            for response_id, ecu in sorted(ecu_results.items())
        ],
        "evidence": evidence,
    }
