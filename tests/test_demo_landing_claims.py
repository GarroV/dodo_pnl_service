"""Титульная демо не вправе обещать доступ, которого нет (T117).

Что случилось. На одной странице, через пятнадцать строк друг от друга, стояло:
«Enter as **Accountant** — sees official, supplementary, internal» (собирается из
`demo.seed.ROLES`, то есть всегда верно) и «Switching roles is the point: **an
accountant sees only the official ledger**» (жёстко зашитый абзац, оставшийся от
модели доступа до D036). Демо — витрина, и это её первый абзац про суть
продукта.

Почему проверка устроена как детектор дрейфа, а не как «нет такой фразы».
Убрать одну фразу легко, и завтра рядом появится вторая такая же: список ролей
на странице собирается из кода, а прозу пишет человек. Поэтому проверяется
**согласие прозы с кодом**: ни одна роль с полным набором регистров не должна
описываться как ограниченная, а объяснение механизма обязано называть роль,
которая ограничена **на самом деле**.

Проверка статическая — по шаблону и `ROLES`. Живьём титульная страница
проверяется смоуком демо (`tools/smoke_demo.mjs`): здесь нужен именно детектор,
который сработает в обычном прогоне, без поднятого демо-стенда.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
LANDING = ROOT / "src" / "demo" / "templates" / "demo" / "landing.html"

# Тело шаблона без разметки и без блоков, которые собираются из кода: проверять
# надо прозу, а сгенерированный список ролей верен по построению.
TAG = re.compile(r"\{%.*?%\}|\{\{.*?\}\}|<[^>]+>", re.S)
COMMENT = re.compile(
    r"\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}|\{#.*?#\}", re.S
)
STYLE = re.compile(r"<style\b.*?</style>", re.S | re.I)


def prose(*, lower: bool = True) -> str:
    """Слова титульной страницы — те, что увидит посетитель.

    Комментарий шаблона вырезается: он написан по-русски и для того, кто правит
    страницу, а не для гостя. Список ролей — тоже: он собирается из `ROLES` и
    врать не может по построению.
    """
    text = LANDING.read_text(encoding="utf-8")
    text = COMMENT.sub(" ", STYLE.sub(" ", text))
    text = re.sub(r'<ul class="roles">.*?</ul>', " ", text, flags=re.S)
    text = re.sub(r"\s+", " ", TAG.sub(" ", text)).strip()
    return text.lower() if lower else text


@pytest.fixture(scope="module")
def roles():
    from demo.seed import ALL_LEDGERS, ROLES

    return [
        {
            "code": code,
            "title": title.lower(),
            # Первое слово названия: в прозе роль называют «an accountant», а не
            # «Unit manager (Novi Sad Bulevar)».
            "word": title.lower().split(" (")[0].split()[-1],
            "full": set(ledgers) == set(ALL_LEDGERS),
            "unit": unit,
        }
        for code, title, ledgers, unit, _permissions in ROLES
    ]


def test_no_role_with_every_ledger_is_described_as_limited(roles):
    """Роль, у которой все три регистра, не может «видеть только один».

    Ровно эта фраза и стояла про бухгалтера после D036. Ищется не она сама, а
    её форма: имя роли и «only … ledger» в одном предложении.
    """
    text = prose()
    for role in roles:
        if not role["full"]:
            continue
        for sentence in re.split(r"[.;]", text):
            if role["word"] in sentence and "ledger" in sentence:
                assert "only" not in sentence, (
                    f"титульная говорит, что {role['word']} видит не всё, "
                    f"а по коду у него все регистры: «{sentence.strip()}»"
                )


def test_the_mechanism_is_explained_on_a_role_that_is_really_limited(roles):
    """Объяснение среза не должно исчезнуть вместе с неверной фразой.

    Иначе починка свелась бы к удалению абзаца, и демо перестало бы показывать
    главное своё свойство: числа пересчитываются под то, что роли видно.
    """
    text = prose()
    limited = [role for role in roles if not role["full"]]
    assert limited, "в демо не осталось ни одной роли с неполным набором регистров"

    named = [role["word"] for role in limited if role["word"] in text]
    assert named, (
        "титульная объясняет срез регистров, не называя ни одной роли, "
        f"которая действительно ограничена: {[r['word'] for r in limited]}"
    )
    assert "cannot be recovered by subtracting" in text, (
        "исчезло обещание, ради которого срез и делался: скрытое не выводится "
        "вычитанием"
    )


def test_the_landing_stays_english():
    """Демо всегда английское — правило владельца, а не пожелание."""
    assert not re.search(r"[а-яё]", prose(lower=False), re.I), (
        "на титульной демо появилась кириллица"
    )
