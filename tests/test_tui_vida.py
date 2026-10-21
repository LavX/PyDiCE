import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from volvo_p3.diagnostics import build_report
from volvo_p3.tui import (
    VolvoTuiApp,
    build_vida_catalog_view,
    build_vida_report,
    build_vida_script_view,
    format_vida_dtc_lines,
    format_vida_fault_lines,
    format_vida_network_lines,
    vida_dtc_overview_lines,
    vida_fault_trace_lines,
    vida_network_status_lines,
)


FIXTURES = Path(__file__).with_name("fixtures")
SCAN = FIXTURES / "my-car-complete.json"
CATALOG = FIXTURES / "vida_catalog.json"
SCRIPT_XML = FIXTURES / "vida_script_read_dtc.xml"
SCRIPT_CLEAR = FIXTURES / "vida_script_clear_dtc.xml"
SCRIPT_MALFORMED = FIXTURES / "vida_script_malformed.xml"
MISSING = FIXTURES / "does-not-exist.json"


def scan_with_dtcs():
    """A small scan covering a responder, a partial responder and silence."""
    return {
        "name": "unit scan",
        "modules": [
            {
                "name": "ECM",
                "request_id": "0x7E0",
                "response_id": "0x7E8",
                "status": "ok",
                "dtcs": [
                    {"code": "P034211", "status_byte": 0x08},
                    {"code": "P010100", "status_byte": 0x01},
                    {"code": "U9999", "status_byte": 0x00},
                ],
            },
            {
                "name": "DIM",
                "request_id": "0x720",
                "response_id": "0x728",
                "status": "incomplete",
                "dtcs": [{"code": "P034211", "status_byte": 0x08}],
            },
            {
                "name": "CEM",
                "request_id": "0x726",
                "response_id": "0x72E",
                "status": "negative_response",
            },
            {
                "name": "TCM",
                "request_id": "0x7E1",
                "response_id": "0x7E9",
                "status": "no_response",
            },
        ],
    }


class VidaReportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.scan_path = Path(self.tempdir.name) / "scan.json"
        self.scan_path.write_text(
            json.dumps(scan_with_dtcs()), encoding="utf-8"
        )

    def test_build_report_reads_fixture_scan(self):
        report = build_vida_report(SCAN)

        self.assertEqual(report.name, "Discovered UDS modules including partial responders")
        self.assertEqual(report.module_count, 18)
        self.assertEqual(report.modules[0].name, "DIM")
        self.assertEqual(report.modules[0].request_id, 0x720)

    def test_build_report_merges_catalog_descriptions(self):
        report = build_vida_report(self.scan_path, CATALOG)

        self.assertEqual(report.module_count, 4)
        self.assertEqual(report.dtc_count, 4)
        self.assertEqual(report.described_dtc_count, 3)
        self.assertEqual(report.current_fault_count, 3)

        ecm_dtcs = report.modules[0].dtcs
        self.assertEqual(ecm_dtcs[0].code, "P034211")
        self.assertEqual(ecm_dtcs[0].description, "CMP sensor A circuit low")
        self.assertEqual(ecm_dtcs[1].description, "MAF A circuit range/performance")
        self.assertIsNone(ecm_dtcs[2].description)

        dim_dtc = report.modules[1].dtcs[0]
        self.assertEqual(
            dim_dtc.description, "Camshaft Position Sensor A Circuit Low"
        )

    def test_build_report_missing_scan_raises(self):
        with self.assertRaises(FileNotFoundError) as caught:
            build_vida_report(MISSING)

        self.assertIn("scan report not found", str(caught.exception))

    def test_build_report_missing_catalog_raises(self):
        with self.assertRaises(FileNotFoundError) as caught:
            build_vida_report(SCAN, MISSING)

        self.assertIn("catalog not found", str(caught.exception))

    def test_dtc_lines_show_module_ids_and_counts(self):
        report = build_vida_report(SCAN)

        lines = format_vida_dtc_lines(report)

        self.assertTrue(lines[0].startswith("VIDA DTC overview:"))
        self.assertIn(
            "modules: 18 | DTC records: 0 | described: 0 | current faults: 0",
            lines,
        )
        self.assertIn(
            "DIM  request=0x720  response=0x728  status=unknown  dtcs=0",
            lines,
        )

    def test_dtc_lines_use_catalog_text_and_leave_unknown_bare(self):
        report = build_vida_report(
            self.scan_path, CATALOG, {"vin": "yv1dz8256c2000000"}
        )

        lines = format_vida_dtc_lines(report)

        self.assertIn("vin: YV1DZ8256C2000000", lines)
        self.assertIn(
            "modules: 4 | DTC records: 4 | described: 3 | current faults: 3",
            lines,
        )
        self.assertIn(
            "  P034211  confirmed  CMP sensor A circuit low", lines
        )
        self.assertIn(
            "  P034211  confirmed  Camshaft Position Sensor A Circuit Low", lines
        )
        self.assertIn(
            "  P010100  active  MAF A circuit range/performance", lines
        )
        self.assertIn("  U9999  unknown", lines)
        self.assertNotIn(
            "  U9999  unknown  ",
            "\n".join(lines),
        )

    def test_build_report_rejects_malformed_json(self):
        bad = Path(self.tempdir.name) / "bad.json"
        bad.write_text("{not json", encoding="utf-8")

        with self.assertRaises(ValueError) as caught:
            build_vida_report(bad)

        self.assertIn("not valid JSON", str(caught.exception))

    def test_missing_scan_returns_error_line(self):
        wrappers = (
            vida_dtc_overview_lines,
            vida_network_status_lines,
            vida_fault_trace_lines,
        )
        for wrapper in wrappers:
            with self.subTest(wrapper=wrapper.__name__):
                lines = wrapper(MISSING)

                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("error:"))
                self.assertIn("scan report not found", lines[0])

        self.assertEqual(
            vida_dtc_overview_lines(""),
            ["error: enter a scan report JSON path"],
        )

    def test_missing_catalog_returns_error_line(self):
        lines = vida_dtc_overview_lines(SCAN, MISSING)

        self.assertEqual(len(lines), 1)
        self.assertIn("catalog not found", lines[0])

    def test_catalog_view_reports_sections(self):
        lines = build_vida_catalog_view(CATALOG)

        self.assertIn("source: synthetic", lines)
        self.assertIn("dtc descriptions: 5 (4 codes)", lines)
        self.assertIn("timing entries: 4", lines)
        self.assertIn("profiles: 1", lines)
        self.assertIn("scripts: 1", lines)

    def test_catalog_view_handles_missing_and_blank_paths(self):
        missing = build_vida_catalog_view(MISSING)
        blank = build_vida_catalog_view("  ")

        self.assertEqual(len(missing), 1)
        self.assertIn("catalog not found", missing[0])
        self.assertEqual(blank, ["error: enter a VIDA catalog JSON path"])

    def test_network_status_counts_buckets(self):
        report = build_report(scan_with_dtcs())

        lines = format_vida_network_lines(report)

        self.assertIn("total modules: 4", lines)
        self.assertIn("responders: 3 (75%)", lines)
        self.assertIn("no response: TCM", lines)
        self.assertIn("incomplete: DIM", lines)
        self.assertIn("negative response: CEM", lines)
        self.assertIn("unclassified: (none)", lines)
        self.assertTrue(lines[-1].startswith("coverage:"))

    def test_network_status_lists_unclassified_fixture_modules(self):
        lines = vida_network_status_lines(SCAN)
        text = "\n".join(lines)

        self.assertIn("total modules: 18", lines)
        self.assertIn("responders: 0 (0%)", lines)
        self.assertIn("unclassified: DIM, CEM", text)

    def test_fault_trace_groups_by_module_and_state(self):
        report = build_vida_report(self.scan_path, CATALOG)

        lines = format_vida_fault_lines(report)

        self.assertIn("current faults: 3 | groups: 4", lines)
        self.assertIn("ECM  active  (1)", lines)
        self.assertIn("ECM  confirmed  (1)", lines)
        self.assertIn("ECM  unknown  (1)", lines)
        self.assertIn("DIM  confirmed  (1)", lines)
        self.assertIn(
            "  P010100  active  MAF A circuit range/performance", lines
        )
        self.assertIn("  U9999  unknown", lines)

    def test_fault_trace_without_dtcs(self):
        report = build_vida_report(SCAN)

        lines = format_vida_fault_lines(report)

        self.assertIn("current faults: 0 | groups: 0", lines)
        self.assertIn("no DTC records in this report", lines)


class VidaScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def test_script_plan_xml(self):
        lines = build_vida_script_view(SCRIPT_XML)

        self.assertIn("docno: VCC-227144", lines)
        self.assertIn("title: Read DTCs", lines)
        self.assertIn("type: ReadDTCs", lines)
        self.assertIn("variant: 227144", lines)
        self.assertIn("ECUs: 2", lines)
        self.assertIn(
            "  ecu=0x11 type=- tester=0xF1 mode=EcuAddress "
            "services=0x19(read_only), 0x22(read_only)",
            lines,
        )
        self.assertIn("read-only plan: 3 requests", lines)
        self.assertIn(
            "  ecu=0x11 service=0x19 read_dtc_information request=19 02 FF",
            lines,
        )
        self.assertIn(
            "  ecu=0x11 service=0x22 read_data_by_identifier request=22 F1 90",
            lines,
        )
        self.assertIn("DTC list: list=DtcList value=Dtc.value text=Dtc.text", lines)
        self.assertIn("plan only: nothing was sent to a vehicle", lines)
        self.assertNotIn("dropped services:", "\n".join(lines))

    def test_script_plan_zip(self):
        archive_path = Path(self.tempdir.name) / "script.zip"
        with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("VCC-227144", SCRIPT_XML.read_text(encoding="utf-8"))

        lines = build_vida_script_view(archive_path)

        self.assertIn("docno: VCC-227144", lines)
        self.assertIn("read-only plan: 3 requests", lines)
        self.assertIn("DTC list: list=DtcList value=Dtc.value text=Dtc.text", lines)

    def test_script_plan_drops_non_read_services(self):
        lines = build_vida_script_view(SCRIPT_CLEAR)

        self.assertIn("type: ClearDtc", lines)
        dropped = [line for line in lines if line.startswith("dropped services:")]
        self.assertEqual(len(dropped), 1)
        self.assertIn("0x14(clear_diagnostic_information)", dropped[0])
        self.assertIn("0x27(security_access)", dropped[0])

        planned = [line for line in lines if " request=" in line]
        self.assertEqual(len(planned), 1)
        self.assertIn("service=0x19", planned[0])
        self.assertNotIn("service=0x14", planned[0])

    def test_script_plan_handles_missing_and_malformed_paths(self):
        missing = build_vida_script_view(MISSING)
        malformed = build_vida_script_view(SCRIPT_MALFORMED)
        blank = build_vida_script_view("")

        self.assertEqual(len(missing), 1)
        self.assertIn("script not found", missing[0])
        self.assertEqual(len(malformed), 1)
        self.assertTrue(malformed[0].startswith("error:"))
        self.assertIn("malformed", malformed[0].lower())
        self.assertEqual(blank, ["error: enter a VIDA script ZIP or XML path"])


class VidaMenuTests(unittest.TestCase):
    def test_menu_exposes_vida_actions(self):
        app = VolvoTuiApp()

        titles = [section.title for section in app.sections]
        self.assertEqual(
            titles,
            ["Connection", "Vehicle Profile", "Native DiCE", "Diagnostics", "VIDA"],
        )
        vida = app.sections[-1]
        self.assertEqual(
            [action.name for action in vida.actions],
            [
                "Load VIDA Catalog",
                "VIDA DTC Overview",
                "VIDA Fault Trace",
                "VIDA Network Status",
                "VIDA Script Plan",
            ],
        )
        self.assertEqual(
            [field.name for field in vida.actions[0].fields], ["vida_catalog"]
        )
        self.assertEqual(
            [field.name for field in vida.actions[1].fields],
            ["vida_scan", "vida_catalog"],
        )

    def test_actions_return_error_lines_instead_of_raising(self):
        app = VolvoTuiApp()
        missing = str(MISSING)

        cases = (
            (app._vida_load_catalog, {"vida_catalog": missing}),
            (app._vida_dtc_overview, {"vida_scan": missing, "vida_catalog": ""}),
            (app._vida_fault_trace, {"vida_scan": missing, "vida_catalog": ""}),
            (app._vida_network_status, {"vida_scan": missing}),
            (app._vida_script_plan, {"vida_script": missing}),
        )
        for handler, values in cases:
            with self.subTest(handler=handler.__name__):
                lines = handler(values)

                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("error:"))


if __name__ == "__main__":
    unittest.main()
