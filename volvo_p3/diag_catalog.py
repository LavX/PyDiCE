"""Runtime catalog of diagnostic data.

The source keeps DTC text, request timing and vehicle profiles in separate SQL
Server databases. This module loads a small, versioned JSON export of the
parts a diagnostic client can use, and answers the three questions PyDiCE
asks at runtime:

* What does this DTC mean, on this ECU?
* How long should a request to this ECU variant wait?
* Which profiles and scripts does the catalog know about?

The catalog file is JSON only. It has seven sections:

.. code-block:: json

    {
      "schema": "pydice.diag_catalog",
      "version": 2,
      "source": {
        "kind": "source-mdf",
        "generated_at": "2026-10-21T12:00:00Z",
        "image": "/mnt/vida",
        "databases": [
          {"name": "CarComRT", "path": "VIDA/db/CarComRT_Data.MDF",
           "sha256": "..."}
        ],
        "notes": ["..."]
      },
      "dtc_descriptions": [
        {
          "code": "P034211",
          "ecu": "ECM",
          "long_text": "Camshaft Position Sensor A Circuit Low ...",
          "short_text": "Camshaft Position Sensor A Circuit Low",
          "symptoms": ["..."],
          "causes": ["..."],
          "language": "en-US",
          "source": "CarComRT T150_BlockValue/T191_TextData"
        }
      ],
      "timing": [
        {
          "ecu_variant": 329,
          "request": "AE1B",
          "message_timing_id": 1,
          "timeout_and_resend_id": 0,
          "p1max": 40,
          "p3min": 110,
          "p4min": 10,
          "timeout_ms": 3000,
          "resend": 0,
          "comment": "SRS X40 01-04"
        }
      ],
      "profiles": [
        {
          "id": "0b00c8af80206a61",
          "title": "S40 (-04) 1999 B4164S",
          "model": "S40 (-04)",
          "model_year": "1999",
          "engine": "B4164S",
          "markets": ["EUR"]
        }
      ],
      "scripts": [
        {
          "id": "VCC-227144",
          "name": "Read DTCs",
          "script_type": 10,
          "script_type_name": "ReadDTCs",
          "profiles": ["0b00c8af80206a61"],
          "ecus": [{"ecu_address": 17, "service_ids": [5]}],
          "status": "crc-verified"
        }
      ],
      "symptom_definitions": [],
      "freeze_frames": []
    }

A DTC entry with ``"ecu": null`` or no ``ecu`` key is global. A caller that
passes an ECU name gets an exact ECU entry first and falls back to the global
entry. If the catalog has several different descriptions for the same code
and the caller did not narrow them by ECU, the lookup returns ``None`` rather
than choosing one. A missing code always returns ``None``; the loader never
synthesises text.

The exporter in ``tools/export_diag_catalog.py`` builds this document from the
source image, or transforms an existing JSON document with ``--from-json``.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union


SCHEMA_NAME = "pydice.diag_catalog"
SCHEMA_VERSION = 2
SUPPORTED_VERSIONS = (1, 2)

_HEX_DIGITS = frozenset("0123456789ABCDEF")
_DTC_LETTERS = "PCBU"


class CatalogError(ValueError):
    """Raised when a catalog document is malformed or unsupported."""


@dataclass(frozen=True)
class DatabaseSource:
    """One database file that contributed to a catalog."""

    name: str
    path: str
    sha256: Optional[str] = None


@dataclass(frozen=True)
class CatalogSource:
    """Where a catalog came from and how it was produced."""

    kind: str
    generated_at: Optional[str] = None
    image: Optional[str] = None
    databases: Tuple[DatabaseSource, ...] = ()
    extractor: Optional[str] = None
    language: Optional[str] = None
    notes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class DtcDescription:
    """A service-style DTC description.

    ``ecu`` is a free-form ECU label. the source calls some of these ``ECM`` or
    ``TCM``; the exporter may also use a vendor-specific label. ``None`` means
    the description is not ECU-specific.
    """

    code: str
    long_text: Optional[str] = None
    short_text: Optional[str] = None
    symptoms: Tuple[str, ...] = ()
    causes: Tuple[str, ...] = ()
    ecu: Optional[str] = None
    language: Optional[str] = None
    source: Optional[str] = None

    def has_text(self) -> bool:
        """Return true when at least one description string is present."""

        return bool(self.long_text or self.short_text)


@dataclass(frozen=True)
class TimingEntry:
    """Request timing for one ECU variant and request prefix.

    ``request`` is the first one to three request bytes. The the source procedures
    narrow from three bytes to one, so a lookup with three bytes also matches
    a stored one-byte or two-byte prefix.
    """

    ecu_variant: int
    request: Tuple[int, ...]
    message_timing_id: Optional[int] = None
    timeout_and_resend_id: Optional[int] = None
    p1max: Optional[int] = None
    p3min: Optional[int] = None
    p4min: Optional[int] = None
    timeout_ms: Optional[int] = None
    resend: Optional[int] = None
    comment: Optional[str] = None


@dataclass(frozen=True)
class VehicleProfile:
    """One vehicle profile, as exported from BaseData.

    Every component is nullable, matching the source model. A null component
    is a wildcard when a selection is resolved, so the profile is deliberately
    not flattened into one vehicle type. ``markets`` keeps every partner group
    the profile is mapped to; ``market`` is the primary one when one is known.
    """

    id: str
    title: Optional[str] = None
    market: Optional[str] = None
    model: Optional[str] = None
    model_year: Optional[str] = None
    engine: Optional[str] = None
    transmission: Optional[str] = None
    steering: Optional[str] = None
    body_style: Optional[str] = None
    special_vehicle: Optional[str] = None
    suspension: Optional[str] = None
    brake_system: Optional[str] = None
    structure_week: Optional[str] = None
    chassis_from: Optional[str] = None
    chassis_to: Optional[str] = None
    folder_level: Optional[int] = None
    node_ecu: Optional[str] = None
    markets: Tuple[str, ...] = ()
    notes: Optional[str] = None

    def component_summary(self) -> str:
        """Return the non-null components as a short readable line."""

        values = [
            self.model,
            self.model_year,
            self.engine,
            self.transmission,
            self.steering,
            self.body_style,
            self.special_vehicle,
        ]
        return ", ".join(value for value in values if value)


@dataclass(frozen=True)
class ScriptEcu:
    """One addressed ECU node inside a recovered diagnostic script."""

    ecu_address: Optional[int] = None
    ecu_type: Optional[int] = None
    tester_address: Optional[int] = None
    service_ids: Tuple[int, ...] = ()
    mode: Optional[str] = None


@dataclass(frozen=True)
class ScriptEntry:
    """One diagnostic script, when the exporter can recover it.

    ``script_type`` is the numeric script type when the source wrote one;
    ``script_type_name`` keeps the symbolic value (``ReadDTCs``, ``ClearDtc``)
    because the source uses both spellings. ``status`` is ``crc-verified`` or
    ``crc-mismatch``. A script whose body failed its CRC is exported with that
    status rather than dropped.
    """

    id: str
    name: Optional[str] = None
    script_type: Optional[int] = None
    script_type_name: Optional[str] = None
    language: Optional[str] = None
    profiles: Tuple[str, ...] = ()
    ecu_address: Optional[int] = None
    ecu_type: Optional[int] = None
    service_ids: Tuple[int, ...] = ()
    ecus: Tuple[ScriptEcu, ...] = ()
    dtc_list_path: Optional[str] = None
    dtc_value_path: Optional[str] = None
    dtc_text_path: Optional[str] = None
    read_only_service_ids: Tuple[int, ...] = ()
    status: Optional[str] = None
    body_sha256: Optional[str] = None
    xml_path: Optional[str] = None
    source: Optional[str] = None


@dataclass(frozen=True)
class SymptomEntry:
    """One service symptom, with the causes attached to it.

    The source keeps the symptom text, category, type and section in separate
    tables and joins them by id. A null field means the source row was absent
    or the table could not be read; the exporter records which in ``source``.
    """

    id: str
    text: Optional[str] = None
    category: Optional[str] = None
    symptom_type: Optional[str] = None
    section: Optional[str] = None
    csc: Optional[str] = None
    causes: Tuple[str, ...] = ()
    dtc_codes: Tuple[str, ...] = ()
    ecu_variants: Tuple[int, ...] = ()
    source: Optional[str] = None


@dataclass(frozen=True)
class FreezeFrameEntry:
    """One candidate freeze-frame parameter for a DTC or an ECU variant."""

    parameter: str
    dtc_code: Optional[str] = None
    ecu: Optional[str] = None
    value: Optional[str] = None
    unit: Optional[str] = None
    block_id: Optional[int] = None
    confirmed: bool = False
    source: Optional[str] = None


@dataclass(frozen=True)
class DiagnosticCatalog:
    """A loaded service catalog with lookup indexes built at construction."""

    source: CatalogSource
    dtc_descriptions: Tuple[DtcDescription, ...] = ()
    timing: Tuple[TimingEntry, ...] = ()
    profiles: Tuple[VehicleProfile, ...] = ()
    scripts: Tuple[ScriptEntry, ...] = ()
    symptom_definitions: Tuple[SymptomEntry, ...] = ()
    freeze_frames: Tuple[FreezeFrameEntry, ...] = ()
    _dtc_index: Dict[str, Tuple[DtcDescription, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _timing_index: Dict[int, Tuple[TimingEntry, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _profile_index: Dict[str, Tuple[VehicleProfile, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _script_index: Dict[str, Tuple[ScriptEntry, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def __post_init__(self) -> None:
        dtc_index: Dict[str, List[DtcDescription]] = {}
        for entry in self.dtc_descriptions:
            dtc_index.setdefault(entry.code, []).append(entry)
        object.__setattr__(
            self,
            "_dtc_index",
            {code: tuple(entries) for code, entries in dtc_index.items()},
        )

        timing_index: Dict[int, List[TimingEntry]] = {}
        for entry in self.timing:
            timing_index.setdefault(entry.ecu_variant, []).append(entry)
        object.__setattr__(
            self,
            "_timing_index",
            {variant: tuple(entries) for variant, entries in timing_index.items()},
        )

        profile_index: Dict[str, List[VehicleProfile]] = {}
        for profile in self.profiles:
            profile_index.setdefault(profile.id, []).append(profile)
        object.__setattr__(
            self,
            "_profile_index",
            {key: tuple(value) for key, value in profile_index.items()},
        )

        script_index: Dict[str, List[ScriptEntry]] = {}
        for script in self.scripts:
            script_index.setdefault(script.id, []).append(script)
        object.__setattr__(
            self,
            "_script_index",
            {key: tuple(value) for key, value in script_index.items()},
        )

    # DTC lookups ---------------------------------------------------------

    def lookup_dtc(
        self, code: str, ecu: Optional[str] = None
    ) -> Optional[DtcDescription]:
        """Return the description for ``code``.

        The code may be written as ``P034211``, ``p0342``, ``0x034211`` or the
        bare six-digit hex form. A five-character code is a prefix lookup; it
        resolves only when the catalog has one unambiguous match. Missing or
        invalid codes return ``None``.
        """

        normalized = normalize_dtc_code(code)
        if normalized is None:
            return None

        candidates = self._candidates_for_code(normalized)
        if not candidates:
            return None
        return _select_description(candidates, ecu)

    def describe_dtc(
        self, code: str, ecu: Optional[str] = None
    ) -> Optional[DtcDescription]:
        """Return the full service-style record for ``code``.

        This is the same record as :meth:`lookup_dtc`, exposed under the name
        a UI is more likely to call. ``long_text``, ``short_text``,
        ``symptoms`` and ``causes`` are only present when the catalog carried
        them; no field is filled in from another code.
        """

        return self.lookup_dtc(code, ecu=ecu)

    def _candidates_for_code(self, code: str) -> Tuple[DtcDescription, ...]:
        exact = self._dtc_index.get(code)
        if exact is not None:
            return exact

        matches: List[DtcDescription] = []
        for stored, entries in self._dtc_index.items():
            if stored.startswith(code) or code.startswith(stored):
                matches.extend(entries)
        return tuple(matches)

    # Timing lookups ------------------------------------------------------

    def lookup_timing(
        self,
        ecu_variant: int,
        request_bytes: Union[int, bytes, bytearray, memoryview, str, Sequence[int]],
    ) -> Optional[TimingEntry]:
        """Return timing for an ECU variant and request prefix.

        ``request_bytes`` can be a byte string, a hex string such as
        ``"AE1B"``, a single integer or a sequence of integers. the caller passes the
        bytes it knows and leaves the rest as SQL ``NULL``, so a one- or
        two-byte lookup matches every stored request with that prefix. An
        exact-length match wins. If the matching rows disagree on timing, the
        lookup returns ``None`` rather than choosing one.
        """

        if isinstance(ecu_variant, bool) or not isinstance(ecu_variant, int):
            return None
        request = normalize_request_bytes(request_bytes)
        if request is None:
            return None

        candidates = self._timing_index.get(ecu_variant, ())
        exact = [
            entry
            for entry in candidates
            if len(entry.request) == len(request) and entry.request == request
        ]
        if exact:
            return _collapse_timing(exact)

        matches = [
            entry
            for entry in candidates
            if entry.request[: len(request)] == request
            or request[: len(entry.request)] == entry.request
        ]
        if not matches:
            return None
        return _collapse_timing(matches)

    # Profile and script lookups ------------------------------------------

    def lookup_profile(self, profile_id: str) -> Optional[VehicleProfile]:
        """Return the profile with exactly ``profile_id``, or ``None``.

        The id is the 16-hex profile identifier the scripts carry. Case is
        ignored. If the catalog has more than one row with the same id, the
        first one in catalog order is returned; ``iter_profiles`` still shows
        every row.
        """

        if not isinstance(profile_id, str):
            return None
        key = profile_id.strip().lower()
        if not key:
            return None
        entries = self._profile_index.get(key)
        if entries:
            return entries[0]
        for stored, values in self._profile_index.items():
            if stored.lower() == key:
                return values[0]
        return None

    def lookup_scripts(self, script_id: str) -> Tuple[ScriptEntry, ...]:
        """Return every recovered script with ``script_id``.

        A document number can occur in more than one language or in more than
        one damaged variant, so this returns a tuple rather than one entry.
        """

        if not isinstance(script_id, str):
            return ()
        key = script_id.strip().upper()
        exact = self._script_index.get(key)
        if exact:
            return exact
        for stored, values in self._script_index.items():
            if stored.upper() == key:
                return values
        return ()

    # Section iterators --------------------------------------------------

    def iter_profiles(self) -> Iterator[VehicleProfile]:
        """Yield every vehicle profile in catalog order."""

        return iter(self.profiles)

    def iter_scripts(self) -> Iterator[ScriptEntry]:
        """Yield every diagnostic script in catalog order."""

        return iter(self.scripts)

    def iter_symptoms(self) -> Iterator[SymptomEntry]:
        """Yield every symptom definition in catalog order."""

        return iter(self.symptom_definitions)

    def iter_freeze_frames(self) -> Iterator[FreezeFrameEntry]:
        """Yield every freeze-frame candidate in catalog order."""

        return iter(self.freeze_frames)

    def counts(self) -> Dict[str, int]:
        """Return row counts for every catalog section."""

        return {
            "dtc_descriptions": len(self.dtc_descriptions),
            "timing": len(self.timing),
            "profiles": len(self.profiles),
            "scripts": len(self.scripts),
            "symptom_definitions": len(self.symptom_definitions),
            "freeze_frames": len(self.freeze_frames),
        }


# Public helpers ---------------------------------------------------------


def normalize_dtc_code(code: str) -> Optional[str]:
    """Normalize a DTC code, or return ``None`` when it is not a DTC shape.

    ``0x034211`` becomes ``P034211`` because the top two bits of the first
    nibble select the system letter. Five-character codes are kept as written
    so that prefix lookups can match a full seven-character entry.
    """

    if not isinstance(code, str):
        return None
    text = "".join(code.split()).upper()
    if text.startswith("0X"):
        text = text[2:]

    if len(text) == 6 and all(char in _HEX_DIGITS for char in text):
        letter = _DTC_LETTERS[int(text[0], 16) >> 2]
        text = letter + text

    if len(text) not in (5, 7):
        return None
    if text[0] not in _DTC_LETTERS:
        return None
    if not all(char in _HEX_DIGITS for char in text[1:]):
        return None
    return text


def normalize_request_bytes(
    value: Union[int, bytes, bytearray, memoryview, str, Sequence[int]]
) -> Optional[Tuple[int, ...]]:
    """Normalize request bytes to a one-to-three byte tuple.

    Invalid or empty input returns ``None``. Strings are read as hexadecimal
    with optional ``0x`` prefix and separators.
    """

    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        if 0 <= value <= 0xFF:
            return (value,)
        return None
    if isinstance(value, (bytes, bytearray, memoryview)):
        raw = tuple(bytes(value))
    elif isinstance(value, str):
        text = value.strip().upper()
        if text.startswith("0X"):
            text = text[2:]
        for separator in (" ", "-", ":", "_"):
            text = text.replace(separator, "")
        if not text or len(text) % 2:
            return None
        try:
            raw = tuple(bytes.fromhex(text))
        except ValueError:
            return None
    elif isinstance(value, Sequence):
        raw_list: List[int] = []
        for item in value:
            if isinstance(item, bool) or not isinstance(item, int):
                return None
            if not 0 <= item <= 0xFF:
                return None
            raw_list.append(item)
        raw = tuple(raw_list)
    else:
        return None

    if not raw or len(raw) > 3:
        return None
    return raw


def sanitize_text(value: Optional[str]) -> Optional[str]:
    """Return readable text with decoding artefacts removed.

    Replacement characters, NULs and control bytes are turned into spaces
    instead of being dropped, so a damaged row still shows where text was lost.
    Whitespace is collapsed but line breaks are kept. ``None`` stays ``None``.
    """

    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError("text must be a string or None")
    text = value.replace("\r\n", "\n").replace("\r", "\n")
    chars: List[str] = []
    for char in text:
        if char == "\ufffd" or char == "\x00":
            chars.append(" ")
        elif char in "\n\t":
            chars.append(char)
        elif char.isprintable():
            chars.append(char)
        else:
            chars.append(" ")
    cleaned = "".join(chars)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


def load_catalog(path: Union[str, os.PathLike[str]]) -> DiagnosticCatalog:
    """Load and validate a catalog JSON file.

    ``CatalogError`` wraps JSON syntax errors and every schema violation.
    File-system errors propagate unchanged.
    """

    try:
        with open(path, "r", encoding="utf-8") as handle:
            document = json.load(handle)
    except json.JSONDecodeError as exc:
        raise CatalogError(
            "catalog is not valid JSON: {} (line {}, column {})".format(
                exc.msg, exc.lineno, exc.colno
            )
        ) from exc
    if not isinstance(document, dict):
        raise CatalogError("catalog root must be a JSON object")
    return catalog_from_dict(document)


def catalog_from_dict(document: Mapping[str, Any]) -> DiagnosticCatalog:
    """Validate a catalog mapping and return a :class:`DiagnosticCatalog`."""

    root = _require_mapping(document, "catalog")
    if root.get("schema") != SCHEMA_NAME:
        raise CatalogError(
            "unsupported catalog schema {!r}; expected {!r}".format(
                root.get("schema"), SCHEMA_NAME
            )
        )
    version = root.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise CatalogError("catalog version must be an integer")
    if version not in SUPPORTED_VERSIONS:
        raise CatalogError(
            "unsupported catalog version {}; expected one of {}".format(
                version, ", ".join(str(item) for item in SUPPORTED_VERSIONS)
            )
        )

    source = _parse_source(root.get("source"))
    dtc_descriptions = tuple(
        _parse_dtc_description(item, "dtc_descriptions[{}]".format(index))
        for index, item in enumerate(
            _require_list(root.get("dtc_descriptions"), "dtc_descriptions")
        )
    )
    timing = tuple(
        _parse_timing(item, "timing[{}]".format(index))
        for index, item in enumerate(_require_list(root.get("timing"), "timing"))
    )
    profiles = tuple(
        _parse_profile(item, "profiles[{}]".format(index))
        for index, item in enumerate(
            _require_list(root.get("profiles"), "profiles")
        )
    )
    scripts = tuple(
        _parse_script(item, "scripts[{}]".format(index))
        for index, item in enumerate(
            _require_list(root.get("scripts"), "scripts")
        )
    )
    symptom_definitions = tuple(
        _parse_symptom(item, "symptom_definitions[{}]".format(index))
        for index, item in enumerate(
            _optional_list(root.get("symptom_definitions"), "symptom_definitions")
        )
    )
    freeze_frames = tuple(
        _parse_freeze_frame(item, "freeze_frames[{}]".format(index))
        for index, item in enumerate(
            _optional_list(root.get("freeze_frames"), "freeze_frames")
        )
    )

    return DiagnosticCatalog(
        source=source,
        dtc_descriptions=dtc_descriptions,
        timing=timing,
        profiles=profiles,
        scripts=scripts,
        symptom_definitions=symptom_definitions,
        freeze_frames=freeze_frames,
    )


def catalog_to_dict(catalog: DiagnosticCatalog) -> Dict[str, Any]:
    """Serialize a catalog back to the versioned JSON shape."""

    return {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "source": _source_to_dict(catalog.source),
        "dtc_descriptions": [
            _compact(
                {
                    "code": entry.code,
                    "ecu": entry.ecu,
                    "long_text": entry.long_text,
                    "short_text": entry.short_text,
                    "symptoms": list(entry.symptoms),
                    "causes": list(entry.causes),
                    "language": entry.language,
                    "source": entry.source,
                }
            )
            for entry in catalog.dtc_descriptions
        ],
        "timing": [
            _compact(
                {
                    "ecu_variant": entry.ecu_variant,
                    "request": bytes(entry.request).hex().upper(),
                    "message_timing_id": entry.message_timing_id,
                    "timeout_and_resend_id": entry.timeout_and_resend_id,
                    "p1max": entry.p1max,
                    "p3min": entry.p3min,
                    "p4min": entry.p4min,
                    "timeout_ms": entry.timeout_ms,
                    "resend": entry.resend,
                    "comment": entry.comment,
                }
            )
            for entry in catalog.timing
        ],
        "profiles": [
            _compact(
                {
                    "id": entry.id,
                    "title": entry.title,
                    "market": entry.market,
                    "model": entry.model,
                    "model_year": entry.model_year,
                    "engine": entry.engine,
                    "transmission": entry.transmission,
                    "steering": entry.steering,
                    "body_style": entry.body_style,
                    "special_vehicle": entry.special_vehicle,
                    "suspension": entry.suspension,
                    "brake_system": entry.brake_system,
                    "structure_week": entry.structure_week,
                    "chassis_from": entry.chassis_from,
                    "chassis_to": entry.chassis_to,
                    "folder_level": entry.folder_level,
                    "node_ecu": entry.node_ecu,
                    "markets": list(entry.markets),
                    "notes": entry.notes,
                }
            )
            for entry in catalog.profiles
        ],
        "scripts": [
            _compact(
                {
                    "id": entry.id,
                    "name": entry.name,
                    "script_type": entry.script_type,
                    "script_type_name": entry.script_type_name,
                    "language": entry.language,
                    "profiles": list(entry.profiles),
                    "ecu_address": entry.ecu_address,
                    "ecu_type": entry.ecu_type,
                    "service_ids": list(entry.service_ids),
                    "ecus": [
                        _compact(
                            {
                                "ecu_address": ecu.ecu_address,
                                "ecu_type": ecu.ecu_type,
                                "tester_address": ecu.tester_address,
                                "service_ids": list(ecu.service_ids),
                                "mode": ecu.mode,
                            }
                        )
                        for ecu in entry.ecus
                    ],
                    "dtc_list_path": entry.dtc_list_path,
                    "dtc_value_path": entry.dtc_value_path,
                    "dtc_text_path": entry.dtc_text_path,
                    "read_only_service_ids": list(entry.read_only_service_ids),
                    "status": entry.status,
                    "body_sha256": entry.body_sha256,
                    "xml_path": entry.xml_path,
                    "source": entry.source,
                }
            )
            for entry in catalog.scripts
        ],
        "symptom_definitions": [
            _compact(
                {
                    "id": entry.id,
                    "text": entry.text,
                    "category": entry.category,
                    "symptom_type": entry.symptom_type,
                    "section": entry.section,
                    "csc": entry.csc,
                    "causes": list(entry.causes),
                    "dtc_codes": list(entry.dtc_codes),
                    "ecu_variants": list(entry.ecu_variants),
                    "source": entry.source,
                }
            )
            for entry in catalog.symptom_definitions
        ],
        "freeze_frames": [
            _compact(
                {
                    "parameter": entry.parameter,
                    "dtc_code": entry.dtc_code,
                    "ecu": entry.ecu,
                    "value": entry.value,
                    "unit": entry.unit,
                    "block_id": entry.block_id,
                    "confirmed": entry.confirmed,
                    "source": entry.source,
                }
            )
            for entry in catalog.freeze_frames
        ],
    }


def dump_catalog(catalog: DiagnosticCatalog, path: Union[str, os.PathLike[str]]) -> None:
    """Write a catalog as UTF-8 JSON with a trailing newline."""

    with open(path, "w", encoding="utf-8") as handle:
        json.dump(
            catalog_to_dict(catalog),
            handle,
            ensure_ascii=False,
            indent=2,
        )
        handle.write("\n")


# Parsing helpers --------------------------------------------------------


def _require_mapping(value: Any, where: str) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise CatalogError("{} must be a JSON object".format(where))
    return value


def _require_list(value: Any, where: str) -> List[Any]:
    if not isinstance(value, list):
        raise CatalogError("{} must be a JSON list".format(where))
    return value


def _optional_list(value: Any, where: str) -> List[Any]:
    if value is None:
        return []
    return _require_list(value, where)


def _require_text(value: Any, where: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CatalogError("{} must be a non-empty string".format(where))
    return value


def _optional_text(value: Any, where: str) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    return _require_text(value, where)


def _optional_int(
    value: Any,
    where: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise CatalogError("{} must be an integer".format(where))
    if minimum is not None and value < minimum:
        raise CatalogError("{} must be at least {}".format(where, minimum))
    if maximum is not None and value > maximum:
        raise CatalogError("{} must be at most {}".format(where, maximum))
    return value


def _optional_bool(value: Any, where: str, default: bool = False) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise CatalogError("{} must be true or false".format(where))
    return value


def _require_int(
    value: Any,
    where: str,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
) -> int:
    parsed = _optional_int(value, where, minimum=minimum, maximum=maximum)
    if parsed is None:
        raise CatalogError("{} is required".format(where))
    return parsed


def _text_tuple(value: Any, where: str) -> Tuple[str, ...]:
    items = _optional_list(value, where)
    result: List[str] = []
    for index, item in enumerate(items):
        result.append(_require_text(item, "{}[{}]".format(where, index)))
    return tuple(result)


def _int_tuple(value: Any, where: str, maximum_length: Optional[int] = None) -> Tuple[int, ...]:
    items = _optional_list(value, where)
    result: List[int] = []
    for index, item in enumerate(items):
        result.append(
            _require_int(
                item,
                "{}[{}]".format(where, index),
                minimum=0,
                maximum=0xFF,
            )
        )
    if maximum_length is not None and len(result) > maximum_length:
        raise CatalogError(
            "{} must have at most {} entries".format(where, maximum_length)
        )
    return tuple(result)


def _int_tuple_unbounded(value: Any, where: str) -> Tuple[int, ...]:
    items = _optional_list(value, where)
    result: List[int] = []
    for index, item in enumerate(items):
        result.append(
            _require_int(item, "{}[{}]".format(where, index), minimum=0)
        )
    return tuple(result)


def _parse_source(value: Any) -> CatalogSource:
    data = _require_mapping(value, "source")
    databases: List[DatabaseSource] = []
    for index, item in enumerate(
        _optional_list(data.get("databases"), "source.databases")
    ):
        where = "source.databases[{}]".format(index)
        database = _require_mapping(item, where)
        databases.append(
            DatabaseSource(
                name=_require_text(database.get("name"), where + ".name"),
                path=_require_text(database.get("path"), where + ".path"),
                sha256=_optional_text(database.get("sha256"), where + ".sha256"),
            )
        )
    return CatalogSource(
        kind=_require_text(data.get("kind"), "source.kind"),
        generated_at=_optional_text(
            data.get("generated_at"), "source.generated_at"
        ),
        image=_optional_text(data.get("image"), "source.image"),
        databases=tuple(databases),
        extractor=_optional_text(data.get("extractor"), "source.extractor"),
        language=_optional_text(data.get("language"), "source.language"),
        notes=_text_tuple(data.get("notes"), "source.notes"),
    )


def _parse_dtc_description(value: Any, where: str) -> DtcDescription:
    data = _require_mapping(value, where)
    raw_code = _require_text(data.get("code"), where + ".code")
    code = normalize_dtc_code(raw_code)
    if code is None:
        raise CatalogError(
            "{}.code {!r} is not a diagnostic trouble code".format(where, raw_code)
        )
    return DtcDescription(
        code=code,
        long_text=_optional_text(data.get("long_text"), where + ".long_text"),
        short_text=_optional_text(data.get("short_text"), where + ".short_text"),
        symptoms=_text_tuple(data.get("symptoms"), where + ".symptoms"),
        causes=_text_tuple(data.get("causes"), where + ".causes"),
        ecu=_optional_text(data.get("ecu"), where + ".ecu"),
        language=_optional_text(data.get("language"), where + ".language"),
        source=_optional_text(data.get("source"), where + ".source"),
    )


def _parse_timing(value: Any, where: str) -> TimingEntry:
    data = _require_mapping(value, where)
    request = normalize_request_bytes(data.get("request"))
    if request is None:
        raise CatalogError(
            "{}.request must be one to three bytes, a hex string or a list "
            "of byte values".format(where)
        )
    return TimingEntry(
        ecu_variant=_require_int(
            data.get("ecu_variant"), where + ".ecu_variant", minimum=0
        ),
        request=request,
        message_timing_id=_optional_int(
            data.get("message_timing_id"), where + ".message_timing_id", minimum=0
        ),
        timeout_and_resend_id=_optional_int(
            data.get("timeout_and_resend_id"),
            where + ".timeout_and_resend_id",
            minimum=0,
        ),
        p1max=_optional_int(data.get("p1max"), where + ".p1max", minimum=0),
        p3min=_optional_int(data.get("p3min"), where + ".p3min", minimum=0),
        p4min=_optional_int(data.get("p4min"), where + ".p4min", minimum=0),
        timeout_ms=_optional_int(
            data.get("timeout_ms"), where + ".timeout_ms", minimum=0
        ),
        resend=_optional_int(data.get("resend"), where + ".resend", minimum=0),
        comment=_optional_text(data.get("comment"), where + ".comment"),
    )


def _parse_profile(value: Any, where: str) -> VehicleProfile:
    data = _require_mapping(value, where)
    return VehicleProfile(
        id=_require_text(data.get("id"), where + ".id"),
        title=_optional_text(data.get("title"), where + ".title"),
        market=_optional_text(data.get("market"), where + ".market"),
        model=_optional_text(data.get("model"), where + ".model"),
        model_year=_optional_text(data.get("model_year"), where + ".model_year"),
        engine=_optional_text(data.get("engine"), where + ".engine"),
        transmission=_optional_text(
            data.get("transmission"), where + ".transmission"
        ),
        steering=_optional_text(data.get("steering"), where + ".steering"),
        body_style=_optional_text(data.get("body_style"), where + ".body_style"),
        special_vehicle=_optional_text(
            data.get("special_vehicle"), where + ".special_vehicle"
        ),
        suspension=_optional_text(data.get("suspension"), where + ".suspension"),
        brake_system=_optional_text(
            data.get("brake_system"), where + ".brake_system"
        ),
        structure_week=_optional_text(
            data.get("structure_week"), where + ".structure_week"
        ),
        chassis_from=_optional_text(
            data.get("chassis_from"), where + ".chassis_from"
        ),
        chassis_to=_optional_text(data.get("chassis_to"), where + ".chassis_to"),
        folder_level=_optional_int(
            data.get("folder_level"), where + ".folder_level", minimum=0
        ),
        node_ecu=_optional_text(data.get("node_ecu"), where + ".node_ecu"),
        markets=_text_tuple(data.get("markets"), where + ".markets"),
        notes=_optional_text(data.get("notes"), where + ".notes"),
    )


def _parse_script_ecu(value: Any, where: str) -> ScriptEcu:
    data = _require_mapping(value, where)
    return ScriptEcu(
        ecu_address=_optional_int(
            data.get("ecu_address"), where + ".ecu_address", minimum=0
        ),
        ecu_type=_optional_int(
            data.get("ecu_type"), where + ".ecu_type", minimum=0
        ),
        tester_address=_optional_int(
            data.get("tester_address"), where + ".tester_address", minimum=0
        ),
        service_ids=_int_tuple(
            data.get("service_ids"), where + ".service_ids"
        ),
        mode=_optional_text(data.get("mode"), where + ".mode"),
    )


def _parse_script(value: Any, where: str) -> ScriptEntry:
    data = _require_mapping(value, where)
    ecus = tuple(
        _parse_script_ecu(item, "{}.ecus[{}]".format(where, index))
        for index, item in enumerate(
            _optional_list(data.get("ecus"), where + ".ecus")
        )
    )
    return ScriptEntry(
        id=_require_text(data.get("id"), where + ".id"),
        name=_optional_text(data.get("name"), where + ".name"),
        script_type=_optional_int(
            data.get("script_type"), where + ".script_type", minimum=0
        ),
        script_type_name=_optional_text(
            data.get("script_type_name"), where + ".script_type_name"
        ),
        language=_optional_text(data.get("language"), where + ".language"),
        profiles=_text_tuple(data.get("profiles"), where + ".profiles"),
        ecu_address=_optional_int(
            data.get("ecu_address"), where + ".ecu_address", minimum=0
        ),
        ecu_type=_optional_int(
            data.get("ecu_type"), where + ".ecu_type", minimum=0
        ),
        service_ids=_int_tuple(
            data.get("service_ids"), where + ".service_ids"
        ),
        ecus=ecus,
        dtc_list_path=_optional_text(
            data.get("dtc_list_path"), where + ".dtc_list_path"
        ),
        dtc_value_path=_optional_text(
            data.get("dtc_value_path"), where + ".dtc_value_path"
        ),
        dtc_text_path=_optional_text(
            data.get("dtc_text_path"), where + ".dtc_text_path"
        ),
        read_only_service_ids=_int_tuple(
            data.get("read_only_service_ids"),
            where + ".read_only_service_ids",
        ),
        status=_optional_text(data.get("status"), where + ".status"),
        body_sha256=_optional_text(
            data.get("body_sha256"), where + ".body_sha256"
        ),
        xml_path=_optional_text(data.get("xml_path"), where + ".xml_path"),
        source=_optional_text(data.get("source"), where + ".source"),
    )


def _parse_symptom(value: Any, where: str) -> SymptomEntry:
    data = _require_mapping(value, where)
    return SymptomEntry(
        id=_require_text(data.get("id"), where + ".id"),
        text=_optional_text(data.get("text"), where + ".text"),
        category=_optional_text(data.get("category"), where + ".category"),
        symptom_type=_optional_text(
            data.get("symptom_type"), where + ".symptom_type"
        ),
        section=_optional_text(data.get("section"), where + ".section"),
        csc=_optional_text(data.get("csc"), where + ".csc"),
        causes=_text_tuple(data.get("causes"), where + ".causes"),
        dtc_codes=_text_tuple(data.get("dtc_codes"), where + ".dtc_codes"),
        ecu_variants=_int_tuple_unbounded(
            data.get("ecu_variants"), where + ".ecu_variants"
        ),
        source=_optional_text(data.get("source"), where + ".source"),
    )


def _parse_freeze_frame(value: Any, where: str) -> FreezeFrameEntry:
    data = _require_mapping(value, where)
    return FreezeFrameEntry(
        parameter=_require_text(data.get("parameter"), where + ".parameter"),
        dtc_code=_optional_text(data.get("dtc_code"), where + ".dtc_code"),
        ecu=_optional_text(data.get("ecu"), where + ".ecu"),
        value=_optional_text(data.get("value"), where + ".value"),
        unit=_optional_text(data.get("unit"), where + ".unit"),
        block_id=_optional_int(
            data.get("block_id"), where + ".block_id", minimum=0
        ),
        confirmed=_optional_bool(
            data.get("confirmed"), where + ".confirmed", default=False
        ),
        source=_optional_text(data.get("source"), where + ".source"),
    )


def _source_to_dict(source: CatalogSource) -> Dict[str, Any]:
    return _compact(
        {
            "kind": source.kind,
            "generated_at": source.generated_at,
            "image": source.image,
            "databases": [
                _compact(
                    {
                        "name": database.name,
                        "path": database.path,
                        "sha256": database.sha256,
                    }
                )
                for database in source.databases
            ],
            "extractor": source.extractor,
            "language": source.language,
            "notes": list(source.notes),
        }
    )


def _select_description(
    candidates: Iterable[DtcDescription], ecu: Optional[str]
) -> Optional[DtcDescription]:
    wanted = ecu.strip().upper() if isinstance(ecu, str) and ecu.strip() else None
    candidate_list = list(candidates)

    if wanted is not None:
        exact = [
            entry
            for entry in candidate_list
            if entry.ecu is not None and entry.ecu.strip().upper() == wanted
        ]
        if exact:
            return _collapse_descriptions(exact)

    global_entries = [entry for entry in candidate_list if entry.ecu is None]
    if global_entries:
        return _collapse_descriptions(global_entries)

    if wanted is not None:
        return None
    return _collapse_descriptions(candidate_list)


def _collapse_descriptions(
    candidates: Iterable[DtcDescription],
) -> Optional[DtcDescription]:
    unique: Dict[Tuple[Any, ...], DtcDescription] = {}
    for entry in candidates:
        key = (
            entry.long_text,
            entry.short_text,
            entry.symptoms,
            entry.causes,
        )
        unique.setdefault(key, entry)
    if len(unique) == 1:
        return next(iter(unique.values()))

    english = [
        entry
        for entry in unique.values()
        if (entry.language or "").lower().startswith("en")
    ]
    if len(english) == 1:
        return english[0]
    return None


def _collapse_timing(candidates: Iterable[TimingEntry]) -> Optional[TimingEntry]:
    unique: Dict[Tuple[Any, ...], TimingEntry] = {}
    for entry in candidates:
        key = (
            entry.message_timing_id,
            entry.timeout_and_resend_id,
            entry.p1max,
            entry.p3min,
            entry.p4min,
            entry.timeout_ms,
            entry.resend,
        )
        unique.setdefault(key, entry)
    if len(unique) == 1:
        return next(iter(unique.values()))
    return None


def _compact(mapping: Mapping[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    for key, value in mapping.items():
        if value is None:
            continue
        if isinstance(value, (list, tuple)) and not value:
            continue
        result[key] = value
    return result


__all__ = [
    "CatalogError",
    "CatalogSource",
    "DatabaseSource",
    "DtcDescription",
    "FreezeFrameEntry",
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "SUPPORTED_VERSIONS",
    "ScriptEcu",
    "ScriptEntry",
    "SymptomEntry",
    "TimingEntry",
    "VehicleProfile",
    "DiagnosticCatalog",
    "catalog_from_dict",
    "catalog_to_dict",
    "dump_catalog",
    "load_catalog",
    "normalize_dtc_code",
    "normalize_request_bytes",
    "sanitize_text",
]
