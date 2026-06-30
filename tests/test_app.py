from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from app import create_app


def test_dashboard_creates_database(tmp_path):
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(tmp_path / "test.sqlite3"),
            "SECRET_KEY": "test",
        }
    )

    response = app.test_client().get("/")

    assert response.status_code == 200
    assert b"Greenboard" in response.data
    assert b"Add Item" in response.data


def test_create_project_task_and_meeting(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = create_app(
        {"TESTING": True, "DATABASE_PATH": str(db_path), "SECRET_KEY": "test"}
    )
    client = app.test_client()

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
        "/meetings",
        data={
            "project_id": "1",
            "title": "Kickoff",
            "status": "Planned",
            "starts_at": "2030-01-02T10:00",
            "ends_at": "2030-01-02T10:30",
        },
    )

    db = sqlite3.connect(db_path)
    try:
        assert db.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1
        assert db.execute("SELECT COUNT(*) FROM meetings").fetchone()[0] == 1
        assert db.execute("SELECT completion FROM projects").fetchone()[0] == 35
    finally:
        db.close()


def test_due_alert_json_without_webhook(tmp_path):
    db_path = tmp_path / "test.sqlite3"
    app = create_app(
        {
            "TESTING": True,
            "DATABASE_PATH": str(db_path),
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
