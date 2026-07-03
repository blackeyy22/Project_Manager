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
        "ADMIN_DISCORD_WEBHOOK_URL": "",
        "EMP_DISCORD_WEBHOOK_URL": "",
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


def create_user(
    client,
    username="dev",
    password="dev123",
    display_name="Dev User",
    role="employee",
    position="Developer",
    project_id=None,
):
    data = {
        "username": username,
        "password": password,
        "display_name": display_name,
        "role": role,
        "position": position,
        "discord_user_id": "123456",
    }
    if project_id is not None:
        data["project_id"] = str(project_id)
    client.post(
        "/users",
        data=data,
    )


def create_employee(client, username="dev", password="dev123", project_id=None):
    create_user(client, username=username, password=password, project_id=project_id)


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
            "attendees": ["Admin", "Dev User"],
            "agenda": "Kickoff agenda",
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
        assert (
            db.execute("SELECT attendees FROM meetings WHERE id = 1").fetchone()[0]
            == "Admin, Dev User"
        )
        priorities = [
            row[0]
            for row in db.execute("SELECT priority FROM tasks ORDER BY id").fetchall()
        ]
        assert priorities == ["Critical", "High", "Low"]
    finally:
        db.close()

    client.post(
        "/meetings/1",
        data={
            "project_id": "1",
            "title": "Updated Kickoff",
            "status": "Planned",
            "starts_at": meeting_start,
            "ends_at": meeting_end,
            "location": "Zoom",
            "attendees": ["Dev User"],
            "agenda": "Updated agenda",
        },
    )
    db = sqlite3.connect(db_path)
    try:
        meeting = db.execute(
            "SELECT title, attendees, location, agenda FROM meetings WHERE id = 1"
        ).fetchone()
        assert meeting == ("Updated Kickoff", "Dev User", "Zoom", "Updated agenda")
    finally:
        db.close()

    dashboard = client.get("/")
    assert b"Task Status" in dashboard.data
    assert b"Build prototype" in dashboard.data
    assert b"QA handoff" in dashboard.data
    assert b"Updated Kickoff" in dashboard.data
    assert b"Edit Meeting" in dashboard.data
    assert b"Delete Meeting" in dashboard.data
    assert b'type="checkbox" name="attendees"' in dashboard.data
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
    dashboard = client.get("/")
    assert b"All members" not in dashboard.data

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


