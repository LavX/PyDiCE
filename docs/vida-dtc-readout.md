# VIDA DTC readout and fault context

The other VIDA notes stop where a diagnostic script returns. This note follows the
record after that point: how a code read from the car acquires a description, what
else is attached to it, and which database each part comes from.

It answers the question a reader of `docs/vida-diagnostics-client.md` is left with.
That note establishes that the read plan is a compressed script keyed by profile.
It does not say where the sentence next to the code comes from. VIDA has more
than one description path: the DTC list procedure returns `T141_Block.name` as
`Data`, the standard OBD description is a `T150_BlockValue` row pointing at a
localized `T191` text, and the long article sentence is a separate `T191` row.
None is interpreted from the fault.

Nothing here was executed. The assemblies were read statically: PE/CLI metadata,
member signatures, call edges, and user strings. The databases were searched as
bytes, not attached to SQL Server.

A second pass corrected the label path. `T147_BlockMetaDTC` scopes blocks to ECU
variants; it is not the text join. The evidence is in
`docs/vida-second-pass.md`.

## Target

Paths are relative to an image root, written `$VIDA` below, unless noted. All paths below the ear
directory are prefixed `jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics`.

| Assembly | Size | SHA-256 | Role |
| --- | --- | --- | --- |
| `Workflow.DLL` | 372736 B | `ba78b669efea360887d333353451e9411e82004e368b356f74200d8fcb427ebd` | fault-trace workflow, 254 types, 2062 methods |
| `AppServices.VehicleAccess.DLL` | 77824 B | `3bcaac2ce2dbe0b986a1c8588bbe3d75fe579bb41bb1eada759ccdb7214545a0` | readout entry point, script invocation |
| `BizServices.CarCom.VehicleServices.DLL` | 94208 B | `1735d8ac20d63a20afe13742ea5156cce5a5328392bd3e44d9ec3cd8ae0dbd07` | request/response decoding, text resolution |
| `BizServices.CarCom.DB.DLL` | 57344 B | `a54117e6b2cc97c28bacf5df72f060ee8694de85291194046235bfd85a700be6` | symptom and text lookups |
| `BizServices.DiagnosticSession.DLL` | 45056 B | `6bf8443387677297717a057714a8c9a8a74b685f41002791d4a71078ff0fbd94` | readout persistence |
| `BizServices.InfoElementsRepository.DLL` | 28672 B | `79c78e1443d988661b4e1dc8abc76084e914fb197a970451011d98b0716a0967` | fault-tracing document lookup |
| `ValueObjects.DLL` | 159744 B | `f9f875183031b9553096d771f3f186b85d16256d63bdb4b49977ea0795bbb5c5` | `DtcVO`, 259 types |
| `UI.DLL` | 1388544 B | `6a521808273dcda07f09e9a5766e45bb5ae6ab70a6093d5dfefe990ffb404bf2` | fault list, counters, timeline, 346 types |
| `ResourceManager.DLL` | 921600 B | `8b40b9676574add979f62d355baaa3939600b23e40034b9dc14a41edf7bdf7e6` | localized labels |

All are PE/CLI, IL-only, .NET metadata `v2.0.50727`, x86.

## The data layer

`components.config.xml` binds each repository to one database. The split matters:
the text and symptom data is not in the same database as the documents.

| Component | Database | Provides |
| --- | --- | --- |
| `VehicleServiceDatabase` | `CarCom` | service definitions, blocks, texts, symptoms |
| `CarComRepository` | `CarCom` | ECU data, DTC symptom mapping, customer symptoms |
| `InfoElementsRepository` | `DiagSwdlRepository` | fault-tracing information elements |
| `DiagnosticSessionService` | `DiagSwdlSession` | readouts, counters, status identifiers, freeze frames |

## The read path

The call chain below is observed as call edges, not as a run.

```
DtcFaulttraceWF.ReadDtcForEcu
  -> IVehicleAccess.PerformDtcReadoutOnEcu
  -> IRunScriptService.set_EcuTypeContext
  -> PerformDtcReadoutPrivate
       -> IRunScriptService.RunDtcScript
       -> IRunScriptService.RunEcmDtcScript
       -> ScriptOutputConverter.CreateReadDtcResult
  -> IVehicleAccess.ReadStatusIdentifiers
```

