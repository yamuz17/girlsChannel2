#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path
from typing import Optional, List

# Allow running from scripts_done/ directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import CFG

SCRIPTS_PARTS_DIR = REPO_ROOT / "scripts_parts"
SCRIPT_SCHEDULE = SCRIPTS_PARTS_DIR / (CFG.SCRIPT_SCHEDULE_NAME or "post_upload.py")
TIMEOUT_SCHEDULE = CFG.TIMEOUT_SCHEDULE


def run_script_realtime(
    script_path: Path, timeout: Optional[int], extra_args: Optional[List[str]] = None
) -> None:
    if not script_path.exists():
        raise FileNotFoundError(f"script not found: {script_path}")

    cmd = [sys.executable, str(script_path)]
    if extra_args:
        cmd.extend(extra_args)

    print("[RUN]", " ".join(cmd))
    start = time.time()

    p = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        universal_newlines=True,
    )

    try:
        assert p.stdout is not None
        for line in p.stdout:
            print(line.rstrip("\n"))
        rc = p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        p.kill()
        raise RuntimeError(f"{script_path.name} timeout")
    finally:
        elapsed = time.time() - start

    if rc != 0:
        raise RuntimeError(f"{script_path.name} failed (exit={rc})")

    print(f"[OK] {script_path.name} finished in {elapsed:.1f}s")


def main() -> int:
    print(f"[CONF] SCRIPT_SCHEDULE : {SCRIPT_SCHEDULE}")
    print(f"[CONF] TIMEOUT_SCHEDULE: {TIMEOUT_SCHEDULE}")

    run_script_realtime(SCRIPT_SCHEDULE, TIMEOUT_SCHEDULE)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
