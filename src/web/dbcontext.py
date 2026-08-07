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

Три вещи, на которых это держится, и каждая проверяется, а не предполагается:

- **Транзакция обязана быть открыта.** Вне транзакции Postgres на
  `set_config(..., true)` лишь пишет предупреждение в свой лог и ничего не
  делает: роль осталась прежней, контекст пуст, приложение поехало дальше.
  Поэтому здесь отказ (`ContextOutsideTransaction`), а не надежда.
  `ATOMIC_REQUESTS` тут не помощник: он оборачивает только вызов
  представления, а middleware работает вне его — транзакцию открываем сами,
  и `ATOMIC_REQUESTS` становится вложенной точкой сохранения.
- **Выставленное перечитывается обратно.** «Выполнили два оператора» и
  «контекст стоит» — разные утверждения, и разошлись бы они молча.
- **Контекст не переживает запрос по построению**, а не по дисциплине: он
  умирает вместе с транзакцией, что бы ни случилось внутри, и пул отдаёт
  следующему запросу чистое соединение.

Код без HTTP-запроса (фоновая задача, команда) выставляет контекст сам —
через `db_context`. Мимо него ходить нельзя: запрос пойдёт ролью подключения,
а она в разработке владеет таблицами, и RLS её не ограничивает.
"""
from __future__ import annotations

from contextlib import contextmanager

from django.db import transaction

APP_ROLE = "app_user"
USER_SETTING = "app.user_id"


class ContextOutsideTransaction(RuntimeError):
    """Контекст пытались выставить вне транзакции — он бы не подействовал."""


class ContextNotApplied(RuntimeError):
    """Контекст выставили, а база отдаёт другое. Работать дальше нельзя."""


def set_db_context(connection, user_id) -> None:
    """Представиться базе. `user_id=None` — контекста нет, выборки будут пустыми."""
    if not connection.in_atomic_block:
        raise ContextOutsideTransaction(
            "контекст пользователя выставляется только внутри транзакции: "
            "вне её Postgres молча игнорирует set_config(..., true)"
        )

    # Пустая строка, а не NULL: app_user_id() приводит её к null сама (nullif),
    # а параметр драйвера должен остаться строкой.
    expected = str(user_id or "")
    with connection.cursor() as cursor:
        cursor.execute(f"set local role {APP_ROLE}")
        cursor.execute("select set_config(%s, %s, true)", (USER_SETTING, expected))
        cursor.execute(
            "select current_setting(%s, true), current_user", (USER_SETTING,)
        )
        applied, role = cursor.fetchone()

    if applied != expected or role != APP_ROLE:
        raise ContextNotApplied(
            f"база работает под ролью {role!r}, а контекст пользователя не тот, "
            "что выставляли — запрос дальше не идёт"
        )


@contextmanager
def db_context(user_id, using: str = "default"):
    """Транзакция с выставленным контекстом — для кода без HTTP-запроса.

    Фоновой задаче некому выставить контекст: у неё нет запроса, а middleware
    не срабатывает. Без этого она пошла бы ролью подключения — то есть, в
    разработке, владельцем таблиц, которого политики не ограничивают.
    """
    from django.db import connections

    connection = connections[using]
    with transaction.atomic(using=using):
        set_db_context(connection, user_id)
        yield connection


class DbContextMiddleware:
    """Оборачивает запрос в транзакцию и выставляет в ней контекст пользователя.

    Порядок важен: контекст выставляется до первого обращения к данным, поэтому
    ни одно представление не может случайно прочитать что-то мимо политик.
    Личность берётся из сессии — в базу за ней идти нельзя, таблица учёток
    закрыта той же RLS.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        from .auth import drop_dev_session_if_disabled, session_user_id

        drop_dev_session_if_disabled(request)

        with transaction.atomic():
            from django.db import connection

            set_db_context(connection, session_user_id(request))
            return self.get_response(request)
