"""Сверка не выдаёт поимённый состав скрытого от роли регистра (T100).

Что было. Раздел «Есть в расчёте, нет в таблице» перечислял всех, у кого сверка
нашла нашу сторону и не нашла строки в загруженном файле. Нашу сторону
`collect_run` собирает из **двух** выборок с разной видимостью: входы (табель,
условия найма) роль видит по обычным политикам, а итоги — только если ей видны
все регистры учёта (T071). Значит в разность попадал всякий, чьи часы роли
видны, а деньги нет, — то есть ровно люди скрытого регистра. Воспроизведено на
живом стенде до правки: у бухгалтера в разделе стояли «Курир Ана» и «Курир
Марко» (обе строки `{internal}`), у управляющего — «Курир Ана» с его точки.

Заголовок вдобавок утверждал о базе то, чего роли не показывали: «есть **в
расчёте**». Расчёта этих строк роль не видела — она видела их часы.

Что проверяется здесь и почему именно так.

**Главный инвариант — не список запретных имён, а происхождение имени.** На
экране сверки роли, которой расчёт отдан не весь, не может стоять ни одного
имени, которого нет в **принесённом ею самой файле**. Так проверка не зависит
от того, кто именно лежит в скрытом регистре сида: заведут третьего курьера —
она поймает и его. Список конкретных скрытых имён проверяется рядом, потому что
именно он и утёк.

**Ролью `app_user`.** Страницы открываются `client`, а он ходит путём продукта:
`set local role app_user` в каждом запросе (`web/dbcontext.py`). Владелец схемы
в этой базе суперпользователь, и под ним проверка была бы зелёной при снятых
политиках. Владельцем снимаются только **ожидания** — кого сверка не должна
называть; сама страница видит ровно то, что база отдала роли.

**С обеих сторон.** Рядом стоят проверки, что у директора раздел на месте
построчно: вырезать раздел для всех — не починка, а потеря приёмки T031. Без
этой пары «починкой» прошло бы удаление кода.

**Молчание — тоже неправда.** Пустая строка сводки «0» утверждает о расчёте
(«в нём нет никого сверх вашей таблицы») ровно так же, как список. Поэтому у
роли без всего расчёта на её месте стоит слово «не проверялось», а не число, и
это проверяется числом из сводки, а не наличием подписи.
"""
from __future__ import annotations

from contextlib import contextmanager

import pytest

from conftest import (JUNE, PLATA_SAMPLE, body, content, login_as, narrowed_ledgers,
                      period_url, wipe_payruns)
from test_reports_reconcile_db import section_rows, summary

SECTION = "Есть в расчёте, нет в таблице"
NOT_CHECKED = "не проверялось"

# Регистры каждой роли сида. Списки разные и снимаются не друг с друга: «чего не
# видно» — свойство роли, и общая константа однажды объявила бы утечкой её
# собственные данные.
#
# После D036 у бухгалтера в сиде набор полон (как у директора), и она сама
# больше не демонстрирует случай «расчёт отдан не весь» — держит его теперь
# только управляющий (D031). Запись для бухгалтера здесь не история, а условие,
# которое ниже создаётся явно `narrowed_ledgers`: без него в продукте роли с
# такими правами и таким набором просто нет.
LEDGERS = {
    "accountant": ["official"],
    "manager": ["official", "supplementary"],
}


@contextmanager
def role_with_ledgers(web_env, user: str):
    """Дать роли ровно тот набор регистров, на котором держится этот тест.

    Управляющему сужать нечего — её набор в сиде и так неполон (D031). А у
    бухгалтера после D036 набор полон, и без явного сужения `narrowed_ledgers`
    все проверки ниже проверяли бы не утечку, а обычное поведение роли с
    равным директору доступом.
    """
    if user == "accountant":
        with narrowed_ledgers(web_env, "accountant", LEDGERS[user]):
            yield
    else:
        yield


@pytest.fixture
def calculated_june(client, web_env):
    """Посчитанный июнь на данных сида — тот же материал, что у сверки T031."""
    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200
    return None


