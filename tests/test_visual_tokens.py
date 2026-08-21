"""Дизайн-система: значения доехали, рисовать их есть чем, палитра одна (T176, T177).

Зачем эти проверки существуют. Владелец 2026-08-21: «визуал тоже важен, потому
что в текущем варианте я даже протестить не могу». Причина была не во вкусе, а в
устройстве: 336 строк оформления лежали инлайном в `base.html`, и каждая очередь
дописывала туда свои цвета. Две палитры в одном продукте — это и есть «интерфейс
вразнобой»: одно и то же серое в двух местах разное, и глаз перестаёт верить
экрану.

Поэтому здесь три разных вопроса, и ни один не проверяет другой.

**Все ли значения доехали.** `tokens.css` — копия эталона, и потеря половины
блока при переносе выглядела бы как «часть экранов чуть другого оттенка», а не
как поломка. Числа записаны здесь руками: считать их из эталона нельзя — папка
эталона лежит вне git (issue #136), и на чистой копии репозитория такой тест
проверял бы сам себя.

**Есть ли чем рисовать.** Шрифт, которого нет на диске, не ломает страницу — она
открывается системным начертанием, с другими метриками и «почти той же»
вёрсткой. Худший вид поломки: заметить её можно только глазом и только рядом с
эталоном.

**Одна ли палитра.** Литеральный цвет в разметке или во втором листе стилей
возвращает исходную беду, поэтому он запрещён везде, кроме самого `tokens.css`.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
TOKENS = SRC / "web" / "static" / "web" / "tokens.css"

# Сколько переменных объявляет эталон: 115 в светлой теме и 61 переопределение в
# тёмной. Числа проверены разбором эталонного файла на момент переноса (T176).
ROOT_TOKENS = 115
DARK_TOKENS = 61

COMMENT = re.compile(r"/\*.*?\*/", re.S)
DECLARED = re.compile(r"(--[a-z0-9-]+)\s*:")


def _block(css: str, selector: str) -> str:
    """Тело правила. Комментарии срезаны: в них тоже встречаются «--что-то:»."""
    match = re.search(re.escape(selector) + r"\s*\{(.*?)\n\}", COMMENT.sub("", css), re.S)
    assert match, f"в tokens.css нет блока {selector}"
    return match.group(1)


def test_all_token_values_arrived_from_the_reference():
    css = TOKENS.read_text(encoding="utf-8")
    light = set(DECLARED.findall(_block(css, ":root")))
    dark = set(DECLARED.findall(_block(css, '[data-theme="dark"]')))

    assert len(light) == ROOT_TOKENS, (
        f"в :root {len(light)} переменных вместо {ROOT_TOKENS} — при переносе "
        "потерялся блок эталона"
    )
    assert len(dark) == DARK_TOKENS, (
        f"в тёмной теме {len(dark)} переопределений вместо {DARK_TOKENS}"
    )
    # Тёмная тема только переопределяет значения. Своё имя в ней — это токен,
    # которого нет в светлой: компонент, читающий его, на светлой теме останется
    # без значения вовсе, и заметит это только тот, кто выключил тёмную.
    assert not dark - light, f"тёмная тема заводит свои имена: {sorted(dark - light)}"


def test_fonts_are_local_and_present():
    """Локальные `.woff2` на диске есть, и внешних загрузок в них нет."""
    css = TOKENS.read_text(encoding="utf-8")
    urls = re.findall(r"url\('([^']+)'\)", css)
    assert urls, "в tokens.css нет ни одного @font-face — шрифты откуда-то извне?"
    for url in urls:
        assert not url.startswith(("http", "//")), (
            f"{url}: внешняя загрузка в проде запрещена, шрифт кладётся рядом"
        )
        assert (TOKENS.parent / url).exists(), f"{url}: файла нет, продукт уедет на системный шрифт"


# --- Одна палитра на продукт --------------------------------------------------

# Цвет литералом: #abc, #aabbcc, rgb(...), rgba(...). Ищется в файлах, где его
# быть не должно вовсе.
LITERAL_COLOR = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(")

# Где смотрим. Демо не здесь намеренно: его титульная страница не наследует
# шаблон продукта и несёт свои стили внутри себя по своей причине (язык и
# независимость от входа) — это чужой блок и отдельный разговор.
WATCHED = [
    ("шаблоны продукта", SRC / "web" / "templates", "*.html"),
    ("лист оформления", SRC / "web" / "static", "*.css"),
    ("лист табеля", SRC / "timesheets" / "static", "*.css"),
]


def _files():
    for label, root, pattern in WATCHED:
        for path in sorted(root.rglob(pattern)):
            if path == TOKENS:  # единственный дом значений
                continue
            yield label, path


@pytest.mark.parametrize("label,path", list(_files()), ids=lambda v: getattr(v, "name", v))
def test_no_literal_colours_outside_tokens(label, path):
    text = path.read_text(encoding="utf-8")
    # Комментарии не красят ничего: в них цвет иногда цитируют, объясняя, откуда
    # он взялся. Срезаются оба вида — CSS и HTML.
    text = COMMENT.sub("", text)
    text = re.sub(r"<!--.*?-->", "", text, flags=re.S)
    text = re.sub(r"{%\s*comment\s*%}.*?{%\s*endcomment\s*%}", "", text, flags=re.S)
    found = LITERAL_COLOR.findall(text)
    assert not found, (
        f"{label} {path.name}: цвет литералом {sorted(set(found))} — "
        "значения живут только в tokens.css, иначе палитр снова станет две"
    )
