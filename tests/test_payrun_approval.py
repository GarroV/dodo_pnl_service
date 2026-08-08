"""Утверждение периода и откат с причиной (T025).

Что здесь проверяется и почему именно так:

1. **Откат без причины невозможен не только в форме.** Требование стоит
   триггером базы, поэтому тесты бьют по базе напрямую — и отдельно проверяют,
   что отказ приходит даже владельцу таблиц. Проверка, которую обходит любой
   второй путь записи, гарантией не является.
2. **Право утверждать и право открывать — разные права.** Роль бухгалтера в
   фикстуре умеет считать и утверждать, но не умеет открывать: на ней и видно,
   что `period.reopen` действительно отдельное право, а не следствие «умею
   писать в расчёт».
3. **Пара «политика + внятный отказ».** У каждого запрета две стороны: база
   отвергает, интерфейс объясняет и не предлагает того, что запретит. Поэтому
   на каждое «нельзя» здесь два теста: по базе и по странице.

Тесты доступа ходят ролью `app_user` (`as_app_user`): владелец таблиц и
суперпользователь обходят RLS, и на этом в проекте уже прожил незамеченным
дефект видимости регистров.
"""
from __future__ import annotations

import pytest

from conftest import (
    USER_ACCOUNTANT,
    USER_DIRECTOR,
    USER_MANAGER,
    as_app_user,
    body,
    login_as,
    period_url,
    wipe_payruns,
)
from test_payrun_lifecycle import (
    journal,
    new_payrun,
    payrun_in,
    rejected,
    set_status,
    status_of,
)

REASON = "ошиблись в часах за третью неделю"


# --- сторона базы ------------------------------------------------------------


def reopen(conn, payrun_id: str, reason: str | None = None):
    """Откат так, как его делает приложение: причина — настройкой транзакции."""
    if reason is not None:
        conn.execute("select set_config('app.transition_reason', %s, true)", (reason,))
    conn.execute("update payruns set status = 'reopened' where id = %s", (payrun_id,))


def rejected_reopen(conn, payrun_id: str, reason: str | None = None):
    """Ожидаем отказ на откате. Транзакция остаётся рабочей — проверяем данные."""
    import psycopg

    with pytest.raises(psycopg.Error) as caught:
        with conn.transaction():
            reopen(conn, payrun_id, reason)
    return caught.value


def test_reopen_without_a_reason_is_rejected(db):
    """Главное правило задачи: открыть период молча нельзя."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        assert "причин" in str(rejected_reopen(conn, payrun_id))
        assert status_of(conn, payrun_id) == "approved"
        # Отказ пришёл до записи: в журнале ровно прежние три перехода.
        assert [row[1] for row in journal(conn, payrun_id)] == [
            "draft", "calculated", "approved",
        ]


def test_a_blank_reason_is_not_a_reason(db):
    """Пробелы вместо объяснения — то же самое, что пустота."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        assert "причин" in str(rejected_reopen(conn, payrun_id, "   "))
        assert status_of(conn, payrun_id) == "approved"


def test_reopen_with_a_reason_names_the_author_and_the_reason(db):
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        reopen(conn, payrun_id, REASON)

        assert status_of(conn, payrun_id) == "reopened"
        last = journal(conn, payrun_id)[-1]
        assert last[0] == "approved" and last[1] == "reopened"
        assert str(last[2]) == USER_DIRECTOR
        assert last[3] == REASON


