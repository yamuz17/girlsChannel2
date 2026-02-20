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

from core.config import CFG

# =========================================================
# 設定値（このスクリプトで使う運用変数は上部に集約）
# =========================================================

# 投稿処理スクリプト配置
SCRIPTS_PARTS_DIR = REPO_ROOT / "scripts_parts"
SCRIPT_SCHEDULE = SCRIPTS_PARTS_DIR / (CFG.SCRIPT_SCHEDULE_NAME or "post_upload.py")

# タイムアウト設定
TIMEOUT_SCHEDULE = CFG.TIMEOUT_SCHEDULE

# 1回で投稿処理する件数の既定値
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


def _pick_expr(cols: set[str], candidates: List[str], default_expr: str) -> str:
    for c in candidates:
        if c in cols:
            return c
    return default_expr


def ensure_history_table(con: sqlite3.Connection) -> None:
    cur = con.execute("PRAGMA table_info(run_history)")
    rows = cur.fetchall()
    if not rows:
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS run_history (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              steps TEXT NOT NULL,
              start_at TEXT NOT NULL,
              end_at TEXT NOT NULL,
              sec REAL NOT NULL,
              error TEXT
            )
            """
        )
        con.commit()
        return

    cols = {str(r[1]) for r in rows}
    expected = {"id", "steps", "start_at", "end_at", "sec", "error"}
    if cols == expected:
        return

    old = "run_history__old"
    con.execute(f"DROP TABLE IF EXISTS {old}")
    con.execute("ALTER TABLE run_history RENAME TO run_history__old")
    con.execute(
        """
        CREATE TABLE run_history (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          steps TEXT NOT NULL,
          start_at TEXT NOT NULL,
          end_at TEXT NOT NULL,
          sec REAL NOT NULL,
          error TEXT
        )
        """
    )
    old_cols = {str(r[1]) for r in con.execute(f"PRAGMA table_info({old})").fetchall()}
    steps_expr = _pick_expr(old_cols, ["steps"], "'upload_youtube'")
    start_expr = _pick_expr(old_cols, ["start_at", "started_at"], "''")
    end_expr = _pick_expr(old_cols, ["end_at", "ended_at"], "''")
    sec_expr = _pick_expr(old_cols, ["sec", "duration_sec"], "0")
    err_expr = _pick_expr(old_cols, ["error", "last_error"], "NULL")
    con.execute(
        f"""
        INSERT INTO run_history (steps, start_at, end_at, sec, error)
        SELECT {steps_expr},
               {start_expr},
               {end_expr},
               ROUND(COALESCE({sec_expr}, 0), 2),
               {err_expr}
          FROM {old}
        """
    )
    con.execute(f"DROP TABLE {old}")
    con.commit()


def record_post_history(
    start_at: str,
    end_at: str,
    sec: float,
    error: str,
) -> None:
    db_path = CFG.DB_PATH
    if not db_path or not db_path.exists():
        return
    try:
        with connect(db_path) as con:
            ensure_history_table(con)
            con.execute(
                """
                INSERT INTO run_history (steps, start_at, end_at, sec, error)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    "upload_youtube",
                    start_at,
                    end_at,
                    round(max(0.0, float(sec)), 2),
                    error or None,
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
        return_code = 0
    except Exception as e:
        last_error = str(e)
        print(f"[ERR] {last_error}", file=sys.stderr)
        return_code = 1
    finally:
        record_post_history(
            start_at=started_at,
            end_at=now_jst_str(),
            sec=time.time() - t0,
            error=last_error,
        )

    return return_code


if __name__ == "__main__":
    raise SystemExit(main())
