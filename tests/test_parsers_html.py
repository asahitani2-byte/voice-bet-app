"""HTMLパーサーのテスト（fixture利用）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_logic.parsers import (parse_db_result_html, parse_note_fragment,
                              parse_note_paging, parse_race_distance,
                              parse_shutuba_html)

FIXTURES = Path(__file__).parent / "fixtures"

MEMO_FRAGMENT = """
<li id="RaceNote-202505040511_10">
<div class="RaseDetail">
<dl>
<dd>2025/10/13 東京11R 芝2000m</dd>
<dt>
<a href="https://race.sp.netkeiba.com/?pid=race_result&race_id=202505040511">オクトーバーS</a>
<span class="ResultRank3">3着</span>
</dt>
</dl>
</div>
<div class="RaceComment">
<p class="RaceComment_Text">1:35.5-22.4、-24 木幡巧騎手 好位から</p>
</div>
</li>
<li id="RaceNote-202605021212_10">
<div class="RaseDetail">
<dl>
<dd>2026/05/31 東京12R 芝2500m</dd>
<dt>
<a href="https://race.sp.netkeiba.com/?pid=race_result&race_id=202605021212">目黒記念</a>
<span class="Icon_GradeType Icon_GradeType2">GII</span>
<span class="ResultRank11">11着</span>
</dt>
</dl>
</div>
<div class="RaceComment">
<p class="RaceComment_Text">7.4、2:07.6-22.8、-22</p>
</div>
</li>
"""

# 注意: tr id="tr_N" のNは表示順の連番であり馬番ではない。
# 馬番は Horse_Select の option value「{馬番}_{印}」から取る（実ページ準拠）。
SHUTUBA_MIN = """
<div class="RaceList_NameBox_inner">
  <div class="RaceList_Item01">11R</div>
  <div class="RaceList_Item02">GIII 七夕賞 15:45 芝 2000m (右 B) 16頭 良</div>
</div>
<div class="Shutuba_HorseList">
<table>
<tr class="HorseList" id="tr_1">
  <td class="Waku4">4</td>
  <td class="Horse_Select"><select name="1"><option value="7_0">--</option></select></td>
  <td class="Horse_Info"><dl><dt class="Horse HorseLink">
    <a href="https://race.sp.netkeiba.com/modal/horse.html?race_id=202603020611&horse_id=2019104658&i=0">ボーンディスウェイ</a>
  </dt></dl></td>
  <td class="Popular"><span id="odds-1_07">1.4</span></td>
</tr>
<tr class="HorseList" id="tr_2">
  <td class="Waku2">2</td>
  <td class="Horse_Select"><select name="2"><option value="2_0">--</option></select></td>
  <td class="Horse_Info"><dl><dt class="Horse HorseLink">
    <a href="https://race.sp.netkeiba.com/modal/horse.html?race_id=202603020611&horse_id=2019104998&i=1">ショウナンマグマ</a>
  </dt></dl></td>
  <td class="Popular"><span id="odds-1_02">5.0</span></td>
</tr>
<tr class="HorseList Cancel" id="tr_3">
  <td class="Waku5">5</td>
  <td class="Horse_Info"><dl><dt class="Horse HorseLink">
    <a href="https://race.sp.netkeiba.com/modal/horse.html?race_id=202603020611&horse_id=2020102740&i=2">トリセツウマ</a>
  </dt></dl><span class="Cancel_Txt">出走取消</span>
  <td class="Popular"><span id="ninki-1_05">**</span></td>
