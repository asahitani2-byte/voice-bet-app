"""Streamlit UI（Myロジック分析・簡易パスワード認証・履歴・コピー）。

voice_bet_app.py からは require_app_password() と
render_mylogic_section() の2つだけを呼び出す（既存機能への影響を最小化）。
"""
from __future__ import annotations

import hmac
import logging
import logging.handlers
import re

import streamlit as st

from . import LOGIC_VERSION
from .analyzer import rank_horses, select_candidate, summarize
from .batch import (analyze_race, get_jra_schedule, get_nar_venues,
                    nar_race_ids)
from .config import data_dir, get_secret
from .format import (adj_text as _adj_text, build_tsv as _build_tsv,
                     dist_label as _dist_label, race_label as _race_label,
                     result_to_rows, th_label as _th_label,
                     time_full as _time_full)
from .models import HorseAnalysisResult, RaceAnalysisResult
from .keibabook import fetch_danwa
from .nar import is_nar_race_id, select_candidate_nar
from .netkeiba_client import BlockedError, NetkeibaClient, NetkeibaError
from .parsers import seconds_to_time
from .repository import Repository
from .sheets import export_to_spreadsheet, setup_guide, sheets_configured

logger = logging.getLogger("my_logic")


def _setup_logging() -> None:
    if logger.handlers:
        return
    logger.setLevel(logging.INFO)
    handler = logging.handlers.RotatingFileHandler(
        data_dir() / "mylogic.log", maxBytes=2_000_000, backupCount=2,
        encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)


_setup_logging()


# ─── アプリ簡易認証 ──────────────────────────────────────────
def require_app_password() -> None:
    """APP_PASSWORD が設定されていれば認証ゲートを表示する。

    未設定の場合はローカル開発モードとして通す（警告表示のみ）。
    外部公開前に必ず APP_PASSWORD を設定すること（README参照）。
    """
    password = get_secret("APP_PASSWORD")
    if not password:
        st.session_state["_app_auth_mode"] = "open"
        return
    if st.session_state.get("_app_authed"):
        with st.sidebar:
            if st.button("🔒 ログアウト", key="app_logout",
                         use_container_width=True):
                st.session_state["_app_authed"] = False
                st.rerun()
        return
    st.title("🔐 WINVOICE")
    st.caption("アプリパスワードを入力してください")
    entered = st.text_input("パスワード", type="password", key="_app_pw_input")
    if st.button("ログイン", type="primary"):
        if hmac.compare_digest(entered, password):
            st.session_state["_app_authed"] = True
            st.rerun()
        else:
            st.error("パスワードが違います")
    st.stop()


def _show_recent_log(lines: int = 15) -> None:
    """直近の警告/エラーログを画面に表示する（原因調査用）。

    ログには認証情報を書かない運用のため、そのまま表示して安全。
    """
    try:
        log_path = data_dir() / "mylogic.log"
        if not log_path.exists():
            return
        recent = [l for l in log_path.read_text(encoding="utf-8",
                                                errors="replace").splitlines()
                  if " WARNING " in l or " ERROR " in l][-lines:]
        if recent:
            with st.expander("🔍 直近のエラーログ（調査用）", expanded=True):
                st.code("\n".join(recent), language=None)
    except OSError:
        pass


