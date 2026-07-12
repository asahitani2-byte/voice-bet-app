"""タイム解析・秒変換・netkeiba HTML解析（純粋関数のみ・通信なし）。"""
from __future__ import annotations

import datetime
import math
import re
import unicodedata

from bs4 import BeautifulSoup

from .models import HorseEntry, HorseRaceNote, ParsedTime, RaceInfo, ResultRow

# ─── タイム文字列の抽出 ──────────────────────────────────────
# 形式: 先頭タイム(M:SS.f) - ラスト400m 、 任意の独自数値
#   例: "1:35.5-22.4、-24" / "1:09.0-22.4" / "13.2、1:23.5-22.9、-22"
# 先頭タイムはコロン形式のみ有効（コロン無しの数値は騎手コメント等と
# 区別できないため対象外とする — 仕様上の最低対応形式はすべてコロン形式）。
_TIME_RE = re.compile(
    r"(\d{1,2}):(\d{2})\.(\d)"                # 先頭タイム M:SS.f
    r"\s*-\s*"                                 # 区切り
    r"(\d{1,2}(?:\.\d)?)"                      # ラスト400m
    r"(?:\s*[、,]\s*([+-]?\d+(?:\.\d+)?))?"    # 任意の独自数値（符号付き可）
)

_DASHES = "−ー–—―ｰ"  # 全角系ダッシュ → 半角ハイフンへ正規化


def _normalize(text: str) -> str:
    t = unicodedata.normalize("NFKC", text or "")
    for d in _DASHES:
        t = t.replace(d, "-")
    return t


def parse_time_text(comment: str) -> ParsedTime | None:
    """コメントから指定フォーマットに一致する最初のタイムを抽出する。

    一致しなければ None。複数一致しても最初の1件のみ採用。
    """
    if not comment:
        return None
    norm = _normalize(comment)
    m = _TIME_RE.search(norm)
    if not m:
        return None
    minutes, sec, frac, last400, custom = m.groups()
    head_seconds = int(minutes) * 60 + int(sec) + int(frac) / 10.0
    head_text = f"{int(minutes)}:{sec}.{frac}"
    try:
        last400_seconds = float(last400)
    except ValueError:
        return None
    return ParsedTime(
        text=m.group(0).strip(),
        head_text=head_text,
        head_seconds=round(head_seconds, 1),
        last400_seconds=last400_seconds,
        custom_value=custom,
    )


# ─── 秒変換 ──────────────────────────────────────────────────
def time_to_seconds(text: str) -> float:
    """"1:35.5" → 95.5。コロン無し（"59.8"）は秒としてそのまま返す。"""
    t = _normalize(text).strip()
    if ":" in t:
        m, _, s = t.partition(":")
        return round(int(m) * 60 + float(s), 1)
    return round(float(t), 1)


def round1(x: float) -> float:
    """小数第1位へ四捨五入（浮動小数点誤差対策の half-up）。"""
    return math.floor(x * 10 + 0.5) / 10


