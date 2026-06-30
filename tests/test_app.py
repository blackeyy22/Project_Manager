from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from app import create_app


def test_dashboard_creates_database(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(tmp_path / "test.sqlite3"),
            "DISCORD_WEBHOOK_URL": "",
            "SECRET_KEY": "test",
        }
    )

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"Greenboard" in response.data
    assert b"Add Item" in response.data
    assert b"Work Board" in response.data
    assert b"Task Status" in response.data
    assert b"Meeting Calendar" in response.data


def test_create_project_task_and_meeting(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(db_path),
            "DISCORD_WEBHOOK_URL": "",
            "SECRET_KEY": "test",
        }
    )
    client = app.test_client()
    meeting_start = datetime.now().replace(day=15, hour=10, minute=0).strftime(
        "%Y-%m-%dT%H:%M"
    )
    meeting_end = datetime.now().replace(day=15, hour=10, minute=30).strftime(
        "%Y-%m-%dT%H:%M"
    )

    client.post(
        "/projects",
        data={
            "name": "Website rebuild",
            "status": "Active",
            "completion": "35",
            "git_url": "https://github.com/example/website",
            "description": "Client work",
        },
    )
    client.post(
        "/tasks",
        data={
            "project_id": "1",
            "title": "Draft milestones",
            "status": "Todo",
            "priority": "High",
            "due_at": "2030-01-01T09:00",
        },
    )
    client.post(
        "/tasks",
        data={
            "project_id": "1",
            "title": "Build prototype",
            "status": "Doing",
            "priority": "Normal",
            "due_at": "2030-01-01T10:00",
        },
    )
    client.post(
        "/tasks",
        data={
            "project_id": "1",
            "title": "QA handoff",
            "status": "Review",
            "priority": "Critical",
            "due_at": "2030-01-01T11:00",
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
        assert db.execute("SELECT completion FROM projects").fetchone()[0] == 35
    finally:
        db.close()

    dashboard = client.get("/")
    assert b"Task Status" in dashboard.data
    assert b"Build prototype" in dashboard.data
    assert b"QA handoff" in dashboard.data
    assert b"Kickoff" in dashboard.data


def test_task_status_json_endpoint_moves_card(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(db_path),
            "DISCORD_WEBHOOK_URL": "",
            "SECRET_KEY": "test",
        }
    )
    client = app.test_client()

    client.post("/tasks", data={"title": "Move me", "status": "Todo"})
    response = client.post(
        "/tasks/1/status",
        json={"status": "Review"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    assert response.get_json()["status"] == "Review"
    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT status FROM tasks WHERE id = 1").fetchone()[0] == "Review"
    finally:
        db.close()


def test_task_status_json_endpoint_rejects_bad_status(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(tmp_path / "test.sqlite3"),
            "DISCORD_WEBHOOK_URL": "",
            "SECRET_KEY": "test",
        }
    )

    response = app.test_client().post(
        "/tasks/1/status",
        json={"status": "Not Real"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 400


def test_due_alert_json_without_webhook(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(db_path),
            "DISCORD_WEBHOOK_URL": "",
            "SECRET_KEY": "test",
            "ALERT_WINDOW_HOURS": 48,
        }
    )
    client = app.test_client()

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
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(db_path),
            "DISCORD_WEBHOOK_URL": "",
            "SECRET_KEY": "test",
        }
    )
    client = app.test_client()

    Path(db_path).unlink()
    response = client.get("/")

    assert response.status_code == 200
    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 0
    finally:
        db.close()


def test_stale_project_assignment_saves_unassigned(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(db_path),
            "DISCORD_WEBHOOK_URL": "",
            "SECRET_KEY": "test",
        }
    )
    client = app.test_client()

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
