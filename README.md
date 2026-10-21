# PYOBD

PYOBD started as a generic OBD2 tool. The current work has shifted to Volvo
P3 diagnostics with a DiCE adapter and Python, without VIDA or Windows
dependencies.

The native DiCE readout work is being exercised on a 2014 Volvo V60 D6 plug-in
hybrid. The repository also retains generic OBD and legacy Volvo P3 workflows.

## Native DiCE on Linux

The native path uses direct USB RPC framing, firmware and supply-voltage
queries, and a 500 kbit/s standard-ID CAN channel. Live reads verified the
DiCE `0x80` RawCAN transmit and `0x81` event receive path, generic OBD DTC
services, and ten candidate hybrid module addresses over default-session UDS.

```sh
python3 -m volvo_p3.cli dice-info
python3 -m volvo_p3.cli dice-read-dtc --timeout 1.5
python3 -m volvo_p3.cli dice-hybrid-dtc --timeout 1.5
python3 -m volvo_p3.cli dice-hybrid-dtc --module BECM --timeout 1.5
```

The USB device needs read/write access. On a Linux host where only root has
access, run these commands with `sudo`. PyUSB and libusb are required.

`dice-read-dtc` reads generic OBD modes 03, 07, and 0A. `dice-hybrid-dtc` reads
candidate P3 D6 modules with UDS `19 02 FF`. Both produce JSON with raw
USB/CAN evidence. These are read-only commands; neither clears codes or
changes diagnostic sessions. The successful captures and status interpretation
are documented in [native-dice.md](docs/native-dice.md).

### Scan all responding UDS addresses

```sh
# Scan the diagnostic address range and save responders for later reads.
sudo python3 -m volvo_p3.cli dice-scan --discover --timeout 0.5 --save-profile reports/my-car.json
# Read the discovered modules again, including their full DTC status records.
sudo python3 -m volvo_p3.cli dice-scan --profile reports/my-car.json --all-records
# Interactive menu: choose Native DiCE.
sudo python3 -m volvo_p3.cli tui
```

Every scan saves a timestamped JSON report under `reports/`, including partial
results on interruption. Normal terminal output shows records with supported
fault or history flags; `--all-records` also shows uncompleted monitors.
Custom JSON profiles accept explicit request/response CAN IDs.
Discovery defaults to `0x700..0x7F7`, skips functional `0x7DF`, and assumes
response ID = request ID + 8. This covers the current 500 kbit/s, 11-bit
channel. Other buses, 29-bit addressing, legacy D2, and modules requiring
other diagnostic sessions are not automatically scanned. A timeout is not
proof that a module is absent or fault-free.

The earlier reverse-engineering notes below are historical.

Background material lives under `docs/`: the native path in
[native-dice.md](docs/native-dice.md), the recovered adapter command space in
[dice-compatibility.md](docs/dice-compatibility.md), and the VIDA reference notes
in [vida-vehicle-profile.md](docs/vida-vehicle-profile.md),
[vida-diagnostics-client.md](docs/vida-diagnostics-client.md),
[vida-dtc-readout.md](docs/vida-dtc-readout.md), and
[vida-image-boundary.md](docs/vida-image-boundary.md).

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
* A verified native DiCE CAN read path using RawCAN commands `0x80` and `0x81`
* Generic OBD and default-session UDS DTC readers with raw evidence output
* A recovered DiCE command table for `RawCan*`, `Iso15765*`, and system commands
* DiCE installer extraction helpers for `SetupDiCE.exe`
* A basic curses TUI for connection setup, vehicle profile, diagnostics, fault
  trace, and DTC operations

## Commands

Open the setup launcher that presents the legacy pyOBD and ADIV/Volvo tools:

```sh
python3 launcher.py
```

If the environment is installed, the launcher is also available as:

```sh
pyobd-launcher
```

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
python3 tui.py
```

`socketcan` is Linux-only. On macOS, use the DiCE probing commands above or a
different CAN backend.

## Current Protocol Scope

Implemented Volvo services:

* `AE` read DTC
* `AF` clear DTC

Requests use extended CAN ID `0x000FFFFE`.

The legacy `AE` read path still reports raw Volvo DTC payload bytes. Native
generic OBD and UDS reports decode SAE-style DTC bytes and preserve the raw
bytes and status flags.

## DiCE Reverse-Engineering Notes

The current DiCE work has identified:

* the Windows device interface GUID used by the DiCE user-mode DLL
* the read/write endpoint open path
* the `CommandThread()` worker flow
* the write-side and reply-side wrappers
* the RFC status decoder
* the first visible frame layout for DiCE commands

Use `python3 -m volvo_p3.cli list-dice-commands` for the corrected command IDs.
The old decimal rendering of MSVC hexadecimal arguments and the presumed 1D
wire prefix were incorrect. The native outer framing uses B5.

## To-Dos

* Extend the TUI's diagnostics views so network state and fault-trace output
  are closer to VIDA's presentation.
* Add clearer vehicle identification and module mapping in the TUI once real
  captures from the XC60 are available.
* Map real Volvo response frames to human-readable DTC and module labels.
* Verify candidate module identities against vehicle-specific diagnostic data.
* Expand the TUI into a lightweight command runner for cataloging and clearing
  fault codes.

## Project Notes

The older ELM327-focused documentation is still in the repository history, but
the current implementation direction is Volvo P3 + DiCE + Python.
