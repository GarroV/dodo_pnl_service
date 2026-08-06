"""
Регистрация доменных enum-типов в драйвере.

Зачем: psycopg знает только встроенные типы. Колонку `accounting_layer` он
отдаёт строкой (повезло), а массив `accounting_layer[]` — строкой целиком:
`'{white,grey,black}'` вместо списка. Проверено на живой базе 2026-08-07,
через сырой psycopg и через ORM одинаково.

Молча это не ломается — ломается позже и не здесь: `roles.visible_layers`
уходит в контракт блока auth как `visible_ledgers: list[str]`, и вместо списка
регистров пришла бы строка, по которой `in` работает по символам. Поэтому типы
регистрируются на каждом соединении, а не там, где вспомнят.
"""
from __future__ import annotations

from psycopg.types import TypeInfo

# Типы, созданные миграцией 0001_types. Массивы нужны не всем, но регистрация
# дешевле, чем разбираться, почему поле стало строкой.
ENUM_TYPES = (
    "accounting_layer",
    "payout_channel",
    "allocation_method",
    "period_status",
    "payrun_status",
    "rule_scope",
)


def register_enum_types(conn) -> int:
    """Научить соединение читать наши enum-типы и массивы из них.

    Один запрос на соединение вместо TypeInfo.fetch на каждый тип: oid у типов
    свои в каждой базе, кешировать их между базами нельзя.
    """
    rows = conn.execute(
        "select typname, oid, typarray from pg_type where typname = any(%s)",
        (list(ENUM_TYPES),),
    ).fetchall()
    for name, oid, array_oid in rows:
        TypeInfo(name, oid, array_oid).register(conn)
    return len(rows)


def on_connection_created(sender, connection, **kwargs) -> None:
    """Обработчик сигнала Django: у соединения ORM тот же драйвер."""
    if connection.vendor != "postgresql":
        return
    register_enum_types(connection.connection)
