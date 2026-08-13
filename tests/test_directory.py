"""Админка справочников: экраны, права и версионирование (T018).

Три вещи, ради которых эти проверки написаны.

**Право `directory.manage` до сих пор не решало ничего.** Оно лежало в ролях с
самого начала (`seed_dev`, `conftest`), но экранов не было, и спросить его было
некому — миграция `0022` записала это прямо и оставила долг. Поэтому здесь
проверяется вся пара целиком: интерфейс не показывает того, что запретит, адрес
отвечает отказом словами, а база не даёт записать даже мимо интерфейса.

**Правка справочника не смеет двигать закрытый месяц.** Это главная проверка
задачи. Ведомость за утверждённый месяц уже на руках у людей, и если правка
ставки меняет то, как этот месяц пересчитается, — воспроизводимости нет, а
значит нет и сверки (D020, T026). Проверяется двумя способами сразу: строки
закрытого расчёта остаются байт в байт прежними **и** расчёт, собранный заново
на июнь, берёт по-прежнему июньскую версию условий.

Правку **с датой внутри** закрытого месяца справочник принимает (T121, D020):
закрытый месяц от неё не двигается, а разница едет вперёд помеченной строкой.
Этот путь целиком — в `test_retro_from_screen.py`; здесь остались правки,
которые закрытого месяца не касаются, и запрет на то, у чего версий по датам
нет вовсе (схема и регистр группы).

**Регистр, которого роль не видит, не называется даже в справочнике** (D023).
Группа несёт регистр учёта, и её строка называет его словом. Разграничение,
которое держится на том, в какой раздел человек не заглянул, — не разграничение.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as, period_url, wipe_payruns

# Роли без права вести справочники. Директор здесь не случайно: он может
# больше всех в расчёте — и справочники всё равно не его.
ROLES_WITHOUT_RIGHT = ["director", "accountant", "manager"]

DIRECTORY_URLS = [
    "/directory/",
    "/directory/employees/",
    "/directory/groups/",
    "/directory/units/",
    "/directory/legal-entities/",
    "/directory/calendar/",
]


def rows_of(conn, query: str, args=()) -> list:
    return conn.execute(query, args).fetchall()


@pytest.fixture
def sql(web_env):
    """Прямое соединение к базе стенда — суперпользователем, мимо политик.

    Нужно ровно затем, чтобы **готовить и восстанавливать** состояние, которого
    экраны не заводят (D029 запрещает заводить сотрудников экраном). Проверки
    доступа этим соединением не делаются: на владельца политики не действуют, и
    зелёный результат ничего не значил бы — см. `as_app_user` в `conftest`.
    """
    import psycopg

    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


# --- право на экран -----------------------------------------------------------


def test_only_the_role_that_manages_directories_sees_the_link(client):
    """Ссылка на справочники есть у администратора и больше ни у кого (T072)."""
    seen = {}
    for role in ["director", "accountant", "manager", "admin"]:
        login_as(client, role)
        seen[role] = 'href="/directory/"' in body(client.get("/periods/"))
        client.post("/logout/")

    assert seen == {"director": False, "accountant": False, "manager": False, "admin": True}


@pytest.mark.parametrize("role", ROLES_WITHOUT_RIGHT)
def test_the_directory_refuses_the_other_roles_in_words(client, role):
    """Адрес остаётся рабочим и отвечает отказом, а не пустотой и не 404.

    Прятать адрес значило бы завести третий контур доступа — в разметке, где его
    никто не проверит (то же решение, что в T072).
    """
    login_as(client, role)
    for url in DIRECTORY_URLS:
        answer = client.get(url)
        assert answer.status_code == 403, f"{role} {url}: {answer.status_code}"
        assert "Ведение справочников" in body(answer), f"{role} {url}: отказ без названия действия"
    client.post("/logout/")


def test_the_guest_is_sent_to_the_login(client):
    client.post("/logout/")
    answer = client.get("/directory/")
    assert answer.status_code == 302 and "/login/" in answer["Location"]


def test_the_admin_opens_every_directory(client):
    login_as(client, "admin")
    for url in DIRECTORY_URLS:
        answer = client.get(url)
        assert answer.status_code == 200, f"{url}: {answer.status_code}"
    client.post("/logout/")


def test_there_is_no_way_to_create_an_employee(client):
    """D029: карточки появляются из данных партнёра, а не заводятся экраном.

    Проверяется отсутствие и кнопки, и адреса: кнопка могла бы просто не
    попасть в разметку, а рабочий адрес остался бы — и тогда решение держалось
    бы на вёрстке.
    """
    from django.urls import NoReverseMatch, reverse

    login_as(client, "admin")
    html = body(client.get("/directory/employees/"))
    assert "/directory/employees/new/" not in html
    assert client.get("/directory/employees/new/").status_code == 404
    with pytest.raises(NoReverseMatch):
        reverse("directory-employee-new")
    client.post("/logout/")


# --- справочники ведутся ------------------------------------------------------


def test_a_new_legal_entity_appears_in_the_directory(client, sql):
    """Приёмка задачи в самом простом виде: справочник ведётся без базы руками."""
    login_as(client, "admin")
    answer = client.post(
        "/directory/legal-entities/new/",
        {"title": "Проверочное юрлицо", "tax_number": "123456789"},
    )
    assert answer.status_code == 302
    try:
        html = body(client.get("/directory/legal-entities/"))
        assert "Проверочное юрлицо" in html and "123456789" in html
    finally:
        sql.execute("delete from legal_entities where title = 'Проверочное юрлицо'")
        client.post("/logout/")


def test_editing_a_unit_saves_and_shows_up(client, sql):
    login_as(client, "admin")
    unit_id = sql.execute(
        "select id from units where code = 'NS2' and tenant_id in "
        "(select id from tenants where code = 'rs-dev')"
    ).fetchone()[0]
    before = sql.execute("select title from units where id = %s", (unit_id,)).fetchone()[0]
    try:
        answer = client.post(f"/directory/units/{unit_id}/", {
            "code": "NS2", "title": "Дунавска (проверка)", "legal_entity": "",
            "opened_at": "2023-01-01", "closed_at": "",
        })
        assert answer.status_code == 302
        assert "Дунавска (проверка)" in body(client.get("/directory/units/"))
    finally:
        sql.execute("update units set title = %s where id = %s", (before, unit_id))
        client.post("/logout/")


def test_a_wrong_date_is_explained_not_swallowed(client, sql):
    """Ошибка ввода объясняется словами, а введённое не пропадает."""
    login_as(client, "admin")
    unit_id = sql.execute(
        "select id from units where code = 'NS2' and tenant_id in "
        "(select id from tenants where code = 'rs-dev')"
    ).fetchone()[0]
    answer = client.post(f"/directory/units/{unit_id}/", {
        "code": "NS2", "title": "Дунавска", "legal_entity": "",
        "opened_at": "первое января", "closed_at": "",
    })
    assert answer.status_code == 200
    html = body(answer)
    assert "первое января" in html, "введённое пропало вместе с отказом"
    assert "2026-06-01" in html, "отказ не показал, как надо"
    client.post("/logout/")


def test_the_calendar_is_the_source_of_the_norm_on_the_period_page(client, sql, web_env):
    """Календарь ведётся экраном, и страница месяца берёт норму именно оттуда (T063).

    Самая ценная проверка календаря: она соединяет два конца — то, что
    администратор набрал на своём экране, и то, что бухгалтер прочитал на
    своём. Без неё «календарь сохранился» доказывало бы только запись в базу.
    """
    wipe_payruns(web_env)
    login_as(client, "admin")
    try:
        answer = client.post("/directory/calendar/2026-06/", {
            "norm_hours": "168", "working_days": "21",
        })
        assert answer.status_code == 302, body(answer)
        client.post("/logout/")

        login_as(client, "director")
        html = body(client.get(period_url(client)))
        assert "168,00" in html, "страница месяца показывает не то, что завели в календаре"
    finally:
        sql.execute(
            "update calendars set norm_hours = 176.00, working_days = 22 "
            "where country_code = 'RS' and period = '2026-06-01'"
        )
        client.post("/logout/")


def test_a_new_calendar_month_can_be_created(client, sql):
    login_as(client, "admin")
    try:
        answer = client.post("/directory/calendar/new/", {
            "month": "2027-03", "norm_hours": "184", "working_days": "23",
        })
        assert answer.status_code == 302, body(answer)
        assert "184,00" in body(client.get("/directory/calendar/"))
    finally:
        sql.execute("delete from calendars where period = '2027-03-01'")
        client.post("/logout/")


# --- версионирование ----------------------------------------------------------


def victim(sql) -> tuple:
    """Человек, на котором проверяется версионирование, и его текущая версия.

    Берётся тот, у кого есть строка в июньской ведомости и официальный регистр:
    закрытый месяц должен быть виден администратору, иначе проверка сравнивала
    бы пустоту с пустотой.
    """
    row = sql.execute(
        """select t.id, t.employee_id, t.valid_from, t.valid_to, t.base_rate, t.group_id
             from employment_terms t
             join employee_groups g on g.id = t.group_id
            where g.ledger = 'official'
              and t.tenant_id in (select id from tenants where code = 'rs-dev')
              and exists (select 1 from timesheets s where s.employee_id = t.employee_id)
            order by t.employee_id, t.valid_from
            limit 1"""
    ).fetchone()
    assert row is not None, "в сиде некого проверять: нет официального человека с табелем"
    return row


@pytest.fixture
def terms_restored(sql):
    """Условия найма стенда возвращаются как были — что бы тест ни завёл."""
    before = rows_of(
        sql,
        "select id, valid_from, valid_to, base_rate, group_id, unit_id, scheme, ledger "
        "from employment_terms",
    )
    yield
    keep = {row[0] for row in before}
    sql.execute(
        "delete from employment_terms where id <> all(%s::uuid[])", ([str(k) for k in keep],)
    )
    for row in before:
        sql.execute(
            "update employment_terms set valid_from = %s, valid_to = %s, base_rate = %s, "
            "group_id = %s, unit_id = %s, scheme = %s, ledger = %s where id = %s",
            (*row[1:], row[0]),
        )


def post_new_version(client, sql, employee_id, *, valid_from: str, rate: str):
    """Завести версию условий найма экраном — так же, как это делает человек."""
    current = sql.execute(
        "select group_id, unit_id, coefficient, scheme, ledger from employment_terms "
        "where employee_id = %s order by valid_from desc limit 1",
        (employee_id,),
    ).fetchone()
    group_id, unit_id, coefficient, scheme, ledger = current
    return client.post(f"/directory/employees/{employee_id}/", {
        "what": "terms",
        "valid_from": valid_from,
        "group": str(group_id),
        "unit": str(unit_id) if unit_id else "",
        "base_rate": rate,
        "coefficient": str(coefficient),
        "scheme": scheme or "",
        "ledger": ledger or "",
    })


def test_a_rate_change_makes_a_new_version_and_keeps_the_old(client, sql, terms_restored):
    """Правка ставки заводит версию, а не переписывает прошлую."""
    _, employee_id, valid_from, _to, rate, _group = victim(sql)
    login_as(client, "admin")

    answer = post_new_version(client, sql, employee_id, valid_from="2026-09-01", rate="999.0000")
    assert answer.status_code == 302, body(answer)

    versions = rows_of(
        sql,
        "select valid_from, valid_to, base_rate from employment_terms "
        "where employee_id = %s order by valid_from",
        (employee_id,),
    )
    assert len(versions) >= 2, f"новая версия не появилась: {versions}"
    old, new = versions[-2], versions[-1]
    assert old[2] == rate, "прежняя ставка переписана — истории больше нет"
    assert old[1] == date(2026, 9, 1), "прежняя версия не закрыта днём начала новой"
    assert new[2] == Decimal("999.0000")
    client.post("/logout/")


def test_nothing_changed_means_no_new_version(client, sql, terms_restored):
    """«Сохранить» без изменений не засоряет историю пустой версией."""
    _, employee_id, _from, _to, rate, _group = victim(sql)
    login_as(client, "admin")
    before = sql.execute(
        "select count(*) from employment_terms where employee_id = %s", (employee_id,)
    ).fetchone()[0]

    answer = post_new_version(client, sql, employee_id, valid_from="2026-09-01", rate=str(rate))
    assert answer.status_code == 302
    assert "saved=same" in answer["Location"], answer["Location"]
    after = sql.execute(
        "select count(*) from employment_terms where employee_id = %s", (employee_id,)
    ).fetchone()[0]
    assert after == before
    client.post("/logout/")


@pytest.fixture
def payruns_restored(web_env):
    """Утверждённый месяц не переживает теста.

    Тесты ниже утверждают июнь, чтобы было чему сопротивляться правке
    справочника. Оставленный утверждённым, он меняет вид страницы месяца для
    **всех** остальных тестов стенда: полоса шагов перестаёт показывать текущий
    шаг, потому что пройдены все. Проверка онбординга из-за этого падала, и
    выглядело это поломкой соседнего блока, а не следом здешнего теста.
    """
    yield
    wipe_payruns(web_env)


def approve_june(client, web_env) -> str:
    """Посчитать и утвердить июнь — руками по экрану, как это делает человек."""
    wipe_payruns(web_env)
    login_as(client, "director")
    url = period_url(client)
    assert client.post(url + "calculate/", {"inline": "1"}, follow=True).status_code == 200
    assert client.post(url + "approve/", follow=True).status_code == 200
    return url


def june_snapshot(sql) -> list:
    """Снимок закрытого расчёта: всё, из чего он состоит, а не только итог."""
    return rows_of(
        sql,
        """select p.id, p.employee_id, p.unit_id, c.code, c.title, c.amount, c.ledger
             from payslips p join pay_components c on c.payslip_id = p.id
             join payruns r on r.id = p.payrun_id
            where r.period = '2026-06-01'
            order by p.id, c.code, c.amount""",
    )


def june_rates(web_env) -> dict:
    """Ставки, которые расчёт возьмёт на июнь — тем же кодом, что и сам расчёт."""
    from core.models import Tenant
    from payrun.calc import collect_cases

    tenant_id = Tenant.objects.filter(code="rs-dev").values_list("id", flat=True).first()
    return {
        case.external_id: case.employee.base_rate
        for case in collect_cases(tenant_id, date(2026, 6, 1))
    }


def test_a_directory_edit_does_not_move_a_closed_period(
    client, sql, web_env, terms_restored, payruns_restored,
):
    """Главная проверка задачи: закрытый месяц остаётся байт в байт прежним.

    Проверяется двумя способами, и второй важнее первого. Первый (строки не
    изменились) прошёл бы и от того, что закрытый расчёт вообще некому трогать.
    Второй показывает, что правило выбрано верно: расчёт, собранный заново на
    июнь **после** правки, по-прежнему берёт июньскую версию условий. Если бы
    экран переписывал версию вместо заведения новой, здесь и вылезла бы новая
    ставка.
    """
    approve_june(client, web_env)
    before_rows = june_snapshot(sql)
    assert before_rows, "июнь не посчитался — проверять нечего"
    before_rates = june_rates(web_env)
    client.post("/logout/")

    _, employee_id, _from, _to, _rate, _group = victim(sql)
    external_id = sql.execute(
        "select external_id from employees where id = %s", (employee_id,)
    ).fetchone()[0]

    login_as(client, "admin")
    answer = post_new_version(client, sql, employee_id, valid_from="2026-07-01", rate="12345.0000")
    assert answer.status_code == 302, body(answer)
    client.post("/logout/")

    assert june_snapshot(sql) == before_rows, "строки закрытого июня изменились"
    after_rates = june_rates(web_env)
    assert after_rates == before_rates, "расчёт июня стал брать другие ставки"
    assert after_rates[external_id] != Decimal("12345.0000"), (
        "июль приехал в июнь: расчёт взял новую версию условий"
    )


def test_the_group_scheme_is_frozen_while_a_month_is_closed(client, sql, web_env, payruns_restored):
    """Схема расчёта группы версий не имеет — значит при закрытом месяце не правится.

    Название группы при этом править можно: оно денег не считает. Проверяется и
    то и другое, иначе запрет мог бы оказаться запретом на любую правку группы.
    """
    approve_june(client, web_env)
    client.post("/logout/")
    group_id, code, title, scheme, ledger = sql.execute(
        "select id, code, title, scheme, ledger from employee_groups "
        "where ledger = 'official' and tenant_id in "
        "(select id from tenants where code = 'rs-dev') limit 1"
    ).fetchone()

    login_as(client, "admin")
    try:
        refused = client.post(f"/directory/groups/{group_id}/", {
            "code": code, "title": title, "scheme": "совсем другая", "ledger": ledger,
        })
        assert refused.status_code == 409, refused.status_code
        assert sql.execute(
            "select scheme from employee_groups where id = %s", (group_id,)
        ).fetchone()[0] == scheme

        renamed = client.post(f"/directory/groups/{group_id}/", {
            "code": code, "title": f"{title} (проверка)", "scheme": scheme, "ledger": ledger,
        })
        assert renamed.status_code == 302, body(renamed)
    finally:
        sql.execute(
            "update employee_groups set title = %s, scheme = %s where id = %s",
            (title, scheme, group_id),
        )
        client.post("/logout/")


# --- регистры учёта (D023) ----------------------------------------------------


@pytest.fixture
def manager_manages(sql):
    """Право вести справочники — временно управляющему, у которого неполный набор.

    Зачем такая подмена вообще понадобилась (T089). Раньше срез регистров в
    справочнике проверялся на администраторе сети: у него в сиде стоял один
    официальный регистр. Ровно это и оказалось дефектом — админка показывала ему
    8 карточек из 35 и 3 группы из 6, а `directory.manage` есть **только** у
    него, то есть карточку курьера не мог открыть ни один пользователь продукта.
    Регистры администратору открыли (тот же довод, что в D033), и проверять срез
    на нём стало нельзя: он видит всё по праву.

    Роль для подмены сначала была бухгалтером — у него в сиде стоял один
    официальный регистр. После D036 бухгалтеру открыты все три, как директору,
    и подмена на нём перестала бы что-либо проверять: и без урезания регистров
    видел бы всё то же самое. Управляющий точки — единственная роль с
    по-прежнему неполным набором (официальный и дополнительный, без
    внутреннего, D031), поэтому подмена права переехала на него.

    Само правило при этом никуда не делось: партнёр вправе поручить справочники
    тому, кто видит не все регистры. Такой роли в сиде нет (у управляющего в
    сиде нет `directory.manage`), поэтому она делается здесь — и разбирается
    обратно, что бы ни случилось с тестом.
    """
    sql.execute(
        "update roles set permissions = permissions || '[\"directory.manage\"]'::jsonb "
        "where code = 'manager' and tenant_id is not null"
    )
    try:
        yield
    finally:
        sql.execute(
            "update roles set permissions = permissions - 'directory.manage' "
            "where code = 'manager' and tenant_id is not null"
        )


def test_the_directory_never_names_a_ledger_the_role_cannot_see(
    client, sql, manager_manages,
):
    """Ни строкой, ни словом: у управляющего нет внутреннего регистра (D031).

    Была роль бухгалтера с одним официальным регистром — после D036 её набор
    полон, как у директора, и проверка на ней перестала бы что-либо доказывать
    (она законно увидела бы всё). Управляющий видит официальный и дополнительный,
    но не внутренний, поэтому «дополнительный» здесь ожидаемо виден, а
    «внутренний» — нет: срез всё так же проверяется по регистру, а не по праву
    и не по точке (справочник групп точками не режется вовсе).

    В сиде есть группы всех трёх регистров, и без отбора справочник назвал бы
    управляющему слово «Внутренний» — то есть сообщил бы о существовании
    регистра, которого он не видит нигде больше.
    """
    hidden = rows_of(
        sql,
        "select title from employee_groups where ledger = 'internal' and tenant_id in "
        "(select id from tenants where code = 'rs-dev')",
    )
    assert hidden, "в сиде нет групп внутреннего регистра — проверка проверяет пустоту"
    shown = rows_of(
        sql,
        "select title from employee_groups where ledger = 'supplementary' and tenant_id in "
        "(select id from tenants where code = 'rs-dev')",
    )
    assert shown, "в сиде нет групп дополнительного регистра — контрольная часть проверки пуста"

    login_as(client, "manager")
    for url in ["/directory/", "/directory/groups/", "/directory/employees/"]:
        html = body(client.get(url))
        assert "Внутренний" not in html, url
        for (title,) in hidden:
            assert title not in html, f"{url}: группа скрытого регистра названа — {title}"

    # Контроль: дополнительный регистр управляющему открыт (D031), и его слово
    # и группы обязаны быть на месте — иначе непонятно, срез это или дырка.
    groups_html = body(client.get("/directory/groups/"))
    assert "Дополнительный" in groups_html
    for (title,) in shown:
        assert title in groups_html, f"группа своего регистра пропала у управляющего — {title}"
    client.post("/logout/")


def test_a_group_of_another_ledger_is_not_openable_by_address(
    client, sql, manager_manages,
):
    """Отбор в списке — не защита, если запись открывается прямой ссылкой.

    Роль — управляющий, а не бухгалтер: после D036 бухгалтер видит внутренний
    регистр законно, и 404 на его карточке доказывал бы уже не отбор по
    регистру. У управляющего внутренний регистр не открыт (D031).
    """
    group_id = sql.execute(
        "select id from employee_groups where ledger = 'internal' and tenant_id in "
        "(select id from tenants where code = 'rs-dev') limit 1"
    ).fetchone()[0]
    login_as(client, "manager")
    assert client.get(f"/directory/groups/{group_id}/").status_code == 404
    client.post("/logout/")


# --- справочники ведутся ЦЕЛИКОМ (T089) ---------------------------------------


def test_the_admin_leads_every_card_not_a_quarter_of_them(client, sql):
    """Тупик T089 закрыт: у того, кто ведёт справочники, они видны полностью.

    Раньше здесь было 8 карточек из 35 и 3 группы из 6, и это не «часть данных
    скрыта» — это «продуктом нельзя сделать работу»: `directory.manage` есть
    только у администратора, и поменять ставку курьеру не мог никто.

    Проверяется парой чисел, а не «страница открылась»: страница открывалась и
    до починки. И числа берутся из базы, а не написаны в тесте, — иначе тест
    пришлось бы править всякий раз, когда меняется фикстура, и однажды его
    поправили бы под сломанное поведение.
    """
    people, groups_count = sql.execute(
        """select (select count(*) from employees e
                    where e.tenant_id = t.id and e.dismissed_at is null),
                  (select count(*) from employee_groups g where g.tenant_id = t.id)
             from tenants t where t.code = 'rs-dev'"""
    ).fetchone()
    assert people > 8 and groups_count > 3, "в фикстуре нет скрытых карточек — проверять нечего"

    login_as(client, "admin")
    index = body(client.get("/directory/"))
    assert f">{people}<" in index, f"на оглавлении не {people} сотрудников:\n{index}"
    assert f">{groups_count}<" in index, f"на оглавлении не {groups_count} групп"

    groups_html = body(client.get("/directory/groups/"))
    for (title,) in rows_of(
        sql,
        "select title from employee_groups where tenant_id in "
        "(select id from tenants where code = 'rs-dev')",
    ):
        assert title in groups_html, f"группа {title} не видна тому, кто ведёт справочники"
    client.post("/logout/")


def test_the_admin_opens_a_card_of_every_ledger(client, sql):
    """Карточка курьера открывается — та самая, ради которой задача и заведена."""
    group_id = sql.execute(
        "select id from employee_groups where ledger = 'internal' and tenant_id in "
        "(select id from tenants where code = 'rs-dev') limit 1"
    ).fetchone()[0]
    login_as(client, "admin")
    assert client.get(f"/directory/groups/{group_id}/").status_code == 200
    client.post("/logout/")
