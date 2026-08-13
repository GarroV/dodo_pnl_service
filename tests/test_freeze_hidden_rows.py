"""Заморозка строки, расчёта которой роли не отдали (T101).

Что было. `POST /payslips/<id>/freeze/` по строке внутреннего регистра под
бухгалтером отвечал `302 ?froze=1`, а по случайному uuid — `404`. Три следствия,
и каждое отдельное:

* **запись по невидимой строке проходила** — официальная роль молча помечала
  спорной строку, ни одного числа которой ей не показывают;
* **пара «302 против 404» была оракулом**: ответ маршрута зависел от того, есть
  ли строка, а не от того, вправе ли роль её трогать;
* спорная строка **исключается из переноса разницы** (T026), то есть отметка
  меняла деньги в чужом регистре.

Что проверяется здесь и почему именно так.

**Правило — свойство роли, а не строки.** Морозить строку вправе тот, кому база
отдаёт **весь** её расчёт, то есть кому видны все регистры учёта (тот же
`app_sees_every_ledger()`, на котором стоит видимость итогов после T071).
Правило по содержимому строки («можно морозить, если все её регистры видны»)
здесь не годится и проверяется отдельным тестом: оно само выдаёт поимённый
список — по тому, на какой строке ответ меняется.

**Ответ обязан быть одинаков буквально.** Не «оба отказ», а тот же код, то же
тело, те же заголовки: разница в один байт и есть оракул. Поэтому ответы
сравниваются целиком.

**Ролью `app_user`.** Проверки уровня базы идут через `as_app_user`, страничные —
через `client`, а он ходит тем же путём продукта (`set local role app_user` в
`web/dbcontext.py`). Владелец схемы в этой базе суперпользователь: под ним
зелено и при снятых политиках.
"""
from __future__ import annotations

import re
import uuid
from contextlib import nullcontext

import pytest

from conftest import (
    JUNE,
    T1,
    U_NS1,
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
    body,
    login_as,
    narrowed_ledgers,
    period_url,
    wipe_payruns,
)
from test_payrun_lifecycle import payrun_in, rejected
from test_payslip_freezing import REASON, make_payslip

RANDOM_ID = "00000000-dead-4bee-8000-000000000001"

# Регистры, до которых на время части проверок ниже сужается бухгалтер.
# После D036 у неё в сиде полный набор регистров, а часть проверок этого
# файла написана про роль «право `payslip.freeze` есть, а регистры не все» —
# такой роли в сиде больше нет (у управляющего регистры узкие, но нет самого
# права — см. `test_the_right_still_matters_on_its_own`). Значение то же, что
# в образце из T101 (`test_payrun.py::test_the_screen_explains_the_refusal_
# when_the_role_misses_a_ledger`), поэтому условие узнаваемо на весь модуль.
NARROW_LEDGERS = ["official"]


def narrow_accountant(conn) -> None:
    """Сузить регистры бухгалтера прямо в транзакции теста уровня `db`.

    `db` подключается владельцем схемы, и `_seed()` заводит роли той же
    транзакцией — правка идёт тем же путём и откатится вместе с ней, когда
    фикстура сделает `conn.rollback()`. Восстанавливать значение вручную не
    нужно (в отличие от `narrowed_ledgers` из conftest, которая правит общую
    базу веб-тестов по DSN и обязана вернуть набор сама).
    """
    conn.execute(
        "update roles set visible_ledgers = %s::text[]::ledger[]"
        " where tenant_id = %s and code = 'accountant'",
        (NARROW_LEDGERS, T1),
    )


def internal_payslip(conn, payrun_id: str) -> str:
    """Строка ведомости, ни одного компонента которой бухгалтер не видит."""
    payslip = make_payslip(conn, payrun_id, "freeze-hidden", U_NS1, "1000.00")
    conn.execute("delete from pay_components where payslip_id = %s", (payslip,))
    conn.execute(
        """insert into pay_components (tenant_id, payslip_id, code, title, amount, ledger)
           values (%s, %s, 'hours.regular', 'Часы', '1000.00', 'internal')""",
        (T1, payslip),
    )
    return payslip


@pytest.fixture
def two_rows(db):
    """Официальная и внутренняя строки одного расчёта — материал почти всех проверок."""
    payrun_id = payrun_in(db, "calculated")
    return {
        "payrun": payrun_id,
        "official": make_payslip(db, payrun_id, "freeze-official", U_NS1, "1000.00"),
        "internal": internal_payslip(db, payrun_id),
    }


def freeze_attempt(conn, payslip_id: str):
    return rejected(
        conn,
        """insert into payslip_freezes (tenant_id, payslip_id, reason)
           values (%s, %s, %s)""",
        (T1, payslip_id, REASON),
    )


# =============================================================================
# 1. Уровень базы: ролью app_user
# =============================================================================


