# VIDA vehicle profile selection

How VIDA 2014D resolves "which car is this?" into the data set it reads out. The
reference tree examined is a VIDA 2014D installation, and the findings are
recorded so `volvo_p3/` can decide whether a vehicle type should influence which
modules get read. Today it has a TUI field that accepts a model year and model
and does nothing with them.

The goal is not to copy the UI. It is to capture the shape of the data model.

Nothing here was measured on a running VIDA. Everything is read from the files in
that tree: Java class files, JSP/JS, and the SQL Server database files. The only
runtime evidence is the logs it contains.

## Target

Paths are relative to the tree root.

| Artifact | Role |
| --- | --- |
| `jboss/standalone/deployments/VidaEar.ear/VidaEjb.jar` | business layer, 325 classes |
| `.../VidaEar.ear/lib/VidaData-2014D.jar` | JDBC layer, all SQL |
| `.../VidaEar.ear/VidaWeb.war` | exploded web app, 279 classes under `WEB-INF/classes`, JSP and JS |
| `db/BaseData_Data.MDF` | base data: `VehicleProfile` and components, stored procedures |
| `db/DiceTiming_Data.MDF` | per-ECU-variant request timing and resend policy |

Obvious decoys: `VidaConfig-2014D.jar` is i18n resources only, and
`jboss/standalone/deployments/VidaEar.ear/VidaWeb.war` is exploded, so the war
file itself is a directory.

## A vehicle type is a tuple, not a value

The picker is `vehicleProfileForm`. Its fields are fixed in
`JavaScripts/vehicleprofile/vehicleprofile.js` (`disableAllSelectBoxes`), and the
submitted payload is assembled in `vehicleprofiledwr.js` (`setVehicleProfile`):

```
partnerGroupId, vehicleModelId, modelYearId, engineId,
transmissionId, steeringId, bodyStyleId, specialVehicleId,
vinStart, chassis
```

`partnerGroup` is the market. The rest resolve to foreign keys on
`dbo.VehicleProfile`.

The cascade is implemented in
`com/ford/vcc/vida/web/action/vehicleprofile/VehicleProfileActionHelper`:

```
getModelYears(partnerGroup, model)
getEngines(partnerGroup, model, year)
getTransmissions(partnerGroup, model, year)
getSteerings(partnerGroup, model, year)
getBodyStyles(partnerGroup, model, year)
getSpecialVehicles(partnerGroup, model, year)
```

Each call returns the options allowed by the choices already made plus an
optional chassis-number constraint. The model list is itself filtered per user
by `VehicleModelsFiltering.filter(list, IUserSubscription)` before the cascade
starts, so two users on the same tree can be offered different model sets.
`SQLStatement` in `VidaData` holds eight
variants per component: with and without a chassis range, and with one other
component pre-fixed. One recovered example:

```sql
SELECT DISTINCT ENG.Id, ENG.Cid, ENG.Description
FROM dbo.Engine ENG
INNER JOIN dbo.VehicleProfile VP ON ENG.Id = VP.fkEngine
WHERE VP.fkPartnerGroup = ? AND VP.fkVehicleModel = ?
  AND VP.fkModelYear IN(#) AND VP.fkBodyStyle = ?
  AND (VP.ChassisNoFrom < ? OR VP.ChassisNoFrom IS NULL)
  AND (VP.ChassisNoTo   > ? OR VP.ChassisNoTo   IS NULL)
```

## The profile row

`dbo.VehicleProfile`, confirmed by the FK relationships recovered from the MDF:

| Column | Meaning |
| --- | --- |
| `fkPartnerGroup`, `fkVehicleModel`, `fkModelYear` | required |
| `fkBodyStyle`, `fkEngine`, `fkTransmission`, `fkSteering`, `fkSpecialVehicle` | nullable refinements |
| `fkBrakeSystem`, `fkSuspension`, `fkStructureWeek` | not exposed in the picker, database only |
| `ChassisNoFrom`, `ChassisNoTo` | chassis range |
| `FolderLevel` | `0` = generic profile, `3` = fully specific leaf |
| `fkNodeECU` | per-profile ECU link |

`VehicleProfileVO` exposes all of these. `VehicleVO`, which is what the session
carries, exposes only the seven pickable components plus `partnerGroup`, `title`
and `vehicleProfileId`, so the database-only columns are dropped before the
session sees them.

## How a choice becomes a result set

The selected `VehicleProfile.Id` values are joined into a comma-separated string
and passed to `GetValidProfilesForSelected(?)`, a table-valued UDF that reads a
cache and falls back to `GetValidProfileManager(?)`.

The manager logic, recovered from `BaseData_Data.MDF`:

1. Parse the selected profile IDs (`ParseString`).
2. Aggregate each component FK with `MAX()` across the selected profiles.
3. Re-select every `VehicleProfile` row matching that aggregate, where **a NULL
   component on a candidate is a wildcard**: `(validProfiles.fkEngine = ? or
   validProfiles.fkEngine is null)`. This is why a partly specified vehicle still
   returns a useful content set.
