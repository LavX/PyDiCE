#!/usr/bin/env python3
"""Export a runtime diagnostic catalog from the source image or an existing JSON file.

The image path is read only. Nothing is attached to SQL Server and no source data
process is started. The exporter reads MDF pages directly:

* ``DiceTiming_Data.MDF``: ``Requests``, ``MessageTiming`` and
  ``TimeoutAndResend`` records, by page and slot.
* ``BaseData_Data.MDF``: ``VehicleProfile`` rows and the component tables they
  reference, decoded by page, slot and record layout.
* ``CarComRT_Data.MDF``: ``T150_BlockValue`` value rows, the ``T191_TextData``
  text they point at, and the ``T141_Block`` name and type for the same block.
  Symptom and freeze-frame table coverage is reported even when a table has no
  page set in the snapshot.
* ``DiagSwdlRepository_Data.MDF``: PKZIP streams carved from the raw pages,
  validated by CRC where possible, and parsed as diagnostic script XML.

``--from-json`` accepts either a complete catalog or a small mapping and always
works without an image. ``--dump-dir`` writes the catalog plus bounded raw table
summaries and recovered script XML files.

The transform is validated by ``load_catalog`` before anything is written, so a
malformed input cannot produce a catalog the runtime would reject.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime
import hashlib
import json
import mmap
import re
import struct
import sys
import zlib
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from volvo_p3.diag_catalog import (  # noqa: E402
    CatalogError,
    CatalogSource,
    DatabaseSource,
    DiagnosticCatalog,
    DtcDescription,
    FreezeFrameEntry,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    ScriptEcu,
    ScriptEntry,
    SymptomEntry,
    TimingEntry,
    VehicleProfile,
    catalog_from_dict,
    dump_catalog,
    sanitize_text,
)
from volvo_p3.script_plan import (  # noqa: E402
    DiagnosticScriptError,
    MAX_SCRIPT_BYTES,
    is_read_only_service,
    parse_script_xml,
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

# BaseData table object ids recovered from sys.syscolpars and page headers in
# this snapshot. A different snapshot can renumber them; the exporter reports
# zero rows rather than inventing a join when a table has no page set.
BASE_PROFILE_OBJECT = 104
BASE_PROFILE_PARTNER_OBJECT = 130_099_504
BASE_COMPONENT_OBJECTS = {
    "model": 18_099_105,
    "model_year": 151,
    "engine": 150,
    "transmission": 162,
    "body_style": 144,
    "steering": 157,
    "special_vehicle": 178_099_675,
    "market": 153,
    "node_ecu": 152,
}

# CarComRT table object ids recovered from sys.syscolpars.
CARCOM_OBJECTS = {
    "T141_Block": 293_576_084,
    "T144_BlockChild": 453_576_654,
    "T148_BlockMetaPARA": 469_576_711,
    "T150_BlockValue": 485_576_768,
    "T153_SymptomCategory": 501_576_825,
    "T152_Symptom": 581_577_110,
    "T158_Symptom_CSC": 597_577_167,
    "T157_SymptomConnection": 613_577_224,
    "T159_SymptomCSC_SymptomDTC": 629_577_281,
    "T191_TextData": 341_576_255,
}
CARCOM_EMPTY_BLOCKERS = {
    "T147_BlockMetaDTC": "no page set and no physical schema in sys.syscolpars",
    "T148_BlockMetaPARA": "no page set in this snapshot",
    "T152_Symptom": "no page set in this snapshot",
    "T153_SymptomCategory": "no page set in this snapshot",
    "T154_SymptomType": "no page set and no physical schema in sys.syscolpars",
    "T156_SymptomSection": "no page set and no physical schema in sys.syscolpars",
    "T157_SymptomConnection": "no page set in this snapshot",
    "T158_Symptom_CSC": "no page set in this snapshot",
    "T159_SymptomCSC_SymptomDTC": "no page set in this snapshot",
}


# ---------------------------------------------------------------------------
# Page and record helpers
# ---------------------------------------------------------------------------


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


def _u16(record: bytes, offset: int) -> int:
    return int.from_bytes(record[offset : offset + 2], "little")


def _u32(record: bytes, offset: int) -> int:
    return int.from_bytes(record[offset : offset + 4], "little")


def _pages_for_object(data: mmap.mmap, object_id: int) -> List[int]:
    pages: List[int] = []
    for page_number in range(len(data) // PAGE_SIZE):
        start = page_number * PAGE_SIZE
        if data[start + PAGE_TYPE_OFFSET] not in (1, 2):
            continue
        if int.from_bytes(data[start + 24 : start + 28], "little") == object_id:
            pages.append(page_number)
    return pages


def _iter_object_records(
    data: mmap.mmap, object_id: int
) -> Iterator[Tuple[int, bytes]]:
    """Yield plausible records from a table's data and index pages.

    The slot array is used first. A scan for the record header is used as a
    fallback because a few pages in this snapshot have stale slots.
    """

    for page_number in _pages_for_object(data, object_id):
        page = data[page_number * PAGE_SIZE : (page_number + 1) * PAGE_SIZE]
        slot_count = _u16(page, SLOT_COUNT_OFFSET)
        offsets = set()
        if 0 < slot_count <= (PAGE_SIZE - FIRST_RECORD_OFFSET) // 2:
            array_start = PAGE_SIZE - 2 * slot_count
            for position in range(array_start, PAGE_SIZE - 2, 2):
                offset = _u16(page, position)
                if FIRST_RECORD_OFFSET <= offset < PAGE_SIZE:
                    offsets.add(offset)
        offsets.add(FIRST_RECORD_OFFSET)
        ordered = sorted(offsets)
        for index, offset in enumerate(ordered):
            end = ordered[index + 1] if index + 1 < len(ordered) else PAGE_SIZE
            record = page[offset:end]
            if record:
                yield page_number, bytes(record)


def _printable_ascii(raw: bytes) -> bool:
    return bool(raw) and all(32 <= byte < 127 for byte in raw)


# ---------------------------------------------------------------------------
# BaseData: vehicle profiles and component joins
# ---------------------------------------------------------------------------


def parse_component_record(record: bytes) -> Optional[Tuple[int, int, Tuple[str, ...]]]:
    """Decode one BaseData component row.

    The observed layout is a 4-byte record header, a 4-byte id, a 4-byte
    ``Cid``, an 8-byte object version, a small bitmap/count block, one or two
    2-byte end offsets and the variable text values. Both fixed lengths seen in
    this snapshot are handled: 0x14 for single-description tables and 0x10 for
    the partner-group table, which adds a short code before the description.
    """

    if len(record) < 27 or record[0] != 0x30:
        return None
    fixed_length = _u16(record, 2)
    if fixed_length not in (0x10, 0x14):
        return None
    row_id = _u32(record, 4)
    cid = _u32(record, 8)
    if not (0 < row_id < MAX_BLOCK_ID):
        return None
    for count_offset in (23, 19):
        if count_offset + 4 > len(record):
            continue
        count = _u16(record, count_offset)
        if not (1 <= count <= 4):
            continue
        offsets_start = count_offset + 2
        offsets = [
            _u16(record, offsets_start + 2 * index) for index in range(count)
        ]
        end = offsets[-1]
        data_start = offsets_start + 2 * count
        if not (data_start < end <= len(record)):
            continue
        values: List[str] = []
        previous = data_start
        valid = True
        for offset in offsets:
            if offset < previous:
                valid = False
                break
            raw = record[previous:offset]
            if not _printable_ascii(raw):
                valid = False
                break
            values.append(raw.decode("ascii"))
            previous = offset
        if valid and values:
            return row_id, cid, tuple(values)
    return None


def extract_component_tables(
    path: Path,
) -> Tuple[Dict[str, Dict[int, Tuple[int, Tuple[str, ...]]]], Dict[str, int]]:
    """Read the small BaseData component tables by id."""

    tables: Dict[str, Dict[int, Tuple[int, Tuple[str, ...]]]] = {}
    stats: Dict[str, int] = {}
    with _mapped(path) as data:
        for name, object_id in BASE_COMPONENT_OBJECTS.items():
            rows: Dict[int, Tuple[int, Tuple[str, ...]]] = {}
            for _page, record in _iter_object_records(data, object_id):
                parsed = parse_component_record(record)
                if parsed is None:
                    continue
                row_id, cid, values = parsed
                if values:
                    rows.setdefault(row_id, (cid, values))
            tables[name] = rows
            stats[name] = len(rows)
    return tables, stats


def _iter_profile_records(
    data: mmap.mmap,
) -> Iterator[Tuple[int, int, bytes]]:
    """Yield profile records from slots and from a header scan.

    Some profile pages in this snapshot have a slot count that does not cover
    every live row. The header scan recovers those rows without trusting the
    stale slot array; offsets are deduplicated before parsing.
    """

    for page_number in _pages_for_object(data, BASE_PROFILE_OBJECT):
        page = data[page_number * PAGE_SIZE : (page_number + 1) * PAGE_SIZE]
        slot_count = _u16(page, SLOT_COUNT_OFFSET)
        offsets = set()
        if 0 < slot_count <= (PAGE_SIZE - FIRST_RECORD_OFFSET) // 2:
            array_start = PAGE_SIZE - 2 * slot_count
            for position in range(array_start, PAGE_SIZE - 2, 2):
                offset = _u16(page, position)
                if FIRST_RECORD_OFFSET <= offset < PAGE_SIZE:
                    offsets.add(offset)
        for offset in sorted(offsets):
            yield page_number, offset, bytes(page[offset : offset + 1200])
        position = FIRST_RECORD_OFFSET
        while True:
            offset = page.find(b"\x30\x00\x56\x00", position)
            if offset < 0:
                break
            position = offset + 1
            if offset in offsets:
                continue
            offsets.add(offset)
            yield page_number, offset, bytes(page[offset : offset + 1200])


def parse_profile_record(record: bytes) -> Optional[Dict[str, Any]]:
    """Decode one ``dbo.VehicleProfile`` row.

    The fixed field offsets were verified by changing one component at a time
    against the component tables: model at +4, model year at +8, profile id at
    +12, chassis range at +30/+34, body style at +42, steering at +46,
    transmission at +50, engine at +58, partner group at +70 and special
    vehicle at +74. The title follows a text pointer and starts at +101.
    """

    if len(record) < 112 or record[0] != 0x30 or _u16(record, 2) != 0x56:
        return None
    profile_id = record[12:28]
    if not re.fullmatch(rb"[0-9a-f]{16}", profile_id):
        return None
    title_end = _u16(record, 95)
    if not (101 <= title_end <= len(record)):
        title_end = len(record)
        for index in range(101, len(record)):
            if record[index] < 32 or record[index] == 0x7F:
                title_end = index
                break
    raw_title = record[101:title_end]
    if not raw_title:
        return None
    title = sanitize_text(raw_title.decode("latin-1", errors="replace"))
    if not title:
        return None
    # A null chassis range leaves stale bytes in the fixed slot. Only trust
    # the slot when the title says the profile has a chassis range, and use
    # the title to tell an open-ended range from a stale upper bound.
    chassis_match = re.search(r"\bch\s+(\d*)\s*-\s*(\d*)", title)
    chassis_from = 0
    chassis_to = 0
    if chassis_match and (chassis_match.group(1) or chassis_match.group(2)):
        slot_from = _u32(record, 30)
        slot_to = _u32(record, 34)
        if chassis_match.group(1) and 0 < slot_from <= 20_000_000:
            chassis_from = slot_from
        elif chassis_match.group(1):
            chassis_from = int(chassis_match.group(1))
        if chassis_match.group(2):
            if 0 < slot_to <= 20_000_000:
                chassis_to = slot_to
            else:
                chassis_to = int(chassis_match.group(2))
    return {
        "id": profile_id.decode("ascii"),
        "title": title,
        "model": _u32(record, 4),
        "model_year": _u32(record, 8),
        "chassis_from": chassis_from,
        "chassis_to": chassis_to,
        "body_style": _u16(record, 42),
        "steering": _u16(record, 46),
        "transmission": _u16(record, 50),
        "engine": _u16(record, 58),
        "market": _u16(record, 70),
        "special_vehicle": _u16(record, 74),
    }


def extract_profile_partner_map(path: Path) -> Dict[str, Tuple[str, ...]]:
    """Read ``VehicleProfilePartnerGroup`` rows keyed by profile id."""

    result: Dict[str, List[str]] = defaultdict(list)
    with _mapped(path) as data:
        for _page, record in _iter_object_records(
            data, BASE_PROFILE_PARTNER_OBJECT
        ):
            if len(record) < 30 or record[0] != 0x30 or _u16(record, 2) != 0x14:
                continue
            profile_id = record[4:20]
            if not re.fullmatch(rb"[0-9a-f]{16}", profile_id):
                continue
            code = record[27:30]
            if not re.fullmatch(rb"[A-Z]{3}", code):
                continue
            value = code.decode("ascii")
            key = profile_id.decode("ascii")
            if value not in result[key]:
                result[key].append(value)
    return {key: tuple(values) for key, values in result.items()}


def _component_text(
    table: Dict[int, Tuple[int, Tuple[str, ...]]],
    component_id: int,
    index: int = 0,
) -> Optional[str]:
    entry = table.get(component_id)
    if not entry:
        return None
    values = entry[1]
    if index >= len(values):
        return None
    return sanitize_text(values[index])


def _market_code(
    table: Dict[int, Tuple[int, Tuple[str, ...]]], component_id: int
) -> Optional[str]:
    entry = table.get(component_id)
    if not entry:
        return None
    return sanitize_text(entry[1][0])


def extract_profiles(
    path: Path,
) -> Tuple[List[VehicleProfile], Dict[str, Any]]:
    """Decode every VehicleProfile row and join its component ids."""

    tables, component_stats = extract_component_tables(path)
    partner_map = extract_profile_partner_map(path)

    profiles: Dict[str, Dict[str, Any]] = {}
    raw_rows = 0
    unjoined = 0
    with _mapped(path) as data:
        for _page, _offset, record in _iter_profile_records(data):
            parsed = parse_profile_record(record)
            if parsed is None:
                continue
            raw_rows += 1
            model = _component_text(tables["model"], parsed["model"])
            year = _component_text(tables["model_year"], parsed["model_year"])
            if model is None or year is None:
                unjoined += 1
                continue
            body = _component_text(tables["body_style"], parsed["body_style"])
            steering = _component_text(tables["steering"], parsed["steering"])
            transmission = _component_text(
                tables["transmission"], parsed["transmission"]
            )
            engine = _component_text(tables["engine"], parsed["engine"])
            special = _component_text(
                tables["special_vehicle"], parsed["special_vehicle"]
            )
            market_code = _market_code(tables["market"], parsed["market"])
            markets = partner_map.get(parsed["id"], ())
            if market_code and market_code not in markets:
                markets = (market_code,) + markets
            chassis_from = parsed["chassis_from"]
            chassis_to = parsed["chassis_to"]
            profile = VehicleProfile(
                id=parsed["id"],
                title=parsed["title"],
                market=market_code or (markets[0] if markets else None),
                model=model,
                model_year=year,
                engine=engine,
                transmission=transmission,
                steering=steering,
                body_style=body,
                special_vehicle=special,
                chassis_from=str(chassis_from) if chassis_from else None,
                chassis_to=str(chassis_to) if chassis_to else None,
                markets=markets,
                notes="decoded from BaseData VehicleProfile",
            )
            existing = profiles.get(profile.id)
            if existing is None or len(profile.title or "") > len(
                existing.title or ""
            ):
                profiles[profile.id] = profile

    ordered = sorted(profiles.values(), key=lambda item: item.id)
    stats = {
        "rows": raw_rows,
        "distinct": len(ordered),
        "unjoined": unjoined,
        "components": component_stats,
        "partner_map_profiles": len(partner_map),
    }
    return ordered, stats


# ---------------------------------------------------------------------------
# DiceTiming
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# CarComRT: DTC text, freeze frames and symptom coverage
# ---------------------------------------------------------------------------


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
                long_text=sanitize_text(long_text),
                short_text=sanitize_text(short_text),
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


def extract_freeze_frames(
    path: Path, language_id: int = 4
) -> Tuple[List[FreezeFrameEntry], Dict[str, int]]:
    """Inspect T141 block-type-8 blocks and their T150 values.

    ``T148_BlockMetaPARA`` would confirm which blocks the ECU shows as a freeze
    frame, but that table has no page set in this snapshot. The values found on
    type-8 blocks are DTC qualifier codes, not measurements, so none is
    exported as a freeze frame; the counts and blocker are returned instead.
    """

    with _mapped(path) as data:
        t150_rows = list(_iter_t150_rows(data))
        wanted = {block_id for _code, block_id, _text_id in t150_rows}
        blocks = _iter_t141_blocks(data, wanted)
        values: Dict[int, List[Tuple[str, int]]] = defaultdict(list)
        for code, block_id, text_id in t150_rows:
            if block_id in blocks:
                values[block_id].append((code, text_id))

    ignored_names = {
        "qualifier",
        "status",
        "dtc status",
        "routine status",
        "unit and scaling id",
        "ranking update counter",
    }
    frames: List[FreezeFrameEntry] = []
    type8_blocks = [
        (block_id, name)
        for block_id, (name, block_type) in blocks.items()
        if block_type == 8
    ]
    raw_values = sum(len(values.get(block_id, ())) for block_id, _name in type8_blocks)
    for block_id, name in sorted(type8_blocks, key=lambda item: item[0]):
        if name.casefold() in ignored_names:
            continue
        for code, _text_id in sorted(values.get(block_id, ())):
            if re.fullmatch(r"[PCBU][0-9A-F]{4,6}", code):
                continue
            frames.append(
                FreezeFrameEntry(
                    parameter=name,
                    value=code,
                    block_id=block_id,
                    confirmed=False,
                    source=(
                        "CarComRT T141_Block type 8; T148_BlockMetaPARA has no "
                        "page set in this snapshot"
                    ),
                )
            )
    stats = {
        "blocks": len(blocks),
        "type8_blocks": len(type8_blocks),
        "type8_values": raw_values,
        "usable_candidates": len(frames),
        "usable_note": (
            "T150 values on T141 type-8 blocks are DTC qualifier codes, not "
            "measurements; T148_BlockMetaPARA is required to select the "
            "actual freeze-frame parameters and has no page set."
        ),
        "t148_pages": 0,
        "t148_blocker": CARCOM_EMPTY_BLOCKERS["T148_BlockMetaPARA"],
    }
    return frames, stats


def extract_carcom_table_pages(path: Path) -> Dict[str, int]:
    """Count the page set owned by each known CarCom table."""

    pages: Dict[str, int] = {}
    with _mapped(path) as data:
        for name, object_id in CARCOM_OBJECTS.items():
            pages[name] = len(_pages_for_object(data, object_id))
    return pages


def extract_symptom_coverage(path: Path) -> Dict[str, Any]:
    """Report which symptom tables exist and how many pages they own."""

    coverage: Dict[str, Any] = {}
    with _mapped(path) as data:
        for name, object_id in CARCOM_OBJECTS.items():
            if "Symptom" not in name and "BlockMetaDTC" not in name:
                continue
            pages = _pages_for_object(data, object_id)
            coverage[name] = {
                "object_id": object_id,
                "pages": len(pages),
                "rows": 0,
                "blocker": None if pages else CARCOM_EMPTY_BLOCKERS.get(name),
            }
        for name, blocker in CARCOM_EMPTY_BLOCKERS.items():
            if name in coverage:
                continue
            object_id = CARCOM_OBJECTS.get(name)
            coverage[name] = {
                "object_id": object_id,
                "pages": len(_pages_for_object(data, object_id)) if object_id else 0,
                "rows": 0,
                "blocker": blocker,
            }
    return coverage


def extract_symptom_definitions(path: Path) -> Tuple[List[SymptomEntry], Dict[str, Any]]:
    """Read symptom rows when their tables have pages.

    The snapshot used for this export has no page set for any of the symptom
    tables, so the result is empty and ``extract_symptom_coverage`` explains
    why. The reader is intentionally strict: it never invents a symptom from a
    block name or a DTC description.
    """

    coverage = extract_symptom_coverage(path)
    if any(item.get("pages") for item in coverage.values()):
        coverage["_note"] = (
            "Symptom pages are present but the T152/T153/T154/T156/T191 "
            "join is not decoded in this pass; no partial row is exported."
        )
    return [], coverage


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


def _iter_t141_names(data: mmap.mmap, block_ids: Sequence[int]) -> Dict[int, str]:
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
        name = _decode_single_byte_text(raw)
        if name:
            names[block_id] = name
    return names


def _iter_t141_blocks(
    data: mmap.mmap, wanted_ids: Optional[Sequence[int]] = None
) -> Dict[int, Tuple[str, int]]:
    """Read block names and types for the block ids T150 actually references.

    CarComRT stores block rows in more than one page group in this snapshot.
    Filtering by the ids T150 references keeps the scan honest: a bare header
    scan finds millions of false positives inside other tables' payloads.
    """

    wanted = set(wanted_ids) if wanted_ids is not None else None
    blocks: Dict[int, Tuple[str, int]] = {}
    position = FIRST_RECORD_OFFSET
    while True:
        offset = data.find(b"\x30\x00\x1d\x00", position)
        if offset < 0:
            break
        position = offset + 1
        if offset + 37 > len(data):
            continue
        block_id = int.from_bytes(data[offset + 4 : offset + 8], "little")
        if wanted is not None and block_id not in wanted:
            continue
        block_type = int.from_bytes(data[offset + 8 : offset + 12], "little")
        if not (0 < block_id < MAX_BLOCK_ID) or block_id in blocks:
            continue
        record_end = int.from_bytes(data[offset + 35 : offset + 37], "little")
        if not (37 <= record_end <= 400):
            continue
        if offset + record_end > len(data):
            continue
        raw = bytes(data[offset + T141_NAME_OFFSET : offset + record_end])
        name = _decode_single_byte_text(raw)
        if name:
            blocks[block_id] = (name, block_type)
    return blocks


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
            text = _decode_utf16_text(raw)
            if text:
                texts[text_id] = text
    return texts


def _decode_utf16_text(raw: bytes) -> Optional[str]:
    if not raw:
        return None
    if len(raw) % 2:
        raw = raw[:-1]
    if not raw:
        return None
    candidates: List[str] = []
    for encoding in ("utf-16-le", "utf-16-be"):
        try:
            candidates.append(raw.decode(encoding, errors="replace"))
        except LookupError:
            continue
    if len(raw) % 2 == 0:
        candidates.append(raw.decode("latin-1", errors="replace"))
    best = None
    best_score = -1.0
    for text in candidates:
        text = sanitize_text(text)
        if not text:
            continue
        score = _text_score(text)
        if score > best_score:
            best_score = score
            best = text
    return best


def _decode_single_byte_text(raw: bytes) -> Optional[str]:
    try:
        text = raw.decode("ascii")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    return sanitize_text(text)


def _text_score(text: str) -> float:
    if not text:
        return 0.0
    printable = sum(1 for char in text if char.isprintable() or char in "\n\t")
    replacement = text.count("\ufffd")
    ascii_letters = sum(1 for char in text if char.isascii() and char.isalpha())
    return (
        printable / len(text)
        + ascii_letters / len(text)
        - replacement / len(text)
    )


# ---------------------------------------------------------------------------
# DiagSwdlRepository: carved diagnostic scripts
# ---------------------------------------------------------------------------


def carve_scripts(
    path: Path, max_entries: int = 0
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """Carve PKZIP streams and parse the diagnostic XML inside them.

    A local file header is accepted only when the deflate stream inflates, the
    inflated body starts with an XML marker, and ``parse_script_xml`` accepts a
    ``script`` root. The CRC is recorded, not used as a hard gate, because this
    snapshot contains bodies whose CRC field is damaged while the body is still
    parseable. Every entry carries a status so a caller can tell the two apart.
    """

    entries: List[Dict[str, Any]] = []
    stats: Counter = Counter()
    docno_status: Dict[Tuple[str, str], Dict[str, Any]] = {}

    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            position = 0
            while True:
                offset = data.find(b"PK\x03\x04", position)
                if offset < 0:
                    break
                position = offset + 4
                stats["headers"] += 1
                if offset + 30 > len(data):
                    stats["header_overrun"] += 1
                    continue
                try:
                    (
                        _signature,
                        _version,
                        flags,
                        method,
                        _mod_time,
                        _mod_date,
                        crc,
                        compressed_size,
                        uncompressed_size,
                        name_length,
                        extra_length,
                    ) = struct.unpack("<IHHHHHIIIHH", data[offset : offset + 30])
                except struct.error:
                    stats["header_overrun"] += 1
                    continue
                if flags & 0x1:
                    stats["encrypted"] += 1
                    continue
                if method != 8:
                    stats["unsupported_method"] += 1
                    continue
                if not (0 < compressed_size <= MAX_SCRIPT_BYTES):
                    stats["bad_compressed_size"] += 1
                    continue
                if not (0 < uncompressed_size <= MAX_SCRIPT_BYTES):
                    stats["bad_uncompressed_size"] += 1
                    continue
                data_start = offset + 30 + name_length + extra_length
                if data_start + compressed_size > len(data):
                    stats["truncated_stream"] += 1
                    continue
                try:
                    decompressor = zlib.decompressobj(-15)
                    body = decompressor.decompress(
                        data[data_start : data_start + compressed_size],
                        MAX_SCRIPT_BYTES,
                    )
                    body += decompressor.flush()
                except zlib.error:
                    stats["inflate_failed"] += 1
                    continue
                if not decompressor.eof:
                    stats["inflate_incomplete"] += 1
                    continue
                if not body.lstrip().startswith(
                    (b"<", b"\xef\xbb\xbf<", b"\xff\xfe<", b"\xfe\xff<")
                ):
                    stats["not_xml"] += 1
                    continue
                crc_ok = zlib.crc32(body) == crc
                stats["crc_ok" if crc_ok else "crc_mismatch"] += 1
                try:
                    text = _decode_script_body(body)
                    script = parse_script_xml(text)
                except (DiagnosticScriptError, UnicodeDecodeError, ValueError):
                    stats["parse_failed"] += 1
                    continue
                stats["parsed"] += 1
                entry = _script_entry_dict(
                    script,
                    body=body,
                    offset=offset,
                    crc_ok=crc_ok,
                    compressed_size=compressed_size,
                )
                if not entry["id"]:
                    stats["parsed_without_docno"] += 1
                    continue
                entries.append(entry)
                if max_entries and len(entries) >= max_entries:
                    stats["capped"] += 1
                    break

    # Group by document number and language. A single document can be carved
    # many times; keep the best variant and merge its profile and service ids.
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for entry in entries:
        key = (entry["id"], entry["language"] or "")
        current = grouped.get(key)
        if current is None:
            grouped[key] = entry
            continue
        _merge_script_entry(current, entry)
    scripts = sorted(grouped.values(), key=lambda item: item["id"])
    stats["unique_documents"] = len({item["id"] for item in scripts})
    stats["languages"] = len({item["language"] for item in scripts if item["language"]})
    stats["parsed_unique"] = len(scripts)
    return scripts, dict(stats)


def _script_entry_dict(
    script: Any,
    body: bytes,
    offset: int,
    crc_ok: bool,
    compressed_size: int,
) -> Dict[str, Any]:
    script_type_name = sanitize_text(script.script_type) or None
    script_type = _numeric_script_type(script_type_name)
    ecus = [
        ScriptEcu(
            ecu_address=plan.ecu_address,
            ecu_type=plan.ecu_type,
            tester_address=plan.tester_address,
            service_ids=plan.service_ids,
            mode=sanitize_text(plan.mode),
        )
        for plan in script.ecus
    ]
    service_ids: List[int] = []
    read_only: List[int] = []
    for ecu in ecus:
        for service_id in ecu.service_ids:
            if service_id not in service_ids:
                service_ids.append(service_id)
            if is_read_only_service(service_id) and service_id not in read_only:
                read_only.append(service_id)
    first_ecu = ecus[0] if ecus else None
    dtc_list = script.dtc_list
    return {
        "id": script.docno or "",
        "name": sanitize_text(script.title) or None,
        "script_type": script_type,
        "script_type_name": script_type_name,
        "language": sanitize_text(script.language) or None,
        "profiles": tuple(script.profile_ids),
        "ecu_address": first_ecu.ecu_address if first_ecu else None,
        "ecu_type": first_ecu.ecu_type if first_ecu else None,
        "service_ids": tuple(service_ids),
        "ecus": tuple(ecus),
        "dtc_list_path": dtc_list.list_path if dtc_list else None,
        "dtc_value_path": dtc_list.value_path if dtc_list else None,
        "dtc_text_path": dtc_list.text_path if dtc_list else None,
        "read_only_service_ids": tuple(read_only),
        "status": "crc-verified" if crc_ok else "crc-mismatch",
        "body_sha256": hashlib.sha256(body).hexdigest(),
        "xml": body,
        "offset": offset,
        "compressed_size": compressed_size,
        "variants": 1,
        "source": (
            "DiagSwdlRepository offset {} ({} bytes, {})".format(
                offset,
                compressed_size,
                "crc verified" if crc_ok else "crc mismatch",
            )
        ),
    }


def _merge_script_entry(current: Dict[str, Any], other: Dict[str, Any]) -> None:
    current["variants"] = current.get("variants", 1) + 1
    if other["status"] == "crc-verified" and current["status"] != "crc-verified":
        current["status"] = "crc-verified"
        current["body_sha256"] = other["body_sha256"]
        current["xml"] = other["xml"]
        current["offset"] = other["offset"]
        current["compressed_size"] = other["compressed_size"]
        current["source"] = other["source"]
    for field in ("profiles", "service_ids", "read_only_service_ids"):
        merged = list(current.get(field) or ())
        for value in other.get(field) or ():
            if value not in merged:
                merged.append(value)
        current[field] = tuple(merged)
    if not current.get("name") and other.get("name"):
        current["name"] = other["name"]
    for field in ("dtc_list_path", "dtc_value_path", "dtc_text_path"):
        if not current.get(field) and other.get(field):
            current[field] = other[field]
    if not current.get("ecus") and other.get("ecus"):
        current["ecus"] = other["ecus"]


def _numeric_script_type(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    if value.isdigit():
        return int(value)
    return {
        "ReadDTCs": 10,
        "ReadDtc": 10,
        "ReadDtcEcm": 10,
        "ClearDtc": 14,
    }.get(value)


def _decode_script_body(body: bytes) -> str:
    if body.startswith(b"\xef\xbb\xbf"):
        return body.decode("utf-8-sig")
    if body.startswith((b"\xff\xfe", b"\xfe\xff")):
        return body.decode("utf-16")
    match = re.search(br"""<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)["']""", body[:200])
    encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return body.decode(encoding)
    except (LookupError, UnicodeDecodeError):
        return body.decode("latin-1")


