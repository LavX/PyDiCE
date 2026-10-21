# VIDA image and adapter boundary

The other VIDA notes describe the vehicle data model, the diagnostic client, the
DTC readout, and the timing tables. This note records the artifact boundary
around them: what the VIDA application directory contains, which adapter binaries
sit outside it, and which statements in the repository are observations,
inferences, or still open.

Nothing here was executed. The binaries were read statically, the configuration
files were read as text, and the only runtime material is log text shipped with
the image. The second pass and its corrections are in
`docs/vida-second-pass.md`.

## Target

Paths are relative to an image root, written `$IMG` below, unless noted.

| Artifact | Size | SHA-256 | Role |
| --- | --- | --- | --- |
| `VIDA/Vida.exe` | 150016 B | `8be88c38df0e8223a7d502072c79da0ff8f53fe281afc777839d0a63984a268a` | top-level VIDA executable, PE32+ x86-64 |
| `VIDA/VidaConfigApplication.exe.Config` | 6516 B | not hashed | client path and J2534 registry settings |
| `VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics/VehCom2.DLL` | 266240 B | `3a08f5ff5819396e676eac8751fdb57475e8f76e20488926259993674ec97e82` | managed .NET J2534/DiCE layer, 347 types |
| `VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics/diagnostic.config.xml` | 1167 B | not hashed | shipped diagnostic settings |
| `Program Files (x86)/DiCE/Tools/TSDiCE32.dll` | 531456 B | `4dd4eb3bead080d6f7eedab3c93d587b2990ac7fe7565897a4085d0afb55a930` | 32-bit J2534 DLL named by the WOW6432Node view |
| `Program Files/DiCE/Tools/TSDiCE64.dll` | 790016 B | `5af341982e8785507aa639001e77a9ceec316ef3685c45718956a8800d89dc83` | 64-bit J2534 DLL named by the 64-bit view |
| `Program Files/DiCE/Tools/DiagApp.exe` | 471040 B | `c5ade052131c99913d3199c01d6296a7c6662a89a9633c3094d1078340bd99b6` | J2534 configuration application, also a client |
| `VIDA/db/DiceTiming_Data.MDF` | 1638400 B | not hashed | per-ECU-variant request timing |
| `VIDA/db/BaseData_Data.MDF` | 115081216 B | not hashed | vehicle profile and base data |
| `VIDA/System/Log/VidaTrace.log` | 20120 B | not hashed | runtime trace cited by the profile note |

This is not a complete database inventory. The data layer also includes
`DiagSwdlRepository_Data.MDF` (2944335872 B, scripts and documents),
`CarComRT_Data.MDF` (1822294016 B, DTC text and symptoms), and
`DiagSwdlSession_Data.MDF` (3801088 B, readout history). The specific notes give
their roles.

Two path facts matter when reading the other documents.

First, the J2534 libraries are not under `VIDA/`. `TSDiCE32.dll` is installed
under the image's `Program Files (x86)/DiCE/Tools/` directory; `TSDiCE64.dll`
and `DiagApp.exe` are under `Program Files/DiCE/Tools/`. `docs/dice-compatibility.md`
uses "VIDA image" for evidence beyond the application directory, including these
libraries and the driver registry. A search limited to `VIDA/` misses them.

Second, `Vida.exe` is a native PE32+ executable, while the diagnostic client in
`docs/vida-diagnostics-client.md` is a set of managed assemblies under
`VIDAWeb.war/Diagnostics`. `VidaConfigApplication.exe.Config` points
`DiagnosticPath` at that directory and `DiagConfigFile` at
`diagnostic.config.xml`. They are separate layers of the same installation.

## What the repository uses

Each VIDA document covers a different layer:

| Layer | Document | What it recovers |
| --- | --- | --- |
| vehicle identity and content | `docs/vida-vehicle-profile.md` | profile tuple, wildcard resolution, profile-ID set, timing tables |
| diagnostic client | `docs/vida-diagnostics-client.md` | script selection, vehicle resolution, ECU naming, client configuration |
| DTC description and context | `docs/vida-dtc-readout.md` | text-id lookup, symptom records, counters, freeze frames, fault-trace documents |
| second pass and corrections | `docs/vida-second-pass.md` | script bodies, `fkNodeECU`, registry split, DTC label correction |
| adapter command space | `docs/dice-compatibility.md` | DiCE USB framing, command IDs, J2534 entry points |
| scope | `README.md`, `docs/native-dice.md` | which VIDA workflows are mirrored and which are out of scope |

