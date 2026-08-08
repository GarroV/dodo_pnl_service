"""Экран следа расчёта: от суммы до входных часов и версии правила (T029).

Что проверяется и почему именно так.

**По следу можно повторить сумму руками.** Это приёмка задачи, поэтому главная
проверка — не «поля заполнены», а сложение: числа, показанные на экране,
складываются в итог строки ведомости. Складывает разборщик разметки, а не
внутренние объекты: человек складывает то, что видит.

**След не показывает шагов из регистров, которых роли не видно** (D023). Шаг
чужого регистра — это и строка, и след её существования сразу: по нему видно и
сумму, и правило, и человека. Поэтому у бухгалтера итог следа обязан совпадать
с её же строкой ведомости, а не с полной.

**След пересобирается по сегодняшним правилам** — хранения следа в продукте
пока нет (issue #48, T056). Значит экран обязан **сверить** пересобранное с
сохранённым и сказать, если они разошлись: молчаливое «вот как это посчитано»
поверх других правил — ровно та ложь, которой на экране быть не должно.
"""
from __future__ import annotations

import re
from datetime import date
from decimal import Decimal
from html.parser import HTMLParser

import pytest

from conftest import body, login_as, period_url, wipe_payruns

JUNE = date(2026, 6, 1)
MIN_RATE = Decimal("371")


# --- материал для проверок без базы ------------------------------------------


def employee(scheme="standard", group="office", rate=MIN_RATE, coef=1, ledger=None):
    from payroll import Employee, d

    return Employee(
        ext_id="test", name="Тест Тестович", group=group, scheme=scheme,
        base_rate=d(rate), coefficient=d(coef), ledger=ledger,
    )


def timesheet(**hours):
    from payroll import Timesheet, d

    insured = d(hours.pop("insured", 176))
    return Timesheet(
        hours={k: d(v) for k, v in hours.items()},
        insured_hours=insured, norm_hours=insured,
    )


def stored_from(e, ts, preset) -> dict:
    """Что лежало бы в базе, посчитай мы этими же правилами: (код, регистр) → сумма."""
    from payroll import PayrollEngine
    from reports.trace import to_cents

    rows: dict = {}
    for component in PayrollEngine(preset).calculate(e, ts).components:
        key = (component.code, component.ledger)
        rows[key] = rows.get(key, Decimal(0)) + to_cents(component.amount)
    return rows


# --- главное: след складывается в сумму строки -------------------------------


def test_the_steps_add_up_to_the_stored_row(serbia_preset):
    """Приёмка T029 в самой прямой форме: сложил шаги — получил строку."""
    from reports.trace import trace_row

    e, ts = employee(), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)
    view = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=["official"])

    assert view.steps, "след пуст — объяснять нечем"
    assert view.traced_total == sum(stored.values(), Decimal(0))
    assert view.agrees


def test_every_step_carries_what_the_sum_was_made_of(serbia_preset):
    """Часы × ставка × процент — три числа, по которым сумма повторяется руками."""
    from reports.trace import trace_row

    e, ts = employee(coef=2), timesheet(regular=156, sick=20)
    view = trace_row(e, ts, serbia_preset, stored=stored_from(e, ts, serbia_preset),
                     visible_ledgers=["official"])

    sick = next(step for step in view.steps if step.code == "hours.sick")
    assert sick.inputs["hours"] == 20
    assert sick.inputs["rate"] == MIN_RATE * 2
    assert sick.inputs["pay_percent"] == Decimal("0.65")
    assert sick.inputs["rate"] * sick.inputs["pay_percent"] * sick.inputs["hours"] == sick.amount


def test_the_step_names_the_level_the_rule_came_from(serbia_preset):
    """Версия правила — часть контракта блока: «по чему считали», а не только формула."""
    from reports.trace import trace_row

    e, ts = employee(), timesheet(regular=176)
    view = trace_row(e, ts, serbia_preset, stored=stored_from(e, ts, serbia_preset),
                     visible_ledgers=["official"])
    assert {step.level for step in view.steps} == {"country"}


def test_the_screen_shows_the_engine_trace_and_not_a_second_one(serbia_preset):
    """Второго следа рядом с движковым нет — это и есть главное решение задачи."""
    from payroll.trace import explain
    from reports.trace import to_cents, trace_row

    e, ts = employee(), timesheet(regular=120, sick=16)
    view = trace_row(e, ts, serbia_preset, stored=stored_from(e, ts, serbia_preset),
                     visible_ledgers=["official"])

    engine_steps = [s for s in explain(e, ts, serbia_preset) if s.contributes_to == "net"]
    assert [step.code for step in view.steps] == [s.rule_code for s in engine_steps]
    # Экран округляет до копейки — тем же способом, каким записана строка
    # (см. `to_cents`), — но ничего больше с числом движка не делает.
    assert [step.amount for step in view.steps] == [
        to_cents(s.applied_value) for s in engine_steps
    ]


