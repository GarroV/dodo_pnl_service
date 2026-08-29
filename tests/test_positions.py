"""Должности: шаблон условий найма (issue #181).

Зачем. Сегодня каждому человеку условия найма набираются с нуля: группа, точка,
ставка, коэффициент, схема расчёта, мера работы, регистр — семь полей, и все
семь надо знать. Эталон (модуль 16) держит между справочниками и людьми
отдельный слой: **должность** задаёт тип оплаты, вилку ставки и обычные
надбавки, а человека заводят в два клика.

Что здесь проверяется:

1. Должность — справочник партнёра со своими правами: ведёт её тот же, кто
   ведёт остальные справочники, а видят все.
2. Она **подставляет** значения в условия найма, но не диктует их: ставку
   конкретному человеку можно поставить свою.
3. Шаблон **не меняет условия уже нанятых** — иначе правка должности молча
   пересчитывала бы закрытые месяцы всем, кто на ней сидит.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import as_app_user, body, login_as

JUNE = date(2026, 6, 1)


@pytest.fixture
def a_position(web_env):
    """Должность «Пиццамейкер» с вилкой ставки — материал для проверок."""
    from core.models import EmployeeGroup, Position, Tenant

    tenant = Tenant.objects.get(code="rs-dev")
    group = EmployeeGroup.objects.filter(tenant=tenant).order_by("code").first()
    position = Position.objects.create(
        tenant=tenant, code="pizzamaker", title="Пиццамейкер",
        group=group, contract_hours=Decimal("176.00"),
    )
    yield position
    Position.objects.filter(pk=position.pk).delete()


def test_the_position_lives_in_the_directory(client, a_position):
    """Должности — раздел справочников, а не спрятанная таблица."""
    login_as(client, "admin")
    html = body(client.get("/directory/"))
    assert "Должности" in html, "раздела должностей нет в справочниках"

    listing = body(client.get("/directory/positions/"))
    assert "Пиццамейкер" in listing


def test_hiring_by_position_fills_the_terms(client, a_position):
    """Выбрал должность — условия найма заполнились ею."""
    from core.models import Employee, EmploymentTerm

    login_as(client, "admin")
    response = client.post("/directory/employees/new/", {
        "last_name": "Петров", "first_name": "Иван",
        "external_id": "POS-0001", "hired_at": "2026-06-01",
        "position": str(a_position.id),
        "base_rate": "450", "coefficient": "1",
    }, follow=True)
    assert response.status_code == 200, body(response)[:400]

    person = Employee.objects.filter(external_id="POS-0001").first()
    assert person is not None, "человек не заведён"
    term = EmploymentTerm.objects.filter(employee=person).first()
    assert term is not None, "условий найма нет — заводить человека незачем"
    assert term.group_id == a_position.group_id, "группа не подставилась из должности"
    assert term.position_id == a_position.id, "связь с должностью не сохранена"

    Employee.objects.filter(pk=person.pk).delete()


def test_editing_the_position_does_not_touch_those_already_hired(client, a_position):
    """Шаблон правят на будущее: у нанятых условия остаются прежними."""
    from core.models import Employee, EmploymentTerm

    login_as(client, "admin")
    client.post("/directory/employees/new/", {
        "last_name": "Кузнецов", "first_name": "Олег",
        "external_id": "POS-0003", "hired_at": "2026-06-01",
        "position": str(a_position.id), "base_rate": "500", "coefficient": "1",
    }, follow=True)
    person = Employee.objects.get(external_id="POS-0003")
    before = EmploymentTerm.objects.get(employee=person).base_rate

    client.post(f"/directory/positions/{a_position.id}/", {
        "code": "pizzamaker", "title": "Пиццамейкер",
        "group": str(a_position.group_id), "contract_hours": "160",
    }, follow=True)

    assert EmploymentTerm.objects.get(employee=person).base_rate == before, (
        "правка должности переписала условия нанятого — закрытые месяцы поехали бы"
    )
    Employee.objects.filter(pk=person.pk).delete()


def test_the_screen_refuses_the_one_who_does_not_keep_directories(client, a_position):
    """Право то же, что у остальных справочников: ведёт администратор сети."""
        # Роль без права ведения справочников — теперь это управляющий точки:
    # бухгалтер и оперативный директор их ведут с 28.08.2026 (D059).
    login_as(client, "manager")
    response = client.post("/directory/positions/new/", {
        "code": "courier", "title": "Курьер", "group": str(a_position.group_id),
    })
    assert response.status_code == 403, body(response)[:300]


def test_the_database_refuses_it_too(db):
    """И мимо интерфейса тоже: держит политика базы, а не только экран.

    Проверка идёт ролью `app_user` (иначе владелец таблиц обходит RLS, и
    зелёный прогон ничего не доказывает) и на отдельной базе доступа, где
    заведены роли фикстуры.
    """
    import psycopg

    from conftest import T1, USER_ACCOUNTANT, USER_ADMIN

    # Группа нужна как материал: должность на неё ссылается.
    group = db.execute(
        """insert into employee_groups (tenant_id, code, title, scheme)
           values (%s, 'g-pos', 'Группа для должности', 'standard')
           returning id""",
        (T1,),
    ).fetchone()[0]

    with as_app_user(db, USER_ACCOUNTANT) as conn:
        # Точка сохранения: отказ базы рвёт транзакцию целиком, и без отката к
        # ней следующий запрос получил бы «transaction is aborted» вместо
        # ответа — то есть тест проверял бы не то, что написано.
        conn.execute("savepoint attempt")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            conn.execute(
                "insert into positions (tenant_id, code, title, group_id)"
                " values (%s, 'courier', 'Курьер', %s)",
                (T1, group),
            )
        conn.execute("rollback to savepoint attempt")

    # Контроль: тому, кто ведёт справочники, та же вставка проходит — иначе
    # проверка выше доказывала бы, что таблица просто закрыта для всех.
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute(
            "insert into positions (tenant_id, code, title, group_id)"
            " values (%s, 'courier', 'Курьер', %s)",
            (T1, group),
        )