`RunDtcScript` and `RunEcmDtcScript` are why the script note is correct: the code
read is driven by the script selected for the profile and ECU function group, not
by a request table in the assembly. What the assembly adds is the decoding and
display layer, which runs on the script's output.

`DtcFaulttraceWF` also exposes `ReadDtcAllEcu`, `GetDtcs`, `GetEcuStatus`,
`ClearDtcForEcu`, `ShowFreezeFrame`, `GetFreezeFramesToShow`, and
`GetIEFromDtcSymptomId`. The read is therefore one step in a workflow that keeps
readout state across calls.

## Where the description comes from

The display text is resolved by integer text id through three hops:

```
Block.get_AsText          (CarCom.VehicleServices)
  -> BlockHandler.GetText
  -> SPGeneral.GetText
  -> exec vadis_GetText   (CarCom database)
```

`Block.get_DisplayText` calls `AsText` and `PpeText`, and
`ResponseHandler.HandleBlockByType` calls `get_DisplayText` and
`BlockValue.get_ValueText`. So for blocks that carry a text id, the text is
whatever row the id points at, in the active language. DTC blocks are the
exception; the next subsection shows their label comes from the block name.

The assembly literals make the call concrete:

```
vadis_GetText
@textId
@languageCode
Database: {0}, SP: {1}, TextId: {2}, Language: {3}
TEXT NOT FOUND
```

`BizServices.CarCom.DB.DLL` `CarComRepository.GetText` carries the same procedure
name and logs the same `TextId`/`Language` pair. The language code appears in the
assembly as `en-US`.

The text store is the pair `T190_Text` (the key) and `T191_TextData` (the
localized rows). Two statements from `VehicleServiceDatabase` address it directly:

```sql
select fkt190_text from t191_textdata where status=0 and fkt190_text={0}
```

```sql
select
    val=case fkt155_scaling
        when 0 then fkt190_text_ppevalue
        else fkt190_text_value
        end,
    unit=case fkt155_scaling
        when 0 then fkt190_text_ppeunit
        else fkt190_text_unit
        end
from t150_blockvalue
join t141_block t141 on fkt141_block = t141.id
where t141.fkt190_text in ({0}) and t141.fkt142_blocktype=8
```

`t142_blocktype=8` is also the filter for freeze frames, joined through
`t148_blockmetapara` on `showasfreezeframe=1`. Blocks of the same type carry both
the fault record and its context values.

The rows themselves are in the shipped database. Extracting UTF-16 strings from
`db/CarComRT_Data.MDF` yields 122 lines of the form:

```
Diagnostic trouble code (DTC) ECM-P034211 is for the camshaft position (CMP) sensor, intake.
```

That is the pattern behind the long description: the article sentence names both
the code and the component. A missing id has an explicit fallback,
`TEXT NOT FOUND`.

### Which text the DTC list procedure returns

The DTC list procedure, `vadis_GetAllPossibleDtcsOnEcu`, returns
`t141.name AS Data` and `SUBSTRING(t150.CompareValue, 3, 6)`. `T147` joins the
block to an ECU variant, not to a text row. The `T141_Block` rows of type 10
overwhelmingly carry `fkt190_Text = 0`, so the block text path does not supply
their data.

There are three text paths, and only one is the DTC list data:

| Path | Key | Supplies |
| --- | --- | --- |
| DTC list data | `T141_Block.name` | the structured fault text |
| Standard OBD description | `T150_BlockValue.CompareValue` -> `fkt190_text_value` -> `T191_TextData` | the SAE-style description |
| Long article sentence | `T191_TextData` by text id, no CarCom referrer found | article or info-element text |

For `0x034211`, the value row points at text id 1,004,326, "Camshaft Position
Sensor A Circuit Low (Bank 1 or single sensor)". The long sentence text id
1,034,063 is a real `T191` row, but no CarCom table referenced it in a raw
4-byte scan. The `0x034211` block is type 32, not type 10, so it is not a D2
possible-DTC row; the T150/T191 description is the generic OBD path.

`DtcVO.Text` is populated from `T141_Block.name` by
`CarComRepository.GetAllPossibleDtcsOnD2Ecu`, and from the script's `Dtc.Text`
by `ScriptOutputConverter.GetDtcList`. The UI reads `Workflow.Dtc.Text`, not
`DtcVO.Text`; on the GGD path that text is the `T150_BlockValue` value text.

## What is attached as context

`BizServices.CarCom.DB.DLL` exposes and names these stored procedures. They are
the second half of the answer, the part that is not the DTC text itself.

