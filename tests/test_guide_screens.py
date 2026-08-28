"""Гайд и съёмка его экранов не расходятся (правило владельца 27.08.2026).

Владелец: «если у нас происходят изменения либо в интерфейсе, либо в
функционале, который затронет интерфейс, нам необходимо гайд подтянуть и
изменить под актуальные изменения».

Правило держится на трёх вещах, и без любой из них разваливается:

* сам гайд — `docs/guides/first-month.html` (лежит в репозитории, а не только
  опубликованной страницей: иначе его нечем ни сверить, ни пересобрать);
* список экранов — `docs/guides/screens.json`, один на съёмку и на сборку;
* этот сторож.

Что он ловит. Добавили в гайд экран и забыли вписать его в список — пересъёмка
обойдёт его стороной, и в гайде навсегда останется одна устаревшая картинка,
которую никто не заметит. Обратное тоже: убрали шаг из гайда, а экран остался в
списке — снимаем то, что никому не нужно, и не понимаем, зачем.

Проверять это глазами нельзя: расхождение видно только тому, кто помнит оба
файла целиком.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
GUIDE = ROOT / "docs" / "guides" / "first-month.html"
SCREENS = ROOT / "docs" / "guides" / "screens.json"
SHOOTER = ROOT / "tools" / "guide_shots.mjs"

PLACEHOLDER = re.compile(r"\{\{SHOT:([0-9a-z-]+)\}\}")


def screens() -> dict[str, str]:
    """Список экранов без служебной строки с пояснением."""
    data = json.loads(SCREENS.read_text(encoding="utf-8"))
    return {name: url for name, url in data.items() if not name.startswith("_")}


def test_the_guide_is_in_the_repository():
    """Гайд живёт в репозитории, а не только опубликованной страницей."""
    assert GUIDE.exists(), (
        "исходника гайда нет — значит его нельзя ни сверить с продуктом, "
        "ни пересобрать в следующий раз"
    )
    assert PLACEHOLDER.search(GUIDE.read_text(encoding="utf-8")), (
        "в гайде нет ни одного снимка экрана — правило про пересъёмку теряет смысл"
    )


def test_every_screen_in_the_guide_is_in_the_list():
    """Экран показан в гайде — значит его снимут при пересъёмке."""
    used = set(PLACEHOLDER.findall(GUIDE.read_text(encoding="utf-8")))
    listed = set(screens())
    forgotten = sorted(used - listed)
    assert not forgotten, (
        f"эти экраны есть в гайде, но их не снимают: {forgotten}. "
        f"Добавьте их в {SCREENS.name} — иначе картинки протухнут молча"
    )


def test_the_list_holds_nothing_the_guide_does_not_show():
    """И наоборот: снимаем только то, что показываем."""
    used = set(PLACEHOLDER.findall(GUIDE.read_text(encoding="utf-8")))
    extra = sorted(set(screens()) - used)
    assert not extra, (
        f"эти экраны снимают, но гайд их не показывает: {extra}. "
        "Либо верните их в гайд, либо уберите из списка"
    )


def test_the_shooter_reads_the_same_list():
    """Съёмка берёт список из файла, а не держит свою копию.

    Вторая копия списка внутри скрипта — это ровно тот дубль, который
    разъезжается молча: гайд поправили, список поправили, а снимают по старому.
    """
    source = SHOOTER.read_text(encoding="utf-8")
    assert "screens.json" in source, "скрипт съёмки не читает общий список экранов"
    hardcoded = re.findall(r'\["[0-9]{2}-[a-z-]+",', source)
    assert not hardcoded, (
        f"в скрипте осталась своя копия списка экранов: {hardcoded[:3]}"
    )


PLACEHOLDER_IN_URL = re.compile(r"\{([a-z_]+)\}")


@pytest.mark.parametrize("name,url", sorted(screens().items()))
def test_every_screen_points_somewhere(name, url):
    """Адрес экрана — либо путь продукта, либо целиком подстановка."""
    assert url.startswith("/") or PLACEHOLDER_IN_URL.fullmatch(url), (
        f"{name}: непонятный адрес «{url}». Адрес — либо путь, начинающийся "
        f"с «/», либо подстановка целиком, вида {{invoice}}"
    )


def test_the_shooter_knows_every_substitution():
    """Подстановку в адресе экрана скрипт съёмки обязан уметь разрешать.

    Иначе `{trace}` уедет в браузер как есть, страница не откроется, и в гайде
    окажется дыра — а узнают о ней на сборке, в лучшем случае. Проверка дешёвая
    и ловит ровно то, что забывают: экран в список вписали, а научить съёмку
    искать его адрес — нет.
    """
    source = SHOOTER.read_text(encoding="utf-8")
    used = {
        name
        for url in screens().values()
        for name in PLACEHOLDER_IN_URL.findall(url)
    }
    unknown = sorted(name for name in used if "{" + name + "}" not in source)
    assert not unknown, (
        f"эти подстановки есть в {SCREENS.name}, но {SHOOTER.name} их не знает: "
        f"{unknown}"
    )


# --- гайд называет разделы так же, как их называет продукт ------------------


def test_the_guide_calls_the_sections_the_way_the_product_calls_them():
    """Название раздела в гайде — то же, что в шапке продукта.

    Куплено разрывом: разделы переименовали по словарю эталона (issue #162,
    было «Периоды · Расходы · Счета», стало «Табель · Наличные расходы · Инбокс
    документов»), а гайд остался со старыми именами. Человек читал про «Счета»,
    искал их в шапке и не находил — и решал, что сломался он.

    Проверка идёт от кода к тексту: список разделов берётся из `web.navigation`,
    то есть из того же места, откуда собирается шапка. Переименуют раздел —
    покраснеет здесь, а не у читателя.
    """
    text = GUIDE.read_text(encoding="utf-8")
    from web.navigation import GROUPS

    missing = [
        str(item.title)
        for group in GROUPS
        for item in group.items
        if str(item.title) not in text
    ]
    assert not missing, (
        f"гайд не называет разделы продукта: {missing}. "
        "Либо раздел переименовали и гайд отстал, либо гайд про него молчит"
    )