# ─── 分析の実行 ──────────────────────────────────────────────
def _run_analysis(race_id: str, use_cache: bool) -> RaceAnalysisResult | None:
    repo = Repository()
    client = NetkeibaClient(repo, force_refresh=not use_cache)

    status_box = st.status("分析を準備しています...", expanded=True)
    progress = status_box.progress(0.0)
    msg = status_box.empty()

    nar = is_nar_race_id(race_id)
    try:
        msg.write("レース情報を取得中...")
        race = client.get_shutuba(race_id, nar=nar)
    except BlockedError as e:
        status_box.update(label="アクセス制限を検知", state="error")
        st.error(str(e))
        return None
    except NetkeibaError as e:
        status_box.update(label="レース取得エラー", state="error")
        st.error(str(e))
        return None

    track = race.track_type or "?"
    st.session_state["mylogic_race_label"] = (
        f"{race.name}（{track}{race.distance}m / race_id: {race_id}）")
    msg.write(f"分析対象: **{race.name}** 距離: **{track}{race.distance}m**")

    msg.write("netkeibaのログイン状態を確認中...（クラウド初回は数分かかります）")
    if not client.ensure_login():
        status_box.update(label="netkeibaログイン失敗", state="error")
        has_creds = bool(
            (get_secret("NETKEIBA_LOGIN_ID") or get_secret("NETKEIBA_USER"))
            and (get_secret("NETKEIBA_PASSWORD") or get_secret("NETKEIBA_PASS")))
        if not has_creds:
            st.error(
                "netkeibaのログイン情報が見つかりません。\n\n"
                "- ローカル: `.env` に NETKEIBA_LOGIN_ID / NETKEIBA_PASSWORD を設定\n"
                "- クラウド(Streamlit Cloud): アプリの Settings → Secrets に "
                "NETKEIBA_LOGIN_ID / NETKEIBA_PASSWORD を設定\n"
                "（ローカルはサイドバーの「🔐 netkeiba ログイン設定」でも可）")
        else:
            st.error(
                "netkeibaのログイン情報は設定されていますが、ログインに失敗しました。\n\n"
                "考えられる原因：\n"
                "1. IDまたはパスワードの誤り（値を再確認してください）\n"
                "2. クラウド環境の場合：ブラウザ(Chromium)の準備失敗、または "
                "netkeibaがクラウドIPからのアクセスを制限している可能性")
            _show_recent_log()
        return None

    targets = [e for e in race.entries if not e.is_cancelled]
    skipped = [e for e in race.entries if e.is_cancelled]
    results: list[HorseAnalysisResult] = []
    global_warnings: list[str] = []
    for e in skipped:
        global_warnings.append(
            f"{e.umaban}番 {e.name}: {e.cancel_reason or '取消/除外'}のため分析対象外")

    n = len(targets)
    for i, entry in enumerate(targets):
        src = "戦績" if nar else "メモ"
        msg.write(f"{i + 1} / {n}頭を分析中 — {entry.name} の{src}を取得しています")
        progress.progress((i + 1) / max(n, 1))
        try:
            if nar:
                # 地方: 戦績表から採用タイムを合成（タイム−上り3F、着差でTH判定）
                notes_all = client.get_horse_db_results(entry.horse_id)
                res = select_candidate_nar(entry, notes_all, race.distance,
                                           race.track_type)
            else:
                notes, warns = client.get_horse_notes(entry.horse_id)
                res = select_candidate(entry, notes, race.distance,
                                       client.get_race_result,
                                       today_track=race.track_type)
                res.fetch_warnings.extend(warns)
        except BlockedError as e2:
            # ブロック検知: 以降の取得を中止し、取得済み分のみで継続
            st.warning(str(e2))
            res = HorseAnalysisResult(entry=entry, fetch_error="取得エラー",
                                      no_record_reason="取得エラー")
            results.append(res)
            for rest in targets[i + 1:]:
                results.append(HorseAnalysisResult(
                    entry=rest, fetch_error="取得エラー",
                    no_record_reason="取得エラー（アクセス制限のため未取得）"))
            break
        except NetkeibaError as e2:
            logger.warning("馬の取得失敗 %s: %s", entry.horse_id, e2)
            res = HorseAnalysisResult(entry=entry, fetch_error="取得エラー",
                                      no_record_reason="取得エラー")
        except Exception as e2:  # 想定外でも1頭の失敗で全体を止めない
            logger.exception("想定外エラー horse_id=%s", entry.horse_id)
            res = HorseAnalysisResult(entry=entry, fetch_error="取得エラー",
                                      no_record_reason="取得エラー")
        results.append(res)

    ranked = rank_horses(results)
    out = summarize(race, ranked)
    out.warnings = global_warnings

    # 厩舎の話（競馬ブック）— 中央・地方両対応。失敗しても分析は継続
    msg.write("厩舎の話（競馬ブック）を取得中...")
    danwa, danwa_warn = fetch_danwa(race_id, repo, force_refresh=not use_cache)
    out.danwa = danwa
    if danwa_warn:
        out.warnings.append(danwa_warn)

    run_id = repo.save_analysis(out)
    if run_id is None:
        st.warning("履歴の保存に失敗しました（分析結果は表示されます）")

    status_box.update(
        label=(f"分析完了 — 取得成功: {out.success_count}頭 / "
               f"記録なし: {out.no_record_count}頭 / "
               f"取得失敗: {out.error_count}頭"),
        state="complete", expanded=False)
    return out


