"""Какие правила применять к расчёту периода.

Правила выбираются по стране тенанта и дате периода — не по условию в коде.
Страну и дату начала действия объявляет сам пресет, поэтому новая страна = новая
строка в `rule_presets` и ни строчки кода.

Источник — база (T011). YAML-файлы остались источником **первичной загрузки**
страны: их разово перекладывает в таблицу `manage.py load_presets`. Молчаливого
отката на файл здесь нет намеренно: он прятал бы незагруженные правила, и расчёт
шёл бы мимо переопределений партнёра, выглядя при этом правильным.
"""
from __future__ import annotations

from datetime import date
from uuid import UUID

from core.rules import PresetNotFound, RuleSet, load_rules_at

from .errors import PayrunRefused

__all__ = ["select_rules"]


def select_rules(tenant_id: UUID, country_code: str, on_date: date,
                 *, language: str | None = None) -> RuleSet:
    """Действующие правила тенанта: код пресета, тело и переопределения.

    `language` — язык, к которому свернуть подписи правил (T092). Экран
    оставляет его пустым и получает язык страницы. Расчёт передаёт `""` —
    язык, объявленный самим пресетом: подпись компонента замерзает в
    `pay_components.title`, и зависеть она должна от правил, а не от того, на
    каком языке смотрел человек, нажавший «посчитать». Иначе один и тот же
    период, пересчитанный дважды, хранил бы разные слова.
    """
    try:
        return load_rules_at(tenant_id, country_code, on_date, language=language)
    except PresetNotFound as exc:
        raise PayrunRefused(str(exc)) from exc
