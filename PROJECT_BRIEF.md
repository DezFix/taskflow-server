# TaskFlow Server — full project case

> An online IT department management system: tasks, staff, positions, roles, a
> chat that transcribes voice messages, photo reports, and a web interface.
> Deployed on a server inside any office.

---

## 1. Purpose

A company/office installs one server of its own. All department staff connect to
it from their phones (Android) or browsers (Web) and work in a single space.

Three connection scenarios (all implemented):

| Scenario | Server address | Example |
|---|---|---|
| Public domain behind Cloudflare | `https://taskflow.example.com` | `https://` |
| VPN into the office | `https://10.10.0.5:8443` (LAN certificate) | self-signed TLS |
| Local network without TLS | `http://192.168.1.50:8080` | cleartext, trusted network only |

On first launch the app asks for the server address, checks availability, and
remembers it. Several addresses can be saved and switched between.

## 2. Two workspaces

One codebase, two modes — the switch happens based on user permissions.

**Department head (manager)**
- Staff: creation, blocking, role and position changes, password reset
- Positions: creation, renaming, archiving
- Roles: creation, permission matrix, assignment to staff
- All department tasks: creation, assignment, deadlines, priorities, control
- Chats: all conversations, group creation
- Reports: summary by tasks and staff, export
- Settings: profile, AI transcription options, backups

**Staff member (executor)**
- Profile: photo, name, position, contacts
- My tasks: list, filters, status change, comments, photo report
- Chat: direct dialogs and groups, voice messages
- Notifications about new tasks, deadlines, messages
- Does not see management sections or other people's tasks, except the ones
  assigned to them

## 3. Roles and permissions

A flexible permission matrix rather than three rigid roles. Each role is a set
of boolean permissions.

**System roles are created on first start (they cannot be deleted):**

- `Администратор` (Administrator) — all permissions, including system settings
- `Глава отдела` (Department head) — department management, without system
  settings
- `Сотрудник` (Staff member) — own tasks, chat, profile only

**Permissions (41 of them), grouped:**

```
users.view, users.create, users.edit, users.delete, users.reset_password
positions.view, positions.create, positions.edit, positions.delete
roles.view, roles.create, roles.edit, roles.delete
tasks.view_all, tasks.create, tasks.edit_any, tasks.edit_assigned,
tasks.delete, tasks.assign, tasks.reports
chat.direct, chat.group, chat.group_create
files.upload, files.view_all
voice.transcribe
settings.view, settings.edit, settings.manage_roles,
settings.backup, settings.audit_log
```

Permission checks are centralised in the `require_perm(...)` dependency. Each
endpoint declares the permission it needs declaratively, and the list ends up
in OpenAPI and in `GET /api/v1/meta/permissions`.

## 4. Functional blocks

### 4.1 Authentication
- Sign in with a username (or email) and password
- Passwords: Argon2id, a complexity policy, lockout after N failures
- JWT: access (short, 30 min) + refresh (30 days, stored in the DB, rotated)
- Devices can be listed and their refresh token revoked
- A password change requires the current one; changing all sessions is an option
- First launch: the setup wizard creates an administrator

### 4.2 Staff and positions
- Staff member: full name, username, email, phone, position, roles, photo, status
- Position: title, description, order, active/archived
- Creating a staff member issues a temporary password, shown once
- Soft delete: `is_active=False`, task and chat history is preserved

### 4.3 Tasks
- Fields: title, description, assignee, author, status, priority, deadline,
  tags, project/folder, time label
- Statuses: `new`, `in_progress`, `review`, `done`, `cancelled`
- Priorities: `low`, `normal`, `high`, `urgent`
- Comments with attachments
- A status change is recorded in the history (`task_history`) — a change log
- Filters: by assignee, status, priority, deadline, tag, text
- Cursor pagination for large lists

### 4.4 Photo reports on completed work
- A file is attached to a task or a comment
- Storage on disk, metadata in the DB
- Types: photo, document, audio; size limit (25 MB by default)
- Served via a short token link, access controlled by permissions
- Preview compression for large photos

### 4.5 Chat
- Direct dialogs (1-on-1) and groups
- Messages: text, images, files, **voice**
- Delivery states: sent, delivered, read
- Time labels, editing, deletion for everyone
- Unread counters in the chat list
- Realtime via WebSocket

