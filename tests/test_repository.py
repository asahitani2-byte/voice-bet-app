"""SQLite保存処理のテスト。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from my_logic.analyzer import rank_horses, select_candidate, summarize
from my_logic.models import HorseEntry, HorseRaceNote, RaceInfo, ResultRow
from my_logic.parsers import parse_time_text
from my_logic.repository import Repository


def make_result():
    race = RaceInfo(race_id="202603020611", name="七夕賞",
                    distance=2000, track_type="芝")
    n = HorseRaceNote(source_race_id="202505040511", distance=2000,
                      comment="1:35.5-22.4、-24",
                      date_text="2025/10/13 東京11R 芝2000m",
                      race_name="オクトーバーS")
    n.parsed_time = parse_time_text(n.comment)
    fetcher = lambda rid: [
        ResultRow("1", 1, 5, "カチウマ", "1:35.3", 95.3),
        ResultRow("2", 2, 1, "テスト馬", "1:35.5", 95.5)]
    r1 = select_candidate(
        HorseEntry(umaban=1, waku=1, name="テスト馬", horse_id="111"),
        [n], 2000, fetcher)
    r2 = select_candidate(
        HorseEntry(umaban=2, waku=1, name="メモナシ馬", horse_id="222"),
        [], 2000, fetcher)
    return summarize(race, rank_horses([r1, r2]))


class TestRepository:
    def test_init_creates_db(self, tmp_path):
        db = tmp_path / "t.db"
        Repository(db_path=db)
        assert db.exists()

    def test_save_and_list(self, tmp_path):
        repo = Repository(db_path=tmp_path / "t.db")
        run_id = repo.save_analysis(make_result())
        assert run_id is not None
        runs = repo.list_runs()
        assert len(runs) == 1
        assert runs[0]["race_id"] == "202603020611"
        assert runs[0]["race_distance"] == 2000
        assert runs[0]["success_count"] == 1

    def test_load_payload(self, tmp_path):
        repo = Repository(db_path=tmp_path / "t.db")
        run_id = repo.save_analysis(make_result())
        payload = repo.load_run_payload(run_id)
        assert payload["race"]["race_id"] == "202603020611"
        assert len(payload["horses"]) == 2
        sel = payload["horses"][0]["selected"]
        assert sel["original_time_seconds"] == 95.5

    def test_duplicate_save_creates_new_run(self, tmp_path):
        """同じ分析の再保存は新しいrunとして追加される（履歴として自然）。"""
        repo = Repository(db_path=tmp_path / "t.db")
        repo.save_analysis(make_result())
        repo.save_analysis(make_result())
        assert len(repo.list_runs()) == 2

    def test_corrupt_db_recovers(self, tmp_path):
        db = tmp_path / "t.db"
        db.write_text("this is not a sqlite file at all" * 100)
        repo = Repository(db_path=db)  # 破損検知→退避→再作成
        assert repo.save_analysis(make_result()) is not None

    def test_cache_roundtrip(self, tmp_path):
        repo = Repository(db_path=tmp_path / "t.db")
        repo.cache_set("k1", "value1")
        assert repo.cache_get("k1") == "value1"
        assert repo.cache_get("k1", max_age_seconds=3600) == "value1"
        assert repo.cache_get("k1", max_age_seconds=0) is None  # 期限切れ
        assert repo.cache_get("missing") is None

    def test_load_missing_run(self, tmp_path):
        repo = Repository(db_path=tmp_path / "t.db")
        assert repo.load_run_payload(9999) is None

    def test_prune_keeps_latest_runs(self, tmp_path):
        """11件以上保存すると古い履歴が自動削除され直近10件のみ残る。"""
        repo = Repository(db_path=tmp_path / "t.db")
        run_ids = [repo.save_analysis(make_result()) for _ in range(12)]
        runs = repo.list_runs(limit=100)
        assert len(runs) == 10
        # 残っているのは新しい10件
        assert {r["id"] for r in runs} == set(run_ids[-10:])
        # 削除されたrunのpayload・resultsも消えている
        assert repo.load_run_payload(run_ids[0]) is None
        import sqlite3
        conn = sqlite3.connect(repo.db_path)
        n = conn.execute(
            "SELECT COUNT(*) FROM analysis_results WHERE analysis_run_id=?",
            (run_ids[0],)).fetchone()[0]
        assert n == 0

    def test_prune_removes_old_cache(self, tmp_path):
        import datetime, sqlite3
        repo = Repository(db_path=tmp_path / "t.db")
        repo.cache_set("fresh", "v")
        old = (datetime.datetime.now()
               - datetime.timedelta(days=40)).isoformat()
        conn = sqlite3.connect(repo.db_path)
        conn.execute(
            "INSERT INTO cache(key, payload, created_at) VALUES(?,?,?)",
            ("v1:stale", "v", old))
        conn.commit()
        conn.close()
        repo.prune()
        assert repo.cache_get("fresh") == "v"
        conn = sqlite3.connect(repo.db_path)
        assert conn.execute(
            "SELECT COUNT(*) FROM cache WHERE key='v1:stale'"
        ).fetchone()[0] == 0
