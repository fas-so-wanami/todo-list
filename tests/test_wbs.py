from datetime import date

from app import clock, db as database, projects, tasks, wbs


def test_week_view_uses_four_weeks_from_monday_and_builds_plan_bars(db, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 29))
    project_id = projects.create_project(db, 1, "週表示案件")
    parent = tasks.create_task(
        db, 1, "親", project_id=project_id, start_date="2026-10-28", due_date="2026-11-10",
    )
    child = tasks.create_task(
        db, 1, "子", parent_id=parent, start_date="2026-11-02", due_date="2026-11-04",
    )

    view = wbs.build_view(db, 1, project_id=project_id, mode="week")

    assert view["week_start"] == "2026-10-26"
    assert [period["label"] for period in view["periods"]] == [
        "10/26 週", "11/2 週", "11/9 週", "11/16 週",
    ]
    assert [period["today_position"] for period in view["periods"]] == [50.0, None, None, None]
    assert view["previous_week"] == "2026-10-19"
    assert view["next_week"] == "2026-11-02"
    assert [task["id"] for task in view["tasks"]] == [parent, child]
    assert view["tasks"][0]["bar_cells"] == ["bar-start", "bar-middle", "bar-end", ""]
    assert view["tasks"][1]["bar_cells"] == ["", "bar-start bar-end", "", ""]


def test_day_view_uses_selected_week_and_keeps_tasks_without_dates(db, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 29))
    project_id = projects.create_project(db, 1, "日表示案件")
    task_id = tasks.create_task(db, 1, "日付なし", project_id=project_id)
    spanning_task_id = tasks.create_task(
        db, 1, "期間外へ続く", project_id=project_id,
        start_date="2026-10-30", due_date="2026-11-20",
    )

    view = wbs.build_view(db, 1, project_id=project_id, mode="day", week="2026-11-04")

    assert len(view["periods"]) == 7
    assert view["week_start"] == "2026-11-02"
    assert view["periods"][0]["label"] == "11/2(月)"
    assert view["periods"][-1]["label"] == "11/8(日)"
    assert all(period["today_position"] is None for period in view["periods"])
    undated_task = next(task for task in view["tasks"] if task["id"] == task_id)
    assert undated_task["schedule_label"] == "判定なし"
    assert undated_task["bar_cells"] == ["", "", "", "", "", "", ""]
    spanning_task = next(task for task in view["tasks"] if task["id"] == spanning_task_id)
    assert spanning_task["bar_cells"] == ["bar-middle"] * 7


def test_schedule_signals_use_each_tasks_own_dates_and_separate_completion(db, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 29))
    project_id = projects.create_project(db, 1, "信号案件")
    overdue = tasks.create_task(
        db, 1, "期限超過", project_id=project_id, start_date="2026-10-20", due_date="2026-10-28",
    )
    start_late = tasks.create_task(
        db, 1, "開始予定超過", project_id=project_id, start_date="2026-10-28", due_date="2026-11-06",
    )
    on_track = tasks.create_task(
        db, 1, "遅れなし", project_id=project_id, start_date="2026-10-29", due_date="2026-11-06",
    )
    completed = tasks.create_task(
        db, 1, "完了", project_id=project_id, start_date="2026-10-20", due_date="2026-10-28",
    )
    tasks.set_status(db, completed, "done")

    view = wbs.build_view(db, 1, project_id=project_id)
    signals = {task["title"]: (task["schedule_label"], task["schedule_class"]) for task in view["tasks"]}

    assert signals == {
        "期限超過": ("期限超過", "late"),
        "開始予定超過": ("開始予定超過", "start-late"),
        "遅れなし": ("遅れなし", "on-track"),
        "完了": ("完了", "done"),
    }


def test_project_selection_includes_closed_projects_and_rejects_other_users(db):
    closed_project = projects.create_project(db, 1, "終了案件")
    projects.set_project_closed(db, 1, closed_project, True)
    db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "別の利用者"))
    other_project = projects.create_project(db, 2, "別の人の案件")

    view = wbs.build_view(db, 1, project_id=closed_project)
    invalid_view = wbs.build_view(db, 1, project_id=other_project)

    assert view["project"]["status"] == "closed"
    assert view["project"]["name"] == "終了案件"
    assert invalid_view["project"]["id"] == closed_project