def test_a_role_without_every_ledger_does_not_freeze_a_hidden_row(two_rows, db):
    """Главная дырка задачи: запись по строке, расчёта которой роли не отдали.

    Взят бухгалтер с сужённым набором (см. `NARROW_LEDGERS`), а не управляющий:
    у управляющего нет самого права `payslip.freeze`, и его отказ доказывал бы
    не то — он бы срабатывал на permissive-политике права, до restrictive-
    политики регистров (`payslip_freeze_whole_run_insert`), которую здесь и
    нужно проверить.
    """
    narrow_accountant(db)
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        error = freeze_attempt(conn, two_rows["internal"])
        assert "row-level security" in str(error)
        assert conn.execute(
            "select count(*) from payslip_freezes where payslip_id = %s",
            (two_rows["internal"],),
        ).fetchone()[0] == 0


def test_the_same_role_does_not_freeze_a_visible_row_either(two_rows, db):
    """Правило — свойство роли, а не строки, и это не придирка к формулировке.

    Разреши бухгалтеру морозить строку, все регистры которой ей видны, — и
    маршрут начнёт отвечать по-разному на официальную и внутреннюю строку. Это
    и есть поимённый список: перебирать ничего не нужно, достаточно сравнить
    два ответа. Поэтому отказ одинаков для любой строки.

    Бухгалтер сужена так же, как выше: право у неё есть, отказывает регистр.
    """
    narrow_accountant(db)
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        error = freeze_attempt(conn, two_rows["official"])
        assert "row-level security" in str(error)


def test_the_refusal_does_not_depend_on_what_is_in_the_row(two_rows, db):
    """Два отказа обязаны быть неотличимы: разница в тексте и есть утечка."""
    narrow_accountant(db)
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        hidden = str(freeze_attempt(conn, two_rows["internal"]))
        shown = str(freeze_attempt(conn, two_rows["official"]))

    assert hidden == shown, f"по строкам разных регистров разные отказы:\n{hidden}\n{shown}"


def test_releasing_is_refused_the_same_way(two_rows, db):
    """Снятие — та же запись. Отказ громкий, а не тихое «изменено 0 строк»."""
    from test_payslip_freezing import freeze

    freeze(db, two_rows["internal"])
    narrow_accountant(db)
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        error = rejected(
            conn,
            """update payslip_freezes set released_at = now()
                where payslip_id = %s and released_at is null""",
            (two_rows["internal"],),
        )
        assert "row-level security" in str(error)
    assert db.execute(
        "select released_at from payslip_freezes where payslip_id = %s",
        (two_rows["internal"],),
    ).fetchone()[0] is None


