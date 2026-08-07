"""Какой пресет правил применять.

Пресет выбирается по стране тенанта и дате периода — не по условию в коде.
Страну и дату начала действия объявляет сам пресет (`country`, `valid_from`),
поэтому новая страна = новый YAML и ни строчки кода.

Временное: правила лежат в файлах. Задача T011 переносит их в таблицу
`rule_presets` вместе с переопределениями партнёра — тогда меняется тело этой
функции, а не её вызовы.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from payroll import list_presets, load_preset

from .errors import PayrunRefused


def _valid_from(preset: dict[str, Any]) -> date:
    value = preset.get("valid_from")
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def select_preset(country_code: str, on_date: date) -> tuple[str, dict[str, Any]]:
    """Действующий пресет страны на дату: его код и тело.

    Из нескольких подходящих берётся самый поздний — так же, как берётся
    последняя версия правила в базе.
    """
    candidates = []
    for code in list_presets():
        preset = load_preset(code)
        if str(preset.get("country", "")).upper() != country_code.upper():
            continue
        if _valid_from(preset) <= on_date:
            candidates.append((_valid_from(preset), code, preset))

    if not candidates:
        raise PayrunRefused(
            f"нет правил расчёта для страны {country_code} на {on_date:%m.%Y}. "
            "Добавьте пресет страны — считать наугад нечем."
        )
    _, code, preset = max(candidates, key=lambda item: item[0])
    return code, preset
