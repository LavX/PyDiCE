#!/usr/bin/env python3
"""Recover or derive a logical-address to CAN-ID map from local evidence.

The source image does not contain a populated physical-address table.  The
configuration tables that would join a script's logical ECU identifier to a
CAN request ID have no page set in this snapshot.  This tool therefore does not
invent a map.  It reports:

* the physical request/response IDs that a saved module profile already states;
* the logical identifiers (``EcuAddress`` or ``EcuType``) found in a script;
* script-title evidence that associates a module label with a logical
  ``EcuType``;
* one selected entry per module, with a confidence and a source, or an explicit
  reason why no entry was selected.

The output JSON contains an ``addresses`` object in the shape accepted by the
read-only script runner (``{"addresses": {"0x11": "0x7E0"}}``) plus an
``entries`` array that records where each value came from.  A missing or
ambiguous match is reported, never filled in.

Examples::

    python3 tools/recover_address_map.py \
        --profile reports/my-car-complete.json

    python3 tools/recover_address_map.py \
        --profile reports/my-car-complete.json \
        --script path/to/script.zip --selection dominant

    python3 tools/recover_address_map.py --script path/to/script.zip

    python3 tools/recover_address_map.py \
        --profile reports/my-car-complete.json \
        --scan-image /path/to/DiagSwdlRepository_Data.MDF

The ``--scan-image`` option inflates ZIP streams in a database image and
records ``EcuAddress``/``EcuType`` properties from bodies that may fail full XML
parsing.  That evidence is reported but never selected automatically, because a
damaged title without a module name cannot establish ownership.

Only the standard library and the repository's script parser are used.  No
adapter, database server or source process is started.
"""

from __future__ import annotations

import argparse
import io
import json
import mmap
import re
import struct
import sys
import zipfile
import zlib
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from volvo_p3.script_plan import (  # noqa: E402
    DiagnosticScript,
    DiagnosticScriptError,
    parse_script_xml,
    parse_script_zip,
)


SCHEMA_NAME = "diag-address-map"
SCHEMA_VERSION = 1

FUNCTIONAL_REQUEST_ID = 0x7DF
MAX_CAN_ID = 0x7FF

#: Upper bound on one inflated script body while carving a database image.
MAX_FRAGMENT_BYTES = 8 * 1024 * 1024

#: Property and element patterns used by the fragment scanner.  These are
#: deliberately loose: a damaged body can still expose its title and address
#: even when the XML parser rejects the document.
_FRAGMENT_ADDRESS = re.compile(
    rb'name="EcuAddress"[^>]{0,160}?value="([^"]*)"', re.IGNORECASE
)
_FRAGMENT_TYPE = re.compile(
    rb'name="EcuType"[^>]{0,160}?value="([^"]*)"', re.IGNORECASE
)
_FRAGMENT_MODE = re.compile(
    rb'name="Mode"[^>]{0,160}?value="EcuAddress"', re.IGNORECASE
)
_FRAGMENT_DOCNO = re.compile(rb'docno="([^"]+)"')
_FRAGMENT_TITLE = re.compile(rb"<title>(.*?)</title>", re.DOTALL | re.IGNORECASE)
_FRAGMENT_XML_START = (b"<", b"\xef\xbb\xbf<", b"\xff\xfe<", b"\xfe\xff<")

#: A profile name that is only a discovery placeholder, not an ECU label.
PLACEHOLDER_NAME = re.compile(r"^CAN_[0-9A-F]{3}$", re.IGNORECASE)

#: Generic title words that must not be treated as module labels even when a
#: saved profile happens to use one of them as a name.
TITLE_STOPWORDS = frozenset(
    {
        "ALL",
        "AND",
        "CARCOM",
        "DTC",
        "DTCS",
        "ECU",
        "FOR",
        "ID",
        "READ",
        "STATUS",
        "THE",
        "WITH",
    }
)

#: Physical request IDs used by the built-in discovery candidates.  These are a
#: cross-check only; the saved profile is the authority for the car in front of
#: the user.  Names are neutral industry abbreviations.
KNOWN_REQUEST_IDS = {
    "ECM": 0x7E0,
    "TCM": 0x7E1,
    "BECM": 0x7E4,
    "IEM": 0x745,
    "IGM": 0x747,
    "DCDC": 0x746,
    "OBC": 0x753,
    "ACCM": 0x7C7,
    "CEM": 0x726,
    "DIM": 0x720,
}

