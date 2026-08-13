"""Формат даты в тексте для человека — язык страницы, а не жёсткий русский (T103).

Дефект: рядом на одной и той же странице табеля стояли две даты одного вида,
отформатированные по-разному. `calculated_at` на странице периода рисуется
Django-ом самим (`{{ value }}` без фильтра) и следует языку страницы —
`Aug. 13, 2026, 3:10 p.m.` на английской. А `unit.closed_at` на табеле был
зашит фильтром `|date:"d.m.Y H:i"` — русский формат на любом языке. Такое же
было в `importer.py`: дата увольнения собиралась `f"{...:%d.%m.%Y}"` вручную.

Три проверки:

1. **Статическая.** Жёсткого русского формата даты (`d.m.Y` в шаблонном
   фильтре, `%d.%m.%Y` в Python) не должно быть в `src/` вовсе — иначе через
   месяц появится третье такое место, и никто не заметит.
2. **Табель на экране.** `unit.closed_at` рисуется по-настоящему через
   HTTP: на английской странице — не `13.08.2026`, а формат вида
   `Aug. 13, 2026, ...`; на русской — привычный русский вид (тот же, что уже
   рисует `calculated_at` на странице периода), не голая проверка байт.
3. **Отчёт импорта.** Дата увольнения в тексте предупреждения `dismissed`
   следует за активным языком так же, как и всё остальное сообщение.
"""
from __future__ import annotations

import re
from datetime import date

import psycopg
import pytest

from conftest import body, login_as, period_url

RUSSIAN_HARDCODED_DATE = re.compile(r'd\.m\.Y|%d\.%m\.%Y')

# Данные, показанные людям, не должны выглядеть как жёсткое «13.08.2026» —
# признак того, что кто-то забыл про локаль и написал формат руками.
DD_MM_YYYY = re.compile(r"\b\d{2}\.\d{2}\.\d{4}\b")


# =============================================================================
# 1. Статическая: жёсткого русского формата даты в src/ не осталось
# =============================================================================


def test_no_hardcoded_russian_date_format_in_src():
    """`d.m.Y` (шаблонный фильтр) и `%d.%m.%Y` (Python) — вне закона в src/.

    Каталог перевода (`src/locale/`) не проверяется: там лежат переведённые
    строки, а не код, который их собирает.
    """
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src"
    hits = []
    for path in root.rglob("*"):
        if path.is_dir() or "locale" in path.parts:
            continue
        if path.suffix not in (".py", ".html"):
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if RUSSIAN_HARDCODED_DATE.search(line):
                hits.append(f"{path.relative_to(root.parent)}:{lineno}: {line.strip()}")
    assert not hits, "жёсткий русский формат даты в коде, показываемом человеку:\n" + "\n".join(hits)


# =============================================================================
# 2. Табель на экране: closed_at следует за языком страницы
# =============================================================================


@pytest.fixture
def clean_closures(web_env):
    """Ни одного закрытия точки до теста и после него.

    База веб-тестов общая на весь прогон (см. `conftest.web_env`) — оставленное
    закрытие сломало бы соседние модули, которые правят те же часы того же
    тенанта. Уборка идёт владельцем базы, мимо политик — своя копия, а не
    импорт из `test_unit_closing.py`, чтобы не создавать связь между файлами.
    """
    def _wipe() -> None:
        with psycopg.connect(web_env, autocommit=True) as conn:
            conn.execute(
                "delete from timesheet_closures where tenant_id in "
                "(select id from tenants where code = 'rs-dev')"
            )

    _wipe()
    yield
    _wipe()


def _grid_url(client) -> str:
    html = body(client.get(period_url(client)))
    match = re.search(r'href="(/timesheets/[0-9a-f-]+/)"', html)
    assert match, "со страницы периода нет ссылки на табель"
    return match.group(1)


def _closable_unit(html: str) -> str:
    match = re.search(r'name="unit" value="([0-9a-f-]{36})"', html)
    assert match, f"на табеле нет формы закрытия точки:\n{html[:2000]}"
    return match.group(1)


def _closed_at_text(html: str) -> str:
    match = re.search(r'unit-state closed">(.*?)</span>', html)
    assert match, f"на табеле не нашлось закрытой точки:\n{html[:2000]}"
    return match.group(1)


