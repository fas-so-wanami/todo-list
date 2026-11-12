"""通常・ミニ・日報・アーカイブで同じ件数と状態を表示することを確かめる。"""
from datetime import date
from pathlib import Path
import re

from fastapi.testclient import TestClient

from app import daily, db as database, reports, tasks


def seed_family(db_path: Path) -> tuple[int, int, int]:
    """画面確認用の親子を一時DBに保存する。"""
    connection = database.connect(db_path)
    try:
        parent = tasks.create_task(connection, 1, "親の資料")
        first = tasks.create_task(connection, 1, "子の下書き", parent_id=parent)
        second = tasks.create_task(connection, 1, "子の確認", parent_id=parent)
        tasks.set_status(connection, first, "done")
        daily.add_to_plan(connection, parent, date(2026, 10, 29))
        reports.toggle_item(connection, 1, parent, date(2026, 10, 29))
        connection.commit()
        return parent, first, second
    finally:
        connection.close()


def test_board_mini_report_share_counts_without_percent(client: TestClient, db_path: Path) -> None:
    parent, first, second = seed_family(db_path)
    for url in ("/", "/mini", "/report"):
        response = client.get(url)
        assert response.status_code == 200
        assert "1/2件完了" in response.text
        assert 'max="2"' in response.text
        assert "/progress/" not in response.text
        assert "進捗(%)" not in response.text
        assert "25%" not in response.text
        assert "50%" not in response.text
        assert "75%" not in response.text
    html = client.get("/").text
    assert f"/tasks/{parent}/status/" not in html
    assert f"/tasks/{second}/status/doing" in html
    assert f"/tasks/{first}/status/doing" in html
    assert 'class="task-state-action start-task"' in html
    assert "作業を始める" in html
    mini_html = client.get("/mini").text
    assert 'class="task-state-action start-task"' in mini_html
    assert "作業を始める" in mini_html


def test_mini_action_returns_mini_and_completes_parent(client: TestClient, db_path: Path) -> None:
    parent, first, second = seed_family(db_path)
    response = client.post(f"/tasks/{second}/status/done?view=mini")
    assert response.status_code == 200
    assert 'class="mini-list"' in response.text
    assert "2/2件完了" in response.text
    assert "/status/doing?view=mini" in response.text
    assert "just-completed" in response.text
    assert 'class="task-columns"' not in response.text
    response = client.post(f"/tasks/{first}/status/doing?view=mini")
    assert "1/2件完了" in response.text
    connection = database.connect(db_path)
    try:
        assert tasks.get_task(connection, parent)["status"] == "doing"
    finally:
        connection.close()


def test_board_defaults_to_due_date_order_in_both_sections(db) -> None:
    today = date(2026, 10, 29)
    today_later = tasks.create_task(db, 1, "今日・期限後", due_date="2026-10-31", importance=1)
    today_sooner = tasks.create_task(db, 1, "今日・期限前", due_date="2026-10-30", importance=3)
    today_without_due = tasks.create_task(db, 1, "今日・期限なし")
    later_sooner = tasks.create_task(db, 1, "後日・期限前", due_date="2026-11-01")
    later_later = tasks.create_task(db, 1, "後日・期限後", due_date="2026-11-03")
    later_without_due = tasks.create_task(db, 1, "後日・期限なし")
    daily.add_to_plan(db, today_later, today)
    daily.add_to_plan(db, today_sooner, today)
    daily.add_to_plan(db, today_without_due, today)

    board = daily.build_board(db, 1, set())

    assert [task["title"] for task in board["today"]] == ["今日・期限前", "今日・期限後", "今日・期限なし"]
    assert [task["title"] for task in board["others"]] == ["後日・期限前", "後日・期限後", "後日・期限なし"]


def test_quick_add_uses_today_when_due_date_is_blank(client: TestClient, db_path: Path) -> None:
    client.post("/tasks", data={"title": "期限欄を空にして追加", "due_date": ""})
    connection = database.connect(db_path)
    try:
        task = connection.execute(
            "SELECT due_date, start_date FROM tasks WHERE user_id = ? AND title = ?",
            (1, "期限欄を空にして追加"),
        ).fetchone()
        assert task["due_date"] == "2026-10-29"
        assert task["start_date"] == "2026-10-29"
    finally:
        connection.close()