def test_the_director_still_freezes_and_releases(two_rows, db):
    """T027 для того, кому положено: у роли со всеми регистрами ничего не изменилось."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        conn.execute(
            """insert into payslip_freezes (tenant_id, payslip_id, reason, frozen_by)
               values (%s, %s, %s, %s)""",
            (T1, two_rows["internal"], REASON, USER_DIRECTOR),
        )
        assert conn.execute(
            """update payslip_freezes set released_at = now(), released_by = %s
                where payslip_id = %s and released_at is null""",
            (USER_DIRECTOR, two_rows["internal"]),
        ).rowcount == 1


def test_the_right_still_matters_on_its_own(two_rows, db):
    """Два условия независимы: у управляющего нет права, и это отдельный отказ."""
    with as_app_user(db, USER_MANAGER) as conn:
        assert "row-level security" in str(freeze_attempt(conn, two_rows["official"]))


def test_the_protection_is_the_policy_and_not_luck(two_rows, db):
    """Порча: без новых политик бухгалтер морозит внутреннюю строку снова.

    Без этого теста проверки выше зеленели бы и от того, что материал собран
    неудачно, — а не от того, что запрет работает.

    Важно сузить бухгалтера тем же способом: без этого её полный (после D036)
    набор регистров и так проходит `app_sees_every_ledger`, и вставка удалась
    бы независимо от политики — тест зеленел бы не по своей причине.
    """
    narrow_accountant(db)
    db.execute("drop policy payslip_freeze_whole_run_insert on payslip_freezes")
    try:
        with as_app_user(db, USER_ACCOUNTANT) as conn:
            conn.execute(
                """insert into payslip_freezes (tenant_id, payslip_id, reason, frozen_by)
                   values (%s, %s, %s, %s)""",
                (T1, two_rows["internal"], REASON, USER_ACCOUNTANT),
            )
            assert conn.execute(
                "select count(*) from payslip_freezes where payslip_id = %s",
                (two_rows["internal"],),
            ).fetchone()[0] == 1
    finally:
        db.execute("""
            create policy payslip_freeze_whole_run_insert on payslip_freezes
                as restrictive for insert
                with check (app_sees_every_ledger(tenant_id))
        """)


def test_the_freeze_still_holds_the_numbers_of_a_frozen_row(two_rows, db):
    """Сторож замороженной строки не должен ослабнуть от новых политик.

    Ловушка, ради которой тест написан: сторож читает `payslip_freezes`
    функцией `payslip_is_frozen()`. Дай новой политике `using`, и на площадке,
    где владелец схемы — обычная роль, функция перестала бы видеть заморозку
    глазами ограниченной роли: «не видно» превратилось бы в «не заморожено», и
    запрет снимался бы сам собой у того, кого он ограничивает.
    """
    from test_payslip_freezing import freeze

    # Строка официальная намеренно: компоненты внутренней бухгалтеру не видны
    # вовсе, её `update` не нашёл бы строки и до сторожа не дошёл — то есть
    # проверял бы не сторожа, а политику `pay_components`.
    freeze(db, two_rows["official"])
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        error = rejected(
            conn,
            "update pay_components set amount = 7 where payslip_id = %s",
            (two_rows["official"],),
        )
        assert "заморожена" in str(error)


# =============================================================================
# 2. Уровень страницы: тот же путь, которым ходит человек
# =============================================================================


@pytest.fixture
def calculated_june(client, web_env):
    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(period_url(client) + "calculate/", follow=True).status_code == 200
    return None


def hidden_payslip(period: str = JUNE) -> str:
    """Строка ведомости, у которой нет ни одного официального компонента.

    Спрашивается владельцем схемы напрямую: колонку `ledgers` роль приложения
    не читает вовсе (T065), и узнать её «как продукт» нельзя намеренно.
    """
    from django.db import connection

    with connection.cursor() as cursor:
        cursor.execute(
            """select p.id from payslips p
                 join payruns r on r.id = p.payrun_id
                where r.period = %s
                  and not (p.ledgers && '{official}'::ledger[])
                limit 1""",
            [period],
        )
        row = cursor.fetchone()
    assert row, "в расчёте сида нет строки без официальных компонентов"
    return str(row[0])


CSRF_TOKEN = re.compile(rb'name="csrfmiddlewaretoken" value="[^"]*"')


def shape(response, url: str) -> tuple:
    """Ответ целиком: код, тело и заголовки. Оракул прячется в любом из трёх.

    Из тела вычёркиваются ровно две вещи, и обе — не каналы утечки:

    * **токен формы**. С T099 человеческая страница «не найдено» рисует общую
      шапку, а в ней формы с `csrfmiddlewaretoken`; Django выдаёт его новой
      маскировкой на каждый ответ. То есть побайтово не совпадут даже два
      ответа на **один и тот же** запрос — сравнивать без этого нечего.
    * **сам запрошенный адрес**. Форма выбора языка кладёт текущий путь в
      `next`, чтобы вернуть человека туда, откуда он пришёл. Это строка,
      которую прислал сам клиент: она различается на любых двух разных
      адресах, включая два несуществующих, и о существовании строки не
      говорит ничего.

    Всё остальное сравнивается как есть: оракул прячется именно в мелочах.
    """
    body = CSRF_TOKEN.sub(b'name="csrfmiddlewaretoken" value="TOKEN"', response.content)
    return (
        response.status_code,
        body.replace(url.encode(), b"<REQUESTED-URL>"),
        sorted((k, v) for k, v in response.items() if k.lower() != "date"),
    )


def production_404(client, url: str, data: dict | None = None):
    """Тот же запрос, но страницей отказа площадки, а не отладочной.

    С `DEBUG=1` Django печатает в теле 404 сам запрошенный адрес — то есть
    строку, которую клиент прислал сам. Сравнивать такие тела побайтно
    бессмысленно: они разойдутся на любом двух разных адресах и проверка
    зеленела бы от подстановки, а не от устройства. Поэтому оракул проверяется
    на том ответе, который получает человек на площадке.
    """
    from django.test.utils import override_settings

    with override_settings(DEBUG=False):
        return client.post(url, data or {})


@pytest.mark.parametrize("user", ["accountant", "manager"])
@pytest.mark.parametrize("action", ["freeze", "release"])
def test_the_route_answers_a_hidden_row_exactly_as_a_random_id(
    client, calculated_june, web_env, action, user
):
    """Оракул существования проверяется буквально, а не «оба не 302».

    Два разных случая роли с неполным набором регистров нарочно проверяются
    оба: у управляющего (после D036 — единственная такая роль в сиде) права
    `payslip.freeze` вообще нет, а у бухгалтера оно есть — набор ей сужен
    здесь искусственно (`narrowed_ledgers`), чтобы получить второй случай:
    право есть, регистры не все. Маршрут (T101) сначала проверяет регистры
    роли и только потом ищет строку, поэтому оба случая обязаны прийти к
    одному и тому же `404` независимо от того, есть ли право.
    """
    context = (
        narrowed_ledgers(web_env, "accountant", ["official"])
        if user == "accountant" else nullcontext()
    )
    with context:
        login_as(client, user)
        hidden_url = f"/payslips/{hidden_payslip()}/{action}/"
        nothing_url = f"/payslips/{RANDOM_ID}/{action}/"
        hidden = production_404(client, hidden_url, {"reason": "спор"})
        nothing = production_404(client, nothing_url, {"reason": "спор"})

        assert hidden.status_code == 404, (
            f"{action}: по невидимой строке ответ {hidden.status_code}, а не 404"
        )
        assert shape(hidden, hidden_url) == shape(nothing, nothing_url), (
            f"{action}: ответы по существующей и несуществующей строке различимы"
        )


def test_the_route_answers_a_visible_row_the_same_way_too(client, calculated_june, web_env):
    """Роль либо морозит, либо нет: по своим строкам ответ не отличается.

    Строка выбрана в точности по набору бухгалтера ('{official}', см.
    `narrowed_ledgers` ниже): все её регистры роли видны, и всё равно ответ
    неотличим от несуществующей строки — правило свойство роли, а не строки.
    """
    from django.db import connection

    with narrowed_ledgers(web_env, "accountant", ["official"]):
        login_as(client, "accountant")
        with connection.cursor() as cursor:
            cursor.execute(
                """select p.id from payslips p
                     join payruns r on r.id = p.payrun_id
                    where r.period = %s and p.ledgers = '{official}'::ledger[] limit 1""",
                [JUNE],
            )
            visible = str(cursor.fetchone()[0])

        own_url = f"/payslips/{visible}/freeze/"
        nothing_url = f"/payslips/{RANDOM_ID}/freeze/"
        own = production_404(client, own_url, {"reason": "спор"})
        nothing = production_404(client, nothing_url, {"reason": "спор"})
        assert shape(own, own_url) == shape(nothing, nothing_url)


def test_nothing_was_written_by_all_those_attempts(client, calculated_june, web_env):
    """Отказ маршрута — не косметика: в базе не должно появиться ни одной заморозки.

    Бухгалтер сужена до официального: с полным (после D036) набором регистров
    она прошла бы проверку регистров и запись состоялась бы — тест проверял бы
    не то.
    """
    from django.db import connection

    with narrowed_ledgers(web_env, "accountant", ["official"]):
        login_as(client, "accountant")
        client.post(f"/payslips/{hidden_payslip()}/freeze/", {"reason": "спор"})

        with connection.cursor() as cursor:
            cursor.execute("select count(*) from payslip_freezes")
            assert cursor.fetchone()[0] == 0


def test_the_director_still_freezes_from_the_page(client, calculated_june):
    """Приёмка T027 не должна пострадать: у директора кнопка работает как прежде."""
    payslip = hidden_payslip()
    login_as(client, "director")
    froze = client.post(f"/payslips/{payslip}/freeze/", {"reason": "спорные часы"})
    assert froze.status_code == 302 and "froze=1" in froze["Location"]

    released = client.post(f"/payslips/{payslip}/release/")
    assert released.status_code == 302 and "released=1" in released["Location"]


def test_the_accountant_is_not_offered_a_button_she_cannot_use(client, calculated_june, web_env):
    """Кнопка, которая всегда отказывает, — не разграничение, а ловушка для человека.

    Взята бухгалтер с искусственно сужённым набором, а не управляющий: у него
    нет и права `payslip.freeze`, поэтому его отсутствующая кнопка ничего не
    говорит про регистры отдельно от права (та проверка — в
    `test_payslip_freezing.py::test_a_role_without_the_right_sees_no_button_
    but_an_explanation`). Здесь показано, что даже роли, у которой право
    есть, кнопку не покажут, если её регистры не все.
    """
    with narrowed_ledgers(web_env, "accountant", ["official"]):
        login_as(client, "accountant")
        html = body(client.get(period_url(client)))

        assert not re.search(r"/payslips/[0-9a-f-]+/freeze/", html), (
            "на странице бухгалтера стоит форма заморозки, которой ей нельзя пользоваться"
        )


def test_the_director_is_still_offered_the_button(client, calculated_june):
    """Обратная сторона: чинить видимость кнопки вырезанием кнопки нельзя."""
    login_as(client, "director")
    html = body(client.get(period_url(client)))

    assert re.search(r"/payslips/[0-9a-f-]+/freeze/", html), (
        "у директора пропала форма заморозки"
    )


def test_an_unknown_id_is_not_special_for_the_director_either(client, calculated_june):
    """У роли со всеми регистрами несуществующая строка отвечает как всегда — 404."""
    login_as(client, "director")
    assert client.post(
        f"/payslips/{uuid.uuid4()}/freeze/", {"reason": "спор"}
    ).status_code == 404
