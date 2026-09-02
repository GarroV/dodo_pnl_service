"""Чего стоит очередь, когда делать ей нечего (T180, issue #190).

Брокер очереди — сама база (`"orm": "default"`), а `django-q2` на пустой
очереди спит `Conf.POLL` и спрашивает снова (`django_q/brokers/orm.py`).
Умолчание библиотеки — 0,2 с, то есть **пять запросов в секунду круглосуточно**
при нулевой работе: 432 тысячи запросов в день. Замер на стенде это и показал —
рабочий процесс 6,08 % CPU и база 10,15 % в полном простое, при том что веб,
который отвечает на запросы, стоял на 1 %.

Умолчание библиотеки — не решение продукта, а то, что досталось молчанием.
Поэтому `poll` задан явно, и тесты ниже сторожат две вещи.

**Первое: опрос обязан остаться заметно быстрее порога «никто не взял».**
`PAYRUN_QUEUE_STALE_SECONDS` — через сколько страница говорит человеку, что
рабочий процесс, похоже, не запущен. Если опрос подберётся к этому порогу,
страница начнёт врать про живую очередь: задача ещё лежит законные полсекунды
своего цикла, а человеку уже сказано, что её никто не возьмёт. Разъехаться
этим двум числам легко — они правятся в разное время и разными людьми.

**Второе: разъезд отвергается вслух, а не подгоняется молча.** Подогнать
значение самим означало бы, что на площадке работает не то, что написано в
`.env`, и узнать об этом неоткуда.
"""
from __future__ import annotations

import importlib

import pytest
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def test_the_poll_interval_is_a_decision_of_the_product_not_a_library_default():
    """`poll` задан явно. Умолчание `django-q2` — 0,2 с, и оно нам дорого."""
    assert "poll" in settings.Q_CLUSTER, (
        "poll в Q_CLUSTER снова не задан — очередь вернулась к умолчанию "
        "django-q2 (0,2 с) и опрашивает базу пять раз в секунду круглосуточно"
    )
    assert settings.Q_CLUSTER["poll"] >= 1, (
        f"опрос раз в {settings.Q_CLUSTER['poll']} с — это тот самый расход, "
        "ради которого задача и заводилась"
    )


def test_the_page_does_not_start_lying_about_a_live_queue():
    """Порог «никто не взял» обязан быть кратно больше цикла опроса.

    Кратность, а не «больше»: при poll = stale задача, лежащая свой законный
    цикл, попадает под порог ровно на границе, и страница объявляет живую
    очередь мёртвой примерно в половине случаев.
    """
    poll = settings.Q_CLUSTER["poll"]
    stale = settings.PAYRUN_QUEUE_STALE_SECONDS
    assert stale >= poll * 3, (
        f"опрос {poll} с против порога {stale} с: страница скажет «рабочий "
        "процесс не запущен» про очередь, которая просто ещё не проснулась"
    )


def test_the_queue_still_wakes_up_faster_than_a_person_notices():
    """Верхняя граница есть и у самой экономии.

    Очередь, просыпающаяся раз в полминуты, дешева и бесполезна: человек нажал
    кнопку и смотрит на страницу.
    """
    assert settings.Q_CLUSTER["poll"] <= 5, (
        f"опрос раз в {settings.Q_CLUSTER['poll']} с — человек успеет решить, "
        "что кнопка не сработала"
    )


def test_a_poll_that_outruns_the_stale_threshold_is_refused_out_loud(monkeypatch):
    """Настроенное мимо — отказ на старте, а не тихая подгонка.

    Проверяется настоящей перезагрузкой модуля настроек с чужим окружением:
    утверждение о поведении настроек, которое не гоняет настройки, доказывает
    только то, что автор так думал.
    """
    monkeypatch.setenv("PAYRUN_QUEUE_POLL_SECONDS", "30")
    monkeypatch.setenv("PAYRUN_QUEUE_STALE_SECONDS", "10")
    import config.settings as settings_module

    with pytest.raises(ImproperlyConfigured) as refusal:
        importlib.reload(settings_module)
    assert "PAYRUN_QUEUE_POLL_SECONDS" in str(refusal.value)
    assert "PAYRUN_QUEUE_STALE_SECONDS" in str(refusal.value)

    # Вернуть модуль в рабочее состояние: он общий для всего прогона.
    monkeypatch.undo()
    importlib.reload(settings_module)


def test_the_env_example_explains_the_knob_and_its_neighbour():
    """Переменная, которой нет в примере окружения, не существует для площадки."""
    from pathlib import Path

    example = (Path(__file__).resolve().parent.parent / ".env.example").read_text(encoding="utf-8")
    assert "PAYRUN_QUEUE_POLL_SECONDS=" in example