def test_client_manages_only_their_project_work(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(tmp_path, DATABASE_PATH=str(db_path))
    client = app.test_client()
    login(client)

    client.post("/projects", data={"name": "Client project", "status": "Active"})
    client.post("/projects", data={"name": "Other project", "status": "Active"})
    create_user(
        client,
        username="project-dev",
        password="dev123",
        display_name="Project Dev",
        role="employee",
        project_id=1,
    )
    create_user(
        client,
        username="outside-dev",
        password="dev123",
        display_name="Outside Dev",
        role="employee",
        project_id=2,
    )
    create_user(
        client,
        username="client-user",
        password="client123",
        display_name="Client User",
        role="client",
        position="Client",
        project_id=1,
    )
    client.post(
        "/tasks",
        data={"project_id": "1", "title": "Visible task", "assignee_user_id": "2"},
    )
    client.post(
        "/tasks",
        data={"project_id": "2", "title": "Hidden task", "assignee_user_id": "3"},
    )

    client.post("/logout")
    login(client, "client-user", "client123")
    dashboard = client.get("/")
    assert b"Visible task" in dashboard.data
    assert b"Hidden task" not in dashboard.data
    assert b"Project Dev" in dashboard.data
    assert b"Outside Dev" not in dashboard.data
    assert b"All members" in dashboard.data
    assert b"data-form-tab=\"meeting\"" in dashboard.data
    assert b"data-form-tab=\"project\"" not in dashboard.data

    client.post(
        "/tasks",
        data={
            "project_id": "2",
            "title": "Client-created task",
            "assignee_user_id": "2",
        },
    )
    client.post(
        "/tasks",
        data={
            "project_id": "1",
            "title": "Wrong assignee",
            "assignee_user_id": "3",
        },
    )
    starts_at = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    client.post(
        "/meetings",
        data={"project_id": "2", "title": "Client sync", "starts_at": starts_at},
    )
    allowed_move = client.post(
        "/tasks/1/status",
        json={"status": "Doing"},
        headers={"Accept": "application/json"},
    )
    forbidden_move = client.post(
        "/tasks/2/status",
        json={"status": "Doing"},
        headers={"Accept": "application/json"},
    )

    assert allowed_move.status_code == 200
    assert forbidden_move.status_code == 403

    db = sqlite3.connect(db_path)
    try:
        assert (
            db.execute(
                "SELECT project_id, assignee_user_id FROM tasks WHERE title = ?",
                ("Client-created task",),
            ).fetchone()
            == (1, 2)
        )
        assert (
            db.execute(
                "SELECT project_id, assignee_user_id FROM tasks WHERE title = ?",
                ("Wrong assignee",),
            ).fetchone()
            == (1, None)
        )
        assert (
            db.execute(
                "SELECT project_id FROM meetings WHERE title = ?",
                ("Client sync",),
            ).fetchone()[0]
            == 1
        )
    finally:
        db.close()


def test_discord_webhooks_route_by_event_type(tmp_path, monkeypatch):
    app = make_app(
        tmp_path,
        ADMIN_DISCORD_WEBHOOK_URL="admin-hook",
        EMP_DISCORD_WEBHOOK_URL="employee-hook",
    )
    client = app.test_client()
    login(client)

    sent_messages = []

    def fake_send(title, lines, webhook_url=None):
        sent_messages.append((title, lines, webhook_url))
        return True, "sent"

    monkeypatch.setattr(app_module, "send_discord_alert", fake_send)

    client.post(
        "/projects",
        data={
            "name": "Webhook project",
            "status": "Active",
            "client_webhook_url": "project-client-hook",
        },
    )
    create_employee(client, project_id=1)
    sent_messages.clear()

    client.post(
        "/tasks",
        data={"project_id": "1", "title": "Notify me", "assignee_user_id": "2"},
    )
    assignment_urls = {
        webhook_url
        for title, _lines, webhook_url in sent_messages
        if title == "Task assigned"
    }
    assert assignment_urls == {
        "admin-hook",
        "project-client-hook",
        "employee-hook",
    }

    sent_messages.clear()
    client.post(
        "/tasks/1",
        data={
            "project_id": "1",
            "title": "Notify me edited",
            "status": "Todo",
            "assignee_user_id": "2",
            "due_at": "",
            "notes": "Updated notes",
        },
    )
    update_urls = {
        webhook_url
        for title, _lines, webhook_url in sent_messages
        if title == "Task updated"
    }
    assert update_urls == {"admin-hook", "project-client-hook"}
    assert "employee-hook" not in update_urls

    sent_messages.clear()
    response = client.post(
        "/tasks/1/status",
        json={"status": "Doing"},
        headers={"Accept": "application/json"},
    )
    assert response.status_code == 200
    move_urls = {
        webhook_url
        for title, _lines, webhook_url in sent_messages
        if title == "Task moved"
    }
    assert move_urls == {"admin-hook", "project-client-hook"}
    assert "employee-hook" not in move_urls

    sent_messages.clear()
    client.post(
        "/projects/1",
        data={
            "name": "Webhook project edited",
            "status": "Active",
            "git_url": "",
            "drive_url": "",
            "client_webhook_url": "project-client-hook",
            "description": "",
        },
    )
    project_update_urls = {
        webhook_url
        for title, _lines, webhook_url in sent_messages
        if title == "Project updated"
    }
    assert project_update_urls == {"admin-hook", "project-client-hook"}

    starts_at = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    sent_messages.clear()
    client.post(
        "/meetings",
        data={"project_id": "1", "title": "Webhook sync", "starts_at": starts_at},
    )
    meeting_create_urls = {
        webhook_url
        for title, _lines, webhook_url in sent_messages
        if title == "Meeting scheduled"
    }
    assert meeting_create_urls == {"admin-hook", "project-client-hook"}

    sent_messages.clear()
    client.post(
        "/meetings/1",
        data={
            "project_id": "1",
            "title": "Webhook sync edited",
            "status": "Planned",
            "starts_at": starts_at,
        },
    )
    meeting_update_urls = {
        webhook_url
        for title, _lines, webhook_url in sent_messages
        if title == "Meeting updated"
    }
    assert meeting_update_urls == {"admin-hook", "project-client-hook"}


def test_scheduled_discord_jobs_send_meeting_reminder_and_daily_greeting(
    tmp_path, monkeypatch
):
    db_path = tmp_path / "test.sqlite3"
    app = make_app(
        tmp_path,
        DATABASE_PATH=str(db_path),
        ADMIN_DISCORD_WEBHOOK_URL="https://discord.test/webhook",
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

    def fake_send(title, lines, webhook_url=None):
        sent_messages.append((title, lines, webhook_url))
        return True, "sent"

    monkeypatch.setattr(app_module, "send_discord_alert", fake_send)

    with app.app_context():
        result = app_module.run_scheduled_discord_jobs(app_module.get_db())

    assert result["sent"] == 2
    assert "Meeting starts in 5 minutes" in [
        title for title, _lines, _webhook_url in sent_messages
    ]
    assert "Good morning" in [
        title for title, _lines, _webhook_url in sent_messages
    ]

    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT reminder_sent_at FROM meetings").fetchone()[0] is not None
        assert db.execute("SELECT COUNT(*) FROM automation_runs").fetchone()[0] == 1
    finally:
        db.close()
