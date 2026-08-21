"""Право вести справочники держит база, а не только экран (T018).

**Зачем отдельно от `test_directory.py`.** Там проверяется интерфейс: ссылка
показана тому, у кого право есть, адрес отвечает отказом словами. Всё это —
одна половина пары. Вторая половина в том, что запись не пройдёт **мимо**
экрана: через будущий API, через Telegram, через чужой скрипт с теми же
доступами к базе. Половина пары, проверенная за обе, — это ровно тот способ,
которым в этом проекте уже прожил незамеченным дефект видимости регистров.

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в тестовой базе
суперпользователь: политики его не ограничивают вовсе, а `force row level
security` он обходит. Запрет, проверенный без переключения роли, зелен всегда.

**Что именно проверяется.** Пять справочников задачи и условия найма: у роли
без `directory.manage` каждая запись отвергается громко (`with check`
ограничивающей политики), а у администратора сети — проходит. Парная проверка
обязательна: без неё «нельзя никому» выглядело бы точно так же, как «нельзя
тому, кому не положено», и запрет мог бы оказаться сломанным справочником.

**Календарь — отдельный случай.** Он общий для страны и `tenant_id` не имеет
(`0004_rls`, `SHARED_TABLES`), поэтому право на него считает своя функция
`app_manages_calendar` — по стране тенанта, в котором у человека есть право.
Значит проверять надо и границу: администратор одной страны не ведёт календарь
другой.
"""
from __future__ import annotations

import psycopg
import pytest

from conftest import (
    LE1,
    T1,
    U_NS1,
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


# --- материал ----------------------------------------------------------------


def _existing(conn) -> dict:
    """Строки, которые уже есть: их правят, а не заводят.

    Кладутся владельцем схемы, мимо политик, — это подготовка, а не проверка.
    """
    employee = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, 'pol-1', 'Тест', 'Тестов') returning id""",
        (T1,),
    ).fetchone()[0]
    group = conn.execute(
        """insert into employee_groups (tenant_id, code, title, scheme, ledger)
           values (%s, 'pol-g', 'Группа', 'hourly', 'official') returning id""",
        (T1,),
    ).fetchone()[0]
    term = conn.execute(
        """insert into employment_terms
               (tenant_id, employee_id, group_id, unit_id, base_rate, valid_from)
           values (%s, %s, %s, %s, 100, '2026-01-01') returning id""",
        (T1, employee, group, U_NS1),
    ).fetchone()[0]
    return {"employee": employee, "group": group, "term": term}


# Правка каждого справочника одной строкой: что именно писать, чтобы запрет и
# разрешение проверялись **одним и тем же** запросом. Разные запросы для «можно»
# и «нельзя» — способ незаметно проверить разные вещи.
def _writes(rows: dict) -> dict:
    return {
        "employees": (
            "update employees set last_name = 'Правленый' where id = %s",
            (rows["employee"],),
        ),
        "employment_terms": (
            "update employment_terms set base_rate = 555 where id = %s",
            (rows["term"],),
        ),
        "employee_groups": (
            "update employee_groups set title = 'Другое название' where id = %s",
            (rows["group"],),
        ),
        "units": (
            "update units set title = 'Другое название' where id = %s",
            (U_NS1,),
        ),
        "legal_entities": (
            "update legal_entities set title = 'Другое название' where id = %s",
            (LE1,),
        ),
    }


@pytest.fixture
def rows(db):
    return _existing(db)


# --- запрет ------------------------------------------------------------------


@pytest.mark.parametrize("user", ROLES_WITHOUT_RIGHT)
@pytest.mark.parametrize("table", sorted(_writes({"employee": None, "group": None, "term": None})))
def test_a_role_without_the_right_cannot_write_a_directory(db, rows, user, table):
    """Отказ громкий: `with check` ограничивающей политики, а не «изменено 0 строк»."""
    query, args = _writes(rows)[table]
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(query, args)
        conn.execute("rollback to savepoint attempt")


@pytest.mark.parametrize("user", ROLES_WITHOUT_RIGHT)
def test_a_role_without_the_right_cannot_hire(db, rows, user):
    """Завести человека и его условия найма база не даёт (T164).

    Раньше проверять здесь `insert` в эти две таблицы было почти не за чем:
    экраном сотрудник не заводился вовсе (D029), а загрузка таблицы шла ролью, у
    которой право есть. Экран заведения появился — и вставка стала тем самым
    действием, которое попробуют мимо интерфейса первым.

    Обе таблицы, а не одна: карточка без условий найма и условия найма чужому
    человеку — разные записи, и каждая закрыта своей политикой.
    """
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(
                """insert into employees (tenant_id, external_id, first_name, last_name)
                   values (%s, 'pol-hire', 'Свой', 'Человек')""",
                (T1,),
            )
        conn.execute("rollback to savepoint attempt")

        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(
                """insert into employment_terms
                       (tenant_id, employee_id, group_id, unit_id, base_rate, valid_from)
                   values (%s, %s, %s, %s, 999, '2026-09-01')""",
                (T1, rows["employee"], rows["group"], U_NS1),
            )
        conn.execute("rollback to savepoint attempt")


def test_the_network_administrator_hires(db, rows):
    """Обратная сторона: у кого право есть — заводит. Иначе запрет неотличим от поломки."""
    with as_app_user(db, USER_ADMIN) as conn:
        person = conn.execute(
            """insert into employees (tenant_id, external_id, first_name, last_name)
               values (%s, 'pol-hire-ok', 'Свой', 'Человек') returning id""",
            (T1,),
        ).fetchone()[0]
        assert conn.execute(
            """insert into employment_terms
                   (tenant_id, employee_id, group_id, unit_id, base_rate, valid_from,
                    work_measure)
               values (%s, %s, %s, %s, 999, '2026-09-01', 'fixed_amount')""",
            (T1, person, rows["group"], U_NS1),
        ).rowcount == 1


@pytest.mark.parametrize("user", ROLES_WITHOUT_RIGHT)
def test_a_role_without_the_right_cannot_create_a_directory_row(db, user):
    """Не только правка: завести точку или юрлицо тоже нельзя."""
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(
                "insert into legal_entities (tenant_id, title) values (%s, 'Своё юрлицо')",
                (T1,),
            )
        conn.execute("rollback to savepoint attempt")


@pytest.mark.parametrize("user", ROLES_WITHOUT_RIGHT)
def test_a_role_without_the_right_cannot_delete_a_directory_row(db, rows, user):
    """Удаление закрыто отдельной политикой: у `delete` своё `using`."""
    with as_app_user(db, user) as conn:
        conn.execute("savepoint attempt")
        assert conn.execute(
            "delete from employee_groups where id = %s", (rows["group"],)
        ).rowcount == 0, "удаление прошло: политики на delete нет"
        conn.execute("rollback to savepoint attempt")


# --- разрешение (без него запрет неотличим от сломанного справочника) --------


@pytest.mark.parametrize("table", sorted(_writes({"employee": None, "group": None, "term": None})))
def test_the_network_administrator_writes_every_directory(db, rows, table):
    query, args = _writes(rows)[table]
    with as_app_user(db, USER_ADMIN) as conn:
        assert conn.execute(query, args).rowcount == 1, table


def test_the_network_administrator_creates_and_deletes(db, rows):
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute(
            "insert into legal_entities (tenant_id, title) values (%s, 'Своё юрлицо')", (T1,)
        )
        assert conn.execute(
            "delete from employee_groups where id = %s", (rows["group"],)
        ).rowcount == 1


# --- чужой тенант ------------------------------------------------------------


def test_the_right_does_not_cross_the_tenant(db, rows):
    """Право вести справочник — в своём тенанте, а не вообще.

    Изоляция тенантов и так стоит с `0004_rls`; проверка здесь затем, что новая
    политика ограничивающая, и её легко было написать без `tenant_id` — тогда
    администратор одной сети правил бы справочник другой.
    """
    with as_app_user(db, USER_OTHER) as conn:
        conn.execute("savepoint attempt")
        assert conn.execute(
            "update employees set last_name = 'Чужой' where id = %s", (rows["employee"],)
        ).rowcount == 0
        conn.execute("rollback to savepoint attempt")


# --- производственный календарь ----------------------------------------------


def test_the_calendar_is_writable_only_with_the_right(db):
    """До этой задачи календарь не мог вести никто: разрешающей политики не было."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(
                "insert into calendars (country_code, period, norm_hours, working_days) "
                "values ('RS', '2027-05-01', 160, 20)"
            )
        conn.execute("rollback to savepoint attempt")

    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute(
            "insert into calendars (country_code, period, norm_hours, working_days) "
            "values ('RS', '2027-05-01', 160, 20)"
        )
        assert conn.execute(
            "update calendars set norm_hours = 168 "
            "where country_code = 'RS' and period = '2027-05-01'"
        ).rowcount == 1


