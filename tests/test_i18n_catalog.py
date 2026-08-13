"""Правильность перевода, а не его наличие (T102).

Проверки в `tests/test_i18n.py` отвечают на вопрос «перевод есть?»: строка не
пустая, не помечена `fuzzy`, каталог собран и покрывает исходники. Все они были
зелёными в тот день, когда на главном экране под словами `Payroll calculation:
not calculated yet` стояла полная ведомость на 47 строк. Потому что перевод там
был. Просто не свой.

Как это получилось. `makemessages`, встретив новую строку, похожую на старую,
переносит на неё **чужой перевод** и вешает пометку `fuzzy` с подсказкой
`#| msgid "прежняя строка"`. Пометка означает ровно одно: «угадано, посмотри
глазами». `gettext` строку с ней не показывает вовсе. Дальше пометку сняли — и
угаданный перевод молча вышел на экран, а проверка «пустых и `fuzzy` нет»
позеленела: она требует **снять пометку**, а не исправить текст.

Отсюда три проверки, каждая на свой признак угаданного перевода:

1. **Один перевод у двух разных оригиналов.** Угаданный перевод — всегда чужой,
   то есть уже занятый другой строкой. Совпадения бывают и законные (русский
   различает род там, где английский не различает), поэтому они перечислены
   поимённо: список коротких групп читается, список исключений «по подстроке» —
   нет.
2. **Перевод, который когда-то угадали и с тех пор не трогали.** Прямой ответ на
   «`fuzzy` сняли, текст не поправили»: история каталога знает, что и на что
   угадал `makemessages`, и если сегодня там стоит ровно та же строка — её никто
   не смотрел. Угадать верно тоже можно, поэтому подтверждённые совпадения
   перечислены с обоснованием.
3. **Инструмент раскладки не снимает пометку с чужой строки.** Пометку снимал
   `tools/apply_translations.py` — у **всех** записей подряд, включая те, для
   которых перевода ему не дали. Так угаданный текст и остался без пометки.
"""
from __future__ import annotations

import collections
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from test_i18n import ROOT, TRANSLATED, read_po, catalog

# --- 1. один перевод у двух разных оригиналов --------------------------------

# Законные совпадения: разные русские строки, которым в языке перевода
# соответствует одно и то же слово. Каждая группа — с причиной; без причины
# группе здесь не место, иначе список превратится в место, куда прячут дефект.
SHARED_ON_PURPOSE: list[tuple[frozenset[str], str]] = [
    (
        frozenset({"Закрыт", "Закрыта"}),
        "род русского прилагательного: период закрыт, точка закрыта — "
        "в английском и сербском форма одна",
    ),
    (
        frozenset({"Открыт", "Открыта"}),
        "то же самое про открытие; в английском формы всё-таки разные "
        "(Open — состояние периода, Opened — дата открытия точки)",
    ),
    (
        frozenset({"не задана", "не задано"}),
        "род русского причастия при разных подлежащих (ставка не задана, "
        "правило не задано)",
    ),
    (
        frozenset({"Регистр", "Регистр учёта"}),
        "краткая и полная форма одного термина: на узкой колонке «Регистр», "
        "в заголовке — «Регистр учёта»",
    ),
    (
        frozenset({"Величина", "Значение"}),
        "два русских слова для одного понятия: шапка следа расчёта и шапка "
        "таблицы правил",
    ),
    (
        frozenset({"переопределение по сотруднику", "переопределение по человеку"}),
        "один и тот же уровень правила назван по-русски дважды: в следе расчёта "
        "(web/views.py) и на экране правил (web/rules.py). Переводу делить "
        "нечего — это одно понятие; расходится русский исходник, см. issue",
    ),
    (
        frozenset({"Сотрудник", "Сотрудники"}),
        "единственное и множественное; в сербском у «zaposleni» эти формы "
        "совпадают, в английском — нет",
    ),
]


def shared_group(msgid: str) -> frozenset[str] | None:
    for group, _reason in SHARED_ON_PURPOSE:
        if msgid in group:
            return group
    return None


def collisions(language: str) -> dict[str, list[str]]:
    """Переводы, отданные больше чем одному оригиналу, кроме законных групп."""
    by_translation = collections.defaultdict(list)
    for item in catalog(language):
        by_translation[item["msgstr"]].append(item["msgid"])

    found = {}
    for translation, sources in by_translation.items():
        if len(sources) < 2:
            continue
        group = shared_group(sources[0])
        if group is not None and set(sources) <= group:
            continue
        found[translation] = sorted(sources)
    return found


