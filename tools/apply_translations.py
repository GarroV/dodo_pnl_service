"""Разложить переводы по каталогам `.po` (T017).

Зачем скрипт, а не правка каталога руками. Строк перевода больше трёхсот, и
языков два: правка руками неизбежно расходится с каталогом — где-то потеряется
плейсхолдер, где-то `msgid` перенабран и перестал совпадать. Скрипт сшивает по
точному ключу и **отказывается** класть перевод, у которого набор плейсхолдеров
не тот же, что у оригинала: подставленный не туда `%(count)s` — это не опечатка,
а падение страницы у человека.

Вход — файлы вида `{"русская строка": {"en": "...", "sr": "..."}}`.
Выход — `src/locale/en/LC_MESSAGES/django.po` и `.../sr_Latn/...`.

    python tools/apply_translations.py перевод1.json перевод2.json ...

Каталоги перед этим должны быть собраны `manage.py makemessages`: скрипт только
заполняет `msgstr`, но не заводит записей — иначе каталог разошёлся бы с кодом.
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOGS = {"en": ROOT / "src/locale/en", "sr": ROOT / "src/locale/sr_Latn"}

PLACEHOLDER = re.compile(r"%\([^)]+\)s")
VARIABLE = re.compile(r"\{\{\s*[\w.]+\s*\}\}")


def quote(value: str) -> str:
    """Строка в виде, который понимает формат `.po`."""
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    )
    return f'"{escaped}"'


def unquote(lines: list[str]) -> str:
    return "".join(json.loads(line) for line in lines)


def check(source: str, translated: str) -> list[str]:
    """Что обязано совпасть у перевода с оригиналом.

    Не «похоже ли», а ровно два множества, потерять которые — сломать страницу:
    именованные подстановки и переменные шаблона. Порядок при этом свободен: он
    у каждого языка свой, ради чего подстановки и делались именованными.
    """
    problems = []
    if sorted(PLACEHOLDER.findall(source)) != sorted(PLACEHOLDER.findall(translated)):
        problems.append("разошлись подстановки %(...)s")
    if sorted(VARIABLE.findall(source)) != sorted(VARIABLE.findall(translated)):
        problems.append("разошлись переменные {{ ... }}")
    return problems


def apply(po: Path, table: dict[str, str]) -> tuple[int, list[str]]:
    lines = po.read_text(encoding="utf-8").splitlines()
    out: list[str] = []
    problems: list[str] = []
    filled = 0

    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("msgid "):
            out.append(line)
            index += 1
            continue

        # Пометки, которые `makemessages` вешает на угаданный перевод:
        # `fuzzy` — «этот текст я взял у похожей строки, посмотри глазами», и
        # gettext такую строку не показывает **вовсе**; `#| msgid` — прежний
        # ключ, от которого текст достался. Прочие флаги (`python-format`)
        # трогать нельзя: ими gettext проверяет подстановки.
        #
        # Снять их вправе только тот, кто заменил текст. Раньше они снимались
        # здесь, до того как выяснялось, есть ли для строки перевод, — и записи,
        # которой перевода не дали, доставался чужой угаданный текст уже без
        # пометки. С этой минуты каталог врал молча: `gettext` строку показывал,
        # а проверка «пустых и fuzzy нет» была зелёной, потому что она требует
        # снять пометку, а не исправить перевод. Именно так на главный экран
        # вышло `Payroll calculation: not calculated yet` над полной ведомостью
        # (T102). Поэтому пометки только откладываются в сторону, а вернуть их
        # или выбросить решает ветка ниже.
        def guessed(line: str) -> bool:
            return line.startswith("#|") or (line.startswith("#,") and "fuzzy" in line)

        markers: list[str] = []
        while out and guessed(out[-1]):
            markers.insert(0, out.pop())
        marker_at = len(out)

        # Оригинал: строка `msgid` и продолжения под ней.
        raw = [line[len("msgid "):].strip()]
        index += 1
        while index < len(lines) and lines[index].startswith('"'):
            raw.append(lines[index].strip())
            index += 1
        source = unquote(raw)

        out.append("msgid " + raw[0])
        out.extend(raw[1:])

        # Перевод: пропускаем прежний `msgstr` целиком и пишем свой.
        old = []
        if index < len(lines) and lines[index].startswith("msgstr"):
            old.append(lines[index])
            index += 1
            while index < len(lines) and lines[index].startswith('"'):
                old.append(lines[index])
                index += 1

        # Позиция и список пометок связываются значением, а не замыканием:
        # иначе обе вложенные функции читали бы состояние ПОСЛЕДНЕЙ итерации
        # цикла, а вызываются они внутри своей (ruff B023).
        def keep_markers(out=out, marker_at=marker_at, markers=markers) -> None:
            """Вернуть пометки на место: текст остался чужим, признать его своим
            нельзя. Из `#, fuzzy, python-format` при этом сохраняется всё."""
            out[marker_at:marker_at] = markers

        def drop_markers(out=out, marker_at=marker_at, markers=markers) -> None:
            """Текст заменён — пометки уходят. `#| msgid` выбрасывается целиком:
            он про прежний ключ. Прочие флаги остаются, `fuzzy` из них вынут."""
            kept = []
            for marker in markers:
                if marker.startswith("#|"):
                    continue
                flags = [f.strip() for f in marker[2:].split(",") if f.strip() != "fuzzy"]
                if flags:
                    kept.append("#, " + ", ".join(flags))
            out[marker_at:marker_at] = kept

        known = table.get(source)
        if source and known:
            broken = check(source, known)
            if broken:
                problems.append(f"{'; '.join(broken)}: {source[:60]}")
                keep_markers()
                out.extend(old)
                continue
            drop_markers()
            out.append("msgstr " + quote(known))
            filled += 1
        else:
            keep_markers()
            out.extend(old)

    po.write_text("\n".join(out) + "\n", encoding="utf-8")
    return filled, problems


def main(argv: list[str]) -> int:
    table: dict[str, dict[str, str]] = {}
    for name in argv:
        part = json.loads(Path(name).read_text(encoding="utf-8"))
        for key, value in part.items():
            if key in table and table[key] != value:
                print(f"строка переведена дважды и по-разному: {key[:60]}")
            table[key] = value

    failed = False
    for language, folder in CATALOGS.items():
        po = folder / "LC_MESSAGES" / "django.po"
        if not po.exists():
            print(f"нет каталога {po} — соберите makemessages")
            return 2
        column = {key: value[language] for key, value in table.items() if value.get(language)}
        filled, problems = apply(po, column)
        print(f"{language}: заполнено {filled} из {len(column)}")
        for problem in problems:
            failed = True
            print(f"  ОТКАЗ {problem}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
