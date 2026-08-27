"""Собрать всё, что известно про модуль или блок, — перед тем как брать в работу.

Решение владельца 27.08.2026: «каждый раз, когда что-то берёшь в работу, когда
новый блок, обязательно сверяешься и смотришь, где, как». Сверка, которую нужно
делать памятью, не делается: источников три, и лежат они в разных местах.

    python tools/context.py табель
    python tools/context.py suppliers
    python tools/context.py "Наличные расходы"

Показывает три вещи и ничего не придумывает:

1. **строку карты продукта** — состояние и задачи;
2. **что говорит эталон** — заголовки и подписи модуля дизайн-системы;
3. **что говорят документы Forge** — журнал блока и решения, где упомянута тема.

Эталон — источник истины о том, что должно быть на экране; журнал блока — как
это себя ведёт и что уже пробовали; решения — почему сделано так.
"""
from __future__ import annotations

import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP = ROOT / "docs" / "product-map.md"
DESIGN = ROOT / "Дизайн-система Dodo P&L"
BLOCKS = ROOT / "docs" / "forge" / "blocks"
DECISIONS = ROOT / "docs" / "forge" / "decisions.md"

LIMIT = 40


def nfc(text: str) -> str:
    """Имена файлов на macOS хранятся в другой форме юникода, чем текст в них."""
    return unicodedata.normalize("NFC", text)


def visible(html: str) -> list[str]:
    """Видимые надписи страницы эталона: заголовки, подписи, кнопки."""
    html = re.sub(r"<style.*?</style>|<script.*?</script>", " ", html, flags=re.S)
    html = re.sub(r"<[^>]+>", "\n", html)
    lines = [" ".join(line.split()) for line in html.splitlines()]
    return [line for line in lines if len(line) > 12 and not line.startswith("{{")]


def show(title: str, lines: list[str]) -> None:
    print(f"\n{'─' * 72}\n{title}\n{'─' * 72}")
    if not lines:
        print("  (ничего не нашлось)")
        return
    for line in lines[:LIMIT]:
        print(" ", line)
    if len(lines) > LIMIT:
        print(f"  … ещё {len(lines) - LIMIT} строк")


def main(query: str) -> int:
    needle = nfc(query).casefold()

    rows = [
        line for line in MAP.read_text(encoding="utf-8").splitlines()
        if line.startswith("| ") and needle in nfc(line).casefold()
    ]
    show(f"КАРТА ПРОДУКТА — состояние и задачи по запросу «{query}»", rows)

    pages = [
        path for path in DESIGN.glob("*.dc.html")
        if needle in nfc(path.name).casefold()
    ]
    for page in pages:
        show(
            f"ЭТАЛОН — {nfc(page.name).removesuffix('.dc.html')}: что должно быть на экране",
            visible(page.read_text(encoding="utf-8")),
        )
    if not pages:
        show("ЭТАЛОН", [
            "модуль дизайн-системы по этому запросу не нашёлся — проверьте имя "
            "в docs/product-map.md",
        ])

    for block in sorted(BLOCKS.glob("*.md")):
        if needle not in nfc(block.stem).casefold():
            continue
        head = block.read_text(encoding="utf-8").splitlines()
        show(f"ЖУРНАЛ БЛОКА — {block.name}: как себя ведёт и что пробовали",
             [line for line in head if line.strip()][:LIMIT])

    decisions = [
        line for line in DECISIONS.read_text(encoding="utf-8").splitlines()
        if line.startswith("| D") and needle in nfc(line).casefold()
    ]
    show("РЕШЕНИЯ — почему сделано так", [line[:300] for line in decisions])
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1]))
