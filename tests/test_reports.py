"""日報の入力ON/OFFと、切り替えても入力内容が残ることを確認する。"""
from datetime import date
from pathlib import Path
import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import clock, daily, db as database, reports, tasks


def add_report_task(db: sqlite3.Connection) -> int:
    """日報に選んだタスクを用意し、時間の保存も確認できるようにする。"""
    task_id = tasks.create_task(db, 1, "日報確認")
    reports.toggle_item(db, 1, task_id, clock.today())
    db.commit()
    return task_id


def test_initial_toggles_are_on(db: sqlite3.Connection, client: TestClient) -> None:
    view = reports.build_report_view(db, 1, clock.today())
    assert view["time_tracking_enabled"] is True
    assert view["details_enabled"] is True
    response = client.get("/report")
    assert response.text.count('aria-pressed="true"\n                  hx-post="/report/toggle-') == 2
    assert 'type="checkbox" name="work_style" value="telework"' in response.text
    assert 'type="radio" name="work_style"' not in response.text
    report = reports.get_or_create_report(db, 1, clock.today())
    assert (report["time_tracking_enabled"], report["details_enabled"]) == (1, 1)


def test_time_totals_are_hidden_when_time_tracking_is_off(
    db: sqlite3.Connection, client: TestClient,
) -> None:
    add_report_task(db)
    assert "基本勤務時間" in client.get("/report").text

    off_response = client.post("/report/toggle-time-tracking", data={"comment": ""})
    assert "基本勤務時間" not in off_response.text
    assert "作業時間の合計" not in off_response.text
    assert "差(残業時間)" not in off_response.text
    assert "提出する" in off_response.text
    assert "勤務形態・休暇などを入力する" in off_response.text

    autosave_response = client.post("/report/save", data={"comment": "時間OFFの備考"})
    assert "基本勤務時間" not in autosave_response.text
    assert "作業時間の合計" not in autosave_response.text
    assert "差(残業時間)" not in autosave_response.text

    on_response = client.post("/report/toggle-time-tracking", data={"comment": "時間OFFの備考"})
    assert "基本勤務時間" in on_response.text
    assert "作業時間の合計" in on_response.text
    assert "差(残業時間)" in on_response.text


@pytest.mark.parametrize("first", ["time-tracking", "details"])
def test_toggles_are_independent_and_remembered(
    db: sqlite3.Connection, client: TestClient, first: str,
) -> None:
    second = "details" if first == "time-tracking" else "time-tracking"
    first_column = "time_tracking_enabled" if first == "time-tracking" else "details_enabled"
    second_column = "details_enabled" if first == "time-tracking" else "time_tracking_enabled"
    for section in [first, second, first]:
        assert client.post(f"/report/toggle-{section}", data={"comment": ""}).status_code == 200
    report = reports.find_report(db, 1, clock.today())
    assert (report[first_column], report[second_column]) == (1, 0)
    assert client.get("/report").status_code == 200
    assert reports.find_report(db, 1, clock.today())[second_column] == 0
    other_day = date(2026, 10, 28)
    view = reports.build_report_view(db, 1, other_day)
    assert view["time_tracking_enabled"] is True
    assert view["details_enabled"] is True
    db.execute("INSERT INTO users (id, name) VALUES (?, ?)", (2, "別の利用者"))
    other_report = reports.get_or_create_report(db, 2, clock.today())
    assert (other_report["time_tracking_enabled"], other_report["details_enabled"]) == (1, 1)


@pytest.mark.parametrize("time_on,details_on", [(True, True), (True, False), (False, True), (False, False)])
def test_save_and_submit_in_all_toggle_states(
    db: sqlite3.Connection, client: TestClient, time_on: bool, details_on: bool,
) -> None:
    task_id = add_report_task(db)
    client.post("/report/save", data={
        f"time_{task_id}": "1:30", "work_style": "telework", "partial_leave": "午前休",
        "leave_types": "早退", "comment": "保存済み",
    })
    if not time_on:
        client.post("/report/toggle-time-tracking", data={
            f"time_{task_id}": "1:30", "work_style": "telework", "partial_leave": "午前休",
            "leave_types": "早退", "comment": "保存済み",
        })
    if not details_on:
        client.post("/report/toggle-details", data={
            "work_style": "telework", "partial_leave": "午前休", "leave_types": "早退",
            "comment": "保存済み",
        })
    form = {"comment": "変更後"}
    if time_on:
        form[f"time_{task_id}"] = "2:15"
    if details_on:
        form["partial_leave"] = ""
    for endpoint in ["save", "submit"]:
        response = client.post(f"/report/{endpoint}", data=form, follow_redirects=False)
        assert response.status_code == (200 if endpoint == "save" else 303)
    view = reports.build_report_view(db, 1, clock.today())
    assert view["task_items"][0]["report_minutes"] == (135 if time_on else 90)
    assert daily.get_minutes(db, task_id, clock.today()) == (135 if time_on else 90)
    assert view["work_style"] == ("office" if details_on else "telework")
    assert view["leave_types"] == ([] if details_on else ["早退", "午前休"])
    assert view["comment"] == "変更後"
    assert view["status"] == "submitted"


