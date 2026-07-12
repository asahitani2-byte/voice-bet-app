"""候補選定・距離補正・TargetHorse判定・ランキング（純粋ロジック・通信なし）。

通信が必要な公式結果は result_fetcher コールバックで注入する。
"""
from __future__ import annotations

import datetime
import logging
from typing import Callable

from .models import (CandidateRecord, HorseAnalysisResult, HorseEntry,
                     HorseRaceNote, RaceAnalysisResult, RaceInfo, ResultRow)
from .parsers import round1

logger = logging.getLogger("my_logic")

# 仕様定数
MAX_DISTANCE_DIFF = 400        # 距離差がこれ以上（>=）なら記録なし
NEAR_DISTANCE_DIFF = 200       # 同距離がある場合に比較する別距離の上限（<=）
TARGET_HORSE_REJECT_GAP = 0.6  # TargetHorseとの差がこれ以上（>=）で除外

# result_fetcher: 過去race_id -> 公式結果行リスト（取得不能はNone）
ResultFetcher = Callable[[str], list[ResultRow] | None]


def build_candidates(notes: list[HorseRaceNote], today_distance: int,
                     section_meters: int = 400) -> tuple[
        list[CandidateRecord], str]:
    """メモ→候補化。有効候補リストと、空のときの記録なし理由を返す。

    section_meters: 末尾区間の距離。中央=400（ラスト400m）、
    地方=600（上り3ハロン）。距離補正の分母に使う。
    """
    if not notes:
        return [], "レース別馬メモなし"
    timed = [n for n in notes if n.parsed_time and n.distance]
    if not timed:
        return [], "対象タイムなし"

    candidates: list[CandidateRecord] = []
    for n in notes:
        if not (n.parsed_time and n.distance):
            continue
        diff = today_distance - n.distance
        c = CandidateRecord(
            source_race_id=n.source_race_id,
            source_race_name=n.race_name,
            source_date_text=n.date_text,
            source_venue=n.venue,
            source_distance=n.distance,
            source_track_type=n.track_type,
            note_text=n.comment,
            original_time_text=n.parsed_time.text,
            original_time_seconds=n.parsed_time.head_seconds,
            last_400_seconds=n.parsed_time.last400_seconds,
            custom_value=n.parsed_time.custom_value,
            distance_difference=diff,
            section_meters=section_meters,
            precomputed_gap=n.gap_to_target,
            precomputed_target=n.target_name,
        )
        if abs(diff) >= MAX_DISTANCE_DIFF:
            c.rejection_reason = f"距離差{abs(diff)}m（400m以上）"
            candidates.append(c)
            continue
        if diff > 0:
            # 過去が短い → 加算補正（分母は末尾区間の距離）
            c.adjustment_seconds = round1(
                n.parsed_time.last400_seconds * diff / float(section_meters))
            c.adjusted_time_seconds = round1(
                n.parsed_time.head_seconds + c.adjustment_seconds)
            c.ranking_time_seconds = c.adjusted_time_seconds
            c.adjustment_type = "adjusted_shorter"
        elif diff < 0:
            # 過去が長い → 補正なしでそのまま比較
            c.ranking_time_seconds = n.parsed_time.head_seconds
            c.adjustment_type = "none_longer"
        else:
            c.ranking_time_seconds = n.parsed_time.head_seconds
            c.adjustment_type = "same"
        candidates.append(c)

    in_range = [c for c in candidates if not c.rejection_reason]
    if not in_range:
        return candidates, "近い距離の記録なし"
    return candidates, ""


def _distance_tiers(group: list[CandidateRecord]) -> list[list[CandidateRecord]]:
    """1つの馬場グループ内を距離ルールで優先度順の段（tier）に分ける。

    - 同距離があれば「同距離＋距離差200m以内」が第1段
    - 以降（同距離が無い場合は最初から）距離差の小さい順に1段ずつ
      （案A: 前の段が全滅したら次の距離の段へ繰り下がる）
    """
    tiers: list[list[CandidateRecord]] = []
    rest = list(group)
    if any(c.distance_difference == 0 for c in group):
        tier0 = [c for c in group
                 if c.distance_difference == 0
                 or abs(c.distance_difference) <= NEAR_DISTANCE_DIFF]
        tiers.append(tier0)
        rest = [c for c in group if c not in tier0]
    for d in sorted({abs(c.distance_difference) for c in rest}):
        tiers.append([c for c in rest if abs(c.distance_difference) == d])
    return tiers