# --- ни строк, ни следа: чужие регистры --------------------------------------


def test_steps_of_an_invisible_ledger_are_not_shown_at_all(serbia_preset):
    """Шаг чужого регистра выдал бы и сумму, и правило, и человека сразу."""
    from reports.trace import trace_row

    e, ts = employee(ledger="internal"), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)
    hours = next(key for key in stored if key[0] == "hours.regular")
    assert hours[1] == "internal", "материал собран не про тот регистр"

    view = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=["official"])

    assert "hours.regular" not in {step.code for step in view.steps}
    assert all(step.ledger == "official" for step in view.steps)
    assert view.traced_total == stored[("meal_and_vacation_bonus", "official")]


def test_the_visible_part_still_adds_up_when_part_is_hidden(serbia_preset):
    """Половина строки скрыта — итог следа равен видимой половине, не всей.

    Иначе скрытое вычисляется вычитанием: ровно так устроены обе утечки,
    закрытые в этом продукте (T050, T071). Материал настоящий: часы сотрудника
    кухни идут в дополнительном регистре, а надбавка объявлена пресетом в
    официальном — то есть строка честно разложена на две половины.
    """
    from reports.trace import trace_row

    e, ts = employee(group="kitchen", ledger="supplementary"), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)
    assert {ledger for _code, ledger in stored} == {"official", "supplementary"}
    visible_part = sum(
        (amount for (_code, ledger), amount in stored.items() if ledger == "official"),
        Decimal(0),
    )

    view = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=["official"])

    assert {step.ledger for step in view.steps} == {"official"}
    assert view.traced_total == visible_part
    assert view.traced_total < sum(stored.values(), Decimal(0))


def test_derived_totals_are_hidden_when_the_row_is_not_fully_visible(serbia_preset):
    """Бруто и взносы посчитаны по всем регистрам сразу — это готовая утечка.

    Они показываются, только когда роль видит строку целиком. Тот же довод, по
    которому `payslip_totals` закрыты отдельной политикой (T050).
    """
    from reports.trace import trace_row

    e, ts = employee(group="kitchen", ledger="supplementary"), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)

    partial = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=["official"])
    whole = trace_row(e, ts, serbia_preset, stored=stored,
                      visible_ledgers=["official", "supplementary"])

    assert partial.derived == []
    assert whole.derived, "при полной видимости строки производные обязаны быть"
    assert {step.kind for step in whole.derived} >= {"gross", "contributions"}


def test_a_cut_narrows_the_trace_the_same_way_the_sheet_narrows(serbia_preset):
    """Разрез следа — тот же разрез, что у ведомости: один способ на два экрана."""
    from reports.trace import trace_row

    e, ts = employee(group="kitchen", ledger="supplementary"), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)
    visible = ["official", "supplementary"]

    cut = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=visible,
                    cut="supplementary")
    assert {step.ledger for step in cut.steps} == {"supplementary"}
    assert cut.traced_total == sum(
        amount for (_code, ledger), amount in stored.items() if ledger == "supplementary"
    )
    # Производные не про разрез: они посчитаны по строке целиком.
    assert cut.derived == []


def test_an_invisible_cut_falls_back_to_everything_visible(serbia_preset):
    """Ответ на подобранный `?ledger=` неотличим от ответа без параметра вовсе."""
    from reports.trace import trace_row

    e, ts = employee(), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)

    guessed = trace_row(e, ts, serbia_preset, stored=stored,
                        visible_ledgers=["official"], cut="internal")
    plain = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=["official"])

    assert guessed.cut == plain.cut == ""
    assert guessed.traced_total == plain.traced_total
    assert [step.code for step in guessed.steps] == [step.code for step in plain.steps]


# --- честность: след пересобран, а не сохранён -------------------------------


def test_a_rule_changed_after_the_calculation_is_reported_as_disagreement(serbia_preset):
    """Правила уехали — экран обязан сказать это, а не выдать новый счёт за старый.

    Хранения следа в продукте нет (issue #48, T056), поэтому единственная
    защита от вранья — сверка пересобранного с сохранённым.
    """
    from payroll.presets import apply_overrides
    from reports.trace import trace_row

    e, ts = employee(), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)          # посчитано вчера
    tweaked = apply_overrides(
        serbia_preset, {"allowances.meal_and_vacation_bonus.amount_per_norm": 2000}
    )

    view = trace_row(e, ts, tweaked, stored=stored, visible_ledgers=["official"])

    assert not view.agrees, "след разошёлся с сохранённым, а экран этого не заметил"
    assert view.stored_total == sum(stored.values(), Decimal(0))
    assert view.traced_total != view.stored_total
    changed = next(step for step in view.steps if step.code == "meal_and_vacation_bonus")
    assert changed.differs and changed.stored == Decimal("1500.00")


