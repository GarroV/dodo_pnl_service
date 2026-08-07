"""
Настоящий вход, четыре роли и контекст пользователя в базе (T014).

Что здесь проверяется и почему именно это:

1. **Вход один.** Пароль проверяется штатным механизмом Django, личность
   попадает в сессию, а из сессии — в контекст базы. Другого пути получить
   контекст нет: dev-вход отличается только тем, чем доказывают личность.
2. **Роль решает, что видно.** Не интерфейс: для каждой из четырёх ролей спеки
   проверяется срез, который отдаёт сама база под её контекстом.
3. **Контекст живёт только в транзакции.** Попытка выставить его вне
   транзакции обязана падать явно: `set_config(..., true)` вне транзакции
   Postgres молча игнорирует, и «защита» превратилась бы в пустое место.

Тесты гоняются на живом Postgres с сидом (фикстура `web_env` в conftest); без
Postgres пропускаются вместе с остальными тестами схемы.
"""
from __future__ import annotations

import logging
import re
from decimal import Decimal
from pathlib import Path

import pytest

from conftest import body, login_as, period_url, wipe_payruns

JUNE = "2026-06-01"

# Суммы разные и круглые: в HTML их нельзя спутать друг с другом.
AMOUNT_OFFICIAL = Decimal("111000.00")
AMOUNT_SUPPLEMENTARY = Decimal("22000.00")
AMOUNT_INTERNAL = Decimal("3300.00")

# Ожидания по ролям спеки. Регистры и точки — то, ради чего роль вообще нужна.
# Управляющий видит два регистра, хотя спека называет один: так заведено сидом
# блока `db`, и на этих числах снята приёмка второй очереди. Расхождение
# спеки и сида — вопрос владельцу, а не решение блока auth.
ROLE_LEDGERS = {
    "director": {"official", "supplementary", "internal"},
    "accountant": {"official"},
    "manager": {"official", "supplementary"},
    "admin": {"official"},
}


def user_id_of(code: str):
    from core.management.commands.seed_dev import det_id

    return det_id("user", code)


def login_real(client, username: str, password: str):
    """Войти так, как это делает человек: логин и пароль на странице входа."""
    return client.post("/login/", {"username": username, "password": password})


@pytest.fixture
def seed_password(web_env) -> str:
    from core.management.commands.seed_dev import SEED_PASSWORD

    return SEED_PASSWORD


@pytest.fixture
def three_ledgers(web_env):
    """Компоненты трёх регистров на одном сотруднике — материал для срезов."""
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


# --- вход --------------------------------------------------------------------


def test_login_page_is_open_to_anonymous(client):
    response = client.get("/login/")
    assert response.status_code == 200
    text = body(response)
    assert 'name="username"' in text
    assert 'name="password"' in text


def test_right_password_lets_in(client, seed_password):
    response = login_real(client, "director", seed_password)
    assert response.status_code == 302, body(response)
    assert "Dodo Serbia" in body(client.get("/periods/"))


def test_wrong_password_is_refused_and_gives_no_context(client, seed_password):
    response = login_real(client, "director", seed_password + "-нет")
    assert response.status_code == 200, "неверный пароль не должен пускать дальше"
    assert "Dodo Serbia" not in body(response)
    # И на страницу с данными такой «вход» не пускает.
    assert client.get("/periods/").status_code == 302


def test_unknown_user_is_refused(client, seed_password):
    response = login_real(client, "никого-нет", seed_password)
    assert response.status_code == 200
    assert client.get("/periods/").status_code == 302


def test_inactive_user_cannot_log_in(client, seed_password):
    """Отключённая учётка не входит — иначе увольнение не имело бы силы."""
    from core.models import User

    User.objects.filter(username="admin").update(is_active=False)
    try:
        login_real(client, "admin", seed_password)
        assert client.get("/periods/").status_code == 302
    finally:
        User.objects.filter(username="admin").update(is_active=True)


def test_password_is_stored_hashed(web_env):
    """Своей криптографии нет: пароль лежит хэшем штатного механизма Django."""
    import psycopg

    from core.management.commands.seed_dev import SEED_PASSWORD

    with psycopg.connect(web_env) as conn:
        stored = conn.execute(
            "select password from users where username = 'director'"
        ).fetchone()[0]
    assert stored.startswith("pbkdf2_"), stored[:20]
    assert SEED_PASSWORD not in stored


def test_logout_ends_the_session(client, seed_password):
    login_real(client, "director", seed_password)
    assert client.get("/periods/").status_code == 200
    client.post("/logout/")
    assert client.get("/periods/").status_code == 302