# ─── 結果の表示 ──────────────────────────────────────────────
# フォーマッタ群は format.py に共通化（スプレッドシート出力と共用）




def _render_result(result: RaceAnalysisResult) -> None:
    race = result.race
    track = race.track_type or ""
    st.markdown(
        f"#### 分析対象：{race.name or race.race_id}\n"
        f"距離：**{track}{race.distance}m**　（race_id: `{race.race_id}`）")
    if result.warnings:
        for w in result.warnings:
            st.caption(f"⚠️ {w}")

    # サマリー表（PC向け・スマホでは横スクロール）
    rows = []
    for h in result.horses:
        s = h.selected
        rows.append({
            "順位": h.rank if h.rank else "－",
            "馬番": h.entry.umaban,
            "馬名": h.entry.name,
            "採用タイム": _time_full(s) if s else "記録なし",
            "採用レース": _race_label(s) if s else
                        (h.no_record_reason or h.fetch_error or ""),
            "TH判定": _th_label(s) if s else "",
        })
    st.dataframe(rows, use_container_width=True, hide_index=True)

    # 馬ごとの詳細カード（スマホ向け・展開式）
    st.markdown("##### 詳細（タップで展開）")
    for h in result.horses:
        s = h.selected
        if s:
            title = (f"{h.rank}位　{h.entry.umaban}番 {h.entry.name}　"
                     f"{seconds_to_time(s.ranking_time_seconds)}")
        else:
            title = (f"－　{h.entry.umaban}番 {h.entry.name}　"
                     f"記録なし（{h.no_record_reason or h.fetch_error}）")
        with st.expander(title):
            if s:
                lines = [
                    f"**採用タイム**：{s.original_time_text}" +
                    (f" → 補正後 {_adj_text(s)}-{s.last_400_seconds}" +
                     (f"、{s.custom_value}" if s.custom_value else "")
                     if s.adjustment_type == "adjusted_shorter" else ""),
                    f"**元タイム**：{s.original_time_text}",
                    f"**補正後タイム**：{_adj_text(s)}",
                    f"**採用レース**：{s.source_race_name or s.source_race_id}"
                    f"（{s.source_date_text}）",
                    f"**採用距離**：{s.source_track_type}{s.source_distance}m",
                    f"**今回の距離**：{race.track_type}{race.distance}m",
                    f"**距離区分**：{_dist_label(s)}",
                ]
                if s.adjustment_type == "adjusted_shorter":
                    section = getattr(s, "section_meters", 400) or 400
                    lines.append(
                        f"**補正式**：{s.original_time_text.split('-')[0]} + "
                        f"{s.last_400_seconds} × {s.distance_difference} ÷ {section} "
                        f"= +{s.adjustment_seconds:.1f}秒")
                if s.target_horse_status == "ok":
                    lines.append(
                        f"**TargetHorseとの差**：{s.target_horse_gap:+.1f}秒"
                        f"（{s.target_horse_name}）")
                elif s.target_horse_status == "unknown":
                    lines.append("**TargetHorse判定**：判定不能（採用は有効）")
                st.markdown("  \n".join(lines))
                _danwa = result.danwa.get(str(h.entry.umaban))
                if _danwa:
                    st.markdown("**厩舎の話（競馬ブック）**：")
                    st.text(_danwa)
                st.markdown("**メモ全文**：")
                st.text(s.note_text or "（なし）")
                if h.rejected_candidates:
                    with st.container():
                        st.caption("除外された候補：")
                        for c in h.rejected_candidates[:8]:
                            if c.rejection_reason:
                                st.caption(
                                    f"・{c.source_race_name or c.source_race_id}"
                                    f"（{c.source_distance}m）: {c.rejection_reason}")
            else:
                st.markdown(f"**理由**：{h.no_record_reason or h.fetch_error}")
                if h.notes_count:
                    st.caption(f"メモ件数: {h.notes_count}")
                _danwa = result.danwa.get(str(h.entry.umaban))
                if _danwa:
                    st.markdown("**厩舎の話（競馬ブック）**：")
                    st.text(_danwa)
            for w in h.fetch_warnings:
                st.caption(f"⚠️ {w}")

    # クリップボードコピー（タブ区切り）
    with st.expander("📋 結果をコピー（タブ区切り・スプレッドシート貼り付け用）"):
        tsv = _build_tsv(result)
        st.caption("右上のコピーアイコンでクリップボードへコピーできます")
        st.code(tsv, language=None)
        st.download_button("TSVをダウンロード", tsv,
                           file_name=f"mylogic_{race.race_id}.tsv",
                           mime="text/tab-separated-values")


