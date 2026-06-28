# PYOBD

PYOBD started as a generic OBD2 tool. The current work has shifted to Volvo
P3 diagnostics with a DiCE adapter and Python, without VIDA or Windows
dependencies.

The near-term target is a practical Volvo service tool for a 2013 XC60:
connect, read fault codes, clear fault codes, and eventually speak the DiCE
native USB protocol directly from Python.

## Current Context

The reverse-engineering work has established a few facts about the DiCE stack:

* The adapter is `VID_17AA / PID_D1CE`.
* Windows DiCE software uses a custom USB driver and a J2534 DLL.
* We can identify the DiCE device on macOS, but the native DiCE transport is
  still under implementation.
* The command protocol has been partially recovered:
  * outbound frames begin with `0x1D`
  * the second byte is the DiCE command ID
  * reply status bytes map to RFC-style error messages

The reverse-engineering notes live in
[docs/dice-reversing.md](docs/dice-reversing.md).

## Scope From VIDA Help

`VIDA-Help-en-GB.pdf` confirms the diagnostic workflow we should mirror and
the parts we can leave out.

Keep:

* connection setup and adapter detection
* vehicle profile and VIN-oriented identification
* diagnostics overview, DTC readout, DTC detail, fault trace, network status
* vehicle communication views for parameters, activations, and programmed values
* clear connection state and live diagnostic feedback in the UI

Leave out:

* work orders
* software ordering and download flows
* firmware updates and reprogramming
* PIE, DMS, and Volvo back-office communication
* report generation and other dealer-only service workflows

## What Works Today

The repository already includes these pieces:

* Volvo P3 CAN request helpers for reading and clearing DTCs
* `python-can` transport wrapper for supported CAN backends
* DiCE USB detection on macOS through `system_profiler`
* DiCE USB probing through PyUSB
* A recovered DiCE command table for `RawCan*`, `Iso15765*`, and system
  commands
* DiCE installer extraction helpers for `SetupDiCE.exe`
* A basic curses TUI for connection setup, vehicle profile, diagnostics, fault
  trace, and DTC operations

## Commands

List the built-in ECU address hints:

```sh
python3 -m volvo_p3.cli list-modules
```

Show the recovered DiCE command IDs:

```sh
python3 -m volvo_p3.cli list-dice-commands
python3 -m volvo_p3.cli list-dice-commands --category raw_can
python3 -m volvo_p3.cli list-dice-commands --category iso15765
```

Probe whether macOS can see the DiCE adapter:

```sh
python3 -m volvo_p3.cli probe-dice
python3 -m volvo_p3.cli probe-dice-raw
```

Extract the DiCE payloads from the Windows installer:

```sh
python3 -m volvo_p3.cli extract-dice-installer /Users/wjk/Downloads/SetupDiCE.exe
```

Read and clear Volvo DTCs on a supported CAN interface:

```sh
python3 -m volvo_p3.cli read-dtc --interface socketcan --channel can0 --ecu ECM
python3 -m volvo_p3.cli clear-dtc --interface socketcan --channel can0 --ecu ECM --yes
```

Launch the interactive terminal UI:

```sh
python3 -m volvo_p3.cli tui
```

`socketcan` is Linux-only. On macOS, use the DiCE probing commands above or a
different CAN backend.

## Current Protocol Scope

Implemented Volvo services:

* `AE` read DTC
* `AF` clear DTC

Requests use extended CAN ID `0x000FFFFE`.

The DTC payload is currently reported as raw bytes. Code decoding should be
added only after captures from the target vehicle are validated.

## DiCE Reverse-Engineering Notes

The current DiCE work has identified:

* the Windows device interface GUID used by the DiCE user-mode DLL
* the read/write endpoint open path
* the `CommandThread()` worker flow
* the write-side and reply-side wrappers
* the RFC status decoder
* the first visible frame layout for DiCE commands

Recovered command IDs currently captured in code include:

* `0`: `RfcVersion`
* `2`: `SysHardwareVersion`
* `3`: `SysFirmwareVersion`
* `10`: `RawCanInit`
* `12`: `RawCanStart`
* `13`: `RawCanStop`
* `14`: `RawCanSet`
* `15`: `RawCanGet`
* `18`: `RawCanTxClear`
* `19`: `RawCanRxClear`
* `20`: `RawCanAddFilter`
* `21`: `RawCanRemoveFilter`
* `40`: `Iso15765Init`
* `42`: `Iso15765Start`
* `43`: `Iso15765Stop`
* `44`: `Iso15765Set`
* `45`: `Iso15765Get`
* `50`: `Iso15765AddFilter`
* `51`: `Iso15765RemoveFilter`
* `55`: `WarrantyClockGet`
* `62`: `BitSupplyVoltage`
* `68`: `BitLed`

## To-Dos

* Extend the TUI's diagnostics views so network state and fault-trace output
  are closer to VIDA's presentation.
* Add clearer vehicle identification and module mapping in the TUI once real
  captures from the XC60 are available.
* Recover the payload layout for `RawCanInit`, `RawCanStart`, `RawCanSet`,
  and the corresponding ISO-TP commands.
* Implement a native Python DiCE transport that can open the adapter directly
  on macOS.
* Add a small DiCE smoke test that opens the device and round-trips one known
  command.
* Add a TUI lane for DiCE-native transport once the command payloads are
  recovered.
* Map real Volvo response frames to human-readable DTC and module labels.
* Add safer discovery logic for the 2013 XC60 module set.
* Expand the TUI into a lightweight command runner for cataloging and clearing
  fault codes.

## Project Notes

The older ELM327-focused documentation is still in the repository history, but
the current implementation direction is Volvo P3 + DiCE + Python.
