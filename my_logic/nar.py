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
RANGE_DAYS = 365           # 分析当日を起点とした過去1年以内を候補にする


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
    """NARの候補選定。

    分析当日を起点とした過去1年以内（RANGE_DAYS）の全レースを候補にする。
    日付は戦績表のrace_info（例「26/06/27 高知 6R」= 2026-06-27）から取得。
    日付が読めない行は1年以内と確認できないため対象外。
    """
    today = today or datetime.date.today()
    cutoff = today - datetime.timedelta(days=RANGE_DAYS)
    in_range = [n for n in notes_all if n.date and n.date >= cutoff]
    res = select_candidate(
        entry, in_range, today_distance, _no_result_fetcher,
        today_track=today_track, section_meters=NAR_SECTION_METERS,
        use_note_gap=True)
    if not res.selected:
        if not notes_all:
            res.no_record_reason = "出走履歴なし（初出走）"
        elif not in_range:
            res.no_record_reason = "1年以内に出走なし"
        elif res.no_record_reason == "レース別馬メモなし":
            res.no_record_reason = "1年以内に対象レースなし"
    res.notes_count = len(notes_all)
    return res
