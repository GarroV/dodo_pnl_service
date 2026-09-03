"""Рамка страны: мягче — нельзя, и об этом говорят словами (T192, issue #182).

**Что было.** Уровень партнёра, группы и человека принимал любое значение в
любую сторону. Процент оплаты больничного — 0,65 по закону Сербии — опускался с
экрана правил до 0,10, форма отвечала «сохранено», расчёт послушно считал, и
единственным следом оставалась строка в `rule_overrides`, которую никто не
читает. Журнал блока `rules` записал этот долг за T165 прямо: «рамка НЕ
реализована… партнёр всегда вправе переопределить у себя любое значение в любую
сторону».

**Что проверяется здесь.** Пять требований, и все пять — из эталона модуля 17:

1. значение мягче страны **отвергается**, а не проходит молча и не обрезается;
2. отказ **называет границу, сторону и статью закона** — «мягче» без числа
   человек не выполнит, он переспросит;
3. рамка видна **до** правки: словом в списке правил и подсказкой у поля;
4. запертое законом правило формы не имеет вовсе — не «имеет и откажет»;
5. отвергнутая попытка **записана**: «Отказ, который никуда не записан,
   повторят завтра».

Строже страны при этом обязано проходить — рамка держит одну сторону, а не
запрещает движение. И правило без рамки обязано остаться свободным: рамка
ставится только там, где направление «мягче» названо законом, и проверка на
`rates.net_factor` держит именно это.

Половина про базу — в `test_frame_attempts_policy.py`: там роль `app_user`.
"""
from __future__ import annotations

import json
from datetime import date

import pytest

from conftest import body, content, login_as

JUNE = date(2026, 6, 1)
SEPTEMBER = "2026-09-01"

# Ночные часы: минимум закона 1,26 (Закон о раду, чл. 108). Правило удобно тем,
# что рамка у него односторонняя — платить больше можно, меньше нельзя.
NIGHT = "hour_types.night.pay_percent"
# Доплата до минимальной зарплаты: выключатель, запертый законом.
GUARANTEE = "minimum_guarantee.enabled"
# Правило без рамки: `net_factor` производен от ставок налога и взносов, и
# направление «мягче» у него не объявлено (см. комментарий в пресете).
FREE_RULE = "rates.net_factor"


@pytest.fixture
def sql(web_env):
    """Соединение владельцем схемы — только для подготовки и уборки."""
    import psycopg

    with psycopg.connect(web_env, autocommit=True) as conn:
        yield conn


@pytest.fixture
def overrides_restored(web_env, sql):
    """Переопределения правил не переживают теста.

    Копией, а не общей фикстурой: она живёт в `test_web_rules.py` и там же
    объяснена. Тащить её в `conftest.py` ради второго читателя значило бы
    сделать её общей для всех двухсот файлов прогона.
    """
    before = [row[0] for row in sql.execute("select id from rule_overrides").fetchall()]
    yield
    if before:
        sql.execute("delete from rule_overrides where id <> all(%s)", (before,))
    else:
        sql.execute("delete from rule_overrides")


@pytest.fixture
def attempts_restored(web_env, sql):
    """Журнал попыток не переживает теста.

    База стенда одна на весь прогон, а карточка правила показывает журнал
    целиком: строка, оставленная одним тестом, попала бы в проверку «журнала
    нет» у соседнего.
    """
    before = [row[0] for row in sql.execute("select id from rule_frame_attempts").fetchall()]
    yield
    if before:
        sql.execute("delete from rule_frame_attempts where id <> all(%s)", (before,))
    else:
        sql.execute("delete from rule_frame_attempts")


def post_rule(client, path: str, *, value: str, valid_from: str = SEPTEMBER, target=""):
    fields = {"value": value, "valid_from": valid_from}
    if target:
        fields["target"] = target
    return client.post(f"/rules/{path}/", fields)


def overrides_of(sql, path: str) -> int:
    return sql.execute(
        "select count(*) from rule_overrides where path = %s", (path,)
    ).fetchone()[0]


def attempts_of(sql, path: str) -> list[tuple]:
    return sql.execute(
        "select wanted, country_value, frame, scope_type, created_by_name "
        "from rule_frame_attempts where path = %s order by created_at",
        (path,),
    ).fetchall()