def test_status_signal_and_text_filters_combine_and_keep_matching_child_context(db, monkeypatch):
    monkeypatch.setattr(clock, "today", lambda: date(2026, 10, 29))
    project_id = projects.create_project(db, 1, "絞り込み案件")
    parent = tasks.create_task(db, 1, "進行中の親", project_id=project_id)
    matching_child = tasks.create_task(
        db, 1, "認証APIの確認", parent_id=parent,
        start_date="2026-10-28", due_date="2026-11-06",
    )
    done_child = tasks.create_task(db, 1, "完了した検査", parent_id=parent)
    tasks.set_status(db, matching_child, "doing")
    tasks.set_status(db, done_child, "done")
    overdue_task = tasks.create_task(
        db, 1, "別の期限超過", project_id=project_id,
        start_date="2026-10-20", due_date="2026-10-28",
    )

    open_view = wbs.build_view(db, 1, project_id=project_id, status_filter="open")
    done_view = wbs.build_view(db, 1, project_id=project_id, status_filter="done")
    late_view = wbs.build_view(db, 1, project_id=project_id, signal_filter="late")
    search_view = wbs.build_view(
        db, 1, project_id=project_id, status_filter="open", search_query=" 認証api ",
    )

    assert {task["id"] for task in open_view["tasks"]} == {parent, matching_child, overdue_task}
    assert {task["id"] for task in done_view["tasks"]} == {done_child}
    assert [task["id"] for task in late_view["tasks"]] == [overdue_task]
    assert [task["id"] for task in search_view["tasks"]] == [matching_child]
    assert search_view["tasks"][0]["depth"] == 0
    assert search_view["tasks"][0]["parent_title"] == "進行中の親"
    assert search_view["search_query"] == "認証api"


def test_wbs_route_renders_dedicated_page_tabs_and_project_tasks(client, db_path):
    connection = database.connect(db_path)
    try:
        project_id = projects.create_project(connection, 1, "WBS案件")
        parent = tasks.create_task(
            connection, 1, "親タスク", project_id=project_id,
            start_date="2026-10-28", due_date="2026-11-06",
        )
        tasks.create_task(
            connection, 1, "子タスク", parent_id=parent,
            start_date="2026-10-29", due_date="2026-11-02",
        )
        connection.commit()
    finally:
        connection.close()

    response = client.get(f"/wbs?project_id={project_id}")

    assert response.status_code == 200
    assert 'aria-current=page>WBS</a>' in response.text
    assert "WBS案件のWBS" in response.text
    assert "親タスク" in response.text
    assert "子タスク" in response.text
    assert "遅れなし" in response.text
    assert 'class="wbs-bar bar-start' in response.text


def test_wbs_route_marks_closed_project_and_supports_day_mode(client, db_path):
    connection = database.connect(db_path)
    try:
        project_id = projects.create_project(connection, 1, "終了WBS案件")
        projects.set_project_closed(connection, 1, project_id, True)
        tasks.create_task(connection, 1, "日付なしタスク", project_id=project_id)
        connection.commit()
    finally:
        connection.close()

    response = client.get(f"/wbs?project_id={project_id}&mode=day&week=2026-10-29")

    assert "終了案件" in response.text
    assert "日付なしタスク" in response.text
    assert "判定なし" in response.text
    assert "10/26(月)" in response.text
    assert "11/1(日)" in response.text
    assert '<span class="wbs-today-label">今日</span>' in response.text
    assert response.text.count('class="wbs-today-marker"') == 2
    assert "--today-position: 50.0%" in response.text


def test_wbs_route_applies_filters_and_keeps_them_when_switching_period(client, db_path):
    connection = database.connect(db_path)
    try:
        project_id = projects.create_project(connection, 1, "フィルタ案件")
        parent = tasks.create_task(connection, 1, "親項目", project_id=project_id)
        overdue_child = tasks.create_task(
            connection, 1, "対象API確認", parent_id=parent,
            start_date="2026-10-20", due_date="2026-10-28",
        )
        tasks.create_task(
            connection, 1, "別の未完了", project_id=project_id,
            start_date="2026-10-29", due_date="2026-11-06",
        )
        completed = tasks.create_task(connection, 1, "完了項目", project_id=project_id)
        tasks.set_status(connection, completed, "done")
        connection.commit()
    finally:
        connection.close()

    response = client.get(
        f"/wbs?project_id={project_id}&mode=day&week=2026-10-26&status=open&signal=late&q=API",
    )

    assert response.status_code == 200
    assert "1 / 4 件を表示" in response.text
    assert 'name="status"' in response.text
    assert 'name="signal"' in response.text
    assert 'name="q" type="search" value="API"' in response.text
    assert "対象API確認" in response.text
    assert "親項目 ›" in response.text
    assert "別の未完了" not in response.text
    assert "完了項目" not in response.text
    assert f"mode=week&amp;week=2026-10-26&amp;status=open&amp;signal=late&amp;q=API" in response.text