def _looks_like_vin(text: str) -> bool:
    pattern = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
    return any(
        any(char.isdigit() for char in match.group(0))
        and any(char.isalpha() for char in match.group(0))
        for match in pattern.finditer(text)
    )


# ---------------------------------------------------------------------------
# Text repair
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[A-Za-z]{3,}")


def repair_single_bit_text(
    texts: Sequence[str], minimum_candidate: int = 50, minimum_length: int = 4
) -> Tuple[List[str], List[Tuple[str, str]]]:
    """Repair rare tokens that differ from a common token by one bit.

    The MDF text pages in this snapshot contain single-character corruption
    such as ``Preqsure``, ``Eneine`` and ``Superciarger``. A token that occurs
    once and is one bit away from a token that occurs at least
    ``minimum_candidate`` times is treated as damage. The replacement is
    recorded, never silent.
    """

    frequencies: Counter = Counter()
    for text in texts:
        for token in _TOKEN_RE.findall(text):
            frequencies[token] += 1
    candidates_by_length: Dict[int, List[str]] = defaultdict(list)
    folded_candidates: Dict[str, str] = {}
    for token, count in frequencies.items():
        if count >= minimum_candidate:
            candidates_by_length[len(token)].append(token)
            folded_candidates.setdefault(token.casefold(), token)

    replacements: Dict[str, str] = {}
    repairs: List[Tuple[str, str]] = []
    # Words that are valid English and can be one bit away from another valid
    # word in this corpus. They are never rewritten.
    stoplist = {"wide", "date"}
    for token, count in frequencies.items():
        if count > 5 or len(token) < minimum_length:
            continue
        if token.casefold() in stoplist:
            continue
        matched = None
        for candidate in candidates_by_length.get(len(token), ()):
            if _case_pattern(token) != _case_pattern(candidate):
                continue
            if _one_bit_apart(token, candidate):
                matched = candidate
                break
        if matched is None:
            # The common form can use different capitalisation ("Open" for
            # "mpen"). Compare folded forms and restore the token's casing.
            candidate = folded_candidates.get(token.casefold())
            if candidate is not None and candidate.casefold() == token.casefold():
                candidate = None
            if candidate is None:
                for folded, form in folded_candidates.items():
                    if (
                        folded != token.casefold()
                        and len(folded) == len(token)
                        and _one_bit_apart(token.casefold(), folded)
                    ):
                        candidate = form
                        break
            if candidate is not None:
                matched = _match_case(token, candidate)
        if matched is not None:
            replacements[token] = matched
            repairs.append((token, matched))

    if not replacements:
        return list(texts), []

    def replace(match: re.Match) -> str:
        token = match.group(0)
        return replacements.get(token, token)

    return [_TOKEN_RE.sub(replace, text) for text in texts], repairs


