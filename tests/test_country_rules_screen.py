"""Правила страны видны в продукте и правятся тем, кто вправе (T165).

**Что было.** Тело пресета страны — ночные часы, больничные, ставки взносов —
приезжало в базу один раз командой `manage.py load_presets` из YAML. Чтобы
поднять минимальную зарплату Сербии, нужен был разработчик и доступ к серверу.
Владелец 2026-08-18 искал эти правила в продукте и не нашёл.

**Что проверяется здесь.** Три вещи, и все три — про молчание, которого быть не
должно:

1. Правило страны **находится**: на карточке правила видно значение страны, с
   какого числа оно действует и кто его ведёт. Партнёр, которому не положено его
   менять, читает об этом словами, а не смотрит на пустое место.
2. Правка страны заводит **версию**, а не переписывает тело: уже посчитанный
   месяц собирается прежней версией и остаётся байт в байт тем же.
3. Повторный `load_presets` **не откатывает** правку, сделанную в продукте, и не
   молчит об этом: он говорит, что пропустил и почему.

Проверка права на стороне базы — в `test_country_rules_policy.py`: там роль
`app_user`, здесь экран.
"""
from __future__ import annotations

from datetime import date

import pytest

from conftest import body, login_as

JUNE = date(2026, 6, 1)
SEPTEMBER = date(2026, 9, 1)
NIGHT_PERCENT = "hour_types.night.pay_percent"
NIGHT_TITLE = "hour_types.night.title"
COURIERS_MEASURE = "groups.couriers.work_measure"
PRESET_CODE = "serbia-2026"
# Дата, с которой пресет объявлен в файле. Правка ровно этой датой правит тело
# по месту — единственный случай, в котором повторная загрузка файла способна
# откатить сделанное в продукте.
PRESET_FROM = "2026-01-01"


@pytest.fixture
def sql(web_env):
    """Соединение владельцем схемы — для подготовки и уборки, не для проверок."""
    import psycopg

    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def presets_restored(web_env, sql):
    """Правила страны не переживают теста.

    База стенда одна на весь прогон, а тело страны — общая база расчёта: лишняя
    версия сдвинула бы контрольные суммы у каждого, кто считает период после.
    Тот же довод, что у `overrides_restored`.
    """
    before = sql.execute(
        "select id, body, valid_from, valid_to, edited_at from rule_presets order by valid_from"
    ).fetchall()
    yield
    kept = [row[0] for row in before]
    if kept:
        sql.execute("delete from rule_presets where id <> all(%s)", (kept,))
    else:
        sql.execute("delete from rule_presets")
    for row_id, row_body, valid_from, valid_to, edited_at in before:
        sql.execute(
            "update rule_presets set body = %s, valid_from = %s, valid_to = %s, "
            "edited_at = %s where id = %s",
            (__import__("json").dumps(row_body), valid_from, valid_to, edited_at, row_id),
        )


@pytest.fixture
def platform_admin(web_env, sql):
    """Право вести правила стран у администратора сети — на время теста.

    Кладётся владельцем схемы, потому что из приложения эта таблица не пишется
    вовсе: политик на запись у неё нет. Настоящий путь продукта — команда
    `manage.py platform_admin`, которая делает ровно этот insert.
    """
    from core.models import User

    user_id = User.objects.filter(username="admin").values_list("id", flat=True).first()
    assert user_id is not None, "в сиде нет учётки admin"
    sql.execute(
        "insert into platform_admins (user_id, note) values (%s, 'тест') "
        "on conflict (user_id) do nothing",
        (user_id,),
    )
    yield user_id
    sql.execute("delete from platform_admins where user_id = %s", (user_id,))


def country_rule(path: str, when: date):
    """Значение правила в ТЕЛЕ СТРАНЫ на дату — не в собранном пресете.

    Разница существенная: собранный несёт поверх себя настройки партнёра, и
    проверка на нём не отличила бы правку страны от переопределения.
    """
    from web import rules
    from web.rules_country import in_force_at

    version = in_force_at("RS", when)
    return None if version is None else rules.value_at(version.body, path)


def post_country(client, path: str, *, value: str, valid_from: str):
    return client.post(
        f"/rules/{path}/",
        {"value": value, "valid_from": valid_from, "layer": "country"},
    )


# --- правило страны находится в продукте --------------------------------------


