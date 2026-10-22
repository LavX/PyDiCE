"""Tests for the guarded read-only diagnostic script runner."""

import json
from pathlib import Path

import pytest

from volvo_p3.cli import build_parser, cmd_diag_run_script, main
from volvo_p3.module_scan import ModuleTarget
from volvo_p3.protocol import VolvoFrame
from volvo_p3.script_runner import (
    AddressMap,
    AddressMapError,
    RequestRecord,
    RunPlan,
    build_profile_name_map,
    build_run_plan,
    decode_response_payload,
    load_profile_name_map,
    normalize_ecu_name,
    parse_address_map,
    run_plan,
)
from volvo_p3.script_plan import parse_script_xml
from volvo_p3.vida_service_templates import READ_ONLY_CATALOG_SERVICES


FIXTURES = Path(__file__).with_name("fixtures")
SCRIPT_XML = FIXTURES / "script_plan_read_dtc.xml"
SCRIPT_CLEAR = FIXTURES / "script_plan_clear_dtc.xml"
SCRIPT_NAMED = FIXTURES / "script_plan_named.xml"
PROFILE_NAMED = FIXTURES / "profile_named.json"


def read_script():
    return parse_script_xml(SCRIPT_XML.read_text(encoding="utf-8"))


def named_script():
    return parse_script_xml(SCRIPT_NAMED.read_text(encoding="utf-8"))


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


# Name entries --------------------------------------------------------------


def test_address_map_resolves_name_when_numeric_is_absent():
    address_map = AddressMap(names={"D.I.M.": "0x720"})

    match = address_map.resolve(17, None, "dim", None)

    assert match.can_id == 0x720
    assert match.logical_kind == "ecu_name"
    assert match.logical_address is None


def test_address_map_numeric_entry_wins_over_a_name_entry():
    address_map = AddressMap({"17": "0x7E1"}, {"ECM": "0x7E0"})

    match = address_map.resolve(17, None, "ECM")

    assert match.can_id == 0x7E1
    assert match.logical_kind == "ecu_address"


def test_address_map_uses_identifier_after_the_name_misses():
    address_map = AddressMap(names={"DIM": "0x720"})

    match = address_map.resolve(
        None, 381101, "Driver Information Module", "DIM"
    )

    assert match.can_id == 0x720
    assert match.logical_kind == "ecu_identifier"


def test_address_map_reports_a_name_without_guessing():
    address_map = AddressMap(names={"ECM": "0x7E0"})

    match = address_map.resolve(None, None, "ABS")

    assert match.can_id is None
    assert match.logical_kind == "ecu_name"
    assert match.logical_address is None


def test_address_map_rejects_conflicting_name_entries():
    with pytest.raises(AddressMapError, match="ECU name"):
        AddressMap(names={"DIM": "0x720", "D.I.M.": "0x721"})


def test_normalize_ecu_name_folds_case_and_punctuation_only():
    assert normalize_ecu_name(" d.i.m. ") == "DIM"
    assert normalize_ecu_name("Driver Information Module") != "DIM"
    assert normalize_ecu_name("lås") == "LÅS"


# Profile name matching -----------------------------------------------------


def test_profile_name_map_matches_names_and_identifiers_exactly():
    name_map, report = load_profile_name_map(named_script(), PROFILE_NAMED)

    assert name_map.get_name("ECM") == 0x7E0
    assert name_map.get_name("DIM") == 0x720
    assert [entry.status for entry in report.entries] == [
        "matched",
        "matched",
        "missing",
        "ambiguous",
    ]
    assert report.matched[0].field == "ecu_name"
    assert report.matched[1].field == "ecu_identifier"
    assert report.missing[0].value == "ABS"
    assert report.ambiguous[0].value == "GWM"
    assert set(report.ambiguous[0].profile_names) == {"GWM", "G.W.M"}
    assert report.counts()["ecus"] == 4


def test_profile_name_map_never_maps_an_ambiguous_name():
    name_map, report = build_profile_name_map(
        named_script(),
        [
            ModuleTarget("GWM", 0x730, 0x738),
            ModuleTarget("G.W.M", 0x731, 0x739),
        ],
    )

    assert name_map.name_items() == ()
    assert len(report.ambiguous) == 1
    assert report.ambiguous[0].can_id is None


