"""Хранимый след расчёта: закрытый месяц объясняется тем, чем считался (T056).

Почему это отдельный файл, а не строки в `test_reports_trace.py`: там проверен
**экран** следа — отбор видимого, сложение, разрезы. Здесь проверено другое
свойство, и проверить его можно только на живой базе: что объяснение
**записано вместе с расчётом** и переживает изменение правил.

Главная проверка ровно одна, остальные её страхуют: посчитать, утвердить,
поменять правило задним числом, открыть след — и увидеть **прежние** числа.
Пока след пересобирался, эта проверка была невозможна в принципе: пересборка
по определению даёт сегодняшние правила.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from conftest import (
    T1,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
    body,
    login_as,
    pay_component,
    period_url,
    wipe_payruns,
)

# Надбавка страны: 1500 за полную норму. На ней проверяется хранение, потому что
# она одна и та же у всех и её легко подменить одним переопределением.
BONUS_PATH = "allowances.meal_and_vacation_bonus.amount_per_norm"
BONUS_WAS = Decimal("1500")
BONUS_NOW = Decimal("2000")


def trace_urls(client, user: str) -> list[str]:
    """Адреса следов со страницы периода — так же, как их берёт человек."""
    login_as(client, user)
    html = body(client.get(period_url(client)))
    found = re.findall(r'<td class="num strong"><a class="trace" href="([^"]+)"', html)
    assert found, "в ведомости нет ни одной ссылки на след расчёта"
    return [url.replace("&amp;", "&") for url in found]


def bonus_step(html: str) -> str:
    """Кусок разметки шага надбавки — по нему сверяются числа объяснения."""
    match = re.search(
        r'<tr[^>]*>(?:(?!</tr>).)*meal_and_vacation_bonus(?:(?!</tr>).)*</tr>', html, re.S
    )
    assert match, "на следе нет шага надбавки, а он есть в каждой строке"
    return match.group(0)


def per_norm(html: str) -> str:
    """Вход «за полную норму» из шага надбавки — само переопределяемое число.

    Сверяется именно вход, а не сумма шага: у неполной ставки сумма прорастает
    пропорцией и совпасть с прежней может по совпадению, а вход — это ровно то
    значение правила, которым считали.
    """
    step = bonus_step(html)
    match = re.search(r'за полную норму\s*<b>([^<]+)</b>', step)
    assert match, f"в шаге надбавки нет входа «за полную норму»:\n{step}"
    return match.group(1).strip()


def override_bonus(dsn: str, value: Decimal) -> None:
    """Поменять правило страны задним числом — тем же способом, что и продукт.

    Переопределение с датой начала действия **раньше** периода: это и есть
    случай из issue #48, когда пересобранный след объяснял бы закрытый месяц
    правилами, которых в нём не было.
    """
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            """insert into rule_overrides (tenant_id, scope_type, path, value, valid_from)
               select id, 'tenant', %s, %s::jsonb, '2026-01-01' from tenants
                where code = 'rs-dev'""",
            (BONUS_PATH, str(value)),
        )


def drop_overrides(dsn: str) -> None:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("delete from rule_overrides where path = %s", (BONUS_PATH,))


@pytest.fixture
def june_calculated_and_approved(client, web_env):
    """Посчитанный и утверждённый июнь — и уборка за собой в любом исходе.

    Уборка обязательна: база `web_env` одна на весь прогон, а переопределение
    правила, оставленное после теста, сдвинуло бы контрольные суммы у всех, кто
    считает период после.
    """
    wipe_payruns(web_env)
    login_as(client, "director")
    url = period_url(client)
    assert client.post(url + "calculate/", follow=True).status_code == 200
    assert client.post(url + "approve/", follow=True).status_code == 200
    try:
        yield url
    finally:
        drop_overrides(web_env)
        wipe_payruns(web_env)


def test_the_trace_of_a_closed_period_survives_a_rule_change(
    client, web_env, june_calculated_and_approved
):
    """Приёмка T056: посчитали, утвердили, поменяли правило — объяснение прежнее.

    До хранения следа этот тест был невозможен: экран пересобирал объяснение по
    сегодняшним правилам и показал бы 2000 там, где выплачено 1500.
    """
    url = trace_urls(client, "director")[0]
    before = per_norm(body(client.get(url)))
    assert before == "1 500,00", f"надбавка в следе не та, что в правилах: {before}"

    override_bonus(web_env, BONUS_NOW)

    after = per_norm(body(client.get(url)))
    assert after == "1 500,00", (
        "след закрытого периода объяснён сегодняшними правилами, "
        f"а не теми, которыми считали: {after}"
    )


def test_the_stored_trace_is_written_by_the_calculation(
    client, web_env, june_calculated_and_approved
):
    """Шаг есть у каждого компонента: объяснение записано целиком, а не частями."""
    import psycopg

    with psycopg.connect(web_env, autocommit=True) as conn:
        components, steps = conn.execute(
            """select
                   (select count(*) from pay_components
                     where retro_source_period is null),
                   (select count(*) from payslip_steps where kind = 'net')"""
        ).fetchone()
    assert components > 0, "материал теста собран не про тот случай: расчёта нет"
    assert steps == components, (
        f"компонентов {components}, а сохранённых шагов {steps} — "
        "объяснение записано не для всех сумм"
    )


def test_the_page_says_the_trace_is_the_stored_one(
    client, web_env, june_calculated_and_approved
):
    """Экран обязан назвать источник объяснения, а не показывать числа молча."""
    html = body(client.get(trace_urls(client, "director")[0]))
    assert "сохранён" in html, "экран не говорит, что показывает сохранённое объяснение"
    assert "пересобран" not in html, (
        "экран говорит о пересборке, хотя показывает сохранённый след"
    )


def test_no_row_of_a_fresh_calculation_disagrees_with_its_own_trace(
    client, web_env, june_calculated_and_approved
):
    """Две записи одного расчёта обязаны сходиться на каждой строке, а не на первой.

    Сохранённый шаг сверяется с сохранённой суммой по паре «код + регистр».
    Пара, встретившаяся дважды, сложилась бы в одну сумму и разошлась бы с
    каждым из своих шагов — то есть «не сходится» у всех подряд сразу после
    расчёта. Проверяется на всех строках, потому что ловится это только так.
    """
    for user in ("director", "accountant", "manager"):
        for url in trace_urls(client, user):
            html = body(client.get(url))
            assert "не сходится" not in html, f"свежий расчёт разошёлся со своим следом: {url}"


# --- D023 на новой поверхности: ни строк, ни следа ---------------------------
# Хранимый след — это сумма, правило и человек в одной строке базы. Проверяется
# **ролью приложения**: владелец таблиц и суперпользователь обходят RLS, и
# политику можно написать неправильно, получив зелёный прогон.


def step_of(conn, ledger: str | None, *, code: str, amount: str = "1000.00") -> str:
    """Сохранённый шаг нужного регистра рядом с его компонентом."""
    component = pay_component(conn, ledger=ledger or "official", amount=amount, code=code)
    payslip_id = conn.execute(
        "select payslip_id from pay_components where id = %s", (component,)
    ).fetchone()[0]
    return conn.execute(
        """insert into payslip_steps
               (tenant_id, payslip_id, position, code, title, applied_value, ledger, kind)
           values (%s, %s, 0, %s, 'Часы', %s, %s, %s) returning id""",
        (T1, payslip_id, code, amount, ledger, "net" if ledger else "gross"),
    ).fetchone()[0]


def test_a_stored_step_of_an_invisible_ledger_is_invisible(db):
    """Шаг чужого регистра не виден роли — ни числом, ни фактом существования.

    Ролью для проверки взят управляющий, а не бухгалтер. После D036 у
    бухгалтера в сиде полный набор регистров (изначально проверка стояла на
    ней) — её выборка сейчас была бы про читаемость всего, а не про сокрытие.
    Управляющий (D031) — единственная роль сида с неполным набором: видит
    официальный и дополнительный, но не внутренний, ровно тот случай, который
    здесь нужен.
    """
    step_of(db, "official", code="official.one")
    step_of(db, "internal", code="internal.one", amount="200.00")

    with as_app_user(db, USER_MANAGER) as conn:
        seen = conn.execute("select ledger, applied_value from payslip_steps").fetchall()
    assert [(ledger, value) for ledger, value in seen] == [("official", 1000)]

    with as_app_user(db, USER_DIRECTOR) as conn:
        count = conn.execute("select count(*) from payslip_steps").fetchone()[0]
    assert count == 2, "материал теста собран не про тот случай: директор видит не всё"


def test_the_ledger_check_is_meaningful(db):
    """Страховка от фиктивной зелени: без политики проверка выше обязана краснеть.

    Владельцем схемы (без `set role app_user`) видно оба шага. Если бы тест выше
    случайно ходил не ролью приложения, он был бы зелёным при любой политике.
    """
    step_of(db, "official", code="official.one")
    step_of(db, "internal", code="internal.one", amount="200.00")
    assert db.execute("select count(*) from payslip_steps").fetchone()[0] == 2


def test_a_derived_step_is_closed_by_role_not_by_row(db):
    """Производные величины видит тот, кому видны все регистры (T071).

    Условие роли, а не строки — в этом вся суть. Была бы политика по содержимому
    строки, наличие или отсутствие бруто само стало бы поимённым списком тех, у
    кого есть выплаты в закрытом регистре: ровно та утечка, которую закрыла
    миграция 0023.

    Управляющий (D031) — единственная роль сида с неполным набором, и только
    он должен производную величину не увидеть. Бухгалтер после D036 набором
    равна директору (обе видят все регистры) и здесь проверена в паре с ним —
    иначе проверка «не видит» стояла бы на роли, которая после решения D036
    видит, и была бы неверна по существу, а не молча зелена по формулировке.
    """
    step_of(db, None, code="gross")

    with as_app_user(db, USER_MANAGER) as conn:
        seen = conn.execute(
            "select count(*) from payslip_steps where ledger is null"
        ).fetchone()[0]
    assert seen == 0, "управляющий видит производную величину, а видит не все регистры"

    for user in (USER_DIRECTOR, USER_ACCOUNTANT):
        with as_app_user(db, user) as conn:
            seen = conn.execute(
                "select count(*) from payslip_steps where ledger is null"
            ).fetchone()[0]
        assert seen == 1, f"{user} видит все регистры, а производной величины не видит"
