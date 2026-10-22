# Diagnostic catalog export

`tools/export_diag_catalog.py` reads the source image as pages, never attaches a
database and never executes a source binary. This note records what the export
recovers, what it cannot recover from this snapshot, and how the dump is laid
out.

The image path is the only required input. The output catalog is JSON, with an
optional compressed form selected by its suffix. The raw dump is optional and
can be written as a plain tree or one archive.

```sh
python3 tools/export_diag_catalog.py --image /mnt/vida --out catalog.json.xz
python3 tools/export_diag_catalog.py --image /mnt/vida --dump-dir /tmp/diag-dump
python3 tools/export_diag_catalog.py --image /mnt/vida --dump-archive data/diag_dump.tar.xz
python3 tools/export_diag_catalog.py --from-json input.json --out catalog.json.bz2
python3 tools/pack_diag_dump.py --input /tmp/diag-dump --output /tmp/diag-dump.zip
```

`--out` still writes only the catalog. Its suffix selects plain JSON or gzip,
bzip2 or xz compression. `--dump-dir` writes `<dump>/catalog.json`, bounded raw
table summaries and the recovered script XML. `--dump-archive` packs the dump
tree into one archive and can be used alone or together with `--dump-dir`.
`--max-script-files N` caps the XML files without changing the catalog rows.

## Compression and archives

The catalog loader and writer in `volvo_p3.diag_catalog` select the codec from
the path suffix. Both directions use the standard library, so no extra package
is needed.

| Catalog suffix | Encoding |
| --- | --- |
| `.json` | plain UTF-8 JSON |
| `.json.gz` | gzip |
| `.json.bz2` | bzip2 |
| `.json.xz` | xz |
| `.zip` | zip with exactly one JSON member |

A `.zip` catalog is rejected when it holds zero or several JSON members, so the
loader never guesses which document to read. `catalog.json.zip` writes a single
member named `catalog.json`; `catalog.zip` also gets a member named
`catalog.json`.

The dump archive formats are selected the same way:

| Archive suffix | Container |
| --- | --- |
| `.tar.gz` | gzip-compressed tar |
| `.tar.xz` | xz-compressed tar |
| `.zip` | deflate-compressed zip |

`tools/pack_diag_dump.py` packs an existing dump through the same
`volvo_p3.diag_archive.pack_dump` function the exporter uses. It prints the
summed input size, the archive size and the compression ratio. The packer
requires `catalog.json` at the dump root and refuses an archive path inside the
dump directory.

The archive holds the dump contents, not the dump directory itself:

```
catalog.json
raw/
  base_data.json
  carcom_tables.json
  ...
scripts/
  <docno>_<language>_<offset>.xml
```

Compression does not change the JSON document or its schema. A compressed
catalog loads through `load_catalog` exactly like a plain one, and the
`diag-catalog` and `diag-report` commands accept either form.

## Schema version 2

The catalog keeps the version 1 sections and adds two optional ones. A version 1
catalog still loads; a version 2 catalog serializes as version 2.

| Section | Contents |
| --- | --- |
| `dtc_descriptions` | code, short text, value text, ECU, language, symptoms and causes |
| `timing` | ECU variant, request prefix, message timing, timeout and resend |
| `profiles` | 16-hex profile id, title, nullable components, markets, chassis range |
| `scripts` | document number, language, type, profiles, ECU plans, DTC paths, CRC status |
| `symptom_definitions` | symptom text, category, type, section, CSC, causes, DTC codes |
| `freeze_frames` | parameter, value, block id and a `confirmed` flag |

`VehicleProfile` uses the nullable tuple from the profile notes rather than one
flattened vehicle type: model, model year, engine, transmission, steering, body
style, special vehicle, suspension, brake system, structure week, chassis range,
folder level and node ECU. A null component is a wildcard, not an unknown value
that was guessed. `markets` keeps every partner group the profile is mapped to.

`ScriptEntry` keeps the numeric `script_type` when the source wrote one and the
symbolic `script_type_name` when it did not. It also carries per-ECU plans,
service ids, DTC path bindings, a body SHA-256 and a CRC status. A body that
fails CRC is exported with `status: "crc-mismatch"` rather than dropped.

The loader accepts version 1 and version 2. A malformed section still raises
`CatalogError`; missing new sections in a version 1 file default to empty.

## Recovered counts in this snapshot

These are the counts from the 2022 source image used for this pass. Numbers
are exact for that image and will differ on another release.

| Item | Count |
| --- | --- |
| VehicleProfile rows read | 153,600 |
| Distinct profile ids exported | 134,459 |
| Profile rows not joined to a model and year | 4,553 |
| Model rows | 44 |
| Model year rows | 46 |
| Engine rows | 242 |
| Transmission rows | 53 |
| Body style rows | 10 |
| Steering rows | 2 |
| Special vehicle rows | 12 |
| Partner group rows | 5 |
| NodeECU label rows | 54 |
| Profiles with a partner-group mapping | 112,648 |
| DTC descriptions | 9,033 |
| T150_BlockValue rows read | 155,910 |
| T141 block names matched | 92,622 |
| T191 texts matched, language id 4 | 7,348 |
| Timing rows | 41 Requests, 3 MessageTiming, 1 TimeoutAndResend |
| PKZIP local headers carved | 89,229 |
| Bodies with a valid CRC | 2,054 |
| Bodies that parsed as script XML | 6,631 |
| Catalog script entries (document + language) | 6,567 |
| Distinct script document numbers | 2,977 |
| Languages seen | 40 |
| Symptom definitions | 0 |
| Confirmed freeze frames | 0 |
| T150 values on T141 type-8 blocks | 233 |
| Single-bit text corrections | 158 |