def test_profile_name_map_reports_a_missing_profile(tmp_path):
    with pytest.raises(ValueError, match="module profile not found"):
        load_profile_name_map(named_script(), tmp_path / "missing.json")


def test_profile_name_map_rejects_an_empty_profile(tmp_path):
    path = tmp_path / "empty.json"
    path.write_text(json.dumps({"modules": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="invalid module profile"):
        load_profile_name_map(named_script(), path)


# Planning ------------------------------------------------------------------


def test_plan_uses_read_only_requests_and_maps_both_address_kinds():
    plan = build_run_plan(
        read_script(),
        parse_address_map({"addresses": {"0x11": "0x7E0", "381101": "0x7E1"}}),
    )

    assert [record.service_id for record in plan.records] == [1, 2, 1]
    assert [record.can_id for record in plan.records] == [0x7E0, 0x7E0, 0x7E1]
    assert [record.request_hex for record in plan.records] == [
        "19 02 FF",
        "22 F1 90",
        "19 02 FF",
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


def test_plan_with_profile_name_map_plans_matches_and_reports_the_rest():
    name_map, _ = load_profile_name_map(named_script(), PROFILE_NAMED)

    plan = build_run_plan(named_script(), name_map)

    assert [record.status for record in plan.records] == [
        "planned",
        "planned",
        "skipped_unmapped",
        "skipped_unmapped",
    ]
    assert [record.can_id for record in plan.records] == [0x7E0, 0x720, None, None]
    assert [record.logical_kind for record in plan.records] == [
        "ecu_name",
        "ecu_identifier",
        "ecu_address",
        "ecu_address",
    ]
    assert [item.name for item in plan.unmapped] == ["ABS", "GWM"]
    assert plan.to_dict()["unmapped"][0]["name"] == "ABS"


def test_named_script_still_drops_every_non_read_service():
    script = parse_script_xml(
        """<script type="ClearDtc"><content><nodes>
        <node class="Components.VehComm">
          <property name="Mode" value="EcuAddress" />
          <property name="EcuAddress" value="17" />
          <property name="EcuName" value="ECM" />
          <property name="ServiceId" value="14" />
          <property name="ServiceId" value="27" />
          <property name="ServiceId" value="1" />
          <property name="Request" value="14 FF FF" />
          <property name="Request" value="27 01" />
          <property name="Request" value="19 02 FF" />
        </node>
        </nodes></content></script>"""
    )
    name_map, _ = load_profile_name_map(script, PROFILE_NAMED)

    plan = build_run_plan(script, name_map)

    assert [record.service_id for record in plan.records] == [1]
    assert plan.records[0].status == "planned"


def test_plan_drops_every_non_read_service():
    plan = build_run_plan(
        clear_script(), parse_address_map({"17": "0x7E0"})
    )

    assert [record.service_id for record in plan.records] == [1]
    assert all(
        record.service_id in READ_ONLY_CATALOG_SERVICES
        for record in plan.records
    )


def test_plan_skips_request_without_request_bytes():
    script = parse_script_xml(
        """<script type="ReadDTCs"><content><nodes>
        <node class="Components.VehComm">
          <property name="Mode" value="EcuAddress" />
          <property name="EcuAddress" value="17" />
          <property name="ServiceId" value="1" />
        </node>
        </nodes></content></script>"""
    )
    plan = build_run_plan(script, parse_address_map({"0x11": "0x7E0"}))

    record = plan.records[0]
    assert record.request_bytes == b""
    assert record.status == "skipped_unsupported"
    assert "no request bytes" in record.detail


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
            make_record(14, b"\x14\xFF\x00", index=1),
            make_record(10, b"\x10\x03", index=2),
            make_record(1, b"\x19\x02\xFF", index=3),
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
            make_record(2, b"\x22\xF1\x90", index=1),
            make_record(1, b"\x19\x02\xFF", index=2),
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
            "diag-run-script",
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
            "diag-run-script",
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
        def __init__(self, rpc, uds_requests=(), allow_read_services=False):
            self.uds_requests = tuple(uds_requests)
            self.allow_read_services = allow_read_services

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
            "diag-run-script",
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
            "diag-run-script",
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
            "diag-run-script",
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
            "diag-run-script",
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


def test_cli_profile_only_plans_and_reports_name_matches(capsys):
    status = main(
        [
            "diag-run-script",
            "--script",
            str(SCRIPT_NAMED),
            "--profile",
            str(PROFILE_NAMED),
        ]
    )

    out = capsys.readouterr().out
    assert status == 0
    assert (
        "Name matches: 2 matched, 1 ambiguous, 1 missing, 0 unnamed "
        "(4 script ECUs)" in out
    )
    assert "ambiguous ecu_name GWM: profile names GWM, G.W.M" in out
    assert "no profile module named ABS (ecu_name)" in out
    assert "-> 0x7E0" in out
    assert "-> 0x720" in out
    assert "Unmapped logical addresses (2):" in out
    assert "Mode: plan only. No CAN frames were sent." in out


def test_cli_profile_only_json_has_no_address_map_path(capsys):
    status = main(
        [
            "diag-run-script",
            "--script",
            str(SCRIPT_NAMED),
            "--profile",
            str(PROFILE_NAMED),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["address_map"]["path"] is None
    assert payload["address_map"]["entries"] == []
    assert {entry["name"] for entry in payload["address_map"]["names"]} == {
        "ECM",
        "DIM",
    }
    assert payload["profile"]["path"] == str(PROFILE_NAMED)
    assert payload["profile"]["name_map"]["counts"]["ambiguous"] == 1


def test_cli_address_map_wins_over_a_profile_name_match(tmp_path, capsys):
    map_path = write_map(tmp_path, {"addresses": {"17": "0x7E1"}})

    status = main(
        [
            "diag-run-script",
            "--script",
            str(SCRIPT_NAMED),
            "--address-map",
            str(map_path),
            "--profile",
            str(PROFILE_NAMED),
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    first = payload["plan"]["records"][0]
    assert first["can_id"] == 0x7E1
    assert first["logical_kind"] == "ecu_address"
    names = {
        entry["name"]: entry["can_id"]
        for entry in payload["address_map"]["names"]
    }
    assert names == {"DIM": 0x720, "ECM": 0x7E0}


def test_cli_requires_an_address_map_or_a_profile(capsys):
    status = main(["diag-run-script", "--script", str(SCRIPT_XML)])

    captured = capsys.readouterr()
    assert status == 1
    assert "supply --address-map, --profile, or both" in captured.err
    assert "Traceback" not in captured.err


def test_cli_profile_with_missing_file_is_clear(tmp_path, capsys):
    missing = tmp_path / "missing-profile.json"

    status = main(
        [
            "diag-run-script",
            "--script",
            str(SCRIPT_NAMED),
            "--profile",
            str(missing),
        ]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "module profile not found: {}".format(missing) in captured.err
    assert "Traceback" not in captured.err


def test_cli_execute_with_profile_sends_only_planned_reads(monkeypatch, capsys):
    sent = []
    answered = {"done": False}

    class FakeBus:
        def __init__(self, rpc, uds_requests=(), allow_read_services=False):
            self.uds_requests = tuple(uds_requests)
            self.allow_read_services = allow_read_services

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
            "diag-run-script",
            "--script",
            str(SCRIPT_NAMED),
            "--profile",
            str(PROFILE_NAMED),
            "--execute",
            "--timeout",
            "0.02",
            "--json",
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert [record["status"] for record in payload["result"]["records"]] == [
        "response",
        "timeout",
        "skipped_unmapped",
        "skipped_unmapped",
    ]
    assert [frame.arbitration_id for frame in sent] == [0x7E0, 0x720]
    assert all(
        frame.data[1] in {0x01, 0x09, 0x19, 0x22} for frame in sent
    )


def test_parser_registers_diag_run_script(tmp_path):
    map_path = write_map(tmp_path, {"0x11": "0x7E0"})

    args = build_parser().parse_args(
        [
            "diag-run-script",
            "--script",
            str(SCRIPT_XML),
            "--address-map",
            str(map_path),
        ]
    )

    assert args.func is cmd_diag_run_script
    assert args.execute is False
    assert args.timeout == 1.5
    assert args.json is False
