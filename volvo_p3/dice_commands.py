"""Recovered DiCE command identifiers.

The IDs are decoded from MSVC RTTI/template names in ``DiCE32_x64.dll``. The DLL
declares classes such as ``URFQ_RawCanInit`` and ``URFR_RawCanInit`` whose numeric
template argument is the command byte. MSVC encodes multi-letter decimal numbers
in hexadecimal with ``A``..``P`` meaning ``0``..``15``; a single letter ``A``..``J``
is that hex digit alone. ``docs/dice-compatibility.md`` records the evidence chain
for every row below.

Each ``DiceCommand`` carries the evidence tier that produced it:

``decompile``
    A decompiled function body instantiates ``UnaryCommand<ID, ...>`` with this
    byte, which also names the owning class method and both payload structs.
``rtti``
    Name, command byte and wire shape come from a template/RTTI symbol pair only.
``strings``
    RTTI gives no name for the slot; the readable name comes from a debug string
    in the same function (for example ``Iso9141AddPeriodic``).
``store-site``
    Only the ``movb $id`` store site is known; no RTTI symbol names the byte.
``live``
    Confirmed against real DiCE hardware, not from the binary alone.

``direction`` records whether the command carries a request payload (``request``),
returns a reply payload (``response``), does both (``request_response``), or has no
recovered payload shape yet (``unknown``). It is not asserted independently: it
follows from which template arguments the instantiation carries, so
``UnaryCommand`` is ``request_response``, ``VoidUnaryCommand`` is ``request`` and
``UnaryCommandVoid`` is ``response``.

``request_type``/``reply_type`` name the payload structs the firmware template is
instantiated with (``URFQ_*`` for requests, ``URFR_*`` for replies, spelled
``RFQ_*``/``RFR_*`` inside the decompiled template arguments). They are the field
layouts a compatible host has to build and parse; ``None`` means no struct of that
direction exists rather than "not yet recovered".
"""

from __future__ import annotations

from dataclasses import dataclass


DICE_USB_INTERFACE_GUID = "4e8a43b0-259a-43c2-a025-baee43c02571"
DICE_USB_PATH_PREFIX = r"\\?\usb#vid_17aa&pid_d1ce#"


@dataclass(frozen=True)
class DiceCommand:
    command_id: int
    name: str
    category: str
    direction: str
    method: tuple[str, ...] = ()
    source: str = "rtti"
    request_type: str | None = None
    reply_type: str | None = None


