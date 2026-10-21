"""Tests for the guarded read-only VIDA script runner."""

import json
from pathlib import Path

import pytest

from volvo_p3.cli import build_parser, cmd_vida_run_script, main
from volvo_p3.protocol import VolvoFrame
from volvo_p3.vida_runner import (
    AddressMapError,
    RequestRecord,
    RunPlan,
    build_run_plan,
    decode_response_payload,
    parse_address_map,
    run_plan,
)
from volvo_p3.vida_script import parse_script_xml


FIXTURES = Path(__file__).with_name("fixtures")
SCRIPT_XML = FIXTURES / "vida_script_read_dtc.xml"
SCRIPT_CLEAR = FIXTURES / "vida_script_clear_dtc.xml"


def read_script():
    return parse_script_xml(SCRIPT_XML.read_text(encoding="utf-8"))


def clear_script():
    return parse_script_xml(SCRIPT_CLEAR.read_text(encoding="utf-8"))


def write_map(tmp_path, document, name="addresses.json"):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


class FakeTransport:
    """Record sent frames and return canned responses in order."""

    def __init__(self, responses=()):
        self.responses = list(responses)
        self.sent = []
        self.recv_timeouts = []

    def send(self, frame):
        self.sent.append(frame)

    def recv(self, timeout=1.0):
        self.recv_timeouts.append(timeout)
        if self.responses:
            return self.responses.pop(0)
        return None


def dtc_response(request_id=0x7E0, records=((0x03, 0x42, 0x11, 0x09),)):
    payload = bytes((0x59, 0x02, 0xFF)) + b"".join(bytes(record) for record in records)
    return VolvoFrame(
        request_id + 8,
        bytes((len(payload),)) + payload + bytes(8 - 1 - len(payload)),
        False,
    )


def make_record(service_id, request_bytes, can_id=0x7E0, index=1):
    return RequestRecord(
        index=index,
        source_ecu="EcuAddress 0x11",
        service_id=service_id,
        request_bytes=request_bytes,
        status="planned",
        can_id=can_id,
    )


# Address map ---------------------------------------------------------------


def test_address_map_accepts_prefixed_hex_and_decimal():
    address_map = parse_address_map(
        {"addresses": {"0x11": "0x7E0", "831701": "2016"}}
    )

    assert address_map.resolve(0x11, None).can_id == 0x7E0
    assert address_map.resolve(None, 831701).can_id == 2016
    assert address_map.resolve(0x11, None).logical_kind == "ecu_address"


def test_address_map_accepts_flat_mapping_and_unprefixed_hex():
    address_map = parse_address_map({"17": "7E0"})

    match = address_map.resolve(17, None)
    assert match.can_id == 0x7E0
    assert match.mapped is True


def test_address_map_accepts_integer_values():
    address_map = parse_address_map({"0x11": 2016, "381101": 0x7E1})

    assert address_map.get(0x11) == 2016
    assert address_map.get(381101) == 0x7E1


def test_address_map_prefers_ecu_address_over_ecu_type():
    address_map = parse_address_map({"0x11": "0x7E0", "381101": "0x7E1"})

    match = address_map.resolve(0x11, 381101)

    assert match.logical_kind == "ecu_address"
    assert match.logical_address == 0x11
    assert match.can_id == 0x7E0


@pytest.mark.parametrize("can_id", ["0x800", "-1", "0x7DF"])
def test_address_map_rejects_non_physical_or_too_large_ids(can_id):
    with pytest.raises(AddressMapError):
        parse_address_map({"0x11": can_id})


def test_address_map_rejects_conflicting_duplicate_keys():
    with pytest.raises(AddressMapError, match="mapped to both"):
        parse_address_map({"0x11": "0x7E0", "17": "0x7E1"})


def test_address_map_rejects_non_object():
    with pytest.raises(AddressMapError, match="JSON object"):
        parse_address_map(["0x11", "0x7E0"])


