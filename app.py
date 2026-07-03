from __future__ import annotations

import calendar
import json
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path
from urllib import error, request as urlrequest

from flask import (
    Flask,
    current_app,
    flash,
    g,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from werkzeug.security import check_password_hash, generate_password_hash


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "project_manager.sqlite3"
DATETIME_FORMAT = "%Y-%m-%dT%H:%M"

PROJECT_STATUSES = ("Planned", "Active", "Paused", "Done")
TASK_STATUSES = ("Todo", "Doing", "Done")
TASK_PRIORITIES = ("Low", "Normal", "High", "Critical")
MEETING_STATUSES = ("Planned", "Held", "Cancelled")
USER_ROLES = ("admin", "employee")
DEFAULT_ADMIN_USERNAME = "Robin@dreamsycn.com"
DEFAULT_ADMIN_PASSWORD = "Robin@1234"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'employee',
    position TEXT,
    password_hash TEXT NOT NULL,
    discord_user_id TEXT,
    active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Active',
    completion INTEGER NOT NULL DEFAULT 0,
    git_url TEXT,
    description TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Todo',
    priority TEXT NOT NULL DEFAULT 'Normal',
    due_at TEXT,
    assignee TEXT,
    assignee_user_id INTEGER,
    notes TEXT,
    discord_alerted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL,
    FOREIGN KEY (assignee_user_id) REFERENCES users(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER,
    title TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Planned',
    starts_at TEXT NOT NULL,
    ends_at TEXT,
    location TEXT,
    attendees TEXT,
    agenda TEXT,
    discord_alerted_at TEXT,
    reminder_sent_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS automation_runs (
    name TEXT PRIMARY KEY,
    last_run_at TEXT NOT NULL
);
"""


def create_app(test_config: dict | None = None) -> Flask:
    load_env_file()

    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE_PATH=os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH)),
        DISCORD_WEBHOOK_URL=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        ALERT_WINDOW_HOURS=int(os.environ.get("ALERT_WINDOW_HOURS", "24")),
        APP_PUBLIC_URL=os.environ.get("APP_PUBLIC_URL", "http://127.0.0.1:5000"),
        DAILY_GREETING_TIME=os.environ.get("DAILY_GREETING_TIME", "09:00"),
        DAILY_GREETING_GRACE_MINUTES=int(
            os.environ.get("DAILY_GREETING_GRACE_MINUTES", "10")
        ),
        ENABLE_BACKGROUND_JOBS=parse_bool(
            os.environ.get("ENABLE_BACKGROUND_JOBS", "true")
        ),
        AUTOMATION_POLL_SECONDS=int(os.environ.get("AUTOMATION_POLL_SECONDS", "60")),
        DEFAULT_ADMIN_USERNAME=os.environ.get(
            "DEFAULT_ADMIN_USERNAME", DEFAULT_ADMIN_USERNAME
        ),
        DEFAULT_ADMIN_PASSWORD=os.environ.get(
            "DEFAULT_ADMIN_PASSWORD", DEFAULT_ADMIN_PASSWORD
        ),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    )

    if test_config:
        app.config.update(test_config)

    app.teardown_appcontext(close_db)
    app.before_request(load_logged_in_user)

    with app.app_context():
        init_db()

    register_routes(app)
    start_background_jobs(app)
    return app


def load_env_file(path: Path | None = None) -> None:
    env_path = path or BASE_DIR / ".env"
    if not env_path.exists():
        return

    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip("\"'")
        if key and key not in os.environ:
            os.environ[key] = value


def get_db() -> sqlite3.Connection:
    db = g.get("_database")
    if db is None:
        db_path = Path(current_app.config["DATABASE_PATH"])
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(db_path)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        g._database = db
        ensure_schema(db)
    return db


def close_db(_error: Exception | None = None) -> None:
    db = g.pop("_database", None)
    if db is not None:
        db.close()


def init_db() -> None:
    ensure_schema(get_db())


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)
    migrate_db(db)
    db.commit()


def migrate_db(db: sqlite3.Connection | None = None) -> None:
    db = db or get_db()
    project_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(projects)").fetchall()
    }
    if "completion" not in project_columns:
        db.execute(
            "ALTER TABLE projects ADD COLUMN completion INTEGER NOT NULL DEFAULT 0"
        )

    task_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(tasks)").fetchall()
    }
    if "assignee_user_id" not in task_columns:
        db.execute("ALTER TABLE tasks ADD COLUMN assignee_user_id INTEGER")

    meeting_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(meetings)").fetchall()
    }
    if "reminder_sent_at" not in meeting_columns:
        db.execute("ALTER TABLE meetings ADD COLUMN reminder_sent_at TEXT")

    user_columns = {
        row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()
    }
    if "active" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN active INTEGER NOT NULL DEFAULT 1")

    status_placeholders = ", ".join("?" for _ in TASK_STATUSES)
    db.execute(
        f"UPDATE tasks SET status = ? WHERE status NOT IN ({status_placeholders})",
        ("Todo", *TASK_STATUSES),
    )
    seed_default_admin(db)
    refresh_derived_fields(db)


def register_routes(app: Flask) -> None:
    @app.get("/login")
    def login():
        if g.get("current_user") is not None:
            return redirect(url_for("dashboard"))
        return render_template("login.html")

    @app.post("/login")
    def login_post():
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "")
        db = get_db()
        user = db.execute(
            "SELECT * FROM users WHERE username = ? AND active = 1", (username,)
        ).fetchone()

        if user is None or not check_password_hash(user["password_hash"], password):
            flash("Username or password is incorrect.", "error")
            return redirect(url_for("login"))

        session.clear()
        session["user_id"] = user["id"]
        flash(f"Welcome, {user['display_name']}.", "success")
        return redirect(request.args.get("next") or url_for("dashboard"))

    @app.post("/logout")
    def logout():
        session.clear()
        flash("Logged out.", "success")
        return redirect(url_for("login"))

    @app.get("/")
    @login_required
    def dashboard():
        db = get_db()
        refresh_derived_fields(db)
        db.commit()
        projects = db.execute(
            "SELECT * FROM projects ORDER BY status != 'Active', updated_at DESC"
        ).fetchall()

        task_where = ""
        task_params: tuple = ()
        if not is_admin():
            task_where = """
            WHERE tasks.assignee_user_id = ?
                OR tasks.assignee = ?
            """
            task_params = (
                g.current_user["id"],
                g.current_user["display_name"],
            )

        tasks = db.execute(
            f"""
            SELECT tasks.*, projects.name AS project_name, projects.git_url AS project_git_url
                , users.display_name AS assignee_name, users.username AS assignee_username
                , users.discord_user_id AS assignee_discord_user_id
            FROM tasks
            LEFT JOIN projects ON projects.id = tasks.project_id
            LEFT JOIN users ON users.id = tasks.assignee_user_id
            {task_where}
            ORDER BY
                tasks.status = 'Done',
                tasks.due_at IS NULL,
                tasks.due_at ASC,
                tasks.priority = 'Critical' DESC,
                tasks.updated_at DESC
            """,
            task_params,
        ).fetchall()
        meetings = db.execute(
            """
            SELECT meetings.*, projects.name AS project_name, projects.git_url AS project_git_url
            FROM meetings
            LEFT JOIN projects ON projects.id = meetings.project_id
            ORDER BY meetings.starts_at ASC
            """
        ).fetchall()
        stats = load_stats(db)
        project_summaries = load_project_summaries(db, projects)
        task_chart = load_task_chart(db)
        calendar_month = load_calendar_month(meetings)
        discord_ready = bool(current_app.config.get("DISCORD_WEBHOOK_URL"))
        users = db.execute(
            "SELECT * FROM users WHERE active = 1 ORDER BY role, display_name"
        ).fetchall()

        return render_template(
            "index.html",
            calendar_month=calendar_month,
            current_user=g.current_user,
            discord_ready=discord_ready,
            is_admin=is_admin(),
            meeting_statuses=MEETING_STATUSES,
            meetings=meetings,
            project_statuses=PROJECT_STATUSES,
            projects=projects,
            project_summaries=project_summaries,
            stats=stats,
            task_chart=task_chart,
            task_priorities=TASK_PRIORITIES,
            task_statuses=TASK_STATUSES,
            tasks=tasks,
            users=users,
        )

    @app.post("/projects")
    @admin_required
    def create_project():
        name = require_value("name", "Project name")
        if not name:
            return redirect(url_for("dashboard"))

        now = current_timestamp()
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO projects (
                name, status, completion, git_url, description, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                name,
                choose("status", PROJECT_STATUSES, "Active"),
                0,
                optional_value("git_url"),
                optional_value("description"),
                now,
                now,
            ),
        )
        db.commit()
        project = db.execute(
            "SELECT * FROM projects WHERE id = ?", (cursor.lastrowid,)
        ).fetchone()
        notify_event(
            "Project created",
            [
                f"Name: {project['name']}",
                f"Status: {project['status']}",
                f"Completion: {project['completion']}%",
                f"Git: {project['git_url'] or 'Not linked'}",
            ],
        )
        flash("Project saved.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/projects/<int:project_id>")
    @admin_required
    def update_project(project_id: int):
        name = require_value("name", "Project name")
        if not name:
            return redirect(url_for("dashboard"))

        db = get_db()
        db.execute(
            """
            UPDATE projects
            SET name = ?, status = ?, git_url = ?, description = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                name,
                choose("status", PROJECT_STATUSES, "Active"),
                optional_value("git_url"),
                optional_value("description"),
                current_timestamp(),
                project_id,
            ),
        )
        db.commit()
        flash("Project updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/projects/<int:project_id>/delete")
    @admin_required
    def delete_project(project_id: int):
        db = get_db()
        db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        db.commit()
        flash("Project deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks")
    @login_required
    def create_task():
        title = require_value("title", "Task title")
        if not title:
            return redirect(url_for("dashboard"))

        now = current_timestamp()
        db = get_db()
        due_at = optional_datetime("due_at")
        assignee_user = resolve_task_assignee(db)
        assignee_user_id = assignee_user["id"] if assignee_user else None
        assignee_name = assignee_user["display_name"] if assignee_user else None
        cursor = db.execute(
            """
            INSERT INTO tasks (
                project_id, title, status, priority, due_at, assignee,
                assignee_user_id, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                optional_project_id(),
                title,
                choose("status", TASK_STATUSES, "Todo"),
                calculate_task_priority(due_at),
                due_at,
                assignee_name,
                assignee_user_id,
                optional_value("notes"),
                now,
                now,
            ),
        )
        refresh_project_completion(db)
        db.commit()
        task = load_task_detail(db, cursor.lastrowid)
        notify_task_assignment(task, "Task assigned" if assignee_user else "Task created")
        flash("Task saved.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<int:task_id>")
    @admin_required
    def update_task(task_id: int):
        title = require_value("title", "Task title")
        if not title:
            return redirect(url_for("dashboard"))

        db = get_db()
        old_task = load_task_detail(db, task_id)
        if old_task is None:
            flash("Task was not found.", "error")
            return redirect(url_for("dashboard"))

        due_at = optional_datetime("due_at")
        assignee_user = resolve_task_assignee(db)
        assignee_user_id = assignee_user["id"] if assignee_user else None
        assignee_name = assignee_user["display_name"] if assignee_user else None
        status = choose("status", TASK_STATUSES, "Todo")
        db.execute(
            """
            UPDATE tasks
            SET project_id = ?, title = ?, status = ?, priority = ?, due_at = ?,
                assignee = ?, assignee_user_id = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                optional_project_id(),
                title,
                status,
                calculate_task_priority(due_at),
                due_at,
                assignee_name,
                assignee_user_id,
                optional_value("notes"),
                current_timestamp(),
                task_id,
            ),
        )
        refresh_project_completion(db)
        db.commit()
        task = load_task_detail(db, task_id)
        if task is not None and old_task["status"] != task["status"]:
            notify_task_status_change(task, old_task["status"], task["status"])
        if task is not None and task_assignee_label(old_task) != task_assignee_label(task):
            notify_task_assignment(task, "Task reassigned")
        flash("Task updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<int:task_id>/status")
    @login_required
    def update_task_status(task_id: int):
        payload = request.get_json(silent=True) or {}
        status = payload.get("status") or request.form.get("status", "")
        if status not in TASK_STATUSES:
            if wants_json():
                return jsonify({"ok": False, "error": "Invalid status."}), 400
            flash("Invalid task status.", "error")
            return redirect(url_for("dashboard"))

        db = get_db()
        task = load_task_detail(db, task_id)
        if task is None:
            if wants_json():
                return jsonify({"ok": False, "error": "Task not found."}), 404
            flash("Task was not found.", "error")
            return redirect(url_for("dashboard"))

        if not can_change_task_status(task):
            if wants_json():
                return jsonify({"ok": False, "error": "Not allowed."}), 403
            flash("You can only move tasks assigned to you.", "error")
            return redirect(url_for("dashboard"))

        db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, current_timestamp(), task_id),
        )
        refresh_project_completion(db)
        db.commit()
        updated_task = load_task_detail(db, task_id)
        if updated_task is not None and task["status"] != status:
            notify_task_status_change(updated_task, task["status"], status)

        if wants_json():
            return jsonify({"ok": True, "status": status})

        flash("Task moved.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<int:task_id>/delete")
    @admin_required
    def delete_task(task_id: int):
        db = get_db()
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        refresh_project_completion(db)
        db.commit()
        flash("Task deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/meetings")
    @admin_required
    def create_meeting():
        title = require_value("title", "Meeting title")
        starts_at = optional_datetime("starts_at")
        if not title or not starts_at:
            if not starts_at:
                flash("Meeting start time is required.", "error")
            return redirect(url_for("dashboard"))

        now = current_timestamp()
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO meetings (
                project_id, title, status, starts_at, ends_at, location,
                attendees, agenda, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                optional_project_id(),
                title,
                choose("status", MEETING_STATUSES, "Planned"),
                starts_at,
                optional_datetime("ends_at"),
                optional_value("location"),
                optional_value("attendees"),
                optional_value("agenda"),
                now,
                now,
            ),
        )
        db.commit()
        meeting = db.execute(
            """
            SELECT meetings.*, projects.name AS project_name
            FROM meetings
            LEFT JOIN projects ON projects.id = meetings.project_id
            WHERE meetings.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        notify_event(
            "Meeting scheduled",
            [
                f"Meeting: {meeting['title']}",
                f"Project: {meeting['project_name'] or 'Unassigned'}",
                f"Starts: {meeting['starts_at']}",
                f"Location: {meeting['location'] or 'Not set'}",
            ],
        )
        flash("Meeting saved.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/meetings/<int:meeting_id>")
    @admin_required
    def update_meeting(meeting_id: int):
        title = require_value("title", "Meeting title")
        starts_at = optional_datetime("starts_at")
        if not title or not starts_at:
            if not starts_at:
                flash("Meeting start time is required.", "error")
            return redirect(url_for("dashboard"))

        db = get_db()
        db.execute(
            """
            UPDATE meetings
            SET project_id = ?, title = ?, status = ?, starts_at = ?, ends_at = ?,
                location = ?, attendees = ?, agenda = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                optional_project_id(),
                title,
                choose("status", MEETING_STATUSES, "Planned"),
                starts_at,
                optional_datetime("ends_at"),
                optional_value("location"),
                optional_value("attendees"),
                optional_value("agenda"),
                current_timestamp(),
                meeting_id,
            ),
        )
        db.commit()
        meeting = load_meeting_detail(db, meeting_id)
        if meeting is not None:
            notify_event(
                "Meeting updated",
                [
                    f"Meeting: {meeting['title']}",
                    f"Project: {meeting['project_name'] or 'Unassigned'}",
                    f"Starts: {meeting['starts_at']}",
                    f"Location: {meeting['location'] or 'Not set'}",
                ],
            )
        flash("Meeting updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/meetings/<int:meeting_id>/delete")
    @admin_required
    def delete_meeting(meeting_id: int):
        db = get_db()
        db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        db.commit()
        flash("Meeting deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/alerts/test")
    @admin_required
    def test_alert():
        sent, message = send_discord_alert(
            "Project manager test",
            ["Discord alerts are connected."],
        )
        flash(message, "success" if sent else "warning")
        return redirect(url_for("dashboard"))

    @app.post("/alerts/due")
    @admin_required
    def send_due_alerts():
        db = get_db()
        refresh_derived_fields(db)
        db.commit()
        horizon = (
            datetime.now() + timedelta(hours=current_app.config["ALERT_WINDOW_HOURS"])
        ).strftime(DATETIME_FORMAT)

        tasks = db.execute(
            """
            SELECT tasks.*, projects.name AS project_name, projects.git_url AS project_git_url
            FROM tasks
            LEFT JOIN projects ON projects.id = tasks.project_id
            WHERE tasks.status != 'Done'
                AND tasks.due_at IS NOT NULL
                AND tasks.due_at <= ?
                AND tasks.discord_alerted_at IS NULL
            ORDER BY tasks.due_at ASC
            """,
            (horizon,),
        ).fetchall()
        meetings = db.execute(
            """
            SELECT meetings.*, projects.name AS project_name, projects.git_url AS project_git_url
            FROM meetings
            LEFT JOIN projects ON projects.id = meetings.project_id
            WHERE meetings.status = 'Planned'
                AND meetings.starts_at <= ?
                AND meetings.discord_alerted_at IS NULL
            ORDER BY meetings.starts_at ASC
            """,
            (horizon,),
        ).fetchall()

        lines = build_due_alert_lines(tasks, meetings)
        if not lines:
            result = {"sent": False, "message": "No due alerts found.", "count": 0}
            return alert_response(result, "info")

        sent, message = send_discord_alert("Due soon", lines)
        if sent:
            alert_time = current_timestamp()
            db.executemany(
                "UPDATE tasks SET discord_alerted_at = ? WHERE id = ?",
                [(alert_time, task["id"]) for task in tasks],
            )
            db.executemany(
                "UPDATE meetings SET discord_alerted_at = ? WHERE id = ?",
                [(alert_time, meeting["id"]) for meeting in meetings],
            )
            db.commit()

        result = {"sent": sent, "message": message, "count": len(lines)}
        return alert_response(result, "success" if sent else "warning")

    @app.post("/alerts/scheduled")
    @admin_required
    def send_scheduled_alerts():
        db = get_db()
        result = run_scheduled_discord_jobs(db)
        return alert_response(
            {
                "sent": result["sent"] > 0,
                "message": f"Scheduled alerts checked. Sent {result['sent']}.",
                "count": result["sent"],
            },
            "success" if result["sent"] else "info",
        )

    @app.post("/users")
    @admin_required
    def create_user():
        username = require_value("username", "Username")
        password = require_value("password", "Password")
        display_name = require_value("display_name", "Display name")
        if not username or not password or not display_name:
            return redirect(url_for("dashboard"))

        db = get_db()
        if db.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone():
            flash("That username already exists.", "error")
            return redirect(url_for("dashboard"))

        now = current_timestamp()
        db.execute(
            """
            INSERT INTO users (
                username, display_name, role, position, password_hash,
                discord_user_id, active, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
            """,
            (
                username,
                display_name,
                choose("role", USER_ROLES, "employee"),
                optional_value("position"),
                generate_password_hash(password),
                optional_value("discord_user_id"),
                now,
                now,
            ),
        )
        db.commit()
        flash("User added.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/users/<int:user_id>")
    @admin_required
    def update_user(user_id: int):
        display_name = require_value("display_name", "Display name")
        if not display_name:
            return redirect(url_for("dashboard"))

        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            flash("User was not found.", "error")
            return redirect(url_for("dashboard"))

        role = choose("role", USER_ROLES, "employee")
        if user["role"] == "admin" and role != "admin" and active_admin_count(db) <= 1:
            flash("Keep at least one active admin.", "error")
            return redirect(url_for("dashboard"))

        password = optional_value("password")
        if password:
            db.execute(
                """
                UPDATE users
                SET display_name = ?, role = ?, position = ?, discord_user_id = ?,
                    password_hash = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    display_name,
                    role,
                    optional_value("position"),
                    optional_value("discord_user_id"),
                    generate_password_hash(password),
                    current_timestamp(),
                    user_id,
                ),
            )
        else:
            db.execute(
                """
                UPDATE users
                SET display_name = ?, role = ?, position = ?, discord_user_id = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    display_name,
                    role,
                    optional_value("position"),
                    optional_value("discord_user_id"),
                    current_timestamp(),
                    user_id,
                ),
            )
        db.execute(
            "UPDATE tasks SET assignee = ? WHERE assignee_user_id = ?",
            (display_name, user_id),
        )
        db.commit()
        flash("User updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/users/<int:user_id>/delete")
    @admin_required
    def delete_user(user_id: int):
        db = get_db()
        user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        if user is None:
            flash("User was not found.", "error")
            return redirect(url_for("dashboard"))
        if user_id == g.current_user["id"]:
            flash("You cannot deactivate your own account.", "error")
            return redirect(url_for("dashboard"))
        if user["role"] == "admin" and active_admin_count(db) <= 1:
            flash("Keep at least one active admin.", "error")
            return redirect(url_for("dashboard"))

        db.execute("UPDATE users SET active = 0, updated_at = ? WHERE id = ?", (current_timestamp(), user_id))
        db.execute("UPDATE tasks SET assignee_user_id = NULL WHERE assignee_user_id = ?", (user_id,))
        db.commit()
        flash("User deactivated.", "success")
        return redirect(url_for("dashboard"))