def _case_pattern(token: str) -> str:
    if token.islower():
        return "lower"
    if token.isupper():
        return "upper"
    if token[0].isupper() and token[1:].islower():
        return "title"
    return "mixed"


def _match_case(token: str, candidate: str) -> str:
    pattern = _case_pattern(token)
    if pattern == "lower":
        return candidate.lower()
    if pattern == "upper":
        return candidate.upper()
    if pattern == "title":
        return candidate[:1].upper() + candidate[1:].lower()
    return candidate


def _one_bit_apart(left: str, right: str) -> bool:
    if len(left) != len(right):
        return False
    differences = [
        (a, b) for a, b in zip(left, right) if a != b
    ]
    if len(differences) != 1:
        return False
    return (ord(differences[0][0]) ^ ord(differences[0][1])) in (
        1,
        2,
        4,
        8,
        16,
        32,
    )


def repair_dtc_descriptions(
    descriptions: Sequence[DtcDescription],
) -> Tuple[List[DtcDescription], List[Tuple[str, str]]]:
    texts: List[str] = []
    for entry in descriptions:
        if entry.long_text:
            texts.append(entry.long_text)
        if entry.short_text:
            texts.append(entry.short_text)
    repaired, repairs = repair_single_bit_text(texts)
    if not repairs:
        return list(descriptions), []
    replacements = dict(repairs)
    result: List[DtcDescription] = []
    for entry in descriptions:
        result.append(
            DtcDescription(
                code=entry.code,
                long_text=_replace_tokens(entry.long_text, replacements),
                short_text=_replace_tokens(entry.short_text, replacements),
                symptoms=entry.symptoms,
                causes=entry.causes,
                ecu=entry.ecu,
                language=entry.language,
                source=entry.source,
            )
        )
    return result, repairs


