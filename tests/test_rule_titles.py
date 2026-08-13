"""Подписи правил на трёх языках (T092).

Подпись вида часов — данные партнёра, а не строка интерфейса: состава видов код
не знает, и страна, заведённая завтра, добавит свой вид часов без правки кода.
Разбор — в журнале блока `web`. Отсюда следует, что переводит их не gettext, а
сами правила: `title` несёт либо одну строку, либо отображение «язык → текст».

Здесь проверяется обе половины этого решения:

* **свёртка** — что из многоязычной подписи на экран приезжает язык страницы, и
  что откат при отсутствии языка объявлен, а не случаен;
* **полнота** — что в пресете страны нет подписи, забытой на одном из языков
  продукта. Это и есть проверка, которая краснеет от непереведённой подписи
  колонки: колонки ведомости и табеля — это ровно эти подписи.

Тесты чистые: ни базы, ни Django. Свёртка живёт в `payroll`, потому что язык
приезжает в неё параметром — движок остаётся без ORM и без запроса.
"""
from __future__ import annotations

import re

import pytest

from payroll.presets import (
    DEFAULT_LANGUAGE,
    load_preset,
    load_preset_body,
    localize,
    preset_language,
)

# Языки продукта. Список продублирован из настроек намеренно: настройки — это
# Django, а этот файл чистый. Расхождение ловит `test_the_languages_here_are_the
# _languages_of_the_product` ниже, оно же не даёт списку протухнуть.
LANGUAGES = ["ru", "en", "sr-latn"]

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")

# Узлы пресета, чьи подписи попадают на экран колонкой ведомости, колонкой
# табеля, строкой следа или значением в справочнике. Перечислены здесь, а не
# обходом всего пресета: у пресета есть и служебные строки (`preset`, `country`),
# и требовать от них перевода бессмысленно.
TITLED_NODES = [
    "hour_types",
    "allowances",
    "work_measures",
    "schemes",
    "groups",
]

# Подписи-одиночки: у них нет узла со списком, они лежат прямо в корне.
TITLED_SINGLES = ["minimum_guarantee", "manual_correction"]


@pytest.fixture(scope="module")
def raw_serbia() -> dict:
    """Тело пресета как в файле — до свёртки. Многоязычные подписи целы."""
    return load_preset_body("serbia-2026")


# --- свёртка -----------------------------------------------------------------


def test_a_multilingual_title_collapses_to_the_language_asked_for():
    body = {"hour_types": {"regular": {
        "title": {"ru": "Отработанные", "en": "Worked hours", "sr-latn": "Radni sati"},
        "pay_percent": 1.0,
    }}}
    for language, expected in [
        ("ru", "Отработанные"), ("en", "Worked hours"), ("sr-latn", "Radni sati"),
    ]:
        collapsed = localize(body, language)
        assert collapsed["hour_types"]["regular"]["title"] == expected
        # Свернулась только подпись: ставку свёртка не трогает.
        assert collapsed["hour_types"]["regular"]["pay_percent"] == 1.0


def test_a_plain_title_stays_as_the_partner_wrote_it():
    """Партнёр на одном языке — прежняя запись, и она обязана работать как была.

    Иначе переход на многоязычные подписи потребовал бы переписать пресеты всех
    стран разом, а старое переопределение партнёра («title»: «Мои часы») стало
    бы показываться пустым.
    """
    body = {"hour_types": {"regular": {"title": "Мои часы"}}}
    for language in LANGUAGES:
        assert localize(body, language)["hour_types"]["regular"]["title"] == "Мои часы"


def test_a_missing_language_falls_back_to_the_language_of_the_preset():
    """Языка нет — берётся тот, на котором пресет написан, а не пустая строка.

    Пустая подпись на экране означала бы колонку без названия: читатель не
    поймёт, что за суммы под ней, и подумает, что сломался расчёт.
    """
    body = {
        "language": "sr-latn",
        "hour_types": {"regular": {"title": {"sr-latn": "Radni sati", "en": "Worked"}}},
    }
    assert localize(body, "ru")["hour_types"]["regular"]["title"] == "Radni sati"


def test_without_a_declared_language_the_fallback_is_the_source_language():
    """Пресет не объявил язык — откат к языку исходника продукта, а не к случайному."""
    body = {"hour_types": {"regular": {"title": {"ru": "Отработанные", "en": "Worked"}}}}
    assert preset_language(body) == DEFAULT_LANGUAGE
    assert localize(body, "sr-latn")["hour_types"]["regular"]["title"] == "Отработанные"


def test_an_empty_translation_is_not_a_translation():
    """Пустая строка в языке — это забытый перевод, а не ответ.

    Ловится здесь, потому что на экране пустая подпись и отсутствие подписи
    выглядят одинаково, а исправляются по-разному.
    """
    body = {"hour_types": {"regular": {"title": {"ru": "Отработанные", "en": "  "}}}}
    assert localize(body, "en")["hour_types"]["regular"]["title"] == "Отработанные"


