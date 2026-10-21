# VIDA diagnostic client

The managed client VIDA hands off to when diagnostics opens. The web application
collects a vehicle identity and a set of enabled functions, then a local .NET
client resolves that into a diagnostic script and talks to the car through the
DiCE J2534 driver.

This is the layer that answers the question the vehicle-profile work raised: what
actually changes a read. Paths are relative to the tree root.

Nothing here was executed. The assemblies were read statically: PE/CLI metadata,
member signatures, user strings, and the SQL in `DiagSwdlRepository_Data.MDF`.

## Where it lives

`VidaConfigApplication.exe.Config` sets `DiagnosticPath` to
`jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics`, about 18 MB of
managed assemblies, and `DiagConfigFile` to `diagnostic.config.xml`.

| Assembly | SHA-256 | Role |
| --- | --- | --- |
| `BizServices.VehicleResolver.DLL` | `31d107b0ca13ae22a2c2f3893c76d38550ac3ce160648d07888866ddafe46720` | VIN and component resolution |
| `BizServices.ScriptRepository.DLL` | `f046b5fa3c8d3487fc05aa1140ac3c4ef1880eef5251c8c46b1c3d73f45a6bca` | script lookup |
| `AppServices.EcuDataResolver.DLL` | `da965d2f455346d3d2e17ab115afb1f1b41bc5e5ea6897f53eaa32cd903fd62c` | ECU naming |
| `BizServices.VehicleCommProvider.DLL` | `763c26d195ee7324fd08d6da1bf7304e0090a7220c29ec46038ae695db5a82e5` | vehicle communication |
| `VehCom2.DLL` | `3a08f5ff5819396e676eac8751fdb57475e8f76e20488926259993674ec97e82` | 266 KB, 347 types, the largest comm assembly |

All are PE/CLI, IL-only, .NET metadata `v2.0.50727`, x86 or anycpu.

## Configuration

`diagnostic.config.xml` `appSettings`:

```
UseClientSettings        false
UseLocalDatabase         false
ScriptPath               res/scripts/
VehType                  p2x
DiagnosticConnection     NoCommConnection
CommToolName             (empty)
ComPort                  COM1
VctBaudrate              115200
CommToolPollingFrequency 2000
CommToolBatteryLimit     11
LogLevel                 Trace
```

Two things stand out. `VehType` is `p2x`, which is a platform designation sitting
in the same tree as this repository's `P3_D6_MODULES`, so the client is
parameterised by platform rather than by model. And the shipped configuration is
`NoCommConnection` with an empty `CommToolName`, which matches
`docs/dice-compatibility.md`: nothing in the image selects a J2534 device.

`components.config.xml` wires the client through a component factory. The
`ScriptProvider` component receives both settings:

```xml
<component class="Vcc.Vida.DiagSwdl.BizServices.ScriptRepository.ScriptProvider">
  <propertyConfig name="ScriptPath" value="res/scripts/" type="System.String" />
  <propertyConfig name="VehType" value="p2x" type="System.String" />
```

`VidaConfigApplication.exe.Config` also names the adapter through the J2534
registry view, `SOFTWARE\PassThruSupport.04.04\SETEK - DiCE`, with `LogLevel` as
the only key read.

## Vehicle resolution

`BizServices.VehicleResolver.DLL` is small: 4 types, 32 methods. The service
surface:

```
ResolveVin(string, bool, string)
ResolveOverriddenVin(string, string)
ResolveVin(string, int)
ProfileFromVin(string, bool, string)
ProfileFromModelAndYear(int, ...)
ProfileFromModelYearAndEngine(int, ..., int, string)
GetAvailableEngines(int, int)
GetModelYears(int)
GetModelName(int)
CreateVehicleProfile(...)
ResolveProfile(...)
profileFromComponents(...)
GetAggregateProfileString(string[])
```

It reaches the **same stored procedures as the web application**, on the same
databases:

| Reference in the assembly | Database |
| --- | --- |
| `[BaseData]..getVINComponentsByPartnerGroupId` | BaseData |
| `[BaseData]..getEnginesForModelAndModelYear` | BaseData |
| `[AccessServer]..getOverriddenVINComponent` | AccessServer |

So the diagnostic client does not trust the profile chosen in the browser. It
re-resolves the vehicle itself from the VIN, or from model plus year plus engine,
and rebuilds the profile string through `GetAggregateProfileString`. The override
path explains the `vinOverridden` and `IsOverrideVin` flags carried in the web
form: a manually corrected VIN is stored in AccessServer and substituted during
resolution.

The assembly also contains a hardcoded SQL login in a connection-string literal.
It is a shipped 2014 default, not something this document needs to reproduce, and
it is not repeated here. Anyone deploying this tree should treat that account as
compromised by publication.

## ECU naming

`AppServices.EcuDataResolver.DLL` exposes three calls:

```
EcuNameFromType(...)
EcuDescriptionFromType(...)
EcuNameFromVariantId(...)
```

