"""Точки человека заводятся с экрана, а не в базе (T210, D055).

Связь «человек → точки» появилась в T197 и уже делит ФОТ: `payrun.posting`
берёт все точки, действующие в месяце, и раскладывает по ним стоимость
человека. Завести её было можно только SQL-ом — то есть партнёр без
разработчика управляющего на две пиццерии не оформит, а это ровно та ручная
работа, ради избавления от которой продукт и пишется.

Что проверяется здесь и почему именно это.

**Набор пишется целиком и с даты.** Управляющий на двух точках — не «две
записи, заведённые по одной»: половина набора означает половину денег не там.
Поэтому проверяется не «строка появилась», а что перевод закрывает прежний
набор датой и оставляет его в истории: закрытый месяц обязан остаться
разнесённым по тем точкам, которые были у человека тогда (D020).

**Форма не снимает того, о чём её не просили.** Галки приходят отмеченными по
действующему набору. Пустая форма означала бы «снять со всех точек», и тот, кто
пришёл добавить вторую пиццерию, снял бы первую — и узнал бы об этом из P&L
следующего месяца.

**Половина долей заданных, половина пустых — отвергается вводом.** Пустая доля
идёт в расчёт весом 1, поэтому 0,7 рядом с пустой даёт не 70 % и 30 %, а 41 % и
59 %: числа, которых никто не вводил.

**Право проверяется базой, ролью `app_user`.** На владельца схемы политики не
действуют, и зелёный результат владельцем не значил бы ничего. Заодно
проверяется срез по точкам (`0267`): управляющий не должен узнать из карточки
своего человека, на каких ЕЩЁ точках тот работает (D023).

**И главное — дорога до денег.** Заведённый экраном набор обязан делить ФОТ в
расчёте. Без этой проверки всё остальное доказывало бы, что таблица
записывается, а не что она чем-то управляет.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import as_app_user, body, login_as

LIST = "/directory/employees/"


@pytest.fixture
def sql(web_env):
    """Прямое соединение владельцем — только чтобы готовить и убирать состояние."""
    import psycopg

    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def rls(web_env):
    """Соединение для проверок доступа: своя транзакция, роль `app_user`.

    Отдельно от `sql`: `set local role` действует до конца транзакции, а в
    `autocommit` каждый оператор — своя транзакция, и проверка молча пошла бы
    владельцем схемы.
    """
    import psycopg

    with psycopg.connect(web_env) as conn:
        yield conn
        conn.rollback()


@pytest.fixture(autouse=True)
def units_of_people_removed(sql):
    """Привязки к точкам не переживают тест.

    Та же уборка, что в `test_payroll_across_units`, и по той же причине:
    оставленная строка меняет расчёт у соседних файлов — ведомость начинает
    делиться по точкам там, где тест этого не заводил. По отдельности каждый
    файл при этом зелёный, вместе — нет.
    """
    yield
    sql.execute("delete from employee_units")


def somebody(sql) -> tuple:
    """Человек справочника, чью карточку открываем. Первый по ключу — чтобы не гадать."""
    return sql.execute(
        "select id, external_id from employees order by external_id limit 1"
    ).fetchone()


def unit(sql, code: str) -> str:
    return str(sql.execute("select id from units where code = %s", (code,)).fetchone()[0])


def bindings(sql, employee_id) -> list[tuple]:
    """Что лежит в базе: точка, доля, с какого дня, по какой."""
    return sql.execute(
        """select u.code, eu.share, eu.valid_from, eu.valid_to
             from employee_units eu join units u on u.id = eu.unit_id
            where eu.employee_id = %s
            order by eu.valid_from, u.code""",
        (employee_id,),
    ).fetchall()


def card(client, employee_id) -> str:
    return body(client.get(f"{LIST}{employee_id}/"))


# --- 1. Набор точек заводится с экрана ----------------------------------------


def test_a_manager_of_two_pizzerias_is_bound_from_the_screen(client, web_env, sql):
    """Главная проверка задачи: две точки заводятся формой, без единой строки SQL."""
    person, _ext = somebody(sql)
    login_as(client, "admin")

    answer = client.post(f"{LIST}{person}/", {
        "what": "units",
        "units_from": "2026-06-01",
        "units": [unit(sql, "BG1"), unit(sql, "NS1")],
    })
    assert answer.status_code == 302, body(answer)

    rows = bindings(sql, person)
    assert [row[0] for row in rows] == ["BG1", "NS1"], f"точки не завелись: {rows}"
    assert all(row[1] is None for row in rows), (
        f"доли записаны, хотя их не вводили — умолчание «поровну» потеряно: {rows}"
    )
    assert all(row[2] == date(2026, 6, 1) and row[3] is None for row in rows), rows
    client.post("/logout/")


def test_the_shares_of_the_units_are_saved_as_typed(client, web_env, sql):
    """Заданные доли доезжают до базы: 0,7 и 0,3 — это 70 % и 30 %, а не поровну."""
    person, _ext = somebody(sql)
    bg1, ns1 = unit(sql, "BG1"), unit(sql, "NS1")
    login_as(client, "admin")

    answer = client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-06-01", "units": [bg1, ns1],
        f"share_{bg1}": "0,7", f"share_{ns1}": "0,3",
    })
    assert answer.status_code == 302, body(answer)

    shares = {code: share for code, share, _from, _to in bindings(sql, person)}
    assert shares == {"BG1": Decimal("0.700000"), "NS1": Decimal("0.300000")}, shares
    client.post("/logout/")


def test_an_empty_set_means_the_whole_network_and_says_so(client, web_env, sql):
    """Ни одной точки — это офис, и продукт называет это словами.

    Молчание здесь читалось бы как «точки не сохранились»: человек нажал
    «Сохранить» и не увидел ничего. Владелец про офис сказал прямо: «офис на
    всех работает, вне зависимости».
    """
    person, _ext = somebody(sql)
    login_as(client, "admin")
    client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-01-01", "units": [unit(sql, "BG1")],
    })

    answer = client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-06-01", "units": [],
    }, follow=True)
    page = body(answer)
    assert "на всю сеть" in page, page[:600]

    now = [row for row in bindings(sql, person) if row[3] is None]
    assert not now, f"после снятия со всех точек действующая привязка осталась: {now}"
    client.post("/logout/")


# --- 2. Версии: перевод не переписывает прошлое --------------------------------


def test_a_transfer_closes_the_previous_set_instead_of_rewriting_it(client, web_env, sql):
    """Перевод в сентябре не меняет июньского разнесения (D020, D055).

    Именно то, ради чего связь версионируется. Переписанная строка означала бы,
    что P&L июня, посчитанный в июне, и он же, посчитанный в октябре, — разные
    отчёты.
    """
    person, _ext = somebody(sql)
    login_as(client, "admin")

    client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-01-01", "units": [unit(sql, "BG1")],
    })
    client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-09-01", "units": [unit(sql, "NS1")],
    })

    rows = bindings(sql, person)
    assert rows == [
        ("BG1", None, date(2026, 1, 1), date(2026, 9, 1)),
        ("NS1", None, date(2026, 9, 1), None),
    ], f"перевод переписал прошлое вместо новой версии: {rows}"
    client.post("/logout/")


def test_the_same_set_does_not_breed_a_version(client, web_env, sql):
    """Ничего не изменилось — версия не заводится, и об этом сказано.

    «Сохранено» на пустой правке означало бы историю из одинаковых строк, в
    которой не найти настоящий перевод.
    """
    person, _ext = somebody(sql)
    login_as(client, "admin")
    form = {"what": "units", "units_from": "2026-06-01", "units": [unit(sql, "BG1")]}

    client.post(f"{LIST}{person}/", form)
    answer = client.post(f"{LIST}{person}/", form, follow=True)

    assert "не изменился" in body(answer), body(answer)[:600]
    assert len(bindings(sql, person)) == 1, bindings(sql, person)
    client.post("/logout/")


def test_a_set_typed_backwards_does_not_swallow_a_future_transfer(client, web_env, sql):
    """Правка задним числом не отменяет молча уже назначенного перевода.

    Человека перевели с сентября; потом задним числом поправили май. Новый набор
    обязан кончиться там, где начинается сентябрьский, — иначе сентябрьский
    перевод перестал бы существовать, и никто бы об этом не узнал.
    """
    person, _ext = somebody(sql)
    login_as(client, "admin")

    client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-01-01", "units": [unit(sql, "BG1")],
    })
    client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-09-01", "units": [unit(sql, "NS1")],
    })
    client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-05-01",
        "units": [unit(sql, "BG1"), unit(sql, "NS2")],
    })

    rows = bindings(sql, person)
    assert ("NS1", None, date(2026, 9, 1), None) in rows, (
        f"сентябрьский перевод исчез после правки задним числом: {rows}"
    )
    assert all(row[3] == date(2026, 9, 1) for row in rows if row[2] == date(2026, 5, 1)), (
        f"майский набор переехал через сентябрьскую границу: {rows}"
    )
    client.post("/logout/")


# --- 3. Форма не решает за человека -------------------------------------------


def test_the_form_comes_checked_by_what_is_effective_now(client, web_env, sql):
    """Галки отмечены действующим набором — иначе правка снимает то, о чём не просили.

    Набор пишется целиком. Форма с пустыми галками означала бы «снять со всех
    точек», и человек, пришедший добавить вторую пиццерию, потерял бы первую.
    """
    person, _ext = somebody(sql)
    bg1 = unit(sql, "BG1")
    login_as(client, "admin")
    client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2020-01-01", "units": [bg1],
    })

    page = card(client, person)
    # Тег целиком, а не строка разметки: атрибуты переносятся, и проверка «в
    # одной строке есть и value, и checked» была бы про вёрстку, а не про то,
    # что браузер отметит галку.
    import re

    tag = re.search(
        r"<input[^>]*name=\"units\"[^>]*value=\"%s\"[^>]*>" % re.escape(bg1), page,
    )
    assert tag and "checked" in tag.group(0), (
        f"действующая точка пришла в форму неотмеченной: {tag.group(0) if tag else page[-2000:]}"
    )
    client.post("/logout/")


def test_half_typed_shares_are_refused_with_the_real_arithmetic(client, web_env, sql):
    """Доля у одной точки и пустая у другой — отказ, а не «как-нибудь посчитается».

    Пустая доля идёт в расчёт весом 1 (`payrun.posting._shares`). Рядом с 0,7
    это 41 % и 59 %, а не 70 % и 30 %: разошлось бы молча — в P&L двух пиццерий.
    """
    person, _ext = somebody(sql)
    bg1, ns1 = unit(sql, "BG1"), unit(sql, "NS1")
    login_as(client, "admin")

    answer = client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-06-01", "units": [bg1, ns1],
        f"share_{bg1}": "0,7",
    })
    assert answer.status_code == 400, answer.status_code
    assert "либо у всех" in body(answer), body(answer)[:800]
    assert not bindings(sql, person), "отвергнутая форма записала половину набора"
    client.post("/logout/")


def test_a_zero_share_is_refused(client, web_env, sql):
    """Нулевая доля — точка, которая не получит ничего, и человек об этом не узнает."""
    person, _ext = somebody(sql)
    bg1, ns1 = unit(sql, "BG1"), unit(sql, "NS1")
    login_as(client, "admin")

    answer = client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-06-01", "units": [bg1, ns1],
        f"share_{bg1}": "0", f"share_{ns1}": "1",
    })
    assert answer.status_code == 400, answer.status_code
    assert "больше нуля" in body(answer), body(answer)[:800]
    client.post("/logout/")


def test_the_empty_card_tells_where_the_money_goes_today(client, web_env, sql):
    """Пустое состояние блока называет точку из условий найма, а не молчит.

    Без набора деньги не «никуда» идут: `payrun.posting._shares` кладёт их на
    точку ведомости, то есть на ту, что стоит в условиях найма. Сказать «точек
    нет» и замолчать значило бы оставить человека гадать.
    """
    person, _ext = sql.execute(
        """select e.id, u.code from employees e
             join employment_terms t on t.employee_id = e.id
             join units u on u.id = t.unit_id
            order by e.external_id limit 1"""
    ).fetchone()
    login_as(client, "admin")
    page = card(client, person)
    assert "Точки не заданы" in page, page[-2500:]
    assert "условиях найма" in page, page[-2500:]
    client.post("/logout/")


# --- 4. Закрытый месяц: правду говорим до правки и после -----------------------


def test_the_screen_does_not_promise_a_delta_that_will_never_come(client, web_env, sql):
    """Слова про утверждённый месяц — свои, а не те, что у условий найма.

    У ставки разница едет вперёд помеченной строкой (`payrun.retro`). У точек её
    не бывает вовсе: разнесение утверждённого месяца выполнено при утверждении и
    заморожено сторожем фактов. Обещать дельту значило бы отправить человека
    искать строку, которой не будет.
    """
    person, _ext = somebody(sql)
    tenant = sql.execute(
        "select tenant_id from employees where id = %s", (person,),
    ).fetchone()[0]
    # Утверждённый месяц берётся свой (май) и убирается за собой: июнь считают
    # соседние модули, и оставленный им утверждённым расчёт ломает их вдалеке
    # отсюда. Статусы проходятся по одному — сторож `payrun_guard` не пускает
    # завести расчёт сразу утверждённым, и это правильно.
    payrun = sql.execute(
        "insert into payruns (tenant_id, period) values (%s, '2026-05-01') returning id",
        (tenant,),
    ).fetchone()[0]
    try:
        for status in ("calculated", "approved"):
            sql.execute(
                "update payruns set status = %s where id = %s", (status, payrun),
            )
        login_as(client, "admin")
        page = card(client, person)
        assert "не переедет" in page, page[-3000:]
        assert "разнесена по тем" in page, page[-3000:]

        answer = client.post(f"{LIST}{person}/", {
            "what": "units", "units_from": "2026-05-15", "units": [unit(sql, "BG1")],
        }, follow=True)
        # Спрашивается ПЛАШКА, а не страница целиком: слова про перенос разницы
        # на странице есть законно — они стоят у формы условий найма, где
        # разница действительно бывает. Проверка «нет на странице» ловила бы
        # соседний блок и была бы красной по чужой причине.
        import re

        said = re.search(r'<div class="ok">(.*?)</div>', body(answer), re.S)
        assert said, body(answer)[:900]
        told = said.group(1)
        assert "разнесение осталось" in told, told
        # И ни слова про перенос разницы: её здесь не будет.
        assert "помеченной строкой" not in told, told
        client.post("/logout/")
    finally:
        # Уйти из утверждённого можно только с причиной — так же, как это
        # делает продукт (D021). Причина настройкой транзакции, поэтому она
        # ставится в том же операторе, а `autocommit` тут не мешает: `false`
        # у `set_config` означает «на сеанс».
        sql.execute("select set_config('app.transition_reason', %s, false)",
                    ("уборка после проверки слов о разнесении",))
        sql.execute("update payruns set status = 'reopened' where id = %s", (payrun,))
        sql.execute("delete from payruns where id = %s", (payrun,))
        sql.execute("select set_config('app.transition_reason', '', false)")


# --- 5. Право и видимость — ролью `app_user`, а не глазами ---------------------


def test_a_role_without_the_right_cannot_bind_units(rls, sql):
    """Запись в `employee_units` закрыта правом `directory.manage` — проверено базой.

    Ролью `app_user`, а не владельцем схемы: на владельца политики не действуют,
    и зелёный результат ничего не значил бы. Именами людей, а не «первым
    держателем роли»: продукт умеет приглашать людей, и первым в выборке может
    оказаться кто угодно.
    """
    import psycopg

    person, _ext = somebody(sql)
    tenant, unit_id = sql.execute(
        "select tenant_id, (select id from units order by code limit 1) "
        "from employees where id = %s", (person,),
    ).fetchone()

    for login in ("manager", "accountant", "director"):
        user_id = sql.execute(
            "select id from users where username = %s", (login,),
        ).fetchone()
        if user_id is None:
            continue
        with as_app_user(rls, str(user_id[0])):
            has_right = rls.execute(
                "select app_has_permission(%s, 'directory.manage')", (tenant,),
            ).fetchone()[0]
            if has_right:
                continue
            rls.execute("savepoint probe")
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                rls.execute(
                    "insert into employee_units (tenant_id, employee_id, unit_id, valid_from) "
                    "values (%s, %s, %s, '2026-06-01')",
                    (tenant, person, unit_id),
                )
            rls.execute("rollback to savepoint probe")


def test_a_manager_does_not_learn_about_foreign_units_from_the_card(rls, sql):
    """Управляющий точки не видит, на каких ЕЩЁ точках работает его человек (D023).

    Правило видимости точки одно на продукт (`app_unit_is_visible`), и с `0267`
    оно стоит и на этой таблице. Проверяется ролью `app_user`: владелец схемы
    политики обходит, и без переключения роли проверка была бы зелёной всегда.
    """
    person, _ext = sql.execute(
        """select e.id, e.external_id from employees e
             join employment_terms t on t.employee_id = e.id
             join units u on u.id = t.unit_id
            where u.code = 'NS1' order by e.external_id limit 1"""
    ).fetchone()
    tenant = sql.execute(
        "select tenant_id from employees where id = %s", (person,),
    ).fetchone()[0]
    for code in ("NS1", "BG1"):
        sql.execute(
            "insert into employee_units (tenant_id, employee_id, unit_id, valid_from) "
            "values (%s, %s, (select id from units where code = %s), '2020-01-01')",
            (tenant, person, code),
        )

    boss = sql.execute(
        """select m.user_id from memberships m
             join units u on u.id = any(m.unit_ids)
            where u.code = 'NS1' and m.unit_ids is not null limit 1"""
    ).fetchone()
    assert boss, "в сиде нет управляющего с ограниченным списком точек"

    # Предохранитель от зелёного по чужой причине: обе строки в базе есть.
    # Без него проверка зеленела бы и тогда, когда привязки просто не завелись.
    assert [row[0] for row in bindings(sql, person)] == ["BG1", "NS1"], bindings(sql, person)

    with as_app_user(rls, str(boss[0])):
        seen = [
            row[0] for row in rls.execute(
                """select u.code from employee_units eu join units u on u.id = eu.unit_id
                    where eu.employee_id = %s order by u.code""",
                (person,),
            ).fetchall()
        ]
    assert seen == ["NS1"], f"управляющему видны чужие точки его человека: {seen}"


def test_a_reader_gets_no_form_but_gets_the_words(client, web_env, sql):
    """У читателя формы набора нет, и на её месте объяснение, а не пустота (T072)."""
    person, _ext = sql.execute(
        """select e.id, e.external_id from employees e
             join employment_terms t on t.employee_id = e.id
             join units u on u.id = t.unit_id
            where u.code = 'NS1' order by e.external_id limit 1"""
    ).fetchone()
    login_as(client, "manager")
    page = card(client, person)
    assert "Набор точек с даты" not in page, "читателю показали форму записи"
    assert 'name="what" value="units"' not in page, "читателю показали форму записи"
    assert "делятся затраты" in page, "читателю не показали, между чем делятся деньги"
    client.post("/logout/")


# --- 6. Дорога до денег --------------------------------------------------------


def test_the_splitter_reads_exactly_what_the_screen_wrote(client, web_env, sql):
    """То, что набрано формой, деньги и делит — читает это `payrun.posting`.

    Проверяется стык, а не арифметика деления: как раскладываются копейки,
    доказано в `test_payroll_across_units`. Здесь доказывается другое — что
    экран пишет ровно те строки и с теми датами, которые расчёт потом найдёт.
    Без этой проверки всё остальное говорило бы, что таблица записывается, а не
    что она чем-то управляет.

    Заодно проверяется граница месяца, о которой экран говорит словами: набор
    берётся по состоянию на **первое число** считаемого месяца, поэтому
    заведённый серединой июня подействует с июля. Обещание в подсказке и
    поведение расчёта обязаны совпадать — иначе подсказка врёт увереннее, чем
    молчание.
    """
    from types import SimpleNamespace

    from payrun.posting import _units_of

    person, _ext = somebody(sql)
    tenant = sql.execute(
        "select tenant_id from employees where id = %s", (person,),
    ).fetchone()[0]
    june = SimpleNamespace(tenant_id=tenant, period=date(2026, 6, 1))
    july = SimpleNamespace(tenant_id=tenant, period=date(2026, 7, 1))

    login_as(client, "admin")
    assert client.post(f"{LIST}{person}/", {
        "what": "units", "units_from": "2026-06-15",
        "units": [unit(sql, "BG1"), unit(sql, "NS1")],
    }).status_code == 302

    assert str(person) not in {str(key) for key in _units_of(june)}, (
        "набор, заведённый серединой июня, попал в июньское деление — "
        "экран обещает обратное"
    )
    mine = _units_of(july).get(person) or _units_of(july).get(str(person)) or []
    assert len(mine) == 2, f"расчёт июля не увидел набора, заведённого экраном: {mine}"
    assert all(share is None for _unit_id, share in mine), (
        f"расчёту достались доли, которых никто не вводил: {mine}"
    )
    client.post("/logout/")


def test_the_database_refuses_two_overlapping_rows_of_one_unit(sql):
    """Одна точка не действует у человека дважды в один день.

    Пересечение портит деньги молча: `payrun.posting` берёт действующие строки
    списком, и каждая идёт отдельным весом — точка с двумя строками получила бы
    двойную долю ФОТ. Экран такого не наберёт, но гарантия обязана стоять на
    данных, а не на вежливости единственного пути записи.
    """
    import psycopg

    person, _ext = somebody(sql)
    tenant = sql.execute(
        "select tenant_id from employees where id = %s", (person,),
    ).fetchone()[0]
    bg1 = unit(sql, "BG1")
    sql.execute(
        "insert into employee_units (tenant_id, employee_id, unit_id, valid_from, valid_to) "
        "values (%s, %s, %s, '2026-01-01', '2026-12-01')", (tenant, person, bg1),
    )
    with pytest.raises(psycopg.errors.ExclusionViolation):
        sql.execute(
            "insert into employee_units (tenant_id, employee_id, unit_id, valid_from) "
            "values (%s, %s, %s, '2026-06-01')", (tenant, person, bg1),
        )
