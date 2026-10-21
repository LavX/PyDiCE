# VIDA diagnostic client

The managed client VIDA hands off to when diagnostics opens. The web application
collects a vehicle identity and a set of enabled functions, then a local .NET
client resolves that into a diagnostic script and talks to the car through the
DiCE J2534 driver.

This is the layer that answers the question the vehicle-profile work raised: what
actually changes a read. Paths are relative to the tree root.

Nothing here was executed. The assemblies were read statically: PE/CLI metadata,
member signatures, user strings, and the SQL in `DiagSwdlRepository_Data.MDF`.
A second pass decoded script bodies and corrected the `VehType` and addressing
findings; see `docs/vida-second-pass.md`.

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

Two things stand out. `VehType` is `p2x`, a three-character platform code
carried as configuration data. No managed code branches on it: the `_vehType`
field is read only by its getter, `GetScript` does not take it, and no lowercase
`p2x` literal exists in the 243 managed PEs. It is not a P2/P3 switch. The
shipped configuration also sets `DiagnosticConnection` to `NoCommConnection`
with an empty `CommToolName`. The empty name maps to `CommToolType=NOCOMM`,
which throws `VehComException("Unsupported communication tool")` at connection
construction, before any J2534 call. That is a statement about the snapshot, not
about what the UI can do; the boundary note records the selection path.

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

`EcuNameFromVariantId` is the display-side mapping for the timing table's
integer `ECU_variant`. The runtime timing path in `VehCom2` passes the integer
straight to SQL and does not reference this assembly. Only `DIM`, `CEM`, `CCM`
and `Unknown` appear as inline literals, and `sv-SV` appears as a culture
string, so the rest of the names come from data or localized resources rather
than the assembly.

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

## Script structure

`BizServices.Scripting.DLL` carries the element and attribute names the parser
uses. The root is `script`, with `script/@docno`, `script/@VariantId`,
`script/title` and `script/content`. Inside, the names that matter for a read:

| Group | Names |
| --- | --- |
| ECU identity | `ecu`, `EcuAddress`, `TesterAddress`, `EcuIdentifier`, `EcuName`, `EcuType`, `EcuVariant` |
| Messaging | `Request`, `Response`, `ServiceId`, `OnNegativeResponse`, `OnNoResponse`, `ADDR` |
| Sections | `readingParameters/section`, `activations/section`, `programmedValues/section`, `specialTools/section` |
| Structure | `nodes/node`, `handlers/eventhandler`, `handlers/propertyhandler`, `valuelist/item` |
| Faults | `Dtc`, `DtcList`, `DTC1` to `DTC3` |

Supporting strings describe the runtime around them: `Alive Ecus:`,
`Dead Ecus:`, `Ecus not in car config:`, `Unknown ECU`, `Unknown response
type`, `Could not build request`, `Car config parameters:`, `Vehicle info
parameters:`, `Error executing script`, and `Not a valid EcuMode`.

Two consequences. First, an `EcuAddress` field means the script carries a
per-vehicle node address, so the read plan is data rather than code. It is not
the CAN ID; `T121_Config` maps the node address to `CanIdRX`/`CanIdTX`/
`CanIdFunc`. Second, `Alive Ecus:` and `Dead Ecus:` plus
`Ecus not in car config:` mean the client tracks which modules answered and
compares against a car configuration before deciding what to read.

The element semantics above are still partly inferred. Decoded bodies confirm
the ZIP container and the `Components.VehComm` shape, but not every element's
runtime meaning.

Scripts are fetched from the database already zipped (`GetFromDb`,
`GetZippedScriptXml`, `GetOneScriptFromDb`) or read from disk through
`IsScriptOnDisk` and `FormatScriptPath`, which build a path ending in `.xmlds`.
No `.xmlds` file exists anywhere in this tree, so every script here comes from
the database.

### The body is ZIP XML

`ScriptContent.XmlDataCompressed` is a single-entry PKZIP archive with a deflate
stream. `ScriptProvider.GetZippedScriptXml` calls
`Vcc.Vida.DiagSwdl.Util.Compression.ZIP.DecompressString`, which uses
`ICSharpCode.SharpZipLib.Zip.ZipInputStream`. A carved entry at offset
`2176131182` of `DiagSwdlRepository_Data.MDF` passes `unzip -t`.

Decoded `Components.VehComm` nodes carry either `EcuAddress` or `EcuType`, plus
`ServiceId`, `Request` and `Response`. VCC-242880 carries `EcuAddress=17` and
`ServiceId=71`; its body failed its CRC, so those node values come from the
recovered fragment. VCC-241986, a ClearDtc script, carries `EcuType=381101` and
service IDs 13, 41, 5, 6, 14, 63 and 66. `script/@VariantId` is a script id,
not an ECU variant.

A carve found 89,229 PKZIP headers: 44,983 parse as XML bodies, 2,047 are
complete with a verified CRC, and 42,936 are truncated by non-contiguous LOB
pages. The complete set covers 1,472 docnos in 18 languages. Type 10 read
script `VCC-227144` ("Read DTCs", en-US), recovered from offset `1985634414`,
has 38 profile IDs, `EcuType=831701`, `ServiceId=5`, and a
`DtcList` iterator binding `Dtc.value` and `Dtc.text`.

`EcuAddress` is a one-byte CarCom node address, not a CAN request ID. It is the
`T121_Config.commAddress` side; the table has the CAN-ID columns
(`CanIdRX`, `CanIdTX`, `CanIdFunc`) but no rows in this snapshot, so no physical
CAN ID is statically resolvable here.