| Lookup | Stored procedure | Returns |
| --- | --- | --- |
| symptoms for an ECU | `vadis_GetDtcSymptomsForEcu` | symptom ids for the DTCs on that ECU |
| symptom status | `vadis_GetSymptomStatusText` | status wording per symptom and ECU variant, in one language |
| customer symptom | `vadis_GetCustomerSymptomCode` | the customer-facing code |
| customer symptom | `vadis_GetCustomerSymptomComponentName` | component name |
| customer symptom | `vadis_GetCustomerSymptomDeviation` | deviation text |
| customer symptom | `vadis_GetCustomerSymptomComment1`, `Comment2` | two free-text comments |
| reverse mapping | `vadis_GetDtcSymptomIdsFromCustomerSymptomId` | DTC symptoms behind a complaint |
| freeze frames | `vadis_GetAllowedFFForEcuVariant` | context values that ECU allows |
| completeness | `vadis_GetAllPossibleDtcsOnEcu`, `vadis_GetNumberOfDtcsOnEcu` | expected population, so a short list can be noticed |
| grouping | `vadis_GetSymptomType`, `vadis_GetFunctionGroup1`, `vadis_GetFunctionGroup2`, `vadis_GetComponent`, `vadis_GetCSC` | the symptom tree the fault-trace form navigates |

Six schema families back this: `T152_Symptom`, `T153_SymptomCategory`,
`T154_SymptomType`, `T156_SymptomSection`, `T158_Symptom_CSC`, plus
`T147_BlockMetaDTC` for DTC block metadata. `T147` is an ECU-variant scope, not
a per-DTC code mapping; its observed columns are `fkT141_Block` and
`fkT100_EcuVariant`.

This is the layer that produces "more context". VIDA does not only show the code
and its sentence. It shows the customer symptom (component plus deviation), the
symptom status for this ECU variant, allowed freeze frames, fault counters, and
status identifiers, and it can tell whether a module returned fewer codes than it
should have.

## Status, counters, and recommendation

`Dtc` in `Workflow.DLL` computes the state locally from the readout:

```
CalculateDTCStatus      RecalculateDtcStatus    GetCalculatedProbability
CheckIfIsIntermittent   CheckIfAged             GetCorrectCounter
GetStatusIdentifier     GetStatusIdentifierValue
SetDtcOK                SetFrequencies          PrepareCounters
```

The fields behind it include `_countersCurrent`, `_countersPrevious`,
`_statusIdentifiersCurrent`, `_statusIdentifiersPrevious`, `_currentFrequency`,
`_previousFrequency`, `_firstTimeStored`, `_readoutTime`, `_mileageKilometres`,
`_isAged`, and `_showAlways`. A DTC is not a single string; it is a record with a
history across readouts.

`DtcManager` in `UI.DLL` turns that into words with resource keys:

```
lblShortConclusionActive        lblShortConclusionNotActive
lblFaultNotFound                lblShortConclusionUnknown
lblFaultCouldBeAged             lblTestConditionsNotFulfilled
lblDiagnoseRunFaultsFoundNow    lblDiagnoseRunNoFaultsFoundNow
lblFulfilOperationConditionsFaultCouldBeIntermittent
lblSB00 .. lblSB12              (status bits)
lblSI01 .. lblSI09              (status identifiers)
```

So "active", "intermittent", "aged", and "not found" are conclusions computed from
the status bytes and the stored counters, then translated through labels. The
words are not in the ECU response.

## Persistence

A readout survives the session. `DiagnosticSessionService` writes through named
procedures in `DiagSwdlSession`:

```
workshopSession_AddDtc                 @dtcReadOutId
workshopSession_AddDtcReadout          @readoutID
workshopSession_UpdateDtc              @dtcReadoutConclusion
workshopSession_AddFaultCounter        @counterName @counterValue @counterTextId
workshopSession_AddStatusIdentifier    @statusIdentifierName @statusIdentifierText
                                       @statusIdentifierValue
workshopSession_AddFreezeFrameParameter
workshopSession_GetFreezeFrames
workshopSession_RemoveCounters, _RemoveFreezeFrames, _RemoveStatusIdentifiers
```

