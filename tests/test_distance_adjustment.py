"""距離補正のテスト。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_logic.analyzer import build_candidates
from my_logic.models import HorseRaceNote
from my_logic.parsers import parse_time_text, seconds_to_time


def note(race_id="202500000001", distance=2000, comment="1:35.5-22.4、-24",
         **kw) -> HorseRaceNote:
    n = HorseRaceNote(source_race_id=race_id, distance=distance,
                      comment=comment, **kw)
    n.parsed_time = parse_time_text(comment)
    return n


def one_candidate(today, dist, comment="1:35.5-22.4、-24"):
    cands, reason = build_candidates([note(distance=dist, comment=comment)], today)
    return cands[0] if cands else None, reason


class TestDistanceAdjustment:
    def test_200m_shorter(self):
        """例1: 今回2000m・過去1800m → +11.2秒 = 1:46.7。"""
        c, _ = one_candidate(2000, 1800)
        assert c.adjustment_seconds == 11.2
        assert c.adjusted_time_seconds == 106.7
        assert seconds_to_time(c.adjusted_time_seconds) == "1:46.7"
        assert c.adjustment_type == "adjusted_shorter"

    def test_100m_shorter(self):
        """例2: 100m差 → 22.4×100÷400 = 5.6秒。"""
        c, _ = one_candidate(2000, 1900)
        assert c.adjustment_seconds == 5.6
        assert c.adjusted_time_seconds == 101.1

    def test_300m_shorter(self):
        """例3: 300m差 → 16.8秒。"""
        c, _ = one_candidate(2000, 1700)
        assert c.adjustment_seconds == 16.8

    def test_399m_design(self):
        """399m差も比例計算できる設計（実際の距離は100m単位だが仕様上対応）。"""
        c, _ = one_candidate(2000, 1601)
        assert c.rejection_reason == ""
        assert c.adjustment_seconds == round(22.4 * 399 / 400, 1)

    def test_400m_diff_is_no_record(self):
        """距離差ちょうど400mは記録なし。"""
        c, reason = one_candidate(2000, 1600)
        assert c.rejection_reason != ""
        assert reason == "近い距離の記録なし"

    def test_over_400m_is_no_record(self):
        c, reason = one_candidate(2000, 1400)
        assert c.rejection_reason != ""
        assert reason == "近い距離の記録なし"

    def test_longer_no_adjustment(self):
        """過去の方が長い場合は補正なし・元タイムのまま比較。"""
        c, _ = one_candidate(1800, 2000)
        assert c.adjustment_type == "none_longer"
        assert c.adjusted_time_seconds is None
        assert c.ranking_time_seconds == 95.5

    def test_minute_carry_after_adjustment(self):
        """補正後に分が繰り上がるケース。1:55.0 + 23.2×100÷400=5.8 → 2:00.8。"""
        c, _ = one_candidate(2000, 1900, "1:55.0-23.2")
        assert c.adjusted_time_seconds == 120.8
        assert seconds_to_time(c.adjusted_time_seconds) == "2:00.8"

    def test_custom_value_preserved(self):
        c, _ = one_candidate(2000, 1800)
        assert c.custom_value == "-24"
        assert c.last_400_seconds == 22.4

    def test_no_custom_value_preserved(self):
        c, _ = one_candidate(2000, 1800, "1:35.5-22.4")
        assert c.custom_value is None
        assert c.adjusted_time_seconds == 106.7

    def test_same_distance_no_adjustment(self):
        c, _ = one_candidate(2000, 2000)
        assert c.adjustment_type == "same"
        assert c.ranking_time_seconds == 95.5
