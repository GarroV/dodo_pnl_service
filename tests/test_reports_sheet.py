"""Ведомость с разрезом по регистрам (T028).

Что здесь проверяется и почему именно так.

**Итог обязан быть суммой видимых строк.** Это не про красоту, а про D023:
если показанный роли итог больше суммы её строк, скрытый регистр вычисляется
вычитанием, и «ни строк, ни следа» перестаёт выполняться. Поэтому проверка
идёт не по внутренним объектам, а **по разметке страницы**: складываются числа
из последней колонки каждой строки и сравниваются с числом в подвале — ровно то,
что человек может сложить на калькуляторе.

**Переключатель разреза не называет чужих регистров.** Пустая строка с
названием «Внутренний» — тоже сообщение о его существовании, поэтому список
разрезов собирается из **показанных строк**, а не из справочника регистров и не
из роли.

**Проверка идёт ролью `app_user`.** Владелец таблиц и суперпользователь обходят
RLS, и на этом в проекте уже прожил незамеченным дефект видимости регистров.
Числа, которые видит бухгалтер в браузере, сверяются с тем, что отдаёт база
её же ролью.
"""
from __future__ import annotations

from decimal import Decimal
from html.parser import HTMLParser

import pytest

from conftest import body, login_as, narrowed_ledgers, period_url, wipe_payruns

JUNE = "2026-06-01"

# Ориентир приёмки, снятый на данных сида. Три роли перебираются не для
# полноты: дефект видимости — это всегда расхождение между ролями, и одной
# ролью он не виден.
#
# После D036 у бухгалтера набор регистров полон, как у директора, и её точки
# в сиде тоже не сужены — её строки и итог теперь равны директорским. Роль с
# реально урезанным набором собирается ниже явно `narrowed_ledgers`, где
# проверка именно про ограничение, а не про бухгалтера как таковую (см.
# `NARROWED_ACCOUNTANT`).
CONTROL = {
    "director": (60, Decimal("1951806.13")),
    "accountant": (60, Decimal("1951806.13")),
    "manager": (24, Decimal("891373.32")),
}

# Набор, до которого сужается бухгалтер там, где нужна роль с её правами и
# урезанным набором (единственная в сиде с правом видеть все точки и
# `payrun.calculate` — управляющий этого сочетания не даёт, см. D031).
NARROWED_ACCOUNTANT = ["official"]
# Итог ведомости под этим набором — тот самый, что был её умолчательным
# итогом до D036 (см. `CONTROL` в `test_reports_reconcile_db.py`).
NARROWED_ACCOUNTANT_TOTAL = Decimal("464752.41")

HIDDEN_FROM_ACCOUNTANT = ("Дополнительный", "Внутренний")


# --- чтение того, что видно глазами ------------------------------------------


class SheetReader(HTMLParser):
    """Разбирает таблицу ведомости так же, как её читает человек.

    Смотрит на текст ячеек, а не на внутренние объекты: тест обязан ловить
    расхождение между посчитанным и **показанным**, а сравнение объекта с самим
    собой ничего не проверяет.
    """

    def __init__(self) -> None:
        super().__init__()
        self.in_sheet = False
        self.section = ""      # tbody | tfoot
        self.rows: list[list[str]] = []
        self.foot: list[list[str]] = []
        self.cell: list[str] | None = None
        self.text: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            self.depth += 1
            if "sheet" in (attributes.get("class") or "").split():
                self.in_sheet = True
        if not self.in_sheet:
            return
        if tag in ("tbody", "tfoot", "thead"):
            self.section = tag
        elif tag == "tr":
            self.cell = []
        elif tag in ("td", "th"):
            self.text = []

    def handle_endtag(self, tag):
        if tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.in_sheet = False
            return
        if not self.in_sheet:
            return
        if tag in ("td", "th") and self.cell is not None:
            self.cell.append(" ".join("".join(self.text).split()))
        elif tag == "tr" and self.cell is not None:
            if self.section == "tbody":
                self.rows.append(self.cell)
            elif self.section == "tfoot":
                self.foot.append(self.cell)
            self.cell = None

    def handle_data(self, data):
        if self.in_sheet:
            self.text.append(data)


def read_sheet(html: str) -> SheetReader:
    reader = SheetReader()
    reader.feed(html)
    assert reader.rows, "на странице нет ни одной строки ведомости — проверять нечего"
    return reader


def to_decimal(value: str) -> Decimal:
    """«1 951 806,13» → Decimal. Ровно обратное тому, что делает `web.format.money`."""
    cleaned = value.replace(" ", "").replace(" ", "").replace(",", ".")
    return Decimal(cleaned)


def row_totals(html: str) -> list[Decimal]:
    return [to_decimal(row[-1]) for row in read_sheet(html).rows]


def shown_total(html: str) -> Decimal:
    foot = read_sheet(html).foot
    assert foot, "у ведомости нет подвала с итогом"
    return to_decimal(foot[-1][-1])


