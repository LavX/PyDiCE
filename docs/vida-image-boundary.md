# VIDA image and adapter boundary

The other VIDA notes describe the vehicle data model, the diagnostic client, the
DTC readout, and the timing tables. This note records the artifact boundary
around them: what the VIDA application directory contains, which adapter binaries
sit outside it, and which statements in the repository are observations,
inferences, or still open.

Nothing here was executed. The binaries were read statically, the configuration
files were read as text, and the only runtime material is log text shipped with
the image.

## Target

Paths are relative to an image root, written `$IMG` below, unless noted.

| Artifact | Size | SHA-256 | Role |
| --- | --- | --- | --- |
| `VIDA/Vida.exe` | 150016 B | `8be88c38df0e8223a7d502072c79da0ff8f53fe281afc777839d0a63984a268a` | top-level VIDA executable, PE32+ x86-64 |
| `VIDA/VidaConfigApplication.exe.Config` | 6516 B | not hashed | client path and J2534 registry settings |
| `VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics/VehCom2.DLL` | 266240 B | `3a08f5ff5819396e676eac8751fdb57475e8f76e20488926259993674ec97e82` | managed .NET J2534/DiCE layer, 347 types |
| `VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics/diagnostic.config.xml` | 1167 B | not hashed | shipped diagnostic settings |
| `Program Files (x86)/DiCE/Tools/TSDiCE32.dll` | 531456 B | `4dd4eb3bead080d6f7eedab3c93d587b2990ac7fe7565897a4085d0afb55a930` | 32-bit J2534 DLL installed on the image |
| `VIDA/db/DiceTiming_Data.MDF` | 1638400 B | not hashed | per-ECU-variant request timing |
| `VIDA/db/BaseData_Data.MDF` | 115081216 B | not hashed | vehicle profile and base data |
| `VIDA/System/Log/VidaTrace.log` | 20120 B | not hashed | runtime trace cited by the profile note |

This is not a complete database inventory. The data layer also includes
`DiagSwdlRepository_Data.MDF` (2944335872 B, scripts and documents),
`CarComRT_Data.MDF` (1822294016 B, DTC text and symptoms), and
`DiagSwdlSession_Data.MDF` (3801088 B, readout history). The specific notes give
their roles.

Two path facts matter when reading the other documents.

First, `TSDiCE32.dll` is not under `VIDA/`. It is installed under the image's
`Program Files (x86)/DiCE/Tools/` directory. `docs/dice-compatibility.md` uses
"VIDA image" for evidence beyond the application directory, including this
library and the driver registry. A search limited to `VIDA/` misses the adapter
library.

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
| adapter command space | `docs/dice-compatibility.md` | DiCE USB framing, command IDs, J2534 entry points |
| scope | `README.md`, `docs/native-dice.md` | which VIDA workflows are mirrored and which are out of scope |

This note does not repeat those recoveries. It records what the artifact boundary
contains and which claims are supported by direct observation.

## The adapter path

Of the Diagnostics assemblies, this pass inspected only `VehCom2.DLL` for the
adapter handoff. Its metadata contains:

```
Vcc.Vida.DiagSwdl.VehCom2.Utils.DiceCommToolPropertiesReader
    .GetRegistryInformation()
Vcc.Vida.DiagSwdl.VehCom2.J2534.J2534DeviceManager
Vcc.Vida.DiagSwdl.VehCom2.J2534.J2534Proxy
Vcc.Vida.DiagSwdl.VehCom2.J2534.Diagnostics.D2onCanApplicationDICE
Vcc.Vida.DiagSwdl.VehCom2.J2534.Diagnostics.DiceTimingLookup
```

Its user strings include the vendor registry path and the driver load messages:

```
SOFTWARE\PassThruSupport.04.04\SETEK - DiCE\
Error accessing registry
Loading J2534 driver:
J2534 Driver could not be loaded
```

`diagnostic.config.xml` ships `DiagnosticConnection` as `NoCommConnection` and
leaves `CommToolName` empty. `TSDiCE32.dll` on the image is 32-bit and matches
the hash recorded in `docs/dice-compatibility.md` exactly.

`VehCom2.DLL` is also where the timing database is consumed. Its strings include
`EXECUTE GetRequestTimeoutAndResend @ECU={0}` and the protocol-specific timing
messages `New ISO9141 timing set. P1max:{0}, P3min:{1}, P4min:{2}` and
`P2Can: {0} ms, P2*Can: {1} ms, P4Can: {2} ms`. That ties the
`DiceTiming_Data.MDF` rows to the J2534 channel rather than to the web layer.
The client note records the full set of timing strings and the session-parameter
adjustment.

Taken together, the static evidence is consistent with this chain:

```
VIDA diagnostic session
    -> VehCom2 managed layer
    -> PassThruSupport registry entry
    -> J2534 DLL (TSDiCE32.dll)
    -> DiceUsb.sys
    -> DiCE adapter
```

That chain is an inference. No component was loaded or called. The repository's
`dice-compatibility.md` describes `VehCom2.DLL` as a single wrapper; the
observed types and strings support the wrapper role, but "single" is an
exclusivity claim this pass did not test against every assembly.

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

* **The script carries the read plan.** The profile set selects a compressed XML
  script through `GetScript`. The parser names in `BizServices.Scripting.DLL`
  show `ecu` elements with `EcuAddress`, `TesterAddress`, `EcuIdentifier`,
  `EcuName`, `EcuType` and `EcuVariant`, plus `Request`, `Response`, `ServiceId`,
  `OnNegativeResponse` and `OnNoResponse`. That is where a per-vehicle CAN
  address and service live. No script body was read, so the field values and the
  exact wire meaning of `EcuAddress` remain open.
