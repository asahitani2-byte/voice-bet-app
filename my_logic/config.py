"""認証情報・設定の取得。

優先順位: st.secrets → OS環境変数 → プロジェクトルートの .env
（依存を増やさないため .env は自前の最小ローダーで読む）
"""
from __future__ import annotations

import os
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_ENV_LOADED = False


def _load_dotenv_once() -> None:
    """プロジェクトルートの .env を一度だけ読み込み、未設定の環境変数へ反映。"""
    global _ENV_LOADED
    if _ENV_LOADED:
        return
    _ENV_LOADED = True
    env_path = _ROOT / ".env"
    if not env_path.exists():
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value
    except OSError:
        pass


def get_secret(name: str, default: str = "") -> str:
    """st.secrets → 環境変数 → .env の順で設定値を返す。"""
    # 1. Streamlit Secrets（secrets.toml が無い環境では例外になるため握りつぶす）
    try:
        import streamlit as st
        if name in st.secrets:
            return str(st.secrets[name])
    except Exception:
        pass
    # 2. 環境変数（.env は未設定キーのみ補完）
    _load_dotenv_once()
    return os.environ.get(name, default)


def data_dir() -> Path:
    """SQLite・ログなどの保存先（リポジトリ内、gitignore対象）。"""
    d = _ROOT / "data"
    d.mkdir(exist_ok=True)
    return d


def browser_launch_configs() -> list[dict]:
    """Playwright chromium の起動構成を優先順に返す。

    1. 通常構成（ローカルはこれで動く）
    2. 省リソース構成（クラウドのメモリ制限・依存不足でのクラッシュ対策）
    3. システム版Chromium（packages.txt の chromium。依存ライブラリ完備）
    """
    base = ["--no-sandbox", "--disable-dev-shm-usage"]
    hardened = base + ["--disable-gpu", "--no-zygote", "--single-process",
                       "--disable-extensions", "--disable-background-networking"]
    configs: list[dict] = [{"args": base}, {"args": hardened}]
    for path in ("/usr/bin/chromium", "/usr/bin/chromium-browser"):
        if os.path.exists(path):
            configs.append({"args": hardened, "executable_path": path})
    return configs


_PW_INSTALL_TRIED = False


def try_install_playwright_chromium(error_text: str = "") -> bool:
    """Playwrightのchromium未導入環境（Streamlit Cloud等）で一度だけ導入を試みる。

    ローカルでは既に導入済みのため通常呼ばれない。導入に成功したらTrue。
    """
    global _PW_INSTALL_TRIED
    if _PW_INSTALL_TRIED:
        return False
    if error_text and "Executable doesn't exist" not in error_text \
            and "playwright install" not in error_text:
        return False
    _PW_INSTALL_TRIED = True
    import subprocess
    import sys
    try:
        r = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            capture_output=True, timeout=600)
        return r.returncode == 0
    except Exception:
        return False


def cookie_store_path() -> Path:
    """CookieキャッシュはリポジトリEXTERNAL（~/.winvoice/）に置く。"""
    d = Path.home() / ".winvoice"
    d.mkdir(exist_ok=True)
    return d / "nk_cookies.json"
