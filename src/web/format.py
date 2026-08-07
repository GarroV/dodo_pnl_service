"""
Показ чисел. Соглашение блока: деньги — по правому краю моноширинным,
разделитель тысяч — пробел, ноль отличается от пустого значения.

Разделители пока жёстко русские: локали появятся вместе с переводами
в четвёртой очереди, и тогда это место заменится штатным механизмом Django.
"""
from __future__ import annotations

from decimal import Decimal

# Неразрывный пробел рвал бы поиск по странице и сравнение в тестах, поэтому
# обычный: выравнивание всё равно держится на моноширинном начертании.
THOUSANDS = " "
EMPTY = "—"

# Регистры учёта по-человечески. В базе значения пока прежние
# (`white/grey/black`): переименование — задача T004, следующая очередь.
LEDGER_TITLES = {
    "white": "Официальный",
    "grey": "Дополнительный",
    "black": "Внутренний",
}


def ledger_title(value: str) -> str:
    return LEDGER_TITLES.get(value, value)


def money(value: Decimal | int | float | None) -> str:
    """Сумма с двумя знаками: `1 234,50`. Пустое значение — прочерк, не ноль."""
    if value is None:
        return EMPTY
    quantized = Decimal(value).quantize(Decimal("0.01"))
    whole, _, fraction = f"{quantized:.2f}".partition(".")
    sign = ""
    if whole.startswith("-"):
        sign, whole = "-", whole[1:]
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{sign}{THOUSANDS.join(groups)},{fraction}"
