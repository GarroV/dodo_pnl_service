"""Экран правил расчёта: версии, закрытый месяц, право и срез роли (T090, T091).

Четыре вещи, ради которых написаны эти проверки.

**Главная — правка правила не двигает закрытый месяц.** Ведомость за
утверждённый месяц уже на руках у людей. Если новая версия правила меняет то,
как этот месяц пересчитается, воспроизводимости нет, а значит нет и сверки
(D020, T026). Проверяется двумя способами сразу, и второй важнее первого:
строки закрытого расчёта остаются байт в байт прежними **и** расчёт, собранный
заново на июнь после правки, по-прежнему собирает июньский пресет. Первый
прошёл бы и от того, что закрытый расчёт некому трогать; второй показывает, что
правило заведено новой версией с датой, а не переписано по месту.

**Право `rules.manage` наконец кем-то спрашивается.** До экрана оно полгода
лежало в ролях и в миграциях, ни разу никем не спрошенное — миграция `0022`
записала этот долг прямо. Поэтому здесь проверяется вся пара: интерфейс не
показывает того, что запретит, адрес отвечает отказом словами, а база не даёт
записать даже мимо экрана (вторая половина — в `test_rules_policy.py`).

**Регистр, которого роль не видит, не называется и в правилах** (D023). Тело
правил называет регистр у групп и у надбавок: `groups.couriers.ledger =
internal` — это сообщение о том, что внутренний регистр существует, ничем не
отличающееся от строки в ведомости.

**Способ работы курьеров переключается из продукта** (D032, T091). До этого он
был сделан в движке и не доведён до интерфейса: поля в форме группы нет, экрана
правил нет — то есть включить второй способ было нечем.
"""
from __future__ import annotations

from datetime import date

import pytest

from conftest import body, login_as, period_url, wipe_payruns

JUNE = date(2026, 6, 1)
COURIERS_MEASURE = "groups.couriers.work_measure"
NIGHT_PERCENT = "hour_types.night.pay_percent"
# Делитель пересчёта нето в бруто: правило, которое двигает деньги у каждого.
# Нужно там, где проверка «закрытый месяц не изменился» обязана быть непустой.
NET_FACTOR = "rates.net_factor"


@pytest.fixture
def sql(web_env):
    """Прямое соединение к базе стенда — владельцем схемы, мимо политик.

    Только для подготовки и уборки состояния. Проверки доступа этим соединением
    не делаются: на владельца политики не действуют, и зелёный результат ничего
    не значил бы — см. `as_app_user` в `conftest`.
    """
    import psycopg

    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def overrides_restored(web_env, sql):
    """Переопределения правил не переживают теста.

    База стенда одна на весь прогон, а правило меняет деньги всем, кого
    касается: оставленная строка сдвинула бы контрольные числа у каждого, кто
    считает период после. Тот же довод, что у `period_restored`.
    """
    before = [row[0] for row in sql.execute("select id from rule_overrides").fetchall()]
    yield
    if before:
        sql.execute("delete from rule_overrides where id <> all(%s)", (before,))
    else:
        sql.execute("delete from rule_overrides")


@pytest.fixture
def payruns_restored(web_env):
    """Утверждённый месяц не переживает теста — см. тот же приём в `test_directory.py`."""
    yield
    wipe_payruns(web_env)


def approve_june(client, web_env) -> str:
    """Посчитать и утвердить июнь — руками по экрану, как это делает человек."""
    wipe_payruns(web_env)
    login_as(client, "director")
    url = period_url(client)
    assert client.post(url + "calculate/", {"inline": "1"}, follow=True).status_code == 200
    assert client.post(url + "approve/", follow=True).status_code == 200
    client.post("/logout/")
    return url


def june_snapshot(sql) -> list:
    """Снимок закрытого расчёта: всё, из чего он состоит, а не только итог."""
    return sql.execute(
        """select p.id, p.employee_id, c.code, c.amount, c.ledger
             from payslips p join pay_components c on c.payslip_id = p.id
             join payruns r on r.id = p.payrun_id
            where r.period = '2026-06-01'
            order by p.id, c.code, c.amount"""
    ).fetchall()