def test_other_transitions_do_not_need_a_reason(db):
    """Причина обязательна там, где решение спорное, а не на каждом шаге."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = new_payrun(conn)
        set_status(conn, payrun_id, "calculated", "approved")
        assert status_of(conn, payrun_id) == "approved"


def test_the_reason_is_required_of_the_table_owner_too(db):
    """Требование держит триггер, а не политика: политику владелец обходит."""
    payrun_id = new_payrun(db)
    set_status(db, payrun_id, "calculated", "approved")

    assert "причин" in str(
        rejected(db, "update payruns set status = 'reopened' where id = %s", (payrun_id,))
    )
    assert status_of(db, payrun_id) == "approved"


def test_the_reason_does_not_leak_to_the_next_transition(db):
    """Причина одноразовая: пересчёт после отката не наследует чужое объяснение."""
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "approved")
        reopen(conn, payrun_id, REASON)
        set_status(conn, payrun_id, "calculated")

        reasons = {row[1]: row[3] for row in journal(conn, payrun_id)}
        assert reasons["reopened"] == REASON
        assert reasons["calculated"] is None


def test_approval_requires_the_right_to_approve(db):
    """У управляющего права утверждать нет — база отказывает, а не интерфейс.

    Расчёт доводит до нужного статуса директор: у управляющего нет и права
    считать, поэтому подготовка его руками падала бы на `insert`, не дойдя до
    проверяемого запрета — тест был бы зелёным не по той причине.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        payrun_id = payrun_in(conn, "calculated")

    with as_app_user(db, USER_MANAGER) as conn:
        error = rejected(
            conn, "update payruns set status = 'approved' where id = %s", (payrun_id,)
        )
        assert "row-level security" in str(error)
        assert status_of(conn, payrun_id) == "calculated"


def test_reopening_requires_its_own_right(db):
    """Бухгалтер умеет считать и утверждать, но открывать период — не его дело.

    Ровно на этой роли видно, что `period.reopen` — самостоятельное право:
    писать в расчёт она умеет, причину подставляет, и всё равно отказ.
    """
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        payrun_id = payrun_in(conn, "calculated")
        set_status(conn, payrun_id, "approved")  # утвердить бухгалтер вправе
        assert status_of(conn, payrun_id) == "approved"

        assert "row-level security" in str(rejected_reopen(conn, payrun_id, REASON))
        assert status_of(conn, payrun_id) == "approved"


def test_the_journal_of_an_approval_keeps_the_author(db):
    with as_app_user(db, USER_ACCOUNTANT) as conn:
        payrun_id = payrun_in(conn, "calculated")
        set_status(conn, payrun_id, "approved")
        last = journal(conn, payrun_id)[-1]
        assert last[1] == "approved" and str(last[2]) == USER_ACCOUNTANT


def test_the_author_name_is_visible_to_colleagues_only(db):
    """Имя автора отдаётся тем, с кем есть общий тенант, и никому больше.

    Из-за этого имя достаётся функцией, а не политикой на `users`: в самой
    строке лежат хэш пароля и почта, показывать их коллегам незачем.
    """
    from conftest import USER_OTHER

    # Учётки заводит владелец: роли приложения `insert` на `users` не выдан.
    db.execute(
        """insert into users (id, username, password, full_name) values
               (%s, 'director', 'x', 'Оперативный директор'),
               (%s, 'accountant', 'x', 'Бухгалтер')""",
        (USER_DIRECTOR, USER_ACCOUNTANT),
    )

    with as_app_user(db, USER_DIRECTOR) as conn:
        assert conn.execute(
            "select app_user_display_name(%s)", (USER_ACCOUNTANT,)
        ).fetchone()[0] == "Бухгалтер"

    with as_app_user(db, USER_OTHER) as conn:
        assert conn.execute(
            "select app_user_display_name(%s)", (USER_DIRECTOR,)
        ).fetchone()[0] is None


# --- сторона страницы --------------------------------------------------------
# Дальше живой Django на базе с сидом: то же самое глазами человека.


@pytest.fixture
def clean_payruns(web_env):
    wipe_payruns(web_env)
    yield web_env
    wipe_payruns(web_env)


HISTORY_HEADING = "История периода"


def history_block(page: str) -> str:
    """Только кусок страницы с историей — искать имя и причину надо в нём.

    Иначе проверка зелёная не по той причине: имя вошедшего стоит в шапке
    **каждой** страницы, и «имя есть где-то в HTML» проходит, даже если история
    автора не показывает вовсе. Найдено порчей: обнуление имени автора в
    `lifecycle.history` не покраснило ни одного теста.
    """
    assert HISTORY_HEADING in page, f"на странице нет истории:\n{page}"
    start = page.index(HISTORY_HEADING)
    end = page.find("<h2>", start)
    return page[start : end if end != -1 else len(page)]


