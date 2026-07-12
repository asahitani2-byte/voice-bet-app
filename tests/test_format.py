"""表示フォーマッタ・スプレッドシート行生成のテスト。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_logic.analyzer import rank_horses, summarize
from my_logic.format import RESULT_COLUMNS, build_tsv, result_to_rows, time_full
from my_logic.models import (CandidateRecord, HorseAnalysisResult, HorseEntry,
                             RaceInfo)


def make_result():
    race = RaceInfo(race_id="202654071210", name="ファイナルレース(C1)",
                    distance=1400, track_type="ダート")
    sel = CandidateRecord(
        source_race_id="X", source_race_name="ヒマワリ特別",
        source_date_text="2025/07/27 高知11R ダ1300m",
        source_distance=1300, source_track_type="ダート",
        original_time_text="0:44.5-38.9、-20", original_time_seconds=44.5,
        last_400_seconds=38.9, custom_value="-20", distance_difference=100,
        adjustment_type="adjusted_shorter", adjustment_seconds=6.5,
        adjusted_time_seconds=51.0, ranking_time_seconds=51.0,
        target_horse_status="ok", target_horse_gap=-0.3,
        target_horse_name="カチウマ", note_text="メモ本文", is_selected=True,
        section_meters=600)
    h1 = HorseAnalysisResult(
        entry=HorseEntry(umaban=3, waku=2, name="ニヨドスマイル", horse_id="1"),
        selected=sel)
    h2 = HorseAnalysisResult(
        entry=HorseEntry(umaban=5, waku=3, name="キロクナシ", horse_id="2"),
        no_record_reason="1年以内に対象レースなし")
    result = summarize(race, rank_horses([h1, h2]))
    result.danwa = {"3": "厩舎の話テスト", "5": "こちらも談話"}
    return result


class TestResultToRows:
    def test_structure(self):
        rows = result_to_rows(make_result())
        assert rows[0] == ["10R ファイナルレース(C1)　ダート1400m"]
        assert rows[1] == RESULT_COLUMNS
        assert len(rows) == 4  # タイトル+ヘッダ+2頭

    def test_selected_row(self):
        rows = result_to_rows(make_result())
        r = rows[2]
        assert r[0] == "1" and r[1] == "3" and r[2] == "ニヨドスマイル"
        assert r[3] == "0:51.0-38.9、-20"     # 採用タイム（フル表記）
        assert r[4] == "0:44.5-38.9、-20"     # 元タイム
        assert r[6] == "ヒマワリ特別（2025/07/27 高知11R ダ1300m）"
        assert r[9] == "メモ本文"
        assert r[10] == "厩舎の話テスト"

    def test_no_record_row(self):
        rows = result_to_rows(make_result())
        r = rows[3]
        assert r[0] == "－" and r[3] == "記録なし"
        assert r[6] == "1年以内に対象レースなし"
        assert r[10] == "こちらも談話"

    def test_no_duplicate_r_prefix(self):
        result = make_result()
        result.race.name = "10R ファイナルレース(C1)"
        rows = result_to_rows(result)
        assert rows[0][0].count("10R") == 1

    def test_tsv(self):
        tsv = build_tsv(make_result())
        lines = tsv.split("\n")
        assert lines[0].split("\t") == RESULT_COLUMNS
        assert len(lines) == 3

    def test_time_full_no_adjustment(self):
        s = CandidateRecord(source_race_id="Y",
                            original_time_text="1:33.4-22.8、-20",
                            adjustment_type="same")
        assert time_full(s) == "1:33.4-22.8、-20"
