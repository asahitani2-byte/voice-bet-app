"""データモデル定義。"""
from __future__ import annotations

import datetime
from dataclasses import dataclass, field, asdict


@dataclass
class ParsedTime:
    """馬メモから抽出した独自タイム。"""
    text: str                     # 抽出した文字列全体（例 "1:35.5-22.4、-24"）
    head_text: str                # 先頭タイム表示（例 "1:35.5"）
    head_seconds: float           # 先頭タイム秒（例 95.5）
    last400_seconds: float        # ラスト400mタイム（例 22.4）
    custom_value: str | None      # 末尾の独自数値（例 "-24"、無ければ None）


@dataclass
class HorseRaceNote:
    """レース別馬メモ1件。"""
    source_race_id: str
    date_text: str = ""
    date: datetime.date | None = None
    venue: str = ""
    race_no: int | None = None
    track_type: str = ""          # 芝 / ダート / 障害 / ""
    distance: int | None = None
    race_name: str = ""
    rank_text: str = ""
    rank: int | None = None
    comment: str = ""
    parsed_time: ParsedTime | None = None
    source_url: str = ""


@dataclass
class HorseEntry:
    """出馬表の1頭。"""
    umaban: int
    waku: int | None
    name: str
    horse_id: str
    is_cancelled: bool = False
    cancel_reason: str = ""
    modal_url: str = ""


@dataclass
class RaceInfo:
    """今回の分析対象レース。"""
    race_id: str
    name: str = ""
    distance: int | None = None
    track_type: str = ""
    raw_conditions: str = ""
    entries: list[HorseEntry] = field(default_factory=list)


@dataclass
class ResultRow:
    """公式レース結果の1行（TargetHorse判定用）。"""
    rank_text: str
    rank: int | None
    umaban: int | None
    name: str
    time_text: str
    seconds: float | None


@dataclass
class CandidateRecord:
    """馬ごとの過去レース候補（採用判定の追跡データ込み）。"""
    source_race_id: str
    source_race_name: str = ""
    source_date_text: str = ""
    source_venue: str = ""
    source_distance: int | None = None
    source_track_type: str = ""
    note_text: str = ""
    original_time_text: str = ""
    original_time_seconds: float | None = None
    last_400_seconds: float | None = None
    custom_value: str | None = None
    distance_difference: int = 0          # 今回 - 過去（正=過去が短い）
    adjustment_seconds: float = 0.0
    adjusted_time_seconds: float | None = None
    ranking_time_seconds: float | None = None
    adjustment_type: str = "same"         # same / adjusted_shorter / none_longer
    target_horse_name: str = ""
    target_horse_time: float | None = None
    horse_official_time: float | None = None
    target_horse_gap: float | None = None
    target_horse_status: str = "unchecked"  # ok / rejected / unknown / unchecked
    is_selected: bool = False
    rejection_reason: str = ""


@dataclass
class HorseAnalysisResult:
    """1頭分の分析結果。"""
    entry: HorseEntry
    selected: CandidateRecord | None = None
    rejected_candidates: list[CandidateRecord] = field(default_factory=list)
    no_record_reason: str = ""    # 空=記録あり
    notes_count: int = 0
    fetch_error: str = ""
    fetch_warnings: list[str] = field(default_factory=list)
    rank: int | None = None       # ランキング順位（記録なしは None）


@dataclass
class RaceAnalysisResult:
    """レース全体の分析結果。"""
    race: RaceInfo
    horses: list[HorseAnalysisResult] = field(default_factory=list)
    analyzed_at: str = ""
    success_count: int = 0
    no_record_count: int = 0
    error_count: int = 0
    warnings: list[str] = field(default_factory=list)
    danwa: dict[str, str] = field(default_factory=dict)  # {馬番(str): 厩舎の話}


def to_dict(obj) -> dict:
    return asdict(obj)
