import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from volvo_p3.diag_catalog import (
    CatalogError,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    catalog_from_dict,
    catalog_to_dict,
    dump_catalog,
    load_catalog,
    normalize_dtc_code,
    normalize_request_bytes,
)


FIXTURE = Path(__file__).parent / "fixtures" / "diag_catalog.json"
COMPRESSED_FIXTURE = Path(__file__).parent / "fixtures" / "diag_catalog.json.gz"
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
EXPORTER = REPOSITORY_ROOT / "tools" / "export_diag_catalog.py"


class DiagnosticCatalogTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.catalog = load_catalog(FIXTURE)
        with FIXTURE.open(encoding="utf-8") as handle:
            cls.document = json.load(handle)

    def test_loads_sections_and_source(self):
        self.assertEqual(self.catalog.source.kind, "synthetic")
        self.assertEqual(self.catalog.source.language, "en-US")
        self.assertEqual(len(self.catalog.source.databases), 1)
        self.assertEqual(len(self.catalog.dtc_descriptions), 5)
        self.assertEqual(len(self.catalog.timing), 4)
        self.assertEqual(len(self.catalog.profiles), 1)
        self.assertEqual(len(self.catalog.scripts), 1)

    def test_lookup_global_by_code(self):
        entry = self.catalog.lookup_dtc("P034211")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.ecu, None)
        self.assertIn("Camshaft Position Sensor A Circuit Low", entry.long_text)
        self.assertEqual(entry.short_text, "Camshaft Position Sensor A Circuit Low")

    def test_lookup_prefers_ecu_then_falls_back_to_global(self):
        ecu_entry = self.catalog.lookup_dtc("P034211", ecu="ECM")
        self.assertIsNotNone(ecu_entry)
        self.assertEqual(ecu_entry.ecu, "ECM")
        self.assertIn("ECM reports", ecu_entry.long_text)

        fallback = self.catalog.lookup_dtc("P034211", ecu="TCM")
        self.assertIsNotNone(fallback)
        self.assertIsNone(fallback.ecu)
        self.assertIn("Bank 1 or single sensor", fallback.long_text)

    def test_lookup_normalizes_hex_code(self):
        entry = self.catalog.lookup_dtc("0x034211")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.code, "P034211")

    def test_lookup_by_five_character_code_uses_exact_entry(self):
        entry = self.catalog.lookup_dtc("P0342")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.code, "P0342")
        self.assertEqual(entry.short_text, "CMP sensor A circuit")

    def test_ambiguous_prefix_returns_none(self):
        # P0101 matches P010100 and P010101, which have different text.
        self.assertIsNone(self.catalog.lookup_dtc("P0101"))
        self.assertIsNotNone(self.catalog.lookup_dtc("P010100"))
        self.assertIsNotNone(self.catalog.lookup_dtc("P010101"))

    def test_describe_returns_diag_style_record(self):
        entry = self.catalog.describe_dtc("P034211")
        self.assertIsNotNone(entry)
        self.assertTrue(entry.long_text)
        self.assertTrue(entry.short_text)
        self.assertEqual(len(entry.symptoms), 2)
        self.assertEqual(len(entry.causes), 2)
        self.assertEqual(entry.language, "en-US")
        self.assertEqual(entry.source, "synthetic T150/T191")

    def test_missing_or_invalid_code_returns_none(self):
        for code in ("P999999", "P000000", "", "not-a-code", None, 12345):
            with self.subTest(code=code):
                self.assertIsNone(self.catalog.lookup_dtc(code))
                self.assertIsNone(self.catalog.describe_dtc(code))

    def test_timing_lookup_is_prefix_specific(self):
        exact = self.catalog.lookup_timing(329, "AE1B")
        self.assertIsNotNone(exact)
        self.assertEqual(exact.request, (0xAE, 0x1B))
        self.assertEqual(exact.timeout_ms, 3000)
        self.assertEqual(exact.p1max, 40)

        with_extra = self.catalog.lookup_timing(329, b"\xae\x1b\x00")
        self.assertIsNotNone(with_extra)
        self.assertEqual(with_extra.request, (0xAE, 0x1B))

        one_byte = self.catalog.lookup_timing(304, "01")
        self.assertIsNotNone(one_byte)
        self.assertEqual(one_byte.request, (0x01,))

        one_byte_with_more = self.catalog.lookup_timing(304, [0x01, 0x02])
        self.assertIsNotNone(one_byte_with_more)
        self.assertEqual(one_byte_with_more.request, (0x01,))

        # A one-byte lookup matches both AE rules, which disagree on timing.
        self.assertIsNone(self.catalog.lookup_timing(329, "AE"))
        self.assertIsNone(self.catalog.lookup_timing(999, "AE1B"))
        self.assertIsNone(self.catalog.lookup_timing(329, "not hex"))
        self.assertIsNone(self.catalog.lookup_timing(329, b""))

    def test_iterators(self):
        profiles = list(self.catalog.iter_profiles())
        scripts = list(self.catalog.iter_scripts())
        self.assertEqual([profile.id for profile in profiles], ["0b00c8af80206a61"])
        self.assertEqual([script.id for script in scripts], ["VCC-227144"])

    def test_round_trip_serialization(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalog.json"
            dump_catalog(self.catalog, target)
            reloaded = load_catalog(target)
        self.assertEqual(reloaded.dtc_descriptions, self.catalog.dtc_descriptions)
        self.assertEqual(reloaded.timing, self.catalog.timing)
        self.assertEqual(reloaded.profiles, self.catalog.profiles)
        self.assertEqual(reloaded.scripts, self.catalog.scripts)
        self.assertEqual(reloaded.source, self.catalog.source)
        serialized = catalog_to_dict(reloaded)
        self.assertEqual(serialized["schema"], SCHEMA_NAME)
        self.assertEqual(serialized["version"], SCHEMA_VERSION)
        self.assertEqual(serialized["timing"][0]["request"], "AE1B")

    def test_loads_compressed_fixture(self):
        catalog = load_catalog(COMPRESSED_FIXTURE)
        self.assertEqual(catalog_to_dict(catalog), catalog_to_dict(self.catalog))

    def test_round_trip_compressed_formats(self):
        expected = catalog_to_dict(self.catalog)
        magic = {
            ".json.gz": b"\x1f\x8b",
            ".json.bz2": b"BZh",
            ".json.xz": b"\xfd7zXZ\x00",
            ".json.zip": b"PK\x03\x04",
        }
        with tempfile.TemporaryDirectory() as directory:
            for suffix in magic:
                with self.subTest(suffix=suffix):
                    target = Path(directory) / ("catalog" + suffix)
                    dump_catalog(self.catalog, target)
                    self.assertTrue(
                        target.read_bytes().startswith(magic[suffix]),
                        "missing {} magic".format(suffix),
                    )
                    reloaded = load_catalog(target)
                    self.assertEqual(catalog_to_dict(reloaded), expected)

    def test_zip_catalog_member_is_named_from_the_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalog.json.zip"
            dump_catalog(self.catalog, target)
            with zipfile.ZipFile(target) as archive:
                self.assertEqual(archive.namelist(), ["catalog.json"])

    def test_unsupported_catalog_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalog.txt"
            target.write_text("{}", encoding="utf-8")
            with self.assertRaises(CatalogError) as caught:
                load_catalog(target)
            self.assertIn("unsupported catalog suffix", str(caught.exception))
            with self.assertRaises(CatalogError):
                dump_catalog(self.catalog, target)

    def test_corrupt_compressed_catalog_is_a_catalog_error(self):
        labels = {
            ".json.gz": "gzip",
            ".json.bz2": "bzip2",
            ".json.xz": "xz",
        }
        with tempfile.TemporaryDirectory() as directory:
            for suffix, label in labels.items():
                with self.subTest(suffix=suffix):
                    target = Path(directory) / ("catalog" + suffix)
                    target.write_bytes(b"not a compressed catalog")
                    with self.assertRaises(CatalogError) as caught:
                        load_catalog(target)
                    self.assertIn(label, str(caught.exception))
                    self.assertIn("corrupt", str(caught.exception))

    def test_zip_catalog_requires_exactly_one_json_member(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "catalog.zip"
            with zipfile.ZipFile(target, "w") as archive:
                archive.writestr("first.json", "{}")
                archive.writestr("second.json", "{}")
            with self.assertRaises(CatalogError) as caught:
                load_catalog(target)
            self.assertIn("exactly one .json member", str(caught.exception))

    def test_missing_compressed_catalog_propagates_file_error(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "missing.json.gz"
            with self.assertRaises(FileNotFoundError):
                load_catalog(target)

    def test_malformed_catalogs_raise_catalog_error(self):
        cases = []

        wrong_schema = dict(self.document)
        wrong_schema["schema"] = "other"
        cases.append(("wrong schema", wrong_schema))

        wrong_version = dict(self.document)
        wrong_version["version"] = 99
        cases.append(("wrong version", wrong_version))

        missing_source = dict(self.document)
        del missing_source["source"]
        cases.append(("missing source", missing_source))

        missing_dtc_section = dict(self.document)
        del missing_dtc_section["dtc_descriptions"]
        cases.append(("missing dtc section", missing_dtc_section))

        bad_code = json.loads(json.dumps(self.document))
        bad_code["dtc_descriptions"][0]["code"] = "NOPE"
        cases.append(("bad code", bad_code))

        bad_timing = json.loads(json.dumps(self.document))
        bad_timing["timing"][0]["request"] = "A"
        cases.append(("bad timing request", bad_timing))

        for label, document in cases:
            with self.subTest(label=label):
                with self.assertRaises(CatalogError):
                    catalog_from_dict(document)

    def test_invalid_json_raises_catalog_error(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "broken.json"
            target.write_text("{not json", encoding="utf-8")
            with self.assertRaises(CatalogError):
                load_catalog(target)

    def test_exporter_from_json_mapping(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input.json"
            target = Path(directory) / "output.json"
            source.write_text(
                json.dumps(
                    {
                        "dtc_descriptions": {
                            "P034211": "Camshaft Position Sensor A Circuit Low",
                            "P010100": {
                                "long_text": "Mass Air Flow A Circuit Range",
                                "short_text": "MAF A range",
                            },
                        },
                        "timing": [
                            {
                                "ecu_variant": 329,
                                "request": "AE1B",
                                "timeout_ms": 3000,
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(EXPORTER),
                    "--from-json",
                    str(source),
                    "--out",
                    str(target),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            catalog = load_catalog(target)

        entry = catalog.lookup_dtc("P034211")
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry.long_text, "Camshaft Position Sensor A Circuit Low"
        )
        self.assertEqual(catalog.lookup_dtc("P010100").short_text, "MAF A range")
        self.assertEqual(catalog.lookup_timing(329, "AE1B").timeout_ms, 3000)
        self.assertEqual(catalog.profiles, ())
        self.assertEqual(catalog.scripts, ())

    def test_exporter_round_trips_complete_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "output.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(EXPORTER),
                    "--from-json",
                    str(FIXTURE),
                    "--out",
                    str(target),
                ],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            catalog = load_catalog(target)
        self.assertEqual(
            len(catalog.dtc_descriptions), len(self.catalog.dtc_descriptions)
        )
        self.assertEqual(catalog.lookup_dtc("P034211", ecu="ECM").ecu, "ECM")

    def test_normalizers(self):
        self.assertEqual(normalize_dtc_code("0x034211"), "P034211")
        self.assertEqual(normalize_dtc_code("p034211"), "P034211")
        self.assertEqual(normalize_dtc_code("P0342"), "P0342")
        self.assertIsNone(normalize_dtc_code("P03421"))
        self.assertIsNone(normalize_dtc_code("Z0342"))
        self.assertEqual(normalize_request_bytes("0xAE1B"), (0xAE, 0x1B))
        self.assertEqual(normalize_request_bytes("AE 1B"), (0xAE, 0x1B))
        self.assertEqual(normalize_request_bytes([0xAE, 0x1B]), (0xAE, 0x1B))
        self.assertIsNone(normalize_request_bytes("AE1B00FF"))
