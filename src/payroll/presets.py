"""Загрузка пресетов правил.

Пресет — это набор правил страны из коробки. Партнёр меняет только то,
что у него отличается. Новая страна = новый YAML, не новый код.
"""
from __future__ import annotations

import copy
from functools import cache
from pathlib import Path
from typing import Any

import yaml

PRESETS_DIR = Path(__file__).parent / "presets"


@cache
def load_preset(code: str) -> dict[str, Any]:
    path = PRESETS_DIR / f"{code}.yaml"
    if not path.exists():
        available = ", ".join(list_presets()) or "нет ни одного"
        raise FileNotFoundError(f"пресет '{code}' не найден. Доступны: {available}")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def list_presets() -> list[str]:
    return sorted(p.stem for p in PRESETS_DIR.glob("*.yaml"))


def apply_overrides(preset: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """
    Накладывает переопределения на пресет.

    Ключ — путь через точку: 'hour_types.night.pay_percent'. Так переопределения
    хранятся в базе (таблица rule_overrides) и складываются регистрами:
    страна → партнёр → группа → сотрудник.
    """
    result = copy.deepcopy(preset)
    for path, value in overrides.items():
        node = result
        parts = path.split(".")
        for key in parts[:-1]:
            node = node.setdefault(key, {})
        node[parts[-1]] = value
    return result