def tenant_id_of(code: str = "rs-dev"):
    from core.models import Tenant

    return Tenant.objects.filter(code=code).values_list("id", flat=True).first()


def group_id(code: str):
    from core.models import EmployeeGroup

    return (
        EmployeeGroup.objects.filter(tenant_id=tenant_id_of(), code=code)
        .values_list("id", flat=True)
        .first()
    )


def someone_from(group_code: str):
    from core.models import EmploymentTerm

    return (
        EmploymentTerm.objects.filter(
            tenant_id=tenant_id_of(), group__code=group_code, valid_from__lte=JUNE,
        )
        .exclude(valid_to__lte=JUNE)
        .values_list("employee_id", flat=True)
        .first()
    )


# --- мягче страны не проходит -------------------------------------------------


def test_a_value_softer_than_the_country_is_refused_with_words(
    client, sql, overrides_restored, attempts_restored,
):
    """1,10 при минимуме закона 1,26 — отказ, и в базе ничего не появилось.

    Проверка держит обе половины: отказ виден человеку И версии правила нет.
    Одна первая половина прошла бы и от того, что форма показала предупреждение,
    а значение всё равно записалось.
    """
    login_as(client, "admin")
    answer = post_rule(client, NIGHT, value="1.10")
    page = body(answer)
    client.post("/logout/")

    assert answer.status_code == 400, page[:600]
    assert "мягче правил страны" in page, page[:900]
    # Граница, сторона и источник — три вещи, без которых отказ не выполним.
    assert "1.26" in page, "отказ не назвал границу"
    assert "Закон о раду, чл. 108" in page, "отказ не назвал источник"
    assert "столько же или больше" in page, "отказ не сказал, куда двигаться"
    assert overrides_of(sql, NIGHT) == 0, "версия всё-таки записалась"


def test_a_stricter_value_passes(client, sql, overrides_restored, attempts_restored):
    """Рамка держит одну сторону, а не запрещает правку.

    Без этой проверки «рамка» и «запрет» были бы неразличимы, а эталон говорит
    ровно обратное: «юрлицо может стать строже рамки, но никогда — мягче».
    """
    login_as(client, "admin")
    answer = post_rule(client, NIGHT, value="1.40")
    client.post("/logout/")

    assert answer.status_code == 302, body(answer)[:600]
    assert overrides_of(sql, NIGHT) == 1
    assert attempts_of(sql, NIGHT) == [], "прошедшая правка записана как попытка"


def test_a_rule_without_a_frame_stays_free(client, sql, overrides_restored):
    """Правило, у которого рамки нет, по-прежнему ставится в любую сторону.

    Рамка ставится только там, где направление «мягче» назвал закон. Если бы
    механизм держал всё подряд, он отвергал бы верные правки словами про закон,
    которого нет, — и это хуже, чем отсутствие рамки.
    """
    login_as(client, "admin")
    answer = post_rule(client, FREE_RULE, value="0.65")
    client.post("/logout/")

    assert answer.status_code == 302, body(answer)[:600]
    assert overrides_of(sql, FREE_RULE) == 1


@pytest.mark.parametrize("level", ["group", "employee"])
def test_the_frame_holds_below_the_partner_too(
    client, sql, overrides_restored, attempts_restored, level,
):
    """Группе и человеку — тот же отказ, что и партнёру.

    Уровни ниже партнёра сильнее его, и рамка, проверенная только на верхнем,
    закрывалась бы обходом в один клик: то же значение, выбранный уровень
    «группа».
    """
    target = (
        f"group:{group_id('couriers')}" if level == "group"
        else f"employee:{someone_from('couriers')}"
    )
    login_as(client, "admin")
    answer = post_rule(client, NIGHT, value="1.10", target=target)
    client.post("/logout/")

    assert answer.status_code == 400, body(answer)[:600]
    assert overrides_of(sql, NIGHT) == 0
    written = attempts_of(sql, NIGHT)
    assert len(written) == 1, written
    assert written[0][3] == level, "попытка записана не на том уровне"


# --- запертое правило -----------------------------------------------------------


