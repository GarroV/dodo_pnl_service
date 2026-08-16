"""
Набор демо-данных: то, что он обязан показывать, проверяется до всякой базы.

Смысл этих проверок не «пересчитать константы». Definition of Done блока говорит,
что демо показывает продукт целиком: два юрлица, три точки, тридцать человек, все
четыре схемы расчёта, все три регистра учёта, два закрытых месяца и один
открытый. Каждое из этих требований можно сломать одной правкой в списке людей и
не заметить — тогда демо тихо станет беднее, а узнают об этом на показе.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from demo import dataset
from demo.dataset import MONTHS, PEOPLE, UNITS
from payroll import load_preset

CYRILLIC = re.compile(r"[а-яА-ЯёЁ]")


@pytest.fixture(scope="module")
def preset() -> dict:
    return load_preset("serbia-2026")


def test_thirty_people():
    assert len(PEOPLE) == 30
    assert len({person.key for person in PEOPLE}) == 30, "двое людей с одним ключом"


def test_two_legal_entities_and_three_units():
    assert len(dataset.LEGAL_ENTITIES) == 2
    assert len(UNITS) == 3
    # Юрлицо каждой точки — из того же списка, а не выдуманное на месте.
    known = {title for title, _tax in dataset.LEGAL_ENTITIES}
    assert {entity for _code, _title, entity in UNITS} <= known
    # Оба юрлица используются: второе, на которое никто не ссылается, ничего не
    # показывает.
    assert {entity for _code, _title, entity in UNITS} == known


def test_all_four_schemes_are_present(preset):
    """Четыре схемы расчёта продукта плюс прямая выплата курьерам."""
    schemes = set()
    for person in PEOPLE:
        schemes.add(person.scheme or preset["groups"][person.group]["scheme"])
    assert {"standard", "half_time", "half_time_min_base", "temporary"} <= schemes
    assert "direct" in schemes, "курьеров нет — внутренний регистр показать нечем"


def test_all_three_ledgers_are_present(preset):
    ledgers = {preset["groups"][person.group]["ledger"] for person in PEOPLE}
    assert ledgers == {"official", "supplementary", "internal"}


def test_two_closed_months_and_one_open():
    assert len(MONTHS) == 3
    assert [m.approved for m in MONTHS] == [True, True, False]
    # По возрастанию: расчёт и утверждение идут в этом порядке, и переставленный
    # список молча сломал бы перенос разницы задним числом.
    assert [m.period for m in MONTHS] == sorted(m.period for m in MONTHS)


def test_nothing_in_the_demo_is_written_in_russian():
    """Демо всегда англоязычное — правило владельца, а не пожелание."""
    words = [person.first for person in PEOPLE] + [person.last for person in PEOPLE]
    words += [title for _code, title, _entity in UNITS]
    words += [title for title, _tax in dataset.LEGAL_ENTITIES]
    words += [
        event.correction_reason
        for event in dataset.EVENTS.values()
        if event.correction_reason
    ]
    russian = [word for word in words if CYRILLIC.search(word)]
    assert not russian, f"по-русски в демо: {russian}"


def test_nobody_from_the_anonymised_fixture_is_here(sample_rows):
    """Ни одной строки из обезличенной таблицы партнёра (D028).

    Проверка не про «а вдруг мы её загрузили»: источник данных здесь другой
    физически. Она про то, что имена не совпадут случайно, если однажды кто-то
    решит «взять пару человек из фикстуры, чтобы не выдумывать».
    """
    from_fixture = {row.name.strip() for row in sample_rows}
    ours = {person.key for person in PEOPLE}
    assert not (from_fixture & ours)


def test_insured_base_matches_the_hours(preset):
    """База взносов = сумма застрахованных часов.

    Расхождение здесь означало бы отказ расчёта «база не сходится с часами» — на
    данных, которые мы пишем сами. Демо, которое не считается, не демо.
    """
    insured_types = {
        code for code, body in preset["hour_types"].items() if body.get("insured")
    }
    # Подмножество, а не равенство: в правилах страны застрахованными помечены и
    # ночные с переработкой, но они там заготовки (`status: unverified`), и в
    # табелях демо их нет. Требование поэтому двустороннее: наши виды часов
    # обязаны быть застрахованными, а других видов в табелях быть не должно —
    # иначе база взносов разошлась бы с часами именно на них.
    assert set(dataset.insured_types()) <= insured_types
    used = {
        kind
        for month in MONTHS
        for person in PEOPLE
        if (row := dataset.timesheet_for(person, month)) is not None
        for kind in row.hours
    }
    assert used == set(dataset.insured_types())

    for month in MONTHS:
        for person in PEOPLE:
            row = dataset.timesheet_for(person, month)
            if row is None:
                continue
            declared = sum(
                (row.hours.get(kind, Decimal(0)) for kind in insured_types), Decimal(0)
            )
            assert row.insured_hours == declared, f"{person.key} {month.period}"


def test_hours_never_exceed_the_norm():
    """Отпуск и болезнь **вместо** работы, а не сверх неё."""
    for month in MONTHS:
        for person in PEOPLE:
            row = dataset.timesheet_for(person, month)
            if row is None:
                continue
            assert sum(row.hours.values(), Decimal(0)) == row.norm_hours


def test_one_person_appears_mid_way_and_one_gets_a_raise():
    """Два события, ради которых отчёт расхождений вообще есть что показать."""
    first, second, third = MONTHS
    newcomers = [
        person for person in PEOPLE
        if dataset.timesheet_for(person, first) is None
        and dataset.timesheet_for(person, second) is not None
    ]
    assert len(newcomers) == 1, "нужен ровно один принятый в середине стенда"

    raised = [person for person in PEOPLE if person.raise_from is not None]
    assert len(raised) == 1
    person = raised[0]
    assert dataset.rate_at(person, first.period) != dataset.rate_at(person, third.period)


def test_events_reach_every_month():
    """В каждом месяце что-то происходит: одинаковые месяцы отчёту нечего сравнить."""
    for month in MONTHS:
        changed = [
            person for person in PEOPLE
            if (person.key, f"{month.period:%Y-%m}") in dataset.EVENTS
        ]
        assert changed, f"в {month.period:%Y-%m} не происходит ничего"


def test_manual_correction_carries_a_reason():
    """Правка руками без объяснения — сумма, которую никто не объяснит (D025)."""
    corrections = [
        event for event in dataset.EVENTS.values()
        if event.manual_correction is not None
    ]
    assert corrections, "в демо нет ни одной правки руками"
    for event in corrections:
        assert event.correction_reason.strip()


def test_the_open_month_has_a_till_and_a_vat_expense():
    """Экран, куда демо приводит посетителя (Н7, сверка 8), не должен быть пустым.

    Посетитель попадает на **открытый** месяц — последний в `MONTHS` — и это
    единственный месяц, чьи данные он видит без смены фильтра. Касса и НДС
    проставлены только у закрытых месяцев (июнь, июль) — обе фичи (T145, T146)
    посетителю не видны вовсе, хотя обе уже приняты. Красный этот тест значит:
    открытый месяц опять показывает «—» в обеих колонках.
    """
    open_month = MONTHS[-1]
    assert not open_month.approved, "последний месяц набора должен быть открытым"
    open_key = f"{open_month.period:%Y-%m}"
    of_the_month = [e for e in dataset.EXPENSES if e.on.strftime("%Y-%m") == open_key]
    assert any(e.till for e in of_the_month), (
        f"в {open_key} ни одного расхода из кассы — колонка Till пуста на экране входа"
    )
    assert any(e.vat for e in of_the_month), (
        f"в {open_key} ни одного расхода с НДС — колонка VAT пуста на экране входа"
    )


def test_no_expense_mentions_a_till_it_does_not_have():
    """Комментарий про кассу без самой кассы — витрина, которая противоречит себе.

    Найдено сверкой ровно в этом виде: строка «Fuel paid from the till» при
    пустой колонке Till читается посетителем как поломка продукта, а не как
    незаполненный сид. Проверка — по признаку «в комментарии упомянута касса»
    (`"till" in note.lower()`), а не по конкретной фразе: иначе она зазеленеет
    на любой перефразировке того же противоречия.
    """
    liars = [
        e for e in dataset.EXPENSES
        if "till" in e.note.lower() and not e.till
    ]
    assert not liars, [f"{e.on} {e.unit} {e.item}: {e.note!r}" for e in liars]
