"""Recovered DiCE command identifiers.

These IDs are decoded from MSVC RTTI/template names in DiCE32.dll. The DLL uses
classes named like ``URFQ_RawCanInit`` and ``URFR_RawCanInit`` with a numeric
template argument. MSVC encodes multi-letter numbers in hexadecimal, with A..P meaning 0..15.
A single digit 0..9 instead represents the value 1..10.
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
    DiceCommand(0x00, "RfcVersion", "system", "response"),
    DiceCommand(0x03, "SysHardwareVersion", "system", "response"),
    DiceCommand(0x04, "SysFirmwareVersion", "system", "response"),
    DiceCommand(0x10, "RawCanInit", "raw_can", "request_response"),
    DiceCommand(0x11, "RawCanFree", "raw_can", "request"),
    DiceCommand(0x12, "RawCanStart", "raw_can", "request"),
    DiceCommand(0x13, "RawCanStop", "raw_can", "request"),
    DiceCommand(0x14, "RawCanSet", "raw_can", "request"),
    DiceCommand(0x15, "RawCanGet", "raw_can", "request_response"),
    DiceCommand(0x17, "RawCanTxStatus", "raw_can", "request_response"),
    DiceCommand(0x18, "RawCanTxClear", "raw_can", "request"),
    DiceCommand(0x19, "RawCanRxClear", "raw_can", "request"),
    DiceCommand(0x1A, "RawCanAddFilter", "raw_can", "request_response"),
    DiceCommand(0x1B, "RawCanRemoveFilter", "raw_can", "request"),
    DiceCommand(0x1C, "RawCanAddPeriodic", "raw_can", "request_response"),
    DiceCommand(0x1D, "RawCanRemovePeriodic", "raw_can", "request"),
    DiceCommand(0x40, "Iso15765Init", "iso15765", "request_response"),
    DiceCommand(0x41, "Iso15765Free", "iso15765", "request"),
    DiceCommand(0x42, "Iso15765Start", "iso15765", "request"),
    DiceCommand(0x43, "Iso15765Stop", "iso15765", "request"),
    DiceCommand(0x44, "Iso15765Set", "iso15765", "request"),
    DiceCommand(0x45, "Iso15765Get", "iso15765", "request_response"),
    DiceCommand(0x46, "Iso15765TxStatus", "iso15765", "request_response"),
    DiceCommand(0x48, "Iso15765TxClear", "iso15765", "request"),
    DiceCommand(0x49, "Iso15765RxClear", "iso15765", "request"),
    DiceCommand(0x4A, "Iso15765AddFilter", "iso15765", "request_response"),
    DiceCommand(0x4B, "Iso15765RemoveFilter", "iso15765", "request"),
    DiceCommand(0x4D, "Iso15765RemovePeriodic", "iso15765", "request"),
    DiceCommand(0x5E, "WarrantyClockSetOnce", "system", "request_response"),
    DiceCommand(0x5F, "WarrantyClockGet", "system", "response"),
    DiceCommand(0x62, "BitSupplyVoltage", "system", "response"),
    DiceCommand(0x68, "BitLed", "system", "request_response"),
)


COMMANDS_BY_ID = {command.command_id: command for command in DICE_COMMANDS}
COMMANDS_BY_NAME = {command.name: command for command in DICE_COMMANDS}


def iter_commands(category: str | None = None) -> list[DiceCommand]:
    commands = list(DICE_COMMANDS)
    if category is not None:
        commands = [command for command in commands if command.category == category]
    return sorted(commands, key=lambda command: command.command_id)
