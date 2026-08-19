"""Экран ролей глазами человека (T171, T172; issue #77).

Проверки идут через настоящие страницы, а не через модель: смысл задачи в том,
что администратор **доходит** от отказа до выданного права, а не в том, что в
базе можно обновить строку.

Отдельно проверяется, что экран не превращается в обход разграничения: роль без
`roles.manage` не получает ни страницы, ни возможности что-то записать POST'ом
в обход интерфейса.
"""
from __future__ import annotations

import re

from conftest import body, login_as, period_url


def role_id(html: str, code: str) -> str:
    """Идентификатор роли по её коду, а не по месту в таблице.

    Первая версия теста брала первую форму на странице — и порядок ролей в базе
    оказался не тот, что казался: правка ушла бухгалтеру вместо администратора,
    а следующий тест получил бухгалтера с правом вести роли. Тест, который
    зависит от сортировки, врёт молча.
    """
    rows = re.findall(
        r'<td>[^<]+<br><span class="note">([a-z]+)</span>.*?action="/roles/([0-9a-f-]+)/rights/"',
        html,
        flags=re.S,
    )
    found = dict((c, i) for c, i in rows)
    assert code in found, f"роли {code} нет на экране: {sorted(found)}"
    return found[code]


def granted_rights(html: str, code: str) -> set[str]:
    """Какие права отмечены у роли — глазами страницы, а не базы."""
    block = re.search(
        r'<span class="note">' + code + r'</span>.*?</form>', html, flags=re.S,
    )
    assert block, f"на экране нет формы прав роли {code}"
    return set(re.findall(r'name="right:([a-z.]+)" checked', block.group(0).replace(">", "> ")))


def test_the_administrator_opens_roles(client, web_env):
    """У кого право — тот видит роли, права и людей."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    assert "Роли и права" in html
    assert "Расчёт периода" in html, "права показаны кодами, а не словами"
    assert "Бухгалтер" in html, "людей и их роли на экране нет"


def test_the_accountant_is_refused_in_words(client, web_env):
    """Кому не положено — отказ теми же словами, что и везде, а не пустой экран."""
    login_as(client, "accountant")
    response = client.get("/roles/")
    assert response.status_code == 403
    assert "не входит в права вашей роли" in body(response)


def test_the_navigation_offers_roles_only_to_the_one_who_leads_them(client, web_env):
    """Раздел, которого роль не ведёт, не предлагается в шапке."""
    login_as(client, "admin")
    assert 'href="/roles/"' in body(client.get("/periods/"))

    login_as(client, "accountant")
    assert 'href="/roles/"' not in body(client.get("/periods/"))


def test_the_administrator_grants_the_right_to_calculate(client, web_env):
    """Главный сценарий D047: право выдано с экрана — и человек им пользуется.

    Проверяется не «строка обновилась», а то, ради чего задача заведена:
    управляющий точки, которому расчёт был запрещён, после выдачи права
    перестаёт получать отказ. И право возвращается на место в конце — иначе
    следующий тест считал бы, что управляющий умеет считать всегда.
    """
    login_as(client, "admin")
    manager = role_id(body(client.get("/roles/")), "manager")

    login_as(client, "manager")
    before = body(client.get(period_url(client)))
    assert "не входит в права вашей роли" in before

    login_as(client, "admin")
    saved = client.post(
        f"/roles/{manager}/rights/",
        {
            "right:timesheet.edit": "on",
            "right:unit.close": "on",
            "right:payrun.calculate": "on",
        },
        follow=True,
    )
    assert "сохранены" in body(saved).lower()
    assert granted_rights(body(client.get("/roles/")), "manager") == {
        "timesheet.edit", "unit.close", "payrun.calculate",
    }

    login_as(client, "manager")
    after = body(client.get(period_url(client)))
    assert "Расчёт периода не входит в права вашей роли" not in after

    # Вернуть как было: набор прав управляющего — часть доводов D031 и D033, и
    # оставить его расширенным значило бы менять условия соседних проверок.
    login_as(client, "admin")
    client.post(
        f"/roles/{manager}/rights/",
        {"right:timesheet.edit": "on", "right:unit.close": "on"},
        follow=True,
    )


def test_a_role_of_another_partner_is_not_editable(client, web_env):
    """Чужая роль не правится даже прямым POST: адрес не защита."""
    login_as(client, "admin")
    response = client.post(
        "/roles/11111111-2222-3333-4444-555555555555/rights/",
        {"right:roles.manage": "on"},
    )
    assert response.status_code == 404


def test_the_accountant_cannot_grant_themselves_anything(client, web_env):
    """Тот же POST от того, кому нельзя, — отказ, а не тихое сохранение."""
    login_as(client, "admin")
    theirs = role_id(body(client.get("/roles/")), "accountant")

    login_as(client, "accountant")
    response = client.post(f"/roles/{theirs}/rights/", {"right:roles.manage": "on"})
    assert response.status_code == 403
    assert "не входит в права вашей роли" in body(response)


def test_the_accountant_becomes_an_administrator_too(client, web_env):
    """То, из-за чего всё началось (D047): вторая роль человеку.

    У партнёра бухгалтер часто ведёт весь проект — и тогда он администратор.
    Проверяется по последствию: после выдачи второй роли бухгалтеру открыт
    раздел, который до этого отвечал отказом, и первая роль при этом остаётся
    (иначе «вторая роль» была бы подменой первой).
    """
    login_as(client, "accountant")
    assert client.get("/roles/").status_code == 403

    login_as(client, "admin")
    html = body(client.get("/roles/"))
    admin_role = role_id(html, "admin")
    person = re.search(
        r'<td>[^<]*Бухгалтер[^<]*</td>.*?action="/roles/people/([0-9a-f-]+)/"', html, flags=re.S,
    )
    assert person, "бухгалтера нет в списке людей — некому выдавать роль"
    client.post(f"/roles/people/{person.group(1)}/", {"role": admin_role}, follow=True)

    login_as(client, "accountant")
    assert client.get("/roles/").status_code == 200, "вторая роль не подействовала"
    # Первая роль на месте: расчёт периода бухгалтеру по-прежнему открыт.
    assert "Расчёт периода не входит в права вашей роли" not in body(client.get(period_url(client)))

    # Убрать за собой: соседние проверки исходят из того, что бухгалтер ролями
    # не ведает.
    login_as(client, "admin")
    client.post(
        f"/roles/people/{person.group(1)}/",
        {"role": admin_role, "action": "remove"},
        follow=True,
    )
    login_as(client, "accountant")
    assert client.get("/roles/").status_code == 403, "роль не снялась"


def test_the_refusal_shows_the_way_out_to_the_one_who_leads_roles(client, web_env):
    """T172: администратору отказ говорит, что делать, а не «попросите кого-то».

    Именно на этой формулировке владелец и встал: он вошёл администратором,
    прочитал «попросите того, у кого это право есть» — и просить оказалось
    некого, потому что тот самый человек и есть он.
    """
    login_as(client, "admin")
    refusal = body(client.get(period_url(client)))
    assert "Расчёт периода не входит в права вашей роли" in refusal
    assert "откройте «Роли и права» и выдайте его" in refusal
    assert "Попросите того" not in refusal


def test_the_refusal_stays_plain_for_everyone_else(client, web_env):
    """А тому, кто роли не ведёт, обещать нечего: он права себе не выдаст."""
    login_as(client, "manager")
    refusal = body(client.get(period_url(client)))
    assert "Попросите того, у кого это право есть" in refusal
    assert "Роли и права" not in refusal