def load_logged_in_user() -> None:
    g.current_user = None
    user_id = session.get("user_id")
    if user_id is None:
        return

    user = get_db().execute(
        "SELECT * FROM users WHERE id = ? AND active = 1", (user_id,)
    ).fetchone()
    if user is None:
        session.clear()
        return
    g.current_user = user


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if g.get("current_user") is None:
            if wants_json():
                return jsonify({"ok": False, "error": "Login required."}), 401
            return redirect(url_for("login", next=request.full_path))
        return view(*args, **kwargs)

    return wrapped_view


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped_view(*args, **kwargs):
        if not is_admin():
            if wants_json():
                return jsonify({"ok": False, "error": "Admin required."}), 403
            flash("Only admins can do that.", "error")
            return redirect(url_for("dashboard"))
        return view(*args, **kwargs)

    return wrapped_view


def is_admin() -> bool:
    user = g.get("current_user")
    return bool(user and user["role"] == "admin")


def seed_default_admin(db: sqlite3.Connection) -> None:
    if db.execute("SELECT COUNT(*) FROM users").fetchone()[0]:
        return

    now = current_timestamp()
    db.execute(
        """
        INSERT INTO users (
            username, display_name, role, position, password_hash,
            discord_user_id, active, created_at, updated_at
        )
        VALUES (?, ?, 'admin', 'Administrator', ?, NULL, 1, ?, ?)
        """,
        (
            current_app.config["DEFAULT_ADMIN_USERNAME"],
            "Admin",
            generate_password_hash(current_app.config["DEFAULT_ADMIN_PASSWORD"]),
            now,
            now,
        ),
    )


