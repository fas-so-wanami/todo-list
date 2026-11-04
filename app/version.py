"""アプリのバージョン(プログラムファイルの内容から計算する識別子)。

- 起動時に「すでに動いているサーバーが最新か」を確かめるのに使う(app/launcher.py)。
- CSS の URL に付け、更新したときにブラウザが古い CSS を使い続けないようにする。
ファイルを1文字でも変えると値が変わるので、手で番号を増やす必要はない。
"""
import hashlib
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
VERSIONED_SUFFIXES = {".py", ".html", ".css", ".js"}


def compute_app_version() -> str:
    digest = hashlib.sha1()
    for path in sorted(APP_DIR.rglob("*")):
        if path.suffix not in VERSIONED_SUFFIXES or "__pycache__" in path.parts:
            continue
        digest.update(path.relative_to(APP_DIR).as_posix().encode("utf-8"))
        digest.update(path.read_bytes())
    return digest.hexdigest()[:12]
