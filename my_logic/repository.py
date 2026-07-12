"""SQLite永続化（キャッシュ＋分析履歴）。

UIや分析ロジックから分離し、将来Supabase/PostgreSQLへ差し替えやすい
インターフェースにしている。認証情報・Cookieは一切保存しない。
"""
from __future__ import annotations

import datetime
import json
import logging
import sqlite3
from dataclasses import asdict
from pathlib import Path

from . import CACHE_VERSION, LOGIC_VERSION
from .config import data_dir
from .models import (CandidateRecord, HorseAnalysisResult, HorseEntry,
                     RaceAnalysisResult, RaceInfo)

logger = logging.getLogger("my_logic")

# 保存件数・キャッシュ保持の上限（超過分は保存時に自動削除）
KEEP_RUNS = 10               # 分析履歴は直近10レース分のみ保持
CACHE_MAX_AGE_DAYS = 30      # 30日を超えたキャッシュは削除（再分析で再取得可能）

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cache (
    key TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS analysis_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id TEXT NOT NULL,
    race_name TEXT,
    race_distance INTEGER,
    track_type TEXT,
    analyzed_at TEXT NOT NULL,
    status TEXT,
    horse_count INTEGER,
    success_count INTEGER,
    error_count INTEGER,
    logic_version TEXT,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS analysis_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    analysis_run_id INTEGER NOT NULL REFERENCES analysis_runs(id),
    horse_id TEXT,
    horse_number INTEGER,
    horse_name TEXT,
    rank INTEGER,
    selected_source_race_id TEXT,
    selected_source_race_name TEXT,
    selected_source_distance INTEGER,
    original_time_text TEXT,
    original_time_seconds REAL,
    adjusted_time_text TEXT,
    adjusted_time_seconds REAL,
    ranking_time_seconds REAL,
    last_400_seconds REAL,
    custom_value TEXT,
    distance_difference INTEGER,
    adjustment_type TEXT,
    target_horse_name TEXT,
    target_horse_gap REAL,
    target_horse_status TEXT,
    note_text TEXT,
    no_record_reason TEXT,
    created_at TEXT
);
"""


class Repository:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or (data_dir() / "winvoice_mylogic.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        try:
            with self._connect() as conn:
                conn.executescript(_SCHEMA)
        except sqlite3.DatabaseError as e:
            # 破損時は退避して作り直す（アプリ全体は落とさない）
            logger.error("SQLite破損の可能性: %s (%s)", self.db_path, e)
            try:
                backup = self.db_path.with_suffix(
                    ".corrupt." + datetime.datetime.now().strftime("%Y%m%d%H%M%S"))
                self.db_path.rename(backup)
                logger.error("破損DBを退避: %s", backup)
            except OSError:
                pass
            with self._connect() as conn:
                conn.executescript(_SCHEMA)

    # ─── キャッシュ ──────────────────────────────────────────
    def _cache_key(self, key: str) -> str:
        return f"v{CACHE_VERSION}:{key}"

    def cache_get(self, key: str, max_age_seconds: int | None = None) -> str | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload, created_at FROM cache WHERE key=?",
                    (self._cache_key(key),)).fetchone()
        except sqlite3.DatabaseError as e:
            logger.warning("cache_get失敗 key=%s: %s", key, e)
            return None
        if not row:
            return None
        if max_age_seconds is not None:
            try:
                created = datetime.datetime.fromisoformat(row["created_at"])
                age = (datetime.datetime.now() - created).total_seconds()
                if age > max_age_seconds:
                    return None
            except ValueError:
                return None
        return row["payload"]

    def cache_set(self, key: str, payload: str) -> None:
        try:
            with self._connect() as conn:
                conn.execute(
                    "INSERT OR REPLACE INTO cache(key, payload, created_at) VALUES(?,?,?)",
                    (self._cache_key(key), payload,
                     datetime.datetime.now().isoformat()))
        except sqlite3.DatabaseError as e:
            logger.warning("cache_set失敗 key=%s: %s", key, e)

    # ─── 分析履歴 ────────────────────────────────────────────
    def save_analysis(self, result: RaceAnalysisResult) -> int | None:
        """分析結果を保存してrun_idを返す。失敗時はNone（例外にしない）。"""
        now = datetime.datetime.now().isoformat()
        try:
            with self._connect() as conn:
                cur = conn.execute(
                    """INSERT INTO analysis_runs
                       (race_id, race_name, race_distance, track_type,
                        analyzed_at, status, horse_count, success_count,
                        error_count, logic_version, payload)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (result.race.race_id, result.race.name,
                     result.race.distance, result.race.track_type,
                     result.analyzed_at, "completed",
                     len(result.horses), result.success_count,
                     result.error_count, LOGIC_VERSION,
                     json.dumps(asdict(result), ensure_ascii=False,
                                default=str)))
                run_id = cur.lastrowid
                for h in result.horses:
                    s = h.selected
                    conn.execute(
                        """INSERT INTO analysis_results
                           (analysis_run_id, horse_id, horse_number, horse_name,
                            rank, selected_source_race_id,
                            selected_source_race_name, selected_source_distance,
                            original_time_text, original_time_seconds,
                            adjusted_time_text, adjusted_time_seconds,
                            ranking_time_seconds, last_400_seconds, custom_value,
                            distance_difference, adjustment_type,
                            target_horse_name, target_horse_gap,
                            target_horse_status, note_text, no_record_reason,
                            created_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (run_id, h.entry.horse_id, h.entry.umaban, h.entry.name,
                         h.rank,
                         s.source_race_id if s else None,
                         s.source_race_name if s else None,
                         s.source_distance if s else None,
                         s.original_time_text if s else None,
                         s.original_time_seconds if s else None,
                         _fmt_adj(s) if s else None,
                         s.adjusted_time_seconds if s else None,
                         s.ranking_time_seconds if s else None,
                         s.last_400_seconds if s else None,
                         s.custom_value if s else None,
                         s.distance_difference if s else None,
                         s.adjustment_type if s else None,
                         s.target_horse_name if s else None,
                         s.target_horse_gap if s else None,
                         s.target_horse_status if s else None,
                         s.note_text if s else None,
                         h.no_record_reason or h.fetch_error or None,
                         now))
            self.prune()
            return run_id
        except sqlite3.DatabaseError as e:
            logger.error("分析結果の保存に失敗: %s", e)
            return None

    def prune(self, keep_runs: int = KEEP_RUNS,
              cache_max_age_days: int = CACHE_MAX_AGE_DAYS) -> None:
        """古い履歴・キャッシュを削除して肥大化を防ぐ（保存時に自動実行）。"""
        try:
            deleted = 0
            with self._connect() as conn:
                old_ids = [r[0] for r in conn.execute(
                    "SELECT id FROM analysis_runs ORDER BY id DESC "
                    "LIMIT -1 OFFSET ?", (keep_runs,))]
                if old_ids:
                    ph = ",".join("?" * len(old_ids))
                    conn.execute(
                        f"DELETE FROM analysis_results "
                        f"WHERE analysis_run_id IN ({ph})", old_ids)
                    conn.execute(
                        f"DELETE FROM analysis_runs WHERE id IN ({ph})",
                        old_ids)
                    deleted += len(old_ids)
                cutoff = (datetime.datetime.now() - datetime.timedelta(
                    days=cache_max_age_days)).isoformat()
                cur = conn.execute(
                    "DELETE FROM cache WHERE created_at < ?", (cutoff,))
                deleted += cur.rowcount
            if deleted:
                logger.info("prune: 履歴%d件/古いキャッシュを削除 → VACUUM",
                            len(old_ids))
                # 削除領域をディスクへ返す（トランザクション外で実行）
                conn2 = sqlite3.connect(self.db_path, timeout=10)
                conn2.execute("VACUUM")
                conn2.close()
        except sqlite3.DatabaseError as e:
            logger.warning("pruneに失敗（動作には影響なし）: %s", e)

    def list_runs(self, limit: int = 30) -> list[dict]:
        try:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT id, race_id, race_name, race_distance, track_type,
                              analyzed_at, horse_count, success_count, error_count
                       FROM analysis_runs ORDER BY id DESC LIMIT ?""",
                    (limit,)).fetchall()
                return [dict(r) for r in rows]
        except sqlite3.DatabaseError as e:
            logger.error("履歴一覧の取得に失敗: %s", e)
            return []

    def load_run_payload(self, run_id: int) -> dict | None:
        try:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT payload FROM analysis_runs WHERE id=?",
                    (run_id,)).fetchone()
            return json.loads(row["payload"]) if row and row["payload"] else None
        except (sqlite3.DatabaseError, json.JSONDecodeError) as e:
            logger.error("履歴の読み込みに失敗 run_id=%s: %s", run_id, e)
            return None


def _fmt_adj(s: CandidateRecord) -> str | None:
    if s.adjustment_type != "adjusted_shorter" or s.adjusted_time_seconds is None:
        return None
    from .parsers import seconds_to_time
    tail = f"-{s.last_400_seconds}" if s.last_400_seconds is not None else ""
    custom = f"、{s.custom_value}" if s.custom_value else ""
    return f"{seconds_to_time(s.adjusted_time_seconds)}{tail}{custom}"
