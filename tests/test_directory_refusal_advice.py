"""Отказ обязан советовать выполнимое (часть T103).

Отказ по закрытому месяцу приходит от одной функции, но поводов у него два, и
они разные.

**Повод первый — правка версионируемого с датой.** Ставка, календарь, правило
расчёта: человек ввёл дату, дата попала в утверждённый месяц. Совет «возьмите
дату позже такой-то» тут выполним — поле даты у него перед глазами.

**Повод второй — правка того, у чего версий нет вовсе.** Схема расчёта и регистр
группы одни на всю историю: правка задевает утверждённый месяц не потому, что
дату выбрали неудачно, а потому, что выбирать нечего. Даты в форме группы нет.
Чтобы всё-таки позвать общий отказ, вызывающий подставлял `date.min`, и человек
получал `с 0001-01-01` и совет взять дату позже — то есть выдуманную дату и
невыполнимое действие сразу.

Здесь проверяется, что у второго повода свой отказ: без даты, которой человек не
вводил, и с советом, который он может выполнить, — переопределить нужное
человеку в условиях найма или открыть месяц заново. И что первый повод при этом
свою дату и свой совет сохранил: одинаковыми эти два отказа быть не должны.
"""
from __future__ import annotations

from datetime import date

import pytest

from conftest import body, login_as
from test_directory import approve_june, payruns_restored, sql  # noqa: F401

# Дата, которой человек не вводил. Ровно она и вылезала на экран.
MADE_UP_DATE = "0001-01-01"


def official_group(sql):  # noqa: F811  — имя фикстуры pytest, не переопределение
    return sql.execute(
        "select id, code, title, scheme, ledger from employee_groups "
        "where ledger = 'official' and tenant_id in "
        "(select id from tenants where code = 'rs-dev') limit 1"
    ).fetchone()


def test_the_group_refusal_names_no_date_the_person_never_typed(
    client, sql, web_env, payruns_restored,  # noqa: F811
):
    """Отказ по схеме и регистру группы не называет `0001-01-01`.

    Проверяется именно текст, а не код ответа: отказать экран умел и раньше —
    врал он в объяснении. Выдуманная дата в отказе хуже, чем её отсутствие:
    человек идёт искать месяц, которого нет, и решает, что сломан продукт.
    """
    approve_june(client, web_env)
    client.post("/logout/")
    group_id, code, title, scheme, ledger = official_group(sql)

    login_as(client, "admin")
    try:
        refused = client.post(f"/directory/groups/{group_id}/", {
            "code": code, "title": title, "scheme": "совсем другая", "ledger": ledger,
        })
        assert refused.status_code == 409, refused.status_code
        html = body(refused)
        assert MADE_UP_DATE not in html, (
            "отказ назвал дату, которой человек не вводил и которой не бывает"
        )
        assert "2026-06" in html, (
            "отказ обязан назвать месяц, из-за которого отказано, — иначе он "
            "говорит «нельзя» и не говорит почему"
        )
    finally:
        sql.execute(
            "update employee_groups set scheme = %s where id = %s", (scheme, group_id)
        )
        client.post("/logout/")


def test_the_group_refusal_does_not_advise_picking_a_date(
    client, sql, web_env, payruns_restored,  # noqa: F811
):
    """В форме группы поля даты нет — значит совета «возьмите дату позже» быть не может.

    Отдельной проверкой от предыдущей: убрать выдуманную дату можно и оставив
    совет, а совет без даты стал бы просто непонятным вместо неверного.
    Выполнимость проверяется по тому, что отказ называет **другой** путь —
    условия найма, где дата есть, либо переоткрытие месяца.
    """
    approve_june(client, web_env)
    client.post("/logout/")
    group_id, code, title, scheme, ledger = official_group(sql)

    login_as(client, "admin")
    try:
        refused = client.post(f"/directory/groups/{group_id}/", {
            "code": code, "title": title, "scheme": "совсем другая", "ledger": ledger,
        })
        html = body(refused)
        assert "озьмите дату" not in html, (
            "отказ советует выбрать дату, а поля даты в этой форме нет"
        )
        assert "словиях найма" in html, (
            "отказ не назвал путь, которым правку всё-таки можно провести"
        )
    finally:
        sql.execute(
            "update employee_groups set scheme = %s where id = %s", (scheme, group_id)
        )
        client.post("/logout/")


# --- а у отказа с датой дата и совет остаются на месте -------------------------


@pytest.fixture
def june_closed(monkeypatch):
    """Утверждённый июнь без базы: отказ считается по одной дате-границе."""
    from web import directory

    monkeypatch.setattr(directory, "closed_through", lambda tenant_id: date(2026, 6, 30))
    return directory


def test_the_dated_refusal_still_names_the_date_and_advises_a_later_one(june_closed):
    """Контраст: там, где дата есть, отказ по-прежнему называет её и советует позже.

    Без этой проверки починку легко «доделать» до конца — свести оба отказа к
    одному тексту без даты. Тогда человек, у которого поле даты перед глазами,
    перестал бы получать единственный нужный ему ответ: до какого числа нельзя.
    """
    with pytest.raises(june_closed.DirectoryRefused) as refusal:
        june_closed.refuse_if_touches_closed_month(
            None, date(2026, 6, 15), "условия найма",
        )
    message = refusal.value.message
    assert "2026-06-15" in message, "отказ не назвал дату, которую человек ввёл"
    assert "2026-06-30" in message, "отказ не назвал границу, позже которой можно"
    assert "озьмите дату" in message, "отказ перестал советовать выполнимое действие"


def test_the_undated_refusal_is_silent_when_no_month_is_closed(monkeypatch):
    """Закрытых месяцев нет — правится свободно, и это тоже надо проверить.

    Иначе отказ, написанный «на всякий случай», запретил бы менять схему группы
    у партнёра, который ещё ни одного месяца не утверждал, — то есть у каждого
    нового.
    """
    from web import directory

    monkeypatch.setattr(directory, "closed_through", lambda tenant_id: None)
    directory.refuse_if_unversioned_touches_closed_month(None, "схема расчёта группы")
