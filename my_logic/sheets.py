"""Googleスプレッドシート出力（gspread / サービスアカウント認証）。

認証情報の探索順:
1. st.secrets["gcp_service_account"]（Streamlit Cloud向け・TOMLテーブル）
2. data/gcp_service_account.json（ローカル向け・gitignore対象）

書き込み先はユーザーが作成した固定のスプレッドシート（GOOGLE_SPREADSHEET_URL）。
Googleの制限（2025年〜）によりサービスアカウントは自身のDrive容量を持たず
新規ファイルを作成できないため、「ユーザーが1枚作ってサービスアカウントへ
共有 → 集計ごとにタブを追加」する方式を採る。
"""
from __future__ import annotations

import json
import logging
import re

from .config import data_dir, get_secret

logger = logging.getLogger("my_logic")

_LOCAL_SA_PATH = data_dir() / "gcp_service_account.json"


def _load_sa_info() -> dict | None:
    try:
        import streamlit as st
        if "gcp_service_account" in st.secrets:
            return dict(st.secrets["gcp_service_account"])
    except Exception:
        pass
    if _LOCAL_SA_PATH.exists():
        try:
            return json.loads(_LOCAL_SA_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            logger.error("サービスアカウントJSONの読み込み失敗: %s", e)
    return None


def sa_email() -> str:
    """サービスアカウントのメールアドレス（共有先指定用の識別子）。"""
    info = _load_sa_info()
    return (info or {}).get("client_email", "")


def setup_guide() -> str:
    email = sa_email() or "（サービスアカウントJSON設定後にここに表示されます）"
    return f"""\
**Googleスプレッドシート連携の初回設定（無料・10分程度）**

1. https://console.cloud.google.com/ でGoogleアカウントにログインし、
   新しいプロジェクトを作成（名前は例: winvoice）
2. 「APIとサービス → ライブラリ」で **Google Sheets API** と
   **Google Drive API** の2つを有効化
3. 「APIとサービス → 認証情報 → 認証情報を作成 → サービスアカウント」
   で作成（名前は例: winvoice-sheets。ロールは不要・省略可）
4. 作成したサービスアカウント → 「キー」タブ → 「鍵を追加 → 新しい鍵を作成
   → JSON」でキーファイルをダウンロード
5. ダウンロードしたJSONを次の場所に置く:
   - ローカル: `~/claude-workspace/data/gcp_service_account.json` に保存
   - クラウド: Streamlit CloudのSecretsに `[gcp_service_account]` セクション
     としてJSONの中身を貼り付け
6. **Googleドライブで新しいスプレッドシートを1枚作成**（名前は例: WINVOICE集計）し、
   右上の「共有」から次のメールアドレスへ**編集者**権限で共有:
   `{email}`
7. そのスプレッドシートのURLを Secrets/`.env` に追加:
   `GOOGLE_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/..."`

※Googleの制限によりサービスアカウントは新規ファイルを作れないため、
　この固定シートに集計ごとのタブ（例「7/12(日) 福島」）が追加されていきます。

```toml
# Streamlit Cloud Secrets の記載例
GOOGLE_SPREADSHEET_URL = "https://docs.google.com/spreadsheets/d/xxxx/edit"

[gcp_service_account]
type = "service_account"
project_id = "..."
private_key_id = "..."
private_key = "-----BEGIN PRIVATE KEY-----\\n...\\n-----END PRIVATE KEY-----\\n"
client_email = "...@....iam.gserviceaccount.com"
client_id = "..."
auth_uri = "https://accounts.google.com/o/oauth2/auth"
token_uri = "https://oauth2.googleapis.com/token"
auth_provider_x509_cert_url = "https://www.googleapis.com/oauth2/v1/certs"
client_x509_cert_url = "..."
```
"""


# 後方互換（旧UIコードが参照）
SETUP_GUIDE = "設定手順は画面の「初回設定の手順」を参照してください"


def sheets_configured() -> tuple[bool, str]:
    """設定状態を返す（実行前チェック用）。(OK?, 不足内容)"""
    if _load_sa_info() is None:
        return False, "サービスアカウントJSONが未設定"
    if not get_secret("GOOGLE_SPREADSHEET_URL"):
        return False, "GOOGLE_SPREADSHEET_URL（集計用シートのURL）が未設定"
    return True, ""


def _safe_tab_name(name: str) -> str:
    # シート名に使えない文字を除去し100文字制限に収める
    return re.sub(r"[\[\]:*?/\\']", " ", name).strip()[:95]


def export_to_spreadsheet(tab_prefix: str,
                          venue_blocks: dict[str, list[list[str]]]) -> str:
    """固定スプレッドシートに「{tab_prefix} {場名}」タブを追加して書き込む。

    同名タブが既にあれば削除して作り直す（再集計の上書き）。
    Returns: スプレッドシートのURL
    """
    import gspread

    sa_info = _load_sa_info()
    if sa_info is None:
        raise RuntimeError("Googleサービスアカウントが未設定です")
    sheet_url = get_secret("GOOGLE_SPREADSHEET_URL")
    if not sheet_url:
        raise RuntimeError("GOOGLE_SPREADSHEET_URL が未設定です")
    gc = gspread.service_account_from_dict(sa_info)
    sh = gc.open_by_url(sheet_url)
    for venue, rows in venue_blocks.items():
        name = _safe_tab_name(f"{tab_prefix} {venue}")
        try:
            old = sh.worksheet(name)
            sh.del_worksheet(old)
        except gspread.WorksheetNotFound:
            pass
        ws = sh.add_worksheet(title=name,
                              rows=max(len(rows) + 10, 50), cols=15)
        if rows:
            ws.update(rows, "A1", raw=True)
        logger.info("シート書き込み: %s (%d行)", name, len(rows))
    return sh.url