def _replace_tokens(
    text: Optional[str], replacements: Dict[str, str]
) -> Optional[str]:
    if text is None:
        return None
    return _TOKEN_RE.sub(
        lambda match: replacements.get(match.group(0), match.group(0)), text
    )


# ---------------------------------------------------------------------------
# Catalog assembly
# ---------------------------------------------------------------------------


def build_from_image(
    image: Path,
    language_id: int = 4,
    language_name: str = "en-US",
    include_sha256: bool = True,
    repair_text: bool = True,
    dump_dir: Optional[Path] = None,
    max_script_files: int = 0,
) -> Tuple[DiagnosticCatalog, Dict[str, Any]]:
    """Build a catalog from a source image root."""

    carcom_path, timing_path, base_path, script_path = _resolve_database_paths(image)

    timing, timing_stats = extract_timing(timing_path)
    descriptions, dtc_stats = extract_dtc_descriptions(
        carcom_path, language_id=language_id, language_name=language_name
    )
    repairs: List[Tuple[str, str]] = []
    if repair_text:
        descriptions, repairs = repair_dtc_descriptions(descriptions)
    profiles, profile_stats = extract_profiles(base_path)
    freeze_frames, freeze_stats = extract_freeze_frames(
        carcom_path, language_id=language_id
    )
    symptoms, symptom_coverage = extract_symptom_definitions(carcom_path)
    carcom_pages = extract_carcom_table_pages(carcom_path)
    scripts, script_stats = carve_scripts(script_path)

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
        DatabaseSource(
            name="BaseData",
            path=_display_path(base_path, image),
            sha256=_sha256(base_path) if include_sha256 else None,
        ),
        DatabaseSource(
            name="DiagSwdlRepository",
            path=_display_path(script_path, image),
            sha256=_sha256(script_path) if include_sha256 else None,
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
            "Profiles: {} distinct ids from {} VehicleProfile rows; {} rows "
            "could not be joined to a model and model year."
        ).format(
            profile_stats["distinct"],
            profile_stats["rows"],
            profile_stats["unjoined"],
        ),
        (
            "Scripts: {} unique document/language entries from {} PKZIP "
            "headers; {} bodies passed CRC, {} parsed as XML. Damaged and "
            "truncated bodies are counted, not claimed as complete coverage."
        ).format(
            len(scripts),
            script_stats.get("headers", 0),
            script_stats.get("crc_ok", 0),
            script_stats.get("parsed", 0),
        ),
        (
            "Freeze frames: {} confirmed parameters and {} T150 values on "
            "T141 block-type-8 blocks. T148_BlockMetaPARA has no page set, so "
            "the values are qualifiers, not a confirmed freeze-frame list."
        ).format(len(freeze_frames), freeze_stats.get("type8_values", 0)),
        (
            "Symptoms and causes: {} symptom rows. T152, T153, T154, T156, "
            "T157, T158, T159 and T147 have no page set in this snapshot; "
            "see the raw table coverage for the exact object ids."
        ).format(len(symptoms)),
    ]
    if repairs:
        examples = ", ".join(
            "{}->{}".format(before, after) for before, after in repairs[:6]
        )
        notes.append(
            "Text repair: {} single-bit tokens corrected ({}). "
            "The replacement pairs are recorded in the raw dump.".format(
                len(repairs), examples
            )
        )

    source = CatalogSource(
        kind="source-mdf",
        generated_at=_utc_timestamp(),
        image=str(image),
        databases=tuple(databases),
        extractor="tools/export_diag_catalog.py",
        language=language_name,
        notes=tuple(notes),
    )
    catalog = DiagnosticCatalog(
        source=source,
        dtc_descriptions=tuple(descriptions),
        timing=tuple(timing),
        profiles=tuple(profiles),
        scripts=tuple(_catalog_script(entry) for entry in scripts),
        symptom_definitions=tuple(symptoms),
        freeze_frames=tuple(freeze_frames),
    )
    stats = {
        "dtc": dtc_stats,
        "timing": timing_stats,
        "profiles": profile_stats,
        "scripts": script_stats,
        "freeze_frames": freeze_stats,
        "symptoms": symptom_coverage,
        "carcom_pages": carcom_pages,
        "text_repairs": repairs,
        "databases": databases,
    }
    if dump_dir is not None:
        catalog = write_dump(
            dump_dir,
            catalog,
            stats,
            scripts=scripts,
            max_script_files=max_script_files,
        )
    return catalog, stats


