# Logical-address map recovery

`tools/recover_address_map.py` builds a JSON address map from a saved module
profile and recovered script plans. It never invents a logical address. Every
selected entry carries a confidence and a source, and every module without a
match is reported with the reason.

This note records the evidence, what the tool can and cannot recover from the
2022 source image used for this pass, and the exact blockers.

## Usage

```sh
python3 tools/recover_address_map.py \
  --profile reports/my-car-complete.json

python3 tools/recover_address_map.py \
  --profile reports/my-car-complete.json \
  --script path/to/script.zip --selection dominant

python3 tools/recover_address_map.py --script path/to/script.zip

python3 tools/recover_address_map.py \
  --scan-image path/to/DiagSwdlRepository_Data.MDF
```

`--script` and `--scan-image` can be repeated. ZIP and plain XML scripts are
accepted. The tool does not start an adapter, attach a database, or execute a
script. `--scan-image` inflates ZIP streams directly from a database image; it
records titles, document numbers and `EcuAddress`/`EcuType` properties even when
XML parsing or CRC validation fails. That evidence is reported, not selected.

## Output

The top level is an ordinary JSON object. `addresses` is the part the read-only
runner reads:

```json
{
  "schema": "diag-address-map",
  "version": 1,
  "selection": "unique",
  "addresses": {"381101": "0x720"},
  "entries": [
    {
      "name": "DIM",
      "logical_kind": "ecu_type",
      "logical_value": 381101,
      "address_key": "381101",
      "can_id": 1824,
      "can_id_hex": "0x720",
      "confidence": "medium",
      "source": "script title match: 12 document(s), e.g. VCC-231100; profile request_id 0x720"
    }
  ],
  "unmapped": [],
  "warnings": []
}
```

`addresses` keys follow the runner's number syntax: a one-byte `EcuAddress` is
written as `0x..`, while an `EcuType` is written as a decimal integer. Values
are 11-bit physical request IDs. The runner resolves `EcuAddress` before
`EcuType`, matching its existing behaviour.

When `--script` is given, `script.scripts[]` also lists every distinct
`(EcuAddress, EcuType)` pair, its service IDs, any name candidates associated
with the type, and whether the pair resolved through the selected map.

When `--scan-image` is given, `fragment_scan` is a list with one object per
image. Each object carries `stats`, `address_counts`, `pairs` and `documents`.
`documents` lists every distinct title, document number and address combination
with its CRC status and occurrence count. The section does not populate
`addresses`; it exists so a reviewer can see what the damaged bodies contain.

## Selection policy

| Policy | Meaning |
| --- | --- |
| `unique` (default) | Select only when exactly one script-derived candidate exists. |
| `dominant` | Select the candidate supported by more documents than the runner-up. |
| `all` | Select every candidate. |

`unique` is the only policy that claims a match without a tie-break. The other
two mark every selected entry `confidence: "low"` and add a note that the
candidate was chosen by policy, not by a unique match. A candidate is never
selected when the name is a discovery placeholder such as `CAN_730` or when no
script title names the module.

Confidence values reflect the evidence, not a verified car:

* `medium` - one candidate from an exact module token in a script title.
* `low` - a policy tie-break or an explicit all-candidates selection.
* `high` is reserved for a future match that also joins the script profile to
  the car's profile set. This snapshot does not provide that join.

## What the databases contain

The configuration database does carry the schema. `T121_Config` has object id
`0x182C9B23` in its schema catalog, is named by foreign-key constraints, and is
read by stored procedures that select `commAddress`, `canAddress`, `CanIdTX`,
`CanIdRX`, `CanIdFunc` and `CanIdUUDT`. The schema is not the problem.

The data is. Scanning every 8 KiB chunk of the 1.8 GB configuration database
file, under every page type, found zero pages whose header object id is
`0x182C9B23`. The same scan found:

| Table | Object id | Pages |
| --- | --- | --- |
| `T121_Config` | `0x182C9B23` | 0 |
| `T120_Config_EcuVariant` | `0x21B6055D` | 0 |
| `T100_EcuVariant` | `0x0EA330E9` | 0 |
| `T101_Ecu` | `0x07F6335A` | 0 |
| `T102_EcuType` | `0x79A81401` | 0 |
| `T110_Service_EcuVariant` | `0x173876EA` | 0 |
| `T111_Service` | `0x08EA5793` | 0 |
| `T122_Protocol` | `0x7B905C75` | 0 |
| `T123_Bus` | `0x0F975522` | 0 |
| `T124_Net` | `0x7C8480AE` | 0 |

The control tables are present: `T141_Block` owns 28 pages and `T191_TextData`
owns 50. A few catalog rows still report a legacy count of 2,766 rows for
`T121_Config`, but the page set is gone. The pages are not misattributed to
another object id; there is simply no page with that object id. A search for
CAN-ID-shaped tuples in other objects did not find a displaced config block.

The two sibling databases do not carry the schema either. String scans of
`BaseData_Data.MDF`, `DiagSwdlRepository_Data.MDF`, `DiagSwdlSession_Data.MDF`
and `DiceTiming_Data.MDF` found no `T121_Config`, `T120_Config_EcuVariant`,
`T100_EcuVariant`, `T102_EcuType`, `commAddress` or `CanIdTX` text. The physical
map is therefore not recoverable from this snapshot. A separate ZIP-stream scan
of the three named images found 89,229 local headers in
`DiagSwdlRepository_Data.MDF` and none in `CarComRT_Data.MDF` or
`BaseData_Data.MDF`, so every script body in this snapshot lives in the script
repository database.