def ledgers_shown(html: str) -> set[str]:
    """Названия регистров в колонке «Регистр» — то, что человек видит в строках."""
    reader = read_sheet(html)
    return {row[2] for row in reader.rows}


def assert_total_matches_rows(html: str, who: str) -> Decimal:
    """Главное требование задачи в точной форме: итог = сумма видимых строк."""
    rows = row_totals(html)
    total = shown_total(html)
    assert sum(rows, Decimal(0)) == total, (
        f"{who}: показано строк {len(rows)} на {sum(rows, Decimal(0))}, "
        f"а итог в подвале {total} — разница выдаёт скрытое вычитанием"
    )
    return total


def test_the_reader_notices_a_total_that_does_not_match(client):
    """Страховка от фиктивной зелени: проверка обязана краснеть на подделке.

    Без этого нельзя отличить «итог сходится» от «разбор ничего не нашёл».
    """
    html = """<table class="sheet">
      <tbody><tr><td>Иванов</td><td>BG1</td><td>Официальный</td><td></td>
        <td class="num strong">100,00</td></tr></tbody>
      <tfoot><tr class="grand"><td colspan="4">Итого</td>
        <td class="num">999,00</td></tr></tfoot>
    </table>"""
    with pytest.raises(AssertionError):
        assert_total_matches_rows(html, "подделка")


# --- разрез: чистая функция без базы -----------------------------------------


def cells(*triples):
    """Материал для сборки: `(сотрудник, регистр, сумма)`."""
    from payrun.sheet import Cell

    return [
        Cell(employee=name, unit="BG1", ledger=ledger, code=f"pay.{ledger}",
             title=f"Начислено {ledger}", amount=Decimal(amount), key=name)
        for name, ledger, amount in triples
    ]


def test_a_cut_narrows_rows_and_recounts_the_total():
    """Итог разреза считается заново по его строкам, а не берётся от полного."""
    from reports.sheet import slice_cells

    material = cells(
        ("Иванов", "official", "100.00"),
        ("Иванов", "supplementary", "50.00"),
        ("Петров", "official", "300.00"),
    )

    whole = slice_cells(material)
    assert whole.sheet.total == Decimal("450.00")

    only_official = slice_cells(material, "official")
    assert [row.ledger for row in only_official.sheet.rows] == ["official", "official"]
    assert only_official.sheet.total == Decimal("400.00")
    assert only_official.sheet.total == sum(
        (row.total for row in only_official.sheet.rows), Decimal(0)
    )


def test_the_switcher_offers_only_the_ledgers_that_are_actually_shown():
    """Разрез, которого в видимых строках нет, не предлагается даже пустым.

    Пустая строка с названием регистра — это сообщение о его существовании,
    то есть тот самый «след» из D023.
    """
    from reports.sheet import slice_cells

    material = cells(("Иванов", "official", "100.00"), ("Петров", "official", "300.00"))
    offered = {cut.code for cut in slice_cells(material).cuts}
    assert "supplementary" not in offered and "internal" not in offered


def test_there_is_no_switcher_when_there_is_nothing_to_switch():
    """Один регистр — переключать нечего, и ряд кнопок из одной кнопки не рисуется."""
    from reports.sheet import slice_cells

    material = cells(("Иванов", "official", "100.00"))
    assert slice_cells(material).cuts == []


def test_the_switcher_keeps_the_order_of_the_ledgers():
    """Порядок разрезов тот же, что у строк: переставленные читаются как другие."""
    from reports.sheet import slice_cells

    material = cells(
        ("Иванов", "internal", "1.00"),
        ("Иванов", "official", "2.00"),
        ("Петров", "supplementary", "3.00"),
    )
    codes = [cut.code for cut in slice_cells(material).cuts]
    assert codes == ["", "official", "supplementary", "internal"]


def test_an_unavailable_cut_falls_back_to_everything_visible():
    """Чужой регистр в адресе не должен ни показывать пустоту, ни признавать себя.

    Ответ на `?ledger=internal` от того, кому внутренний не виден, обязан быть
    неотличим от ответа без параметра вовсе: иначе адрес превращается в
    вопросник «а есть ли такой регистр».
    """
    from reports.sheet import slice_cells

    material = cells(("Иванов", "official", "100.00"), ("Петров", "official", "300.00"))
    guessed = slice_cells(material, "internal")
    plain = slice_cells(material)

    assert guessed.cut == plain.cut == ""
    assert guessed.sheet.total == plain.sheet.total
    assert [row.ledger for row in guessed.sheet.rows] == ["official", "official"]


def test_a_cut_never_widens_what_was_given():
    """Разрез только сужает: показать он может лишь то, что доехало из базы.

    Строки чужих регистров до сборки не доезжают вовсе — их режут политики.
    Здесь закреплено, что и сама сборка ничего не добавляет.
    """
    from reports.sheet import slice_cells

    material = cells(("Иванов", "official", "100.00"))
    for cut in ("", "official", "supplementary", "internal", "нет-такого"):
        result = slice_cells(material, cut)
        assert {row.ledger for row in result.sheet.rows} <= {"official"}


