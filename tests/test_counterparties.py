"""Справочник контрагентов: изоляция, право вести, версионирование (T150).

Все проверки гоняются **ролью `app_user`**. Владелец таблиц обходит `force row
level security`, а суперпользователь обходит её всегда — тот же набор проверок,
выполненный владельцем, был бы зелёным при снятых политиках, то есть бесполезным.
На этом проекте так уже прожил незамеченным дефект видимости регистров.

Каждая проверка написана так, чтобы **краснеть от порчи своей политики**: см.
журнал блока `docs/forge/blocks/suppliers.md`, раздел про порчу.

Почему у контрагента вообще появились даты действия и ключ Dodo IS. Смысл
справочника — чтобы траты одного поставщика складывались, а не рассыпались по
написаниям названия (спека очереди). Значит нужны три вещи: одно название на
партнёра (иначе «EPS» и «EPS Elektro» — два поставщика), ключ, по которому
строка сойдётся со справочником Dodo IS в шестой очереди (искать потом по
названию = гарантированная ручная работа, `docs/dodo-is-api.md`), и даты
действия — контрагент заканчивается датой, а не удалением: на него ссылаются
факты закрытых месяцев.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    CP_EPS,
    T1,
    T2,
    USER_ACCOUNTANT,
    USER_ADMIN,
    USER_DIRECTOR,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
)


def add(conn, *, tenant=T1, title="Novi dobavljač", external=None,
        valid_from="2026-01-01", valid_to=None):
    return conn.execute(
        """insert into counterparties
                (tenant_id, title, external_id, valid_from, valid_to)
           values (%s, %s, %s, %s, %s) returning id""",
        (tenant, title, external, valid_from, valid_to),
    ).fetchone()[0]


# --- изоляция партнёров -------------------------------------------------------


def test_other_tenant_counterparties_are_invisible(db):
    """Контрагент чужого партнёра не виден ни строкой, ни в счётчике."""
    add(db, tenant=T2, title="Чужой поставщик")

    with as_app_user(db, USER_DIRECTOR) as conn:
        titles = {
            row[0] for row in conn.execute("select title from counterparties").fetchall()
        }
    assert "Чужой поставщик" not in titles
    assert "EPS Elektro" in titles


def test_the_isolation_check_is_meaningful(db):
    """Тот же запрос владельцем видит обоих — значит выше отсекала именно RLS."""
    add(db, tenant=T2, title="Чужой поставщик")

    titles = {row[0] for row in db.execute("select title from counterparties").fetchall()}
    assert {"EPS Elektro", "Чужой поставщик"} <= titles


def test_writing_into_another_tenant_is_rejected(db):
    """Забытый фильтр на записи тоже не проходит: `with check` закрывает вставку."""
    with as_app_user(db, USER_ADMIN) as conn:
        with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
            add(conn, tenant=T2, title="Подкидыш")


# --- кто ведёт справочник -----------------------------------------------------


def test_only_the_directory_manager_adds_a_counterparty(db):
    """Заводит контрагента тот же, кто ведёт остальные справочники (T018)."""
    with as_app_user(db, USER_ADMIN) as conn:
        add(conn, title="Delhaize")

    for who in (USER_ACCOUNTANT, USER_DIRECTOR, USER_MANAGER):
        with as_app_user(db, who) as conn:
            with pytest.raises(psycopg.errors.InsufficientPrivilege), conn.transaction():
                add(conn, title=f"Мимо-{who[-2:]}")


def test_only_the_directory_manager_edits_and_closes(db):
    """Правка и удаление закрыты тем же правом, что и заведение."""
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        # Ни правка, ни удаление не падают ошибкой — они не находят строк:
        # `using` у ограничивающей политики отсекает их раньше, чем дело дойдёт
        # до проверки записи. Это ровно то поведение, которого требует D014:
        # забытое право даёт пустой результат, а не чужую строку. Словами
        # человеку объясняет экран (`_guard`), а не база.
        with conn.transaction():
            changed = conn.execute(
                "update counterparties set title = 'Подмена' where id = %s", (CP_EPS,)
            ).rowcount
            removed = conn.execute(
                "delete from counterparties where id = %s", (CP_EPS,)
            ).rowcount
        assert (changed, removed) == (0, 0)
        assert conn.execute(
            "select title from counterparties where id = %s", (CP_EPS,)
        ).fetchone()[0] == "EPS Elektro"

    with as_app_user(db, USER_ADMIN) as conn:
        changed = conn.execute(
            "update counterparties set title = 'EPS Distribucija' where id = %s", (CP_EPS,)
        ).rowcount
    assert changed == 1


def test_everyone_reads_the_directory(db):
    """Читают контрагентов все роли: без этого счёт вносить не из чего."""
    for who in (USER_ACCOUNTANT, USER_DIRECTOR, USER_MANAGER, USER_ADMIN):
        with as_app_user(db, who) as conn:
            assert conn.execute(
                "select count(*) from counterparties where id = %s", (CP_EPS,)
            ).fetchone()[0] == 1


def test_a_stranger_without_membership_sees_nothing(db):
    """Пользователь чужого партнёра не видит справочник вовсе (D014)."""
    with as_app_user(db, USER_OTHER) as conn:
        assert conn.execute("select count(*) from counterparties").fetchone()[0] == 0


# --- как справочник держит форму ----------------------------------------------


def test_one_title_per_partner(db):
    """Одно написание названия у партнёра: иначе траты рассыпаются по копиям."""
    with as_app_user(db, USER_ADMIN) as conn:
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            add(conn, title="EPS Elektro")


def test_the_same_title_lives_at_another_partner(db):
    """У другого партнёра поставщик с тем же названием — обычное дело."""
    add(db, tenant=T2, title="EPS Elektro")
    assert db.execute(
        "select count(*) from counterparties where title = 'EPS Elektro'"
    ).fetchone()[0] == 2


def test_the_dodo_is_key_is_unique_per_partner(db):
    """Ключ Dodo IS сопоставляет строки один к одному — двух хозяев у него нет."""
    with as_app_user(db, USER_ADMIN) as conn:
        add(conn, title="Metro Cash & Carry", external="vendor-77")
        with pytest.raises(psycopg.errors.UniqueViolation), conn.transaction():
            add(conn, title="Metro veleprodaja", external="vendor-77")


def test_an_empty_dodo_is_key_does_not_collide(db):
    """Пустой ключ — «ещё не сопоставлен», и таких строк сколько угодно.

    Уникальность частичная намеренно: до шестой очереди поле пустое у всех, и
    обычный уникальный ключ разрешил бы ровно одного контрагента без ключа.
    """
    with as_app_user(db, USER_ADMIN) as conn:
        add(conn, title="Без ключа один")
        add(conn, title="Без ключа два")
    assert db.execute(
        "select count(*) from counterparties where external_id is null"
    ).fetchone()[0] >= 2


def test_validity_runs_forward(db):
    """Закрыт раньше, чем начал действовать, — это не версия, а опечатка."""
    with as_app_user(db, USER_ADMIN) as conn:
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            add(conn, title="Задом наперёд", valid_from="2026-06-01", valid_to="2026-01-01")


def test_a_closed_counterparty_stays_in_the_directory(db):
    """Контрагент закрывается датой, а не удалением: факты на него ссылаются."""
    with as_app_user(db, USER_ADMIN) as conn:
        row = add(conn, title="Ушёл с рынка", valid_from="2024-01-01", valid_to="2026-03-01")
        assert conn.execute(
            "select valid_to from counterparties where id = %s", (row,)
        ).fetchone()[0].isoformat() == "2026-03-01"
