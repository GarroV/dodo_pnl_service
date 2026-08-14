"""Отказ читается как предложение, а не как обрывок (T140, issue #78).

Общий отказ по закрытому месяцу собран из подставляемого «чего именно» и
продолжения: `«%(what)s нельзя изменить: …»`. Плашка над формой при этом
подписана «Не сохранено.», и вместе выходило:

> Не сохранено. **с**хема расчёта и регистр группы нельзя изменить: …

Мелочь ровно до того момента, когда это единственное, что человек читает на
экране после неудачной правки: строчная буква сразу за точкой выглядит как
обрезанный текст, то есть как сбой продукта поверх отказа.

Чинится в одном месте — там, где текст собирается, — а не подстановкой заглавной
буквы у каждого вызова: вызовов сегодня два, завтра будет пять, и склонять
`what` по-разному в каждом никто не станет.
"""
from __future__ import annotations

import pytest

from conftest import body, login_as
from test_directory import approve_june, payruns_restored, sql  # noqa: F401
from test_directory_refusal_advice import official_group

# То, как продукт называет правимое в живых вызовах — оба они лежат в коде.
WHATS = ["схема расчёта и регистр группы", "строка P&L статьи расходов"]


@pytest.mark.parametrize("what", WHATS)
def test_the_refusal_starts_with_a_capital_letter(monkeypatch, what):
    """Первая буква — заглавная при любом «чего именно»."""
    from datetime import date

    from web import directory

    monkeypatch.setattr(directory, "closed_through", lambda tenant_id: date(2026, 6, 30))
    with pytest.raises(directory.DirectoryRefused) as refused:
        directory.refuse_if_unversioned_touches_closed_month(None, what)

    message = refused.value.message
    assert message[:1].isupper(), message
    assert not message.startswith(what), (
        "отказ по-прежнему начинается с подставленного «чего именно» — "
        "а оно пишется со строчной"
    )


@pytest.mark.parametrize("what", WHATS)
def test_the_refusal_still_says_what_and_which_month(monkeypatch, what):
    """Заглавная буква не должна стоить смысла: и «что», и месяц на месте."""
    from datetime import date

    from web import directory

    monkeypatch.setattr(directory, "closed_through", lambda tenant_id: date(2026, 6, 30))
    with pytest.raises(directory.DirectoryRefused) as refused:
        directory.refuse_if_unversioned_touches_closed_month(None, what)

    message = refused.value.message
    assert what in message, message
    assert "2026-06" in message, message
    assert "условиях найма" in message, "отказ перестал советовать выполнимое"


def test_the_page_shows_that_very_sentence_under_the_plaque(
    client, sql, web_env, payruns_restored,  # noqa: F811
):
    """Экран показывает ровно этот текст, а не свою копию.

    Проверяется на настоящей странице: плашка «Не сохранено.» и сразу за ней
    предложение с заглавной буквы. Без этой половины текст можно было бы
    починить в модуле и не заметить, что на экран едет что-то другое.
    """
    approve_june(client, web_env)
    client.post("/logout/")
    group_id, code, title, scheme, ledger = official_group(sql)

    login_as(client, "admin")
    try:
        refused = client.post(f"/directory/groups/{group_id}/", {
            "code": code, "title": title,
            "scheme": "half_time" if scheme != "half_time" else "standard",
            "ledger": ledger,
        })
    finally:
        client.post("/logout/")

    assert refused.status_code == 409
    html = body(refused)
    assert "Не сохранено." in html
    assert "Правку отклонили" in html, (
        "на экране не то предложение, которое собирает модуль справочников"
    )
    assert "схема расчёта и регистр группы нельзя изменить" in html.lower() or (
        "схема расчёта и регистр группы" in html
    )
