"""Предпросчёт на карточке: что человеку начислится в этом месяце (issue #185).

Зачем. До этого проверить условия найма можно было одним способом — посчитать
весь месяц и посмотреть, что вышло. То есть узнать об опечатке в ставке после
расчёта тридцати человек, а поправить её — пересчётом всех.

**Считает тот же движок, что и месяц.** Свой упрощённый расчёт «для показа»
разошёлся бы с настоящим — и хуже всего в тот день, когда человек поверил
карточке. Поэтому здесь нет ни одной формулы: берётся `payrun.calc.compute`,
который считает месяц и **ничего не записывает**, и из его результата достаётся
один человек.

**Чтение остаётся чтением.** Карточку открывают чаще, чем считают месяц; запись
при просмотре засорила бы ведомость строками, которых никто не просил.

Цена честная: предпросчёт считает весь месяц, чтобы показать одного. Это
осознанный размен — правильность против скорости. Станет медленно на большой
сети — считать по одному человеку придётся движку, а не этому модулю.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from uuid import UUID

from django.utils.translation import gettext as _

from .format import money

__all__ = ["Preview", "preview_for"]


@dataclass(frozen=True)
class Line:
    title: str
    # Сумма уже строкой: форматирование денег в этом продукте живёт в питоне
    # (`web/format.py`), а не в шаблоне — разделители зависят от языка страницы,
    # и второй способ их ставить разошёлся бы с первым.
    amount: str


@dataclass(frozen=True)
class Preview:
    """Что начислится человеку. Пусто — с объяснением, а не молча."""

    period: date
    lines: list[Line]
    net: str
    gross: str
    note: str = ""

    def __bool__(self) -> bool:
        return bool(self.lines) or bool(self.note)


def preview_for(tenant_id: UUID, employee_id: UUID, period: date) -> Preview:
    """Предпросчёт одного человека за месяц. Ничего не записывает."""
    from django.utils.translation import get_language

    from payrun.calc import compute
    from payrun.errors import PayrunRefused

    try:
        # Язык страницы, а не язык правил: предпросмотр ничего не замораживает,
        # его читают здесь и сейчас — и подписи начислений в нём обязаны быть
        # на том же языке, что и остальной экран.
        _rules, results = compute(tenant_id, period, language=get_language() or "")
    except PayrunRefused as refusal:
        # Отказ расчёта — это ответ, а не поломка карточки: не хватает правил,
        # не сходится база взносов. Человеку он полезнее пустого блока.
        return Preview(period=period, lines=[], net=money(0), gross=money(0),
                       note=str(refusal))

    slip = next(
        (result for case, result in results if case.employee_id == employee_id), None,
    )
    if slip is None:
        return Preview(
            period=period, lines=[], net=money(0), gross=money(0),
            note=_("Часов за этот месяц нет — начислять пока нечего."),
        )

    return Preview(
        period=period,
        lines=[Line(title=c.title, amount=money(c.amount)) for c in slip.components],
        net=money(slip.net),
        gross=money(slip.gross),
    )
