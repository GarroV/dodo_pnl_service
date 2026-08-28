"""Разделы продукта названы словарём эталона и живут в одном месте (issue #162, T194).

Модуль 10 эталона называет области учёта по работе человека: «Сбор данных»
(табель, наличные, выписка, инбокс), «Расчёт» (ведомость, закрытие, выплаты),
«Отчёты» (P&L, сверка, аналитика), «Справочники», «Настройки». У нас в шапке
стояли «Периоды · Расходы · Счета · Сотрудники» — слова о механике продукта, а
не о работе: «Периоды» это учётный месяц, а человек приходит считать зарплату.

Владелец 18.08.2026 сказал ровно об этом: «я не понимаю, как она работает, где
какие функции».

**Словарь — в одном месте** (`web/navigation.py`), а не в разметке шапки: пока
названия живут в шаблоне, они не сверяются ни с чем, и расхождение с эталоном
видно только тому, кто помнит обе картинки. Здесь сверка механическая.

Чего этот тест НЕ требует: чтобы у нас были все пункты эталона. P&L, выписки и
аналитики в продукте ещё нет, и рисовать пункт, ведущий в никуда, хуже, чем не
рисовать. Требование другое — **у каждого нашего раздела имя из словаря
эталона**, а не своё.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
REFERENCE = ROOT / "Дизайн-система Dodo P&L" / "Модуль 10 - Вход и каркас.dc.html"


def reference_words() -> set[str]:
    """Слова навигации из самого эталона — читаются из его исходника.

    Не список, переписанный сюда руками: переписанный однажды разойдётся с
    эталоном молча, и тест начнёт охранять нашу копию вместо источника истины.
    """
    text = REFERENCE.read_text(encoding="utf-8")
    block = re.search(r"nav:\s*\{(.*?)\}", text, re.S)
    menu = re.search(r"menu:\s*\{(.*?)\n\s*\}\s*\}", text, re.S)
    words = set()
    for chunk in (block.group(1) if block else "", menu.group(1) if menu else ""):
        words.update(re.findall(r'"([А-ЯЁ][^"]{2,40})"', chunk))
    return words


def test_the_reference_still_carries_its_navigation():
    """Сначала убеждаемся, что читаем эталон, а не пустоту."""
    words = reference_words()
    assert "Табель" in words and "Ведомость" in words, (
        f"словарь навигации в эталоне не найден: {sorted(words)[:10]}"
    )


def test_every_section_of_the_product_is_named_by_the_reference():
    """Каждый раздел шапки назван словом эталона — или объяснён исключением."""
    from web import navigation

    allowed = reference_words() | navigation.OUR_OWN
    strangers = [
        item.title for group in navigation.GROUPS for item in group.items
        if str(item.title) not in allowed
    ]
    assert not strangers, (
        "эти разделы названы не словарём эталона и не объявлены нашими: "
        + ", ".join(strangers)
    )


def test_the_header_is_built_from_the_dictionary(client, web_env):
    """Шапка собирается из словаря, а не из слов, набранных в шаблоне."""
    from conftest import body, login_as
    from web import navigation

    login_as(client, "director")
    html = body(client.get("/periods/"))

    visible = [
        str(item.title) for group in navigation.GROUPS for item in group.items
    ]
    shown = [title for title in visible if title in html]
    assert len(shown) >= 3, (
        f"в шапке не видно разделов словаря: показаны {shown}, ожидались {visible}"
    )
    assert "Периоды" not in html, "старое название раздела осталось в шапке"


def test_each_item_points_somewhere_real(client, web_env):
    """Пункт ведёт на существующий адрес: пункт в никуда хуже отсутствующего."""
    from django.urls import reverse

    from web import navigation

    for group in navigation.GROUPS:
        for item in group.items:
            assert reverse(item.route), f"пункт «{item.title}» ведёт в никуда"
