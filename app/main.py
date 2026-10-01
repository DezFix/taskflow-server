"""Точка входа сервера TaskFlow."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.api.v1 import (
    admin,
    auth,
    chat,
    files,
    meta,
    roles,
    tasks,
    users,
    voice,
)
from app.api.ws import router as ws_router
from app.config import get_settings
from app.database import Base, dispose_engine, get_engine, get_sessionmaker
from app.errors import (
    AppError,
    app_error_handler,
    http_error_handler,
    unhandled_error_handler,
    validation_error_handler,
)
from app.logging_setup import configure_logging, get_logger
from app.schemas import ErrorResponse

logger = get_logger("startup")

API_PREFIX = "/api/v1"
API_VERSION = "1.0.0"

DESCRIPTION = """
Сервер **TaskFlow** — задачи IT-отдела, сотрудники, чат с распознаванием
голосовых сообщений и веб-интерфейс.

### Как пользоваться API

1. `GET /api/v1/meta/info` — проверка адреса сервера. Клиент вызывает его,
   когда сотрудник вводит IP или домен.
2. `POST /api/v1/auth/setup` — первоначальная настройка: создаёт администратора
   и системные роли. Доступен, пока в системе нет ни одного пользователя.
3. `POST /api/v1/auth/login` — вход по логину или email, возвращает пару токенов.
4. Дальше передаётся заголовок `Authorization: Bearer <access_token>`.

### Реалтайм

WebSocket `ws(s)://<host>/api/v1/ws?token=<access_token>`.
Сервер шлёт события `message.created`, `message.read`, `task.updated`,
`transcript.ready` и другие. Клиент может слать `ping`, `read` и `typing`.

### Права

