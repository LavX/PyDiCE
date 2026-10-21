"""Pack a diagnostic catalog dump directory into one archive.

The archive keeps the dump layout at its root: ``catalog.json`` sits at the
top level and ``raw/`` and ``scripts/`` are children. The exporter and
``tools/pack_diag_dump.py`` both call :func:`pack_dump`, so a freshly written
dump and an existing dump are packed by the same code.

Three container formats are supported, selected by the output suffix:

* ``.tar.gz``: gzip-compressed tar
* ``.tar.xz``: xz-compressed tar
* ``.zip``: deflate-compressed zip

The packer refuses an output path inside the dump directory, so it cannot
archive the archive it is writing. Symbolic links are refused rather than
followed, because a dump produced by the exporter contains regular files only.
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Union

ARCHIVE_SUFFIXES = (".tar.gz", ".tar.xz", ".zip")

_ARCHIVE_FORMATS = {
    ".tar.gz": "tar.gz",
    ".tar.xz": "tar.xz",
    ".zip": "zip",
}


class DiagArchiveError(ValueError):
    """Raised when a dump directory or archive path cannot be packed."""


@dataclass(frozen=True)
class ArchiveResult:
    """Sizes and counts for one completed archive."""

    output: Path
    input_bytes: int
    output_bytes: int
    file_count: int

    @property
    def ratio(self) -> float:
        """Output size divided by input size, or zero for an empty input."""

        if not self.input_bytes:
            return 0.0
        return self.output_bytes / self.input_bytes

    @property
    def savings(self) -> float:
        """Fraction of the input size removed, or zero for an empty input."""

        return max(0.0, 1.0 - self.ratio)


def archive_format(path: Union[str, os.PathLike[str]]) -> str:
    """Return the archive format selected by a path suffix.

    Matching is case-insensitive. An unsupported suffix raises
    :class:`DiagArchiveError` with the accepted suffixes in the message.
    """

    text = os.fspath(path)
    lower = text.lower()
    for suffix, name in _ARCHIVE_FORMATS.items():
        if lower.endswith(suffix):
            return name
    raise DiagArchiveError(
        "unsupported archive suffix for {!r}; expected .tar.gz, .tar.xz "
        "or .zip".format(text)
    )


def pack_dump(
    directory: Union[str, os.PathLike[str]],
    output: Union[str, os.PathLike[str]],
) -> ArchiveResult:
    """Pack a dump directory into a single archive.

    The directory must exist and hold ``catalog.json`` at its root. The
    archive contains the directory contents, not the directory itself, so
    ``catalog.json`` is always a root member. The returned result reports the
    summed input file sizes and the final archive size.

    ``DiagArchiveError`` covers a missing or invalid dump, an unsupported
    output suffix, an output inside the dump, and symbolic links in the dump.
    File-system errors propagate unchanged.
    """

    root = Path(directory)
    fmt = archive_format(output)
    target = Path(output)

    if not root.exists():
        raise DiagArchiveError("dump directory not found: {}".format(root))
    if not root.is_dir():
        raise DiagArchiveError("dump path is not a directory: {}".format(root))
    catalog = root / "catalog.json"
    if not catalog.is_file():
        raise DiagArchiveError(
            "dump directory has no catalog.json at its root: {}".format(root)
        )

    root_resolved = root.resolve()
    target_resolved = target.resolve()
    if target_resolved == root_resolved or root_resolved in target_resolved.parents:
        raise DiagArchiveError(
            "archive path must be outside the dump directory: {}".format(target)
        )

    entries = list(_iter_entries(root))
    input_bytes = sum(size for _, _, is_dir, size in entries if not is_dir)
    file_count = sum(1 for _, _, is_dir, _ in entries if not is_dir)

    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if fmt == "tar.gz":
            _write_tar(entries, target, "w:gz")
        elif fmt == "tar.xz":
            _write_tar(entries, target, "w:xz")
        else:
            _write_zip(entries, target)
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise

    return ArchiveResult(
        output=target,
        input_bytes=input_bytes,
        output_bytes=target.stat().st_size,
        file_count=file_count,
    )


def _iter_entries(root: Path) -> Iterator[tuple]:
    """Yield ``(path, arcname, is_dir, size)`` for every dump entry."""

    for dirpath, dirnames, filenames in os.walk(str(root)):
        directory = Path(dirpath)
        dirnames.sort()
        filenames.sort()
        relative = directory.relative_to(root)
        if relative.parts:
            yield (directory, relative.as_posix(), True, 0)
        for name in dirnames:
            if (directory / name).is_symlink():
                raise DiagArchiveError(
                    "dump contains a symbolic link, which is not packed: "
                    "{}".format(directory / name)
                )
        for name in filenames:
            path = directory / name
            if path.is_symlink():
                raise DiagArchiveError(
                    "dump contains a symbolic link, which is not packed: "
                    "{}".format(path)
                )
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise DiagArchiveError(
                    "cannot read dump file {}: {}".format(path, exc)
                ) from exc
            yield (path, path.relative_to(root).as_posix(), False, size)


def _write_tar(entries: list, target: Path, mode: str) -> None:
    with tarfile.open(str(target), mode) as archive:
        for path, arcname, _is_dir, _size in entries:
            archive.add(str(path), arcname=arcname, recursive=False)


def _write_zip(entries: list, target: Path) -> None:
    with zipfile.ZipFile(
        str(target), "w", compression=zipfile.ZIP_DEFLATED
    ) as archive:
        for path, arcname, is_dir, _size in entries:
            if is_dir:
                info = zipfile.ZipInfo(arcname + "/")
                info.external_attr = (0o40755 << 16) | 0x10
                archive.writestr(info, b"")
            else:
                archive.write(str(path), arcname=arcname)


def format_bytes(count: int) -> str:
    """Render a byte count as a short binary unit string."""

    value = int(count)
    if value < 1024:
        return "{} B".format(value)
    scaled = float(value)
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        scaled /= 1024.0
        if scaled < 1024.0 or unit == "TiB":
            return "{:.1f} {}".format(scaled, unit)
    return "{} B".format(value)
