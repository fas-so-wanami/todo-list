"""状態の目印と、完了した操作だけに付ける演出の印を確かめる。"""
from datetime import date
from pathlib import Path
import re

import pytest
from fastapi.testclient import TestClient

from app import daily, db as database, reports, tasks


def test_status_labels_are_shared_by_board_mini_and_report(client: TestClient, db_path: Path) -> None:
    connection = database.connect(db_path)
    try:
        for status in tasks.STATUSES:
            task_id = tasks.create_task(connection, 1, f"状態の確認・{status}")
            tasks.set_status(connection, task_id, status)
            daily.add_to_plan(connection, task_id, date(2026, 10, 29))
            reports.toggle_item(connection, 1, task_id, date(2026, 10, 29))
        connection.commit()
    finally:
        connection.close()
    for url in ("/", "/mini", "/report"):
        html = client.get(url).text
        for status, label in (("todo", "未着手"), ("doing", "進行中"), ("done", "完了")):
            assert f'data-task-status="{status}">{label}' in html


@pytest.mark.parametrize("url", ["/", "/mini", "/report", "/archive"])
def test_completion_preference_is_outside_replaced_content(client: TestClient, url: str) -> None:
    html = client.get(url).text
    header = html[html.index("<header"):html.index("</header>")]
    assert "data-completion-effects-toggle" in header
    assert "data-completion-effects-state" in header
    assert "todo.completionEffects" in html
    assert 'window.addEventListener("storage"' in html
    assert "data-completion-effects-toggle" not in client.get("/content").text


@pytest.mark.parametrize("view", ["", "mini"])
def test_only_new_successful_completion_gets_feedback(client: TestClient, db_path: Path, view: str) -> None:
    connection = database.connect(db_path)
    try:
        parent = tasks.create_task(connection, 1, "完了を選ぶ親")
        child = tasks.create_task(connection, 1, "完了する子", parent_id=parent)
        daily.add_to_plan(connection, parent, date(2026, 10, 29))
        connection.commit()
    finally:
        connection.close()
    suffix = f"?view={view}" if view else ""
    highlight_pattern = r'<li\b[^>]*class="[^"]*\bjust-completed\b[^"]*"[^>]*>'
    response = client.post(f"/tasks/{parent}/status/done{suffix}")
    assert "残っているため完了にできません" in response.text
    assert "just-completed" not in response.text
    response = client.post(f"/tasks/{child}/status/done{suffix}")
    highlighted = re.findall(highlight_pattern, response.text)
    assert len(highlighted) == 1
    assert (
        f'data-view-transition-name="mini-task-{child}"'
        if view else f'id="task-{child}"'
    ) in highlighted[0]
    assert "HX-Trigger-After-Settle" not in response.headers
    assert "親も完了にする" in response.text
    for url in (f"/tasks/{child}/status/done{suffix}", f"/tasks/999999/status/done{suffix}"):
        assert "just-completed" not in client.post(url).text
    assert not re.search(highlight_pattern, client.get("/mini" if view else "/").text)
    response = client.post(f"/tasks/{parent}/status/done{suffix}")
    highlighted = re.findall(highlight_pattern, response.text)
    assert len(highlighted) == 1
    assert (
        f'data-view-transition-name="mini-task-{parent}"'
        if view else f'id="task-{parent}"'
    ) in highlighted[0]
    assert "just-completed" not in client.post(f"/tasks/{parent}/status/doing{suffix}").text
