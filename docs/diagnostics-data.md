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
  --out data/diag_catalog.json.xz
```

The `--out` suffix selects the encoding: `.json` for plain UTF-8, `.json.gz`
for gzip, `.json.bz2` for bzip2 and `.json.xz` for xz. The loader makes the
same choice, so every command below accepts a compressed catalog. A `.zip`
catalog is also readable when it contains exactly one JSON member.

A full dump is much larger than the catalog. Write it as a single archive
instead of a plain tree:

```sh
python3 tools/export_diag_catalog.py \
  --image /mnt/vida \
  --dump-archive data/diag_dump.tar.xz
```

`--dump-archive` also works next to `--dump-dir`, which keeps the plain tree
and packs it afterwards. The archive contains `catalog.json` at its root with
`raw/` and `scripts/` as children.

Pack an existing dump without rebuilding it:

```sh
python3 tools/pack_diag_dump.py \
  --input data/diag_dump \
  --output data/diag_dump.tar.xz
```

The packer and exporter share the same implementation. `.tar.gz`, `.tar.xz`
and `.zip` are supported; the packer prints input and output sizes and the
compression ratio.

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
python3 -m volvo_p3.cli diag-catalog --catalog data/diag_catalog.json.xz
python3 -m volvo_p3.cli diag-catalog \
  --catalog data/diag_catalog.json.xz --code P034211
```

Build diagnostic views from a saved scan:

```sh
python3 -m volvo_p3.cli diag-report \
  --scan reports/dice-scan-TIMESTAMP.json \
  --catalog data/diag_catalog.json.xz \
  --view all
```

Attach the same diagnostics to a live scan:

```sh
sudo python3 -m volvo_p3.cli dice-scan \
  --discover --timeout 0.5 \
  --diag-catalog data/diag_catalog.json.xz
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

Plan one by matching the script's ECU names to a saved module profile:

```sh
python3 -m volvo_p3.cli diag-run-script \
  --script path/to/script.zip \
  --profile reports/my-car-complete.json
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

`--profile` uses the `modules` list a saved scan already carries. Each script
`EcuName`, or its `EcuIdentifier` when the name does not match, is compared
with a module name after case, whitespace and punctuation are folded. Only an
exact folded match is used. A name claimed by two modules with different
request IDs is ambiguous and stays unmapped; a missing name stays unmapped too.
Both cases are reported with the profile path. A `--address-map` entry still
wins, because numeric lookup runs before name lookup.

## TUI

The TUI has a `Service Data` menu with:

- Load Catalog
- Live DTC Read
- DTC Overview
- Fault Trace
- Network Status
- Script Plan
- Run Script

The DTC and network views read a saved scan report. They do not open the
adapter themselves. The default catalog path is `data/diag_catalog.json.xz`;
the Load Catalog field accepts any plain or compressed catalog suffix.

`Run Script` takes the script path, an optional profile path, an optional
address map path, a per-request timeout and an explicit `Execute (yes/no)`
field. The field defaults to `no`, so the action only shows the plan, the name
matches, and every unmapped entry. Typing `yes` sends the same filtered reads
through the native transport; the read-only filter is unchanged.

## Safety

The script parser keeps only read services (`0x01`, `0x09`, `0x19`, `0x22`).
The runner never emits session control, security access, clears, writes,
routines, or resets. The native CAN transmit allowlist is still separate and
unchanged for the existing commands.

## Limits

- `T121_Config` is empty in this snapshot, so the logical-to-CAN map is manual
  or name-matched against a saved profile.
- Profile name matching is only as good as the profile labels. Placeholder
  names such as `CAN_730` cannot match a script ECU name, and a name that two
  modules claim stays unmapped.
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
