"""
Веб-каркас первой очереди (T041): dev-вход, список периодов, страница периода.

Главное, что здесь проверяется, — не «страница открылась», а то, что страница
показывает ровно тот срез данных, который положен вошедшему. Разграничение живёт
в политиках базы, поэтому тесты гоняются на живом Postgres с накатанной схемой и
данными сида; без Postgres они пропускаются, как и остальные тесты схемы.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

# Фикстуры живого Django и помощники лежат в conftest: база у веб-тестов общая,
# потому что настройки Django читаются один раз на процесс.
from conftest import body, login_as, period_url, wipe_payruns

JUNE = "2026-06-01"

# Суммы для проверки видимости регистров. Разные и круглые, чтобы в HTML их
# нельзя было спутать друг с другом.
AMOUNT_OFFICIAL = Decimal("100000.00")
AMOUNT_SUPPLEMENTARY = Decimal("50000.00")
AMOUNT_INTERNAL = Decimal("25000.00")


@pytest.fixture
def ledger_rows(web_env):
    """Компоненты выплаты в трёх регистрах — материал для проверки видимости.

    Кладём мимо ORM и мимо политик (суперпользователем): здесь проверяется
    показ, а не расчёт, и суммы должны быть узнаваемыми в HTML.

    Расчёты сносим: в той же базе работает `test_payrun`, и без этого итоги
    зависели бы от того, считал ли кто-то период до нас.
    """
    import psycopg

    wipe_payruns(web_env)
    with psycopg.connect(web_env, autocommit=True) as conn:
        tenant = conn.execute("select id from tenants where code = 'rs-dev'").fetchone()[0]
        employee = conn.execute(
            "select id from employees where tenant_id = %s order by external_id limit 1",
            (tenant,),
        ).fetchone()[0]
        payrun = conn.execute(
            """insert into payruns (tenant_id, period) values (%s, %s)
               on conflict (tenant_id, period) do update set period = excluded.period
               returning id""",
            (tenant, JUNE),
        ).fetchone()[0]
        payslip = conn.execute(
            """insert into payslips (tenant_id, payrun_id, employee_id)
               values (%s, %s, %s) returning id""",
            (tenant, payrun, employee),
        ).fetchone()[0]
        for ledger, amount in (
            ("official", AMOUNT_OFFICIAL),
            ("supplementary", AMOUNT_SUPPLEMENTARY),
            ("internal", AMOUNT_INTERNAL),
        ):
            conn.execute(
                """insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
                   values (%s, %s, %s, %s, %s, %s)""",
                (tenant, payslip, f"hours.{ledger}", "Часы", amount, ledger),
            )
    return None


# --- форматирование чисел ----------------------------------------------------


def test_money_format_separates_thousands():
    from web.format import money

    assert money(Decimal("1234.5")) == "1 234,50"


def test_money_zero_differs_from_empty():
    """Требование контракта блока: ноль виден как ноль, пустое — как пустое."""
    from web.format import money

    assert money(Decimal("0")) == "0,00"
    assert money(None) == "—"


def test_hours_format_keeps_two_digits_and_marks_empty():
    """Часы — не деньги: тысяч в них не бывает, а пустое всё равно не ноль."""
    from web.format import hours

    assert hours(Decimal("176")) == "176,00"
    assert hours(Decimal("87.5")) == "87,50"
    assert hours(Decimal("0")) == "0,00"
    assert hours(None) == "—"


# --- контекст пользователя в базе -------------------------------------------


def test_context_lives_only_inside_transaction(web_env):
    """`set local` и `set_config(..., true)`: контекст не переживает транзакцию.

    Если однажды кто-то уберёт LOCAL, настройка утечёт на следующего
    пользователя того же соединения из пула — тест обязан на этом упасть.
    """
    from django.db import connection, transaction

    from web.dbcontext import set_db_context

    user_id = "d1111111-0000-0000-0000-000000000001"
    with transaction.atomic():
        set_db_context(connection, user_id)
        with connection.cursor() as cur:
            cur.execute("select current_setting('app.user_id', true), current_user")
            got_user, got_role = cur.fetchone()
        assert got_user == user_id
        assert got_role == "app_user"

    with connection.cursor() as cur:
        cur.execute("select current_setting('app.user_id', true), current_user")
        after_user, after_role = cur.fetchone()
    assert after_user in ("", None)
    assert after_role != "app_user"


def test_no_session_wide_set_in_source():
    """Сторож на будущее: в модуле контекста не должно появиться `set` без `local`."""
    import re
    from pathlib import Path

    import web.dbcontext as module

    source = Path(module.__file__).read_text()
    # Ищем начало SQL-оператора в литерале: `"set local role ...`
    statements = re.findall(r"""(?i)["'f]\s*set\s+(\w+)""", source)
    assert statements, "в модуле не нашлось ни одного SET — тест перестал что-либо проверять"
    assert all(word.lower() == "local" for word in statements), statements
    assert "set_config(" in source and "true" in source


