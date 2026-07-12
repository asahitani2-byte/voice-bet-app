"""分析結果の表示用フォーマッタ（UI・スプレッドシート出力で共用）。"""
from __future__ import annotations

from .models import CandidateRecord, RaceAnalysisResult
from .parsers import seconds_to_time


def adj_text(s: CandidateRecord) -> str:
    if s.adjustment_type != "adjusted_shorter" or s.adjusted_time_seconds is None:
        return "なし"
    return seconds_to_time(s.adjusted_time_seconds)


def time_full(s: CandidateRecord) -> str:
    """採用タイムのフル表記（例 "1:21.2-24.2、-16"）。

    補正ありは補正後の先頭タイム＋元のラスト区間・独自数値、
    補正なしは元タイム文字列をそのまま返す。
    """
    if s.adjustment_type == "adjusted_shorter" and s.adjusted_time_seconds is not None:
        tail = (f"-{s.last_400_seconds}"
                if s.last_400_seconds is not None else "")
        custom = f"、{s.custom_value}" if s.custom_value else ""
        return f"{seconds_to_time(s.adjusted_time_seconds)}{tail}{custom}"
    return s.original_time_text


def race_label(s: CandidateRecord) -> str:
    """採用レースの統合表記（例 "3歳未勝利（2026/04/05 阪神1R ダ1400m）"）。"""
    name = s.source_race_name or s.source_race_id
    detail = s.source_date_text.strip() if s.source_date_text else ""
    if not detail:
        detail = f"{s.source_venue}{s.source_track_type}{s.source_distance}m"
    return f"{name}（{detail}）"


def dist_label(s: CandidateRecord) -> str:
    if s.distance_difference == 0:
        return "同距離"
    if s.distance_difference > 0:
        return f"{s.distance_difference}m短いレースから補正"
    return f"{-s.distance_difference}m長いレース・補正なし"


def th_label(s: CandidateRecord) -> str:
    if s.target_horse_status == "ok" and s.target_horse_gap is not None:
        return f"差{s.target_horse_gap:+.1f}秒（{s.target_horse_name}）"
    if s.target_horse_status == "unknown":
        return "判定不能"
    return "-"


RESULT_COLUMNS = ["順位", "馬番", "馬名", "採用タイム", "元タイム",
                  "補正後タイム", "採用レース", "距離区分", "TargetHorse判定",
                  "メモ", "厩舎の話"]


def result_to_rows(result: RaceAnalysisResult,
                   include_header: bool = True) -> list[list[str]]:
    """1レース分の結果を表形式（スプレッドシート貼り付け用）に変換する。"""
    race = result.race
    rows: list[list[str]] = []
    rno = race.race_id[10:12].lstrip("0")
    name = race.name or race.race_id
    # レース名が既に「11R …」で始まる場合はR番号を重複させない
    head = name if name.startswith(f"{rno}R") else f"{rno}R {name}"
    rows.append([f"{head}　{race.track_type}{race.distance}m"])
    if include_header:
        rows.append(RESULT_COLUMNS)
    for h in result.horses:
        s = h.selected
        danwa = result.danwa.get(str(h.entry.umaban), "")
        if s:
            rows.append([
                str(h.rank), str(h.entry.umaban), h.entry.name,
                time_full(s), s.original_time_text, adj_text(s),
                race_label(s), dist_label(s), th_label(s),
                (s.note_text or "").replace("\n", " "),
                danwa.replace("\n", " "),
            ])
        else:
            rows.append([
                "－", str(h.entry.umaban), h.entry.name, "記録なし", "", "",
                h.no_record_reason or h.fetch_error or "", "", "", "",
                danwa.replace("\n", " "),
            ])
    return rows


def build_tsv(result: RaceAnalysisResult) -> str:
    rows = result_to_rows(result)
    return "\n".join("\t".join(r) for r in rows[1:])  # タイトル行は除く
