import copy
import json
import unittest
from pathlib import Path

from volvo_p3.diagnostics import (
    DEFAULT_COVERAGE_NOTE,
    DiagnosticReport,
    build_report,
    fault_trace,
    network_status,
)


REPO_ROOT = Path(__file__).resolve().parents[1]
# Vehicle reports under reports/ are gitignored. Prefer the local capture when
# it exists; the fixture is the same small discovery profile for a clean tree.
PROFILE_PATHS = (
    REPO_ROOT / "reports" / "my-car-complete.json",
    REPO_ROOT / "tests" / "fixtures" / "my-car-complete.json",
)

STATUS_BIT_ORDER = (
    "testFailed",
    "testFailedThisOperationCycle",
    "pendingDTC",
    "confirmedDTC",
    "testNotCompletedSinceLastClear",
    "testFailedSinceLastClear",
    "testNotCompletedThisOperationCycle",
    "warningIndicatorRequested",
)

CATALOG = {
    "B1B69": {"description": "Brake control module, general failure"},
    "U0250": "Lost communication with body control module",
}


def load_profile():
    for path in PROFILE_PATHS:
        if path.exists():
            with path.open(encoding="utf-8") as handle:
                return json.load(handle)
    raise AssertionError("missing my-car-complete.json in reports/ and tests/fixtures/")


def uds_dtc(code, status_byte, failure_type=0):
    """Build a DTC record shaped like uds_read.py output."""
    bits = {
        name: bool(status_byte & (1 << index))
        for index, name in enumerate(STATUS_BIT_ORDER)
    }
    set_bits = [name for name in STATUS_BIT_ORDER if bits[name]]
    return {
        "code": code,
        "raw_dtc_hex": "00 00 00",
        "failure_type_byte": failure_type,
        "failure_type_hex": "0x{:02X}".format(failure_type),
        "status_byte": status_byte,
        "status_hex": "0x{:02X}".format(status_byte),
        "status_bits": bits,
        "set_status_bits": set_bits,
        "effective_status_byte": status_byte,
        "effective_status_hex": "0x{:02X}".format(status_byte),
        "effective_status_bits": dict(bits),
        "set_effective_status_bits": list(set_bits),
        "raw_record_hex": "00 00 {:02X} {:02X}".format(failure_type, status_byte),
    }


def profile_scan_with_dtcs():
    """Use the saved discovery profile and add records to two responders."""
    scan = load_profile()
    scan["modules"][0]["status"] = "ok"
    scan["modules"][0]["dtc_count"] = 1
    scan["modules"][0]["dtcs"] = [uds_dtc("B1B69", 0x28, failure_type=0x16)]

    scan["modules"][1]["status"] = "ok"
    scan["modules"][1]["dtc_count"] = 2
    scan["modules"][1]["dtcs"] = [
        uds_dtc("U0250", 0x50, failure_type=0x00),
        uds_dtc("U9999", 0x00, failure_type=0x00),
    ]
    return scan


