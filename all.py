#!/usr/bin/env python3
"""
Unified Ptown status check — runs nuheat, hottub, and nest in parallel
and prints a single combined report with a timestamp header.

Each sub-script is run as its own subprocess so they stay fully
independent; one failing won't block the others. The three tasks are
I/O-bound on vendor cloud APIs, so running them concurrently makes
the wall-clock time ≈ the slowest single check rather than the sum.

Usage (via wrapper):
    ./ptown all
    ./ptown all --raw       # passes --raw through to each sub-script

Exit code: 0 if every sub-script exited cleanly, otherwise the first
non-zero exit code we saw.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent

# Order matters — this is the order the sections print in.
SCRIPTS = ["nuheat", "hottub", "nest"]

PER_SCRIPT_TIMEOUT = 60  # seconds


def _run_one(name: str, extra_args: list[str]) -> tuple[str, int, str, str]:
    """Run {name}.py in the same Python interpreter and capture output."""
    script = HERE / f"{name}.py"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), *extra_args],
            capture_output=True,
            text=True,
            timeout=PER_SCRIPT_TIMEOUT,
        )
        return name, proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired:
        return name, 124, "", f"{name}: timed out after {PER_SCRIPT_TIMEOUT}s\n"
    except FileNotFoundError:
        return name, 127, "", f"{name}: script not found at {script}\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Unified Ptown status check")
    parser.add_argument(
        "--raw", action="store_true",
        help="pass --raw through to each sub-script",
    )
    args = parser.parse_args()

    extra_args = ["--raw"] if args.raw else []

    print(f"Ptown status — {time.strftime('%a %b %d %H:%M:%S %Z %Y')}")
    print()

    # Run all three in parallel. ThreadPoolExecutor.map preserves order.
    with ThreadPoolExecutor(max_workers=len(SCRIPTS)) as ex:
        results = list(ex.map(lambda n: _run_one(n, extra_args), SCRIPTS))

    exit_code = 0
    for name, rc, stdout, stderr in results:
        if stdout:
            sys.stdout.write(stdout)
            if not stdout.endswith("\n"):
                sys.stdout.write("\n")
        if rc != 0:
            if exit_code == 0:
                exit_code = rc
            sys.stderr.write(f"[{name}] failed (rc={rc})\n")
            if stderr:
                sys.stderr.write(stderr)
                if not stderr.endswith("\n"):
                    sys.stderr.write("\n")
        print()

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