def rule_at(web_env, path: str, when: date):
    """Значение правила, которое расчёт возьмёт на эту дату, — тем же кодом, что и он."""
    from core.models import Tenant
    from core.rules import load_rules_at
    from web import rules
    from web.directory import country_of

    tenant_id = Tenant.objects.filter(code="rs-dev").values_list("id", flat=True).first()
    preset = load_rules_at(tenant_id, country_of(tenant_id), when).base
    return rules.value_at(preset, path)


def june_rule(web_env, path: str):
    return rule_at(web_env, path, JUNE)


def post_rule(client, path: str, *, value: str, valid_from: str):
    return client.post(f"/rules/{path}/", {"value": value, "valid_from": valid_from})


# --- право --------------------------------------------------------------------


ROLES_WITHOUT_RIGHT = ["director", "accountant", "manager"]


def test_only_the_role_that_manages_rules_sees_the_link(client):
    """Ссылка на правила есть у администратора и больше ни у кого (T072)."""
    seen = {}
    for role in ["director", "accountant", "manager", "admin"]:
        login_as(client, role)
        seen[role] = 'href="/rules/"' in body(client.get("/periods/"))
        client.post("/logout/")

    assert seen == {"director": False, "accountant": False, "manager": False, "admin": True}


@pytest.mark.parametrize("role", ROLES_WITHOUT_RIGHT)
def test_the_rules_screen_refuses_the_other_roles_in_words(client, role):
    """Адрес остаётся рабочим и отвечает отказом словами, а не пустотой и не 404."""
    login_as(client, role)
    answer = client.get("/rules/")
    assert answer.status_code == 403, answer.status_code
    assert "Ведение правил расчёта" in body(answer), "отказ не назвал действия"
    denied = client.get(f"/rules/{NIGHT_PERCENT}/")
    assert denied.status_code == 403, denied.status_code
    client.post("/logout/")


def test_a_role_without_the_right_cannot_write_a_rule_through_the_screen(
    client, sql, overrides_restored,
):
    """Отказ отказом, но записать POST-ом тоже не выходит."""
    login_as(client, "director")
    answer = post_rule(client, NIGHT_PERCENT, value="1.5", valid_from="2026-09-01")
    assert answer.status_code == 403, answer.status_code
    assert sql.execute(
        "select count(*) from rule_overrides where path = %s", (NIGHT_PERCENT,)
    ).fetchone()[0] == 0, "отказ отказал, но правило всё-таки записал"
    client.post("/logout/")


def test_the_guest_is_sent_to_the_login(client):
    answer = client.get("/rules/")
    assert answer.status_code == 302 and "/login/" in answer["Location"]


# --- что видно ----------------------------------------------------------------


def test_the_admin_sees_the_rules_with_where_each_came_from(client):
    """Экран показывает правила и след: кто положил значение и с какого числа."""
    login_as(client, "admin")
    html = body(client.get("/rules/"))
    assert "rates.net_factor" in html, "правил на экране нет вовсе"
    assert "0.701" in html, "значение правила не показано"
    assert "правила страны" in html, "не сказано, откуда взялось значение"
    assert "Типы часов" in html, "разделы правил не названы"
    client.post("/logout/")


def test_the_preset_identity_is_not_offered_for_editing(client):
    """Имя пресета, страну и дату его начала правит не партнёр — их и не предлагают.

    Переопределение поверх `valid_from` выглядело бы работающим и не меняло бы
    ничего: строку `rule_presets` сборка выбирает ДО того, как накладывает
    переопределения.
    """
    login_as(client, "admin")
    assert client.get("/rules/country/").status_code == 404
    assert client.get("/rules/valid_from/").status_code == 404
    assert client.get("/rules/hour_types.night.nonesuch/").status_code == 404
    client.post("/logout/")


def test_a_broken_date_in_the_address_does_not_silently_become_today(client):
    login_as(client, "admin")
    assert client.get("/rules/?on=вчера").status_code == 404
    client.post("/logout/")


# --- версии -------------------------------------------------------------------