def active_admin_count(db: sqlite3.Connection) -> int:
    return db.execute(
        "SELECT COUNT(*) FROM users WHERE role = 'admin' AND active = 1"
    ).fetchone()[0]


def parse_bool(value: str | bool | None) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() not in {"0", "false", "no", "off"}


def refresh_derived_fields(db: sqlite3.Connection) -> None:
    refresh_automatic_priorities(db)
    refresh_project_completion(db)


def refresh_automatic_priorities(db: sqlite3.Connection) -> None:
    for task in db.execute("SELECT id, due_at, priority FROM tasks").fetchall():
        priority = calculate_task_priority(task["due_at"])
        if task["priority"] != priority:
            db.execute(
                "UPDATE tasks SET priority = ? WHERE id = ?",
                (priority, task["id"]),
            )


def calculate_task_priority(due_at: str | None) -> str:
    due_date = parse_datetime(due_at)
    if due_date is None:
        return "Low"

    hours_left = (due_date - datetime.now()).total_seconds() / 3600
    if hours_left <= 24:
        return "Critical"
    if hours_left <= 72:
        return "High"
    if hours_left <= 168:
        return "Normal"
    return "Low"


def refresh_project_completion(db: sqlite3.Connection) -> None:
    db.execute(
        """
        UPDATE projects
        SET completion = CAST(COALESCE(ROUND((
            SELECT AVG(
                CASE tasks.status
                    WHEN 'Done' THEN 100
                    WHEN 'Doing' THEN 50
                    ELSE 0
                END
            )
            FROM tasks
            WHERE tasks.project_id = projects.id
        )), 0) AS INTEGER)
        """
    )


