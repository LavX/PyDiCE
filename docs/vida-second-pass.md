# VIDA second pass: script bodies, addressing, and the J2534 boundary

The first VIDA notes left four questions open: what a script body contains, what
`fkNodeECU` is for, where the J2534 library is resolved, and how a DTC acquires
its text. This note records the second pass and the corrections it forced.

Nothing was executed. The assemblies were read statically, the registry hive was
read read-only, and the MDF files were parsed as pages, not attached to SQL
Server.

## Target

Paths are relative to an image root, written `$IMG` below. `$VIDA` is
`$IMG/VIDA`.

| Artifact | Size | Role |
| --- | --- | --- |
| `VIDA/db/DiagSwdlRepository_Data.MDF` | 2944335872 B | diagnostic scripts and documents |
| `VIDA/db/CarComRT_Data.MDF` | 1822294016 B | DTC text, blocks, symptoms, CarCom addressing |
| `VIDA/db/BaseData_Data.MDF` | 115081216 B | vehicle profiles and base data |
| `VIDA/jboss/.../Diagnostics/BizServices.ScriptRepository.DLL` | 28672 B | script lookup and ZIP decompression entry point |
| `VIDA/jboss/.../Diagnostics/Util.DLL` | 114688 B | registry reader and ZIP helpers |
| `VIDA/jboss/.../Diagnostics/VehCom2.DLL` | 266240 B | J2534 loader and timing consumer |
| `VIDA/jboss/.../Diagnostics/UI.DLL` | 1388544 B | comm-tool selection UI |
| `Program Files/DiCE/Tools/DiagApp.exe` | 471040 B | J2534 configuration application |
| `Program Files/DiCE/Tools/TSDiCE64.dll` | 790016 B | 64-bit J2534 library named by the 64-bit registry view |
| `Program Files (x86)/DiCE/Tools/TSDiCE32.dll` | 531456 B | 32-bit J2534 library named by the WOW6432Node view |
| `Windows/System32/config/SOFTWARE` | - | registry hive carrying both PassThruSupport views |

The hashes for the two J2534 libraries and the exact registry values are in
`docs/vida-image-boundary.md`.

## 1. The script body is ZIP XML

`ScriptContent.XmlDataCompressed` is a single-entry PKZIP archive with a deflate
stream. One carved entry starts at offset `2176131182` in
`DiagSwdlRepository_Data.MDF` and passes `unzip -t`.

The decompression path is observed:

```
ScriptProvider.GetZippedScriptXml
  -> Vcc.Vida.DiagSwdl.Util.Compression.ZIP.DecompressString
    -> ICSharpCode.SharpZipLib.Zip.ZipInputStream
```

`BizServices.ScriptRepository.DLL` carries `GetZippedScriptXml` and
`DecompressString`; `Util.DLL` carries `ZipInputStream`, `GZipInputStream` and
`DecompressString`. A generic PKZIP reader is enough to read the bodies.

The root is a `script` element. It carries the profile set directly:

```xml
<script docno="VCC-241986"
        nevis:profile="urn:x-nevis:profile:0b00c8af80206a61
                       urn:x-nevis:profile:0b00c8af80206a68 ...">
```

The 16-hex profile IDs match `BaseData.dbo.VehicleProfile.Id`. A read node is a
`Components.VehComm` element:

```xml
<property name="Mode"       type="Internal" value="EcuAddress" />
<property name="ServiceId"  type="In"       value="71" />
<property name="EcuAddress" type="In"       value="17" />
```

VCC-242880 carries `EcuAddress=17` and `ServiceId=71`. VCC-241986, a ClearDtc
script for X40 MY1999-2000 Diesel, carries `EcuType=381101` and service IDs 13,
41, 5, 6, 14, 63 and 66. These are decoded values, not only parser names.
VCC-241986 passed its CRC; VCC-242880's body failed, so its node values come
from the recovered fragment.

`script/@VariantId` is a script identifier, not an ECU variant. `EcuVariantId`
is not serialized in the address script. It is resolved from the address through
CarCom, as section 2 describes.

### `EcuAddress` is a node address, not a CAN ID

`EcuAddress` is a one-byte diagnostic address. It matches
`CarCom.dbo.T121_Config.commAddress`, which the procedures call `NodeAddress`.
The physical identifiers live in the same row:

