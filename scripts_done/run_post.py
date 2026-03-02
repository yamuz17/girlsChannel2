#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, List
from zoneinfo import ZoneInfo

# Allow running from scripts_done/ directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core.config import CFG
from core import env_loader

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

# 前回投稿から次回投稿までの最短待機時間（時間）
POST_COOLDOWN_HOURS = float(env_loader.env_float("POST_COOLDOWN_HOURS", 24.0))
if POST_COOLDOWN_HOURS < 0:
    POST_COOLDOWN_HOURS = 0.0

# 監視開始する経過時間（時間）。この値以上で 24h 未満なら5分おきに再判定する
POST_WATCH_START_HOURS = float(env_loader.env_float("POST_WATCH_START_HOURS", 23.0))
if POST_WATCH_START_HOURS < 0:
    POST_WATCH_START_HOURS = 0.0

# 再判定間隔（秒）
POST_RECHECK_SEC = int(env_loader.env_int("POST_RECHECK_SEC", 300))
if POST_RECHECK_SEC <= 0:
    POST_RECHECK_SEC = 300

# 投稿キュー判定に使う設定
POST_UPLOAD_READY_STAGE = int(env_loader.env_int("POST_UPLOAD_READY_STAGE", 60))
POST_UPLOAD_TABLE = CFG.ITEMS_DONE_TABLE or "items_done"


def now_jst_str() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d-%H:%M:%S")


def parse_jst_datetime(raw: str) -> Optional[datetime]:
    s = (raw or "").strip()
    if not s:
        return None

    for fmt in ("%Y/%m/%d-%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return dt.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
        except ValueError:
            pass

    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=ZoneInfo("Asia/Tokyo"))
    return dt.astimezone(ZoneInfo("Asia/Tokyo"))


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


def latest_successful_post_at(db_path: Path) -> Optional[datetime]:
    if not db_path or not db_path.exists():
        return None
    try:
        with connect(db_path) as con:
            row = con.execute(
                """
                SELECT end_at
                  FROM run_history
                 WHERE steps = 'upload_youtube'
                   AND COALESCE(error, '') = ''
                 ORDER BY id DESC
                 LIMIT 1
                """
            ).fetchone()
            if not row:
                return None
            return parse_jst_datetime(str(row["end_at"] or ""))
    except Exception:
        return None


def count_ready_upload_queue(db_path: Path) -> Optional[int]:
    if not db_path or not db_path.exists():
        return None
    try:
        with connect(db_path) as con:
            cols = {
                str(r[1]) for r in con.execute(f"PRAGMA table_info({POST_UPLOAD_TABLE})").fetchall()
            }
            if "stage" not in cols or "video_created_at" not in cols:
                return None

            upload_col: Optional[str] = None
            for c in ("upload_youtube_at", "youtube_upload_at"):
                if c in cols:
                    upload_col = c
                    break
            if not upload_col:
                return None

            row = con.execute(
                f"""
                SELECT COUNT(*) AS n
                  FROM {POST_UPLOAD_TABLE}
                 WHERE stage = ?
                   AND COALESCE(video_created_at, '') != ''
                   AND COALESCE({upload_col}, '') = ''
                """,
                (POST_UPLOAD_READY_STAGE,),
            ).fetchone()
            return int(row["n"] or 0) if row else 0
    except Exception:
        return None


def cooldown_status(now: datetime, last_post_at: datetime) -> tuple[bool, timedelta]:
    cooldown = timedelta(hours=max(0.0, POST_COOLDOWN_HOURS))
    elapsed = now - last_post_at
    if elapsed < cooldown:
        return False, cooldown - elapsed
    return True, timedelta(0)


def _format_duration(delta: timedelta) -> str:
    sec = max(0, int(delta.total_seconds()))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}:{m:02d}:{s:02d}"


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
    jst_now = datetime.now(ZoneInfo("Asia/Tokyo"))

    print(f"[CONF] SCRIPT_SCHEDULE : {SCRIPT_SCHEDULE}")
    print(f"[CONF] TIMEOUT_SCHEDULE: {TIMEOUT_SCHEDULE}")
    print(f"[CONF] RUNS_POST_DEFAULT: {RUNS_POST_DEFAULT}")
    print(f"[CONF] POST_COOLDOWN_HOURS: {POST_COOLDOWN_HOURS}")
    print(f"[CONF] POST_WATCH_START_HOURS: {POST_WATCH_START_HOURS}")
    print(f"[CONF] POST_RECHECK_SEC: {POST_RECHECK_SEC}")
    print(f"[CONF] POST_UPLOAD_TABLE: {POST_UPLOAD_TABLE}")
    print(f"[CONF] POST_UPLOAD_READY_STAGE: {POST_UPLOAD_READY_STAGE}")

    try:
        last_post_at = latest_successful_post_at(CFG.DB_PATH)
        if last_post_at is not None:
            elapsed = jst_now - last_post_at
            watch_start = timedelta(hours=max(0.0, POST_WATCH_START_HOURS))
            can_run, remaining = cooldown_status(jst_now, last_post_at)
            if not can_run and elapsed < watch_start:
                last_error = (
                    "[SKIP] upload cooldown active "
                    f"(last_success={last_post_at.strftime('%Y/%m/%d-%H:%M:%S')}, "
                    f"remaining={_format_duration(remaining)})"
                )
                print(last_error)
                return_code = 0
                return return_code
            if not can_run:
                print(
                    "[INFO] cooldown watch mode "
                    f"(last_success={last_post_at.strftime('%Y/%m/%d-%H:%M:%S')}, "
                    f"remaining={_format_duration(remaining)})"
                )

        ok_count = 0
        while ok_count < RUNS_POST_DEFAULT:
            pending = count_ready_upload_queue(CFG.DB_PATH)
            if pending == 0:
                print("[INFO] upload queue is empty. stop run_post.")
                break
            if pending is not None:
                print(
                    f"[INFO] upload queue pending={pending} "
                    f"(ok_count={ok_count}/{RUNS_POST_DEFAULT})"
                )
            else:
                print(
                    f"[INFO] upload queue count unavailable "
                    f"(ok_count={ok_count}/{RUNS_POST_DEFAULT})"
                )

            try:
                run_script_realtime(
                    SCRIPT_SCHEDULE,
                    TIMEOUT_SCHEDULE,
                    extra_args=[
                        "--limit",
                        "1",
                        "--first_schedule_mode",
                        "delay",
                        "--start_delay_min",
                        "0",
                        "--interval_min",
                        "0",
                        "--blocked_hour_ranges",
                        "25-29,11-15",
                        "--align_first_to_hour",
                    ],
                )
                last_error = ""
                ok_count += 1
                print(f"[OK] single upload succeeded ({ok_count}/{RUNS_POST_DEFAULT})")
            except Exception as e:
                last_error = str(e)
                print(f"[WARN] single upload failed: {last_error}", file=sys.stderr)
                print(f"[WAIT] retry in {POST_RECHECK_SEC}s")
                time.sleep(POST_RECHECK_SEC)

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
