"""Закрытие часов по точке (T022).

Что здесь проверяется и почему именно так.

**Ролью `app_user`.** Тесты подключаются владельцем схемы, а он в этой базе
суперпользователь: политики его не ограничивают вовсе. Проверка запрета,
написанная без переключения роли, зелена всегда — на этом в проекте уже прожил
незамеченным дефект видимости регистров. Поэтому всё, что говорит «база не
даст», идёт через `as_app_user`.

**Спорная точка не держит остальные.** Главное требование задачи и спеки
(«хочу закрывать свою точку независимо от других, чтобы не ждать всю сеть»).
Поэтому почти у каждого запрета есть парный тест: закрыли одну — соседняя
пишется как раньше.

**Экран не предлагает того, что запретит.** У роли без права `unit.close` формы
закрытия нет, а запрос мимо экрана отвергается — так же, как сделано в T064 и
T072.
"""
from __future__ import annotations

import re
from decimal import Decimal

import psycopg
import pytest

from conftest import (
    JULY,
    JUNE,
    T1,
    T2,
    U_BG1,
    U_NS1,
    U_NS2,
    U_OTHER,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    USER_OTHER,
    as_app_user,
    body,
    login_as,
    period_url,
)

DENIED = psycopg.errors.InsufficientPrivilege


# =============================================================================
# 1. Уровень базы: ролью app_user
# =============================================================================


def make_timesheet(conn, ext_id: str, unit_id: str | None, period: str = JUNE) -> str:
    """Строка табеля на точке. Кладётся владельцем, мимо политик."""
    employee = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Тест', 'Часовой') returning id""",
        (T1, ext_id),
    ).fetchone()[0]
    return conn.execute(
        """insert into timesheets (tenant_id, employee_id, unit_id, period, norm_hours, hours)
           values (%s, %s, %s, %s, 176, '{"regular": "8.00"}'::jsonb) returning id""",
        (T1, employee, unit_id, period),
    ).fetchone()[0]


def close_unit(conn, unit_id: str, period: str = JUNE, tenant: str = T1) -> str:
    """Закрыть часы точки владельцем — материал для проверки запрета."""
    return conn.execute(
        """insert into timesheet_closures (tenant_id, unit_id, period, closed_by)
           values (%s, %s, %s, %s) returning id""",
        (tenant, unit_id, period, USER_MANAGER),
    ).fetchone()[0]


def write_hours(conn, timesheet_id: str, hours: str = "12.00") -> None:
    conn.execute(
        "update timesheets set hours = %s::jsonb where id = %s",
        (f'{{"regular": "{hours}"}}', timesheet_id),
    )


def attempt(conn):
    """Точка сохранения: отказ политики обрывает транзакцию.

    Без отката упало бы уже возвращение роли, пряча то, что проверяем.
    """
    conn.execute("savepoint attempt")


def rollback(conn):
    conn.execute("rollback to savepoint attempt")


def test_closed_unit_refuses_hours_in_the_database(db):
    """Главное требование: после закрытия запись отклоняет база, а не экран."""
    sheet = make_timesheet(db, "closed-ns1", U_NS1)
    close_unit(db, U_NS1)

    with as_app_user(db, USER_MANAGER) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            write_hours(conn, sheet)
        rollback(conn)

    assert db.execute(
        "select hours->>'regular' from timesheets where id = %s", (sheet,)
    ).fetchone()[0] == "8.00"


def test_closing_one_unit_leaves_the_others_writable(db):
    """Спорная точка не держит остальные — иначе смысл закрытия по точке пропал."""
    closed = make_timesheet(db, "hold-ns1", U_NS1)
    open_one = make_timesheet(db, "hold-ns2", U_NS2)
    close_unit(db, U_NS1)

    with as_app_user(db, USER_DIRECTOR) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            write_hours(conn, closed)
        rollback(conn)
        write_hours(conn, open_one, "20.00")

    assert db.execute(
        "select hours->>'regular' from timesheets where id = %s", (open_one,)
    ).fetchone()[0] == "20.00"


