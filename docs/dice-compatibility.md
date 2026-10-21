# DiCE protocol compatibility surface

Static recovery of the command protocol spoken over USB by the DiCE J2534
adapter, from the shipped Windows driver. The goal is a complete list of
command IDs and enough wire detail that the native transport in `volvo_p3/`
can be verified or extended without the vendor DLL.

Nothing here was measured on hardware. Everything is read out of
`DiCE32_x64.dll`. The only hardware-verified behaviour is the handful of
commands this repository already exercises: those are marked `live` in the
tables, plus `0x04` and `0x62` used by `dice-info`. Treat every other payload
layout as a hypothesis.

## Target

| Artifact | Size | SHA-256 | Role |
| --- | --- | --- | --- |
| `DiCE32_x64.dll` | 790016 B | `5af341982e8785507aa639001e77a9ceec316ef3685c45718956a8800d89dc83` | primary decompilation target |
| `TSDiCE32.dll` (= `DiCE32_wow64.dll` = `J2534_DLL_FILE_WOW64`) | 531456 B | `4dd4eb3bead080d6f7eedab3c93d587b2990ac7fe7565897a4085d0afb55a930` | the 32-bit build; its WOW6432Node registry view names it |
| `DiceUsb.sys` | 33280 B | `ccb78183635a9e422a9d703fd0575c3bd83859127766c07f9785801358683836` | kernel driver |
| `RfcFWDL32.dll` | 81408 B | – | firmware download |

**Which binary is authoritative.** `DiCE32_x64.dll` is the primary target, because
it is the only one with a full set of decompiled bodies and it carries the
`UnaryCommand` template instantiations that name most commands. The image
installs both builds: `TSDiCE64.dll` is the 64-bit library and `TSDiCE32.dll`
the 32-bit one. If VIDA's diagnostic process is 32-bit, as the earlier notes
inferred, the DLL beneath it is `TSDiCE32.dll`. The installer's WOW64 payload,
the `J2534_DLL_FILE_WOW64` cab entry, and the `TSDiCE32.dll` installed on the
image are all the same bytes, SHA-256 `4dd4eb3b…`. The 32-bit build's `URFQ_*`/`URFR_*` table was recovered separately and every
command ID matches the 64-bit table, so the ID space is not in doubt. As in the
64-bit build it covers 57 of the 68 rows: the eleven it cannot name are the
ones that never instantiate the template, namely the four hand-built commands,
the two verified only at runtime, and the five still unidentified.

The image's own registry carries both views. The 64-bit `SETEK - DiCE` key names
`TSDiCE64.dll`, and the `WOW6432Node` mirror names `TSDiCE32.dll`. The 32-bit
claim above is supported: VIDA's Diagnostics control is IE-hosted 32-bit and
writes the WOW6432Node `LogFile` with the same `05:53:11` session start as
`VIDA.log`. A live DLL load was not logged. The command-table analysis is
unaffected: the 32-bit and 64-bit tables agree.

`DiCE32_x64.dll` is PE32+, image base `0x180000000`, seven sections, and 1206
functions described by the `.pdata` exception directory. All function extents
quoted below are `.pdata` `BeginAddress`/`EndAddress` pairs, which are
authoritative. Two earlier attempts to guess function starts from prologues
produced wrong bounds and were discarded; do not replace `.pdata` with a
heuristic.

## How the table was recovered

Five passes, each recorded per row in the `source` field the CLI prints:

1. **`decompile`** — the authoritative pass. Every channel command is built by a
   member function that instantiates a firmware wrapper template, and the
   decompiled body spells out the command byte, the owning method and both
   payload structs:

   ```
   local_248[0] = UnaryCommand<26,
       private: virtual enum _J2534_ERROR_CODE_ __cdecl
       CanChannel::AddMsgFilter(_J2534_FILTER, _PASSTHRU_MSG *, ...) __ptr64
       '::`2'::RFQ_RawCanAddFilter,
       '::`2'::RFR_RawCanAddFilter>::vftable;
   ```

   `26` is `0x1A`, the method names the J2534 entry point, and the two
   `'::`2'::RF*_` arguments are the request and reply structs. 57 rows. Which
   template class it is also settles `direction` without a second source:
   `UnaryCommand` carries both structs, `VoidUnaryCommand` only the request,
   `UnaryCommandVoid` only the reply, `VoidUnaryCommandVoid` neither. Because
   those 57 rows have both an independently recovered direction and an
   independently recovered struct pair, the two can be cross-checked, and they
   agree on all 57.
2. **`strings`** — four rows have no template instantiation but sit inside a
   function whose literals name the operation (`XonXoffAddPeriodic`,
   `Iso9141FastInit`, ...). 4 rows.
3. **`store-site`** — the command byte is written with `movb $0xNN` into the
   outgoing buffer. Scanning `.text` for that store shape, filtered to
   `0x02..0x90`, yields the remaining IDs, including the five rows whose meaning
   is still unknown. 5 rows.
4. **`live`** — observed working against a real adapter through this
   repository. 2 rows.

RTTI is no longer a separate tier: the template argument it was read from is the
same text `decompile` reads, and reading it out of the function body also gets
the owner and the struct names. The earlier RTTI sweep agreed with the decompile
pass on every one of the 55 rows it covered, which is why the table keeps no
`rtti` rows — they were upgraded rather than dropped.

Two exclusions matter when re-running the sweep:

- `0x0F` is a decoy. The store at `0x18002b38e` belongs to a timer helper
  (`0x18002b384`..`0x18002b38e`) that writes a deadline, then calls through
  `0x180071020` and compares elapsed ticks against `0x7d0`. It is not a command.
- The region `0x18006e8xx`..`0x18006ea7x` is full of one-instruction
  `lea`/`jmp` stubs. Those are MSVC destructor and `catch`-handler thunks, not
  command builders. Do not classify a small function as a decoy until its real
  bounds have been dumped: `0x82`, `0x84` and `0x87` live near that region and
  are genuine commands.

That leaves 66 distinct store sites plus `0x00` (template only, no store found)
and `0x81` (a reply opcode, never stored as a request) — 68 rows, 68 unique IDs,
68 unique names.

One correction to an earlier reading of this table: `0x11`, `0x13`, `0x21`,
`0x23`, `0x31`, `0x33`, `0x41` and `0x43` (`Free`/`Stop` on all five channels)
were once flagged as having no request struct, which read like a defect. They do
have one — `RFQ_RawCanFree`, `RFQ_Iso15765Stop` and so on — and the flag was a
bug in the ad-hoc harvester, which stopped at the first `>` inside the nested
template instead of the end of the argument list. Every `Free`/`Stop` row is a
plain request with no reply.

## Framing

Every USB transfer after enumeration is one RPC frame:

```
B5 | length:u16le | checksum(header) | payload... | checksum(payload)
```

- `length` is `len(payload) + 1`, so it counts the trailing payload checksum
  byte but not the header checksum.
- `checksum(data) = (0x69 + sum(data)) & 0xFF`, additive. It is computed over
  the three header bytes and over the payload independently.
- `payload[0]` is the command ID. For replies, `payload[1]` is a status byte:
  `0x00` is success, and the remaining codes are listed in
  `volvo_p3/dice_wire.py`.

### The two checksums are confirmed independently

This framing was originally recovered from `DiCE32_x64.dll`. It is corroborated
by three independent sources that were analysed separately.

The vendor's own error enum distinguishes the two checksums. `0xFC` is
"RFC header checksum error" and `0xFE` is "RFC message checksum error"
(`volvo_p3/dice_wire.py`). A single running sum cannot produce two distinct
checksum error classes, so the wire format must carry two separately computed
values.

`TSFRDiCE.dll` — the flight recorder, a third module — carries the error symbols
`ERR_RFC_HCS` and `ERR_RFC_MCS` side by side, along with `RFC_ERR_TIMEOUT` and a
`[RFC] FID = 0x%02X` trace format. That names all three frame roles at once:
two checksums and a function ID.

`RfcFWDL32.dll` — the firmware-download library, a different module that shares
the same USB pipe — encodes and decodes the frame with constants `0x1E`
and `0x69` in two separate accumulators. Describing the frame from that end
gives:

```
header checksum  = (0x1E + n_lo + n_hi) & 0xFF
message checksum = (0x69 + fid + sum(data)) & 0xFF
```

These are the same functions as the ones above, not competing ones. The
firmware encoder simply does not fold the `0xB5` marker into its accumulator,
and `(0x69 + 0xB5) & 0xFF == 0x1E`. The message checksum matches term for term,
since `payload[0]` *is* the function ID. So `0x1E` in one binary and
`0xB5`-included-in-a-`0x69`-seeded-sum in the other are two spellings of one
formula.

**One field is still ambiguous: the length value.** The two readings disagree by
one. Counting the bytes after the length field gives `header_csum + fid + data +
message_csum`; this project's implementation counts `fid + data + message_csum`
and excludes the header checksum byte. Both interpretations decode their own
frames correctly and neither has been checked against a real frame, because no
DiCE has been attached to this machine. The implementation keeps its current
behaviour rather than silently shifting by one, and the question stays open
until a captured frame settles it. A one-byte disagreement here desynchronises
the stream, so it is the first thing to verify on hardware.

A reply whose `payload[0]` differs from the request's command ID is an
asynchronous event, not an answer. `0x81` is the only such event this project
has seen, but the driver's receive path (`0x18001c800`) is generic, so other
event opcodes may exist.

## The layer underneath: `DiceUsb.sys`

The kernel driver was analysed separately, because a wrong assumption here would
put phantom structure in the model. It contains no protocol at all.

`EvtIoRead` (`0x129f0`) and `EvtIoWrite` (`0x12f74`) each take the request's
memory handle once, hand it to `FormatRequestForRead`/`FormatRequestForWrite`,
and send it. There is no store through the buffer, no length arithmetic, no
comparison against `0xB5` or `0x80`, no per-stream state, no short-read padding,
no chunking and no checksum. Completions (`0x12dc0`, `0x13338`) log and complete.

There is likewise no payload validation: the `4128` message-size constant does
not appear, nothing knows about the sixteen-byte `0x80` record, and there is no
clamping, splitting, coalescing or retry. The image's only block-fill helper
(`0x13b50`) is never called on the data path.

So **the frame described above is entirely host-side.** The device receives
exactly those bytes, and everything a DiCE understands is expressed in the RFC
frame — including vendor functions such as `ENABLE_BT`, which cannot be reached
another way, because the driver offers no way to issue a vendor control
transfer. It never builds a USB setup packet.

Two things follow for a client. Because nothing pads short reads, a partial
frame can arrive at any boundary; the decoder has to buffer across reads, which
is what `PacketDecoder` does. And because nothing validates, framing errors
surface as device-side status codes rather than host errors.

**Interface and pipes.** The interface GUID `{4E8A43B0-259A-43C2-A025-BAEE43C02571}`
is created at `0x15150` (referenced once, from `0x19bd5`). It is a device
interface only — there is no symbolic link and no `IoCreateDevice`. The driver
never compares endpoint addresses; `0x1a1b0` selects one interface and accepts
only `PipeType == 3` (bulk), taking direction from the framework
(`0x16c90`/`0x16c98`) and storing the pipes at `DEVICE_CONTEXT+0x10` (IN) and
`+0x18` (OUT). Both must exist, or the request fails with `0xC0000184`. The
endpoint numbers `0x01`/`0x81` are therefore descriptor properties, not
assertions in driver code.

Bulk traffic bypasses IOCTLs entirely: `ReadFile`/`WriteFile` are served by
three independent WDF queues (read `0x199f9`, write `0x19af8`, device-control
`0x19959`), so one read and one write can be outstanding at once.

**Only two IOCTLs exist.** Both are control-plane; neither appears on the wire.

| Code | Purpose | Where |
| --- | --- | --- |
| `0x55006000` | Parameter/descriptor query. Probes with a zero-length buffer, expects `0xC0000023`, then fills the real buffer and returns the length. Input buffer is not read. If the buffer is too small, the required size is *not* returned. | `0x1a550` |
| `0x5500A004` | Abort/reset the pipe. | `0x1a768` |

Anything else returns `STATUS_INVALID_DEVICE_REQUEST` (`0xC0000010`) at
`0x1a5ec`. The only timeout constant in the image is `0x2710` (10000 ms) in the
idle policy at `0x1a084`, gated on a registry flag. There are no `D0Entry` or
`D0Exit` callbacks, so the driver does no power-transition work; a re-enumeration
would be consistent with selective suspend, but that causal link is inference.

**The driver writes its own registration.** It creates
`\Registry\Machine\Software\PassThruSupport.04.04` (and the `Wow6432Node`
mirror) with `Name`, `Automatic`, `Comport`, `BtAddress`, `BtAutomatic` and
`BtPerformance`. The VIDA image carries a populated vendor key with no `Name`
value, which is consistent with the driver never having run — the image has no
`Enum\USB\VID_17AA&PID_D1CE` device instance either.

**What the VIDA image does and does not contribute.** The coupling is by
registry, not by import, but it is split across more than one assembly.
`Util.DLL` reads `Vendor`, `FunctionLibrary` and `ConfigApplication` from
`PassThruSupport.04.04`; `VehCom2.DLL` selects by `CommToolName` and calls
`LoadLibrary`; `UI.DLL` fills the comm-tool list and can launch the named
`ConfigApplication`; and `DiagApp.exe` outside the VIDA directory is a second
client that opens a named device. The shipped snapshot selects nothing:
`DiagnosticConnection` is `NoCommConnection`, `CommToolName` is empty, and no
USB device instance exists. The empty name maps to `CommToolType=NOCOMM`, which
this build does not implement; connection construction throws
`VehComException("Unsupported communication tool")` before any J2534 call. So the
vendor code path for selection does exist, but this snapshot leaves it
unconfigured. A second pass records the split in `docs/vida-second-pass.md`.

The image's only protocol-timing data is a SQL Express database,
`VIDA/db/DiceTiming_Data.MDF`, whose lookup key is an ECU variant plus the raw
first three request bytes and whose results are `P1`/`P3`/`P4` timing values.
That is vehicle-side protocol timing, not adapter behaviour: it says nothing
about any command ID in this document. Searching the image for the command space
accordingly yields nothing, and the same is true of the CHM, the kernel driver
and the flight recorder. Several independent artifacts are silent on those
names, which is why the remaining unknowns are recorded as unknowns.

## Command space

The ID space splits into three planes:

- **Device plane** (`0x00`..`0x6F`) — adapter identity, warranty clock, supply
  voltage, LED.
- **Protocol control plane** — four protocol blocks of sixteen IDs each:
  RawCAN `0x10`, XON/XOFF `0x20`, ISO 9141 `0x30`, ISO 15765 `0x40`. Each block
  implements the same operation sequence with the same relative order, but the
  IDs are **not** slot indices: every block skips exactly one ID at a
  block-specific position (`0x16`, `0x27`, `0x38`, `0x47`), so from that point
  on the ID is one higher than the naive `base + operation` arithmetic. ISO 9141
  is the clearest case: its transmit-clear is `0x39`, not `0x38`. RawCAN's hole
  sits immediately *before* `TxStatus`; the other three sit immediately *after*
  it.
- **Data plane** (`0x80`..`0x8F`) — bulk frame transfer, the receive event, and
  five-baud initialisation.

### A third numbering space: firmware FIDs

Everything above is the **DLL's** command space. The device firmware answers a
different set of small integers, the function IDs, and those are what
`RfcFWDL32.dll` sends during a firmware update. They must not be added to the
command table: the two spaces overlap numerically and only *partly* agree in
meaning.

| FID | Name | FID | Name |
| --- | --- | --- | --- |
| `0x00` | `RfcDeviceVersion` | `0x77` | `FwdlProg` |
| `0x01` | `RfcMaxSize` | `0x79` | `FwdlErase` |
| `0x02` | `SysReset` | `0x7A` | `FwdlLock` |
| `0x03` | `SysHardwareVersion` | `0x7B` | `FwdlRead` |
| `0x04` | `SysFirmwareVersion` | `0x7C` | `FwdlWrite` |
| `0x05` | `SysFirmwareDisable` | `0x7D` | `FwdlCS` |
| `0x06` | `SysBootloaderVersion` | `0x7E` | `FwdlReadE2` |
| `0x07` | `SysDeviceName` | `0x7F` | `FwdlWriteE2` |
| `0x08` | `SysUsbProductId` | | |

The overlap is informative where it exists. `0x00` is the version query in both
spaces, `0x03` is the hardware version in both, and `0x04` is the firmware
version in both — the same three meanings, reached through two different
transports. That agreement is the reason the device-plane names are trusted.

`0x02` is the case to be careful with. Its FID meaning is settled, because
`FwdlApp.exe` reaches it by name and passes `0x0BADC0DE` before `SysReset`
returns. But there is **no** `UnaryCommand<2, …>` instantiation and no
`RFQ_SysReset`/`RFR_SysReset` symbol anywhere in the DLL, so `0x02` is not
established as a channel command and is deliberately absent from the table. A
reset is reachable only through the firmware path.

The device block therefore holds `0x00`, `0x03` and `0x04` and nothing else, so
it has two holes at the bottom. That is a different reason from the protocol
blocks, which each skip exactly one ID for their own reasons.

### Protocol blocks: operation to ID

Tier markers: `*` = named from strings (no `UnaryCommand` instantiation carries
that ID, so the name comes from the log/tag string the caller passes to the
command object); every other row in this table is `decompile`, meaning a
decompiled function body instantiates `UnaryCommand<ID, …>` with that byte.

| Operation | RawCAN (`0x10`) | XON/XOFF (`0x20`) | ISO 9141 (`0x30`) | ISO 15765 (`0x40`) |
| --- | --- | --- | --- | --- |
| Init | `0x10` | `0x20` | `0x30` | `0x40` |
| Free | `0x11` | `0x21` | `0x31` | `0x41` |
| SimpleStart | `0x12` | `0x22` | `0x32` | `0x42` |
| Stop | `0x13` | `0x23` | `0x33` | `0x43` |
| SetParameter | `0x14` | `0x24` | `0x34` | `0x44` |
| GetParameter | `0x15` | `0x25` | `0x35` | `0x45` |
| FastInit | — | — | `0x36` \* | — |
| TxStatus | `0x17` | `0x26` | `0x37` | `0x46` |
| TxClear | `0x18` | `0x28` | `0x39` | `0x48` |
| RxClear | `0x19` | `0x29` | `0x3A` | `0x49` |
| AddMsgFilter | `0x1A` | `0x2A` | `0x3B` | `0x4A` |
| RemoveMsgFilter | `0x1B` | `0x2B` | `0x3C` | `0x4B` |
| AddPeriodicMsg | `0x1C` | `0x2C` \* | `0x3D` \* | `0x4C` \* |
| RemovePeriodic | `0x1D` | `0x2D` | `0x3E` | `0x4D` |
| *(no command)* | `0x16` | `0x27` | `0x38` | `0x47` |

Replies are returned by `Init`, `GetParameter`, `TxStatus`, `AddMsgFilter`,
`AddPeriodicMsg`, `FastInit` and `Iso9141FiveBaudInit`; the rest are one-way
requests. A client that ignores the reply-bearing ones still works, but loses
the channel handle (`Init`), filter handle (`AddMsgFilter`) and periodic handle
(`AddPeriodicMsg`) that the corresponding `Remove*` calls need.

`Iso9141FiveBaudInit` is `0x86`, outside the ISO 9141 window, because it is
implemented as a raw-line operation rather than a channel operation.

### Device plane

| ID | Name | Reply | Tier |
| --- | --- | --- | --- |
| `0x00` | RfcVersion | response | decompile |
| `0x03` | SysHardwareVersion | response | decompile |
| `0x04` | SysFirmwareVersion | response | decompile |
| `0x50` | UnknownDevice50 | unknown | store-site |
| `0x51` | UnknownDevice51 | unknown | store-site |
| `0x5E` | WarrantyClockSetOnce | request_response | decompile |
| `0x5F` | WarrantyClockGet | response | decompile |
| `0x62` | BitSupplyVoltage | response | decompile |
| `0x68` | BitLed | request_response | decompile |

`0x03` and `0x04` are both identity replies, and both are template-confirmed
rather than inferred: `Device::GetHardwareVersion` instantiates
`UnaryCommand<3, …::RFQ_SysHardwareVersion, …::RFR_SysHardwareVersion>` and
`Device::ReadVersion` instantiates the same template at `4` with
`SysFirmwareVersion`. Earlier passes had them provisional because all they could
see was a `movb $0x03`/`$0x04` store site and not which method owned it;
decompiling the body resolves the owner and removes the doubt.

Their payloads are still unevenly known. `0x04` is measured: 35 bytes come back
and `[4:34]` is the firmware build string (`Aug 22 2011 09:03:43` on the tested
unit), which is what `dice_rpc.system_info()` reads. `0x03`'s reply shape is
unmeasured.

`0x50` and `0x51` are the same function twice. Both build a one-byte payload —
the reserve call at `0x18001cb90` is given a count of exactly `1` — so neither
carries an argument, and they differ only in the log string they emit. Their
purpose is unknown; plausible readings are a reset/status pair.

Two payloads here are measured rather than inferred.

`0x62` has no request payload at all. `Device::ReadBatteryVoltage` instantiates
`UnaryCommandVoid<98, …::RFR_BitSupplyVoltage>`, which reserves the reply but
never writes a request body, and it returns a single `u32` decoded from the
reply. This is a reply-only command: sending `0x62` with a body would be wrong.

`0x68` takes five bytes:

```
u8  led_number
u32 period      (little-endian)
```

`Device::RunLedTest` writes them via overlapping locals — the `u8` store to
`local_30` and the `u32` store to `local_2f` share storage — which is why the
field widths had to be read off the store instructions. Note that this is
narrower than the API-side `S_LED_CONTROL` struct in the vendor header, which is
twelve bytes and carries both a state and a period. The struct is not sent raw;
only the LED number and period reach the wire, and the LED state is expressed by
which command is used.

### Data plane

| ID | Name | Reply | Tier |
| --- | --- | --- | --- |
| `0x80` | RawCanWriteMsgs | request | live |
| `0x81` | RawCanEvent | response | live |
| `0x82` | UnknownBulk82 | request | store-site |
| `0x84` | UnknownBulk84 | request | store-site |
| `0x86` | Iso9141FiveBaudInit | request_response | decompile |
| `0x87` | UnknownBulk87 | request | store-site |

`0x82`, `0x84` and `0x87` are real command builders, not thunks: each reserves
space, writes `payload[0] = id`, copies the channel number from the channel
object at `+0x120` into `payload[1]`, then a qword at `payload[2..9]` and a word
at `payload[10..11]`, and appends a variable-length tail through the shared
emitter `0x1800637d0`. That is the same `[id][channel]` header shape as `0x80`,
which is why they are grouped here, but the record that follows is not the
`0x80` 16-byte CAN record. Their names and record layouts are unknown.

## Wire layouts known well enough to implement

### `0x80` — RawCAN transmit

```
80 | channel:u8 | 10 00 | dlc+8:u8 | flags:u8 | seq:u16le | id:u32le | data[8]
```

The `10 00` is a two-byte little-endian record size of 16. `flags` is `0x00`
for an 8-byte standard-ID frame. The reply to a transmit is the `0x81` event
below with bit 3 of `flags` set, not a `0x80` reply.

### `0x81` — CAN event

```
81 | status:u8 | length+14:u8 | channel:u8 | flags:u8 | ?:u8 | seq:u16le | ?:u32 | id:u32le | data[length]
```

- `status` must be `0x00`.
- `flags` bit 3 (`0x08`) marks a transmit confirmation; such frames are exactly
  16 bytes and carry the sequence number of the request in `[6:8]`.
- `flags` bit 0 (`0x01`) marks a 29-bit identifier.
- For receive frames, `length = payload[2] - 14` and the frame is
  `16 + length` bytes long; `length` is the CAN DLC and must be 8 or less for a
  classic frame.

This is the only bulk layout that has been checked against hardware. It is
implemented in `volvo_p3/dice_can.py`.

### Channel commands

`0x10` (init) and the `SetParameter`/filter calls take `[id][channel]` followed
by their arguments; `Init` additionally carries a protocol selector (`0x01` for
CAN in the current Python). Filter records are `flags:u16 | type:u8 |
pattern id:u32 | pattern data[8] | mask id:u32 | mask data[8]`, and
`AddMsgFilter` answers with the filter handle in `reply[2]`.

Everything else — parameter encodings, periodic-message records, ISO 9141 and
ISO 15765 framing — is **not** recovered. Deriving it requires either a
decompiler over the `URFQ_*`/`URFR_*` accessor functions or a hardware capture.

### A second idiom: hand-built commands

Four commands do not use the `UnaryCommand` template at all: `0x2C`
(`XonXoffAddPeriodic`), `0x36` (`Iso9141FastInit`), `0x3D`
(`Iso9141AddPeriodic`) and `0x4C` (`Iso15765AddPeriodic`). They assemble their
frame byte by byte — size the buffer, write the command ID, write the channel
index, copy the caller's message, then hand the buffer to the generic command
object. Because they never instantiate the template, they leave no `URFQ_*`
symbol behind, which is why the RTTI harvest missed them and why their names
come from string tables instead. These four are exactly the `strings`-tier rows
in the table.

Note that the two idioms do not split along Add/Remove lines. `0x1C`
(`RawCanAddPeriodic`) *is* a template instantiation and sits in the decompile
tier, while its ISO 9141 counterpart `0x3D` is hand-built. `0x3E`
(`Iso9141RemovePeriodic`) is templated as `UnaryCommand<62, …RFQ_Iso9141RemovePeriodic>`
while `0x2C` is not. Check both paths for every command; the tier column is
there because the difference is not predictable.

`0x3D`, `Iso9141AddPeriodic`, is the one with a fully measured layout:

```
3D | channel:u8 | interval:u16le | message bytes past their 2-byte header
```

The interval is the caller's period in milliseconds. The message is copied
verbatim after its own two-byte length header. The reply is a single byte, the
message ID the device assigned, which the caller stores for a later
`RemovePeriodic`. Three failure paths are distinguished by string rather than by
RFC status: a failed allocation logs `"Failed to create message"` and returns
`7`, an empty response logs `"Response empty"` and returns `7`, and a malformed
message logs `"Invalid message"` and returns `3`. So this family reports its own
error class instead of surfacing the wire status byte.

## J2534 entry points

The DLL exports fourteen J2534 functions at ordinal base 1. There is no
`PassThruSelect`, `PassThruQueueMsgs` or `PassThruGetVersion`.

| Ordinal | Export | Body |
| --- | --- | --- |
| 1 | `PassThruOpen` | `0x1800596b0` |
| 2 | `PassThruClose` | `0x180059cc0` |
| 3 | `PassThruConnect` | stack-arg stub into `0x18005a030` |
| 4 | `PassThruDisconnect` | `0x18005a5a0` |
| 5 | `PassThruReadMsgs` | `0x18005a820` |
| 6 | `PassThruWriteMsgs` | `0x18005ad40` |
| 7 | `PassThruStartPeriodicMsg` | `0x18005b260` |
| 8 | `PassThruStopPeriodicMsg` | `0x18005b690` |
| 9 | `PassThruStartMsgFilter` | stack-arg stub into `0x18005b9a0` |
| 10 | `PassThruStopMsgFilter` | `0x18005bf00` |
| 11 | `PassThruSetProgrammingVoltage` | `0x18005c210` |
| 12 | `PassThruReadVersion` | `0x18005c360` |
| 13 | `PassThruGetLastError` | `0x18005c980` |
| 14 | `PassThruIoctl` | `0x18005cbe0` |

The export table entry points at a `jmp rel32` thunk, so the export RVA is never
the function body; each one has to be followed before use. Two exports
(`Connect`, `StartMsgFilter`) need one more hop because their fourth and fifth
arguments arrive on the stack and the thunk reshuffles them into registers.

The ordinal-to-command mapping is not yet established. A call-graph sweep from
each export saturates at roughly 540 of 1206 functions because everything funnels
through shared buffer and USB helpers, so it does not discriminate between
exports. There is also no data-resident table of function pointers — a
qword-aligned scan of the whole image for command-function addresses returned
zero hits — which suggests dispatch is by a runtime-built object or by a
32-bit relative table that has not been located.

The vendor's own header, `TSDiCE32.h`, was recovered from a Doxygen dump and
confirms the surface is exactly these fourteen. It is J2534-1 with no J2534-2
logical-channel API: no `PassThruSelect`, no `PassThruQueueMsgs`, no
`PassThruGetVersion`, and every function returns a `J2534_ERROR_CODE`.

**`PassThruSetProgrammingVoltage` is a stub.** The header says so outright —
"THIS FUNCTION IS NOT IMPLEMENTED FOR THIS DEVICE", returning
`ERR_NOT_SUPPORTED` unconditionally — and there is no pin list or voltage range
anywhere in the documentation to implement it against. This is not a gap in the
reverse engineering; the feature does not exist. It rules out any workflow that
needs pin switching or a programming voltage from the adapter, which aligns with
the absence of `*_PS` protocol variants and with VIDA's own log strings
announcing that it is checking for pin-switching support on `CAN_PS` and
`ISO15765_PS` and not finding it.

`PassThruReadVersion` requires a buffer of at least eighty characters per
pointer; that length is the `J2534_DEFAULT_STRING_LENGTH` macro.

`PassThruIoctl` itself performs no per-ioctl validation. Its documented body is
only "the structures pointed to by pInput and pOutput are determined by the
IoctlID", so every size and range rule lives in the enum comments of the header
rather than in the implementation. Those documented limits are worth recording
because they bound what a caller may ask for: periodic and filter intervals
5–65535 ms, at least ten filters and ten periodic messages supported, a
twelve-byte cap on the filter and periodic payloads, `DATA_RATE` 5–500000 baud,
and `ledBlinkPeriod` 10–10000.

### Two unrelated IOCTL spaces

`PassThruIoctl` codes and kernel-driver IOCTL codes are different namespaces and
must not be conflated. The driver understands only `0x55006000` and
`0x5500A004`. Every J2534 ioctl below is translated by the DLL into an RFC frame
first, so a J2534 ioctl never reaches the driver as an IOCTL. The driver's two
codes appear nowhere in the vendor header, which is what makes this visible.

Standard ioctls follow the J2534-1 enumeration: `GET_CONFIG` `0x01`,
`SET_CONFIG` `0x02`, `READ_VBATT` `0x03`, `FIVE_BAUD_INIT` `0x04`, `FAST_INIT`
`0x05`, `CLEAR_TX_BUFFER` `0x07`, `CLEAR_RX_BUFFER` `0x08`,
`CLEAR_PERIODIC_MSGS` `0x09`, `CLEAR_MSG_FILTERS` `0x0A`,
`CLEAR_FUNCT_MSG_LOOKUP_TABLE` `0x0B`, `ADD_TO_FUNCT_MSG_LOOKUP_TABLE` `0x0C`,
`DELETE_FROM_FUNCT_MSG_LOOKUP_TABLE` `0x0D`, `READ_PROG_VOLTAGE` `0x0E`.

The vendor then adds private codes above that range:

| Code | Name | Command ID |
| --- | --- | --- |
| `0x10000001` | `TEST_CARB_PLUG_CONNECTED` | — |
| `0x10000002`..`0x10000006` | `TEST_EEPROM`, `TEST_FLASH_INTERNAL`, `TEST_FLASH_EXTERNAL`, `TEST_RAM_INTERNAL`, `TEST_RAM_EXTERNAL` | — |
| `0x10000007` | `TEST_LED` | `0x68` |
| `0x10000008`..`0x1000000E` | seven loopback tests (`PC_DEVICE`, `K_PIN7_TO_L`, `K_PIN11_TO_L`, `L_TO_K_PIN7`, `L_TO_K_PIN11`, `CAN_MS_CAN_HS`, `CAN_HS_CAN_MS`) | — |
| `0x10001001` | `WARRANTY_CLOCK_GET` | `0x5F` |
| `0x10001002` | `GET_HW_VERSION` | `0x03` |
| `0x10001003` | `ENABLE_BT` | none found |
| `0x20000001` | `CAN_XON_XOFF_FILTER` | — |
| `0x20000002` | `CAN_XON_XOFF_FILTER_ACTIVE` | — |

Three of these corroborate the command table from a second direction: the ioctl
names line up with command IDs recovered from the decompiler independently.
`ENABLE_BT` has no matching ID, so either it is unimplemented in this build or it
travels by a path not yet found — it is listed as an open question rather than
assumed to exist.

The header also pins the API-side sizes, which bound any buffer arithmetic:
`MAX_PASSTHRU_MSG_DATA_SIZE` is `4128`, so `PASSTHRU_MSG` is **4152** bytes
(six `u32` fields plus the data array), and `S_XON_XOFF_FILTER` is four
`PASSTHRU_MSG` records, **16608** bytes. Two constants are named
`PHYSICAL_CHANNEL` (`1<<29`) and `FUNC_ADDR` (`1<<28`) and belong to the
*connect-flags* enum, not to the message `TxFlags` — and `CAN_FRAME_RAW`
(`1<<10`) appears in **both** the `RxStatus` and `TxFlags` enums, so a raw-CAN
message sets it in one and tests it in the other.

Where the header and the decompiled bodies disagree, the measured wire layout
wins. `S_LED_CONTROL` is declared as twelve bytes (`LED_NO`, `LED_STATE`, `u32`
blink period) with a blink range of 10 to 10000, yet `0x68` puts five bytes on
the wire (`u8` LED number, `u32` period). The struct is evidently never sent
raw.

## Internal helpers worth knowing

| Address | Role |
| --- | --- |
| `0x18001c3a0` | send a device-plane buffer (`%rcx` device, `%rdx` buffer, `%r8` reply struct) |
| `0x18000ff10` | send a per-channel buffer |
| `0x18001c800` | receive and parse one frame |
| `0x18001cb90` | reserve/grow a byte vector (`%edx` count) |
| `0x180043180` | reserve/grow a channel-command builder |
| `0x1800637d0` | append a variable-length tail and emit |
| `0x18002b840` | sized-buffer copy helper |

Command builders have a consistent shape: zero a vector, reserve
`1 + len(args)`, guard the returned pointer against null, store the command byte,
advance the write pointer, then call the send helper. That shape is the most
reliable signature for finding further commands.

## What this means for `volvo_p3`

Implemented and hardware-verified: `0x10`, `0x11`, `0x12`, `0x13`, `0x14`,
`0x1A`, `0x1B`, `0x80`, `0x81`, plus `0x04` and `0x62` for `dice-info`.
Unimplemented but named and available: the rest of the RawCAN block, the whole
XON/XOFF and ISO 9141 blocks, `Iso15765*`, `FastInit`, `FiveBaudInit`, the
warranty clock, `BitLed`, and `RfcVersion`.

Two cautions before extending the native path:

- `dice_can.py` deliberately allow-lists the CAN payloads it will transmit
  (OBD mode requests, flow control, and `19 02 FF` to the module range). That
  restriction is a design guarantee of this project, not a protocol limit, and
  must not be relaxed to chase compatibility.
- The ISO 15765 block is *not* what `dice_can.py` uses. Its ISO-TP segmentation
  is done in Python over the RawCAN block. Adding `Iso15765*` commands would
  duplicate that logic inside the adapter, where this project cannot audit what
  the firmware does with a malformed frame.

Three structural facts from the layers below the DLL bear on that list. The
driver is a transparent pipe, so nothing this project sends is reshaped or
sanitised on the way out — a malformed frame reaches the device as-is, and the
only thing standing between a bug and the hardware is the code in this project.
`PassThruSetProgrammingVoltage` does not exist, so no amount of protocol support
will reach the `*_PS` variants. And `Iso15765*` inherits the same objection as
above: it would move segmentation into firmware this project cannot inspect.

## Open questions

Each is written so it can be closed by a specific piece of evidence.

1. **The length field counts one byte more or one byte less than this
   implementation assumes** (see *Framing*). This is the highest-value open
   question, because a one-byte error desynchronises the stream. Closing it
   needs a single captured frame from real hardware.
2. Payload and reply layout for the remaining `URFQ_*`/`URFR_*` pairs. Needs a
   decompiler over the class accessors, or a hardware capture of each command.
3. Names for `0x50`, `0x51`, `0x82`, `0x84`, `0x87`. Everything available has
   been tried: the vendor header, the firmware function IDs, RTTI in both the
   64-bit and 32-bit builds, the kernel driver, the flight recorder and the
   whole VIDA image all come up empty. The 32-bit build was expected to help and
   did not — its template table covers the same 57 rows and omits these five,
   exactly as the 64-bit one does. What is left is a decompiler pass over the
   32-bit bodies looking for a string-based label, or a capture.
4. Which J2534 export emits which command ID. Needs the dispatch mechanism: look
   for a 32-bit relative slot table or an object with a function-pointer member
   written at construction time.
5. What the four unused IDs (`0x16`, `0x27`, `0x38`, `0x47`) were for.
6. Units and scaling of the `0x62` supply-voltage reply (currently treated as a
   raw little-endian word at `[2:4]`). Needs a multimeter comparison.
7. Whether `ENABLE_BT` is implemented at all. The ioctl is documented in one
   sentence with no payload layout, no polarity and no companion getter, it has
   no matching command ID, and the flight recorder spells it `BT_ENABLE` while
   the header spells it `ENABLE_BT`. Those are three independent signs of a
   vestigial API rather than a missing lead.
8. How the `SetParameter` values `0x14`, `0x24`, `0x34`, `0x44` decompose. They
   do not split into `(ioctl_id << 16) | parameter_id` for any documented pair,
   so either the packing differs or they carry protocol-specific meaning.

### Closed since this document was first written

- *Whether `0x03` is hardware or firmware version* — settled by the RTTI
  template names, not by a capture: `GetHardwareVersion` instantiates
  `UnaryCommand<3, …SysHardwareVersion, …SysHardwareVersion>` and
  `ReadVersion` instantiates the same template at `4` with `SysFirmwareVersion`.
- *Whether `DiceUsb.sys` adds framing or filtering below the DLL* — no. See
  *The layer underneath*.
- *Whether the two-checksum frame model is right* — confirmed three ways. The
  only part still open is the length field, which is question 1 above.
- *Whether the 32-bit build uses a different ID space* — no, and it does not
  carry extra names either. Its `URFQ_*`/`URFR_*` table was recovered separately
  and every one of the 57 IDs agrees with the 64-bit table, so the build VIDA
  loads speaks the same numbering. The six non-template helper strings in it
  (`Iso9141Keys`, `Iso9141WriteCommand`, `Iso9141FastInit`, `Iso15765Keys`,
  `Iso15765WriteCommand`, `XonXoffKeys`) are present in the 64-bit build too,
  checked directly in both files. Only `Iso9141FastInit` is a command name —
  `0x36` — and the `*Keys`/`*WriteCommand` names are helper structures and
  objects, so this line of enquiry does not close question 3.

## Reproducing

```sh
python3 -m volvo_p3.cli list-dice-commands
python3 -m volvo_p3.cli list-dice-commands --category raw_can
python3 -m volvo_p3.cli list-dice-commands --types
```

`--types` shows the evidence tier of every row, which is the fastest way to see
how much of the table is inferred rather than measured. The table itself lives
in `volvo_p3/dice_commands.py`; each row carries its tier, so an inference can
be challenged without re-reading the binary.

The raw disassembly used here is `objdump -d --no-show-raw-insn` output with
image base `0x180000000`; `0x180000000 + RVA` is the address space used
throughout. Function extents come from the `.pdata` directory parsed as 12-byte
`BeginRVA`/`EndRVA`/`UnwindRVA` triples at RVA `0xbf000`, not from a prologue
heuristic.

Decompilation was done with Ghidra headless, `analyzeHeadless` with a Java
`GhidraScript` — PyGhidra was unusable in this environment because its bundled
JPype wheels stop at CPython 3.13 and the host interpreter is 3.14. Scripts must
therefore be Java, not Python.

The other artifacts analysed alongside the DLL were `DiceUsb.sys`, `RfcFWDL32.dll`,
`TSFRDiCE.dll` and the vendor's `TSDiCE32.h` as rendered by Doxygen, plus the
VIDA Windows image for integration context. Each was examined for one specific
question; none of them was assumed to speak the same vocabulary as the command
table, and several did not.