# Planning ------------------------------------------------------------------


def test_plan_uses_read_only_requests_and_maps_both_address_kinds():
    plan = build_run_plan(
        read_script(),
        parse_address_map({"addresses": {"0x11": "0x7E0", "381101": "0x7E1"}}),
    )

    assert [record.service_id for record in plan.records] == [0x19, 0x22, 0x19]
    assert [record.can_id for record in plan.records] == [0x7E0, 0x7E0, 0x7E1]
    assert [record.request_hex for record in plan.records] == [
        "19 02 FF",
        "22 F1 90",
        "19",
    ]
    assert all(record.status == "planned" for record in plan.records)
    assert plan.unmapped == ()


def test_plan_reports_unmapped_address_without_guessing():
    plan = build_run_plan(
        read_script(), parse_address_map({"0x11": "0x7E0"})
    )

    assert [record.status for record in plan.records] == [
        "planned",
        "planned",
        "skipped_unmapped",
    ]
    assert plan.records[2].can_id is None
    assert plan.records[2].logical_kind == "ecu_type"
    assert plan.records[2].logical_address == 381101
    assert len(plan.unmapped) == 1
    assert plan.unmapped[0].kind == "ecu_type"
    assert plan.unmapped[0].value == 381101


def test_plan_drops_every_non_read_service():
    plan = build_run_plan(
        clear_script(), parse_address_map({"17": "0x7E0"})
    )

    assert [record.service_id for record in plan.records] == [0x19]
    assert all(
        record.service_id in {0x01, 0x09, 0x19, 0x22}
        for record in plan.records
    )


def test_plan_marks_service_only_request():
    plan = build_run_plan(
        read_script(),
        parse_address_map({"0x11": "0x7E0", "381101": "0x7E1"}),
    )

    record = plan.records[2]
    assert record.request_bytes == b"\x19"
    assert "service byte only" in record.detail


def test_plan_to_dict_reports_unmapped_and_counts():
    plan = build_run_plan(
        read_script(), parse_address_map({"0x11": "0x7E0"})
    )

    payload = plan.to_dict()

    assert payload["docno"] == "VCC-227144"
    assert payload["counts"]["planned"] == 2
    assert payload["counts"]["skipped_unmapped"] == 1
    assert payload["unmapped"][0]["source_ecu"] == "EcuType 381101"


# Running -------------------------------------------------------------------


def test_dry_run_does_not_touch_transport():
    plan = build_run_plan(
        read_script(),
        parse_address_map({"0x11": "0x7E0", "381101": "0x7E1"}),
    )
    transport = FakeTransport([dtc_response()])

    result = run_plan(plan, transport, timeout=0.01, dry_run=True)

    assert transport.sent == []
    assert transport.recv_timeouts == []
    assert result.dry_run is True
    assert result.transport_touched is False
    assert [record.status for record in result.records] == [
        "planned",
        "planned",
        "planned",
    ]


def test_execute_sends_read_request_and_decodes_dtc_response():
    plan = build_run_plan(
        read_script(),
        parse_address_map({"0x11": "0x7E0", "381101": "0x7E1"}),
    )
    transport = FakeTransport([dtc_response(0x7E0)])

    result = run_plan(plan, transport, timeout=0.01)

    assert len(transport.sent) == 3
    frame = transport.sent[0]
    assert frame.arbitration_id == 0x7E0
    assert frame.is_extended_id is False
    assert frame.data == bytes.fromhex("03 19 02 FF 00 00 00 00")

    first = result.records[0]
    assert first.status == "response"
    assert first.transmitted is True
    assert first.response_id == 0x7E8
    assert first.response_payload == bytes.fromhex("59 02 FF 03 42 11 09")
    assert first.decoded["kind"] == "dtc_list"
    assert first.decoded["status"] == "ok"
    assert first.decoded["dtcs"][0]["code"] == "P0342"
    assert first.decoded["dtcs"][0]["failure_type_hex"] == "0x11"
    assert first.decoded["dtcs"][0]["status_hex"] == "0x09"

    assert result.records[1].status == "timeout"
    assert result.records[2].status == "timeout"


