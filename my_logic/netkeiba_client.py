"""netkeiba通信クライアント。

方針:
- 取得はすべて requests（軽量・安定）。Playwright は「ログインCookieの
  取得」のみに使用（既存の永続プロファイル ~/.netkeiba_ipat_profile を
  headless で開いてCookieを抽出、無効なら NETKEIBA_LOGIN_ID/PASSWORD で
  ログイン）。
- 各リクエスト間に待機＋ジッター、リトライ上限、403/429検知で抑制。
- 取得結果は Repository のSQLiteキャッシュへ保存（Cookieは保存しない）。
"""
from __future__ import annotations

import json
import logging
import os
import random
import re
import time
from pathlib import Path

import requests

from .config import cookie_store_path, get_secret
from .models import HorseRaceNote, RaceInfo, ResultRow
from .parsers import parse_db_result_html, parse_note_fragment, parse_shutuba_html
from .repository import Repository

logger = logging.getLogger("my_logic")

UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
             "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile Safari/604.1")
UA_PC = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"

PROFILE_DIR = os.path.join(os.path.expanduser("~"), ".netkeiba_ipat_profile")

MAX_NOTE_PAGES = 30       # 「もっと見る」全ページ取得の安全上限
REQUEST_TIMEOUT = 15
MAX_RETRIES = 2
WAIT_BASE = 0.8           # 馬ごとの基本待機秒
WAIT_JITTER = 0.5         # ランダムジッター上限

# キャッシュTTL（秒）
TTL_SHUTUBA = 6 * 3600
TTL_NOTES = 24 * 3600
TTL_RESULT = None         # 過去レース結果は不変 → 無期限


class NetkeibaError(Exception):
    """ユーザー向けメッセージを持つ通信・解析エラー。"""


class BlockedError(NetkeibaError):
    """403/429などブロックの兆候。"""


# ─── ログインCookieの管理 ───────────────────────────────────
def _cookies_valid(cookies: dict) -> bool:
    return bool(cookies.get("nkauth") and cookies.get("netkeiba"))


def _load_cookie_file() -> dict:
    path = cookie_store_path()
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if _cookies_valid(data.get("cookies", {})):
                age = time.time() - data.get("saved_at", 0)
                if age < 12 * 3600:
                    return data["cookies"]
    except (json.JSONDecodeError, OSError) as e:
        logger.warning("Cookieファイル読み込み失敗: %s", e)
    return {}


def _save_cookie_file(cookies: dict) -> None:
    path = cookie_store_path()
    try:
        path.write_text(json.dumps(
            {"cookies": cookies, "saved_at": time.time()}))
        os.chmod(path, 0o600)
    except OSError as e:
        logger.warning("Cookieファイル保存失敗: %s", e)


def _extract_cookies_from_profile() -> dict:
    """既存のPlaywright永続プロファイルからCookieを抽出（headless）。

    プロファイルが他プロセス使用中（SingletonLock）の場合は失敗する。
    既存機能への影響を避けるため、プロセスkillやロック解除は行わない。
    """
    if not os.path.isdir(PROFILE_DIR):
        return {}
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                PROFILE_DIR, headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"])
            raw = ctx.cookies("https://race.sp.netkeiba.com/")
            ctx.close()
        return {c["name"]: c["value"] for c in raw
                if ".netkeiba.com" in c.get("domain", "")}
    except Exception as e:
        logger.warning("プロファイルからのCookie抽出失敗（使用中の可能性）: %s", e)
        return {}


def _login_with_credentials() -> dict:
    """NETKEIBA_LOGIN_ID / NETKEIBA_PASSWORD でPlaywrightログインする。

    後方互換: 既存WINVOICEの NETKEIBA_USER / NETKEIBA_PASS も受け付ける。
    """
    login_id = get_secret("NETKEIBA_LOGIN_ID") or get_secret("NETKEIBA_USER")
    password = get_secret("NETKEIBA_PASSWORD") or get_secret("NETKEIBA_PASS")
    if not login_id or not password:
        return {}
    return _login_with_credentials_impl(login_id, password, retry_ok=True)


