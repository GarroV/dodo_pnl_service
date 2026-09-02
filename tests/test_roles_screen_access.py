"""Приглашение, срок и история доступов глазами человека (T188, issue #178).

До этой задачи учётка появлялась только сидом или руками в базе, роль выдавалась
навсегда и не оставляла следа. Эталон (модуль 11) требует ровно обратного:
человека приглашают из интерфейса, роль дают на срок с причиной, а история
выдач и снятий видна и не переписывается.

Проверки идут настоящими страницами: смысл задачи в том, что партнёр **доходит**
от пустого списка до заведённого человека с ролью, а не в том, что в базе можно
вставить строку.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

from conftest import body, login_as


def role_option(html: str, title: str) -> str:
    """Идентификатор роли из выпадающего списка выдачи — по названию, не по месту."""
    found = re.findall(r'<option value="([0-9a-f-]+)">([^<]+)</option>', html)
    for role_id, name in found:
        if name.strip() == title:
            return role_id
    raise AssertionError(f"роли «{title}» нет в списке: {[n for _, n in found]}")


def held_roles(html: str, name: str) -> str:
    """Только выданные роли человека — плашки, без выпадающего списка выдачи.

    Список выдачи стоит в той же строке и содержит названия ВСЕХ ролей: проверка
    «роли больше нет в строке» без этого отделения зелёной не бывает никогда.
    """
    row = person_row(html, name)
    return " ".join(re.findall(r'<span class="chip">.*?</span>', row, flags=re.S))


def person_row(html: str, name: str) -> str:
    """Строка человека, а не первая строка с таким текстом.

    Первая версия брала любой `<tr>` с этим словом — и попадала в таблицу
    ПРАВ, где ровно так же написано название роли «Бухгалтер». Тест, который
    зависит от того, что встретится раньше, врёт молча.
    """
    rows = [
        row for row in re.findall(r"<tr>.*?</tr>", html, flags=re.S)
        if "/roles/people/" in row
    ]
    for row in rows:
        if name in row:
            return row
    raise AssertionError(f"человека «{name}» нет в списке людей ({len(rows)} строк)")


def test_the_administrator_invites_a_person(client, web_env):
    """Человек заводится из интерфейса, сразу с ролью, и попадает в историю."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    manager = role_option(html, "Управляющий точки")

    response = client.post("/roles/invite/", {
        "full_name": "Jovana Kostić",
        "email": "jovana@example.test",
        "role": manager,
        "reason": "Новый управляющий вместо уволенного",
    })
    assert response.status_code == 302

    html = body(client.get("/roles/"))
    assert "Jovana Kostić" in html
    assert "Новый управляющий вместо уволенного" in html, "в истории нет причины"


def test_an_invitation_without_a_reason_is_refused_in_words(client, web_env):
    """Причина обязательна везде, как у отката периода: пустое «зачем» —
    это история, которая ни на что не отвечает."""
    login_as(client, "admin")
    manager = role_option(body(client.get("/roles/")), "Управляющий точки")

    response = client.post("/roles/invite/", {
        "full_name": "Без причины", "email": "no-reason@example.test",
        "role": manager, "reason": "",
    })
    assert response.status_code == 400
    assert "Причина" in body(response)
    assert "Без причины" not in body(client.get("/roles/"))


def test_a_person_is_not_invited_twice_by_the_same_mail(client, web_env):
    """Второй человек с той же почтой — это не второй человек."""
    login_as(client, "admin")
    manager = role_option(body(client.get("/roles/")), "Управляющий точки")
    payload = {
        "full_name": "Дубль", "email": "dubl@example.test",
        "role": manager, "reason": "проверка",
    }
    client.post("/roles/invite/", payload)
    response = client.post("/roles/invite/", payload)
    assert response.status_code == 409
    assert "уже" in body(response)


def test_only_the_one_who_leads_roles_invites(client, web_env):
    """Приглашение — это выдача доступа, и правом закрыто тем же самым."""
    login_as(client, "accountant")
    response = client.post("/roles/invite/", {
        "full_name": "Мимо", "email": "mimo@example.test",
        "role": "00000000-0000-0000-0000-000000000000", "reason": "проверка",
    })
    assert response.status_code == 403
    assert "не входит в права вашей роли" in body(response)