def resolve_task_assignee(db: sqlite3.Connection) -> sqlite3.Row | None:
    if not is_admin():
        return g.current_user

    raw_value = request.form.get("assignee_user_id", "").strip()
    if not raw_value:
        return None

    try:
        assignee_user_id = int(raw_value)
    except ValueError:
        flash("Selected assignee was not found, so the task was saved unassigned.", "warning")
        return None

    user = db.execute(
        "SELECT * FROM users WHERE id = ? AND active = 1", (assignee_user_id,)
    ).fetchone()
    if user is None:
        flash("Selected assignee was not found, so the task was saved unassigned.", "warning")
    return user


def load_task_detail(db: sqlite3.Connection, task_id: int) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT tasks.*, projects.name AS project_name, projects.git_url AS project_git_url,
            users.display_name AS assignee_name, users.username AS assignee_username,
            users.discord_user_id AS assignee_discord_user_id
        FROM tasks
        LEFT JOIN projects ON projects.id = tasks.project_id
        LEFT JOIN users ON users.id = tasks.assignee_user_id
        WHERE tasks.id = ?
        """,
        (task_id,),
    ).fetchone()


def load_meeting_detail(db: sqlite3.Connection, meeting_id: int) -> sqlite3.Row | None:
    return db.execute(
        """
        SELECT meetings.*, projects.name AS project_name, projects.git_url AS project_git_url
        FROM meetings
        LEFT JOIN projects ON projects.id = meetings.project_id
        WHERE meetings.id = ?
        """,
        (meeting_id,),
    ).fetchone()


def task_assignee_label(task: sqlite3.Row) -> str:
    return task["assignee_name"] or task["assignee"] or "Unassigned"


def task_assignee_mention(task: sqlite3.Row) -> str:
    label = task_assignee_label(task)
    discord_user_id = task["assignee_discord_user_id"]
    if discord_user_id:
        return f"<@{discord_user_id}> ({label})"
    return label


def can_change_task_status(task: sqlite3.Row) -> bool:
    if is_admin():
        return True

    current_user = g.current_user
    if task["assignee_user_id"] == current_user["id"]:
        return True

    return (
        task["assignee_user_id"] is None
        and (task["assignee"] or "").strip().lower()
        == current_user["display_name"].strip().lower()
    )


def notify_task_assignment(task: sqlite3.Row | None, title: str) -> None:
    if task is None:
        return

    notify_event(
        title,
        [
            f"Task: {task['title']}",
            f"Assigned to: {task_assignee_mention(task)}",
            f"Project: {task['project_name'] or 'Unassigned'}",
            f"Priority: {task['priority']}",
            f"Due: {task['due_at'] or 'No due date'}",
        ],
    )


def notify_task_status_change(
    task: sqlite3.Row, old_status: str, new_status: str
) -> None:
    notify_event(
        "Task moved",
        [
            f"Task: {task['title']}",
            f"Status: {old_status} -> {new_status}",
            f"Assigned to: {task_assignee_mention(task)}",
            f"Project: {task['project_name'] or 'Unassigned'}",
        ],
    )


def start_background_jobs(app: Flask) -> None:
    if app.config.get("TESTING") or not app.config.get("ENABLE_BACKGROUND_JOBS"):
        return
    if app.extensions.get("project_manager_jobs_started"):
        return

    app.extensions["project_manager_jobs_started"] = True

    def worker() -> None:
        while True:
            try:
                with app.app_context():
                    db = get_db()
                    run_scheduled_discord_jobs(db)
                    db.commit()
            except Exception as exc:  # pragma: no cover - background guardrail
                app.logger.warning("Scheduled Discord job failed: %s", exc)
            time.sleep(app.config["AUTOMATION_POLL_SECONDS"])

    thread = threading.Thread(target=worker, name="discord-scheduler", daemon=True)
    thread.start()


def run_scheduled_discord_jobs(db: sqlite3.Connection) -> dict[str, int]:
    refresh_derived_fields(db)
    sent = 0
    now = datetime.now()
    sent += send_meeting_reminders(db, now)
    sent += send_daily_greeting(db, now)
    db.commit()
    return {"sent": sent}


def send_meeting_reminders(db: sqlite3.Connection, now: datetime) -> int:
    starts_before = (now + timedelta(minutes=5)).strftime(DATETIME_FORMAT)
    starts_after = now.strftime(DATETIME_FORMAT)
    meetings = db.execute(
        """
        SELECT meetings.*, projects.name AS project_name, projects.git_url AS project_git_url
        FROM meetings
        LEFT JOIN projects ON projects.id = meetings.project_id
        WHERE meetings.status = 'Planned'
            AND meetings.starts_at >= ?
            AND meetings.starts_at <= ?
            AND meetings.reminder_sent_at IS NULL
        ORDER BY meetings.starts_at ASC
        """,
        (starts_after, starts_before),
    ).fetchall()

    sent_count = 0
    for meeting in meetings:
        sent, _message = send_discord_alert(
            "Meeting starts in 5 minutes",
            [
                f"Meeting: {meeting['title']}",
                f"Project: {meeting['project_name'] or 'Unassigned'}",
                f"Starts: {meeting['starts_at']}",
                f"Location: {meeting['location'] or 'Not set'}",
                f"Login: {current_app.config['APP_PUBLIC_URL']}",
            ],
        )
        if sent:
            db.execute(
                "UPDATE meetings SET reminder_sent_at = ? WHERE id = ?",
                (current_timestamp(), meeting["id"]),
            )
            sent_count += 1
    return sent_count


def send_daily_greeting(db: sqlite3.Connection, now: datetime) -> int:
    greeting_time = parse_clock_time(current_app.config["DAILY_GREETING_TIME"])
    if greeting_time is None:
        return 0

    scheduled_at = now.replace(
        hour=greeting_time.hour, minute=greeting_time.minute, second=0, microsecond=0
    )
    grace_until = scheduled_at + timedelta(
        minutes=current_app.config["DAILY_GREETING_GRACE_MINUTES"]
    )
    if not scheduled_at <= now <= grace_until:
        return 0

    run_name = f"daily_greeting:{now.date().isoformat()}"
    if db.execute("SELECT 1 FROM automation_runs WHERE name = ?", (run_name,)).fetchone():
        return 0

    stats = load_stats(db)
    sent, _message = send_discord_alert(
        "Good morning",
        [
            f"Open tasks: {stats['open_tasks']}",
            f"Planned meetings: {stats['planned_meetings']}",
            f"Login: {current_app.config['APP_PUBLIC_URL']}",
        ],
    )
    if not sent:
        return 0

    db.execute(
        "INSERT INTO automation_runs (name, last_run_at) VALUES (?, ?)",
        (run_name, current_timestamp()),
    )
    return 1


def parse_clock_time(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%H:%M")
    except ValueError:
        return None


def load_stats(db: sqlite3.Connection) -> dict[str, int]:
    return {
        "active_projects": db.execute(
            "SELECT COUNT(*) FROM projects WHERE status = 'Active'"
        ).fetchone()[0],
        "open_tasks": db.execute(
            "SELECT COUNT(*) FROM tasks WHERE status != 'Done'"
        ).fetchone()[0],
        "planned_meetings": db.execute(
            "SELECT COUNT(*) FROM meetings WHERE status = 'Planned'"
        ).fetchone()[0],
        "avg_completion": db.execute(
            "SELECT COALESCE(ROUND(AVG(completion)), 0) FROM projects"
        ).fetchone()[0],
    }


def load_task_chart(db: sqlite3.Connection) -> dict:
    counts = {status: 0 for status in TASK_STATUSES}
    for row in db.execute("SELECT status, COUNT(*) AS count FROM tasks GROUP BY status"):
        if row["status"] in counts:
            counts[row["status"]] = row["count"]

    total = sum(counts.values())
    cursor = 0
    segments = []
    for status in TASK_STATUSES:
        count = counts[status]
        degrees = round((count / total) * 360) if total else 0
        next_cursor = cursor + degrees
        key = status.lower()
        if count:
            segments.append(f"var(--status-{key}) {cursor}deg {next_cursor}deg")
        cursor = next_cursor

    gradient = ", ".join(segments) if segments else "var(--status-empty) 0deg 360deg"

    return {
        "counts": counts,
        "total": total,
        "todo": counts["Todo"],
        "doing": counts["Doing"],
        "done": counts["Done"],
        "done_percent": round((counts["Done"] / total) * 100) if total else 0,
        "doing_percent": round((counts["Doing"] / total) * 100) if total else 0,
        "gradient": gradient,
    }


def load_calendar_month(meetings: list[sqlite3.Row]) -> dict:
    today = datetime.now().date()
    month_calendar = calendar.Calendar(firstweekday=0)
    meetings_by_date: dict[str, list[dict]] = {}
    for meeting in meetings:
        starts_at = parse_datetime(meeting["starts_at"])
        if starts_at is None:
            continue

        key = starts_at.date().isoformat()
        meetings_by_date.setdefault(key, []).append(
            {
                "id": meeting["id"],
                "project_id": meeting["project_id"],
                "project_name": meeting["project_name"] or "Unassigned",
                "project_git_url": meeting["project_git_url"] or "",
                "title": meeting["title"],
                "status": meeting["status"],
                "starts_at": meeting["starts_at"],
                "ends_at": meeting["ends_at"] or "",
                "time": starts_at.strftime("%H:%M"),
                "location": meeting["location"] or "",
                "attendees": meeting["attendees"] or "",
                "agenda": meeting["agenda"] or "",
            }
        )

    weeks = []
    for week in month_calendar.monthdatescalendar(today.year, today.month):
        weeks.append(
            [
                {
                    "date": day.isoformat(),
                    "number": day.day,
                    "in_month": day.month == today.month,
                    "is_today": day == today,
                    "meetings": meetings_by_date.get(day.isoformat(), []),
                }
                for day in week
            ]
        )

    return {
        "label": today.strftime("%B %Y"),
        "weekdays": ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"],
        "weeks": weeks,
    }


def load_project_summaries(
    db: sqlite3.Connection, projects: list[sqlite3.Row]
) -> list[dict]:
    summaries: list[dict] = []
    for project in projects:
        task_counts = db.execute(
            """
            SELECT
                COUNT(*) AS total_tasks,
                SUM(CASE WHEN status = 'Done' THEN 1 ELSE 0 END) AS done_tasks,
                SUM(CASE WHEN status != 'Done' THEN 1 ELSE 0 END) AS open_tasks
            FROM tasks
            WHERE project_id = ?
            """,
            (project["id"],),
        ).fetchone()
        next_meeting = db.execute(
            """
            SELECT starts_at
            FROM meetings
            WHERE project_id = ? AND status = 'Planned'
            ORDER BY starts_at ASC
            LIMIT 1
            """,
            (project["id"],),
        ).fetchone()
        total_tasks = task_counts["total_tasks"] or 0
        done_tasks = task_counts["done_tasks"] or 0
        task_completion = round((done_tasks / total_tasks) * 100) if total_tasks else 0
        completion = clamp_percent(project["completion"])
        summaries.append(
            {
                "id": project["id"],
                "name": project["name"],
                "status": project["status"],
                "completion": completion,
                "task_completion": task_completion,
                "total_tasks": total_tasks,
                "done_tasks": done_tasks,
                "open_tasks": task_counts["open_tasks"] or 0,
                "git_url": project["git_url"] or "",
                "description": project["description"] or "",
                "next_meeting": next_meeting["starts_at"] if next_meeting else "",
            }
        )
    return summaries


def build_due_alert_lines(tasks: list[sqlite3.Row], meetings: list[sqlite3.Row]) -> list[str]:
    lines: list[str] = []
    for task in tasks:
        project = task["project_name"] or "Unassigned"
        git = f" | {task['project_git_url']}" if task["project_git_url"] else ""
        lines.append(
            f"Task: {task['title']} | {project} | {task['priority']} | due {task['due_at']}{git}"
        )

    for meeting in meetings:
        project = meeting["project_name"] or "Unassigned"
        git = f" | {meeting['project_git_url']}" if meeting["project_git_url"] else ""
        lines.append(
            f"Meeting: {meeting['title']} | {project} | starts {meeting['starts_at']}{git}"
        )

    return lines


def send_discord_alert(title: str, lines: list[str]) -> tuple[bool, str]:
    webhook_url = current_app.config.get("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook_url:
        return False, "Discord webhook is not configured."

    content = "**{}**\n{}".format(title, "\n".join(f"- {line}" for line in lines))
    payload = json.dumps({"content": content[:1900]}).encode("utf-8")
    req = urlrequest.Request(
        webhook_url,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "project-manager-flask/1.0",
        },
        method="POST",
    )

    try:
        with urlrequest.urlopen(req, timeout=10) as response:
            if 200 <= response.status < 300:
                return True, "Discord alert sent."
            return False, f"Discord returned HTTP {response.status}."
    except error.HTTPError as exc:
        return False, f"Discord returned HTTP {exc.code}."
    except error.URLError as exc:
        return False, f"Discord alert failed: {exc.reason}."


def notify_event(title: str, lines: list[str]) -> None:
    if not current_app.config.get("DISCORD_WEBHOOK_URL"):
        return

    sent, message = send_discord_alert(title, lines)
    if not sent:
        flash(message, "warning")


def alert_response(result: dict, category: str):
    if wants_json():
        return jsonify(result)

    flash(result["message"], category)
    return redirect(url_for("dashboard"))


def wants_json() -> bool:
    return "application/json" in request.headers.get("Accept", "")


def current_timestamp() -> str:
    return datetime.now().strftime(DATETIME_FORMAT)


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, DATETIME_FORMAT)
    except ValueError:
        return None


def optional_value(field: str) -> str | None:
    value = request.form.get(field, "").strip()
    return value or None


def require_value(field: str, label: str) -> str | None:
    value = optional_value(field)
    if not value:
        flash(f"{label} is required.", "error")
    return value


def choose(field: str, allowed: tuple[str, ...], default: str) -> str:
    value = request.form.get(field, default)
    return value if value in allowed else default


def optional_percent(field: str, default: int) -> int:
    raw_value = request.form.get(field, str(default)).strip()
    try:
        return clamp_percent(int(raw_value))
    except ValueError:
        return default


def clamp_percent(value: int) -> int:
    return max(0, min(100, value))


def optional_project_id() -> int | None:
    raw_value = request.form.get("project_id", "").strip()
    if not raw_value:
        return None
    try:
        project_id = int(raw_value)
    except ValueError:
        return None

    project = get_db().execute(
        "SELECT id FROM projects WHERE id = ?", (project_id,)
    ).fetchone()
    if project is None:
        flash("Selected project was not found, so the item was saved unassigned.", "warning")
        return None
    return project_id


def optional_datetime(field: str) -> str | None:
    value = optional_value(field)
    if value is None:
        return None

    try:
        datetime.strptime(value, DATETIME_FORMAT)
    except ValueError:
        flash(f"{field.replace('_', ' ').title()} must be a valid date and time.", "error")
        return None
    return value


if __name__ == "__main__":
    create_app().run(debug=True, use_reloader=False)
