# girlsChannel2

ガールズチャンネルのトピック取得 → 画像生成 → 音声生成 → サムネ/preview → 動画組み立てを、
SQLite のキューで自動実行するスクリプト群です。

## 使い方（最小）

### 前提
- Python 3.11
- ffmpeg
- VOICEVOX エンジン（ローカル or API）

### セットアップ
1. `girlsChannel.env` を作成して `DB_PATH` / `BASE_OUTPUT_ROOT` / `SCRIPTS_DIR` を設定
2. 依存ライブラリをインストール（playwright / pillow / requests / tqdm / python-dateutil など）
3. Playwright を使う場合はブラウザを導入

### 実行
```bash
# 1) リスト作成
python3 99_/build_list.py

# 2) パイプライン実行（1本だけ作る場合は --runs 1）
python3 99_/run_pipeline.py --steps list,pipeline --runs 1 --until 99

# 3) 投稿予約（必要な場合）
python3 99_/投稿予約.py
```

## 注意
- 生成物（動画・音声・画像・DB・ログ・.env）は Git で管理しません。
- `ENGINE_URL` は `girlsChannel.env` で指定してください。
