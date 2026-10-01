# TaskFlow Server

The TaskFlow server side — an IT department management system: tasks, staff,
positions, roles, a chat that transcribes voice messages, photo reports, and a
web interface.

The server is installed once in the office, and staff connect to it from their
phones and browsers. The full project case and architecture are in
[PROJECT_BRIEF.md](PROJECT_BRIEF.md).

The client is a separate project: [DezFix/taskflow](https://github.com/DezFix/taskflow).

---

## Capabilities

| Area | Features |
|---|---|
| **Authentication** | Login or email, Argon2id passwords, JWT with refresh rotation, lockout after N failures, list of active sessions |
| **Staff** | Creation, blocking, transfer to another position, role changes, password reset with a temporary password issued |
| **Positions** | Creation, renaming, archiving, deletion blocked while in use |
| **Roles** | Arbitrary roles with a permission matrix, system roles cannot be deleted |
| **Tasks** | Statuses, priorities, deadlines, labels, comments, change history, cursor pagination |
| **Photo reports** | Photo and document upload to a task, the report moves the task to review |
| **Chat** | Direct dialogs and groups, attachments, read/delivered, editing, deletion |
| **Voice** | Voice messages, server-side recognition via faster-whisper, manual transcript editing |
| **Reports** | Task summary, staff workload, CSV export for Excel |
| **Administration** | Action log, backups, system information |
| **Web** | Serves the built Flutter web client, Swagger at `/docs` |

## Requirements

- Python 3.12 or newer (verified on 3.13)
- 2 GB RAM, 2 CPU cores — 4 cores recommended for recognition
- 1 GB of disk for data, 2–5 GB for the recognition model

## Quick start

### Docker (recommended)

```bash
git clone https://github.com/DezFix/taskflow-server.git
cd taskflow-server
docker compose up -d
```

The server comes up on `http://<machine-address>:8080`. The first visit is the
setup wizard: it creates the administrator and the system roles.

### Without Docker

```bash
python -m venv .venv
# Windows:  .venv\Scripts\activate
# Linux:    source .venv/bin/activate

pip install -e ".[dev]"

# On Windows, voice recognition needs the Visual C++ Redistributable;
# on Linux it needs ffmpeg and libsndfile1.

python -m app.cli check        # check the environment
python -m app.cli initdb       # create the schema and initial data
python -m app.cli serve        # start the server
```

## Configuration

All settings live in `.env` (create it from `.env.example`).

| Variable | Default | Purpose |
|---|---|---|
| `TASKFLOW_HOST` | `0.0.0.0` | Listen address |
| `TASKFLOW_PORT` | `8080` | Port |
| `DATABASE_URL` | `sqlite+aiosqlite:///./data/taskflow.db` | Database connection string |
| `JWT_SECRET` | generated | Token signing key, at least 32 characters |
| `ACCESS_TOKEN_MINUTES` | `30` | Access token lifetime |
| `REFRESH_TOKEN_DAYS` | `30` | Refresh token lifetime |
| `MAX_UPLOAD_MB` | `25` | Maximum file size |
| `VOICE_ENABLED` | `true` | Voice recognition |
| `VOICE_MODEL` | `base` | Model: `tiny`, `base`, `small`, `medium` |
| `VOICE_LANGUAGE` | `ru` | Recognition language |
| `WEB_ROOT` | — | Directory of the built Flutter web client |
| `CORS_ORIGINS` | — | Allowed origins, comma separated |

### Database

SQLite is the default, which is enough for a department of up to 10 people.
The code works with any RDBMS through SQLAlchemy, only one line changes:

```env
# PostgreSQL
DATABASE_URL=postgresql+asyncpg://taskflow:password@localhost:5432/taskflow

# MySQL / MariaDB
DATABASE_URL=mysql+aiomysql://taskflow:password@localhost:3306/taskflow
```

For external databases, install the drivers:

```bash
pip install asyncpg      # PostgreSQL
pip install aiomysql     # MySQL
```

## Voice recognition

Runs entirely on the server, with no cloud and no API keys. Recordings go
nowhere.

| Model | Size | Speed (CPU) | When to pick it |
|---|---|---|---|
| `tiny` | 75 MB | very fast | drafts, short notes |
| `base` | 145 MB | fast | **default** — a working compromise |
| `small` | 480 MB | moderate | accuracy matters, short queues |
| `medium` | 1.5 GB | slow | maximum accuracy, overnight jobs |

The model is downloaded once on first use and cached in `data/models/`. To
fetch it in advance:

```bash
python -m app.cli warmup
```

Change the model on the fly: `PUT /api/v1/voice/settings` or through the web
interface.

If Whisper got the text wrong, the sender can fix it by hand — the edit is
marked and is no longer overwritten.

> Note: the PyAV version is pinned to `av>=11,<16`. PyAV 16 removed the
> `metadata_errors` parameter that faster-whisper uses.

## External access

Three real scenarios — the app supports all three:

| Scenario | Address | Server setup |
|---|---|---|
| Domain behind Cloudflare or nginx | `https://taskflow.example.com` | reverse proxy with a certificate |
| VPN into the office | `https://10.0.0.5:8080` | self-signed certificate, the client checks the fingerprint |
| Local network | `http://192.168.1.50:8080` | Nothing, it is a trusted network |

CORS by default only allows mirrors of the current address, which is enough
for the web client served from the same server. Set the list of external
origins in `CORS_ORIGINS`.

## Commands

```bash
python -m app.cli serve              # start the server
python -m app.cli serve --reload     # start with reload on changes
python -m app.cli check              # check the environment
python -m app.cli initdb             # create the schema and initial data
python -m app.cli admin              # create an administrator
python -m app.cli admin --username ivan  # reset a staff password
python -m app.cli backup             # backup (database + files)
python -m app.cli warmup             # fetch the recognition model
```

## API documentation

Once the server is running:

- Swagger UI — <http://localhost:8080/docs>
- ReDoc — <http://localhost:8080/redoc>
- Schema — <http://localhost:8080/openapi.json>

The order of working with the API:

1. `GET /api/v1/meta/info` — address check (the client calls it when an IP or
   domain is entered)
2. `POST /api/v1/auth/setup` — initial setup
3. `POST /api/v1/auth/login` — sign in, returns a token pair
4. After that, the `Authorization: Bearer <access_token>` header

Realtime: `wss://<host>/api/v1/ws?token=<access_token>`.

## Development

```bash
pip install -e ".[dev]"

pytest -q                       # tests
pytest --cov=app --cov-report=term-missing
ruff check app tests            # code style
python scripts/smoke.py         # end-to-end API check against a live server
python scripts/voice_check.py   # voice recognition check
python scripts/voice_check.py --strict-accuracy  # fail on recognition mismatch
python scripts/check_schema.py  # table composition in the database
```

`voice_check.py` verifies the decoding and resampling pipeline and the
transcription queue. Recognition accuracy on synthesised speech depends on the
synthesiser and the model, so by default a mismatch is reported as a warning.
Pass `--strict-accuracy` locally to turn it into a failure.

### Migrations

The schema is created automatically on startup. Alembic is needed for serious
structural changes:

```bash
alembic revision --autogenerate -m "description"
alembic upgrade head
alembic downgrade -1
```

### Structure

```
app/
├── main.py          entry point, lifespan, serving the web client
├── config.py        settings
├── database.py      engine, sessions, base classes
├── security.py      Argon2, JWT
├── deps.py          FastAPI dependencies and permission checks
├── errors.py        single error format
├── models/          SQLAlchemy models (18 tables)
├── schemas/         Pydantic request and response schemas
├── api/v1/          routers (89 endpoints)
├── api/ws.py        realtime WebSocket channel
├── services/        business logic
├── realtime/        connection hub and events
├── serializers.py   converting models to schemas
├── permissions.py   permission catalogue and system roles
└── cli.py           command line
```

Layers: `api` (HTTP) → `services` (rules) → `models` (data). Services know
nothing about HTTP, which is why the same functions are used by background jobs
and WebSocket handlers.

## Backup

Automatic: `POST /api/v1/admin/backup`. Manually:

```bash
python -m app.cli backup
```

The archive contains `database/taskflow.db` and all uploaded files. For SQLite
the copy is taken through a WAL checkpoint, so the file is always consistent.

Restore: stop the server, replace `data/taskflow.db` with the file from the
archive, and put back the `data/storage` directory. Files can also be restored
through `POST /api/v1/admin/backups/{id}/restore`.

For regular backups, set up a scheduler task:

```bash
# Windows: Task Scheduler
python -m app.cli backup
# Linux: crontab
0 2 * * * cd /opt/taskflow && python -m app.cli backup
```

## Security

- Passwords are stored as irreversible Argon2id hashes
- Refresh tokens are stored in the database only as SHA-256: a database leak
  does not grant access
- Changing the password revokes all active sessions
- Account lockout for 15 minutes after 10 failed attempts
- Uploads are restricted by an extension allowlist, and the file signature is
  verified (a renamed `.exe` will not pass as `.jpg`)
- On-disk file names are random, the path is never taken from the name
- SQL injection is impossible: only parameterised SQLAlchemy queries are used
- CORS is closed to third-party origins by default
- Permissions are checked centrally, every endpoint declares what it requires
- The action log records who did what and when

Worth doing before public internet access:

- Set your own `JWT_SECRET`
- Put the server behind an HTTPS proxy with a proper certificate
- Configure `CORS_ORIGINS` if the web client lives on another domain
- Restrict access to the port with a firewall or VPN

## Languages

The API speaks language-neutral error codes (`task_access_denied`), and the
client renders them in the language of the interface. Server-side text stays in
Russian for logs and API debugging, so responses are not duplicated per locale.

Names that live in the database need care. System roles keep a Russian `key`
because it is stored and used in permission checks, and changing it would break
existing installations. The API therefore adds `i18n_key` to role responses:
`'admin'`, `'head'` or `'staff'`, independent of the storage language. Custom
roles return `null` and their titles are shown as entered.

`Accept-Language` is not used: adding a language to the server would multiply
every message in the API, while the codes stay the same for every locale.

## Licence

MIT. See [LICENSE](LICENSE).

## Contributing

Send changes through a pull request. Before submitting:

```bash
ruff check app tests
pytest -q
```
