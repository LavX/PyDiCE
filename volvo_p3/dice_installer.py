"""Extraction helpers for the Windows DiCE installer payloads.

SetupDiCE.exe is a small bootstrapper. The useful DiCE binaries are stored as
zlib-compressed RCDATA resources containing MSI packages.
"""

from __future__ import annotations

import shutil
import subprocess
import zlib
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class DiceInstallerPayload:
    resource_id: str
    filename: str


DICE_INSTALLER_PAYLOADS = (
    DiceInstallerPayload("300", "DiceDriver_x86.msi"),
    DiceInstallerPayload("301", "Dice_x86.msi"),
    DiceInstallerPayload("303", "DiceDriver_x64.msi"),
    DiceInstallerPayload("304", "Dice_x64.msi"),
)


def payloads_for_arch(arch: str) -> tuple[DiceInstallerPayload, ...]:
    if arch == "x64":
        return tuple(payload for payload in DICE_INSTALLER_PAYLOADS if payload.filename.endswith("_x64.msi"))
    if arch == "x86":
        return tuple(payload for payload in DICE_INSTALLER_PAYLOADS if payload.filename.endswith("_x86.msi"))
    if arch == "all":
        return DICE_INSTALLER_PAYLOADS
    raise ValueError(f"unsupported arch: {arch}")


def ensure_7z() -> str:
    executable = shutil.which("7z")
    if not executable:
        raise RuntimeError("7z is required to extract SetupDiCE.exe and MSI/CAB payloads")
    return executable


def run_7z(args: list[str]) -> None:
    executable = ensure_7z()
    result = subprocess.run([executable, *args], check=False, capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(detail or f"7z failed with exit code {result.returncode}")


def extract_setup_dice(installer: Path, output_dir: Path, arch: str = "x64", unpack: bool = True) -> list[Path]:
    """Extract DiCE MSI payloads from SetupDiCE.exe.

    Returns the recovered MSI paths. When ``unpack`` is true, each MSI is also
    expanded into a sibling directory named after the MSI stem.
    """

    if not installer.exists():
        raise RuntimeError(f"installer not found: {installer}")

    output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = output_dir / ".rsrc" / "RCDATA"
    payloads = payloads_for_arch(arch)

    resource_args = [f".rsrc/RCDATA/{payload.resource_id}" for payload in payloads]
    run_7z(["x", "-y", str(installer), *resource_args, f"-o{output_dir}"])

    extracted: list[Path] = []
    for payload in payloads:
        raw_path = raw_dir / payload.resource_id
        if not raw_path.exists():
            raise RuntimeError(f"resource {payload.resource_id} was not extracted")
        msi_path = output_dir / payload.filename
        msi_path.write_bytes(zlib.decompress(raw_path.read_bytes()))
        extracted.append(msi_path)

        if unpack:
            unpack_dir = output_dir / msi_path.stem
            unpack_dir.mkdir(parents=True, exist_ok=True)
            run_7z(["x", "-y", str(msi_path), f"-o{unpack_dir}"])
            for cab in unpack_dir.glob("*.cab"):
                cab_dir = output_dir / f"{msi_path.stem}_cab"
                cab_dir.mkdir(parents=True, exist_ok=True)
                run_7z(["x", "-y", str(cab), f"-o{cab_dir}"])

    return extracted
