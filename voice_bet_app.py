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
import streamlit.components.v1 as components
from streamlit_mic_recorder import mic_recorder
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

st.set_page_config(page_title="音声馬券メーカー", page_icon="🎤", layout="centered")

# ─── Myロジック分析（分離パッケージ）＋アプリ簡易認証 ───────
# APP_PASSWORD が設定されている場合のみ認証ゲートが有効になる
from my_logic.ui import require_app_password, render_mylogic_section
require_app_password()

# ─── 会場名 → netkeibaコード ────────────────────────────────
VENUE_CODE = {
    "札幌": "01", "函館": "02", "福島": "03", "新潟": "04",
    "東京": "05", "中山": "06", "中京": "07", "京都": "08",
    "阪神": "09", "小倉": "10",
}
VENUE_NAMES = list(VENUE_CODE.keys())

# ─── 地方競馬場コード ────────────────────────────────────────
# 地方競馬場コード（NAR race_id・楽天jcd 共通）
# race_id形式: {年4}{場コード2}{月2}{日2}{R番号2}
LOCAL_VENUE_CODE = {
    "門別": "30", "盛岡": "35", "水沢": "36",
    "浦和": "42", "船橋": "43", "大井": "44", "川崎": "45",
    "金沢": "46", "笠松": "47", "名古屋": "48",
    "園田": "50", "姫路": "51",
    "高知": "54", "佐賀": "55",
    "帯広": "65",
}
LOCAL_VENUE_NAMES = list(LOCAL_VENUE_CODE.keys())
ALL_VENUE_NAMES   = VENUE_NAMES + LOCAL_VENUE_NAMES

# NAR 予想ページ用場コード（LOCAL_VENUE_CODE と同一）
NAR_YOSO_CODE = LOCAL_VENUE_CODE

# 競馬場ローマ字（keiba-lv-st.jp track_id 用）
VENUE_ROMAJI = {
    "門別": "monbetsu", "盛岡": "morioka",  "水沢": "mizusawa",
    "浦和": "urawa",    "船橋": "funabashi", "大井": "oi",
    "川崎": "kawasaki", "金沢": "kanazawa",  "笠松": "kasamatsu",
    "名古屋": "nagoya", "園田": "sonoda",    "姫路": "himeji",
    "高知": "kochi",    "佐賀": "saga",      "帯広": "obihiro",
}

# 楽天競馬 式別クラス名
RAKUTEN_TYPE_CLASS = {
    "単勝": "type-tan", "複勝": "type-fuku",
    "枠連": "type-waku-fuku", "馬連": "type-uma-fuku",
    "ワイド": "type-wide", "馬単": "type-uma-tan",
    "3連複": "type-san-ren-fuku", "三連複": "type-san-ren-fuku",
    "3連単": "type-san-ren-tan",  "三連単": "type-san-ren-tan",
}

# 予想印スコア
YOSO_SCORE_MAP = {"Honmei": 3, "Taikou": 2, "Kurosan": 1, "Hoshi": 1, "Osae": 1}
MARK_SYMBOLS   = ["◎", "○", "▲", "△", "☆1", "☆2", "☆3"]
CANDIDATE_PAIRS = [
    ("◎","○"),("◎","▲"),("○","▲"),("◎","△"),("○","△"),
    ("◎","☆1"),("○","☆1"),("◎","☆2"),("○","☆2"),("◎","☆3"),("○","☆3"),
]

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
    # 日付指定は day= ではなく kaisai_date=（day= は無視され今週分が返る）
    url = f"https://race.sp.netkeiba.com/?pid=race_list&kaisai_date={day_str}"
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []
    wraps = soup.find_all("div", class_="RaceListDayWrap")
    # ページには週内の全開催日のセクションが含まれる。日付タブ
    # （data-date属性）とセクションは同順なので、指定日のみに絞る。
    # 別日の race_id が同一会場・同一R番号を上書きするのを防ぐ。
    dates = [a.get("data-date")
             for a in soup.select(".Tab_RaceDaySelect a[data-date]")]
    if day_str in dates and len(dates) == len(wraps):
        wraps = [wraps[dates.index(day_str)]]
    else:
        visible = [w for w in wraps
                   if "display:none" not in re.sub(r"\s", "", w.get("style") or "")]
        wraps = visible or wraps
    return [
        list(dict.fromkeys(re.findall(r"race_id=(\d{12})", str(wrap))))
        for wrap in wraps
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

@st.cache_data(ttl=300)
def fetch_nar_race_id(venue: str, race_num: int, day_str: str) -> str | None:
    """race_id = {年4}{場コード2}{月2}{日2}{R番号2} を直接計算して返す"""
    code = LOCAL_VENUE_CODE.get(venue)
    if not code:
        return None
    # day_str は YYYYMMDD 形式
    return f"{day_str[:4]}{code}{day_str[4:6]}{day_str[6:8]}{race_num:02d}"

def get_race_info(venue: str, race_num: int) -> dict:
    """レース情報 {url, race_id, label, is_local} を返す"""
    if venue in LOCAL_VENUE_NAMES:
        today = datetime.date.today()
        day_str = today.strftime("%Y%m%d")
        jcd = LOCAL_VENUE_CODE.get(venue, "")
        rakuten_url = (
            f"https://bet.keiba.rakuten.co.jp/bet/normal/?jcd={jcd}&hd={day_str}&rno={race_num}"
        ) if jcd else None
        race_id = fetch_nar_race_id(venue, race_num, day_str) or ""
        return {"url": rakuten_url, "race_id": race_id,
                "label": f"{venue} {race_num}R（地方）", "is_local": True}
    race_map, _ = fetch_today_races()
    race_id = race_map.get((venue, race_num), "")
    url = f"https://race.sp.netkeiba.com/?pid=odds_view&race_id={race_id}" if race_id else None
    return {"url": url, "race_id": race_id,
            "label": f"{venue} {race_num}R", "is_local": False}

def _assign_marks(scores: dict, overrides: dict | None) -> dict:
    mark_to_horse: dict = {}
    if overrides:
        for m, h in overrides.items():
            if h and int(h) > 0:
                mark_to_horse[m] = int(h)
    sorted_h = sorted(scores.keys(), key=lambda h: scores[h], reverse=True)
    remaining  = [h for h in sorted_h if h not in mark_to_horse.values()]
    rem_marks  = [m for m in MARK_SYMBOLS if m not in mark_to_horse]
    for m, h in zip(rem_marks, remaining):
        mark_to_horse[m] = h
    return mark_to_horse

def suggest_bets(scores: dict, overrides: dict | None, odds: dict) -> list[dict]:
    mth = _assign_marks(scores, overrides)
    all_c = []
    for m1, m2 in CANDIDATE_PAIRS:
        h1, h2 = mth.get(m1), mth.get(m2)
        if not h1 or not h2:
            continue
        key = tuple(sorted([h1, h2]))
        all_c.append({"h1": h1, "h2": h2, "mark1": m1, "mark2": m2, "odds": odds.get(key, 0.0)})
    best_n = len(all_c)
    for n in range(min(11, len(all_c)), 0, -1):
        if not odds:
            best_n = n; break
        hi = sum(1 for c in all_c[:n] if c["odds"] >= n * 3)
        if hi / n >= 0.7:
            best_n = n; break
    for i, c in enumerate(all_c):
        c["selected"] = i < best_n
    return all_c

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
    r"(" + "|".join(ALL_VENUE_NAMES) + r").*?(\d+)\s*(?:レース|Ｒ|R)",
    re.IGNORECASE,
)

def classify_text(text: str) -> str:
    if RACE_PATTERN.search(text):
        return "race"
    if any(v in text for v in ALL_VENUE_NAMES):
        return "race"
    return "bet"

def try_parse_combined(text: str):
    """レース指定＋馬券指定が同一テキストにある場合を検出する。
    Returns {'race': race_spec, 'bet': bet} or None。"""
    m = RACE_PATTERN.search(text)
    if not m:
        return None
    race_spec = {"venue": m.group(1), "race_num": int(m.group(2)), "error": None}
    remaining = (text[:m.start()] + text[m.end():]).strip()
    if not remaining:
        return None
    bet = parse_bet(remaining)
    if bet["error"] or not bet["horses"]:
        return None
    return {"race": race_spec, "bet": bet}

def parse_race_spec(text: str) -> dict:
    m = RACE_PATTERN.search(text)
    if m:
        return {"venue": m.group(1), "race_num": int(m.group(2)), "error": None}
    for venue in ALL_VENUE_NAMES:
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

# ─── 馬名指定の買い目（中央のみ）────────────────────────────
def _kana_norm(s: str) -> str:
    """ひらがな→カタカナ変換＋空白/記号除去（馬名照合用）"""
    s = re.sub(r"[\s・、。･]", "", s)
    out = []
    for ch in s:
        o = ord(ch)
        if 0x3041 <= o <= 0x3096:  # ぁ-ゖ → ァ-ヶ
            ch = chr(o + 0x60)
        out.append(ch)
    return "".join(out).upper()


def _extract_umaban_from_row(tr) -> int | None:
    """出馬表SP版の行から馬番を取得する。

    注意: tr の id="tr_{N}" のNは馬番ではなく表示順の連番
    （50音順などで並び替わる）。正しい馬番は
      1. 印選択 select の option value「{馬番}_{印}」の先頭
      2. オッズ/人気 span の id「odds-1_{馬番2桁}」の末尾
    から取得する。どちらも無い場合のみ tr id にフォールバック。
    """
    opt = tr.select_one("td.Horse_Select select option")
    if opt:
        m = re.match(r"(\d+)_", opt.get("value") or "")
        if m:
            return int(m.group(1))
    sp = tr.select_one("span[id^='odds-'], span[id^='ninki-']")
    if sp:
        m = re.search(r"-\d+_(\d+)$", sp.get("id") or "")
        if m:
            return int(m.group(1))
    m = re.match(r"tr_(\d+)", tr.get("id") or "")
    return int(m.group(1)) if m else None


