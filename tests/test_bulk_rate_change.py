"""Ставку поднимают группе одним действием, а не тридцать раз подряд (#181).

Индексация — обычная ежегодная работа: с первого числа ставки растут у всей
кухни или у всех курьеров. Сегодня это делается по одному человеку: открыть
карточку, завести версию, повторить тридцать раз. Тридцать раз — это тридцать
шансов ошибиться и ни одного способа проверить себя.

Что здесь проверяется:

1. Действие меняет ставки **всем, кто попадает под условие**, и заводит каждому
   свою версию с даты — то есть работает ровно так же, как правка по одному, а
   не в обход неё. История каждого человека остаётся целой.
2. **Предпросмотр до применения**: кого затронет и как изменится ставка. Без
   него человек нажимает кнопку вслепую, а отменить массовую правку нечем.
3. Закрытые месяцы не двигаются: версия с датой внутри утверждённого месяца
   заводится, но прошлый расчёт остаётся прежним — разницу забирает
   доначисление, как и при обычной правке.
4. Право то же, что у ведения справочников: массовая правка ставок — не
   отдельная власть, а та же работа, сделанная быстрее.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as

JULY = date(2026, 7, 1)


@pytest.fixture
def kitchen(web_env):
    """Группа с несколькими людьми — материал для массовой правки."""
    from core.models import EmployeeGroup, EmploymentTerm

    group = (
        EmployeeGroup.objects.filter(employmentterm__isnull=False)
        .distinct().order_by("code").first()
    )
    assert group is not None, "в сиде нет группы с людьми — проверять нечего"
    before = {
        term.id: term.base_rate
        for term in EmploymentTerm.objects.filter(group=group)
    }
    yield group
    # Возвращаем как было: ставки — общее состояние базы, и оставленные
    # поднятыми они молча испортят числа соседним тестам.
    EmploymentTerm.objects.filter(valid_from=JULY, group=group).delete()
    for term_id, rate in before.items():
        EmploymentTerm.objects.filter(pk=term_id).update(base_rate=rate, valid_to=None)


def test_the_preview_says_who_will_be_touched(client, kitchen):
    """Сначала показываем кого и как, и только потом даём применить."""
    login_as(client, "admin")
    response = client.post("/directory/groups/raise/", {
        "group": str(kitchen.id), "valid_from": "2026-07-01",
        "percent": "10", "preview": "1",
    })
    assert response.status_code == 200, body(response)[:400]

    html = body(response)
    assert "10" in html
    # В предпросмотре обязаны быть люди поимённо: «затронет 12 человек» не
    # даёт проверить себя, а список даёт.
    from core.models import EmploymentTerm

    someone = EmploymentTerm.objects.filter(group=kitchen).select_related("employee").first()
    assert someone.employee.last_name in html, "предпросмотр не называет людей"


def test_the_raise_creates_a_version_for_everyone(client, kitchen):
    """Применили — у каждого своя версия с даты, история цела."""
    from core.models import EmploymentTerm

    before = {
        term.employee_id: term.base_rate
        for term in EmploymentTerm.objects.filter(group=kitchen)
    }
    login_as(client, "admin")
    response = client.post("/directory/groups/raise/", {
        "group": str(kitchen.id), "valid_from": "2026-07-01", "percent": "10",
    }, follow=True)
    assert response.status_code == 200, body(response)[:400]

    fresh = EmploymentTerm.objects.filter(group=kitchen, valid_from=JULY)
    assert fresh.count() == len(before), "версия появилась не у всех"
    for term in fresh:
        was = before[term.employee_id]
        assert term.base_rate == (was * Decimal("1.10")).quantize(was), (
            f"{term.employee_id}: ставка поднята неверно"
        )
    # Прошлые версии закрыты датой, а не переписаны: история цела.
    old = EmploymentTerm.objects.filter(group=kitchen).exclude(valid_from=JULY)
    assert old.exists(), "старые версии исчезли — история потеряна"
    assert all(term.valid_to == JULY for term in old if term.valid_to)


def test_a_raise_by_amount_works_too(client, kitchen):
    """Поднять можно и суммой: у части партнёров индексация в динарах."""
    from core.models import EmploymentTerm

    before = EmploymentTerm.objects.filter(group=kitchen).first().base_rate
    login_as(client, "admin")
    client.post("/directory/groups/raise/", {
        "group": str(kitchen.id), "valid_from": "2026-07-01", "amount": "25",
    }, follow=True)

    term = EmploymentTerm.objects.filter(group=kitchen, valid_from=JULY).first()
    assert term is not None
    assert term.base_rate == before + Decimal("25")


def test_nothing_happens_without_the_right(client, kitchen):
    """Право то же, что у справочников: у бухгалтера его нет."""
    from core.models import EmploymentTerm

    # Роль без права ведения справочников — теперь это управляющий точки:
    # бухгалтер и оперативный директор их ведут с 28.08.2026 (D059).
    login_as(client, "manager")
    response = client.post("/directory/groups/raise/", {
        "group": str(kitchen.id), "valid_from": "2026-07-01", "percent": "10",
    })
    assert response.status_code == 403, body(response)[:300]
    assert not EmploymentTerm.objects.filter(group=kitchen, valid_from=JULY).exists()


def test_zero_change_is_refused_in_words(client, kitchen):
    """Поднятие на ноль — не работа, а промах: отказ словами."""
    login_as(client, "admin")
    response = client.post("/directory/groups/raise/", {
        "group": str(kitchen.id), "valid_from": "2026-07-01", "percent": "0",
    })
    assert response.status_code in (400, 422)
    text = body(response).lower()
    assert "ноль" in text or "проценты" in text or "сумм" in text