# --- на живых данных, через страницу -----------------------------------------


@pytest.fixture
def calculated_june(client, web_env):
    """Посчитанный июнь на данных сида — общий материал для проверок ниже."""
    wipe_payruns(web_env)
    login_as(client, "director")
    response = client.post(period_url(client) + "calculate/", follow=True)
    assert response.status_code == 200
    return None


def page(client, user: str, query: str = "") -> str:
    login_as(client, user)
    return body(client.get(period_url(client) + query))


def test_the_total_equals_the_visible_rows_for_every_role(client, calculated_june):
    """Главная проверка T028 — и она же ориентир приёмки, снятый на сиде."""
    for user, (rows, total) in CONTROL.items():
        html = page(client, user)
        assert len(row_totals(html)) == rows, f"{user}: строк ведомости не {rows}"
        assert assert_total_matches_rows(html, user) == total


def test_the_accountant_is_never_told_that_other_ledgers_exist(client, web_env, calculated_june):
    """Ни строкой, ни разрезом, ни пустой кнопкой (D023).

    После D036 у бухгалтера набор регистров полон, поэтому проверка держится
    на роли с её правами, но урезанной явно `narrowed_ledgers` — иначе она
    проверяла бы обычное поведение равного директору доступа, а не D023.
    """
    with narrowed_ledgers(web_env, "accountant", NARROWED_ACCOUNTANT):
        html = page(client, "accountant")
        assert ledgers_shown(html) == {"Официальный"}
        for name in HIDDEN_FROM_ACCOUNTANT:
            assert name not in html, f"на странице бухгалтера есть слово «{name}»"


def test_the_manager_sees_a_switcher_with_exactly_his_two_ledgers(client, calculated_june):
    """У управляющего два регистра (D031) — переключать есть что, но не три."""
    html = page(client, "manager")
    assert ledgers_shown(html) == {"Официальный", "Дополнительный"}
    assert "Внутренний" not in html


def test_a_cut_keeps_the_total_equal_to_the_visible_rows(client, calculated_june):
    """Итог пересчитывается по разрезу, а не остаётся от полной ведомости."""
    whole = page(client, "director")
    parts = {}
    for ledger in ("official", "supplementary", "internal"):
        html = page(client, "director", f"?ledger={ledger}")
        parts[ledger] = assert_total_matches_rows(html, f"директор, разрез {ledger}")
        assert len(ledgers_shown(html)) == 1, "в разрезе показан не один регистр"

    assert sum(parts.values(), Decimal(0)) == shown_total(whole), (
        f"сумма разрезов {sum(parts.values(), Decimal(0))} не сходится с полной "
        f"ведомостью {shown_total(whole)}"
    )


def test_a_cut_the_role_cannot_see_shows_the_same_page_as_no_cut(
    client, web_env, calculated_june
):
    """Роль без внутреннего регистра, набравшая `?ledger=internal`, видит свою обычную ведомость.

    После D036 у бухгалтера внутренний регистр виден, и адрес перестал быть
    для неё подобранным — поэтому здесь та же урезанная явно роль, что и в
    предыдущей проверке, а не её настоящий набор.
    """
    with narrowed_ledgers(web_env, "accountant", NARROWED_ACCOUNTANT):
        guessed = page(client, "accountant", "?ledger=internal")
        plain = page(client, "accountant")

        assert shown_total(guessed) == shown_total(plain) == NARROWED_ACCOUNTANT_TOTAL
        for name in HIDDEN_FROM_ACCOUNTANT:
            assert name not in guessed


def test_the_page_total_equals_what_the_database_gives_her_role(client, web_env, calculated_june):
    """Сверка показанного с базой **ролью `app_user`**, а не владельцем схемы.

    Владелец таблиц и суперпользователь обходят RLS: под ними ведомость сойдётся
    с базой даже при снятых политиках, и проверка не будет значить ничего.
    """
    import psycopg

    from conftest import as_app_user
    from core.db_types import register_enum_types
    from core.management.commands.seed_dev import det_id

    for user, (_, expected) in CONTROL.items():
        with psycopg.connect(web_env) as conn:
            register_enum_types(conn)
            with as_app_user(conn, str(det_id("user", user))) as scoped:
                visible = scoped.execute(
                    """select coalesce(sum(c.amount), 0)
                         from pay_components c
                         join payslips p on p.id = c.payslip_id
                         join payruns r on r.id = p.payrun_id
                        where r.period = %s""",
                    (JUNE,),
                ).fetchone()[0]
        assert visible == expected, f"{user}: база отдаёт {visible}, ожидалось {expected}"
        assert shown_total(page(client, user)) == visible, (
            f"{user}: страница показывает не то, что отдаёт база его же ролью"
        )