def test_the_country_rule_is_shown_on_the_rule_page(client):
    """Значение страны, его дата и набор правил названы на карточке правила."""
    login_as(client, "admin")
    html = body(client.get(f"/rules/{NIGHT_PERCENT}/"))
    assert "Правило страны" in html, html[:600]
    assert PRESET_CODE in html, "набор правил страны не назван"
    assert "1.26" in html, "значение страны не показано"
    client.post("/logout/")


def test_the_partner_is_told_who_keeps_the_country_rules(client):
    """Формы правки у партнёра нет, и вместо неё — объяснение, а не пустое место.

    Кнопка, пропавшая без слов, читается как поломка продукта (T072). Здесь
    вдобавок надо сказать, где эти правила живут: человек, не нашедший их,
    именно это и спрашивает.
    """
    login_as(client, "admin")
    html = body(client.get(f"/rules/{NIGHT_PERCENT}/"))
    assert "администратор платформы" in html, html[:900]
    assert "Завести версию правил страны" not in html, "форма страны показана не тому"
    client.post("/logout/")


def test_the_partner_cannot_change_the_country_rule_by_hand(client, sql, presets_restored):
    """Подменённый запрос без права платформы — отказ, а не версия правил страны."""
    login_as(client, "admin")
    answer = post_country(client, NIGHT_PERCENT, value="1.9", valid_from="2026-09-01")
    assert answer.status_code == 403, answer.status_code
    assert "администратор платформы" in body(answer)
    client.post("/logout/")
    assert sql.execute("select count(*) from rule_presets").fetchone()[0] == 1, (
        "версия правил страны всё-таки заведена"
    )


# --- правка страны: версия, а не переписывание ---------------------------------


def test_the_platform_admin_starts_a_new_country_version(
    client, web_env, sql, presets_restored, platform_admin,
):
    """Правка заводит новую версию с датой, прежняя закрывается этим же днём.

    И главное: уже посчитанный июнь собирается **прежней** версией. Это и есть
    версионирование по датам — то, на чём стоит весь продукт: правило,
    вступающее в силу в сентябре, не имеет права трогать июнь.
    """
    login_as(client, "admin")
    answer = post_country(client, NIGHT_PERCENT, value="1.5", valid_from="2026-09-01")
    assert answer.status_code == 302, body(answer)[:800]
    client.post("/logout/")

    rows = sql.execute(
        "select valid_from, valid_to, edited_at is not null from rule_presets "
        "order by valid_from"
    ).fetchall()
    assert [(str(a), str(b), c) for a, b, c in rows] == [
        (PRESET_FROM, "2026-09-01", False),
        ("2026-09-01", "None", True),
    ], rows

    assert country_rule(NIGHT_PERCENT, JUNE) == 1.26, "правка с сентября достала до июня"
    assert country_rule(NIGHT_PERCENT, SEPTEMBER) == 1.5


def test_a_country_change_reaches_every_partner_of_that_country(
    client, web_env, presets_restored, platform_admin,
):
    """Правка страны действует у партнёра без всякого переопределения.

    Ради этого всё и делалось: правило страны — база, и она обязана доезжать до
    расчёта партнёра сама. Проверяется тем же кодом, каким правила берёт расчёт.
    """
    from core.models import Tenant
    from core.rules import load_rules_at
    from web import rules
    from web.directory import country_of

    login_as(client, "admin")
    assert post_country(
        client, NIGHT_PERCENT, value="1.7", valid_from="2026-09-01"
    ).status_code == 302
    client.post("/logout/")

    tenant_id = Tenant.objects.filter(code="rs-dev").values_list("id", flat=True).first()
    assembled = load_rules_at(tenant_id, country_of(tenant_id), SEPTEMBER)
    assert rules.value_at(assembled.base, NIGHT_PERCENT) == 1.7
    # А след говорит, что значение пришло от страны, а не от партнёра: иначе
    # человек искал бы у себя настройку, которой нет.
    assert assembled.base.origin_of(NIGHT_PERCENT).level == "country"


def test_the_same_country_value_makes_no_new_version(
    client, sql, presets_restored, platform_admin,
):
    """Прежнее значение новой версии не заводит и говорит об этом спокойно."""
    login_as(client, "admin")
    answer = post_country(client, NIGHT_PERCENT, value="1.26", valid_from="2026-09-01")
    assert answer.status_code == 200, answer.status_code
    assert "и так такое" in body(answer)
    client.post("/logout/")
    assert sql.execute("select count(*) from rule_presets").fetchone()[0] == 1


