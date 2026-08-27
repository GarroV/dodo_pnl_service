"""Собрать страницу гайда: подставить снимки экранов в исходник.

Внешние картинки в опубликованном артефакте не загружаются вовсе — строгая
политика безопасности пропускает только то, что лежит в самой странице.
Поэтому снимки встраиваются как `data:`-адреса, а не ссылками.

    python tools/guide_build.py docs/guides/first-month.html <папка-со-снимками> <куда-собрать>

Снимки берутся по именам из `docs/guides/screens.json` — тому же списку, по
которому их снимает `tools/guide_shots.mjs`. Не хватает снимка — сборка
отказывается, а не кладёт пустую картинку: страница с дырой выглядит готовой.
"""
from __future__ import annotations

import base64
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCREENS = ROOT / "docs" / "guides" / "screens.json"
PLACEHOLDER = re.compile(r"\{\{SHOT:([0-9a-z-]+)\}\}")


def main() -> int:
    if len(sys.argv) != 4:
        print(__doc__)
        return 2
    source, shots, target = (Path(arg) for arg in sys.argv[1:])
    known = {
        name for name in json.loads(SCREENS.read_text(encoding="utf-8"))
        if not name.startswith("_")
    }

    missing: list[str] = []

    def embed(match: re.Match) -> str:
        name = match.group(1)
        if name not in known:
            missing.append(f"{name}: нет в {SCREENS.name}")
            return ""
        picture = shots / f"{name}.jpg"
        if not picture.exists():
            missing.append(f"{name}: снимок не сделан")
            return ""
        return "data:image/jpeg;base64," + base64.b64encode(picture.read_bytes()).decode()

    built = PLACEHOLDER.sub(embed, source.read_text(encoding="utf-8"))
    if missing:
        # Отказ, а не страница с дырами: пустая картинка выглядит как готовая,
        # и заметят её не раньше, чем покажут человеку.
        print("Не собрано, не хватает снимков:")
        for line in missing:
            print(" ", line)
        return 1

    target.write_text(built, encoding="utf-8")
    size = target.stat().st_size / 1024 / 1024
    print(f"собрано: {target} ({size:.1f} МБ, снимков {len(PLACEHOLDER.findall(source.read_text(encoding='utf-8')))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