def test_grid_closed_at_is_english_on_the_english_page(client, clean_closures):
    """Английская страница: не `13.08.2026`, а `Aug. 13, 2026, ...` — как рядом
    на странице периода показывает `calculated_at` без фильтра."""
    login_as(client, "manager")
    url = _grid_url(client)
    html = body(client.get(url))
    client.post(f"{url}close/", {"unit": _closable_unit(html)})

    client.cookies["django_language"] = "en"
    closed_text = _closed_at_text(body(client.get(url)))

    assert not DD_MM_YYYY.search(closed_text), (
        f"жёсткий русский формат даты на английской странице табеля: {closed_text!r}"
    )
    # DATETIME_FORMAT английской локали Django — "N j, Y, P", например
    # "Aug. 13, 2026, 3:10 p.m." (ровно то, что уже показывает `calculated_at`
    # на странице периода) — сверяем форму, а не точное время закрытия.
    assert re.search(r"[A-Za-z]{3}\.?\s+\d{1,2},\s+\d{4},\s+\d{1,2}:\d{2}\s*[ap]\.m\.", closed_text), (
        f"дата закрытия не похожа на английский DATETIME_FORMAT: {closed_text!r}"
    )


def test_grid_closed_at_stays_familiar_on_the_russian_page(client, clean_closures):
    """Русская страница: дата остаётся привычной — тем же видом, каким уже
    рисуется `calculated_at` на странице периода (полное русское имя месяца),
    а не байт-в-байт старым `d.m.Y H:i`."""
    login_as(client, "manager")
    url = _grid_url(client)
    html = body(client.get(url))
    client.post(f"{url}close/", {"unit": _closable_unit(html)})

    client.cookies["django_language"] = "ru"
    closed_text = _closed_at_text(body(client.get(url)))

    assert "часы закрыты" in closed_text
    # DATETIME_FORMAT русской локали Django — "j E Y г. G:i", например
    # "13 августа 2026 г. 15:10".
    assert re.search(r"\d{1,2}\s+[а-яё]+\s+\d{4}\s+г\.\s+\d{1,2}:\d{2}", closed_text), (
        f"русская дата закрытия не похожа на локализованный DATETIME_FORMAT: {closed_text!r}"
    )


# =============================================================================
# 3. Отчёт импорта: дата увольнения следует за языком отчёта
# =============================================================================


def _tenant_id():
    from core.models import Tenant

    return Tenant.objects.get(code="rs-dev").id


def _import_with_language(language: str):
    from conftest import PLATA_SAMPLE
    from django.utils import translation
    from timesheets.importer import import_partner_table

    with translation.override(language):
        with open(PLATA_SAMPLE, "rb") as handle:
            return import_partner_table(handle, tenant_id=_tenant_id(), period=date(2026, 6, 1))


def _dismissed_texts(result) -> list[str]:
    return [note.text for note in result.warnings if note.kind == "dismissed"]


def test_dismissed_note_date_is_english_on_the_english_report(period_restored):
    from core.models import Employee

    Employee.objects.filter(external_id="VUK MILOSEVIC").update(dismissed_at=date(2026, 5, 31))
    try:
        texts = _dismissed_texts(_import_with_language("en"))
    finally:
        Employee.objects.filter(external_id="VUK MILOSEVIC").update(dismissed_at=None)

    assert texts, "не нашлось предупреждения об увольнении в отчёте"
    text = texts[0]
    assert not DD_MM_YYYY.search(text), (
        f"жёсткий русский формат даты в английском отчёте импорта: {text!r}"
    )
    # DATE_FORMAT английской локали Django — "N j, Y", например "May 31, 2026".
    assert re.search(r"[A-Za-z]{3,}\.?\s+\d{1,2},\s+\d{4}", text), (
        f"дата увольнения не похожа на английский DATE_FORMAT: {text!r}"
    )


def test_dismissed_note_date_stays_familiar_on_the_russian_report(period_restored):
    from core.models import Employee

    Employee.objects.filter(external_id="VUK MILOSEVIC").update(dismissed_at=date(2026, 5, 31))
    try:
        texts = _dismissed_texts(_import_with_language("ru"))
    finally:
        Employee.objects.filter(external_id="VUK MILOSEVIC").update(dismissed_at=None)

    assert texts, "не нашлось предупреждения об увольнении в отчёте"
    text = texts[0]
    # DATE_FORMAT русской локали Django — "j E Y г.", например "31 мая 2026 г.".
    assert re.search(r"\d{1,2}\s+[а-яё]+\s+\d{4}\s+г\.", text), (
        f"русская дата увольнения не похожа на локализованный DATE_FORMAT: {text!r}"
    )
