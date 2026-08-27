"""Карта продукта знает про каждый модуль дизайн-системы (решение владельца).

Владелец 27.08.2026: «сейчас эталон это дизайн-система + issues, доки форджа для
поддержки идут». Значит эталон — источник истины, и то, что в нём появилось,
обязано попасть в карту: иначе нарисованный модуль останется незамеченным, как
уже случилось с четырьмя.

История, ради которой сторож существует. Дизайн-система пришла в проект после
того, как стройка по спеке закончилась. Слияние прошло наполовину — блок
`visual` появился, а спека осталась про первый модуль, — и через неделю ревизия
нашла 63 расхождения, а две трети открытых задач оказались порождены этим
разрывом. Проверять такое глазами нельзя: расхождение видно только тому, кто
помнит и папку эталона, и карту целиком.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP = ROOT / "docs" / "product-map.md"
DESIGN = ROOT / "Дизайн-система Dodo P&L"


def pages() -> set[str]:
    """Страницы эталона — по именам файлов, без расширения.

    `Path.stem` снимает только последнее расширение, а у файлов их два
    (`.dc.html`), — поэтому имя чистится явно. Иначе сверка идёт по строкам с
    хвостом «.dc», которых в карте нет и быть не должно.
    """
    return {path.name.removesuffix(".dc.html") for path in DESIGN.glob("*.dc.html")}


def rows() -> str:
    return MAP.read_text(encoding="utf-8")


def test_the_map_exists():
    assert MAP.exists(), "карты продукта нет — связывать эталон с задачами нечем"
    assert DESIGN.exists(), (
        "папки дизайн-системы нет: эталон обязан лежать в репозитории, иначе "
        "сверить продукт с ним на чистой копии невозможно"
    )


def test_the_reference_folder_is_named_in_one_normal_form():
    """Имя папки эталона в git — в NFC, как во всём коде (issue #197).

    Куплено красным прогоном, который никто не заметил двое суток. В имени есть
    «й», и юникод записывает его двумя способами: одним символом (NFC) или «и»
    плюс значок (NFD). macOS показывает обе формы одинаково и открывает папку по
    любой, а Linux — нет. Папка попала в git в NFD, код искал её в NFC: на
    машине всё зелено, в CI «папки дизайн-системы нет».

    Сторож смотрит **в индекс git**, а не на диск: на macOS проверка диска
    пройдёт при любой форме и ничего не докажет.
    """
    import subprocess
    import unicodedata

    listing = subprocess.run(
        ["git", "ls-files", "-z"], capture_output=True, cwd=ROOT, check=True,
    ).stdout.split(b"\0")
    tops = {
        entry.decode().split("/")[0]
        for entry in listing
        if entry and b"Dodo P&L" in entry
    }
    assert tops, "эталона нет в индексе git"
    wrong = [name for name in tops if not unicodedata.is_normalized("NFC", name)]
    assert not wrong, (
        "имя папки эталона записано в git не в NFC: " + ", ".join(map(ascii, wrong))
        + " — на Linux код её не найдёт, а на macOS этого не видно"
    )


def test_every_page_of_the_reference_is_in_the_map():
    """Нарисовали модуль — карта обязана про него знать."""
    text = rows()
    missing = []
    for page in sorted(pages()):
        # В карте модули названы человеческим именем: «Модуль 7 — Банковская
        # выписка» против имени файла «Модуль 7 - Банковская выписка». Сверяем
        # по существенной части, а не по тире.
        import unicodedata

        marker = unicodedata.normalize("NFC", page.split(" - ")[-1].strip())
        if marker not in unicodedata.normalize("NFC", text):
            missing.append(page)
    assert not missing, (
        "эти модули дизайн-системы карта не видит: "
        f"{missing}. Впишите их в docs/product-map.md — иначе они останутся "
        "незамеченными, как уже случилось с четырьмя"
    )


def test_the_map_says_where_the_work_is():
    """У карты есть все три колонки, ради которых она заведена."""
    text = rows()
    for column in ("Состояние", "Задачи", "Контекст"):
        assert column in text, f"в карте нет колонки «{column}»"


def test_the_map_names_the_mandatory_check():
    """Правило сверки записано в самой карте, а не только в памяти.

    Решение владельца: сверяться каждый раз при взятии работы. Правило,
    записанное лишь в переписке, живёт до конца недели.
    """
    text = rows()
    assert "сверк" in text.lower() or "сверя" in text.lower()
    for source in ("эталон", "blocks", "decisions"):
        assert source in text.lower() or source in text, (
            f"в правиле сверки не назван источник «{source}»"
        )


def test_states_are_from_the_dictionary():
    """Состояние — одно из трёх слов, а не свободный текст.

    Свободные формулировки («почти готово», «в работе») не сравниваются между
    собой: карту читают, чтобы решить, за что браться, и «почти» этого вопроса
    не решает.
    """
    allowed = {"готово", "беднее", "нет", "справочная", "Состояние"}
    table = [line for line in rows().splitlines() if line.startswith("| ")]
    states = []
    for line in table:
        cells = [cell.strip().strip("*") for cell in line.split("|")[1:-1]]
        if len(cells) == 5 and not cells[0].startswith("---"):
            states.append(cells[2])
    unknown = sorted({state for state in states if state and state not in allowed})
    assert not unknown, f"в карте состояния не из словаря: {unknown}"
