"""Native USB probing and future transport for Volvo DiCE.

DiCE is visible on macOS as a vendor-specific USB device, not as a serial
adapter and not as a python-can backend. This module keeps the native USB work
separate from the Volvo diagnostic protocol code.
"""

from __future__ import annotations

import json
import platform
import subprocess
from dataclasses import dataclass
from typing import Any

from .dice_commands import DICE_USB_INTERFACE_GUID, DICE_USB_PATH_PREFIX
from .dice_wire import DICE_COMMAND_PREFIX, DICE_REPLY_STATUS_MESSAGES

DICE_VENDOR_ID = 0x17AA
DICE_PRODUCT_ID = 0xD1CE


@dataclass(frozen=True)
class DiceUsbDevice:
    name: str
    vendor: str
    vendor_id: int | None
    product_id: int | None
    serial: str
    location_id: str
    source: str

    def format_lines(self) -> list[str]:
        vendor_id = "unknown" if self.vendor_id is None else f"0x{self.vendor_id:04X}"
        product_id = "unknown" if self.product_id is None else f"0x{self.product_id:04X}"
        return [
            f"name:        {self.name}",
            f"vendor:      {self.vendor}",
            f"vendor_id:   {vendor_id}",
            f"product_id:  {product_id}",
            f"serial:      {self.serial}",
            f"location_id: {self.location_id}",
            f"source:      {self.source}",
            f"win_guid:    {DICE_USB_INTERFACE_GUID}",
            f"win_prefix:  {DICE_USB_PATH_PREFIX}",
            f"wire_prefix:  0x{DICE_COMMAND_PREFIX:02X}",
            f"reply_status: {len(DICE_REPLY_STATUS_MESSAGES)} known codes",
        ]


def parse_hex_id(value: Any) -> int | None:
    if value is None:
        return None
    text = str(value).split()[0]
    try:
        return int(text, 0)
    except ValueError:
        return None


def iter_system_profiler_usb_items(items: list[dict[str, Any]]):
    for item in items:
        yield item
        for child in item.get("_items", []):
            yield from iter_system_profiler_usb_items([child])


def find_dice_system_profiler() -> list[DiceUsbDevice]:
    if platform.system() != "Darwin":
        return []

    result = subprocess.run(
        ["system_profiler", "-json", "SPUSBDataType"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "system_profiler failed")

    profile = json.loads(result.stdout)
    devices: list[DiceUsbDevice] = []
    for item in iter_system_profiler_usb_items(profile.get("SPUSBDataType", [])):
        vendor_id = parse_hex_id(item.get("vendor_id"))
        product_id = parse_hex_id(item.get("product_id"))
        name = item.get("_name", "")
        if "dice" not in name.lower() and vendor_id != DICE_VENDOR_ID and product_id != DICE_PRODUCT_ID:
            continue
        devices.append(
            DiceUsbDevice(
                name=name or "unknown",
                vendor=item.get("manufacturer", item.get("vendor_id", "unknown")),
                vendor_id=vendor_id,
                product_id=product_id,
                serial=item.get("serial_num", "unknown"),
                location_id=item.get("location_id", "unknown"),
                source="system_profiler",
            )
        )
    return devices


def find_dice_pyusb() -> list[DiceUsbDevice]:
    try:
        import usb.core
    except ImportError as exc:
        raise RuntimeError("pyusb is not installed") from exc

    devices: list[DiceUsbDevice] = []
    for dev in usb.core.find(find_all=True, idVendor=DICE_VENDOR_ID, idProduct=DICE_PRODUCT_ID):
        serial = "unknown"
        manufacturer = "unknown"
        product = "DiCE Automotive Adapter"
        try:
            serial = usb.util.get_string(dev, dev.iSerialNumber) or serial
            manufacturer = usb.util.get_string(dev, dev.iManufacturer) or manufacturer
            product = usb.util.get_string(dev, dev.iProduct) or product
        except Exception:
            pass
        devices.append(
            DiceUsbDevice(
                name=product,
                vendor=manufacturer,
                vendor_id=dev.idVendor,
                product_id=dev.idProduct,
                serial=serial,
                location_id=f"bus={getattr(dev, 'bus', 'unknown')} address={getattr(dev, 'address', 'unknown')}",
                source="pyusb",
            )
        )
    return devices


def get_pyusb_backend_status() -> str:
    try:
        import usb.backend.libusb1
    except ImportError:
        return "pyusb/libusb1 import failed"
    backend = usb.backend.libusb1.get_backend()
    return "available" if backend is not None else "not available"


class DiceUsbTransport:
    """Placeholder for the native DiCE USB transport.

    The public shape mirrors the CAN transport enough for the diagnostic CLI to
    use it later. The endpoint setup and command protocol need to be filled in
    from VIDA/J2534 reverse engineering.
    """

    def __init__(self):
        self.device: DiceUsbDevice | None = None

    def open(self) -> None:
        devices = find_dice_pyusb()
        if not devices:
            raise RuntimeError("DiCE is not accessible through PyUSB yet")
        self.device = devices[0]
        raise NotImplementedError("native DiCE USB endpoint setup is not implemented yet")

    def close(self) -> None:
        self.device = None