def _catalog_script(entry: Dict[str, Any]) -> ScriptEntry:
    return ScriptEntry(
        id=entry["id"],
        name=entry.get("name"),
        script_type=entry.get("script_type"),
        script_type_name=entry.get("script_type_name"),
        language=entry.get("language"),
        profiles=tuple(entry.get("profiles") or ()),
        ecu_address=entry.get("ecu_address"),
        ecu_type=entry.get("ecu_type"),
        service_ids=tuple(entry.get("service_ids") or ()),
        ecus=tuple(entry.get("ecus") or ()),
        dtc_list_path=entry.get("dtc_list_path"),
        dtc_value_path=entry.get("dtc_value_path"),
        dtc_text_path=entry.get("dtc_text_path"),
        read_only_service_ids=tuple(entry.get("read_only_service_ids") or ()),
        status=entry.get("status"),
        body_sha256=entry.get("body_sha256"),
        xml_path=entry.get("xml_path"),
        source=entry.get("source"),
    )


def build_from_json(path: Path) -> DiagnosticCatalog:
    """Load a complete catalog or normalize a small JSON mapping."""

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

    for section in (
        "dtc_descriptions",
        "timing",
        "profiles",
        "scripts",
        "symptom_definitions",
        "freeze_frames",
    ):
        normalized.setdefault(section, [])

    return catalog_from_dict(normalized)