class DiagnosticsTests(unittest.TestCase):
    def test_existing_profile_builds_modules_and_can_ids(self):
        scan = load_profile()

        report = build_report(scan)

        self.assertIsInstance(report, DiagnosticReport)
        self.assertEqual(report.name, scan["name"])
        self.assertEqual(report.module_count, 18)
        self.assertEqual(report.dtc_count, 0)
        self.assertEqual(report.modules[0].name, "DIM")
        self.assertEqual(report.modules[0].request_id, 0x720)
        self.assertEqual(report.modules[0].response_id, 0x728)
        self.assertEqual(report.modules[0].status, "unknown")
        self.assertEqual(report.modules[0].dtcs, ())

    def test_catalog_description_merges_only_when_code_matches(self):
        report = build_report(profile_scan_with_dtcs(), catalog=CATALOG)

        self.assertEqual(report.dtc_count, 3)
        dim_dtc = report.modules[0].dtcs[0]
        self.assertEqual(dim_dtc.code, "B1B69")
        self.assertEqual(
            dim_dtc.description, "Brake control module, general failure"
        )
        self.assertEqual(dim_dtc.description_source, "catalog")
        self.assertEqual(dim_dtc.raw["raw_record_hex"], "00 00 16 28")

        cem_dtcs = report.modules[1].dtcs
        self.assertEqual(
            cem_dtcs[0].description,
            "Lost communication with body control module",
        )
        self.assertIsNone(cem_dtcs[1].description)
        self.assertIsNone(cem_dtcs[1].description_source)

    def test_missing_or_self_named_catalog_entry_never_invents_text(self):
        scan = profile_scan_with_dtcs()
        catalog = {"U9999": "U9999", "U0250": {"name": ""}}

        report = build_report(scan, catalog=catalog)

        for module in report.modules:
            for dtc in module.dtcs:
                if dtc.code in ("U9999", "U0250"):
                    self.assertIsNone(dtc.description)
                    self.assertIsNone(dtc.description_source)

    def test_fault_trace_groups_by_module_and_separates_current_from_history(self):
        scan = {
            "coverage_note": "test coverage",
            "modules": [
                {
                    "module": "CEM",
                    "request_id": 0x726,
                    "response_id": 0x72E,
                    "status": "ok",
                    "dtc_count": 6,
                    "dtcs": [
                        uds_dtc("U3000", 0x2F),
                        uds_dtc("B1000", 0x08),
                        uds_dtc("B1001", 0x04),
                        uds_dtc("B1002", 0x20),
                        uds_dtc("B1003", 0x10),
                        uds_dtc("B1004", 0x00),
                    ],
                },
                {
                    "module": "DIM",
                    "request_id": 0x720,
                    "response_id": 0x728,
                    "status": "ok",
                    "dtc_count": 1,
                    "dtcs": [uds_dtc("B1B69", 0x20, failure_type=0x16)],
                },
            ],
        }

        report = build_report(scan)
        groups = fault_trace(report)

        self.assertEqual(
            [(group.module, group.status, group.count) for group in groups],
            [
                ("CEM", "active", 1),
                ("CEM", "confirmed", 1),
                ("CEM", "pending", 1),
                ("CEM", "history", 1),
                ("CEM", "incomplete", 1),
                ("CEM", "unknown", 1),
                ("DIM", "history", 1),
            ],
        )
        active = groups[0]
        self.assertTrue(active.is_current)
        self.assertTrue(active.dtcs[0].is_active)
        self.assertTrue(active.dtcs[0].is_confirmed)
        self.assertEqual(
            active.dtcs[0].set_effective_status_bits,
            (
                "testFailed",
                "testFailedThisOperationCycle",
                "pendingDTC",
                "confirmedDTC",
                "testFailedSinceLastClear",
            ),
        )
        self.assertTrue(groups[1].is_current)
        self.assertFalse(groups[2].is_current)
        self.assertFalse(groups[3].is_current)
        self.assertEqual(groups[6].dtcs[0].code, "B1B69")

    def test_network_status_summarizes_response_buckets(self):
        scan = {
            "coverage_note": "custom coverage note",
            "modules": [
                {"module": "DIM", "status": "ok", "dtcs": []},
                {"module": "ECM", "status": "no_response", "dtcs": None},
                {"module": "CEM", "status": "incomplete", "dtcs": None},
                {
                    "module": "TCM",
                    "status": "negative_response",
                    "dtcs": None,
                    "negative_response": {
                        "nrc": 0x12,
                        "name": "subFunctionNotSupported",
                    },
                },
                {"module": "BECM", "status": "error", "dtcs": None},
            ],
        }

        report = build_report(scan)
        status = network_status(report)

        self.assertEqual(status.total_modules, 5)
        self.assertEqual(status.responders, ("DIM", "CEM", "TCM"))
        self.assertEqual(status.no_response, ("ECM",))
        self.assertEqual(status.incomplete, ("CEM",))
        self.assertEqual(status.negative_response, ("TCM",))
        self.assertEqual(status.other, ("BECM",))
        self.assertEqual(status.responder_count, 3)
        self.assertAlmostEqual(status.coverage_ratio, 3 / 5)
        self.assertEqual(status.coverage_note, "custom coverage note")
        self.assertEqual(
            status.to_dict()["negative_response"], ["TCM"]
        )
        self.assertEqual(
            report.modules[3].negative_response["name"],
            "subFunctionNotSupported",
        )

    def test_default_coverage_note_when_scan_has_none(self):
        status = network_status(build_report({"modules": []}))

        self.assertEqual(status.coverage_note, DEFAULT_COVERAGE_NOTE)
        self.assertEqual(status.coverage_ratio, 0.0)

    def test_json_round_trip_is_stable_and_json_serializable(self):
        scan = profile_scan_with_dtcs()
        vehicle = {"vin": "YV1FW97C0F1000000", "model_year": 2014}
        report = build_report(scan, catalog=CATALOG, vehicle=vehicle)

        payload = report.to_dict()
        text = report.to_json()

        self.assertEqual(json.loads(text), payload)
        self.assertEqual(report.to_json(), text)
        json.dumps(payload)
        self.assertEqual(payload["vehicle"], vehicle)
        self.assertEqual(
            payload["counts"],
            {
                "modules": 18,
                "dtcs": 3,
                "described_dtcs": 2,
                "current_faults": 1,
            },
        )
        self.assertEqual(payload["modules"][0]["name"], "DIM")
        self.assertEqual(
            payload["modules"][0]["dtcs"][0]["raw"]["raw_record_hex"],
            "00 00 16 28",
        )
        self.assertEqual(
            payload["fault_trace"][0]["dtcs"][0]["code"], "B1B69"
        )
        self.assertNotIn("raw", payload["fault_trace"][0]["dtcs"][0])

    def test_build_does_not_mutate_the_scan(self):
        scan = profile_scan_with_dtcs()
        before = copy.deepcopy(scan)

        build_report(scan, catalog=CATALOG)

        self.assertEqual(scan, before)

    def test_scan_wrapper_is_accepted(self):
        scan = profile_scan_with_dtcs()

        report = build_report({"scan": scan})

        self.assertEqual(report.module_count, 18)
        self.assertEqual(report.dtc_count, 3)


if __name__ == "__main__":
    unittest.main()
