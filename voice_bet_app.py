import os
import re
import io
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

# ─── 今日のレース一覧をnetkeibaから取得 ──────────────────────
def _get_today_nichi() -> str | None:
    today = date.today()
    today_str = today.strftime("%Y%m%d")
    try:
        r = requests.get(
            "https://race.sp.netkeiba.com/?rf=navi",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=10,
        )
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None

    for a in soup.find_all("a", href=True):
        href = a["href"]
        if f"day={today_str}" in href or f"day%3D{today_str}" in href:
            parent = a.find_parent()
            ids = re.findall(r"race_id=(\d{12})", str(parent))
            if ids:
                return ids[0][8:10]

    weekday = today.isoweekday()
    nichi_candidates: dict[str, int] = {}
    for rid in re.findall(r"race_id=(\d{12})", r.text):
        if rid[:4] == today.strftime("%Y"):
            nichi_candidates[rid[8:10]] = nichi_candidates.get(rid[8:10], 0) + 1
    if nichi_candidates:
        sorted_nichi = sorted(nichi_candidates.keys())
        if weekday == 6:
            return sorted_nichi[0]
        elif weekday == 7:
            return sorted_nichi[-1]
    return None


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
def fetch_today_races() -> dict:
    today = date.today()
    today_str = today.strftime("%Y%m%d")
    yesterday = today - timedelta(days=1)
    venue_by_code = {v: k for k, v in VENUE_CODE.items()}

    def section_to_map(ids):
        race_map = {}
        for rid in ids:
            venue_name = venue_by_code.get(rid[4:6])
            race_num = int(rid[10:12])
            if venue_name:
                race_map[(venue_name, race_num)] = rid
        return race_map

    today_nichi = _get_today_nichi()
    if today_nichi:
        sections = _fetch_sections(today_str)
        for section_ids in sections:
            if section_ids and section_ids[0][8:10] == today_nichi:
                return section_to_map(section_ids)

    yesterday_sections = _fetch_sections(yesterday.strftime("%Y%m%d"))
    today_sections = _fetch_sections(today_str)
    yesterday_nichi = set(
        rid[8:10] for rid in (yesterday_sections[0] if yesterday_sections else [])
    )
    for section_ids in today_sections:
        if section_ids and section_ids[0][8:10] not in yesterday_nichi:
            return section_to_map(section_ids)

    if today_sections:
        return section_to_map(today_sections[-1])
    return {}


