"""通常・ミニ共通の引き継ぎ処理と、未保存の編集を見分ける印を確認する。"""
from pathlib import Path
import re
import mimetypes

import pytest
from fastapi.testclient import TestClient

from app import clock, daily, db as database, reports, tasks
from app.main import create_app


def test_javascript_type_ignores_windows_file_association(db_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(mimetypes.types_map, ".js", "text/plain")
    with TestClient(create_app(db_path), base_url="http://127.0.0.1") as client:
        response = client.get("/static/window-views.js")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/javascript")


@pytest.mark.parametrize("url", ["/", "/mini", "/report", "/archive"])
def test_views_load_one_versioned_handoff_script(client: TestClient, url: str) -> None:
    html = client.get(url).text
    scripts = re.findall(r'<script src="(/static/window-views.js\?v=[^"]+)"></script>', html)
    assert len(scripts) == 1
    response = client.get(scripts[0])
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "todo-view-probe" in response.text
    assert "todo-view-ready" in response.text
    assert html.index('id="view-handoff"') < html.index("<main>")
    assert 'id="view-handoff"' not in client.get("/content?view=mini").text


def test_rejected_edit_is_marked_as_unsaved_until_saved(client: TestClient, db_path: Path) -> None:
    connection = database.connect(db_path)
    try:
        task_id = tasks.create_task(connection, 1, "保存済みの名前")
        connection.commit()
        response = client.post(f"/tasks/{task_id}/edit", data={
            "title": "保存できなかった名前", "parent_id": str(task_id),
        })
        assert "data-unsaved-draft" in response.text
        assert "保存できなかった名前" in response.text
        assert tasks.get_task(connection, task_id)["title"] == "保存済みの名前"
        response = client.post(f"/tasks/{task_id}/edit", data={"title": "保存できた名前"})
        assert "data-unsaved-draft" not in response.text
        assert tasks.get_task(connection, task_id)["title"] == "保存できた名前"
    finally:
        connection.close()


def test_shared_dependencies_keep_app_databases_separate(tmp_path: Path) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    with TestClient(create_app(first_path), base_url="http://127.0.0.1") as first_client:
        with TestClient(create_app(second_path), base_url="http://127.0.0.1") as second_client:
            first_client.post("/tasks", data={"title": "最初のアプリだけ"})
            second_client.post("/tasks", data={"title": "別のアプリだけ"})
            assert "最初のアプリだけ" in first_client.get("/").text
            assert "別のアプリだけ" not in first_client.get("/").text
            assert "別のアプリだけ" in second_client.get("/").text
            assert "最初のアプリだけ" not in second_client.get("/").text


@pytest.mark.parametrize("url,prefix", [("/", "task"), ("/mini", "mini-task"), ("/report", "report-item")])
def test_transition_names_use_shared_script(client: TestClient, db_path: Path, url: str, prefix: str) -> None:
    connection = database.connect(db_path)
    try:
        task_id = tasks.create_task(connection, 1, "行の移動確認")
        daily.add_to_plan(connection, task_id, clock.today())
        reports.toggle_item(connection, 1, task_id, clock.today())
        connection.commit()
    finally:
        connection.close()
    html = client.get(url).text
    assert f'data-view-transition-name="{prefix}-{task_id}"' in html
    assert 'style="view-transition-name:' not in html
    script_urls = re.findall(r'<script src="(/static/task-visibility.js\?v=[^"]+)"></script>', html)
    assert len(script_urls) == 1
    script = client.get(script_urls[0]).text
    assert 'addEventListener("htmx:afterSwap", applyTransitionNames)' in script