def test_toggle_saves_pending_inputs_and_restores_them(db: sqlite3.Connection, client: TestClient) -> None:
    task_id = add_report_task(db)
    form = {
        f"time_{task_id}": "3:20", "work_style": "telework", "partial_leave": "午後休",
        "comment": "切り替える直前の入力",
    }
    response = client.post("/report/toggle-time-tracking", data=form)
    assert response.status_code == 200
    assert "日報確認" in response.text
    assert f'name="time_{task_id}"' not in response.text
    view = reports.build_report_view(db, 1, clock.today())
    assert view["task_items"][0]["report_minutes"] == 200
    assert view["comment"] == form["comment"]
    client.post("/report/toggle-details", data=form)
    response = client.post("/report/toggle-time-tracking", data={"comment": form["comment"]})
    assert 'value="3:20"' in response.text
    response = client.post("/report/toggle-details", data={"comment": form["comment"]})
    assert 'value="telework" checked' in response.text
    assert 'value="午後休" checked' in response.text


@pytest.mark.parametrize("section", ["time-tracking", "details"])
def test_invalid_pending_time_keeps_form_and_toggle(
    db: sqlite3.Connection, client: TestClient, section: str,
) -> None:
    task_id = add_report_task(db)
    response = client.post(f"/report/toggle-{section}", data={
        f"time_{task_id}": "1:99", "comment": "書きかけ",
    })
    assert response.headers["HX-Retarget"] == "#report-summary"
    assert response.headers["HX-Reswap"] == "outerHTML"
    assert "作業時間が読めません" in response.text
    view = reports.build_report_view(db, 1, clock.today())
    assert view["time_tracking_enabled"] is True
    assert view["details_enabled"] is True
    assert view["task_items"][0]["report_minutes"] == 0


@pytest.mark.parametrize("section", ["time-tracking", "details"])
def test_toggle_marks_submitted_report_modified(db: sqlite3.Connection, client: TestClient, section: str) -> None:
    client.post("/report/submit", data={"comment": ""})
    assert reports.report_status(reports.find_report(db, 1, clock.today())) == "submitted"
    client.post(f"/report/toggle-{section}", data={"comment": ""})
    assert reports.report_status(reports.find_report(db, 1, clock.today())) == "modified"
    client.post("/report/submit", data={"comment": ""})
    assert reports.report_status(reports.find_report(db, 1, clock.today())) == "submitted"


def test_existing_report_migration_preserves_values(tmp_path: Path) -> None:
    old_path = tmp_path / "old-report.db"
    with sqlite3.connect(old_path) as connection:
        connection.executescript("""
            CREATE TABLE daily_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
                report_date TEXT NOT NULL, work_style TEXT NOT NULL DEFAULT 'office',
                leave_types TEXT NOT NULL DEFAULT '', comment TEXT NOT NULL DEFAULT '',
                submitted_at TEXT, has_unsubmitted_changes INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL, UNIQUE (user_id, report_date)
            );
        """)
        connection.execute(
            "INSERT INTO daily_reports (user_id, report_date, work_style, leave_types, comment, "
            "submitted_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (1, "2026-10-28", "telework", "早退", "以前の日報", "2026-10-28T17:00:00", "2026-10-28T17:00:00"),
        )
    database.init_db(old_path)
    database.init_db(old_path)
    connection = database.connect(old_path)
    try:
        report = reports.find_report(connection, 1, date(2026, 10, 28))
        assert (report["time_tracking_enabled"], report["details_enabled"]) == (1, 1)
        assert (report["work_style"], report["leave_types"], report["comment"]) == ("telework", "早退", "以前の日報")
        assert reports.report_status(report) == "submitted"
    finally:
        connection.close()
