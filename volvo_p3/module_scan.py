"""Scan explicitly supplied Volvo module IDs with read-only UDS DTC requests."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

from .dice_can import P3_D6_MODULES
from .uds_read import read_uds_dtcs


FUNCTIONAL_REQUEST_ID = 0x7DF


@dataclass(frozen=True)
class ModuleTarget:
    """A physical standard-ID CAN request/response pair for one ECU."""

    name: str
    request_id: int
    response_id: int


ProgressCallback = Callable[[int, int, ModuleTarget, dict[str, Any]], None]


def _target_id(value: Any, field_name: str) -> int:
    if type(value) is int:
        identifier = value
    elif isinstance(value, str) and value.lower().startswith("0x"):
        try:
            identifier = int(value, 16)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an integer or 0x-prefixed CAN ID") from exc
    else:
        raise ValueError(f"{field_name} must be an integer or 0x-prefixed CAN ID")
    if not 0 <= identifier <= 0x7FF:
        raise ValueError(f"{field_name} must be an 11-bit CAN ID from 0x000 to 0x7FF")
    return identifier


def _validate_targets(targets: Iterable[ModuleTarget]) -> list[ModuleTarget]:
    result = list(targets)
    seen_names: set[str] = set()
    seen_requests: set[int] = set()
    seen_responses: set[int] = set()
    for target in result:
        if not isinstance(target, ModuleTarget):
            raise ValueError("targets must contain ModuleTarget values")
        if not isinstance(target.name, str) or not target.name.strip():
            raise ValueError("module target names must not be empty")
        if target.name in seen_names:
            raise ValueError(f"duplicate module name: {target.name}")
        seen_names.add(target.name)
        for field_name, identifier, seen_ids in (
            ("request_id", target.request_id, seen_requests),
            ("response_id", target.response_id, seen_responses),
        ):
            if type(identifier) is not int or not 0 <= identifier <= 0x7FF:
                raise ValueError(f"{field_name} must be an 11-bit CAN ID")
            if identifier in seen_ids:
                raise ValueError(f"duplicate {field_name}: 0x{identifier:03X}")
            seen_ids.add(identifier)
        if not 0x700 <= target.request_id <= 0x7F7:
            raise ValueError("physical request ID must be within diagnostic range 0x700..0x7F7")
        if target.request_id == FUNCTIONAL_REQUEST_ID:
            raise ValueError("request_id 0x7DF is functional; a physical request ID is required")
        if target.request_id == target.response_id:
            raise ValueError("request_id and response_id must differ")
    return result


def default_module_targets() -> list[ModuleTarget]:
    """Return the built-in candidate P3 D6 targets, preserving their labels."""
    targets = [
        ModuleTarget(name, request_id, request_id + 8)
        for name, request_id in P3_D6_MODULES.items()
    ]
    return _validate_targets(targets)


def load_module_profile(path: str | Path) -> list[ModuleTarget]:
    """Load physical module IDs from JSON with a top-level ``modules`` list.

    Each entry requires ``name``, ``request_id``, and ``response_id``. IDs may
    be JSON integers or strings prefixed with ``0x``.
    """
    try:
        profile = json.loads(Path(path).read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid module profile JSON: {exc}") from exc
    if not isinstance(profile, dict) or not isinstance(profile.get("modules"), list):
        raise ValueError("module profile must contain a top-level modules list")

    targets: list[ModuleTarget] = []
    for index, item in enumerate(profile["modules"]):
        if not isinstance(item, dict):
            raise ValueError(f"modules[{index}] must be an object")
        name = item.get("name")
        if not isinstance(name, str) or not name.strip():
            raise ValueError(f"modules[{index}].name must be a non-empty string")
        if "request_id" not in item or "response_id" not in item:
            raise ValueError(f"modules[{index}] requires request_id and response_id")
        targets.append(
            ModuleTarget(
                name=name.strip(),
                request_id=_target_id(item["request_id"], f"modules[{index}].request_id"),
                response_id=_target_id(item["response_id"], f"modules[{index}].response_id"),
            )
        )
    if not targets:
        raise ValueError("module profile must contain at least one module")
    return _validate_targets(targets)


def build_discovery_targets(
    start: int = 0x700,
    end: int = 0x7F7,
    response_offset: int = 8,
) -> list[ModuleTarget]:
    """Build standard-ID physical targets over an inclusive request-ID range."""
    for name, value in (("start", start), ("end", end)):
        if type(value) is not int or not 0 <= value <= 0x7FF:
            raise ValueError(f"{name} must be an 11-bit CAN ID")
    if start > end:
        raise ValueError("start must be less than or equal to end")
    if type(response_offset) is not int or not 1 <= response_offset <= 0x7FF:
        raise ValueError("response_offset must be between 1 and 0x7FF")

    known_names = {request_id: name for name, request_id in P3_D6_MODULES.items()}
    targets = []
    for request_id in range(start, end + 1):
        if request_id == FUNCTIONAL_REQUEST_ID:
            continue
        response_id = request_id + response_offset
        if response_id > 0x7FF:
            continue
        targets.append(
            ModuleTarget(
                name=known_names.get(request_id, f"CAN_{request_id:03X}"),
                request_id=request_id,
                response_id=response_id,
            )
        )
    return _validate_targets(targets)


def scan_modules(
    bus: Any,
    targets: Optional[Iterable[ModuleTarget]] = None,
    *,
    timeout: float = 1.5,
    progress: Optional[ProgressCallback] = None,
) -> dict[str, Any]:
    """Read each target sequentially and summarize effective fault records.

    ``progress`` is called after each target with its one-based index, total
    target count, target, and JSON-serializable per-module result.
    """
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and positive")
    selected = _validate_targets(default_module_targets() if targets is None else targets)
    modules: list[dict[str, Any]] = []
    for index, target in enumerate(selected, start=1):
        try:
            result = read_uds_dtcs(
                bus,
                target.request_id,
                target.response_id,
                timeout=timeout,
            )
        except (RuntimeError, OSError):
            # The caller saves each completed target. Stop if the transport fails.
            raise
        except ValueError as exc:
            result = {
                "request_id": target.request_id,
                "request_id_hex": f"0x{target.request_id:03X}",
                "response_id": target.response_id,
                "response_id_hex": f"0x{target.response_id:03X}",
                "status": "error",
                "error": str(exc),
                "dtcs": None,
            }
        module = {"module": target.name, **result}
        modules.append(module)
        if progress is not None:
            progress(index, len(selected), target, module)

    status_counts: dict[str, int] = {}
    for module in modules:
        status = module.get("status", "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1

    summary = {
        "target_count": len(modules),
        "status_counts": status_counts,
        "dtc_record_count": sum(module.get("dtc_count", 0) for module in modules),
        "effective_fault_record_count": sum(
            module.get("fault_record_count", 0)
            for module in modules
            if module.get("status") == "ok"
        ),
        "modules_with_effective_faults": sum(
            module.get("fault_record_count", 0) > 0
            for module in modules
            if module.get("status") == "ok"
        ),
    }
    return {
        "status": "read" if any(module.get("status") == "ok" for module in modules)
        else "no_dtc_readout",
        "modules": modules,
        "summary": summary,
    }