def test_editing_a_rule_makes_a_new_version_and_keeps_the_old(
    client, sql, overrides_restored,
):
    """Правка заводит версию с датой; прежняя закрывается этим же днём."""
    login_as(client, "admin")
    assert post_rule(
        client, NIGHT_PERCENT, value="1.4", valid_from="2026-09-01"
    ).status_code == 302
    assert post_rule(
        client, NIGHT_PERCENT, value="1.5", valid_from="2026-10-01"
    ).status_code == 302
    client.post("/logout/")

    rows = sql.execute(
        "select valid_from, valid_to, value from rule_overrides "
        "where path = %s order by valid_from",
        (NIGHT_PERCENT,),
    ).fetchall()
    assert [(str(a), str(b), c) for a, b, c in rows] == [
        ("2026-09-01", "2026-10-01", 1.4),
        ("2026-10-01", "None", 1.5),
    ], rows


def test_nothing_changed_means_no_new_version(client, sql, overrides_restored):
    """Прежнее значение с новой датой версии не заводит и говорит об этом.

    Иначе история обрастала бы строками «то же самое с другой даты», и найти в
    ней настоящую смену правила стало бы нельзя.
    """
    login_as(client, "admin")
    answer = post_rule(client, NIGHT_PERCENT, value="1.26", valid_from="2026-09-01")
    assert answer.status_code == 200, answer.status_code
    assert "Ничего не изменилось" in body(answer)
    client.post("/logout/")
    assert sql.execute(
        "select count(*) from rule_overrides where path = %s", (NIGHT_PERCENT,)
    ).fetchone()[0] == 0


def test_a_new_version_is_what_the_screen_shows_afterwards(client, overrides_restored):
    """После правки экран показывает новое значение и называет его настройкой партнёра."""
    login_as(client, "admin")
    assert post_rule(
        client, NIGHT_PERCENT, value="1.4", valid_from="2026-09-01"
    ).status_code == 302
    html = body(client.get("/rules/?on=2026-09-15"))
    assert "1.4" in html and "настройка партнёра" in html, html[:400]
    # А на дату до начала действия — по-прежнему правило страны.
    assert "настройка партнёра" not in body(client.get("/rules/?on=2026-08-15"))
    client.post("/logout/")


def test_a_wrong_value_is_explained_not_swallowed(client, sql, overrides_restored):
    """Буквы вместо числа — отказ словами, а не строка в jsonb вместо числа."""
    login_as(client, "admin")
    answer = post_rule(client, NIGHT_PERCENT, value="много", valid_from="2026-09-01")
    assert answer.status_code == 400, answer.status_code
    assert "нужно число" in body(answer)
    assert sql.execute(
        "select count(*) from rule_overrides where path = %s", (NIGHT_PERCENT,)
    ).fetchone()[0] == 0
    client.post("/logout/")


# --- закрытый месяц -----------------------------------------------------------


def test_a_rule_change_does_not_move_a_closed_period(
    client, sql, web_env, overrides_restored, payruns_restored,
):
    """Главная проверка задачи: закрытый месяц остаётся байт в байт прежним.

    **Правило сначала правится в открытом июне, и только потом июнь
    утверждается.** Без этого шага проверка пуста: если у правила нет ни одной
    версии партнёра, переписывать по месту нечего, и экран, который переписывает
    вместо заведения новой версии, прошёл бы её зелёным. Проверено порчей —
    ровно так первая редакция этого теста и промахнулась.

    Правило выбрано такое, которое двигает деньги всем (`net_factor` — делитель
    в пересчёте нето в бруто): иначе «строки не изменились» означало бы только
    то, что правка ни на что не влияет.

    Проверяется тремя утверждениями, и каждое ловит своё: строки закрытого июня
    не изменились; пресет, собранный заново **на июнь**, по-прежнему отдаёт
    июньское значение; на июль действует новое — иначе тест был бы про то, что
    правка не сработала вовсе.
    """
    wipe_payruns(web_env)
    login_as(client, "admin")
    assert post_rule(
        client, NET_FACTOR, value="0.71", valid_from="2026-06-01"
    ).status_code == 302
    client.post("/logout/")
    assert june_rule(web_env, NET_FACTOR) == 0.71, "подготовка не подействовала"

    approve_june(client, web_env)
    before_rows = june_snapshot(sql)
    assert before_rows, "июнь не посчитался — проверять нечего"

    login_as(client, "admin")
    assert post_rule(
        client, NET_FACTOR, value="0.65", valid_from="2026-07-01"
    ).status_code == 302
    client.post("/logout/")

    assert june_snapshot(sql) == before_rows, "строки закрытого июня изменились"
    assert june_rule(web_env, NET_FACTOR) == 0.71, (
        "июль приехал в июнь: расчёт стал брать новую версию правила"
    )
    assert rule_at(web_env, NET_FACTOR, date(2026, 7, 1)) == 0.65, (
        "правка не подействовала нигде"
    )


