"""Module entrypoint: launches GUI when invoked with no arguments, or runs CLI."""
from __future__ import annotations

import sys

from .cli import run_cli


def main() -> int:
    if len(sys.argv) <= 1:
        return run_cli(["--gui"])
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