### 4.6 Voice messages and AI transcription
- A voice message is recorded in the app and uploaded to the server
- The server transcribes it in the background via **faster-whisper**
  (CTranslate2)
- Default model `base`, `small`/`medium` available — chosen in settings
- Works **offline**, with no cloud and no API keys
- Transcription goes `pending` → `done` / `failed`
- A push arrives in the chat when the text is ready
- A separate `transcripts` entity holds text, language, duration, model
- Audio is normalised purely in Python (PyAV), with no external ffmpeg
- Model cache in `data/models/`, downloaded on first use

### 4.7 Web interface
- The same Flutter code, built to static files (`flutter build web`)
- The server serves the static files at `/`, SPA routing via fallback
- Signing in from a browser uses the same account as the app
- WebSocket works on the same platform

### 4.8 Notifications
- A WebSocket channel for realtime
- Local push notifications on Android (flutter_local_notifications)
- Inside the app — counters and badges

### 4.9 Audit and backup
- `audit_log`: who, what, when, from which IP
- Manual backup of SQLite/state into an archive, download, automatic restore

## 5. Architecture

```
taskflow-server/
├── app/
│   ├── main.py              entry point, lifespan, static file mounting
│   ├── config.py            settings (env, pydantic-settings)
│   ├── database.py          engine, session, async wrapper
│   ├── security.py          JWT, Argon2, tokens
│   ├── deps.py              FastAPI dependencies (current_user, require_perm)
│   ├── errors.py            single error format
│   ├── models/              SQLAlchemy models
│   │   ├── base.py user.py position.py role.py task.py chat.py
│   │   ├── file.py voice.py audit.py
│   ├── schemas/             Pydantic schemas (request/response)
│   ├── api/                 routers
│   │   ├── v1/
│   │   │   ├── auth.py users.py positions.py roles.py
│   │   │   ├── tasks.py comments.py chat.py messages.py
│   │   │   ├── files.py voice.py reports.py admin.py
│   │   ├── ws.py            WebSocket hub
│   ├── services/            business logic
│   │   ├── auth.py tasks.py chat.py files.py
│   │   ├── voice.py         Whisper orchestration
│   │   ├── reports.py backup.py audit.py
│   ├── realtime/            ConnectionManager, events
│   ├── seed.py              initial data
│   └── web/                 serving Flutter Web
├── alembic/                 migrations
├── tests/                   pytest
├── data/                    database, files, models (in .gitignore)
├── Dockerfile
├── docker-compose.yml
└── pyproject.toml
```

**Layers:** `api` (HTTP) → `services` (rules) → `models` (data). Services know
nothing about HTTP — only about business rules. This lets the same functions be
reused from WebSocket handlers and background jobs.

## 6. Data model

```
users ──┬── user_roles ── roles
        ├── position_id ── positions
        ├── assigned_tasks / created_tasks ── tasks
        └── chat_participants ── chat_members ── chats ── messages

tasks ──┬── task_comments ── (files)
        ├── task_history
        └── task_tags ── tags

messages ──┬── files
            └── transcripts (voice, 1:1)

sessions (refresh tokens), audit_log, settings_kv, attachments
```

Key decisions:
- Primary keys are string UUIDs (compatible with any backend, do not leak
  counters)
- Time is UTC, naive in the DB, serialised to ISO-8601 with `Z`
- Soft delete via `is_active` / `deleted_at`
- JSON columns for role permissions and file metadata (portable across DBs)

## 7. Database compatibility

The code works through SQLAlchemy 2.0, the driver is chosen from `DATABASE_URL`:

```env
DATABASE_URL=sqlite+aiosqlite:///./data/taskflow.db     # default
DATABASE_URL=postgresql+asyncpg://user:pass@host/db   # PostgreSQL
DATABASE_URL=mysql+aiomysql://user:pass@host/db       # MySQL/MariaDB
```

Column types are chosen abstractly, the Alembic migrations are identical. For
SQLite, WAL and `foreign_keys=ON` are enabled — otherwise foreign keys and
concurrent writes behave incorrectly.

## 8. Transport and security

- Static files and API on one port — one process, one database, one address
- CORS: by default only mirrors of the current origin, the list is extended in
  `.env`