def test_start_date_defaults_on_add_and_can_be_cleared_on_edit(client: TestClient, db_path: Path) -> None:
    """新規追加の空欄は当日にし、既存タスクの編集では空欄に戻せる。"""
    response = client.post("/tasks", data={
        "title": "開始日を設定", "due_date": "2026-11-10", "start_date": "2026-11-03",
    })
    assert response.status_code == 200
    board_html = client.get("/").text
    assert board_html.count('name="start_date"') >= 2
    assert board_html.index('name="due_date"') < board_html.index('name="start_date"')
    connection = database.connect(db_path)
    try:
        task = connection.execute(
            "SELECT id, start_date FROM tasks WHERE user_id = ? AND title = ?", (1, "開始日を設定"),
        ).fetchone()
        assert task["start_date"] == "2026-11-03"

        response = client.post(f"/tasks/{task['id']}/edit", data={
            "title": "開始日を編集", "due_date": "2026-11-10", "start_date": "2026-11-04",
        })
        assert response.status_code == 200
        edited = connection.execute(
            "SELECT start_date FROM tasks WHERE user_id = ? AND id = ?", (1, task["id"]),
        ).fetchone()
        assert edited["start_date"] == "2026-11-04"

        client.post(f"/tasks/{task['id']}/edit", data={
            "title": "開始日を編集", "due_date": "2026-11-10", "start_date": "",
        })
        cleared = connection.execute(
            "SELECT start_date FROM tasks WHERE user_id = ? AND id = ?", (1, task["id"]),
        ).fetchone()
        assert cleared["start_date"] is None

        client.post("/tasks", data={
            "title": "開始日なし", "due_date": "2026-11-10", "start_date": "",
        })
        blank = connection.execute(
            "SELECT start_date FROM tasks WHERE user_id = ? AND title = ?", (1, "開始日なし"),
        ).fetchone()
        assert blank["start_date"] == "2026-10-29"

        client.post("/tasks", data={
            "title": "期限が過去で開始日なし", "due_date": "2026-10-28", "start_date": "",
        })
        past_deadline = connection.execute(
            "SELECT start_date FROM tasks WHERE user_id = ? AND title = ?", (1, "期限が過去で開始日なし"),
        ).fetchone()
        assert past_deadline["start_date"] == "2026-10-28"
    finally:
        connection.close()


def test_start_date_after_due_date_is_rejected_without_saving(client: TestClient, db_path: Path) -> None:
    connection = database.connect(db_path)
    try:
        task_id = tasks.create_task(connection, 1, "変更前", due_date="2026-11-05")
        connection.commit()
    finally:
        connection.close()

    response = client.post(f"/tasks/{task_id}/edit", data={
        "title": "保存してはいけない", "due_date": "2026-11-05", "start_date": "2026-11-06",
    })

    assert response.status_code == 200
    assert "開始日は期限以前の日付にしてください" in response.text
    assert 'value="2026-11-06"' in response.text
    connection = database.connect(db_path)
    try:
        task = connection.execute(
            "SELECT title, due_date, start_date FROM tasks WHERE user_id = ? AND id = ?", (1, task_id),
        ).fetchone()
        assert tuple(task) == ("変更前", "2026-11-05", None)
    finally:
        connection.close()


def test_done_today_section_open_state_is_restored_after_content_swap(client: TestClient, db_path: Path) -> None:
    """通常・ミニ両方で、一覧更新後に完了欄の開閉を復元する処理が読み込まれる。"""
    seed_family(db_path)
    for url, section_id in (
        ("/", "done-today-section"),
        ("/mini", "mini-done-today-section"),
    ):
        html = client.get(url).text
        assert f'id="{section_id}"' in html
        assert f'todo.doneTodayOpen." + (document.body.classList.contains("mini-mode") ? "mini" : "board")' in html
        assert 'document.body.addEventListener("htmx:afterSwap", restoreDoneTodaySection)' in html


def test_parent_has_child_visibility_controls_and_readable_levels(client: TestClient, db_path: Path) -> None:
    """親子の表示切り替えが両画面にあり、重要度を文字で読めることを確かめる。"""
    parent, first, second = seed_family(db_path)
    for url in ("/", "/mini"):
        html = client.get(url).text
        assert f'data-child-controls="{parent}"' in html
        assert f'data-parent-id="{parent}"' in html
        assert "子タスク 2件・完了 1件" in html
        for mode in ("all", "done", "none"):
            assert f'data-child-mode="{mode}"' in html
        assert "data-child-hidden-count" in html
        assert "data-child-alerts" in html
    html = client.get("/").text
    assert "重要：中" in html
    assert "緊急：中" in html
    assert 'class="bars"' not in html
    assert "取り組む日" in html
    assert "<span>移動</span>" not in html
    assert "data-child-controls" not in client.get("/report").text
    assert "data-child-controls" not in client.get("/archive").text


