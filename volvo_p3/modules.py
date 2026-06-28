"""Seed module addresses for Volvo P2/P3 diagnostic discovery.

These are intentionally only hints. VIDA-derived data or discovery captures
from the target car should replace or extend this list.
"""

KNOWN_MODULES = {
    "DIM": 0x51,
    "ECM": 0x7A,
}


def parse_ecu(value: str) -> int:
    key = value.upper()
    if key in KNOWN_MODULES:
        return KNOWN_MODULES[key]
    return int(value, 0)