def test_closed_unit_refuses_a_new_timesheet_row(db):
    """Закрыто — значит закрыто и для новых людей, а не только для правки."""
    close_unit(db, U_NS1)
    employee = db.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, 'late-hire', 'Поздний', 'Найм') returning id""",
        (T1,),
    ).fetchone()[0]

    with as_app_user(db, USER_MANAGER) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            conn.execute(
                """insert into timesheets (tenant_id, employee_id, unit_id, period, norm_hours)
                   values (%s, %s, %s, %s, 176)""",
                (T1, employee, U_NS1, JUNE),
            )
        rollback(conn)


def test_closed_unit_refuses_days_too(db):
    """Подневное хранение — тот же табель другой таблицей (D011).

    Без этой проверки часы закрытой точки правились бы в обход: месячный итог
    собирается из дней, и запись дня меняет ровно то же самое.
    """
    sheet = make_timesheet(db, "days-ns1", U_NS1)
    close_unit(db, U_NS1)

    with as_app_user(db, USER_MANAGER) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            conn.execute(
                """insert into timesheet_days
                       (tenant_id, timesheet_id, work_date, hour_type, hours)
                   values (%s, %s, '2026-06-02', 'regular', 8)""",
                (T1, sheet),
            )
        rollback(conn)


def test_closure_belongs_to_a_month_not_to_the_unit(db):
    """Закрыт июнь — июль пишется. Иначе точку нельзя было бы вести дальше."""
    june = make_timesheet(db, "month-june", U_NS1, JUNE)
    july = make_timesheet(db, "month-july", U_NS1, JULY)
    close_unit(db, U_NS1, JUNE)

    with as_app_user(db, USER_MANAGER) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            write_hours(conn, june)
        rollback(conn)
        write_hours(conn, july, "30.00")

    assert db.execute(
        "select hours->>'regular' from timesheets where id = %s", (july,)
    ).fetchone()[0] == "30.00"


def test_reopened_unit_accepts_hours_again(db):
    """Закрытие обратимо: иначе нажатие по ошибке стоило бы месяца работы."""
    sheet = make_timesheet(db, "reopen-ns1", U_NS1)
    closure = close_unit(db, U_NS1)
    db.execute(
        "update timesheet_closures set reopened_at = now(), reopened_by = %s where id = %s",
        (USER_MANAGER, closure),
    )

    with as_app_user(db, USER_MANAGER) as conn:
        write_hours(conn, sheet, "16.00")

    assert db.execute(
        "select hours->>'regular' from timesheets where id = %s", (sheet,)
    ).fetchone()[0] == "16.00"


def test_manager_closes_own_unit(db):
    with as_app_user(db, USER_MANAGER) as conn:
        conn.execute(
            """insert into timesheet_closures (tenant_id, unit_id, period)
               values (%s, %s, %s)""",
            (T1, U_NS1, JUNE),
        )
        assert conn.execute("select count(*) from timesheet_closures").fetchone()[0] == 1


def test_manager_cannot_close_another_unit(db):
    """Попытка залезть в чужую точку — та самая проверка, ради которой задача."""
    with as_app_user(db, USER_MANAGER) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            conn.execute(
                """insert into timesheet_closures (tenant_id, unit_id, period)
                   values (%s, %s, %s)""",
                (T1, U_NS2, JUNE),
            )
        rollback(conn)


def test_role_without_the_right_cannot_close(db):
    """Роль без `unit.close` база не пускает — даже видящую все точки.

    Проверка не про иерархию ролей: право не подразумевается из «вижу все
    точки» (как в T064) и не выдаётся заодно с `period.approve`. Роль фикстуры
    выбрана та, что видит все точки тенанта и утверждает период, — то есть
    отказ нельзя списать ни на видимость, ни на «мало полномочий».

    Роль фикстуры, а не роль продукта: у бухгалтера продукта `unit.close` с
    T115 есть (D036 — доступ равен директорскому). Условие для этой проверки
    создаётся явно, как это делает `narrowed_ledgers` с регистрами: механизм
    базы остаётся на месте и обязан проверяться, а сужать доступы партнёр будет
    экраном ролей.
    """
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            conn.execute(
                """insert into timesheet_closures (tenant_id, unit_id, period)
                   values (%s, %s, %s)""",
                (T1, U_BG1, JUNE),
            )
        rollback(conn)


def test_director_closes_any_unit(db):
    """D033: закрывать часы точки вправе и оперативный директор.

    До T076 это могла делать только точка сама себя: управляющий в отпуске —
    месяц не закрыть никем, при том что весь период директор утверждает.
    Проверяются все три точки тенанта, а не одна: право директора не привязано
    к точке, и проверка на единственной точке этого бы не показала.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        for unit in (U_BG1, U_NS1, U_NS2):
            conn.execute(
                """insert into timesheet_closures (tenant_id, unit_id, period)
                   values (%s, %s, %s)""",
                (T1, unit, JUNE),
            )
        assert conn.execute("select count(*) from timesheet_closures").fetchone()[0] == 3