def reconcile_page(client, user: str) -> str:
    """Загрузить обезличенную таблицу на страницу сверки — как это делает человек."""
    login_as(client, user)
    with PLATA_SAMPLE.open("rb") as handle:
        response = client.post(
            period_url(client) + "reconcile/", {"table": handle}, follow=True
        )
    assert response.status_code == 200, f"{user}: сверка ответила {response.status_code}"
    # Только содержимое: проверка ищет ИМЕНА подстрокой, а в шапке с D064 стоит
    # «Аналитика по людям» — внутри неё нашлось имя «Ана», и утечкой это
    # выглядело убедительно.
    return content(response)


def file_keys() -> set[str]:
    """Табельные номера, которые человек принёс сам, — из того же файла.

    Разбирается тем же импортёром, каким его читает сверка: свой разбор в тесте
    означал бы, что «в файле есть» здесь и в продукте — разные утверждения.
    """
    from payroll.importers import read_plata_file

    with PLATA_SAMPLE.open("rb") as handle:
        parsed = read_plata_file(handle)
    keys = {row.employee.ext_id for row in parsed.rows}
    assert keys, "обезличенный файл не разобран — проверять нечего"
    return keys


def names_outside_the_file() -> list[str]:
    """Кого сверка может назвать только со своей стороны, а не из файла.

    Люди, у которых в июне есть табель или строка ведомости, но которых нет в
    принесённом файле. Спрашивается владельцем схемы: это **ожидание** теста, а
    не то, что видит роль, и собирать его через политики значило бы проверять
    страницу ею же самой.
    """
    from core.models import Employee, Payslip, Timesheet

    in_run = set(
        Timesheet.objects.filter(period=JUNE).values_list("employee_id", flat=True)
    ) | set(
        Payslip.objects.filter(payrun__period=JUNE).values_list("employee_id", flat=True)
    )
    people = Employee.objects.filter(id__in=list(in_run)).exclude(
        external_id__in=list(file_keys())
    )
    names = [f"{p.last_name} {p.first_name}".strip() for p in people]
    assert names, "в расчёте сида нет никого сверх таблицы — проверять нечего"
    return names


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


# =============================================================================
# 1. Страница под ролью, которой расчёт отдан не весь
# =============================================================================


@pytest.mark.parametrize("user", ["accountant", "manager"])
def test_the_reconciliation_names_nobody_from_a_ledger_the_role_cannot_see(
    client, web_env, calculated_june, user
):
    """Ровно та утечка, ради которой задача: фамилии людей скрытого регистра."""
    with role_with_ledgers(web_env, user):
        html = reconcile_page(client, user)
    hidden = names_hidden_from(LEDGERS[user])

    for name in hidden:
        assert name not in html, (
            f"сверка {user} называет «{name}» — человека, расчёта которого "
            f"база этой роли не отдала"
        )
        # Отдельной строкой: фамилии и имени по отдельности тоже быть не должно —
        # разметка могла бы разнести их по соседним ячейкам.
        for part in name.split():
            assert part not in html, f"сверка {user} называет «{part}»"


@pytest.mark.parametrize("user", ["accountant", "manager"])
def test_every_name_on_the_page_came_from_the_file_the_person_brought(
    client, web_env, calculated_june, user
):
    """Инвариант шире списка курьеров: имя со **своей** стороны не показывается.

    Пока сверка называет хоть кого-то, кого нет в принесённом файле, она
    рассказывает про расчёт, которого роли не показывали. Кто именно окажется
    в этой разности — вопрос данных, а не устройства.
    """
    with role_with_ledgers(web_env, user):
        html = reconcile_page(client, user)

    for name in names_outside_the_file():
        assert name not in html, (
            f"сверка {user} называет «{name}» — этого имени нет в файле, "
            f"который человек загрузил сам"
        )