Права выдаются ролями сотрудника. Каталог всех прав —
`GET /api/v1/meta/permissions`, права текущего пользователя — `GET /api/v1/auth/me`.
Каждый защищённый эндпоинт объявляет требуемое право в поле
`x-required-permissions` в OpenAPI.
"""

#: Документация и схема API по умолчанию закрыты.
#:
#: Анонимный посетитель получал полную карту: все пути, все имена полей и
#: требуемое право на каждом эндпоинте. Для планирования атаки этого
#: достаточно, а включать схему можно администратору осознанно.
EXPOSE_API_DOCS = get_settings().expose_api_docs

app = FastAPI(
    title="TaskFlow Server",
    description=DESCRIPTION,
    version=API_VERSION,
    docs_url="/docs" if EXPOSE_API_DOCS else None,
    redoc_url="/redoc" if EXPOSE_API_DOCS else None,
    openapi_url="/openapi.json" if EXPOSE_API_DOCS else None,
    license_info={"name": "MIT"},
    responses={
        400: {"model": ErrorResponse, "description": "Ошибка запроса"},
        401: {"model": ErrorResponse, "description": "Требуется авторизация"},
        403: {"model": ErrorResponse, "description": "Недостаточно прав"},
        404: {"model": ErrorResponse, "description": "Не найдено"},
        413: {"model": ErrorResponse, "description": "Файл слишком большой"},
        422: {"model": ErrorResponse, "description": "Ошибка валидации"},
    },
)

# --- Единый формат ошибок для всего приложения ---
app.add_exception_handler(AppError, app_error_handler)
app.add_exception_handler(StarletteHTTPException, http_error_handler)
app.add_exception_handler(RequestValidationError, validation_error_handler)
app.add_exception_handler(Exception, unhandled_error_handler)


@app.middleware("http")
async def security_headers(request: Request, call_next):  # noqa: ANN001, ANN201
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    if request.url.path.startswith(API_PREFIX):
        # Данные API не кэшируем: иначе после смены прав клиент увидит старое.
        response.headers.setdefault("Cache-Control", "no-store")
    return response


def install_middlewares() -> None:
    app.add_middleware(GZipMiddleware, minimum_size=1024)

    origins = get_settings().cors_origin_list
    if origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["Content-Disposition", "X-Total-Count"],
        )
        return

    # Список origins не задан: отражаем Origin, когда он совпадает с хостом
    # запроса. Этого достаточно для веб-клиента с того же адреса, а для
    # посторонних источников CORS-заголовки не выдаются вовсе.
    @app.middleware("http")
    async def same_origin_cors(request: Request, call_next):  # noqa: ANN001, ANN201
        if request.method == "OPTIONS":
            response = await call_next(request)
        else:
            response = await call_next(request)

        origin = request.headers.get("origin")
        if not origin:
            return response

        parsed = urlparse(origin)
        if parsed.netloc == request.headers.get("host", ""):
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
            response.headers["Vary"] = "Origin"
        return response


# --- Роутеры API ---
app.include_router(meta.router, prefix=API_PREFIX)
app.include_router(auth.router, prefix=API_PREFIX)
app.include_router(users.router, prefix=API_PREFIX)
app.include_router(roles.router, prefix=API_PREFIX)
app.include_router(tasks.router, prefix=API_PREFIX)
app.include_router(chat.router, prefix=API_PREFIX)
app.include_router(files.router, prefix=API_PREFIX)
app.include_router(voice.router, prefix=API_PREFIX)
app.include_router(admin.router, prefix=API_PREFIX)
app.include_router(ws_router, prefix=API_PREFIX)


# --- Раздача веб-клиента (Flutter Web) ---

#: Служебные файлы Flutter Web отдаём точечно, а не каталогом целиком.
WEB_SERVICE_FILES = (
    "flutter_service_worker.js",
    "manifest.json",
    "version.json",
    "favicon.png",
    "icons",
)


def mount_web() -> bool:
    """Подключает раздачу собранного Flutter Web. False — статики пока нет."""
    settings = get_settings()
    web_root = settings.web_path
    if web_root is None or not web_root.exists() or not (web_root / "index.html").exists():
        logger.info(
            "web_not_mounted",
            path=str(web_root) if web_root else "WEB_ROOT не задан",
        )
        return False

    assets = web_root / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    index = web_root / "index.html"

    for name in WEB_SERVICE_FILES:
        target = web_root / name
        if target.is_file():

            @app.get(f"/{name}", include_in_schema=False)
            async def _serve_service_file(_target: Path = target):  # noqa: ANN202
                return FileResponse(_target, headers={"Cache-Control": "no-cache"})

    @app.get("/", include_in_schema=False)
    async def web_index():  # noqa: ANN202
        return FileResponse(
            index,
            # index.html не кэшируем, иначе после обновления сервера
            # клиент продолжит грузить старую версию приложения.
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/{full_path:path}", include_in_schema=False)
    async def web_fallback(full_path: str):  # noqa: ANN202
        """SPA-роутинг: неизвестный путь отдаёт index.html, иначе перезагрузка
        страницы ломала бы навигацию. Пути API и документации не перехватываем."""
        head = full_path.split("/", 1)[0]
        if full_path.startswith(("api/", "docs", "redoc", "openapi.json")):
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": "Страница не найдена"}},
            )
        if head in {"assets", "docs", "redoc"}:
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": "Ресурс не найден"}},
            )

        candidate = (web_root / full_path).resolve()
        try:
            candidate.relative_to(web_root.resolve())
        except ValueError:
            # Попытка выйти за пределы каталога статики.
            return JSONResponse(
                status_code=404,
                content={"error": {"code": "not_found", "message": "Ресурс не найден"}},
            )
        if candidate.is_file():
            return FileResponse(candidate)

        return FileResponse(index, headers={"Cache-Control": "no-cache"})

    logger.info("web_mounted", path=str(web_root))
    return True


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    settings = get_settings()
    settings.ensure_dirs()

    # Схема создаётся автоматически: для офисного развёртывания это удобнее,
    # чем ручной запуск миграций. Alembic остаётся для серьёзных изменений.
    async with get_engine().begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    from app.services import seed as seed_service
    from app.services.voice_engine import engine as voice_engine
    from app.services.voice_jobs import queue as voice_queue

    sessionmaker = get_sessionmaker()
    async with sessionmaker() as session:
        await seed_service.seed_all(session)
        await session.commit()

    voice_queue.bind(sessionmaker)
    await voice_queue.start()

    recovered = 0
    with contextlib.suppress(Exception):
        # Незавершённые распознавания после перезапуска возвращаем в очередь.
        recovered = await voice_queue.recover_orphans(sessionmaker)

    logger.info(
        "server_started",
        host=settings.host,
        port=settings.port,
        database=settings.database_url.split("://")[0],
        sqlite=settings.is_sqlite,
        voice_enabled=settings.voice_enabled,
        voice_model=settings.voice_model,
        recovered_voice_jobs=recovered,
    )

    warmup: asyncio.Task | None = None
    if settings.voice_enabled:
        # Модель грузится в фоне: первый запрос не должен ждать скачивание.
        warmup = asyncio.create_task(voice_engine.warmup())

    try:
        yield
    finally:
        if warmup is not None:
            warmup.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await warmup
        await voice_queue.stop()
        await dispose_engine()
        logger.info("server_stopped")


app.router.lifespan_context = lifespan
install_middlewares()
WEB_ENABLED = mount_web()


@app.get("/", include_in_schema=False)
async def root():  # noqa: ANN202
    """Корень: веб-клиент либо подсказка, как подключиться к API."""
    if WEB_ENABLED:
        return FileResponse(
            get_settings().web_path / "index.html",  # type: ignore[operator]
            headers={"Cache-Control": "no-cache"},
        )
    return {
        "service": "TaskFlow Server",
        "version": API_VERSION,
        "docs": "/docs",
        "api": API_PREFIX,
        "websocket": f"{API_PREFIX}/ws",
        "hint": ("Веб-клиент не собран. Выполните flutter build web и укажите каталог в WEB_ROOT."),
    }
