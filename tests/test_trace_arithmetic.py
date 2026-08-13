"""След расчёта: показанные числа складываются в показанный итог (T116).

Чем эта проверка отличается от той, что уже есть в `test_reports_trace.py`.
Там складывается **колонка «Сумма»**: шаги дают итог строки. Это верно и
ничего не говорит о том, откуда взялась сама сумма шага. Здесь проверяется
второе сложение, ради которого экран и написан: человек берёт числа из колонки
«Из чего собрано», перемножает их на калькуляторе и обязан получить число
справа.

Именно это и не работало. `input_value` форматировал **любой** `Decimal`
деньгами, до двух знаков, и на экране стояло:

    часов 152,00 · ставка за час 421,08 · процент оплаты 1,00  →  64 004,92

152 × 421,08 = 64 004,16. В базе ставка 421,085, коэффициент 1,135, множитель
нето → бруто 0,701 — тот самый 0,701, про который в `CLAUDE.md` написано «через
полгода никто не вспомнит, откуда взялось». Экран показывал 0,70.

Ставка, коэффициент и множитель — **не деньги**, и округление их до копеек
делает след неверным ровно там, где он обязан объяснять. Поэтому проверки ниже
идут по разметке страницы, а не по внутренним объектам: сходятся объекты и на
экране округлено — это и был дефект.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from conftest import body, login_as, period_url, wipe_payruns
from test_reports_trace import sheet_rows, to_decimal

D = Decimal
CENTS = D("0.01")

# Пара «подпись — значение» так, как её видит человек: подпись словами,
# значение жирным. Разбор идёт по разметке намеренно (см. модульный docstring).
PAIR = re.compile(r'<span class="pair">(.*?)<b>(.*?)</b>\s*</span>', re.S)
ROW = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
NUM_CELL = re.compile(r'<td class="num[^"]*">\s*([^<\s][^<]*?)\s*</td>')


@pytest.fixture
def calculated_june(client, web_env):
    """Посчитанный июнь на данных сида — общий материал для проверок ниже.

    Своя копия в каждом файле проверок намеренно: фикстура, импортированная из
    соседнего модуля, тянет за собой его порядок и его материал, а стоит она
    пять строк.
    """
    wipe_payruns(web_env)
    login_as(client, "director")
    response = client.post(period_url(client) + "calculate/", follow=True)
    assert response.status_code == 200
    return None


def steps_with_inputs(html: str) -> list[tuple[dict[str, str], Decimal]]:
    """Шаги страницы: подписанные входы и сумма шага. Оба — из разметки."""
    found = []
    for row in ROW.findall(html):
        pairs = PAIR.findall(row)
        amounts = NUM_CELL.findall(row)
        if not pairs or not amounts:
            continue
        inputs = {title.strip(): value.strip() for title, value in pairs}
        found.append((inputs, to_decimal(amounts[-1])))
    assert found, "на странице следа нет ни одного шага с входами"
    return found


def cents(value: Decimal) -> Decimal:
    return value.quantize(CENTS)


def reproduces(got: Decimal, amount: Decimal, *, from_money: bool) -> bool:
    """Сошлось ли то, что человек получил на калькуляторе, с числом на экране.

    Копейка допуска — и только там, где в формулу входят **деньги**. Нето и
    бруто показаны в копейках потому, что это копейки: та же сумма стоит в
    ведомости и уедет в банк, и показывать её иначе нельзя. Внутри же расчёта
    у неё есть хвост, поэтому цепочка через деньги имеет право разойтись на
    последнюю копейку — но не больше.

    Там, где все входы точны (часы × ставка × процент), допуска нет вовсе:
    именно там и жил дефект, ради которого написана эта проверка, — 152 ×
    421,08 = 64 004,16 против показанных 64 004,92, то есть 76 копеек.
    """
    return abs(cents(got) - amount) <= (CENTS if from_money else 0)


def check_step(inputs: dict[str, str], amount: Decimal) -> list[str]:
    """Что из этого шага можно повторить на калькуляторе. Возвращает — что проверено.

    Проверяется только то, что на экране показано целиком: шаг, у которого
    видна часть входов, молчит, а не считается сошедшимся. Пустой список
    проверок у **всех** шагов означал бы зелёный тест, не проверивший ничего, —
    поэтому вызывающий требует их количество.
    """
    done: list[str] = []
    have = {title: to_decimal(value) for title, value in inputs.items()
            if re.fullmatch(r"-?[\d  ]+,\d+", value)}

    # Часы × ставка × процент — формула начисления за часы.
    if {"часов", "ставка за час", "процент оплаты"} <= set(have):
        got = have["часов"] * have["ставка за час"] * have["процент оплаты"]
        assert reproduces(got, amount, from_money=False), (
            f"часов {inputs['часов']} × ставка {inputs['ставка за час']} × "
            f"процент {inputs['процент оплаты']} = {cents(got)}, "
            f"а на экране {amount}"
        )
        done.append("часы × ставка")

    # Ставка сотрудника — это базовая, умноженная на коэффициент. Обе показаны
    # рядом ровно затем, чтобы третье число из них получалось.
    if {"базовая ставка", "коэффициент", "ставка за час"} <= set(have):
        got = have["базовая ставка"] * have["коэффициент"]
        assert got == have["ставка за час"], (
            f"базовая {inputs['базовая ставка']} × коэффициент "
            f"{inputs['коэффициент']} = {got}, а ставкой показано "
            f"{inputs['ставка за час']}"
        )
        done.append("базовая × коэффициент")

    # Нето → бруто: (нето − зачтено) / множитель.
    if {"нето", "множитель нето → бруто"} <= set(have):
        base = have["нето"] - have.get("зачтено", D(0))
        got = base / have["множитель нето → бруто"]
        assert reproduces(got, amount, from_money=True), (
            f"({inputs['нето']} − {inputs.get('зачтено', '0')}) / "
            f"{inputs['множитель нето → бруто']} = {cents(got)}, "
            f"а на экране {amount}"
        )
        done.append("нето → бруто")

    # Взносы по плоской ставке: бруто × ставка взносов.
    if {"бруто", "ставка взносов"} <= set(have):
        got = have["бруто"] * have["ставка взносов"]
        assert reproduces(got, amount, from_money=True), (
            f"бруто {inputs['бруто']} × ставка взносов "
            f"{inputs['ставка взносов']} = {cents(got)}, а на экране {amount}"
        )
        done.append("бруто × ставка взносов")

    # Взносы работодателя плюс удержанное с работника.
    if {"бруто", "взносы работодателя", "удержано с работника"} <= set(have):
        got = have["бруто"] * have["взносы работодателя"] + have["удержано с работника"]
        assert reproduces(got, amount, from_money=True), (
            f"бруто {inputs['бруто']} × {inputs['взносы работодателя']} + "
            f"{inputs['удержано с работника']} = {cents(got)}, а на экране {amount}"
        )
        done.append("взносы работодателя + удержанное")

    return done


# --- главное: сложить показанное и получить показанное ------------------------


def test_the_shown_numbers_reproduce_the_shown_sum(client, calculated_june):
    """Приёмка T116: бухгалтер повторяет каждый шаг на калькуляторе.

    Идёт по всем строкам ведомости директора, а не по одной: округление ставки
    видно не у каждого сотрудника (у коэффициента 1,000 ставка целая), и
    проверка на удачно выбранной строке зеленела бы при том же дефекте.
    """
    checked: list[str] = []
    for url, _total in sheet_rows(client, "director"):
        html = body(client.get(url))
        for inputs, amount in steps_with_inputs(html):
            checked += check_step(inputs, amount)

    assert len(checked) > 60, f"проверок повторения сумм слишком мало: {len(checked)}"
    # Каждая формула обязана встретиться: пропавшая означает, что проверка
    # смотрит уже не на тот экран, а тест при этом зелёный.
    assert set(checked) == {
        "часы × ставка", "базовая × коэффициент", "нето → бруто",
        "бруто × ставка взносов", "взносы работодателя + удержанное",
    }, f"проверены не все формулы следа: {sorted(set(checked))}"


def test_the_reader_notices_a_step_that_does_not_multiply():
    """Страховка от фиктивной зелени: разбор обязан краснеть на подделке."""
    rows = steps_with_inputs(
        '<tr><td class="inputs">'
        '<span class="pair">часов <b>152,00</b></span>'
        '<span class="pair">ставка за час <b>421,08</b></span>'
        '<span class="pair">процент оплаты <b>1,00</b></span>'
        '</td><td class="num strong">64 004,92</td></tr>'
    )
    with pytest.raises(AssertionError):
        check_step(*rows[0])


# --- откуда бралось округление ------------------------------------------------


@pytest.mark.parametrize(
    "value,shown",
    [
        ("421.08500000", "421,085"),   # ставка часа: 371 × 1,135
        ("1.1350", "1,135"),           # коэффициент сотрудника
        ("0.701", "0,701"),            # тот самый множитель из CLAUDE.md
        ("0.4505", "0,4505"),          # ставка взносов по временному договору
        ("0.1515", "0,1515"),          # взносы работодателя
        ("371.00", "371,00"),          # целая ставка остаётся с двумя знаками
        ("120960.00", "120 960,00"),   # разделитель тысяч на месте
        ("0", "0,00"),                 # ноль — это ноль, а не пусто
        ("-1.5", "-1,50"),             # знак не теряется, знаков не меньше двух
    ],
)
def test_a_rate_is_shown_as_it_is_stored(value, shown):
    """Не деньги — не округляем. Два знака остаются нижней границей, не верхней."""
    from web.format import exact

    assert exact(D(value)) == shown


def test_a_long_tail_says_that_it_continues():
    """У доли часов знаков бесконечно много, и молча обрезать их нельзя.

    Обрезанное без пометки читается как точное значение, а по нему сумма не
    повторится: это тот же класс правдоподобно-неверного, из-за которого
    задача и заведена. Многоточие говорит, что продолжение есть.
    """
    from web.format import exact

    assert exact(D(88) / D(176)) == "0,50"
    assert exact(D(90) / D(176)) == "0,511363…"


def test_every_input_the_engine_produces_has_a_declared_kind(serbia_preset, web_env):
    """Новый вход движка не должен молча получить формат денег.

    Список снимается с самого движка — как и список подписей рядом
    (`test_every_input_the_engine_produces_has_a_human_label`). Умолчание у
    неизвестного ключа — «показать как есть»: лишние знаки на экране некрасивы,
    округлённая ставка — неверна, и ошибаться здесь можно только в первую
    сторону.
    """
    from test_reports_trace import employee, timesheet
    from web.views import INPUT_KINDS

    produced: set[str] = set()
    for scheme in ("standard", "half_time", "half_time_min_base", "direct", "temporary"):
        for shape in ({"regular": 176}, {"regular": 32, "sick": 20},
                      {"regular": 88, "vacation": 8, "holiday": 8}):
            try:
                from payroll import PayrollEngine

                slip = PayrollEngine(serbia_preset).calculate(
                    employee(scheme=scheme), timesheet(**shape)
                )
            except KeyError:
                continue  # схемы нет в пресете — это проверяет другой тест
            for step in slip.trace:
                produced |= set(step.input_values)

    missing = sorted(produced - set(INPUT_KINDS))
    assert not missing, f"входы движка без объявленного вида: {', '.join(missing)}"


# --- подпись величины соответствует её смыслу (issue #72, #75) ----------------


def test_the_contributions_rate_is_not_called_an_hourly_rate(client, calculated_june):
    """`rate` шага взносов — это ставка взносов, а не ставка за час.

    Смысл `rate` задаёт соседний вход того же шага, а не имя ключа: у сдельной
    группы это цена единицы (T081), у взносов — ставка взносов. Подпись,
    которая врёт про величину, обесценивает экран ровно там, где его читают.
    """
    seen = False
    for url, _total in sheet_rows(client, "director"):
        html = body(client.get(url))
        for inputs, _amount in steps_with_inputs(html):
            if "какая ставка" not in inputs:
                continue
            seen = True
            assert "ставка за час" not in inputs, (
                f"ставка взносов подписана как ставка за час: {inputs}"
            )
            assert "ставка взносов" in inputs, f"у ставки взносов нет подписи: {inputs}"
    assert seen, "в июне нет ни одного шага взносов по ставке — проверять нечего"