@st.cache_data(ttl=3600, show_spinner=False)
def fetch_jra_entries(day_key: str) -> list[dict]:
    """当日の中央全レースの出走馬一覧
    [{venue, race_num, race_id, umaban, name}, ...] を返す。"""
    import concurrent.futures
    race_map, _ = fetch_today_races()

    def _one(item):
        (venue, race_num), rid = item
        url = f"https://race.sp.netkeiba.com/race/shutuba.html?race_id={rid}"
        try:
            r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=10)
            soup = BeautifulSoup(r.text, "html.parser")
        except Exception:
            return []
        rows = []
        for tr in soup.select("tr.HorseList"):
            a = tr.select_one("dt.Horse a")
            if not a:
                continue
            name = a.get_text(strip=True)
            umaban = _extract_umaban_from_row(tr)
            if name and umaban:
                rows.append({"venue": venue, "race_num": race_num, "race_id": rid,
                             "umaban": umaban, "name": name})
        return rows

    entries: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
        for rows in ex.map(_one, list(race_map.items())):
            entries.extend(rows)
    return entries


def find_horse_today(query: str) -> list[dict]:
    """当日の中央出走馬から馬名をあいまい検索して上位3件を返す。"""
    import difflib
    race_map, target = fetch_today_races()
    if not race_map:
        return []
    entries = fetch_jra_entries(target.strftime("%Y%m%d"))
    qn = _kana_norm(query)
    if not qn:
        return []
    scored = []
    for e in entries:
        en = _kana_norm(e["name"])
        if not en:
            continue
        if qn == en:
            score = 1.0
        elif qn in en or en in qn:
            score = 0.95
        else:
            score = difflib.SequenceMatcher(None, qn, en).ratio()
        if score >= 0.6:
            scored.append((score, e))
    scored.sort(key=lambda x: -x[0])
    return [dict(e, score=s) for s, e in scored[:3]]


def try_parse_name_bet(text: str) -> dict | None:
    """「馬名 単勝/複勝 金額」形式を検出する。
    Returns {'type_name', 'amount', 'query'} or None。
    馬番の数字指定がある場合は None（既存の番号買いに委ねる）。"""
    text = normalize_bet_text(text.strip())
    if RACE_PATTERN.search(text) or any(v in text for v in ALL_VENUE_NAMES):
        return None

    type_name = next((n for n in ("単勝", "複勝") if n in text), None)
    if not type_name:
        return None
    rest = text.replace(type_name, " ")

    amount = 100
    m = re.search(r"(\d+)\s*(千円|百円|円)", rest)
    if m:
        n, unit = int(m.group(1)), m.group(2)
        amount = n * 1000 if unit == "千円" else n * 100 if unit == "百円" else n
        rest = rest[:m.start()] + " " + rest[m.end():]
    else:
        _KANJI = {"": 1, "一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
                  "六": 6, "七": 7, "八": 8, "九": 9}
        m2 = re.search(r"([一二三四五六七八九]?)(千|百)円", rest)
        if m2:
            amount = _KANJI.get(m2.group(1), 1) * (1000 if m2.group(2) == "千" else 100)
            rest = rest[:m2.start()] + " " + rest[m2.end():]

    # 馬番らしき数字（1〜18）が残っていれば番号買い
    if any(1 <= int(n) <= 18 for n in re.findall(r"\d+", rest)):
        return None

    tokens = re.findall(r"[ぁ-んァ-ヶヴーa-zA-Zａ-ｚＡ-Ｚ]+", rest)
    tokens = [re.sub(r"[のをでにはがと]+$", "", t) for t in tokens]
    _NOISE = {"ください", "お願いします", "お願い", "買って", "買い", "円", "番"}
    tokens = [t for t in tokens if len(t) >= 2 and t not in _NOISE]
    if not tokens:
        return None
    query = max(tokens, key=len)
    return {"type_name": type_name, "amount": amount, "query": query}


# ─── AI意図解釈（Claude API）＋オッズ条件買い（中央のみ）────
# JRAオッズJSON APIの式別コード
JRA_ODDS_TYPE = {
    "単勝": "1", "複勝": "2", "枠連": "3", "馬連": "4",
    "ワイド": "5", "馬単": "6",
    "3連複": "7", "三連複": "7", "3連単": "8", "三連単": "8",
}


@st.cache_data(ttl=120, show_spinner=False)
def fetch_jra_odds_table(race_id: str, type_name: str) -> list[dict]:
    """JRAオッズJSON APIから指定券種の全組み合わせオッズを取得。
    [{"horses": [1,2,3], "odds": 123.4, "pop": 5}, ...]（人気順）を返す。"""
    code = JRA_ODDS_TYPE.get(type_name)
    if not code:
        return []
    url = (f"https://race.netkeiba.com/api/api_get_jra_odds.html"
           f"?race_id={race_id}&type={code}")
    try:
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        odds_map = r.json()["data"]["odds"].get(code, {})
    except Exception:
        return []
    rows = []
    for key, vals in odds_map.items():
        try:
            odds = float(str(vals[0]).replace(",", ""))
            pop = int(str(vals[2]).replace(",", "") or 0)
        except (ValueError, IndexError, TypeError):
            continue
        if odds <= 0:  # 発売前・取消
            continue
        rows.append({
            "horses": [int(key[i:i + 2]) for i in range(0, len(key), 2)],
            "odds": odds, "pop": pop,
        })
    rows.sort(key=lambda x: x["pop"] or 10 ** 9)
    return rows


AI_INTENT_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {"type": "string", "enum": ["bet", "race_select", "unknown"]},
        "venue": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "race_num": {"anyOf": [{"type": "integer"}, {"type": "null"}]},
        "bet_type": {"anyOf": [
            {"type": "string",
             "enum": ["単勝", "複勝", "枠連", "馬連", "ワイド", "馬単",
                      "3連複", "3連単"]},
            {"type": "null"},
        ]},
        "horses": {"type": "array", "items": {"type": "integer"}},
        "horse_name": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "box": {"type": "boolean"},
        "odds_min": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "odds_max": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "amount_per_bet": {"type": "integer"},
    },
    "required": ["intent", "venue", "race_num", "bet_type", "horses",
                 "horse_name", "box", "odds_min", "odds_max",
                 "amount_per_bet"],
    "additionalProperties": False,
}

AI_SYSTEM_PROMPT = """あなたは競馬の音声馬券アプリ「WINVOICE」のコマンド解釈エンジンです。
音声認識されたユーザー発話から、馬券購入の意図を抽出してください。

## フィールドの規則
- intent: 買い目・購入条件の指定があれば "bet"、レース選択だけなら "race_select"、競馬と無関係・解釈不能なら "unknown"
- venue: 競馬場名。中央=札幌/函館/福島/新潟/東京/中山/中京/京都/阪神/小倉、地方=門別/盛岡/水沢/浦和/船橋/大井/川崎/金沢/笠松/名古屋/園田/姫路/高知/佐賀/帯広。発話に無ければ null
- race_num: レース番号 (1〜12)。無ければ null
- bet_type: 券種。読み仮名や音声誤変換も正規化する（さんれんたん/三連単→3連単、大正/短小→単勝、うまれん→馬連 など）
- horses: 発話で明示された馬番のみ (1〜18)。金額やオッズの数字は含めない
- horse_name: 馬名で指定された場合のみ設定（カタカナ/ひらがな）。無ければ null
- box: 「ボックス」指定があれば true
- odds_min / odds_max: オッズ条件。「100倍つく」「100倍以上」→ odds_min=100。「50倍以下」→ odds_max=50。「100倍前後」→ odds_min=70, odds_max=150 程度。条件が無ければ null
- amount_per_bet: 1点あたりの金額（円）。「各500円」「500円ずつ」→500、「千円」→1000。指定が無ければ 100

## 例
「東京8レースで、3連単が100倍つく買い目を全て買って」
→ intent=bet, venue=東京, race_num=8, bet_type=3連単, horses=[], horse_name=null, box=false, odds_min=100, odds_max=null, amount_per_bet=100

「中山10レースの馬連で30倍以下を各500円」
→ intent=bet, venue=中山, race_num=10, bet_type=馬連, horses=[], horse_name=null, box=false, odds_min=null, odds_max=30, amount_per_bet=500
"""


@st.cache_data(ttl=600, show_spinner=False)
def ai_parse_intent(text: str, api_key: str) -> dict:
    """Claude APIで発話を構造化された購入意図に変換する。"""
    import anthropic
    import json as _json
    try:
        client = anthropic.Anthropic(api_key=api_key)
        resp = client.messages.create(
            model="claude-opus-4-8",
            max_tokens=1024,
            system=AI_SYSTEM_PROMPT,
            output_config={
                "effort": "low",
                "format": {"type": "json_schema", "schema": AI_INTENT_SCHEMA},
            },
            messages=[{"role": "user", "content": text}],
        )
        if resp.stop_reason == "refusal":
            return {"error": "AIが解釈を拒否しました"}
        txt = next(b.text for b in resp.content if b.type == "text")
        return _json.loads(txt)
    except Exception as e:
        return {"error": f"AI解釈エラー: {e}"}


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


def _release_profile_lock():
    """永続プロファイルのロック解除 + exit_type を Normal にリセット"""
    import json

    # 1. SingletonLock の PID を強制終了
    lock_path = os.path.join(IPAT_PROFILE, "SingletonLock")
    if os.path.islink(lock_path) or os.path.exists(lock_path):
        try:
            target = os.readlink(lock_path)
            pid_str = target.rsplit("-", 1)[-1]
            subprocess.run(["kill", "-9", pid_str], capture_output=True)
            time.sleep(1)
        except Exception:
            pass

    # 2. ロックファイルをすべて削除
    for name in ["SingletonLock", "SingletonSocket", "SingletonCookie"]:
        p = os.path.join(IPAT_PROFILE, name)
        try:
            if os.path.islink(p) or os.path.exists(p):
                os.unlink(p)
        except Exception:
            pass

    # 3. Default/lockfile を削除
    lockfile = os.path.join(IPAT_PROFILE, "Default", "lockfile")
    try:
        if os.path.exists(lockfile):
            os.unlink(lockfile)
    except Exception:
        pass

    # 4. Preferences の exit_type を Normal に設定（「プロファイルエラー」防止）
    prefs_path = os.path.join(IPAT_PROFILE, "Default", "Preferences")
    if os.path.exists(prefs_path):
        try:
            with open(prefs_path, "r", encoding="utf-8") as f:
                prefs = json.load(f)
            changed = False
            profile = prefs.setdefault("profile", {})
            if profile.get("exit_type") != "Normal":
                profile["exit_type"] = "Normal"
                changed = True
            if not profile.get("exited_cleanly", True):
                profile["exited_cleanly"] = True
                changed = True
            if changed:
                with open(prefs_path, "w", encoding="utf-8") as f:
                    json.dump(prefs, f)
        except Exception:
            pass

    time.sleep(0.3)


