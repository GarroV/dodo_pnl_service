"""Сверка не выдаёт поимённый состав скрытого от роли регистра (T100).

Что было. Раздел «Есть в расчёте, нет в таблице» перечислял всех, у кого сверка
нашла нашу сторону, но не нашла строки в загруженном файле. Нашу сторону
`collect_run` собирает из **двух** выборок с разной видимостью: входы (табель,
условия найма) роль видит по обычным политикам, а итоги — только если ей видны
все регистры учёта (T071). У бухгалтера входы есть, итогов нет — и раздел
превращался в поимённый список тех, у кого выплаты лежат в невидимом ей
регистре. Не вычитанием, а перечислением: «Calloway Iris», «Frost Milo»,
«Nolan Jasper», «Vance Elena» — ровно четверо людей с `ledgers = {internal}`.

Заголовок вдобавок утверждал о базе то, чего роли не показывали: «есть **в
расчёте**». Расчёта этих строк роль не видела — она видела их часы.

Что проверяется здесь и почему именно так.

**Именами, снятыми с базы, а не словами из разметки.** Проверка «на странице нет
слова internal» ловит подпись регистра и не ловит фамилию. Здесь имена берутся
из самой базы по набору регистров строки ведомости и ищутся в ответе.

**Ролью `app_user`.** Страницы открываются `client`, а он ходит путём продукта:
`set local role app_user` в каждом запросе (`web/dbcontext.py`). Владелец схемы
в этой базе суперпользователь, и под ним проверка была бы зелёной при снятых
политиках.

**С обеих сторон.** Рядом стоят проверки, что у директора раздел на месте: чинить
утечку вырезанием раздела для всех — не починка, а потеря приёмки T031.
"""
from __future__ import annotations

from decimal import Decimal as D

import pytest

from conftest import JUNE, PLATA_SAMPLE, body, login_as, period_url, wipe_payruns

SECTION = "Есть в расчёте, нет в таблице"

# Регистры каждой роли сида. Списки разные и снимаются не друг с друга: «чего не
# видно» — свойство роли, и общая константа однажды объявила бы утечкой её
# собственные данные.
LEDGERS = {
    "accountant": ["official"],
    "manager": ["official", "supplementary"],
}


@pytest.fixture
def calculated_june(client, web_env):
    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200
    return None