The value object crossing this boundary is `DtcVO` in `ValueObjects.DLL`:
`Id`, `EcuType`, `Text`, `Value`, `RawValue`, `Status`, `SymptomId`,
`FreezeFrames`, `MecuGlobalTime`, `GlobalTimeWhenStored`, `MileageKilometres`,
`MileageMiles`, `PermanentFaultStatus`, `DetectedInVehicle`, `ShowAlways`,
`OkIfIntermittent`, `CalculateUsingCounters`, `IsActive`, plus the counter and
status-identifier collections.

This is also why a second read can say more than the first. `ReadDtc` compares
against prior readouts, and `CheckIfAged` needs `_firstTimeStored` from an earlier
one.

## The document click-through

The long-form context is not stored with the DTC. It is looked up by the info
element repository, which points at `DiagSwdlRepository`, not `CarCom`:

```
InfoElementsRepository.GetDtcIEs
InfoElementsRepository.GetDtcObservableSymptoms
InfoElementsRepository.GetSymptomsDescriptions
InfoElementsRepository.GetIE, GetReferenceIE
```

`DtcFaulttraceWF.GetDtcInformationUrl` then builds the link the JSP opens:

```
../InfoBrowse.do?id=<culture><IE id>&viewer=diagnostic&subtype=1&type=si&funcgrp=<n>
```

`Diagnostics.jsp` has the other half of this: `DiagMgr::NavigateIE` sends type
`FT` to a fault-trace page and everything else to `InfoBrowse.do`. The UI strings
name the surrounding tabs: `Fault trace`, `Initial State`, `Selected CSC`,
`Unselected CSC`, plus `FaultCounterForm`, `DtcTimelineControl`, and
`DtcExtendedViewDialog`.

## Verification status

`observed` means this pass saw it directly in the shipped file. `inferred` means
the evidence supports one reading and does not exclude another. `unverified`
means the available evidence does not settle it. `contradicted` means the
available evidence goes against the earlier wording.

| Statement | Status | Basis |
| --- | --- | --- |
| The DTC read is invoked through a script service | observed | call edges from `PerformDtcReadoutOnEcu` to `RunDtcScript` and `RunEcmDtcScript` |
| Block display text resolves through `vadis_GetText` for blocks that carry a text id | observed | call edges plus procedure names and parameter literals |
| `CarCom` holds the text and symptom tables | observed | `components.config.xml` plus SQL literals addressing `t190`, `t191`, `t141`, `t150` |
| DTC description sentences exist in `CarComRT_Data.MDF` | observed | 122 UTF-16 string lines of the `Diagnostic trouble code (DTC) ...` form |
| The DTC list procedure returns `T141_Block.name` as `Data` | observed | `vadis_GetAllPossibleDtcsOnEcu` selects `t141.name AS Data`; DTC blocks carry `fkt190_Text = 0` |
| The standard OBD description joins through `T150_BlockValue` to `T191_TextData` | observed | `0x034211` -> text id 1,004,326 |
| `DtcVO.Text` is set from `T141_Block.name` on the possible-DTC path | observed | `CarComRepository.GetAllPossibleDtcsOnD2Ecu` stores reader ordinal 2 |
| The UI reads `Workflow.Dtc.Text`, not `DtcVO.Text` | observed | `UI.DLL` table controls use `Dtc.Text`; no direct `DtcVO.Text` access |
| Status words are computed and translated, not read from the ECU | inferred | `Dtc` calculation methods plus `lblSB`/`lblSI` resource keys |
| Service ids `AE1B`, `AE11`, `AE02`, `AE18` map to specific Volvo services | inferred | they appear only in compiled test method names and format literals |
| `T147_BlockMetaDTC` carries the DTC-to-text mapping | contradicted | it carries `fkT141_Block` and `fkT100_EcuVariant`; no code, text or language column was observed |
| The script body drives the same block definitions end to end | unverified | the script ZIP and node shape are read; the block definitions were not matched |

## What this means for PyDiCE

Three things transfer, and one does not.

The transferable part is the shape of the data, not the tables. VIDA does not
use one description path: the list procedure returns `T141_Block.name` as
`Data`, the standard OBD description is a `T150_BlockValue` row pointing at a
localized `T191` text, and the long article sentence is a separate `T191` row.
Symptom records add the component and deviation. `volvo_p3` currently returns codes and, for generic
OBD, the standard descriptions. If this repository ever wants VIDA-style
descriptions, that is a data pipeline problem, and the missing artifact is a
code-to-description map, not more protocol work.