- WebSocket: JWT checked in the query parameter, heartbeat every 25 seconds
- Rate limiting on `/auth/*` (built-in, no Redis)
- Uploads: extension allowlist, size limit, random names on disk
- The JWT secret is generated on first start and stored in `data/secret.key`,
  in plaintext only in dev mode

## 9. API (main)

All routes are under `/api/v1`.

```
POST   /auth/setup                 first-launch wizard
POST   /auth/login                 sign in
POST   /auth/refresh               token refresh
POST   /auth/logout                sign out
POST   /auth/logout-all            end all sessions
GET    /auth/me                    current user
POST   /auth/change-password

GET    /users                      staff list
POST   /users                      create staff
GET    /users/{id}                 card
PATCH  /users/{id}                 edit
POST   /users/{id}/reset-password  password reset
POST   /users/{id}/deactivate      deactivation

GET    /positions   POST /positions   PATCH/DELETE /positions/{id}

GET    /roles   POST /roles   PATCH/DELETE /roles/{id}
GET    /meta/permissions             catalogue of all permissions

GET    /tasks                    list with filters
POST   /tasks                    create
GET    /tasks/{id}               card with history
PATCH  /tasks/{id}               edit
POST   /tasks/{id}/status        change status
POST   /tasks/{id}/assign        assign an assignee
GET    /tasks/{id}/comments
POST   /tasks/{id}/comments     with attachments

GET    /chats                    chat list with unread counts
POST   /chats/direct             start a direct dialog
POST   /chats/groups             create a group
GET    /chats/{id}               details
GET    /chats/{id}/messages      pagination
POST   /chats/{id}/messages      send (multipart: text/file/voice)
POST   /chats/{id}/read          mark as read

POST   /files                    upload (multipart)
GET    /files/{id}               metadata
GET    /files/{id}/download      content
GET    /files/{id}/preview       compressed preview

GET    /voice/{message_id}       transcription status
POST   /voice/{message_id}/retry retry
GET    /voice/settings           model options
PUT    /voice/settings           change options

GET    /reports/tasks-summary    task summary
GET    /reports/user-load        staff workload
GET    /reports/export           CSV export

GET    /admin/audit              action log
POST   /admin/backup             create a backup
GET    /admin/backups            backup list
GET    /ws                       realtime channel
```

Documentation: `/docs` (Swagger), `/redoc`, the OpenAPI schema at
`/openapi.json`.

## 10. Realtime events

WebSocket `/api/v1/ws?token=JWT`. The server sends:

```json
{"type": "message.created",   "data": {...}}
{"type": "message.read",      "data": {...}}
{"type": "task.created",      "data": {...}}
{"type": "task.updated",      "data": {...}}
{"type": "transcript.ready",  "data": {...}}
{"type": "user.updated",      "data": {...}}
{"type": "ping"}
```

The Android and web clients use the same protocol.

## 11. Testing

- **Server:** pytest, an isolated database per test run, fixtures for roles and
  users. Coverage: authentication, the permission matrix, tasks, chat, files,
  voice, reports. The goal is all tests green, with no skips and no data leaking
  between tests.
- **Client:** `flutter test` — unit tests for models, state managers, view
  models, the date parser and serialisation.

## 12. Delivery

- **Docker:** `docker compose up` brings up the server with its database and the
  web static files
- **CI:** GitHub Actions — server tests, client tests, APK build in Releases
- **Keystore:** debug — generated automatically; release — passwords in Secrets
- **Licence:** MIT
- **Repositories:** `DezFix/taskflow` (client), `DezFix/taskflow-server`
  (server)

## 13. Development stages

| Stage | Content | Status |
|---|---|---|
| 1 | Skeleton, configs, database, models, migrations | base ready |
| 2 | Authentication, roles, permissions | sign-in works |
| 3 | Users, positions, roles in UI and API | department management |
| 4 | Tasks, comments, history | tasks work |
| 5 | Files, photo reports | reports work |
| 6 | Chat + WebSocket | chat works |
| 7 | Voice + Whisper | voice works |
| 8 | Reports, audit, backup | administration |
| 9 | Flutter client: all screens | app ready |
| 10 | Tests, CI, Docker, APK build | ready to deploy |

## 14. Deliberately out of the MVP

- LLM assistant (summaries, task extraction) — the extension point is in place
- Push via Firebase — local notifications only
- Offline mode with synchronisation
- Several departments in one installation
- Video calls
