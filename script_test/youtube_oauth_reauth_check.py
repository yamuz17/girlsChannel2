#!/usr/bin/env python3
# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Optional

from googleapiclient.discovery import build
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = ["https://www.googleapis.com/auth/youtube.readonly"]
DEFAULT_API_DIR = Path(
    "/Users/yumahama/Library/CloudStorage/GoogleDrive-yuma17.service@gmail.com/マイドライブ/python/project_girlsChannel/01_inputs/api"
)
DEFAULT_TOKEN_NAME = "token.json"


def find_client_secrets(api_dir: Path, client_json_name: str) -> Path:
    explicit = (api_dir / client_json_name).expanduser()
    if explicit.exists():
        return explicit
    explicit_json = Path(str(explicit) + ".json")
    if explicit_json.exists():
        return explicit_json

    cands = sorted(api_dir.glob("client_secret*.json"))
    if len(cands) == 1:
        return cands[0]
    if len(cands) > 1:
        names = ", ".join(p.name for p in cands[:5])
        raise FileNotFoundError(
            "client_secret が複数あるため特定できません。"
            f" --client-json-name で指定してください: {names}"
        )

    raise FileNotFoundError(
        f"client_secret が見つかりません: {explicit}（{explicit_json} も確認済み）"
    )


def get_authenticated_service(client_secrets: Path, token_file: Path) -> Any:
    creds: Optional[Credentials] = None

    if token_file.exists():
        creds = Credentials.from_authorized_user_file(str(token_file), SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secrets), SCOPES
            )
            creds = flow.run_local_server(port=0, open_browser=True)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text(creds.to_json(), encoding="utf-8")

    return build("youtube", "v3", credentials=creds)


def check_read_api(youtube: Any) -> tuple[str, str, str]:
    ch = (
        youtube.channels()
        .list(part="snippet,contentDetails", mine=True, maxResults=1)
        .execute()
    )
    items = ch.get("items", [])
    if not items:
        raise RuntimeError("channels.list(mine=True) の結果が空です")
    item = items[0]
    channel_id = str(item.get("id", "") or "")
    channel_title = str(item.get("snippet", {}).get("title", "") or "")
    uploads = (
        item.get("contentDetails", {})
        .get("relatedPlaylists", {})
        .get("uploads", "")
    )
    if not uploads:
        raise RuntimeError("uploads プレイリストIDを取得できませんでした")

    pl = (
        youtube.playlistItems()
        .list(part="contentDetails", playlistId=uploads, maxResults=1)
        .execute()
    )
    pitems = pl.get("items", [])
    if not pitems:
        return channel_id, channel_title, "アップロード動画なし"

    video_id = str(
        pitems[0].get("contentDetails", {}).get("videoId", "") or "不明"
    )
    return channel_id, channel_title, video_id


def main() -> int:
    parser = argparse.ArgumentParser(
        description="YouTube readonly OAuth の再認可と疎通チェック"
    )
    parser.add_argument(
        "--api-dir",
        default=str(DEFAULT_API_DIR),
        help=f"APIファイル格納ディレクトリ（既定: {DEFAULT_API_DIR}）",
    )
    parser.add_argument(
        "--reset-token",
        action="store_true",
        help="既存tokenを消して再認可を強制",
    )
    parser.add_argument(
        "--token-name",
        default=DEFAULT_TOKEN_NAME,
        help=f"tokenファイル名（既定: {DEFAULT_TOKEN_NAME}）",
    )
    parser.add_argument(
        "--client-json-name",
        default="client_secret.json",
        help="client json名（既定: client_secret.json。未一致なら client_secret*.json を自動探索）",
    )
    args = parser.parse_args()

    api_dir = Path(str(args.api_dir)).expanduser()
    if not api_dir:
        print("[FAIL] API_DIR が空です。")
        return 2

    client_secrets = find_client_secrets(api_dir, str(args.client_json_name))
    token_file = Path(api_dir) / str(args.token_name)

    print(f"[設定] API_DIR: {api_dir}")
    print(f"[設定] client_secret: {client_secrets}")
    print(f"[設定] token: {token_file}")

    if args.reset_token and token_file.exists():
        token_file.unlink()
        print("[手順] 既存 token を削除しました（再認可を強制）")

    try:
        print("[手順] OAuth 認可（またはトークン更新）を実行")
        youtube = get_authenticated_service(client_secrets, token_file)
        print("[成功] token の作成/更新が完了")

        print("[手順] 読み取りAPI疎通チェックを実行")
        channel_id, channel_title, latest_video_id = check_read_api(youtube)
        print(f"[成功] チャンネル: {channel_title} ({channel_id})")
        print(f"[成功] 最新動画ID: {latest_video_id}")

        print("\n[SUCCESS] 再認可は成功です。")
        print("判定基準:")
        print("1) token.json が API_DIR に存在する")
        print("2) channels.list(mine=True) が成功する")
        print("3) playlistItems.list が成功する")
        return 0
    except Exception as e:
        print(f"[失敗] {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