def seconds_to_time(seconds: float) -> str:
    """95.5 → "1:35.5"。小数第1位へ四捨五入。60秒未満は "0:59.8" 形式。"""
    total = round1(seconds)
    minutes = int(total // 60)
    rest = round1(total - minutes * 60)
    if rest >= 60:  # 丸めで繰り上がった場合
        minutes += 1
        rest = round1(rest - 60)
    return f"{minutes}:{rest:04.1f}"


# ─── 出馬表の解析 ────────────────────────────────────────────
_DIST_RE = re.compile(r"(芝|ダート|ダ|障害|障)?\s*(?:右|左|直線|直)?\s*(\d{3,4})\s*m")
_TRACK_MAP = {"芝": "芝", "ダ": "ダート", "ダート": "ダート", "障": "障害", "障害": "障害"}


def parse_race_distance(text: str) -> tuple[int | None, str]:
    """レース条件テキストから (距離m, 馬場区分) を取得する。"""
    norm = _normalize(text)
    m = _DIST_RE.search(norm)
    if m:
        track = _TRACK_MAP.get(m.group(1) or "", "")
        return int(m.group(2)), track
    # フォールバック: 数字+m のみ
    m2 = re.search(r"(\d{3,4})\s*m", norm)
    if m2:
        return int(m2.group(1)), ""
    return None, ""


def _extract_umaban(tr) -> int | None:
    """出馬表SP版の行から馬番を取得する。

    注意: tr の id="tr_{N}" のNは馬番ではなく表示順の連番
    （50音順などで並び替わることがある）。正しい馬番は
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


def parse_shutuba_html(html: str, race_id: str) -> RaceInfo:
    """SP版出馬表HTMLからレース情報と出走馬を取得する。

    - 距離: .RaceList_NameBox_inner（無ければ .RaceList_Item01/02、最後はページ全体）
    - 出走馬: div.Shutuba_HorseList 配下の最初の表の tr.HorseList
      （tr id="tr_{馬番}"、td.Waku{N}=枠、dt.Horse a=馬名+horse_id）
    - 取消/除外: 行内クラスまたはテキストの「取消/除外/Cancel」で判定
    """
    soup = BeautifulSoup(html, "html.parser")
    info = RaceInfo(race_id=race_id)

    # レース名
    name_el = soup.select_one(".RaceList_Item02 .RaceName, .RaceName")
    if name_el:
        info.name = name_el.get_text(strip=True)
    # 距離: 段階的フォールバック
    for sel in (".RaceList_NameBox_inner", ".RaceList_Item02", ".RaceList_Item01"):
        el = soup.select_one(sel)
        if el:
            text = " ".join(el.get_text(" ", strip=True).split())
            dist, track = parse_race_distance(text)
            if dist:
                info.distance, info.track_type = dist, track
                info.raw_conditions = text[:200]
                if not info.name:
                    # Item02 の先頭付近にレース名が含まれる（例 "GIII 七夕賞 15:45 芝 2000m ..."）
                    nm = re.sub(r"\s*\d{1,2}:\d{2}.*$", "", text)
                    nm = re.sub(r"^(GI{1,3}|Listed|OP)\s*", "", nm).strip()
                    info.name = nm[:40]
                break
    if not info.distance:
        dist, track = parse_race_distance(soup.get_text(" ", strip=True)[:3000])
        info.distance, info.track_type = dist, track

    # 出走馬: Shutuba_HorseList 配下を優先（ページ内に別表のtr.HorseListが重複存在するため）
    container = soup.select_one("div.Shutuba_HorseList") or soup
    seen: set[int] = set()
    for tr in container.select("tr.HorseList"):
        a = tr.select_one("dt.Horse a")
        if not a:
            continue
        umaban = _extract_umaban(tr)
        if umaban is None or umaban in seen:
            continue
        href = a.get("href") or ""
        hm = re.search(r"horse_id=(\d+)", href)
        if not hm:
            continue
        name = a.get_text(strip=True)
        if not name:
            continue
        waku = None
        waku_td = tr.select_one("td[class*='Waku']")
        if waku_td:
            wm = re.search(r"Waku(\d+)", " ".join(waku_td.get("class") or []))
            if wm:
                waku = int(wm.group(1))
            else:
                wt = waku_td.get_text(strip=True)
                if wt.isdigit():
                    waku = int(wt)
        row_classes = " ".join(
            c for el in tr.find_all(True) for c in (el.get("class") or []))
        row_text = tr.get_text(" ", strip=True)
        cancelled = False
        reason = ""
        if "Cancel" in (tr.get("class") or []) or "Cancel_Txt" in row_classes:
            cancelled, reason = True, "取消/除外"
        if "取消" in row_text:
            cancelled, reason = True, "出走取消"
        elif "除外" in row_text:
            cancelled, reason = True, "競走除外"
        seen.add(umaban)
        info.entries.append(HorseEntry(
            umaban=umaban, waku=waku, name=name, horse_id=hm.group(1),
            is_cancelled=cancelled, cancel_reason=reason, modal_url=href,
        ))
    info.entries.sort(key=lambda e: e.umaban)
    return info


# ─── 馬メモ断片の解析 ────────────────────────────────────────
_DD_RE = re.compile(
    r"(\d{4})/(\d{1,2})/(\d{1,2})\s*(\S*?)(\d{1,2})R\s*(芝|ダート|ダ|障害|障)?\s*(\d{3,4})m")


def parse_note_fragment(html: str, base_url: str = "") -> list[HorseRaceNote]:
    """馬メモAPIのHTML断片（li#RaceNote-...の連なり）を解析する。"""
    soup = BeautifulSoup(html, "html.parser")
    notes: list[HorseRaceNote] = []
    for li in soup.select("li[id^='RaceNote-']"):
        li_id = li.get("id") or ""
        m = re.match(r"RaceNote-(\d{12})", li_id)
        if not m:
            continue
        note = HorseRaceNote(source_race_id=m.group(1), source_url=base_url)
        dd = li.select_one("dd")
        if dd:
            note.date_text = dd.get_text(strip=True)
            dm = _DD_RE.search(_normalize(note.date_text))
            if dm:
                y, mo, d, venue, rno, track, dist = dm.groups()
                try:
                    note.date = datetime.date(int(y), int(mo), int(d))
                except ValueError:
                    note.date = None
                note.venue = venue
                note.race_no = int(rno)
                note.track_type = _TRACK_MAP.get(track or "", "")
                note.distance = int(dist)
        dt_a = li.select_one("dt a")
        if dt_a:
            note.race_name = dt_a.get_text(strip=True)
            hm = re.search(r"race_id=(\d{12})", dt_a.get("href") or "")
            if hm:  # li idよりhrefを信頼
                note.source_race_id = hm.group(1)
        rank_el = li.select_one("dt span[class*='ResultRank'], dt span[class*='Rank']")
        if rank_el:
            note.rank_text = rank_el.get_text(strip=True)
            rm = re.match(r"(\d+)", _normalize(note.rank_text))
            note.rank = int(rm.group(1)) if rm else None
        comment_el = li.select_one(".RaceComment_Text, .RaceComment p")
        if comment_el:
            note.comment = comment_el.get_text("\n", strip=True)
            note.parsed_time = parse_time_text(note.comment)
        notes.append(note)
    return notes


def parse_note_paging(html: str) -> tuple[int | None, bool | None]:
    """モーダルHTMLの ul data-page / data-last を解析（(page, is_last)）。"""
    soup = BeautifulSoup(html, "html.parser")
    ul = soup.select_one("ul[id^='UserRaceHorseNote']")
    if not ul:
        return None, None
    page = ul.get("data-page")
    last = ul.get("data-last")
    return (int(page) if page is not None and str(page).isdigit() else None,
            bool(int(last)) if last is not None and str(last).isdigit() else None)


# ─── 出走馬DBページの戦績表解析（地方Myロジック用）──────────
_DB_ROW_HEAD_RE = re.compile(
    r"(\d{2})/(\d{2})/(\d{2})\s*(\S+?)\s*(\d{1,2})\s*R\s*(.*)")
_DB_DIST_RE = re.compile(r"(芝|ダ|障)\s*(\d{3,4})")


def parse_horse_db_results(html: str) -> list[HorseRaceNote]:
    """db.sp.netkeiba.com の馬ページ戦績表を解析しメモ相当データを合成する。

    採用タイム = 走破タイム − 上り3F を先頭タイムとし、
    「{先頭}-{上り}、{馬場指数}」形式（馬場指数なしなら「{先頭}-{上り}」）。
    着差列（勝ち馬との秒差）と勝ち馬(2着馬)列をTargetHorse判定用に保持。
    """
    soup = BeautifulSoup(html, "html.parser")
    # 「タイム」「上り」列を持つ戦績テーブルを特定
    table = None
    header_map: dict[str, int] = {}
    for t in soup.select("table"):
        head_tr = t.select_one("tr")
        if not head_tr:
            continue
        cells = [re.sub(r"\s", "", c.get_text(" ", strip=True))
                 for c in head_tr.find_all(["th", "td"])]
        if any("タイム" in c for c in cells) and any(c.startswith("上り")
                                                   for c in cells):
            table = t
            for i, c in enumerate(cells):
                header_map[c] = i
            break
    if table is None:
        return []

    def col(row_tds, *names) -> str:
        for name in names:
            for key, idx in header_map.items():
                if key.startswith(name) and idx < len(row_tds):
                    return row_tds[idx].get_text(" ", strip=True)
        return ""

    notes: list[HorseRaceNote] = []
    for tr in table.select("tr")[1:]:
        tds = tr.find_all("td")
        if len(tds) < 10:
            continue
        note = HorseRaceNote(source_race_id="")
        # 1列目: 日付 開催 R レース名
        head_text = _normalize(tds[0].get_text(" ", strip=True))
        m = _DB_ROW_HEAD_RE.match(head_text)
        if m:
            yy, mo, dd, venue, rno, rname = m.groups()
            try:
                note.date = datetime.date(2000 + int(yy), int(mo), int(dd))
            except ValueError:
                note.date = None
            note.venue = venue
            note.race_no = int(rno)
            note.race_name = rname.strip() or head_text
            note.date_text = (f"20{yy}/{mo}/{dd} {venue}{int(rno)}R")
        else:
            note.race_name = head_text[:30]
        link = tr.select_one("a[href*='/race/']")
        if link:
            rm = re.search(r"/race/(\d{12})", link.get("href") or "")
            if rm:
                note.source_race_id = rm.group(1)
        if not note.source_race_id:
            continue
        # 距離・馬場
        dm = _DB_DIST_RE.search(_normalize(col(tds, "距離")))
        if dm:
            note.track_type = _TRACK_MAP.get(dm.group(1), "")
            note.distance = int(dm.group(2))
            note.date_text += f" {dm.group(1)}{dm.group(2)}m"
        # 着順
        note.rank_text = col(tds, "着順")
        rm2 = re.match(r"(\d+)", _normalize(note.rank_text))
        note.rank = int(rm2.group(1)) if rm2 else None
        # タイム・上り → 採用タイムを合成
        time_text = _normalize(col(tds, "タイム"))
        agari_text = _normalize(col(tds, "上り"))
        shisu_text = _normalize(col(tds, "馬場指数"))
        if re.match(r"^\d{1,2}:\d{2}\.\d$", time_text) and \
                re.match(r"^\d{2}\.\d$", agari_text):
            total = time_to_seconds(time_text)
            agari = float(agari_text)
            head = round1(total - agari)
            custom = shisu_text if re.match(r"^[+-]?\d+(\.\d+)?$",
                                            shisu_text) else None
            text = f"{seconds_to_time(head)}-{agari_text}" + (
                f"、{custom}" if custom else "")
            note.parsed_time = ParsedTime(
                text=text, head_text=seconds_to_time(head),
                head_seconds=head, last400_seconds=agari,
                custom_value=custom)
        # TargetHorse情報（着差=勝ち馬との秒差、勝ち馬(2着馬)列）
        note.target_name = col(tds, "勝ち馬")
        gap_text = _normalize(col(tds, "着差"))
        gm = re.match(r"^([+-]?\d+(?:\.\d+)?)$", gap_text)
        if gm:
            gap = float(gm.group(1))
            # 自身が勝ち馬の場合、着差は2着馬との差 → 常に条件クリア側の0以下へ
            note.gap_to_target = min(gap, 0.0) if note.rank == 1 else gap
        elif note.rank == 1:
            note.gap_to_target = 0.0  # 勝ち馬は無条件で有効
        # 表示用コメント（メモ全文相当）
        parts = [f"{note.date_text}", f"{note.race_name}",
                 f"{note.rank_text}着" if note.rank_text else "",
                 f"タイム{time_text}" if time_text else "",
                 f"上り{agari_text}" if agari_text else "",
                 f"馬場指数{shisu_text}" if shisu_text else "馬場指数なし",
                 f"勝ち馬(2着馬):{note.target_name}" if note.target_name else ""]
        note.comment = "　".join(p for p in parts if p)
        notes.append(note)
    return notes


# ─── 公式レース結果（db.netkeiba.com）の解析 ────────────────
def parse_db_result_html(html: str) -> list[ResultRow]:
    """db.netkeiba.com/race/{race_id}/ の結果テーブルを解析する。

    列: 着順, 枠, 馬番, 馬名, 性齢, 斤量, 騎手, タイム, 着差, ...
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.select_one("table.race_table_01") or soup.select_one(
        "table[class*='RaceTable']")
    rows: list[ResultRow] = []
    if not table:
        return rows
    for tr in table.select("tr"):
        tds = tr.find_all("td")
        if len(tds) < 8:
            continue
        rank_text = tds[0].get_text(strip=True)
        rm = re.match(r"(\d+)", rank_text)
        umaban_text = tds[2].get_text(strip=True)
        time_text = tds[7].get_text(strip=True)
        seconds = None
        if re.match(r"^\d{1,2}:\d{2}\.\d$", time_text):
            seconds = time_to_seconds(time_text)
        elif re.match(r"^\d{2,3}\.\d$", time_text):
            seconds = round1(float(time_text))
        rows.append(ResultRow(
            rank_text=rank_text,
            rank=int(rm.group(1)) if rm else None,
            umaban=int(umaban_text) if umaban_text.isdigit() else None,
            name=tds[3].get_text(strip=True),
            time_text=time_text,
            seconds=seconds,
        ))
    return rows
