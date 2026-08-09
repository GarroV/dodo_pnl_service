"""
Показ чисел. Соглашение блока: деньги — по правому краю моноширинным,
разделитель тысяч — по языку страницы, ноль отличается от пустого значения.

Почему разделители свои, а не из локалей Django (T017). У Django они есть, но в
русской локали разделитель тысяч — **неразрывный** пробел. На экране он выглядит
так же, а поиск по странице, копирование в калькулятор и сравнение в тестах
ломает молча: «1 951 806,13» глазами и `1\\xa0951\\xa0806,13` в разметке — разные
строки. Поэтому таблица ниже: она короткая, читается целиком и не удивляет.
"""
from __future__ import annotations

from decimal import Decimal

from django.utils.translation import get_language
from django.utils.translation import gettext_lazy as _

# Разделители по языку. Русский — пробел и запятая, английский — запятая и
# точка, сербский — точка и запятая (как в самой зарплатной таблице партнёра).
SEPARATORS = {
    "ru": (" ", ","),
    "en": (",", "."),
    "sr-latn": (".", ","),
}
# Умолчание — русское: язык исходника. Неизвестный язык лучше показать привычно,
# чем без разделителей вовсе.
DEFAULT_SEPARATORS = SEPARATORS["ru"]

EMPTY = "—"


def separators() -> tuple[str, str]:
    """Разделитель тысяч и десятичный — для языка, на котором рисуется страница."""
    return SEPARATORS.get((get_language() or "").lower(), DEFAULT_SEPARATORS)


# Регистры учёта по-человечески. Значения — как в базе (тип `ledger`);
# на экране всегда подпись, а не код: код здесь ничего не объясняет.
#
# Словарь знает все три названия, и это не нарушение D023: подпись берётся
# только для регистра, который **уже отобран** политиками базы для этой роли.
# Показывать сам список — нельзя ни на одном экране, включая переключатель
# языка: перечень названий и есть сообщение о том, что регистр существует.
LEDGER_TITLES = {
    "official": _("Официальный"),
    "supplementary": _("Дополнительный"),
    "internal": _("Внутренний"),
}


def ledger_title(value: str) -> str:
    """Название регистра на языке страницы.

    Возвращается готовая строка, а не отложенный перевод: значение уезжает в
    списки, в `", ".join(...)` и в ячейки xlsx, и отложенный объект там либо
    падает, либо превращается в служебный мусор.
    """
    known = LEDGER_TITLES.get(value)
    return str(known) if known is not None else value


# Разрез ведомости «все видимые регистры» (`reports.sheet.ALL`). Название
# честное: «все» здесь — это все, что видно роли, а не все, что есть в базе.
ALL_LEDGERS_TITLE = _("Все регистры")


def cut_title(value: str) -> str:
    """Подпись кнопки переключателя разреза."""
    return str(ALL_LEDGERS_TITLE) if not value else ledger_title(value)


def hours(value: Decimal | int | float | None) -> str:
    """Часы с двумя знаками: `176,00`. Пустое значение — прочерк, не ноль.

    Отдельно от `money`, хотя формат похож: разделитель тысяч часам не нужен
    (месяц короче тысячи часов), а главное — «нормы нет» и «норма ноль» на
    экране должны отличаться так же, как отличаются в базе.
    """
    if value is None:
        return EMPTY
    _thousands, decimal = separators()
    whole, _dot, fraction = f"{Decimal(value).quantize(Decimal('0.01')):.2f}".partition(".")
    return f"{whole}{decimal}{fraction}"


def money(value: Decimal | int | float | None) -> str:
    """Сумма с двумя знаками: `1 234,50`. Пустое значение — прочерк, не ноль."""
    if value is None:
        return EMPTY
    thousands, decimal = separators()
    quantized = Decimal(value).quantize(Decimal("0.01"))
    whole, _dot, fraction = f"{quantized:.2f}".partition(".")
    sign = ""
    if whole.startswith("-"):
        sign, whole = "-", whole[1:]
    groups = []
    while len(whole) > 3:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    groups.insert(0, whole)
    return f"{sign}{thousands.join(groups)}{decimal}{fraction}"