def _comparison_tiers(candidates: list[CandidateRecord],
                      today_track: str = "") -> list[list[CandidateRecord]]:
    """候補全体を優先度順の段リストにする。

    案B: 今回と同じ馬場区分（芝/ダート/障害）のグループを最優先し、
         同馬場の全段が尽きた場合のみ他馬場のグループへ進む。
    案A: 各グループ内では距離差の近い段から順に試す（_distance_tiers）。
    """
    valid = [c for c in candidates if not c.rejection_reason]
    if not valid:
        return []
    if today_track:
        same_track = [c for c in valid if c.source_track_type == today_track]
        other_track = [c for c in valid if c.source_track_type != today_track]
        groups = [g for g in (same_track, other_track) if g]
    else:
        groups = [valid]
    tiers: list[list[CandidateRecord]] = []
    for g in groups:
        tiers.extend(_distance_tiers(g))
    return tiers


def _date_key(c: CandidateRecord) -> str:
    """新しいレース優先ソート用（date_textの先頭 YYYY/MM/DD を利用）。"""
    return c.source_date_text[:10] or "0000/00/00"


def _sort_candidates(pool: list[CandidateRecord]) -> list[CandidateRecord]:
    return sorted(pool, key=lambda c: (
        c.ranking_time_seconds if c.ranking_time_seconds is not None else 9999.0,
        c.last_400_seconds if c.last_400_seconds is not None else 999.0,
        # 新しい順（降順）
        tuple(-ord(ch) for ch in _date_key(c)),
    ))


def check_target_horse(candidate: CandidateRecord, horse_name: str,
                       rows: list[ResultRow] | None) -> None:
    """公式結果からTargetHorse判定を行い、candidateを更新する。

    TargetHorse = その過去レースの勝ち馬（対象馬自身が勝ち馬なら2着馬）。
    対象馬の走破タイム - TargetHorseの走破タイム >= 0.6秒 → rejected。
    情報不足は unknown（採用可・警告表示）。
    """
    if not rows:
        candidate.target_horse_status = "unknown"
        return
    norm = lambda s: (s or "").replace("　", "").replace(" ", "")
    me = next((r for r in rows if norm(r.name) == norm(horse_name)), None)
    if me is None or me.seconds is None:
        candidate.target_horse_status = "unknown"
        logger.info("TargetHorse判定不能: %s in %s（対象馬の公式タイムなし）",
                    horse_name, candidate.source_race_id)
        return
    candidate.horse_official_time = me.seconds
    # 勝ち馬（自身を除く最上位）
    others = [r for r in rows if r is not me and r.seconds is not None
              and r.rank is not None]
    if not others:
        candidate.target_horse_status = "unknown"
        return
    if me.rank == 1:
        # 自身が勝ち馬 → 2着馬（同着1着が他にいればその馬）
        target = min(others, key=lambda r: (r.rank, r.seconds))
        if target.rank == 1:
            logger.info("同着1着の特殊ケース: %s / %s",
                        candidate.source_race_id, target.name)
    else:
        target = next((r for r in others if r.rank == 1), None)
        if target is None:
            candidate.target_horse_status = "unknown"
            return
    candidate.target_horse_name = target.name
    candidate.target_horse_time = target.seconds
    gap = round1(me.seconds - target.seconds)
    candidate.target_horse_gap = gap
    if gap >= TARGET_HORSE_REJECT_GAP:
        candidate.target_horse_status = "rejected"
        candidate.rejection_reason = (
            f"TargetHorse({target.name})との差{gap:.1f}秒（0.6秒以上）")
    else:
        candidate.target_horse_status = "ok"


