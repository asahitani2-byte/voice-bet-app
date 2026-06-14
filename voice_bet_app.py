import os
import re
import io
import time
import threading
import datetime
import urllib.parse
import requests
from bs4 import BeautifulSoup
from datetime import date, timedelta
import speech_recognition as sr
import streamlit as st
from streamlit_mic_recorder import mic_recorder
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

st.set_page_config(page_title="音声馬券メーカー", page_icon="🎤", layout="centered")

# ─── 会場名 → netkeibaコード ────────────────────────────────
VENUE_CODE = {
    "札幌": "01", "函館": "02", "福島": "03", "新潟": "04",
    "東京": "05", "中山": "06", "中京": "07", "京都": "08",
    "阪神": "09", "小倉": "10",
}
VENUE_NAMES = list(VENUE_CODE.keys())

# ─── 馬券種別定義 ───────────────────────────────────────────
BET_TYPES = {
    "単勝":  {"horses": 1, "type_code": "b1", "housiki": "",   "frm": "tan_b1"},
    "複勝":  {"horses": 1, "type_code": "b1", "housiki": "",   "frm": "tan_b2"},
    "枠連":  {"horses": 2, "type_code": "b3", "housiki": "c1", "frm": "multi2"},
    "馬連":  {"horses": 2, "type_code": "b4", "housiki": "c1", "frm": "multi2"},
    "ワイド":{"horses": 2, "type_code": "b5", "housiki": "c1", "frm": "multi2"},
    "馬単":  {"horses": 2, "type_code": "b6", "housiki": "c1", "frm": "multi2"},
    "3連複": {"horses": 3, "type_code": "b7", "housiki": "c1", "frm": "multi3"},
    "三連複":{"horses": 3, "type_code": "b7", "housiki": "c1", "frm": "multi3"},
    "3連単": {"horses": 3, "type_code": "b8", "housiki": "c1", "frm": "multi3"},
    "三連単":{"horses": 3, "type_code": "b8", "housiki": "c1", "frm": "multi3"},
}

# ─── レース取得対象日の決定 ──────────────────────────────────
def _target_date() -> date:
    """
    現在時刻に応じて取得するレース日を返す:
      金曜17:00 〜 土曜17:00 → 当該土曜
      土曜17:01 〜 日曜17:00 → 当該日曜
      それ以外               → 直近過去の日曜（前の開催）
    """
    now = datetime.datetime.now()
    wd = now.weekday()          # Mon=0, Fri=4, Sat=5, Sun=6
    t  = now.hour * 60 + now.minute   # 分換算

    CUTOFF = 17 * 60  # 17:00 = 1020分

    if wd == 4 and t >= CUTOFF:     # 金曜17:00〜 → 翌土曜
        return now.date() + timedelta(days=1)
    if wd == 5 and t <= CUTOFF:     # 土曜〜17:00 → 当日土曜
        return now.date()
    if wd == 5 and t > CUTOFF:      # 土曜17:01〜 → 翌日曜
        return now.date() + timedelta(days=1)
    if wd == 6 and t <= CUTOFF:     # 日曜〜17:00 → 当日日曜
        return now.date()

    # それ以外（月〜木・金17前・日17後）→ 直近の日曜
    d = now.date()
    while d.weekday() != 6:
        d -= timedelta(days=1)
    return d


def _fetch_sections(day_str: str) -> list[list[str]]:
    url = f"https://race.sp.netkeiba.com/?pid=race_list&day={day_str}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []
    return [
        list(dict.fromkeys(re.findall(r"race_id=(\d{12})", str(wrap))))
        for wrap in soup.find_all("div", class_="RaceListDayWrap")
    ]


@st.cache_data(ttl=300)
def fetch_today_races() -> tuple[dict, date]:
    """(race_map, target_date) を返す。"""
    target = _target_date()
    venue_by_code = {v: k for k, v in VENUE_CODE.items()}
    race_map: dict = {}
    for section_ids in _fetch_sections(target.strftime("%Y%m%d")):
        for rid in section_ids:
            venue_name = venue_by_code.get(rid[4:6])
            race_num = int(rid[10:12])
            if venue_name:
                race_map[(venue_name, race_num)] = rid
    return race_map, target


