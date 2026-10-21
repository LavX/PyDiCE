"""Tests for the read-only VIDA commands in ``volvo_p3.cli``."""

import io
import json
import zipfile
from argparse import Namespace
from pathlib import Path

from volvo_p3.cli import (
    _vida_report_section,
    build_parser,
    cmd_vida_catalog,
    cmd_vida_report,
    cmd_vida_script,
    main,
)


FIXTURES = Path(__file__).with_name("fixtures")
CATALOG = FIXTURES / "vida_catalog.json"
SCRIPT_XML = FIXTURES / "vida_script_read_dtc.xml"
SCRIPT_CLEAR = FIXTURES / "vida_script_clear_dtc.xml"
SCRIPT_MALFORMED = FIXTURES / "vida_script_malformed.xml"
SCAN_PROFILE = FIXTURES / "my-car-complete.json"


def dtc_record(code, status_byte, failure_type):
    """Build one DTC record shaped like ``uds_read.py`` output."""
    return {
        "code": code,
        "failure_type_byte": failure_type,
        "failure_type_hex": "0x{:02X}".format(failure_type),
        "status_byte": status_byte,
        "status_hex": "0x{:02X}".format(status_byte),
        "effective_status_byte": status_byte,
        "effective_status_hex": "0x{:02X}".format(status_byte),
    }


def test_vida_report_section_for_live_scan_shape():
    scan = json.loads(SCAN_PROFILE.read_text(encoding="utf-8"))
    section = _vida_report_section(scan, CATALOG)

    assert section["counts"]["modules"] == 18
    assert section["counts"]["dtcs"] == 0
    assert section["network"]["responder_count"] == 0


def scan_with_dtcs():
    scan = json.loads(SCAN_PROFILE.read_text(encoding="utf-8"))
    modules = {module["name"]: module for module in scan["modules"]}
    modules["ECM"].update(
        status="ok",
        dtc_count=2,
        dtcs=[
            dtc_record("P0342", 0x09, 0x11),
            dtc_record("P0101", 0x08, 0x00),
        ],
    )
    modules["DIM"].update(
        status="ok",
        dtc_count=1,
        dtcs=[dtc_record("P0342", 0x28, 0x11)],
    )
    modules["CEM"].update(
        status="negative_response",
        dtc_count=1,
        dtcs=[dtc_record("U9999", 0x04, 0x00)],
        negative_response={"code": "0x31"},
    )
    return scan


def write_scan(tmp_path, scan):
    path = tmp_path / "scan.json"
    path.write_text(json.dumps(scan), encoding="utf-8")
    return path


def catalog_args(**overrides):
    values = {"catalog": CATALOG, "code": None, "json": False}
    values.update(overrides)
    return Namespace(**values)


def report_args(scan, **overrides):
    values = {
        "scan": scan,
        "catalog": None,
        "vehicle": None,
        "view": "all",
        "json": False,
    }
    values.update(overrides)
    return Namespace(**values)


def script_args(script, **overrides):
    values = {"script": script, "json": False}
    values.update(overrides)
    return Namespace(**values)


# vida-catalog -------------------------------------------------------------


def test_catalog_human_shows_source_and_counts(capsys):
    status = cmd_vida_catalog(catalog_args())

    out = capsys.readouterr().out
    assert status == 0
    assert "Schema: pydice.vida_catalog version 1" in out
    assert "Source kind: synthetic" in out
    assert "Language: en-US" in out
    assert "Databases: 1" in out
    assert "CarComRT" in out
    assert "DTC descriptions: 5" in out
    assert "Timing rows: 4" in out
    assert "Profiles: 1" in out
    assert "Scripts: 1" in out


def test_catalog_code_lookup_prints_catalog_text(capsys):
    status = cmd_vida_catalog(catalog_args(code="P034211"))

    out = capsys.readouterr().out
    assert status == 0
    assert "DTC P034211:" in out
    assert "ECU: global" in out
    assert "Camshaft Position Sensor A Circuit Low (Bank 1 or single sensor)" in out
    assert "Rough running after a cold start" in out
    assert "synthetic T150/T191" in out