# --- страницы ----------------------------------------------------------------


def test_root_redirects_to_periods(client):
    response = client.get("/")
    assert response.status_code == 302
    assert response["Location"] == "/periods/"


def test_periods_page_shows_seeded_period(client):
    login_as(client, "director")
    response = client.get("/periods/")
    assert response.status_code == 200
    assert "2026" in body(response)
    assert "Июнь" in body(response) or "июнь" in body(response)


def test_period_page_opens_on_seed_data(client):
    login_as(client, "director")
    response = client.get(period_url(client))
    assert response.status_code == 200
    text = body(response)
    assert "Dodo Serbia" in text
    # Сотрудники сида с табелем за июнь — заготовка под ведомость должна их считать.
    assert "32" in text


def test_unknown_period_is_not_found(client):
    login_as(client, "director")
    response = client.get("/periods/00000000-0000-4000-8000-000000000000/")
    assert response.status_code == 404


def test_without_login_no_data_leaks(client):
    """Не вошёл — страницу с данными не показывают, а уводят на вход.

    Второй контур — политики базы: даже если однажды кто-то снимет проверку
    входа с представления, контекст не выставлен и выборки пусты (это
    проверяется в `test_context_leak.py`).
    """
    response = client.get("/periods/")
    assert response.status_code == 302
    assert "/login/" in response["Location"]

    text = body(client.get("/login/"))
    assert "2026" not in text
    assert "Dodo Serbia" not in text


def test_period_page_without_login_sends_to_the_entrance(client):
    login_as(client, "director")
    url = period_url(client)
    client.post("/logout/")
    response = client.get(url)
    assert response.status_code == 302
    assert "/login/" in response["Location"]


def test_accountant_does_not_see_other_ledgers(client, ledger_rows):
    """Главная проверка: бухгалтеру не видно ни строк чужих регистров, ни их вклада.

    Итог считается по видимому срезу (D023), поэтому у бухгалтера и у директора
    он обязан отличаться.
    """
    from web.format import money

    login_as(client, "director")
    director = body(client.get(period_url(client)))
    assert money(AMOUNT_OFFICIAL) in director
    assert money(AMOUNT_SUPPLEMENTARY) in director
    assert money(AMOUNT_INTERNAL) in director
    assert money(AMOUNT_OFFICIAL + AMOUNT_SUPPLEMENTARY + AMOUNT_INTERNAL) in director

    login_as(client, "accountant")
    accountant = body(client.get(period_url(client)))
    assert money(AMOUNT_OFFICIAL) in accountant
    assert money(AMOUNT_SUPPLEMENTARY) not in accountant
    assert money(AMOUNT_INTERNAL) not in accountant
    assert money(AMOUNT_OFFICIAL + AMOUNT_SUPPLEMENTARY + AMOUNT_INTERNAL) not in accountant


def test_switching_user_changes_who_you_are(client):
    login_as(client, "accountant")
    assert "Бухгалтер" in body(client.get("/periods/"))

    login_as(client, "manager")
    text = body(client.get("/periods/"))
    assert "Управляющий точки" in text
    assert "Бухгалтер" not in text


def test_logout_drops_context(client):
    login_as(client, "director")
    client.post("/dev/logout/")
    assert "Dodo Serbia" not in body(client.get("/periods/"))


def test_login_page_lists_the_dev_shortcuts(client):
    """Кнопки быстрого входа живут на общей странице входа, отдельной больше нет."""
    text = body(client.get("/dev/login/", follow=True))
    assert "Оперативный директор" in text
    assert "Бухгалтер" in text
    assert "Управляющий точки" in text
    assert "Администратор сети" in text


def test_unknown_dev_user_is_rejected(client):
    """Из сессии в контекст базы попадает только известная учётка, не любой uuid."""
    response = client.post("/dev/login/", {"user": "root"})
    assert response.status_code == 400
    assert "Dodo Serbia" not in body(client.get("/periods/"))


def test_dev_login_can_be_switched_off(client):
    """Заглушка обязана выключаться настройкой — иначе она уедет на площадку."""
    from django.test import override_settings

    login_as(client, "director")
    with override_settings(DEV_LOGIN_ENABLED=False):
        assert client.get("/dev/login/").status_code == 404
        # и уже выбранный пользователь перестаёт действовать
        assert "Dodo Serbia" not in body(client.get("/periods/"))


def test_pages_are_marked_as_dev_only(client):
    """Пока на странице входа есть кнопки-ярлыки, об этом написано в шапке."""
    login_as(client, "director")
    assert "dev" in body(client.get("/periods/")).lower()


# --- норма часов на странице периода -----------------------------------------
# Норма часов — свойство производственного календаря страны, а не сотрудника.
# До 2026-08-07 страница брала её из первой попавшейся строки табеля, и число
# зависело от роли: у управляющего выборка сужена его точкой, и первой приходила
# другая строка. Сверка увидела 176,00 у троих и 88,00 у четвёртого.

