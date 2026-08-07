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

# Регистры учёта по-человечески. Значения — как в базе (тип `ledger`);
# на экране всегда подпись, а не код: код здесь ничего не объясняет.
LEDGER_TITLES = {
    "official": "Официальный",
    "supplementary": "Дополнительный",
    "internal": "Внутренний",
}


def ledger_title(value: str) -> str:
    return LEDGER_TITLES.get(value, value)


def hours(value: Decimal | int | float | None) -> str:
    """Часы с двумя знаками: `176,00`. Пустое значение — прочерк, не ноль.

    Отдельно от `money`, хотя формат похож: разделитель тысяч часам не нужен
    (месяц короче тысячи часов), а главное — «нормы нет» и «норма ноль» на
    экране должны отличаться так же, как отличаются в базе.
    """
    if value is None:
        return EMPTY
    whole, _, fraction = f"{Decimal(value).quantize(Decimal('0.01')):.2f}".partition(".")
    return f"{whole},{fraction}"


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
