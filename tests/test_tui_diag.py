import json
import os
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

from volvo_p3.diagnostics import build_report
from volvo_p3.tui import (
    DEFAULT_DIAG_CATALOG,
    NativeScanResult,
    VolvoTuiApp,
    build_diag_catalog_view,
    build_diag_report,
    build_live_diag_lines,
    build_live_scan_options,
    build_script_plan_view,
    default_live_diag_profile_path,
    format_diag_dtc_lines,
    format_diag_fault_lines,
    format_diag_network_lines,
    diag_dtc_overview_lines,
    diag_fault_trace_lines,
    diag_network_status_lines,
)


FIXTURES = Path(__file__).with_name("fixtures")
SCAN = FIXTURES / "my-car-complete.json"
CATALOG = FIXTURES / "diag_catalog.json"
COMPRESSED_CATALOG = FIXTURES / "diag_catalog.json.gz"
SCRIPT_XML = FIXTURES / "script_plan_read_dtc.xml"
SCRIPT_CLEAR = FIXTURES / "script_plan_clear_dtc.xml"
SCRIPT_MALFORMED = FIXTURES / "script_plan_malformed.xml"
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


class DiagReportTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.scan_path = Path(self.tempdir.name) / "scan.json"
        self.scan_path.write_text(
            json.dumps(scan_with_dtcs()), encoding="utf-8"
        )

    def test_build_report_reads_fixture_scan(self):
        report = build_diag_report(SCAN)

        self.assertEqual(report.name, "Discovered UDS modules including partial responders")
        self.assertEqual(report.module_count, 18)
        self.assertEqual(report.modules[0].name, "DIM")
        self.assertEqual(report.modules[0].request_id, 0x720)

    def test_build_report_merges_catalog_descriptions(self):
        report = build_diag_report(self.scan_path, CATALOG)

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
            build_diag_report(MISSING)

        self.assertIn("scan report not found", str(caught.exception))

    def test_build_report_missing_catalog_raises(self):
        with self.assertRaises(FileNotFoundError) as caught:
            build_diag_report(SCAN, MISSING)

        self.assertIn("catalog not found", str(caught.exception))

    def test_dtc_lines_show_module_ids_and_counts(self):
        report = build_diag_report(SCAN)

        lines = format_diag_dtc_lines(report)

        self.assertTrue(lines[0].startswith("Diagnostic DTC overview:"))
        self.assertIn(
            "modules: 18 | DTC records: 0 | described: 0 | current faults: 0",
            lines,
        )
        self.assertIn(
            "DIM  request=0x720  response=0x728  status=unknown  dtcs=0",
            lines,
        )

    def test_dtc_lines_use_catalog_text_and_leave_unknown_bare(self):
        report = build_diag_report(
            self.scan_path, CATALOG, {"vin": "yv1dz8256c2000000"}
        )

        lines = format_diag_dtc_lines(report)

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
            build_diag_report(bad)

        self.assertIn("not valid JSON", str(caught.exception))

    def test_missing_scan_returns_error_line(self):
        wrappers = (
            diag_dtc_overview_lines,
            diag_network_status_lines,
            diag_fault_trace_lines,
        )
        for wrapper in wrappers:
            with self.subTest(wrapper=wrapper.__name__):
                lines = wrapper(MISSING)

                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("error:"))
                self.assertIn("scan report not found", lines[0])

        self.assertEqual(
            diag_dtc_overview_lines(""),
            ["error: enter a scan report JSON path"],
        )

    def test_missing_catalog_returns_error_line(self):
        lines = diag_dtc_overview_lines(SCAN, MISSING)

        self.assertEqual(len(lines), 1)
        self.assertIn("catalog not found", lines[0])

    def test_catalog_view_reports_sections(self):
        lines = build_diag_catalog_view(CATALOG)

        self.assertIn("source: synthetic", lines)
        self.assertIn("dtc descriptions: 5 (4 codes)", lines)
        self.assertIn("timing entries: 4", lines)
        self.assertIn("profiles: 1", lines)
        self.assertIn("scripts: 1", lines)

    def test_catalog_view_accepts_a_compressed_catalog(self):
        lines = build_diag_catalog_view(COMPRESSED_CATALOG)

        self.assertIn("source: synthetic", lines)
        self.assertIn("dtc descriptions: 5 (4 codes)", lines)

    def test_default_catalog_path_is_compressed(self):
        self.assertEqual(DEFAULT_DIAG_CATALOG, "data/diag_catalog.json.xz")

    def test_catalog_view_handles_missing_and_blank_paths(self):
        missing = build_diag_catalog_view(MISSING)
        blank = build_diag_catalog_view("  ")

        self.assertEqual(len(missing), 1)
        self.assertIn("catalog not found", missing[0])
        self.assertEqual(blank, ["error: enter a service catalog JSON path"])

    def test_network_status_counts_buckets(self):
        report = build_report(scan_with_dtcs())

        lines = format_diag_network_lines(report)

        self.assertIn("total modules: 4", lines)
        self.assertIn("responders: 3 (75%)", lines)
        self.assertIn("no response: TCM", lines)
        self.assertIn("incomplete: DIM", lines)
        self.assertIn("negative response: CEM", lines)
        self.assertIn("unclassified: (none)", lines)
        self.assertTrue(lines[-1].startswith("coverage:"))

    def test_network_status_lists_unclassified_fixture_modules(self):
        lines = diag_network_status_lines(SCAN)
        text = "\n".join(lines)

        self.assertIn("total modules: 18", lines)
        self.assertIn("responders: 0 (0%)", lines)
        self.assertIn("unclassified: DIM, CEM", text)

    def test_fault_trace_groups_by_module_and_state(self):
        report = build_diag_report(self.scan_path, CATALOG)

        lines = format_diag_fault_lines(report)

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
        report = build_diag_report(SCAN)

        lines = format_diag_fault_lines(report)

        self.assertIn("current faults: 0 | groups: 0", lines)
        self.assertIn("no DTC records in this report", lines)


class DiagnosticScriptTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)

    def test_script_plan_xml(self):
        lines = build_script_plan_view(SCRIPT_XML)

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

        lines = build_script_plan_view(archive_path)

        self.assertIn("docno: VCC-227144", lines)
        self.assertIn("read-only plan: 3 requests", lines)
        self.assertIn("DTC list: list=DtcList value=Dtc.value text=Dtc.text", lines)

    def test_script_plan_drops_non_read_services(self):
        lines = build_script_plan_view(SCRIPT_CLEAR)

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
        missing = build_script_plan_view(MISSING)
        malformed = build_script_plan_view(SCRIPT_MALFORMED)
        blank = build_script_plan_view("")

        self.assertEqual(len(missing), 1)
        self.assertIn("script not found", missing[0])
        self.assertEqual(len(malformed), 1)
        self.assertTrue(malformed[0].startswith("error:"))
        self.assertIn("malformed", malformed[0].lower())
        self.assertEqual(blank, ["error: enter a diagnostic script ZIP or XML path"])


class LiveDiagReadTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.scan_path = self.root / "scan.json"
        self.scan_path.write_text(
            json.dumps(scan_with_dtcs()), encoding="utf-8"
        )

    def _fake_scan(self, calls, report_path=None):
        report = self.scan_path if report_path is None else report_path

        def fake(options, *, profile_path=None):
            calls.append((list(options), profile_path))
            return NativeScanResult(
                report, profile_path, ("Saved scan: {}".format(report),)
            )

        return fake

    def test_live_scan_options_use_catalog_and_read_only_profile(self):
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"modules": []}), encoding="utf-8")

        options = build_live_scan_options(CATALOG, "2.5", profile)

        self.assertEqual(
            options,
            [
                "--timeout",
                "2.5",
                "--diag-catalog",
                str(CATALOG),
                "--profile",
                str(profile),
            ],
        )
        self.assertNotIn("--save-profile", options)

        plain = build_live_scan_options(CATALOG, "1.5", "")
        self.assertNotIn("--profile", plain)

    def test_live_scan_options_reject_bad_input(self):
        with self.assertRaises(FileNotFoundError) as caught:
            build_live_scan_options(MISSING, "1.5")
        self.assertIn("catalog not found", str(caught.exception))

        with self.assertRaises(FileNotFoundError) as caught:
            build_live_scan_options(CATALOG, "1.5", MISSING)
        self.assertIn("profile not found", str(caught.exception))

        for timeout in ("0", "-1", "nan", "inf", "soon"):
            with self.subTest(timeout=timeout):
                with self.assertRaises(ValueError):
                    build_live_scan_options(CATALOG, timeout)

        with self.assertRaises(ValueError) as caught:
            build_live_scan_options("  ", "1.5")
        self.assertIn("enter a service catalog JSON path", str(caught.exception))

    def test_live_read_returns_counts_faults_and_network_lines(self):
        app = VolvoTuiApp()
        calls = []

        with mock.patch.object(
            app, "_run_native_scan_result", self._fake_scan(calls)
        ):
            lines = app._diag_live_read(
                {
                    "live_catalog": str(CATALOG),
                    "live_timeout": "1.5",
                    "live_profile": "",
                }
            )

        self.assertEqual(len(calls), 1)
        options, profile_path = calls[0]
        self.assertIn("--diag-catalog", options)
        self.assertIn(str(CATALOG), options)
        self.assertIn("--timeout", options)
        self.assertNotIn("--save-profile", options)
        self.assertIsNone(profile_path)

        self.assertEqual(lines[0], "Saved scan: {}".format(self.scan_path))
        text = "\n".join(lines)
        self.assertIn("Live DTC read: unit scan", lines)
        self.assertIn(
            "modules: 4 | DTC records: 4 | described: 3 | current faults: 3",
            lines,
        )
        self.assertIn("Diagnostic fault trace:", text)
        self.assertIn(
            "  P010100  active  MAF A circuit range/performance", lines
        )
        self.assertIn("Diagnostic network status:", text)
        self.assertIn("total modules: 4", lines)
        self.assertIn("no response: TCM", lines)

    def test_live_read_passes_saved_profile_without_writing_it(self):
        app = VolvoTuiApp()
        calls = []
        profile = self.root / "profile.json"
        profile.write_text(json.dumps({"modules": []}), encoding="utf-8")

        with mock.patch.object(
            app, "_run_native_scan_result", self._fake_scan(calls)
        ):
            app._diag_live_read(
                {
                    "live_catalog": str(CATALOG),
                    "live_timeout": "1.5",
                    "live_profile": str(profile),
                }
            )

        options, _ = calls[0]
        self.assertEqual(options[-2:], ["--profile", str(profile)])
        self.assertNotIn("--save-profile", options)

    def test_live_read_reports_missing_catalog_without_scanning(self):
        app = VolvoTuiApp()

        def fail(*_args, **_kwargs):
            raise AssertionError("the scan must not start")

        with mock.patch.object(app, "_run_native_scan_result", fail):
            lines = app._diag_live_read(
                {
                    "live_catalog": str(MISSING),
                    "live_timeout": "1.5",
                    "live_profile": "",
                }
            )

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("error:"))
        self.assertIn("catalog not found", lines[0])

    def test_live_read_turns_scan_failure_into_error_line(self):
        app = VolvoTuiApp()

        def fail(options, *, profile_path=None):
            raise RuntimeError("native scan exited with code 1")

        with mock.patch.object(app, "_run_native_scan_result", fail):
            lines = app._diag_live_read(
                {
                    "live_catalog": str(CATALOG),
                    "live_timeout": "1.5",
                    "live_profile": "",
                }
            )

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("error:"))
        self.assertIn("live DTC read failed", lines[0])
        self.assertIn("code 1", lines[0])

    def test_live_read_reports_a_missing_saved_report(self):
        app = VolvoTuiApp()
        calls = []

        with mock.patch.object(
            app,
            "_run_native_scan_result",
            self._fake_scan(calls, report_path=MISSING),
        ):
            lines = app._diag_live_read(
                {
                    "live_catalog": str(CATALOG),
                    "live_timeout": "1.5",
                    "live_profile": "",
                }
            )

        text = "\n".join(lines)
        self.assertIn("Saved scan:", lines[0])
        self.assertIn("scan report not found", text)

    def test_live_lines_handle_missing_saved_report(self):
        lines = build_live_diag_lines(MISSING, CATALOG)

        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0].startswith("error:"))
        self.assertIn("scan report not found", lines[0])

    def test_native_scan_result_exposes_the_saved_report_path(self):
        app = VolvoTuiApp()
        original = Path.cwd()
        self.addCleanup(os.chdir, original)
        os.chdir(self.root)
        seen = []

        def fake_cli(argv):
            seen.append(list(argv))
            output = Path(argv[argv.index("--output") + 1])
            output.write_text(
                json.dumps(scan_with_dtcs()), encoding="utf-8"
            )
            print("Report: {}".format(output))
            return 0

        with mock.patch("volvo_p3.cli.main", fake_cli):
            result = app._run_native_scan_result(
                ["--timeout", "1.5", "--diag-catalog", str(CATALOG)]
            )

        self.assertEqual(seen[0][0], "dice-scan")
        self.assertIn("--diag-catalog", seen[0])
        self.assertIn(str(CATALOG), seen[0])
        self.assertNotIn("--save-profile", seen[0])
        self.assertEqual(result.report_path.parent, self.root / "reports")
        self.assertTrue(result.report_path.is_file())
        self.assertIn(
            "Saved scan: {}".format(result.report_path), result.lines
        )
        self.assertTrue(
            any("Report:" in line for line in app.logs)
        )

    def test_native_scan_wrapper_still_returns_lines(self):
        app = VolvoTuiApp()
        result = NativeScanResult(self.scan_path, None, ("Saved scan: x",))

        with mock.patch.object(
            app,
            "_run_native_scan_result",
            lambda options, *, profile_path=None: result,
        ):
            lines = app._run_native_scan(["--timeout", "1.5"])

        self.assertEqual(lines, ["Saved scan: x"])

    def test_default_live_profile_path_needs_an_existing_file(self):
        original = Path.cwd()
        tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(tempdir.cleanup)
        self.addCleanup(os.chdir, original)

        os.chdir(tempdir.name)
        self.assertEqual(default_live_diag_profile_path(), "")

        reports = Path(tempdir.name) / "reports"
        reports.mkdir()
        (reports / "my-car-complete.json").write_text("{}", encoding="utf-8")
        self.assertEqual(
            default_live_diag_profile_path(), "reports/my-car-complete.json"
        )