def test_a_change_that_cancels_itself_in_the_total_is_still_caught(serbia_preset):
    """Сверка идёт покомпонентно, а не по одному итогу — и в этом весь смысл.

    Понижение процента больничного гасится доплатой до минимума: итог остаётся
    прежним до копейки, а считалось при этом другое. Сверка одних итогов такое
    пропустила бы молча.
    """
    from payroll.presets import apply_overrides
    from reports.trace import trace_row

    e, ts = employee(), timesheet(regular=120, sick=20)
    stored = stored_from(e, ts, serbia_preset)
    tweaked = apply_overrides(serbia_preset, {"hour_types.sick.pay_percent": 0.5})

    view = trace_row(e, ts, tweaked, stored=stored, visible_ledgers=["official"])

    assert view.traced_total == view.stored_total, "материал теста собран не про тот случай"
    assert not view.agrees, "итог сошёлся, а считали другим — экран этого не заметил"
    assert next(step for step in view.steps if step.code == "hours.sick").differs


def test_a_step_that_appeared_out_of_nowhere_is_marked_too(serbia_preset):
    """Шаг без сохранённой пары — тоже расхождение, а не «просто новая строка»."""
    from reports.trace import trace_row

    e, ts = employee(), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)
    stored.pop(("meal_and_vacation_bonus", "official"))

    view = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=["official"])
    appeared = next(step for step in view.steps if step.code == "meal_and_vacation_bonus")

    assert appeared.stored is None and appeared.differs
    assert not view.agrees


def test_agreement_is_measured_only_on_the_visible_part(serbia_preset):
    """Сверка идёт по видимому срезу — иначе она сама стала бы каналом утечки.

    Роль, которой не видно половины строки, увидела бы «не сходится» ровно на
    размер скрытого — то есть узнала бы его величину.
    """
    from reports.trace import trace_row

    e, ts = employee(group="kitchen", ledger="supplementary"), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)

    view = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=["official"])
    assert view.agrees, "видимая часть сошлась, а экран считает иначе"
    assert view.stored_total == sum(
        amount for (_code, ledger), amount in stored.items() if ledger == "official"
    )


# --- перенос из закрытого месяца ---------------------------------------------


def test_a_carried_line_says_where_it_came_from_instead_of_pretending(serbia_preset):
    """Разница за прошлый месяц правилом этого месяца не объясняется — и не врёт.

    Её объяснение живёт в периоде-источнике; здесь стоит строка со ссылкой на
    него, и в сверку следа она не входит (так же, как в `payrun.retro._stored`).
    """
    from reports.trace import Carried, trace_row

    e, ts = employee(), timesheet(regular=176)
    stored = stored_from(e, ts, serbia_preset)
    carried = [Carried(code="hours.regular", title="Отработанные", amount=Decimal("500.00"),
                       ledger="official", source_period=date(2026, 5, 1))]

    view = trace_row(e, ts, serbia_preset, stored=stored, visible_ledgers=["official"],
                     carried=carried)

    assert [line.source_period for line in view.carried] == [date(2026, 5, 1)]
    assert view.agrees, "перенос не должен ломать сверку следа этого месяца"
    assert view.traced_total + Decimal("500.00") == view.row_total


def test_a_carried_line_of_an_invisible_ledger_is_dropped(serbia_preset):
    """Регистр переноса — свойство компонента, и он тоже может быть чужим."""
    from reports.trace import Carried, trace_row

    e, ts = employee(), timesheet(regular=176)
    carried = [Carried(code="hours.regular", title="Отработанные", amount=Decimal("500.00"),
                       ledger="internal", source_period=date(2026, 5, 1))]

    view = trace_row(e, ts, serbia_preset, stored=stored_from(e, ts, serbia_preset),
                     visible_ledgers=["official"], carried=carried)
    assert view.carried == []


# --- страница --------------------------------------------------------------