def names_hidden_from(ledgers: list[str]) -> list[str]:
    """Кого роль с этими регистрами не имеет права прочитать из расчёта.

    Люди, у которых в этом периоде нет ни одного компонента видимого регистра.
    Спрашивается владельцем схемы: колонку `payslips.ledgers` роль приложения не
    читает вовсе (T065), и «как продукт» этот список не собрать намеренно.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """select e.last_name, e.first_name
                 from payslips p
                 join payruns r on r.id = p.payrun_id
                 join employees e on e.id = p.employee_id
                where r.period = %s and not (p.ledgers && %s::ledger[])""",
            [JUNE, ledgers],
        )
        rows = cursor.fetchall()
    assert rows, f"в расчёте сида нет строк вне регистров {ledgers}"
    return [f"{last} {first}".strip() for last, first in rows]


def reconcile_page(client, user: str) -> str:
    login_as(client, user)
    with PLATA_SAMPLE.open("rb") as handle:
        response = client.post(
            period_url(client) + "reconcile/", {"table": handle}, follow=True
        )
    assert response.status_code == 200
    return body(response)


@pytest.mark.parametrize("user", ["accountant", "manager"])
def test_the_reconciliation_names_nobody_from_a_ledger_the_role_cannot_see(
    client, calculated_june, user
):
    """Главная проверка задачи: ни одной фамилии из невидимого регистра."""
    html = reconcile_page(client, user)
    hidden = names_hidden_from(LEDGERS[user])

    for name in hidden:
        assert name not in html, (
            f"сверка {user} называет «{name}» — человека, расчёта которого "
            f"база этой роли не отдала"
        )
    # Отдельной строкой: фамилии и имени по отдельности тоже быть не должно —
    # разметка могла бы разнести их по ячейкам.
    for name in hidden:
        for part in name.split():
            assert part not in html, f"сверка {user} называет «{part}»"


@pytest.mark.parametrize("user", ["accountant", "manager"])
def test_the_section_that_speaks_about_the_calculation_is_not_shown_to_that_role(
    client, calculated_june, user
):
    """Раздел утверждает «есть в расчёте». Расчёта роли не показали — значит нечего.

    Пустой раздел с числом «0» тоже утверждение о базе: «в расчёте нет никого,
    кого не хватает в вашей таблице». Проверять его роль не может, поэтому нет
    ни раздела, ни строки в сводке.
    """
    html = reconcile_page(client, user)

    assert f"<h2>{SECTION}</h2>" not in html, f"{user}: раздел показан"
    assert SECTION not in html, f"{user}: строка сводки утверждает про расчёт"


@pytest.mark.parametrize("user", ["accountant", "manager"])
def test_the_page_says_plainly_that_it_could_not_check_this(
    client, calculated_june, user
):
    """Молча убрать раздел нельзя: пропажа читается как «всё в порядке»."""
    html = reconcile_page(client, user)

    assert "не проверялось" in html, (
        f"{user}: раздел исчез без единого слова о том, что он не проверялся"
    )


def test_the_director_still_gets_the_section_row_for_row(client, calculated_june):
    """Обратная сторона: у роли, которой отдан весь расчёт, приёмка T031 не тронута."""
    from test_reports_reconcile_db import section_rows

    html = reconcile_page(client, "director")
    rows = section_rows(html, SECTION)

    assert len(rows) == 3, f"в разделе директора {len(rows)} строк вместо трёх"
    for _name, why in rows:
        assert "нет" in why, f"факт о загруженном файле размыт: {why!r}"


# --- ядро сверки: оба условия по отдельности ---------------------------------


def test_a_role_without_the_whole_run_gets_no_names_even_with_totals():
    """Первое условие — свойство роли. Итоги в строке его не отменяют."""
    from reports.reconcile import compare
    from test_reports_reconcile import run_line

    result = compare([], {"k": run_line("КУРИР АНА")}, whole_run_visible=False)
    assert result.only_in_run == []


def test_a_row_without_totals_is_not_called_part_of_the_calculation():
    """Второе условие — про саму строку: «есть в расчёте» проверяется итогами.

    Роль видит весь расчёт, но по этой строке итогов нет вовсе — значит расчёта
    по ней и не было. Назвать её «есть в расчёте» было бы неправдой и у
    директора.
    """
    from reports.reconcile import compare
    from test_reports_reconcile import run_line_without_totals

    result = compare(
        [], {"k": run_line_without_totals("КУРИР АНА")}, whole_run_visible=True
    )
    assert result.only_in_run == []


def test_both_conditions_together_still_name_the_row():
    """Порча наоборот: при обоих выполненных условиях строка обязана называться.

    Без этой проверки две предыдущие зеленели бы и от того, что раздел вырезан
    совсем.
    """
    from reports.reconcile import compare
    from test_reports_reconcile import run_line

    result = compare([], {"k": run_line("КУРИР АНА")}, whole_run_visible=True)
    assert [row.name for row in result.only_in_run] == ["КУРИР АНА"]


def test_forgetting_the_flag_hides_names_rather_than_showing_them():
    """Умолчание — закрытое. Забытый параметр обязан молчать, а не выдавать людей."""
    from reports.reconcile import compare
    from test_reports_reconcile import run_line

    assert compare([], {"k": run_line("КУРИР АНА")}).only_in_run == []


def test_the_slice_reports_whether_the_role_was_given_the_whole_run(client, calculated_june):
    """Флаг приходит из базы, а не из головы приложения.

    Спрашивается та же функция, на которой стоят политики
    (`app_sees_every_ledger`), — второй ответ о видимости рядом с первым
    разошёлся бы с ним молча (D014).
    """
    from django.db import connection

    from web.dbcontext import APP_ROLE, USER_SETTING
    from core.management.commands.seed_dev import det_id
    from reports.reconcile import collect_run

    seen = {}
    for user in ("director", "accountant"):
        with connection.cursor() as cursor:
            cursor.execute(f"set local role {APP_ROLE}")
            cursor.execute(
                "select set_config(%s, %s, true)", [USER_SETTING, str(det_id("user", user))]
            )
        try:
            tenant = det_id("tenant", "rs-dev")
            _rows, whole = collect_run(tenant, D and JUNE)
            seen[user] = whole
        finally:
            with connection.cursor() as cursor:
                cursor.execute("reset role")
                cursor.execute("select set_config(%s, '', true)", [USER_SETTING])

    assert seen == {"director": True, "accountant": False}, seen