def test_timeout_is_recorded_and_run_continues():
    plan = build_run_plan(
        read_script(),
        parse_address_map({"0x11": "0x7E0", "381101": "0x7E1"}),
    )
    transport = FakeTransport()

    result = run_plan(plan, transport, timeout=0.01)

    assert [record.status for record in result.records] == [
        "timeout",
        "timeout",
        "timeout",
    ]
    assert len(transport.sent) == 3
    assert all(record.transmitted for record in result.records)
    assert result.aborted is False
    assert result.counts["timeout"] == 3


def test_non_read_service_is_never_sent():
    plan = RunPlan(
        docno=None,
        title=None,
        script_type=None,
        records=(
            make_record(0x14, b"\x14\xFF\x00", index=1),
            make_record(0x10, b"\x10\x03", index=2),
            make_record(0x19, b"\x19\x02\xFF", index=3),
        ),
    )
    transport = FakeTransport([dtc_response(0x7E0)])

    result = run_plan(plan, transport, timeout=0.01)

    assert [record.status for record in result.records] == [
        "skipped_non_read",
        "skipped_non_read",
        "response",
    ]
    assert [frame.data[1] for frame in transport.sent] == [0x19]


def test_unmapped_plan_record_is_skipped_without_sending():
    plan = build_run_plan(
        read_script(), parse_address_map({"0x11": "0x7E0"})
    )
    transport = FakeTransport()

    result = run_plan(plan, transport, timeout=0.01)

    assert len(transport.sent) == 2
    assert result.records[2].status == "skipped_unmapped"
    assert result.records[2].transmitted is False


def test_transport_refusal_is_recorded_and_run_continues():
    plan = RunPlan(
        docno=None,
        title=None,
        script_type=None,
        records=(
            make_record(0x22, b"\x22\xF1\x90", index=1),
            make_record(0x19, b"\x19\x02\xFF", index=2),
        ),
    )

    class PickyTransport(FakeTransport):
        def send(self, frame):
            if frame.data[1] == 0x22:
                raise ValueError("DiCE transmit is limited to OBD read requests")
            super().send(frame)

    transport = PickyTransport([dtc_response(0x7E0)])
    result = run_plan(plan, transport, timeout=0.01)

    assert [record.status for record in result.records] == ["error", "response"]
    assert "transport refused request" in result.records[0].detail
    assert [frame.data[1] for frame in transport.sent] == [0x19]


# Payload decoding ----------------------------------------------------------


def test_unsupported_service_preserves_raw_payload():
    payload = bytes.fromhex("62 F1 90 41 42")

    decoded = decode_response_payload(0x22, payload)

    assert decoded["kind"] == "raw"
    assert decoded["raw_payload_hex"] == "62 F1 90 41 42"


def test_negative_response_uses_known_nrc_only():
    decoded = decode_response_payload(0x19, bytes.fromhex("7F 19 31"))

    assert decoded["kind"] == "negative_response"
    assert decoded["nrc"] == 0x31
    assert decoded["nrc_name"] == "requestOutOfRange"


def test_unknown_dtc_subfunction_is_not_invented():
    decoded = decode_response_payload(0x19, bytes.fromhex("59 04 01"))

    assert decoded["kind"] == "dtc_list"
    assert decoded["status"] == "raw"
    assert decoded["raw_payload_hex"] == "59 04 01"


# CLI ----------------------------------------------------------------------


def test_cli_dry_run_prints_plan_and_never_executes(tmp_path, capsys):
    map_path = write_map(tmp_path, {"addresses": {"0x11": "0x7E0"}})

    status = main(
        [
            "vida-run-script",
            "--script",
            str(SCRIPT_XML),
            "--address-map",
            str(map_path),
        ]
    )

    out = capsys.readouterr().out
    assert status == 0
    assert "Read-only requests (3):" in out
    assert "skipped_unmapped" in out
    assert "Unmapped logical addresses (1):" in out
    assert "Mode: plan only. No CAN frames were sent." in out


