"""Module entrypoint: launches GUI when invoked with no arguments, or runs CLI."""
from __future__ import annotations

from pathlib import Path
import sys

from msshi_doctor_app.cli import run_cli


def main() -> int:
    console_launcher = getattr(sys, "frozen", False) and Path(sys.executable).stem.lower() == "msshi-doctor-cli"
    if len(sys.argv) <= 1 and not console_launcher:
        return run_cli(["--gui"])
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
