"""Tests for the diagnostic catalog exporter helpers and schema v2."""

import io
import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools import export_diag_catalog as exporter

from volvo_p3.diag_catalog import (
    DiagnosticCatalog,
    DtcDescription,
    FreezeFrameEntry,
    CatalogSource,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    ScriptEcu,
    ScriptEntry,
    SymptomEntry,
    VehicleProfile,
    catalog_from_dict,
    catalog_to_dict,
    dump_catalog,
    load_catalog,
    sanitize_text,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures")
EXPORTER = REPOSITORY_ROOT / "tools" / "export_diag_catalog.py"
V1_FIXTURE = FIXTURES / "diag_catalog.json"
SCRIPT_XML = FIXTURES / "script_plan_read_dtc.xml"


def component_record(row_id, cid, values, fixed_length=0x14):
    """Build one BaseData component record in the observed layout."""

    count_offset = 23 if fixed_length == 0x14 else 19
    offsets_start = count_offset + 2
    data_start = offsets_start + 2 * len(values)
    encoded = [value.encode("ascii") for value in values]
    ends = []
    position = data_start
    for value in encoded:
        position += len(value)
        ends.append(position)
    record = bytearray(data_start)
    record[0] = 0x30
    record[2:4] = fixed_length.to_bytes(2, "little")
    record[4:8] = row_id.to_bytes(4, "little")
    record[8:12] = cid.to_bytes(4, "little")
    record[20] = 0x04
    record[count_offset : count_offset + 2] = len(values).to_bytes(2, "little")
    for index, end in enumerate(ends):
        record[offsets_start + 2 * index : offsets_start + 2 * index + 2] = (
            end.to_bytes(2, "little")
        )
    for value in encoded:
        record.extend(value)
    return bytes(record)


class CatalogV2Tests(unittest.TestCase):
    def test_version_one_catalog_still_loads(self):
        catalog = load_catalog(V1_FIXTURE)
        self.assertEqual(catalog.source.kind, "synthetic")
        self.assertEqual(catalog.counts()["symptom_definitions"], 0)
        self.assertEqual(catalog.counts()["freeze_frames"], 0)
        self.assertEqual(SCHEMA_VERSION, 2)
        self.assertEqual(catalog_to_dict(catalog)["version"], 2)

    def test_round_trip_extended_sections(self):
        document = {
            "schema": SCHEMA_NAME,
            "version": 2,
            "source": {"kind": "test"},
            "dtc_descriptions": [],
            "timing": [],
            "profiles": [
                {
                    "id": "0b00c8af80206a61",
                    "title": "S40 (-04) 1999 B4164S",
                    "market": "EUR",
                    "model": "S40 (-04)",
                    "model_year": "1999",
                    "engine": "B4164S",
                    "transmission": "M56",
                    "steering": "L.H.D",
                    "body_style": "4DRS S.R",
                    "special_vehicle": "Disabled",
                    "chassis_from": "1000",
                    "chassis_to": "2000",
                    "markets": ["EUR", "NOR"],
                }
            ],
            "scripts": [
                {
                    "id": "VCC-227144",
                    "name": "Read DTCs",
                    "script_type": 10,
                    "script_type_name": "ReadDTCs",
                    "language": "en-US",
                    "profiles": ["0b00c8af80206a61"],
                    "ecus": [
                        {
                            "ecu_address": 17,
                            "ecu_type": 831701,
                            "service_ids": [5, 0x19],
                        }
                    ],
                    "read_only_service_ids": [0x19],
                    "status": "crc-verified",
                    "body_sha256": "00" * 32,
                }
            ],
            "symptom_definitions": [
                {
                    "id": "42",
                    "text": "Rough idle",
                    "category": "Engine",
                    "symptom_type": "D",
                    "causes": ["Vacuum leak"],
                    "dtc_codes": ["P034211"],
                    "ecu_variants": [51],
                }
            ],
            "freeze_frames": [
                {
                    "parameter": "Intake air temperature",
                    "value": "20",
                    "block_id": 123,
                    "confirmed": False,
                }
            ],
        }
        catalog = catalog_from_dict(document)
        self.assertEqual(len(catalog.profiles), 1)
        self.assertEqual(catalog.profiles[0].markets, ("EUR", "NOR"))
        self.assertEqual(catalog.profiles[0].component_summary(), (
            "S40 (-04), 1999, B4164S, M56, L.H.D, 4DRS S.R, Disabled"
        ))
        self.assertEqual(catalog.scripts[0].ecus[0].service_ids, (5, 0x19))
        self.assertEqual(catalog.symptom_definitions[0].causes, ("Vacuum leak",))
        self.assertFalse(catalog.freeze_frames[0].confirmed)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            dump_catalog(catalog, path)
            reloaded = load_catalog(path)
        self.assertEqual(reloaded.profiles, catalog.profiles)
        self.assertEqual(reloaded.scripts, catalog.scripts)
        self.assertEqual(reloaded.symptom_definitions, catalog.symptom_definitions)
        self.assertEqual(reloaded.freeze_frames, catalog.freeze_frames)

    def test_lookups_cover_profiles_and_script_variants(self):
        catalog = DiagnosticCatalog(
            source=CatalogSource(kind="test"),
            profiles=(VehicleProfile(id="0b00c8af80206a61", title="S40"),),
            scripts=(
                ScriptEntry(id="VCC-1", language="en-US"),
                ScriptEntry(id="VCC-1", language="de-DE"),
            ),
        )
        self.assertEqual(catalog.lookup_profile("0B00C8AF80206A61").title, "S40")
        self.assertIsNone(catalog.lookup_profile("missing"))
        self.assertEqual(len(catalog.lookup_scripts("vcc-1")), 2)

    def test_sanitize_text_keeps_a_placeholder_for_damage(self):
        self.assertEqual(sanitize_text("a\x00b\ufffdc"), "a b c")
        self.assertEqual(sanitize_text("  one   two  "), "one two")
        self.assertIsNone(sanitize_text(None))

    def test_catalog_rejects_unknown_version(self):
        document = json.loads(V1_FIXTURE.read_text(encoding="utf-8"))
        document["version"] = 99
        with self.assertRaises(Exception):
            catalog_from_dict(document)


class ExporterHelperTests(unittest.TestCase):
    def test_component_record_parser_single_and_double(self):
        single = component_record(1001, 184, ["S80 (-06)"])
        self.assertEqual(
            exporter.parse_component_record(single),
            (1001, 184, ("S80 (-06)",)),
        )
        double = component_record(1002, 240, ["240", "image.jpg"])
        self.assertEqual(
            exporter.parse_component_record(double),
            (1002, 240, ("240", "image.jpg")),
        )

    def test_profile_record_parser(self):
        title = "S40, ch 1000-2000"
        record = bytearray(120)
        record[0] = 0x30
        record[2:4] = (0x56).to_bytes(2, "little")
        record[4:8] = (1001).to_bytes(4, "little")
        record[8:12] = (1185).to_bytes(4, "little")
        record[12:28] = b"0b00c8af80206a61"
        record[30:34] = (1000).to_bytes(4, "little")
        record[34:38] = (2000).to_bytes(4, "little")
        record[42:44] = (1002).to_bytes(2, "little")
        record[46:48] = (1001).to_bytes(2, "little")
        record[50:52] = (1001).to_bytes(2, "little")
        record[58:60] = (1005).to_bytes(2, "little")
        record[70:72] = (1002).to_bytes(2, "little")
        record[74:76] = (1001).to_bytes(2, "little")
        record[101 : 101 + len(title)] = title.encode("ascii")
        record[95:97] = (101 + len(title)).to_bytes(2, "little")
        parsed = exporter.parse_profile_record(bytes(record))
        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["id"], "0b00c8af80206a61")
        self.assertEqual(parsed["chassis_from"], 1000)
        self.assertEqual(parsed["chassis_to"], 2000)
        self.assertEqual(parsed["engine"], 1005)

    def test_carve_scripts_reads_zip_and_marks_crc(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("VCC-227144", SCRIPT_XML.read_text(encoding="utf-8"))
        payload = b"junk" + archive.getvalue() + b"trailing"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repository.bin"
            path.write_bytes(payload)
            scripts, stats = exporter.carve_scripts(path)
        self.assertEqual(stats["headers"], 1)
        self.assertEqual(stats["crc_ok"], 1)
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0]["id"], "VCC-227144")
        self.assertEqual(scripts[0]["status"], "crc-verified")
        self.assertIn("0b00c8af80206a61", scripts[0]["profiles"])

    def test_carve_scripts_marks_crc_mismatch_but_keeps_xml(self):
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
            handle.writestr("VCC-227144", SCRIPT_XML.read_text(encoding="utf-8"))
        payload = bytearray(archive.getvalue())
        # Flip one byte of the stored CRC in the local header.
        payload[14] ^= 0xFF
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "repository.bin"
            path.write_bytes(bytes(payload))
            scripts, _stats = exporter.carve_scripts(path)
        self.assertEqual(len(scripts), 1)
        self.assertEqual(scripts[0]["status"], "crc-mismatch")

    def test_single_bit_text_repair(self):
        texts = ["Fuel volume control valve, mpen circuit"] * 5
        texts.extend(["open circuit"] * 60)
        repaired, repairs = exporter.repair_single_bit_text(texts)
        self.assertIn("open circuit", repaired[0])
        self.assertIn(("mpen", "open"), repairs)

    def test_single_bit_text_repair_leaves_valid_words(self):
        texts = ["Wide open throttle"] * 2 + ["Side"] * 60
        repaired, repairs = exporter.repair_single_bit_text(texts)
        self.assertEqual(repaired[0], "Wide open throttle")
        self.assertEqual(repairs, [])


class DumpModeTests(unittest.TestCase):
    def test_dump_dir_writes_catalog_and_raw_summaries(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = Path(directory) / "dump"
            result = subprocess.run(
                [
                    sys.executable,
                    str(EXPORTER),
                    "--from-json",
                    str(V1_FIXTURE),
                    "--dump-dir",
                    str(dump),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((dump / "catalog.json").is_file())
            self.assertTrue((dump / "raw" / "base_data.json").is_file())
            self.assertTrue((dump / "raw" / "carcom_tables.json").is_file())
            self.assertTrue((dump / "raw" / "scripts.json").is_file())
            catalog = load_catalog(dump / "catalog.json")
            self.assertEqual(catalog.counts()["dtc_descriptions"], 5)
            raw = json.loads(
                (dump / "raw" / "base_data.json").read_text(encoding="utf-8")
            )
            self.assertIn("VehicleProfile", raw)

    def test_out_still_works_without_dump_dir(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalog.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(EXPORTER),
                    "--from-json",
                    str(V1_FIXTURE),
                    "--out",
                    str(target),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            catalog = load_catalog(target)
            self.assertEqual(catalog.counts()["profiles"], 1)


if __name__ == "__main__":
    unittest.main()
