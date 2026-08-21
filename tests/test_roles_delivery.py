"""
Форма ролей из кода доезжает до поднятой базы (T169, issue #126).

Что здесь проверяется и почему именно так.

**Вердикт — отдельно от базы.** Четыре исхода на роль (совпадает / отстала /
правлена / неизвестно) это чистая логика, и проверять её на живом Postgres
значило бы платить секундами за арифметику. Поэтому сначала вердикт, потом база.

**Видимость — ролью `app_user`, и только ею.** Смысл задачи в том, что человек
после доставки видит и может другое. Владелец таблиц и суперпользователь
обходят политики, поэтому проверка «доставка поменяла то, что видно» на них
показала бы зелёное при любой ошибке в политиках — именно так дефект видимости
регистров прожил незамеченным. Всё, что про «видит» и «вправе», проверяется
через `set local role app_user`, как это делает сам сервис.

**Доставку, которая не могла состояться, надо уметь отличить от «нечего
делать».** `roles` под `force row level security`: роль, не обходящая политики,
увидит отсюда ноль строк, обновит ноль и отчитается «всё совпадает» — зелено и
неправда (тот же корень, что у issue #44). На это стоит отдельная проверка.

**Общая база веб-тестов не портится.** Каждая проверка, которая правит роли,
идёт внутри `transaction.atomic()` и заканчивается `set_rollback(True)`: роли
сида нужны всем остальным модулям такими, какими их положил сид. Роль
`app_user` выставляется тем же соединением (`set local role`), иначе изменения
незакоммиченной транзакции ей были бы не видны и проверять было бы нечего.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from conftest import MANAGE_PY, run_manage, temp_database
from core.role_delivery import (
    BEHIND,
    EDITED,
    MATCH,
    UNKNOWN,
    describe,
    diff_lines,
    from_jsonb,
    normalize,
    product_shape,
    sync,
    verdict,
)
from core.roles import ROLE_ORDER, ROLE_SHAPES

# --- вердикт: чистая логика, без базы ----------------------------------------


CODE = normalize(("official", "supplementary"), ("timesheet.edit", "unit.close"))
OLD = normalize(("official",), ("timesheet.edit",))
HAND = normalize(("official", "supplementary"), ("timesheet.edit",))


@pytest.mark.parametrize(
    ("db", "shipped", "expected", "why"),
    [
        (CODE, CODE, MATCH, "база и код сошлись — трогать нечего"),
        (CODE, None, MATCH, "код совпал, снимка нет — всё равно трогать нечего"),
        (OLD, OLD, BEHIND, "база ровно та, что поставил продукт, а код ушёл вперёд"),
        (HAND, CODE, EDITED, "снимок говорит одно, в базе другое — это правка человека"),
        (OLD, None, UNKNOWN, "снимка нет, база не совпадает — продукт не знает, что тут"),
        (OLD, HAND, EDITED, "и снимок, и код разошлись с базой — решает человек"),
    ],
)
def test_the_four_outcomes(db, shipped, expected, why):
    assert verdict(db=db, shipped=shipped, wanted=CODE) == expected, why


def test_the_order_of_the_permissions_is_not_a_change():
    """Переставленные права — та же форма.

    Не придирка: миграция `0110` дописывала право в конец списка, поэтому в
    живой базе порядок какой угодно. Сравнение списками объявило бы такую роль
    правленной человеком и перестало бы к ней ездить — молча и навсегда.
    """
    straight = normalize(("official", "supplementary"), ("timesheet.edit", "unit.close"))
    shuffled = normalize(("supplementary", "official"), ("unit.close", "timesheet.edit"))
    assert straight == shuffled
    # Дубль права формы тоже не меняет: множество, а не список.
    assert normalize(("official",), ("unit.close", "unit.close")) == normalize(
        ("official",), ("unit.close",)
    )


def test_every_product_role_has_a_deliverable_shape():
    """У каждой роли спеки форма собирается — иначе доставка упала бы на ней."""
    for code in ROLE_ORDER:
        shape = product_shape(code)
        assert shape["visible_ledgers"], f"у роли {code} нет ни одного регистра"
        assert set(shape["permissions"]) == set(ROLE_SHAPES[code].permissions)
        assert set(shape["visible_ledgers"]) == set(ROLE_SHAPES[code].ledgers)


def test_the_diff_names_what_changes():
    """Диф — то, что человек прочитает в выводе: что прибавилось, что убыло."""
    lines = diff_lines(OLD, CODE)
    assert "+ регистры: supplementary" in lines
    assert "+ права: unit.close" in lines
    assert diff_lines(CODE, CODE) == []
    assert "- права: unit.close" in diff_lines(CODE, OLD)


def test_an_unknown_ledger_is_refused():
    """Регистр не из словаря в базу не уезжает.

    Массив регистров собирается литералом, и сверка со словарём здесь — не
    формальность: она единственное, что стоит между опечаткой в `ROLE_SHAPES` и
    строкой, которую база отвергнет посреди доставки.
    """
    from core.role_delivery import _ledger_literal

    assert _ledger_literal(("official", "internal")) == "{official,internal}"
    with pytest.raises(ValueError, match="неизвестные регистры"):
        _ledger_literal(("official", "оффициальный"))


# --- живая база: роли сида, доставка, видимость -------------------------------


@pytest.fixture
def stand(web_env):
    """Отменяемая транзакция поверх общей базы веб-тестов.

    Роли сида нужны остальным модулям нетронутыми, а проверять доставку можно
    только на настоящих ролях. Поэтому вся правка идёт в транзакции, которая
    заведомо откатится, и в ней же выполняется переключение на `app_user`:
    отдельным соединением незакоммиченные строки были бы не видны.
    """
    from django.db import connection, transaction

    with transaction.atomic():
        yield connection
        transaction.set_rollback(True)


def _role(conn, code: str) -> tuple:
    """Роль из базы: регистры списком, права и снимок — разобранным json.

    Разбор обязателен: курсор Django отдаёт `jsonb` строкой (см. `from_jsonb`),
    и без него проверка сравнивала бы множества символов, а не прав.
    """
    with conn.cursor() as cur:
        cur.execute(
            "select id::text, visible_ledgers, permissions, shipped_shape"
            "  from roles where code = %s and tenant_id is not null",
            [code],
        )
        rows = cur.fetchall()
    assert len(rows) == 1, f"ожидалась одна роль {code}, найдено {len(rows)}"
    role_id, ledgers, permissions, shipped = rows[0]
    return role_id, list(ledgers), from_jsonb(permissions), from_jsonb(shipped)


def _set_role(conn, code: str, *, ledgers, permissions, shipped) -> None:
    """Привести роль к состоянию поднятого стенда — мимо доставки."""
    import json

    with conn.cursor() as cur:
        cur.execute(
            "update roles set visible_ledgers = %s::ledger[], permissions = %s::jsonb,"
            "       shipped_shape = %s::jsonb"
            " where code = %s and tenant_id is not null",
            [
                "{" + ",".join(ledgers) + "}",
                json.dumps(list(permissions)),
                json.dumps(shipped) if shipped is not None else None,
                code,
            ],
        )


def _as_app_user(conn, user_id: str, question: str, *params):
    """Спросить базу тем же способом, каким её спрашивает сервис.

    `set local role` — не педантизм: владелец схемы обходит `force row level
    security`, и на нём любой ответ про видимость означал бы только «владелец
    видит всё».
    """
    with conn.cursor() as cur:
        cur.execute("set local role app_user")
        cur.execute("select set_config('app.user_id', %s, true)", [user_id])
        try:
            cur.execute(question, list(params))
            return cur.fetchone()[0]
        finally:
            cur.execute("reset role")
            cur.execute("select set_config('app.user_id', '', true)")


def _ids(conn) -> dict:
    with conn.cursor() as cur:
        cur.execute("select id::text from tenants where code = 'rs-dev'")
        tenant = cur.fetchone()[0]
        cur.execute("select username, id::text from users where username = any(%s)",
                    [list(ROLE_ORDER)])
        users = dict(cur.fetchall())
        cur.execute("select id::text from units where tenant_id = %s and code = 'NS1'",
                    [tenant])
        unit = cur.fetchone()[0]
    return {"tenant": tenant, "users": users, "unit": unit}


def test_the_seed_records_what_it_shipped(stand):
    """Сид кладёт снимок формы вместе с ролью.

    Иначе первая же доставка увидела бы роль без снимка, приняла бы её за правку
    человека и обошла стороной — то есть механизм не работал бы ровно на тех
    базах, для которых сделан.
    """
    for code in ROLE_ORDER:
        _role_id, ledgers, permissions, shipped = _role(stand, code)
        assert shipped is not None, f"сид не записал снимок формы роли {code}"
        assert normalize(shipped["visible_ledgers"], shipped["permissions"]) == product_shape(code)
        assert normalize(ledgers, permissions) == product_shape(code)

    report = sync(stand, apply=True)
    assert report.in_sync, "сразу после сида доставке нечего делать"
    assert report.delivered == []


def test_a_role_behind_the_code_is_delivered(stand):
    """Стенд с прошлой формой роли догоняет код одной командой."""
    _set_role(
        stand, "accountant",
        ledgers=["official"], permissions=["timesheet.edit"],
        shipped={"visible_ledgers": ["official"], "permissions": ["timesheet.edit"]},
    )

    report = sync(stand, apply=True)

    delivered = [item.code for item in report.delivered]
    assert delivered == ["accountant"], f"довезено не то: {delivered}"
    _role_id, ledgers, permissions, shipped = _role(stand, "accountant")
    assert normalize(ledgers, permissions) == product_shape("accountant")
    # Снимок обязан обновиться вместе с формой: иначе следующая доставка примет
    # только что поставленное за правку человека.
    assert normalize(shipped["visible_ledgers"], shipped["permissions"]) == product_shape(
        "accountant"
    )
    # Порядок в базе — тот, что задал код, а не отсортированный: регистры
    # человек читает в шапке страницы подряд.
    assert list(ledgers) == list(ROLE_SHAPES["accountant"].ledgers)

    # Идемпотентность: второй прогон ничего не делает и говорит то же самое.
    again = sync(stand, apply=True)
    assert again.delivered == []
    assert again.in_sync


def test_a_partner_edit_is_not_overwritten(stand):
    """Правку человека доставка не трогает и показывает разъездом.

    Экран `/roles/` живёт с T171, то есть партнёр уже может снять роли право.
    Затереть это очередным деплоем — то же молчание, что и не доставить, только
    в другую сторону.
    """
    hand = [code for code in ROLE_SHAPES["director"].permissions if code != "retro.post"]
    _set_role(
        stand, "director",
        ledgers=list(ROLE_SHAPES["director"].ledgers), permissions=hand,
        shipped=product_shape("director"),
    )

    report = sync(stand, apply=True)

    assert [item.code for item in report.delivered] == []
    assert [item.code for item in report.by_state(EDITED)] == ["director"]
    _role_id, _ledgers, permissions, _shipped = _role(stand, "director")
    assert "retro.post" not in permissions, "доставка затёрла правку партнёра"
    assert not report.in_sync, "разъезд обязан оставаться видимым, а не считаться нормой"


def test_a_role_without_a_snapshot_waits_for_a_human(stand):
    """Роль без снимка не трогают, пока человек не скажет, что там его нет.

    Это стенды, поднятые до появления колонки: продукт не может отличить правку
    партнёра от своего отставания и не вправе решать за него. Выход из ожидания
    один и явный — `--adopt`.
    """
    _set_role(
        stand, "accountant",
        ledgers=["official"], permissions=["timesheet.edit"], shipped=None,
    )

    waiting = sync(stand, apply=True)
    assert [item.code for item in waiting.by_state(UNKNOWN)] == ["accountant"]
    assert waiting.delivered == []
    _role_id, _ledgers, permissions, _shipped = _role(stand, "accountant")
    assert list(permissions) == ["timesheet.edit"], "роль без снимка тронули без спроса"

    adopted = sync(stand, apply=True, adopt=True)
    assert [item.code for item in adopted.adopted] == ["accountant"]
    assert [item.code for item in adopted.delivered] == ["accountant"]
    _role_id, ledgers, permissions, _shipped = _role(stand, "accountant")
    assert normalize(ledgers, permissions) == product_shape("accountant")

    # Порядок строк отчёта — в порядке событий: сначала приняли, потом довезли.
    # Обратный порядок стоял здесь до приёмки и читался как «довезли, а потом
    # зачем-то приняли», то есть описывал механизм неверно.
    said = describe(adopted)
    assert said.index("принято как поставленное продуктом: rs-dev/accountant") < next(
        i for i, line in enumerate(said) if line.startswith("довезено:")
    ), f"отчёт рассказывает события в обратном порядке: {said}"


def test_check_writes_nothing(stand):
    """`--check` отвечает списком и не притрагивается к базе."""
    _set_role(
        stand, "accountant",
        ledgers=["official"], permissions=["timesheet.edit"],
        shipped={"visible_ledgers": ["official"], "permissions": ["timesheet.edit"]},
    )

    report = sync(stand, apply=False)

    assert [item.code for item in report.by_state(BEHIND)] == ["accountant"]
    assert report.delivered == []
    _role_id, ledgers, permissions, _shipped = _role(stand, "accountant")
    assert normalize(ledgers, permissions) != product_shape("accountant"), (
        "проверка изменила базу — тогда это не проверка"
    )


def test_the_delivery_changes_what_the_person_sees(stand):
    """Главное: после доставки человек видит другое — и это спрошено у базы.

    Проверяется ролью `app_user`, потому что на владельце схемы вопрос
    бессмысленен: он видит всё при любых политиках. Смотрим на управляющего
    точки — единственную роль с неполным набором регистров (D031): ему
    доставляется `supplementary`, и вместе с ним обязана появиться сумма
    надбавки его смены, которой до доставки не было видно вовсе.
    """
    ids = _ids(stand)
    manager = ids["users"]["manager"]

    # Материал: компонент выплаты дополнительного регистра на точке управляющего.
    with stand.cursor() as cur:
        cur.execute(
            # Точки у сотрудника нет — она живёт в условиях найма; срез по
            # точке для компонента делают `payslips.unit_id` и политика,
            # присоединяющая ведомость (`0011`).
            "insert into employees (tenant_id, external_id, first_name, last_name)"
            " values (%s, 't169-extra', 'Тест', 'Доставка') returning id",
            [ids["tenant"]],
        )
        employee = cur.fetchone()[0]
        cur.execute(
            "insert into payruns (tenant_id, period) values (%s, '2026-06-01')"
            " on conflict (tenant_id, period) do update set period = excluded.period"
            " returning id",
            [ids["tenant"]],
        )
        payrun = cur.fetchone()[0]
        cur.execute(
            "insert into payslips (tenant_id, payrun_id, employee_id, unit_id)"
            " values (%s, %s, %s, %s) returning id",
            [ids["tenant"], payrun, employee, ids["unit"]],
        )
        payslip = cur.fetchone()[0]
        cur.execute(
            "insert into pay_components"
            " (tenant_id, payslip_id, code, title, amount, ledger)"
            " values (%s, %s, 't169.bonus', 'Надбавка смены', 1234.00, 'supplementary')",
            [ids["tenant"], payslip],
        )

    # Стенд отстал: у управляющего только официальный регистр.
    _set_role(
        stand, "manager",
        ledgers=["official"], permissions=list(ROLE_SHAPES["manager"].permissions),
        shipped={
            "visible_ledgers": ["official"],
            "permissions": list(ROLE_SHAPES["manager"].permissions),
        },
    )

    ledgers_before = _as_app_user(
        stand, manager, "select app_visible_ledgers(%s)::text[]", ids["tenant"]
    )
    seen_before = _as_app_user(
        stand, manager,
        "select count(*) from pay_components where code = 't169.bonus'",
    )
    assert "supplementary" not in ledgers_before
    assert seen_before == 0, "материал теста виден и до доставки — проверять нечего"

    sync(stand, apply=True)

    ledgers_after = _as_app_user(
        stand, manager, "select app_visible_ledgers(%s)::text[]", ids["tenant"]
    )
    seen_after = _as_app_user(
        stand, manager,
        "select count(*) from pay_components where code = 't169.bonus'",
    )
    assert "supplementary" in ledgers_after, (
        "регистр не доехал до того, чем база отвечает на вопрос «что видно»"
    )
    assert seen_after == 1, "регистр в роли появился, а сумма человеку так и не видна"


def test_the_delivery_changes_what_the_person_may_do(stand):
    """То же со стороны прав: база отвечает на «вправе ли» иначе.

    Проверяется той же функцией, которую зовут все ограничивающие политики, —
    то есть проверяется механизм, а не наша копия его понимания.
    """
    ids = _ids(stand)
    accountant = ids["users"]["accountant"]
    _set_role(
        stand, "accountant",
        ledgers=list(ROLE_SHAPES["accountant"].ledgers), permissions=["timesheet.edit"],
        shipped={
            "visible_ledgers": list(ROLE_SHAPES["accountant"].ledgers),
            "permissions": ["timesheet.edit"],
        },
    )
    question = "select app_has_permission(%s, 'payrun.calculate')"

    assert _as_app_user(stand, accountant, question, ids["tenant"]) is False

    sync(stand, apply=True)

    assert _as_app_user(stand, accountant, question, ids["tenant"]) is True, (
        "право доставлено в таблицу, а база о нём не знает"
    )


def test_a_connection_that_cannot_see_the_roles_says_so(stand):
    """Доставка ролью без обхода политик обязана сказать словами, а не «всё ок».

    Это ловушка, на которую механизм и рассчитан: `roles` под `force row level
    security`, поэтому роль, не обходящая политики, увидит ноль строк, обновит
    ноль и честно отчитается «расхождений нет». Зелено и неправда — тот же
    корень, что у issue #44 и у проверки в конце миграции `0110`.

    Проверяется не только то, что оговорка сказана, но и то, что отчёт **не
    утверждает согласия**. Так он и выглядел на приёмке: первой строкой «роли
    совпадают с кодом (0 шт.)», оговорка — после. Первая строка и есть то, что
    человек запоминает, поэтому «сказано где-то ниже» здесь не годится.
    """
    _set_role(
        stand, "accountant",
        ledgers=["official"], permissions=["timesheet.edit"],
        shipped={"visible_ledgers": ["official"], "permissions": ["timesheet.edit"]},
    )

    with stand.cursor() as cur:
        cur.execute("set local role app_user")
    try:
        report = sync(stand, apply=False)
    finally:
        with stand.cursor() as cur:
            cur.execute("reset role")

    assert report.bypasses_rls is False, (
        "доставка не заметила, что смотрит на таблицу через политики"
    )
    assert report.states == [], "без контекста пользователя ролей не видно — это и есть ловушка"

    said = describe(report)
    assert "не обходит RLS" in said[0], (
        f"первой строкой отчёта сказано не про невидимые роли: {said}"
    )
    assert not any("совпадают с кодом" in line for line in said), (
        f"отчёт утверждает согласие, которого не может знать: {said}"
    )


# --- приёмка целиком: та самая одна команда на поднятом стенде ----------------


@pytest.fixture(scope="module")
def fresh_stand():
    """Своя база на модуль: приёмку нельзя ставить на общую.

    Здесь проверяется `migrate` и команда целиком, подпроцессом — то есть ровно
    то, что запустит человек на стенде. Общая база веб-тестов для этого не
    годится: `migrate` на ней тронул бы роли всех остальных модулей.
    """
    with temp_database("roles") as dsn:
        yield dsn


def _roles_sync(dsn: str, *args: str) -> subprocess.CompletedProcess:
    """Команда как её запускает человек, без `check=True`: код возврата — ответ."""
    env = {
        **os.environ,
        "DATABASE_URL": dsn,
        "SECRET_KEY": "test-only-not-a-secret",
        "DJANGO_SETTINGS_MODULE": "config.settings",
    }
    return subprocess.run(
        [sys.executable, str(MANAGE_PY), "roles_sync", *args],
        env=env, capture_output=True, text=True,
    )


def _drifted_role(dsn: str) -> None:
    """Поднятый стенд с ролью прошлой формы: то, что есть у владельца сейчас."""
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "insert into tenants (code, title, country_code, base_currency, report_currency)"
            " values ('rs-stand', 'Партнёр стенда', 'RS', 'RSD', 'EUR')"
            " on conflict (code) do nothing"
        )
        conn.execute(
            "update roles set visible_ledgers = '{official}'::ledger[],"
            "       permissions = '[\"timesheet.edit\"]'::jsonb,"
            "       shipped_shape = '{\"visible_ledgers\": [\"official\"],"
            "                         \"permissions\": [\"timesheet.edit\"]}'::jsonb"
            " where code = 'accountant'"
        )
        conn.execute(
            "insert into roles (tenant_id, code, title, visible_ledgers, permissions,"
            "                   shipped_shape)"
            " select id, 'accountant', 'Бухгалтер', '{official}'::ledger[],"
            "        '[\"timesheet.edit\"]'::jsonb,"
            "        '{\"visible_ledgers\": [\"official\"],"
            "          \"permissions\": [\"timesheet.edit\"]}'::jsonb"
            "   from tenants where code = 'rs-stand'"
            " on conflict (tenant_id, code) do nothing"
        )


def _accountant_permissions(dsn: str) -> list[str]:
    import psycopg

    with psycopg.connect(dsn, autocommit=True) as conn:
        # Здесь сырой psycopg, а не курсор Django, — и `jsonb` приезжает уже
        # разобранным списком. Разница ровно та, о которой говорит `from_jsonb`.
        return conn.execute(
            "select permissions from roles where code = 'accountant'"
        ).fetchone()[0]


def test_the_check_answers_with_a_list_and_a_nonzero_code(fresh_stand):
    """`--check` на отставшем стенде: список расхождений и ненулевой код возврата.

    Ненулевой код — чтобы расхождение можно было заметить проверкой, а не
    глазами владельца, ради чего задача и заведена.
    """
    _drifted_role(fresh_stand)

    checked = _roles_sync(fresh_stand, "--check")

    assert checked.returncode != 0, (
        f"проверка на отставшем стенде ответила нулём:\n{checked.stdout}\n{checked.stderr}"
    )
    assert "accountant" in checked.stdout, f"в отчёте нет отставшей роли:\n{checked.stdout}"
    assert "payrun.calculate" in checked.stdout, (
        f"в отчёте нет дифа — непонятно, чего роли не хватает:\n{checked.stdout}"
    )
    assert "timesheet.edit" in _accountant_permissions(fresh_stand)
    assert "payrun.calculate" not in _accountant_permissions(fresh_stand), (
        "проверка изменила базу"
    )


def test_one_command_brings_the_stand_to_the_code(fresh_stand):
    """Та самая одна идемпотентная команда: форма доезжает, повтор молчит."""
    _drifted_role(fresh_stand)

    done = _roles_sync(fresh_stand)

    assert done.returncode == 0, f"{done.stdout}\n{done.stderr}"
    assert "довезено" in done.stdout, f"команда не сказала, что сделала:\n{done.stdout}"
    delivered = _accountant_permissions(fresh_stand)
    assert set(ROLE_SHAPES["accountant"].permissions) <= set(delivered)

    again = _roles_sync(fresh_stand)
    assert again.returncode == 0
    assert "довезено" not in again.stdout, f"второй прогон снова что-то менял:\n{again.stdout}"
    assert _roles_sync(fresh_stand, "--check").returncode == 0


def test_migrate_delivers_the_shape(fresh_stand):
    """Форма едет на `migrate` — тем же шагом, что код, а не отдельной командой.

    Проверка именно на `migrate`, потому что деплой это он. Механизм на сигнале
    `post_migrate`, а не миграцией, ровно поэтому: миграция выполняется один раз
    и остаётся применённой, то есть следующая правка формы через неё не поедет —
    и каждая новая правка требовала бы новой миграции (`0110`, `0220`, `0230`).
    """
    _drifted_role(fresh_stand)
    assert "payrun.calculate" not in _accountant_permissions(fresh_stand)

    done = run_manage(fresh_stand, "migrate", "--no-input")

    assert "Роли" in done.stdout, f"migrate ничего не сказал про роли:\n{done.stdout}"
    assert set(ROLE_SHAPES["accountant"].permissions) <= set(
        _accountant_permissions(fresh_stand)
    ), "форма роли не доехала на migrate"

    # Второй `migrate` молчит: доставка идемпотентна и не шумит без причины.
    quiet = run_manage(fresh_stand, "migrate", "--no-input")
    assert "довезено" not in quiet.stdout, f"повторный migrate снова что-то менял:\n{quiet.stdout}"


def test_the_stand_check_is_green_on_a_freshly_seeded_base():
    """На базе, наполненной сидом, проверка зелёная с первого раза.

    Иначе механизм ругался бы на собственный сид, и человек привык бы пропускать
    его вывод — а привычка пропускать вывод и есть то, из-за чего T169 вообще
    случилась.
    """
    with temp_database("roles_seed") as dsn:
        run_manage(dsn, "seed_dev")
        checked = _roles_sync(dsn, "--check")
        assert checked.returncode == 0, f"{checked.stdout}\n{checked.stderr}"


def test_the_check_and_the_adopt_are_not_used_together(fresh_stand):
    """«Проверить» и «принять состояние» вместе — отказ, а не тихая запись."""
    refused = _roles_sync(fresh_stand, "--check", "--adopt")
    assert refused.returncode != 0
    assert "--adopt" in refused.stderr or "--adopt" in refused.stdout
