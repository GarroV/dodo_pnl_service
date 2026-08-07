"""Пресеты правил: тело расчёта и след того, откуда взялось каждое правило.

Пресет — это набор правил страны из коробки. Партнёр меняет только то, что у
него отличается. Новая страна = новый YAML, не новый код.

YAML-файлы остаются **источником первичной загрузки** страны: их читает команда
`manage.py load_presets`, которая кладёт тело в таблицу `rule_presets`. Дальше
источник ровно один — база (`core.rules.load_preset_at`), иначе правила
разъехались бы между файлом и настройками, и разъехались бы молча.

Здесь — только чистый Python: ни ORM, ни базы. Слои складываются функцией
`build_preset`, а кто именно положил значение, помнит `Preset.origin_of` — на
этом стоит след расчёта (D025).
"""
from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass
from functools import cache
from pathlib import Path
from typing import Any

import yaml

PRESETS_DIR = Path(__file__).parent / "presets"

# Уровни переопределения, от слабого к сильному. Порядок здесь и есть правило
# «каждый следующий уровень переопределяет предыдущий».
LEVELS = ("country", "tenant", "group", "employee")


@dataclass(frozen=True)
class Origin:
    """Откуда взялось значение правила: с какого уровня и из какой строки.

    `version_id` — идентификатор версии правила: строки `rule_presets` для тела
    пресета либо `rule_overrides` для переопределения. Пусто только у пресета,
    прочитанного прямо из файла (первичная загрузка и тесты движка).
    """

    level: str
    version_id: Any = None
    valid_from: dt.date | None = None


FILE_ORIGIN = Origin(level="country")


class Preset(dict):
    """Тело пресета плюс память о том, какой слой положил каждое правило.

    Наследуется от `dict` намеренно: движок принимает пресет как отображение и
    ничего не знает ни про слои, ни про базу — это его свойство менять нельзя.
    """

    def __init__(self, body: dict[str, Any], *, base: Origin = FILE_ORIGIN,
                 origin: dict[str, Origin] | None = None):
        super().__init__(body)
        self.base = base
        self.origin: dict[str, Origin] = dict(origin or {})

    def origin_of(self, path: str) -> Origin:
        """Кто задал значение по этому пути.

        Ищется самое длинное совпадение по префиксу: переопределили узел
        `hour_types.sick` целиком — значит, и `hour_types.sick.pay_percent`
        пришёл оттуда, а не из тела пресета.
        """
        parts = path.split(".")
        for cut in range(len(parts), 0, -1):
            found = self.origin.get(".".join(parts[:cut]))
            if found is not None:
                return found
        return self.base

    def __deepcopy__(self, memo):
        return Preset(copy.deepcopy(dict(self), memo), base=self.base, origin=dict(self.origin))


# --- чтение файлов -----------------------------------------------------------


@cache
def load_preset(code: str) -> Preset:
    path = PRESETS_DIR / f"{code}.yaml"
    if not path.exists():
        available = ", ".join(list_presets()) or "нет ни одного"
        raise FileNotFoundError(f"пресет '{code}' не найден. Доступны: {available}")
    return Preset(yaml.safe_load(path.read_text(encoding="utf-8")))


def list_presets() -> list[str]:
    return sorted(p.stem for p in PRESETS_DIR.glob("*.yaml"))


def preset_valid_from(preset: dict[str, Any]) -> dt.date:
    """Дата начала действия, объявленная самим пресетом.

    Из файла приезжает `date`, из базы — строка (в JSON дат нет), поэтому
    разбор в одном месте, а не у каждого читателя.
    """
    value = preset.get("valid_from")
    if isinstance(value, dt.date):
        return value
    return dt.date.fromisoformat(str(value))


def to_jsonable(value: Any) -> Any:
    """Тело пресета в том виде, в каком его принимает jsonb.

    Единственное, что не переживает JSON, — даты: YAML разбирает `valid_from`
    в `datetime.date`. Числа не трогаем: `repr` float в Python обратим, поэтому
    круговой рейс через JSON не двигает ни одной ставки.
    """
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, (dt.date, dt.datetime)):
        return value.isoformat()
    return value


# --- сборка слоёв ------------------------------------------------------------


def set_path(body: dict[str, Any], path: str, value: Any) -> None:
    """Записать значение по пути через точку: 'hour_types.night.pay_percent'."""
    node = body
    parts = path.split(".")
    for depth, key in enumerate(parts[:-1]):
        node = node.setdefault(key, {})
        if not isinstance(node, dict):
            raise ValueError(
                f"путь правила '{path}' упирается в значение на "
                f"'{'.'.join(parts[:depth + 1])}' — переопределять внутри нечего"
            )
    node[parts[-1]] = value


def apply_overrides(preset: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Наложить переопределения на пресет одним слоем, без следа происхождения.

    Короткий путь для тестов движка и разовых пересборок. Продуктовый путь —
    `build_preset`: он помнит, какой слой что положил.
    """
    result = copy.deepcopy(dict(preset))
    for path, value in overrides.items():
        set_path(result, path, value)
    return result


def build_preset(body: dict[str, Any], *, base: Origin = FILE_ORIGIN, levels=()) -> Preset:
    """Собрать пресет из тела и уровней переопределения.

    `levels` — последовательность `(level, [(path, value, origin), ...])` в
    порядке возрастания силы. Порядок задаёт вызывающий, а не сортировка внутри:
    он же отвечает за то, откуда взялись строки.
    """
    result = copy.deepcopy(to_jsonable(body))
    origin: dict[str, Origin] = {}
    for _level, rows in levels:
        for path, value, where in rows:
            set_path(result, path, to_jsonable(value))
            # Переопределение узла целиком отменяет след внутри него: значения
            # оттуда больше не действуют, и указывать на них — врать.
            for known in [k for k in origin if k.startswith(path + ".")]:
                del origin[known]
            origin[path] = where
    return Preset(result, base=base, origin=origin)

