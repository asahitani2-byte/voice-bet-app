"""Myロジック分析 — netkeiba馬メモを使った出走馬ランキング機能。

WINVOICE本体（voice_bet_app.py）から利用される分離パッケージ。
分析ロジックは純粋関数中心（analyzer/parsers）、通信は netkeiba_client、
永続化は repository、画面は ui に分離している。
"""

LOGIC_VERSION = "1.0.0"
CACHE_VERSION = "1"