def test_catalog_json_includes_summary_and_lookup(capsys):
    status = cmd_vida_catalog(catalog_args(code="P034211", json=True))

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert list(payload) == sorted(payload)
    assert payload["schema"] == "pydice.vida_catalog"
    assert payload["version"] == 1
    assert payload["counts"] == {
        "dtc_descriptions": 5,
        "timing": 4,
        "profiles": 1,
        "scripts": 1,
    }
    assert payload["lookup"]["code"] == "P034211"
    assert payload["lookup"]["found"] is True
    description = payload["lookup"]["description"]
    assert description["long_text"].startswith("Camshaft Position Sensor A Circuit Low")
    assert description["symptoms"] == [
        "Rough running after a cold start",
        "Reduced engine power",
    ]


def test_catalog_missing_dtc_returns_nonzero(capsys):
    status = cmd_vida_catalog(catalog_args(code="P999999"))

    captured = capsys.readouterr()
    assert status == 1
    assert "no catalog description for P999999" in captured.err


def test_catalog_missing_dtc_json_reports_not_found(capsys):
    status = cmd_vida_catalog(catalog_args(code="P999999", json=True))

    payload = json.loads(capsys.readouterr().out)
    assert status == 1
    assert payload["lookup"] == {
        "code": "P999999",
        "found": False,
        "description": None,
    }


def test_catalog_missing_file_returns_nonzero_without_traceback(tmp_path, capsys):
    missing = tmp_path / "missing-catalog.json"

    status = main(["vida-catalog", "--catalog", str(missing)])

    captured = capsys.readouterr()
    assert status == 1
    assert "catalog not found: {}".format(missing) in captured.err
    assert "Traceback" not in captured.err


# vida-report --------------------------------------------------------------


def test_report_profile_network_view(capsys):
    status = cmd_vida_report(
        report_args(SCAN_PROFILE, view="network")
    )

    out = capsys.readouterr().out
    assert status == 0
    assert "Scan: Discovered UDS modules including partial responders" in out
    assert "Network: 0/18 responders (0%)" in out
    assert "other (18): DIM, CEM" in out
    assert "Silence does not establish module absence" in out


def test_report_dtcs_view_merges_catalog_descriptions(tmp_path, capsys):
    scan = write_scan(tmp_path, scan_with_dtcs())

    status = cmd_vida_report(
        report_args(scan, catalog=CATALOG, view="dtcs")
    )

    out = capsys.readouterr().out
    assert status == 0
    assert "DTCs (4):" in out
    assert "P0342-11" in out
    assert "ECM reports camshaft position sensor A circuit low" in out
    assert "Mass or Volume Air Flow A Circuit Range/Performance" in out
    assert "U9999-00" in out


def test_report_faults_view_groups_current_records(tmp_path, capsys):
    scan = write_scan(tmp_path, scan_with_dtcs())

    status = cmd_vida_report(
        report_args(scan, catalog=CATALOG, view="faults")
    )

    out = capsys.readouterr().out
    assert status == 0
    assert "Fault trace:" in out
    assert "DIM  confirmed (1)" in out
    assert "ECM  active (1)" in out
    assert "CEM  pending (1)" in out
    assert "status=0x09" in out
    assert "ECM reports camshaft position sensor A circuit low" in out


def test_report_all_human_includes_modules_network_and_faults(tmp_path, capsys):
    scan = write_scan(tmp_path, scan_with_dtcs())

    status = cmd_vida_report(report_args(scan, catalog=CATALOG, view="all"))

    out = capsys.readouterr().out
    assert status == 0
    assert "Modules: 18  DTCs: 4  Described: 3  Current faults: 3" in out
    assert "Modules (18):" in out
    assert "Network: 3/18 responders" in out
    assert "Fault trace:" in out
    assert "ECM  active (1)" in out