# Script-title evidence recovered from the script store in this snapshot.
#
# Each tuple is ``(ecu_type, document_count, example_document_numbers)``.
# The evidence was produced by taking every parsed script whose title contains
# the module label as a standalone token and whose parsed node carries a
# non-zero EcuType, then counting distinct document numbers.  A title can name
# a module while the node addresses a gateway, and the title does not encode a
# platform generation.  The values are therefore candidates, not proof.  Only
# a unique candidate is selected by default; ambiguous names stay unmapped
# unless the caller explicitly asks for the dominant or all candidates.
SCRIPT_NAME_EVIDENCE: Dict[str, Tuple[Tuple[int, int, Tuple[str, ...]], ...]] = {
    "DIM": (
        (381101, 12, ("VCC-231100", "VCC-230220", "VCC-233266")),
        (372302, 3, ("VCC-298588", "VCC-402447", "VCC-437899")),
    ),
    "CEM": (
        (372302, 15, ("VCC-227700", "VCC-230926", "VCC-233263")),
        (284101, 1, ("VCC-242220",)),
        (593102, 1, ("VCC-248740",)),
    ),
    "ECM": (
        (284101, 51, ("VCC-232583", "VCC-231540", "VCC-231640")),
        (372302, 3, ("VCC-398005", "VCC-448262", "VCC-448749")),
    ),
    "TCM": (
        (437601, 54, ("VCC-232042", "VCC-232402", "VCC-232427")),
        (284101, 1, ("VCC-346867",)),
        (457601, 1, ("VCC-286207",)),
    ),
    "BCM": (
        (593102, 30, ("VCC-227998", "VCC-225743", "VCC-227997")),
        (372302, 1, ("VCC-230482",)),
        (437601, 1, ("VCC-246800",)),
        (551401, 1, ("VCC-475995",)),
    ),
    "SRS": ((884701, 7, ("VCC-230622", "VCC-233283", "VCC-235724")),),
    "ABS": ((593101, 4, ("VCC-230860", "VCC-226840", "VCC-235609")),),
    "SAS": ((645902, 5, ("VCC-231665", "VCC-231664", "VCC-247801")),),
    "SWM": ((641901, 1, ("VCC-230660",)),),
    "AYC": ((593101, 1, ("VCC-225720",)),),
    "DEM": ((465901, 8, ("VCC-232000", "VCC-230929", "VCC-230930")),),
    "DSTC": ((593101, 1, ("VCC-229340",)),),
    "EPS": ((645901, 1, ("VCC-233267",)),),
    "PAM": ((366902, 3, ("VCC-235502", "VCC-235501", "VCC-235503")),),
    "CCM": (
        (871101, 9, ("VCC-230925", "VCC-233007", "VCC-233523")),
        (372302, 2, ("VCC-240300", "VCC-280929")),
        (872101, 1, ("VCC-230925",)),
    ),
    "KVM": (
        (372302, 2, ("VCC-416527", "VCC-233600")),
        (366901, 1, ("VCC-233422",)),
    ),
    "AUM": (
        (393203, 3, ("VCC-230920", "VCC-229865", "VCC-230921")),
        (393202, 1, ("VCC-227988",)),
    ),
    "IAM": ((393204, 3, ("VCC-244385", "VCC-244421", "VCC-464571")),),
    "UEM": ((372305, 1, ("VCC-231756",)),),
    "DDM": (
        (831701, 6, ("VCC-228101", "VCC-229910", "VCC-230060")),
        (372302, 2, ("VCC-312419", "VCC-431463")),
    ),
    "PDM": (
        (831703, 3, ("VCC-230942", "VCC-233272", "VCC-233609")),
        (372302, 2, ("VCC-312418", "VCC-431475")),
    ),
    "REM": (
        (372303, 3, ("VCC-233220", "VCC-233221", "VCC-250632")),
        (366206, 1, ("VCC-236357",)),
    ),
}


def _build_type_to_names() -> Dict[int, Tuple[str, ...]]:
    result: Dict[int, List[str]] = {}
    for name, candidates in SCRIPT_NAME_EVIDENCE.items():
        for value, _count, _docnos in candidates:
            names = result.setdefault(value, [])
            if name not in names:
                names.append(name)
    return {value: tuple(sorted(names)) for value, names in result.items()}


