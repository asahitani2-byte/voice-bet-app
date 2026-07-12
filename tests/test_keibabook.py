"""keibabook（厩舎の話）連携のテスト。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_logic.keibabook import netkeiba_to_keibabook_id, parse_danwa_html

DANWA_MIN = """
<table class="danwa">
<tr><th class="waku">枠番</th><th class="umaban">馬番</th><th>馬名 厩舎の話</th></tr>
<tr><td class="waku">1</td><td class="umaban">1</td><td class="left">ボーンディスウェイ</td></tr>
<tr><td class="danwa">○ボーンディスウェイ(ここ目標)
　牧師――ここを目標に順調ですし、いつもと変わらないデキにありますよ。</td></tr>
<tr><td></td></tr>
<tr><td class="waku">1</td><td class="umaban">2</td><td class="left">コントラポスト</td></tr>
<tr><td class="danwa">○コントラポスト(状態良)
　調教師――前走から上積みがあります。</td></tr>
</table>
"""


class TestIdConversion:
    def test_tanabata_sho(self):
        """七夕賞: 場03福島・回02 → 回02・場06。"""
        assert netkeiba_to_keibabook_id("202603020611") == "202602060611"

    def test_memory_example(self):
        """過去プロジェクトの例: 場06中山・回04 → 回04・場05。"""
        assert netkeiba_to_keibabook_id("202506040605") == "202504050605"

    def test_all_venues(self):
        # 東京05→04, 京都08→00
        assert netkeiba_to_keibabook_id("202605010101") == "202601040101"
        assert netkeiba_to_keibabook_id("202608010101") == "202601000101"

    def test_invalid(self):
        assert netkeiba_to_keibabook_id("abc") is None
        assert netkeiba_to_keibabook_id("") is None
        # 地方の場コード（30=門別）は未対応 → None
        assert netkeiba_to_keibabook_id("202630010101") is None


class TestParseDanwa:
    def test_two_horses(self):
        d = parse_danwa_html(DANWA_MIN)
        assert set(d.keys()) == {"1", "2"}
        assert "牧師――ここを目標に" in d["1"]
        assert "上積み" in d["2"]

    def test_empty(self):
        assert parse_danwa_html("<html></html>") == {}

    def test_umaban_without_danwa(self):
        """談話行が無い馬はスキップ（クラッシュしない）。"""
        html = """<table class="danwa">
        <tr><td class="umaban">3</td><td class="left">馬A</td></tr>
        <tr><td class="umaban">4</td><td class="left">馬B</td></tr>
        <tr><td class="danwa">馬Bの談話</td></tr>
        </table>"""
        d = parse_danwa_html(html)
        assert d == {"4": "馬Bの談話"}


class TestChihouId:
    def test_nar_kb_id(self):
        """地方ID: {開催日}{当日idx}{R}{月日}（浦和7/13 11R の実例）。"""
        from my_logic.keibabook import nar_kb_id
        assert nar_kb_id("20260713", "01", 11) == "2026071301110713"
        assert nar_kb_id("20260713", "02", 1) == "2026071302010713"