def test_director_reopens_a_unit_closed_by_the_manager(db):
    """Открыть заново — то же право. Иначе тупик просто переехал бы на шаг.

    Управляющий закрыл точку и ушёл в отпуск; если снять закрытие может только
    он, часы так и останутся запертыми — ровно та беда, ради которой D033.
    """
    closure = close_unit(db, U_NS1)

    with as_app_user(db, USER_DIRECTOR) as conn:
        changed = conn.execute(
            "update timesheet_closures set reopened_at = now(), reopened_by = %s "
            "where id = %s",
            (USER_DIRECTOR, closure),
        ).rowcount
    assert changed == 1
    assert db.execute(
        "select reopened_at from timesheet_closures where id = %s", (closure,)
    ).fetchone()[0] is not None


def test_director_reopening_returns_hours_to_writable(db):
    """Смысл открытия — часы снова пишутся, а не отметка в таблице закрытий."""
    sheet = make_timesheet(db, "director-reopen", U_NS1)
    closure = close_unit(db, U_NS1)

    with as_app_user(db, USER_DIRECTOR) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            write_hours(conn, sheet)
        rollback(conn)
        conn.execute(
            "update timesheet_closures set reopened_at = now(), reopened_by = %s "
            "where id = %s",
            (USER_DIRECTOR, closure),
        )
        write_hours(conn, sheet, "19.00")

    assert db.execute(
        "select hours->>'regular' from timesheets where id = %s", (sheet,)
    ).fetchone()[0] == "19.00"


def test_manager_sees_closures_of_own_unit_only(db):
    close_unit(db, U_NS1)
    close_unit(db, U_NS2)

    with as_app_user(db, USER_MANAGER) as conn:
        seen = conn.execute(
            "select u.code from timesheet_closures c join units u on u.id = c.unit_id"
        ).fetchall()
    assert [row[0] for row in seen] == ["NS1"]

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select count(*) from timesheet_closures").fetchone()[0] == 2


def test_closures_are_isolated_between_tenants(db):
    close_unit(db, U_NS1)
    close_unit(db, U_OTHER, tenant=T2)

    with as_app_user(db, USER_OTHER) as conn:
        seen = conn.execute("select unit_id from timesheet_closures").fetchall()
    assert [str(row[0]) for row in seen] == [U_OTHER]


def test_closing_another_unit_does_not_leak_through_the_write(db):
    """Чужая точка закрыта — управляющий об этом не узнаёт и её не открывает."""
    closure = close_unit(db, U_NS2)

    with as_app_user(db, USER_MANAGER) as conn:
        changed = conn.execute(
            "update timesheet_closures set reopened_at = now() where id = %s", (closure,)
        ).rowcount
    assert changed == 0
    assert db.execute(
        "select reopened_at from timesheet_closures where id = %s", (closure,)
    ).fetchone()[0] is None


# --- видимость подневных данных по точке (миграция 0030) ---------------------


def make_day(conn, ext_id: str, unit_id: str | None) -> str:
    sheet = make_timesheet(conn, ext_id, unit_id)
    return conn.execute(
        """insert into timesheet_days (tenant_id, timesheet_id, work_date, hour_type, hours)
           values (%s, %s, '2026-06-01', 'regular', 8) returning id""",
        (T1, sheet),
    ).fetchone()[0]


def test_manager_does_not_see_days_of_another_unit(db):
    """Дыра, названная в журнале блока после T019, и закрытая здесь.

    У дня своей точки нет — она у строки-родителя, и без проверки через неё
    прямой запрос отдавал управляющему подневные часы всех точек тенанта.
    """
    mine = make_day(db, "day-ns1", U_NS1)
    make_day(db, "day-ns2", U_NS2)

    with as_app_user(db, USER_MANAGER) as conn:
        seen = conn.execute("select id from timesheet_days").fetchall()
    assert [str(row[0]) for row in seen] == [str(mine)]


