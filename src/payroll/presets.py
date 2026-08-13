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

# --- многоязычные подписи (T092) ---------------------------------------------
#
# Подпись правила — данные партнёра, а не строка интерфейса: состава видов часов
# код не знает, и страна, заведённая завтра, добавит свой вид обычным
# переопределением, а не релизом. Значит, переводит подписи не gettext, а сами
# правила: `title` несёт либо строку (партнёр на одном языке), либо отображение
# «язык → текст». Полный разбор — в журнале блока `web`, T092.
#
# Свёртка живёт здесь, а не на стороне Django, потому что подпись доезжает до
# движка (`title=cfg["title"]` в `engine.py`), а движок обязан оставаться чистым
# Python. Язык поэтому приходит параметром: ни `get_language()`, ни настроек тут
# нет и быть не может.
TITLE_KEY = "title"

# Ключи, которые называют вещь человеку и потому сворачиваются к его языку.
# Множество, а не один `title`: `pnl_line` — строка P&L, на которую ложатся
# расходы группы, — стоит в том же узле, приезжает из пресета СТРАНЫ и точно так
# же читается человеком. Пока свёртка знала только `title`, на английском и
# сербском экране правил рядом с `Unit managers` стояло `Расходы на управление`.
#
# Список закрытый, и это важно: свернуть к языку **любой** словарь нельзя —
# значение правила само бывает отображением (`calendar`, `allowance_prorate`), и
# свёртка потеряла бы расчёт. Новый называющий ключ вписывается сюда осознанно.
LOCALIZED_KEYS = frozenset({TITLE_KEY, "pnl_line"})

# Язык, на котором написан продукт и его пресеты, если пресет не сказал иного.
DEFAULT_LANGUAGE = "ru"


def language_key(code: str) -> str:
    """Код языка в одном написании: `sr_Latn`, `SR-LATN` и `sr-latn` — одно и то же.

    Django отдаёт код по-разному в разных местах (каталог переводов зовётся
    `sr_Latn`, `get_language()` возвращает `sr-latn`), и подпись не должна
    теряться на разнице в написании — потерялась бы она молча, пустой колонкой.
    """
    return str(code).strip().lower().replace("_", "-")


def preset_language(body: dict[str, Any]) -> str:
    """Язык, на котором написан пресет. Он же — запасной для всех подписей."""
    return language_key(body.get("language") or DEFAULT_LANGUAGE)


def pick_title(title: Any, language: str, fallback: str) -> Any:
    """Подпись на нужном языке. Пусто не возвращается никогда.

    Порядок отката объявлен, а не случаен: точный язык → язык самого пресета →
    первое непустое значение. Последнее — не «как повезёт», а «лучше чужой язык,
    чем колонка без названия»: пустая подпись читается как поломка расчёта.
    """
    if not isinstance(title, dict):
        return title
    by_code = {language_key(code): value for code, value in title.items()}
    for wanted in (language_key(language), fallback):
        value = by_code.get(wanted)
        # Пустая строка — это забытый перевод, а не ответ: на экране её не
        # отличить от отсутствующей подписи, а чинится она иначе.
        if isinstance(value, str) and value.strip():
            return value
    for value in by_code.values():
        if isinstance(value, str) and value.strip():
            return value
    return ""


def localize(body: dict[str, Any], language: str | None = None) -> dict[str, Any]:
    """Свернуть многоязычные подписи тела пресета к одному языку.

    Трогаются только ключи из `LOCALIZED_KEYS`. Остальные отображения остаются
    как есть, и это не мелочь: значение правила само бывает словарём
    (`calendar`, `allowance_prorate`), и свернуть его к языку значило бы
    потерять расчёт.
    """
    fallback = preset_language(body)
    wanted = language_key(language or fallback)

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: (
                    pick_title(value, wanted, fallback)
                    if key in LOCALIZED_KEYS
                    else walk(value)
                )
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [walk(item) for item in node]
        return node

    return walk(body)


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
def load_preset_body(code: str) -> Preset:
    """Тело пресета как в файле — с многоязычными подписями, без свёртки.

    Нужно ровно двум: первичной загрузке страны в базу (`core.rules.import_presets`
    кладёт в `rule_presets.body` все языки сразу — свёрнутое тело оставило бы
    партнёра навсегда на одном) и проверкам полноты подписей.
    """
    path = PRESETS_DIR / f"{code}.yaml"
    if not path.exists():
        available = ", ".join(list_presets()) or "нет ни одного"
        raise FileNotFoundError(f"пресет '{code}' не найден. Доступны: {available}")
    return Preset(yaml.safe_load(path.read_text(encoding="utf-8")))


@cache
def load_preset(code: str, language: str | None = None) -> Preset:
    """Пресет, готовый к расчёту: подписи уже свёрнуты к одному языку.

    Умолчание — язык самого пресета. Движок кладёт подпись в ведомость и про
    языки ничего не знает; многоязычная подпись, доехавшая до него, легла бы в
    базу словарём, и увидели бы это уже на экране.
    """
    body = load_preset_body(code)
    return Preset(localize(dict(body), language), base=body.base, origin=dict(body.origin))


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


def build_preset(body: dict[str, Any], *, base: Origin = FILE_ORIGIN, levels=(),
                 language: str | None = None) -> Preset:
    """Собрать пресет из тела и уровней переопределения.

    `levels` — последовательность `(level, [(path, value, origin), ...])` в
    порядке возрастания силы. Порядок задаёт вызывающий, а не сортировка внутри:
    он же отвечает за то, откуда взялись строки.

    `language` — язык, к которому сворачиваются подписи (T092). Свёртка идёт
    **после** наложения слоёв, а не до: переопределение партнёра тоже бывает
    многоязычным, и свёрнутое раньше времени тело потеряло бы его языки.
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
    return Preset(localize(result, language), base=base, origin=origin)