This note does not repeat those recoveries. It records what the artifact boundary
contains and which claims are supported by direct observation.

## The adapter path

The handoff is split across three managed layers in the Diagnostics folder, plus
the DiCE tools outside it.

`Util.DLL` owns the registry model. `RegistryScanner.ReadJ2534DeviceInfo` merges
the DiCE and generic readers; `RegReader.FillDeviceInformation` reads `Vendor`,
`FunctionLibrary` and `ConfigApplication` under
`SOFTWARE\PassThruSupport.04.04\`. `VehCom2.J2534.J2534ChannelManager` selects a
device by `CommToolName`, and `J2534Proxy.LoadDll` calls `LoadLibrary` and
resolves the PassThru entry points. `LoadDll` also binds six DiCE-specific
ioctls: `DiceGetHardwareVersion`, `DiceGetWarrantyDate`, `DiceControlLed`,
`DiceTest`, `ReadVBatt` and `ConnectionStrength`. `VehCom2`'s `CanIdAddress`
wraps three `u2` values. `UI.CommToolSetting.PopulateCommTools` fills
the selection list; `CommToolSetupButtonClick` launches the named
`ConfigApplication`.

`VehCom2.DLL` still carries the registry path and driver-load strings that the
first pass found:

```
SOFTWARE\PassThruSupport.04.04\SETEK - DiCE\
Error accessing registry
Loading J2534 driver:
J2534 Driver could not be loaded
```

Its timing and J2534 types are still the ones to read:

```
Vcc.Vida.DiagSwdl.VehCom2.Utils.DiceCommToolPropertiesReader
    .GetRegistryInformation()