```
T121_Config.commAddress   node address
T121_Config.canAddress    CAN address
T121_Config.CanIdTX       transmit ID
T121_Config.CanIdRX       receive ID
T121_Config.CanIdFunc     functional ID
T121_Config.CanIdUUDT     UUDT ID
```

The managed chain is:

```
NodeAddressBuilder.CreateNodeAddress
  -> IEcuDataProvider.GetEcuVariantFromEcuAddress
  -> EcuConfig.get_CanIdRx / get_CanIdTx
  -> NodeAddressBuilder.CreateCanIdAddress / CreateEcuAddress
```

`BizServices.VehicleCommProvider` also carries `CanIdToEcuAddressConverter`,
`CreateFromEcuAddress`, `NodeAddressForEcuAddress` and
`GetEcuVariantFromEcuAddress`. `VehCom2` has a `CanIdAddress` type. So the script
selects the logical ECU, and CarCom resolves the physical CAN IDs.

## 2. The profile does not carry an ECU list

`fkNodeECU` is a scalar foreign key on `VehicleProfile`, with the catalog names
`FK_VehicleProfile_NodeECU` and `IX_VehicleProfile_fkNodeECU_NU_NC`. It is
loaded into `VehicleProfileVO` by `VehicleProfileDAO.findByPrimaryKey` and
dropped there. No reader was found.

The search covered:

- all 325 `VidaEjb.jar` classes: zero hits;
- all 1,121 `VidaData-2014D.jar` classes: `SQLStatement`, `VehicleProfileDAO`
  and `VehicleProfileVO` only, seven occurrences;
- all 279 `VidaWeb.war/WEB-INF/classes` classes: zero hits;
- all JSP, JS, XML and properties files in the WAR: zero hits;
- all 243 `.dll`/`.exe` files under `$VIDA`: zero hits in ASCII and UTF-16 user
  strings, including every Diagnostics assembly;
- the string dumps of `BaseData`, `DiagSwdlRepository`, `CarComRT`,
  `AccessServer`, `DiagSwdlSession`, `DiceTiming`, `EPC` and the image
  repository.

The only SQL references are:

- `VehicleProfileDescriptions` and `ProfileDescription` join `NodeECU` but
  project no `NodeECU` column, so the join cannot affect the result;
- `GetCompatibleProfilesWithLevel` filters `t1.fkNodeEcu is null`, but no shipped
  caller was found.

That is a bounded negative, not proof that the table is vestigial. `NodeECU`
has columns `Id`, `Cid`, `Description` and `ObjVersion`; its rows were not
enumerated. It does rule out `fkNodeECU` as the per-profile ECU list the first
notes were looking for.

### The script is the logical map, CarCom is the physical map

A profile is not an ECU. In the recovered script sample, 259 of 1,143 distinct
profile IDs appear in scripts with more than one distinct ECU set. One profile
is reused across BCM, AYC, ECM and other scripts. The script is the logical addressing
map; `T121_Config` is the physical one. `NodeECU` appears in neither chain.

### `VehType` is data, not a switch

`ScriptProvider` has `_vehType`, `get_VehType` and `set_VehType`. Field access
analysis shows `_vehType` is read only by its own getter. `GetScript`,
`GetFromDb`, `FormatScriptPath`, `GetOneScriptFromDb` and `GetZippedScriptXml`
never load it, and `GetScript` has no `VehType` parameter.

No lowercase `p2x` literal exists in the 243 managed PEs. The only `p2x` values
are in `diagnostic.config.xml` and `components.config.xml`. The PIE WSDL types
`VehType` as a 3-character string with no enumeration. It is a platform code
carried as data, not a P2/P3 branch.

## 3. The J2534 boundary is split

The first notes treated `VehCom2.DLL` as the single wrapper. The second pass
splits that role across three layers.

### Registry resolution is in `Util.DLL`

