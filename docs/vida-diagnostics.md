# VIDA-style diagnostics in PyDiCE

This is the read-only layer that turns the recovered VIDA data and scripts into
views a PyDiCE user can work with. It does not program modules, change
diagnostic sessions, request security access, or clear faults.

## What is implemented

- A versioned VIDA catalog export and runtime lookup for DTC descriptions and
  request timing.
- A parser for VIDA script ZIP/XML bodies.
- A diagnostics report that merges saved DiCE scans with catalog descriptions.
- VIDA-style views for DTCs, network status and fault trace.
- A guarded script runner that sends only read services and requires an
  explicit logical-address to CAN-ID map.
- CLI commands and a VIDA menu in the TUI.

## Build a catalog

The exporter reads the MDF files directly. It needs no SQL Server and does not
modify the image.

```sh
python3 tools/export_vida_catalog.py \
  --image /mnt/vida \
  --out reports/vida_catalog.json
```

The current image produces 9,018 DTC descriptions and 41 timing rows. Profiles
and scripts are empty in the catalog because those exports are not implemented
yet. The DTC text comes from `T150_BlockValue`, `T191_TextData` and
`T141_Block`.

Some recovered strings carry single-character corruption from the MDF text
storage. Treat the descriptions as a useful draft, not a byte-exact VIDA copy.

## Commands

Inspect the catalog:

```sh
python3 -m volvo_p3.cli vida-catalog --catalog reports/vida_catalog.json
python3 -m volvo_p3.cli vida-catalog \
  --catalog reports/vida_catalog.json --code P034211
```

Build VIDA views from a saved scan:

```sh
python3 -m volvo_p3.cli vida-report \
  --scan reports/dice-scan-TIMESTAMP.json \
  --catalog reports/vida_catalog.json \
  --view all
```

Attach the same VIDA section to a live scan:

```sh
sudo python3 -m volvo_p3.cli dice-scan \
  --discover --timeout 0.5 \
  --vida-catalog reports/vida_catalog.json
```

The saved report then carries a `vida` object with module, network,
fault-trace and description data alongside the raw scan evidence.

Parse a script without executing it:

```sh
python3 -m volvo_p3.cli vida-script --script path/to/script.zip
```

Plan a script run against a logical-address map:

```sh
python3 -m volvo_p3.cli vida-run-script \
  --script path/to/script.zip \
  --address-map reports/vida_addresses.json
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

The TUI has a `VIDA` menu with:

- Load VIDA catalog
- VIDA DTC overview
- VIDA fault trace
- VIDA network status
- VIDA script plan

The DTC and network views read a saved scan report. They do not open the
adapter themselves.

## Safety

The script parser keeps only read services (`0x01`, `0x09`, `0x19`, `0x22`).
The runner never emits session control, security access, clears, writes,
routines, or resets. The native CAN transmit allowlist is still separate and
unchanged for the existing commands.

## Limits

- `T121_Config` is empty in this snapshot, so the logical-to-CAN map is manual.
- The catalog does not contain vehicle profiles or script rows yet.
- The script store is only partly recoverable: many bodies are truncated or
  damaged in the MDF.
- VIDA symptom, cause and freeze-frame tables are not exported yet.
- The script runner has unit coverage but has not been validated on a car in
  this workspace.

## Tests

```sh
python3 -m pytest -q
```

The VIDA tests cover catalog loading and lookup, script parsing and service
filtering, diagnostic report merging, CLI output, TUI helpers and the runner's
fake-transport behavior.
