"""地方競馬（NAR）Myロジックのテスト。"""
import datetime
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_logic.analyzer import build_candidates
from my_logic.models import HorseEntry, HorseRaceNote, ParsedTime
from my_logic.nar import is_nar_race_id, select_candidate_nar
from my_logic.parsers import parse_horse_db_results

# 実ページ（db.sp.netkeiba.com/horse/2020106686/）の構造を再現したfixture
DB_RESULTS_HTML = """
<table>
<tr><th>レース名</th><th>映 像</th><th>人 気</th><th>着 順</th><th>騎手</th>
<th>斤 量</th><th>オッズ</th><th>頭 数</th><th>枠 番</th><th>馬 番</th>
<th>距離</th><th>天 気</th><th>馬 場</th><th>馬場 指数</th><th>タイム</th>
<th>着差</th><th>通過</th><th>ペース</th><th>上り</th><th>馬体重</th>
<th>勝ち馬 （2着馬)</th><th>賞金</th></tr>
<tr>
<td><a href="https://db.sp.netkeiba.com/race/202654062706/">26/06/27 高知 6R Ｃ１ー５</a></td>
<td></td><td>9</td><td>8</td><td>阿部基嗣</td><td>55</td><td>156.6</td>
<td>10</td><td>5</td><td>5</td><td>ダ1600</td><td>曇</td><td>不</td>
<td>-19</td><td>1:46.4</td><td>2.6</td><td>10-10-10-10</td><td></td>
<td>38.8</td><td>464(-6)</td><td>ウィズユアドリーム</td><td></td>
</tr>
<tr>
<td><a href="https://db.sp.netkeiba.com/race/202654061305/">26/06/13 高知 5R Ｃ１ー４</a></td>
<td></td><td>10</td><td>8</td><td>阿部基嗣</td><td>55</td><td>170.9</td>
<td>12</td><td>6</td><td>7</td><td>ダ1400</td><td>晴</td><td>稍</td>
<td>-14</td><td>1:34.3</td><td>3.3</td><td>11-11-11-12</td><td></td>
<td>40.4</td><td>470(+3)</td><td>ララマルシュドロワ</td><td></td>
</tr>
<tr>
<td><a href="https://db.sp.netkeiba.com/race/202654050101/">26/05/01 高知 1R 勝利戦</a></td>
<td></td><td>1</td><td>1</td><td>阿部基嗣</td><td>55</td><td>2.1</td>
<td>10</td><td>1</td><td>1</td><td>ダ1400</td><td>晴</td><td>良</td>
<td></td><td>1:33.0</td><td>-0.4</td><td>1-1-1-1</td><td></td>
<td>39.0</td><td>468(0)</td><td>ニバンテウマ</td><td></td>
</tr>
<tr>
<td><a href="https://db.sp.netkeiba.com/race/202654040101/">26/04/01 高知 1R 中止戦</a></td>
<td></td><td>5</td><td>中</td><td>阿部基嗣</td><td>55</td><td>10.0</td>
<td>10</td><td>2</td><td>2</td><td>ダ1400</td><td>晴</td><td>良</td>
<td></td><td></td><td></td><td></td><td></td>
<td></td><td>468(0)</td><td>ダレカウマ</td><td></td>
</tr>
</table>
"""


