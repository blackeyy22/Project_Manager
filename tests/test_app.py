from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import app as app_module
from app import create_app


def make_app(tmp_path, **overrides):
    config = {
        "TESTING": True,
        "DATABASE_PATH": str(tmp_path / "test.sqlite3"),
        "DISCORD_WEBHOOK_URL": "",
        "SECRET_KEY": "test",
        "DEFAULT_ADMIN_USERNAME": "admin@example.test",
        "DEFAULT_ADMIN_PASSWORD": "test-admin-password",
    }
    config.update(overrides)
    return create_app(config)


def login(client, username="admin@example.test", password="test-admin-password"):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        follow_redirects=True,
    )


def create_employee(client, username="dev", password="dev123"):
    client.post(
        "/users",
        data={
            "username": username,
            "password": password,
            "display_name": "Dev User",
            "role": "employee",
            "position": "Developer",
            "discord_user_id": "123456",
        },
    )


def test_dashboard_requires_login_and_admin_can_enter(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()

    response = client.get("/")
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]

    response = login(client)
    assert response.status_code == 200
    assert b"DreamBroad" in response.data
    assert b"Operations Workspace" in response.data
    assert b"Add Item" in response.data
    assert b"Users" in response.data


def test_create_project_task_and_meeting_with_automatic_fields(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(tmp_path, DATABASE_PATH=str(db_path))
    client = app.test_client()
    login(client)
    create_employee(client)

    meeting_start = (datetime.now() + timedelta(days=1)).replace(
        hour=10, minute=0
    ).strftime("%Y-%m-%dT%H:%M")
    meeting_end = (datetime.now() + timedelta(days=1)).replace(
        hour=10, minute=30
    ).strftime("%Y-%m-%dT%H:%M")

    client.post(
        "/projects",
        data={
            "name": "Website rebuild",
            "status": "Active",
            "completion": "35",
            "git_url": "https://github.com/example/website",
            "drive_url": "https://drive.google.com/drive/folders/example",
            "description": "Client work",
        },
    )
    client.post(
        "/tasks",
        data={
            "project_id": "1",
            "title": "Draft milestones",
            "status": "Todo",
            "due_at": (datetime.now() + timedelta(hours=1)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "assignee_user_id": "2",
        },
    )
    client.post(
        "/tasks",
        data={
            "project_id": "1",
            "title": "Build prototype",
            "status": "Doing",
            "due_at": (datetime.now() + timedelta(days=2)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "assignee_user_id": "2",
        },
    )
    client.post(
        "/tasks",
        data={
            "project_id": "1",
            "title": "QA handoff",
            "status": "Done",
            "due_at": (datetime.now() + timedelta(days=10)).strftime(
                "%Y-%m-%dT%H:%M"
            ),
            "assignee_user_id": "2",
        },
    )
    client.post(
        "/meetings",
        data={
            "project_id": "1",
            "title": "Kickoff",
            "status": "Planned",
            "starts_at": meeting_start,
            "ends_at": meeting_end,
        },
    )

    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 1
        assert db.execute("SELECT completion FROM projects").fetchone()[0] == 50
        assert (
            db.execute("SELECT drive_url FROM projects").fetchone()[0]
            == "https://drive.google.com/drive/folders/example"
        )
        priorities = [
            row[0]
            for row in db.execute("SELECT priority FROM tasks ORDER BY id").fetchall()
        ]
        assert priorities == ["Critical", "High", "Low"]
    finally:
        db.close()

    dashboard = client.get("/")
    assert b"Task Status" in dashboard.data
    assert b"Build prototype" in dashboard.data
    assert b"QA handoff" in dashboard.data
    assert b"Kickoff" in dashboard.data
    assert b"https://drive.google.com/drive/folders/example" in dashboard.data


def test_task_status_json_endpoint_moves_card_and_updates_completion(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(tmp_path, DATABASE_PATH=str(db_path))
    client = app.test_client()
    login(client)

    client.post("/projects", data={"name": "Migration", "status": "Active"})
    client.post(
        "/tasks",
        data={"project_id": "1", "title": "Move me", "status": "Todo"},
    )
    response = client.post(
        "/tasks/1/status",
        json={"status": "Doing"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "Doing"
    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT status FROM tasks WHERE id = 1").fetchone()[0] == "Doing"
        assert db.execute("SELECT completion FROM projects WHERE id = 1").fetchone()[0] == 50
    finally:
        db.close()


def test_task_status_json_endpoint_rejects_bad_status(tmp_path):
    app = make_app(tmp_path)
    client = app.test_client()
    login(client)

    response = client.post(
        "/tasks/1/status",
        json={"status": "Not Real"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400


def test_due_alert_json_without_webhook(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(
        tmp_path,
        DATABASE_PATH=str(db_path),
        ALERT_WINDOW_HOURS=48,
    )
    client = app.test_client()
    login(client)

    due_at = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M")
    client.post("/tasks", data={"title": "Due now", "due_at": due_at})
    response = client.post("/alerts/due", headers={"Accept": "application/json"})
    payload = json.loads(response.data)

    assert response.status_code == 200
    assert payload["sent"] is False
    assert payload["count"] == 1
    assert "Discord webhook" in payload["message"]


def test_database_self_heals_if_sqlite_file_is_recreated(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(tmp_path, DATABASE_PATH=str(db_path))
    client = app.test_client()
    login(client)

    Path(db_path).unlink()
    response = client.get("/")

    assert response.status_code == 200
    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
    finally:
        db.close()


def test_stale_project_assignment_saves_unassigned(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(tmp_path, DATABASE_PATH=str(db_path))
    client = app.test_client()
    login(client)

    response = client.post(
        "/tasks",
        data={
            "project_id": "999",
            "title": "Stale project id",
            "status": "Todo",
            "priority": "Normal",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT project_id FROM tasks").fetchone()[0] is None
    finally:
        db.close()


def test_employee_can_create_self_task_and_only_move_own_tasks(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(tmp_path, DATABASE_PATH=str(db_path))
    client = app.test_client()
    login(client)
    create_employee(client)

    client.post("/logout")
    login(client, "dev", "dev123")
    client.post(
        "/tasks",
        data={
            "title": "My task",
            "status": "Todo",
            "assignee_user_id": "1",
        },
    )

    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT assignee_user_id FROM tasks WHERE id = 1").fetchone()[0] == 2
    finally:
        db.close()

    own_move = client.post(
        "/tasks/1/status",
        json={"status": "Doing"},
        headers={"Accept": "application/json"},
    )
    assert own_move.status_code == 200

    client.post("/logout")
    login(client)
    client.post(
        "/tasks",
        data={"title": "Admin task", "status": "Todo", "assignee_user_id": "1"},
    )

    client.post("/logout")
    login(client, "dev", "dev123")
    forbidden_move = client.post(
        "/tasks/2/status",
        json={"status": "Doing"},
        headers={"Accept": "application/json"},
    )
    assert forbidden_move.status_code == 403


def test_scheduled_discord_jobs_send_meeting_reminder_and_daily_greeting(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(
        tmp_path,
        DATABASE_PATH=str(db_path),
        DISCORD_WEBHOOK_URL="https://discord.test/webhook",
        DAILY_GREETING_TIME=datetime.now().strftime("%H:%M"),
        DAILY_GREETING_GRACE_MINUTES=60,
    )
    client = app.test_client()
    login(client)

    starts_at = (datetime.now() + timedelta(minutes=4)).strftime("%Y-%m-%dT%H:%M")
    client.post(
        "/meetings",
        data={"title": "Standup", "status": "Planned", "starts_at": starts_at},
    )

    sent_messages = []

    def fake_send(title, lines):
        sent_messages.append((title, lines))
        return True, "sent"

    monkeypatch.setattr(app_module, "send_discord_alert", fake_send)

    with app.app_context():
        result = app_module.run_scheduled_discord_jobs(app_module.get_db())

    assert result["sent"] == 2
    assert "Meeting starts in 5 minutes" in [title for title, _lines in sent_messages]
    assert "Good morning" in [title for title, _lines in sent_messages]

    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT reminder_sent_at FROM meetings").fetchone()[0] is not None
        assert db.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0] == 1
    finally:
        db.close()