DICE_COMMANDS: tuple[DiceCommand, ...] = (
    # Device and system commands. They are not part of a numbered channel block.
    DiceCommand(0x00, "RfcVersion", "system", "response", ("GetRfcVersion",), "decompile", reply_type='RfcVersion'),
    DiceCommand(0x03, "SysHardwareVersion", "system", "response", ("GetHardwareVersion",), "decompile", reply_type='SysHardwareVersion'),
    DiceCommand(0x04, "SysFirmwareVersion", "system", "response", ("ReadVersion",), "decompile", reply_type='SysFirmwareVersion'),
    DiceCommand(0x50, "UnknownDevice50", "system", "unknown", (), "store-site"),
    DiceCommand(0x51, "UnknownDevice51", "system", "unknown", (), "store-site"),
    DiceCommand(0x5E, "WarrantyClockSetOnce", "system", "request_response", ("SetWarrantyClock",), "decompile", request_type='WarrantyClockSetOnce', reply_type='WarrantyClockSetOnce'),
    DiceCommand(0x5F, "WarrantyClockGet", "system", "response", ("GetWarrantyClock",), "decompile", reply_type='WarrantyClockGet'),
    DiceCommand(0x62, "BitSupplyVoltage", "system", "response", ("ReadBatteryVoltage",), "decompile", reply_type='BitSupplyVoltage'),
    DiceCommand(0x68, "BitLed", "system", "request_response", ("RunLedTest",), "decompile", request_type='BitLed', reply_type='BitLed'),
    # RawCAN channel block, base 0x10. Slot +0x06 is absent.
    DiceCommand(0x10, "RawCanInit", "raw_can", "request_response", ("Init",), "decompile", request_type='RawCanInit', reply_type='RawCanInit'),
    DiceCommand(0x11, "RawCanFree", "raw_can", "request", ("Free",), "decompile", request_type='RawCanFree'),
    DiceCommand(0x12, "RawCanStart", "raw_can", "request", ("SimpleStart",), "decompile", request_type='RawCanStart'),
    DiceCommand(0x13, "RawCanStop", "raw_can", "request", ("Stop",), "decompile", request_type='RawCanStop'),
    DiceCommand(0x14, "RawCanSet", "raw_can", "request", ("SetParameter",), "decompile", request_type='RawCanSet'),
    DiceCommand(0x15, "RawCanGet", "raw_can", "request_response", ("GetParameter",), "decompile", request_type='RawCanGet', reply_type='RawCanGet'),
    DiceCommand(0x17, "RawCanTxStatus", "raw_can", "request_response", ("GetDeviceBufferSize",), "decompile", request_type='RawCanTxStatus', reply_type='RawCanTxStatus'),
    DiceCommand(0x18, "RawCanTxClear", "raw_can", "request", ("ClearTxBuffer",), "decompile", request_type='RawCanTxClear'),
    DiceCommand(0x19, "RawCanRxClear", "raw_can", "request", ("ClearRxBuffer",), "decompile", request_type='RawCanRxClear'),
    DiceCommand(0x1A, "RawCanAddFilter", "raw_can", "request_response", ("AddMsgFilter",), "decompile", request_type='RawCanAddFilter', reply_type='RawCanAddFilter'),
    DiceCommand(0x1B, "RawCanRemoveFilter", "raw_can", "request", ("RemoveMsgFilter",), "decompile", request_type='RawCanRemoveFilter'),
    DiceCommand(0x1C, "RawCanAddPeriodic", "raw_can", "request_response", ("AddPeriodicMsg",), "decompile", request_type='RawCanAddPeriodic', reply_type='RawCanAddPeriodic'),
    DiceCommand(0x1D, "RawCanRemovePeriodic", "raw_can", "request", ("RemovePeriodicMsg",), "decompile", request_type='RawCanRemovePeriodic'),
    # XON/XOFF channel block, base 0x20. Slot +0x07 is absent; +0x0c is named from strings.
    DiceCommand(0x20, "XonXoffInit", "xon_xoff", "request_response", ("Init",), "decompile", request_type='XonXoffInit', reply_type='XonXoffInit'),
    DiceCommand(0x21, "XonXoffFree", "xon_xoff", "request", ("Free",), "decompile", request_type='XonXoffFree'),
    DiceCommand(0x22, "XonXoffStart", "xon_xoff", "request", ("SimpleStart",), "decompile", request_type='XonXoffStart'),
    DiceCommand(0x23, "XonXoffStop", "xon_xoff", "request", ("Stop",), "decompile", request_type='XonXoffStop'),
    DiceCommand(0x24, "XonXoffSet", "xon_xoff", "request", ("SetParameter",), "decompile", request_type='XonXoffSet'),
    DiceCommand(0x25, "XonXoffGet", "xon_xoff", "request_response", ("GetParameter",), "decompile", request_type='XonXoffGet', reply_type='XonXoffGet'),
    DiceCommand(0x26, "XonXoffTxStatus", "xon_xoff", "request_response", ("GetDeviceBufferSize",), "decompile", request_type='XonXoffTxStatus', reply_type='XonXoffTxStatus'),
    DiceCommand(0x28, "XonXoffTxClear", "xon_xoff", "request", ("ClearTxBuffer",), "decompile", request_type='XonXoffTxClear'),
    DiceCommand(0x29, "XonXoffRxClear", "xon_xoff", "request", ("ClearRxBuffer",), "decompile", request_type='XonXoffRxClear'),
    DiceCommand(0x2A, "XonXoffAddFilter", "xon_xoff", "request_response", ("AddMsgFilter", "SetupCanXOnXOffFilter"), "decompile", request_type='XonXoffAddFilter', reply_type='XonXoffAddFilter'),
    DiceCommand(0x2B, "XonXoffRemoveFilter", "xon_xoff", "request", ("RemoveMsgFilter",), "decompile", request_type='XonXoffRemoveFilter'),
    DiceCommand(0x2C, "XonXoffAddPeriodic", "xon_xoff", "request_response", ("AddPeriodicMsg",), "strings"),
    DiceCommand(0x2D, "XonXoffRemovePeriodic", "xon_xoff", "request", ("RemovePeriodicMsg",), "decompile", request_type='XonXoffRemovePeriodic'),
    # ISO 9141 channel block, base 0x30. GetParameter is followed by FastInit (+0x06),
    # then GetDeviceBufferSize (+0x07), then a gap at +0x08. +0x0c is named from strings.
    DiceCommand(0x30, "Iso9141Init", "iso9141", "request_response", ("Init",), "decompile", request_type='Iso9141Init', reply_type='Iso9141Init'),
    DiceCommand(0x31, "Iso9141Free", "iso9141", "request", ("Free",), "decompile", request_type='Iso9141Free'),
    DiceCommand(0x32, "Iso9141Start", "iso9141", "request", ("SimpleStart",), "decompile", request_type='Iso9141Start'),
    DiceCommand(0x33, "Iso9141Stop", "iso9141", "request", ("Stop",), "decompile", request_type='Iso9141Stop'),
    DiceCommand(0x34, "Iso9141Set", "iso9141", "request", ("SetParameter",), "decompile", request_type='Iso9141Set'),
    DiceCommand(0x35, "Iso9141Get", "iso9141", "request_response", ("GetParameter",), "decompile", request_type='Iso9141Get', reply_type='Iso9141Get'),
    DiceCommand(0x36, "Iso9141FastInit", "iso9141", "request_response", ("FastInit",), "strings"),
    DiceCommand(0x37, "Iso9141TxStatus", "iso9141", "request_response", ("GetDeviceBufferSize",), "decompile", request_type='Iso9141TxStatus', reply_type='Iso9141TxStatus'),
    DiceCommand(0x39, "Iso9141TxClear", "iso9141", "request", ("ClearTxBuffer",), "decompile", request_type='Iso9141TxClear'),
    DiceCommand(0x3A, "Iso9141RxClear", "iso9141", "request", ("ClearRxBuffer",), "decompile", request_type='Iso9141RxClear'),
    DiceCommand(0x3B, "Iso9141AddFilter", "iso9141", "request_response", ("AddMsgFilter",), "decompile", request_type='Iso9141AddFilter', reply_type='Iso9141AddFilter'),
    DiceCommand(0x3C, "Iso9141RemoveFilter", "iso9141", "request", ("RemoveMsgFilter",), "decompile", request_type='Iso9141RemoveFilter'),
    DiceCommand(0x3D, "Iso9141AddPeriodic", "iso9141", "request_response", ("AddPeriodicMsg",), "strings"),
    DiceCommand(0x3E, "Iso9141RemovePeriodic", "iso9141", "request", ("RemovePeriodicMsg",), "decompile", request_type='Iso9141RemovePeriodic'),
    # ISO 15765 channel block, base 0x40. Slot +0x07 is absent; +0x0c is named from strings.
    DiceCommand(0x40, "Iso15765Init", "iso15765", "request_response", ("Init",), "decompile", request_type='Iso15765Init', reply_type='Iso15765Init'),
    DiceCommand(0x41, "Iso15765Free", "iso15765", "request", ("Free",), "decompile", request_type='Iso15765Free'),
    DiceCommand(0x42, "Iso15765Start", "iso15765", "request", ("SimpleStart",), "decompile", request_type='Iso15765Start'),
    DiceCommand(0x43, "Iso15765Stop", "iso15765", "request", ("Stop",), "decompile", request_type='Iso15765Stop'),
    DiceCommand(0x44, "Iso15765Set", "iso15765", "request", ("SetParameter",), "decompile", request_type='Iso15765Set'),
    DiceCommand(0x45, "Iso15765Get", "iso15765", "request_response", ("GetParameter",), "decompile", request_type='Iso15765Get', reply_type='Iso15765Get'),
    DiceCommand(0x46, "Iso15765TxStatus", "iso15765", "request_response", ("GetDeviceBufferSize",), "decompile", request_type='Iso15765TxStatus', reply_type='Iso15765TxStatus'),
    DiceCommand(0x48, "Iso15765TxClear", "iso15765", "request", ("ClearTxBuffer",), "decompile", request_type='Iso15765TxClear'),
    DiceCommand(0x49, "Iso15765RxClear", "iso15765", "request", ("ClearRxBuffer",), "decompile", request_type='Iso15765RxClear'),
    DiceCommand(0x4A, "Iso15765AddFilter", "iso15765", "request_response", ("AddMsgFilter",), "decompile", request_type='Iso15765AddFilter', reply_type='Iso15765AddFilter'),
    DiceCommand(0x4B, "Iso15765RemoveFilter", "iso15765", "request", ("RemoveMsgFilter",), "decompile", request_type='Iso15765RemoveFilter'),
    DiceCommand(0x4C, "Iso15765AddPeriodic", "iso15765", "request_response", ("AddPeriodicMsg",), "strings"),
    DiceCommand(0x4D, "Iso15765RemovePeriodic", "iso15765", "request", ("RemovePeriodicMsg",), "decompile", request_type='Iso15765RemovePeriodic'),
    # CAN input/output block. 0x80 and 0x81 are the only live-verified commands.
    DiceCommand(0x80, "RawCanWriteMsgs", "raw_can", "request", ("WriteMsgs",), "live"),
    DiceCommand(0x81, "RawCanEvent", "raw_can", "response", (), "live"),
    DiceCommand(0x82, "UnknownBulk82", "raw_can", "request", (), "store-site"),
    DiceCommand(0x84, "UnknownBulk84", "raw_can", "request", (), "store-site"),
    DiceCommand(0x86, "Iso9141FiveBaudInit", "iso9141", "request_response", ("FiveBaudInit",), "decompile", request_type='Iso9141FiveBaudInit', reply_type='Iso9141FiveBaudInit'),
    DiceCommand(0x87, "UnknownBulk87", "raw_can", "request", (), "store-site"),
)


COMMANDS_BY_ID = {command.command_id: command for command in DICE_COMMANDS}
COMMANDS_BY_NAME = {command.name: command for command in DICE_COMMANDS}

CATEGORIES: tuple[str, ...] = ("system", "raw_can", "xon_xoff", "iso9141", "iso15765")


def iter_commands(category: str | None = None) -> list[DiceCommand]:
    commands = list(DICE_COMMANDS)
    if category is not None:
        commands = [command for command in commands if command.category == category]
    return sorted(commands, key=lambda command: command.command_id)
