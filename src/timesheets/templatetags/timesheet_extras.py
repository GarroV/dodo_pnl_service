"""Показ часов на экране табеля."""
from __future__ import annotations

from decimal import Decimal

from django import template
from django.utils.formats import date_format
from django.utils.timezone import localtime
from django.utils.translation import gettext as _

from web.format import money

register = template.Library()


@register.filter
def hours(value) -> str:
    """Часы столбиком: `176,00`. Ноль — пусто.

    Ноль часов и незаполненная ячейка на этом экране одно и то же: человек
    вводит только то, что было. Двадцать две строки «0,00» в сетке на 35 человек
    прячут заполненные значения — а именно их нужно видеть.
    """
    if value is None or Decimal(value) == 0:
        return ""
    return money(value)


@register.filter
def cell_value(value) -> str:
    """Значение в поле ввода: машинное, с точкой. Пустое вместо нуля."""
    if value is None or Decimal(value) == 0:
        return ""
    return f"{Decimal(value):.2f}"


@register.filter
def get(mapping, key):
    """Значение словаря по ключу-переменной: в языке шаблонов этого нет."""
    return mapping.get(key)


@register.simple_tag
def author_note(who: str, when) -> str:
    """Кто поставил число и когда — одной фразой (T143, issue #52).

    Тегом, а не разметкой в каждом месте: подсказка нужна и ячейкам часов, и
    колонке базы для взносов, и сдельной величине, а фраза у них одна. Две копии
    разъехались бы на первой правке, и одна из них осталась бы непереведённой.

    **Пусто — это ответ, а не пустая подсказка.** Автора нет у ровной раскладки
    прежнего итога и у всего, что пришло мимо продукта. Молчание в этом месте
    читалось бы как поломка, а не как «этого мы не знаем».

    Время показывается форматом языка страницы (`DATETIME_FORMAT`) и в часовом
    поясе читателя: в базе оно в UTC, и показанное «09:33» вместо «12:33»
    выглядело бы правкой, сделанной не тогда, когда её сделали.
    """
    if not who or when is None:
        return _("кто поставил, не записано")
    return _("поставил %(who)s, %(when)s") % {
        "who": who,
        "when": date_format(localtime(when), "DATETIME_FORMAT"),
    }


@register.simple_tag
def author_note_of(row, column) -> str:
    """То же, но для ячейки часов: автор берётся по коду колонки.

    Отдельным тегом, потому что в языке шаблонов нет выборки из словаря по
    переменной, а склеивать два фильтра ради одной подсказки — значит писать
    в разметке то, что и так знает строка.
    """
    found = (row.authors or {}).get(column.code)
    return author_note(found.name if found else "", found.at if found else None)


@register.filter
def percent(value) -> str:
    """`1.10` → `110%`. Подпись к колонке, а не число для счёта."""
    if value is None:
        return ""
    return f"{Decimal(value) * 100:.0f}%"
