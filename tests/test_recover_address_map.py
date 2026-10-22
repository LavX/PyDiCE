"""Tests for the logical-address recovery tool."""

import io
import json
import struct
import tempfile
import unittest
import zipfile
import zlib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from tools import recover_address_map as recover

from volvo_p3.script_runner import parse_address_map


FIXTURES = Path(__file__).with_name("fixtures")
PROFILE = FIXTURES / "my-car-complete.json"


def profile_document(modules):
    return {"name": "synthetic profile", "modules": modules}


def module(name, request_id, response_id=None):
    item = {"name": name, "request_id": request_id}
    if response_id is not None:
        item["response_id"] = response_id
    return item


def vehcomm(*, address=None, ecu_type=None, service_ids=(0x19,), nodeid="AE01"):
    properties = []
    if address is not None:
        properties.append(
            '<property name="Mode" type="Internal" value="EcuAddress"/>'
        )
        properties.append(
            '<property name="EcuAddress" type="In" value="{}"/>'.format(address)
        )
    if ecu_type is not None:
        properties.append(
            '<property name="Mode" type="Internal" value="EcuType"/>'
        )
        properties.append(
            '<property name="EcuType" type="In" value="{}"/>'.format(ecu_type)
        )
    for service_id in service_ids:
        properties.append(
            '<property name="ServiceId" type="In" value="{:02X}"/>'.format(
                service_id
            )
        )
    return '<node class="Components.VehComm" nodeid="{}">{}</node>'.format(
        nodeid, "".join(properties)
    )


def script_xml(title, nodes, docno="VCC-000001", language="en-US"):
    return (
        '<script docno="{}" type="ReadDTCs" xml:lang="{}">'
        "<title>{}</title><content><nodes>{}</nodes></content></script>"
    ).format(docno, language, title, "".join(nodes))


def write_text(tmp_path, name, text):
    path = Path(tmp_path) / name
    path.write_text(text, encoding="utf-8")
    return str(path)


def write_profile(tmp_path, modules, name="profile.json"):
    return write_text(tmp_path, name, json.dumps(profile_document(modules)))


def write_script_zip(tmp_path, text, name="script.zip", entry="script"):
    path = Path(tmp_path) / name
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(entry, text)
    path.write_bytes(buffer.getvalue())
    return str(path)


def write_image(tmp_path, bodies, name="image.MDF"):
    """Write a fake database image containing raw ZIP local headers."""

    path = Path(tmp_path) / name
    payload = bytearray(b"MDFPREFIX" * 8)
    for body in bodies:
        name_bytes = b"script"
        compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
        compressed = compressor.compress(body) + compressor.flush()
        header = struct.pack(
            "<IHHHHHIIIHH",
            0x04034B50,
            20,
            0,
            8,
            0,
            0,
            zlib.crc32(body),
            len(compressed),
            len(body),
            len(name_bytes),
            0,
        )
        payload += header + name_bytes + compressed
        payload += b"\x00" * 16
    path.write_bytes(bytes(payload))
    return str(path)