@pytest.mark.parametrize("language", sorted(TRANSLATED))
def test_one_translation_is_not_given_to_two_originals(language):
    """Чужой перевод виден по тому, что он уже занят другой строкой.

    Это и есть след угадывания: `makemessages` не сочиняет текст, он **берёт
    его у соседа**. Поэтому «одна строка перевода на два разных оригинала» —
    почти всегда ровно тот случай, когда один из двух экранов врёт.
    """
    found = collisions(language)
    report = "\n".join(
        f"  {translation!r} отдан оригиналам: "
        + ", ".join(repr(source) for source in sources)
        for translation, sources in sorted(found.items())
    )
    assert not found, (
        f"{language}: один перевод у разных оригиналов ({len(found)}). "
        "Либо перевод чужой — поправьте его; либо совпадение законное — "
        "впишите группу в SHARED_ON_PURPOSE с причиной:\n" + report
    )


def test_every_declared_shared_group_really_collides():
    """Группа-исключение, которая ничего не разрешает, — мусор.

    Список законных совпадений опасен ровно тем, что растёт: сегодня в нём
    разрешают настоящее совпадение, завтра оно исчезает вместе с правкой, а
    запись остаётся и прикрывает будущий дефект. Поэтому каждая группа обязана
    быть живой хотя бы на одном языке.
    """
    live = set()
    for language in TRANSLATED:
        by_translation = collections.defaultdict(set)
        for item in catalog(language):
            by_translation[item["msgstr"]].add(item["msgid"])
        for sources in by_translation.values():
            if len(sources) < 2:
                continue
            for group, _reason in SHARED_ON_PURPOSE:
                if sources <= group:
                    live.add(group)
    dead = [sorted(group) for group, _reason in SHARED_ON_PURPOSE if group not in live]
    assert not dead, (
        "в SHARED_ON_PURPOSE есть группы, которые ни на одном языке уже не "
        "совпадают — уберите их, иначе они прикроют будущее совпадение:\n"
        + "\n".join(", ".join(group) for group in dead)
    )


# --- 2. перевод, который угадали и с тех пор не трогали -----------------------

# Подтверждённые угадывания: `makemessages` предложил чужой перевод, человек
# посмотрел и оставил, потому что он действительно подходит. Ключ — язык и
# оригинал, значение — тот самый текст и причина, по которой он верен.
CONFIRMED_GUESSES: dict[tuple[str, str], tuple[str, str]] = {
    ("en", "Закрыта"): ("Closed", "то же слово, что у «Закрыт»: род тут не важен"),
    ("en", "Регистр учёта"): ("Ledger", "полная форма того же термина, что «Регистр»"),
    ("sr-latn", "Закрыта"): ("Zatvoren", "то же слово, что у «Закрыт»"),
    ("sr-latn", "Открыта"): ("Otvoren", "то же слово, что у «Открыт»"),
    ("sr-latn", "Регистр учёта"): ("Knjiga", "полная форма того же термина"),
    ("sr-latn", "Сотрудники"): (
        "Zaposleni",
        "в сербском единственное и множественное «zaposleni» совпадают",
    ),
}


def git(*args: str) -> str:
    done = subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True
    )
    return done.stdout if done.returncode == 0 else ""


def guessed_in_history(language: str, tmp: Path) -> dict[str, str]:
    """Что `makemessages` когда-либо угадал в этом каталоге: оригинал → текст.

    Берётся из истории, а не из нынешнего файла: пометка `fuzzy` — вещь
    одноразовая, её снимают в том же коммите, где заполняют перевод. След
    угадывания остаётся только в предыдущих версиях каталога.
    """
    relative = f"src/locale/{TRANSLATED[language].name}/LC_MESSAGES/django.po"
    guesses: dict[str, str] = {}
    for commit in git("log", "--format=%h", "--all", "--", relative).split():
        text = git("show", f"{commit}:{relative}")
        if not text:
            continue
        snapshot = tmp / f"{commit}.po"
        snapshot.write_text(text, encoding="utf-8")
        for item in read_po(snapshot):
            if item["fuzzy"] and item["msgid"]:
                guesses.setdefault(item["msgid"], item["msgstr"])
    return guesses