TYPE_TO_NAMES = _build_type_to_names()


class AddressMapError(ValueError):
    """Raised when an input profile or script cannot be read."""


@dataclass(frozen=True)
class Module:
    """One module from a saved discovery profile."""

    name: str
    request_id: int
    response_id: Optional[int]
    index: int

    @property
    def is_placeholder(self) -> bool:
        return PLACEHOLDER_NAME.match(self.name) is not None


@dataclass(frozen=True)
class Candidate:
    """One logical identifier that a name may refer to."""

    name: str
    kind: str  # "ecu_address" or "ecu_type"
    value: int
    occurrences: int
    docnos: Tuple[str, ...]
    source: str

    @property
    def address_key(self) -> str:
        if self.kind == "ecu_address":
            return "0x{:X}".format(self.value)
        return str(self.value)


@dataclass
class Entry:
    """A selected name-to-CAN-ID mapping."""

    module: Module
    candidate: Candidate
    confidence: str
    source: str
    notes: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.module.name,
            "request_id": self.module.request_id,
            "request_id_hex": _hex_id(self.module.request_id),
            "response_id": self.module.response_id,
            "response_id_hex": _optional_hex_id(self.module.response_id),
            "logical_kind": self.candidate.kind,
            "logical_value": self.candidate.value,
            "address_key": self.candidate.address_key,
            "can_id": self.module.request_id,
            "can_id_hex": _hex_id(self.module.request_id),
            "confidence": self.confidence,
            "source": self.source,
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Profile:
    path: str
    modules: Tuple[Module, ...]


def _hex_id(value: int) -> str:
    return "0x{:03X}".format(value)


def _optional_hex_id(value: Optional[int]) -> Optional[str]:
    return None if value is None else _hex_id(value)