def payrun_status(dsn: str) -> str:
    import psycopg

    with psycopg.connect(dsn) as conn:
        return conn.execute("select status from payruns").fetchone()[0]


def calculated_period(client, dsn: str) -> str:
    """Посчитанный период директором — исходное состояние для утверждения."""
    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", follow=True)
    assert payrun_status(dsn) == "calculated"
    return url


def test_the_approve_button_appears_only_after_the_calculation(client, clean_payruns):
    login_as(client, "director")
    url = period_url(client)
    assert "approve/" not in body(client.get(url))

    client.post(url + "calculate/", follow=True)
    assert "approve/" in body(client.get(url))


def test_the_director_approves_from_the_page(client, clean_payruns):
    url = calculated_period(client, clean_payruns)
    response = client.post(url + "approve/", follow=True)

    assert response.status_code == 200
    assert payrun_status(clean_payruns) == "approved"
    assert "Утверждён" in body(response)


def test_a_role_without_the_right_sees_no_button_but_an_explanation(client, clean_payruns):
    """Кнопка не пропадает молча: на её месте тот же текст, которым ответит отказ."""
    calculated_period(client, clean_payruns)
    login_as(client, "manager")

    page = body(client.get(period_url(client)))
    assert "approve/" not in page
    assert "Утверждение периода" in page


def test_approval_without_the_right_is_refused_past_the_interface(client, clean_payruns):
    url = calculated_period(client, clean_payruns)
    login_as(client, "manager")

    response = client.post(url + "approve/", follow=True)
    assert response.status_code == 403
    assert payrun_status(clean_payruns) == "calculated"


def test_reopening_without_a_reason_is_refused_by_the_page(client, clean_payruns):
    url = calculated_period(client, clean_payruns)
    client.post(url + "approve/", follow=True)

    response = client.post(url + "reopen/", {"reason": "   "}, follow=True)
    assert response.status_code == 400
    assert "причин" in body(response).lower()
    assert payrun_status(clean_payruns) == "approved"


def test_reopening_shows_the_reason_and_the_author_in_the_history(client, clean_payruns):
    url = calculated_period(client, clean_payruns)
    client.post(url + "approve/", follow=True)

    response = client.post(url + "reopen/", {"reason": REASON}, follow=True)
    assert response.status_code == 200
    assert payrun_status(clean_payruns) == "reopened"

    shown = history_block(body(response))
    assert REASON in shown
    # Автор — тот, кто нажал: в сиде имя учётки равно названию роли.
    assert "Оперативный директор" in shown
    assert "Открыт заново" in shown


def test_a_reopened_period_is_not_approved_again_without_a_recalculation(client, clean_payruns):
    """Открыли — пересчитайте. Иначе утверждение накрыло бы старые числа."""
    url = calculated_period(client, clean_payruns)
    client.post(url + "approve/", follow=True)
    client.post(url + "reopen/", {"reason": REASON}, follow=True)

    page = body(client.get(url))
    assert "approve/" not in page

    response = client.post(url + "approve/", follow=True)
    assert response.status_code == 409
    assert payrun_status(clean_payruns) == "reopened"


def test_the_reopen_button_belongs_to_the_reopen_right(client, clean_payruns):
    """У бухгалтера право утверждать есть, право открывать — нет."""
    url = calculated_period(client, clean_payruns)
    login_as(client, "accountant")

    assert "approve/" in body(client.get(url))
    client.post(url + "approve/", follow=True)
    assert payrun_status(clean_payruns) == "approved"

    page = body(client.get(url))
    assert "reopen/" not in page
    assert "Откат периода" in page

    response = client.post(url + "reopen/", {"reason": REASON}, follow=True)
    assert response.status_code == 403
    assert payrun_status(clean_payruns) == "approved"


def test_the_history_shows_every_step(client, clean_payruns):
    url = calculated_period(client, clean_payruns)
    client.post(url + "approve/", follow=True)

    shown = history_block(body(client.get(url)))
    # Именно в истории: «Утверждён» стоит и в сводке состояния, поэтому проверка
    # по всей странице прошла бы и с пустой таблицей.
    assert "Посчитан" in shown and "Утверждён" in shown
    assert "Оперативный директор" in shown
