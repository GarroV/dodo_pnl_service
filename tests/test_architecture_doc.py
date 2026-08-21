"""Инвентарь приложений в `docs/architecture.md` не отстал от репозитория (#134).

Архитектурная дока написана руками — и правильно: продуктовую рамку, сценарии и
глоссарий не сгенерировать. Но одна её таблица зеркалит код: список того, что
лежит в `src/`. Именно такие таблицы гниют первыми — приложение добавили, строку
забыли, и следующий читатель считает, что этого модуля нет.

Проверяется в обе стороны: каждый каталог из `src/` упомянут в доке, и каждый
упомянутый каталог существует. Второе не менее важно первого: дока, которая шлёт
в удалённый модуль, тратит время ровно так же.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOC = ROOT / "docs" / "architecture.md"
SRC = ROOT / "src"

# Не код продукта: словари перевода, метаданные пакета, кэш интерпретатора.
NOT_CODE = {"locale", "__pycache__"}


def _packages() -> set[str]:
    return {
        p.name for p in SRC.iterdir()
        if p.is_dir() and p.name not in NOT_CODE and not p.name.endswith(".egg-info")
    }


def test_the_architecture_doc_lists_every_package():
    text = DOC.read_text(encoding="utf-8")
    missing = sorted(name for name in _packages() if f"src/{name}/" not in text)
    assert not missing, (
        "в `src/` есть, в docs/architecture.md не упомянуто: "
        + ", ".join(missing)
        + "\nдобавьте строку в таблицу «Как собран код»"
    )


def test_the_architecture_doc_points_only_at_things_that_exist():
    import re

    text = DOC.read_text(encoding="utf-8")
    named = set(re.findall(r"`src/([a-z_]+)/", text))
    gone = sorted(name for name in named if not (SRC / name).is_dir())
    assert not gone, (
        "docs/architecture.md ссылается на каталоги, которых нет: " + ", ".join(gone)
    )