def test_report_json_all_is_stable_and_scopes_descriptions(tmp_path, capsys):
    scan = write_scan(tmp_path, scan_with_dtcs())
    vehicle = {"vin": "YV1TEST0000000000", "model_year": "2012"}

    status = cmd_vida_report(
        report_args(
            scan,
            catalog=CATALOG,
            vehicle=json.dumps(vehicle),
            view="all",
            json=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["view"] == "all"
    assert payload["vehicle"] == vehicle
    assert payload["counts"] == {
        "modules": 18,
        "dtcs": 4,
        "described_dtcs": 3,
        "current_faults": 3,
    }
    assert payload["network"]["responder_count"] == 3
    assert payload["network"]["negative_response"] == ["CEM"]

    modules = {module["name"]: module for module in payload["modules"]}
    assert len(modules) == 18
    ecm_descriptions = {
        dtc["code"]: dtc["description"] for dtc in modules["ECM"]["dtcs"]
    }
    assert ecm_descriptions["P0342"] == (
        "ECM reports camshaft position sensor A circuit low"
    )
    assert ecm_descriptions["P0101"] == (
        "Mass or Volume Air Flow A Circuit Range/Performance"
    )
    assert modules["CEM"]["dtcs"][0]["description"] is None

    groups = {
        (group["module"], group["status"]) for group in payload["fault_trace"]
    }
    assert ("ECM", "active") in groups
    assert ("DIM", "confirmed") in groups
    assert ("CEM", "pending") in groups


def test_report_vehicle_profile_can_be_a_file(tmp_path, capsys):
    scan = write_scan(tmp_path, scan_with_dtcs())
    vehicle_path = tmp_path / "vehicle.json"
    vehicle_path.write_text(
        json.dumps({"vin": "YV1FILE0000000000"}), encoding="utf-8"
    )

    status = cmd_vida_report(
        report_args(
            scan,
            catalog=CATALOG,
            vehicle=str(vehicle_path),
            view="all",
            json=True,
        )
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["vehicle"] == {"vin": "YV1FILE0000000000"}


def test_report_json_network_view_omits_other_sections(tmp_path, capsys):
    scan = write_scan(tmp_path, scan_with_dtcs())

    status = cmd_vida_report(
        report_args(scan, catalog=CATALOG, view="network", json=True)
    )

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["network"]["total_modules"] == 18
    assert "modules" not in payload
    assert "fault_trace" not in payload
    assert "dtcs" not in payload


def test_report_missing_scan_returns_nonzero_without_traceback(tmp_path, capsys):
    missing = tmp_path / "missing-scan.json"

    status = main(["vida-report", "--scan", str(missing)])

    captured = capsys.readouterr()
    assert status == 1
    assert "scan report not found: {}".format(missing) in captured.err
    assert "Traceback" not in captured.err


def test_report_missing_catalog_returns_nonzero(tmp_path, capsys):
    scan = write_scan(tmp_path, scan_with_dtcs())
    missing = tmp_path / "missing-catalog.json"

    status = main(
        ["vida-report", "--scan", str(scan), "--catalog", str(missing)]
    )

    captured = capsys.readouterr()
    assert status == 1
    assert "catalog not found: {}".format(missing) in captured.err


# vida-script --------------------------------------------------------------


def test_script_human_shows_header_plans_and_read_requests(capsys):
    status = cmd_vida_script(script_args(SCRIPT_XML))

    out = capsys.readouterr().out
    assert status == 0
    assert "Docno: VCC-227144" in out
    assert "Title: Read DTCs" in out
    assert "Language: en-US" in out
    assert "Type: ReadDTCs" in out
    assert "0b00c8af80206a61" in out
    assert "DTC plan:" in out
    assert "list: DtcList" in out
    assert "value: Dtc.value" in out
    assert "Read-only requests (3):" in out
    assert "request=19 02 FF" in out
    assert "request=22 F1 90" in out
    assert "Execution: none. This command only parses the script." in out


def test_script_json_reports_plan_and_never_executes(capsys):
    status = cmd_vida_script(script_args(SCRIPT_XML, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert payload["docno"] == "VCC-227144"
    assert payload["script_type"] == "ReadDTCs"
    assert payload["language"] == "en-US"
    assert payload["profiles"] == [
        "0b00c8af80206a61",
        "0b00c8af80206a68",
    ]
    assert len(payload["ecus"]) == 2
    assert payload["dtc_plan"]["list_path"] == "DtcList"
    assert payload["dtc_plan"]["value_path"] == "Dtc.value"
    assert payload["dtc_plan"]["text_path"] == "Dtc.text"
    assert [request["service_id"] for request in payload["read_only_requests"]] == [
        0x19,
        0x22,
        0x19,
    ]
    assert payload["blocked_services"] == []
    assert payload["executed"] is False


def test_script_safety_filter_drops_write_services(capsys):
    status = cmd_vida_script(script_args(SCRIPT_CLEAR, json=True))

    payload = json.loads(capsys.readouterr().out)
    assert status == 0
    assert [request["service_id"] for request in payload["read_only_requests"]] == [
        0x19
    ]
    assert payload["read_only_requests"][0]["service_name"] == "read_dtc_information"
    assert payload["read_only_requests"][0]["request"] == "19 02 FF"
    assert {item["service_id"] for item in payload["blocked_services"]} == {
        0x10,
        0x14,
        0x27,
        0x2E,
        0x31,
        0x3E,
    }
    assert payload["executed"] is False


def test_script_human_marks_blocked_services(capsys):
    status = cmd_vida_script(script_args(SCRIPT_CLEAR))

    out = capsys.readouterr().out
    assert status == 0
    assert "Read-only requests (1):" in out
    assert "Blocked services (6, never executed):" in out
    assert "0x14 clear_diagnostic_information" in out
    assert "0x27 security_access" in out


def test_script_accepts_zip_archive(tmp_path, capsys):
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.writestr(
            "VCC-227144", SCRIPT_XML.read_text(encoding="utf-8")
        )
    path = tmp_path / "script.zip"
    path.write_bytes(archive.getvalue())

    status = cmd_vida_script(script_args(path))

    out = capsys.readouterr().out
    assert status == 0
    assert "Docno: VCC-227144" in out
    assert "Read-only requests (3):" in out


def test_script_malformed_returns_nonzero_without_traceback(capsys):
    status = main(["vida-script", "--script", str(SCRIPT_MALFORMED)])

    captured = capsys.readouterr()
    assert status == 1
    assert "cannot parse script" in captured.err
    assert "malformed XML" in captured.err
    assert "Traceback" not in captured.err


def test_script_missing_file_returns_nonzero_without_traceback(tmp_path, capsys):
    missing = tmp_path / "missing-script.xml"

    status = main(["vida-script", "--script", str(missing)])

    captured = capsys.readouterr()
    assert status == 1
    assert "script not found: {}".format(missing) in captured.err


# Parser wiring ------------------------------------------------------------


def test_parser_registers_vida_commands():
    parser = build_parser()

    catalog = parser.parse_args(["vida-catalog", "--catalog", str(CATALOG)])
    assert catalog.func is cmd_vida_catalog
    assert catalog.code is None
    assert catalog.json is False

    report = parser.parse_args(["vida-report", "--scan", str(SCAN_PROFILE)])
    assert report.func is cmd_vida_report
    assert report.catalog is None
    assert report.vehicle is None
    assert report.view == "all"
    assert report.json is False

    script = parser.parse_args(["vida-script", "--script", str(SCRIPT_XML)])
    assert script.func is cmd_vida_script
    assert script.json is False