What happens after a script returns, including where the sentence beside a code
comes from and what is attached as context, is in `docs/vida-dtc-readout.md`.

## How it reaches the bus

`VehCom2.DLL` is the J2534 layer. `Util.DLL` resolves the `FunctionLibrary`
value from the PassThru registry key; `VehCom2.J2534ChannelManager` selects by
`CommToolName` and `J2534Proxy.LoadDll` loads it. Its strings show ISO15765 and
ISO9141 handling, five-baud init
(`Calling FiveBaudInit on address 0x{0:X}`, `fivebaudinit (RS2000)`), J2534-2
detection, message filtering (`PassThruStartMsgFilter`, `FilterBuffer() invalid
D2 frame`) and both CAN high and low speed paths (`J2534 cannot access CAN LS
bus`).

The timing database is consumed here, not in the web application:

```
EXECUTE GetRequestTimeoutAndResend @ECU={0}
```

and applied per protocol:

```
New ISO9141 timing set. P1max:{0}, P3min:{1}, P4min:{2}
P2Can: {0} ms, P2*Can: {1} ms, P4Can: {2} ms
Adjusting P2CanServerMax from {0} ms to {1} ms due to session parameters
P2CanDelta
P2CanServerMax deviation: {0} ms
P4CanServerMax deviation: {0} ms
```

The chain the artifacts describe runs end to end: the profile set selects a
script, the script names ECUs with `EcuAddress`/`EcuType` and `ServiceId`,
CarCom `T121_Config` maps the node address to `CanIdRX`/`CanIdTX`/`CanIdFunc`,
and the comm layer looks up per-ECU-variant timeout and resend from
`DiceTiming` and pushes `P2Can`/`P4Can` into the J2534 channel. That is how two
vehicle types can produce different traffic. Concrete script values are now
read; what remains is coverage, not format.

## Timing

`db/DiceTiming_Data.MDF` supplies `P1max`, `P3min`, `P4min`, `Timeout` and
`Resend` keyed by `ECU_variant` plus request bytes `B1`, `B2`, `B3`. The shipped
table has 41 `Requests` rows, 3 `MessageTiming` rows and 1 `TimeoutAndResend`
row. Every override points at 3000 ms and resend 0; the variation is in
`P1max`/`P3min`/`P4min` (id 0: 40/110/10, id 1: 6/27/2, id 2: 10/110/2).
The integer is `T100_EcuVariant.id`: `GetEcuVariantIdForConfigId` reads
`T120_Config_EcuVariant` for a `T121_Config` id and takes ordinal 1.
`EcuNameFromVariantId` calls `vadis_GetEcuVariantData`, which reads
`T102_EcuType.description`; it is display-side and not part of the runtime
timing path. That is covered in `docs/vida-vehicle-profile.md`; it is a separate
axis from the script.

## What this means for PyDiCE

The mechanism is now clear enough to stop guessing about it:

* VIDA does not ship a separate vehicle-to-ECU-address table. The logical
  addresses and service IDs live **inside scripts keyed by profile**; the
  physical CAN IDs live in CarCom `T121_Config`, and the timing comes from
  per-ECU-variant rows.
* The profile string is the interface between the halves, and it is
  comma-separated profile IDs parsed by `ParseString`.
* `volvo_p3` hardcodes `P3_D6_MODULES`. `VehType` is `p2x` in the shipped
  configuration, but it is a three-character data value; no managed branch or
  enumeration was found, so it is not a ready-made P2/P3 switch.

The script body is now readable: `XmlDataCompressed` is ZIP plus deflate, and
decoded nodes carry `EcuAddress`, `EcuType`, `ServiceId` and `Request`. The
values are node addresses, not CAN IDs; `T121_Config` supplies the CAN IDs. The
remaining work is coverage, not format.

## Open questions

1. **What values do scripts carry?** 44,983 bodies parse as XML, 2,047 are
   complete with a verified CRC, and 42,936 are truncated by non-contiguous LOB
   pages. The complete set covers 1,472 docnos in 18 languages. VCC-242880
   carries `EcuAddress=17` and `ServiceId=71`; VCC-241986 carries
   `EcuType=381101` and service IDs 13, 41, 5, 6, 14, 63 and 66; VCC-227144 is
   a type 10 read script. The truncated bodies remain unread.
2. **What does `EcuAddress` mean on the wire?** It is the CarCom `commAddress`
   side, not a CAN ID. `T121_Config` has `CanIdRX`/`CanIdTX`/`CanIdFunc`, but the
   table appears empty in this snapshot, so the physical mapping is not available
   statically.
3. **What values does `VehType` take besides `p2x`?** No code branch or
   enumeration was found; it is a three-character string carried as data. The
   PIE WSDL constrains only its length.
4. **How does `EcuNameFromVariantId` get its data?** It calls
   `vadis_GetEcuVariantData`, which reads `T102_EcuType.description`. The T102
   rows were not read, so the 11 DiceTiming variant ids are not yet joined to
   names. It is display-side and not in the timing path.
5. **Where do the timing values come from at runtime?** The `@ECU` id is
   `T100_EcuVariant.id`, reached through `T120_Config_EcuVariant`. The table
   values are extracted. Whether DiceTiming drives CAN or only the K-line
   `D2onKApplicationDICE3` path remains open; CAN appears to use
   `vadis_GetDiagTimings`, but its managed caller was not found.
6. **Does the client actually connect to DiCE in this tree?** The empty
   `CommToolName` maps to `NOCOMM`, which throws at connection construction, so
   no live connection is possible from this snapshot without changing the tool
   selection. The UI selection path and `DiagApp.exe` exist.

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