def test_a_locked_rule_has_no_form_at_all(client):
    """Формы у запертого законом правила нет, и вместо неё — объяснение.

    Форма, которая гарантированно откажет, — это работа, отнятая у человека до
    того, как он её сделал: он выберет уровень, наберёт дату и значение и только
    тогда прочитает, что так было нельзя с самого начала.
    """
    login_as(client, "admin")
    html = content(client.get(f"/rules/{GUARANTEE}/"))
    client.post("/logout/")

    assert "Завести версию" not in html, "форма правки показана у запертого правила"
    assert "не переопределяется" in html, html[:900]
    assert "Закон о раду, чл. 111" in html, "не назван источник запрета"
    # И пустая история не зовёт к форме, которой на странице нет: обещание
    # «первая же версия ниже» у запертого правила выполнить нечем.
    assert "Первая же версия ниже" not in html, "страница зовёт к несуществующей форме"


def test_a_locked_rule_refuses_a_request_sent_past_the_form(
    client, sql, overrides_restored, attempts_restored,
):
    """Запрос мимо формы отвергается так же: форма — удобство, а не запрет.

    Тот же класс дефекта, что нашёлся у списка допустимых значений в T165:
    `<select>` на форме был, а запрос, собранный мимо неё, приносил в базу что
    угодно.
    """
    login_as(client, "admin")
    answer = post_rule(client, GUARANTEE, value="false")
    page = body(answer)
    client.post("/logout/")

    assert answer.status_code == 400, page[:600]
    assert "задано законом" in page, page[:900]
    assert overrides_of(sql, GUARANTEE) == 0
    assert len(attempts_of(sql, GUARANTEE)) == 1


def test_saving_a_locked_rule_unchanged_is_not_an_attempt(
    client, sql, overrides_restored, attempts_restored,
):
    """Тот, кто ничего не поменял, не получает отказа и не попадает в журнал.

    Порядок проверок: сначала «ничего не изменилось», потом рамка. Обратный
    порядок наказывал бы за бездействие и наполнял журнал аудита событиями,
    которых не было.
    """
    login_as(client, "admin")
    answer = post_rule(client, GUARANTEE, value="true")
    page = body(answer)
    client.post("/logout/")

    assert answer.status_code == 200, page[:600]
    assert "Ничего не изменилось" in page, page[:900]
    assert attempts_of(sql, GUARANTEE) == []


# --- рамка видна до правки ------------------------------------------------------


def test_the_frame_is_named_on_the_rule_page(client):
    """На карточке правила видно, кто его меняет, в каких границах и почему."""
    login_as(client, "admin")
    html = content(client.get(f"/rules/{NIGHT}/"))
    client.post("/logout/")

    # Слово и метка стоят рядом, но в разных элементах: класс метки собирается
    # из режима рамки, а не из переведённого слова.
    assert "Кто меняет:" in html, html[:900]
    assert "mark--frame" in html and "Внутри рамки" in html, "рамка не помечена"
    assert "не ниже 1.26" in html, "границы рамки не названы"
    assert "Закон о раду, чл. 108" in html, "источник рамки не назван"


def test_the_frame_is_the_countrys_one_even_after_the_partner_tightened_it(
    client, sql, overrides_restored, attempts_restored,
):
    """Партнёр, поставивший себе строже, не двигает этим рамку страны.

    Граница «не ниже страны» разворачивается в значение, а в собранном пресете
    поверх страны лежит настройка партнёра. Прочитанная оттуда, подсказка
    назвала бы рамкой собственную правку человека: поставил 1,40 — и вот уже
    «не ниже 1,40», хотя вернуться к 1,30 закон не запрещает.
    """
    login_as(client, "admin")
    assert post_rule(client, NIGHT, value="1.40").status_code == 302
    html = content(client.get(f"/rules/{NIGHT}/?on=2026-09-01"))
    # А заодно и продукт обязан вести себя так же, как обещает экран.
    back = post_rule(client, NIGHT, value="1.30", valid_from="2026-10-01")
    client.post("/logout/")

    assert "не ниже 1.26" in html, html[:900]
    assert "не ниже 1.4" not in html, "рамкой названа правка самого партнёра"
    assert back.status_code == 302, body(back)[:600]