def test_the_form_offers_a_date_that_does_not_touch_the_closed_month(
    client, web_env, payruns_restored,
):
    """Подставленная дата не приглашает получить отказ на ровном месте.

    Проверяется свойство («позже закрытого месяца»), а не конкретное число:
    умолчание считается от сегодня, и тест на строку краснел бы завтра — по
    календарю, а не по продукту.
    """
    approve_june(client, web_env)
    login_as(client, "admin")
    import re

    html = body(client.get(f"/rules/{NIGHT_PERCENT}/"))
    offered = re.search(r'id="valid_from"[^>]*?value="(\d{4}-\d{2}-\d{2})"', html)
    assert offered, f"в форме нет подставленной даты:\n{html[-1500:]}"
    assert date.fromisoformat(offered.group(1)) > date(2026, 6, 30), (
        f"форма предлагает дату внутри закрытого месяца: {offered.group(1)}"
    )
    client.post("/logout/")


# --- срез роли (D023) ---------------------------------------------------------


def test_the_rules_never_name_a_ledger_the_role_cannot_see(client, sql):
    """Роль без внутреннего регистра не узнаёт о нём из правил.

    Право выдаётся управляющему временно: сегодня `rules.manage` есть только у
    администратора, и проверить срез было бы не на ком — а завтра партнёр
    выдаст правила тому, кто видит не все регистры.

    Роль для подмены раньше была бухгалтером — у него в сиде стоял один
    официальный регистр. После D036 бухгалтеру открыты все три, как директору,
    и подмена на нём перестала бы доказывать срез: он увидел бы курьеров
    (внутренний регистр) законно. Управляющий точки видит официальный и
    дополнительный, но не внутренний (D031) — единственная роль в сиде с
    по-прежнему неполным набором. Дополнительный регистр (группа `kitchen`)
    проверяется отдельно как контроль: он обязан остаться на месте, иначе
    непонятно, срез это или дырка в другую сторону.
    """
    sql.execute(
        "update roles set permissions = permissions || '[\"rules.manage\"]'::jsonb "
        "where code = 'manager' and tenant_id is not null"
    )
    try:
        login_as(client, "manager")
        html = body(client.get("/rules/"))
        assert "groups.office" in html, "срез отобрал вообще всё — проверять нечего"
        assert "groups.kitchen" in html, "дополнительный регистр управляющему открыт (D031)"
        assert "couriers" not in html, "внутренний регистр назван в правилах"
        assert "internal" not in html, "внутренний регистр назван в правилах"
        # И по прямому адресу правило чужого регистра не открывается.
        assert client.get(f"/rules/{COURIERS_MEASURE}/").status_code == 404
        client.post("/logout/")
    finally:
        sql.execute(
            "update roles set permissions = permissions - 'rules.manage' "
            "where code = 'manager' and tenant_id is not null"
        )


# --- способ работы группы (D032, T091) ----------------------------------------


def test_the_group_form_shows_how_the_work_is_measured(client):
    login_as(client, "admin")
    html = body(client.get("/directory/groups/"))
    assert "Чем меряется работа" in html, "в списке групп способа работы нет"
    assert "По часам" in html, "способ показан кодом, а не словами"
    client.post("/logout/")