def get_ipat_url(venue: str, race_num: int) -> str | None:
    race_map, _ = fetch_today_races()
    race_id = race_map.get((venue, race_num))
    if race_id:
        return f"https://race.sp.netkeiba.com/?pid=odds_view&race_id={race_id}"
    return None

# ─── 音声テキスト振り分け ────────────────────────────────────
# ─── 馬券種別の読み仮名・誤変換マッピング ───────────────────
BET_ALIASES = {
    # 単勝
    "たんしょう": "単勝", "短小": "単勝", "短勝": "単勝",
    "単小": "単勝", "誕生": "単勝", "タンショウ": "単勝",
    # 複勝
    "ふくしょう": "複勝", "福勝": "複勝", "福小": "複勝",
    "複小": "複勝", "フクショウ": "複勝",
    # 馬連
    "うまれん": "馬連", "馬れん": "馬連", "ウマレン": "馬連",
    # 馬単
    "うまたん": "馬単", "馬たん": "馬単", "ウマタン": "馬単",
    # ワイド
    "わいど": "ワイド", "ワイト": "ワイド",
    # 枠連
    "わくれん": "枠連", "枠れん": "枠連", "ワクレン": "枠連",
    # 3連複
    "さんれんぷく": "3連複", "三連服": "3連複", "三連福": "3連複",
    "3連服": "3連複", "3連福": "3連複", "サンレンプク": "3連複",
    "さんれんふく": "3連複",
    # 3連単
    "さんれんたん": "3連単", "サンレンタン": "3連単",
    "さんれん単": "3連単", "3連たん": "3連単",
}

def normalize_bet_text(text: str) -> str:
    """音声認識の読み仮名・誤変換を正式な馬券種別名に置換する"""
    for alias, official in BET_ALIASES.items():
        if alias in text:
            text = text.replace(alias, official)
    return text

RACE_PATTERN = re.compile(
    r"(" + "|".join(VENUE_NAMES) + r").*?(\d+)\s*(?:レース|Ｒ|R)",
    re.IGNORECASE,
)

def classify_text(text: str) -> str:
    if RACE_PATTERN.search(text):
        return "race"
    if any(v in text for v in VENUE_NAMES):
        return "race"
    return "bet"

def parse_race_spec(text: str) -> dict:
    m = RACE_PATTERN.search(text)
    if m:
        return {"venue": m.group(1), "race_num": int(m.group(2)), "error": None}
    for venue in VENUE_NAMES:
        if venue in text:
            nums = [int(n) for n in re.findall(r"\d{1,2}", text) if 1 <= int(n) <= 12]
            if nums:
                return {"venue": venue, "race_num": nums[-1], "error": None}
    return {"error": "会場名とレース番号が認識できませんでした"}

# ─── 買い目パース ────────────────────────────────────────────
def _parse_frm_groups(text: str, n_cols: int) -> list[list[int]] | None:
    """
    「1頭目1番 2頭目2番3番4番」→ [[1],[2,3,4]] のように列ごとの馬番リストを返す。
    「頭目」「投目」どちらも受け付け、番号は漢数字・全角・半角すべて対応。
    「番」なしの数字も認識。見つからない場合はNone。
    """
    _COL = {"一": 1, "二": 2, "三": 3, "１": 1, "２": 2, "３": 3,
            "1": 1, "2": 2, "3": 3}

    markers = []
    for m in re.finditer(r"([1-3１-３一二三])[頭投]目", text):
        col_num = _COL.get(m.group(1), 0)
        if col_num:
            markers.append((m.start(), m.end(), col_num))

    if not markers:
        return None

    groups: list[list[int]] = [[] for _ in range(n_cols)]

    for i, (start, end, col_num) in enumerate(markers):
        seg_end = markers[i + 1][0] if i + 1 < len(markers) else len(text)
        # マーカー終端以降から馬番を抽出（「番」なし数字も対象）
        segment = text[end:seg_end]
        nums = [int(n) for n in re.findall(r"\d+", segment) if 1 <= int(n) <= 18]
        if 1 <= col_num <= n_cols:
            groups[col_num - 1] = nums

    return groups if any(groups) else None