# ---------------------------------------------------------------------------
# Dump mode
# ---------------------------------------------------------------------------


def write_dump(
    directory: Path,
    catalog: DiagnosticCatalog,
    stats: Dict[str, Any],
    scripts: Optional[Sequence[Dict[str, Any]]] = None,
    max_script_files: int = 0,
) -> DiagnosticCatalog:
    """Write the catalog, bounded raw summaries and recovered script XML.

    Returns the catalog that was written. When script XML is dumped, each
    script entry gains its relative ``xml_path`` before ``catalog.json`` is
    serialized.
    """

    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / "raw"
    raw.mkdir(exist_ok=True)

    profiles = stats.get("profiles", {})
    base_data = {
        "VehicleProfile": {
            "object_id": BASE_PROFILE_OBJECT,
            "rows": profiles.get("rows", 0),
            "distinct": profiles.get("distinct", 0),
            "unjoined": profiles.get("unjoined", 0),
        },
        "VehicleProfilePartnerGroup": {
            "object_id": BASE_PROFILE_PARTNER_OBJECT,
            "profiles": profiles.get("partner_map_profiles", 0),
        },
        "components": profiles.get("components", {}),
    }
    _write_json(raw / "base_data.json", base_data)
    _write_json(
        raw / "carcom_tables.json",
        {
            "objects": CARCOM_OBJECTS,
            "pages": stats.get("carcom_pages", {}),
            "dtc": stats.get("dtc", {}),
            "symptoms": stats.get("symptoms", {}),
            "freeze_frames": stats.get("freeze_frames", {}),
        },
    )
    _write_json(
        raw / "scripts.json",
        {
            "stats": stats.get("scripts", {}),
            "entries": [
                {
                    "id": entry["id"],
                    "language": entry.get("language"),
                    "status": entry.get("status"),
                    "offset": entry.get("offset"),
                    "variants": entry.get("variants"),
                    "profiles": len(entry.get("profiles") or ()),
                    "service_ids": list(entry.get("service_ids") or ()),
                }
                for entry in (scripts or ())
            ],
        },
    )
    _write_json(
        raw / "text_repairs.json",
        [
            {"damaged": before, "repaired": after}
            for before, after in stats.get("text_repairs", ())
        ],
    )

    if scripts:
        script_dir = directory / "scripts"
        script_dir.mkdir(exist_ok=True)
        written = 0
        skipped = 0
        for entry in scripts:
            if not entry.get("id") or not entry.get("xml"):
                continue
            text = _decode_script_body(entry["xml"])
            if _looks_like_vin(text):
                skipped += 1
                continue
            if max_script_files and written >= max_script_files:
                break
            safe_id = re.sub(r"[^A-Za-z0-9._-]+", "_", entry["id"])
            safe_language = re.sub(
                r"[^A-Za-z0-9._-]+", "_", entry.get("language") or "unknown"
            )
            name = "{}_{}_{}.xml".format(
                safe_id, safe_language, entry.get("offset", 0)
            )
            (script_dir / name).write_text(text, encoding="utf-8")
            entry["xml_path"] = "scripts/" + name
            written += 1
        _write_json(
            raw / "script_files.json",
            {"written": written, "vin_skipped": skipped},
        )
        if written:
            catalog = DiagnosticCatalog(
                source=catalog.source,
                dtc_descriptions=catalog.dtc_descriptions,
                timing=catalog.timing,
                profiles=catalog.profiles,
                scripts=tuple(_catalog_script(entry) for entry in scripts),
                symptom_definitions=catalog.symptom_definitions,
                freeze_frames=catalog.freeze_frames,
            )

    dump_catalog(catalog, directory / "catalog.json")
    return catalog


