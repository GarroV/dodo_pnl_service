"""
Предохранитель демо-стенда: куда именно мы сейчас подключены.

Зачем он вообще нужен. Демо сбрасывается **пересозданием базы целиком** (D016):
`drop database` и `create database ... template ...`. Ошибка в адресе здесь
стоит не «неверных данных на экране», а стёртой базы партнёра с ФИО и суммами
живых людей. Такой ценой нельзя расплачиваться за опечатку в переменной
окружения, поэтому ни одна команда демо не выполняется, пока не доказано, что
она работает с демо-базой.

Доказательство двухслойное, и слои разные по природе — иначе они падали бы
вместе.

1. **Адрес** (`require_demo_dsn`). Подключение должно совпадать с
   `DEMO_DATABASE_URL` по трём вещам: хост, порт, имя базы. Это ловит запуск
   демо-команды не там, где надо: в экземпляре продукта `DATABASE_URL` показывает
   на боевую базу, и команда отказывается работать.

2. **Данные** (`require_demo_data`). В демо-базе есть ровно один партнёр —
   демонстрационный. База, где нашёлся чужой, демо-базой не является, чем бы ни
   были заполнены переменные.

Второй слой существует потому, что первого мало, и это стоит понимать точно. У
демо-экземпляра `DATABASE_URL` и `DEMO_DATABASE_URL` **равны** — это его
нормальное состояние, а не ошибка. Значит, опечатка «`DEMO_DATABASE_URL`
показывает на базу продукта» сравнением адресов не ловится в принципе: там тоже
всё совпадёт. Ловит её только взгляд в сами данные.

Отдельно про удаление. Пересоздать можно лишь **помеченную** базу: метку
(таблица `demo_stamp`) ставит эта же команда при создании или при первом
наполнении пустой базы. База, которую этот код не создавал и не наполнял, не
будет удалена, даже если все переменные показывают на неё.

Молчаливого «ну наверное это демо» нет ни в одном месте: не сошлось — отказ
словами.
"""
from __future__ import annotations

import os

from psycopg.conninfo import conninfo_to_dict

__all__ = [
    "DemoGuardRefused",
    "STAMP",
    "STAMP_TABLE",
    "database_name",
    "demo_dsn",
    "maintenance_dsn",
    "require_demo_data",
    "require_demo_dsn",
    "same_database",
    "target",
    "template_name",
]

# Комментарий, которым помечена демо-база. Значение — не украшение: по нему
# уборка отличает базу, созданную демо-командой, от любой другой.
STAMP = "dodo-pnl-demo"

# Где лежит метка: таблица в самой базе. Не комментарий к базе — комментарий
# ставится только текущей базе, то есть проверять его пришлось бы всё равно
# подключением внутрь. Раз так, метка честнее лежит в данных, где её видно
# глазами и где она копируется вместе с базой при создании из эталона.
STAMP_TABLE = "demo_stamp"

# База, к которой подключаются, чтобы создать или удалить другую: подключиться
# к удаляемой нельзя.
MAINTENANCE_DB = "postgres"


class DemoGuardRefused(RuntimeError):
    """Предохранитель не пустил: подключение не доказано демо-базой."""


def _norm_host(value: str) -> str:
    """localhost и 127.0.0.1 — один и тот же хост, пустой — сокет."""
    host = (value or "").strip()
    if not host:
        return "(socket)"
    return "127.0.0.1" if host in ("localhost", "::1") else host


def target(dsn: str) -> tuple[str, str, str]:
    """Куда показывает строка подключения: (хост, порт, база).

    Разбирается той же функцией, которой пользуется сам драйвер, — иначе
    сравнение расходилось бы с реальным подключением на форме `key=value`.
    """
    parts = conninfo_to_dict(dsn)
    return (
        _norm_host(str(parts.get("host", ""))),
        str(parts.get("port") or "5432"),
        str(parts.get("dbname") or ""),
    )


def database_name(dsn: str) -> str:
    return target(dsn)[2]


def same_database(one: str, other: str) -> bool:
    return target(one) == target(other)


def demo_dsn(env=None) -> str:
    """Адрес демо-базы. Пусто — отказ, а не умолчание.

    Умолчание здесь было бы худшим из решений: «забыли переменную» превратилось
    бы в «сбросили что-то другое».
    """
    env = os.environ if env is None else env
    dsn = (env.get("DEMO_DATABASE_URL") or "").strip()
    if not dsn:
        raise DemoGuardRefused(
            "не задан DEMO_DATABASE_URL — адреса демо-базы нет, и подставить его "
            "за человека нельзя: демо сбрасывается пересозданием базы целиком. "
            "Задайте переменную (см. .env.example)."
        )
    return dsn