`Util.RegistryScanner.ReadJ2534DeviceInfo()` merges
`ReadJ2534DiCEDeviceInfo()` and `ReadClassicJ2534DeviceInfo()`.
`Util.J2534.RegReader.ReadDeviceInformation()` opens
`SOFTWARE\PassThruSupport.04.04\` under `HKLM`, enumerates subkeys, and
`FillDeviceInformation` reads exactly three value names:

```
Vendor
FunctionLibrary
ConfigApplication
```

`DiceRegReader.FillDeviceInformation` gates on `DiCECompatible`, then reads
`Name`, `BtAddress` and `FwdlApplication`. `J2534ChannelManager` selects a
device from that map by `CommToolName`. `VehCom2.J2534.J2534Proxy..ctor(string)`
calls `LoadDll(string)`, which calls `LoadLibrary` and resolves the PassThru
entry points with `GetProcAddress`.

So `VehCom2` owns the load, but `Util.DLL` owns the registry model and the
`FunctionLibrary` resolution. The earlier absence of the `FunctionLibrary`
literal from `VehCom2` was expected, not a gap.

### The image has two registry views

Both keys are present in `Windows/System32/config/SOFTWARE`:

| View | `FunctionLibrary` | `ConfigApplication` | `Name` |
| --- | --- | --- | --- |
| `PassThruSupport.04.04\SETEK - DiCE` | `C:\Program Files\DiCE\Tools\TSDiCE64.dll` | `C:\Program Files\DiCE\Tools\DiagApp.exe` | absent |
| `WOW6432Node\PassThruSupport.04.04\SETEK - DiCE` | `C:\Program Files (x86)\DiCE\Tools\TSDiCE32.dll` | `C:\Program Files\DiCE\Tools\DiagApp.exe` | absent |

`TSDiCE64.dll` is 790016 B, SHA-256
`5af341982e8785507aa639001e77a9ceec316ef3685c45718956a8800d89dc83`, the same
bytes as the document's `DiCE32_x64.dll`. `TSDiCE32.dll` is 531456 B, SHA-256
`4dd4eb3bead080d6f7eedab3c93d587b2990ac7fe7565897a4085d0afb55a930`.

Which key a process reads depends on its bitness. The third pass shows VIDA's
Diagnostics control is hosted by 32-bit Internet Explorer and writes the
WOW6432Node `LogFile`, so its registry view names `TSDiCE32.dll`. See section 5.

### Device selection exists, but nothing is bound in this snapshot

`UI.CommToolSetting.PopulateCommTools` calls `RegistryScanner` and fills a
combo box from the result. `SaveSettings` persists `DiagnosticConnection` and
`CommToolName`. `CommToolSetupButtonClick` launches
`J2534DeviceInfo.ConfigApplication`. `J2534ChannelManager` selects by
`CommToolName`. `DiagApp.exe` outside `$VIDA` independently reads the same key
and logs `Calling PassThruOpen() with device name = `.

The shipped snapshot has no device bound: `CommToolName` is empty,
`DiagnosticConnection` is `NoCommConnection`, `commToolInformation.xml` does not
exist, there is no `Enum\USB\VID_17AA&PID_D1CE` instance, and
`DiCE/Tools/DiagApp.exe.log` says `No device found. Please connect a DiCE by USB
cable to your system.` So "nothing is selected" holds; "nothing can select" does
not.

`NoCommConnection` is persisted and the local override path is disabled
(`UseClientSettings=false`), but it is not the gate for the bus path.
`DiagnosticVehCommService` builds a transport from `CommToolType`, which comes
from the UI selection, not from `DiagnosticConnection`. The empty
`CommToolName` maps to `NOCOMM`, and `ensureCommToolConnection` handles only
`J2534` and `VIRTUAL`; `NOCOMM` throws at construction. See section 5.

### The timing consumer

`GetRequestTimeoutAndResend @ECU={0}` receives an integer ECU variant from
CarCom:

```
CarCom EcuConfig.EcuVariantId
  -> DiagnosticSettingBuilder.CreateSetting(EcuConfig)
  -> DiagnosticSetting._ecuVariantId
  -> DiceTimingLookup.set_EcuVariantId(int)
  -> DatabaseSession._ecuVariantId
  -> "EXECUTE GetRequestTimeoutAndResend @ECU={0}"
```

`EcuNameFromVariantId` is display-side. `VehCom2.DLL` does not reference
`AppServices.EcuDataResolver`, so it is not in the timing path. The extracted
rows are in section 5: 41 `Requests`, 3 `MessageTiming`, 1 `TimeoutAndResend`.

## 4. The DTC list is not a T191 block text

`T147_BlockMetaDTC` is an ECU-variant scope, not a text join. The only SQL that
names it, `vadis_GetAllPossibleDtcsOnEcu`, selects:

```sql
t141.name AS Data,
SUBSTRING(t150.CompareValue, 3, 6) AS CompareValue,
t147.fkT100_EcuVariant
FROM T141_Block t141
JOIN T144_BlockChild t144 ON t144.fkT141_Block_Child = t141.id
JOIN T150_BlockValue t150 ON t150.fkT141_Block = t141.id
JOIN T147_BlockMetaDTC t147 ON t147.fkT141_Block = t141.id
JOIN T100_EcuVariant ON t144.fkT100_EcuVariant = T100_EcuVariant.id
                    AND t147.fkT100_EcuVariant = T100_EcuVariant.id