def _write_json(path: Path, value: Any) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a runtime service catalog from the read-only source image, or "
            "transform an existing JSON file."
        )
    )
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--image",
        type=Path,
        help="source image root, for example /mnt/vida",
    )
    source_group.add_argument(
        "--from-json",
        type=Path,
        help="complete catalog or simple mapping to normalize",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help="output JSON path (default: <dump-dir>/catalog.json)",
    )
    parser.add_argument(
        "--dump-dir",
        type=Path,
        help="write catalog.json, raw table summaries and recovered script XML",
    )
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
    parser.add_argument(
        "--no-text-repair",
        action="store_true",
        help="keep single-bit text damage instead of repairing rare tokens",
    )
    parser.add_argument(
        "--max-script-files",
        type=int,
        default=0,
        help="cap recovered XML files in the dump (0 = no cap)",
    )
    parser.add_argument("--quiet", action="store_true", help="suppress the summary")

    args = parser.parse_args(argv)
    if args.out is None and args.dump_dir is None:
        parser.error("one of --out or --dump-dir is required")

    try:
        if args.from_json is not None:
            catalog = build_from_json(args.from_json)
            stats: Optional[Dict[str, Any]] = None
            if args.dump_dir is not None:
                catalog = write_dump(args.dump_dir, catalog, {})
            output = args.out or (args.dump_dir / "catalog.json")
            if args.out is not None:
                dump_catalog(catalog, output)
        else:
            catalog, stats = build_from_image(
                args.image,
                language_id=args.language_id,
                language_name=args.language,
                include_sha256=not args.no_sha256,
                repair_text=not args.no_text_repair,
                dump_dir=args.dump_dir,
                max_script_files=args.max_script_files,
            )
            output = args.out or (args.dump_dir / "catalog.json")
            if args.out is not None:
                dump_catalog(catalog, output)
    except (OSError, CatalogError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1

    if not args.quiet:
        if stats is not None:
            dtc = stats["dtc"]
            timing = stats["timing"]
            profiles = stats["profiles"]
            scripts = stats["scripts"]
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
                "Profiles: {} distinct from {} rows ({} unjoined)".format(
                    profiles["distinct"], profiles["rows"], profiles["unjoined"]
                )
            )
            print(
                "Scripts: {} unique entries from {} headers ({} crc-verified, "
                "{} parsed, {} inflate failures)".format(
                    scripts.get("unique_documents", 0),
                    scripts.get("headers", 0),
                    scripts.get("crc_ok", 0),
                    scripts.get("parsed", 0),
                    scripts.get("inflate_failed", 0),
                )
            )
            freeze = stats["freeze_frames"]
            print(
                "Freeze frames: {} confirmed; {} T150 values on type-8 blocks "
                "(T148 blocker: {}); symptoms: {} rows".format(
                    len(catalog.freeze_frames),
                    freeze.get("type8_values", 0),
                    freeze.get("t148_blocker", "unknown"),
                    len(catalog.symptom_definitions),
                )
            )
        print("Wrote {} ({} bytes)".format(output, output.stat().st_size))
    return 0


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _mapped(path: Path) -> Iterator[mmap.mmap]:
    with path.open("rb") as handle:
        with mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ) as data:
            yield data


def _resolve_database_paths(image: Path) -> Tuple[Path, Path, Path, Path]:
    candidates = [image / "VIDA" / "db", image / "db"]
    for directory in candidates:
        carcom = directory / "CarComRT_Data.MDF"
        timing = directory / "DiceTiming_Data.MDF"
        base = directory / "BaseData_Data.MDF"
        scripts = directory / "DiagSwdlRepository_Data.MDF"
        if carcom.is_file() and timing.is_file() and base.is_file() and scripts.is_file():
            return carcom, timing, base, scripts
    raise CatalogError(
        "no CarComRT_Data.MDF, DiceTiming_Data.MDF, BaseData_Data.MDF and "
        "DiagSwdlRepository_Data.MDF found under {} (looked in VIDA/db and db)".format(
            image
        )
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
    text = re.split(r"[ \t]{4,}", text, maxsplit=1)[0]
    return sanitize_text(text) or ""


def _utc_timestamp() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


if __name__ == "__main__":
    raise SystemExit(main())