class ProfileSelectionTests(unittest.TestCase):
    def test_unique_selection_maps_only_single_candidate_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_profile(
                tmp,
                [
                    module("SRS", "0x7C8", "0x7D0"),
                    module("SAS", "0x7C9", "0x7D1"),
                    module("ZZZ", "0x7CA", "0x7D2"),
                ],
            )
            document = recover.build_document(recover.load_profile(path), ())

        self.assertEqual(
            document["addresses"],
            {"884701": "0x7C8", "645902": "0x7C9"},
        )
        self.assertEqual(
            [entry["name"] for entry in document["entries"]], ["SAS", "SRS"]
        )
        self.assertTrue(
            all(entry["confidence"] == "medium" for entry in document["entries"])
        )
        self.assertTrue(
            all(entry["source"] for entry in document["entries"])
        )
        self.assertEqual(document["unmapped"][0]["name"], "ZZZ")

    def test_fixture_profile_does_not_guess_ambiguous_names(self):
        document = recover.build_document(recover.load_profile(str(PROFILE)), ())

        self.assertEqual(document["addresses"], {})
        by_name = {item["name"]: item for item in document["unmapped"]}
        self.assertEqual(
            [item["logical_value"] for item in by_name["DIM"]["candidates"]],
            [381101, 372302],
        )
        self.assertIn("ambiguous", by_name["DIM"]["reason"])
        self.assertEqual(by_name["BECM"]["candidates"], [])
        self.assertIn("no script title", by_name["BECM"]["reason"])
        self.assertNotIn("CAN_730", document["addresses"])

    def test_dominant_selection_marks_candidates_low_confidence(self):
        document = recover.build_document(
            recover.load_profile(str(PROFILE)), (), selection="dominant"
        )

        self.assertEqual(
            document["addresses"],
            {
                "381101": "0x720",
                "372302": "0x726",
                "284101": "0x7E0",
                "437601": "0x7E1",
            },
        )
        self.assertTrue(
            all(entry["confidence"] == "low" for entry in document["entries"])
        )
        self.assertTrue(
            any(
                "selected by policy" in note
                for entry in document["entries"]
                for note in entry["notes"]
            )
        )

    def test_all_selection_maps_every_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = write_profile(tmp, [module("DIM", "0x720", "0x728")])
            document = recover.build_document(
                recover.load_profile(profile_path), (), selection="all"
            )

        self.assertEqual(
            document["addresses"],
            {"381101": "0x720", "372302": "0x720"},
        )
        self.assertEqual(len(document["entries"]), 2)
        self.assertTrue(
            all(entry["confidence"] == "low" for entry in document["entries"])
        )

    def test_placeholder_name_is_never_mapped(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = write_profile(tmp, [module("CAN_7E0", "0x7E5", "0x7ED")])
            script_path = write_text(
                tmp,
                "script.xml",
                script_xml("Read DTCs, CAN_7E0", [vehcomm(ecu_type=999999)]),
            )
            document = recover.build_document(
                recover.load_profile(profile_path),
                [(script_path, recover.load_script(script_path))],
            )

        self.assertEqual(document["addresses"], {})
        self.assertEqual(document["unmapped"][0]["name"], "CAN_7E0")
        self.assertIn("placeholder", document["unmapped"][0]["reason"])

    def test_duplicate_names_and_ids_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            duplicate_name = write_profile(
                tmp, [module("ECM", "0x7E0"), module("ecm", "0x7E1")]
            )
            duplicate_id = write_profile(
                tmp, [module("ECM", "0x7E0"), module("TCM", "0x7E0")]
            )
            with self.assertRaises(recover.AddressMapError):
                recover.load_profile(duplicate_name)
            with self.assertRaises(recover.AddressMapError):
                recover.load_profile(duplicate_id)

    def test_bad_profile_shapes_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cases = {
                "missing.json": "{}",
                "modules.json": '{"modules": {}}',
                "empty.json": '{"modules": []}',
                "name.json": '{"modules": [{"request_id": "0x7E0"}]}',
                "request.json": '{"modules": [{"name": "ECM", "request_id": "0x7DF"}]}',
                "range.json": '{"modules": [{"name": "ECM", "request_id": "0x600"}]}',
                "json.json": "{not json",
            }
            for name, text in cases.items():
                path = write_text(tmp, name, text)
                with self.subTest(name=name):
                    with self.assertRaises(recover.AddressMapError):
                        recover.load_profile(path)


class ScriptEvidenceTests(unittest.TestCase):
    def test_script_alone_lists_logical_addresses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text(
                tmp,
                "script.xml",
                script_xml(
                    "Read DTCs",
                    [
                        vehcomm(address=17, service_ids=(0x19, 0x22)),
                        vehcomm(ecu_type=884701, service_ids=(0x22,)),
                    ],
                ),
            )
            document = recover.build_document(None, [(path, recover.load_script(path))])

        self.assertEqual(document["addresses"], {})
        logical = document["script"]["scripts"][0]["logical_addresses"]
        self.assertEqual(
            [(item["ecu_address"], item["ecu_type"]) for item in logical],
            [(17, None), (None, 884701)],
        )
        self.assertEqual(logical[0]["service_ids"], [0x19, 0x22])
        self.assertEqual(logical[1]["name_candidates"], ["SRS"])

    def test_script_title_and_type_derive_a_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = write_profile(tmp, [module("ZZZ", "0x7E5", "0x7ED")])
            script_path = write_text(
                tmp,
                "script.xml",
                script_xml("Read DTCs, ZZZ", [vehcomm(ecu_type=999999)]),
            )
            document = recover.build_document(
                recover.load_profile(profile_path),
                [(script_path, recover.load_script(script_path))],
            )

        self.assertEqual(document["addresses"], {"999999": "0x7E5"})
        entry = document["entries"][0]
        self.assertEqual(entry["name"], "ZZZ")
        self.assertEqual(entry["logical_kind"], "ecu_type")
        self.assertEqual(entry["confidence"], "medium")
        self.assertIn("VCC-000001", entry["source"])

    def test_script_title_with_two_types_is_ambiguous(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = write_profile(tmp, [module("ZZZ", "0x7E5", "0x7ED")])
            script_path = write_text(
                tmp,
                "script.xml",
                script_xml(
                    "Read DTCs, ZZZ",
                    [
                        vehcomm(ecu_type=999999),
                        vehcomm(ecu_type=999998, nodeid="AE02"),
                    ],
                ),
            )
            profile = recover.load_profile(profile_path)
            script = recover.load_script(script_path)
            unique = recover.build_document(profile, [(script_path, script)])
            dominant = recover.build_document(
                profile, [(script_path, script)], selection="dominant"
            )

        self.assertEqual(unique["addresses"], {})
        self.assertEqual(dominant["addresses"], {})
        self.assertIn("ambiguous", unique["unmapped"][0]["reason"])
        self.assertEqual(len(unique["unmapped"][0]["candidates"]), 2)

    def test_address_is_preferred_when_a_node_has_both(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = write_profile(tmp, [module("ZZZ", "0x7E5", "0x7ED")])
            script_path = write_text(
                tmp,
                "script.xml",
                script_xml(
                    "Read DTCs, ZZZ",
                    [vehcomm(address=17, ecu_type=999999)],
                ),
            )
            document = recover.build_document(
                recover.load_profile(profile_path),
                [(script_path, recover.load_script(script_path))],
            )

        self.assertEqual(document["addresses"], {"0x11": "0x7E5"})
        self.assertEqual(document["entries"][0]["logical_kind"], "ecu_address")

    def test_conflicting_logical_keys_drop_both_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile_path = write_profile(
                tmp,
                [module("AAA", "0x7E5", "0x7ED"), module("BBB", "0x7E6", "0x7EE")],
            )
            a_path = write_text(
                tmp,
                "aaa.xml",
                script_xml("Read DTCs, AAA", [vehcomm(ecu_type=999999)]),
            )
            b_path = write_text(
                tmp,
                "bbb.xml",
                script_xml(
                    "Read DTCs, BBB",
                    [vehcomm(ecu_type=999999)],
                    docno="VCC-000002",
                ),
            )
            document = recover.build_document(
                recover.load_profile(profile_path),
                [
                    (a_path, recover.load_script(a_path)),
                    (b_path, recover.load_script(b_path)),
                ],
            )

        self.assertEqual(document["addresses"], {})
        self.assertEqual(
            sorted(item["name"] for item in document["unmapped"]), ["AAA", "BBB"]
        )
        self.assertTrue(
            any("dropping both" in warning for warning in document["warnings"])
        )

    def test_zip_and_xml_inputs_are_equivalent(self):
        text = script_xml("Read DTCs, ZZZ", [vehcomm(ecu_type=999999)])
        with tempfile.TemporaryDirectory() as tmp:
            xml_path = write_text(tmp, "script.xml", text)
            zip_path = write_script_zip(tmp, text)
            from_xml = recover.load_script(xml_path)
            from_zip = recover.load_script(zip_path)
            self.assertEqual(from_xml.docno, from_zip.docno)
            self.assertEqual(from_xml.title, from_zip.title)
            self.assertEqual(from_xml.ecus, from_zip.ecus)

    def test_bad_script_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text(tmp, "bad.xml", "<not-script/>")
            with self.assertRaises(recover.AddressMapError):
                recover.load_script(path)

    def test_script_runner_accepts_the_output(self):
        document = recover.build_document(
            recover.load_profile(str(PROFILE)), (), selection="dominant"
        )
        address_map = parse_address_map(document)
        match = address_map.resolve(None, 381101)
        self.assertTrue(match.mapped)
        self.assertEqual(match.can_id, 0x720)
        self.assertEqual(match.logical_kind, "ecu_type")

    def test_address_keys_use_runner_number_syntax(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_path = write_text(
                tmp,
                "script.xml",
                script_xml("Read DTCs, ZZZ", [vehcomm(ecu_type=999999)]),
            )
            profile_path = write_profile(tmp, [module("ZZZ", "0x7E5", "0x7ED")])
            document = recover.build_document(
                recover.load_profile(profile_path),
                [(script_path, recover.load_script(script_path))],
            )
        self.assertEqual(list(document["addresses"]), ["999999"])
        self.assertEqual(document["entries"][0]["address_key"], "999999")


class ImageFragmentTests(unittest.TestCase):
    def test_scanner_records_addresses_from_inflated_bodies(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_image(
                tmp,
                [
                    script_xml(
                        "Read DTC View With Status, P1, DV6",
                        [vehcomm(address=17, ecu_type=381101)],
                        docno="VCC-242880",
                    ).encode("utf-8"),
                    b"<not-a-script/>",
                    b"plain text, not XML",
                ],
            )
            scan = recover.scan_image_fragments(path)

        self.assertEqual(scan["stats"]["headers"], 3)
        self.assertEqual(scan["stats"]["inflated_xml"], 2)
        self.assertEqual(scan["stats"]["with_ecu_address"], 1)
        self.assertEqual(scan["stats"]["with_mode_ecu_address"], 1)
        self.assertEqual(scan["stats"]["crc_ok"], 2)
        self.assertEqual(scan["stats"]["crc_mismatch"], 0)
        self.assertEqual(scan["stats"]["not_xml"], 1)
        self.assertEqual(scan["address_counts"], [{"value": "17", "occurrences": 1}])
        pairs = {(item["ecu_address"], item["ecu_type"]) for item in scan["pairs"]}
        self.assertIn(("17", "381101"), pairs)
        document = scan["documents"][0]
        self.assertEqual(document["docno"], "VCC-242880")
        self.assertEqual(
            document["title"], "Read DTC View With Status, P1, DV6"
        )
        self.assertEqual(document["ecu_addresses"], ["17"])
        self.assertTrue(document["mode_ecu_address"])
        self.assertTrue(document["crc_ok"])

    def test_scanner_ignores_bodies_without_an_address(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_image(
                tmp,
                [script_xml("Read DTCs", [vehcomm(ecu_type=381101)]).encode("utf-8")],
            )
            scan = recover.scan_image_fragments(path)

        self.assertEqual(scan["stats"]["inflated_xml"], 1)
        self.assertEqual(scan["stats"]["with_ecu_address"], 0)
        self.assertEqual(scan["documents"], [])

    def test_scanner_rejects_a_missing_image(self):
        with self.assertRaises(recover.AddressMapError):
            recover.scan_image_fragments("/nonexistent/image.MDF")


class CommandLineTests(unittest.TestCase):
    def run_cli(self, argv):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = recover.main(argv)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_cli_prints_json_to_stdout(self):
        code, stdout, _ = self.run_cli(
            ["--profile", str(PROFILE), "--selection", "dominant"]
        )
        self.assertEqual(code, 0)
        document = json.loads(stdout)
        self.assertEqual(document["schema"], recover.SCHEMA_NAME)
        self.assertIn("381101", document["addresses"])

    def test_cli_writes_json_to_a_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "map.json"
            code, stdout, _ = self.run_cli(
                ["--profile", str(PROFILE), "--out", str(out)]
            )
            self.assertEqual(code, 0)
            self.assertEqual(stdout, "")
            document = json.loads(out.read_text(encoding="utf-8"))
        self.assertEqual(document["profile"]["module_count"], 18)

    def test_cli_rejects_missing_inputs(self):
        with self.assertRaises(SystemExit) as raised:
            self.run_cli([])
        self.assertEqual(raised.exception.code, 2)

    def test_cli_reports_bad_input_without_a_traceback(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_text(tmp, "bad.json", "{")
            code, stdout, stderr = self.run_cli(["--profile", path])
        self.assertEqual(code, 1)
        self.assertEqual(stdout, "")
        self.assertIn("error:", stderr)
        self.assertNotIn("Traceback", stderr)

    def test_cli_scan_image_alone_reports_fragment_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            image_path = write_image(
                tmp,
                [
                    script_xml(
                        "Read DTC View With Status, P1, DV6",
                        [vehcomm(address=17)],
                        docno="VCC-242880",
                    ).encode("utf-8")
                ],
            )
            code, stdout, stderr = self.run_cli(["--scan-image", image_path])
        self.assertEqual(code, 0, stderr)
        document = json.loads(stdout)
        self.assertEqual(document["addresses"], {})
        self.assertEqual(document["fragment_scan"][0]["stats"]["with_ecu_address"], 1)

    def test_cli_output_has_no_vendor_branding(self):
        code, stdout, _ = self.run_cli(
            ["--profile", str(PROFILE), "--selection", "dominant"]
        )
        self.assertEqual(code, 0)
        for word in ("VIDA", "Volvo"):
            self.assertNotIn(word, stdout)


if __name__ == "__main__":
    unittest.main()