* **The timing database changes the wait.** `DiceTiming_Data.MDF` keys `P1max`,
  `P3min`, `P4min`, `Timeout` and `Resend` by ECU variant plus request bytes.
  `VehCom2.DLL` executes `GetRequestTimeoutAndResend @ECU={0}` and applies the
  result per protocol: `P1max`/`P3min`/`P4min` over ISO 9141 and `P2Can`/`P4Can`
  over CAN. This is a separate axis from the profile and the script.
  `AppServices.EcuDataResolver.DLL` exposes `EcuNameFromVariantId` as the named
  bridge from that integer variant to an ECU name; the backing data was not
  located.
* **The DTC layer is mostly report, with one more read.** After a script returns,
  `ReadStatusIdentifiers` can issue another ECU read. The description itself
  comes from a `CarCom` text id, symptoms from `CarCom`, status words are
  computed from the status bytes, and counters, freeze frames and history come
  from `DiagSwdlSession` plus fault-trace documents from `DiagSwdlRepository`.
  That path is in `docs/vida-dtc-readout.md`; it does not supply another source
  of DTC codes.

One more detail matters. The client's `VehType` switch is `p2x` in the shipped
configuration, while `volvo_p3` hardcodes a P3 set. That switch is the natural
seam if this repository ever carries more than one platform profile.

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

In this table, `observed` means this pass saw it directly, `documented` means it
comes from the other VIDA notes and was not independently re-derived, and
`unverified` means the available evidence does not settle it.

| Statement | Status | Basis |
| --- | --- | --- |
| The image contains a 32-bit `TSDiCE32.dll` with the documented hash | observed | `sha256sum`, `file` |
| `VehCom2.DLL` contains J2534, DiCE registry, and driver-loading identifiers | observed | managed metadata and user strings |
| The shipped configuration uses `NoCommConnection` with an empty tool name | observed | `diagnostic.config.xml` |
| `VehCom2.DLL` is the only component that loads the J2534 driver | unverified | only one assembly inspected |
| The `FunctionLibrary` value is resolved by `VehCom2.DLL` | unverified | the literal was not among the assembly's 356 user strings |
| No component in the image selects a J2534 device | unverified | targeted searches over a 12 GB image, not a complete inventory |
| The timing key is ECU variant plus request bytes | documented | recovered from the MDF; only `DiceTimingLookup` and `EcuVariantId` were observed in the assembly |
| `VehCom2.DLL` consumes the timing database per protocol | documented | `GetRequestTimeoutAndResend` and the ISO 9141/CAN timing strings in the client note |
| The profile-selected script carries ECU address and service names | documented | parser names in `BizServices.Scripting.DLL`; no script body was read |
| The concrete `EcuAddress` and `ServiceId` values are known | unverified | no `ScriptContent.XmlDataCompressed` row was decompressed |
| The Java content stack does not map profile IDs to an ECU list | documented | negative result from the Java stack; the client instead selects a script carrying `EcuAddress`, and `fkNodeECU` stays unresolved |
| DTC descriptions and symptoms come from `CarCom`, after the script returns | documented | call edges and SQL in `vida-dtc-readout.md` |
| The VIDA image holds no adapter command table | documented | targeted searches; absence is bounded by that coverage |

## Open questions

1. **Is `VehCom2.DLL` the only J2534 loader?** `J2534DeviceManager` lives there,
   but the other Diagnostics assemblies were not swept for a second loader.
2. **Where is the `FunctionLibrary` value resolved?** The registry path is
   visible, but the value name was not among the 356 user strings in
   `VehCom2.DLL`. It could be in another assembly or built at runtime.
3. **Does `NoCommConnection` hold at runtime?** The shipped file says so. An
   override from user settings or another configuration was not tested.
4. **What does the vendor registry key contain?** The docs say it is populated
   with no `Name` value. The registry hive was not read in this pass.
5. **Can the command-space negative result be made complete?** The image is
   large and the current evidence is a set of targeted searches.
6. **What does `Vida.exe` do?** It was identified and hashed, not analysed.
7. **What values does a script carry, and what does `EcuAddress` mean on the
   wire?** Parser names show the fields; no `XmlDataCompressed` body or concrete
   address was read. The client note records the same gap.

## Reproducing

```sh
VIDA=/path/to/image/VIDA
IMG=/path/to/image
D="$VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics"

sha256sum "$VIDA/Vida.exe" "$D/VehCom2.DLL" \
  "$IMG/Program Files (x86)/DiCE/Tools/TSDiCE32.dll"
file "$VIDA/Vida.exe" \
  "$IMG/Program Files (x86)/DiCE/Tools/TSDiCE32.dll"
stat -c '%n %s' "$VIDA/Vida.exe" "$VIDA/VidaConfigApplication.exe.Config" \
  "$D/VehCom2.DLL" "$IMG/Program Files (x86)/DiCE/Tools/TSDiCE32.dll"

grep -n "DiagnosticPath\|DiagConfigFile\|PassThruSupport" \
  "$VIDA/VidaConfigApplication.exe.Config"
grep -n "DiagnosticConnection\|CommToolName" "$D/diagnostic.config.xml"
strings -a -el "$D/VehCom2.DLL" \
  | grep -E "PassThruSupport|J2534|Loading J2534"
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