class TestParseHorseDbResults:
    def test_row_count_and_ids(self):
        notes = parse_horse_db_results(DB_RESULTS_HTML)
        assert len(notes) == 4
        assert notes[0].source_race_id == "202654062706"

    def test_time_synthesis(self):
        """タイム1:46.4 − 上り38.8 = 1:07.6、表記「1:07.6-38.8、-19」。"""
        n = parse_horse_db_results(DB_RESULTS_HTML)[0]
        assert n.parsed_time is not None
        assert n.parsed_time.head_seconds == 67.6
        assert n.parsed_time.head_text == "1:07.6"
        assert n.parsed_time.last400_seconds == 38.8
        assert n.parsed_time.custom_value == "-19"
        assert n.parsed_time.text == "1:07.6-38.8、-19"

    def test_fields(self):
        n = parse_horse_db_results(DB_RESULTS_HTML)[0]
        assert n.date == datetime.date(2026, 6, 27)
        assert n.venue == "高知"
        assert n.race_no == 6
        assert n.track_type == "ダート"
        assert n.distance == 1600
        assert n.rank == 8
        assert n.gap_to_target == 2.6
        assert n.target_name == "ウィズユアドリーム"

    def test_no_shisu_still_valid(self):
        """馬場指数が空欄でも有効候補（表記に指数なし）。"""
        n = parse_horse_db_results(DB_RESULTS_HTML)[2]
        assert n.parsed_time is not None
        assert n.parsed_time.custom_value is None
        assert n.parsed_time.text == "0:54.0-39.0"

    def test_winner_gap_not_positive(self):
        """自身が勝ち馬の行は gap<=0（TargetHorse条件を必ずクリア）。"""
        n = parse_horse_db_results(DB_RESULTS_HTML)[2]
        assert n.rank == 1
        assert n.gap_to_target == -0.4

    def test_cancelled_row_no_time(self):
        n = parse_horse_db_results(DB_RESULTS_HTML)[3]
        assert n.parsed_time is None
        assert n.rank is None

    def test_empty_html(self):
        assert parse_horse_db_results("<html></html>") == []


PC_RESULTS_HTML = """
<table>
<tr><th>日付</th><th>開催</th><th>天気</th><th>R</th><th>レース名</th>
<th>頭数</th><th>枠番</th><th>馬番</th><th>オッズ</th><th>人気</th>
<th>着順</th><th>騎手</th><th>斤量</th><th>距離</th><th>馬場</th>
<th>馬場指数</th><th>タイム</th><th>着差</th><th>通過</th><th>ペース</th>
<th>上り</th><th>馬体重</th><th>勝ち馬(2着馬)</th><th>賞金</th></tr>
<tr>
<td>2026/06/27</td><td>高知</td><td>曇</td><td>6</td>
<td><a href="https://db.netkeiba.com/race/202654062706/">Ｃ１ー５</a></td>
<td>10</td><td>5</td><td>5</td><td>156.6</td><td>9</td>
<td>8</td><td>阿部基嗣</td><td>55</td><td>ダ1600</td><td>不</td>
<td>-19</td><td>1:46.4</td><td>2.6</td><td>10-10-10-10</td><td></td>
<td>38.8</td><td>464(-6)</td><td>ウィズユアドリーム</td><td></td>
</tr>
<tr>
<td>2025/11/03</td><td>4小倉8</td><td>晴</td><td>12</td>
<td><a href="https://db.netkeiba.com/race/202610040812/">中央戦</a></td>
<td>16</td><td>1</td><td>2</td><td>50.0</td><td>10</td>
<td>12</td><td>某騎手</td><td>56</td><td>芝1200</td><td>良</td>
<td></td><td>1:09.5</td><td>1.2</td><td>3-3</td><td></td>
<td>35.0</td><td>460(0)</td><td>チュウオウウマ</td><td></td>
</tr>
</table>
"""


class TestParsePcLayout:
    """PC版（db.netkeiba.com/horse/result/）の全成績テーブル。"""

    def test_pc_layout_fields(self):
        notes = parse_horse_db_results(PC_RESULTS_HTML)
        assert len(notes) == 2
        n = notes[0]
        assert n.date == datetime.date(2026, 6, 27)
        assert n.venue == "高知"
        assert n.race_no == 6
        assert n.source_race_id == "202654062706"
        assert n.distance == 1600
        assert n.parsed_time.text == "1:07.6-38.8、-19"
        assert n.gap_to_target == 2.6
        assert n.target_name == "ウィズユアドリーム"

    def test_pc_layout_central_venue(self):
        """中央戦の行（開催「4小倉8」）も場名を抽出して扱える。"""
        n = parse_horse_db_results(PC_RESULTS_HTML)[1]
        assert n.venue == "小倉"
        assert n.track_type == "芝"
        assert n.distance == 1200
        assert n.parsed_time.custom_value is None  # 馬場指数空欄でも有効


def nar_note(race_id, distance, head, agari, gap=0.1, days_ago=30,
             track="ダート", shisu="-10"):
    n = HorseRaceNote(
        source_race_id=race_id, distance=distance, track_type=track,
        date=datetime.date.today() - datetime.timedelta(days=days_ago),
        date_text="", race_name=race_id,
        gap_to_target=gap, target_name="カチウマ")
    n.parsed_time = ParsedTime(
        text=f"{head}-{agari}、{shisu}", head_text=str(head),
        head_seconds=head, last400_seconds=agari, custom_value=shisu)
    return n