def require_demo_dsn(current_dsn: str, env=None) -> str:
    """Проверить, что `current_dsn` — это и есть демо-база. Вернуть её адрес.

    `current_dsn` — то, куда подключено приложение прямо сейчас
    (`DATABASE_URL`). Совпасть оно должно с `DEMO_DATABASE_URL`, и при этом
    отличаться от боевой базы.
    """
    env = os.environ if env is None else env
    demo = demo_dsn(env)

    # Эталон — часть демо-стенда, а не посторонняя база: именно на нём
    # выполняются миграции и наполнение, из него потом копируется демо. Имя
    # эталона выводится из имени демо-базы, отдельной переменной для него нет
    # намеренно — лишняя переменная это лишнее место, где окажется чужое имя.
    allowed = (demo, _with_dbname(demo, template_name(demo)))
    if not any(same_database(current_dsn, candidate) for candidate in allowed):
        raise DemoGuardRefused(
            "команда демо запущена не на демо-базе.\n"
            f"  подключено к:      {'/'.join(target(current_dsn))}\n"
            f"  DEMO_DATABASE_URL: {'/'.join(target(demo))}\n"
            f"  (эталон демо:      {template_name(demo)})\n"
            "Демо живёт отдельной базой (D016). Запустите команду с "
            "DATABASE_URL, равным DEMO_DATABASE_URL."
        )
    return demo


def _with_dbname(dsn: str, dbname: str) -> str:
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, dbname=dbname)


def require_demo_data(connection, demo_tenant_code: str) -> None:
    """Отказать, если в этой базе есть чужие данные. Иначе — пометить её.

    Второй слой предохранителя, и он единственный, который работает, когда
    переменные окружения врут. Совпадение адресов доказывает только, что
    `DATABASE_URL` равен `DEMO_DATABASE_URL`, — а у демо-экземпляра они равны
    всегда, это его нормальное состояние. Значит, опечатка «`DEMO_DATABASE_URL`
    показывает на базу продукта» первым слоем не ловится вообще: там тоже всё
    совпадёт.

    Ловится она по данным. Демо-база содержит один тенант — демонстрационный.
    База, в которой есть хоть один чужой тенант, демо-базой не является, чем бы
    ни были заполнены переменные, и запись в неё не выполняется.

    Пустая непомеченная база принимается и помечается: это ровно тот случай,
    когда демо поднимают с нуля. Помеченная база проходит без вопросов — метку
    ставила эта же команда.
    """
    with connection.cursor() as cursor:
        cursor.execute("select to_regclass(%s) is not null", [STAMP_TABLE])
        stamped_table = bool(cursor.fetchone()[0])
        if stamped_table:
            cursor.execute(f"select 1 from {STAMP_TABLE} where marker = %s", [STAMP])
            if cursor.fetchone():
                return

        cursor.execute("select to_regclass('tenants') is not null")
        if cursor.fetchone()[0]:
            cursor.execute(
                "select code from tenants where code <> %s order by code limit 5",
                [demo_tenant_code],
            )
            foreign = [row[0] for row in cursor.fetchall()]
            if foreign:
                raise DemoGuardRefused(
                    "в этой базе есть данные, которых в демо быть не может: "
                    f"партнёры {', '.join(foreign)}. Наполнение демо сюда не "
                    "пойдёт — похоже, DEMO_DATABASE_URL показывает на базу "
                    "продукта. Демо живёт отдельной базой (D016)."
                )

        # База наша: помечаем, чтобы её потом можно было пересоздать сбросом, а
        # чужую — нельзя (см. demo.reset).
        cursor.execute(
            f"create table if not exists {STAMP_TABLE} (marker text primary key)"
        )
        cursor.execute(
            f"insert into {STAMP_TABLE} (marker) values (%s) on conflict do nothing",
            [STAMP],
        )


def maintenance_dsn(dsn: str) -> str:
    """Тот же сервер, но база обслуживания: удаляемую базу отпускают заранее."""
    from psycopg.conninfo import make_conninfo

    return make_conninfo(dsn, dbname=MAINTENANCE_DB)


def template_name(dsn: str, suffix: str = "_template") -> str:
    """Имя базы-эталона, из которой пересоздаётся демо."""
    return f"{database_name(dsn)}{suffix}"
