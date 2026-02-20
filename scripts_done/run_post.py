#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, List
from zoneinfo import ZoneInfo

# Allow running from scripts_done/ directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import CFG

SCRIPTS_PARTS_DIR = REPO_ROOT / "scripts_parts"
SCRIPT_SCHEDULE = SCRIPTS_PARTS_DIR / (CFG.SCRIPT_SCHEDULE_NAME or "post_upload.py")
TIMEOUT_SCHEDULE = CFG.TIMEOUT_SCHEDULE
RUNS_POST_DEFAULT = int(getattr(CFG, "RUNS_DEFAULT", 8) or 8)
if RUNS_POST_DEFAULT <= 0:
    RUNS_POST_DEFAULT = 8


def now_jst_str() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d-%H:%M:%S")


def connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(str(db_path), timeout=60)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA busy_timeout=60000;")
    return con


def ensure_history_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS historyRun (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          started_at TEXT NOT NULL,
          ended_at TEXT NOT NULL,
          duration_sec REAL NOT NULL,
          steps TEXT NOT NULL,
          runs INTEGER NOT NULL,
          until_tag TEXT NOT NULL,
          list_added INTEGER NOT NULL DEFAULT 0,
          pipeline_ok INTEGER NOT NULL DEFAULT 0,
          pipeline_err INTEGER NOT NULL DEFAULT 0,
          schedule_ok INTEGER NOT NULL DEFAULT 0,
          db_path TEXT NOT NULL,
          base_output_root TEXT NOT NULL,
          last_error TEXT
        )
        """
    )
    con.commit()


def record_post_history(
    started_at: str,
    ended_at: str,
    duration_sec: float,
    runs: int,
    schedule_ok: int,
    last_error: str,
) -> None:
    db_path = CFG.DB_PATH
    if not db_path or not db_path.exists():
        return
    try:
        with connect(db_path) as con:
            ensure_history_table(con)
            con.execute(
                """
                INSERT INTO historyRun (
                  started_at, ended_at, duration_sec, steps, runs, until_tag,
                  list_added, pipeline_ok, pipeline_err, schedule_ok,
                  db_path, base_output_root, last_error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    started_at,
                    ended_at,
                    float(duration_sec),
                    "schedule",
                    int(runs),
                    "schedule",
                    0,
                    0,
                    0,
                    int(schedule_ok),
                    str(CFG.DB_PATH),
                    str(CFG.BASE_OUTPUT_ROOT),
                    last_error or None,
                ),
            )
            con.commit()
    except Exception:
        pass


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
    started_at = now_jst_str()
    t0 = time.time()
    schedule_ok = 0
    last_error = ""

    print(f"[CONF] SCRIPT_SCHEDULE : {SCRIPT_SCHEDULE}")
    print(f"[CONF] TIMEOUT_SCHEDULE: {TIMEOUT_SCHEDULE}")
    print(f"[CONF] RUNS_POST_DEFAULT: {RUNS_POST_DEFAULT}")

    try:
        run_script_realtime(
            SCRIPT_SCHEDULE,
            TIMEOUT_SCHEDULE,
            extra_args=[
                "--limit",
                str(RUNS_POST_DEFAULT),
                "--first_schedule_mode",
                "delay",
                "--start_delay_min",
                "0",
                "--interval_min",
                "30",
                "--blocked_hour_ranges",
                "25-29,11-15",
                "--align_first_to_hour",
            ],
        )
        schedule_ok = 1
        return_code = 0
    except Exception as e:
        last_error = str(e)
        print(f"[ERR] {last_error}", file=sys.stderr)
        return_code = 1
    finally:
        record_post_history(
            started_at=started_at,
            ended_at=now_jst_str(),
            duration_sec=time.time() - t0,
            runs=RUNS_POST_DEFAULT,
            schedule_ok=schedule_ok,
            last_error=last_error,
        )

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
