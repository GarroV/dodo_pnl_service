"""Сверка и выгрузки на живых данных, через страницы (T031, T032).

Что здесь проверяется и почему именно так.

**Границу видимости держит база, а не приложение.** Поэтому все проверки идут
через страницу под конкретной ролью, а контрольные суммы снимаются с базы
**ролью `app_user`**: владелец таблиц и суперпользователь обходят RLS, и под
ними сошлось бы даже при снятых политиках.

**Выгрузка читается обратно.** Файл открывается openpyxl, числа складываются и
сравниваются с тем, что показано на экране. «Ответ вернулся» не означает
ничего: файл может быть пустым или собранным из другой выборки.

**Файл роли с ограниченным доступом не знает о чужих регистрах** — ни строкой,
ни названием, ни вкладом в итог (D023). Это главная проверка T032: выгрузка
уходит из продукта и живёт своей жизнью.
"""
from __future__ import annotations

import io
import re
from decimal import Decimal

import openpyxl
import pytest

from conftest import PLATA_SAMPLE, body, login_as, period_url, wipe_payruns

JUNE = "2026-06-01"

# Ориентиры приёмки, снятые на данных сида (те же, что у ведомости, T028).
CONTROL = {
    "director": Decimal("1951806.13"),
    "accountant": Decimal("464752.41"),
    "manager": Decimal("891373.32"),
}

HIDDEN_FROM_ACCOUNTANT = ("Дополнительный", "Внутренний", "supplementary", "internal")


@pytest.fixture
def calculated_june(client, web_env):
    """Посчитанный июнь на данных сида — общий материал для проверок ниже."""
    wipe_payruns(web_env)
    login_as(client, "director")
    response = client.post(period_url(client) + "calculate/", follow=True)
    assert response.status_code == 200
    return None


def reconcile_page(client, user: str) -> str:
    """Загрузить обезличенную таблицу на страницу сверки — как это делает человек."""
    login_as(client, user)
    with PLATA_SAMPLE.open("rb") as handle:
        response = client.post(
            period_url(client) + "reconcile/", {"table": handle}, follow=True
        )
    assert response.status_code == 200, f"{user}: сверка ответила {response.status_code}"
    return body(response)


def download(client, user: str, kind: str, query: str = ""):
    login_as(client, user)
    response = client.get(period_url(client) + f"export/{kind}/{query}")
    assert response.status_code == 200, f"{user}/{kind}: ответ {response.status_code}"
    assert "spreadsheetml" in response["Content-Type"], "это не книга Excel"
    assert "attachment" in response["Content-Disposition"]
    return openpyxl.load_workbook(io.BytesIO(response.content))


def text_of(book) -> str:
    return "\n".join(
        str(value)
        for ws in book.worksheets
        for row in ws.iter_rows(values_only=True)
        for value in row
        if value is not None
    )


def column(ws, header: str) -> list:
    """Значения колонки по её заголовку — как её находит глазами человек."""
    index = None
    out = []
    for row in ws.iter_rows(values_only=True):
        cells = [str(v) if v is not None else None for v in row]
        if index is not None and row[index] is not None:
            out.append(row[index])
        if header in cells:
            index = cells.index(header)
    assert index is not None, f"в листе «{ws.title}» нет колонки «{header}»"
    return out


SUMMARY_ROW = re.compile(r"<td>([^<]+)</td>\s*<td class=\"num[^\"]*\">(\d+)</td>")


def summary(html: str) -> dict[str, int]:
    """Сводка сверки числами, а не наличием подписи.

    Подписи в сводке стоят всегда, включая нулевые строки: это её смысл —
    человек должен видеть, что потерянных строк ноль, а не догадываться об
    этом по отсутствию раздела. Значит проверка «подпись есть на странице»
    не проверяет ничего и зеленеет при любом содержимом. Здесь читаются числа.
    """
    start = html.index("<th>Итог сверки</th>")
    table = html[start:html.index("</table>", start)]
    found = {label.strip(): int(count) for label, count in SUMMARY_ROW.findall(table)}
    assert len(found) == 6, f"сводка сверки прочитана не целиком: {found}"
    return found