def setup_netkeiba_login() -> str:
    """可視ブラウザを開いてnetkeibaにログインし、セッションをプロファイルに永続保存する。"""
    _release_profile_lock()
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=IPAT_PROFILE,
                headless=False,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
                viewport={"width": 390, "height": 844},
            )
            page = context.new_page()
            page.bring_to_front()
            page.goto("https://user.sp.netkeiba.com/", timeout=20000)
            page.wait_for_load_state("domcontentloaded", timeout=10000)
            if "user.sp.netkeiba.com" in page.url:
                context.close()
                return "✅ すでにログイン済みです（セッション保存済み）"
            page.goto("https://regist.sp.netkeiba.com/?rf=navi", timeout=20000)
            page.bring_to_front()
            try:
                page.wait_for_function(
                    "() => !location.href.includes('regist.sp.netkeiba.com')",
                    timeout=300000,
                )
                context.close()
                return "✅ ログイン完了（セッション保存済み）"
            except Exception:
                context.close()
                return "⚠️ タイムアウト — もう一度お試しください"
    except Exception as e:
        return f"❌ エラー: {e}"


# ○(U+25CB) → ◯(U+25EF) など netkeiba ドロップダウン表示に合わせた変換
_LABEL_TO_TSUBA = {
    "◎": "◎", "○": "◯", "▲": "▲", "△": "△",
    "☆1": "☆", "☆2": "☆", "☆3": "☆",
}


def _fill_shutuba_marks(page, horse_marks: dict) -> list[str]:
    """
    netkeiba 出馬表ページで各馬の .tzSelect ドロップダウンに印を入力する。
    各行は id="tr_{N}" で直接アクセスする。
    horse_marks: {horse_num(int): mark_text(str)}
    """
    log = []
    for horse_num, mark_text in sorted(horse_marks.items()):
        n = int(horse_num)
        display = _LABEL_TO_TSUBA.get(mark_text, mark_text)
        try:
            opened = page.evaluate(f"""() => {{
                var row = document.querySelector('#tr_{n}');
                if (!row) return 'no_row';
                var tz = row.querySelector('.tzSelect');
                if (!tz) return 'no_tz';
                var sb = tz.querySelector('.selectBox');
                if (!sb) return 'no_sb';
                sb.click();
                return 'ok';
            }}""")
            if opened != "ok":
                log.append(f"  {n}番 → {mark_text}: ❌ ({opened})")
                continue
            page.wait_for_timeout(300)
            selected = page.evaluate(f"""() => {{
                var row = document.querySelector('#tr_{n}');
                if (!row) return false;
                var spans = row.querySelectorAll('.tzSelect .dropDown li span');
                for (var i = 0; i < spans.length; i++) {{
                    if (spans[i].textContent.trim() === '{display}') {{
                        spans[i].click();
                        return true;
                    }}
                }}
                return false;
            }}""")
            page.wait_for_timeout(250)
            log.append(f"  {n}番 → {mark_text}({display}): {'✅' if selected else '❌(印テキスト不一致)'}")
        except Exception as e:
            log.append(f"  {n}番 → {mark_text}: ❌ {e}")
    return log


def _focus_winvoice_browser():
    """macOS: localhost:8501 タブを含む Chrome ウィンドウを最前面に戻す"""
    script = """
tell application "Google Chrome"
    activate
    set theWindows to every window
    repeat with w in theWindows
        set theTabs to every tab of w
        repeat with t in theTabs
            if URL of t contains "localhost:8501" then
                set active tab of w to t
                set index of w to 1
                return
            end if
        end repeat
    end repeat
end tell
"""
    try:
        subprocess.run(["osascript", "-e", script], timeout=5, capture_output=True)
    except Exception:
        pass