The catalog has 6,567 script entries because the same document number can occur
in more than one language. `lookup_scripts` returns every variant; the script id
is the document number, not a language-specific key.

## Vehicle profiles

`BaseData.dbo.VehicleProfile` is read by page and slot. The fixed field offsets
were verified by changing one component at a time against the component tables:
model at +4, model year at +8, profile id at +12, chassis range at +30/+34,
body style at +42, steering at +46, transmission at +50, engine at +58, partner
group at +70 and special vehicle at +74. The title starts at +101.

The component tables are read from the same file and joined by id. Nullable
component slots can contain stale bytes when the source value is null, so a
component is accepted only when its id resolves in the component table. The
chassis range is accepted only when the title carries `ch <from>-<to>`, which
also distinguishes an open-ended range from a stale upper bound.

4,553 rows could not be joined because either the model id or the model year id
did not resolve in the component table. They are counted and skipped rather
than exported with an invented model or year. The raw dump reports the same
count.

## Scripts

`DiagSwdlRepository_Data.MDF` is not a ZIP file. It contains tens of thousands
of PKZIP local headers spread across a 2.9 GB database file. The exporter finds
each `PK\x03\x04` header, inflates the deflate stream as a raw stream, requires
an XML marker, and parses the result with `volvo_p3.script_plan`.

| Outcome | Count |
| --- | --- |
| Local headers seen | 89,229 |
| Inflate failed | 43,757 |
| Inflate incomplete | 1,449 |
| CRC mismatch | 41,476 |
| CRC verified | 2,054 |
| Parsed as XML | 6,631 |
| Parsed but no `docno` | 19 |
| Parse failed | 36,899 |

Only CRC-verified and CRC-mismatch bodies are used. A CRC-mismatch body is kept
because the body can still parse while the stored CRC or a neighbouring page is
damaged; its status makes that explicit. Bodies that do not inflate or do not
parse are counted and omitted. The export does not claim complete script
coverage: 43,757 streams did not inflate and 36,899 inflated streams did not
parse as XML.

Recovered XML is written to `<dump>/scripts/<docno>_<language>_<offset>.xml`.
A script containing a VIN-shaped 17-character token is not written and is
counted in `raw/script_files.json`.

## Symptoms, causes and freeze frames

The symptom and freeze-frame tables have no page set in this snapshot. Their
object ids come from `sys.syscolpars`; the page count is zero, so there are no
rows to join. The exact blockers are recorded in `raw/carcom_tables.json`.

| Table | Object id | Pages | Blocker |
| --- | --- | --- | --- |
| `T147_BlockMetaDTC` | unknown | 0 | no page set and no physical schema in `sys.syscolpars` |
| `T148_BlockMetaPARA` | 469576711 | 0 | no page set |
| `T152_Symptom` | 581577110 | 0 | no page set |
| `T153_SymptomCategory` | 501576825 | 0 | no page set |
| `T154_SymptomType` | unknown | 0 | no page set and no physical schema |
| `T156_SymptomSection` | unknown | 0 | no page set and no physical schema |
| `T157_SymptomConnection` | 613577224 | 0 | no page set |
| `T158_Symptom_CSC` | 597577167 | 0 | no page set |
| `T159_SymptomCSC_SymptomDTC` | 629577281 | 0 | no page set |

So `symptom_definitions` is empty and the DTC `symptoms` and `causes` fields
are empty for this image. This is a data coverage limit, not a parser error.

`T141_Block` has 144 blocks of type 8, and `T150_BlockValue` has 233 values on
those blocks. Without `T148_BlockMetaPARA.showAsFreezeFrame`, those values are
DTC qualifier codes such as `P044100`, not measurements. They are counted in the
raw dump and are not written as confirmed freeze frames. `freeze_frames` is
empty in the catalog.

## Text quality

Two passes clean the DTC text:

1. Decoding sanitizes replacement characters, NULs and control bytes to spaces.
   A damaged character leaves a space, so text is never silently dropped.
2. A rare token is repaired when it is one bit away from a token that occurs at
   least 50 times and has the same casing, for example `Preqsure` -> `Pressure`,
   `Eneine` -> `Engine`, `mpen` -> `open`. Valid words on a small stoplist
   (`Wide`, `Date`) are left alone. Every replacement pair is written to
   `raw/text_repairs.json`.

158 tokens were corrected in this image. Text that is still wrong after that
pass remains in the catalog; the exporter does not invent a replacement when
there is no common corrected form.

## Raw dump layout

```
<dump>/
  catalog.json
  raw/
    base_data.json       profile row counts, component counts, partner map
    carcom_tables.json   object ids, page counts, symptom and freeze-frame blockers
    scripts.json         carve statistics and one line per script entry
    text_repairs.json    every damaged -> repaired token pair
    script_files.json    XML files written and VIN-shaped files skipped
  scripts/
    <docno>_<language>_<offset>.xml
```

`base_data.json`, `carcom_tables.json` and `scripts.json` are summaries, not a
full 12 GB dump. The full profile and script content is in `catalog.json` and
the recovered XML files. An archive written by `--dump-archive` or
`tools/pack_diag_dump.py` keeps the same layout at its root; it does not add a
top-level directory.

## Verification

The exporter helpers and the version 1 to version 2 loader path are covered by
`tests/test_diag_export.py`. The full suite is run with `python3 -m pytest`.

Nothing in this export attaches SQL Server, starts a source process, or writes
to the image. The input is opened read-only and mapped for the duration of the
scan.