def _login_with_credentials_impl(login_id: str, password: str,
                                 retry_ok: bool = False) -> dict:
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
            # ログインはPC版ページで行う（モバイルUAだとフォーム構成が
            # 変わり送信ボタンを特定できない）。Cookieはドメイン共通
            ctx = browser.new_context(user_agent=UA_PC)
            page = ctx.new_page()
            page.goto("https://regist.netkeiba.com/account/?pid=login",
                      timeout=30000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            for sel in ("input[name='login_id']", "input[name='email']",
                        "input[type='email']", "input[type='text']"):
                if page.query_selector(sel):
                    page.fill(sel, login_id)
                    break
            pw_sel = None
            for sel in ("input[name='pswd']", "input[name='password']",
                        "input[type='password']"):
                if page.query_selector(sel):
                    page.fill(sel, password)
                    pw_sel = sel
                    break
            # ページ内に検索フォーム等の別formがあるため、必ず
            # 「パスワード欄と同じform内」の送信ボタンを押す
            # （netkeibaのログイン送信は input[type='image']）
            submit_sel = (
                f"form:has({pw_sel}) input[type='image'], "
                f"form:has({pw_sel}) input[type='submit'], "
                f"form:has({pw_sel}) button[type='submit']") if pw_sel else None
            if submit_sel and page.query_selector(submit_sel):
                page.click(submit_sel)
            elif pw_sel:
                page.press(pw_sel, "Enter")  # フォールバック
            else:
                raise RuntimeError("パスワード入力欄が見つかりません")
            page.wait_for_load_state("domcontentloaded", timeout=20000)
            page.goto("https://race.sp.netkeiba.com/", timeout=20000)
            raw = ctx.cookies("https://race.sp.netkeiba.com/")
            browser.close()
        cookies = {c["name"]: c["value"] for c in raw
                   if ".netkeiba.com" in c.get("domain", "")}
        if _cookies_valid(cookies):
            logger.info("ID/パスワードによるログイン成功")
            return cookies
        logger.error("ログイン試行後もセッションCookieが取得できず")
        return {}
    except Exception as e:
        # クラウド環境でchromium未導入の場合は一度だけ導入して再試行
        from .config import try_install_playwright_chromium
        if retry_ok and try_install_playwright_chromium(str(e)):
            return _login_with_credentials_impl(login_id, password,
                                                retry_ok=False)
        logger.error("Playwrightログイン失敗: %s", type(e).__name__)
        return {}


def cookie_sources(force: bool = False):
    """Cookie調達元を優先順に返す（各要素は (名前, 取得関数)）。

    Cookieの「存在」と「サーバー側での有効性」は別物のため、
    呼び出し側（ensure_login）が1件ずつ実検証しながら試す。
    """
    sources = []
    if not force:
        sources.append(("cookieファイル", _load_cookie_file))
    sources.append(("ブラウザプロファイル", _extract_cookies_from_profile))
    sources.append(("ID/パスワードログイン", _login_with_credentials))
    return sources


# ─── クライアント ────────────────────────────────────────────
class NetkeibaClient:
    def __init__(self, repo: Repository, force_refresh: bool = False):
        self.repo = repo
        self.force_refresh = force_refresh
        self.blocked = False
        self._last_request = 0.0
        self._session = requests.Session()
        self._session.headers.update({
            "User-Agent": UA_MOBILE,
            "Referer": "https://race.sp.netkeiba.com/",
        })
        self._logged_in = False

    # --- 低レベル ---
    def _wait(self) -> None:
        elapsed = time.time() - self._last_request
        wait = WAIT_BASE + random.uniform(0, WAIT_JITTER) - elapsed
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.time()

    def _request(self, method: str, url: str, **kwargs) -> requests.Response:
        if self.blocked:
            raise BlockedError(
                "netkeibaからアクセス制限の応答（403/429）を受けたため処理を停止しました。"
                "しばらく時間を置いてから再実行してください。")
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            self._wait()
            try:
                r = self._session.request(
                    method, url, timeout=REQUEST_TIMEOUT, **kwargs)
                if r.status_code in (403, 429):
                    self.blocked = True
                    logger.error("ブロック検知 status=%s url=%s",
                                 r.status_code, url[:120])
                    raise BlockedError(
                        f"netkeibaが {r.status_code} を返しました。"
                        "アクセス過多の可能性があるため処理を中断しました。")
                if r.status_code >= 500:
                    raise requests.HTTPError(f"server error {r.status_code}")
                return r
            except BlockedError:
                raise
            except (requests.RequestException, OSError) as e:
                last_exc = e
                logger.warning("通信リトライ %d/%d url=%s err=%s",
                               attempt + 1, MAX_RETRIES, url[:120],
                               type(e).__name__)
                time.sleep(1.5 * (attempt + 1))
        raise NetkeibaError(f"通信に失敗しました: {type(last_exc).__name__}") \
            from last_exc

    def verify_login(self) -> bool:
        """ログインセッションが本当に有効かをAPI応答で検証する。

        pid=ajax_user_horse_note は、ログイン時は必ず内容
        （メモ本文 or「馬メモはありません」ボックス）を返し、
        未ログイン時は空のコメントブロックのみを返す（実測）。
        """
        try:
            r = self._request(
                "POST", "https://race.sp.netkeiba.com/",
                data={"pid": "ajax_user_horse_note", "input": "UTF-8",
                      "output": "json", "horse_id": "2019104658"},
                headers={"X-Requested-With": "XMLHttpRequest"})
        except NetkeibaError:
            return False
        body = r.text
        return ("Race_Infomation_Box" in body or "RaceNote" in body
                or "\\u99ac\\u30e1\\u30e2" in body or "馬メモ" in body)

    def ensure_login(self, force: bool = False) -> bool:
        """ログインCookieを適用し、実際に有効かまで検証する。

        ファイル → プロファイル → ID/PWログイン の順に、
        1件ずつ「実際にAPIが応答するか」を検証しながら試す。
        """
        for name, fetch in cookie_sources(force=force):
            try:
                cookies = fetch()
            except Exception as e:
                logger.warning("%s からのCookie取得で例外: %s",
                               name, type(e).__name__)
                continue
            if not _cookies_valid(cookies):
                continue
            for k, v in cookies.items():
                self._session.cookies.set(k, v, domain=".netkeiba.com")
            if self.verify_login():
                _save_cookie_file(cookies)
                logger.info("ログイン確立（%s）", name)
                self._logged_in = True
                return True
            logger.warning("%s のCookieはセッション無効 → 次の手段へ", name)
        self._logged_in = False
        return False

    # --- 高レベル ---
    def get_shutuba(self, race_id: str) -> RaceInfo:
        """出馬表（ログイン不要）。"""
        key = f"shutuba:{race_id}"
        html = None if self.force_refresh else self.repo.cache_get(key, TTL_SHUTUBA)
        if html is None:
            url = f"https://race.sp.netkeiba.com/race/shutuba.html?race_id={race_id}"
            r = self._request("GET", url)
            html = r.text
            logger.info("出馬表取得 race_id=%s status=%s len=%d",
                        race_id, r.status_code, len(html))
            self.repo.cache_set(key, html)
        info = parse_shutuba_html(html, race_id)
        if not info.entries:
            raise NetkeibaError(
                f"race_id={race_id} の出走馬を取得できませんでした。"
                "race_idが正しいか、レースが存在するかご確認ください。")
        if not info.distance:
            raise NetkeibaError(
                f"race_id={race_id} のレース距離を取得できませんでした。"
                "netkeibaのページ構成が変更された可能性があります。")
        return info

    def get_horse_notes(self, horse_id: str) -> tuple[list[HorseRaceNote], list[str]]:
        """馬メモ全ページ取得（要ログイン）。(メモ一覧, 警告) を返す。"""
        key = f"notes:{horse_id}"
        warnings: list[str] = []
        cached = None if self.force_refresh else self.repo.cache_get(key, TTL_NOTES)
        if cached is not None:
            fragments = json.loads(cached)
        else:
            fragments = []
            for page in range(1, MAX_NOTE_PAGES + 1):
                try:
                    r = self._request(
                        "POST", "https://race.sp.netkeiba.com/",
                        data={"pid": "ajax_user_racehorse_note",
                              "input": "UTF-8", "output": "json",
                              "page": str(page), "horse_id": horse_id},
                        headers={"X-Requested-With": "XMLHttpRequest"})
                except BlockedError:
                    raise
                except NetkeibaError as e:
                    warnings.append(f"page={page}の取得に失敗（取得済み分で継続）")
                    logger.warning("馬メモページ取得失敗 horse_id=%s page=%d: %s",
                                   horse_id, page, e)
                    break
                body = r.text
                if body.startswith('"'):
                    try:
                        body = json.loads(body)
                    except json.JSONDecodeError:
                        pass
                if 'id="RaceNote-' not in body:
                    break  # 最終ページ
                fragments.append(body)
            else:
                warnings.append(
                    f"メモが{MAX_NOTE_PAGES}ページを超えたため以降は打ち切りました")
            # 空結果はキャッシュしない: ログイン失効時の空応答を
            # 「メモなし」として24時間固定化してしまう事故を防ぐ
            if fragments:
                self.repo.cache_set(key, json.dumps(fragments))
        notes: list[HorseRaceNote] = []
        seen: set[str] = set()
        for frag in fragments:
            for n in parse_note_fragment(frag):
                if n.source_race_id in seen:
                    continue  # 重複防止
                seen.add(n.source_race_id)
                notes.append(n)
        logger.info("馬メモ horse_id=%s notes=%d pages=%d",
                    horse_id, len(notes), len(fragments))
        return notes, warnings

    def get_race_result(self, race_id: str) -> list[ResultRow] | None:
        """公式レース結果（db.netkeiba.com・ログイン不要・不変キャッシュ）。"""
        key = f"result:{race_id}"
        html = self.repo.cache_get(key, TTL_RESULT)
        if html is None:
            url = f"https://db.netkeiba.com/race/{race_id}/"
            try:
                # db.netkeiba.com はモバイルUAだとSP版へ誘導され表構造が
                # 変わるため、PC版UAで取得する
                r = self._request("GET", url,
                                  headers={"User-Agent": UA_PC})
            except BlockedError:
                raise
            except NetkeibaError:
                return None
            r.encoding = r.apparent_encoding
            html = r.text
            rows_check = parse_db_result_html(html)
            if rows_check:
                self.repo.cache_set(key, html)
            else:
                logger.info("公式結果なし race_id=%s", race_id)
                return None
        rows = parse_db_result_html(html)
        return rows or None

    def check_login_works(self, sample_horse_id: str) -> bool:
        """ログイン状態でメモAPIが応答するかの軽い確認。"""
        try:
            r = self._request(
                "POST", "https://race.sp.netkeiba.com/",
                data={"pid": "ajax_user_racehorse_note", "input": "UTF-8",
                      "output": "json", "page": "1",
                      "horse_id": sample_horse_id},
                headers={"X-Requested-With": "XMLHttpRequest"})
            return r.status_code == 200
        except NetkeibaError:
            return False