def test_child_visibility_script_is_served_to_both_views(client: TestClient, db_path: Path) -> None:
    """通常とミニで同じ開閉処理を使い、更新後も新しいコードが届くことを確かめる。"""
    seed_family(db_path)
    for url in ("/", "/mini"):
        html = client.get(url).text
        scripts = re.findall(r'<script src="(/static/task-visibility.js\?v=[^"]+)"></script>', html)
        assert len(scripts) == 1
        response = client.get(scripts[0])
        assert response.status_code == 200
        assert "javascript" in response.headers["content-type"]
        sort_scripts = re.findall(r'<script src="(/static/task-sort.js\?v=[^"]+)"></script>', html)
        assert len(sort_scripts) == 1
        sort_response = client.get(sort_scripts[0])
        assert sort_response.status_code == 200
        assert 'new Intl.Collator("ja"' in sort_response.text
        assert 'todo.taskSort' in sort_response.text
    board_html = client.get("/").text
    for field in ("progress", "title", "due", "importance", "urgency"):
        assert f'data-task-sort="{field}"' in board_html


def test_hidden_child_attention_and_level_labels_are_available(client: TestClient, db_path: Path) -> None:
    """収納した未完了の子の注意件数を計算でき、札の高低が読めることを確かめる。"""
    parent, first, second = seed_family(db_path)
    client.post(f"/tasks/{second}/edit", data={
        "title": "期限を過ぎた子", "due_date": "2026-10-28", "importance": "3", "urgency": "1",
    })
    client.post(f"/tasks/{second}/blocker")
    for url in ("/", "/mini"):
        html = client.get(url).text
        child = re.search(rf'<li[^>]*data-task-id="{second}"[^>]*>', html)
        assert child is not None
        assert f'data-parent-id="{parent}"' in child.group()
        assert 'data-child-overdue="true"' in child.group()
        assert "blocked" in child.group()
        assert "子タスク 2件・完了 1件" in html
    html = client.get("/").text
    assert "重要：高" in html
    assert "緊急：低" in html
    client.post(f"/tasks/{second}/status/done")
    html = client.get("/").text
    child = re.search(rf'<li[^>]*data-task-id="{second}"[^>]*>', html)
    assert child is not None
    assert 'data-child-overdue="false"' in child.group()
    assert "子タスク 2件・完了 2件" in html


def test_invalid_and_parent_state_changes_show_explanations(client: TestClient, db_path: Path) -> None:
    parent, first, second = seed_family(db_path)
    assert "子タスク" in client.post(f"/tasks/{parent}/status/done").text
    assert "自動で決まります" in client.post(f"/tasks/{parent}/status/todo").text
    assert "指定された状態には変更できません" in client.post(f"/tasks/{second}/status/invalid").text
    assert "操作できるタスクが見つかりません" in client.post("/tasks/999999/status/done").text
    assert "％での進捗操作は終了" in client.post(f"/tasks/{second}/progress/100").text
    connection = database.connect(db_path)
    try:
        assert tasks.get_task(connection, first)["status"] == "done"
        assert tasks.get_task(connection, second)["status"] == "todo"
    finally:
        connection.close()


def test_other_users_task_cannot_be_operated(client: TestClient, db_path: Path) -> None:
    connection = database.connect(db_path)
    try:
        connection.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "別の利用者"))
        other_task = tasks.create_task(connection, 2, "非公開のタスク")
        connection.commit()
        response = client.post(f"/tasks/{other_task}/status/done")
        assert "非公開のタスク" not in response.text
        assert tasks.get_task(connection, other_task)["status"] == "todo"
    finally:
        connection.close()


def test_archive_has_count_and_can_reopen_a_child(client: TestClient, db_path: Path) -> None:
    parent, first, second = seed_family(db_path)
    client.post(f"/tasks/{second}/status/done")
    client.post(f"/tasks/{parent}/status/done")
    connection = database.connect(db_path)
    try:
        connection.execute(
            "UPDATE tasks SET completed_at = ? WHERE user_id = ? AND id IN (?, ?, ?)",
            ("2026-10-28T15:00:00", 1, parent, first, second),
        )
        connection.commit()
        response = client.get("/archive")
        assert "2/2件完了" in response.text
        assert f"/tasks/{first}/status/doing?view=archive" in response.text
        assert f"/tasks/{parent}/status/doing?view=archive" in response.text
        response = client.post(f"/tasks/{first}/status/doing?view=archive")
        assert response.headers["HX-Redirect"] == "/"
        assert tasks.get_task(connection, parent)["status"] == "doing"
        assert "親の資料" not in client.get("/archive").text
    finally:
        connection.close()


def test_add_and_delete_child_update_count_and_state(client: TestClient, db_path: Path) -> None:
    parent, first, second = seed_family(db_path)
    response = client.post(f"/tasks/{second}/delete")
    assert "1/1件完了" in response.text
    response = client.post(f"/tasks/{parent}/subtasks", data={"title": "追加の子"})
    assert "1/2件完了" in response.text
    assert "追加の子" in response.text


def test_existing_local_request_protections_are_preserved(client: TestClient) -> None:
    assert client.get("/", headers={"host": "not-local.example"}).status_code == 400
    assert client.post("/tasks", data={"title": "拒否する"}, headers={"origin": "https://not-local.example"}).status_code == 403