# ─── 履歴の復元表示 ──────────────────────────────────────────
def _render_history() -> None:
    repo = Repository()
    runs = repo.list_runs()
    if not runs:
        st.caption("保存された分析履歴はまだありません")
        return
    for run in runs:
        c1, c2 = st.columns([4, 1])
        c1.markdown(
            f"`{run['analyzed_at'][:16]}`　**{run['race_name'] or run['race_id']}**　"
            f"{run['track_type'] or ''}{run['race_distance'] or '?'}m　"
            f"{run['horse_count']}頭（成功{run['success_count']}）  \n"
            f"race_id: `{run['race_id']}`")
        if c2.button("開く", key=f"hist_{run['id']}", use_container_width=True):
            payload = repo.load_run_payload(run["id"])
            if payload:
                st.session_state["mylogic_result_dict"] = payload
                st.session_state.pop("mylogic_result", None)
                st.rerun()
            else:
                st.error("履歴の読み込みに失敗しました")


def _result_from_dict(d: dict) -> RaceAnalysisResult:
    """保存payload(dict)からdataclassを復元する。"""
    from .models import (CandidateRecord, HorseAnalysisResult, HorseEntry,
                         RaceInfo)
    race_d = dict(d["race"])
    entries = [HorseEntry(**e) for e in race_d.pop("entries", [])]
    race = RaceInfo(**race_d, entries=entries)
    horses = []
    for hd in d["horses"]:
        hd = dict(hd)
        entry = HorseEntry(**hd.pop("entry"))
        sel = hd.pop("selected", None)
        rejected = [CandidateRecord(**c) for c in hd.pop("rejected_candidates", [])]
        horses.append(HorseAnalysisResult(
            entry=entry,
            selected=CandidateRecord(**sel) if sel else None,
            rejected_candidates=rejected, **hd))
    d2 = {k: v for k, v in d.items() if k not in ("race", "horses")}
    return RaceAnalysisResult(race=race, horses=horses, **d2)