WHERE t141.fkT142_BlockType = 10
```

The observed `T147` columns are `fkT141_Block` and `fkT100_EcuVariant`. No DTC
code column, text id column or language column was observed. The catalog has
`FK_T141_Block_T190_Text`, but DTC blocks do not populate it: the recovered
`T141_Block` rows of type 10 overwhelmingly carry `fkt190_Text = 0`.

The DTC list procedure therefore returns `T141_Block.name`, the structured fault
text, as `Data`. The `DtcVO.Text` and UI `Dtc.Text` binding is in section 5. For
block `1107137` the name is:

```
Ignition system/Misfire, Camshaft pos sensor, Camshaft pos sensor,
electrical fault, Too small resistance
```

The standard OBD description is a separate join through `T150_BlockValue`:

```
T150_BlockValue.CompareValue = "0x034211"
  -> T150_BlockValue.fkt190_text_value = 1004326
  -> T191_TextData.fkt190_text = 1004326 + fkt193_language
  -> "Camshaft Position Sensor A Circuit Low (Bank 1 or single sensor)"
```

The long article sentence is a third path. Text id `1,034,063` is a real
`T191` row:

```
Diagnostic trouble code (DTC) ECM-P034211 is for the camshaft position
(CMP) sensor, intake.
```

No CarCom table referenced that id in a raw 4-byte scan, so it is probably used
by the article or info-element side, not by the DTC list.

## 5. Third pass: closed and bounded

### The VIDA host is 32-bit

The Diagnostics control is IE-hosted from `jsp/login/login.jsp`:
`UI.DLL#Vcc.Vida.DiagSwdl.UI.DotNetPreLoader` in an `<object>` tag.
`VidaConfigApplication.exe` and `VidaMonitor.exe` are PE32 with
`32BITREQUIRED`; the JVM is i586 (`jdk/release` has `OS_ARCH="i586"`).

The registry write settles the view. `DotNetPreLoader.Start` calls
`DiceLogHandler.UpdateRegistryEntry`, which writes `LogFile` under
`SOFTWARE\PassThruSupport.04.04\SETEK - DiCE`. The WOW6432Node value is
`C:\VIDA\System\Log\Diagnostics\TSDice32_2022-02-01_05-53-11.log`, and
`VIDA.log` records the same session start at `05:53:11`. The 64-bit key still
has the vendor default `C:\Program Files\DiCE\Tools\TSDice32.log`. The 32-bit
view is therefore the one VIDA uses, and the earlier inference is supported.

### NOCOMM throws at construction

`NoCommConnection` parses to enum 3. `ensureCommToolConnection` handles only
`J2534` (0) and `VIRTUAL` (2); 1, 3 and 4 throw
`VehComException("Unsupported communication tool")`. `CommToolType` comes from
`UI.CommToolSetting.get_SelectedCommToolType`: empty name maps to 3, a name
starting `DiCE` maps to 1, `Virtual` maps to 2, otherwise 0. The shipped
`CommToolName` is empty, so transport construction throws before any J2534 call.
`DiagnosticConnection` is written by the UI and read by the self-test dialog,
not by the bus path.

### Timing values

The DiceTiming tables are small and fully read:

| Table | Rows | Content |
| --- | --- | --- |
| `Requests` | 41 | 11 ECU variants; B1 in {0xAB, 0xAE, 0xB2, 0xB9}; B2 1..245; B3 0 or 0x70 |
| `MessageTiming` | 3 | id 0: 40/110/10; id 1: 6/27/2; id 2: 10/110/2 |
| `TimeoutAndResend` | 1 | id 0: 3000 ms, resend 0 |

Every `Requests` row points at `TimeoutAndResendId = 0`, so the shipped policy
is 3000 ms and no retransmission. The per-variant variation is in
`P1max`/`P3min`/`P4min`, not in resend.

### DTC text binding