4. Require partner-group agreement through `VehicleProfilePartnerGroup`, and
   check chassis-range overlap.
5. `UNION select id from VehicleProfile where FolderLevel = 0`, so the generic
   profile is always in the result.

`getValidProfilesForSelectedBuilder` caches the result into `selectedProfiles`
and `validProfiles`, keyed on the selection string. The EJB entry point that
issues the call is `ManageVehicleProfileId`, which is the class named in the log
excerpt below.

`GetValidProfilesForSelected` with a null or empty argument returns only
`FolderLevel = 0` rows, which is the "nothing selected yet" state.

The `VehicleProfileDescriptions` view renders the profile as a title by
concatenating the non-null component descriptions, and falls back to the literal
`All Models*` via `COALESCE` when everything is null. That is the same string the
navigation shows, through `getProfileNavTitle`.

## Two ways in, one profile string

**Manual** is the cascade above.

**By VIN** goes through `com/ford/vcc/vida/bl/parser/VinHandler`:
`decodeVIN(vin, ..., partnerGroup)` resolves model and year from
`VINDecodeModel`, then engine and transmission from `VINDecodeVariant`, with
`VINVariantCodes` and `getVINComponents` as helpers, plus the
`getVINComponentsByPartnerGroupId` call used when the partner group is known
first. `vinHandling` produces the `VehicleSpecVO`.

The adapter can also seed the form. `vehicleprofiledwr.js` calls
`setVinFromCommTool`, which copies a 17-character `AssociatedVIN` from the comm
tool settings object, split 11 + 6 into `vinStart` and `chassiNr`.

The session records how the identification was reached. `VehicleSpecVO` carries
`vin`, `chassiNr`, `brakeSystem`, `suspension`, `diagnosticSerialNumber`,
`commTool`, `commToolStatus`, `isCommToolSWDL`, plus the provenance flags
`vinOverridden` and `manuallyAltered`.

## What actually differs per profile

The profile ID set is a join key for content, not for bus traffic:

| Consumer | Effect |
| --- | --- |
| `TreeItemProfile` join `TreeItem` | which service information documents appear |
| `Qualifier` | which information sections apply |
| `ComponentConditions` | which EPC parts apply |
| `getProfileNavTitle` | title suffix shown in navigation |
| CSC symptom search | symptom and condition lookups |

So two vehicle types produce different documents, different parts, and different
titles. They do not, in this stack, produce a different ECU list.

## Protocol timing is a separate axis

The profile is not the only thing that changes a read. VIDA keeps vehicle-side
request timing in a second database, `db/DiceTiming_Data.MDF`, and that one *is*
keyed to the car rather than to the content. Three tables:

| Table | Columns |
| --- | --- |
| `dbo.Requests` | `ECU_variant`, `B1`, `B2`, `B3`, `MessageTimingId`, `TimeoutAndResendId` |
| `dbo.MessageTiming` | `P1max`, `P3min`, `P4min` |
| `dbo.TimeoutAndResend` | `Timeout`, `Resend` |

Two procedures, `GetRequestTiming` and `GetRequestTimeoutAndResend`, take an ECU
variant integer plus the first one, two, or three request bytes and narrow from
three bytes down to one. `RequestTimingView` and `RequestTimeoutAndResendView`
join the tables. Row notes name specific models, for example:

```
329 is SRS X40 01-04 AE 1B request DTCs
304 is CCM/ECM X70 -00. B2 01 start the self adjusting damper motor
Special values for multiple responses
Special values for multiple responses for SRS on classic x70 (-00)
```

So the lookup key is **ECU variant plus request bytes**, not `VehicleProfile`.
That distinction matters: the profile selects which information is shown, while
the variant plus request bytes select how long to wait and whether to resend.
Both are vehicle-dependent, along different axes.

## Runtime confirmation

`System/Log/VidaTrace.log` and `server.log` show the same path in flight:

```
DEBUG [com.ford.vcc.vida.web.action.vehicleprofile.VehicleProfileActionHelper]
  9 model years found when user has partnergroupID = 1002
DEBUG [com.ford.vcc.vida.bl.basedata.ManageVehicleProfileId]
  Running getValidProfileForSelected
DEBUG [com.ford.vcc.vida.bl.parser.VinHandler] Lets handle VIN: <17-char VIN>
ERROR [com.ford.vcc.vida.bl.parser.VinHandler]
  Unable to find vehicle with VIN: <17-char VIN>
```

Timestamps and thread ids are dropped, and the VIN is replaced with a marker, so
the excerpt keeps the message shape without carrying a vehicle identity.

The last line is useful corroboration: VIN decoding is a lookup against base
data, not a local parse, so an unknown VIN degrades to an error rather than a
guess.

## What this means for PyDiCE

The current `volvo_p3/tui.py` vehicle profile is cosmetic. `_create_profile`
stores `vin` and `profile_created`, and `year`/`model` never leave the
confirmation string. `VehicleState` has no field to hold them.

If vehicle type should matter, the VIDA shape worth copying is:

