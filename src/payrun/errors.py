"""Отказы расчёта.

Отдельный модуль, чтобы выбор правил (`rules`) не зависел от расчёта (`calc`),
а сообщение было пригодно для показа человеку: расчёт отказывает по причинам,
которые исправляет пользователь, а не программист.
"""
from __future__ import annotations


class PayrunRefused(Exception):
    """Расчёт не выполнен, и причина понятна человеку.

    `http_status` — чем ответить в интерфейсе: разные причины требуют разных
    действий, и по коду ответа это должно быть видно снаружи.
    """

    http_status = 409

    def __init__(self, message: str, *, details: list[str] | None = None):
        super().__init__(message)
        self.message = message
        self.details = details or []


class LedgerAccessDenied(PayrunRefused):
    """Роль не видит регистры, в которые расчёт собирается писать.

    Это не каприз интерфейса: `insert ... returning` проверяется политиками
    `select`, поэтому запись всё равно упала бы — но ошибкой базы, без
    объяснения.
    """

    http_status = 403