def test_cli_dry_run_json_has_plan_and_no_result(tmp_path, capsys):
    map_path = write_map(tmp_path, {"0x11": "0x7E0"})

    status = main(
        [
            "vida-run-script",
            "--script",
            str(SCRIPT_XML),
            "--address-map",
            str(map_path),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["executed"] is False
    assert payload["dry_run"] is True
    assert payload["result"] is None
    assert [record["status"] for record in payload["plan"]["records"]] == [
        "planned",
        "planned",
        "skipped_unmapped",
    ]
    assert payload["plan"]["unmapped"][0]["value"] == 381101
    assert payload["address_map"]["entries"][0]["can_id_hex"] == "0x7E0"


def test_cli_execute_runs_through_transport(monkeypatch, tmp_path, capsys):
    map_path = write_map(
        tmp_path, {"0x11": "0x7E0", "381101": "0x7E1"}
    )
    sent = []
    answered = {"done": False}

    class FakeBus:
        def __init__(self, rpc, uds_requests=()):
            self.uds_requests = tuple(uds_requests)

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def send(self, frame):
            sent.append(frame)

        def recv(self, timeout=1.0):
            if (
                not answered["done"]
                and sent
                and sent[-1].data[:4] == bytes.fromhex("03 19 02 FF")
            ):
                answered["done"] = True
                return dtc_response(0x7E0)
            return None

    class FakeRpc:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr("volvo_p3.dice_can.DiceCanTransport", FakeBus)
    monkeypatch.setattr("volvo_p3.dice_rpc.DiceRpc", FakeRpc)

    status = main(
        [
            "vida-run-script",
            "--script",
            str(SCRIPT_XML),
            "--address-map",
            str(map_path),
            "--execute",
            "--timeout",
            "0.02",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["executed"] is True
    assert payload["dry_run"] is False
    assert payload["result"]["transport_touched"] is True
    assert payload["result"]["records"][0]["status"] == "response"
    assert payload["result"]["records"][0]["decoded"]["dtcs"][0]["code"] == "P0342"
    assert sent[0].arbitration_id == 0x7E0


def test_cli_missing_address_map_is_clear(tmp_path, capsys):
    missing = tmp_path / "missing.json"

    status = main(
        [
            "vida-run-script",
            "--script",
            str(SCRIPT_XML),
            "--address-map",
            str(missing),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "address map not found: {}".format(missing) in captured.err
    assert "Traceback" not in captured.err


def test_cli_invalid_address_map_is_clear(tmp_path, capsys):
    map_path = write_map(tmp_path, {"addresses": {"0x11": "0x800"}})

    status = main(
        [
            "vida-run-script",
            "--script",
            str(SCRIPT_XML),
            "--address-map",
            str(map_path),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "invalid address map" in captured.err
    assert "11-bit range" in captured.err
    assert "Traceback" not in captured.err


def test_cli_execute_without_mapped_requests_reports(tmp_path, capsys):
    map_path = write_map(tmp_path, {"0x99": "0x7E0"})

    status = main(
        [
            "vida-run-script",
            "--script",
            str(SCRIPT_XML),
            "--address-map",
            str(map_path),
            "--execute",
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "nothing to execute" in captured.err
    assert "Traceback" not in captured.err


def test_parser_registers_vida_run_script(tmp_path):
    map_path = write_map(tmp_path, {"0x11": "0x7E0"})

    args = build_parser().parse_args(
        [
            "vida-run-script",
            "--script",
            str(SCRIPT_XML),
            "--address-map",
            str(map_path),
        ]
    )

    assert args.func is cmd_vida_run_script
    assert args.execute is False
    assert args.timeout == 1.5
    assert args.json is False