The second transferable part is that the counter and status history is what turns
a list of codes into a diagnosis. `Dtc` needs a previous readout to say
"intermittent" or "aged". A single native read cannot reproduce those words, and
should not pretend to. `docs/native-dice.md` already warns that records carrying
only `0x40` or `0x50` are monitor tests not completed.

The third is the separation of concerns. `CarCom` answers "what does this code
mean on this ECU variant", `DiagSwdlSession` answers "what did we see last time",
and `DiagSwdlRepository` answers "what should the technician do". They are
different databases with different lifecycles. The comparable split for this
repository would be a description table, a local readout history, and a document
source, not one denormalized JSON.

The part that does not transfer is the text-id indirection itself. It exists in
VIDA because the same content is localized into roughly twenty languages and
shared across vehicle platforms. A tool with one or two languages and one
platform can key descriptions by code directly.

## Open questions

1. **What is the row-level join?** `T147_BlockMetaDTC` is not the text join; it
   carries `fkT141_Block` and `fkT100_EcuVariant`, and no third column is visible
   to any shipped query. The DTC value text joins through
   `T150_BlockValue.fkt190_text_value`. Remaining: the exact T150 block-id
   offset and the article phrase URN that reaches text id 1,034,063.
2. **Which row is the short name?** The DTC list procedure returns
   `T141_Block.name` as `Data`; `DtcVO.Text` is set from it on the
   possible-DTC path. The UI reads `Workflow.Dtc.Text`, not `DtcVO.Text`; on the
   GGD path that text is the `T150_BlockValue` value text. The D2 script's
   `Dtc.Text` source remains open.
3. **How is the code string built?** `T150_BlockValue.CompareValue` is text
   (`0x034211`), and the same row carries `P034211`. Whether VIDA formats the
   letter prefix in IL or reads it from a text row was not determined.
4. **What do `AE1B`, `AE11`, `AE02`, and `AE18` mean?** They appear only in test
   method names and format literals. They look like service identifiers, and the
   test payloads are three-byte DTC records, but the mapping is inferred.
5. **What is inside the script?** `ScriptContent.XmlDataCompressed` is ZIP plus
   deflate; concrete `EcuAddress`, `EcuType` and `ServiceId` values are in
   `docs/vida-second-pass.md`. Coverage across the script set remains open.
6. **Do the status bits match ISO 14229?** `lblSB00` through `lblSB12` suggest a
   bit naming scheme. The bit assignment was not read.

## Reproducing

```sh
VIDA=/path/to/image/VIDA
D="$VIDA/jboss/standalone/deployments/VidaEar.ear/VidaWeb.war/Diagnostics"

sha256sum "$D/Workflow.DLL" "$D/BizServices.CarCom.VehicleServices.DLL" \
  "$D/BizServices.CarCom.DB.DLL" "$D/BizServices.DiagnosticSession.DLL"

# the text and symptom procedure names
strings -a -el "$D/BizServices.CarCom.DB.DLL" \
  | grep -E "vadis_GetText|vadis_GetDtcSymptoms|vadis_GetCustomerSymptom"

# the display text path
strings -a -el "$D/BizServices.CarCom.VehicleServices.DLL" \
  | grep -E "vadis_GetText|service__GetTexts|service__GetSymptoms|t191_textdata"

# the description rows
strings -a -el "$VIDA/db/CarComRT_Data.MDF" \
  | grep -c "Diagnostic trouble code (DTC)"
```

Managed assembly inspection:

```
inspect_managed_members(path="$D/Workflow.DLL")
inspect_managed_members(path="$D/BizServices.CarCom.VehicleServices.DLL")
inspect_managed_members(path="$D/BizServices.CarCom.DB.DLL")
inspect_managed_members(path="$D/BizServices.DiagnosticSession.DLL")
```

Both operations are static. They do not load the assembly or execute target code.

## Caveats

The inspection is static and the client was not run. No adapter was attached, so
the read path is established from call edges and configuration, not from traffic.

The databases were read as bytes. `strings` finds text, not rows. Table names and
column names next to a string are not proof that the string belongs to that
column, which is why the row-level join stays open.

The text extraction is lossy in both directions. It can miss rows stored in a
compressed or non-UTF-16 form, and it cannot show which language a row belongs to.
Multilingual fragments in the same dump suggest rows from several locales share
the file, but the mapping was not verified.

The tree is a 2022 snapshot of VIDA 2014D. A different release may key the same
content differently.