def test_the_rule_list_says_who_decides(client):
    """Список правил называет владельца значения — колонка эталона модуля 17."""
    login_as(client, "admin")
    html = content(client.get("/rules/?q=hour_types.night"))
    client.post("/logout/")

    assert "Кто меняет" in html, html[:900]
    assert "Внутри рамки" in html, "рамка не видна в списке"
    assert "mark--frame" in html, "рамка в списке не помечена"


def test_the_frames_section_is_not_a_rule(client):
    """Рамку с экрана не переопределить и в списке правил её нет.

    Рамка, которую двигает тот, кого она держит, рамкой быть перестаёт. Адрес
    отвечает «нет такого правила», а не отказом: отказ подтвердил бы, что
    правило существует, и предложил бы поискать обход.
    """
    login_as(client, "admin")
    listed = content(client.get("/rules/"))
    denied = client.get(f"/rules/frames.{NIGHT}/")
    posted = post_rule(client, f"frames.{NIGHT}", value="1.00")
    client.post("/logout/")

    assert "frames." not in listed, "раздел рамок показан среди правил"
    assert denied.status_code == 404
    assert posted.status_code == 404


# --- след отказа ----------------------------------------------------------------


def test_the_refused_attempt_is_written_down_and_shown(
    client, sql, overrides_restored, attempts_restored,
):
    """Попытка записана с автором, значением и снимком рамки — и видна на экране.

    Эталон модуля 17: «Попытка выйти за рамку — 24.07, Nikola Perić… Отказ,
    который никуда не записан, повторят завтра».
    """
    login_as(client, "admin")
    post_rule(client, NIGHT, value="1.10")
    html = content(client.get(f"/rules/{NIGHT}/"))
    client.post("/logout/")

    written = attempts_of(sql, NIGHT)
    assert len(written) == 1, written
    wanted, country_value, frame, scope_type, who = written[0]
    assert wanted == 1.10
    # Значение страны лежит рядом со снимком рамки: рамка со временем едет, и
    # строка обязана читаться через год без пересборки пресета той даты.
    assert country_value == 1.26
    assert frame["source"] == "Закон о раду, чл. 108"
    assert scope_type == "tenant"
    assert who, "автор попытки не записан"

    assert "Попытки выйти за рамку" in html, html[:900]
    assert who in html, "автор попытки не показан"
    assert "1.1" in html, "значение попытки не показано"


def test_without_attempts_the_page_says_nothing_about_them(client):
    """Пустого журнала на экране нет: он рассказывал бы о механизме тому, кто с
    ним не столкнулся."""
    login_as(client, "admin")
    html = content(client.get(f"/rules/{FREE_RULE}/"))
    client.post("/logout/")

    assert "Попытки выйти за рамку" not in html, html[:900]


# --- рамка поднялась, а значение осталось ---------------------------------------


def test_a_value_that_became_softer_is_named_on_the_page(
    client, sql, overrides_restored, attempts_restored,
):
    """Страна подняла минимум — действующее переопределение стало нарушением.

    Проверка при записи такое значение не пропустила бы, но заведено оно было
    раньше, чем рамка стала такой: индексация минималки двигает страну, а
    переопределение партнёра остаётся прежним, и расчёт продолжает считать по
    нему. Продукт видит обе величины на одной странице — молчать здесь было бы
    худшим из возможных поведений.
    """
    sql.execute(
        "insert into rule_overrides (tenant_id, scope_type, path, value, valid_from) "
        "select id, 'tenant', %s, '1.1'::jsonb, date '2026-09-01' "
        "from tenants where code = 'rs-dev'",
        (NIGHT,),
    )
    login_as(client, "admin")
    html = content(client.get(f"/rules/{NIGHT}/?on=2026-09-01"))
    client.post("/logout/")

    assert "Действующее значение вне рамки страны" in html, html[:1200]
    assert "мягче рамки страны" in html, "не сказано, чем именно оно не подходит"
    assert "не ниже 1.26" in html, "не названа граница"


