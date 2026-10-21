# VIDA DTC readout and fault context

The other VIDA notes stop where a diagnostic script returns. This note follows the
record after that point: how a code read from the car acquires a description, what
else is attached to it, and which database each part comes from.

It answers the question a reader of `docs/vida-diagnostics-client.md` is left with.
That note establishes that the read plan is a compressed script keyed by profile.
It does not say where the sentence next to the code comes from. The answer is that
it is a localized text row in `CarCom`, selected by an integer text id, not
something interpreted from the fault.

Nothing here was executed. The assemblies were read statically: PE/CLI metadata,
member signatures, call edges, and user strings. The databases were searched as
bytes, not attached to SQL Server.

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
`BlockValue.get_ValueText`. So the text a technician sees beside a DTC block is
whatever row the text id points at, in the active language.

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
`T147_BlockMetaDTC` for DTC block metadata. `T147` is the table that would carry
the per-DTC code mapping; its columns were not read.

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
means the available evidence does not settle it.

| Statement | Status | Basis |
| --- | --- | --- |
| The DTC read is invoked through a script service | observed | call edges from `PerformDtcReadoutOnEcu` to `RunDtcScript` and `RunEcmDtcScript` |
| Block display text resolves through `vadis_GetText` by text id and language | observed | call edges plus procedure names and parameter literals |
| `CarCom` holds the text and symptom tables | observed | `components.config.xml` plus SQL literals addressing `t190`, `t191`, `t141`, `t150` |
| DTC description sentences exist in `CarComRT_Data.MDF` | observed | 122 UTF-16 string lines of the `Diagnostic trouble code (DTC) ...` form |
| The fault list shows the text row, not a computed description | inferred | the block text path is the only display path found |
| Status words are computed and translated, not read from the ECU | inferred | `Dtc` calculation methods plus `lblSB`/`lblSI` resource keys |
| Service ids `AE1B`, `AE11`, `AE02`, `AE18` map to specific Volvo services | inferred | they appear only in compiled test method names and format literals |
| A specific DTC code joins to a specific `T191` row | unverified | the MDF was not attached; the join goes through `T147_BlockMetaDTC`, whose columns were not read |
| `T147_BlockMetaDTC` carries the DTC-to-text mapping | unverified | the table name and its presence in DTC metadata are observed; the columns are not |
| The script body drives the same block definitions end to end | unverified | `ScriptContent.XmlDataCompressed` was not decompressed |

## What this means for PyDiCE

Three things transfer, and one does not.

The transferable part is the shape of the data, not the tables. A diagnostic
description in VIDA is an indirection: code, then text id, then a localized row,
then symptom records that name a component and a deviation. `volvo_p3` currently
returns codes and, for generic OBD, the standard descriptions. If this repository
ever wants VIDA-style descriptions, that is a data pipeline problem, and the
missing artifact is a code-to-description map, not more protocol work.

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

1. **What is the row-level join?** `T147_BlockMetaDTC` is the missing table.
   Attaching `CarComRT_Data.MDF` to SQL Server and dumping `T147`, `T141`, and
   `T190` would settle whether the DTC code or its description is the block text.
2. **Which row is the short name?** The fault list and the fault-tracing article
   may use two different text ids for the same code. Only an attached database
   distinguishes them.
3. **How is the code string built?** A three-byte DTC record plus format bits is
   the usual encoding. Whether VIDA formats it in IL or reads it from a text row
   was not determined.
4. **What do `AE1B`, `AE11`, `AE02`, and `AE18` mean?** They appear only in test
   method names and format literals. They look like service identifiers, and the
   test payloads are three-byte DTC records, but the mapping is inferred.
5. **What is inside the script?** `ScriptContent.XmlDataCompressed` was not
   decompressed. That is the same gap as in `docs/vida-diagnostics-client.md`.
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