def _parse_integer(value: Any, what: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise AddressMapError(
            "{} must be an integer or a numeric string, got {!r}".format(what, value)
        )
    if isinstance(value, int):
        return value
    text = value.strip()
    if not text:
        raise AddressMapError("{} is empty".format(what))
    lowered = text.lower()
    if lowered.startswith("0x"):
        base = 16
        digits = text[2:]
    elif text.isdigit():
        base = 10
        digits = text
    else:
        base = 16
        digits = text
    if not digits:
        raise AddressMapError("{} is empty".format(what))
    try:
        return int(digits, base)
    except ValueError as exc:
        raise AddressMapError(
            "{} is not a valid number: {!r}".format(what, value)
        ) from exc


def _can_id(value: Any, what: str) -> int:
    identifier = _parse_integer(value, what)
    if not 0 <= identifier <= MAX_CAN_ID:
        raise AddressMapError(
            "{} must be an 11-bit CAN ID from 0x000 to 0x7FF, got {:#x}".format(
                what, identifier
            )
        )
    if identifier == FUNCTIONAL_REQUEST_ID:
        raise AddressMapError(
            "{} is the functional broadcast ID 0x7DF; a physical request ID is "
            "required".format(what)
        )
    if not 0x700 <= identifier <= 0x7F7:
        raise AddressMapError(
            "{} must be in the physical diagnostic range 0x700..0x7F7, got "
            "{:#x}".format(what, identifier)
        )
    return identifier


def load_profile(path: str) -> Profile:
    """Read a saved module profile with a top-level ``modules`` list."""

    file_path = Path(path)
    try:
        text = file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise AddressMapError("cannot read profile {}: {}".format(path, exc)) from exc
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AddressMapError("invalid profile JSON {}: {}".format(path, exc)) from exc
    if not isinstance(document, Mapping) or not isinstance(
        document.get("modules"), list
    ):
        raise AddressMapError(
            "profile {} must contain a top-level modules list".format(path)
        )

    modules: List[Module] = []
    names: Dict[str, int] = {}
    requests: Dict[int, str] = {}
    for index, item in enumerate(document["modules"]):
        if not isinstance(item, Mapping):
            raise AddressMapError("modules[{}] must be an object".format(index))
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise AddressMapError(
                "modules[{}].name must be a non-empty string".format(index)
            )
        clean_name = name.strip()
        if clean_name.upper() in names:
            raise AddressMapError(
                "duplicate module name {!r} at modules[{}] and modules[{}]".format(
                    clean_name, names[clean_name.upper()], index
                )
            )
        names[clean_name.upper()] = index
        if "request_id" not in item:
            raise AddressMapError(
                "modules[{}] requires request_id".format(index)
            )
        request_id = _can_id(
            item["request_id"], "modules[{}].request_id".format(index)
        )
        if request_id in requests:
            raise AddressMapError(
                "duplicate request_id {} at modules[{}] and modules[{}]".format(
                    _hex_id(request_id), requests[request_id], index
                )
            )
        requests[request_id] = index
        response_id: Optional[int] = None
        if item.get("response_id") is not None:
            response_id = _parse_integer(
                item["response_id"], "modules[{}].response_id".format(index)
            )
            if not 0 <= response_id <= MAX_CAN_ID:
                raise AddressMapError(
                    "modules[{}].response_id must be an 11-bit CAN ID".format(index)
                )
            if response_id == request_id:
                raise AddressMapError(
                    "modules[{}].response_id must differ from request_id".format(
                        index
                    )
                )
        modules.append(
            Module(
                name=clean_name,
                request_id=request_id,
                response_id=response_id,
                index=index,
            )
        )
    if not modules:
        raise AddressMapError("profile {} contains no modules".format(path))
    return Profile(path=str(file_path), modules=tuple(modules))


def _decode_script_text(data: bytes, source: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        encoding = "utf-8-sig"
    elif data.startswith((b"\xff\xfe", b"\xfe\xff")):
        encoding = "utf-16"
    else:
        match = re.search(
            br"""<\?xml[^>]*encoding=["']([A-Za-z0-9._-]+)["']""", data[:200]
        )
        encoding = match.group(1).decode("ascii") if match else "utf-8"
    try:
        return data.decode(encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        raise AddressMapError(
            "script {} is not valid {}: {}".format(source, encoding, exc)
        ) from exc


def load_script(path: str) -> DiagnosticScript:
    """Read a script ZIP or XML document without executing it."""

    file_path = Path(path)
    try:
        data = file_path.read_bytes()
    except OSError as exc:
        raise AddressMapError("cannot read script {}: {}".format(path, exc)) from exc
    try:
        if zipfile.is_zipfile(io.BytesIO(data)):
            return parse_script_zip(data)
        return parse_script_xml(_decode_script_text(data, path))
    except DiagnosticScriptError as exc:
        raise AddressMapError("cannot parse script {}: {}".format(path, exc)) from exc


def _title_tokens(title: Optional[str]) -> Tuple[str, ...]:
    if not title:
        return ()
    return tuple(
        token.upper() for token in re.findall(r"[A-Za-z][A-Za-z0-9_]*", title)
    )


def _candidates_from_script(
    script: DiagnosticScript, known_names: Sequence[str]
) -> Dict[str, List[Candidate]]:
    """Return name candidates that one script's title and nodes support.

    A title must contain exactly one known module label as a standalone token.
    Each node contributes one candidate: its ``EcuAddress`` when present,
    otherwise a non-zero ``EcuType``.  The address is preferred because the
    runner resolves it before the type.
    """

    tokens = set(_title_tokens(script.title))
    matches = [
        name
        for name in known_names
        if name.upper() in tokens and name.upper() not in TITLE_STOPWORDS
    ]
    if len(matches) != 1:
        return {}
    name = matches[0]
    docno = script.docno or "(no document number)"
    # The runner resolves EcuAddress before EcuType.  When a node carries both,
    # keep the address as the candidate for that node and do not turn the pair
    # into two competing candidates.
    found: Dict[Tuple[str, int], Candidate] = {}
    for plan in script.ecus:
        if plan.ecu_address is not None:
            kind, value = "ecu_address", plan.ecu_address
            label = "EcuAddress"
        elif plan.ecu_type not in (None, 0):
            kind, value = "ecu_type", plan.ecu_type
            label = "EcuType"
        else:
            continue
        found[(kind, value)] = Candidate(
            name=name,
            kind=kind,
            value=value,
            occurrences=1,
            docnos=(docno,),
            source="script title {!r} and parsed {} (document {})".format(
                script.title, label, docno
            ),
        )
    return {name: sorted(found.values(), key=lambda item: (item.kind, item.value))}


def _merge_candidates(
    target: Dict[str, Dict[Tuple[str, int], Candidate]],
    name: str,
    candidates: Iterable[Candidate],
) -> None:
    bucket = target.setdefault(name.upper(), {})
    for candidate in candidates:
        key = (candidate.kind, candidate.value)
        current = bucket.get(key)
        if current is None:
            bucket[key] = candidate
            continue
        docnos = tuple(dict.fromkeys(current.docnos + candidate.docnos))
        bucket[key] = Candidate(
            name=current.name,
            kind=current.kind,
            value=current.value,
            occurrences=current.occurrences + candidate.occurrences,
            docnos=docnos[:3],
            source=current.source if current.occurrences >= candidate.occurrences
            else candidate.source,
        )


def _built_in_candidates(name: str) -> List[Candidate]:
    candidates: List[Candidate] = []
    for value, count, docnos in SCRIPT_NAME_EVIDENCE.get(name.upper(), ()):
        candidates.append(
            Candidate(
                name=name.upper(),
                kind="ecu_type",
                value=value,
                occurrences=count,
                docnos=tuple(docnos),
                source=(
                    "script title match: {} document(s), e.g. {}".format(
                        count, docnos[0] if docnos else "(unknown)"
                    )
                ),
            )
        )
    return candidates


def _select(
    candidates: Sequence[Candidate], selection: str
) -> Tuple[List[Candidate], str, Optional[str]]:
    """Return selected candidates, a confidence, and an ambiguity reason."""

    ordered = sorted(
        candidates, key=lambda item: (-item.occurrences, item.value)
    )
    if not ordered:
        return [], "", "no script-title evidence names this module"
    if len(ordered) == 1:
        return [ordered[0]], "medium", None
    if selection == "all":
        return ordered, "low", None
    if selection == "dominant":
        top = ordered[0]
        runner_up = ordered[1]
        if top.occurrences > runner_up.occurrences:
            return [top], "low", None
        return [], "", (
            "ambiguous: the leading script-title candidates are tied "
            "({} and {} documents)".format(top.occurrences, runner_up.occurrences)
        )
    return [], "", (
        "ambiguous: {} script-title candidates ({}); pass --selection dominant "
        "or --selection all to inspect them".format(
            len(ordered),
            ", ".join(
                "{}={}".format(item.kind, item.value) for item in ordered
            ),
        )
    )


def build_document(
    profile: Optional[Profile],
    scripts: Sequence[Tuple[str, DiagnosticScript]],
    selection: str = "unique",
) -> Dict[str, Any]:
    """Build the JSON document for the requested inputs."""

    known_names = sorted(SCRIPT_NAME_EVIDENCE)
    if profile is not None:
        known_names = sorted(
            set(known_names)
            | {module.name.upper() for module in profile.modules}
        )

    candidates: Dict[str, Dict[Tuple[str, int], Candidate]] = {}
    for name in known_names:
        _merge_candidates(candidates, name, _built_in_candidates(name))
    for path, script in scripts:
        for name, found in _candidates_from_script(script, known_names).items():
            _merge_candidates(candidates, name, found)

    warnings: List[str] = []
    entries: List[Entry] = []
    unmapped_by_index: Dict[int, Dict[str, Any]] = {}
    used_addresses: Dict[str, Entry] = {}

    if profile is not None:
        for module in profile.modules:
            key = module.name.upper()
            module_candidates = list(candidates.get(key, {}).values())
            if module.is_placeholder:
                unmapped_by_index[module.index] = _unmapped(
                    module,
                    "name is a discovery placeholder, not an ECU label",
                    module_candidates,
                )
                continue
            if key not in SCRIPT_NAME_EVIDENCE and not module_candidates:
                unmapped_by_index[module.index] = _unmapped(
                    module,
                    "no script title in the recovered evidence names this module",
                    module_candidates,
                )
                continue
            selected, confidence, reason = _select(module_candidates, selection)
            if not selected:
                unmapped_by_index[module.index] = _unmapped(
                    module, reason, module_candidates
                )
                continue
            for candidate in selected:
                notes: List[str] = []
                known_id = KNOWN_REQUEST_IDS.get(key)
                if known_id is not None and known_id == module.request_id:
                    notes.append(
                        "request ID agrees with the built-in discovery table"
                    )
                elif known_id is not None:
                    notes.append(
                        "request ID differs from the built-in discovery table "
                        "({} expected)".format(_hex_id(known_id))
                    )
                if candidate.occurrences > 1:
                    notes.append(
                        "{} script documents support this candidate".format(
                            candidate.occurrences
                        )
                    )
                if confidence == "low":
                    notes.append(
                        "candidate selected by policy, not by a unique match"
                    )
                entry = Entry(
                    module=module,
                    candidate=candidate,
                    confidence=confidence,
                    source="{}; profile request_id {}".format(
                        candidate.source, _hex_id(module.request_id)
                    ),
                    notes=notes,
                )
                existing = used_addresses.get(candidate.address_key)
                if (
                    existing is not None
                    and existing.module.request_id != module.request_id
                ):
                    conflict = (
                        "logical key {} is claimed by {} ({}) and {} ({}); "
                        "dropping both".format(
                            candidate.address_key,
                            existing.module.name,
                            _hex_id(existing.module.request_id),
                            module.name,
                            _hex_id(module.request_id),
                        )
                    )
                    warnings.append(conflict)
                    entries = [
                        item
                        for item in entries
                        if item.candidate.address_key != candidate.address_key
                    ]
                    used_addresses.pop(candidate.address_key, None)
                    unmapped_by_index[module.index] = _unmapped(
                        module, conflict, [candidate]
                    )
                    unmapped_by_index[existing.module.index] = _unmapped(
                        existing.module, conflict, [existing.candidate]
                    )
                    continue
                used_addresses[candidate.address_key] = entry
                entries.append(entry)

    entries.sort(
        key=lambda item: (item.module.name.upper(), item.candidate.address_key)
    )
    addresses: Dict[str, str] = {}
    for entry in entries:
        addresses[entry.candidate.address_key] = _hex_id(entry.module.request_id)

    mapped_indexes = {entry.module.index for entry in entries}
    unmapped = [
        item
        for index, item in sorted(unmapped_by_index.items())
        if index not in mapped_indexes
    ]

    document: Dict[str, Any] = {
        "schema": SCHEMA_NAME,
        "version": SCHEMA_VERSION,
        "selection": selection,
        "profile": None
        if profile is None
        else {"path": profile.path, "module_count": len(profile.modules)},
        "script": _script_summary(scripts, entries),
        "addresses": addresses,
        "entries": [entry.to_dict() for entry in entries],
        "unmapped": unmapped,
        "warnings": warnings,
    }
    return document


def _unmapped(
    module: Module, reason: str, candidates: Sequence[Candidate]
) -> Dict[str, Any]:
    return {
        "name": module.name,
        "request_id": module.request_id,
        "request_id_hex": _hex_id(module.request_id),
        "response_id": module.response_id,
        "response_id_hex": _optional_hex_id(module.response_id),
        "reason": reason,
        "candidates": [
            {
                "logical_kind": candidate.kind,
                "logical_value": candidate.value,
                "address_key": candidate.address_key,
                "documents": candidate.occurrences,
                "example_documents": list(candidate.docnos),
                "source": candidate.source,
            }
            for candidate in sorted(
                candidates, key=lambda item: (-item.occurrences, item.value)
            )
        ],
    }


def _script_summary(
    scripts: Sequence[Tuple[str, DiagnosticScript]],
    entries: Sequence[Entry],
) -> Optional[Dict[str, Any]]:
    if not scripts:
        return None
    selected_by_key = {
        entry.candidate.address_key: entry for entry in entries
    }
    result: List[Dict[str, Any]] = []
    for path, script in scripts:
        logical: Dict[Tuple[Optional[int], Optional[int]], List[int]] = {}
        for plan in script.ecus:
            key = (plan.ecu_address, plan.ecu_type)
            services = logical.setdefault(key, [])
            for service_id in plan.service_ids:
                if service_id not in services:
                    services.append(service_id)
        logical_entries: List[Dict[str, Any]] = []
        for (address, ecu_type), services in sorted(
            logical.items(),
            key=lambda item: (
                item[0][0] is None,
                item[0][0] if item[0][0] is not None else -1,
                item[0][1] is None,
                item[0][1] if item[0][1] is not None else -1,
            ),
        ):
            names: List[str] = []
            if ecu_type is not None:
                names = list(TYPE_TO_NAMES.get(ecu_type, ()))
            resolved = None
            resolved_key = None
            if address is not None:
                resolved_key = "0x{:X}".format(address)
            elif ecu_type is not None:
                resolved_key = str(ecu_type)
            if resolved_key is not None and resolved_key in selected_by_key:
                selected = selected_by_key[resolved_key]
                resolved = {
                    "name": selected.module.name,
                    "can_id": selected.module.request_id,
                    "can_id_hex": _hex_id(selected.module.request_id),
                    "confidence": selected.confidence,
                    "source": selected.source,
                }
            logical_entries.append(
                {
                    "ecu_address": address,
                    "ecu_address_hex": None
                    if address is None
                    else "0x{:X}".format(address),
                    "ecu_type": ecu_type,
                    "service_ids": services,
                    "name_candidates": names,
                    "resolved": resolved,
                }
            )
        result.append(
            {
                "path": path,
                "docno": script.docno,
                "title": script.title,
                "language": script.language,
                "script_type": script.script_type,
                "profile_ids": list(script.profile_ids),
                "logical_addresses": logical_entries,
            }
        )
    return {"scripts": result}


def _fragment_text(raw: bytes) -> str:
    """Decode a title or property value from a possibly damaged body."""

    for encoding in ("utf-8-sig", "utf-16", "latin-1"):
        try:
            return raw.decode(encoding)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", "replace")


def scan_image_fragments(path: str) -> Dict[str, Any]:
    """Scan ZIP streams in a database image without parsing the XML.

    A damaged body can fail XML parsing and still expose a script title, a
    document number and one or more ``EcuAddress``/``EcuType`` properties.  The
    scanner inflates every local ZIP header whose body starts with an XML
    marker and records those properties with a loose regular expression.  The
    result is evidence only: it is not used to select a mapping, because a
    title without a module name cannot establish which module an address
    belongs to.
    """

    file_path = Path(path)
    try:
        handle = file_path.open("rb")
    except OSError as exc:
        raise AddressMapError("cannot read image {}: {}".format(path, exc)) from exc

    stats: Counter = Counter(
        {
            "headers": 0,
            "header_overrun": 0,
            "encrypted": 0,
            "unsupported_method": 0,
            "bad_size": 0,
            "truncated": 0,
            "inflate_failed": 0,
            "inflate_incomplete": 0,
            "not_xml": 0,
            "inflated_xml": 0,
            "crc_ok": 0,
            "crc_mismatch": 0,
            "with_ecu_address": 0,
            "with_mode_ecu_address": 0,
        }
    )
    address_counts: Counter = Counter()
    pair_counts: Counter = Counter()
    documents: Dict[Tuple[Any, ...], Dict[str, Any]] = {}

    try:
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
                if not (
                    0 < compressed_size <= MAX_FRAGMENT_BYTES
                    and 0 < uncompressed_size <= MAX_FRAGMENT_BYTES
                ):
                    stats["bad_size"] += 1
                    continue
                data_start = offset + 30 + name_length + extra_length
                if data_start + compressed_size > len(data):
                    stats["truncated"] += 1
                    continue
                try:
                    decompressor = zlib.decompressobj(-15)
                    body = decompressor.decompress(
                        data[data_start : data_start + compressed_size],
                        MAX_FRAGMENT_BYTES,
                    )
                    body += decompressor.flush()
                except zlib.error:
                    stats["inflate_failed"] += 1
                    continue
                if not decompressor.eof:
                    stats["inflate_incomplete"] += 1
                    continue
                if not body.lstrip().startswith(_FRAGMENT_XML_START):
                    stats["not_xml"] += 1
                    continue
                stats["inflated_xml"] += 1
                if zlib.crc32(body) == crc:
                    stats["crc_ok"] += 1
                else:
                    stats["crc_mismatch"] += 1

                addresses = _FRAGMENT_ADDRESS.findall(body)
                if not addresses:
                    continue
                stats["with_ecu_address"] += 1
                has_mode = _FRAGMENT_MODE.search(body) is not None
                if has_mode:
                    stats["with_mode_ecu_address"] += 1
                types = _FRAGMENT_TYPE.findall(body)
                docno_match = _FRAGMENT_DOCNO.search(body)
                title_match = _FRAGMENT_TITLE.search(body)
                docno = (
                    _fragment_text(docno_match.group(1)) if docno_match else None
                )
                title = (
                    _fragment_text(title_match.group(1)).strip()
                    if title_match
                    else None
                )
                address_texts = tuple(
                    _fragment_text(item).strip() for item in addresses
                )
                type_texts = tuple(
                    _fragment_text(item).strip() for item in types
                )
                for address_text in address_texts:
                    address_counts[address_text] += 1
                    for type_text in type_texts or ("",):
                        pair_counts[(address_text, type_text)] += 1

                key = (docno, title, address_texts, type_texts, has_mode)
                current = documents.get(key)
                if current is None:
                    documents[key] = {
                        "docno": docno,
                        "title": title,
                        "ecu_addresses": list(address_texts),
                        "ecu_types": list(type_texts),
                        "mode_ecu_address": has_mode,
                        "crc_ok": zlib.crc32(body) == crc,
                        "offset": hex(offset),
                        "occurrences": 1,
                    }
                else:
                    current["occurrences"] += 1
    except (ValueError, OSError) as exc:
        raise AddressMapError(
            "cannot scan image {}: {}".format(path, exc)
        ) from exc
    finally:
        handle.close()

    ordered_documents = sorted(
        documents.values(),
        key=lambda item: (-item["occurrences"], item["docno"] or "", item["title"] or ""),
    )
    return {
        "path": str(file_path),
        "stats": dict(stats),
        "address_counts": [
            {"value": value, "occurrences": count}
            for value, count in address_counts.most_common()
        ],
        "pairs": [
            {
                "ecu_address": address,
                "ecu_type": ecu_type or None,
                "occurrences": count,
            }
            for (address, ecu_type), count in pair_counts.most_common()
        ],
        "documents": ordered_documents,
    }


def _load_inputs(
    profile_path: Optional[str], script_paths: Sequence[str]
) -> Tuple[Optional[Profile], List[Tuple[str, DiagnosticScript]]]:
    profile = load_profile(profile_path) if profile_path else None
    scripts: List[Tuple[str, DiagnosticScript]] = []
    for path in script_paths:
        scripts.append((path, load_script(path)))
    return profile, scripts


def _write_output(
    document: Mapping[str, Any], out_path: Optional[str], indent: int
) -> None:
    text = json.dumps(document, indent=indent, sort_keys=False) + "\n"
    if out_path:
        try:
            Path(out_path).write_text(text, encoding="utf-8")
        except OSError as exc:
            raise AddressMapError("cannot write {}: {}".format(out_path, exc)) from exc
    else:
        sys.stdout.write(text)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Recover or derive a logical-address to CAN-ID map from a saved "
            "module profile and recovered script plans.  The output is JSON "
            "with an addresses object plus a confidence and source per entry."
        )
    )
    parser.add_argument(
        "--profile",
        help="saved module profile JSON with a top-level modules list",
    )
    parser.add_argument(
        "--script",
        action="append",
        default=[],
        help="script ZIP or XML to inspect; repeat for several scripts",
    )
    parser.add_argument(
        "--scan-image",
        action="append",
        default=[],
        help=(
            "database image to scan for damaged ZIP bodies carrying "
            "EcuAddress properties; repeat for several images"
        ),
    )
    parser.add_argument(
        "--selection",
        choices=("unique", "dominant", "all"),
        default="unique",
        help=(
            "candidate policy: unique (default), dominant (most documents), "
            "or all candidates; non-unique choices are marked low confidence"
        ),
    )
    parser.add_argument("--out", help="write JSON to this path instead of stdout")
    parser.add_argument(
        "--indent", type=int, default=2, help="JSON indentation, default 2"
    )
    args = parser.parse_args(argv)

    if not args.profile and not args.script and not args.scan_image:
        parser.error(
            "at least one of --profile, --script or --scan-image is required"
        )
    if args.indent < 0:
        parser.error("--indent must not be negative")

    try:
        profile, scripts = _load_inputs(args.profile, args.script)
        document = build_document(profile, scripts, selection=args.selection)
        if args.scan_image:
            document["fragment_scan"] = [
                scan_image_fragments(path) for path in args.scan_image
            ]
        _write_output(document, args.out, args.indent)
    except AddressMapError as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
