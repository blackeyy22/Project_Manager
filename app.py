from __future__ import annotations

import calendar
import json
import os
import sqlite3
from datetime import datetime, timedelta
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
    url_for,
)


BASE_DIR = Path(__file__).resolve().parent
DEFAULT_DB_PATH = BASE_DIR / "data" / "project_manager.sqlite3"
DATETIME_FORMAT = "%Y-%m-%dT%H:%M"

PROJECT_STATUSES = ("Planned", "Active", "Paused", "Done")
TASK_STATUSES = ("Todo", "Doing", "Done")
TASK_PRIORITIES = ("Low", "Normal", "High", "Critical")
MEETING_STATUSES = ("Planned", "Held", "Cancelled")

SCHEMA = """
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
    notes TEXT,
    discord_alerted_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
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
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE SET NULL
);
"""


def create_app(test_config: dict | None = None) -> Flask:
    load_env_file()

    app = Flask(__name__)
    app.config.from_mapping(
        DATABASE_PATH=os.environ.get("DATABASE_PATH", str(DEFAULT_DB_PATH)),
        DISCORD_WEBHOOK_URL=os.environ.get("DISCORD_WEBHOOK_URL", ""),
        ALERT_WINDOW_HOURS=int(os.environ.get("ALERT_WINDOW_HOURS", "24")),
        SECRET_KEY=os.environ.get("SECRET_KEY", "dev-secret-change-me"),
    )

    if test_config:
        app.config.update(test_config)

    app.teardown_appcontext(close_db)

    with app.app_context():
        init_db()

    register_routes(app)
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

    legacy_task_statuses = {
        "Review": "Doing",
        "Blocked": "Todo",
    }
    for old_status, new_status in legacy_task_statuses.items():
        db.execute(
            "UPDATE tasks SET status = ? WHERE status = ?",
            (new_status, old_status),
        )


def register_routes(app: Flask) -> None:
    @app.get("/")
    def dashboard():
        db = get_db()
        projects = db.execute(
            "SELECT * FROM projects ORDER BY status != 'Active', updated_at DESC"
        ).fetchall()
        tasks = db.execute(
            """
            SELECT tasks.*, projects.name AS project_name, projects.git_url AS project_git_url
            FROM tasks
            LEFT JOIN projects ON projects.id = tasks.project_id
            ORDER BY
                tasks.status = 'Done',
                tasks.due_at IS NULL,
                tasks.due_at ASC,
                tasks.priority = 'Critical' DESC,
                tasks.updated_at DESC
            """
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

        return render_template(
            "index.html",
            calendar_month=calendar_month,
            discord_ready=discord_ready,
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
        )

    @app.post("/projects")
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
                optional_percent("completion", 0),
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
    def update_project(project_id: int):
        name = require_value("name", "Project name")
        if not name:
            return redirect(url_for("dashboard"))

        db = get_db()
        db.execute(
            """
            UPDATE projects
            SET name = ?, status = ?, completion = ?, git_url = ?, description = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                name,
                choose("status", PROJECT_STATUSES, "Active"),
                optional_percent("completion", 0),
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
    def delete_project(project_id: int):
        db = get_db()
        db.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        db.commit()
        flash("Project deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks")
    def create_task():
        title = require_value("title", "Task title")
        if not title:
            return redirect(url_for("dashboard"))

        now = current_timestamp()
        db = get_db()
        cursor = db.execute(
            """
            INSERT INTO tasks (
                project_id, title, status, priority, due_at, assignee, notes, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                optional_project_id(),
                title,
                choose("status", TASK_STATUSES, "Todo"),
                choose("priority", TASK_PRIORITIES, "Normal"),
                optional_datetime("due_at"),
                optional_value("assignee"),
                optional_value("notes"),
                now,
                now,
            ),
        )
        db.commit()
        task = db.execute(
            """
            SELECT tasks.*, projects.name AS project_name
            FROM tasks
            LEFT JOIN projects ON projects.id = tasks.project_id
            WHERE tasks.id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()
        notify_event(
            "Task created",
            [
                f"Task: {task['title']}",
                f"Project: {task['project_name'] or 'Unassigned'}",
                f"Priority: {task['priority']}",
                f"Due: {task['due_at'] or 'No due date'}",
            ],
        )
        flash("Task saved.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<int:task_id>")
    def update_task(task_id: int):
        title = require_value("title", "Task title")
        if not title:
            return redirect(url_for("dashboard"))

        db = get_db()
        db.execute(
            """
            UPDATE tasks
            SET project_id = ?, title = ?, status = ?, priority = ?, due_at = ?,
                assignee = ?, notes = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                optional_project_id(),
                title,
                choose("status", TASK_STATUSES, "Todo"),
                choose("priority", TASK_PRIORITIES, "Normal"),
                optional_datetime("due_at"),
                optional_value("assignee"),
                optional_value("notes"),
                current_timestamp(),
                task_id,
            ),
        )
        db.commit()
        flash("Task updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<int:task_id>/status")
    def update_task_status(task_id: int):
        payload = request.get_json(silent=True) or {}
        status = payload.get("status") or request.form.get("status", "")
        if status not in TASK_STATUSES:
            if wants_json():
                return jsonify({"ok": False, "error": "Invalid status."}), 400
            flash("Invalid task status.", "error")
            return redirect(url_for("dashboard"))

        db = get_db()
        db.execute(
            "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
            (status, current_timestamp(), task_id),
        )
        db.commit()

        if wants_json():
            return jsonify({"ok": True, "status": status})

        flash("Task moved.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/tasks/<int:task_id>/delete")
    def delete_task(task_id: int):
        db = get_db()
        db.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
        db.commit()
        flash("Task deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/meetings")
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
        flash("Meeting updated.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/meetings/<int:meeting_id>/delete")
    def delete_meeting(meeting_id: int):
        db = get_db()
        db.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
        db.commit()
        flash("Meeting deleted.", "success")
        return redirect(url_for("dashboard"))

    @app.post("/alerts/test")
    def test_alert():
        sent, message = send_discord_alert(
            "Project manager test",
            ["Discord alerts are connected."],
        )
        flash(message, "success" if sent else "warning")
        return redirect(url_for("dashboard"))

    @app.post("/alerts/due")
    def send_due_alerts():
        db = get_db()
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
