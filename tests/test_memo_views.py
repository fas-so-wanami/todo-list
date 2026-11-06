"""メモの札が、タスクの状態や表示先によらずそろうことを確かめる。"""
from datetime import date
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import daily, db as database, reports, tasks


class MemoChipParser(HTMLParser):
    """札だけを取り出し、編集欄のメモと取り違えずに表示を確かめる。"""

    def __init__(self) -> None:
        super().__init__()
        self.memos: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        if tag == "span" and "memo-chip" in (attributes.get("class") or "").split():
            self.memos.append(attributes.get("title") or "")


def memo_chips(html: str) -> list[str]:
    """札に入ったメモを、文字参照を元に戻して返す。"""
    parser = MemoChipParser()
    parser.feed(html)
    return parser.memos


@pytest.mark.parametrize("url", ["/", "/mini", "/content", "/content?view=mini", "/report", "/archive"])
@pytest.mark.parametrize("memo", ["以前からあるメモ", "", " \n\t　", '<script>alert("メモ")</script>\n次の行'])
def test_existing_memo_chips_match_across_views(
    client: TestClient, db_path: Path, url: str, memo: str,
) -> None:
    """既存の親子・単独・当日完了タスクを、各表示先で同じ条件で判定する。"""
    connection = database.connect(db_path)
    try:
        parent = tasks.create_task(connection, 1, "親タスク", memo=memo)
        child = tasks.create_task(connection, 1, "子タスク", parent_id=parent, memo=memo)
        standalone = tasks.create_task(connection, 1, "単独タスク", memo=memo)
        task_ids = [parent, child, standalone]
        for task_id in task_ids:
            daily.add_to_plan(connection, task_id, date(2026, 10, 29))
            reports.toggle_item(connection, 1, task_id, date(2026, 10, 29))
        if url == "/archive":
            for task_id in [child, parent, standalone]:
                tasks.set_status(connection, task_id, "done")
                connection.execute(
                    "UPDATE tasks SET completed_at = ? WHERE id = ? AND user_id = ?",
                    ("2026-10-28T14:30:00", task_id, 1),
                )
        else:
            tasks.set_status(connection, child, "done")
            tasks.set_status(connection, standalone, "done")
        connection.commit()
    finally:
        connection.close()

    response = client.get(url)
    assert response.status_code == 200
    expected_count = 5 if url in ("/", "/mini", "/content", "/content?view=mini") else 3
    assert memo_chips(response.text) == ([memo] * expected_count if memo.strip() else [])
    assert '<script>alert("メモ")</script>' not in response.text


@pytest.mark.parametrize("view", ["", "?view=mini"])
def test_memo_chip_follows_saved_changes(client: TestClient, view: str) -> None:
    """追加・編集・削除したメモを、保存直後の差し替えにも反映する。"""
    response = client.post(f"/tasks{view}", data={"title": "札の確認", "memo": "追加時のメモ"})
    assert response.status_code == 200
    assert memo_chips(response.text) == ["追加時のメモ"]
    for memo in ["変更したメモ", "", "再入力したメモ", " \n　"]:
        response = client.post(f"/tasks/1/edit{view}", data={"title": "札の確認", "memo": memo})
        assert response.status_code == 200
        assert memo_chips(response.text) == ([memo.strip()] if memo.strip() else [])