Vcc.Vida.DiagSwdl.VehCom2.J2534.J2534DeviceManager
Vcc.Vida.DiagSwdl.VehCom2.J2534.J2534Proxy
Vcc.Vida.DiagSwdl.VehCom2.J2534.Diagnostics.D2onCanApplicationDICE
Vcc.Vida.DiagSwdl.VehCom2.J2534.Diagnostics.DiceTimingLookup
```

The image carries two registry views:

| View | `FunctionLibrary` | `ConfigApplication` |
| --- | --- | --- |
| `PassThruSupport.04.04\SETEK - DiCE` | `TSDiCE64.dll` | `DiagApp.exe` |
| `WOW6432Node\PassThruSupport.04.04\SETEK - DiCE` | `TSDiCE32.dll` | `DiagApp.exe` |

`Name` is absent in both. VIDA's Diagnostics control is IE-hosted and 32-bit;
`VidaConfigApplication.exe` and `VidaMonitor.exe` carry `32BITREQUIRED`, and the
JVM is i586. The `LogFile` write confirms the view: the WOW6432Node value is
`...TSDice32_2022-02-01_05-53-11.log`, the 64-bit value is the vendor default,
and `VIDA.log` records the same `05:53:11` session start. VIDA therefore reads
the WOW6432Node key and names `TSDiCE32.dll`.

`DiagApp.exe` outside the VIDA directory is itself a J2534 client: it reads the
same key, calls `PassThruOpen` with the selected device, and logs
`Calling PassThruOpen() with device name = `. `VehCom2` is the only loader in
the managed Diagnostics layer, not the only loader in the image.

`diagnostic.config.xml` ships `DiagnosticConnection` as `NoCommConnection` and
leaves `CommToolName` empty. The empty name maps to `CommToolType=NOCOMM`, and
`ensureCommToolConnection` handles only `J2534` and `VIRTUAL`; `NOCOMM` throws
`VehComException("Unsupported communication tool")` at construction, before any
J2534 call. `DiagnosticConnection` itself is UI and self-test state, not the bus
gate. The shipped snapshot therefore prevents a live connection through the
empty tool name, not through the `NoCommConnection` string. The UI can still
select a tool.

`VehCom2.DLL` is also where the timing database is consumed. Its strings include
`EXECUTE GetRequestTimeoutAndResend @ECU={0}` and the protocol-specific timing
messages `New ISO9141 timing set. P1max:{0}, P3min:{1}, P4min:{2}` and
`P2Can: {0} ms, P2*Can: {1} ms, P4Can: {2} ms`. That ties the
`DiceTiming_Data.MDF` rows to the J2534 channel rather than to the web layer.
The client note records the full set of timing strings and the session-parameter
adjustment.

The static evidence is consistent with this chain:

```
VIDA diagnostic session
    -> UI comm-tool selection (optional)
    -> Util.RegistryScanner (FunctionLibrary)
    -> VehCom2.J2534ChannelManager / J2534Proxy.LoadDll
    -> J2534 DLL (TSDiCE32 for VIDA's 32-bit host; TSDiCE64 for 64-bit clients)
    -> DiceUsb.sys
    -> DiCE adapter
```

That chain is an inference. No component was loaded or called. The first notes
called `VehCom2.DLL` a single wrapper; the second pass shows the split across
`Util.DLL` and `UI.DLL`. `DiagApp.exe` is a second client outside the VIDA
directory.

## The Python path

`volvo_p3` does not cross the J2534 boundary. `dice_rpc.py` builds the `0xB5`
USB RPC frame and its two checksums directly, and `dice_can.py` drives RawCAN
`0x80` and `0x81` over PyUSB. `docs/native-dice.md` records that the framing came
from `DiCE32_x64.dll`, not from VIDA.

The two stacks meet at the adapter, not in code:

```
VIDA web/Java -> profile and script SQL
VIDA .NET Diagnostics -> J2534 -> adapter
PyDiCE -> volvo_p3.dice_rpc -> PyUSB -> adapter
```

This is why the VIDA tree can inform `volvo_p3` without becoming a dependency.
It describes the same car and the same adapter from the other side of the vendor
API.

## What changes a read, and what changes the report

The VIDA work now separates three things that were easy to conflate.

* **The script carries the logical read plan.** The profile set selects a ZIP
  XML script through `GetScript`. The decoded `Components.VehComm` nodes carry
  `EcuAddress` or `EcuType`, `ServiceId`, `Request` and `Response`. `EcuAddress`
  is a one-byte CarCom node address, not a CAN ID; the physical CAN IDs come
  from `T121_Config.CanIdRX`/`CanIdTX`/`CanIdFunc`. Concrete values are in
  `docs/vida-second-pass.md`.
* **The timing database changes the wait.** `DiceTiming_Data.MDF` keys `P1max`,
  `P3min`, `P4min`, `Timeout` and `Resend` by ECU variant plus request bytes.
  `VehCom2.DLL` executes `GetRequestTimeoutAndResend @ECU={0}` and applies the
  result per protocol: `P1max`/`P3min`/`P4min` over ISO 9141 and `P2Can`/`P4Can`
  over CAN. The table has 41 `Requests` overrides, 3 `MessageTiming` rows and 1
  `TimeoutAndResend` row; every override uses 3000 ms and no resend. This is a
  separate axis from the profile and the script.
  `AppServices.EcuDataResolver.DLL` exposes `EcuNameFromVariantId` for display
  use, but `VehCom2.DLL` does not reference that assembly, so it is not part of
  the runtime timing path.
* **The DTC layer is mostly report, with one more read.** After a script returns,
  `ReadStatusIdentifiers` can issue another ECU read. The DTC list procedure
  returns `T141_Block.name` as `Data`, the standard OBD description is a
  `T150_BlockValue` text id, symptoms come from `CarCom`, status words are
  computed from the status bytes, and counters, freeze frames and history come
  from `DiagSwdlSession` plus fault-trace documents from `DiagSwdlRepository`.
  That path is in `docs/vida-dtc-readout.md`; it does not supply another source
  of DTC codes.

One more detail matters. `VehType` is `p2x` in the shipped configuration, but
no managed code branches on it: the field is read only by its getter and
`GetScript` does not take it. It is a three-character data value, not a P2/P3
switch. The `volvo_p3` platform set remains a local design choice.

The data layer is split accordingly: `BaseData` holds profiles,
`DiagSwdlRepository` holds scripts and documents, `CarComRT` holds DTC text and
symptoms, `DiagSwdlSession` holds readout history, and `DiceTiming` holds
per-variant timing. The split follows the data lifecycle, and it is why one
database cannot answer the whole question.

On the Python side the seams are explicit but empty. `VehicleState` has no
`year` or `model` field, and `_create_profile` echoes them into a string and
discards them (`volvo_p3/tui.py:71-76`, `:470-482`). `ModuleTarget` carries only
name, request ID and response ID (`volvo_p3/module_scan.py:19-25`), and
`scan_modules` takes one timeout (`:151-157`). There is no resend policy in
`dice_can.py`.

## Verification status

In this table, `observed` means the second pass saw it directly, `documented`
means it comes from another VIDA note and was not independently re-derived,
`unverified` means the available evidence does not settle it, and `contradicted`
means the available evidence goes against the earlier wording.

| Statement | Status | Basis |
| --- | --- | --- |
| The image carries 32-bit `TSDiCE32.dll` and 64-bit `TSDiCE64.dll` with the documented hashes | observed | `sha256sum`, `file` |
| VIDA's Diagnostics host is 32-bit and writes the WOW6432Node `LogFile` | observed | IE-hosted control, `32BITREQUIRED`, `VIDA.log` timestamp |
| `VehCom2.DLL` contains the J2534 loader and timing identifiers | observed | managed metadata and user strings |
| The shipped configuration uses `NoCommConnection` with an empty tool name | observed | `diagnostic.config.xml` |
| `VehCom2.DLL` is the only J2534 loader in Diagnostics | observed | only `VehCom2` has `LoadLibrary`; `DiagApp.exe` outside VIDA also loads it |
| The `FunctionLibrary` value is resolved by `Util.DLL` | observed | `RegReader.FillDeviceInformation` reads `Vendor`, `FunctionLibrary`, `ConfigApplication` |
| No component can select a J2534 device | contradicted | `UI.CommToolSetting.PopulateCommTools`; the shipped snapshot binds none |
| `NoCommConnection` is the bus-path gate | contradicted | empty `CommToolName` maps to `NOCOMM`, which throws at construction; the string is UI state |
| The image has 64-bit and WOW6432Node registry views with different `FunctionLibrary` values | observed | `SOFTWARE` hive |
| `DiceTiming` has 41 `Requests`, 3 `MessageTiming` and 1 `TimeoutAndResend` row; default 3000 ms, no resend | observed | page-level extraction |
| `T121_Config` has no page set in this snapshot | observed | catalog scan; legacy row counter 2766 |
| Scripts are ZIP XML and `EcuAddress` is a node address | observed | ZIP header, `ZipInputStream`, `T121_Config.commAddress` |
| The script `EcuAddress` is the `commAddress` side, not `canAddress` | observed | CarCom proc selects both; managed chain calls `commAddress` the node address |
| `fkNodeECU` is a per-profile ECU list | contradicted | no shipped consumer; the script is the logical map and `T121_Config` the physical map |
| `NodeECU` has 52 label rows and no address column | observed | BaseData page 12028 / auid 152 |
| The DTC list procedure returns `T141_Block.name` as `Data`; `T147` is not the text join | observed | `vadis_GetAllPossibleDtcsOnEcu` |
| `T147` has no `sysschobjs` or `syscolpars` object in this snapshot | observed | catalog search; name only in `syssoftobjrefs` |
| The P034211 article phrase is inline in the servicerep `servinfosub` XML | observed | phrase text, no URN-to-id resolver |
| The VIDA image holds no adapter command table | documented | targeted searches; absence is bounded by that coverage |

## Open questions

1. **How complete is the script coverage?** Only about 2,000 bodies are
   CRC-clean; several thousand parse as XML, and tens of thousands are truncated
   or character-damaged. A type 10 read script (`VCC-227144`) is decoded; the
   damaged bodies remain unread.
2. **What is the complete `EcuAddress` to CAN-ID mapping?** `T121_Config` has
   the schema, and the proc/managed chain puts `EcuAddress` on the `commAddress`
   side. The table appears empty in this snapshot, so no CAN ID can be resolved
   from the image; a runtime car configuration would populate it.
3. **Does `T147` have a physical third column?** Only the two keys are visible
   to every shipped query. The table has no `sysschobjs` or `syscolpars` object
   in this snapshot, so the physical schema may be absent.
4. **How does the article phrase map to text id 1,034,063?** The P034211 phrase
   `urn:x-nevis:phrase:0800c8af84c73062` is inline in the servicerep
   `servinfosub` XML; no URN-to-text-id table or runtime resolver was found.
5. **How does `EcuVariantId` map to a vehicle or ECU name?** The id is
   `T100_EcuVariant.id` via `T120_Config_EcuVariant`, and
   `EcuNameFromVariantId` uses `vadis_GetEcuVariantData`/`T102_EcuType.description`;
   the T102 rows were not read.
6. **Does DiceTiming drive CAN or only the K-line D2 path?** A third-pass IL read
   puts it on `D2onKApplicationDICE3`, while CAN appears to use
   `vadis_GetDiagTimings`; no managed caller for the CAN proc was found.
7. **Can the command-space negative result be made complete?** The image is
   large and the current evidence is a set of targeted searches.
8. **What does `Vida.exe` do?** It was identified and hashed, not analysed.

## Reproducing

```sh
VIDA=/path/to/image/VIDA
IMG=/path/to/image
D="$VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics"

sha256sum "$VIDA/Vida.exe" "$D/VehCom2.DLL" \
  "$IMG/Program Files (x86)/DiCE/Tools/TSDiCE32.dll" \
  "$IMG/Program Files/DiCE/Tools/TSDiCE64.dll" \
  "$IMG/Program Files/DiCE/Tools/DiagApp.exe"
file "$VIDA/Vida.exe" "$VIDA/VidaConfigApplication.exe" \
  "$VIDA/jdk/bin/java.exe" "$IMG/Program Files/DiCE/Tools/DiagApp.exe" \
  "$IMG/Program Files/DiCE/Tools/TSDiCE64.dll" \
  "$IMG/Program Files (x86)/DiCE/Tools/TSDiCE32.dll"
grep -n "Starting new logsession" "$VIDA/System/Log/Diagnostics/VIDA.log" | tail
stat -c '%n %s' "$VIDA/Vida.exe" "$VIDA/VidaConfigApplication.exe.Config" \
  "$D/VehCom2.DLL" "$IMG/Program Files (x86)/DiCE/Tools/TSDiCE32.dll" \
  "$IMG/Program Files/DiCE/Tools/TSDiCE64.dll" \
  "$IMG/Program Files/DiCE/Tools/DiagApp.exe"

grep -n "DiagnosticPath\|DiagConfigFile\|PassThruSupport" \
  "$VIDA/VidaConfigApplication.exe.Config"
grep -n "DiagnosticConnection\|CommToolName" "$D/diagnostic.config.xml"
strings -a "$D/Util.DLL" | grep -E "FunctionLibrary|RegReader|ZipInputStream"
strings -a "$D/UI.DLL" | grep -E "PopulateCommTools|CommToolName"
strings -a -el "$D/VehCom2.DLL" \
  | grep -E "PassThruSupport|J2534|Loading J2534"

# Read the SOFTWARE hive read-only. Both keys carry the same value names:
# PassThruSupport.04.04\SETEK - DiCE
# WOW6432Node\PassThruSupport.04.04\SETEK - DiCE
# The WOW6432Node LogFile timestamp should match the last VIDA.log session start.
```

Managed assembly inspection:

```
inspect_managed_artifact(path="$D/VehCom2.DLL")
inspect_managed_members(
    path="$D/VehCom2.DLL",
    type_names=[
        "Vcc.Vida.DiagSwdl.VehCom2.Utils.DiceCommToolPropertiesReader",
        "Vcc.Vida.DiagSwdl.VehCom2.J2534.J2534DeviceManager",
        "Vcc.Vida.DiagSwdl.VehCom2.J2534.Diagnostics.DiceTimingLookup",
    ],
)
```

Both operations are static. They do not load the assembly or execute target code.

## Caveats

The inspection is static and the image was not run. `System/Log/VidaTrace.log`
is log text, not a replay, and no hardware was attached.

The database files are SQL Server MDF files. They were not attached to a SQL
Server instance, and the stored-procedure extraction used by the profile note is
lossy. The script bodies are zipped and were not opened, so `EcuAddress` and
`ServiceId` are known by name, not by value.

The tree is a 2022 snapshot of VIDA 2014D. The adapter library lives outside the
application directory, and the full Windows image was not inventoried.
