# Volvo P3 CAN Diagnostics

This is a small, transport-agnostic starting point for VIDA-like Volvo P3
diagnostics. It intentionally starts with raw CAN visibility and DTC management
instead of full VIDA feature parity.

The implementation uses `python-can` for CAN I/O. It does not talk to an
ELM327 adapter and it does not depend on VIDA.

## Commands

List built-in ECU address hints:

```sh
python3 -m volvo_p3.cli list-modules
```

Passively print CAN frames:

```sh
python3 -m volvo_p3.cli readout --interface socketcan --channel can0 --duration 10
```

`socketcan` only works on Linux. On macOS, use `probe-dice` / `probe-dice-raw`
to test the DiCE adapter itself, or a macOS-compatible CAN backend if you have
one installed.

Probe ECU diagnostic addresses with Volvo `AE` read-DTC requests:

```sh
python3 -m volvo_p3.cli discover --interface socketcan --channel can0 --start 0x00 --end 0xFF
```

Read DTC bytes from a known ECU:

```sh
python3 -m volvo_p3.cli read-dtc --interface socketcan --channel can0 --ecu ECM
python3 -m volvo_p3.cli read-dtc --interface socketcan --channel can0 --ecu 0x7A
```

Clear DTCs for one selected ECU:

```sh
python3 -m volvo_p3.cli clear-dtc --interface socketcan --channel can0 --ecu 0x7A --yes
```

## Current Protocol Scope

Requests use extended CAN ID `0x000FFFFE`.

Implemented Volvo services:

* `AE` read DTC
* `AF` clear DTC

The DTC payload is currently reported as raw bytes. Decode rules should be added
from real captures from the target vehicle before trusting decoded code labels.
