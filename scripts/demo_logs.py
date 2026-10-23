#!/usr/bin/env python3
"""Create bounded synthetic logs for the viewer, then append until interrupted.

Run as an ordinary user. This producer only writes its own generated files under
var/demo; it never reads the configured real log, downloads, or remote systems.
The launcher manages its process/PID; --once only initializes the sample files.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import signal
import sys
import threading

PROJECT_ROOT = Path(__file__).resolve().parent.parent
HEADER = "# AstroLOG synthetic demo | generated example, not a real simulation or transfer"
FILES = ("simulation/run.log", "diagnostics/cpu.log", "diagnostics/events.log")


def level_for(step: int) -> str:
    for divisor, level in ((97, "ERROR"), (23, "WARN"), (43, "SUCCESS"), (7, "DEBUG")):
        if step % divisor == 0:
            return level
    return "INFO"


def example_line(relative: str, step: int, timestamp: datetime) -> str:
    stamp = timestamp.isoformat(timespec="seconds").replace("+00:00", "Z")
    level = level_for(step)
    if relative == "simulation/run.log":
        scale = 0.0078125 + (step % 50000) * 0.00001
        if step % 11 == 0:
            return f"{stamp} [{level}] [DEMO] Tree walk complete; synthetic particles={4096 + step % 1000} ranks=8"
        if step % 17 == 0:
            return f"{stamp} [{level}] [DEMO] Checkpoint example written; continuing the generated workload"
        return (f"{stamp} [{level}] [DEMO] Step {step:07d}, Time: {scale:.7f}, "
                f"Redshift: {1 / scale - 1:.5f}, Systemstep: 0.0000100")
    if relative == "diagnostics/cpu.log":
        wall = 0.72 + 0.18 * math.sin(step / 19)
        memory = 3020 + 80 * math.cos(step / 13)
        return (f"{stamp} {level} synthetic=true step={step} ranks=8 wall_s={wall:.4f} "
                f"gravity_s={wall * .46:.4f} hydro_s={wall * .31:.4f} "
                f"sidm_s={wall * .08:.4f} memory_MiB={memory:.2f}")
    if step % 3 == 0:
        return json.dumps({"timestamp": stamp, "level": level, "synthetic": True,
                           "event": "example-heartbeat", "sequence": step,
                           "message": "合成示例 · viewer connection healthy"}, ensure_ascii=False)
    if step % 3 == 1:
        return f"{stamp} [{level}] [DEMO] Worker example completed batch={step}; next batch queued"
    return f"demo event #{step}: an ordinary unstructured log line; status={level.lower()}"


def safe_path(root: Path, relative: str) -> Path:
    path = root / relative
    if path.is_symlink() or not path.resolve().is_relative_to(root):
        raise ValueError("Synthetic log path must remain inside its demo directory")
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def validate_owned(path: Path) -> bool:
    if not path.exists():
        return False
    with path.open("r", encoding="utf-8") as source:
        if source.readline().rstrip("\n") != HEADER:
            raise ValueError(f"Refusing to modify a file that is not a generated demo: {path.name}")
    return True


def initialize(root: Path, seed_lines: int, interval: float) -> dict[str, Path]:
    root.mkdir(parents=True, exist_ok=True)
    result = {}
    start = datetime.now(timezone.utc) - timedelta(seconds=seed_lines * interval)
    for relative in FILES:
        path = safe_path(root, relative)
        if not validate_owned(path):
            with path.open("x", encoding="utf-8") as output:
                output.write(HEADER + "\n")
                for step in range(1, seed_lines + 1):
                    output.write(example_line(relative, step, start + timedelta(seconds=step * interval)) + "\n")
        result[relative] = path
    return result


def append(path: Path, text: str, max_bytes: int) -> None:
    record = (text + "\n").encode("utf-8")
    if path.stat().st_size + len(record) > max_bytes:
        archive = path.with_name(path.name + ".1")
        if archive.is_symlink():
            raise ValueError("Refusing to replace a symlink in the demo archive")
        path.replace(archive)
        with path.open("x", encoding="utf-8") as output:
            output.write(HEADER + "\n")
    with path.open("ab") as output:
        output.write(record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=PROJECT_ROOT / "var" / "demo")
    parser.add_argument("--interval", type=float, default=3.0)
    parser.add_argument("--seed-lines", type=int, default=1200)
    parser.add_argument("--max-bytes", type=int, default=1024 * 1024,
                        help="Rotate each generated file; keep one backup (default 1 MiB)")
    parser.add_argument("--once", action="store_true", help="Initialize fixtures and exit")
    args = parser.parse_args()
    if not 0.5 <= args.interval <= 300:
        parser.error("--interval must be between 0.5 and 300 seconds")
    if not 1 <= args.seed_lines <= 3000:
        parser.error("--seed-lines must be between 1 and 3000")
    if not 512 * 1024 <= args.max_bytes <= 2 * 1024 * 1024:
        parser.error("--max-bytes must be between 512 KiB and 2 MiB")
    root = args.root.expanduser().resolve()
    try:
        paths = initialize(root, args.seed_lines, args.interval)
        if args.once:
            print("Initialized three synthetic demo logs.", flush=True)
            return
        stopped = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda _signum, _frame: stopped.set())
        print("Synthetic demo producer started; three generated logs, bounded rotation enabled.", flush=True)
        step = args.seed_lines
        while not stopped.wait(args.interval):
            step += 1
            timestamp = datetime.now(timezone.utc)
            for relative, path in paths.items():
                safe_path(root, relative)
                append(path, example_line(relative, step, timestamp), args.max_bytes)
        print("Synthetic demo producer stopped.", flush=True)
    except (OSError, ValueError) as exc:
        print(f"Demo producer: {exc}", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
