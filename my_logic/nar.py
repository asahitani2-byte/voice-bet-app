"""地方競馬（NAR）向けMyロジック。

中央（馬メモベース）と異なり、出走馬DBページの戦績表から採用タイムを合成する:
  先頭タイム = 走破タイム − 上り3F、表記「1:07.6-38.8、-19」（末尾は馬場指数）
- 距離補正の分母は600m（上り3ハロン基準）
- TargetHorse判定は戦績表の着差列（勝ち馬との秒差、検証済み）を使用
- 候補範囲: 直近10走 → 有効候補が無ければ直近1年以内まで拡大 → 無ければ記録なし
選定・ランキングのルール自体（同馬場優先/距離段/0.6秒/ソート）は中央と共通。
"""
from __future__ import annotations

import datetime
import logging

from .analyzer import select_candidate
from .models import HorseAnalysisResult, HorseEntry, HorseRaceNote

logger = logging.getLogger("my_logic")

NAR_SECTION_METERS = 600   # 上り3ハロン
PRIMARY_RUNS = 10          # まず直近10走
EXTEND_DAYS = 365          # フォールバックは1年以内


def is_nar_race_id(race_id: str) -> bool:
    """race_idが地方競馬か（場コードが中央01〜10以外）。"""
    rid = str(race_id or "")
    if len(rid) != 12 or not rid.isdigit():
        return False
    return not ("01" <= rid[4:6] <= "10")


def _no_result_fetcher(_race_id: str):
    """NARは戦績表の着差で判定するため公式結果は取得しない。"""
    return None


def select_candidate_nar(entry: HorseEntry, notes_all: list[HorseRaceNote],
                         today_distance: int, today_track: str,
                         today: datetime.date | None = None
                         ) -> HorseAnalysisResult:
    """NARの候補選定（直近10走 → 1年以内フォールバック）。

    notes_all は新しい順（戦績表の表示順）である前提。
    """
    today = today or datetime.date.today()
    primary = notes_all[:PRIMARY_RUNS]
    res = select_candidate(
        entry, primary, today_distance, _no_result_fetcher,
        today_track=today_track, section_meters=NAR_SECTION_METERS,
        use_note_gap=True)
    if res.selected:
        return res

    # 直近10走で採用なし → 1年以内の全レースへ拡大（追加分がある場合のみ）
    cutoff = today - datetime.timedelta(days=EXTEND_DAYS)
    extended = [n for n in notes_all if n.date and n.date >= cutoff]
    primary_ids = {n.source_race_id for n in primary}
    if {n.source_race_id for n in extended} - primary_ids:
        res2 = select_candidate(
            entry, extended, today_distance, _no_result_fetcher,
            today_track=today_track, section_meters=NAR_SECTION_METERS,
            use_note_gap=True)
        if res2.selected:
            res2.fetch_warnings.append(
                "直近10走に有効候補がないため直近1年まで対象を拡大")
            return res2
        res = res2
    if not res.selected and not res.no_record_reason:
        res.no_record_reason = "1年以内に対象レースなし"
    elif res.no_record_reason == "レース別馬メモなし":
        res.no_record_reason = "出走履歴なし（初出走）"
    return res
