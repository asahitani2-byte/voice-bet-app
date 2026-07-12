"""候補選定・TargetHorse判定・ランキングのテスト。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_logic.analyzer import rank_horses, select_candidate
from my_logic.models import HorseEntry, HorseRaceNote, ResultRow
from my_logic.parsers import parse_time_text

HORSE = "テスト馬"


def note(race_id, distance, comment, date_text="2026/01/01 東京11R 芝2000m",
         track="芝"):
    n = HorseRaceNote(source_race_id=race_id, distance=distance,
                      comment=comment, date_text=date_text, track_type=track)
    n.parsed_time = parse_time_text(comment)
    return n


def entry(umaban=1, name=HORSE):
    return HorseEntry(umaban=umaban, waku=1, name=name, horse_id="999")


def result_rows(horse_time, target_time, horse_rank=2,
                horse_name=HORSE, target_name="勝チ馬"):
    """horse=2着、target=1着の標準的な公式結果を作る。"""
    rows = [
        ResultRow("1", 1, 5, target_name, "", target_time),
        ResultRow(str(horse_rank), horse_rank, 1, horse_name, "", horse_time),
        ResultRow("3", 3, 7, "その他馬", "", max(horse_time, target_time) + 1.0),
    ]
    if horse_rank == 1:
        rows[0], rows[1] = (
            ResultRow("1", 1, 1, horse_name, "", horse_time),
            ResultRow("2", 2, 5, target_name, "", target_time))
    return sorted(rows, key=lambda r: r.rank)


def ok_fetcher(rid):
    """常にTargetHorse条件を満たす結果（差0.1秒）。"""
    return result_rows(95.6, 95.5)


class TestCandidateSelection:
    def test_single_same_distance(self):
        res = select_candidate(entry(), [note("A", 2000, "1:35.5-22.4")],
                               2000, ok_fetcher)
        assert res.selected is not None
        assert res.selected.source_race_id == "A"
        assert res.selected.ranking_time_seconds == 95.5

    def test_multiple_same_distance_fastest_wins(self):
        notes = [note("SLOW", 2000, "1:36.0-22.4"),
                 note("FAST", 2000, "1:35.0-22.4")]
        res = select_candidate(entry(), notes, 2000, ok_fetcher)
        assert res.selected.source_race_id == "FAST"

    def test_tie_broken_by_last400(self):
        notes = [note("L_SLOW", 2000, "1:35.5-23.0"),
                 note("L_FAST", 2000, "1:35.5-22.0")]
        res = select_candidate(entry(), notes, 2000, ok_fetcher)
        assert res.selected.source_race_id == "L_FAST"

    def test_same_vs_200m_shorter(self):
        """同距離1:36.0 vs 1800m 1:23.0(+11.2=94.2) → 補正後が速ければ採用。"""
        notes = [note("SAME", 2000, "1:36.0-22.4"),
                 note("SHORT", 1800, "1:23.0-22.4")]
        res = select_candidate(entry(), notes, 2000, ok_fetcher)
        assert res.selected.source_race_id == "SHORT"
        assert res.selected.ranking_time_seconds == 94.2

    def test_same_vs_200m_longer(self):
        """同距離 vs 200m長い（補正なし）を比較。長い方が速ければ採用。"""
        notes = [note("SAME", 2000, "1:36.0-22.4"),
                 note("LONG", 2200, "1:35.0-22.4")]
        res = select_candidate(entry(), notes, 2000, ok_fetcher)
        assert res.selected.source_race_id == "LONG"
        assert res.selected.adjustment_type == "none_longer"

    def test_same_exists_300m_excluded(self):
        """同距離があるとき300m差は比較対象外（どんなに速くても）。"""
        notes = [note("SAME", 2000, "1:36.0-22.4"),
                 note("FAR", 1700, "1:10.0-21.0")]
        res = select_candidate(entry(), notes, 2000, ok_fetcher)
        assert res.selected.source_race_id == "SAME"
        far = next(c for c in res.rejected_candidates
                   if c.source_race_id == "FAR")
        assert "比較対象外" in far.rejection_reason

    def test_no_same_nearest_distance(self):
        """同距離なし → 最小距離差の距離のみ。"""
        notes = [note("NEAR", 1900, "1:30.0-22.4"),    # 100m差
                 note("FARTHER", 1700, "1:10.0-21.0")]  # 300m差（速いが対象外）
        res = select_candidate(entry(), notes, 2000, ok_fetcher)
        assert res.selected.source_race_id == "NEAR"

    def test_equal_diff_short_and_long_faster_wins(self):
        """距離差が同じ短距離・長距離 → 比較用タイムが速い方。"""
        notes = [note("SHORT", 1800, "1:30.0-22.4"),   # +11.2 → 101.2
                 note("LONG", 2200, "1:40.0-22.4")]    # 補正なし → 100.0
        res = select_candidate(entry(), notes, 2000, ok_fetcher)
        assert res.selected.source_race_id == "LONG"

    def test_all_over_400m(self):
        notes = [note("A", 1500, "1:20.0-22.0"),
                 note("B", 2500, "2:30.0-23.0")]
        res = select_candidate(entry(), notes, 2000, ok_fetcher)
        assert res.selected is None
        assert res.no_record_reason == "近い距離の記録なし"

    def test_no_notes(self):
        res = select_candidate(entry(), [], 2000, ok_fetcher)
        assert res.no_record_reason == "レース別馬メモなし"

    def test_no_timed_notes(self):
        res = select_candidate(entry(), [note("A", 2000, "好走した")],
                               2000, ok_fetcher)
        assert res.no_record_reason == "対象タイムなし"


class TestTargetHorse:
    def test_gap_05_accepted(self):
        fetcher = lambda rid: result_rows(95.8 + 0.0, 95.3)  # 差0.5
        res = select_candidate(entry(), [note("A", 2000, "1:35.5-22.4")],
                               2000, fetcher)
        assert res.selected is not None
        assert res.selected.target_horse_gap == 0.5
        assert res.selected.target_horse_status == "ok"

    def test_gap_06_rejected(self):
        fetcher = lambda rid: result_rows(95.9, 95.3)  # 差0.6
        res = select_candidate(entry(), [note("A", 2000, "1:35.5-22.4")],
                               2000, fetcher)
        assert res.selected is None
        assert res.no_record_reason == "TargetHorse条件を満たす記録なし"

    def test_gap_07_rejected(self):
        fetcher = lambda rid: result_rows(96.0, 95.3)  # 差0.7
        res = select_candidate(entry(), [note("A", 2000, "1:35.5-22.4")],
                               2000, fetcher)
        assert res.selected is None

    def test_horse_is_winner_uses_second(self):
        """対象馬自身が勝ち馬 → 2着馬がTargetHorse。"""
        fetcher = lambda rid: result_rows(95.0, 95.4, horse_rank=1)
        res = select_candidate(entry(), [note("A", 2000, "1:35.5-22.4")],
                               2000, fetcher)
        assert res.selected is not None
        assert res.selected.target_horse_name == "勝チ馬"
        assert res.selected.target_horse_gap == -0.4  # 勝っているので負

    def test_fastest_rejected_next_used(self):
        """最速候補がTargetHorse除外 → 次点候補を採用。"""
        def fetcher(rid):
            if rid == "FAST":
                return result_rows(96.0, 95.3)  # 差0.7 → 除外
            return result_rows(95.5, 95.3)      # 差0.2 → OK
        notes = [note("FAST", 2000, "1:35.0-22.4"),
                 note("NEXT", 2000, "1:35.8-22.4")]
        res = select_candidate(entry(), notes, 2000, fetcher)
        assert res.selected.source_race_id == "NEXT"
        fast = next(c for c in res.rejected_candidates
                    if c.source_race_id == "FAST")
        assert fast.target_horse_status == "rejected"

    def test_unavailable_is_unknown_but_accepted(self):
        res = select_candidate(entry(), [note("A", 2000, "1:35.5-22.4")],
                               2000, lambda rid: None)
        assert res.selected is not None
        assert res.selected.target_horse_status == "unknown"
        assert any("判定不能" in w for w in res.fetch_warnings)

    def test_all_rejected(self):
        fetcher = lambda rid: result_rows(96.5, 95.3)  # 差1.2 → 全除外
        notes = [note("A", 2000, "1:35.0-22.4"),
                 note("B", 2000, "1:36.0-22.4")]
        res = select_candidate(entry(), notes, 2000, fetcher)
        assert res.selected is None
        assert res.no_record_reason == "TargetHorse条件を満たす記録なし"

    def test_applies_to_shorter_and_longer(self):
        """短い距離候補・長い距離候補にもTargetHorse判定が適用される。"""
        fetcher = lambda rid: result_rows(96.0, 95.3)  # 全部差0.7 → 除外
        notes = [note("S", 1800, "1:23.0-22.4"),
                 note("L", 2200, "1:40.0-22.4")]
        res = select_candidate(entry(), notes, 2000, fetcher)
        assert res.selected is None


class TestTrackTypePriority:
    """案B: 今回と同じ馬場区分を優先。案A: 段が全滅したら次の距離へ。"""

    def test_same_track_wins_over_nearer_other_track(self):
        """織姫賞ケース: 芝1800mの分析で、ダ1700(100m差)より芝1600(200m差)を優先。"""
        notes = [note("DIRT", 1700, "1:22.1-26.6、-8", track="ダート"),
                 note("TURF", 1600, "1:09.5-22.9、-25", track="芝")]
        res = select_candidate(entry(), notes, 1800, ok_fetcher,
                               today_track="芝")
        assert res.selected.source_race_id == "TURF"

    def test_other_track_used_when_no_same_track(self):
        """同馬場の候補が無ければ他馬場を採用（フォールバック）。"""
        notes = [note("DIRT", 1700, "1:22.1-26.6、-8", track="ダート")]
        res = select_candidate(entry(), notes, 1800, ok_fetcher,
                               today_track="芝")
        assert res.selected.source_race_id == "DIRT"

    def test_fallback_to_other_track_when_same_track_all_rejected(self):
        """同馬場が全てTargetHorse除外 → 他馬場グループへ進む。"""
        def fetcher(rid):
            if rid == "TURF":
                return result_rows(96.0, 95.3)  # 差0.7 → 除外
            return result_rows(95.5, 95.3)      # OK
        notes = [note("TURF", 1800, "1:35.0-22.4", track="芝"),
                 note("DIRT", 1800, "1:37.0-24.0", track="ダート")]
        res = select_candidate(entry(), notes, 1800, fetcher,
                               today_track="芝")
        assert res.selected.source_race_id == "DIRT"

    def test_no_track_info_single_group(self):
        """今回の馬場区分が不明なら馬場では分けない（従来動作）。"""
        notes = [note("DIRT", 1700, "1:20.0-26.0", track="ダート"),
                 note("TURF", 1600, "1:15.0-22.9", track="芝")]
        res = select_candidate(entry(), notes, 1800, ok_fetcher,
                               today_track="")
        assert res.selected.source_race_id == "DIRT"  # 最小距離差優先


class TestDistanceTierFallback:
    """案A: 最近接距離の段が全滅したら次に近い距離の段へ繰り下がる。"""

    def test_nearest_rejected_falls_to_next_distance(self):
        """織姫賞ケースの案A側: 100m差が除外 → 200m差へ繰り下がり。"""
        def fetcher(rid):
            if rid == "NEAR":
                return result_rows(96.0, 95.3)  # 差0.7 → 除外
            return result_rows(95.5, 95.3)      # OK
        notes = [note("NEAR", 1700, "1:20.0-23.0"),
                 note("FAR", 1600, "1:09.5-22.9")]
        res = select_candidate(entry(), notes, 1800, fetcher,
                               today_track="芝")
        assert res.selected.source_race_id == "FAR"
        near = next(c for c in res.rejected_candidates
                    if c.source_race_id == "NEAR")
        assert near.target_horse_status == "rejected"

    def test_same_dist_tier_rejected_falls_beyond_200m(self):
        """同距離+200m以内の段が全滅 → 300m差の段へ繰り下がり。"""
        def fetcher(rid):
            if rid in ("SAME", "NEAR200"):
                return result_rows(96.0, 95.3)  # 除外
            return result_rows(95.5, 95.3)
        notes = [note("SAME", 2000, "1:35.0-22.4"),
                 note("NEAR200", 1800, "1:23.0-22.4"),
                 note("FAR300", 1700, "1:10.0-21.0")]
        res = select_candidate(entry(), notes, 2000, fetcher,
                               today_track="芝")
        assert res.selected.source_race_id == "FAR300"

    def test_all_tiers_rejected_is_no_record(self):
        fetcher = lambda rid: result_rows(96.5, 95.3)  # 全除外
        notes = [note("A", 1700, "1:20.0-23.0"),
                 note("B", 1600, "1:09.5-22.9", track="ダート")]
        res = select_candidate(entry(), notes, 1800, fetcher,
                               today_track="芝")
        assert res.selected is None
        assert res.no_record_reason == "TargetHorse条件を満たす記録なし"


class TestRanking:
    def _result(self, umaban, ranking_time=None):
        e = entry(umaban=umaban, name=f"馬{umaban}")
        if ranking_time is None:
            return select_candidate(e, [], 2000, ok_fetcher)
        # ranking_time秒になるコメントを合成
        m, s = divmod(ranking_time, 60)
        res = select_candidate(
            e, [note(f"R{umaban}", 2000, f"{int(m)}:{s:04.1f}-22.4")],
            2000, ok_fetcher)
        return res

    def test_order_and_ranks(self):
        results = [self._result(1, 96.0), self._result(2, 95.0),
                   self._result(3)]
        ranked = rank_horses(results)
        assert [r.entry.umaban for r in ranked] == [2, 1, 3]
        assert [r.rank for r in ranked] == [1, 2, None]

    def test_tie_by_umaban(self):
        results = [self._result(5, 95.5), self._result(2, 95.5)]
        ranked = rank_horses(results)
        assert [r.entry.umaban for r in ranked] == [2, 5]
        assert [r.rank for r in ranked] == [1, 2]

    def test_no_record_bottom_by_umaban(self):
        results = [self._result(9), self._result(3), self._result(1, 95.0)]
        ranked = rank_horses(results)
        assert [r.entry.umaban for r in ranked] == [1, 3, 9]
        assert ranked[1].rank is None and ranked[2].rank is None
