#!/usr/bin/env python3
"""Pack an existing diagnostic dump directory into a single archive.

The dump layout is kept at the archive root: ``catalog.json`` is a top-level
member and ``raw/`` and ``scripts/`` are its children. The exporter calls the
same ``volvo_p3.diag_archive.pack_dump`` function for fresh dumps, so both
paths produce identical layouts.

Examples::

    python3 tools/pack_diag_dump.py --input data/diag_dump \
        --output data/diag_dump.tar.gz
    python3 tools/pack_diag_dump.py --input data/diag_dump \
        --output data/diag_dump.tar.xz
    python3 tools/pack_diag_dump.py --input data/diag_dump \
        --output data/diag_dump.zip
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Optional, Sequence

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from volvo_p3.diag_archive import (  # noqa: E402
    DiagArchiveError,
    format_bytes,
    pack_dump,
)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pack a diagnostic dump directory into one .tar.gz, .tar.xz or "
            ".zip archive with catalog.json at the archive root."
        )
    )
    parser.add_argument(
        "--input",
        type=Path,
        required=True,
        help="existing dump directory containing catalog.json",
    )
    parser.add_argument(
        "--output",
        type=Path,
        required=True,
        help="archive path; suffix selects .tar.gz, .tar.xz or .zip",
    )
    args = parser.parse_args(argv)

    try:
        result = pack_dump(args.input, args.output)
    except (DiagArchiveError, OSError) as exc:
        print("error: {}".format(exc), file=sys.stderr)
        return 1

    print(
        "Input:  {} ({} bytes, {} files)".format(
            format_bytes(result.input_bytes),
            result.input_bytes,
            result.file_count,
        )
    )
    print(
        "Output: {} ({} bytes)".format(
            format_bytes(result.output_bytes), result.output_bytes
        )
    )
    if result.input_bytes:
        print(
            "Compression ratio: {:.1f}% of input ({:.1f}x smaller)".format(
                result.ratio * 100.0,
                1.0 / result.ratio if result.ratio else float("inf"),
            )
        )
    else:
        print("Compression ratio: input is empty")
    print("Wrote {}".format(result.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