class StepsReader(HTMLParser):
    """Читает таблицу следа так же, как её читает человек: по тексту ячеек."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = False
        self.section = ""
        self.rows: list[list[str]] = []
        self.foot: list[list[str]] = []
        self.cell: list[str] | None = None
        self.text: list[str] = []
        self.depth = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag == "table":
            self.depth += 1
            if "steps" in (attributes.get("class") or "").split():
                self.inside = True
        if not self.inside:
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
                self.inside = False
            return
        if not self.inside:
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
        if self.inside:
            self.text.append(data)


def to_decimal(value: str) -> Decimal:
    return Decimal(value.replace(" ", "").replace(" ", "").replace(",", "."))


def read_steps(html: str) -> StepsReader:
    reader = StepsReader()
    reader.feed(html)
    assert reader.rows, "на странице следа нет ни одного шага — объяснять нечем"
    return reader


def steps_add_up(html: str, who: str) -> Decimal:
    """Сумма чисел в колонке «Сумма» равна итогу в подвале — то самое сложение."""
    reader = read_steps(html)
    parts = [to_decimal(row[-1]) for row in reader.rows]
    assert reader.foot, "у следа нет подвала с итогом"
    total = to_decimal(reader.foot[-1][-1])
    assert sum(parts, Decimal(0)) == total, (
        f"{who}: шаги дают {sum(parts, Decimal(0))}, а в подвале {total}"
    )
    return total


def test_the_reader_notices_steps_that_do_not_add_up():
    """Страховка от фиктивной зелени: разбор обязан краснеть на подделке."""
    html = """<table class="steps">
      <tbody><tr><td>Отработанные</td><td class="num">100,00</td></tr></tbody>
      <tfoot><tr><td>Итого</td><td class="num">999,00</td></tr></tfoot>
    </table>"""
    with pytest.raises(AssertionError):
        steps_add_up(html, "подделка")


@pytest.fixture
def calculated_june(client, web_env):
    wipe_payruns(web_env)
    login_as(client, "director")
    response = client.post(period_url(client) + "calculate/", follow=True)
    assert response.status_code == 200
    return None


def sheet_rows(client, user: str) -> list[tuple[str, str, Decimal]]:
    """Строки ведомости глазами роли: (адрес следа, регистр, итог строки)."""
    login_as(client, user)
    html = body(client.get(period_url(client)))
    found = re.findall(
        r'<td class="num strong"><a class="trace" href="([^"]+)"[^>]*>([^<]+)</a>',
        html, re.S,
    )
    assert found, "в ведомости нет ни одной ссылки на след расчёта"
    return [(url.replace("&amp;", "&"), to_decimal(total)) for url, total in found]


def test_every_row_of_the_sheet_leads_to_its_own_trace(client, calculated_june):
    """Экран открывается не «вообще», а из строки — иначе он не про её сумму."""
    rows = sheet_rows(client, "director")
    assert len(rows) == 60, f"ссылок на след {len(rows)}, а строк ведомости 60"


@pytest.mark.parametrize("user", ["director", "accountant", "manager"])
def test_the_trace_page_adds_up_to_the_row_it_came_from(client, calculated_june, user):
    """Главная проверка на живых данных и для каждой роли отдельно."""
    login_as(client, user)
    for url, row_total in sheet_rows(client, user)[:6]:
        html = body(client.get(url))
        assert steps_add_up(html, f"{user} / {url}") == row_total


def test_the_accountant_never_meets_another_ledger_on_the_trace(client, calculated_june):
    """Ни в шагах, ни в подписях, ни в производных величинах (D023)."""
    login_as(client, "accountant")
    for url, _ in sheet_rows(client, "accountant")[:8]:
        html = body(client.get(url))
        for name in ("Дополнительный", "Внутренний"):
            assert name not in html, f"на следе бухгалтера есть слово «{name}»: {url}"


def test_the_trace_of_a_foreign_row_is_indistinguishable_from_a_missing_one(
    client, calculated_june
):
    """Чужая строка и несуществующая отвечают одинаково — 404 без подробностей."""
    director = sheet_rows(client, "director")
    accountant = {url.split("/")[2] for url, _ in sheet_rows(client, "accountant")}
    foreign = next(
        url for url, _ in director if url.split("/")[2] not in accountant
    )

    login_as(client, "accountant")
    missing = client.get("/payslips/00000000-0000-0000-0000-000000000000/trace/")
    theirs = client.get(foreign)
    assert theirs.status_code == missing.status_code == 404


def test_the_page_says_the_trace_is_rebuilt_and_whether_it_still_agrees(
    client, calculated_june
):
    """Экран не выдаёт пересобранный след за сохранённый (issue #48, T056)."""
    login_as(client, "director")
    url, _ = sheet_rows(client, "director")[0]
    html = body(client.get(url))
    assert "пересобран" in html, "экран молчит о том, что след не хранится"
    assert "сходится" in html, "экран не говорит, сошёлся ли след с сохранённой суммой"


def test_the_rounding_is_the_same_one_that_wrote_the_row(web_env):
    """`reports.trace.to_cents` обязан совпадать с `payrun.calc.money`.

    Округлений в продукте два — второе завелось затем, чтобы `reports` остался
    чистым Python без Django. Разойдись они, след «не сходился» бы на копейку
    у всех подряд, и объяснить это было бы нечем. Поэтому равенство проверено,
    а не обещано комментарием.
    """
    from payrun.calc import money
    from reports.trace import to_cents

    for value in ("0.005", "1.005", "-1.005", "2.344", "2.345", "1022.727272727272727",
                  "65296", "0.014999999"):
        assert to_cents(Decimal(value)) == money(Decimal(value)), value
