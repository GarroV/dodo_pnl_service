"""Первый месяц заводится из продукта, а не командой (issue #192).

Найдено проходом с нуля: новый партнёр заводит юрлицо, точку, группу, людей,
календарь и правила — и упирается в стену. Периода нет, а список месяцев
советует «наполните базу тестовыми данными командой manage.py seed_dev», то
есть отсылает пользователя к команде разработчика. Табель живёт внутри месяца,
поэтому часы вносить некуда, а расчёт запускать не на чем: продукт не даёт
дойти до того, ради чего существует.

Теперь месяц заводит тот, кто его ведёт, — кнопкой на списке.

Почему не «создавать первый месяц автоматически при входе». Месяц — это учётный
период, а не строка интерфейса: он открывается решением, у него есть состояние
и он закрывается. Автоматически заведённый месяц означал бы, что продукт сам
решил, с какого месяца партнёр начинает учёт.
"""
from __future__ import annotations

from datetime import date

from conftest import body, login_as

JUNE = date(2026, 6, 1)


def test_the_empty_list_offers_to_open_a_month(client, web_env):
    """Пустое состояние предлагает действие, а не команду в терминале."""
    from core.models import Period

    saved = list(Period.objects.values_list("id", flat=True))
    Period.objects.all().delete()
    try:
        login_as(client, "director")
        html = body(client.get("/periods/"))
        assert "manage.py" not in html, (
            "продукт советует команду разработчика вместо собственной кнопки"
        )
        assert "Завести месяц" in html or "Open a month" in html
    finally:
        # Периоды сида нужны соседним тестам: без них рушится половина прогона.
        Period.objects.bulk_create(
            [Period(id=pk, tenant_id=_tenant(), period=JUNE) for pk in saved]
        )


def test_a_month_is_opened_from_the_screen(client, web_env):
    """Кнопка заводит месяц, и он сразу открывается в списке."""
    from core.models import Period

    login_as(client, "director")
    response = client.post("/periods/open/", {"month": "2026-09"}, follow=True)
    assert response.status_code == 200, body(response)[:400]

    opened = Period.objects.filter(period=date(2026, 9, 1)).first()
    assert opened is not None, "месяц не заведён"
    assert opened.status == "open"
    assert "2026" in body(response)
    Period.objects.filter(pk=opened.pk).delete()


def test_the_same_month_twice_is_not_two_months(client, web_env):
    """Повторное заведение возвращает тот же месяц, а не заводит второй."""
    from core.models import Period

    login_as(client, "director")
    client.post("/periods/open/", {"month": "2026-10"}, follow=True)
    client.post("/periods/open/", {"month": "2026-10"}, follow=True)

    assert Period.objects.filter(period=date(2026, 10, 1)).count() == 1
    Period.objects.filter(period=date(2026, 10, 1)).delete()


def test_a_role_without_the_right_cannot_open_a_month(client, web_env):
    """Месяц заводит тот, кто его ведёт: у управляющего точки права нет."""
    from core.models import Period

    login_as(client, "manager")
    response = client.post("/periods/open/", {"month": "2026-11"})
    assert response.status_code == 403, body(response)[:300]
    assert not Period.objects.filter(period=date(2026, 11, 1)).exists()


def test_a_broken_month_is_refused_in_words(client, web_env):
    """Опечатка в месяце — отказ словами, а не пятисотая."""
    login_as(client, "director")
    response = client.post("/periods/open/", {"month": "июнь"})
    assert response.status_code in (400, 422)
    assert "2026-06" in body(response), "отказ не показывает, как надо писать"


def _tenant():
    from core.models import Tenant

    return Tenant.objects.get(code="rs-dev").id