def test_a_role_is_granted_until_a_date_and_the_screen_says_so(client, web_env):
    """Сценарий эталона: роль администратора бухгалтеру на время отпуска."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    admin_role = role_option(html, "Администратор сети")
    person = person_row(html, "Бухгалтер")
    user_id = re.search(r"/roles/people/([0-9a-f-]+)/", person).group(1)
    until = (date.today() + timedelta(days=30)).isoformat()

    response = client.post(f"/roles/people/{user_id}/", {
        "role": admin_role, "until": until, "reason": "Отпуск партнёра, нужно закрыть июнь",
    })
    assert response.status_code == 302

    html = body(client.get("/roles/"))
    assert "до " in person_row(html, "Бухгалтер"), "срок роли не показан рядом с ролью"
    assert "Отпуск партнёра" in html, "в истории нет причины выдачи"


def test_a_role_cannot_be_granted_until_a_day_that_has_passed(client, web_env):
    """Роль «до вчера» — это доступ, которого не было ни секунды."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    admin_role = role_option(html, "Администратор сети")
    user_id = re.search(r"/roles/people/([0-9a-f-]+)/", person_row(html, "Бухгалтер")).group(1)

    response = client.post(f"/roles/people/{user_id}/", {
        "role": admin_role,
        "until": (date.today() - timedelta(days=1)).isoformat(),
        "reason": "проверка",
    })
    assert response.status_code == 400
    assert "уже прошёл" in body(response)


def test_revoking_a_role_records_who_and_why(client, web_env):
    """Снятие — такое же событие доступа, как выдача, и след у него такой же.

    Человек заводится тут же своим: база у веб-проверок одна на весь прогон, и
    проверка, опирающаяся на состояние, оставленное соседней, зелена ровно до
    того дня, когда соседнюю переименуют.
    """
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    manager = role_option(html, "Управляющий точки")
    director = role_option(html, "Оперативный директор")

    client.post("/roles/invite/", {
        "full_name": "Petar Petrović", "email": "petar@example.test",
        "role": manager, "reason": "принял точку",
    })
    html = body(client.get("/roles/"))
    user_id = re.search(
        r"/roles/people/([0-9a-f-]+)/", person_row(html, "Petar Petrović")
    ).group(1)

    client.post(f"/roles/people/{user_id}/", {
        "role": director, "reason": "нужен второй администратор",
    })
    revoked = client.post(f"/roles/people/{user_id}/", {
        "role": director, "action": "remove", "reason": "вернулся партнёр",
    })
    assert revoked.status_code == 302

    html = body(client.get("/roles/"))
    assert "вернулся партнёр" in html
    assert "нужен второй администратор" in html, "старая запись истории исчезла"
    assert "Оперативный директор" not in held_roles(html, "Petar Petrović"), (
        "роль снята, а на экране осталась"
    )


def test_a_role_is_not_revoked_without_a_reason(client, web_env):
    """Снятие без «зачем» оставляет в истории строку, которая ни о чём не говорит."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    manager = role_option(html, "Управляющий точки")
    director = role_option(html, "Оперативный директор")

    client.post("/roles/invite/", {
        "full_name": "Miloš Stojanović", "email": "milos@example.test",
        "role": manager, "reason": "принял точку",
    })
    html = body(client.get("/roles/"))
    user_id = re.search(
        r"/roles/people/([0-9a-f-]+)/", person_row(html, "Miloš Stojanović")
    ).group(1)
    client.post(f"/roles/people/{user_id}/", {"role": director, "reason": "временно"})

    response = client.post(f"/roles/people/{user_id}/", {
        "role": director, "action": "remove", "reason": "",
    })
    assert response.status_code == 400
    assert "Причина" in body(response)
    assert "Оперативный директор" in held_roles(
        body(client.get("/roles/")), "Miloš Stojanović"
    ), "роль снялась, хотя причину не назвали"


def test_an_expired_role_no_longer_opens_the_screen(client, web_env):
    """Срок кончился — и доступ закрылся сам, без единого действия человека.

    Берётся оперативный директор: своей роли ведения ролей у него нет, значит
    экран открывает ровно выданная роль и ничто больше. Сначала проверяется,
    что она его туда пускает, — иначе проверка была бы зелёной и при полностью
    сломанном разграничении.
    """
    from core.models import Membership, Role, User

    director = User.objects.get(username="director")
    admin_role = Role.objects.get(code="admin")
    assert not Membership.objects.filter(user_id=director.pk, role=admin_role).exists()

    login_as(client, "director")
    assert client.get("/roles/").status_code == 403, "директор и так ведёт роли — проверять нечего"

    held = Membership.objects.create(
        tenant_id=admin_role.tenant_id, user_id=director.pk, role=admin_role,
    )
    login_as(client, "director")
    assert client.get("/roles/").status_code == 200, "выданная роль не открыла экран"

    Membership.objects.filter(pk=held.pk).update(expires_at=date.today() - timedelta(days=1))
    login_as(client, "director")
    assert client.get("/roles/").status_code == 403, (
        "просроченная роль всё ещё пускает на экран ролей"
    )
