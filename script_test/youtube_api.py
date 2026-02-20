#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
YouTube Data API v3: 指定チャンネルの最新動画IDを10件取得して表示する。

Requirements:
- YOUTUBE_API_KEY を環境変数 or girlsChannel.env に設定
- CHANNEL_ID を環境変数 or このスクリプト内で指定
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import List

import requests


# Allow running from script_test/ directly.
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

# repo直下モジュールの読み込み
from core import env_loader
from core.config import CFG as APP_CFG

# .env / config.local.json を読み込み
env_loader.load_env()

# =============================================================================
# 設定（ここだけ変えればOK）
# =============================================================================
# 環境変数キー
ENV_CHANNEL_ID_KEY = "CHANNEL_ID"
ENV_API_KEY_KEY = "YOUTUBE_API_KEY"

# APIキーの保管先（アップロード処理と同じ場所）
API_KEY_FILENAME = "youtube_api_key.txt"

# APIリクエスト設定
SEARCH_ENDPOINT = "https://www.googleapis.com/youtube/v3/search"
MAX_RESULTS = 10
API_TIMEOUT_SEC = 20

# =============================================================================
# 変数（上部に集約）
# =============================================================================
_BASE_OUTPUT_ROOT = APP_CFG.BASE_OUTPUT_ROOT
_API_DIR = Path(APP_CFG.API_DIR or (_BASE_OUTPUT_ROOT / "api")) if _BASE_OUTPUT_ROOT else Path()
API_KEY_FILE = _API_DIR / API_KEY_FILENAME if _API_DIR else None

CHANNEL_ID = os.environ.get(ENV_CHANNEL_ID_KEY, "").strip() or "UCQsU4lsfBEEJh_asJ3fagPg"
API_KEY = os.environ.get(ENV_API_KEY_KEY, "").strip()
if not API_KEY and API_KEY_FILE and API_KEY_FILE.exists():
    # ファイルに1行でAPIキーを書く運用に対応
    API_KEY = API_KEY_FILE.read_text(encoding="utf-8").strip()


def fetch_video_ids(channel_id: str, api_key: str, max_results: int = 10) -> List[str]:
    if not channel_id or channel_id == "YOUR_CHANNEL_ID":
        raise ValueError("CHANNEL_ID が未設定です。環境変数 or スクリプトで指定してください。")
    if not api_key:
        raise ValueError(
            "YOUTUBE_API_KEY が未設定です。環境変数 / girlsChannel.env / apiキー保存ファイルを確認してください。"
        )

    # チャンネルの最新動画を取得
    url = SEARCH_ENDPOINT
    params = {
        "part": "snippet",
        "channelId": channel_id,
        "maxResults": max_results,
        "order": "date",
        "type": "video",
        "key": api_key,
    }
    resp = requests.get(url, params=params, timeout=API_TIMEOUT_SEC)
    resp.raise_for_status()
    data = resp.json()

    # 取得した動画IDのみを抽出
    items = data.get("items", [])
    video_ids: List[str] = []
    for item in items:
        vid = item.get("id", {}).get("videoId")
        if vid:
            video_ids.append(str(vid))
    return video_ids


def main() -> int:
    try:
        ids = fetch_video_ids(CHANNEL_ID, API_KEY, MAX_RESULTS)
    except Exception as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        return 1

    for vid in ids:
        print(vid)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