def parse_bet(text: str) -> dict:
    text = normalize_bet_text(text.strip())
    result = {"raw": text, "type_name": None, "horses": [], "amount": 100,
              "box": False, "formation": False, "frm_groups": None, "error": None}

    for name in BET_TYPES:
        if name in text:
            result["type_name"] = name
            break
    if not result["type_name"]:
        result["error"] = "馬券種別が認識できません（単勝/複勝/馬連/馬単/ワイド/3連複/3連単）"
        return result

    if re.search(r"ボックス|BOX|box", text, re.I):
        result["box"] = True
    if re.search(r"フォーメーション|フォーメ|formation", text, re.I):
        result["formation"] = True
    elif re.search(r"ながし|流し|軸", text):
        result["formation"] = True

    nums = re.findall(r"\d+", text)
    result["horses"] = [int(n) for n in nums if 1 <= int(n) <= 18]

    # フォーメーション：列ごとの馬番グループを解析
    if result["formation"] and not result["box"]:
        cfg = BET_TYPES.get(result["type_name"], {})
        frm = cfg.get("frm", "")
        if frm in ("multi2", "multi3"):
            n_cols = 2 if frm == "multi2" else 3
            groups = _parse_frm_groups(text, n_cols)
            if groups:
                result["frm_groups"] = groups
                result["horses"] = [h for g in groups for h in g]

    m = re.search(r"(\d+)\s*(千円|百円|円)", text)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        if unit == "千円":
            n *= 1000
        elif unit == "百円":
            n *= 100
        result["amount"] = n
    else:
        _KANJI = {"": 1, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9}
        m2 = re.search(r"([一二三四五六七八九]?)(千|百)円", text)
        if m2:
            prefix = _KANJI.get(m2.group(1), 1)
            unit_val = 1000 if m2.group(2) == "千" else 100
            result["amount"] = prefix * unit_val
        else:
            large = [int(n) for n in nums if int(n) > 18]
            if large:
                result["amount"] = large[-1]

    return result

def bet_label(bet: dict) -> str:
    if bet.get("frm_groups"):
        groups_str = " → ".join(
            ",".join(str(h) for h in g) for g in bet["frm_groups"] if g
        )
        return f"{bet['type_name']}(フォーメ)  {groups_str}  {bet['amount']:,}円"
    horses = "-".join(str(h) for h in bet["horses"])
    suffix = "（BOX）" if bet["box"] else "（ながし）" if bet["formation"] else ""
    return f"{bet['type_name']}{suffix}  {horses}  {bet['amount']:,}円"

# ─── 音声→テキスト変換 ──────────────────────────────────────
def transcribe_audio(audio_bytes: bytes) -> str:
    import subprocess
    import imageio_ffmpeg
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg_exe, "-y", "-i", "pipe:0",
         "-f", "wav", "-ar", "16000", "-ac", "1", "pipe:1"],
        input=audio_bytes,
        capture_output=True,
    )
    if not result.stdout:
        return f"ERROR:ffmpeg変換失敗 {result.stderr.decode()[-200:]}"
    recognizer = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(result.stdout)) as source:
        audio_data = recognizer.record(source)
    try:
        return recognizer.recognize_google(audio_data, language="ja-JP")
    except sr.UnknownValueError:
        return ""
    except sr.RequestError as e:
        return f"ERROR:{e}"

# ─── netkeiba Playwright 自動入力 ────────────────────────────
IPAT_PROFILE = os.path.join(os.path.expanduser("~"), ".netkeiba_ipat_profile")
NETKEIBA_USER = os.environ.get("NETKEIBA_USER", "")
NETKEIBA_PASS = os.environ.get("NETKEIBA_PASS", "")

def _make_context(p, cookie_str: str | None = None):
    context = p.chromium.launch_persistent_context(
        user_data_dir=IPAT_PROFILE,
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
        viewport={"width": 390, "height": 844},
    )
    if cookie_str:
        cookies = []
        for part in cookie_str.split(";"):
            part = part.strip()
            if "=" in part:
                name, _, value = part.partition("=")
                for domain in ["race.sp.netkeiba.com", ".netkeiba.com"]:
                    cookies.append({"name": name.strip(), "value": value.strip(),
                                    "domain": domain, "path": "/"})
        if cookies:
            context.add_cookies(cookies)
    return context

