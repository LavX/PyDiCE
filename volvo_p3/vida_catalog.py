"""Runtime catalog of VIDA diagnostic data.

VIDA keeps DTC text, request timing and vehicle profiles in separate SQL
Server databases. This module loads a small, versioned JSON export of the
parts a diagnostic client can use, and answers the three questions PyDiCE
asks at runtime:

* What does this DTC mean, on this ECU?
* How long should a request to this ECU variant wait?
* Which profiles and scripts does the catalog know about?

The catalog file is JSON only. It has five sections:

.. code-block:: json

    {
      "schema": "pydice.vida_catalog",
      "version": 1,
      "source": {
        "kind": "vida-mdf",
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
        {"id": "0b00c8af80206a61", "title": "S40 (-04) 1999 B4164S"}
      ],
      "scripts": [
        {"id": "VCC-227144", "name": "Read DTCs", "script_type": 10}
      ]
    }

A DTC entry with ``"ecu": null`` or no ``ecu`` key is global. A caller that
passes an ECU name gets an exact ECU entry first and falls back to the global
entry. If the catalog has several different descriptions for the same code
and the caller did not narrow them by ECU, the lookup returns ``None`` rather
than choosing one. A missing code always returns ``None``; the loader never
synthesises text.

The exporter in ``tools/export_vida_catalog.py`` builds this document from the
VIDA image, or transforms an existing JSON document with ``--from-json``.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union


SCHEMA_NAME = "pydice.vida_catalog"
SCHEMA_VERSION = 1

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
    """A VIDA-style DTC description.

    ``ecu`` is a free-form ECU label. VIDA calls some of these ``ECM`` or
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

    ``request`` is the first one to three request bytes. The VIDA procedures
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
    """One VIDA vehicle profile, as exported from BaseData."""

    id: str
    title: Optional[str] = None
    market: Optional[str] = None
    model: Optional[str] = None
    model_year: Optional[str] = None
    chassis_from: Optional[str] = None
    chassis_to: Optional[str] = None
    notes: Optional[str] = None


@dataclass(frozen=True)
class ScriptEntry:
    """One VIDA diagnostic script, when the exporter can recover it."""

    id: str
    name: Optional[str] = None
    script_type: Optional[int] = None
    language: Optional[str] = None
    profiles: Tuple[str, ...] = ()
    ecu_address: Optional[int] = None
    ecu_type: Optional[int] = None
    service_ids: Tuple[int, ...] = ()
    source: Optional[str] = None


@dataclass(frozen=True)
class VidaCatalog:
    """A loaded VIDA catalog with lookup indexes built at construction."""

    source: CatalogSource
    dtc_descriptions: Tuple[DtcDescription, ...] = ()
    timing: Tuple[TimingEntry, ...] = ()
    profiles: Tuple[VehicleProfile, ...] = ()
    scripts: Tuple[ScriptEntry, ...] = ()
    _dtc_index: Dict[str, Tuple[DtcDescription, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )
    _timing_index: Dict[int, Tuple[TimingEntry, ...]] = field(
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
        """Return the full VIDA-style record for ``code``.

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
        ``"AE1B"``, a single integer or a sequence of integers. VIDA passes the
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

    # Section iterators --------------------------------------------------

    def iter_profiles(self) -> Iterator[VehicleProfile]:
        """Yield every vehicle profile in catalog order."""

        return iter(self.profiles)

    def iter_scripts(self) -> Iterator[ScriptEntry]:
        """Yield every diagnostic script in catalog order."""

        return iter(self.scripts)


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


def load_catalog(path: Union[str, os.PathLike[str]]) -> VidaCatalog:
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


def catalog_from_dict(document: Mapping[str, Any]) -> VidaCatalog:
    """Validate a catalog mapping and return a :class:`VidaCatalog`."""

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
    if version != SCHEMA_VERSION:
        raise CatalogError(
            "unsupported catalog version {}; expected {}".format(
                version, SCHEMA_VERSION
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

    return VidaCatalog(
        source=source,
        dtc_descriptions=dtc_descriptions,
        timing=timing,
        profiles=profiles,
        scripts=scripts,
    )


def catalog_to_dict(catalog: VidaCatalog) -> Dict[str, Any]:
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
                    "chassis_from": entry.chassis_from,
                    "chassis_to": entry.chassis_to,
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
                    "language": entry.language,
                    "profiles": list(entry.profiles),
                    "ecu_address": entry.ecu_address,
                    "ecu_type": entry.ecu_type,
                    "service_ids": list(entry.service_ids),
                    "source": entry.source,
                }
            )
            for entry in catalog.scripts
        ],
    }


def dump_catalog(catalog: VidaCatalog, path: Union[str, os.PathLike[str]]) -> None:
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
        chassis_from=_optional_text(
            data.get("chassis_from"), where + ".chassis_from"
        ),
        chassis_to=_optional_text(data.get("chassis_to"), where + ".chassis_to"),
        notes=_optional_text(data.get("notes"), where + ".notes"),
    )


def _parse_script(value: Any, where: str) -> ScriptEntry:
    data = _require_mapping(value, where)
    return ScriptEntry(
        id=_require_text(data.get("id"), where + ".id"),
        name=_optional_text(data.get("name"), where + ".name"),
        script_type=_optional_int(
            data.get("script_type"), where + ".script_type", minimum=0
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
    "SCHEMA_NAME",
    "SCHEMA_VERSION",
    "ScriptEntry",
    "TimingEntry",
    "VehicleProfile",
    "VidaCatalog",
    "catalog_from_dict",
    "catalog_to_dict",
    "dump_catalog",
    "load_catalog",
    "normalize_dtc_code",
    "normalize_request_bytes",
]