* a profile record with **nullable** component fields rather than one enum,
* **wildcard matching** where a null component on a candidate matches any
  selection,
* an **always-included generic profile** so partial selections still resolve,
* resolution to a **set** of profile IDs, used as a join key by every read path,
  rather than a single selected value.

That last point is the one that affects `volvo_p3` most. `module_scan` currently
assumes one flat target list (`P3_D6_MODULES`) or one explicit profile file. A
VIDA-shaped design would resolve a selection to a set of compatible module
profiles and scan the union.

The timing database is the more immediately useful half. Right now every read
uses one fixed `--timeout`, and `dice_can` retries nothing. VIDA treats timeout
and resend as per-ECU-variant data keyed by the first request bytes, which is why
it can wait longer on a slow module without slowing the rest. Adopting that shape
means a per-target timeout and resend policy in `ModuleTarget`, seeded from
observed behaviour rather than from the VIDA table itself.

## Open questions

1. **Does profile selection change bus-level readout?** Nothing in the Java stack
   does. `J2534DeviceDriver` is a device descriptor only: id, name, vendor,
   config application. The client that actually drives DiCE is outside these
   artifacts, so this is open, not answered.
2. **What consumes `fkNodeECU`?** `VehicleProfileDAO` selects it into
   `VehicleProfileVO`, but `getNodeECU` has no caller in `VidaEjb`, `VidaData`,
   or the 279 `VidaWeb` classes. Either the Windows client reads it, or it is
   vestigial. This is the most promising lead for a per-profile ECU list.
3. **What are the `FolderLevel` values between 0 and 3?** Only 0 and 3 are
   observed. 1 and 2 are assumed to be intermediate hierarchy nodes; nothing read
   so far confirms it.
4. **What is in the catalog?** The MDF string dump cannot enumerate rows, and the
   component descriptions do not survive extraction in either encoding tried.
   Row counts, model names and `NodeECU` values need a real SQL Server attach.
5. **Is `getVINComponents` still used?** It is defined and referenced, but the
   VIN path in the log failed before reaching a profile, so its output was never
   observed.
6. **What numbering does `Requests.ECU_variant` use?** The column is an integer
   and `DiceTiming_Data.MDF` carries no name table, so the variant space cannot be
   mapped to ECU names from that database alone.
7. **What are the actual timing values?** `P1max`, `P3min`, `P4min`, `Timeout`
   and `Resend` are numeric columns and were not extracted. The row notes were,
   which is how the model-specific entries above were identified.

## Caveats on the recovered SQL

Stored procedure text was recovered with `strings` over `BaseData_Data.MDF`, and
that extraction is lossy. Recovered text carries byte-level corruption:
`validProeiles`, `imner join`, `BrakeQystem`, `validProfileq`, `uhen`. The
control flow and predicate shape above are read from structure, and each routine
appears once in the dump, but **do not
quote identifier spellings or exact predicates from this document**. Attaching
the MDF to a SQL Server instance is the way to get verbatim definitions.

## Reproducing

```sh
# point these at a VIDA 2014D tree and a scratch directory
VIDA=/path/to/vida-2014d
WORK=/tmp/vida-extract

VW="$VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war"

# the picker fields
sed -n '1,30p' "$VW/JavaScripts/vehicleprofile/vehicleprofile.js"

# the submitted payload
grep -n "function setVehicleProfile" -A 45 "$VW/JavaScripts/vehicleprofile/vehicleprofiledwr.js"

# the cascade and the VIN path
javap -p "$VW/WEB-INF/classes/com/ford/vcc/vida/web/action/vehicleprofile/VehicleProfileActionHelper.class"
javap -p "$WORK/vida-ejb/com/ford/vcc/vida/bl/parser/VinHandler.class"

# the SQL, from the extracted data jar
javap -v -p "$WORK/vida-data/com/ford/vcc/vida/data/jdbc/basedata/dao/SQLStatement.class" \
  | grep -oE "// *String .*" | grep -i select

# the stored procedures, from the database file
strings -a -n 4 "$VIDA/db/BaseData_Data.MDF" > "$WORK/basedata.strings"
grep -n "GetValidProfileManager\|getValidProfilesForSelectedBuilder\|CREATE VIEW dbo.VehicleProfileDescriptions" \
  "$WORK/basedata.strings"

# the protocol timing tables and row notes
strings -a -n 5 "$VIDA/db/DiceTiming_Data.MDF" > "$WORK/dicetiming.strings"
grep -nE "CREATE (VIEW|PROCEDURE)|ECU_variant" "$WORK/dicetiming.strings"
```

Extracting the jars in the first place:

```sh
mkdir -p "$WORK/vida-ejb" "$WORK/vida-data"
unzip -q "$VIDA/jboss/standalone/deployments/VidaEar.ear/VidaEjb.jar" -d "$WORK/vida-ejb"
unzip -q "$VIDA/jboss/standalone/deployments/VidaEar.ear/lib/VidaData-2014D.jar" -d "$WORK/vida-data"
```