def test_director_still_sees_days_of_every_unit(db):
    """Контроль: без него тест выше был бы зелёным и на пустой выборке."""
    make_day(db, "day-all-ns1", U_NS1)
    make_day(db, "day-all-ns2", U_NS2)

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute("select count(*) from timesheet_days").fetchone()[0] == 2


def test_manager_cannot_write_days_of_another_unit(db):
    """Видеть и писать — разные права, и закрыты оба: политика `for all`."""
    sheet = db.execute(
        "select timesheet_id from timesheet_days where id = %s",
        (make_day(db, "day-write-ns2", U_NS2),),
    ).fetchone()[0]

    with as_app_user(db, USER_MANAGER) as conn:
        attempt(conn)
        with pytest.raises(DENIED):
            conn.execute(
                """insert into timesheet_days
                       (tenant_id, timesheet_id, work_date, hour_type, hours)
                   values (%s, %s, '2026-06-03', 'regular', 8)""",
                (T1, sheet),
            )
        rollback(conn)


# =============================================================================
# 2. Экран: закрытие и то, чего он не предлагает
# =============================================================================


@pytest.fixture
def clean_closures(web_env):
    """Ни одного закрытия до теста и после него.

    База веб-тестов общая на прогон: оставленное закрытие сломало бы соседние
    модули, которые правят те же часы. Уборка идёт владельцем базы — у роли
    приложения на это нет права, и это ровно то, что проверяют тесты выше.
    """
    _wipe_closures(web_env)
    yield
    _wipe_closures(web_env)


def _wipe_closures(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "delete from timesheet_closures where tenant_id in "
            "(select id from tenants where code = 'rs-dev')"
        )


def grid_url(client) -> str:
    html = body(client.get(period_url(client)))
    match = re.search(r'href="(/timesheets/[0-9a-f-]+/)"', html)
    assert match, "со страницы периода нет ссылки на табель"
    return match.group(1)


def first_cell(html: str) -> tuple[str, str]:
    match = re.search(r'data-row="([0-9a-f-]{36})" data-kind="([a-z]+)"', html)
    assert match, "в сетке не нашлось ни одной ячейки"
    return match.group(1), match.group(2)


def closable_unit(html: str) -> str:
    """Точка, которую экран предлагает закрыть — так же, как её берёт человек."""
    match = re.search(r'name="unit" value="([0-9a-f-]{36})"', html)
    assert match, f"на табеле нет формы закрытия точки:\n{html[:2000]}"
    return match.group(1)


def test_manager_closes_own_unit_from_the_screen(client, clean_closures):
    login_as(client, "manager")
    url = grid_url(client)
    html = body(client.get(url))
    unit = closable_unit(html)

    response = client.post(f"{url}close/", {"unit": unit})
    assert response.status_code in (200, 302)

    after = body(client.get(url, follow=True))
    assert "закрыт" in after.lower()


def test_closed_unit_refuses_the_cell_and_says_why(client, clean_closures, period_restored):
    login_as(client, "manager")
    url = grid_url(client)
    html = body(client.get(url))
    row_id, kind = first_cell(html)
    client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "12.00"})
    client.post(f"{url}close/", {"unit": closable_unit(html)})

    response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "77.00"})
    assert response.status_code == 409
    text = response.content.decode()
    assert "закрыт" in text.lower()
    # Как и любой отказ на записи ячейки: в поле возвращается то, что в базе.
    assert response["X-Cell-Value"] == "12.00"

    from core.models import Timesheet

    stored = Timesheet.objects.get(pk=row_id).hours or {}
    assert Decimal(str(stored.get(kind, 0))) == Decimal("12.00")


def test_closing_one_unit_does_not_block_the_others_on_screen(
    client, clean_closures, period_restored
):
    """То же требование, что в базе, но по пути, которым ходит человек."""
    from core.models import Membership, Timesheet, User

    login_as(client, "manager")
    url = grid_url(client)
    manager_html = body(client.get(url))
    client.post(f"{url}close/", {"unit": closable_unit(manager_html)})

    own = set(
        Membership.objects.get(user_id=User.objects.get(username="manager").pk).unit_ids
        or []
    )
    alien = (
        Timesheet.objects.filter(period__year=2026).exclude(unit_id__in=own).first()
    )
    assert alien is not None, "в сиде нет строки другой точки — тест бессмысленен"

    login_as(client, "director")
    response = client.post(
        f"{url}cell/", {"row": str(alien.id), "kind": "regular", "hours": "101.00"}
    )
    assert response.status_code == 200, response.content.decode()[:300]
    alien.refresh_from_db()
    assert Decimal(str((alien.hours or {}).get("regular", 0))) == Decimal("101.00")


