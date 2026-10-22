# VIDA service request templates (T111_Service)

This note records how VIDA turns a script's numeric `ServiceId` into request
bytes, and what `volvo_p3/vida_service_templates.py` does with that. It
follows `docs/vida-diagnostics-client.md`, `docs/vida-second-pass.md` and
`docs/diag-address-map.md`, which left this mapping open.

Static analysis only. Nothing in the VIDA image was executed, attached to
SQL Server, or modified.

## The mechanism

`BizServices.CarCom.VehicleServices.DLL` has a `VehicleService` class whose
constructor calls `IVehicleServiceDatabase.GetService`, backed by this
stored procedure (read directly from `CarComRT_Data.MDF`, page 267):

```sql
CREATE PROCEDURE [dbo].[service_GetService]
@service int
AS
select service, mode, [servicename] as [name], [description], definition
from t111_service where id = @service
```

So a script's `ServiceId` is literally `T111_Service.id`. The row's
`definition` column is an XML template:

```xml
<request><item name="Service" length="8" /><item name="Identifier" length="8" /></request>
<response><block id="ID2_1" type="BLOFF" length="8" name="Bloff" />
<dynamiclist type="PARAM" name="Parameters" blockName="Parameter" parent="ID2_1" /></response>
```

`VehicleService` stores this in a `_serviceXml` field and hands it to a
`RequestHandler`, which builds the actual bytes with a `BinaryBuffer`
(fields `_buf: byte[]`, `_position: int`; methods `AddBits`, `GetBits`,
`AddHex`, `ToArray`, `ToHex`). `AddBits` takes a bit length, not a byte
length. One recovered template packs two 4-bit sub-fields (`Mode` and
`IdentifierType`) into a single byte, so items are not always byte-aligned.
This means the template is interpreted generically: every named `<item>` is
packed into the output buffer at the bit length it declares, in document
order. The `Service` item is filled from the row's own `service` column
(the actual UDS/CarCom service byte), never from a script value.

No managed decompiler was available in this pass (confirmed again here), so
`AddBits`/`GetBits`'s exact bit order (MSB-first, byte order of multi-byte
fields) is an assumption from the method names and the `BinaryBuffer.GetBits`
error-message format string (`"pos:{0} len:{1} value:{2}"`), not a
decompiled fact. Validate it against a live VIDA session or a hardware
capture before trusting an encoded request against real hardware.

## What is recovered, and what is not

`T111_Service` has **zero recoverable data rows** in this image, confirmed
twice, independently, with a full page census. See the PyDiCE handoff dated
2026-10-22. What survives is its `definition` column's LOB (text/image)
pages: 189 `<request>...</request><response>...</response>` text fragments,
carved from pages 162216-162226 of `CarComRT_Data.MDF` in this image. SQL
Server does not store a back-pointer from a LOB page to its owning row id,
so **the numeric id that goes with each template cannot be recovered from
this image's data alone**.

`volvo_p3/vida_service_templates.py`'s own carve typically recovers 150-ish
of those 189 as well-formed XML. The rest fail to parse, the same
single-character corruption the rest of this image shows. The gap is
counted, not hidden, by `extract_service_templates`: a template that fails
to parse is skipped and does not consume an index slot.

### The ordering hypothesis, and how far it holds up

`INFERRED_SERVICE_ROLES` in the module records a working hypothesis. If the
189 templates are stored in ascending `id` order, plausible for a small,
rarely-updated lookup table populated once at database creation in 2006,
then a template's position in the carved list equals its `T111_Service.id`.

Cross-checking that hypothesis against the recovered diagnostic-script
corpus (6,566 scripts, `data/diag_dump.tar.xz`), specifically which extra
VehComm properties each corpus `ServiceId` actually binds, compared with the
extra item names the same-numbered template declares, gives **19 exact-set
matches out of 63 ids the corpus exercises**. That is well above chance for
a vocabulary of roughly 15 distinct property names, but far short of
confirmation. Several low ids (4, 8, 10, 11, 18) mismatch outright. Some
plausible causes that do not require abandoning the hypothesis: template
boundary corruption (a template skipped during parsing shifts every later
index by one), the LOB chain not being perfectly insertion-ordered after
seven years of VIDA updates, or a corpus `ServiceId`'s extra property
binding to a different template item than the comparison assumes. `Mode`
was excluded from the comparison on both sides as ambiguous, which hides
any mismatch that is purely a `Mode` disagreement.

Treat `INFERRED_SERVICE_ROLES` as exactly what its name says: a candidate
mapping to validate, not a mapping to execute requests against.

## What this unblocks for PyDiCE

