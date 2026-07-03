# DreamBroad

A white, blue, and green Python Flask web app for managing projects, tasks, meeting calendars, Git/Drive links, and Discord alerts.

## Features

- Single Add button for creating projects, tasks, or meetings from one dialog.
- Project tracking with status, completion percentage, description, and optional Git/Drive URLs.
- Professional kanban board with Todo, Doing, and Done lanes.
- Drag and drop cards between lanes, with the status saved immediately.
- Search, project, member, and priority filters for daily board focus.
- Clickable project list with a completion pie chart and inline project editor.
- Task status pie chart showing the current workflow mix.
- Compact monthly schedule calendar with meetings and task due dates shown on their dates.
- Login with admin, client, and employee roles, user positions, project assignment, and optional Discord user IDs.
- Admins can assign tasks to anyone; project clients can manage tasks and meetings only inside their assigned project; employees can create their own tasks and move only their assigned tasks.
- Task priority is calculated from the due date automatically.
- Project completion is calculated from project task status: Todo, Doing, and Done.
- Task tracking with project assignment, status, due date, assignee, and notes.
- Meeting calendar management with start/end times, location, attendees, and agenda.
- Discord webhook alerts through admin, per-project, and employee channels. Admin gets everything, each project webhook gets only that project's updates, and the employee channel gets assignment messages only.
- Background Discord reminders for meetings starting in 5 minutes and a daily 9:00 greeting with the login link.
- Manual due-soon alert sender for open tasks and planned meetings inside the configured alert window.
- SQLite storage with no external database service required.

## Setup

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` or set environment variables before running the app. Keep the admin and employee webhooks in `.env`; project/client webhooks are saved on each project from the app.

```powershell
$env:SECRET_KEY = "replace-this"
$env:ADMIN_DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
$env:EMP_DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
$env:ALERT_WINDOW_HOURS = "24"
$env:APP_PUBLIC_URL = "http://127.0.0.1:5000"
$env:DEFAULT_ADMIN_USERNAME = "replace this"
$env:DEFAULT_ADMIN_PASSWORD = "replace this"
```

On the first run, the app creates a default admin user from `DEFAULT_ADMIN_USERNAME` and `DEFAULT_ADMIN_PASSWORD`. Set real credentials in your local `.env`; do not commit real passwords.

## Run

```powershell
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

## Discord Alerts

The app sends Discord messages through admin and employee `.env` webhooks plus the webhook saved on each project:

- `ADMIN_DISCORD_WEBHOOK_URL`: receives every project, task, meeting, due-soon, reminder, and greeting alert.
- `EMP_DISCORD_WEBHOOK_URL`: receives task assignment/reassignment alerts only, not task movement alerts.
- Project webhook URL: entered while creating/editing a project; receives only that project's project/task/meeting updates.

- Project, task assignment, task movement, and meeting schedule events send alerts immediately when the matching webhook is configured.
- The radio tower button sends alerts for tasks and meetings due within `ALERT_WINDOW_HOURS`.
- The clock button manually runs scheduled checks.
- The background scheduler checks every `AUTOMATION_POLL_SECONDS` seconds.
- Meetings get a Discord reminder 5 minutes before start.
- The daily greeting runs at `DAILY_GREETING_TIME`, default `09:00`, and includes `APP_PUBLIC_URL` as the login link.
- The send button posts a test alert.

The due alert endpoint can also be called by a scheduled job:

```powershell
Invoke-WebRequest -Method Post -Uri http://127.0.0.1:5000/alerts/due
```

## Project Links

Each project has Git URL, Drive URL, and Project webhook URL fields. Add a GitHub, GitLab, Bitbucket, private repository, or Google Drive folder URL there and the dashboard will expose those links for the project, its tasks, and its meetings. Add the project's Discord webhook there so only that project's client channel receives that project's updates.

This project is intended to be pushed to [blackeyy22/Project_Manager](https://github.com/blackeyy22/Project_Manager).

## Tests

```powershell
pip install -r requirements-dev.txt
python -m pytest
```

If you only want the app runtime:

```powershell
pip install -r requirements.txt
```
