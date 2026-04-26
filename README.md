# girlsChannel2

ガールズチャンネルのトピック取得から動画生成・投稿までを、SQLite キューで実行するスクリプト群です。

## セットアップ
```bash
./scripts/setup.sh
```

## 実行方法（例）
```bash
source .venv/bin/activate
python scripts/pipeline/build_list.py
python scripts/pipeline/run_pipeline.py
python scripts/pipeline/youtube_history_sync.py --limit 300
python scripts/steps/post_upload.py
```

## ディレクトリ構成（整理後）
- `scripts/pipeline`: 日次運用のエントリーポイント（`build_list`, `run_pipeline`, `run_post`）
- `scripts/steps`: 各工程の実処理（`fetch_data`, `make_images`, `make_audio`, `make_preview`, `assemble_video`, `post_upload`）
- `scripts/experimental`: 検証用スクリプト
- `core`: 共通モジュール（`config`, `env_loader`, `queue_db`）
- `scripts_done` / `scripts_parts` / `script_test`: 互換維持のため当面残置

## 注意
- 生成物（動画・音声・画像）、DB、ログ、`.env`、トークン/認証情報は Git 管理しません。
- `ENGINE_URL` などの環境変数は `girlsChannel.env` で管理してください。

## 新仕様（採用方針）

### テーブル構成
- テーブルは `items_do` / `items_done` / `youtube_history` を使う。

`items_do` カラム:
- `id`
- `skip`
- `stage`
- `hot_score`
- `category`
- `title`
- `comments_count`
- `first_post_at`
- `last_post_at`
- `list_add_at`

`items_done` カラム:
- `id`
- `skip`
- `stage`
- `category`
- `post_title`
- `keywords`
- `url`
- `list_add_at`
- `video_created_at`
- `youtube_upload_at`
- `youtube_publish_at`
- `tiktok_upload_at`
- `tiktok_publish_at`
- `folder_delite_at`

`youtube_history` カラム:
- `id`（トピックID）
- `youtube_video_id`
- `youtube_uploaded_at`
- `upload_youtube_at`
- `publish_at_utc`
- `publish_at_jst`
- `source_table`
- `first_detected_at`
- `last_synced_at`

### アイテム選択ロジック（run_pipeline）
- `items_do` を参照する。
- 並び順は `hot_score` 降順、同点時に `list_add_at` の新しい順。
- `skip=0` のみ対象。
- `youtube_history` に `id` があるものは除外する（過去に作成/投稿済み）。
- 上から順に、`run_pipeline` が今回生成する動画本数分を選択する。
- 選択したアイテムは `items_done` に追加して処理管理する。

### フォルダ命名
- `folderName` は次の形式に統一する。  
  `作成日時（yyyymmdd-hhmmss）_id_post_title（文頭から10文字）`

### 動画尺
- 動画時間のデフォルトは `70` 秒。

### ログ出力
- 処理進捗は進捗バー中心で表示する。
- 1件ごとの詳細ログを減らし、ターミナル出力量を抑える。

### girlsChannel.env 見直し方針
- 旧単一テーブル前提の変数を整理し、2テーブル構成に合わせて再設計する。
- 例: `ITEMS_DO_TABLE`, `ITEMS_DONE_TABLE`, `DEFAULT_VIDEO_SEC`, `PICK_ORDER` など。

## CI（簡易）
```bash
make ci
```
