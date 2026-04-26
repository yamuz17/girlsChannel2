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

# 再判定間隔（秒）
POST_RECHECK_SEC = int(env_loader.env_int("POST_RECHECK_SEC", 300))
if POST_RECHECK_SEC <= 0:
    POST_RECHECK_SEC = 300

# 投稿キュー判定に使う設定
POST_UPLOAD_READY_STAGE = int(env_loader.env_int("POST_UPLOAD_READY_STAGE", 60))
POST_UPLOAD_UPLOADING_STAGE = int(env_loader.env_int("POST_UPLOAD_UPLOADING_STAGE", 70))
POST_UPLOAD_TABLE = CFG.ITEMS_DONE_TABLE or "items_done"


def now_jst_str() -> str:
    return datetime.now(ZoneInfo("Asia/Tokyo")).strftime("%Y/%m/%d-%H:%M:%S")


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
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


def ensure_post_tables(con: sqlite3.Connection) -> None:
    con.execute(
        f"""
        CREATE TABLE IF NOT EXISTS {POST_UPLOAD_TABLE} (
          id TEXT PRIMARY KEY,
          skip INTEGER NOT NULL DEFAULT 0,
          stage INTEGER NOT NULL DEFAULT {POST_UPLOAD_READY_STAGE},
          category TEXT,
          post_title TEXT,
          keywords TEXT,
          url TEXT,
          list_add_at TEXT,
          video_created_at TEXT,
          youtube_upload_at TEXT,
          youtube_publish_at TEXT,
          tiktok_upload_at TEXT,
          tiktok_publish_at TEXT,
          folder_delite_at TEXT,
          folder_name TEXT
        )
        """
    )
    cols = {
        str(r[1]) for r in con.execute(f"PRAGMA table_info({POST_UPLOAD_TABLE})").fetchall()
    }
    if "upload_youtube_at" not in cols:
        con.execute(f"ALTER TABLE {POST_UPLOAD_TABLE} ADD COLUMN upload_youtube_at TEXT")
    if "upload_tiktok_at" not in cols:
        con.execute(f"ALTER TABLE {POST_UPLOAD_TABLE} ADD COLUMN upload_tiktok_at TEXT")
    con.execute(
        f"CREATE INDEX IF NOT EXISTS idx_{POST_UPLOAD_TABLE}_stage ON {POST_UPLOAD_TABLE}(stage, skip, id DESC)"
    )
    ensure_history_table(con)
    con.commit()


def record_post_history(
    start_at: str,
    end_at: str,
    sec: float,
    error: str,
) -> None:
    db_path = CFG.DB_PATH
    if not db_path:
        return
    try:
        with connect(db_path) as con:
            ensure_post_tables(con)
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


def count_ready_upload_queue(db_path: Path) -> Optional[int]:
    if not db_path:
        return None
    try:
        with connect(db_path) as con:
            ensure_post_tables(con)
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


def recover_stuck_uploading_queue(db_path: Path) -> int:
    if not db_path:
        return 0
    try:
        with connect(db_path) as con:
            ensure_post_tables(con)
            cols = {
                str(r[1]) for r in con.execute(f"PRAGMA table_info({POST_UPLOAD_TABLE})").fetchall()
            }
            if "stage" not in cols or "video_created_at" not in cols:
                return 0

            upload_col: Optional[str] = None
            for c in ("upload_youtube_at", "youtube_upload_at"):
                if c in cols:
                    upload_col = c
                    break
            if not upload_col:
                return 0

            set_parts = [f"stage = {POST_UPLOAD_READY_STAGE}"]
            params: List[object] = []
            if "youtube_status" in cols:
                set_parts.append("youtube_status = ?")
                params.append("requeued_from_run_post")
            if "youtube_error" in cols:
                set_parts.append("youtube_error = ?")
                params.append(None)

            params.append(POST_UPLOAD_UPLOADING_STAGE)
            cur = con.execute(
                f"""
                UPDATE {POST_UPLOAD_TABLE}
                   SET {", ".join(set_parts)}
                 WHERE stage = ?
                   AND COALESCE(video_created_at, '') != ''
                   AND COALESCE({upload_col}, '') = ''
                   AND COALESCE(skip, 0) = 0
                """,
                tuple(params),
            )
            con.commit()
            return int(cur.rowcount or 0)
    except Exception:
        return 0


def print_section(title: str) -> None:
    print(f"\n=== {title} ===")


def run_script_realtime(
    script_path: Path, timeout: Optional[int], extra_args: Optional[List[str]] = None
) -> None:
    if not script_path.exists():
        raise FileNotFoundError(f"スクリプトが見つかりません: {script_path}")

    cmd = [sys.executable, str(script_path)]
    if extra_args:
        cmd.extend(extra_args)

    print(f"[実行] {' '.join(cmd)}")
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
        raise RuntimeError(f"{script_path.name} がタイムアウトしました")
    finally:
        elapsed = time.time() - start

    if rc != 0:
        raise RuntimeError(f"{script_path.name} が失敗しました (終了コード={rc})")

    print(f"[完了] {script_path.name} を終了しました ({elapsed:.1f}秒)")


def main() -> int:
    started_at = now_jst_str()
    t0 = time.time()
    last_error = ""

    print_section("投稿処理設定")
    print(f"[設定] 実行スクリプト: {SCRIPT_SCHEDULE}")
    print(f"[設定] タイムアウト秒: {TIMEOUT_SCHEDULE}")
    print(f"[設定] 1回あたり最大投稿数: {RUNS_POST_DEFAULT}")
    print(f"[設定] 再確認間隔秒: {POST_RECHECK_SEC}")
    print(f"[設定] 投稿対象テーブル: {POST_UPLOAD_TABLE}")
    print(f"[設定] 投稿待ちステージ: {POST_UPLOAD_READY_STAGE}")
    print(f"[設定] 投稿中ステージ: {POST_UPLOAD_UPLOADING_STAGE}")

    try:
        ok_count = 0
        while ok_count < RUNS_POST_DEFAULT:
            pending = count_ready_upload_queue(CFG.DB_PATH)
            if pending == 0:
                recovered = recover_stuck_uploading_queue(CFG.DB_PATH)
                if recovered > 0:
                    print(
                        f"[復旧] stage={POST_UPLOAD_UPLOADING_STAGE} で滞留していた "
                        f"{recovered}件を stage={POST_UPLOAD_READY_STAGE} に戻しました"
                    )
                    pending = count_ready_upload_queue(CFG.DB_PATH)
                if pending == 0:
                    print("[終了] 投稿待ちキューが空のため、run_post を停止します")
                    break
            if pending is not None:
                print(
                    f"[進捗] 投稿待ち件数={pending} "
                    f"(成功={ok_count}/{RUNS_POST_DEFAULT})"
                )
            else:
                print(
                    f"[進捗] 投稿待ち件数を取得できません "
                    f"(成功={ok_count}/{RUNS_POST_DEFAULT})"
                )

            try:
                print_section(f"投稿実行 {ok_count + 1}/{RUNS_POST_DEFAULT}")
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
                print(f"[成功] 1件の投稿が完了しました ({ok_count}/{RUNS_POST_DEFAULT})")
            except Exception as e:
                last_error = str(e)
                print(f"[警告] 1件の投稿に失敗しました: {last_error}", file=sys.stderr)
                print(f"[待機] {POST_RECHECK_SEC}秒後に再試行します")
                time.sleep(POST_RECHECK_SEC)

        return_code = 0
    except Exception as e:
        last_error = str(e)
        print(f"[エラー] {last_error}", file=sys.stderr)
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
