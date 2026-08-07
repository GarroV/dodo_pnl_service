"""Показ часов на экране табеля."""
from __future__ import annotations

from decimal import Decimal

from django import template

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


@register.filter
def percent(value) -> str:
    """`1.10` → `110%`. Подпись к колонке, а не число для счёта."""
    if value is None:
        return ""
    return f"{Decimal(value) * 100:.0f}%"