def withheld_totals(dsn: str) -> set[str]:
    """Итоги расчёта так, как их напечатала бы страница, — глазами директора.

    Ровно эти строки не имеет права встретиться на сверке роли, которой база
    итогов не отдала: совпадение означало бы, что скрытая часть уехала на
    экран. Снимаются ролью `app_user` под директором, а не владельцем схемы:
    владельца политики не касаются, и набор был бы тот же при снятых.
    """
    import psycopg

    from conftest import as_app_user
    from core.db_types import register_enum_types
    from core.management.commands.seed_dev import det_id
    from web.format import money

    with psycopg.connect(dsn) as conn:
        register_enum_types(conn)
        with as_app_user(conn, str(det_id("user", "director"))) as scoped:
            rows = scoped.execute(
                """select t.net, t.gross, t.contributions, t.total_cost
                     from payslip_totals t
                     join payslips p on p.id = t.payslip_id
                     join payruns r on r.id = p.payrun_id
                    where r.period = %s""",
                (JUNE,),
            ).fetchall()

    # Ноль отбрасывается намеренно: он встречается в разметке сам по себе и
    # объявил бы утечкой любую страницу.
    return {money(value) for row in rows for value in row if value}


def visible_total(dsn: str, user: str) -> Decimal:
    """Сколько база отдаёт этой роли — её же ролью, а не владельцем схемы."""
    import psycopg

    from conftest import as_app_user
    from core.db_types import register_enum_types
    from core.management.commands.seed_dev import det_id

    with psycopg.connect(dsn) as conn:
        register_enum_types(conn)
        with as_app_user(conn, str(det_id("user", user))) as scoped:
            return scoped.execute(
                """select coalesce(sum(c.amount), 0)
                     from pay_components c
                     join payslips p on p.id = c.payslip_id
                     join payruns r on r.id = p.payrun_id
                    where r.period = %s""",
                (JUNE,),
            ).fetchone()[0]


# --- сверка ------------------------------------------------------------------


def test_the_reconciliation_of_the_reference_table_matches(client, calculated_june):
    """Эталонная таблица июня сходится с расчётом продукта построчно.

    Настоящая таблица бухгалтерии в репозиторий не попадает никогда (D028), и
    её загрузка — приёмочный шаг MVP (T039). Здесь тот же формат и те же четыре
    схемы на обезличенном файле: сверка обязана сойтись и назвать всё, что не
    сошлось.
    """
    html = reconcile_page(client, "director")
    counts = summary(html)

    assert "Сверка с таблицей бухгалтера" in html
    assert "Не разобрано в файле" not in html, "обезличенный файл разбирается целиком"
    assert counts["Сошлось до копейки"] == 32, (
        f"не все 32 строки эталонной таблицы сошлись с расчётом: {counts}"
    )
    assert counts["Разошлось"] == 0
    assert counts["Разошлось на копейки (округление)"] == 0
    assert counts["Есть в таблице, нет в расчёте"] == 0, (
        "директору видны все строки — терять их сверке негде"
    )
    assert counts["Сверены только входы — деньги не сравнивались"] == 0, (
        "директору отданы все итоги: сверять по одним входам ему нечего"
    )
    # Курьеры и строка исправления есть в сиде и не могут быть в таблице
    # бухгалтера: она за другой месяц другой сети. Это не потеря, а разница
    # наборов, и она названа числом, чтобы молчаливый рост был виден.
    assert counts["Есть в расчёте, нет в таблице"] == 3, (
        f"в расчёте сида ровно три человека сверх таблицы: {counts}"
    )


def test_the_reconciliation_never_reports_a_match_it_did_not_make(
    client, calculated_june
):
    """Главная проверка T031 под ролью со скрытыми итогами.

    Нето, бруто и взносы посчитаны по строке ведомости целиком, поэтому база
    отдаёт их только роли, которой видны все регистры (T071). У бухгалтера их
    нет — и сверка обязана сказать это прямо, а не отрапортовать совпадение.

    Ловушка, ради которой тест написан: пустое `all()` даёт истину. Строка, в
    которой нечего было сравнивать, без различения «сравнивали» и «сошлось»
    попадала бы в сошедшиеся, и бухгалтер прочитал бы «сошлось до копейки» по
    расчёту, которого он не видел.
    """
    html = reconcile_page(client, "accountant")
    counts = summary(html)

    assert counts["Сверены только входы — деньги не сравнивались"] == 32, (
        f"итоги бухгалтеру не отданы — сверять по деньгам нечего: {counts}"
    )
    assert counts["Сошлось до копейки"] == 0, (
        f"сверка объявила совпадением строки, деньги которых не сравнивала: {counts}"
    )
    assert counts["Разошлось"] == 0, (
        f"несравнённое — не расхождение: сравнивать было нечего: {counts}"
    )
    assert "Всё сошлось до копейки" not in html, (
        "сверка назвала себя чистой, не сверив ни одной суммы"
    )
    assert "Деньги не сравнивались ни по одной строке" in html, (
        "подвал обязан сказать, что суммы не сверялись, а не показать ноль"
    )