def _ensure_logged_in(page, log_lines: list):
    """未ログインの場合、環境変数の認証情報でnetkeibaにログインする"""
    if not NETKEIBA_USER or not NETKEIBA_PASS:
        log_lines.append("⚠️ 環境変数 NETKEIBA_USER / NETKEIBA_PASS が未設定")
        return
    log_lines.append(f"IPAT確認中...")
    page.goto("https://race.sp.netkeiba.com/ipat/", timeout=20000)
    page.wait_for_load_state("domcontentloaded", timeout=10000)
    log_lines.append(f"  現在URL: {page.url}")

    # ページ内のinput要素のname一覧を診断ログに出す
    input_names = page.evaluate("""
        () => Array.from(document.querySelectorAll('input')).map(e => e.name || e.id || e.type).filter(Boolean)
    """)
    log_lines.append(f"  input要素: {input_names}")

    need_login = "login" in page.url or bool(page.query_selector("input[name='login_id'], input[name='email'], input[type='email']"))
    if not need_login:
        log_lines.append("  → ログイン済み")
        return

    log_lines.append("自動ログイン中...")
    try:
        page.goto(
            "https://regist.netkeiba.com/account/?pid=login"
            "&redirect=https%3A%2F%2Frace.sp.netkeiba.com%2Fipat%2F",
            timeout=20000,
        )
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        log_lines.append(f"  ログインページURL: {page.url}")
        input_names2 = page.evaluate("""
            () => Array.from(document.querySelectorAll('input')).map(e => e.name || e.id || e.type).filter(Boolean)
        """)
        log_lines.append(f"  ログインページinput要素: {input_names2}")

        # 複数パターンに対応
        for sel in ["input[name='login_id']", "input[name='email']", "input[type='email']"]:
            if page.query_selector(sel):
                page.fill(sel, NETKEIBA_USER)
                log_lines.append(f"  ID入力: {sel}")
                break
        for sel in ["input[name='pswd']", "input[name='password']", "input[type='password']"]:
            if page.query_selector(sel):
                page.fill(sel, NETKEIBA_PASS)
                log_lines.append(f"  PW入力: {sel}")
                break
        page.click("input[type='submit'], button[type='submit']")
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        log_lines.append(f"  ログイン後URL: {page.url}")
    except Exception as e:
        log_lines.append(f"ログインエラー: {e}")

# IPAT式別コード（bet_id生成用）
IPAT_SHIKIBETU = {
    "単勝": 1, "複勝": 2, "枠連": 3, "馬連": 4,
    "ワイド": 5, "馬単": 6,
    "3連複": 7, "三連複": 7, "3連単": 8, "三連単": 8,
}

def _build_ipat_bet_id(race_id: str, shikibetu: int, horses: list[int]) -> str:
    """
    IPAT bet_id形式: a{曜日}-{nichi}-{race}_b{式別}_c0_{馬番...}
    曜日 = JS getDay() of date(year, venue_as_month, kai_as_day)
    """
    year  = int(race_id[0:4])
    venue = int(race_id[4:6])   # 場コード → 月として使用
    kai   = int(race_id[6:8])   # 回 → 日として使用
    nichi = race_id[8:10]
    race_num = int(race_id[10:12])

    try:
        d = datetime.date(year, venue, kai)
        # Python weekday(): Mon=0, Sun=6 → JS getDay(): Sun=0, Mon=1, ..., Sat=6
        js_day = (d.weekday() + 1) % 7
    except ValueError:
        js_day = 0

    horses_part = "_".join(str(h) for h in horses)
    return f"a{js_day}-{nichi}-{race_num}_b{shikibetu}_c0_{horses_part}"


