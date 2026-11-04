"""現在日時の取得を一箇所に集約する(テストで差し替えやすくするため)。"""
from datetime import date, datetime


def today() -> date:
    return date.today()


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")