def test_the_reconciliation_never_shows_our_numbers_for_a_row_it_cannot_see(
    client, web_env, calculated_june
):
    """Главная проверка D023 на сверке: скрытый регистр не выдаётся вычитанием.

    Итог файла минус сумма видимых компонентов — это ровно скрытая часть.
    Поэтому по строке без отданных итогов не показывается **ни одного нашего
    числа**: ни суммы, ни разницы. Проверяется самими числами расчёта, а не
    наличием раздела на странице.
    """
    html = reconcile_page(client, "accountant")

    for word in HIDDEN_FROM_ACCOUNTANT:
        assert word not in html, f"на странице сверки бухгалтера есть «{word}»"

    hidden = withheld_totals(web_env)
    assert len(hidden) >= 30, f"нечего проверять: итогов в расчёте {len(hidden)}"
    for shown in hidden:
        assert shown not in html, (
            f"на сверке бухгалтера стоит наш итог {shown} по строке, "
            f"итоги которой база ему не отдала"
        )


def test_the_reconciliation_refuses_a_file_that_is_not_a_workbook(client, calculated_june):
    login_as(client, "director")
    response = client.post(
        period_url(client) + "reconcile/",
        {"table": io.BytesIO(b"nonsense")},
        follow=True,
    )
    assert response.status_code == 422
    assert "не удалось прочитать" in body(response)


def test_the_reconciliation_says_when_no_file_was_chosen(client, calculated_june):
    login_as(client, "director")
    response = client.post(period_url(client) + "reconcile/", {}, follow=True)
    assert response.status_code == 400
    assert "Файл не выбран" in body(response)


# --- выгрузки ----------------------------------------------------------------


@pytest.mark.parametrize("kind", ["payout", "pnl", "partner"])
def test_every_export_opens_as_a_workbook_with_data(client, calculated_june, kind):
    book = download(client, "director", kind)

    assert book.worksheets, f"{kind}: в книге нет ни одного листа"
    assert "Июнь 2026" in text_of(book), f"{kind}: в файле не сказано, какой это месяц"


def test_the_payout_export_carries_the_same_total_the_role_sees(
    client, web_env, calculated_june
):
    """Файл сходится с базой, спрошенной ролью `app_user`, — для каждой роли."""
    for user, expected in CONTROL.items():
        book = download(client, user, "payout")
        *lines, footer = [Decimal(str(v)) for v in column(book.active, "Итого")]

        assert sum(lines, Decimal(0)) == footer, f"{user}: подвал файла не равен его строкам"
        assert footer == expected, f"{user}: в файле {footer}, ожидалось {expected}"
        assert footer == visible_total(web_env, user), (
            f"{user}: файл показывает не то, что отдаёт база его же ролью"
        )


@pytest.mark.parametrize("kind", ["payout", "pnl", "partner"])
def test_no_export_of_the_accountant_knows_about_other_ledgers(
    client, calculated_june, kind
):
    """Главная проверка T032: чужого регистра в файле нет ни в каком виде."""
    text = text_of(download(client, "accountant", kind))

    for word in HIDDEN_FROM_ACCOUNTANT:
        assert word not in text, f"{kind}: в файле бухгалтера есть «{word}»"


def test_the_export_of_a_cut_holds_exactly_that_cut(client, calculated_june):
    """Выгрузка берёт тот же разрез, что показан на экране, а не всю ведомость."""
    parts = {}
    for ledger in ("official", "supplementary", "internal"):
        book = download(client, "director", "payout", f"?ledger={ledger}")
        *lines, footer = [Decimal(str(v)) for v in column(book.active, "Итого")]
        assert sum(lines, Decimal(0)) == footer
        parts[ledger] = footer

    assert sum(parts.values(), Decimal(0)) == CONTROL["director"], (
        "сумма разрезов не сходится с полной ведомостью"
    )


def test_a_cut_the_role_cannot_see_gives_the_same_file_as_no_cut(client, calculated_june):
    """Подобранный `?ledger=internal` у бухгалтера — её обычная выгрузка."""
    guessed = download(client, "accountant", "payout", "?ledger=internal")
    plain = download(client, "accountant", "payout")

    assert text_of(guessed) == text_of(plain)


def test_the_pnl_export_splits_accruals_from_taxes(client, calculated_june):
    """Начисления сходятся с ведомостью, налоги стоят отдельными строками."""
    book = download(client, "director", "pnl")
    ws = book.active

    kinds = column(ws, "Тип строки")
    amounts = column(ws, "Сумма")
    accruals = [
        Decimal(str(amount))
        for kind, amount in zip(kinds, amounts, strict=True)
        if kind == "Начисление"
    ]

    assert sum(accruals, Decimal(0)) == CONTROL["director"]
    assert "Налог" in kinds and "Взносы" in kinds


def test_an_unknown_export_is_not_found(client, calculated_june):
    login_as(client, "director")
    assert client.get(period_url(client) + "export/secret/").status_code == 404
