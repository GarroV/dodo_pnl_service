"""Месяц не закрыть с дырами: продукт сам говорит, чего не хватает (#175).

Эталон (модуль 4) формулирует прямо: «список „прежде чем закрыть“ — не отчёт,
который надо открыть, а условие, без которого кнопка не нажимается». Сегодня
утвердить период можно при незакрытых часах, неразобранных бумагах и строках
без статьи — и P&L получится неверным молча.

Три вида находок, и они ведут себя по-разному:

* **блокирующее** — то, из-за чего P&L будет неверным: незакрытые часы точки,
  неразобранные документы, строки без статьи. Утверждение не проходит;
* **предупреждение** — стоит посмотреть, но закрыть не мешает;
* **подозрительное** — то, что может быть правдой: рост вдвое, ноль там, где
  обычно не ноль. Система не знает, ошибка это или жизнь.

Почему проверка не может жить только на экране: экран показывает то, что
человек открыл, а утверждение приходит запросом. Значит отказ обязан стоять на
самом действии, а список — рядом с кнопкой.
"""
from __future__ import annotations

from datetime import date

import pytest

from conftest import body, login_as, period_url

JUNE = date(2026, 6, 1)


@pytest.fixture
def calculated(client, web_env):
    """Посчитанный, но не утверждённый месяц — состояние перед закрытием.

    Убирается сносом расчёта, а не возвратом состояния: цикл периода стережёт
    база, и «утверждён → посчитан» она не разрешает — как и должна.
    """
    from conftest import wipe_payruns
    from core.models import ClosingWaiver, TimesheetClosure

    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    yield url
    TimesheetClosure.objects.all().delete()
    ClosingWaiver.objects.all().delete()
    wipe_payruns(web_env)


def test_the_page_lists_what_is_missing(client, calculated):
    """На странице месяца видно, что мешает закрытию."""
    html = body(client.get(calculated))
    assert "Прежде чем закрыть" in html or "Готовность" in html, (
        "перед утверждением продукт молчит о полноте данных"
    )


def test_open_unit_hours_block_the_approval_where_closing_is_used(client, calculated):
    """Незакрытые часы держат утверждение — но только там, где точки закрывают.

    Само по себе незакрытие часов не делает P&L неверным: числа уже посчитаны
    из внесённых часов. Оно означает, что работа над месяцем не закончена, — и
    это разной силы утверждение в зависимости от того, как партнёр ведёт месяц.

    Партнёр закрыл хоть одну точку — значит механизм у него в ходу, и
    незакрытые остальные держат закрытие. Не закрыл ни одной — требовать этого
    нельзя: месяц оказался бы заперт кнопкой, о которой человек не знает.
    """
    from core.models import Period, Unit
    from timesheets import closing

    first = Unit.objects.order_by("code").first()
    closing.close_unit(
        tenant_id=first.tenant_id, period=JUNE, unit_id=first.id, actor_id=None,
    )

    response = client.post(calculated + "approve/", follow=True)
    assert response.status_code == 409, body(response)[:400]
    assert "час" in body(response).lower(), "отказ не называет причину"
    assert Period.objects.get(period=JUNE).status != "closed"


def test_untouched_closing_does_not_lock_the_month(client, calculated):
    """А если точки никто не закрывал — месяц закрывается, и это не дыра."""
    from core.models import Period

    response = client.post(calculated + "approve/", follow=True)
    assert response.status_code == 200, body(response)[:400]
    assert Period.objects.get(period=JUNE).status == "closed"


def test_papers_waiting_for_a_decision_block_it_too(client, calculated):
    """Неразобранная бумага — деньги, которых нет в отчёте."""
    from core.models import SourceDocument

    # Разобрана — значит у документа появились строки; отдельного признака
    # «разобрано» в продукте нет намеренно (`web/papers`).
    waiting = SourceDocument.objects.filter(
        handed_over_at__isnull=False, fact__isnull=True,
    ).count()
    if not waiting:
        pytest.skip("в сиде нет неразобранных бумаг")

    response = client.post(calculated + "approve/", follow=True)
    assert response.status_code == 409
    assert "бумаг" in body(response).lower() or "документ" in body(response).lower()


def test_a_clean_month_closes(client, calculated):
    """Всё устранено — месяц закрывается. Иначе проверка была бы тупиком."""
    from core.models import Period, TimesheetClosure, Unit
    from timesheets import closing

    for unit in Unit.objects.all():
        closing.close_unit(
            tenant_id=unit.tenant_id, period=JUNE, unit_id=unit.id, actor_id=None,
        )
    assert TimesheetClosure.objects.exists()

    response = client.post(calculated + "approve/", follow=True)
    assert response.status_code == 200, body(response)[:400]
    assert Period.objects.get(period=JUNE).status == "closed"


def test_a_blocker_can_be_postponed_with_a_reason(client, calculated):
    """Блокирующее можно отложить — с причиной, и она остаётся в протоколе.

    Без этого проверка превращается в тупик: часть находок законна («выписка
    придёт послезавтра, а зарплату платить сегодня»), и запрет без выхода
    заставит закрывать месяц мимо продукта.
    """
    from core.models import Period

    response = client.post(calculated + "postpone/", {
        "finding": "unit_hours", "reason": "точка закроет часы завтра, платим сегодня",
    }, follow=True)
    assert response.status_code == 200, body(response)[:400]

    approved = client.post(calculated + "approve/", follow=True)
    assert approved.status_code == 200, body(approved)[:400]
    assert Period.objects.get(period=JUNE).status == "closed"


def test_postponing_without_a_reason_is_refused(client, calculated):
    """Причина обязательна: отложенное без причины через полгода необъяснимо."""
    response = client.post(calculated + "postpone/", {
        "finding": "unit_hours", "reason": "  ",
    })
    assert response.status_code in (400, 422)