def test_the_calendar_of_another_country_is_closed(db):
    """Календарь общий для страны — значит право считается по стране тенанта.

    Администратор сербской сети не ведёт календарь страны второго тенанта, хотя
    право `directory.manage` у него есть: у календаря нет `tenant_id`, и без
    проверки страны право открыло бы чужой справочник целиком.
    """
    with as_app_user(db, USER_ADMIN) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(
                "insert into calendars (country_code, period, norm_hours, working_days) "
                "values ('XX', '2027-05-01', 160, 20)"
            )
        conn.execute("rollback to savepoint attempt")


# --- доказательство, что режет политика, а не случайность --------------------


def test_the_protection_is_the_policy_and_not_luck(db, rows):
    """Снимаем политику внутри транзакции — проверка обязана покраснеть.

    Без этого нельзя отличить работающий запрет от того, что директору просто
    нечего было писать: тест, зелёный и до, и после починки, не доказывает
    ничего.
    """
    db.execute("savepoint before_damage")
    db.execute("drop policy directory_manage_update on employees")
    try:
        with as_app_user(db, USER_DIRECTOR) as conn:
            assert conn.execute(
                "update employees set last_name = 'Правленый' where id = %s", (rows["employee"],)
            ).rowcount == 1, "без политики директор обязан писать — иначе тест проверяет не то"
    finally:
        db.execute("rollback to savepoint before_damage")

    with as_app_user(db, USER_DIRECTOR) as conn:
        conn.execute("savepoint attempt")
        with pytest.raises(DENIED):
            conn.execute(
                "update employees set last_name = 'Правленый' where id = %s", (rows["employee"],)
            )
        conn.execute("rollback to savepoint attempt")
