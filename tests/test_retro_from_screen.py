"""Правка задним числом достижима из продукта (T121).

D020 описывает **умолчание**: закрытый период не переписывается, разница ложится
помеченной строкой в текущий, со ссылкой на исходный. Переоткрытие месяца с
причиной — альтернатива для партнёра, который настроил иначе.

Построено при этом было ровно наоборот. Все три правки, из которых разница
только и может возникнуть, — правило расчёта, условия найма, производственный
календарь — отклонялись, если дата попадала в утверждённый месяц, и отказ
советовал переоткрыть период. То есть продукт предлагал единственной дорогой
альтернативу, а умолчание доказывалось только тестами: они правили модель мимо
интерфейса (`test_payrun_retro.bump_rates`). Механизм переноса существовал, а
дороги к нему с экрана не было.

Здесь проверяется дорога целиком и именно с экрана:

1. правка с датой внутри закрытого месяца **проходит** — всеми тремя путями;
2. закрытый месяц от неё не сдвигается ни на копейку (построчный слепок);
3. на странице закрытого месяца появляется перенос, и после него в текущем
   периоде стоит помеченная строка, которая называет месяц-источник.

Слепок сверяется целиком, а не по итогу: перестановка сумм между людьми оставила
бы итог прежним.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as, wipe_payruns
from test_directory import post_new_version, terms_restored, victim  # noqa: F401
from test_web_rules import (  # noqa: F401
    NET_FACTOR,
    approve_june,
    june_snapshot,
    overrides_restored,
    payruns_restored,
    sql,
)

JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)
# Дата внутри утверждённого месяца — та самая, которую продукт не принимал.
INSIDE_JUNE = "2026-06-15"


def june_url(client) -> str:
    """Ссылка на страницу июня — так же, как её берёт человек со списка."""
    import re

    html = body(client.get("/periods/"))
    found = re.findall(r'href="(/periods/[0-9a-f-]+/)"', html)
    assert found, f"на списке периодов нет ссылок:\n{html}"
    for url in found:
        page = body(client.get(url))
        if "2026" in page and "юнь" in page:
            return url
    raise AssertionError("на списке периодов не нашлось июня")


def post_rule(client, path: str, *, value: str, valid_from: str):
    return client.post(f"/rules/{path}/", {"value": value, "valid_from": valid_from})


# =============================================================================
# 1. Правка проходит, а закрытый месяц остаётся прежним
# =============================================================================


def test_a_rule_version_inside_a_closed_month_is_accepted(
    client, sql, web_env, overrides_restored, payruns_restored,  # noqa: F811
):
    """Правило расчёта с датой внутри июня заводится, июнь не двигается."""
    approve_june(client, web_env)
    before = june_snapshot(sql)
    assert before, "июнь не посчитался — проверять нечего"

    login_as(client, "admin")
    answer = post_rule(client, NET_FACTOR, value="0.65", valid_from=INSIDE_JUNE)
    assert answer.status_code == 302, (
        f"правка задним числом снова отклонена: {answer.status_code}\n{body(answer)}"
    )
    client.post("/logout/")

    assert sql.execute(
        "select count(*) from rule_overrides where path = %s and valid_from = %s",
        (NET_FACTOR, INSIDE_JUNE),
    ).fetchone()[0] == 1, "версия правила не завелась"
    assert june_snapshot(sql) == before, "строки закрытого июня изменились"


def test_a_terms_version_inside_a_closed_month_is_accepted(
    client, sql, web_env, terms_restored, payruns_restored,  # noqa: F811
):
    """Условия найма с датой внутри июня заводятся, июнь не двигается."""
    approve_june(client, web_env)
    before = june_snapshot(sql)
    assert before, "июнь не посчитался — проверять нечего"
    _, employee_id, _from, _to, _rate, _group = victim(sql)

    login_as(client, "admin")
    answer = post_new_version(
        client, sql, employee_id, valid_from=INSIDE_JUNE, rate="777.0000",
    )
    assert answer.status_code == 302, (
        f"правка задним числом снова отклонена: {answer.status_code}\n{body(answer)}"
    )
    client.post("/logout/")

    assert sql.execute(
        "select count(*) from employment_terms where employee_id = %s and valid_from = %s",
        (employee_id, INSIDE_JUNE),
    ).fetchone()[0] == 1, "версия условий найма не завелась"
    assert june_snapshot(sql) == before, "строки закрытого июня изменились"


def test_the_calendar_of_a_closed_month_is_accepted(
    client, sql, web_env, payruns_restored,  # noqa: F811
):
    """Норма часов закрытого месяца правится, а сам месяц остаётся прежним."""
    approve_june(client, web_env)
    before = june_snapshot(sql)
    was = sql.execute(
        "select norm_hours, working_days from calendars "
        "where country_code = 'RS' and period = '2026-06-01'"
    ).fetchone()

    login_as(client, "admin")
    try:
        answer = client.post("/directory/calendar/2026-06/", {
            "norm_hours": "100", "working_days": "12",
        })
        assert answer.status_code == 302, (
            f"календарь закрытого месяца снова отклонён: {answer.status_code}"
        )
        assert sql.execute(
            "select norm_hours from calendars "
            "where country_code = 'RS' and period = '2026-06-01'"
        ).fetchone()[0] == Decimal("100.00"), "норма часов не записалась"
        assert june_snapshot(sql) == before, "строки закрытого июня изменились"
    finally:
        sql.execute(
            "update calendars set norm_hours = %s, working_days = %s "
            "where country_code = 'RS' and period = '2026-06-01'",
            was,
        )
        client.post("/logout/")


# =============================================================================
# 2. Продукт говорит, что случится, — до правки и после неё
# =============================================================================


def test_the_form_no_longer_promises_a_refusal(
    client, web_env, payruns_restored,  # noqa: F811
):
    """Страница правила обещала отказ. Отказа больше нет — обещания тоже.

    Проверяется пара: обещания отказа нет **и** сказано, куда денется разница.
    Половина этого (просто убрать фразу) оставила бы человека без ответа на
    единственный вопрос, который у него здесь есть: что будет с закрытым месяцем.
    """
    approve_june(client, web_env)
    login_as(client, "admin")
    html = body(client.get(f"/rules/{NET_FACTOR}/"))
    client.post("/logout/")

    assert "будет отклонена" not in html, (
        "страница по-прежнему обещает отказ, которого больше нет"
    )
    assert "2026-06-30" in html, "страница не называет границу утверждённой зарплаты"
    assert "разниц" in html.lower(), (
        "страница не говорит, куда денется разница с закрытым месяцем"
    )


def test_the_screen_says_the_closed_month_did_not_change(
    client, sql, web_env, overrides_restored, payruns_restored,  # noqa: F811
):
    """После правки человек читает, что случилось с закрытым месяцем.

    Молчание здесь — тот же дефект, что и отказ: человек, набравший июньскую
    дату, обязан узнать, что июнь остался прежним, а разница ждёт переноса.
    """
    approve_june(client, web_env)
    login_as(client, "admin")
    answer = post_rule(client, NET_FACTOR, value="0.65", valid_from=INSIDE_JUNE)
    assert answer.status_code == 302
    html = body(client.get(answer["Location"]))
    client.post("/logout/")

    assert "разниц" in html.lower(), "страница молчит о разнице с закрытым месяцем"


# =============================================================================
# 3. Разница доезжает до текущего периода — с экрана и до конца
# =============================================================================


@pytest.fixture
def july_ready(web_env):
    """Июль, которому есть что считать: свой период и свои табели.

    Заводится и убирается здесь целиком. Оставленный июль становится «первым
    периодом» для каждого теста, который берёт первую ссылку со списка, — и
    ломает их молча, вдалеке отсюда (тот же приём, что у `june_approved`).
    """
    from core.models import Period, Tenant, Timesheet

    tenant = Tenant.objects.get(code="rs-dev")
    Period.objects.get_or_create(tenant=tenant, period=JULY, defaults={"status": "open"})
    for sheet in list(Timesheet.objects.filter(period=JUNE)):
        sheet.pk = None
        sheet.id = None
        sheet.period = JULY
        sheet.save()
    try:
        yield tenant
    finally:
        wipe_payruns(web_env)
        Timesheet.objects.filter(period=JULY).delete()
        Period.objects.filter(tenant=tenant, period=JULY).delete()


def test_the_difference_from_a_screen_edit_reaches_the_current_period(
    client, sql, web_env, terms_restored, july_ready,  # noqa: F811
):
    """ГЛАВНАЯ ПРОВЕРКА ЗАДАЧИ: путь целиком, и весь он с экрана.

    Правка условий найма с июньской датой → страница июня предлагает перенос →
    перенос → июль посчитан → в июльской ведомости стоит помеченная строка,
    называющая июнь. И июнь при этом байт в байт прежний.
    """
    # Июнь считается и утверждается по своей ссылке, а не по первой в списке:
    # июль уже заведён фикстурой и стоит в списке выше.
    wipe_payruns(web_env)
    login_as(client, "director")
    url = june_url(client)
    assert client.post(url + "calculate/", {"inline": "1"}, follow=True).status_code == 200
    assert client.post(url + "approve/", follow=True).status_code == 200
    client.post("/logout/")

    before = june_snapshot(sql)
    assert before, "июнь не посчитался — проверять нечего"
    _, employee_id, _from, _to, _rate, _group = victim(sql)

    login_as(client, "admin")
    assert post_new_version(
        client, sql, employee_id, valid_from=INSIDE_JUNE, rate="777.0000",
    ).status_code == 302
    client.post("/logout/")

    login_as(client, "director")
    page = body(client.get(url))
    assert "Перенести разницу" in page, (
        "страница закрытого месяца не предлагает перенести разницу — "
        "правка задним числом опять недостижима"
    )

    moved = client.post(url + "retro/", follow=True)
    assert moved.status_code == 200, moved.status_code
    assert sql.execute(
        "select count(*) from retro_adjustments "
        "where source_period = '2026-06-01' and target_period = '2026-07-01' "
        "and cancelled_at is null"
    ).fetchone()[0] > 0, "перенос не записался"
    assert june_snapshot(sql) == before, "перенос сдвинул закрытый июнь"

    july = next(
        link for link in _period_links(client) if "юль" in body(client.get(link))
    )
    assert client.post(july + "calculate/", {"inline": "1"}, follow=True).status_code == 200
    sheet = body(client.get(july))
    client.post("/logout/")

    assert "Перерасчёт за" in sheet, "в июльской ведомости нет помеченной строки"
    assert "юнь" in sheet, "помеченная строка не называет месяц-источник"
    assert june_snapshot(sql) == before, "расчёт июля сдвинул закрытый июнь"


def _period_links(client) -> list[str]:
    import re

    return re.findall(r'href="(/periods/[0-9a-f-]+/)"', body(client.get("/periods/")))
