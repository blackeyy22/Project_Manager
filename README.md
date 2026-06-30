# Project Manager

A dark green and black Python Flask web app for managing projects, tasks, meeting calendars, Git repository links, and Discord alerts.

## Features

- Single Add button for creating projects, tasks, or meetings from one dialog.
- Project tracking with status, completion percentage, description, and optional Git URL.
- Clickable existing project list with a completion pie chart and inline project editor.
- Task status pie chart showing Todo, Doing, Blocked, and Done work.
- One task board grouped by Todo, Doing, Blocked, and Done.
- Full monthly meeting calendar with meetings shown on their dates.
- Task tracking with project assignment, priority, status, due date, assignee, and notes.
- Meeting calendar management with start/end times, location, attendees, and agenda.
- Discord webhook alerts when projects, tasks, or meetings are created.
- Manual due-soon alert sender for open tasks and planned meetings inside the configured alert window.
- SQLite storage with no external database service required.

## Setup

```powershell
python -m venv .venv
.\\.venv\\Scripts\\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
```

Edit `.env` or set environment variables before running the app. Keep real webhooks in `.env`; it is ignored by Git.

```powershell
$env:SECRET_KEY = "replace-this"
$env:DISCORD_WEBHOOK_URL = "https://discord.com/api/webhooks/..."
$env:ALERT_WINDOW_HOURS = "24"
```

## Run

```powershell
python app.py
```

Open [http://127.0.0.1:5000](http://127.0.0.1:5000).

## Discord Alerts

The app sends Discord messages through `DISCORD_WEBHOOK_URL`.

- Create events send alerts immediately when the webhook is configured.
- The radio tower button sends alerts for tasks and meetings due within `ALERT_WINDOW_HOURS`.
- The send button posts a test alert.

The due alert endpoint can also be called by a scheduled job:

```powershell
Invoke-WebRequest -Method Post -Uri http://127.0.0.1:5000/alerts/due
```

## Git Links

Each project has a Git URL field. Add a GitHub, GitLab, Bitbucket, or private repository URL there and the dashboard will expose a Git link for the project, its tasks, and its meetings.

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
