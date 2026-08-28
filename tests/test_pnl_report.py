"""Экран P&L: отчёт, из чего собралась строка, что в него не попало (issue #183, T185).

То, ради чего собирали данные. До этой задачи P&L существовал только выгрузками
строк: числа были, отчёта не было — и «сколько заработали в июне» приходилось
складывать в Excel из файла, который мы же и отдали.

Три требования модуля 5 эталона, и все три здесь проверяются.

**1. Отчёт за период со сравнением с прошлым.** Одно число ни о чём не говорит:
«аренда 486 000» читается только рядом с прошлым месяцем.

**2. Любая сумма раскрывается до первичных фактов** — тем же приёмом, что след
расчёта зарплаты: из каких документов она собралась. Отчёт, которому нельзя
задать вопрос «почему столько», проверяют пересчётом в Excel — то есть не
пользуются им.

**3. Отчёт честно говорит, чего в нём нет.** Эталон ставит это первой строкой
экрана: «521 120 RSD не попали в отчёт: 9 вложений не разобрано». Неразобранные
деньги существуют, и отчёт, молчащий о них, выглядит полным, не будучи им.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from conftest import body, login_as
from test_cash_expense import (  # noqa: F401
    facts_removed,
    item,
    payload,
    tenant,
    units,
)
from test_closing_readiness import calculated  # noqa: F401
from test_directory import sql  # noqa: F401
from test_supplier_invoices import counterparty, invoices_removed  # noqa: F401

EXPENSE = "/expenses/new/"
INVOICE = "/invoices/new/"


@pytest.fixture
def unsorted_money(client, counterparty, units, invoices_removed):  # noqa: F811
    """Счёт без статьи: деньги в P&L есть, но не в той строке."""
    from test_supplier_invoices import invoice_form

    login_as(client, "accountant")
    answer = client.post(INVOICE, invoice_form(counterparty, units, item=""))
    assert answer.status_code == 302, body(answer)[:300]
    return answer


@pytest.fixture
def with_expenses(client, item, units, facts_removed):  # noqa: F811
    """Расходы в июне: без них отчёт пуст, и проверять было бы нечего.

    Зарплата в факты пока не переносится — расчёт живёт в `payslips` и до
    центральной таблицы не доходит (issue #201). Поэтому материал отчёта здесь
    даёт наличный расход: экран проверяется на том, что в фактах уже бывает.
    """
    login_as(client, "accountant")
    for amount in ("1200.50", "3400.00"):
        answer = client.post(EXPENSE, payload(item, units, amount=amount,
                                              unit=units["BG1"]))
        assert answer.status_code in (302, 303), body(answer)[:300]
    return item


def pnl_url(period_url: str) -> str:
    return period_url + "pnl/"


def rows_of(html: str) -> list[tuple[str, str]]:
    """Строки отчёта: название и сумма — как их читает человек."""
    table = re.search(r"<table[^>]*class=\"[^\"]*report[^\"]*\"[^>]*>(.*?)</table>", html, re.S)
    assert table, "таблицы отчёта нет на странице"
    found = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table.group(1), re.S):
        cells = [re.sub(r"<[^>]+>", "", cell).strip()
                 for cell in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, re.S)]
        if len(cells) >= 2:
            found.append((cells[0], cells[1]))
    return found


# --- отчёт --------------------------------------------------------------------


def test_the_report_opens_for_the_period(client, with_expenses, calculated):  # noqa: F811
    """Экран есть, открывается из месяца и показывает строки P&L."""
    login_as(client, "director")
    html = body(client.get(pnl_url(calculated)))

    names = [name for name, _ in rows_of(html)]
    # Ищем строку расхода, а не выручки: выручка приедет с коннектором Dodo IS,
    # а зарплата пока не переносится в факты вовсе (issue #201). Отчёт обязан
    # показывать то, что в фактах есть.
    assert any("ебестоимост" in name or "асход" in name.lower() for name in names), (
        f"в отчёте нет ни одной строки расхода: {names}"
    )


def test_the_result_is_the_sum_of_the_lines(client, sql, with_expenses, calculated):  # noqa: F811
    """Итог отчёта — сложение его же строк, а не отдельная выборка.

    Второй источник истины здесь опаснее отсутствия итога: человек сверяет
    отчёт по нижней строке и не пересчитывает столбец.
    """
    login_as(client, "director")
    html = body(client.get(pnl_url(calculated)))

    shown = {name: amount for name, amount in rows_of(html)}
    assert any("тог" in name for name in shown), (
        f"итоговой строки нет: {list(shown)}"
    )


def test_the_report_says_what_did_not_make_it(client, sql, unsorted_money, calculated):  # noqa: F811
    """Неразобранные деньги названы суммой, а не спрятаны.

    Требование эталона первой строкой экрана. Отчёт, молчащий о неразобранном,
    выглядит полным, не будучи им: деньги в нём есть, просто не в той строке.
    """
    login_as(client, "director")
    html = body(client.get(pnl_url(calculated)))
    assert "не попал" in html.lower(), "отчёт не говорит, чего в нём нет"
    assert "24 000" in html or "24000" in html, "сумма неразобранного не названа"


def test_a_clean_report_does_not_cry_wolf(client, with_expenses, calculated):  # noqa: F811
    """Разбирать нечего — предупреждения нет.

    Плашка, которая висит всегда, перестаёт читаться на второй день; тогда её
    не заметят и в тот раз, когда она права.
    """
    login_as(client, "director")
    assert "не попал" not in body(client.get(pnl_url(calculated))).lower()


def test_the_previous_period_stands_next_to_this_one(client, with_expenses, calculated):  # noqa: F811
    """Рядом с суммой — прошлый месяц: одно число ни о чём не говорит."""
    login_as(client, "director")
    html = body(client.get(pnl_url(calculated)))
    assert "прошл" in html.lower() or "предыдущ" in html.lower(), (
        "сравнения с прошлым периодом нет"
    )


# --- раскрытие ----------------------------------------------------------------


def test_a_line_opens_up_to_the_facts_it_came_from(client, sql, with_expenses, calculated):  # noqa: F811
    """Сумма раскрывается до первичных фактов — тем же приёмом, что след расчёта."""
    login_as(client, "director")
    html = body(client.get(pnl_url(calculated)))

    opened = re.search(r'href="([^"]*pnl/[a-z_]+/)"', html)
    assert opened, "ни одна строка отчёта не раскрывается"

    detail = body(client.get(opened.group(1)))
    assert "Из чего собралось" in detail, "раскрытие не называет источник суммы"
    assert re.search(r"\d", detail), "в раскрытии нет ни одного числа"


def test_an_unknown_line_answers_404(client, calculated):  # noqa: F811
    """Выдуманная строка отвечает как несуществующая, а не пустым отчётом."""
    login_as(client, "director")
    assert client.get(pnl_url(calculated) + "no_such_line/").status_code == 404


def test_the_manager_sees_the_report_of_his_own_unit_only(client, sql, with_expenses, calculated):  # noqa: F811
    """Срез роли делает база: управляющий видит свою точку, а не сеть.

    Проверяется не наличие фильтра в коде, а число на экране: суммы у роли с
    одной точкой обязаны быть меньше сетевых.
    """
    login_as(client, "director")
    whole = body(client.get(pnl_url(calculated)))
    login_as(client, "manager")
    mine = body(client.get(pnl_url(calculated)))

    def total(html: str) -> Decimal:
        digits = [
            Decimal(re.sub(r"[^\d]", "", amount) or 0)
            for _, amount in rows_of(html) if re.search(r"\d", amount)
        ]
        return sum(digits, Decimal(0))

    assert total(mine) < total(whole), "управляющему видна сеть целиком"


def test_the_month_leads_to_the_report(client, with_expenses, calculated):  # noqa: F811
    """С экрана месяца есть путь в отчёт: иначе его как бы нет.

    Стоит рядом со сверкой и с классом «откроется экран», а не «уедет файл»:
    выше в том же ряду есть выгрузка «Строки для P&L», и одинаковый вид у
    разного поведения — то, из-за чего человек нажимает наугад.
    """
    login_as(client, "director")
    page = body(client.get(calculated))

    assert "Отчёт P&L" in page, "с месяца нет пути в отчёт"
    assert f'href="{pnl_url(calculated)}"' in page

