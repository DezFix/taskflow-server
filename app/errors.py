"""Единый формат ошибок API."""

from __future__ import annotations

from typing import Any

from fastapi import HTTPException, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse


class AppError(HTTPException):
    """Ошибка с машиночитаемым кодом в теле ответа."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        details: Any = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=message)
        self.code = code
        self.message = message
        self.details = details

    def to_body(self) -> dict[str, Any]:
        body: dict[str, Any] = {"error": {"code": self.code, "message": self.message}}
        if self.details is not None:
            body["error"]["details"] = self.details
        return body


def bad_request(code: str, message: str, details: Any = None) -> AppError:
    return AppError(status.HTTP_400_BAD_REQUEST, code, message, details)


def unauthorized(code: str = "unauthorized", message: str = "Требуется авторизация") -> AppError:
    return AppError(status.HTTP_401_UNAUTHORIZED, code, message)


def forbidden(
    code: str = "forbidden", message: str = "Недостаточно прав", details: Any = None
) -> AppError:
    return AppError(status.HTTP_403_FORBIDDEN, code, message, details)


def not_found(code: str, message: str) -> AppError:
    return AppError(status.HTTP_404_NOT_FOUND, code, message)


def conflict(code: str, message: str) -> AppError:
    return AppError(status.HTTP_409_CONFLICT, code, message)


def too_large(message: str) -> AppError:
    return AppError(413, "file_too_large", message)


def too_many_requests(code: str, message: str, details: Any = None) -> AppError:
    return AppError(status.HTTP_429_TOO_MANY_REQUESTS, code, message, details)


async def app_error_handler(_request: Request, exc: AppError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content=exc.to_body())


async def http_error_handler(_request: Request, exc: HTTPException) -> JSONResponse:
    detail = exc.detail
    if isinstance(detail, dict) and "code" in detail:
        return JSONResponse(status_code=exc.status_code, content=detail)
    message = detail if isinstance(detail, str) else "Ошибка запроса"
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": "http_error", "message": message}},
    )


async def validation_error_handler(
    _request: Request, exc: RequestValidationError
) -> JSONResponse:
    # Pydantic-ошибки переводим в компактный вид: поле -> сообщения.
    fields: dict[str, list[str]] = {}
    for err in exc.errors():
        loc = err.get("loc") or ()
        key = ".".join(str(p) for p in loc[1:]) or "body"
        fields.setdefault(key, []).append(str(err.get("msg", "некорректное значение")))
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "validation_error",
                "message": "Проверьте правильность заполнения полей",
                "details": fields,
            }
        },
    )


async def unhandled_error_handler(_request: Request, exc: Exception) -> JSONResponse:
    from sqlalchemy.exc import OperationalError

    from app.logging_setup import get_logger

    logger = get_logger("errors")

    # Временная блокировка базы — это не поломка: операция не выполнена,
    # её можно повторить. Отдаём 503 с честным кодом, а не 500.
    if isinstance(exc, OperationalError) and any(
        word in str(exc).lower() for word in ("locked", "busy")
    ):
        logger.warning("database_busy", error=str(exc))
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": {
                    "code": "database_busy",
                    "message": "База данных занята другой операцией. Повторите попытку.",
                }
            },
        )

    logger.error("unhandled_exception", error=str(exc), exc_info=exc)
    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={"error": {"code": "internal_error", "message": "Внутренняя ошибка сервера"}},
    )