`DtcVO.Text` is populated from `T141_Block.name` by
`CarComRepository.GetAllPossibleDtcsOnD2Ecu`, and from the script's `Dtc.Text`
by `ScriptOutputConverter.GetDtcList`. The UI reads `Workflow.Dtc.Text`, not
`DtcVO.Text`; on the GGD path that text comes from the `T150_BlockValue` value
text. The `0x034211` block is type 32, not type 10, so it is not a D2
possible-DTC row; the T150/T191 description is the generic OBD path.

### T147 and the article text

`T147_BlockMetaDTC` exposes only `fkT141_Block` and `fkT100_EcuVariant` to every
shipped query, assembly, Java class, JSP and script. No third column is visible.
Text id 1,034,063 is referenced only from T190/T191/T192 text-store pages; no
DTC, block, script or application table points at it. The article is reached by
phrase URN, not by that id.

### NodeECU and T121 schema

`NodeECU` columns are `Id`, `Cid`, `Description`, `ObjVersion`; the clustered
index and FK names are as before. `T121_Config` has `id`, `fkT123_Bus`,
`fkT122_Protocol`, `fkT130_Init_Diag`, `fkT130_Init_Timing`, `physicalAddress`,
`functionalAddress`, `canAddress`, `commAddress`, `priority`, `canIdTX`,
`canIdRX`, `canIdFunc`, `canIdUUDT`, `busRate`, `addressSize` and
`fkT121_Config_Gateway`.

Rows for both tables were not reachable by page parsing. The page headers do not
carry the catalog object ids, and the `sys.sysrowsets` indirection was not
decoded. `NodeECU` row count and the `commAddress` to CAN-ID mapping therefore
remain open, but the schema is exact.

## 6. Corrections to the first notes

| First note | Correction |
| --- | --- |
| `vida-diagnostics-client.md` says script bodies are unopened and the compression scheme is unknown | ZIP plus deflate; bodies decoded |
| `vida-diagnostics-client.md` treats `EcuAddress` as possible CAN addressing | It is a CarCom node address; `T121_Config` holds the CAN IDs |
| `vida-diagnostics-client.md` calls `VehType` a platform switch | It is a 3-character data field; no branch or enumeration found |
| `vida-vehicle-profile.md` calls `fkNodeECU` the best lead for a per-profile ECU list | No shipped consumer found; the script is the logical map and CarCom is the physical map |
| `native-dice.md` repeats the `fkNodeECU` lead and the `VehType` switch | Same corrections |
| `vida-dtc-readout.md` treats `T147_BlockMetaDTC` as the missing DTC-to-text join | `T147` scopes blocks to ECU variants; the DTC list procedure returns `T141_Block.name` as `Data`, and `T150` carries the OBD value text |
| `dice-compatibility.md` says `VehCom2` is the single wrapper and no component selects a device | Registry resolution is in `Util.DLL`, load is in `VehCom2`, selection is in `UI.DLL` and `DiagApp.exe`; the shipped snapshot binds nothing |
| `dice-compatibility.md` says `TSDiCE32.dll` is the build VIDA loads | The image has both views. VIDA's Diagnostics host is 32-bit and writes the WOW6432Node `LogFile`, so it reads `TSDiCE32.dll`; a live DLL load was not logged |
| `vida-image-boundary.md` said the `NOCOMM` branch was not traced | The branch is decompiled: `NOCOMM` throws `VehComException("Unsupported communication tool")` at construction |
| `vida-diagnostics-client.md` left the timing values unread | 41 `Requests` rows, 3 `MessageTiming` rows, 1 `TimeoutAndResend` row: 3000 ms and no resend |
| `vida-dtc-readout.md` left the `DtcVO.Text` binding open | `DtcVO.Text` is set from `T141_Block.name` on the possible-DTC path and from script `Dtc.Text` on the read path; the UI reads `Workflow.Dtc.Text` |
| `vida-vehicle-profile.md` left the `NodeECU` rows open | Columns are `Id`, `Cid`, `Description`, `ObjVersion`; rows are still not readable by page parsing |

## 7. Remaining open questions

1. **How much script coverage is there?** Two concrete scripts were decoded.
   The full script set, and the bodies that fail CRC, remain unread.
2. **What is the complete `EcuAddress` to CAN-ID mapping?** `T121_Config`
   supplies the columns, but its rows were not reachable by page parsing. A
   read-only SQL Server attach on a copy would close it.
3. **What are the `NodeECU` rows?** The columns are known and the code consumer
   is absent, but the rows were not enumerated.