def input_bets_to_netkeiba(base_race_url: str, bets: list, ipat_cookie: str | None = None) -> tuple[str, str]:
    """
    可視ブラウザでodds_viewを開き、券種選択・馬番チェック・買い目追加・金額入力まで自動化する。
    完了後もブラウザを開いたままにし、ユーザーが確認・投票できる状態にする。
    Returns: (log_str, bet_url)
    """
    m = re.search(r"race_id=(\d{12})", base_race_url)
    race_id = m.group(1) if m else ""
    bet_url = f"https://race.sp.netkeiba.com/?pid=bet&race_id={race_id}"
    bets_copy = list(bets)
    result: dict = {"log": "", "done": False}

    def _run():
        log_lines = []
        with sync_playwright() as p:
            context = _make_context(p, cookie_str=ipat_cookie)
            page = context.new_page()
            _ensure_logged_in(page, log_lines)

            for i, bet in enumerate(bets_copy):
                log_lines.append(f"\n{'='*40}")
                log_lines.append(f"買い目 {i+1}: {bet_label(bet)}")

                cfg = BET_TYPES.get(bet["type_name"])
                if not cfg:
                    log_lines.append(f"  ❌ 未対応: {bet['type_name']}")
                    continue

                # 券種別 odds_view URLへ
                housiki_param = f"&housiki={cfg['housiki']}" if cfg["housiki"] else ""
                url = (f"https://race.sp.netkeiba.com/?pid=odds_view"
                       f"&type={cfg['type_code']}{housiki_param}&race_id={race_id}")
                page.goto(url, timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
                log_lines.append(f"  ページ: {page.url}")

                # 馬番チェック
                horses = bet["horses"]
                frm = cfg["frm"]
                frm_groups = bet.get("frm_groups")

                if frm in ("tan_b1", "tan_b2"):
                    b_code = "1" if frm == "tan_b1" else "2"
                    for h in horses:
                        ok = page.evaluate(
                            f"() => {{ var e=document.querySelector(\"input[value*='_b{b_code}_c0_{h}']\");"
                            f" if(e){{e.click();return true;}} return false; }}"
                        )
                        log_lines.append(f"  {'✅' if ok else '❌'} 馬番{h}")
                elif frm in ("multi2", "multi3"):
                    n_cols = 2 if frm == "multi2" else 3
                    if frm_groups:
                        # フォーメーション：列ごとに複数馬番
                        for col_idx, group in enumerate(frm_groups[:n_cols]):
                            col = f"frm{col_idx + 1}"
                            for h in group:
                                ok = page.evaluate(
                                    f"() => {{ var e=document.querySelector(\"input[name='{col}[]'][value='{h}']\");"
                                    f" if(e){{e.click();return true;}} return false; }}"
                                )
                                log_lines.append(f"  {'✅' if ok else '❌'} {col} 馬番{h}")
                    else:
                        # 通常：1列1頭
                        for col_idx in range(min(n_cols, len(horses))):
                            h = horses[col_idx]
                            col = f"frm{col_idx + 1}"
                            ok = page.evaluate(
                                f"() => {{ var e=document.querySelector(\"input[name='{col}[]'][value='{h}']\");"
                                f" if(e){{e.click();return true;}} return false; }}"
                            )
                            log_lines.append(f"  {'✅' if ok else '❌'} {col} 馬番{h}")

                page.wait_for_timeout(300)

                # 買い目追加ボタンをクリック
                click_result = page.evaluate("""() => {
                    var btn = document.querySelector('button.AddBtn');
                    if (btn) { btn.click(); return 'AddBtn'; }
                    var btns = document.querySelectorAll('button.SubmitBtn');
                    for (var b of btns) {
                        if (b.textContent.includes('まとめて')) { b.click(); return 'SubmitBtn(まとめて)'; }
                    }
                    return 'ボタン未検出';
                }""")
                log_lines.append(f"  ボタン: {click_result}")

                page.wait_for_load_state("networkidle", timeout=8000)
                page.wait_for_timeout(800)
                log_lines.append(f"  遷移後: {page.url}")

            # ?pid=bet ページへ（金額入力）
            page.goto(bet_url, timeout=20000)
            page.wait_for_load_state("domcontentloaded", timeout=15000)
            page.wait_for_timeout(2000)
            log_lines.append(f"\n金額入力ページ: {page.url}")

            # 金額フィールドを探して入力（100円単位）
            # bet_listは追加の逆順（新しいものが上）で表示されるため逆インデックスで対応
            n_bets = len(bets_copy)
            for i, bet in enumerate(bets_copy):
                amount_100 = bet["amount"] // 100
                el_idx = n_bets - 1 - i
                filled = page.evaluate(f"""() => {{
                    var els = Array.from(document.querySelectorAll(
                        'input[type="number"], input[class*="Kin"], input[class*="kin"], input[name*="money"], input[name*="kin"]'
                    )).filter(e => e.type !== 'hidden');
                    var el = els[{el_idx}];
                    if (el) {{
                        el.value = '{amount_100}';
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                    return false;
                }}""")
                log_lines.append(f"  買い目{i+1} 金額{bet['amount']:,}円: {'✅' if filled else '❌'}")

            log_lines.append("\n✅ 入力完了 — ブラウザで確認してIPAT投票を進めてください")
            result["log"] = "\n".join(log_lines)
            result["done"] = True
            page.bring_to_front()

            try:
                page.wait_for_event("close", timeout=1800000)
            except Exception:
                pass
            context.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()

    for _ in range(120):
        if result["done"]:
            break
        time.sleep(0.5)

    return result.get("log", "タイムアウト: ブラウザの準備に時間がかかっています"), bet_url


# ════════════════════════════════════════
# メインアプリ
# ════════════════════════════════════════
st.title("🎤 音声馬券メーカー")
st.caption("「東京7レース」でレース選択 → 「3連複 1-3-5 各100円」で買い目追加")

# セッション初期化
for key, default in [
    ("bets", []), ("recognized", ""), ("race_url", ""), ("race_label", ""),
    ("ipat_cookie", ""),
]:
    if key not in st.session_state:
        st.session_state[key] = default

# ─── サイドバー ───────────────────────────────────────────────
with st.sidebar:
    race_map, race_date = fetch_today_races()
    weekday_ja = ["月", "火", "水", "木", "金", "土", "日"][race_date.weekday()]
    st.markdown(f"### {race_date.month}/{race_date.day}({weekday_ja})のレース")
    if race_map:
        venues_today = sorted(set(v for v, _ in race_map.keys()))
        st.caption(f"開催：{'、'.join(venues_today)}")
    else:
        st.caption("レース情報を取得できませんでした")
        venues_today = []

    if st.session_state.race_url:
        st.success(f"選択中: **{st.session_state.race_label}**")
        if st.button("レースを変更"):
            st.session_state.race_url = ""
            st.session_state.race_label = ""
            st.rerun()

    st.divider()
    with st.expander("🔑 IPATセッション連携", expanded=not st.session_state.ipat_cookie):
        st.caption("iPhoneのSafariでnetkeibaを開き、アドレスバーに\n`javascript:prompt('',document.cookie)`\nと入力して表示されたCookieを貼り付けてください")
        cookie_input = st.text_area("Cookie文字列", value=st.session_state.ipat_cookie,
                                     placeholder="netkeibaipat=xxx; _gcl_au=...", height=80, key="cookie_input")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("連携する", use_container_width=True):
                st.session_state.ipat_cookie = cookie_input.strip()
                st.rerun()
        with c2:
            if st.button("解除", use_container_width=True):
                st.session_state.ipat_cookie = ""
                st.rerun()
        if st.session_state.ipat_cookie:
            st.success("✅ セッション連携中")
        else:
            st.warning("未連携（サーバー側プロファイルを使用）")

    st.divider()
    st.markdown("### 買い目一覧")
    if st.session_state.bets:
        for i, b in enumerate(st.session_state.bets):
            col1, col2 = st.columns([5, 1])
            col1.markdown(f"`{i+1}.` {bet_label(b)}")
            if col2.button("✕", key=f"del_{i}"):
                st.session_state.bets.pop(i)
                st.rerun()
        st.divider()
        if st.button("🗑️ 全削除", use_container_width=True):
            st.session_state.bets = []
            st.rerun()
    else:
        st.caption("まだ買い目がありません")

# ─── 音声入力 ─────────────────────────────────────────────────
if not st.session_state.race_url:
    st.markdown("#### ① レースを選んでください")
    st.caption("「東京7レース」「京都11レース」のように話してください")
else:
    st.markdown(f"#### ② 買い目を入力 — {st.session_state.race_label}")
    st.caption("「3連複 1-3-5 各100円」「単勝 7番 500円」のように話してください")

audio = mic_recorder(
    start_prompt="🎤 録音開始",
    stop_prompt="⏹️ 録音停止",
    just_once=True,
    use_container_width=True,
    key="mic",
)

if audio and audio.get("bytes"):
    with st.spinner("音声を認識中..."):
        text = transcribe_audio(audio["bytes"])
    if text.startswith("ERROR:"):
        st.error(f"認識エラー: {text}")
    elif text:
        st.session_state.recognized = text
    else:
        st.warning("音声を認識できませんでした。もう一度試してください。")

with st.expander("手動入力"):
    manual = st.text_input("テキスト", placeholder="例：東京7レース　or　3連複 1-3-5 各100円")
    if st.button("確定"):
        if manual:
            st.session_state.recognized = manual

# ─── 認識結果の処理 ──────────────────────────────────────────
recognized = st.session_state.recognized
if recognized:
    st.divider()
    st.markdown(f"**認識テキスト：** `{recognized}`")

    kind = classify_text(recognized)

    if kind == "race":
        spec = parse_race_spec(recognized)
        if spec["error"]:
            st.error(spec["error"])
            if st.button("クリア"):
                st.session_state.recognized = ""
                st.rerun()
        else:
            venue, race_num = spec["venue"], spec["race_num"]
            url = get_ipat_url(venue, race_num)
            label = f"{venue} {race_num}R"
            if url:
                st.success(f"レース確認：**{label}**")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("✅ このレースで確定", type="primary", use_container_width=True):
                        st.session_state.race_url = url
                        st.session_state.race_label = label
                        st.session_state.recognized = ""
                        st.rerun()
                with col2:
                    if st.button("✕ キャンセル", use_container_width=True):
                        st.session_state.recognized = ""
                        st.rerun()
            else:
                st.error(f"{race_date.month}/{race_date.day}の開催に「{label}」が見つかりません。"
                         f"（開催：{'、'.join(venues_today) if venues_today else 'なし'}）")
                if st.button("クリア"):
                    st.session_state.recognized = ""
                    st.rerun()
    else:
        bet = parse_bet(recognized)
        if bet["error"]:
            st.error(bet["error"])
        else:
            c1, c2, c3 = st.columns(3)
            type_label = bet["type_name"] + (
                "(フォーメ)" if bet.get("frm_groups") else
                "(BOX)" if bet["box"] else ""
            )
            if bet.get("frm_groups"):
                horses_label = " → ".join(
                    ",".join(str(h) for h in g) for g in bet["frm_groups"] if g
                )
            else:
                horses_label = "-".join(str(h) for h in bet["horses"])
            c1.metric("馬券種別", type_label)
            c2.metric("馬番", horses_label)
            c3.metric("金額", f"{bet['amount']:,}円")
            col1, col2 = st.columns(2)
            with col1:
                if st.button("✅ 買い目に追加", type="primary", use_container_width=True):
                    st.session_state.bets.append(bet)
                    st.session_state.recognized = ""
                    st.rerun()
            with col2:
                if st.button("✕ キャンセル", use_container_width=True):
                    st.session_state.recognized = ""
                    st.rerun()

# ─── netkeiba 反映 ────────────────────────────────────────────
if st.session_state.bets:
    st.divider()
    st.markdown("### netkeiba に反映")

    if not st.session_state.race_url:
        st.warning("先にレースを音声で選択してください（「東京7レース」など）")
    else:
        st.info(f"**{len(st.session_state.bets)}件** → {st.session_state.race_label}")
        if st.session_state.ipat_cookie:
            st.success("🔑 セッション連携中 — 完了後にiPhoneでbet_listを開けます")
        else:
            st.caption("※ IPATセッション未連携のため、iPhone側で買い目を確認できません")

        if st.button("🏇 netkeiba に自動入力", type="primary", use_container_width=True):
            with st.spinner("ブラウザを起動して買い目を入力中..."):
                log, _ = input_bets_to_netkeiba(
                    st.session_state.race_url,
                    st.session_state.bets,
                    ipat_cookie=st.session_state.ipat_cookie or None,
                )
            st.success("✅ 買い目入力完了 — 開いたブラウザで「IPAT投票へすすむ」を押してください")
            with st.expander("操作ログ"):
                st.text(log)

    with st.expander("買い目データ（JSON）"):
        st.json(st.session_state.bets)
