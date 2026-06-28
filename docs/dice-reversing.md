# DiCE Native Driver Notes

Goal: implement a Python-native DiCE transport with no Windows, VIDA runtime, or
Microsoft dependencies.

Known USB identity from the test adapter:

* Vendor ID: `0x17AA`
* Product ID: `0xD1CE`
* Product: `DiCE Automotive Adapter`
* Manufacturer: `SETEK AB`

Current host observations on macOS:

* `system_profiler` and IOKit see the device.
* No `/dev/cu.*` serial node is created.
* `python-can` does not detect DiCE as a CAN backend.
* PyUSB/libusb currently loads, but may not enumerate IOKit devices in the
  default macOS context.

Reverse-engineering targets:

1. USB descriptors: configuration, interfaces, endpoints, transfer types.
2. Open/init sequence from VIDA/J2534 driver.
3. CAN channel setup commands for HS/MS CAN and baud selection.
4. Frame send/receive packet format.
5. Periodic tester-present handling.
6. Error/status packet format.

Ghidra targets:

* J2534 entry points and exported PassThru functions.
* Vendor-specific DLL calls below PassThru.
* Constants for USB control requests and endpoint IDs.
* Byte layout for CAN frame packets.

SetupDiCE.exe findings:

* `SetupDiCE.exe` is only an installer bootstrapper. It extracts driver/app MSI
  packages from zlib-compressed `RCDATA` resources, then installs them.
* Resource map from the decompiled installer:
  * `300`: `DiceDriver_x86.msi`
  * `301`: `Dice_x86.msi`
  * `303`: `DiceDriver_x64.msi`
  * `304`: `Dice_x64.msi`
* The installer preserves/restores the J2534 registry key
  `SOFTWARE\PassThruSupport.04.04\SETEK - DiCE`.
* Reproducible extraction:

```sh
.venv/bin/python -m volvo_p3.cli extract-dice-installer /Users/wjk/Downloads/SetupDiCE.exe
```

Driver MSI findings:

* The USB driver INF binds `USB\VID_17AA&PID_D1CE` to a custom KMDF driver.
* Windows files from `DiceDriver_x64.msi`:
  * `DiCEBULK_INF`: `DiceUsb.inf`
  * `DiCEBULK_SYS`: `DiceUsb.sys`
  * `DiCECO1_DLL`: DiCE co-installer
  * `WDFCO09_DLL`: KMDF 1.9 co-installer
  * `DiCE_CAT`: driver catalog
* This confirms the adapter is not a serial CAN adapter. The native path needs
  direct USB bulk access or a macOS IOKit user-client equivalent.

Application MSI findings:

* `J2534_DLL_FILE` is the x64 J2534 library. Its export DLL name is
  `DiCE32.DLL`.
* `J2534_DLL_FILE_WOW64` is the 32-bit J2534 library.
* Export ordinals match J2534:
  * `1`: `PassThruOpen`
  * `2`: `PassThruClose`
  * `3`: `PassThruConnect`
  * `4`: `PassThruDisconnect`
  * `5`: `PassThruReadMsgs`
  * `6`: `PassThruWriteMsgs`
  * `7`: `PassThruStartPeriodicMsg`
  * `8`: `PassThruStopPeriodicMsg`
  * `9`: `PassThruStartMsgFilter`
  * `10`: `PassThruStopMsgFilter`
  * `11`: `PassThruSetProgrammingVoltage`
  * `12`: `PassThruReadVersion`
  * `13`: `PassThruGetLastError`
  * `14`: `PassThruIoctl`
* Useful app payloads:
  * `DIAG_APP_FILE`: diagnostic UI that exercises J2534 open/version/ioctl.
  * `FWDL_DLL_FILE`: firmware downloader DLL.
  * `FWDL_APP_FILE`: firmware downloader app.
  * `MOT_FILE`: Motorola S-record firmware payload.
* `DiCE32.DLL` imports `SetupDiGetClassDevsW`,
  `SetupDiEnumDeviceInterfaces`, `SetupDiGetDeviceInterfaceDetailW`,
  `CreateFileW`, `ReadFile`, and `WriteFile`. On Windows it locates the custom
  USB driver interface and speaks over file-like bulk endpoints.
* `DiCE32.DLL` also has a Bluetooth fallback path, but this project should avoid
  that because it is fragile on macOS and not needed for USB DiCE.