def _make_context(p, cookie_str: str | None = None, pc_mode: bool = False):
    w, h = (1280, 900) if pc_mode else (390, 844)
    context = p.chromium.launch_persistent_context(
        user_data_dir=IPAT_PROFILE,
        headless=False,
        args=["--no-sandbox", "--disable-dev-shm-usage",
              f"--window-size={w},{h}", "--no-first-run", "--no-default-browser-check"],
        viewport={"width": w, "height": h},
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


def input_bets_to_netkeiba(base_race_url: str, bets: list,
                          ipat_cookie: str | None = None,
                          shutuba_info: dict | None = None) -> tuple[str, str]:
    """
    可視ブラウザでodds_viewを開き、券種選択・馬番チェック・買い目追加・金額入力まで自動化する。
    shutuba_info: {"race_id": str, "is_nar": bool, "horse_marks": {num: sym}}
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
            # bet_listは式別コード順（単勝→複勝→馬連…）で表示されるため、
            # IPAT_SHIKIBETUでソートしてページの並びに合わせる
            sorted_indices = sorted(
                range(len(bets_copy)),
                key=lambda j: IPAT_SHIKIBETU.get(bets_copy[j]["type_name"], 99)
            )
            for rank, orig_idx in enumerate(sorted_indices):
                bet = bets_copy[orig_idx]
                amount_100 = bet["amount"] // 100
                filled = page.evaluate(f"""() => {{
                    var els = Array.from(document.querySelectorAll('input.InputMoney'));
                    var el = els[{rank}];
                    if (el) {{
                        el.value = '{amount_100}';
                        el.dispatchEvent(new Event('input', {{bubbles: true}}));
                        el.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}
                    return false;
                }}""")
                log_lines.append(f"  買い目{orig_idx+1} 金額{bet['amount']:,}円: {'✅' if filled else '❌'}")

            log_lines.append("\n✅ 入力完了 — ブラウザで確認してIPAT投票を進めてください")
            result["log"] = "\n".join(log_lines)
            result["done"] = True

            # ── 出馬表への印入力 ──────────────────────────────────
            if shutuba_info and shutuba_info.get("horse_marks"):
                try:
                    sid  = shutuba_info["race_id"]
                    snar = shutuba_info["is_nar"]
                    shutuba_url = (
                        f"https://nar.sp.netkeiba.com/race/shutuba.html?race_id={sid}"
                        if snar else
                        f"https://race.sp.netkeiba.com/race/shutuba.html?race_id={sid}"
                    )
                    sp = context.new_page()
                    sp.goto(shutuba_url, timeout=30000)
                    sp.wait_for_load_state("domcontentloaded", timeout=15000)
                    sp.wait_for_timeout(2000)
                    mark_log = _fill_shutuba_marks(sp, shutuba_info["horse_marks"])
                    log_lines.extend(["", "📋 出馬表への印入力:"] + mark_log)
                    result["log"] = "\n".join(log_lines)
                    sp.bring_to_front()
                except Exception as _e:
                    log_lines.append(f"⚠️ 出馬表印入力エラー: {_e}")
                    result["log"] = "\n".join(log_lines)
            else:
                page.bring_to_front()

            _focus_winvoice_browser()

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


def input_bets_to_rakuten(race_url: str, bets: list,
                         shutuba_info: dict | None = None,
                         venue: str = "") -> tuple[str, str]:
    result = {"log": "", "done": False, "start_time": ""}

    def _run():
        log_lines = []
        with sync_playwright() as p:
            ctx = _make_context(p, pc_mode=True)
            pg = ctx.new_page()
            try:
                # ── メインページを開いてテーブルから競馬場を探す ──
                m_rno = re.search(r"rno=(\d+)", race_url)
                rno   = str(int(m_rno.group(1))) if m_rno else ""
                pg.goto("https://bet.keiba.rakuten.co.jp/bet/normal?l-id=keiba_header_bet",
                        timeout=30000)
                pg.wait_for_load_state("domcontentloaded", timeout=15000)
                pg.wait_for_timeout(1500)
                log_lines.append(f"ページ: {pg.url}")

                start_time = ""
                if venue and rno:
                    info = pg.evaluate(f"""() => {{
                        var rows = document.querySelectorAll('table.commonTable tbody tr');
                        for (var row of rows) {{
                            var th = row.querySelector('th');
                            if (th && th.textContent.includes('{venue}')) {{
                                var a = row.querySelector('a.voteContent[racenumber="{rno}"]');
                                if (a) {{
                                    var td = a.closest('td');
                                    var sp = td ? td.querySelector('.start') : null;
                                    var t = sp ? sp.textContent.trim() : '';
                                    a.click();
                                    return t;
                                }}
                            }}
                        }}
                        return '';
                    }}""")
                    start_time = info or ""
                    clicked = bool(start_time is not None)
                    log_lines.append(f"レースリンク({venue} {rno}R 発走{start_time}): {'✅' if clicked else '❌'}")
                    pg.wait_for_load_state("domcontentloaded", timeout=10000)
                    pg.wait_for_timeout(1500)

                for i, bet in enumerate(bets):
                    log_lines.append(f"\n{'='*30}")
                    log_lines.append(f"買い目 {i+1}: {bet_label(bet)}")
                    type_cls = RAKUTEN_TYPE_CLASS.get(bet["type_name"])
                    if not type_cls:
                        log_lines.append(f"  ❌ 未対応式別: {bet['type_name']}"); continue

                    ok = pg.evaluate(f"""() => {{
                        var a = document.querySelector('li.{type_cls} a');
                        if (a) {{ a.click(); return true; }}
                        return false;
                    }}""")
                    log_lines.append(f"  式別: {'✅' if ok else '❌'}")
                    pg.wait_for_timeout(400)

                    col_names = ["kaime1", "kaime2", "kaime3"]
                    for ci, h in enumerate(bet["horses"][:3]):
                        hh = str(h).zfill(2)
                        col = col_names[ci]
                        ok = pg.evaluate(f"""() => {{
                            var tr = document.querySelector('tr#umaban_{hh}');
                            if (!tr) return false;
                            var a = tr.querySelector('td.link.selecter.{col} a');
                            if (a) {{ a.click(); return true; }}
                            return false;
                        }}""")
                        log_lines.append(f"  馬番{h}({col}): {'✅' if ok else '❌'}")
                        pg.wait_for_timeout(200)

                    amt = bet["amount"] // 100
                    ok = pg.evaluate(f"""() => {{
                        var inp = document.querySelector('#baseAmount');
                        if (!inp) return false;
                        inp.value = '{amt}';
                        inp.dispatchEvent(new Event('change', {{bubbles: true}}));
                        return true;
                    }}""")
                    log_lines.append(f"  金額({bet['amount']:,}円): {'✅' if ok else '❌'}")

                    ok = pg.evaluate("""() => {
                        var btn = document.querySelector('li.kaimeSet a.confirmBtn.set');
                        if (btn) { btn.click(); return true; }
                        return false;
                    }""")
                    log_lines.append(f"  セット: {'✅' if ok else '❌'}")
                    pg.wait_for_timeout(800)

                log_lines.append("\n✅ 楽天競馬 入力完了 — ブラウザで投票内容を確認してください")
                result["log"]        = "\n".join(log_lines)
                result["start_time"] = start_time
                result["done"]       = True

                # ── 出馬表への印入力 ──────────────────────────────
                if shutuba_info and shutuba_info.get("horse_marks"):
                    try:
                        sid  = shutuba_info["race_id"]
                        snar = shutuba_info["is_nar"]
                        shutuba_url = (
                            f"https://nar.sp.netkeiba.com/race/shutuba.html?race_id={sid}"
                            if snar else
                            f"https://race.sp.netkeiba.com/race/shutuba.html?race_id={sid}"
                        )
                        sp = ctx.new_page()
                        sp.goto(shutuba_url, timeout=30000)
                        sp.wait_for_load_state("domcontentloaded", timeout=15000)
                        sp.wait_for_timeout(2000)
                        mark_log = _fill_shutuba_marks(sp, shutuba_info["horse_marks"])
                        log_lines.extend(["", "📋 出馬表への印入力:"] + mark_log)
                        result["log"] = "\n".join(log_lines)
                        sp.bring_to_front()
                    except Exception as _e:
                        log_lines.append(f"⚠️ 出馬表印入力エラー: {_e}")
                        result["log"] = "\n".join(log_lines)
                else:
                    pg.bring_to_front()

                _focus_winvoice_browser()

                try:
                    pg.wait_for_event("close", timeout=1800000)
                except Exception:
                    pass
            except Exception as e:
                result["log"] = f"エラー: {e}"
                result["done"] = True
            finally:
                ctx.close()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    for _ in range(120):
        if result["done"]: break
        time.sleep(0.5)
    return result.get("log", "タイムアウト"), result.get("start_time", "")


def fetch_umaren_odds(race_id: str, is_nar: bool = True) -> dict:
    """馬連オッズを {(h1,h2): float} で返す（Playwright 永続プロファイル headless）"""
    result: dict = {"odds": {}, "done": False}

    def _parse_odds_table(soup) -> dict:
        odds: dict = {}
        for tbl in soup.find_all("table"):
            th = tbl.find("th")
            if not th: continue
            bm = re.search(r"\d+", th.text)
            if not bm: continue
            base_h = int(bm.group())
            for row in tbl.find_all("tr")[1:]:
                cols = row.find_all("td")
                if len(cols) < 2: continue
                try:
                    h2  = int(re.search(r"\d+", cols[0].text).group())
                    ods = float(cols[1].text.strip().replace(",", ""))
                    if ods > 0:
                        odds[tuple(sorted([base_h, h2]))] = ods
                except Exception:
                    pass
        return odds

    def _run():
        _release_profile_lock()
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=IPAT_PROFILE,
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage",
                      "--no-first-run", "--no-default-browser-check"],
                viewport={"width": 430, "height": 900},
            )
            pg = ctx.new_page()
            try:
                if is_nar:
                    pg.goto(
                        f"https://nar.sp.netkeiba.com/odds/?race_id={race_id}&type=b4",
                        timeout=20000,
                    )
                    pg.wait_for_load_state("networkidle", timeout=15000)
                else:
                    pg.goto(
                        f"https://race.sp.netkeiba.com/?pid=odds_view&type=b4&housiki=c1&race_id={race_id}",
                        timeout=20000,
                    )
                    pg.wait_for_load_state("domcontentloaded", timeout=15000)
                    pg.wait_for_timeout(800)
                    # chk_frm1_* と chk_frm2_* を全てチェック
                    pg.evaluate("""() => {
                        document.querySelectorAll(
                            'input[type="checkbox"][name^="chk_frm1_"], input[type="checkbox"][name^="chk_frm2_"]'
                        ).forEach(cb => {
                            if (!cb.checked) {
                                cb.checked = true;
                                cb.dispatchEvent(new Event('change', {bubbles: true}));
                                cb.dispatchEvent(new Event('click',  {bubbles: true}));
                            }
                        });
                    }""")
                    pg.wait_for_timeout(600)
                    # オッズ表示ボタンをクリック（複数セレクタを順番に試す）
                    pg.evaluate("""() => {
                        var btn = document.querySelector('#btn_display')
                               || document.querySelector('.btn_odds_disp')
                               || document.querySelector('input[type="submit"]')
                               || document.querySelector('button[type="submit"]')
                               || document.querySelector('.btnOdds');
                        if (btn) btn.click();
                    }""")
                    pg.wait_for_load_state("networkidle", timeout=20000)
                    pg.wait_for_timeout(1000)

                soup = BeautifulSoup(pg.content(), "html.parser")
                result["odds"] = _parse_odds_table(soup)
            except Exception:
                pass
            finally:
                ctx.close()
        result["done"] = True

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    for _ in range(60):
        if result["done"]: break
        time.sleep(0.5)
    return result["odds"]


def _make_auto_overrides(scores: dict) -> dict:
    """スコアランキング順に ◎○▲△☆1☆2☆3 を自動割り当て"""
    marks = ["◎", "○", "▲", "△", "☆1", "☆2", "☆3"]
    top = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return {marks[i]: top[i][0] for i in range(min(len(marks), len(top)))}


def buy_and_fetch_yoso(race_id: str, is_nar: bool) -> dict | None:
    race_path = "nar" if is_nar else "jra"
    yoso_url  = f"https://yoso.sp.netkeiba.com/race/{race_path}/yoso_list.html?race_id={race_id}"
    result: dict = {"data": None, "done": False}

    def _run():
        _release_profile_lock()
        with sync_playwright() as p:
            ctx = p.chromium.launch_persistent_context(
                user_data_dir=IPAT_PROFILE,
                headless=False,
                args=[
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--window-size=430,900",
                    "--no-first-run",
                    "--no-default-browser-check",
                ],
                viewport={"width": 430, "height": 900},
            )
            pg = ctx.new_page()
            try:
                # ── Step 1: yoso_list を開いて即座に前面表示 ──
                pg.goto(yoso_url, timeout=30000)
                pg.wait_for_load_state("domcontentloaded", timeout=15000)
                pg.bring_to_front()
                pg.wait_for_timeout(1200)

                # ── Step 2: 上位5名のチェックボックスをクリック ──
                for i in range(5):
                    pg.evaluate(
                        f"() => {{ var ls=document.querySelectorAll('label.Yoso_CheckLabel');"
                        f" if(ls[{i}]) ls[{i}].click(); }}"
                    )
                    pg.wait_for_timeout(400)

                # ── Step 3: 一括購入ボタン ──
                pg.evaluate("() => { var b=document.querySelector('#multi_buy'); if(b) b.click(); }")
                pg.wait_for_load_state("domcontentloaded", timeout=10000)
                pg.wait_for_timeout(1000)

                # ── Step 4: 購入確定 → nar_buy_yoso_complete を待つ ──
                pg.evaluate("() => { var b=document.querySelector('#duplication'); if(b) b.click(); }")
                try:
                    pg.wait_for_url("**buy_yoso_complete**", timeout=20000)
                except Exception:
                    pass
                pg.wait_for_load_state("domcontentloaded", timeout=10000)
                pg.wait_for_timeout(1000)

                # ── Step 5: 印集計ページに遷移してパース ──
                if is_nar:
                    mark_url = f"https://nar.sp.netkeiba.com/yoso/yoso_mark_list.html?race_id={race_id}"
                else:
                    mark_url = f"https://race.sp.netkeiba.com/yoso/mark_list.html?race_id={race_id}"
                pg.goto(mark_url, timeout=20000)
                pg.wait_for_load_state("domcontentloaded", timeout=15000)
                pg.wait_for_timeout(1000)

                soup = BeautifulSoup(pg.content(), "html.parser")
                _ICON_SYM = {
                    "Honmei": "◎", "Taikou": "○", "Kurosan": "▲",
                    "Hoshi": "☆", "Osae": "△",
                }

                def _parse_container(container, seq, use_index: bool):
                    """
                    use_index=True  → JRA: リスト内の順番（1始まり）が馬番
                    use_index=False → NAR: mark_N クラスの N が馬番
                    """
                    dt = container.find("dt")
                    pred_name = dt.get_text(strip=True) if dt else f"予想家{seq+1}"
                    pred_marks: dict[int, str] = {}
                    lis = container.find_all("li", class_=re.compile(r"Mark_Pro"))
                    for idx, li in enumerate(lis):
                        if use_index:
                            hn = idx + 1
                        else:
                            hn = None
                            for cls_name in li.get("class", []):
                                if cls_name.startswith("mark_") and cls_name[5:].isdigit():
                                    hn = int(cls_name[5:])
                                    break
                            if hn is None or not (1 <= hn <= 18): continue
                        span = li.find("span", class_=re.compile(r"Icon_Shirushi"))
                        if not span: continue
                        cls_str = " ".join(span.get("class", []))
                        if "Icon_Shirushi" not in cls_str: continue
                        for key, sc in YOSO_SCORE_MAP.items():
                            if f"Icon_{key}" in cls_str:
                                pred_marks[hn] = _ICON_SYM.get(key, key)
                                break
                    return pred_name, pred_marks

                horse_scores: dict[int, int] = {}
                predictors: list[dict] = []

                # コンテナ検出: dl#yoso_goods_seq_N
                containers = [soup.find("dl", id=f"yoso_goods_seq_{seq}") for seq in range(5)]
                containers = [c for c in containers if c is not None]
                if not containers:
                    containers = list(soup.find_all(True, id=re.compile(r"yoso_goods_seq")))

                # NAR=mark_Nクラスが馬番、JRA=リスト順が馬番
                use_index = not is_nar

                for seq, container in enumerate(containers[:5]):
                    pred_name, pred_marks = _parse_container(container, seq, use_index)
                    for hn, sym in pred_marks.items():
                        for key, sc in YOSO_SCORE_MAP.items():
                            if _ICON_SYM.get(key) == sym:
                                horse_scores[hn] = horse_scores.get(hn, 0) + sc
                                break
                    if pred_marks:
                        predictors.append({"name": pred_name, "marks": pred_marks})

                result["data"] = {"scores": horse_scores, "predictors": predictors}
            except Exception as e:
                result["data"] = {"error": str(e)}
            finally:
                result["done"] = True
                try:
                    ctx.close()
                except Exception:
                    pass
                # まず AppleScript で既存タブにフォーカス、次に Chrome 指定で URL を開く
                _focus_winvoice_browser()
                subprocess.run(
                    ["open", "-a", "Google Chrome", "http://localhost:8501/"],
                    capture_output=True,
                )

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    for _ in range(240):
        if result["done"]: break
        time.sleep(0.5)
    return result.get("data")


# ════════════════════════════════════════
# メインアプリ
# ════════════════════════════════════════

# キービジュアルをページ最上部にフル幅で表示
st.markdown("""<style>
.block-container { padding-top: 0 !important; }
[data-testid="stImage"] { margin: 0 !important; }
[data-testid="stImage"] img { display: block; width: 100%; }
header[data-testid="stHeader"] { display: none; }

/* ── マイクリップル ── */
@keyframes wv-ripple {
  0%   { transform:translate(-50%,-50%) scale(1);   opacity:.55; }
  100% { transform:translate(-50%,-50%) scale(2.0); opacity:0;   }
}
/* ── マイクの脈動グロー ── */
@keyframes wv-glow {
  0%,100% { box-shadow:0 0 30px rgba(200,168,76,.45),0 0 60px rgba(200,168,76,.18); }
  50%     { box-shadow:0 0 55px rgba(200,168,76,.75),0 0 110px rgba(200,168,76,.32); }
}
.mic-stage {
    display:flex; flex-direction:column; align-items:center;
    margin:22px 0 4px; position:relative;
}
.mic-ripple {
    position:absolute; top:50%; left:50%;
    transform:translate(-50%,-50%) scale(1);
    width:152px; height:152px; border-radius:50%;
    border:2px solid rgba(200,168,76,.5);
    animation:wv-ripple 2s ease-out infinite;
    pointer-events:none;
}
.mic-ripple:nth-child(2) { animation-delay:.65s; }
.mic-ripple:nth-child(3) { animation-delay:1.3s; }
.mic-btn-circle {
    position:relative; z-index:2;
    width:152px; height:152px; border-radius:50%;
    background:radial-gradient(circle at 38% 35%,#E0B840 0%,#A07018 48%,#5C3D08 100%);
    border:3px solid rgba(255,215,100,.75);
    display:flex; flex-direction:column;
    align-items:center; justify-content:center;
    gap:4px;
    animation:wv-glow 2.2s ease-in-out infinite;
    cursor:pointer;
    user-select:none;
    -webkit-tap-highlight-color:transparent;
    transition:transform .12s;
}
.mic-btn-circle:active { transform:scale(.94); }
.mic-icon { font-size:60px; line-height:1; }
@keyframes wv-rec-glow {
  0%,100% { box-shadow:0 0 30px rgba(255,75,75,.55),0 0 60px rgba(255,75,75,.2); }
  50%     { box-shadow:0 0 55px rgba(255,75,75,.85),0 0 110px rgba(255,75,75,.4); }
}
.mic-btn-circle.recording {
    background:radial-gradient(circle at 38% 35%,#FF7070 0%,#CC2020 48%,#800010 100%);
    border-color:rgba(255,120,120,.8);
    animation:wv-rec-glow 1s ease-in-out infinite;
}
/* 実ボタン(iframe)は機能させたまま非表示 */
div[data-testid="stCustomComponentV1"] {
    height:1px !important;
    overflow:hidden !important;
    margin:0 !important;
    padding:0 !important;
    opacity:0 !important;
    pointer-events:none !important;
}
/* ── 音声認識バブル ── */
.voice-bubble {
    background: rgba(255,255,255,.08);
    border: 1px solid rgba(200,168,76,.25);
    border-radius: 14px 14px 14px 4px;
    padding: 12px 16px;
    font-style: italic;
    color: #000 !important;
    margin: 10px 0;
    font-size: .92rem;
}
/* ── 買い目リスト行 ── */
.bet-row {
    display: flex;
    align-items: center;
    background: rgba(255,255,255,.05);
    border: 1px solid rgba(200,168,76,.18);
    border-radius: 8px;
    padding: 10px 14px;
    margin-bottom: 6px;
    gap: 8px;
}
.bet-index {
    background: rgba(200,168,76,.25);
    color: #C8A84C !important;
    font-weight: 700;
    font-size: .78rem;
    border-radius: 50%;
    width: 22px; height: 22px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
}
.bet-text { flex: 1; font-size: .88rem; color: #eee !important; }
/* ── 転送ボタン（大） ── */
.wv-transfer-btn .stButton > button {
    background: linear-gradient(135deg, #C8A84C 0%, #7A5010 100%) !important;
    font-size: 1.05rem !important;
    padding: .7rem 1rem !important;
    border-radius: 12px !important;
    box-shadow: 0 4px 20px rgba(200,168,76,.35) !important;
    color: #0B1F0E !important;
}
/* ── ログカード ── */
.log-card {
    background: rgba(0,0,0,.35);
    border-radius: 10px;
    padding: 12px 14px;
    font-size: .78rem;
    color: #aaa !important;
    font-family: monospace;
    white-space: pre-wrap;
    max-height: 200px;
    overflow-y: auto;
}
</style>""", unsafe_allow_html=True)

import pathlib as _pl
_kv_path = _pl.Path(__file__).parent / "keymage.png"
if _kv_path.exists():
    st.image(str(_kv_path), use_container_width=True)

st.title("WinVoice")

# セッション初期化
for key, default in [
    ("bets", []), ("recognized", ""), ("race_url", ""), ("race_label", ""),
    ("ipat_cookie", ""), ("race_id", ""), ("is_local", False),
    ("yoso_result", None), ("yoso_race_id", ""), ("yoso_odds", {}),
    ("yoso_predictors", []),
    ("bet_suggestion", None), ("yoso_overrides", None),
    ("last_transfer_log", ""),
    ("race_start_time", ""), ("notify_pending", False),
    ("anthropic_key", os.environ.get("ANTHROPIC_API_KEY", "")),
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
    with st.expander("🔐 netkeiba ログイン設定", expanded=False):
        st.caption("一度ログインするとセッションが永続保存されます")
        if st.button("ブラウザを開いてログイン", use_container_width=True, key="btn_login_setup"):
            with st.spinner("ブラウザでnetkeibaにログインしてください..."):
                _login_result = setup_netkeiba_login()
            if "完了" in _login_result or "済み" in _login_result:
                st.success(_login_result)
            else:
                st.warning(_login_result)

    st.divider()
    with st.expander("🤖 AI設定（自然言語解釈）", expanded=False):
        st.caption("「東京8レースで3連単が100倍つく買い目を全部買って」のような"
                   "自然な発話の解釈に Claude API を使用します")
        st.text_input("Anthropic APIキー", type="password", key="anthropic_key",
                      placeholder="sk-ant-...")
        if st.session_state.anthropic_key.strip():
            st.success("✅ AI解釈 有効")
        else:
            st.info("未設定（ルールベース解釈のみ）")

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
    st.caption("買い目が決まっている場合はレース名と買い目を、決まっていない場合はレース名だけを話しかけてください。買い目を分析提案します。中央なら「馬名 単勝 500円」のように馬名だけでもOK。AI設定済みなら「東京8レースで3連単が100倍つく買い目を全部」のような自然な発話も解釈します。")
else:
    st.caption(f"買い目を入力 — {st.session_state.race_label}　「単勝 7番 500円」など")

st.markdown("""<div class="mic-stage">
  <div class="mic-ripple"></div>
  <div class="mic-ripple"></div>
  <div class="mic-ripple"></div>
  <div class="mic-btn-circle" id="wvMicBtn">
    <span class="mic-icon" id="wvMicIcon">🎙️</span>
  </div>
</div>""", unsafe_allow_html=True)

audio = mic_recorder(
    start_prompt="録音開始",
    stop_prompt="⏹ 録音停止",
    just_once=True,
    use_container_width=False,
    key="mic",
)

_mic_ctrl_js = """<script>
(function(){
  var _observer = null;
  var _frame    = null;

  function findBtn(){
    var frames = window.parent.document.querySelectorAll('iframe');
    for(var i=0;i<frames.length;i++){
      if(frames[i]===window.frameElement) continue;
      try{
        var d=frames[i].contentDocument||frames[i].contentWindow.document;
        var b=d.querySelector('button.myButton');
        if(b){ _frame=frames[i]; return b; }
      }catch(e){}
    }
    return null;
  }

  function overlayFrame(){
    if(!_frame) return;
    var circle = window.parent.document.getElementById('wvMicBtn');
    if(!circle) return;
    var r = circle.getBoundingClientRect();
    _frame.style.cssText = [
      'position:fixed',
      'top:'+r.top+'px',
      'left:'+r.left+'px',
      'width:'+r.width+'px',
      'height:'+r.height+'px',
      'border-radius:50%',
      'opacity:0',
      'z-index:9999',
      'border:none',
      'cursor:pointer',
    ].join(';');
    var d = _frame.contentDocument;
    if(d && !d.getElementById('wv-style')){
      var s = d.createElement('style');
      s.id = 'wv-style';
      s.textContent = 'body,#root,.App{margin:0;padding:0;width:100%;height:100vh}button.myButton{width:100%;height:100%;border:none;background:transparent;cursor:pointer;}';
      d.head.appendChild(s);
    }
  }

  function syncVisual(){
    var btn    = findBtn();
    var circle = window.parent.document.getElementById('wvMicBtn');
    var icon   = window.parent.document.getElementById('wvMicIcon');
    if(!circle) return;
    var isRec  = btn && (btn.textContent||'').includes('停止');
    if(isRec){
      circle.classList.add('recording');
      if(icon) icon.textContent = '⏹';
    } else {
      circle.classList.remove('recording');
      if(icon) icon.textContent = '🎙️';
    }
    overlayFrame();
  }

  function attachObserver(){
    var btn = findBtn();
    if(!btn) return false;
    overlayFrame();
    if(_observer) _observer.disconnect();
    _observer = new MutationObserver(syncVisual);
    _observer.observe(btn, {childList:true,characterData:true,subtree:true,attributes:true});
    syncVisual();
    return true;
  }

  var init = setInterval(function(){
    if(attachObserver()) clearInterval(init);
  }, 200);

  setInterval(function(){
    var btn = findBtn();
    if(_observer && !btn){ _observer.disconnect(); _observer=null; _frame=null; }
    if(!_observer && btn){ attachObserver(); }
  }, 1000);

  window.parent.addEventListener('resize', overlayFrame);
  window.parent.addEventListener('scroll', overlayFrame, true);
})();
</script>"""
components.html(_mic_ctrl_js, height=0)

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
    st.markdown(f'<div class="voice-bubble">"{recognized}"</div>', unsafe_allow_html=True)

    combined = try_parse_combined(recognized)
    name_bet = None if combined else try_parse_name_bet(recognized)
    kind = classify_text(recognized)

    # ─── AI意図解釈の要否判定 ─────────────────────────────
    # オッズ条件（〜倍）を含む発話、またはルールベースで解釈できない
    # 発話のみ Claude API を呼ぶ（レイテンシ・コスト対策）
    ai_key = st.session_state.anthropic_key.strip()
    ai_intent = None
    if ai_key and combined is None and name_bet is None:
        if re.search(r"倍|オッズ", recognized):
            _need_ai = True
        elif kind == "race":
            _need_ai = False
        else:
            _rb = parse_bet(recognized)
            _need_ai = bool(_rb["error"] or not _rb["horses"])
        if _need_ai:
            with st.spinner("🤖 AIが発話を解釈中..."):
                ai_intent = ai_parse_intent(recognized, ai_key)

    if combined:
        # ─── レース＋馬券を一括処理 ───────────────────────────
        race_spec = combined["race"]
        bet       = combined["bet"]
        venue, race_num = race_spec["venue"], race_spec["race_num"]
        info = get_race_info(venue, race_num)
        type_label = bet["type_name"] + (
            "(フォーメ)" if bet.get("frm_groups") else "(BOX)" if bet["box"] else ""
        )
        horses_label = (
            " → ".join(",".join(str(h) for h in g) for g in bet["frm_groups"] if g)
            if bet.get("frm_groups")
            else "-".join(str(h) for h in bet["horses"])
        )
        if info["url"]:
            st.markdown(f"""
<div class="wv-card">
  <div class="wv-card-title">レース＋買い目</div>
  <div style="font-size:1.1rem;font-weight:800;color:#C8A84C;margin-bottom:8px">{info['label']}</div>
  <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">
    <div><span style="font-size:.7rem;color:#C8A84C">券種</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{type_label}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">馬番</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{horses_label}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">金額</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{bet['amount']:,}円</span></div>
  </div>
</div>""", unsafe_allow_html=True)
            col1, col2 = st.columns(2)
            with col1:
                if st.button("✅ 買い目に追加", type="primary", use_container_width=True):
                    st.session_state.race_url   = info["url"]
                    st.session_state.race_label = info["label"]
                    st.session_state.race_id    = info["race_id"]
                    st.session_state.is_local   = info["is_local"]
                    st.session_state.bets.append(bet)
                    st.session_state.recognized = ""
                    st.rerun()
            with col2:
                if st.button("✕ キャンセル", use_container_width=True):
                    st.session_state.recognized = ""
                    st.rerun()
        else:
            st.error(f"「{info['label']}」が見つかりません。"
                     f"（中央開催：{'、'.join(venues_today) if venues_today else 'なし'}）")
            if st.button("クリア"):
                st.session_state.recognized = ""
                st.rerun()

    elif name_bet:
        # ─── 馬名指定の買い目（中央のみ）─────────────────────
        with st.spinner(f"「{name_bet['query']}」を本日の中央出走馬から検索中..."):
            matches = find_horse_today(name_bet["query"])
        if not matches:
            st.error(f"「{name_bet['query']}」に一致する出走馬が見つかりませんでした"
                     f"（中央開催：{'、'.join(venues_today) if venues_today else 'なし'}）")
            if st.button("クリア"):
                st.session_state.recognized = ""
                st.rerun()
        else:
            st.markdown(f"""
<div class="wv-card">
  <div class="wv-card-title">馬名から検索</div>
  <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">
    <div><span style="font-size:.7rem;color:#C8A84C">馬名</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{name_bet['query']}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">券種</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{name_bet['type_name']}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">金額</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{name_bet['amount']:,}円</span></div>
  </div>
</div>""", unsafe_allow_html=True)
            if len(matches) > 1:
                st.caption("候補が複数見つかりました。追加する馬を選んでください。")
            for i, mt in enumerate(matches):
                pct = int(mt["score"] * 100)
                label = (f"✅ {mt['name']}　{mt['venue']}{mt['race_num']}R "
                         f"{mt['umaban']}番（一致度{pct}%）に追加")
                if st.button(label, key=f"namebet_{i}",
                             type="primary" if i == 0 else "secondary",
                             use_container_width=True):
                    info = get_race_info(mt["venue"], mt["race_num"])
                    st.session_state.race_url   = info["url"]
                    st.session_state.race_label = info["label"]
                    st.session_state.race_id    = mt["race_id"]
                    st.session_state.is_local   = False
                    st.session_state.bets.append({
                        "raw": recognized, "type_name": name_bet["type_name"],
                        "horses": [mt["umaban"]], "amount": name_bet["amount"],
                        "box": False, "formation": False, "frm_groups": None,
                        "error": None,
                    })
                    st.session_state.recognized = ""
                    st.rerun()
            if st.button("✕ キャンセル", use_container_width=True):
                st.session_state.recognized = ""
                st.rerun()

    elif ai_intent is not None:
        # ─── AI解釈結果の処理 ─────────────────────────────────
        _venue    = ai_intent.get("venue")
        _race_num = ai_intent.get("race_num")
        _bt       = ai_intent.get("bet_type")
        _amt      = max(100, int(ai_intent.get("amount_per_bet") or 100))
        _omin     = ai_intent.get("odds_min")
        _omax     = ai_intent.get("odds_max")
        _horses   = [h for h in (ai_intent.get("horses") or []) if 1 <= h <= 18]
        _hname    = ai_intent.get("horse_name")

        def _ai_clear_button(key: str):
            if st.button("クリア", key=key):
                st.session_state.recognized = ""
                st.rerun()

        if ai_intent.get("error") or ai_intent.get("intent") == "unknown":
            st.error(ai_intent.get("error")
                     or "発話を解釈できませんでした。もう一度話しかけてください。")
            _ai_clear_button("ai_clear_unknown")

        elif _omin is not None or _omax is not None:
            # ── オッズ条件買い（中央のみ）──────────────────
            _cond = (f"{_omin:g}倍以上" if _omax is None else
                     f"{_omax:g}倍以下" if _omin is None else
                     f"{_omin:g}〜{_omax:g}倍")
            if not (_venue and _race_num and _bt):
                st.error("会場・レース番号・券種が特定できませんでした"
                         "（例:「東京8レースで3連単が100倍つく買い目を全部」）")
                _ai_clear_button("ai_clear_nospec")
            elif _venue in LOCAL_VENUE_NAMES:
                st.error("オッズ条件買いは中央競馬のみ対応です")
                _ai_clear_button("ai_clear_nar")
            else:
                info = get_race_info(_venue, _race_num)
                if not info["race_id"]:
                    st.error(f"「{_venue} {_race_num}R」が見つかりません。"
                             f"（中央開催：{'、'.join(venues_today) if venues_today else 'なし'}）")
                    _ai_clear_button("ai_clear_norace")
                else:
                    with st.spinner("オッズを取得中..."):
                        _rows = fetch_jra_odds_table(info["race_id"], _bt)
                    _hit = [r for r in _rows
                            if (_omin is None or r["odds"] >= _omin)
                            and (_omax is None or r["odds"] <= _omax)]
                    _hit.sort(key=lambda r: r["odds"])
                    if not _rows:
                        st.error(f"{info['label']} の{_bt}オッズを取得できませんでした"
                                 "（発売前の可能性があります）")
                        _ai_clear_button("ai_clear_noodds")
                    elif not _hit:
                        st.warning(f"{info['label']} {_bt}で {_cond} の買い目は"
                                   f"ありません（全{len(_rows)}点）")
                        _ai_clear_button("ai_clear_nohit")
                    else:
                        st.markdown(f"""
<div class="wv-card">
  <div class="wv-card-title">🤖 オッズ条件買い</div>
  <div style="font-size:1.1rem;font-weight:800;color:#C8A84C;margin-bottom:8px">{info['label']}</div>
  <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">
    <div><span style="font-size:.7rem;color:#C8A84C">券種</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{_bt}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">条件</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{_cond}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">該当</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{len(_hit)}点</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">1点あたり</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{_amt:,}円</span></div>
  </div>
</div>""", unsafe_allow_html=True)
                        _CAP = 500
                        if len(_hit) > _CAP:
                            st.warning(f"該当{len(_hit)}点のうち、オッズが低い順に"
                                       f"{_CAP}点のみ表示します")
                            _hit = _hit[:_CAP]
                        import pandas as _pd
                        _df = _pd.DataFrame({
                            "買う": [True] * len(_hit),
                            "買い目": ["-".join(map(str, r["horses"])) for r in _hit],
                            "オッズ": [r["odds"] for r in _hit],
                            "人気": [r["pop"] for r in _hit],
                        })
                        _ed = st.data_editor(
                            _df, hide_index=True, height=320,
                            disabled=["買い目", "オッズ", "人気"],
                            key=f"ai_odds_{info['race_id']}_{_bt}",
                        )
                        _sel_idx = [i for i in _ed.index if _ed.loc[i, "買う"]]
                        _n = len(_sel_idx)
                        st.caption("⚠️ 転送は1点ずつブラウザ操作するため、"
                                   "点数が多いと時間がかかります")
                        col1, col2 = st.columns(2)
                        with col1:
                            if st.button(f"✅ {_n}点を一括追加（計{_n * _amt:,}円）",
                                         type="primary", use_container_width=True,
                                         disabled=_n == 0, key="ai_odds_add"):
                                st.session_state.race_url   = info["url"]
                                st.session_state.race_label = info["label"]
                                st.session_state.race_id    = info["race_id"]
                                st.session_state.is_local   = False
                                for i in _sel_idx:
                                    r = _hit[i]
                                    st.session_state.bets.append({
                                        "raw": recognized, "type_name": _bt,
                                        "horses": r["horses"], "amount": _amt,
                                        "box": False, "formation": False,
                                        "frm_groups": None, "error": None,
                                    })
                                st.session_state.recognized = ""
                                st.rerun()
                        with col2:
                            if st.button("✕ キャンセル", use_container_width=True,
                                         key="ai_odds_cancel"):
                                st.session_state.recognized = ""
                                st.rerun()

        elif _hname and _bt in ("単勝", "複勝"):
            # ── 馬名買い（AI経由）→ 既存の馬名検索フローへ ──
            st.session_state.recognized = f"{_hname} {_bt} {_amt}円"
            st.rerun()

        elif _bt and _horses:
            # ── 通常買い目（AI経由）─────────────────────────
            info = get_race_info(_venue, _race_num) if (_venue and _race_num) else None
            if info and not info["url"]:
                st.error(f"「{info['label']}」が見つかりません。"
                         f"（中央開催：{'、'.join(venues_today) if venues_today else 'なし'}）")
                _ai_clear_button("ai_clear_betrace")
            else:
                _box = bool(ai_intent.get("box"))
                type_label = _bt + ("(BOX)" if _box else "")
                horses_label = "-".join(str(h) for h in _horses)
                _place = info["label"] if info else st.session_state.race_label or "（レース未選択）"
                st.markdown(f"""
<div class="wv-card">
  <div class="wv-card-title">🤖 買い目（AI解釈）</div>
  <div style="font-size:1.1rem;font-weight:800;color:#C8A84C;margin-bottom:8px">{_place}</div>
  <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">
    <div><span style="font-size:.7rem;color:#C8A84C">券種</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{type_label}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">馬番</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{horses_label}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">金額</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{_amt:,}円</span></div>
  </div>
</div>""", unsafe_allow_html=True)
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("✅ 買い目に追加", type="primary",
                                 use_container_width=True, key="ai_bet_add"):
                        if info:
                            st.session_state.race_url   = info["url"]
                            st.session_state.race_label = info["label"]
                            st.session_state.race_id    = info["race_id"]
                            st.session_state.is_local   = info["is_local"]
                        st.session_state.bets.append({
                            "raw": recognized, "type_name": _bt,
                            "horses": _horses, "amount": _amt,
                            "box": _box, "formation": False,
                            "frm_groups": None, "error": None,
                        })
                        st.session_state.recognized = ""
                        st.rerun()
                with col2:
                    if st.button("✕ キャンセル", use_container_width=True,
                                 key="ai_bet_cancel"):
                        st.session_state.recognized = ""
                        st.rerun()

        elif _venue and _race_num:
            # ── レース選択のみ（AI経由）──────────────────────
            info = get_race_info(_venue, _race_num)
            if not info["url"]:
                st.error(f"「{info['label']}」が見つかりません。"
                         f"（中央開催：{'、'.join(venues_today) if venues_today else 'なし'}）")
                _ai_clear_button("ai_clear_raceonly")
            else:
                st.success(f"レース確認：**{info['label']}**")
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("✅ このレースで確定", type="primary",
                                 use_container_width=True, key="ai_race_ok"):
                        st.session_state.race_url   = info["url"]
                        st.session_state.race_label = info["label"]
                        st.session_state.race_id    = info["race_id"]
                        st.session_state.is_local   = info["is_local"]
                        st.session_state.recognized = ""
                        st.rerun()
                with col2:
                    if st.button("✕ キャンセル", use_container_width=True,
                                 key="ai_race_cancel"):
                        st.session_state.recognized = ""
                        st.rerun()
        else:
            st.error("発話から買い目・レースを特定できませんでした")
            _ai_clear_button("ai_clear_fallback")

    elif kind == "race":
        spec = parse_race_spec(recognized)
        if spec["error"]:
            st.error(spec["error"])
            if st.button("クリア"):
                st.session_state.recognized = ""
                st.rerun()
        else:
            venue, race_num = spec["venue"], spec["race_num"]
            info = get_race_info(venue, race_num)
            if info["url"]:
                st.success(f"レース確認：**{info['label']}**")
                if info["race_id"]:
                    st.caption(f"race_id: {info['race_id']}")

                col1, col2, col3 = st.columns(3)
                with col1:
                    if st.button("✅ このレースで確定", type="primary", use_container_width=True):
                        st.session_state.race_url   = info["url"]
                        st.session_state.race_label = info["label"]
                        st.session_state.race_id    = info["race_id"]
                        st.session_state.is_local   = info["is_local"]
                        st.session_state.yoso_result    = None
                        st.session_state.bet_suggestion = None
                        st.session_state.yoso_overrides = None
                        st.session_state.yoso_odds  = {}
                        st.session_state.recognized = ""
                        st.rerun()
                with col2:
                    if st.button("📊 予想を購入・分析", use_container_width=True):
                        rid = info["race_id"]
                        if rid and len(rid) == 12 and rid.isdigit():
                            with st.spinner("予想を購入・取得中...（1〜2分かかります）"):
                                _res = buy_and_fetch_yoso(rid, is_nar=info["is_local"])
                            if _res and _res.get("scores"):
                                _odds = fetch_umaren_odds(rid, is_nar=info["is_local"])
                                _auto_ov = _make_auto_overrides(_res["scores"])
                                st.session_state.yoso_result     = _res["scores"]
                                st.session_state.yoso_predictors = _res.get("predictors", [])
                                st.session_state.yoso_race_id    = rid
                                st.session_state.yoso_odds       = _odds
                                st.session_state.yoso_overrides  = _auto_ov
                                st.session_state.bet_suggestion  = suggest_bets(_res["scores"], _auto_ov, _odds)
                                st.session_state.race_url    = info["url"]
                                st.session_state.race_label  = info["label"]
                                st.session_state.race_id     = rid
                                st.session_state.is_local    = info["is_local"]
                                st.session_state.recognized  = ""
                                st.rerun()
                            else:
                                st.error("予想取得に失敗しました")
                        else:
                            st.error("race_idを取得できませんでした（地方競馬は自動取得に時間がかかる場合があります）")
                with col3:
                    if st.button("✕ キャンセル", use_container_width=True):
                        st.session_state.recognized = ""
                        st.rerun()
                # スマホ幅で窮屈にならないよう、Myロジックは全幅ボタンで下段に配置
                if not info["is_local"]:
                    if st.button("🧮 Myロジックで分析", use_container_width=True,
                                 key="btn_mylogic_from_race"):
                        st.session_state.race_url   = info["url"]
                        st.session_state.race_label = info["label"]
                        st.session_state.race_id    = info["race_id"]
                        st.session_state.is_local   = info["is_local"]
                        st.session_state.recognized = ""
                        st.session_state.mylogic_autorun = True
                        st.rerun()
            else:
                st.error(f"「{info['label']}」が見つかりません。"
                         f"（中央開催：{'、'.join(venues_today) if venues_today else 'なし'}）")
                if st.button("クリア"):
                    st.session_state.recognized = ""
                    st.rerun()
    else:
        bet = parse_bet(recognized)
        if bet["error"]:
            st.error(bet["error"])
        else:
            type_label = bet["type_name"] + (
                "(フォーメ)" if bet.get("frm_groups") else
                "(BOX)" if bet["box"] else ""
            )
            horses_label = (
                " → ".join(",".join(str(h) for h in g) for g in bet["frm_groups"] if g)
                if bet.get("frm_groups")
                else "-".join(str(h) for h in bet["horses"])
            )
            st.markdown(f"""
<div class="wv-card">
  <div class="wv-card-title">買い目</div>
  <div style="display:flex;gap:18px;align-items:center;flex-wrap:wrap">
    <div><span style="font-size:.7rem;color:#C8A84C">券種</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{type_label}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">馬番</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{horses_label}</span></div>
    <div><span style="font-size:.7rem;color:#C8A84C">金額</span><br>
         <span style="font-size:1.2rem;font-weight:800;color:#000">{bet['amount']:,}円</span></div>
  </div>
</div>""", unsafe_allow_html=True)
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

# ─── 予想を購入・分析する（買い目なし＝レースだけ確定した場合のみ）────
if st.session_state.race_id and not st.session_state.bets:
    st.divider()
    st.markdown("### 予想を購入・分析する")

    if st.button("🔍 予想を購入してオッズで分析", use_container_width=True):
        with st.spinner("予想を購入・オッズ取得中（1〜2分かかります）..."):
            _scores = buy_and_fetch_yoso(
                st.session_state.race_id, st.session_state.is_local
            )
            _odds = fetch_umaren_odds(st.session_state.race_id, is_nar=st.session_state.is_local)
        if _scores and _scores.get("scores"):
            _auto_ov = _make_auto_overrides(_scores["scores"])
            st.session_state.yoso_result     = _scores["scores"]
            st.session_state.yoso_predictors = _scores.get("predictors", [])
            st.session_state.yoso_race_id    = st.session_state.race_id
            st.session_state.yoso_odds       = _odds
            st.session_state.yoso_overrides  = _auto_ov
            st.session_state.bet_suggestion  = suggest_bets(_scores["scores"], _auto_ov, _odds)
            st.rerun()
        else:
            st.error("予想取得に失敗しました（ログインが必要な場合はサイドバーから連携してください）")

    if st.session_state.yoso_result:
        _scores = st.session_state.yoso_result
        _odds   = st.session_state.yoso_odds or {}

        st.markdown("#### 集計スコア（◎=3 ○=2 ▲☆△=1）")
        _sorted = sorted(_scores.items(), key=lambda x: x[1], reverse=True)
        st.markdown("　".join(f"**{h}番** {s}pt" for h, s in _sorted[:7]))

        _preds = st.session_state.get("yoso_predictors", [])
        if _preds:
            st.markdown("#### 予想家別の印")
            _MARK_ORDER = {"◎": 0, "○": 1, "▲": 2, "☆": 3, "△": 4}
            rows = []
            for pred in _preds:
                marks_str = "　".join(
                    f"{sym}{hn}番"
                    for hn, sym in sorted(
                        pred["marks"].items(),
                        key=lambda kv: _MARK_ORDER.get(kv[1], 9)
                    )
                )
                rows.append(f"**{pred['name']}** ： {marks_str}" if marks_str else f"**{pred['name']}** ： 印なし")
            st.markdown("\n\n".join(rows))

        st.markdown("#### 印の割り当て（変更可）")
        _ov = dict(st.session_state.yoso_overrides or {})
        _opts = [0] + list(range(1, 19))

        def _sel(mark: str, col) -> int:
            cur = _ov.get(mark, 0)
            idx = _opts.index(cur) if cur in _opts else 0
            return col.selectbox(mark, _opts, index=idx, key=f"ov_{mark}",
                                 format_func=lambda v: f"{v}番" if v else "—")

        _c1, _c2, _c3 = st.columns(3)
        _ov["◎"] = _sel("◎", _c1)
        _ov["○"] = _sel("○", _c2)
        _ov["▲"] = _sel("▲", _c3)
        _c4, _c5, _c6, _c7 = st.columns(4)
        _ov["△"]  = _sel("△",  _c4)
        _ov["☆1"] = _sel("☆1", _c5)
        _ov["☆2"] = _sel("☆2", _c6)
        _ov["☆3"] = _sel("☆3", _c7)

        _fov = {k: v for k, v in _ov.items() if v and v > 0}
        if _fov != (st.session_state.yoso_overrides or {}):
            st.session_state.yoso_overrides = _fov
            st.session_state.bet_suggestion = suggest_bets(_scores, _fov, _odds)
            st.rerun()

        if st.session_state.bet_suggestion:
            import pandas as pd
            all_c = st.session_state.bet_suggestion
            _df = pd.DataFrame([{
                "選択": c.get("selected", True),
                "組み合わせ": f"{c['mark1']} {c['h1']}番 - {c['mark2']} {c['h2']}番",
                "オッズ": c["odds"],
            } for c in all_c])

            _edited = st.data_editor(
                _df, use_container_width=True, hide_index=True,
                column_config={"選択": st.column_config.CheckboxColumn("選択", default=True),
                               "オッズ": st.column_config.NumberColumn("オッズ", format="%.1f")},
            )
            _sel_idxs = [i for i, row in _edited.iterrows() if row["選択"]]
            _n_sel = len(_sel_idxs)

            if _n_sel > 0:
                _budget = 2500 if st.session_state.is_local else 3500
                _unit = max(100, (_budget // _n_sel // 100) * 100)
                if st.button(
                    f"📝 チェックした {_n_sel}点をまとめて買い目に追加（各{_unit:,}円）",
                    use_container_width=True, key="add_suggest_btn",
                ):
                    for idx in _sel_idxs:
                        c = all_c[idx]
                        st.session_state.bets.append({
                            "type_name": "馬連", "horses": [c["h1"], c["h2"]],
                            "amount": _unit, "box": False, "formation": False, "frm_groups": None,
                        })
                    st.rerun()

# ─── netkeiba / 楽天競馬 反映 ────────────────────────────────
if st.session_state.bets:
    st.divider()
    is_local = st.session_state.is_local
    st.markdown(f"### {'楽天競馬' if is_local else 'netkeiba'} に反映")

    if not st.session_state.race_url:
        st.warning("先にレースを音声で選択してください（「東京7レース」など）")
    else:
        _shutuba_info = None
        st.info(f"**{len(st.session_state.bets)}件** → {st.session_state.race_label}")

        # shutuba_info を構築（yoso_overrides 優先、なければ yoso_result スコア順）
        _hm = {}
        if st.session_state.get("yoso_result") and st.session_state.get("yoso_race_id") == st.session_state.race_id:
            _marks = _assign_marks(
                st.session_state.yoso_result,
                st.session_state.get("yoso_overrides"),
            )
            _hm = {h: m for m, h in _marks.items()}
        if _hm:
            _shutuba_info = {
                "race_id": st.session_state.race_id,
                "is_nar": st.session_state.is_local,
                "horse_marks": _hm,
            }

        # ── 提案買い目サマリー ──────────────────────────────────────
        _sugg = st.session_state.get("bet_suggestion")
        if _sugg and st.session_state.get("yoso_race_id") == st.session_state.race_id:
            _sent_keys = {
                tuple(sorted([b["horses"][0], b["horses"][1]]))
                for b in st.session_state.bets
                if len(b.get("horses", [])) >= 2
            }
            st.markdown("#### 提案買い目")
            import pandas as pd
            _sum_rows = []
            for c in _sugg:
                _key  = tuple(sorted([c["h1"], c["h2"]]))
                _sent = _key in _sent_keys
                _sum_rows.append({
                    "状態": "✅ 送信" if _sent else ("📋 提案" if c.get("selected") else "－"),
                    "組み合わせ": f"{c['mark1']} {c['h1']}番 - {c['mark2']} {c['h2']}番",
                    "オッズ": c["odds"],
                })
            st.dataframe(
                pd.DataFrame(_sum_rows),
                use_container_width=True,
                hide_index=True,
                column_config={"オッズ": st.column_config.NumberColumn("オッズ", format="%.1f")},
            )

        st.markdown('<div class="wv-transfer-btn">', unsafe_allow_html=True)
        if is_local:
            if st.button("🏇 楽天競馬に自動入力", type="primary", use_container_width=True):
                with st.spinner("ブラウザを起動して買い目を入力中..."):
                    _venue = st.session_state.race_label.split()[0] if st.session_state.race_label else ""
                    log, _st_time = input_bets_to_rakuten(
                        st.session_state.race_url,
                        st.session_state.bets,
                        shutuba_info=_shutuba_info,
                        venue=_venue,
                    )
                st.session_state["last_transfer_log"] = log
                st.session_state["race_start_time"]   = _st_time
                st.session_state["notify_pending"]     = bool(_st_time)
                st.rerun()

        if st.session_state.get("last_transfer_log"):
            st.success("✅ 買い目入力完了 — ブラウザで投票内容を確認してください")
            with st.expander("操作ログ"):
                st.markdown(f'<div class="log-card">{st.session_state["last_transfer_log"]}</div>',
                            unsafe_allow_html=True)

        # ── 発走通知セットアップ（転送直後の1回のみ）──────────────
        if st.session_state.get("notify_pending") and st.session_state.get("race_start_time"):
            _n_venue   = st.session_state.race_label.split()[0] if st.session_state.race_label else ""
            _n_romaji  = VENUE_ROMAJI.get(_n_venue, "")
            _n_live    = f"https://keiba-lv-st.jp/top.html?track_id={_n_romaji}" if _n_romaji else ""
            _raw_time  = st.session_state["race_start_time"]
            # 発走時刻表示は締切時刻なので +2分を通知時刻とする
            if _raw_time and ":" in _raw_time:
                _h, _m = map(int, _raw_time.split(":"))
                _tot   = _h * 60 + _m + 2
                _n_time = f"{_tot // 60:02d}:{_tot % 60:02d}"
            else:
                _n_time = _raw_time
            _n_label   = st.session_state.race_label
            components.html(f"""<script>
(async function() {{
    var conf = window.parent.confirm(
        '\\u{ord("🔔"):X} {_n_label}\\n発走 {_n_time} に通知をセットしますか？'
    );
    if (!conf) return;
    var perm = window.parent.Notification ? window.parent.Notification.permission : 'denied';
    if (perm !== 'granted') {{
        perm = await window.parent.Notification.requestPermission();
    }}
    if (perm !== 'granted') {{
        window.parent.alert('通知の許可が必要です。ブラウザの設定から許可してください。');
        return;
    }}
    var parts = '{_n_time}'.split(':');
    var raceDate = new window.parent.Date();
    raceDate.setHours(parseInt(parts[0]), parseInt(parts[1]), 0, 0);
    var delay = raceDate.getTime() - window.parent.Date.now();
    if (delay <= 0) {{
        window.parent.alert('発走時刻が過去のため通知できません');
        return;
    }}
    window.parent.setTimeout(function() {{
        var n = new window.parent.Notification('\\uD83C\\uDFC7 発走！ {_n_label}', {{
            body: 'タップしてライブ映像を開く',
            requireInteraction: true
        }});
        n.onclick = function() {{
            window.parent.open('{_n_live}', '_blank');
            n.close();
        }};
    }}, delay);
    window.parent.alert('\\u2705 発走 {_n_time} に通知をセットしました');
}})();
</script>""", height=0)
            st.session_state["notify_pending"] = False
        else:
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
                        shutuba_info=_shutuba_info,
                    )
                st.success("✅ 買い目入力完了 — 開いたブラウザで「IPAT投票へすすむ」を押してください")
                with st.expander("操作ログ"):
                    st.markdown(f'<div class="log-card">{log}</div>', unsafe_allow_html=True)
        st.markdown('</div>', unsafe_allow_html=True)

    with st.expander("買い目データ（JSON）"):
        st.json(st.session_state.bets)

# ─── Myロジックで分析（馬メモベースのランキング）───────────
render_mylogic_section()

# ── 自動スクロール：最新ボタンが常に見えるようにページ末尾へスクロール ──
components.html("""<script>
(function(){
  var selectors = [
    '[data-testid="stAppViewBlockContainer"]',
    '.main > div',
    '.block-container',
  ];
  var target = null;
  for (var i = 0; i < selectors.length; i++) {
    target = window.parent.document.querySelector(selectors[i]);
    if (target && target.scrollHeight > target.clientHeight) break;
  }
  if (target) {
    target.scrollTo({ top: target.scrollHeight, behavior: 'smooth' });
  } else {
    window.parent.scrollTo({ top: window.parent.document.body.scrollHeight, behavior: 'smooth' });
  }
})();
</script>""", height=0)
