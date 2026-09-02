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


def unit_option(html: str, code: str) -> str:
    """Идентификатор точки из выпадающего списка — по коду, а не по месту."""
    found = re.findall(r'<option value="([0-9a-f-]+)">([^<]+)</option>', html)
    for unit_id, label in found:
        if label.strip().split(" — ")[0] == code:
            return unit_id
    raise AssertionError(f"точки «{code}» нет в списке: {[n for _, n in found]}")


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
        "unit": unit_option(html, "NS1"),
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
    html = body(client.get("/roles/"))
    manager = role_option(html, "Управляющий точки")

    response = client.post("/roles/invite/", {
        "full_name": "Без причины", "email": "no-reason@example.test",
        "role": manager, "unit": unit_option(html, "NS1"), "reason": "",
    })
    assert response.status_code == 400
    assert "Причина" in body(response)
    assert "Без причины" not in body(client.get("/roles/"))


def test_a_person_is_not_invited_twice_by_the_same_mail(client, web_env):
    """Второй человек с той же почтой — это не второй человек."""
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    payload = {
        "full_name": "Дубль", "email": "dubl@example.test",
        "role": role_option(html, "Управляющий точки"),
        "unit": unit_option(html, "NS1"), "reason": "проверка",
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
    """Сценарий эталона: роль администратора бухгалтеру на время отпуска.

    Человек заводится свой, а не берётся из сида, — по тому же доводу, что
    расписан ниже. Смысл проверки в том, что выданная роль ОСТАЁТСЯ со сроком,
    поэтому снять её в конце нельзя: снятие проверяет соседняя. Достанься она
    сидовому бухгалтеру — он оставался бы администратором до конца прогона,
    потому что база у веб-проверок одна на весь.

    Так и было: `test_web_rules` видел у бухгалтера ведение правил, которого
    его роли не выдают ни при каких условиях, и краснел на ровном месте — но
    только в полном прогоне, а поодиночке был зелёным.
    """
    login_as(client, "admin")
    html = body(client.get("/roles/"))
    accountant = role_option(html, "Бухгалтер")
    admin_role = role_option(html, "Администратор сети")
    until = (date.today() + timedelta(days=30)).isoformat()

    client.post("/roles/invite/", {
        "full_name": "Jelena Nikolić", "email": "jelena@example.test",
        "role": accountant, "reason": "ведёт учёт партнёра",
    })
    html = body(client.get("/roles/"))
    user_id = re.search(
        r"/roles/people/([0-9a-f-]+)/", person_row(html, "Jelena Nikolić")
    ).group(1)

    response = client.post(f"/roles/people/{user_id}/", {
        "role": admin_role, "until": until, "reason": "Отпуск партнёра, нужно закрыть июнь",
    })
    assert response.status_code == 302

    html = body(client.get("/roles/"))
    assert "до " in person_row(html, "Jelena Nikolić"), "срок роли не показан рядом с ролью"
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
        "role": manager, "unit": unit_option(html, "NS1"), "reason": "принял точку",
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
        "role": manager, "unit": unit_option(html, "NS1"), "reason": "принял точку",
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


# =============================================================================
# Точка членства: роль на одну точку не должна открывать все (D031)
# =============================================================================
#
# `unit_ids is null` в функциях контекста (`0264`) означает ВСЕ точки тенанта.
# Экран ролей этого поля не заполнял вовсе, поэтому приглашённый управляющий
# получал кассы, наличные, табели и надбавки всего партнёра вместо своей точки —
# молча и вопреки форме роли, объявленной в `core/roles.py`.
#
# Проверки ниже смотрят В БАЗУ, а не на экран: разграничение делают функции
# контекста по этой самой колонке, и зелёная разметка о ней ничего не говорит.


def invited_membership(name: str):
    """Единственное членство приглашённого — по имени человека."""
    from core.models import Membership, User

    person = User.objects.get(full_name=name)
    return Membership.objects.get(user_id=person.pk)


def test_an_invited_manager_gets_only_the_chosen_unit(client, web_env):
    """Главная проверка задачи: у роли на одну точку членство названо точкой.

    Пустой `unit_ids` здесь — не «не заполнили», а «все точки партнёра». Разница
    невидима на экране и видна только в базе, поэтому проверяется база.
    """
    from core.models import Unit

    login_as(client, "admin")
    html = body(client.get("/roles/"))
    ns1 = Unit.objects.get(code="NS1")

    response = client.post("/roles/invite/", {
        "full_name": "Ana Marković", "email": "ana@example.test",
        "role": role_option(html, "Управляющий точки"),
        "unit": unit_option(html, "NS1"),
        "reason": "приняла точку",
    })
    assert response.status_code == 302

    held = invited_membership("Ana Marković")
    assert held.unit_ids == [ns1.id], (
        f"членство управляющего не названо точкой: {held.unit_ids} — "
        "пустой список означает ВСЕ точки партнёра"
    )


def test_a_role_of_one_unit_is_not_granted_without_a_unit(client, web_env):
    """Забыли выбрать точку — отказ словами, а не тихая выдача всех точек."""
    from core.models import User

    login_as(client, "admin")
    html = body(client.get("/roles/"))

    response = client.post("/roles/invite/", {
        "full_name": "Bez tačke", "email": "bez-tacke@example.test",
        "role": role_option(html, "Управляющий точки"), "reason": "проверка",
    })

    assert response.status_code == 400
    assert "точк" in body(response).lower(), "отказ не называет причину"
    assert not User.objects.filter(full_name="Bez tačke").exists(), (
        "человек всё-таки заведён — отказ оказался только на словах"
    )


def test_a_role_of_the_whole_partner_takes_no_unit(client, web_env):
    """Точка у роли, которая ведёт партнёра целиком, — противоречие, а не мелочь.

    Промолчать и выдать все точки значило бы сделать не то, что человек просил,
    и не сказать об этом; молча сузить до одной точки — тем более.
    """
    from core.models import User

    login_as(client, "admin")
    html = body(client.get("/roles/"))

    response = client.post("/roles/invite/", {
        "full_name": "Lišnja tačka", "email": "lisnja@example.test",
        "role": role_option(html, "Бухгалтер"),
        "unit": unit_option(html, "NS1"), "reason": "проверка",
    })

    assert response.status_code == 400
    assert not User.objects.filter(full_name="Lišnja tačka").exists()


def test_granting_a_unit_role_to_a_person_also_names_the_unit(client, web_env):
    """Второй путь выдачи — тот же самый, и дыра в нём была такая же.

    Роль выдают не только при заведении человека, но и потом, из строки списка.
    Проверять только приглашение значило бы закрыть одну дверь из двух.
    """
    from core.models import Membership, Unit, User

    login_as(client, "admin")
    html = body(client.get("/roles/"))
    bg1 = Unit.objects.get(code="BG1")

    client.post("/roles/invite/", {
        "full_name": "Nikola Ilić", "email": "nikola@example.test",
        "role": role_option(html, "Оперативный директор"), "reason": "ведёт партнёра",
    })
    person = User.objects.get(full_name="Nikola Ilić")

    response = client.post(f"/roles/people/{person.pk}/", {
        "role": role_option(body(client.get("/roles/")), "Управляющий точки"),
        "unit": unit_option(body(client.get("/roles/")), "BG1"),
        "reason": "подменяет управляющего",
    })
    assert response.status_code == 302

    granted = Membership.objects.get(
        user_id=person.pk, role__code="manager",
    )
    assert granted.unit_ids == [bg1.id], granted.unit_ids
