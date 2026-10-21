#!/usr/bin/env python3
"""Export a runtime VIDA catalog from the VIDA image or an existing JSON file.

The image path is read only. Nothing is attached to SQL Server and no VIDA
process is started. The exporter reads MDF pages directly:

* ``DiceTiming_Data.MDF``: ``Requests``, ``MessageTiming`` and
  ``TimeoutAndResend`` records, by page and slot.
* ``CarComRT_Data.MDF``: ``T150_BlockValue`` value rows, the ``T191_TextData``
  text they point at, and the ``T141_Block`` name for the same block.

``--from-json`` accepts either a complete catalog or a small mapping and always
works without an image. The transform is validated by ``load_catalog`` before
anything is written, so a malformed input cannot produce a catalog the runtime
would reject.

What is not extracted yet:

* Vehicle profiles from ``BaseData_Data.MDF``. The table and component joins
  are present, but this pass does not decode the profile tuple.
* Script bodies from ``DiagSwdlRepository_Data.MDF``. They are PKZIP streams
  spread over a 2.9 GB file and need a carve plus CRC validation.

Both sections are written empty rather than guessed.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import mmap
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Sequence, Tuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from volvo_p3.vida_catalog import (  # noqa: E402
    CatalogError,
    CatalogSource,
    DatabaseSource,
    DtcDescription,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    TimingEntry,
    VidaCatalog,
    catalog_from_dict,
    dump_catalog,
)


PAGE_SIZE = 8192
PAGE_TYPE_OFFSET = 1
DATA_PAGE_TYPE = 1
SLOT_COUNT_OFFSET = 0x16
FIRST_RECORD_OFFSET = 96

# T150_BlockValue records have a 44-byte fixed part and two variable columns,
# CompareValue and the P-code. In this snapshot CompareValue starts at +56.
T150_FIXED_LENGTH = 0x2C
T150_VALUE_OFFSET = 56
T150_PATTERN = re.compile(rb"0x([0-9A-Fa-f]{6})(P[0-9A-Fa-f]{6})")

T141_FIXED_LENGTH = 0x1D
T141_NAME_OFFSET = 37

T191_FIXED_LENGTH = 0x10
T191_TEXT_OFFSET = 23

TIMING_REQUEST_LENGTH = 519
TIMING_MESSAGE_LENGTH = 520
TIMING_TIMEOUT_LENGTH = 516

MAX_DTC_TEXT_ID = 50_000_000
MAX_BLOCK_ID = 50_000_000


def iter_page_records(path: Path) -> Iterator[Tuple[int, int, memoryview]]:
    """Yield ``(page_number, offset, record_bytes)`` for every data page.

    The page type is at byte 1 in this image. The slot array starts at
    ``PAGE_SIZE - 2 * slot_count``. Its final entry is stale in this snapshot
    and points into the first record's padding (the timing pages have the same
    quirk, documented in ``docs/vida-second-pass.md``), so the last entry is
    skipped and the first record at offset 96 is always included.
    """

    with path.open("rb") as handle:
        page_number = 0
        while True:
            page = handle.read(PAGE_SIZE)
            if len(page) < PAGE_SIZE:
                break
            if page[PAGE_TYPE_OFFSET] == DATA_PAGE_TYPE:
                slot_count = int.from_bytes(
                    page[SLOT_COUNT_OFFSET : SLOT_COUNT_OFFSET + 2], "little"
                )
                if 0 < slot_count <= (PAGE_SIZE - FIRST_RECORD_OFFSET) // 2:
                    offsets = {FIRST_RECORD_OFFSET}
                    array_start = PAGE_SIZE - 2 * slot_count
                    for position in range(array_start, PAGE_SIZE - 2, 2):
                        offset = int.from_bytes(
                            page[position : position + 2], "little"
                        )
                        if FIRST_RECORD_OFFSET <= offset < PAGE_SIZE:
                            offsets.add(offset)
                    for offset in sorted(offsets):
                        yield page_number, offset, memoryview(page)[offset:]
            page_number += 1


def extract_timing(path: Path) -> Tuple[List[TimingEntry], Dict[str, int]]:
    """Read the three DiceTiming tables and join them into timing entries.

    Counts are returned for the export summary. Rows whose record shape does
    not match one of the three known fixed lengths are ignored.
    """

    requests: List[Dict[str, Any]] = []
    message_timing: Dict[int, Dict[str, int]] = {}
    timeout_and_resend: Dict[int, Dict[str, int]] = {}
    seen_requests = set()

    for _page_number, _offset, record in iter_page_records(path):
        if len(record) < 4 or record[0] not in (0x10, 0x30):
            continue
        fixed_length = int.from_bytes(record[2:4], "little")

        if fixed_length == TIMING_REQUEST_LENGTH:
            ecu_variant = int.from_bytes(record[4:8], "little")
            b1, b2, b3 = record[8], record[9], record[10]
            message_timing_id = int.from_bytes(record[11:13], "little")
            timeout_and_resend_id = int.from_bytes(record[13:17], "little")
            comment = _clean_text(record[19:])
            if not (0 < ecu_variant <= 1_000_000):
                continue
            key = (
                ecu_variant,
                b1,
                b2,
                b3,
                message_timing_id,
                timeout_and_resend_id,
                comment,
            )
            if key in seen_requests:
                continue
            seen_requests.add(key)
            requests.append(
                {
                    "ecu_variant": ecu_variant,
                    "request": (b1, b2, b3),
                    "message_timing_id": message_timing_id,
                    "timeout_and_resend_id": timeout_and_resend_id,
                    "comment": comment,
                }
            )

        elif fixed_length == TIMING_MESSAGE_LENGTH:
            timing_id = int.from_bytes(record[4:8], "little")
            p1max = int.from_bytes(record[8:12], "little")
            p3min = int.from_bytes(record[12:16], "little")
            p4min = int.from_bytes(record[16:20], "little")
            comment = _clean_text(record[20:])
            if not (0 <= timing_id <= 1_000_000):
                continue
            message_timing.setdefault(
                timing_id,
                {
                    "p1max": p1max,
                    "p3min": p3min,
                    "p4min": p4min,
                    "comment": comment,
                },
            )

        elif fixed_length == TIMING_TIMEOUT_LENGTH:
            timeout_id = int.from_bytes(record[4:8], "little")
            timeout = int.from_bytes(record[8:12], "little")
            resend = int.from_bytes(record[12:16], "little")
            comment = _clean_text(record[16:])
            if not (0 <= timeout_id <= 1_000_000):
                continue
            timeout_and_resend.setdefault(
                timeout_id,
                {"timeout_ms": timeout, "resend": resend, "comment": comment},
            )

    entries: List[TimingEntry] = []
    for request in sorted(
        requests, key=lambda item: (item["ecu_variant"], item["request"])
    ):
        message = message_timing.get(request["message_timing_id"], {})
        timeout = timeout_and_resend.get(request["timeout_and_resend_id"], {})
        entries.append(
            TimingEntry(
                ecu_variant=request["ecu_variant"],
                # The third byte is a real lookup byte, but 0 is the common
                # wildcard value in this table; keep all three as stored.
                request=tuple(request["request"]),
                message_timing_id=request["message_timing_id"],
                timeout_and_resend_id=request["timeout_and_resend_id"],
                p1max=message.get("p1max"),
                p3min=message.get("p3min"),
                p4min=message.get("p4min"),
                timeout_ms=timeout.get("timeout_ms"),
                resend=timeout.get("resend"),
                comment=request["comment"] or None,
            )
        )

    stats = {
        "requests": len(requests),
        "message_timing": len(message_timing),
        "timeout_and_resend": len(timeout_and_resend),
    }
    return entries, stats


def extract_dtc_descriptions(
    path: Path, language_id: int = 4, language_name: str = "en-US"
) -> Tuple[List[DtcDescription], Dict[str, int]]:
    """Read T150 value rows, T191 texts and T141 block names.

    Only rows that carry a standard ``0xNNNNNN`` CompareValue and a matching
    ``PNNNNNN`` code are used. A code with several block variants keeps the
    most frequent (short text, long text) pair; the other variants are counted
    but not merged into invented text.
    """

    with _mapped(path) as data:
        rows = list(_iter_t150_rows(data))
        block_ids = {block_id for _code, block_id, _text_id in rows}
        text_ids = {text_id for _code, _block_id, text_id in rows}

        block_names = _iter_t141_names(data, block_ids)
        texts = _iter_t191_texts(data, text_ids, language_id)

    pairs: Dict[str, Counter] = {}
    for code, block_id, text_id in rows:
        long_text = texts.get(text_id)
        if not long_text:
            continue
        short_text = block_names.get(block_id)
        pairs.setdefault(code, Counter())[(short_text, long_text)] += 1

    descriptions: List[DtcDescription] = []
    variant_total = 0
    for code in sorted(pairs):
        counter = pairs[code]
        variant_total += len(counter)
        (short_text, long_text), count = sorted(
            counter.items(),
            key=lambda item: (
                -item[1],
                item[0][0] is None,
                len(item[0][1]),
                item[0][1],
            ),
        )[0]
        descriptions.append(
            DtcDescription(
                code=code,
                long_text=long_text,
                short_text=short_text,
                language=language_name,
                source=(
                    "CarComRT T150_BlockValue/T191_TextData/T141_Block "
                    "({} row(s), {} variant(s))".format(count, len(counter))
                ),
            )
        )

    stats = {
        "t150_rows": len(rows),
        "blocks": len(block_ids),
        "block_names": len(block_names),
        "text_ids": len(text_ids),
        "texts": len(texts),
        "codes": len(pairs),
        "variants": variant_total,
    }
    return descriptions, stats


def build_from_image(
    image: Path,
    language_id: int = 4,
    language_name: str = "en-US",
    include_sha256: bool = True,
) -> Tuple[VidaCatalog, Dict[str, Any]]:
    """Build a catalog from a VIDA image root."""

    carcom_path, timing_path = _resolve_database_paths(image)

    timing, timing_stats = extract_timing(timing_path)
    descriptions, dtc_stats = extract_dtc_descriptions(
        carcom_path, language_id=language_id, language_name=language_name
    )

    databases = [
        DatabaseSource(
            name="CarComRT",
            path=_display_path(carcom_path, image),
            sha256=_sha256(carcom_path) if include_sha256 else None,
        ),
        DatabaseSource(
            name="DiceTiming",
            path=_display_path(timing_path, image),
            sha256=_sha256(timing_path) if include_sha256 else None,
        ),
    ]

    notes = [
        (
            "DTC descriptions: {} codes from CarComRT T150_BlockValue, "
            "T191_TextData and T141_Block; language id {} ({})."
        ).format(len(descriptions), language_id, language_name),
        (
            "Timing: {} Requests, {} MessageTiming, {} TimeoutAndResend "
            "rows from DiceTiming_Data.MDF."
        ).format(
            timing_stats["requests"],
            timing_stats["message_timing"],
            timing_stats["timeout_and_resend"],
        ),
        (
            "Codes with more than one block variant keep the most frequent "
            "T150/T141/T191 pair; variants were counted, not merged."
        ),
        (
            "Profiles are empty: BaseData_Data.MDF contains VehicleProfile "
            "rows, but the profile tuple and component joins were not decoded "
            "in this pass."
        ),
        (
            "Scripts are empty: script bodies in DiagSwdlRepository_Data.MDF "
            "are PKZIP streams spread over a 2.9 GB file and need a carve plus "
            "CRC validation."
        ),
    ]

    source = CatalogSource(
        kind="vida-mdf",
        generated_at=_utc_timestamp(),
        image=str(image),
        databases=tuple(databases),
        extractor="tools/export_vida_catalog.py",
        language=language_name,
        notes=tuple(notes),
    )
    catalog = VidaCatalog(
        source=source,
        dtc_descriptions=tuple(descriptions),
        timing=tuple(timing),
        profiles=(),
        scripts=(),
    )
    stats = {
        "dtc": dtc_stats,
        "timing": timing_stats,
        "databases": databases,
    }
    return catalog, stats


def build_from_json(path: Path) -> VidaCatalog:
    """Load a complete catalog or normalize a small JSON mapping.

    Accepted input for the mapping form:

    .. code-block:: json

        {
          "dtc_descriptions": {
            "P034211": "Camshaft Position Sensor A Circuit Low",
            "P010100": {"long_text": "...", "short_text": "..."}
          },
          "timing": [{"ecu_variant": 329, "request": "AE1B"}],
          "profiles": [],
          "scripts": []
        }

    A document already carrying ``"schema": "pydice.vida_catalog"`` is loaded
    with the normal validator and re-serialized unchanged in shape.
    """

    with path.open("r", encoding="utf-8") as handle:
        try:
            document = json.load(handle)
        except json.JSONDecodeError as exc:
            raise CatalogError(
                "input is not valid JSON: {} (line {}, column {})".format(
                    exc.msg, exc.lineno, exc.colno
                )
            ) from exc

    if not isinstance(document, dict):
        raise CatalogError("input root must be a JSON object")

    if document.get("schema") == SCHEMA_NAME:
        return catalog_from_dict(document)

    normalized: Dict[str, Any] = dict(document)
    normalized.setdefault("schema", SCHEMA_NAME)
    normalized.setdefault("version", SCHEMA_VERSION)
    source = normalized.get("source")
    if not isinstance(source, dict):
        source = {"kind": "json"}
    else:
        source = dict(source)
        source.setdefault("kind", "json")
    normalized["source"] = source

    dtc_section = normalized.get("dtc_descriptions", [])
    if isinstance(dtc_section, dict):
        converted: List[Dict[str, Any]] = []
        for code, value in dtc_section.items():
            if isinstance(value, str):
                converted.append({"code": code, "long_text": value})
            elif isinstance(value, dict):
                entry = dict(value)
                entry.setdefault("code", code)
                converted.append(entry)
            else:
                raise CatalogError(
                    "dtc_descriptions[{!r}] must be a string or an object".format(
                        code
                    )
                )
        normalized["dtc_descriptions"] = converted
    elif not isinstance(dtc_section, list):
        raise CatalogError("dtc_descriptions must be a list or an object")

    for section in ("dtc_descriptions", "timing", "profiles", "scripts"):
        normalized.setdefault(section, [])

    return catalog_from_dict(normalized)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a runtime VIDA catalog from the read-only VIDA image, or "
            "transform an existing JSON file."
        )
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--image",
        type=Path,
        help="VIDA image root, for example /mnt/vida",
    )
    source_group.add_argument(
        "--from-json",
        type=Path,
        help="complete catalog or simple mapping to normalize",
    )
    parser.add_argument("--out", type=Path, required=True, help="output JSON path")
    parser.add_argument(
        "--language-id",
        type=int,
        default=4,
        help="T191 language id to export (default: 4)",
    )
    parser.add_argument(
        "--language",
        default="en-US",
        help="language label stored in the catalog (default: en-US)",
    )
    parser.add_argument(
        "--no-sha256",
        action="store_true",
        help="skip SHA-256 hashes of the source databases",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the summary")

    args = parser.parse_args(argv)

    try:
        if args.from_json is not None:
            catalog = build_from_json(args.from_json)
            stats: Optional[Dict[str, Any]] = None
        else:
            catalog, stats = build_from_image(
                args.image,
                language_id=args.language_id,
                language_name=args.language,
                include_sha256=not args.no_sha256,
            )
        dump_catalog(catalog, args.out)
    except (OSError, CatalogError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1

    if not args.quiet:
        if stats is not None:
            dtc = stats["dtc"]
            timing = stats["timing"]
            print(
                "DTC descriptions: {} codes ({} T150 rows, {} T141 names, "
                "{} T191 texts, language id {}/{})".format(
                    dtc["codes"],
                    dtc["t150_rows"],
                    dtc["block_names"],
                    dtc["texts"],
                    args.language_id,
                    args.language,
                )
            )
            print(
                "Timing: {} Requests, {} MessageTiming, {} TimeoutAndResend".format(
                    timing["requests"],
                    timing["message_timing"],
                    timing["timeout_and_resend"],
                )
            )
        print(
            "Wrote {} ({} bytes)".format(args.out, args.out.stat().st_size)
        )
    return 0


@contextlib.contextmanager
def _mapped(path: Path) -> Iterator[mmap.mmap]:
    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def _iter_t150_rows(data: mmap.mmap) -> Iterator[Tuple[str, int, int]]:
    for match in T150_PATTERN.finditer(data):
        record_start = match.start() - T150_VALUE_OFFSET
        if record_start < FIRST_RECORD_OFFSET:
            continue
        if record_start % PAGE_SIZE < FIRST_RECORD_OFFSET:
            continue
        if record_start % PAGE_SIZE + 71 > PAGE_SIZE:
            continue
        if record_start + 71 > len(data):
            continue
        if data[record_start] not in (0x10, 0x30):
            continue
        if data[record_start + 1] != 0:
            continue
        if data[record_start + 2 : record_start + 4] != b"\x2c\x00":
            continue

        hex_code = match.group(1).decode("ascii").upper()
        p_code = match.group(2).decode("ascii").upper()
        if p_code[1:] != hex_code:
            continue

        block_id = int.from_bytes(data[record_start + 4 : record_start + 8], "little")
        text_id = int.from_bytes(
            data[record_start + 20 : record_start + 24], "little"
        )
        if not (0 < block_id < MAX_BLOCK_ID):
            continue
        if not (0 < text_id < MAX_DTC_TEXT_ID):
            continue
        yield p_code, block_id, text_id


def _iter_t141_names(
    data: mmap.mmap, block_ids: Sequence[int]
) -> Dict[int, str]:
    wanted = set(block_ids)
    names: Dict[int, str] = {}
    if not wanted:
        return names

    start = 0
    while True:
        position = data.find(b"\x30\x00\x1d\x00", start)
        if position < 0:
            break
        start = position + 1
        if position % PAGE_SIZE < FIRST_RECORD_OFFSET:
            continue
        if position + 37 > len(data):
            continue
        block_id = int.from_bytes(data[position + 4 : position + 8], "little")
        if block_id not in wanted or block_id in names:
            continue
        record_end = int.from_bytes(data[position + 35 : position + 37], "little")
        if not (37 <= record_end <= 400):
            continue
        if position + record_end > len(data):
            continue
        raw = bytes(data[position + T141_NAME_OFFSET : position + record_end])
        if not raw:
            continue
        if any(byte < 32 or byte > 126 for byte in raw):
            continue
        name = raw.decode("ascii").strip()
        if name:
            names[block_id] = name
    return names


def _iter_t191_texts(
    data: mmap.mmap, text_ids: Sequence[int], language_id: int
) -> Dict[int, str]:
    wanted = set(text_ids)
    texts: Dict[int, str] = {}
    if not wanted:
        return texts

    for header in (b"\x30\x00\x10\x00", b"\x10\x00\x10\x00"):
        start = 0
        while True:
            position = data.find(header, start)
            if position < 0:
                break
            start = position + 1
            if position % PAGE_SIZE < FIRST_RECORD_OFFSET:
                continue
            if position + 23 > len(data):
                continue
            if data[position + 2 : position + 4] != b"\x10\x00":
                continue
            text_id = int.from_bytes(data[position + 8 : position + 12], "little")
            if text_id not in wanted or text_id in texts:
                continue
            language = int.from_bytes(data[position + 4 : position + 8], "little")
            if language != language_id:
                continue
            record_end = int.from_bytes(
                data[position + 21 : position + 23], "little"
            )
            if not (23 <= record_end <= 4096):
                continue
            if position + record_end > len(data):
                continue
            raw = bytes(data[position + T191_TEXT_OFFSET : position + record_end])
            if not raw or len(raw) % 2:
                continue
            try:
                text = raw.decode("utf-16-le")
            except UnicodeDecodeError:
                continue
            text = text.strip("\x00").strip()
            if not text:
                continue
            printable = sum(
                1 for char in text if char.isprintable() or char in "\r\n\t"
            )
            if printable / len(text) < 0.95:
                continue
            texts[text_id] = text
    return texts


def _resolve_database_paths(image: Path) -> Tuple[Path, Path]:
    candidates = [image / "VIDA" / "db", image / "db"]
    for directory in candidates:
        carcom = directory / "CarComRT_Data.MDF"
        timing = directory / "DiceTiming_Data.MDF"
        if carcom.is_file() and timing.is_file():
            return carcom, timing
    raise CatalogError(
        "no CarComRT_Data.MDF and DiceTiming_Data.MDF found under {} "
        "(looked in VIDA/db and db)".format(image)
    )


def _display_path(path: Path, image: Path) -> str:
    try:
        return str(path.relative_to(image))
    except ValueError:
        return str(path)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _clean_text(raw: Any) -> str:
    text = bytes(raw).decode("latin-1", errors="replace")
    text = text.split("\x00", 1)[0]
    text = "".join(
        char if char.isprintable() or char in "\r\n\t" else " " for char in text
    )
    # The DiceTiming comment columns are fixed-width and space padded. A long
    # run of spaces marks the end of the text; anything after it is padding or
    # leftover bytes from the next record.
    text = re.split(r"[ \t]{4,}", text, maxsplit=1)[0]
    return text.strip()


def _utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