def test_role_without_the_right_gets_no_button_and_no_action(
    client, clean_closures, admin_without_month_rights
):
    """Интерфейс не предлагает того, что запретит (T064, T072).

    Роль — администратор сети: `unit.close` у него нет, а видит он все точки и
    все регистры, то есть отказ нельзя списать на видимость. Раньше здесь стоял
    бухгалтер; с T115 у него это право есть (D036), и проверка на нём стала бы
    зелёной не по своей причине.
    """
    login_as(client, "admin")
    url = grid_url(client)
    html = body(client.get(url))
    assert 'name="unit"' not in html
    assert "не входит в права вашей роли" in html

    from core.models import Unit

    unit = Unit.objects.filter(tenant__code="rs-dev").first()
    response = client.post(f"{url}close/", {"unit": str(unit.id)})
    assert response.status_code == 403
    assert "не входит в права вашей роли" in response.content.decode()


def test_manager_is_offered_only_his_own_unit(client, clean_closures):
    """У управляющего осталась только своя точка — и на экране тоже (D033).

    Проверяется числом форм, а не наличием одной: «кнопка есть» была бы
    зелёной и в тот момент, когда управляющему предложили бы закрыть всю сеть.
    """
    from core.models import Membership, Unit, User

    login_as(client, "manager")
    html = body(client.get(grid_url(client)))
    offered = re.findall(r'name="unit" value="([0-9a-f-]{36})"', html)

    own = Membership.objects.get(
        user_id=User.objects.get(username="manager").pk
    ).unit_ids or []
    assert len(own) == 1, "у управляющего сида не одна точка — тест надо переписать"
    assert set(offered) == {str(own[0])}
    # Контроль: точек в тенанте больше одной, иначе срез ничего не значит.
    assert Unit.objects.filter(tenant__code="rs-dev").count() > 1


def test_director_closes_and_reopens_any_unit_from_the_screen(client, clean_closures):
    """Директор ведёт месяц целиком: ему предлагают все точки, и они работают.

    Закрывается именно та точка, которая управляющему не своя, — иначе тест
    прошёл бы и при праве «закрывать только свою».
    """
    from core.models import Membership, TimesheetClosure, User

    login_as(client, "director")
    url = grid_url(client)
    html = body(client.get(url))
    offered = re.findall(r'name="unit" value="([0-9a-f-]{36})"', html)
    assert len(offered) > 1, f"директору предложена не вся сеть: {offered}"

    managers_unit = str(
        (Membership.objects.get(user_id=User.objects.get(username="manager").pk).unit_ids
         or [None])[0]
    )
    alien = next(unit for unit in offered if unit != managers_unit)

    assert client.post(f"{url}close/", {"unit": alien}).status_code in (200, 302)
    closure = TimesheetClosure.objects.filter(unit_id=alien, reopened_at__isnull=True)
    assert closure.exists(), "закрытие директора не легло в базу"

    assert client.post(f"{url}reopen/", {"unit": alien}).status_code in (200, 302)
    assert not closure.exists(), "открытие заново не сняло закрытие"


def test_closed_rows_lose_their_input_fields(client, clean_closures):
    """Закрытую точку экран показывает числами, а не полями, которые не примут."""
    login_as(client, "manager")
    url = grid_url(client)
    html = body(client.get(url))
    row_id, _ = first_cell(html)
    client.post(f"{url}close/", {"unit": closable_unit(html)})

    after = body(client.get(url))
    assert f'data-row="{row_id}"' not in after


def test_reopening_from_the_screen_restores_editing(client, clean_closures, period_restored):
    login_as(client, "manager")
    url = grid_url(client)
    html = body(client.get(url))
    row_id, kind = first_cell(html)
    unit = closable_unit(html)

    client.post(f"{url}close/", {"unit": unit})
    client.post(f"{url}reopen/", {"unit": unit})

    response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "9.00"})
    assert response.status_code == 200, response.content.decode()[:300]
