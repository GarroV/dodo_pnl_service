"""Право вести правила держит база, а не только экран (T090).

**Зачем отдельно от экранного теста.** Там проверяется интерфейс: ссылка на
переопределение показана тому, у кого право есть, адрес отвечает отказом
словами. Это одна половина пары. Вторая половина в том, что запись не пройдёт
**мимо** экрана: через будущий API, через Telegram, через чужой скрипт с теми
же доступами к базе. Половина пары, проверенная за обе, — это ровно тот
способ, которым в этом проекте уже прожил незамеченным дефект видимости
регистров.

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в тестовой
базе суперпользователь: политики его не ограничивают вовсе, а `force row
level security` он обходит. Запрет, проверенный без переключения роли, зелен
всегда.

**Что именно проверяется.** У роли без `rules.manage` каждая запись в
`rule_overrides` отвергается, у администратора сети — проходит. Парная
проверка обязательна: без неё «нельзя никому» выглядело бы точно так же, как
«нельзя тому, кому не положено», и запрет мог бы оказаться сломанной
таблицей.

**Insert и update отвергаются громко, delete — тихо.** У `insert` и `update`
политика несёт только `with check`: строка, не прошедшая проверку, роняет
`InsufficientPrivilege`. У `delete` политика несёт `using` — она отсекает
строки до удаления, поэтому запрет выглядит как «удалено 0 строк», а не как
исключение (`0180_rules_permissions`, комментарий про `using` на `update`
намеренно поясняет именно эту разницу).
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    T1,
    T2,
    USER_ACCOUNTANT,
    USER_ADMIN,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
)

pytestmark = pytest.mark.usefixtures("db")

DENIED = psycopg.errors.InsufficientPrivilege

# Роли, у которых права вести правила нет. В фикстуре conftest `rules.manage`
# есть только у администратора сети (`P_ADMIN`) — директор, бухгалтер и
# управляющий его не получают, хоть директор и держит почти все остальные права.
ROLES_WITHOUT_RIGHT = [USER_DIRECTOR, USER_ACCOUNTANT, USER_MANAGER]

INSERT_OVERRIDE = (
    """insert into rule_overrides (tenant_id, scope_type, scope_id, path, value, valid_from)
       values (%s, 'tenant', null, 'rates.net_factor', '0.71'::jsonb, '2026-09-01')"""
)


def _existing_override(conn, *, tenant: str) -> str:
    """Строка переопределения, которую правят и удаляют, а не заводят.

    Кладётся администратором сети через `as_app_user` — настоящим путём
    продукта, а не в обход RLS владельцем схемы: подготовка обязана пройти
    те же политики, что и проверяемая запись.
    """
    with as_app_user(conn, USER_ADMIN) as admin_conn:
        row = admin_conn.execute(
            """insert into rule_overrides
                   (tenant_id, scope_type, scope_id, path, value, valid_from)
               values (%s, 'tenant', null, 'rates.net_factor', '0.71'::jsonb, '2026-09-01')
               returning id""",
            (tenant,),
        ).fetchone()
    return row[0]


@pytest.fixture
def override(db):
    return _existing_override(db, tenant=T1)


# --- запрет ------------------------------------------------------------------


@pytest.mark.parametrize("user", ROLES_WITHOUT_RIGHT)
def test_a_role_without_the_right_cannot_insert_a_rule_override(db, user):
    """Отказ громкий: `with check` ограничивающей политики на insert."""
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(INSERT_OVERRIDE, (T1,))
        conn.execute("rollback to savepoint attempt")


@pytest.mark.parametrize("user", ROLES_WITHOUT_RIGHT)
def test_a_role_without_the_right_cannot_update_a_rule_override(db, override, user):
    """Отказ громкий и на update: `using` для него намеренно не заведён."""
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(
                "update rule_overrides set value = '0.75'::jsonb where id = %s", (override,)
            )
        conn.execute("rollback to savepoint attempt")


@pytest.mark.parametrize("user", ROLES_WITHOUT_RIGHT)
def test_a_role_without_the_right_cannot_delete_a_rule_override(db, override, user):
    """Удаление закрыто своей политикой: у `delete` есть `using`, отказ тихий."""
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        assert conn.execute(
            "delete from rule_overrides where id = %s", (override,)
        ).rowcount == 0, "удаление прошло: политика на delete не сработала"
        conn.execute("rollback to savepoint attempt")


# --- разрешение (без него запрет неотличим от сломанной таблицы) -------------


def test_the_network_administrator_inserts_a_rule_override(db):
    with as_app_user(db, USER_ADMIN) as conn:
        assert conn.execute(INSERT_OVERRIDE, (T1,)).rowcount == 1


def test_the_network_administrator_updates_and_deletes_a_rule_override(db, override):
    with as_app_user(db, USER_ADMIN) as conn:
        assert conn.execute(
            "update rule_overrides set value = '0.75'::jsonb where id = %s", (override,)
        ).rowcount == 1
        assert conn.execute(
            "delete from rule_overrides where id = %s", (override,)
        ).rowcount == 1


# --- чужой тенант --------------------------------------------------------------


def test_the_right_does_not_cross_the_tenant(db):
    """Право вести правила — в своём тенанте, а не вообще.

    Изоляция тенантов и так стоит с `0004_rls` (`for all ... using (tenant_id
    in (select app_tenant_ids()))`). Проверка здесь затем, что новая
    ограничивающая политика легла поверх изоляции, а не заменила её собой:
    администратор сети T1 не заводит переопределение тенанту T2, хотя
    `rules.manage` у него есть.
    """
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(INSERT_OVERRIDE, (T2,))
        conn.execute("rollback to savepoint attempt")
