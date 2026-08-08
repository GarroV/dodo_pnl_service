"""Срез ведомости: одна таблица на все регистры плюс разрез по одному из них.

Три решения, на которых всё держится.

**Разрез только сужает.** Сюда приезжают суммы, уже отобранные политиками базы:
чужой регистр в список не попадает физически. Поэтому разрез не может ничего
открыть — он может только убрать лишнее с экрана, и это свойство закреплено
тестом (`test_a_cut_never_widens_what_was_given`).

**Список разрезов собирается из показанных строк, а не из справочника
регистров и не из роли.** Пустая кнопка «Внутренний» — тоже сообщение о том,
что такой регистр существует и в нём кто-то есть, а D023 требует «ни строк, ни
следа». Отсюда же поведение на подобранный адрес `?ledger=internal`: разрез,
которого в видимых строках нет, молча приравнивается к «все видимые», и ответ
неотличим от ответа без параметра вовсе.

**Итог разреза считается заново.** Ведомость пересобирается из отобранных сумм
целиком — вместе с колонками и подвалом, — а не маскируется на выводе. Иначе
итог остался бы от полной ведомости и выдал бы скрытое вычитанием; ровно так
устроены две утечки, уже закрытые в этом продукте (T050, T071).

Оформления здесь нет намеренно: названия регистров и формат чисел — дело того,
кто показывает. Экран берёт отсюда срез и рисует его страницей, выгрузка (T032)
берёт **тот же самый срез** и пишет его файлом.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from payrun.sheet import LEDGER_ORDER, Cell, Sheet, assemble, collect_cells

# Разрез «все видимые регистры». Пустая строка, а не None: это же значение
# приезжает из адресной строки, и лишнего преобразования между ними быть не
# должно.
ALL = ""


@dataclass(frozen=True)
class Cut:
    """Одна кнопка переключателя. Названия здесь нет — его даёт показывающий."""

    code: str
    selected: bool


@dataclass(frozen=True)
class SheetSlice:
    """Что человек видит сейчас: ведомость, выбранный разрез и чем его сменить."""

    sheet: Sheet
    cut: str
    cuts: list[Cut]


def shown_ledgers(sheet: Sheet) -> list[str]:
    """Регистры, которые есть в показанных строках, в постоянном порядке.

    Порядок один на весь продукт и объявлен в `payrun.sheet.LEDGER_ORDER`:
    кнопки переключателя обязаны стоять в том же порядке, что строки, иначе
    одна и та же ведомость читается как разные данные.
    """
    return sorted(
        {row.ledger for row in sheet.rows},
        key=lambda ledger: (
            LEDGER_ORDER.index(ledger) if ledger in LEDGER_ORDER else len(LEDGER_ORDER),
            ledger,
        ),
    )


def slice_cells(cells: list[Cell], cut: str = ALL) -> SheetSlice:
    """Собрать срез из видимых сумм. Чистая функция: базы здесь нет."""
    whole = assemble(cells)
    available = shown_ledgers(whole)

    # Разрез, которого в видимых строках нет, — это «все видимые». Не отказ и
    # не пустая таблица: и то и другое было бы ответом на вопрос «а есть ли
    # такой регистр».
    chosen = cut if cut in available else ALL

    sheet = whole if chosen == ALL else assemble(
        [cell for cell in cells if cell.ledger == chosen]
    )

    # Переключатель из одной кнопки переключать нечего — его не рисуем вовсе.
    # Это не украшение: у роли с одним регистром ряд кнопок намекал бы, что
    # где-то есть и другие.
    cuts = (
        [Cut(ALL, chosen == ALL)] + [Cut(code, code == chosen) for code in available]
        if len(available) > 1
        else []
    )
    return SheetSlice(sheet=sheet, cut=chosen, cuts=cuts)


def build_slice(tenant_id: UUID, period: date, cut: str = ALL) -> SheetSlice:
    """Срез ведомости периода из базы — общий вход для экрана и выгрузки."""
    return slice_cells(collect_cells(tenant_id, period), cut)