def check_target_horse_from_note(candidate: CandidateRecord) -> None:
    """戦績表の着差列（勝ち馬との秒差）でTargetHorse判定する（NAR用）。

    - 着差が取得できないレースは unknown（採用可・警告表示）
    - 対象馬自身が勝ち馬の場合はパース時に gap<=0 で格納されている
    """
    gap = candidate.precomputed_gap
    if gap is None:
        candidate.target_horse_status = "unknown"
        return
    gap = round1(gap)
    candidate.target_horse_gap = gap
    candidate.target_horse_name = candidate.precomputed_target
    if gap >= TARGET_HORSE_REJECT_GAP:
        candidate.target_horse_status = "rejected"
        candidate.rejection_reason = (
            f"TargetHorse({candidate.precomputed_target or '勝ち馬'})との差"
            f"{gap:.1f}秒（0.6秒以上）")
    else:
        candidate.target_horse_status = "ok"


def select_candidate(entry: HorseEntry, notes: list[HorseRaceNote],
                     today_distance: int,
                     result_fetcher: ResultFetcher,
                     today_track: str = "",
                     section_meters: int = 400,
                     use_note_gap: bool = False) -> HorseAnalysisResult:
    """1頭分の候補選定。

    優先度: 同馬場グループ → 他馬場グループ（案B）、
    各グループ内は 同距離(+200m以内) → 距離差の近い順の段（案A）。
    各段の中ではタイム順にTargetHorse判定し、段が全滅したら次の段へ。
    """
    res = HorseAnalysisResult(entry=entry, notes_count=len(notes))
    candidates, empty_reason = build_candidates(notes, today_distance,
                                                section_meters=section_meters)
    if empty_reason:
        res.no_record_reason = empty_reason
        res.rejected_candidates = candidates
        return res

    selected = None
    for tier in _comparison_tiers(candidates, today_track):
        for cand in _sort_candidates(tier):
            if use_note_gap:
                # NAR: 戦績表の着差列から事前計算済みの差で判定
                check_target_horse_from_note(cand)
            else:
                rows = None
                try:
                    rows = result_fetcher(cand.source_race_id)
                except Exception as e:  # 取得失敗は判定不能として扱う
                    logger.warning("公式結果取得失敗 race_id=%s: %s",
                                   cand.source_race_id, e)
                check_target_horse(cand, entry.name, rows)
            if cand.target_horse_status == "rejected":
                continue
            if cand.target_horse_status == "unknown":
                res.fetch_warnings.append(
                    f"{cand.source_race_name or cand.source_race_id}: "
                    "TargetHorse判定不能")
            selected = cand
            break
        if selected is not None:
            break
    if selected is None:
        res.no_record_reason = "TargetHorse条件を満たす記録なし"
    else:
        selected.is_selected = True
        res.selected = selected
        # 未評価のまま残った候補に理由を付ける（追跡用）
        for c in candidates:
            if not c.is_selected and not c.rejection_reason \
                    and c.target_horse_status == "unchecked":
                if today_track and c.source_track_type != today_track:
                    c.rejection_reason = "馬場区分が異なるため比較対象外（未評価）"
                else:
                    c.rejection_reason = "優先順位の高い候補が採用されたため比較対象外"
    res.rejected_candidates = [c for c in candidates if not c.is_selected]
    return res


def rank_horses(results: list[HorseAnalysisResult]) -> list[HorseAnalysisResult]:
    """全馬をランキング順に並べ、rankを付与する。

    記録あり: 採用タイム昇順 → 同タイムは馬番昇順。順位は連番。
    記録なし: 最下部で馬番昇順。rank=None。
    """
    with_rec = [r for r in results if r.selected and
                r.selected.ranking_time_seconds is not None]
    without = [r for r in results if r not in with_rec]
    with_rec.sort(key=lambda r: (r.selected.ranking_time_seconds,
                                 r.entry.umaban))
    without.sort(key=lambda r: r.entry.umaban)
    for i, r in enumerate(with_rec, start=1):
        r.rank = i
    for r in without:
        r.rank = None
    return with_rec + without


def summarize(race: RaceInfo, results: list[HorseAnalysisResult]) -> RaceAnalysisResult:
    out = RaceAnalysisResult(race=race, horses=results)
    out.analyzed_at = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    out.success_count = sum(1 for r in results if r.selected)
    out.error_count = sum(1 for r in results if r.fetch_error)
    out.no_record_count = sum(
        1 for r in results if not r.selected and not r.fetch_error)
    return out