# ─── メインセクション ────────────────────────────────────────
def render_mylogic_section() -> None:
    """Myロジック分析セクション全体（voice_bet_app.py末尾から呼ばれる）。"""
    st.divider()
    st.markdown("### 🧮 Myロジックで分析")
    st.caption("中央＝netkeibaの馬メモの独自タイム、地方＝戦績表（タイム−上り3F、"
               f"馬場指数）で出走馬をランキングします（ロジック v{LOGIC_VERSION}）")

    current_rid = st.session_state.get("race_id", "")
    current_label = st.session_state.get("race_label", "")

    # 対象race_idの決定: 直接入力とレース認識の新しい方を優先
    manual = st.text_input(
        "race_id直接入力（過去レース・地方も分析可）",
        placeholder="例：202603020611（12桁）", key="mylogic_manual_rid")
    manual_clean = (manual or "").strip()

    target_rid = ""
    target_desc = ""
    if manual_clean:
        if re.fullmatch(r"\d{12}", manual_clean):
            target_rid = manual_clean
            target_desc = f"直接入力: {manual_clean}"
        else:
            st.error("race_idは12桁の数字で入力してください（例: 202603020611）")
    elif current_rid and re.fullmatch(r"\d{12}", str(current_rid)):
        target_rid = str(current_rid)
        target_desc = f"認識中のレース: {current_label}"

    if target_rid:
        kind = "地方" if is_nar_race_id(target_rid) else "中央"
        st.markdown(f"**分析対象race_id：`{target_rid}`**（{target_desc}／{kind}）")

    c1, c2 = st.columns(2)
    run_clicked = c1.button("🧮 分析を実行", type="primary",
                            use_container_width=True,
                            disabled=not target_rid, key="mylogic_run")
    refresh_clicked = c2.button("🔄 最新情報で再分析",
                                use_container_width=True,
                                disabled=not target_rid,
                                key="mylogic_refresh")

    autorun = st.session_state.pop("mylogic_autorun", False)
    if autorun and not target_rid and current_rid:
        target_rid = str(current_rid)

    if (run_clicked or refresh_clicked or autorun) and target_rid:
        result = _run_analysis(target_rid, use_cache=not refresh_clicked)
        if result:
            st.session_state["mylogic_result"] = result
            st.session_state.pop("mylogic_result_dict", None)

    # 結果表示（直近の実行結果 or 履歴から復元）
    result = st.session_state.get("mylogic_result")
    if result is None and st.session_state.get("mylogic_result_dict"):
        try:
            result = _result_from_dict(st.session_state["mylogic_result_dict"])
        except (TypeError, KeyError) as e:
            logger.error("履歴payloadの復元失敗: %s", e)
            st.error("履歴データの形式が古いため表示できません")
            st.session_state.pop("mylogic_result_dict", None)
            result = None
    if result:
        _render_result(result)

    with st.expander("🗂 分析履歴"):
        _render_history()

    _render_batch_section()


# ─── 開催日まるごと一括集計 → Googleスプレッドシート ─────────
def _render_batch_section() -> None:
    st.divider()
    st.markdown("### 📅 開催日まるごと集計 → Googleスプレッドシート")
    st.caption("開催日を選ぶと全レースをMyロジックで一括分析し、"
               "新規スプレッドシートに競馬場ごとのタブで書き出します")

    repo = Repository()
    client = NetkeibaClient(repo)

    # ── 中央ブロック ──
    st.markdown("**中央競馬**（1日＝全場・全レース）")
    try:
        schedule = get_jra_schedule(client, repo)
    except (NetkeibaError, BlockedError) as e:
        st.caption(f"開催日を取得できませんでした: {e}")
        schedule = []
    if schedule:
        cols = st.columns(min(max(len(schedule), 1), 4))
        for i, day in enumerate(schedule):
            n_races = sum(len(v) for v in day["venues"].values())
            if cols[i % 4].button(f"{day['label']}\n{n_races}R",
                                  key=f"batch_jra_{day['date']}",
                                  use_container_width=True):
                st.session_state["batch_sel"] = {
                    "kind": "jra", "date": day["date"], "tab_prefix": day["label"],
                    "label": f"中央 {day['label']}", "venues": day["venues"]}
                st.rerun()
    else:
        st.caption("直近の中央開催が見つかりません")

    # ── 地方ブロック ──
    st.markdown("**地方競馬**（日付＋競馬場を選択）")
    import datetime as _dt
    nar_dates = [(_dt.date.today() + _dt.timedelta(days=i)) for i in range(3)]
    _wd = ["月", "火", "水", "木", "金", "土", "日"]
    date_labels = [f"{d.month}/{d.day}({_wd[d.weekday()]})" for d in nar_dates]
    sel_idx = st.selectbox("開催日", range(len(nar_dates)),
                           format_func=lambda i: date_labels[i],
                           key="batch_nar_date")
    nar_date = nar_dates[sel_idx].strftime("%Y%m%d")
    try:
        venues = get_nar_venues(client, repo, nar_date)
    except (NetkeibaError, BlockedError) as e:
        st.caption(f"開催場を取得できませんでした: {e}")
        venues = []
    if venues:
        cols = st.columns(min(max(len(venues), 1), 4))
        for i, (code, name) in enumerate(venues):
            if cols[i % 4].button(name, key=f"batch_nar_{nar_date}_{code}",
                                  use_container_width=True):
                st.session_state["batch_sel"] = {
                    "kind": "nar", "date": nar_date,
                    "tab_prefix": date_labels[sel_idx],
                    "label": f"{date_labels[sel_idx]} {name}",
                    "venues": {name: nar_race_ids(nar_date, code)}}
                st.rerun()
    else:
        st.caption("この日の地方開催が見つかりません")

    # ── 選択後の確認・実行 ──
    sel = st.session_state.get("batch_sel")
    if not sel:
        return
    total = sum(len(v) for v in sel["venues"].values())
    st.info(f"**対象: {sel['label']}** — {len(sel['venues'])}場 最大{total}レース\n\n"
            "初回は1レースあたり1〜2分かかります（分析済みキャッシュがあれば高速）。"
            "実行中はこのタブを閉じないでください。")
    ok, missing = sheets_configured()
    if not ok:
        st.error(f"Googleスプレッドシート連携が未設定です（{missing}）。"
                 "下の手順で設定してから実行してください。")
        with st.expander("📗 初回設定の手順", expanded=True):
            st.markdown(setup_guide())
    c1, c2 = st.columns(2)
    run = c1.button("🚀 集計開始", type="primary", disabled=not ok,
                    use_container_width=True, key="batch_run")
    if c2.button("キャンセル", use_container_width=True, key="batch_cancel"):
        st.session_state.pop("batch_sel", None)
        st.rerun()
    if run:
        _run_batch(sel)


