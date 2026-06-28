"""Recovered DiCE command identifiers.

These IDs are decoded from MSVC RTTI/template names in DiCE32.dll. The DLL uses
classes named like ``URFQ_RawCanInit`` and ``URFR_RawCanInit`` with a numeric
template argument. MSVC encodes decimal digits as letters in those mangled
names, where ``A`` is 0, ``B`` is 1, and so on.
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


DICE_COMMANDS: tuple[DiceCommand, ...] = (
    DiceCommand(0, "RfcVersion", "system", "response"),
    DiceCommand(2, "SysHardwareVersion", "system", "response"),
    DiceCommand(3, "SysFirmwareVersion", "system", "response"),
    DiceCommand(10, "RawCanInit", "raw_can", "request_response"),
    DiceCommand(11, "RawCanFree", "raw_can", "request"),
    DiceCommand(12, "RawCanStart", "raw_can", "request"),
    DiceCommand(13, "RawCanStop", "raw_can", "request"),
    DiceCommand(14, "RawCanSet", "raw_can", "request"),
    DiceCommand(15, "RawCanGet", "raw_can", "request_response"),
    DiceCommand(17, "RawCanTxStatus", "raw_can", "request_response"),
    DiceCommand(18, "RawCanTxClear", "raw_can", "request"),
    DiceCommand(19, "RawCanRxClear", "raw_can", "request"),
    DiceCommand(20, "RawCanAddFilter", "raw_can", "request_response"),
    DiceCommand(21, "RawCanRemoveFilter", "raw_can", "request"),
    DiceCommand(22, "RawCanAddPeriodic", "raw_can", "request_response"),
    DiceCommand(23, "RawCanRemovePeriodic", "raw_can", "request"),
    DiceCommand(40, "Iso15765Init", "iso15765", "request_response"),
    DiceCommand(41, "Iso15765Free", "iso15765", "request"),
    DiceCommand(42, "Iso15765Start", "iso15765", "request"),
    DiceCommand(43, "Iso15765Stop", "iso15765", "request"),
    DiceCommand(44, "Iso15765Set", "iso15765", "request"),
    DiceCommand(45, "Iso15765Get", "iso15765", "request_response"),
    DiceCommand(46, "Iso15765TxStatus", "iso15765", "request_response"),
    DiceCommand(48, "Iso15765TxClear", "iso15765", "request"),
    DiceCommand(49, "Iso15765RxClear", "iso15765", "request"),
    DiceCommand(50, "Iso15765AddFilter", "iso15765", "request_response"),
    DiceCommand(51, "Iso15765RemoveFilter", "iso15765", "request"),
    DiceCommand(52, "Iso15765AddPeriodic", "iso15765", "request"),
    DiceCommand(53, "Iso15765RemovePeriodic", "iso15765", "request"),
    DiceCommand(54, "WarrantyClockSetOnce", "system", "request_response"),
    DiceCommand(55, "WarrantyClockGet", "system", "response"),
    DiceCommand(62, "BitSupplyVoltage", "system", "response"),
    DiceCommand(68, "BitLed", "system", "request_response"),
)


COMMANDS_BY_ID = {command.command_id: command for command in DICE_COMMANDS}
COMMANDS_BY_NAME = {command.name: command for command in DICE_COMMANDS}


def iter_commands(category: str | None = None) -> list[DiceCommand]:
    commands = list(DICE_COMMANDS)
    if category is not None:
        commands = [command for command in commands if command.category == category]
    return sorted(commands, key=lambda command: command.command_id)
