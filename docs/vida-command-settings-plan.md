# Command and settings inventory (CommandPlan)

`volvo_p3/command_plan.py` turns one recovered diagnostic script into a
typed inventory of what it can change on the car: actuator activations
(`Components.Activation`, `Components.Gui.ActivationList`) and
write-shaped `Components.VehComm` nodes (`ControlOption`/`ControlMode`, or
an `Identifier`/`Value` pair). This follows `docs/vida-service-templates.md`
and the activation/settings models report 04 and report 05 proposed in the
2026-10-22 handoff.

**It only builds a plan. It never sends a request.** There is no execute
path in this module, unlike `volvo_p3.script_runner` for read-only requests.
Two things stand between this inventory and anything safe to send to real
hardware:

1. The `ServiceId -> T111_Service.id` mapping is still a working hypothesis
   (`docs/vida-service-templates.md`), so the exact request bytes for most
   of these writes are not known.
2. VIDA's real execution order, security preconditions and stop/relock
   steps live in the script's event-handler graph (`OnStart`,
   `OnPositiveResponse`, button clicks), which this module does not
   reconstruct. A `CommandPlan` is a flat inventory of what exists in a
   script, not a safe replay order.

## Usage

```sh
python3 -m volvo_p3.cli diag-command-plan --script path/to/script.xml
python3 -m volvo_p3.cli diag-command-plan --script path/to/script.zip --json
```

Example, against the real recovered corpus (`VCC-225701`, the pump-motor
activation report 04 walked by hand):

```
Activations (1):
  Activation1  [activation]
    ecu=593101 type=REOFF value=0B write=35 stop=18 parameters=Pumpmotor
```

And a settings-style write (`VCC-239300`, "Read/program low level fuel
warning"):

```
Write-shaped VehComm nodes (1):
  VehComm3  service_id=17 ecu_type=381102 identifier=0E value=28 ...  [guess: write_by_identifier]
```

## Corpus-wide sanity check

Running `build_command_plan` over every one of the 6,566 recovered scripts
in `data/diag_dump.tar.xz` (parsed with `volvo_p3.script_plan.parse_script_xml`,
no VIDA image needed): all 6,566 parse and plan without error, and 1,272
scripts have at least one detectable activation or write node (1,299
activation nodes, 340 write-shaped `VehComm` nodes total). This is close to,
not identical to, report 04's "1,172 strict activation-bearing scripts"
figure, because the two counts use slightly different inclusion rules
(report 04's "strict" set required a CRC-verified body among other
filters; this count takes every parseable script in the dump as is).

## What is still open

Same open items as `docs/vida-service-templates.md`, plus:

- No event-handler ordering, so a caller cannot yet tell *when* each
  activation or write fires relative to others, or what a Stop button
  actually does (report 04 found Hide/Stop do not reliably call
  `Activation.Stop`).
- `WriteStep.security_gated` only looks at a node's own properties. Most
  real security-gated writes get their value from a separate preceding
  `Components.Security` node through a `StringFormat` node, which this
  flag does not see (see the field's docstring).
- No settings-specific `valuelist`/allowed-value resolution yet (report 05's
  proposed `PyDiCE setting` schema has a `validate` step this module does
  not implement).