def _run_batch(sel: dict) -> None:
    import datetime as _dt

    repo = Repository()
    client = NetkeibaClient(repo)
    status_box = st.status(f"{sel['label']} を一括集計中...", expanded=True)
    bar = status_box.progress(0.0)
    msg = status_box.empty()

    msg.write("netkeibaのログイン状態を確認中...")
    if not client.ensure_login():
        status_box.update(label="netkeibaログイン失敗", state="error")
        st.error("netkeibaへログインできませんでした。単発の分析を一度実行して"
                 "ログイン状態を確認してください。")
        return

    total = sum(len(v) for v in sel["venues"].values())
    done = 0
    venue_blocks: dict[str, list[list[str]]] = {}
    errors: list[str] = []
    ok_races = 0
    aborted = False
    for venue, rids in sel["venues"].items():
        rows_all: list[list[str]] = []
        for rid in rids:
            done += 1
            rno = int(rid[10:12])
            msg.write(f"{venue}{rno}R を分析中（{done}/{total}）")
            bar.progress(done / max(total, 1))
            try:
                result = analyze_race(repo, client, rid)
                rows_all.extend(result_to_rows(result))
                rows_all.append([""])
                ok_races += 1
            except BlockedError as e:
                errors.append(f"{venue}{rno}R以降: アクセス制限を検知して中断（{e}）")
                aborted = True
                break
            except NetkeibaError as e:
                # NARの規則生成race_idは存在しないRを含むため静かにスキップ
                if sel["kind"] == "nar" and "出走馬" in str(e):
                    continue
                errors.append(f"{venue}{rno}R: {e}")
        if rows_all:
            venue_blocks[venue] = rows_all
        if aborted:
            break

    if not venue_blocks:
        status_box.update(label="集計失敗", state="error")
        st.error("集計できたレースがありませんでした")
        for e in errors[:10]:
            st.caption(f"・{e}")
        return

    msg.write("Googleスプレッドシートへ書き込み中...")
    tab_prefix = sel.get("tab_prefix") or sel["label"]
    try:
        url = export_to_spreadsheet(tab_prefix, venue_blocks)
    except Exception as e:
        logger.exception("スプレッドシート出力失敗")
        status_box.update(label="スプレッドシート出力失敗", state="error")
        st.error(f"スプレッドシートへの書き込みに失敗しました: {type(e).__name__}")
        with st.expander("📗 設定手順の確認"):
            st.markdown(setup_guide())
        return

    status_box.update(
        label=f"集計完了 — {ok_races}レースを書き出しました", state="complete",
        expanded=False)
    tabs = "、".join(f"{tab_prefix} {v}" for v in venue_blocks)
    st.success(f"✅ 書き込み完了（タブ: {tabs}）　[スプレッドシートを開く]({url})")
    if errors:
        with st.expander(f"⚠️ 取得できなかったレース（{len(errors)}件）"):
            for e in errors:
                st.caption(f"・{e}")
    st.session_state.pop("batch_sel", None)
