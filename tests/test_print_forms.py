"""Печатные формы: то, что человек уносит с собой и подписывает (T187, #184).

Экран объясняет расчёт, бумага его закрепляет: платёжную ведомость подписывают
сотрудники, расчётный листок отдают человеку на руки. Поэтому здесь проверяется
не «страница открылась», а три свойства, без которых бумага бесполезна.

**Числа на листе сходятся между собой.** Начислено − удержано = к выплате, и это
верно построчно и в итоге. Ведомость, у которой итог не равен сумме строк, — это
спор с сотрудником в момент подписи, и разбирать его будут по бумаге, а не по
экрану.

**Лист не обрезан.** Разбиение на листы считает продукт, а не браузер: только так
на бумаге может стоять честное «Лист 2 из 3». Значит вместимость листа —
проверяемая арифметика, и она проверяется здесь, а геометрия в миллиметрах — на
живом браузере смоуком (`tools/smoke_print_forms.mjs`): числа сходятся оба раза
или не сходятся оба раза.

**Форма, которую нельзя собрать честно, не собирается вовсе.** «Начислено»,
«удержано» и «к выплате» посчитаны по строке ведомости целиком и живут в
`payslip_totals` — их видно роли, только если ей видна вся строка (T050). Роли с
неполным набором регистров ведомость выдала бы половину людей и итог, который ни
с чем не сходится; разрезу по регистру эти числа не принадлежат вовсе (тот же
довод, что у налогов в T141). В обоих случаях документ не печатается, а называет
причину — свою для каждого случая. Одна фраза на все была бы неправдой в двух
случаях из трёх: эту ошибку в блоке уже чинили дважды (T120, T134, T141).
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from conftest import body, login_as
from test_directory import payruns_restored, sql  # noqa: F401

JUNE = "2026-06-01"


# =============================================================================
# Разбиение на листы — чистая арифметика, без базы и без браузера
# =============================================================================


def test_a_short_run_is_one_sheet():
    """Пятнадцать человек — один лист, на нём и шапка, и подвал."""
    from reports.printing import paginate

    sheets = paginate(list(range(15)))

    assert len(sheets) == 1
    assert sheets[0].first and sheets[0].last
    assert sheets[0].number == 1 and sheets[0].of == 1
    assert len(sheets[0].rows) == 15


def test_every_row_lands_on_exactly_one_sheet():
    """Ни одна строка не потерялась и ни одна не задвоилась — на любом объёме.

    Проверка не про красоту: потерянная строка на подписной ведомости — это
    человек, который не расписался и не получил денег, и заметят это в кассе.
    """
    from reports.printing import paginate

    for count in [0, 1, 33, 34, 42, 43, 45, 100, 250]:
        sheets = paginate(list(range(count)))
        laid = [row for sheet in sheets for row in sheet.rows]
        assert laid == list(range(count)), f"строки поехали при {count}"


def test_the_sheets_number_themselves_honestly():
    """«Лист 2 из 3» — не украшение: по нему замечают потерянный лист."""
    from reports.printing import paginate

    sheets = paginate(list(range(200)))

    assert [sheet.number for sheet in sheets] == list(range(1, len(sheets) + 1))
    assert {sheet.of for sheet in sheets} == {len(sheets)}
    assert [sheet.first for sheet in sheets].count(True) == 1
    assert [sheet.last for sheet in sheets].count(True) == 1
    assert sheets[0].first and sheets[-1].last


def test_no_sheet_is_taller_than_the_paper():
    """Вместимость листа — арифметика, а не «на глаз влезло».

    Считается тем же способом, каким лист собирает разметка: шапка (полная на
    первом листе, короткая на продолжении) + строка заголовков + строки + подвал
    на последнем. Больше высоты бумаги за вычетом полей — обрезка.
    """
    from reports import printing

    usable = printing.SHEET_MM - printing.MARGIN_TOP_MM - printing.MARGIN_BOTTOM_MM
    for count in [0, 1, 33, 34, 42, 43, 44, 45, 90, 91, 250]:
        for sheet in printing.paginate(list(range(count))):
            filled = (
                (printing.HEAD_FIRST_MM if sheet.first else printing.HEAD_NEXT_MM)
                + printing.TABLE_HEAD_MM
                + len(sheet.rows) * printing.ROW_MM
                + (printing.FOOT_MM if sheet.last else 0)
            )
            assert filled <= usable, (
                f"лист {sheet.number} из {sheet.of} при {count} строках занимает "
                f"{filled} мм при доступных {usable} мм — на бумаге это обрезка"
            )


def test_a_run_that_exactly_fills_a_sheet_moves_the_footer_to_its_own_sheet():
    """Подвал не влезает — уезжает на отдельный лист, а не прячется.

    Самый опасный случай: строк ровно столько, сколько влезает без подвала.
    Наивное разбиение поставило бы подвал поверх последней строки — итог,
    сумма прописью и подписи пропали бы молча.
    """
    from reports import printing

    full = printing.capacity(first=True, last=False)
    sheets = printing.paginate(list(range(full)))

    assert len(sheets) == 2, "подвалу не нашлось листа"
    assert len(sheets[0].rows) == full and not sheets[0].last
    assert sheets[1].rows == [] and sheets[1].last


# =============================================================================
# Отказы: форму, которую нельзя собрать честно, не собирают
# =============================================================================


def test_every_refusal_speaks_its_own_reason():
    """Три причины — три разных текста, и ни один не пуст.

    Одна фраза на все причины была бы неправдой в двух случаях из трёх — ровно
    та ошибка, которую в этом блоке уже чинили (T120, T141).
    """
    from reports import printing

    texts = {code: printing.refusal_text(code) for code in printing.REFUSALS}

    assert len(set(texts.values())) == len(printing.REFUSALS), texts
    assert all(text.strip() for text in texts.values()), texts


def test_the_refusal_of_a_cut_names_the_cut_and_not_the_rights():
    """Разрез и права — разные причины, и они не смешиваются."""
    from reports import printing

    by_cut = printing.payout_refusal(has_rows=True, whole_run=True, cut="official")
    withheld = printing.payout_refusal(has_rows=True, whole_run=False, cut="")
    nothing = printing.payout_refusal(has_rows=False, whole_run=True, cut="")

    assert by_cut == printing.BY_CUT
    assert withheld == printing.TOTALS_WITHHELD
    assert nothing == printing.NOT_CALCULATED
    assert printing.payout_refusal(has_rows=True, whole_run=True, cut="") == ""


def test_the_refusal_of_rights_names_no_ledger_and_no_amount():
    """Отказ по правам — факт о правах, а не о данных (D023, D014).

    Ни названия скрытого регистра, ни числа: и то и другое рассказывало бы о
    том, что от роли закрыто.
    """
    from reports import printing

    text = printing.refusal_text(printing.TOTALS_WITHHELD)

    assert "Внутренний" not in text and "Дополнительный" not in text
    assert not re.search(r"\d", text), text


# =============================================================================
# Платёжная ведомость на живых данных
# =============================================================================


@pytest.fixture
def june_calculated(client, web_env, sql, payruns_restored):  # noqa: F811
    """Июнь посчитан: без расчёта печатать нечего ни одной роли."""
    from conftest import wipe_payruns

    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(
        period_url(sql) + "calculate/", {"inline": "1"}, follow=True
    ).status_code == 200
    yield
    client.post("/logout/")


def period_url(sql) -> str:  # noqa: F811
    period_id = sql.execute(
        "select p.id from periods p join tenants t on t.id = p.tenant_id "
        "where t.code = 'rs-dev' and p.period = %s", (JUNE,)
    ).fetchone()[0]
    return f"/periods/{period_id}/"


def payout_page(client, sql, query: str = "") -> str:  # noqa: F811
    response = client.get(period_url(sql) + "print/payout/" + query)
    assert response.status_code == 200, response.status_code
    return body(response)


def amount(text: str) -> Decimal:
    """Сумма из ячейки: «1 234,50» → Decimal («1234.50»)."""
    return Decimal(text.replace(" ", "").replace(" ", "").replace(",", "."))


def payout_rows(html: str) -> list[dict]:
    """Строки ведомости так, как их читает человек, — из разметки страницы."""
    rows = []
    for chunk in re.findall(r"<tr class=\"line\">(.*?)</tr>", html, re.S):
        cells = dict(
            re.findall(r"<td class=\"[^\"]*\b(who|hours|accrued|held|paid)\b[^\"]*\">(.*?)</td>",
                       chunk, re.S)
        )
        if cells:
            rows.append({key: value.strip() for key, value in cells.items()})
    return rows


def test_the_payout_sheet_shows_every_person_of_the_run(client, sql, june_calculated):  # noqa: F811
    """Сколько человек в расчёте — столько строк на бумаге."""
    html = payout_page(client, sql)
    rows = payout_rows(html)

    counted = sql.execute(
        "select count(*) from payslips s join payruns r on r.id = s.payrun_id "
        "where r.period = %s", (JUNE,)
    ).fetchone()[0]
    assert len(rows) == counted, f"на бумаге {len(rows)} строк, в расчёте {counted}"
    assert all(row["who"] for row in rows), "строка без имени — некому расписываться"


def test_the_three_money_columns_reconcile_row_by_row(client, sql, june_calculated):  # noqa: F811
    """Начислено − удержано = к выплате. В каждой строке и в итоге.

    Это то самое равенство, по которому бумагу проверяют в кассе. Не сойдись
    оно — спор идёт с человеком, а не с экраном.
    """
    html = payout_page(client, sql)
    rows = payout_rows(html)
    assert rows, "ведомость пуста — проверять нечего"

    for row in rows:
        accrued, held, paid = (amount(row[key]) for key in ("accrued", "held", "paid"))
        assert accrued - held == paid, f"строка {row['who']}: {accrued} − {held} ≠ {paid}"

    totals = re.search(
        r"<tr class=\"grand\">(.*?)</tr>", html, re.S
    )
    assert totals, "у ведомости нет итоговой строки"
    got = dict(
        re.findall(r"<td class=\"[^\"]*\b(hours|accrued|held|paid)\b[^\"]*\">(.*?)</td>",
                   totals.group(1), re.S)
    )
    for key in ("accrued", "held", "paid"):
        assert amount(got[key].strip()) == sum(amount(row[key]) for row in rows), (
            f"итог по колонке «{key}» не равен сумме строк"
        )


def test_a_real_withholding_reaches_the_held_column(
    client, sql, web_env, period_restored, payruns_restored,  # noqa: F811
):
    """Удержание из выплаты видно на бумаге — своей суммой, а не нулём.

    Проверка заведена после нарочной порчи: `held` было заменено на постоянный
    ноль, и прогон остался зелёным. В сиде удержаний нет ни у кого, поэтому
    «начислено − удержано = к выплате» выполнялось само собой при любом
    поведении колонки. Проверка, которую нельзя сломать, не проверяет ничего —
    поэтому удержание здесь заводится намеренно.
    """
    from conftest import wipe_payruns

    who = sql.execute(
        "select t.id, upper(e.last_name || ' ' || e.first_name) "
        "from timesheets t join employees e on e.id = t.employee_id "
        "where t.period = %s order by e.last_name limit 1", (JUNE,)
    ).fetchone()
    assert who, "в июньском табеле нет ни одной строки"
    sheet_id, name = who
    sql.execute("update timesheets set deduction = 1200 where id = %s", (sheet_id,))

    wipe_payruns(web_env)
    login_as(client, "director")
    try:
        assert client.post(
            period_url(sql) + "calculate/", {"inline": "1"}, follow=True
        ).status_code == 200
        rows = payout_rows(payout_page(client, sql))
    finally:
        client.post("/logout/")

    # Имя на бумаге пишется как «ФАМИЛИЯ Имя», а сравниваем без учёта регистра:
    # проверка про деньги, а не про раскладку имени.
    mine = [row for row in rows if row["who"].upper() == name]
    assert mine, f"человека {name} нет в напечатанной ведомости"
    held = amount(mine[0]["held"])
    assert held == Decimal("1200.00"), f"удержание доехало как {held}"
    assert amount(mine[0]["accrued"]) - held == amount(mine[0]["paid"])


def test_the_total_in_words_repeats_the_total_paid(client, sql, june_calculated):  # noqa: F811
    """Сумма прописью — не украшение: по ней сверяют цифру при подписи."""
    from reports.words import in_words

    html = payout_page(client, sql)
    rows = payout_rows(html)
    paid = sum(amount(row["paid"]) for row in rows)

    said = re.search(r"class=\"in-words\">(.*?)</p>", html, re.S)
    assert said, "на ведомости нет суммы прописью"
    assert in_words(paid, language="ru", currency="RSD") in said.group(1)


def test_the_sheet_says_which_page_it_is(client, sql, june_calculated):  # noqa: F811
    """«Лист N из M» совпадает с числом листов в разметке.

    Число листов считает продукт, поэтому оно обязано совпасть с тем, что
    напечатается. Разойдись они — на бумаге появится «Лист 1 из 1» на первом из
    трёх, и потерю двух никто не заметит.
    """
    html = payout_page(client, sql)

    sheets = re.findall(r"<section class=\"paper\"", html)
    said = re.findall(r"Лист (\d+) из (\d+)", html)
    assert said, "на листе не написано, какой он по счёту"
    assert len(said) == len(sheets), "подпись листа стоит не на каждом листе"
    assert [int(number) for number, _of in said] == list(range(1, len(sheets) + 1))
    assert {int(of) for _n, of in said} == {len(sheets)}


def test_the_document_names_the_company_the_month_and_the_currency(
    client, sql, june_calculated,  # noqa: F811
):
    """Бумага без шапки — это листок с числами, а не документ."""
    html = payout_page(client, sql)

    assert "Платёжная ведомость" in html
    assert "июнь 2026" in html.lower() or "Июнь 2026" in html
    assert "RSD" in html


def test_the_sheet_has_lines_to_sign(client, sql, june_calculated):  # noqa: F811
    """Подписывать — главное назначение этой бумаги."""
    html = payout_page(client, sql)

    assert 'class="signatures"' in html
    assert "Расчёт составил" in html and "Утвердил" in html
    assert html.count('<td class="sign"></td>') == len(payout_rows(html))


def test_a_cut_of_the_ledger_refuses_the_payout_form(client, sql, june_calculated):  # noqa: F811
    """Разрезу «к выплате» не принадлежит — и об этом сказано, а не умолчано."""
    from reports import printing

    html = payout_page(client, sql, "?ledger=official")

    assert printing.refusal_text(printing.BY_CUT) in html
    assert '<table class="payout"' not in html, "документ разреза всё-таки собрался"


def test_the_manager_is_refused_the_payout_form_and_told_why(
    client, sql, june_calculated,  # noqa: F811
):
    """Роли с неполным набором регистров бумага не собирается.

    Собранная, она была бы опаснее отсутствующей: часть людей молча пропала бы
    из подписного листа, а итог не сошёлся бы ни с чем.
    """
    from reports import printing

    client.post("/logout/")
    login_as(client, "manager")
    try:
        html = payout_page(client, sql)
    finally:
        client.post("/logout/")
        login_as(client, "director")

    assert printing.refusal_text(printing.TOTALS_WITHHELD) in html
    assert '<table class="payout"' not in html


def test_an_uncalculated_month_refuses_with_its_own_reason(client, sql, web_env):  # noqa: F811
    """Нет расчёта — нет бумаги, и причина названа своя, а не про права."""
    from conftest import wipe_payruns
    from reports import printing

    wipe_payruns(web_env)
    login_as(client, "director")
    try:
        html = payout_page(client, sql)
    finally:
        client.post("/logout/")

    assert printing.refusal_text(printing.NOT_CALCULATED) in html


# =============================================================================
# Расчётный листок
# =============================================================================


def payslip_id(sql, name: str = "") -> str:  # noqa: F811
    where = "and e.last_name = %s" if name else ""
    args = (JUNE, name) if name else (JUNE,)
    return str(sql.execute(
        "select s.id from payslips s "
        "join payruns r on r.id = s.payrun_id "
        "join employees e on e.id = s.employee_id "
        f"where r.period = %s {where} order by e.last_name limit 1", args
    ).fetchone()[0])


def slip_page(client, slip_id: str) -> str:
    response = client.get(f"/payslips/{slip_id}/print/")
    assert response.status_code == 200, response.status_code
    return body(response)


def slip_lines(html: str) -> list[tuple[str, Decimal]]:
    return [
        (title.strip(), amount(value.strip()))
        for title, value in re.findall(
            r"<tr class=\"item\">\s*<td class=\"what\">(.*?)</td>.*?"
            r"<td class=\"[^\"]*\bsum\b[^\"]*\">(.*?)</td>",
            html, re.S,
        )
    ]


def test_the_payslip_lists_the_items_that_make_the_amount(client, sql, june_calculated):  # noqa: F811
    """Позиции листка складываются в начисленное — иначе объяснять нечего."""
    html = slip_page(client, payslip_id(sql))
    lines = slip_lines(html)
    assert lines, "в листке нет ни одной позиции"

    accrued = re.search(r'class="[^"]*\baccrued-sum\b[^"]*">(.*?)<', html, re.S)
    assert accrued, "в листке нет начисленной суммы"
    assert sum(value for _title, value in lines) == amount(accrued.group(1).strip())


def test_the_payslip_says_what_is_paid_and_how(client, sql, june_calculated):  # noqa: F811
    """«К выплате» — то, ради чего листок отдают человеку."""
    html = slip_page(client, payslip_id(sql))

    assert "К выплате" in html
    assert 'class="signatures"' in html


def test_the_payslip_explains_that_gross_is_not_taken_from_the_hand(
    client, sql, june_calculated,  # noqa: F811
):
    """Бруто и взносы на листке обязаны объяснить себя.

    В Сербии договариваются о сумме на руки, а бруто и взносы считаются от неё.
    Числа без этой оговорки читаются как удержание из зарплаты — и человек
    приходит с вопросом, куда делись деньги, которых у него не забирали.
    """
    html = slip_page(client, payslip_id(sql))

    assert "Бруто" in html
    assert 'class="gross-note"' in html


def test_the_payslip_is_refused_when_the_totals_are_not_given_to_the_role(
    client, sql, june_calculated,  # noqa: F811
):
    """Управляющему точки листок не собирается — и причина названа.

    Строка берётся не запросом мимо экрана, а с его собственной ведомости: у
    управляющего свои строки есть и видны, а их итоги — нет (T050). Именно эта
    развилка и проверяется, а не «чужую строку не пускают» (на неё ответ 404,
    и он проверяется отдельно).
    """
    from reports import printing

    client.post("/logout/")
    login_as(client, "manager")
    try:
        seen = body(client.get(period_url(sql)))
        mine = re.search(r"/payslips/([0-9a-f-]{36})/trace/", seen)
        assert mine, "у управляющего нет ни одной своей строки — проверять нечего"
        response = client.get(f"/payslips/{mine.group(1)}/print/")
    finally:
        client.post("/logout/")
        login_as(client, "director")

    assert response.status_code == 200, response.status_code
    assert printing.refusal_text(printing.TOTALS_WITHHELD) in body(response)
    assert '<table class="slip"' not in body(response)


def test_a_payslip_that_does_not_exist_answers_404(client, june_calculated):
    """Чужая строка и несуществующая отвечают одинаково (как у следа расчёта)."""
    missing = "00000000-0000-0000-0000-000000000000"
    assert client.get(f"/payslips/{missing}/print/").status_code == 404


# =============================================================================
# Свойства самой печати
# =============================================================================


def test_the_print_pages_carry_nothing_that_a_sheet_of_paper_cannot_do(
    client, sql, june_calculated,  # noqa: F811
):
    """На бумаге нет кнопок, шапки продукта и переключателей.

    Не косметика: напечатанная кнопка — это предложение сделать то, чего с листа
    сделать нельзя, и человек ищет способ на неё нажать.
    """
    for html in (payout_page(client, sql), slip_page(client, payslip_id(sql))):
        assert "<button" not in html
        assert 'class="appbar"' not in html
        assert 'class="cut' not in html


def test_both_forms_are_reachable_from_the_screens(client, sql, june_calculated):  # noqa: F811
    """До бумаги можно дойти руками, не подбирая адрес.

    Форма, о которой знает только тот, кто читал код, продукта не касается.
    Ведомость берут со страницы периода, листок — со следа расчёта: «как
    получилась эта сумма» и «отдать человеку бумагу» — соседние шаги одного
    разговора.
    """
    period = period_url(sql)
    screen = body(client.get(period))
    assert f'href="{period}print/payout/"' in screen, "со страницы периода печать не открыть"
    # Разрез в адрес печати не уезжает: он получил бы отказ с собственного экрана.
    assert f'href="{period}print/payout/?ledger=' not in screen

    slip = payslip_id(sql)
    trace = body(client.get(f"/payslips/{slip}/trace/"))
    assert f'href="/payslips/{slip}/print/"' in trace, "со следа расчёта листок не открыть"


def test_the_print_stylesheet_pins_a4_and_a_light_sheet():
    """Геометрия листа зафиксирована в одном месте и совпадает с расчётом.

    Разъедься миллиметры разметки с миллиметрами разбиения — продукт напишет
    «Лист 1 из 2», а браузер напечатает три, и один из них пустой.
    """
    from pathlib import Path

    from reports import printing

    css = (
        Path(__file__).resolve().parents[1]
        / "src/web/static/web/print.css"
    ).read_text(encoding="utf-8")

    assert "@page" in css and "size: A4" in css and "margin: 0" in css
    assert f"width: {printing.SHEET_WIDTH_MM}mm" in css
    assert f"height: {printing.SHEET_MM}mm" in css
    assert f"--print-row: {printing.ROW_MM}mm" in css