def get_ipat_url(venue: str, race_num: int) -> str | None:
    race_map = fetch_today_races()
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
def parse_bet(text: str) -> dict:
    text = normalize_bet_text(text.strip())
    result = {"raw": text, "type_name": None, "horses": [], "amount": 100,
              "box": False, "formation": False, "error": None}

    for name in BET_TYPES:
        if name in text:
            result["type_name"] = name
            break
    if not result["type_name"]:
        result["error"] = "馬券種別が認識できません（単勝/複勝/馬連/馬単/ワイド/3連複/3連単）"
        return result

    if re.search(r"ボックス|BOX|box", text, re.I):
        result["box"] = True
    if re.search(r"ながし|流し|軸", text):
        result["formation"] = True

    nums = re.findall(r"\d+", text)
    result["horses"] = [int(n) for n in nums if 1 <= int(n) <= 18]

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
    """ブラウザコンテキストを作成。cookie_strが指定された場合はそれを注入してユーザーセッションを引き継ぐ。"""
    context = p.chromium.launch_persistent_context(
        user_data_dir=IPAT_PROFILE,
        headless=True,
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
        return
    page.goto("https://race.sp.netkeiba.com/ipat/", timeout=20000)
    page.wait_for_load_state("domcontentloaded", timeout=10000)
    if "login" not in page.url and not page.query_selector("input[name='login_id']"):
        log_lines.append("ログイン済み")
        return
    log_lines.append("自動ログイン中...")
    try:
        page.goto(
            "https://regist.netkeiba.com/account/?pid=login"
            "&redirect=https%3A%2F%2Frace.sp.netkeiba.com%2Fipat%2F",
            timeout=20000,
        )
        page.wait_for_load_state("domcontentloaded", timeout=10000)
        page.fill("input[name='login_id']", NETKEIBA_USER)
        page.fill("input[name='pswd']", NETKEIBA_PASS)
        page.click("input[type='submit'], button[type='submit']")
        page.wait_for_load_state("domcontentloaded", timeout=15000)
        log_lines.append("ログイン完了")
    except Exception as e:
        log_lines.append(f"ログインエラー: {e}")

def _odds_view_url(base_race_url: str, bet: dict) -> str:
    m = re.search(r"race_id=(\d{12})", base_race_url)
    race_id = m.group(1) if m else ""
    cfg = BET_TYPES[bet["type_name"]]
    housiki = f"&housiki={cfg['housiki']}" if cfg["housiki"] else ""
    return (f"https://race.sp.netkeiba.com/?pid=odds_view"
            f"&type={cfg['type_code']}{housiki}&race_id={race_id}")

def _js_click(page, selector: str) -> bool:
    return page.evaluate(f"""
        (function() {{
            var el = document.querySelector("{selector}");
            if (!el) return false;
            el.click();
            return true;
        }})()
    """)

def _check_horses(page, bet: dict, log_lines: list):
    cfg = BET_TYPES[bet["type_name"]]
    frm = cfg["frm"]
    horses = bet["horses"]
    if frm == "tan_b1":
        for h in horses:
            ok = _js_click(page, f"input[value*='_b1_c0_{h}']")
            log_lines.append(f"  {'✅' if ok else '❌'} 単勝 馬番{h}")
            page.wait_for_timeout(80)
    elif frm == "tan_b2":
        for h in horses:
            ok = _js_click(page, f"input[value*='_b2_c0_{h}']")
            log_lines.append(f"  {'✅' if ok else '❌'} 複勝 馬番{h}")
            page.wait_for_timeout(80)
    elif frm in ("multi2", "multi3"):
        cols = ["frm1", "frm2"] if frm == "multi2" else ["frm1", "frm2", "frm3"]
        for col in cols:
            for h in horses:
                ok = _js_click(page, f"input[name='{col}[]'][value='{h}']")
                log_lines.append(f"  {'✅' if ok else '❌'} {col} 馬番{h}")
                page.wait_for_timeout(80)

def _click_add_button(page, bet: dict, log_lines: list):
    cfg = BET_TYPES[bet["type_name"]]
    if cfg["frm"] in ("tan_b1", "tan_b2"):
        result = page.evaluate("""
            (function() {
                var btn = document.querySelector('button.AddBtn');
                if (btn) { btn.click(); return 'clicked AddBtn'; }
                return 'AddBtn not found';
            })()
        """)
    else:
        result = page.evaluate("""
            (function() {
                if (typeof add_odds === 'function') {
                    add_odds('bet');
                    return 'add_odds(bet) called';
                }
                var btns = document.querySelectorAll('button.SubmitBtn');
                for (var b of btns) {
                    if (b.textContent.indexOf('まとめて') >= 0) {
                        b.click();
                        return 'SubmitBtn clicked';
                    }
                }
                return 'no button found';
            })()
        """)
    log_lines.append(f"  → {result}")

def _fill_amount_on_betlist(page, amount: int, log_lines: list):
    page.wait_for_timeout(800)
    filled_count = page.evaluate(f"""
        (function() {{
            var selectors = [
                'input[type="number"]',
                'input[name*="kin"]', 'input[id*="kin"]',
                'input[name*="money"]', 'input[name*="amount"]',
                'input[placeholder*="円"]', 'input[placeholder*="金額"]',
                'input[class*="Kin"]', 'input[class*="Amount"]'
            ];
            var count = 0;
            for (var sel of selectors) {{
                var els = document.querySelectorAll(sel);
                for (var el of els) {{
                    if (el.type !== 'hidden') {{
                        el.value = '{amount // 100}';
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        count++;
                    }}
                }}
            }}
            return count;
        }})()
    """)
    if filled_count > 0:
        log_lines.append(f"  ✅ 金額 {amount:,}円 入力（{filled_count}箇所）")
    else:
        log_lines.append(f"  ⚠️  金額入力欄が見つかりません。手動で {amount:,}円 を入力してください。")


def input_bets_to_netkeiba(base_race_url: str, bets: list, ipat_cookie: str | None = None) -> tuple[str, str]:
    """
    ヘッドレスPlaywrightで買い目・金額を入力する。
    ipat_cookieが指定された場合はユーザーのセッションで操作し、iPhone側でbet_listが確認できる。
    Returns: (log_str, bet_list_url)
    """
    log_lines = []
    m = re.search(r"race_id=(\d{12})", base_race_url)
    race_id = m.group(1) if m else ""
    bet_list_url = f"https://race.sp.netkeiba.com/ipat/bet_list.html?race_id={race_id}"

    with sync_playwright() as p:
        context = _make_context(p, cookie_str=ipat_cookie)
        page = context.new_page()
        _ensure_logged_in(page, log_lines)

        for i, bet in enumerate(bets):
            log_lines.append(f"\n{'='*40}")
            log_lines.append(f"買い目 {i+1}: {bet_label(bet)}")
            try:
                url = _odds_view_url(base_race_url, bet)
                log_lines.append(f"→ {url}")
                page.goto(url, timeout=30000)
                page.wait_for_load_state("domcontentloaded", timeout=15000)
                page.wait_for_timeout(500)
                _check_horses(page, bet, log_lines)
                page.wait_for_timeout(150)
                _click_add_button(page, bet, log_lines)
                page.wait_for_load_state("domcontentloaded", timeout=10000)
                _fill_amount_on_betlist(page, bet["amount"], log_lines)
            except Exception as e:
                log_lines.append(f"  ❌ エラー: {e}")

        log_lines.append("\n✅ 買い目入力完了")
        context.close()

    return "\n".join(log_lines), bet_list_url


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
    st.markdown("### 今日のレース")
    race_map = fetch_today_races()
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
                st.error(f"今日の開催に「{label}」が見つかりません。"
                         f"（今日の開催：{'、'.join(venues_today) if venues_today else 'なし'}）")
                if st.button("クリア"):
                    st.session_state.recognized = ""
                    st.rerun()
    else:
        bet = parse_bet(recognized)
        if bet["error"]:
            st.error(bet["error"])
        else:
            c1, c2, c3 = st.columns(3)
            c1.metric("馬券種別", bet["type_name"] + ("(BOX)" if bet["box"] else ""))
            c2.metric("馬番", "-".join(str(h) for h in bet["horses"]))
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
            with st.spinner("自動入力中..."):
                log, bet_list_url = input_bets_to_netkeiba(
                    st.session_state.race_url,
                    st.session_state.bets,
                    ipat_cookie=st.session_state.ipat_cookie or None,
                )
            st.success("✅ 買い目入力完了")
            st.link_button("📋 bet_list を開く（同じセッションで確認）", bet_list_url,
                           use_container_width=True)
            with st.expander("操作ログ"):
                st.text(log)

    with st.expander("買い目データ（JSON）"):
        st.json(st.session_state.bets)
