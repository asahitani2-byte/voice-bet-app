"""開催日まるごと一括集計。

- 中央: race_list の日付タブ（今週）＋次週リンクから開催日を取得し、
  日付ごとの全場・全レースのrace_idを収集する
- 地方: race_list の kaisai_id タブ（例 2026540712 = 高知 7/12）から
  開催場を取得し、race_idは規則（年+場コード+月日+R）で生成する
- 1レースずつMyロジック分析を実行（analyze_race）
"""
from __future__ import annotations

import datetime
import json
import logging
import re

from bs4 import BeautifulSoup

from .analyzer import rank_horses, select_candidate, summarize
from .keibabook import fetch_danwa
from .models import HorseAnalysisResult, RaceAnalysisResult
from .nar import NAR_VENUES, is_nar_race_id, select_candidate_nar
from .netkeiba_client import BlockedError, NetkeibaClient, NetkeibaError
from .repository import Repository

logger = logging.getLogger("my_logic")

JRA_VENUES = {
    "01": "札幌", "02": "函館", "03": "福島", "04": "新潟", "05": "東京",
    "06": "中山", "07": "中京", "08": "京都", "09": "阪神", "10": "小倉",
}
_WD = ["月", "火", "水", "木", "金", "土", "日"]

TTL_SCHEDULE = 3 * 3600


def _date_label(date_str: str) -> str:
    d = datetime.date(int(date_str[:4]), int(date_str[4:6]), int(date_str[6:8]))
    return f"{d.month}/{d.day}({_WD[d.weekday()]})"


def _parse_jra_list_page(html: str) -> tuple[dict[str, dict[str, list[str]]], str]:
    """race_listページから {日付: {場名: [race_id...]}} と次週リンク日付を返す。"""
    soup = BeautifulSoup(html, "html.parser")
    wraps = soup.find_all("div", class_="RaceListDayWrap")
    dates = [a.get("data-date")
             for a in soup.select(".Tab_RaceDaySelect a[data-date]")]
    out: dict[str, dict[str, list[str]]] = {}
    if len(dates) == len(wraps):
        pairs = zip(dates, wraps)
    else:  # フォールバック: 表示中のみ
        visible = [w for w in wraps
                   if "display:none" not in re.sub(r"\s", "", w.get("style") or "")]
        pairs = zip(dates[:1], visible[:1])
    for date_str, wrap in pairs:
        if not date_str:
            continue
        by_venue: dict[str, list[str]] = {}
        for rid in dict.fromkeys(re.findall(r"race_id=(\d{12})", str(wrap))):
            vname = JRA_VENUES.get(rid[4:6])
            if vname:
                by_venue.setdefault(vname, []).append(rid)
        for v in by_venue:
            by_venue[v].sort(key=lambda r: int(r[10:12]))
        out[date_str] = by_venue
    next_a = soup.select_one(".RaceDayNext a")
    m = re.search(r"kaisai_date=(\d{8})", next_a.get("href") or "") if next_a else None
    return out, (m.group(1) if m else "")


def get_jra_schedule(client: NetkeibaClient, repo: Repository) -> list[dict]:
    """中央の開催日リスト（今週＋翌週）。

    Returns: [{"date": "20260711", "label": "7/11(土)",
               "venues": {"福島": [race_id...], ...}}, ...]
    """
    cached = None if client.force_refresh else repo.cache_get(
        "jra_schedule", TTL_SCHEDULE)
    if cached:
        try:
            return json.loads(cached)
        except json.JSONDecodeError:
            pass
    schedule: dict[str, dict[str, list[str]]] = {}
    r = client._request("GET", "https://race.sp.netkeiba.com/?pid=race_list")
    week, next_date = _parse_jra_list_page(r.text)
    schedule.update(week)
    if next_date:
        r2 = client._request(
            "GET",
            f"https://race.sp.netkeiba.com/?pid=race_list&kaisai_date={next_date}")
        week2, _ = _parse_jra_list_page(r2.text)
        schedule.update(week2)
    today = datetime.date.today().strftime("%Y%m%d")
    result = [{"date": d, "label": _date_label(d), "venues": v}
              for d, v in sorted(schedule.items()) if d >= today and v]
    repo.cache_set("jra_schedule", json.dumps(result, ensure_ascii=False))
    return result