def group_url(client, code: str) -> str:
    """Адрес карточки группы — со списка, как на неё попадает человек."""
    import re

    from core.models import EmployeeGroup

    group_id = EmployeeGroup.objects.filter(code=code).values_list("id", flat=True).first()
    assert group_id is not None, f"группы {code} нет в справочнике"
    url = f"/directory/groups/{group_id}/"
    assert re.search(re.escape(url), body(client.get("/directory/groups/"))), (
        "карточка группы не находится со списка"
    )
    return url


def test_the_work_measure_switches_from_the_group_form(client, sql, overrides_restored):
    """D032 доведён до продукта: второй способ оплаты курьеров включается кликом."""
    login_as(client, "admin")
    url = group_url(client, "couriers")
    html = body(client.get(url))
    assert 'name="work_measure"' in html, "поля способа работы в форме группы нет"
    assert "Доставки" in html, "второй способ в форме не предложен"

    answer = client.post(url, {
        "code": "couriers", "title": "Курьеры", "scheme": "direct", "ledger": "internal",
        "work_measure": "deliveries", "measure_from": "2026-09-01",
    })
    assert answer.status_code == 302, body(answer)
    client.post("/logout/")

    rows = sql.execute(
        "select valid_from, value, scope_type from rule_overrides where path = %s",
        (COURIERS_MEASURE,),
    ).fetchall()
    assert [(str(a), b, c) for a, b, c in rows] == [
        ("2026-09-01", "deliveries", "tenant")
    ], rows


def test_editing_a_group_without_touching_the_measure_asks_for_no_date(
    client, sql, overrides_restored,
):
    """Правка названия группы не требует даты и версии не заводит."""
    login_as(client, "admin")
    url = group_url(client, "couriers")
    answer = client.post(url, {
        "code": "couriers", "title": "Курьеры", "scheme": "direct", "ledger": "internal",
        "work_measure": "hours", "measure_from": "",
    })
    assert answer.status_code == 302, body(answer)
    client.post("/logout/")
    assert sql.execute(
        "select count(*) from rule_overrides where path = %s", (COURIERS_MEASURE,)
    ).fetchone()[0] == 0


def test_the_timesheet_shows_the_piece_column_once_the_measure_is_switched(
    client, sql, web_env, overrides_restored, period_restored,
):
    """Обещанное CHANGELOG поведение табеля: колонка со сдельной величиной и прочерк.

    До переключения колонки нет вовсе — и это правильно: пустая колонка на
    каждом табеле мира приглашала бы вводить в неё там, где её никто не
    спрашивает. Проверяется поэтому пара целиком, а не половина.
    """
    login_as(client, "admin")
    url = group_url(client, "couriers")
    assert client.post(url, {
        "code": "couriers", "title": "Курьеры", "scheme": "direct", "ledger": "internal",
        "work_measure": "deliveries", "measure_from": "2026-06-01",
    }).status_code == 302
    client.post("/logout/")

    login_as(client, "director")
    period_id = period_url(client).strip("/").split("/")[-1]
    grid = body(client.get(f"/timesheets/{period_id}/"))
    assert "Сдельно" in grid, "колонка сдельной величины не появилась"
    assert "Доставки" in grid, "подпись способа в табеле не показана"
    assert "Работа этой группы меряется часами" in grid, (
        "у почасовых строк нет обещанного прочерка"
    )
    client.post("/logout/")


def test_the_rule_page_names_the_closed_month_before_the_edit(
    client, web_env, payruns_restored,
):
    """Граница утверждённой зарплаты названа до правки, а не только после неё.

    Найдено в браузере: подставленная дата была верной, но человек, набравший
    свою, узнавал ответ только нажав «Завести версию» — продукт молчал там,
    где знал его заранее. С T121 ответ другой (правка проходит, разница едет
    вперёд), а требование то же: сказать заранее.
    """
    approve_june(client, web_env)
    login_as(client, "admin")
    html = body(client.get(f"/rules/{NIGHT_PERCENT}/"))
    assert "2026-06-30" in html, "страница не называет границу закрытого месяца"
    client.post("/logout/")
