"""Каждый класс в разметке где-то описан (T168, детектор дрейфа).

Зачем. Перенос дизайн-системы начался с того, что у половины экранов классы в
шаблонах были, а правил для них не было ни одного: `.rights`, `.right`,
`.chip`, `.frozen`, `.lifecycle`, `.retro-lines` — разметка размечена, а на
экране голый HTML. Заметить это можно было только глазами и только открыв
нужный экран нужной ролью.

Сторож ловит ровно этот случай: класс поставлен, а нарисовать его нечем. Он не
проверяет, что оформление красивое, — только что оно вообще существует.

Что считается описанием: правило в общем листе продукта, в листе табеля или в
собственном `<style>` того же шаблона (титульная демо и страница «стенд не
поднят» намеренно автономны — они показываются, когда продукта может не быть).

Белый список — для классов, которые оформления не требуют по своей природе:
служебные крючки htmx и разметочные роли, на которые опирается JS или тесты.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = sorted(ROOT.glob("src/*/templates/**/*.html"))
STYLESHEETS = (
    ROOT / "src/web/static/web/app.css",
    ROOT / "src/web/static/web/tokens.css",
    # Печатные формы (T187) идут своим листом и намеренно не тянут `app.css`: на
    # бумаге нет ни шапки продукта, ни кнопок, ни прокрутки. Сторож обязан о нём
    # знать, иначе он объявил бы «голым HTML» всю печатную разметку сразу.
    ROOT / "src/web/static/web/print.css",
    ROOT / "src/timesheets/static/timesheets/grid.css",
)

# Классы без оформления по замыслу: крючки для htmx и для проверок. Каждый
# новый пункт здесь — это заявление «оформлять нечего», и он должен быть
# правдой, а не способом погасить красный тест.
NO_STYLE_NEEDED = {
    "htmx-indicator",   # состояние запроса, показывает сам htmx
    "sr-only",          # видно только диктору
    "js",               # крючок скриптов
}


def styles() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in STYLESHEETS if p.exists())


def classes_of(html: str) -> set[str]:
    found: set[str] = set()
    for chunk in re.findall(r'class="([^"]*)"', html):
        if "{" in chunk:            # значение собирается шаблоном — не наш случай
            continue
        found.update(chunk.split())
    return found


def test_every_class_in_the_markup_has_somewhere_to_come_from():
    common = styles()
    orphans: list[str] = []

    for template in TEMPLATES:
        html = template.read_text(encoding="utf-8", errors="ignore")
        # Собственный лист шаблона — тоже описание: автономные страницы
        # (титульная демо, «стенд не поднят») несут стили в себе.
        own = "\n".join(re.findall(r"<style>(.*?)</style>", html, re.S))
        described = common + own
        for name in sorted(classes_of(html)):
            if name in NO_STYLE_NEEDED:
                continue
            if re.search(rf"\.{re.escape(name)}\b", described):
                continue
            orphans.append(f"{template.relative_to(ROOT)}: .{name}")

    assert not orphans, (
        "классы стоят в разметке, а правил для них нет — на экране это голый HTML:\n  "
        + "\n  ".join(orphans)
    )
