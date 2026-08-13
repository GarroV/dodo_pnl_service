"""Правила расчёта из базы: сборка пресета на дату и по слоям (T011).

Почему это здесь, а не в движке: движок обязан оставаться чистым Python без
ORM — он умеет считать по пресету и ничего не знает ни про базу, ни про
тенантов. Здесь наоборот: только чтение таблиц `rule_presets` и
`rule_overrides` и передача собранного тела движку.

Порядок слоёв — страна → партнёр → группа → сотрудник, каждый следующий сильнее
предыдущего. Первый слой (страна) — это само тело пресета; поверх него ложатся
переопределения, у каждого своя дата начала и конца действия.

Границы периода `[valid_from, valid_to)`: конец не входит, поэтому «по 1 июля»
и «с 1 июля» стыкуются, а не спорят. Так же читает расчёт и так же устроены
ограничения `EXCLUDE` в схеме (см. журнал блока db, D015).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any
from uuid import UUID

from payroll.presets import (
    LEVELS,
    Origin,
    Preset,
    build_preset,
    list_presets,
    load_preset_body,
    preset_valid_from,
    to_jsonable,
)

from .models import RuleOverride, RulePreset

__all__ = ["PresetNotFound", "RuleSet", "import_presets", "load_preset_at", "load_rules_at"]


class PresetNotFound(LookupError):
    """Правил для страны на эту дату в базе нет — считать наугад нечем."""


@dataclass(frozen=True)
class RuleSet:
    """Правила тенанта на дату: общая часть плюс переопределения по объектам.

    Собирается одним походом в базу на весь расчёт: тел пресетов и строк
    переопределений мало, а вот запрос на каждого из тридцати человек был бы
    ровно тем N+1, из-за которого расчёт периода потом «необъяснимо медленный».
    """

    code: str
    base: Preset
    scoped: dict[tuple[str, UUID], list] = field(default_factory=dict)
    # Язык, к которому свёрнуты подписи правил (T092). Хранится, потому что
    # досборка пресета для человека накладывает переопределения ЗАНОВО, и
    # многоязычная подпись партнёра осталась бы в нём словарём.
    language: str = ""

    def preset(self, *, group_id: UUID | None = None, employee_id: UUID | None = None) -> Preset:
        """Правила, действующие для конкретного человека.

        Если ни его группе, ни ему лично ничего не переопределяли, возвращается
        тот же объект общей части — это не оптимизация, а гарантия: без
        переопределений расчёт обязан быть побайтово прежним.
        """
        levels = [
            (level, self.scoped.get((level, scope_id), []))
            for level, scope_id in (("group", group_id), ("employee", employee_id))
            if scope_id is not None
        ]
        if not any(rows for _level, rows in levels):
            return self.base
        return build_preset(self.base, base=self.base.base,
                            levels=[("base", _replay_of(self.base)), *levels],
                            language=self.language or None)


def _replay_of(preset: Preset):
    """След уже собранной общей части, чтобы он не потерялся при досборке."""
    return [(path, _lookup(preset, path), where) for path, where in preset.origin.items()]


def _lookup(body: dict[str, Any], path: str) -> Any:
    node: Any = body
    for key in path.split("."):
        node = node[key]
    return node


def _in_force(queryset, on_date: date):
    """Версии, действующие на дату: valid_from <= дата < valid_to."""
    return queryset.filter(valid_from__lte=on_date).exclude(valid_to__lte=on_date)


def ui_language() -> str:
    """Язык, на котором сейчас рисуется страница.

    Подписи правил сворачиваются к нему (T092). Спрашивается здесь, а не
    передаётся вызывающим: читателей правил много (табель, сверка, расчёт,
    справочники), и забыть передать язык в одном из них означало бы получить
    ровно один экран, оставшийся русским, — то есть тот же дефект, только реже.
    """
    from django.utils.translation import get_language

    return get_language() or ""


def load_rules_at(tenant_id: UUID, country_code: str, on_date: date,
                  *, language: str | None = None) -> RuleSet:
    """Правила тенанта на дату: тело пресета страны плюс его переопределения.

    Подписи свёрнуты к языку страницы. Само правило от языка не зависит ничем:
    сворачиваются только `title`, ставки и условия остаются побайтово теми же —
    иначе расчёт зависел бы от того, на каком языке его запустили.
    """
    preset_row = (
        _in_force(RulePreset.objects.filter(country_code__iexact=country_code), on_date)
        .order_by("-valid_from")
        .first()
    )
    if preset_row is None:
        raise PresetNotFound(
            f"в базе нет правил расчёта для страны {country_code} на {on_date:%m.%Y}. "
            "Загрузите пресет страны: python manage.py load_presets"
        )

    rows = list(
        _in_force(RuleOverride.objects.filter(tenant_id=tenant_id), on_date)
        # Внутри одного уровня порядок не важен: пересечение периодов по одному
        # пути на одном уровне запрещено ограничением EXCLUDE. Сортировка нужна
        # только ради повторяемости следа расчёта.
        .order_by("valid_from", "path")
    )

    country_origin = Origin(level="country", version_id=preset_row.id,
                            valid_from=preset_row.valid_from)
    by_scope: dict[tuple[str, UUID], list] = {}
    levels = []
    for level in LEVELS:
        picked = [r for r in rows if r.scope_type == level]
        if level in ("country", "tenant"):
            # Общие для всего тенанта: своего объекта у них нет.
            levels.append((level, [_override_level(r) for r in picked]))
            continue
        for row in picked:
            if row.scope_id is None:
                # Уровень группы или человека без объекта — испорченная строка.
                # Пропустить её молча было бы худшим исходом: правило в базе
                # заведено, в списке видно, а расчёт идёт мимо него.
                raise ValueError(
                    f"переопределение {row.id} задано на уровне «{level}», но без "
                    f"объекта: правило '{row.path}' не к чему применить"
                )
            by_scope.setdefault((level, row.scope_id), []).append(_override_level(row))

    chosen = language if language is not None else ui_language()
    base = build_preset(preset_row.body, base=country_origin, levels=levels,
                        language=chosen or None)
    return RuleSet(code=preset_row.code, base=base, scoped=by_scope, language=chosen)


def _override_level(row: RuleOverride):
    return (row.path, row.value,
            Origin(level=row.scope_type, version_id=row.id, valid_from=row.valid_from))


def load_preset_at(tenant_id: UUID, country_code: str, on_date: date, *,
                   group_id: UUID | None = None, employee_id: UUID | None = None,
                   language: str | None = None) -> Preset:
    """Пресет, действующий у тенанта на дату (контракт блока `core`).

    Необязательные `group_id` и `employee_id` добавляют два последних слоя:
    без них возвращается общая часть — страна плюс партнёр.
    """
    rules = load_rules_at(tenant_id, country_code, on_date, language=language)
    return rules.preset(group_id=group_id, employee_id=employee_id)


def import_presets(codes: list[str] | None = None) -> list[str]:
    """Первичная загрузка стран из YAML в таблицу. Идемпотентна.

    Ключ — код пресета и дата начала действия: повторный прогон обновляет ту же
    строку, а не заводит вторую версию того же месяца (её всё равно не пустило
    бы ограничение непересечения периодов).
    """
    loaded = []
    for code in codes or list_presets():
        # Тело как в файле — со всеми языками подписей (T092). Свёрнутое здесь
        # оставило бы партнёра навсегда на одном языке: база и есть источник
        # правил после первичной загрузки, второго шанса положить языки нет.
        body = load_preset_body(code)
        RulePreset.objects.update_or_create(
            code=body.get("preset", code),
            valid_from=preset_valid_from(body),
            defaults={
                "title": body.get("title", code),
                "country_code": str(body.get("country", "")).upper(),
                "body": to_jsonable(dict(body)),
            },
        )
        loaded.append(code)
    return loaded