* USB enumeration code starts around `0x180013300` in `DiCE32_x64.dll`.
* It calls `SetupDiGetClassDevsW` with interface GUID
  `4e8a43b0-259a-43c2-a025-baee43c02571`.
* It loops through at most `0x80` device interfaces with
  `SetupDiEnumDeviceInterfaces`.
* It calls `SetupDiGetDeviceInterfaceDetailW` first to get the detail length,
  allocates the detail buffer, then calls it again to retrieve the device path.
* It lowercases the path and matches prefix
  `\\?\usb#vid_17aa&pid_d1ce#`.
* This GUID is distinct from the INF device class GUID. It is the user-mode
  device interface GUID needed by the Windows J2534 DLL.

Command strings found in `DiCE32.DLL`:

* Device/system: `RfcVersion`, `SysFirmwareVersion`, `SysHardwareVersion`,
  `WarrantyClockGet`, `WarrantyClockSetOnce`, `BitSupplyVoltage`, `BitLed`.
* Raw CAN: `RawCanInit`, `RawCanStart`, `RawCanStop`, `RawCanGet`,
  `RawCanSet`, `RawCanTxClear`, `RawCanRxClear`, `RawCanAddFilter`,
  `RawCanRemoveFilter`, `RawCanAddPeriodic`, `RawCanRemovePeriodic`.
* ISO-TP: `Iso15765Init`, `Iso15765Start`, `Iso15765Stop`,
  `Iso15765Get`, `Iso15765Set`, `Iso15765AddFilter`,
  `Iso15765RemoveFilter`, `Iso15765AddPeriodic`,
  `Iso15765RemovePeriodic`.
* K-line/other: `Iso9141*`, `XonXoff*`, `FiveBaudInit`.
* IOCTL-style tests exposed by the diagnostic app: `READ_VBATT`,
  `GET_HW_VERSION`, `WARRANTY_CLOCK_GET`, `TEST_PC_DEVICE_LOOPBACK`,
  `TEST_CAN_MS_CAN_HS_LOOPBACK`, `TEST_CAN_HS_CAN_MS_LOOPBACK`, LED, flash,
  RAM, and K/L-line loopback tests.

Command ID decoding:

* MSVC RTTI/template names encode command IDs. Example:
  `?$UnaryCommand@$0BA@URFQ_RawCanInit...` decodes to command id `10`,
  because MSVC encodes decimal digits as letters where `A=0`, `B=1`, etc.
* Confirmed command IDs are captured in `volvo_p3.dice_commands`.
* Quick listing:

```sh
.venv/bin/python -m volvo_p3.cli list-dice-commands
```

Important recovered command IDs:

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

Next Ghidra targets:

1. Identify the class behind `UsbHardware::Open`, `Read`, and `Write`.
2. Recover the packet envelope used by `CommandThread`, `Write`, and `GetReply`.
3. Map `CreateFileW` calls to read/write endpoint paths. Nearby strings include
   endpoint-open failures after the DiCE path prefix, so the DLL probably opens
   multiple device paths or pipes.
4. Confirm whether macOS can claim the same USB interface with libusb. Current
   PyUSB enumeration says no, while IOKit sees the device.

Recovered wire anchors:

* Outbound command frames begin with `0x1D`.
* The second byte is the command ID.
* `FUN_180027930` decodes reply status bytes into RFC error text.
* `FUN_18001c3a0` prepends the `0x1D` byte and copies the command object
  payload into the outbound frame.
* `FUN_18001cc40` is only a byte-copy helper; the real payload is assembled
  earlier in the command object.
* `FUN_18003a4c0` validates a `RawCanInit` reply frame with command byte `0x10`.
* `FUN_18003a9d0` validates a `RawCanInit` reply frame with command byte `0x11`.
* `FUN_180043750` validates an `Iso15765Init` reply frame with command byte `0x40`.
* `FUN_180043c60` validates an `Iso15765Init` reply frame with command byte `0x41`.
* The Python stub now exposes those anchors in `volvo_p3.dice_wire` and the
  DiCE probe output.

Implementation shape:

* `volvo_p3.protocol`: Volvo diagnostic frames and DTC parsing.
* `volvo_p3.can_transport`: generic `python-can` transport.
* `volvo_p3.dice_usb`: native DiCE USB probing and future transport.

The DiCE transport should eventually implement the same minimal API:

* `send(VolvoFrame) -> None`
* `recv(timeout: float) -> VolvoFrame | None`
* `close() -> None`
