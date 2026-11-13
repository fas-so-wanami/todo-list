"""ToDo の起動プログラム(start.bat から呼ばれる)。

1. すでにポート 8765 で ToDo が動いていないか確かめる。
   - 同じバージョンが動いていれば、ブラウザで開くだけにする。
   - 古いバージョンが動いていれば、前の黒いウィンドウを閉じるよう案内して終わる。
   - ToDo 以外のプログラムが使っていれば、その旨を案内して終わる。
2. 空いていればサーバーを起動し、準備ができてからブラウザを開く。
"""
import json
import os
import socket
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

import uvicorn

from app.version import compute_app_version

HOST = "127.0.0.1"  # この PC からだけ開ける(同じネットワークの他の PC からは見えない)
PORT = 8765
APP_URL = f"http://{HOST}:{PORT}/"
STARTUP_TIMEOUT_SECONDS = 60

PORT_FREE = "free"
SAME_VERSION = "same"
OLD_VERSION = "old"
OTHER_PROGRAM = "other"
LOCAL_AUTH_SETTINGS_PATH = Path(__file__).resolve().parent.parent / "data" / "local_auth.json"


def is_port_open() -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(1)
        return probe.connect_ex((HOST, PORT)) == 0


VERSION_UNKNOWN_OLD = "unknown-old"  # バージョンを返す仕組みが入る前の ToDo


# 自分の PC(127.0.0.1)への確認は、会社のプロキシを通さない。プロキシを通すと届かず、誤って「別のプログラム」と判定するため
_LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get_json(path: str) -> dict:
    with _LOCAL_OPENER.open(f"{APP_URL}{path}", timeout=3) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("ToDo の応答ではありません")
    return data


def fetch_running_version() -> str | None:
    """動いている ToDo のバージョンを返す。ToDo 以外が応答した場合は None。"""
    try:
        return _get_json("api/app-version").get("version")
    except urllib.error.HTTPError as error:
        if error.code != 404:
            return None
    except Exception:  # どんな応答でも、ここで止まらず「別のプログラム」として案内を出す
        return None
    # バージョンの API が無くても、以前からある API に応答すれば古い ToDo と判断する
    try:
        _get_json("api/data-version")
        return VERSION_UNKNOWN_OLD
    except Exception:
        return None


def check_port_state(current_version: str) -> str:
    if not is_port_open():
        return PORT_FREE
    running_version = fetch_running_version()
    if running_version is None:
        return OTHER_PROGRAM
    return SAME_VERSION if running_version == current_version else OLD_VERSION


def load_local_auth_settings() -> None:
    """Git管理外のローカル設定から起動モードとデータ所有者を読み込む。"""
    if not LOCAL_AUTH_SETTINGS_PATH.exists():
        return
    try:
        with LOCAL_AUTH_SETTINGS_PATH.open(encoding="utf-8") as settings_file:
            settings = json.load(settings_file)
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"{LOCAL_AUTH_SETTINGS_PATH}を読み込めません: {error}") from error
    if not isinstance(settings, dict):
        raise ValueError(f"{LOCAL_AUTH_SETTINGS_PATH}はJSONの項目一覧にしてください。")
    for key in ("TODO_AUTH_MODE", "TODO_DATA_OWNER_EMAIL"):
        value = settings.get(key)
        if isinstance(value, str) and value.strip():
            os.environ.setdefault(key, value.strip())


def open_browser_when_ready(server: uvicorn.Server) -> None:
    deadline = time.monotonic() + STARTUP_TIMEOUT_SECONDS
    while not server.started:
        if time.monotonic() > deadline or server.should_exit:
            return
        time.sleep(0.2)
    print(f"ToDo を起動しました: {APP_URL}")
    print("このウィンドウは閉じないでください(閉じると ToDo が終了します)。")
    webbrowser.open(APP_URL)


def run_server() -> None:
    config = uvicorn.Config("app.main:app", host=HOST, port=PORT, log_level="warning")
    server = uvicorn.Server(config)
    threading.Thread(target=open_browser_when_ready, args=(server,), daemon=True).start()
    print("ToDo を起動しています。少しお待ちください…")
    server.run()


def main() -> int:
    try:
        load_local_auth_settings()
    except ValueError as error:
        print(f"[エラー] {error}")
        return 1
    state = check_port_state(compute_app_version())
    if state == SAME_VERSION:
        print("ToDo はすでに起動しています。ブラウザで開きます。")
        webbrowser.open(APP_URL)
        return 0
    if state == OLD_VERSION:
        print("[注意] 更新前の古い ToDo が動いています。")
        print("前に開いた ToDo の黒いウィンドウを閉じてから、もう一度 start.bat を実行してください。")
        print("見つからない場合は、PC を再起動すると確実です。")
        return 1
    if state == OTHER_PROGRAM:
        print(f"[エラー] ポート {PORT} を ToDo 以外のプログラムが使っているため起動できません。")
        print("PC を再起動してから、もう一度 start.bat を実行してください。")
        return 1
    run_server()
    return 0


if __name__ == "__main__":
    sys.exit(main())