def test_the_language_is_matched_regardless_of_how_it_is_written():
    """`sr_Latn`, `SR-LATN` и `sr-latn` — один язык.

    Django отдаёт код языка по-разному в разных местах (в каталоге —
    `sr_Latn`, в `get_language()` — `sr-latn`), и подпись не должна теряться на
    разнице в написании.
    """
    body = {"hour_types": {"regular": {"title": {"ru": "Ч", "sr_Latn": "Radni sati"}}}}
    assert localize(body, "sr-latn")["hour_types"]["regular"]["title"] == "Radni sati"


def test_collapsing_does_not_touch_anything_but_titles():
    """Свёртка ходит только по подписям: чужой словарь остаётся словарём.

    Значение правила вполне может быть отображением (например, календарь
    `calendar: {2026-06: {...}}`), и свернуть его к языку значило бы потерять
    правила расчёта.
    """
    body = {"calendar": {"2026-06": {"norm_hours": 176}}}
    assert localize(body, "en") == body


def test_the_default_load_gives_a_calculable_preset(raw_serbia):
    """`load_preset` отдаёт пресет, готовый к расчёту: подписи уже строки.

    Движок кладёт подпись в ведомость (`title=cfg["title"]`) и ничего не знает
    про языки. Многоязычная подпись, доехавшая до него, легла бы в базу
    словарём — и увидели бы это уже на экране.
    """
    assert isinstance(raw_serbia["hour_types"]["regular"]["title"], dict), (
        "в файле подпись обязана быть многоязычной, иначе проверка ниже пустая"
    )
    ready = load_preset("serbia-2026")
    assert isinstance(ready["hour_types"]["regular"]["title"], str)


# --- полнота -----------------------------------------------------------------


def titles_of(body: dict) -> dict[str, object]:
    """Все подписи пресета с путями до них — чтобы отказ называл виноватого."""
    found: dict[str, object] = {}
    for node in TITLED_NODES:
        for code, item in (body.get(node) or {}).items():
            if isinstance(item, dict) and "title" in item:
                found[f"{node}.{code}.title"] = item["title"]
    for node in TITLED_SINGLES:
        item = body.get(node)
        if isinstance(item, dict) and "title" in item:
            found[f"{node}.title"] = item["title"]
    return found


def test_the_serbia_preset_names_everything_in_every_language(raw_serbia):
    """Ни одной подписи, забытой на одном из языков продукта.

    Та самая проверка, которая краснеет от непереведённой колонки: колонки
    ведомости и табеля — это подписи `hour_types` и надбавок, и до этой задачи
    они были русскими на всех трёх языках.
    """
    missing = []
    for path, title in titles_of(raw_serbia).items():
        if not isinstance(title, dict):
            missing.append(f"{path}: подпись на одном языке — {title!r}")
            continue
        collapsed = {str(k).lower().replace("_", "-"): v for k, v in title.items()}
        for language in LANGUAGES:
            if not str(collapsed.get(language, "")).strip():
                missing.append(f"{path}: нет языка {language}")
    assert not missing, "подписи пресета Сербии не на всех языках:\n" + "\n".join(missing)


def test_no_column_of_a_translated_language_stays_russian(raw_serbia):
    """На английской и сербской подписи не остаётся кириллицы.

    Полноты мало: язык можно объявить и вписать в него русский текст — ровно так
    выглядела бы «починка» копированием. Кириллица в нерусской подписи — тот же
    признак непереведённого, на котором стоит проверка экранов.
    """
    russian = []
    for path, title in titles_of(raw_serbia).items():
        if not isinstance(title, dict):
            continue
        for language, value in title.items():
            code = str(language).lower().replace("_", "-")
            if code != "ru" and CYRILLIC.search(str(value)):
                russian.append(f"{path}[{code}]: {value!r}")
    assert not russian, "русский текст в нерусской подписи:\n" + "\n".join(russian)


def test_every_component_the_engine_can_produce_has_a_title(raw_serbia):
    """У каждого компонента, который умеет посчитать движок, есть своя подпись.

    Две подписи (`minimum_guarantee`, `manual_correction`) движок раньше держал
    зашитыми, и на экране они оставались русскими при любом языке. Проверка
    стоит здесь, чтобы следующая зашитая подпись не появилась молча.
    """
    for node in TITLED_SINGLES:
        assert isinstance(raw_serbia.get(node), dict) and raw_serbia[node].get("title"), (
            f"у компонента {node} нет подписи в правилах — движок подставит зашитую"
        )


def test_the_languages_here_are_the_languages_of_the_product():
    """Список языков в этом файле — тот же, что у продукта.

    Без этой сверки добавленный четвёртый язык не потребовал бы ни одной новой
    подписи: проверки выше остались бы зелёными, проверяя старые три.
    """
    django = pytest.importorskip("django.conf")
    try:
        product = [code for code, _name in django.settings.LANGUAGES]
    except Exception:  # noqa: BLE001 — настройки без окружения не поднимаются
        pytest.skip("настройки Django недоступны")
    assert sorted(product) == sorted(LANGUAGES)