def test_a_value_equal_to_the_country_is_not_called_a_breach(client):
    """Правило, которого никто не трогал, нарушением себя не объявляет.

    У запертого правила любое переопределение — нарушение, а отсутствие
    переопределения нарушением не является. Без этой оговорки плашка «вне
    рамки» висела бы на каждом запертом правиле всегда.
    """
    login_as(client, "admin")
    locked = content(client.get(f"/rules/{GUARANTEE}/"))
    framed = content(client.get(f"/rules/{NIGHT}/"))
    client.post("/logout/")

    assert "вне рамки страны" not in locked, locked[:900]
    assert "вне рамки страны" not in framed, framed[:900]


# --- рамка версионируется вместе со страной -------------------------------------


def test_the_frame_is_taken_from_the_country_version_of_that_date(
    client, sql, overrides_restored, attempts_restored,
):
    """Правка меряется рамкой того времени, а не сегодняшней.

    Рамка лежит в теле страны и живёт его версиями: индексация минималки
    двигает и значение, и границу. Версия, заведённая задним числом, обязана
    меряться рамкой, действовавшей тогда, — иначе закрытые месяцы пришлось бы
    перечитывать каждый раз, когда страна поднимает минимум.
    """
    version = sql.execute(
        "select body from rule_presets where country_code = 'RS' "
        "and valid_from <= date '2026-06-01' order by valid_from desc limit 1"
    ).fetchone()[0]
    softer = json.loads(json.dumps(version))
    softer["hour_types"]["night"]["pay_percent"] = 1.05
    softer["valid_from"] = "2026-10-01"
    sql.execute(
        "update rule_presets set valid_to = date '2026-10-01' "
        "where country_code = 'RS' and valid_to is null"
    )
    new_id = sql.execute(
        "insert into rule_presets (code, title, country_code, body, valid_from) "
        "values ('serbia-2026', 'Сербия, октябрь', 'RS', %s, date '2026-10-01') "
        "returning id",
        (json.dumps(softer),),
    ).fetchone()[0]
    try:
        login_as(client, "admin")
        # Сентябрь считается по старой версии: 1,10 всё ещё мягче 1,26.
        refused = post_rule(client, NIGHT, value="1.10", valid_from="2026-09-01")
        # Октябрь — по новой: там минимум уже 1,05, и то же значение проходит.
        allowed = post_rule(client, NIGHT, value="1.10", valid_from="2026-10-01")
        client.post("/logout/")

        assert refused.status_code == 400, body(refused)[:600]
        assert allowed.status_code == 302, body(allowed)[:600]
    finally:
        sql.execute("delete from rule_presets where id = %s", (new_id,))
        sql.execute(
            "update rule_presets set valid_to = null "
            "where country_code = 'RS' and valid_to = date '2026-10-01'"
        )


# --- пресет описан верно ---------------------------------------------------------


def test_every_frame_in_the_serbian_preset_is_usable(serbia_preset):
    """Рамка, которую нельзя применить, — это отсутствующая рамка.

    Проверяется на файле, а не на базе: ошибку в описании рамки надо ловить до
    первичной загрузки страны, пока она стоит одну правку YAML.
    """
    from payroll.frames import problems_in

    assert problems_in(serbia_preset) == []


def test_frames_do_not_leak_into_the_rules_of_the_engine(serbia_preset):
    """Раздел рамок не правило: движок его не видит и в расчёт он не входит.

    Зеркальный раздел лежит в том же теле, что и правила, — значит соблазн
    прочитать его как правило есть у всех, кто это тело обходит.
    """
    from web import rules

    listed = [leaf.path for leaf in rules.leaves(serbia_preset)]
    assert [path for path in listed if path.startswith("frames")] == []


# --- брак в рамке не доезжает до базы -------------------------------------------
#
# Находка приёмки T192, блокирующая. `problems_in` обещала докстрингом, что её
# зовёт первичная загрузка, — а звал её только тест. Барьера не было нигде.
#
# Чем это кончалось. Тело пресета лежит без `tenant_id`: одну строку читают ВСЕ
# партнёры страны. Разбор рамок (`frames_of`) обходит весь раздел на каждое
# чтение и падает на первом плохом узле — независимо от того, какое правило
# сохраняют. Значит один опечатанный узел, однажды загруженный в базу, закрывал
# правку ЛЮБОГО правила ЛЮБОМУ партнёру этой страны. И не отказом формы:
# `FrameMisconfigured` — не `RuleInputRefused`, и `except` на формах его не
# ловит, наружу выходила голая пятисотая.
#
# Единственный канал попадания брака — правка YAML плюс `load_presets`. Барьер
# поэтому стоит там, и проверки ниже держат именно его.


