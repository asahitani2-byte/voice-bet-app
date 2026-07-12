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
    from .config import browser_launch_configs, try_install_playwright_chromium
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {}
    last_err = ""
    for round_ in range(2):  # 2周目はChromium導入後の再試行
        try:
            with sync_playwright() as p:
                for cfg in browser_launch_configs():
                    browser = None
                    try:
                        browser = p.chromium.launch(headless=True, **cfg)
                        cookies, ok = _run_kb_login_flow(
                            browser, login_id, password)
                        browser.close()
                        if not ok or not cookies:
                            logger.error("keibabookログイン失敗"
                                         "（ID/パスワード誤りの可能性）")
                            return {}
                        logger.info("keibabookログイン成功")
                        return cookies
                    except Exception as e:
                        last_err = f"{type(e).__name__}: {str(e)[:150]}"
                        logger.warning("kbブラウザ構成 %s で失敗 → 次の構成: %s",
                                       cfg.get("executable_path", "playwright"),
                                       last_err)
                        if browser:
                            try:
                                browser.close()
                            except Exception:
                                pass
        except Exception as e:
            last_err = f"{type(e).__name__}: {str(e)[:150]}"
        if round_ == 0 and try_install_playwright_chromium(last_err):
            continue
        break
    logger.error("keibabookログイン例外: %s", last_err)
    return {}


def _run_kb_login_flow(browser, login_id: str, password: str) -> tuple[dict, bool]:
    """起動済みブラウザでkeibabookログインを実行する。"""
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
    return cookies, ok


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
    """netkeiba race_idから厩舎の話を取得する（中央・地方両対応）。

    Returns: ({馬番: 談話}, 警告メッセージ)。失敗しても例外にしない
    （厩舎の話は補助情報のため、分析全体を止めない）。
    """
    from .nar import is_nar_race_id
    if is_nar_race_id(nk_race_id):
        return _fetch_danwa_nar(nk_race_id, repo, force_refresh)
    kb_id = netkeiba_to_keibabook_id(nk_race_id)
    if not kb_id:
        return {}, "keibabook ID変換不可"
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


# ─── 地方（chihou）の厩舎の話 ────────────────────────────────
# 地方のkeibabook IDは {開催日8}{当日の開催順idx2}{R番号2}{月日4} の16桁。
# idxは日ごとの連番のため、日程ページ（/chihou/nittei/）から
# 「場名タブ（固定場コード）→ その場のsyutuba ID」の2段で解決する。
# keibabookが掲載していない場（例: 一部の地方場）は取得不可として警告のみ。

def _kb_session_cookies() -> tuple[dict, bool]:
    cookies = _kb_cookies()
    return cookies, bool(cookies)


def nar_kb_id(date_str: str, idx: str, race_no: int) -> str:
    """地方keibabook ID: {YYYYMMDD}{idx}{R2}{MMDD}。"""
    return f"{date_str}{idx}{race_no:02d}{date_str[4:]}"


def _chihou_venue_idx(date_str: str, venue_name: str, cookies: dict,
                      repo: Repository, force_refresh: bool) -> str | None:
    """指定日・場の「当日の開催順インデックス」を日程ページから解決する。"""
    key = f"kb_chihou_idx:{date_str}:{venue_name}"
    cached = None if force_refresh else repo.cache_get(key, TTL_DANWA)
    if cached is not None:
        return cached or None

    def _get(url: str) -> str | None:
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                             cookies=cookies, timeout=15)
            return r.text if r.status_code == 200 else None
        except requests.RequestException as e:
            logger.warning("kb日程ページ取得失敗 url=%s err=%s",
                           url, type(e).__name__)
            return None

    def _find_idx(html: str) -> str | None:
        ids = re.findall(r"/chihou/(?:syutuba|danwa/\d|seiseki)/(\d{16})", html)
        for i in ids:
            if i.startswith(date_str) and i.endswith(date_str[4:]):
                return i[8:10]
        return None

    idx: str | None = None
    html = _get(f"https://s.keibabook.co.jp/chihou/nittei/{date_str}")
    if html:
        # 表示中ページ自体が対象の場か（タイトル等に場名）
        title_zone = html[:4000]
        if venue_name in title_zone and _find_idx(html):
            idx = _find_idx(html)
        if idx is None:
            # 場タブ（/chihou/nittei/{date}{固定場コード2桁}）から対象の場を探す
            soup = BeautifulSoup(html, "html.parser")
            for a in soup.select("a[href*='/chihou/nittei/']"):
                if a.get_text(strip=True) == venue_name:
                    m = re.search(rf"/chihou/nittei/({date_str}\d{{2}})",
                                  a.get("href") or "")
                    if m:
                        html2 = _get(
                            f"https://s.keibabook.co.jp/chihou/nittei/{m.group(1)}")
                        if html2:
                            idx = _find_idx(html2)
                    break
    repo.cache_set(key, idx or "")
    if idx is None:
        logger.info("kb地方: %s %s の掲載なし", date_str, venue_name)
    return idx


def _fetch_danwa_nar(nk_race_id: str, repo: Repository,
                     force_refresh: bool = False) -> tuple[dict[str, str], str]:
    """地方レースの厩舎の話を取得する。"""
    from .nar import NAR_VENUES
    date_str = nk_race_id[0:4] + nk_race_id[6:10]
    venue = NAR_VENUES.get(nk_race_id[4:6], "")
    race_no = int(nk_race_id[10:12])
    if not venue:
        return {}, "厩舎の話: 場コード不明"
    cookies, authed = _kb_session_cookies()
    key = f"danwa:{nk_race_id}:{'auth' if authed else 'anon'}"
    cached = None if force_refresh else repo.cache_get(key, TTL_DANWA)
    if cached is not None:
        try:
            return json.loads(cached), ""
        except json.JSONDecodeError:
            pass
    idx = _chihou_venue_idx(date_str, venue, cookies, repo, force_refresh)
    if idx is None:
        return {}, f"厩舎の話: keibabookに{venue}の掲載がありません"
    kb_id = nar_kb_id(date_str, idx, race_no)
    url = f"https://s.keibabook.co.jp/chihou/danwa/1/{kb_id}"
    last_err = ""
    danwa: dict[str, str] = {}
    for attempt in range(3):
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"},
                             cookies=cookies, timeout=20)
            if r.status_code == 200:
                danwa = parse_danwa_html(r.text)
                break
            last_err = f"HTTP {r.status_code}"
            if r.status_code in (403, 429):
                break
        except requests.RequestException as e:
            last_err = type(e).__name__
        time.sleep(1.5 * (attempt + 1))
    if not danwa:
        logger.info("kb地方 談話なし url=%s last=%s", url, last_err)
        return {}, ("厩舎の話が見つかりませんでした"
                    "（掲載前・未掲載の可能性）" if not last_err else
                    f"厩舎の話を取得できませんでした（{last_err}）")
    repo.cache_set(key, json.dumps(danwa, ensure_ascii=False))
    logger.info("kb地方 厩舎の話 nk_id=%s kb_id=%s horses=%d authed=%s",
                nk_race_id, kb_id, len(danwa), authed)
    warn = ("" if authed else
            "keibabookログイン未設定のため厩舎の話は一部の馬のみです")
    return danwa, warn
