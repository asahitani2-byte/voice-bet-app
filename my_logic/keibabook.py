"""競馬ブック（s.keibabook.co.jp）「厩舎の話」の取得。

過去プロジェクト（keibabook_proxy.py / id_map.json）のロジックを移植。
- ID変換: netkeiba「年+場+回+日+R」→ keibabook「年+回+場+日+R」
  （場と回を入れ替え、場コードをマッピング）
- 厩舎の話ページ（/cyuou/danwa/0/{id}）は未ログインだと先頭数頭分のみ。
  全頭分の取得には keibabook ログインが必要（実測）。
- 認証情報: KEIBABOOK_LOGIN_ID/PASSWORD（env/.env/secrets）→
  旧プロジェクトの keibabook_config.json の順で探す。
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

from .config import get_secret
from .repository import Repository

logger = logging.getLogger("my_logic")

# netkeiba場コード → keibabook場コード（id_map.json と同一）
_VENUE_MAP = {
    "01": "08", "02": "09", "03": "06", "04": "07", "05": "04",
    "06": "05", "07": "02", "08": "00", "09": "01", "10": "03",
}

TTL_DANWA = 6 * 3600  # 直前まで更新されるため6時間
KB_LOGIN_URL = "https://p.keibabook.co.jp/login/login"
# 旧プロジェクト（netkeiba×keibabook自動化）の設定ファイルを再利用
_LEGACY_CONFIG = Path.home() / "Documents/claude-work/keibabook_config.json"
_KB_COOKIE_TTL = 14 * 24 * 3600  # ログインは約1ヶ月維持される（余裕を見て14日）


def _kb_cookie_path() -> Path:
    d = Path.home() / ".winvoice"
    d.mkdir(exist_ok=True)
    return d / "kb_cookies.json"


def _kb_credentials() -> tuple[str, str]:
    lid = get_secret("KEIBABOOK_LOGIN_ID")
    pw = get_secret("KEIBABOOK_PASSWORD")
    if lid and pw:
        return lid, pw
    try:
        if _LEGACY_CONFIG.exists():
            cfg = json.loads(_LEGACY_CONFIG.read_text(encoding="utf-8"))
            return cfg.get("login_id", ""), cfg.get("password", "")
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("keibabook_config.json 読み込み失敗: %s", e)
    return "", ""


def _kb_login() -> dict:
    """Playwright headlessでkeibabookにログインしCookieを返す（proxy移植）。"""
    login_id, password = _kb_credentials()
    if not login_id or not password:
        return {}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            ctx = browser.new_context()
            page = ctx.new_page()
            page.goto(KB_LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)
            for sel in ('input[name="login_id"]', 'input[type="text"]', '#login_id'):
                if page.query_selector(sel):
                    page.fill(sel, login_id)
                    break
            for sel in ('input[name="password"]', 'input[type="password"]',
                        '#password', '#js-password'):
                if page.query_selector(sel):
                    page.fill(sel, password)
                    break
            for sel in ('input[type="submit"]', 'button[type="submit"]', '.login_btn'):
                if page.query_selector(sel):
                    page.click(sel)
                    break
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
            html = page.content()
            ok = ("logout" in html.lower() or "ログアウト" in html
                  or "mypage" in html.lower())
            cookies = {c["name"]: c["value"] for c in ctx.cookies()
                       if "keibabook" in c.get("domain", "")}
            browser.close()
        if not ok or not cookies:
            logger.error("keibabookログイン失敗")
            return {}
        logger.info("keibabookログイン成功")
        return cookies
    except Exception as e:
        # クラウド環境でchromium未導入の場合は一度だけ導入して再試行
        from .config import try_install_playwright_chromium
        if try_install_playwright_chromium(str(e)):
            return _kb_login()
        logger.error("keibabookログイン例外: %s", type(e).__name__)
        return {}


def _kb_cookies(force: bool = False) -> dict:
    """Cookieファイル → ログイン の順で調達（14日キャッシュ）。"""
    path = _kb_cookie_path()
    if not force:
        try:
            if path.exists():
                data = json.loads(path.read_text())
                if (time.time() - data.get("saved_at", 0)) < _KB_COOKIE_TTL \
                        and data.get("cookies"):
                    return data["cookies"]
        except (json.JSONDecodeError, OSError):
            pass
    cookies = _kb_login()
    if cookies:
        try:
            path.write_text(json.dumps({"cookies": cookies,
                                        "saved_at": time.time()}))
            os.chmod(path, 0o600)
        except OSError:
            pass
    return cookies


def netkeiba_to_keibabook_id(nk_id: str) -> str | None:
    """netkeiba race_id → keibabook ID。

    例: 202603020611（場03福島・回02・日06・11R）→ 202602060611
    """
    if not re.fullmatch(r"\d{12}", nk_id or ""):
        return None
    year, venue, kai, day, race = (
        nk_id[0:4], nk_id[4:6], nk_id[6:8], nk_id[8:10], nk_id[10:12])
    kb_venue = _VENUE_MAP.get(venue)
    if not kb_venue:  # 地方など未対応の場コード
        return None
    return f"{year}{kai}{kb_venue}{day}{race}"


def parse_danwa_html(html: str) -> dict[str, str]:
    """厩舎の話ページから {馬番(str): 談話テキスト} を抽出する。

    構造: table.danwa 内で
      行A: td.waku / td.umaban / td.left(馬名)
      行B: td.danwa(談話全文)
    の繰り返し。馬番行→談話行の順で対応付ける。
    """
    soup = BeautifulSoup(html, "html.parser")
    result: dict[str, str] = {}
    current_umaban: str | None = None
    for tr in soup.select("table.danwa tr, table tr"):
        umaban_td = tr.select_one("td.umaban")
        if umaban_td:
            text = umaban_td.get_text(strip=True)
            current_umaban = text if text.isdigit() else None
            continue
        danwa_td = tr.select_one("td.danwa")
        if danwa_td and current_umaban:
            comment = danwa_td.get_text("\n", strip=True)
            if comment and current_umaban not in result:
                result[current_umaban] = comment
            current_umaban = None
    return result


def fetch_danwa(nk_race_id: str, repo: Repository,
                force_refresh: bool = False) -> tuple[dict[str, str], str]:
    """netkeiba race_idから厩舎の話を取得する。

    Returns: ({馬番: 談話}, 警告メッセージ)。失敗しても例外にしない
    （厩舎の話は補助情報のため、分析全体を止めない）。
    """
    kb_id = netkeiba_to_keibabook_id(nk_race_id)
    if not kb_id:
        return {}, "keibabook ID変換不可（中央以外のrace_id）"
    cookies = _kb_cookies()
    authed = bool(cookies)
    # 未ログインは先頭数頭分のみのため、認証状態をキャッシュキーに含める
    key = f"danwa:{nk_race_id}:{'auth' if authed else 'anon'}"
    cached = None if force_refresh else repo.cache_get(key, TTL_DANWA)
    if cached is not None:
        try:
            return json.loads(cached), ""
        except json.JSONDecodeError:
            pass

    def _get(cks: dict) -> dict[str, str] | None:
        """リトライ付きでdanwaページを取得（クラウド環境の不安定さ対策）。"""
        url = f"https://s.keibabook.co.jp/cyuou/danwa/0/{kb_id}"
        last_err = ""
        for attempt in range(3):
            try:
                r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                                 cookies=cks, timeout=20)
                if r.status_code == 200:
                    return parse_danwa_html(r.text)
                last_err = f"HTTP {r.status_code}"
                logger.warning("厩舎の話 取得失敗 status=%s try=%d url=%s",
                               r.status_code, attempt + 1, url)
                if r.status_code in (403, 429):
                    break  # ブロック系はリトライしない
            except requests.RequestException as e:
                last_err = type(e).__name__
                logger.warning("厩舎の話 通信失敗 try=%d url=%s err=%s",
                               attempt + 1, url, last_err)
            time.sleep(1.5 * (attempt + 1))
        logger.warning("厩舎の話 リトライ上限 url=%s last=%s", url, last_err)
        return None

    danwa = _get(cookies)
    if danwa is None:
        return {}, ("厩舎の話を取得できませんでした（通信エラー・リトライ済み）。"
                    "分析結果には影響ありません。クラウド環境では"
                    "keibabook側の制限で取得できない場合があります")
    # ログインしたはずなのに極端に少ない → Cookie失効の可能性 → 再ログイン1回
    if authed and len(danwa) <= 3:
        fresh = _kb_cookies(force=True)
        if fresh:
            retry = _get(fresh)
            if retry and len(retry) > len(danwa):
                danwa = retry
    if not danwa:
        logger.info("厩舎の話なし kb_id=%s", kb_id)
        return {}, "厩舎の話が見つかりませんでした（掲載前の可能性）"
    repo.cache_set(key, json.dumps(danwa, ensure_ascii=False))
    logger.info("厩舎の話 取得 nk_id=%s kb_id=%s horses=%d authed=%s",
                nk_race_id, kb_id, len(danwa), authed)
    warn = ("" if authed else
            "keibabookログイン未設定のため厩舎の話は一部の馬のみです")
    return danwa, warn