def get_nar_venues(client: NetkeibaClient, repo: Repository,
                   date_str: str) -> list[tuple[str, str]]:
    """指定日の地方開催場 [(場コード, 場名), ...] を kaisai_id タブから取得。"""
    key = f"nar_venues:{date_str}"
    cached = None if client.force_refresh else repo.cache_get(key, TTL_SCHEDULE)
    if cached:
        try:
            return [tuple(x) for x in json.loads(cached)]
        except json.JSONDecodeError:
            pass
    # 注意: ?pid=race_list&kaisai_date= は未来日を無視して当日を返すため
    # /top/race_list.html パスを使う（タブに表示中の場は含まれないので
    # 表示中race_idからの補完と合算する）
    r = client._request(
        "GET",
        f"https://nar.sp.netkeiba.com/top/race_list.html?kaisai_date={date_str}")
    codes = sorted(set(
        m[4:6] for m in re.findall(r"kaisai_id=(\d{10})", r.text)
        if m[6:] == date_str[4:]))
    # 表示中の場（タブに出ないことがある）もrace_idから補完
    for rid in re.findall(r"race_id=(\d{12})", r.text):
        if rid[6:10] == date_str[4:] and rid[4:6] in NAR_VENUES:
            if rid[4:6] not in codes:
                codes.append(rid[4:6])
    venues = [(c, NAR_VENUES[c]) for c in sorted(set(codes)) if c in NAR_VENUES]
    repo.cache_set(key, json.dumps(venues, ensure_ascii=False))
    return venues


def nar_race_ids(date_str: str, venue_code: str, max_races: int = 12) -> list[str]:
    """NARのrace_idを規則生成（年+場+月日+R）。存在しないRは分析時にスキップ。"""
    return [f"{date_str[:4]}{venue_code}{date_str[4:]}{r:02d}"
            for r in range(1, max_races + 1)]


def analyze_race(repo: Repository, client: NetkeibaClient, race_id: str,
                 progress_cb=None, with_danwa: bool = True
                 ) -> RaceAnalysisResult:
    """1レース分のMyロジック分析（UI・一括集計で共用のコア処理）。

    呼び出し前に client.ensure_login() 済みであること。
    レース取得不能などは NetkeibaError を送出する。
    """
    def _cb(text: str, frac: float | None = None):
        if progress_cb:
            progress_cb(text, frac)

    nar = is_nar_race_id(race_id)
    race = client.get_shutuba(race_id, nar=nar)
    targets = [e for e in race.entries if not e.is_cancelled]
    warnings = [f"{e.umaban}番 {e.name}: {e.cancel_reason or '取消/除外'}のため分析対象外"
                for e in race.entries if e.is_cancelled]
    results: list[HorseAnalysisResult] = []
    n = len(targets)
    for i, entry in enumerate(targets):
        _cb(f"{entry.name} の{'戦績' if nar else 'メモ'}を取得中",
            (i + 1) / max(n, 1))
        try:
            if nar:
                notes_all = client.get_horse_db_results(entry.horse_id)
                res = select_candidate_nar(entry, notes_all, race.distance,
                                           race.track_type)
            else:
                notes, warns = client.get_horse_notes(entry.horse_id)
                res = select_candidate(entry, notes, race.distance,
                                       client.get_race_result,
                                       today_track=race.track_type)
                res.fetch_warnings.extend(warns)
        except BlockedError:
            raise
        except Exception:
            logger.exception("馬の分析失敗 horse_id=%s", entry.horse_id)
            res = HorseAnalysisResult(entry=entry, fetch_error="取得エラー",
                                      no_record_reason="取得エラー")
        results.append(res)
    out = summarize(race, rank_horses(results))
    out.warnings = warnings
    if with_danwa:
        danwa, danwa_warn = fetch_danwa(race_id, repo,
                                        force_refresh=client.force_refresh)
        out.danwa = danwa
        if danwa_warn:
            out.warnings.append(danwa_warn)
    return out