</tr>
</table>
</div>
"""

DB_RESULT_MIN = """
<table class="race_table_01 nk_tb_common">
<tr><th>着順</th><th>枠番</th><th>馬番</th><th>馬名</th><th>性齢</th>
<th>斤量</th><th>騎手</th><th>タイム</th><th>着差</th></tr>
<tr><td>1</td><td>3</td><td>4</td><td><a>ファイアンクランツ</a></td>
<td>牡4</td><td>56</td><td>レーン</td><td>2:29.8</td><td></td></tr>
<tr><td>2</td><td>4</td><td>6</td><td><a>ウィクトルウェルス</a></td>
<td>牡4</td><td>57</td><td>ルメール</td><td>2:29.8</td><td>クビ</td></tr>
<tr><td>中</td><td>5</td><td>8</td><td><a>チュウシバ</a></td>
<td>牡5</td><td>57</td><td>某騎手</td><td></td><td></td></tr>
</table>
"""

PAGING_HTML = '<ul data-page="0" data-last="1" id="UserRaceHorseNote-2019104658"><li></li></ul>'


class TestNoteFragment:
    def test_parse_two_notes(self):
        notes = parse_note_fragment(MEMO_FRAGMENT)
        assert len(notes) == 2

    def test_fields(self):
        n = parse_note_fragment(MEMO_FRAGMENT)[0]
        assert n.source_race_id == "202505040511"
        assert n.date_text.startswith("2025/10/13")
        assert n.venue == "東京"
        assert n.race_no == 11
        assert n.track_type == "芝"
        assert n.distance == 2000
        assert n.race_name == "オクトーバーS"
        assert n.rank == 3
        assert "木幡巧騎手" in n.comment
        assert n.parsed_time.head_seconds == 95.5

    def test_second_note_leading_number(self):
        n = parse_note_fragment(MEMO_FRAGMENT)[1]
        assert n.distance == 2500
        assert n.rank == 11
        assert n.parsed_time.head_seconds == 127.6  # 7.4は無視

    def test_real_fixture(self):
        """実取得した断片（存在すれば）でクラッシュしないこと。"""
        f = FIXTURES / "memo_fragment.html"
        if f.exists():
            notes = parse_note_fragment(f.read_text())
            assert len(notes) >= 1
            assert all(len(n.source_race_id) == 12 for n in notes)

    def test_no_notes(self):
        assert parse_note_fragment(
            '<div class="Race_Infomation_Box">馬メモはありません</div>') == []

    def test_broken_html_no_crash(self):
        broken = '<li id="RaceNote-202505040511_1"><dl></dl></li>'
        notes = parse_note_fragment(broken)
        assert len(notes) == 1
        assert notes[0].parsed_time is None
        assert notes[0].distance is None

    def test_paging_attrs(self):
        page, last = parse_note_paging(PAGING_HTML)
        assert page == 0
        assert last is True
        assert parse_note_paging("<div></div>") == (None, None)


class TestShutuba:
    def test_distance_and_track(self):
        info = parse_shutuba_html(SHUTUBA_MIN, "202603020611")
        assert info.distance == 2000
        assert info.track_type == "芝"

    def test_entries(self):
        info = parse_shutuba_html(SHUTUBA_MIN, "202603020611")
        assert len(info.entries) == 3
        # 馬番は tr id（表示順）ではなく option value から取る
        e = next(e for e in info.entries if e.name == "ボーンディスウェイ")
        assert e.umaban == 7   # option value="7_0" 由来（tr_1 ではない）
        assert e.waku == 4
        assert e.horse_id == "2019104658"
        e2 = next(e for e in info.entries if e.name == "ショウナンマグマ")
        assert e2.umaban == 2

    def test_umaban_fallback_to_odds_span(self):
        """selectが無い行はオッズspanのidから馬番を取得。"""
        info = parse_shutuba_html(SHUTUBA_MIN, "202603020611")
        e = next(e for e in info.entries if e.name == "トリセツウマ")
        assert e.umaban == 5   # ninki-1_05 由来（tr_3 ではない）

    def test_cancelled(self):
        info = parse_shutuba_html(SHUTUBA_MIN, "202603020611")
        e5 = next(e for e in info.entries if e.umaban == 5)
        assert e5.is_cancelled
        assert "取消" in e5.cancel_reason

    def test_real_fixture(self):
        f = FIXTURES / "shutuba_202603020611.html"
        if f.exists():
            info = parse_shutuba_html(f.read_text(), "202603020611")
            assert info.distance == 2000
            assert info.track_type == "芝"
            assert len(info.entries) == 16
            # 実ページの馬番は 1〜16 が欠けなく揃うはず
            assert sorted(e.umaban for e in info.entries) == list(range(1, 17))

    def test_distance_variants(self):
        assert parse_race_distance("芝 2000m") == (2000, "芝")
        assert parse_race_distance("ダ1400m") == (1400, "ダート")
        assert parse_race_distance("障害 3000m") == (3000, "障害")
        assert parse_race_distance("芝右 1600m") == (1600, "芝")
        assert parse_race_distance("距離情報なし") == (None, "")

    def test_empty_html(self):
        info = parse_shutuba_html("<html></html>", "X")
        assert info.entries == []
        assert info.distance is None


class TestDbResult:
    def test_rows(self):
        rows = parse_db_result_html(DB_RESULT_MIN)
        assert len(rows) == 3
        assert rows[0].rank == 1
        assert rows[0].name == "ファイアンクランツ"
        assert rows[0].seconds == 149.8

    def test_invalid_time_row(self):
        rows = parse_db_result_html(DB_RESULT_MIN)
        chu = rows[2]
        assert chu.rank is None  # 「中」= 競走中止
        assert chu.seconds is None

    def test_no_table(self):
        assert parse_db_result_html("<html><body>404</body></html>") == []