@pytest.mark.parametrize("user", ["accountant", "manager"])
def test_the_section_about_the_calculation_is_not_shown_to_that_role(
    client, web_env, calculated_june, user
):
    """Раздела нет — ни со строками, ни пустого.

    Пустая таблица под этим заголовком читается как «в расчёте нет никого сверх
    вашей таблицы», а это утверждение о расчёте, которого роль не видела.
    """
    with role_with_ledgers(web_env, user):
        html = reconcile_page(client, user)

    assert f"<h2>{SECTION}</h2>" not in html, f"{user}: раздел показан"


@pytest.mark.parametrize("user", ["accountant", "manager"])
def test_the_summary_says_plainly_that_this_was_not_checked(
    client, web_env, calculated_june, user
):
    """Молча убрать строку сводки нельзя: пропажа читается как «всё в порядке».

    И ноль здесь тоже неправда. Проверяется значением из сводки, а не наличием
    слова на странице: слово могло бы стоять где угодно и ничего не значить.
    """
    with role_with_ledgers(web_env, user):
        html = reconcile_page(client, user)
    counts = summary(html)

    assert counts[SECTION] == NOT_CHECKED, (
        f"{user}: сводка отвечает про расчёт «{counts[SECTION]}» вместо "
        f"«{NOT_CHECKED}»"
    )


# =============================================================================
# 2. Обратная сторона: роль, которой отдан весь расчёт
# =============================================================================


def test_the_director_still_gets_the_section_row_for_row(client, calculated_june):
    """Приёмка T031 не тронута: вырезать раздел для всех — не починка.

    Без этой проверки все, что выше, зеленело бы от удаления кода.
    """
    html = reconcile_page(client, "director")
    counts = summary(html)
    rows = section_rows(html, SECTION)

    assert counts[SECTION] == 3, f"в сводке директора {counts[SECTION]} вместо трёх"
    assert len(rows) == 3, f"в разделе директора {len(rows)} строк вместо трёх"
    for name, why in rows:
        assert name, "строка раздела без имени"
        assert "нет" in why, f"факт о загруженном файле размыт: {why!r}"


def test_the_director_sees_exactly_those_the_others_must_not(client, calculated_june):
    """Разность не потеряна, а перестала показываться не тем.

    Проверка держит обе половины сразу: те же имена, что запрещены бухгалтеру и
    управляющему, обязаны стоять у директора. Иначе «починкой» прошла бы правка,
    которая просто перестала находить этих людей вовсе.
    """
    html = reconcile_page(client, "director")
    shown = {name for name, _why in section_rows(html, SECTION)}

    assert shown == set(names_outside_the_file()), (
        f"у директора в разделе {shown}, а сверх таблицы в расчёте "
        f"{set(names_outside_the_file())}"
    )


# =============================================================================
# 3. Ядро сверки: оба условия по отдельности
# =============================================================================


def test_a_role_without_the_whole_run_gets_no_names_even_with_totals():
    """Первое условие — свойство роли. Итоги в строке его не отменяют."""
    from reports.reconcile import compare
    from test_reports_reconcile import run_line

    result = compare([], {"k": run_line("КУРИР АНА")}, whole_run_visible=False)

    assert result.only_in_run == []
    assert result.whole_run_visible is False


def test_a_row_without_totals_is_not_called_part_of_the_calculation():
    """Второе условие — про саму строку: «есть в расчёте» проверяется итогами.

    Роль видит весь расчёт, но по этой строке итогов нет вовсе — значит расчёта
    по ней и не было, только табель. Назвать её «есть в расчёте» было бы
    неправдой и у директора.
    """
    from reports.reconcile import compare
    from test_reports_reconcile import run_line_without_totals

    result = compare(
        [], {"k": run_line_without_totals("КУРИР АНА")}, whole_run_visible=True
    )

    assert result.only_in_run == []


def test_both_conditions_together_still_name_the_row():
    """Порча наоборот: при обоих выполненных условиях строка обязана называться.

    Без неё две предыдущие зеленели бы и от того, что список выключен совсем.
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
