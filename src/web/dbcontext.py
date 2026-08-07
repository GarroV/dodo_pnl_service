"""
Контекст пользователя в базе — единственное место, где приложение
представляется базе конкретным человеком.

Как это работает и почему именно так:

1. `set local role app_user` — приложение ходит ролью без права обходить
   политики. Без этого политики RLS не действуют на владельца таблиц, и любую
   проверку доступа можно написать неправильно, получив зелёные тесты.
2. `select set_config('app.user_id', ..., true)` — третий аргумент `true`
   означает «только до конца транзакции». Обычный `SET` пережил бы транзакцию
   и утёк бы на следующего пользователя того же соединения из пула.

Ошибка здесь не даёт ошибки в интерфейсе — она даёт тихую утечку данных,
поэтому LOCAL обязателен в обоих операторах.

Отдельная тонкость про `ATOMIC_REQUESTS`: он оборачивает в транзакцию только
вызов представления, а middleware работает **вне** её. `set local` из обычного
middleware ушёл бы в автокоммит и не подействовал бы. Поэтому транзакцию
открывает само middleware, а `ATOMIC_REQUESTS` остаётся вторым контуром —
внутри он становится вложенной точкой сохранения.

Это временный дом кода: контекст пользователя — работа блока `auth`. Здесь он
живёт, пока настоящего входа нет.
"""
from __future__ import annotations

from django.db import transaction

APP_ROLE = "app_user"
USER_SETTING = "app.user_id"


def set_db_context(connection, user_id: str | None) -> None:
    """Представиться базе. `user_id=None` — контекста нет, выборки будут пустыми."""
    with connection.cursor() as cursor:
        cursor.execute(f"set local role {APP_ROLE}")
        # Пустая строка, а не NULL: app_user_id() приводит её к null сама
        # (nullif), а параметр драйвера должен остаться строкой.
        cursor.execute(
            "select set_config(%s, %s, true)", (USER_SETTING, str(user_id or "")),
        )


class DbContextMiddleware:
    """Оборачивает запрос в транзакцию и выставляет в ней контекст пользователя.

    Порядок важен: контекст выставляется до первого обращения к данным, поэтому
    ни одно представление не может случайно прочитать что-то мимо политик.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .devauth import current_user_id

        with transaction.atomic():
            from django.db import connection

            set_db_context(connection, current_user_id(request))
            return self.get_response(request)