- `volvo_p3/vida_service_templates.py` ports the generic template
  interpreter: `TemplateItem`/`ServiceTemplate` (shape), `BitWriter`/
  `BitReader` (the bit-packing itself), and `build_request` (fills a
  template's items from a value mapping). This is useful machinery
  independent of the open id question above. Given any confirmed
  `(ServiceId, template)` pair, from a live capture or from resolving the
  ordering hypothesis, `build_request` already knows how to turn it into
  bytes.
- `extract_service_templates` carves the templates straight from a
  `CarComRT_Data.MDF` image (read only), so a different VIDA release can be
  re-carved without re-deriving the mechanism.
- `python3 -m volvo_p3.cli vida-service-templates --image <CarComRT_Data.MDF>`
  lists every recovered template with its `INFERRED_SERVICE_ROLES` guess
  where one exists. `--index N` prints one template's raw XML. `--out`
  writes the full set as JSON.

## A conflict with the existing shipped read path, found and fixed

`volvo_p3/script_plan.py` (shipped, already used by `diag-run-script
--execute`) reads a `VehComm` node's `ServiceId` property through
`_parse_service_id`, which parses it **as hex** and rejects anything
outside 0-255 (`READ_ONLY_SERVICES = {0x01, 0x09, 0x19, 0x22}`). That
module's own docstring states the convention explicitly: "Service ids
inside a script are written as hexadecimal bytes without a prefix."

Today's finding (`ServiceId` is `T111_Service.id`, resolved through
`service_GetService`) is a **decimal** catalog key with no byte-range
limit: the corpus's most common `ServiceId` values include 106, 112, 101,
100, 114, 119 (all exceed 0xFF and would be rejected by
`_parse_service_id`), and `ServiceId=2` (377 occurrences, title "Read ECU
identification" in every case checked) only matches read service `0x02`
if parsed as a plain decimal 2, not as anything in `{0x01, 0x09, 0x19,
0x22}`.

Checked directly against the committed corpus: **zero** of the 4,169
`VehComm` nodes in `data/diag_dump.tar.xz` carry a literal `Request` field
value. This means `_safe_request_values`/`_request_bytes` in
`script_plan.py` and `script_runner.py` always fall through to "script
supplied no request bytes" for every script in this corpus, so
`diag-run-script --execute`'s real coverage against the shipped data is
currently zero scripts, independent of the hex-vs-decimal question. The
hex interpretation was evidently drawn from a narrow sample (two scripts
cited by name in `docs/vida-second-pass.md`, both with small values where
hex and decimal happen to coincide or nearly coincide), not from the
corpus as a whole.

This had not caused an actual wrong byte to be sent (nothing had ever been
sent through that path against this corpus), but it was a real conflict
between two passes' conclusions about the same field.

**Fixed.** `script_plan.py`'s `_parse_catalog_service_id` now parses the
`ServiceId` property as decimal with no byte-range cap. The module's own
raw-byte classification (`READ_ONLY_SERVICES`, `NON_READ_SERVICES`,
`classify_service`, `is_read_only_service`) is unchanged and kept for the
literal-request-byte case; a new, separate catalog-key classification
(`vida_service_templates.READ_ONLY_CATALOG_SERVICES`,
`classify_catalog_service`, `is_read_only_catalog_service`) now gates the
`ServiceId` property case in `read_only_requests()`, `script_runner.py`'s
planning and sending paths, and the `cli.py`/`tui.py` display code. Every
call site that had been applying the raw-byte functions to the catalog
key, including a hidden one in `run_plan`'s actual send loop, one in its
defense-in-depth re-check, and one in the UDS response matcher, is now
corrected; the project's test fixtures were updated accordingly (see git
history on this file's date for the exact set). Full suite: 323 passed.

## What is still open

1. **Confirm or refute the ordering hypothesis** against a live VIDA session
   or a captured CAN trace: send a script with a known `ServiceId`, capture
   the actual request bytes, and check whether they match
   `build_request(templates[that ServiceId], ...)`.
2. ~~The `RequestDataVO` constructor overload~~ **Resolved.** VehCom2.DLL
   has two `RequestDataVO` constructors: `(byte[] bytes)`, which sets
   `Service`/`SubFunction` to `null`, and `(byte[] bytes, byte? service,
   byte? subFunction)`, which sets all three explicitly. Both
   `SecurityAccessHandlerBase.Unlock` and `.sendKey` call the three-argument
   overload (confirmed by call-edge evidence, tokens `0x0600011e` and
   `0x06000122` both target ctor `0x0600001e`), passing the already-built
   wire bytes (header plus payload) alongside the same service/sub-function
   values as metadata. `Service`/`SubFunction` are not re-derived from
   `Bytes`; the caller sets both explicitly and consistently. This also
   confirms the `(Service, SubFunction)` pair used by
   `ServerTimingParameter.GetFromDbTable`'s timing lookup (report 01) is
   sourced the same way: explicit metadata alongside the raw bytes, not
   parsed back out of them.
3. **Confirm the bit order** (`AddBits`/`GetBits`'s MSB-first assumption)
   against a real capture. `BitWriter`/`BitReader` are written so that
   swapping the bit order, if it turns out to be wrong, is a localized
   change.
4. The response side (`block`, `dynamiclist`, `fixedlist` elements) is
   parsed into `ServiceTemplate.response_items` but has no decoder yet.
   `build_request` only handles the request side.

## Provenance

`T111_Service`'s object id (`149_575_571`), the stored-procedure text, the
`VehicleService`/`RequestHandler`/`BinaryBuffer` call chain, and the
189-template carve were all read directly from the image in this pass
(`/mnt/vida`, read only). The cross-reference statistic in this note was
computed against the already-committed script corpus
(`data/diag_dump.tar.xz`), not against anything outside this repository.
