"""タイム解析・秒変換のテスト。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from my_logic.parsers import (parse_time_text, round1, seconds_to_time,
                              time_to_seconds)


class TestParseTimeText:
    def test_basic_with_custom(self):
        t = parse_time_text("1:35.5-22.4、-24")
        assert t is not None
        assert t.head_text == "1:35.5"
        assert t.head_seconds == 95.5
        assert t.last400_seconds == 22.4
        assert t.custom_value == "-24"

    def test_leading_number_skipped(self):
        """先頭の 13.2 ではなくコロン形式のタイムを取得する。"""
        t = parse_time_text("13.2、1:23.5-22.9、-22")
        assert t is not None
        assert t.head_text == "1:23.5"
        assert t.head_seconds == 83.5
        assert t.last400_seconds == 22.9
        assert t.custom_value == "-22"

    def test_no_custom_value(self):
        t = parse_time_text("1:09.0-22.4")
        assert t is not None
        assert t.head_seconds == 69.0
        assert t.last400_seconds == 22.4
        assert t.custom_value is None

    def test_positive_custom_value(self):
        t = parse_time_text("1:35.5-22.4、+3")
        assert t is not None
        assert t.custom_value == "+3"

    def test_with_jockey_comment_after(self):
        t = parse_time_text("1:35.5-22.4、-24 木幡巧騎手 好位から抜け出す")
        assert t is not None
        assert t.head_seconds == 95.5
        assert t.custom_value == "-24"

    def test_first_match_only(self):
        t = parse_time_text("前走1:40.0-23.0、-10、今回1:35.5-22.4、-24")
        assert t is not None
        assert t.head_seconds == 100.0  # 最初の一致

    def test_no_match(self):
        assert parse_time_text("好位から抜け出して快勝") is None
        assert parse_time_text("") is None
        assert parse_time_text("13.2のみ") is None

    def test_fullwidth_and_spaces(self):
        t = parse_time_text("１:３５.５-２２.４、-２４")
        assert t is not None
        assert t.head_seconds == 95.5
        t2 = parse_time_text("1:35.5 - 22.4 、 -24")
        assert t2 is not None
        assert t2.custom_value == "-24"

    def test_two_three_minutes(self):
        t = parse_time_text("2:07.6-22.8、-22")
        assert t.head_seconds == 127.6
        t = parse_time_text("3:09.4-23.5")
        assert t.head_seconds == 189.4

    def test_fullwidth_dash(self):
        t = parse_time_text("1:35.5−22.4、-24")  # U+2212
        assert t is not None
        assert t.last400_seconds == 22.4


class TestSecondsConversion:
    def test_to_seconds(self):
        assert time_to_seconds("1:35.5") == 95.5
        assert time_to_seconds("2:07.6") == 127.6
        assert time_to_seconds("3:09.4") == 189.4

    def test_to_text(self):
        assert seconds_to_time(106.7) == "1:46.7"
        assert seconds_to_time(129.7) == "2:09.7"
        assert seconds_to_time(95.5) == "1:35.5"

    def test_minute_carry(self):
        assert seconds_to_time(119.96) == "2:00.0"  # 繰り上がり
        assert seconds_to_time(59.96) == "1:00.0"

    def test_rounding_half_up(self):
        assert round1(11.25) == 11.3   # half-up
        assert round1(95.55) == 95.6
        assert seconds_to_time(95.55) == "1:35.6"

    def test_float_precision(self):
        # 0.1+0.2系の誤差で表示が崩れないこと
        assert seconds_to_time(95.5 + 11.2) == "1:46.7"
        assert round1(22.4 * 200 / 400) == 11.2

    def test_under_one_minute(self):
        assert seconds_to_time(59.8) == "0:59.8"
