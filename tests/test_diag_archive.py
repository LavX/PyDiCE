"""Tests for the diagnostic dump archive packer and its command-line tool."""

import json
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

from tools import export_diag_catalog as exporter

from volvo_p3.diag_archive import (
    DiagArchiveError,
    archive_format,
    format_bytes,
    pack_dump,
)
from volvo_p3.diag_catalog import load_catalog


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).with_name("fixtures")
V1_FIXTURE = FIXTURES / "diag_catalog.json"
PACKER = REPOSITORY_ROOT / "tools" / "pack_diag_dump.py"


def build_dump(directory):
    """Write a small dump tree with raw and script children."""

    catalog = exporter.build_from_json(V1_FIXTURE)
    exporter.write_dump(directory, catalog, {})
    scripts = directory / "scripts"
    scripts.mkdir(exist_ok=True)
    (scripts / "example.xml").write_text("<script />\n", encoding="utf-8")
    return directory


def archive_names(path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(str(path)) as archive:
            return archive.namelist()
    with tarfile.open(str(path)) as archive:
        return archive.getnames()


def read_catalog_member(path):
    if path.suffix.lower() == ".zip":
        with zipfile.ZipFile(str(path)) as archive:
            return archive.read("catalog.json")
    with tarfile.open(str(path)) as archive:
        member = archive.extractfile("catalog.json")
        if member is None:
            raise AssertionError("catalog.json is not a file member")
        return member.read()


class ArchiveFormatTests(unittest.TestCase):
    def test_archive_format_is_selected_by_suffix(self):
        self.assertEqual(archive_format("dump.tar.gz"), "tar.gz")
        self.assertEqual(archive_format("DUMP.TAR.XZ"), "tar.xz")
        self.assertEqual(archive_format("dump.zip"), "zip")
        with self.assertRaises(DiagArchiveError) as caught:
            archive_format("dump.tar.bz2")
        self.assertIn("unsupported archive suffix", str(caught.exception))

    def test_pack_each_format_keeps_root_layout_and_reads_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = build_dump(root / "dump")
            expected = json.loads(
                (dump / "catalog.json").read_text(encoding="utf-8")
            )
            for suffix in (".tar.gz", ".tar.xz", ".zip"):
                with self.subTest(suffix=suffix):
                    target = root / ("dump" + suffix)
                    result = pack_dump(dump, target)
                    self.assertTrue(target.is_file())
                    self.assertGreater(result.input_bytes, 0)
                    self.assertGreater(result.output_bytes, 0)
                    self.assertGreaterEqual(result.file_count, 4)
                    self.assertGreaterEqual(result.ratio, 0.0)
                    self.assertGreaterEqual(result.savings, 0.0)

                    names = archive_names(target)
                    self.assertIn("catalog.json", names)
                    self.assertIn("raw/base_data.json", names)
                    self.assertIn("scripts/example.xml", names)
                    for name in names:
                        self.assertFalse(
                            name.startswith("dump/"),
                            "archive must not wrap the dump directory",
                        )

                    extracted = root / ("read" + suffix + ".json")
                    extracted.write_bytes(read_catalog_member(target))
                    catalog = load_catalog(extracted)
                    self.assertEqual(
                        catalog.counts()["dtc_descriptions"],
                        len(expected["dtc_descriptions"]),
                    )

    def test_pack_rejects_output_inside_dump(self):
        with tempfile.TemporaryDirectory() as directory:
            dump = build_dump(Path(directory) / "dump")
            with self.assertRaises(DiagArchiveError) as caught:
                pack_dump(dump, dump / "dump.tar.gz")
            self.assertIn("outside the dump directory", str(caught.exception))

    def test_pack_rejects_dump_without_catalog(self):
        with tempfile.TemporaryDirectory() as directory:
            empty = Path(directory) / "empty"
            empty.mkdir()
            with self.assertRaises(DiagArchiveError) as caught:
                pack_dump(empty, Path(directory) / "empty.tar.gz")
            self.assertIn("no catalog.json", str(caught.exception))

    def test_format_bytes_uses_binary_units(self):
        self.assertEqual(format_bytes(999), "999 B")
        self.assertEqual(format_bytes(1024), "1.0 KiB")
        self.assertEqual(format_bytes(1024 * 1024), "1.0 MiB")


class PackerCommandTests(unittest.TestCase):
    def run_packer(self, source, target):
        return subprocess.run(
            [
                sys.executable,
                str(PACKER),
                "--input",
                str(source),
                "--output",
                str(target),
            ],
            capture_output=True,
            text=True,
        )

    def test_cli_prints_sizes_and_ratio(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = build_dump(root / "dump")
            target = root / "dump.tar.gz"
            result = self.run_packer(dump, target)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.is_file())
            self.assertIn("Input:", result.stdout)
            self.assertIn("Output:", result.stdout)
            self.assertIn("Compression ratio:", result.stdout)
            self.assertIn("Wrote {}".format(target), result.stdout)

    def test_cli_reports_missing_input(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = Path(directory) / "does-not-exist"
            result = self.run_packer(missing, Path(directory) / "dump.tar.gz")
            self.assertEqual(result.returncode, 1)
            self.assertIn("dump directory not found", result.stderr)

    def test_cli_reports_unsupported_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = build_dump(root / "dump")
            result = self.run_packer(dump, root / "dump.tar.bz2")
            self.assertEqual(result.returncode, 1)
            self.assertIn("unsupported archive suffix", result.stderr)
            self.assertFalse((root / "dump.tar.bz2").exists())


class ExporterArchiveTests(unittest.TestCase):
    def run_exporter(self, *arguments):
        return subprocess.run(
            [sys.executable, str(REPOSITORY_ROOT / "tools" / "export_diag_catalog.py")]
            + list(arguments),
            capture_output=True,
            text=True,
        )

    def test_out_supports_each_catalog_compression(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix in (".json.gz", ".json.bz2", ".json.xz"):
                with self.subTest(suffix=suffix):
                    target = root / ("catalog" + suffix)
                    result = self.run_exporter(
                        "--from-json", str(V1_FIXTURE), "--out", str(target)
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    catalog = load_catalog(target)
                    self.assertEqual(catalog.counts()["dtc_descriptions"], 5)

    def test_dump_archive_packs_an_existing_dump_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dump = root / "dump"
            target = root / "dump.zip"
            result = self.run_exporter(
                "--from-json",
                str(V1_FIXTURE),
                "--dump-dir",
                str(dump),
                "--dump-archive",
                str(target),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((dump / "catalog.json").is_file())
            names = archive_names(target)
            self.assertIn("catalog.json", names)
            self.assertIn("raw/base_data.json", names)
            extracted = root / "catalog.json"
            extracted.write_bytes(read_catalog_member(target))
            self.assertEqual(
                load_catalog(extracted).counts()["dtc_descriptions"], 5
            )

    def test_dump_archive_works_without_a_kept_dump_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "dump.tar.xz"
            result = self.run_exporter(
                "--from-json",
                str(V1_FIXTURE),
                "--dump-archive",
                str(target),
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(target.is_file())
            names = archive_names(target)
            self.assertIn("catalog.json", names)
            self.assertIn("raw/base_data.json", names)

    def test_dump_archive_rejects_unsupported_suffix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result = self.run_exporter(
                "--from-json",
                str(V1_FIXTURE),
                "--dump-dir",
                str(root / "dump"),
                "--dump-archive",
                str(root / "dump.tar.bz2"),
            )
            self.assertEqual(result.returncode, 1)
            self.assertIn("unsupported archive suffix", result.stderr)


if __name__ == "__main__":
    unittest.main()
