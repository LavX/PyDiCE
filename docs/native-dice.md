# Native DiCE readout

The read-only native DiCE path has been verified on Linux with USB adapter `17aa:d1ce`. It reads generic OBD DTCs and configured modules over default-session UDS. It does not clear DTCs, change diagnostic sessions, or request security access.

## Commands

```sh
python3 -m volvo_p3.cli dice-info
python3 -m volvo_p3.cli dice-read-dtc --timeout 1.5
python3 -m volvo_p3.cli dice-hybrid-dtc --timeout 1.5
python3 -m volvo_p3.cli dice-hybrid-dtc --module BECM --timeout 1.5
```

The USB device needs read/write permission. On hosts where only root has access, run with `sudo`. PyUSB and libusb are required.

## Verified transport and read results

The adapter initializes a 500 kbit/s standard-ID CAN channel, starts it, and installs a receive filter. Native `RawCAN` command `0x80` transmits CAN frames. Command `0x81` delivers asynchronous events, including transmit confirmations and received frames. The implementation matches confirmations by sequence number and retains received events that arrive before their confirmation. Channel cleanup removes the filter, stops the channel, and closes it.

The generic reader sends OBD modes `03`, `07`, and `0A`. The module reader sends UDS `19 02 FF` to each configured physical request ID and uses the paired response ID. Mask `0xFF` requests records for any available DTC status. The response's status-availability mask varies by ECU and must be applied when interpreting each record. Returned records with only status bits `0x40` or `0x50` indicate monitor tests not completed; their presence alone does not establish a fault.

For custom target sets, `volvo_p3.module_scan` provides `ModuleTarget`, `load_module_profile(path)`, `default_module_targets()`, `build_discovery_targets(start=0x700, end=0x7F7, response_offset=8)`, and `scan_modules(bus, targets=None, *, timeout=1.5, progress=None)`. The discovery builder skips functional request ID `0x7DF` and IDs whose paired response would exceed `0x7FF`. It uses known P3 D6 names where available and labels other targets `CAN_XXX`.

A JSON profile has a top-level `modules` array with explicit physical IDs:

```json
{
  "modules": [
    {"name": "MODULE_A", "request_id": "0x700", "response_id": "0x708"}
  ]
}
```

Request IDs must be within `0x700..0x7F7`; response IDs can use the full 11-bit range. IDs can be JSON integers or `0x`-prefixed strings. The loader rejects functional request ID `0x7DF`, non-standard IDs, and duplicate request or response IDs. The scanner runs one read-only request per target, preserves per-module no-response and negative-response statuses, stops on transport failures, calls the optional progress callback after each target, and summarizes effective fault-record counts.

## Adapter query and binary notes

`dice-info` reads firmware information and supply voltage. The captured firmware build is `Aug 22 2011 09:03:43`. Voltage units are not independently confirmed.

The native USB framing and command layouts were recovered from `artifacts/setupdice_extracted/Dice_x64_cab/DiCE32_x64.dll`. The older `docs/dice-reversing.md` contains incorrect framing and command-number assumptions; its 1D prefix is not the outer marker. The live raw CAN path uses commands `0x80` and `0x81` as described above.

The full recovered command surface is in `docs/dice-compatibility.md`, and `python3 -m volvo_p3.cli list-dice-commands` prints it from `volvo_p3/dice_commands.py`. Each row records whether its ID was named from RTTI, from strings, from a `movb` store site, or verified live, so the evidence for any command can be checked without reopening the binary.

## Discovery and repeated reads

`dice-scan --discover --timeout 0.5 --save-profile reports/my-car.json` saves all responding targets, including incomplete responses. Read that profile with `dice-scan --profile reports/my-car.json --timeout 1.5`. Omit `--all-records` for a concise fault/history list. To deliberately refresh an existing profile, add `--replace-profile` to the discovery command.

The initial response timeout is independent of ISO-TP assembly: after a first frame, each consecutive frame has a one-second window, bounded by ten seconds total transfer time. An incomplete response remains distinct from an empty DTC list.

## Vehicle profile

`volvo_p3/tui.py` has a "Create Profile" action with `vin`, `year`, and `model` fields, but only the VIN is stored. `year` and `model` are echoed into one confirmation line and discarded, and `VehicleState` has no field to hold them. Nothing in the read path consults them, so two different model years produce identical bus traffic.

VIDA 2014D models the same concept as a row in `dbo.VehicleProfile`: a partner group plus up to seven component foreign keys (model, year, body style, engine, transmission, steering, special vehicle), an optional chassis range, and three database-only columns the picker never shows. A selection resolves to a *set* of profile IDs, where a null component on a candidate profile acts as a wildcard, so a partial selection still matches. A generic profile at `FolderLevel = 0` is always included.

Two points matter for this transport. First, on the Java side those profile IDs filter service information, parts, and symptom data only; the Java stack never turns them into an ECU list. Second, each profile row carries `fkNodeECU`, which is loaded into `VehicleProfileVO` but has no caller in `VidaEjb`, `VidaData`, or the 279 `VidaWeb` classes. That link is the best remaining lead for a per-profile module list, and it is unresolved.

Until that question closes, `module_scan` target selection stays explicit: `P3_D6_MODULES`, a discovery sweep, or a JSON profile. The transferable idea is the shape, not the tables: nullable components with wildcard matching and an always-valid generic profile, resolving to a set of module profiles that get scanned as a union.

The diagnostic client turns out to select a compressed XML script by that same profile set, not a list of module IDs, and it carries its own platform switch (`VehType`, `p2x` in the shipped config). So a vehicle-to-module mapping is something this repository would be introducing, not copying from VIDA. The client is written up in `docs/vida-diagnostics-client.md`.

The full recovery, including the cascade SQL and the stored procedures, is in `docs/vida-vehicle-profile.md`.

Separately, VIDA keys request timeout and resend policy to an ECU variant plus the first one to three request bytes, not to the vehicle profile. `volvo_p3` currently applies one fixed `--timeout` and never resends, so that is the nearer-term gap: per-target timeout and resend policy on `ModuleTarget`, seeded from observed behaviour rather than copied from VIDA's table.
