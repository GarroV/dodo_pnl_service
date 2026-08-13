"""Числа и фразы на нерусской странице (T093).

Три находки демо, и все три — про то, что кусок продукта не поехал за языком:

* `5 % и 2,000.00` — русский союз на английском экране, склеенный f-строкой;
* `-9,1 %` рядом с `84,840.00` — два разделителя дробной части в одной таблице;
* `This partner the difference is carried over to the current period` —
  предложение, собранное из куска, который по-английски сказуемым не является.

Проверяется формат и целые фразы, а не разметка: экран целиком проверяет
`test_i18n_screens.py`, ему тут дублироваться незачем.
"""
from __future__ import annotations

import re

import pytest

CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")

LANGUAGES = ["ru", "en", "sr-latn"]


def decimal_mark(shown: str) -> str:
    """Разделитель дробной части показанного числа: последний знак перед двумя цифрами."""
    match = re.search(r"([.,])\d+(?:\s|$|\D*$)", shown)
    assert match, f"в «{shown}» не видно дробной части"
    return match.group(1)


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_percent_uses_the_same_separator_as_the_money_next_to_it(language, web_env):
    """Процент и деньги в одной таблице пишутся одним письмом.

    До этой задачи разделитель процента был зашит запятой, и английская страница
    показывала `-9,1 %` в колонке рядом с `84,840.00`. Это не косметика: два
    разделителя подряд читаются как две разные системы записи, и число из
    соседней колонки нельзя сложить с этим в уме.
    """
    from django.utils import translation

    from web.format import money, percent

    with translation.override(language):
        assert decimal_mark(percent(-9.14)) == decimal_mark(money(84840)), (
            f"{language}: процент {percent(-9.14)}, деньги {money(84840)}"
        )


@pytest.mark.parametrize("language", ["en", "sr-latn"])
def test_the_threshold_says_and_in_the_language_of_the_page(language, web_env):
    """Порог — фраза, а не склейка: союз «и» несёт смысл и переводится.

    «5 % и 2 000,00» означает «превышены оба порога», а не «любой из них».
    Склеенный f-строкой, он приезжал русским на любой язык.
    """
    from django.utils import translation

    from web.format import threshold

    with translation.override(language):
        shown = threshold(5, 2000)
    assert not CYRILLIC.search(shown), f"{language}: порог по-русски — {shown}"
    assert "2" in shown and "5" in shown, f"{language}: из порога пропали числа — {shown}"


@pytest.mark.parametrize("language", LANGUAGES)
def test_the_threshold_keeps_one_separator_too(language, web_env):
    """Порог печатает деньги тем же способом, что и вся страница."""
    from django.utils import translation

    from web.format import money, threshold

    with translation.override(language):
        assert decimal_mark(threshold(5, 2000)) == decimal_mark(money(2000))


RETRO_SENTENCES = {
    "en": "For this partner, the difference is carried over to the current period.",
    "sr-latn": "Kod ovog partnera razlika se prenosi u tekući period.",
}


@pytest.mark.parametrize("language", list(RETRO_SENTENCES))
def test_the_retro_mode_is_a_whole_sentence(language, web_env):
    """Фраза про способ правок переводится целиком, а не куском.

    Русское «У этого партнёра X» переводится по-английски только целиком:
    подставленный кусок давал предложение без сказуемого. Проверяется перевод, а
    не разметка, — на экране эта строка появляется только у утверждённого месяца
    с расхождением, и ловить её там значило бы проверять условие, а не язык.
    """
    from django.utils import translation

    with translation.override(language):
        shown = translation.gettext("У этого партнёра разница переносится в текущий период.")
    assert shown == RETRO_SENTENCES[language], f"{language}: {shown}"


def test_the_old_glued_fragment_is_gone_from_the_product():
    """Кусок фразы больше не переводится отдельно — иначе склейка вернётся.

    Проверка по исходникам, а не по экрану: вернуть склейку легче всего именно
    правкой кода, и увидеть её на экране можно только в одном редком состоянии
    периода.
    """
    import re
    from pathlib import Path

    # Именно вызов перевода, а не любое упоминание: та же фраза стоит в
    # комментарии к типу базы (`core/models.py`), и это правильное место —
    # там она объясняет значение enum, а не показывается человеку.
    translated = re.compile(
        r"(?:gettext_noop|gettext_lazy|gettext|_)\(\s*[\"']"
        r"[^\"']*разница переносится в текущий период"
    )
    root = Path(__file__).resolve().parent.parent / "src"
    guilty = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if translated.search(path.read_text(encoding="utf-8"))
        and "migrations" not in path.parts
    ]
    assert not guilty, (
        "кусок фразы снова переводится отдельно: " + ", ".join(guilty)
    )