def test_a_country_value_outside_the_rules_is_refused(
    client, sql, presets_restored, platform_admin,
):
    """Мера работы, которой нет в правилах страны, не принимается и здесь."""
    login_as(client, "admin")
    answer = post_country(client, COURIERS_MEASURE, value="на глазок", valid_from="2026-09-01")
    assert answer.status_code == 400, answer.status_code
    assert "нет в правилах страны" in body(answer)
    client.post("/logout/")
    assert sql.execute("select count(*) from rule_presets").fetchone()[0] == 1


def test_the_name_of_a_country_rule_is_not_edited_here(
    client, sql, presets_restored, platform_admin,
):
    """Подпись правила страны с экрана не правится — она несёт все языки сразу.

    Записать введённое строкой значило бы затереть остальные языки, и увидел бы
    это тот, кто откроет продукт на другом языке: не автор правки и не сразу.
    """
    login_as(client, "admin")
    answer = post_country(client, NIGHT_TITLE, value="Ночь", valid_from="2026-09-01")
    assert answer.status_code == 400, answer.status_code
    assert "на всех языках сразу" in body(answer)
    html = body(client.get(f"/rules/{NIGHT_TITLE}/"))
    assert "Завести версию правил страны" not in html, "форма подписи всё-таки показана"
    client.post("/logout/")
    assert sql.execute("select count(*) from rule_presets").fetchone()[0] == 1
    # Все языки подписи на месте — правка их не тронула.
    stored = sql.execute(
        "select body -> 'hour_types' -> 'night' -> 'title' from rule_presets"
    ).fetchone()[0]
    assert set(stored) >= {"ru", "en"}, stored


# --- файл поверх правки в продукте не кладётся --------------------------------


def test_load_presets_keeps_what_was_changed_in_the_product(
    client, web_env, sql, presets_restored, platform_admin,
):
    """Повторная загрузка пресета не откатывает правку и говорит о пропуске.

    Опасен ровно один случай: правка датой самого пресета правит тело **по
    месту**, и `update_or_create` по ключу «код + дата» вернул бы файл поверх
    неё. Молча — то есть человек ушёл бы с уверенностью, что ставка поднята.
    """
    from core.rules import import_presets_detailed

    login_as(client, "admin")
    assert post_country(
        client, NIGHT_PERCENT, value="1.33", valid_from=PRESET_FROM
    ).status_code == 302
    client.post("/logout/")
    assert country_rule(NIGHT_PERCENT, JUNE) == 1.33

    result = import_presets_detailed()
    assert result.skipped == [PRESET_CODE], result
    assert result.loaded == [], result
    assert country_rule(NIGHT_PERCENT, JUNE) == 1.33, "файл вернулся поверх правки"


# --- право выдаётся вне приложения ---------------------------------------------


def test_the_right_is_granted_and_revoked_by_the_command(web_env, sql):
    """`manage.py platform_admin` — единственная дорога к этому праву.

    Подпроцессом, а не импортом: проверяется ровно то, что запустит человек
    руками. И проверяется целиком путь «нет права → выдали → отобрали»: команда,
    которая умеет выдать и не умеет отобрать, оставляет право навсегда.
    """
    from conftest import run_manage

    listed = run_manage(web_env, "platform_admin", "--list")
    assert listed.returncode == 0, listed.stderr
    assert "нет ни у кого" in listed.stdout, listed.stdout

    granted = run_manage(web_env, "platform_admin", "admin", "--note", "проверка")
    assert granted.returncode == 0, granted.stderr
    assert "Право выдано" in granted.stdout, granted.stdout
    assert sql.execute("select count(*) from platform_admins").fetchone()[0] == 1

    again = run_manage(web_env, "platform_admin", "admin")
    assert "уже есть" in again.stdout, again.stdout

    # Опечатка в логине не проходит молча: иначе человек ушёл бы с уверенностью,
    # что право выдано. Отказ громкий — ненулевой код возврата, поэтому
    # `run_manage` (он с `check=True`) на нём и падает; это и есть проверка.
    import subprocess

    with pytest.raises(subprocess.CalledProcessError) as refused:
        run_manage(web_env, "platform_admin", "нет-такого")
    assert "нет" in (refused.value.stderr or "") + (refused.value.output or "")

    revoked = run_manage(web_env, "platform_admin", "admin", "--revoke")
    assert revoked.returncode == 0, revoked.stderr
    assert "Право отобрано" in revoked.stdout, revoked.stdout
    assert sql.execute("select count(*) from platform_admins").fetchone()[0] == 0