`EcuNameFromVariantId` is the mapping that the timing table needs: `DiceTiming`
keys its rows on an integer `ECU_variant`, and this is the function that turns
such an integer into a name. Only `DIM`, `CEM`, `CCM` and `Unknown` appear as
inline literals, and `sv-SV` appears as a culture string, so the rest of the
names come from data or localized resources rather than the assembly.

## The scripts are the readout

This is the piece that settles the original question. `DiagSwdlRepository`
contains `Script`, `ScriptContent`, `ScriptType`, `ScriptCarFunction` and
`ScriptProfileMap`. Scripts are selected with:

```
GetScript(@scriptType, @profileList, @scriptId, @languageCode, @ecuType)
```

When a profile list is supplied, the join is:

```sql
FROM ScriptContent scriptcontent
INNER JOIN Script s ON s.Id = scriptcontent.fkScript
INNER JOIN scriptprofilemap spm ON spm.fkScript = s.id
INNER JOIN GetValidProfilesForSelected(@profileList) p ON spm.fkProfile = p.profileId
WHERE s.fkScriptType = @scriptTypeId AND scriptcontent.fkLanguage = @languageId
ORDER BY p.folderlevel desc
```

With an ECU function group, it adds
`INNER JOIN ScriptCarFunction scf ON scf.fkScript = s.id` and
`scf.FunctionGroup = @ecuType`. The result is `ScriptContent.XmlDataCompressed`.

Three consequences follow directly from that SQL:

1. **The readout is a script, not a table of addresses.** Vehicle type selects a
   compressed XML diagnostic script through the same profile-ID set that filters
   the service information.
2. **Script selection is profile-specific, most specific first.** `ORDER BY
   p.folderlevel desc` means a leaf profile (level 3) beats a generic one (level
   0), so the generic profile is a fallback in the script case too.
3. **A script can be per ECU function group.** `ScriptCarFunction.FunctionGroup`
   lets one vehicle have different scripts per ECU, which is the closest thing
   found to a per-module read plan.

`GetProfileString` is the display-side counterpart: it aggregates model, year,
engine and transmission from a profile list, requires `fkPartnerGroup is null`,
and takes `TOP 1` ordered by engine then transmission descending, so a
presentation profile is always chosen even when the caller passed a set.

## Timing

`db/DiceTiming_Data.MDF` supplies `P1max`, `P3min`, `P4min`, `Timeout` and
`Resend` keyed by `ECU_variant` plus request bytes `B1`, `B2`, `B3`. That is
covered in `docs/vida-vehicle-profile.md`; it is a separate axis from the script,
and `EcuNameFromVariantId` is what would let a caller join the two.

## What this means for PyDiCE

The mechanism is now clear enough to stop guessing about it:

* VIDA does not ship a vehicle-to-ECU-address map. It ships **scripts keyed by
  profile**, plus per-ECU-variant timing.
* The profile string is the interface between the two halves, and it is
  comma-separated profile IDs parsed by `ParseString`.
* `volvo_p3` hardcodes `P3_D6_MODULES`, which corresponds to the *P3* side of a
  `VehType` switch whose only observed value here is `p2x`. That switch is the
  natural place for this repository to hang a platform profile if it ever needs
  more than one.

The next concrete step would be decompressing one `ScriptContent.XmlDataCompressed`
row to see whether the script records ECU request IDs and services, which is the
only way to confirm the script encodes the read plan rather than only UI text.

## Open questions

1. **What is inside a script?** `XmlDataCompressed` needs decompression. Until
   then, "the script is the readout" rests on the join and the column name, not
   on the script body.
2. **What values does `VehType` take besides `p2x`?** The setting exists on
   `ScriptProvider` and in `diagnostic.config.xml`, but no enumeration of
   platforms was recovered, and `res/scripts/` is empty in this tree.
3. **How does `EcuNameFromVariantId` get its data?** The assembly carries only
   four inline names, so the mapping must come from a resource or a database
   table that has not been located.
4. **Is `res/scripts/` ever populated?** The directory does not exist and no
   script files ship, so file-based scripts are either optional or generated.
5. **Does the client actually connect to DiCE in this tree?** The shipped
   configuration is `NoCommConnection` with an empty tool name, so the live path
   was not exercised here.

## Reproducing

```sh
VIDA=/path/to/vida-2014d
WEB="$VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war"
D="$WEB/Diagnostics"

cat "$D/diagnostic.config.xml"
grep -n -i "VehType" "$D/components.config.xml"

javap -p "$WEB/WEB-INF/classes/com/ford/vcc/vida/web/action/diagnostics/DiagnosticForm.class"

strings -a -n 4 "$VIDA/db/DiagSwdlRepository_Data.MDF" \
  | grep -n "CREATE PROCEDURE \[dbo\].\[GetScript\]"
```

Managed assembly inspection is done with `inspect_managed_artifact` and
`inspect_managed_members` on any of the DLLs above; passing an explicit path
avoids the selected target drifting between calls.