4. **Does `T147` have a physical third column?** Only the two keys are visible
   to every shipped query. A catalog dump would settle the physical schema.
5. **How does the article phrase map to text id 1,034,063?** The id appears only
   in T190/T191/T192, not in DTC or block tables. The IE phrase URN is the likely
   key, but the URN-to-id table was not located.
6. **What produces the D2 script's `Dtc.Text`?** `DtcOutput.AddDtc` has no
   direct caller in `BizServices.Scripting.DLL`; the script engine likely calls
   it. A type 10 read script would settle the source.
7. **How does `EcuVariantId` map to a vehicle or ECU name?** The timing table
   uses the integer; `EcuNameFromVariantId` is display-side and its backing data
   was not located.
8. **How are the timing values applied per session?** `P2CanServerMax` and the
   session-parameter adjustment are visible in strings, but the exact runtime
   arithmetic was not traced.

## Reproducing

```sh
VIDA=/path/to/image/VIDA
IMG=/path/to/image
D="$VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics"

# script ZIP header and decompression path
dd if="$VIDA/db/DiagSwdlRepository_Data.MDF" bs=1 skip=2176131182 count=128 \
  | xxd -g1
strings -a "$D/BizServices.ScriptRepository.DLL" \
  | grep -E "GetZippedScriptXml|DecompressString"
strings -a "$D/Util.DLL" | grep -E "ZipInputStream|GZipInputStream|RegReader"

# registry views, with a read-only REGF parser
# $IMG/Windows/System32/config/SOFTWARE
# PassThruSupport.04.04\SETEK - DiCE
# WOW6432Node\PassThruSupport.04.04\SETEK - DiCE

# 32-bit host and registry view
file "$VIDA/VidaConfigApplication.exe" "$VIDA/jdk/bin/java.exe"
grep -n "Starting new logsession" "$VIDA/System/Log/Diagnostics/VIDA.log" | tail
strings -a "$D/VehCom2.DLL" | grep "Unsupported communication tool"

# timing rows: Requests, MessageTiming, TimeoutAndResend
T="$VIDA/db/DiceTiming_Data.MDF"
dd if="$T" bs=1 skip=655456 count=64 | xxd -g1
dd if="$T" bs=1 skip=647264 count=32 | xxd -g1
dd if="$T" bs=1 skip=712800 count=32 | xxd -g1

# DTC procedure
MDF="$VIDA/db/CarComRT_Data.MDF"
LC_ALL=C grep -abo "vadis_GetAllPossibleDtcsOnEcu" "$MDF"
dd if="$MDF" bs=1 skip=2220800 count=1800 | strings -a -n 4
```

Managed inspection:

```
inspect_managed_members(path="$D/BizServices.ScriptRepository.DLL")
inspect_managed_members(path="$D/Util.DLL")
inspect_managed_members(path="$D/UI.DLL")
inspect_managed_members(path="$D/VehCom2.DLL")
inspect_managed_members(path="$D/Core.DLL")
inspect_managed_members(path="$D/BizServices.VehicleCommProvider.DLL")
inspect_managed_members(path="$D/AppServices.VehicleAccess.DLL")
inspect_managed_members(path="$D/BizServices.CarCom.DB.DLL")
```

## Caveats

The MDF files were read as pages, not attached. Recovered SQL text has sporadic
single-character corruption (`RELECT`, `BREATE`, `T141_Bloci`); every schema
claim above was cross-checked against compiled SQL literals in the assemblies.

The registry hive reports equal sequence numbers, but `SOFTWARE.LOG1`/`LOG2`
were not replayed, so a mid-write capture is not fully excluded. The 32-bit view
is observed from the `LogFile` write and the matching `VIDA.log` timestamp; no
live `LoadLibrary("TSDiCE32.dll")` was logged.

The DiceTiming pages have a stale first slot entry pointing into the padded
comment of the first record. The rows were read from the free-space pointer and
the contiguous record chain; the values match the row comments.

`NodeECU` and `T121_Config` rows were not reachable by page parsing: the page
headers do not carry the catalog object ids, and the `sys.sysrowsets` indirection
was not decoded. Their schemas come from `sys.syscolpars`; the row counts remain
unknown.

Some script bodies fail their CRC and were not used. The decoded values are
examples, not a complete census.

`fkNodeECU` is a bounded negative over the shipped artifacts, not a proof that
the table is unused in every deployment. The rows were not enumerated.