ROLES_ON_THE_PAGE = ("director", "accountant", "manager", "admin")

# Заведомо не совпадает ни с одной персональной нормой сида (20, 40, 64, 88, 96,
# 120, 176): если страница покажет это число, она точно взяла его из календаря.
FOREIGN_NORM = Decimal("168.00")


def calendars(dsn):
    """Календарь страны в базе веб-тестов: чтение и подмена.

    Мимо ORM и мимо политик — здесь проверяется показ, а не доступ. База у
    веб-тестов общая на прогон, поэтому подменённое значение обязано
    возвращаться: иначе соседние тесты стали бы зависеть от порядка запуска.
    """
    import psycopg

    class Calendars:
        def norm(self):
            with psycopg.connect(dsn, autocommit=True) as conn:
                row = conn.execute(
                    "select norm_hours from calendars where country_code = 'RS' and period = %s",
                    (JUNE,),
                ).fetchone()
            return row[0] if row else None

        def set_norm(self, value):
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    """update calendars set norm_hours = %s
                       where country_code = 'RS' and period = %s""",
                    (value, JUNE),
                )

        def drop(self):
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    "delete from calendars where country_code = 'RS' and period = %s", (JUNE,)
                )

        def restore(self, row):
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    """insert into calendars (country_code, period, norm_hours, working_days)
                       values ('RS', %s, %s, 22)
                       on conflict (country_code, period)
                       do update set norm_hours = excluded.norm_hours""",
                    (JUNE, row),
                )

    return Calendars()


@pytest.fixture
def country_calendar(web_env):
    """Календарь страны с восстановлением исходной нормы после теста."""
    api = calendars(web_env)
    before = api.norm()
    try:
        yield api
    finally:
        api.restore(before)


def norm_hours_shown(text: str) -> str:
    """Число из шапки периода — ровно то, что видит человек глазами."""
    import re

    match = re.search(r"Норма часов.*?<dd>(.*?)</dd>", text, re.S)
    assert match, f"на странице периода нет строки «Норма часов»:\n{text[:2000]}"
    return match.group(1).strip()


def test_norm_hours_is_the_country_calendar_norm_for_every_role(client, web_env):
    """Главная проверка: число одно на всех и равно норме месяца по календарю.

    Роли перебираются не для полноты: дефект был именно в расхождении между
    ними, и проверка одной ролью его не видит.
    """
    from web.format import hours

    expected = calendars(web_env).norm()
    assert expected is not None, "в базе сида нет календаря RS на июнь — проверять нечего"

    seen = {}
    for user in ROLES_ON_THE_PAGE:
        login_as(client, user)
        seen[user] = norm_hours_shown(body(client.get(period_url(client))))

    assert set(seen.values()) == {hours(expected)}, seen


def test_norm_hours_does_not_come_from_a_timesheet(client, country_calendar):
    """Негативный контроль источника: подменяем календарь — меняется страница.

    Число `168` не совпадает ни с одной персональной нормой сида, поэтому
    выборка по табелю показать его не может ни при каком порядке строк.
    """
    from web.format import hours

    country_calendar.set_norm(FOREIGN_NORM)
    for user in ROLES_ON_THE_PAGE:
        login_as(client, user)
        assert norm_hours_shown(body(client.get(period_url(client)))) == hours(FOREIGN_NORM)


def test_without_a_calendar_the_page_says_so_instead_of_guessing(client, country_calendar):
    """Нет календаря — прочерк и объяснение, а не персональная норма из табеля.

    Молча подставить чью-то норму хуже пустоты: неверное число выглядит как
    верное, и заводить календарь никто не пойдёт.
    """
    from web.format import EMPTY, hours

    country_calendar.drop()
    login_as(client, "director")
    text = body(client.get(period_url(client)))

    assert norm_hours_shown(text) == EMPTY
    assert "календар" in text.lower(), "прочерк без объяснения — это молчание, а не ответ"
    # Ни одна персональная норма табеля не подставилась вместо календарной.
    for personal in ("176,00", "88,00", "20,00"):
        assert norm_hours_shown(text) != personal
    assert hours(FOREIGN_NORM) not in text


@pytest.mark.parametrize("user", [None, "director", "accountant", "manager"])
def test_no_template_source_leaks_onto_the_page(client, user):
    """Многострочный `{# … #}` — не комментарий: он вываливается на страницу текстом.

    Поймано в браузере 2026-08-07, до того страницы начинались с исходника шаблона.

    Роли перебираются не для красоты. Первая версия этой проверки смотрела на
    страницу **гостя**, поэтому весь блок шапки за `{% if principal %}` вообще не
    отрисовывался — и второй такой же комментарий, добавленный туда позже,
    проверка пропустила. Нашёл его снова браузер (2026-08-07, T044).
    """
    if user:
        login_as(client, user)
    text = body(client.get("/periods/"))
    assert "{#" not in text
    assert "{% comment" not in text
    assert not text.lstrip().startswith("Базовый шаблон")