def entry():
    return HorseEntry(umaban=1, waku=1, name="テスト馬", horse_id="999")


class TestNarSelection:
    def test_is_nar_race_id(self):
        assert is_nar_race_id("202654071210")       # 高知
        assert not is_nar_race_id("202603020611")   # 福島
        assert not is_nar_race_id("abc")

    def test_adjustment_uses_600m(self):
        """距離補正は 上り×距離差÷600。1400m→1600m: 38.8×200÷600=12.9秒。"""
        n = nar_note("A", 1400, 67.6, 38.8)
        cands, _ = build_candidates([n], 1600, section_meters=600)
        assert cands[0].adjustment_seconds == 12.9
        assert cands[0].section_meters == 600

    def test_basic_selection_with_note_gap(self):
        res = select_candidate_nar(entry(), [nar_note("A", 1600, 67.6, 38.8)],
                                   1600, "ダート")
        assert res.selected is not None
        assert res.selected.target_horse_status == "ok"
        assert res.selected.target_horse_gap == 0.1
        assert res.selected.target_horse_name == "カチウマ"

    def test_gap_06_rejected(self):
        notes = [nar_note("A", 1600, 67.0, 38.8, gap=0.6),
                 nar_note("B", 1600, 68.0, 38.8, gap=0.5)]
        res = select_candidate_nar(entry(), notes, 1600, "ダート")
        assert res.selected.source_race_id == "B"

    def test_gap_none_is_unknown_accepted(self):
        n = nar_note("A", 1600, 67.6, 38.8)
        n.gap_to_target = None
        res = select_candidate_nar(entry(), [n], 1600, "ダート")
        assert res.selected is not None
        assert res.selected.target_horse_status == "unknown"

    def test_range_is_one_year_not_ten_runs(self):
        """候補は「過去1年以内の全レース」— 11走目以降でも1年以内なら対象。"""
        notes = [nar_note(f"NG{i}", 1600, 67.0 + i * 0.1, 38.8, gap=1.0,
                          days_ago=10 + i) for i in range(10)]
        notes.append(nar_note("OLD_OK", 1600, 68.0, 38.8, gap=0.1,
                              days_ago=200))
        res = select_candidate_nar(entry(), notes, 1600, "ダート")
        assert res.selected is not None
        assert res.selected.source_race_id == "OLD_OK"

    def test_faster_race_beyond_ten_runs_wins(self):
        """11走目以降にある最速レースが、直近10走内の候補より優先される。"""
        notes = [nar_note(f"SLOW{i}", 1600, 68.0, 38.8, gap=0.1,
                          days_ago=10 + i) for i in range(10)]
        notes.append(nar_note("FAST_11TH", 1600, 66.0, 38.8, gap=0.1,
                              days_ago=300))
        res = select_candidate_nar(entry(), notes, 1600, "ダート")
        assert res.selected.source_race_id == "FAST_11TH"

    def test_over_one_year_not_used(self):
        """1年（365日）より古いレースは速くても対象外。"""
        notes = [nar_note("RECENT", 1600, 68.0, 38.8, gap=0.1, days_ago=30),
                 nar_note("TOO_OLD", 1600, 66.0, 38.8, gap=0.1, days_ago=400)]
        res = select_candidate_nar(entry(), notes, 1600, "ダート")
        assert res.selected.source_race_id == "RECENT"

    def test_all_over_one_year_is_no_record(self):
        notes = [nar_note("OLD", 1600, 66.0, 38.8, gap=0.1, days_ago=400)]
        res = select_candidate_nar(entry(), notes, 1600, "ダート")
        assert res.selected is None
        assert res.no_record_reason == "1年以内に出走なし"

    def test_undated_row_excluded(self):
        """日付が読めない行は1年以内と確認できないため対象外。"""
        n = nar_note("NODATE", 1600, 66.0, 38.8, gap=0.1)
        n.date = None
        res = select_candidate_nar(entry(), [n], 1600, "ダート")
        assert res.selected is None

    def test_no_history(self):
        res = select_candidate_nar(entry(), [], 1600, "ダート")
        assert res.selected is None
        assert res.no_record_reason == "出走履歴なし（初出走）"