def test_password_change_works(client, web_env):
    """Смена пароля: старый перестаёт действовать, новый действует.

    Учётка заводится отдельная и удаляется: менять пароль сидовой — значит
    сломать соседние тесты, которые входят ею же.
    """
    from core.models import User

    user = User.objects.create_user("pwtest", "старый-пароль-1", full_name="Проверка")
    try:
        login_real(client, "pwtest", "старый-пароль-1")
        response = client.post(
            "/account/password/",
            {
                "old_password": "старый-пароль-1",
                "new_password1": "новый-пароль-2",
                "new_password2": "новый-пароль-2",
            },
        )
        assert response.status_code == 302, body(response)

        client.post("/logout/")
        assert login_real(client, "pwtest", "старый-пароль-1").status_code == 200
        assert login_real(client, "pwtest", "новый-пароль-2").status_code == 302
    finally:
        User.objects.filter(pk=user.pk).delete()


def test_password_change_needs_the_old_one(client, web_env):
    from core.models import User

    user = User.objects.create_user("pwtest2", "старый-пароль-1")
    try:
        login_real(client, "pwtest2", "старый-пароль-1")
        response = client.post(
            "/account/password/",
            {
                "old_password": "не-тот",
                "new_password1": "новый-пароль-2",
                "new_password2": "новый-пароль-2",
            },
        )
        assert response.status_code == 200
        client.post("/logout/")
        assert login_real(client, "pwtest2", "новый-пароль-2").status_code == 200
        assert login_real(client, "pwtest2", "старый-пароль-1").status_code == 302
    finally:
        User.objects.filter(pk=user.pk).delete()


def test_anonymous_sees_only_the_way_in(client):
    """Неавторизованному не показывают ни данных, ни страниц с ними."""
    assert client.get("/login/").status_code == 200
    for url in ("/periods/", "/periods/00000000-0000-4000-8000-000000000000/"):
        response = client.get(url)
        assert response.status_code == 302, url
        assert "/login/" in response["Location"], url


def test_user_row_is_invisible_without_context(web_env):
    """Учётки закрыты той же RLS, что и данные: без контекста их не видно."""
    from core.models import User
    from web.dbcontext import db_context

    with db_context(None):
        assert User.objects.count() == 0


def test_user_sees_only_his_own_row(web_env):
    from core.models import User
    from web.dbcontext import db_context

    with db_context(user_id_of("director")):
        names = list(User.objects.values_list("username", flat=True))
    assert names == ["director"], names


# --- четыре роли спеки -------------------------------------------------------


def test_all_four_roles_are_seeded(web_env):
    import psycopg

    with psycopg.connect(web_env) as conn:
        rows = dict(
            conn.execute(
                """select r.code, u.username from roles r
                     join memberships m on m.role_id = r.id
                     join users u on u.id = m.user_id
                    where r.tenant_id = (select id from tenants where code = 'rs-dev')"""
            ).fetchall()
        )
    assert set(rows) == set(ROLE_LEDGERS), rows


@pytest.mark.parametrize("code", sorted(ROLE_LEDGERS))
def test_role_decides_what_the_database_returns(code, three_ledgers):
    """Срез считает база под контекстом роли, а не интерфейс на выводе."""
    from core.models import PayComponent
    from web.dbcontext import db_context

    with db_context(user_id_of(code)):
        ledgers = set(PayComponent.objects.values_list("ledger", flat=True))
    assert ledgers == ROLE_LEDGERS[code]


@pytest.mark.parametrize("code", sorted(ROLE_LEDGERS))
def test_principal_carries_role_units_and_ledgers(client, seed_password, code):
    """`Principal` — то, чем блок отдаёт роль наружу: точки, регистры, права."""
    login_real(client, code, seed_password)
    response = client.get("/periods/")
    assert response.status_code == 200
    who = response.context["principal"]

    assert set(who.visible_ledgers) == ROLE_LEDGERS[code]
    assert who.tenant_id is not None
    assert who.permissions, "роль без единого права — значит право не заведено"
    # Управляющий привязан к одной точке, остальные роли — ко всем.
    assert bool(who.unit_ids) is (code == "manager")


def test_network_admin_does_not_write_payroll_data(client, seed_password, web_env):
    """Администратор сети ведёт справочники, но не правит данные расчёта.

    Отказ даёт не интерфейс: расчёт пишет строки во все регистры, а роль,
    которая их не видит, записать их не может — база отвергнет вставку.
    """
    import psycopg

    wipe_payruns(web_env)
    login_real(client, "admin", seed_password)
    response = client.post(period_url(client) + "calculate/", follow=True)
    assert response.status_code == 403

    with psycopg.connect(web_env) as conn:
        assert conn.execute("select count(*) from payslips").fetchone()[0] == 0


