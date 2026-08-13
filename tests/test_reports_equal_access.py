"""Равный доступ бухгалтера и оперативного директора в отчётах (T088, D036).

Что чинилось. Выгрузка «Строки для P&L» приходила бухгалтеру **молча без
налогов и взносов**: у директора 34 строки, из них 6 налоговых, у бухгалтера 8
строк и ни одной «Налог» или «Взносы». Причина была законная — налог и взносы
живут в `payslip_totals`, а те видны только роли, которой видна вся строка
ведомости (T050, T071), — но человек собрал бы P&L без налогов на зарплату и не
узнал бы об этом ни на экране, ни в файле.

Ответ владельца на Q012 (D036) снял причину: у бухгалтера все три регистра, и
итоги ей видны. Этот файл держит результат проверенным, а не выведенным из
рассуждения: **выгрузка бухгалтера обязана совпадать с выгрузкой директора
байт в байт по содержимому**, и налоговые строки в ней обязаны быть.

Рядом стоит вторая половина: роль с неполным набором регистров (управляющий
точки, D031) налоговых строк по-прежнему не получает. Это не забытая утечка, а
живой механизм видимости — он остаётся на месте и сужается позже, «там где
надо». Проверять его надо ровно потому, что первая половина его больше не
проверяет.
"""
from __future__ import annotations

import io
from decimal import Decimal

import openpyxl
import pytest

from conftest import body, login_as, period_url, wipe_payruns


@pytest.fixture
def calculated_june(client, web_env):
    """Посчитанный июнь на данных сида — тот же материал, что у отчётов."""
    wipe_payruns(web_env)
    login_as(client, "director")
    response = client.post(period_url(client) + "calculate/", follow=True)
    assert response.status_code == 200, body(response)[:1000]
    return None


def download(client, user: str, kind: str):
    login_as(client, user)
    response = client.get(period_url(client) + f"export/{kind}/")
    assert response.status_code == 200, f"{user}/{kind}: ответ {response.status_code}"
    return openpyxl.load_workbook(io.BytesIO(response.content))


def rows_of(book) -> list[tuple]:
    return [
        row
        for ws in book.worksheets
        for row in ws.iter_rows(values_only=True)
        if any(value is not None for value in row)
    ]


def tax_amounts(book) -> list[Decimal]:
    """Суммы строк «Налог» и «Взносы» — то, чего у бухгалтера не было вовсе."""
    out = []
    for row in rows_of(book):
        cells = [str(value) if value is not None else "" for value in row]
        if "Налог" in cells or "Взносы" in cells:
            out.append(Decimal(str(row[-1])))
    return out


def test_the_pnl_export_of_the_accountant_carries_the_taxes(client, calculated_june):
    """Главная проверка T088: налоговая часть в файле бухгалтера есть.

    Проверяется и число строк: «файл не пустой» было бы зелёным и на файле из
    восьми строк — том самом, с которого задача началась.
    """
    accountant = download(client, "accountant", "pnl")
    taxes = tax_amounts(accountant)

    assert taxes, "в выгрузке бухгалтера нет ни строки «Налог» или «Взносы»"
    assert sum(taxes) > 0, "налоговые строки есть, но все нули"
    assert len(rows_of(accountant)) > 20, (
        f"строк в файле всего {len(rows_of(accountant))} — похоже, налоги снова не вошли"
    )


def test_the_pnl_export_is_the_same_for_the_accountant_and_the_director(
    client, calculated_june
):
    """D036 в точной форме: равный доступ — значит один и тот же файл."""
    director = download(client, "director", "pnl")
    accountant = download(client, "accountant", "pnl")

    assert rows_of(accountant) == rows_of(director)
    assert tax_amounts(accountant) == tax_amounts(director)


def test_a_partial_role_still_gets_no_taxes(client, calculated_june):
    """Вторая половина: механизм видимости жив, и это проверено, а не обещано.

    У управляющего точки набор регистров неполный (D031), итоги строк ведомости
    ему не видны — значит налоговых строк в его выгрузке нет. Если однажды они
    там появятся, это будет утечка: налог посчитан по строке целиком, включая
    скрытый от него регистр.
    """
    manager = download(client, "manager", "pnl")
    director = download(client, "director", "pnl")

    assert tax_amounts(director), "у директора налогов нет — проверка ничего не значит"
    assert tax_amounts(manager) == []
