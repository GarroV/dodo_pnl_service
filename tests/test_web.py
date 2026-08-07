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
            """insert into payslips (tenant_id, payrun_id, employee_id, net)
               values (%s, %s, %s, %s) returning id""",
            (tenant, payrun, employee, AMOUNT_OFFICIAL),
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
    """Контекст не выставлен — политики базы обязаны отдать пустоту."""
    response = client.get("/periods/")
    assert response.status_code == 200
    text = body(response)
    assert "2026" not in text
    assert "Dodo Serbia" not in text


def test_period_page_without_login_is_not_found(client):
    login_as(client, "director")
    url = period_url(client)
    client.post("/dev/logout/")
    assert client.get(url).status_code == 404


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


def test_dev_login_page_lists_three_users(client):
    text = body(client.get("/dev/login/"))
    assert "Оперативный директор" in text
    assert "Бухгалтер" in text
    assert "Управляющий точки" in text


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
    """Пока вход ненастоящий, об этом должно быть написано на экране."""
    assert "dev" in body(client.get("/periods/")).lower()


def test_no_template_source_leaks_onto_the_page(client):
    """Многострочный `{# … #}` — не комментарий: он вываливается на страницу текстом.

    Поймано в браузере 2026-08-07, до того страницы начинались с исходника шаблона.
    """
    text = body(client.get("/periods/"))
    assert "{#" not in text
    assert "{% comment" not in text
    assert not text.lstrip().startswith("Базовый шаблон")