@pytest.mark.skipif(
    shutil.which("git") is None or not (ROOT / ".git").exists(),
    reason="нет истории репозитория",
)
@pytest.mark.parametrize("language", sorted(TRANSLATED))
def test_no_translation_stayed_exactly_as_makemessages_guessed_it(language, tmp_path):
    """`fuzzy` сняли — а текст остался прежним. Значит, его никто не смотрел.

    Написать такую проверку по одному нынешнему файлу нельзя в принципе: снятая
    пометка следов в нём не оставляет — запись выглядит как обычный переведённый
    текст. Единственное место, где угадывание ещё видно, — история каталога.
    Поэтому проверка сравнивает сегодняшний перевод с тем, что предложил
    `makemessages` в тот день: совпало дословно — строку не правили.

    Угадать верно тоже можно, и запрещать это глупо. Но подтвердить совпадение
    обязан человек — записью в `CONFIRMED_GUESSES` с причиной.
    """
    guesses = guessed_in_history(language, tmp_path)
    assert guesses, (
        f"{language}: в истории каталога нет ни одной пометки fuzzy — "
        "проверка проверяет пустоту, посмотрите, не сломался ли разбор"
    )

    current = {item["msgid"]: item["msgstr"] for item in catalog(language)}
    untouched = []
    for msgid, guess in sorted(guesses.items()):
        if current.get(msgid) != guess:
            continue
        confirmed = CONFIRMED_GUESSES.get((language, msgid))
        if confirmed and confirmed[0] == guess:
            continue
        untouched.append(f"  {msgid!r}\n      так и стоит: {guess!r}")

    assert not untouched, (
        f"{language}: переводов, оставшихся ровно такими, какими их угадал "
        f"makemessages ({len(untouched)}). Проверьте каждый глазами: чужой — "
        "исправьте, подходящий — впишите в CONFIRMED_GUESSES с причиной:\n"
        + "\n".join(untouched)
    )


# --- 3. инструмент раскладки не снимает пометку с чужой строки ----------------

FUZZY_SAMPLE = '''# Каталог для проверки.
msgid ""
msgstr ""
"Content-Type: text/plain; charset=UTF-8\\n"

#, fuzzy
#| msgid "не считался"
msgid "Посчитан"
msgstr "not calculated yet"

msgid "Ведомость"
msgstr "Payroll sheet"
'''


def test_apply_translations_keeps_fuzzy_on_entries_it_did_not_fill(tmp_path):
    """Пометку вправе снимать только тот, кто заменил текст.

    Здесь и была настоящая дыра. Инструмент снимал `fuzzy` в начале **каждой**
    записи, до того как выяснял, есть ли для неё перевод. Записи, которой
    перевода не дали, он оставлял чужой угаданный текст — но уже без пометки.
    С этой минуты каталог врал молча: `gettext` показывал строку, а проверка
    «пустых и fuzzy нет» была зелёной.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import apply_translations

    po = tmp_path / "django.po"
    po.write_text(FUZZY_SAMPLE, encoding="utf-8")

    # Переводим только вторую запись. Первая инструменту неизвестна.
    filled, problems = apply_translations.apply(po, {"Ведомость": "Payroll sheet"})
    assert filled == 1 and not problems

    result = {item["msgid"]: item for item in read_po(po) if item["msgid"]}
    assert result["Посчитан"]["fuzzy"], (
        "инструмент снял fuzzy со строки, которую не переводил — угаданный "
        "чужой текст вышел на экран, и ни одна проверка каталога этого больше "
        "не видит"
    )
    assert not result["Ведомость"]["fuzzy"], (
        "у заполненной записи пометка обязана уйти: иначе gettext не покажет "
        "даже правильный перевод"
    )


def test_apply_translations_drops_fuzzy_when_it_replaces_the_text(tmp_path):
    """Обратная сторона: у заполненной записи пометка обязана уйти целиком.

    Вместе с подсказкой `#| msgid` — она относится к прежнему ключу, а не к
    новому переводу, и оставленная в файле сбивает следующего читателя.
    """
    sys.path.insert(0, str(ROOT / "tools"))
    import apply_translations

    po = tmp_path / "django.po"
    po.write_text(FUZZY_SAMPLE, encoding="utf-8")
    filled, problems = apply_translations.apply(po, {"Посчитан": "Calculated"})
    assert filled == 1 and not problems

    text = po.read_text(encoding="utf-8")
    assert "#| msgid" not in text, "остался остаток подсказки от прежнего ключа"
    result = {item["msgid"]: item for item in read_po(po) if item["msgid"]}
    assert not result["Посчитан"]["fuzzy"]
    assert result["Посчитан"]["msgstr"] == "Calculated"


# --- json со списком правок остаётся читаемым --------------------------------


def test_translation_tables_are_valid_json():
    """Файлы переводов рядом с инструментом обязаны разбираться.

    Мелочь, но пропущенная запятая в таблице переводов выглядит как «часть строк
    просто не доехала», а не как синтаксическая ошибка.
    """
    tables = sorted((ROOT / "tools" / "translations").glob("*.json"))
    assert tables, "таблиц переводов не нашлось — проверка проверяет пустоту"
    for table in tables:
        loaded = json.loads(table.read_text(encoding="utf-8"))
        assert isinstance(loaded, dict) and loaded, f"{table.name}: пусто"
        for key, value in loaded.items():
            assert set(value) <= {"en", "sr"}, f"{table.name}: чужой язык у {key!r}"