@pytest.fixture
def presets_restored(web_env, sql):
    """Тело правил страны не переживает теста.

    Нужна не «на всякий случай»: если барьер сломается, тест ниже положит в
    общую базу стенда пресет с неприменимой рамкой — и следующий, кто откроет
    любое правило, получит ту самую пятисотую.
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
            (json.dumps(row_body), valid_from, valid_to, edited_at, row_id),
        )


def broken_preset():
    """Пресет с одной опечаткой в режиме рамки — и корректный во всём остальном.

    Узел выбран НЕ связанным с тем правилом, которое потом сохраняют: в этом и
    была суть дефекта — падало сохранение постороннего правила.
    """
    from payroll.presets import Preset, load_preset_body

    body = json.loads(json.dumps(dict(load_preset_body("serbia-2026")), default=str))
    body["frames"]["constants"]["typo_field"] = {"mode": "framee", "source": "опечатка"}
    return Preset(body)


def test_a_typo_in_a_frame_stops_the_load_and_writes_nothing(
    web_env, sql, presets_restored, monkeypatch,
):
    """Опечатка в рамке валит первичную загрузку — и база остаётся прежней."""
    from core import rules as core_rules
    from payroll.frames import FrameMisconfigured

    monkeypatch.setattr(core_rules, "load_preset_body", lambda code: broken_preset())
    before = sql.execute("select count(*), max(body::text) from rule_presets").fetchone()

    with pytest.raises(FrameMisconfigured) as refusal:
        core_rules.import_presets_detailed(["serbia-2026"])

    words = str(refusal.value)
    assert "serbia-2026.yaml" in words, words
    assert "framee" in words, "отказ не назвал саму опечатку"
    assert "не положено ничего" in words, "отказ не сказал, что база не тронута"
    assert sql.execute(
        "select count(*), max(body::text) from rule_presets"
    ).fetchone() == before, "сломанный пресет всё-таки доехал до базы"


def test_the_load_command_refuses_in_words_not_a_traceback(
    web_env, presets_restored, monkeypatch,
):
    """`manage.py load_presets` отвечает отказом, а не трассировкой.

    Загрузку зовут из скриптов развёртывания, и трассировка там читается как
    «упало что-то в Django», а не как «поправь опечатку в YAML вот в этой
    строке».
    """
    from django.core.management import call_command
    from django.core.management.base import CommandError

    from core import rules as core_rules

    monkeypatch.setattr(core_rules, "load_preset_body", lambda code: broken_preset())
    with pytest.raises(CommandError) as refusal:
        call_command("load_presets", "serbia-2026")
    assert "serbia-2026.yaml" in str(refusal.value), str(refusal.value)


def test_nothing_is_written_when_a_later_preset_is_broken(
    web_env, sql, presets_restored, monkeypatch,
):
    """Сломанный второй файл не оставляет базу с загруженным первым.

    Проверка порядка, а не отказа: сначала проверяются ВСЕ запрошенные пресеты и
    только потом пишется первый. Иначе состояние базы зависело бы от того, в
    каком порядке перечислили страны.
    """
    from core import rules as core_rules
    from payroll.frames import FrameMisconfigured
    from payroll.presets import load_preset_body as real_body

    def one_of_two(code: str):
        return real_body("serbia-2026") if code == "good" else broken_preset()

    monkeypatch.setattr(core_rules, "load_preset_body", one_of_two)
    before = sql.execute("select count(*) from rule_presets").fetchone()[0]

    with pytest.raises(FrameMisconfigured):
        core_rules.import_presets_detailed(["good", "bad"])

    assert sql.execute(
        "select count(*) from rule_presets"
    ).fetchone()[0] == before, "первый пресет записан, хотя второй сломан"