# --- dev-вход не обходной путь ----------------------------------------------


def test_dev_login_goes_through_the_same_door(client):
    """Заглушка не заводит второго пути: она логинит настоящую учётку."""
    login_as(client, "director")
    assert client.session.get("_auth_user_id") == str(user_id_of("director"))


def test_dev_session_stops_working_when_dev_login_is_off(client):
    """Выключили dev-вход — выданная им сессия перестаёт действовать сразу."""
    from django.test import override_settings

    login_as(client, "director")
    assert client.get("/periods/").status_code == 200
    with override_settings(DEV_LOGIN_ENABLED=False):
        assert client.get("/periods/").status_code == 302


def test_real_session_survives_switching_dev_login_off(client, seed_password):
    """А настоящий вход от флага не зависит — иначе флаг стал бы выключателем входа."""
    from django.test import override_settings

    login_real(client, "director", seed_password)
    with override_settings(DEV_LOGIN_ENABLED=False):
        assert client.get("/periods/").status_code == 200


def test_dev_login_is_off_by_default_outside_debug():
    """На площадке (`DJANGO_DEBUG=0`) заглушка выключена, пока её не включили явно."""
    from web.auth import dev_login_enabled

    assert dev_login_enabled({}, debug=True) is True
    assert dev_login_enabled({}, debug=False) is False
    assert dev_login_enabled({"DEV_LOGIN": "1"}, debug=False) is True
    assert dev_login_enabled({"DEV_LOGIN": "0"}, debug=True) is False


# --- контекст: транзакция обязательна ----------------------------------------


def test_context_outside_transaction_fails_loudly(web_env):
    """Вне транзакции `set_config(..., true)` не действует — это обязано быть отказом.

    Postgres в таком случае лишь пишет предупреждение в свой лог, а приложение
    поехало бы дальше: роль не переключена, контекст пуст. Молчаливая
    «защита» хуже отсутствующей.
    """
    from django.db import connection

    from web.dbcontext import ContextOutsideTransaction, set_db_context

    assert not connection.in_atomic_block
    with pytest.raises(ContextOutsideTransaction):
        set_db_context(connection, str(user_id_of("director")))


def test_context_is_verified_not_assumed(web_env):
    """Выставили — прочитали обратно: иначе «выставили» проверялось бы по коду."""
    from django.db import connection, transaction

    from web.dbcontext import set_db_context

    with transaction.atomic():
        set_db_context(connection, str(user_id_of("director")))
        with connection.cursor() as cur:
            cur.execute("select current_setting('app.user_id', true), current_user")
            got_user, got_role = cur.fetchone()
    assert got_user == str(user_id_of("director"))
    assert got_role == "app_user"


def test_broken_session_gives_emptiness_not_a_crash(client):
    """В сессии мусор вместо uuid — пустота, а не 500 и не чужие данные."""
    session = client.session
    session["_auth_user_id"] = "не-uuid"
    session.save()
    assert client.get("/periods/").status_code == 302


# --- контракт с представлениями ---------------------------------------------


def test_views_never_filter_by_the_principal(web_env):
    """Представление не фильтрует по тенанту вошедшего — это работа политик базы.

    Ищем именно фильтр по данным принципала (`tenant_id=who...`): доменный
    фильтр «табели этого периода этого партнёра» — не разграничение доступа,
    он берёт тенанта у самой строки, а не у пользователя.
    """
    import web.views as views

    source = Path(views.__file__).read_text()
    bad = re.findall(r"(?:tenant|tenant_id|unit_ids)\s*=\s*(?:who|principal)\b", source)
    assert bad == [], bad


def test_no_identifiers_or_amounts_in_the_log(client, seed_password, three_ledgers, caplog):
    """В логи не попадают ни учётки, ни суммы (конституция, раздел о данных).

    Уровень INFO и выше — то, с чем продукт работает на площадке. Отладочный
    журнал SQL (уровень DEBUG) сюда не относится: он включается вместе с
    `DJANGO_DEBUG` и на площадке выключен.
    """
    with caplog.at_level(logging.INFO):
        login_real(client, "director", seed_password)
        client.get(period_url(client))
    written = "\n".join(record.getMessage() for record in caplog.records)
    assert str(user_id_of("director")) not in written
    assert "111000" not in written
