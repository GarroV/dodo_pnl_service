"""Сторож именования: следы Supabase и сленг регистров учёта.

Два требования, которые легко нарушить обратно одной строкой и не заметить:

1. **Никакой привязки к Supabase** (T003). Платформа выбрана своя, `auth.uid()`
   и файлы `db/platform/*` удалены. Вернувшийся вызов `auth.uid()` не упадёт
   на локальной базе, где кто-то оставил шим, — упадёт на чистой.
2. **Регистры учёта называются нейтрально** (D009, T004): `official`,
   `supplementary`, `internal`, поле `ledger`. Сленг «белый/серый/чёрный» не
   должен торчать ни в открытом репозитории, ни в интерфейсе.

Проверка идёт по исходникам, а не по схеме: имя может вернуться в код задолго
до того, как кто-то накатит миграции.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Что смотрим: код, схема, шаблоны, правила стран.
SOURCE_DIRS = ["src", "db", "tests", "tools"]
SOURCE_SUFFIXES = {".py", ".sql", ".yaml", ".yml", ".html"}

# Слова сленга как отдельные слова. `white-space` в CSS и `blackbox` в тексте
# под это не попадают, а `layer` — попадает: колонка называется `ledger`.
SLANG = re.compile(r"\b(white|grey|black)\b|\blayers?\b", re.IGNORECASE)

# Исключения — там, где слово означает не регистр учёта.
ALLOWED = re.compile(
    r"white-space"              # CSS
    r"|white_space"
    r"|test_naming_hygiene\.py",  # сам сторож: слова-образцы записаны в нём
    re.IGNORECASE,
)


def source_files() -> list[Path]:
    files: list[Path] = []
    for name in SOURCE_DIRS:
        for path in (ROOT / name).rglob("*"):
            if path.suffix in SOURCE_SUFFIXES and path.is_file():
                files.append(path)
    assert files, "не нашлось ни одного исходного файла — проверка была бы фиктивной"
    return files


def hits(pattern: re.Pattern) -> list[str]:
    found = []
    for path in source_files():
        rel = path.relative_to(ROOT)
        if ALLOWED.search(str(rel)):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if ALLOWED.search(line):
                continue
            if pattern.search(line):
                found.append(f"{rel}:{number}: {line.strip()}")
    return found


def test_no_supabase_left_in_sources():
    """T003: привязки к Supabase нет ни в схеме, ни в коде."""
    assert hits(re.compile(r"supabase|auth\.uid\(\)", re.IGNORECASE)) == []


def test_no_platform_directory():
    """Файлы платформы удалены: функции контекста живут в миграции 0004_rls."""
    assert not (ROOT / "db" / "platform").exists()


def test_ledger_slang_is_gone():
    """D009/T004: регистры называются official/supplementary/internal, поле — ledger."""
    assert hits(SLANG) == []
