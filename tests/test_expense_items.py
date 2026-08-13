"""Справочник статей расходов: что держит база, а не экран (T108).

**Зачем отдельно от экранных проверок.** Там проверяется интерфейс: ссылка,
отказ словами, поля формы. Здесь — вторая половина пары: запись не пройдёт
**мимо** экрана, через будущий API, загрузку файла бухгалтера или чужой скрипт
с теми же доступами к базе. Половина пары, проверенная за обе, — тот самый
способ, которым в этом проекте уже прожил незамеченным дефект видимости
регистров.

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в тестовой базе
суперпользователь: политики его не ограничивают, `force row level security` он
обходит. Запрет, проверенный без переключения роли, зелен всегда.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    I_FOOD,
    T1,
    T2,
    USER_ACCOUNTANT,
    USER_ADMIN,
    USER_DIRECTOR,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
)

pytestmark = pytest.mark.usefixtures("db")

DENIED = psycopg.errors.InsufficientPrivilege

# Роли, у которых права вести справочники нет. Директор здесь не для полноты
# списка: в расчёте он может больше всех, и именно на нём видно, что право
# отдельное, а не «кто главнее, тот и правит».
ROLES_WITHOUT_RIGHT = [USER_DIRECTOR, USER_ACCOUNTANT, USER_MANAGER]

INSERT = """
insert into expense_items (tenant_id, code, titles, pnl_item_id, valid_from)
values (%s, %s, %s, %s, '2026-01-01')
"""


def _titles(text: str = "Вода") -> psycopg.types.json.Jsonb:
    from psycopg.types.json import Jsonb

    return Jsonb({"ru": text, "en": "Water", "sr-latn": "Voda"})


def test_the_directory_starts_empty(db):
    """Список статей не выдуман продуктом: он придёт с файла бухгалтера (Q015).

    Проверяется именно пустота: наполненный «на всякий случай» справочник даёт
    двум людям разные названия одной траты, и вскроется это на первой сборке
    P&L — когда сходиться уже поздно.
    """
    assert db.execute("select count(*) from expense_items").fetchone()[0] == 0


def test_only_the_directory_manager_writes_expense_items(db):
    """Ведёт справочник тот, у кого `directory.manage`; остальные — читают."""
    for user in ROLES_WITHOUT_RIGHT:
        with as_app_user(db, user) as conn:
            with pytest.raises(DENIED), conn.transaction():
                conn.execute(INSERT, (T1, f"water-{user[-1]}", _titles(), I_FOOD))

    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute(INSERT, (T1, "water", _titles(), I_FOOD))
        assert conn.execute("select count(*) from expense_items").fetchone()[0] == 1


def test_the_manager_cannot_rewrite_an_item(db):
    """Запрет стоит и на правке, а не только на заведении."""
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute(INSERT, (T1, "water", _titles(), I_FOOD))

    with as_app_user(db, USER_MANAGER) as conn:
        with pytest.raises(DENIED), conn.transaction():
            conn.execute("update expense_items set code = 'stolen'")


def test_everyone_in_the_tenant_reads_the_items(db):
    """Право вести справочник — не право его видеть.

    Статью выбирает тот, кто вносит расход, то есть управляющий и бухгалтер.
    Закрыть им чтение значило бы сделать справочник бесполезным ровно для тех,
    ради кого он заведён.
    """
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute(INSERT, (T1, "water", _titles(), I_FOOD))

    for user in (USER_MANAGER, USER_ACCOUNTANT, USER_DIRECTOR):
        with as_app_user(db, user) as conn:
            codes = {row[0] for row in conn.execute("select code from expense_items").fetchall()}
        assert codes == {"water"}, f"пользователь {user} не видит статей"


def test_items_of_another_tenant_are_invisible(db):
    """Справочник статей — данные партнёра, а не общий словарь продукта."""
    db.execute(INSERT, (T1, "water", _titles(), I_FOOD))
    db.execute(INSERT, (T2, "alien", _titles("Чужая"), I_FOOD))

    with as_app_user(db, USER_DIRECTOR) as conn:
        codes = {row[0] for row in conn.execute("select code from expense_items").fetchall()}
    assert codes == {"water"}

    with as_app_user(db, USER_OTHER) as conn:
        codes = {row[0] for row in conn.execute("select code from expense_items").fetchall()}
    assert codes == {"alien"}


def test_an_item_without_a_title_is_rejected(db):
    """Статья без названия — строка, которую человек не выберет глазами."""
    from psycopg.types.json import Jsonb

    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(INSERT, (T1, "nameless", Jsonb({}), I_FOOD))


def test_the_code_is_unique_within_the_tenant(db):
    """Две статьи с одним кодом — два имени одной траты в отчёте."""
    db.execute(INSERT, (T1, "water", _titles(), I_FOOD))
    with pytest.raises(psycopg.errors.UniqueViolation), db.transaction():
        db.execute(INSERT, (T1, "water", _titles("Вода ещё раз"), I_FOOD))


def test_the_validity_range_is_checked(db):
    """Закрыть статью раньше, чем она начала действовать, нельзя."""
    from psycopg.types.json import Jsonb

    with pytest.raises(psycopg.errors.CheckViolation), db.transaction():
        db.execute(
            """insert into expense_items
                   (tenant_id, code, titles, pnl_item_id, valid_from, valid_to)
               values (%s, 'backwards', %s, %s, '2026-06-01', '2026-01-01')""",
            (T1, Jsonb({"ru": "Задом наперёд"}), I_FOOD),
        )


def test_a_renamed_item_does_not_count_as_a_new_fact(db):
    """Статья входит в сравнение фактов: смена статьи — изменение по существу.

    `facts_same` отвечает на вопрос «одно ли это событие», и от её ответа
    зависит идемпотентность записи. Не включить туда новую колонку значило бы,
    что смена **только** статьи возвращает `unchanged`, то есть правка молча не
    применяется.
    """
    from conftest import U_BG1
    from facts_helpers import fact_payload, upsert_fact

    items = []
    for code in ("water", "power"):
        items.append(
            db.execute(
                INSERT + " returning id", (T1, code, _titles(code), I_FOOD)
            ).fetchone()[0]
        )

    payload = fact_payload(unit=U_BG1, key="with-item")
    payload["expense_item_id"] = str(items[0])
    _, action = upsert_fact(db, payload)
    assert action == "inserted"

    _, action = upsert_fact(db, payload)
    assert action == "unchanged", "тот же факт записался второй раз"

    payload["expense_item_id"] = str(items[1])
    _, action = upsert_fact(db, payload)
    assert action == "updated", "смена статьи не признана изменением факта"