class DiagMenuTests(unittest.TestCase):
    def test_menu_exposes_diag_actions(self):
        app = VolvoTuiApp()

        titles = [section.title for section in app.sections]
        self.assertEqual(
            titles,
            ["Connection", "Vehicle Profile", "Native DiCE", "Diagnostics", "Service Data"],
        )
        diag = app.sections[-1]
        self.assertEqual(
            [action.name for action in diag.actions],
            [
                "Load Catalog",
                "Live DTC Read",
                "DTC Overview",
                "Fault Trace",
                "Network Status",
                "Script Plan",
            ],
        )
        self.assertEqual(
            [field.name for field in diag.actions[0].fields], ["diag_catalog"]
        )
        self.assertEqual(
            [field.name for field in diag.actions[1].fields],
            ["live_catalog", "live_timeout", "live_profile"],
        )
        self.assertEqual(
            [field.name for field in diag.actions[2].fields],
            ["diag_scan", "diag_catalog"],
        )

    def test_actions_return_error_lines_instead_of_raising(self):
        app = VolvoTuiApp()
        missing = str(MISSING)

        cases = (
            (app._diag_load_catalog, {"diag_catalog": missing}),
            (app._diag_dtc_overview, {"diag_scan": missing, "diag_catalog": ""}),
            (app._diag_fault_trace, {"diag_scan": missing, "diag_catalog": ""}),
            (app._diag_network_status, {"diag_scan": missing}),
            (app._script_plan_plan, {"script_plan": missing}),
        )
        for handler, values in cases:
            with self.subTest(handler=handler.__name__):
                lines = handler(values)

                self.assertEqual(len(lines), 1)
                self.assertTrue(lines[0].startswith("error:"))


if __name__ == "__main__":
    unittest.main()
