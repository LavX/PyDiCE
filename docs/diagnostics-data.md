# Diagnostic data views in PyDiCE

This is the read-only layer that turns recovered service data and diagnostic
scripts into views a PyDiCE user can work with. It does not program modules,
change diagnostic sessions, request security access, or clear faults.

## What is implemented

- A versioned diagnostic catalog export and runtime lookup for DTC descriptions
  and request timing.
- A parser for diagnostic script ZIP and XML bodies.
- A diagnostics report that merges saved DiCE scans with catalog descriptions.
- Views for DTCs, network status and fault trace.
- A guarded script runner that sends only read services and requires an
  explicit logical-address to CAN-ID map.
- CLI commands and a `Service Data` menu in the TUI.

## Build a catalog

The exporter reads the MDF files directly. It needs no SQL Server and does not
modify the source image.

```sh
python3 tools/export_diag_catalog.py \
  --image /mnt/vida \
  --out reports/diag_catalog.json
```

The current image produces 9,033 DTC descriptions, 41 timing rows, 134,459
distinct vehicle profiles and 6,567 script entries (2,977 document numbers in
40 languages). The DTC text comes from `T150_BlockValue`, `T191_TextData` and
`T141_Block`; profiles come from `BaseData_Data.MDF`; scripts are carved from
`DiagSwdlRepository_Data.MDF` and kept with a CRC status.

Some recovered strings carry single-character corruption from the MDF text
storage. The exporter sanitizes replacement characters and repairs rare tokens
that are one bit away from a common correct form. The replacement pairs are in
the raw dump, so the descriptions remain a useful draft, not a byte-exact copy.
See `docs/diag-catalog-export.md` for the exact coverage and blockers.

## Commands

Inspect the catalog:

```sh
python3 -m volvo_p3.cli diag-catalog --catalog reports/diag_catalog.json
python3 -m volvo_p3.cli diag-catalog \
  --catalog reports/diag_catalog.json --code P034211
```

Build diagnostic views from a saved scan:

```sh
python3 -m volvo_p3.cli diag-report \
  --scan reports/dice-scan-TIMESTAMP.json \
  --catalog reports/diag_catalog.json \
  --view all
```

Attach the same diagnostics to a live scan:

```sh
sudo python3 -m volvo_p3.cli dice-scan \
  --discover --timeout 0.5 \
  --diag-catalog reports/diag_catalog.json
```

The saved report then carries a `diagnostics` object with module, network,
fault-trace and description data alongside the raw scan evidence.

Parse a script without executing it:

```sh
python3 -m volvo_p3.cli diag-script --script path/to/script.zip
```

Plan a script run against a logical-address map:

```sh
python3 -m volvo_p3.cli diag-run-script \
  --script path/to/script.zip \
  --address-map reports/address_map.json
```

Add `--execute` to send the planned read requests through the native DiCE
transport. Without it, the command prints the plan and sends nothing.

The address map is explicit because `T121_Config` is empty in the shipped
image. It maps the script's logical `EcuAddress` or `EcuType` to an 11-bit
physical request ID:

```json
{
  "addresses": {
    "0x11": "0x7E0",
    "0x47": "0x726",
    "381101": "0x7E0"
  }
}
```

Requests without a mapping are skipped and reported. They are never guessed.

## TUI

The TUI has a `Service Data` menu with:

- Load Catalog
- DTC Overview
- Fault Trace
- Network Status
- Script Plan

The DTC and network views read a saved scan report. They do not open the
adapter themselves.

## Safety

The script parser keeps only read services (`0x01`, `0x09`, `0x19`, `0x22`).
The runner never emits session control, security access, clears, writes,
routines, or resets. The native CAN transmit allowlist is still separate and
unchanged for the existing commands.

## Limits

- `T121_Config` is empty in this snapshot, so the logical-to-CAN map is manual.
- The script store is only partly recoverable: 89,229 headers were carved, but
  43,757 streams did not inflate and 36,899 inflated streams did not parse as
  XML. Damaged bodies are counted, not claimed as complete coverage.
- Symptom, cause and freeze-frame tables have no page set in this snapshot.
  `T152`, `T153`, `T154`, `T156`, `T157`, `T158`, `T159`, `T147` and `T148`
  cannot supply rows; the raw dump records the exact blockers.
- The script runner has unit coverage but has not been validated on a car in
  this workspace.

## Tests

```sh
python3 -m pytest -q
```

The tests cover catalog loading and lookup, script parsing and service
filtering, diagnostic report merging, CLI output, TUI helpers and the runner's
fake-transport behavior.