The same emptiness blocks the descriptive join. `T100`/`T101`/`T102` would turn
an `EcuType` into an ECU variant name, and `T120`/`T121` would turn it into a
CAN ID. Every table in that chain has zero pages, so a script's `EcuType=381101`
cannot be resolved through the database.

## What the application code contains

The Java EJB and data archives contain no `EcuAddress`, `commAddress`,
`CanIdTX` or `CanIdRX` string. The only `nodeAddress` hit is in a vehicle
information web-service payload and describes hardware/software node metadata,
not a diagnostic address. The web archive's XML, JSP, JavaScript and properties
files contain no ECU address table. `Diagnostics/res/x40db/ecmdb.xml` is a
part-number catalogue, and `diagnostic.config.xml` has no ECU list. The managed
assemblies are outside this search, but the documented runtime chain resolves
addresses through the empty configuration tables rather than a static table.

## What the scripts contain

Carving the script store produced 89,229 ZIP local headers; 6,631 bodies parsed
as script XML, grouped into 6,567 document/language entries over 2,977 document
numbers. Of the fully parsed entries:

* 2,360 carry an `EcuType`; 2,338 of those are non-zero.
* 0 carry an `EcuAddress`.

The fully parsed set is therefore type-addressed, not node-addressed. No script
title in that set pairs a module name with an `EcuAddress`.

### Damaged script fragments

A raw scan inflates 43,530 bodies that start with an XML marker even when the
XML parser or CRC check rejects them. The three named images produce:

| Image | ZIP headers | XML bodies | Bodies with `EcuAddress` | With `Mode="EcuAddress"` | Distinct document groups |
| --- | --- | --- | --- | --- | --- |
| `DiagSwdlRepository_Data.MDF` | 89,229 | 43,530 | 129 | 6 | 69 |
| `CarComRT_Data.MDF` | 0 | 0 | 0 | 0 | 0 |
| `BaseData_Data.MDF` | 0 | 0 | 0 | 0 | 0 |

The recovered address values are dominated by zero: 240 occurrences of `0`
against 14 occurrences of `71`, 6 of `16`, 3 of `64` and 2 of `17`. Fourteen
document groups carry a non-zero or corrupt value; six distinct documents carry
a valid non-zero address: `VCC-225200` (`64`), `VCC-242880` (`17`),
`VCC-250840` (`16`), `VCC-226301` (`16`), `VCC-233060` (`71`) and
`VCC-252225` (`16`). The strongest example is `VCC-242880`, title
`Read DTC View With Status, P1, DV6`, with `EcuAddress=17` and
`Mode="EcuAddress"`. Its CRC does not match and its title names a platform and
engine, not a module. `VCC-226301` (`Reload ECM P P2`) carries `16` and does
name ECM, but the title marks it as P2. These bodies are evidence of what the
scripts contain; they do not establish which module on the saved car owns the
address. The scanner reports them and leaves selection unchanged.

Titles do pair module labels with `EcuType` values. That is the only recovered
name-to-logical evidence:

| Label | Candidates (documents) |
| --- | --- |
| DIM | 381101 (12), 372302 (3) |
| CEM | 372302 (15), 284101 (1), 593102 (1) |
| ECM | 284101 (51), 372302 (3) |
| TCM | 437601 (54), 284101 (1), 457601 (1) |
| SRS | 884701 (7) |
| SAS | 645902 (5) |
| ABS / AYC / DSTC | 593101 (4 / 1 / 1) |

The candidates are not a verified map. A title can name a module while the
VehComm node addresses a gateway or another module, and the title does not
encode a platform generation. For example, `ECM P Upgrade, P3` carries the same
`EcuType` as the dominant CEM candidate, and `DIM Reload, P1` carries the
dominant CEM value. A title match is evidence, not proof.

The built-in table in the tool records the distinct-document counts and an
example document number for each candidate. Passing `--script` adds evidence
from that script at run time.

## Coverage on the saved profile

The saved profile has 18 discovered modules: 10 named modules and 8 `CAN_xxx`
placeholders.

| Policy | Mapped | Notes |
| --- | --- | --- |
| `unique` | 0/18 | Every named candidate is ambiguous. |
| `dominant` | 4/18 | DIM, CEM, ECM and TCM, all low confidence. |
| `--scan-image` evidence | 0/18 | 129 bodies carry an address; none names a P3 module. |

BECM, IEM, IGM, DCDC and ACCM appear only in software-download or
vehicle-communication specification documents with no `EcuType` or
`EcuAddress`; OBC has no title match at all. They cannot be mapped from this
snapshot. The `CAN_xxx` names are discovery placeholders and are never mapped
by name.

## Blockers

1. `T121_Config` and every table in the `T100`/`T102`/`T120` chain have no page
   set. The schema survives; the rows do not.
2. The fully parsed scripts are `EcuType`-addressed and carry no `EcuAddress`.
   Damaged bodies do carry 129 `EcuAddress` values, but they are mostly zero and
   the non-zero titles name a platform and engine or a P2 module, not a P3
   module. They cannot be joined to the saved car without a module name and a
   platform marker.
3. Script titles do not encode a platform generation. The same label maps to
   several `EcuType` values, and some title matches name a gateway rather than
   the addressed node.
4. The saved profile has no profile IDs, VIN, model year or platform marker, so
   scripts cannot be filtered to the car's profile set. Without that join, a
   name-to-type candidate cannot be promoted from medium to high confidence.
5. No static ECU-name/address table exists in the Java, JSP, XML or script
   bodies that were searched.

A runtime car configuration that populates `T121_Config`, or a scan report that
records the car's profile IDs, would remove blockers 1 and 4. Until then the
map is explicit and conservative: unique matches only, with every unresolved
module named.
