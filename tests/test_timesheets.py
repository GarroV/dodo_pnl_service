"""
Табель: подневное хранение, сетка ввода и сохранение ячейки (T019).

Три уровня проверок, и каждый ловит своё:

1. **Раскладка** — чистая функция, без базы. Здесь проверяется главное свойство
   подневного хранения: сумма дней в точности равна введённому числу. Если оно
   не держится, часы начинают «усыхать» от самого факта хранения.
2. **Запись** — на живой базе. Строка переходит на подневное хранение целиком,
   инвариант «итог равен сумме дней» держится после каждой правки, а расчёт
   после перехода даёт те же суммы, что и до него.
3. **Экран и доступ** — через настоящий HTTP-клиент и, отдельно, ролью
   `app_user`: политики базы на владельца таблиц не действуют, и проверять их
   владельцем — значит проверять ничего.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import T1, T2, USER_DIRECTOR, USER_MANAGER, as_app_user, body, login_as

JUNE = date(2026, 6, 1)


# =============================================================================
# 1. Раскладка месячного числа по дням
# =============================================================================


def test_working_days_skips_weekends():
    from timesheets.spread import working_days

    days = working_days(JUNE)
    assert len(days) == 22  # июнь 2026: 30 дней, 8 выходных
    assert days[0] == date(2026, 6, 1)
    assert all(day.weekday() < 5 for day in days)


def test_working_days_skips_holidays():
    from timesheets.spread import working_days

    days = working_days(JUNE, holidays=[date(2026, 6, 1), date(2026, 6, 6)])
    # 6 июня — суббота, её и так не было: выходной вычитается один раз.
    assert len(days) == 21
    assert date(2026, 6, 1) not in days


def test_spread_keeps_total_exactly():
    """Главное свойство: сумма дней равна введённому числу до копейки часа."""
    from timesheets.spread import spread, working_days

    days = working_days(JUNE)
    for total in ("176.00", "100.00", "0.01", "88.00", "13.13"):
        parts = spread(Decimal(total), days)
        assert sum(parts.values()) == Decimal(total), total


def test_spread_distributes_remainder_over_first_days():
    from timesheets.spread import spread

    days = [date(2026, 6, 1), date(2026, 6, 2), date(2026, 6, 3)]
    parts = spread(Decimal("10.00"), days)
    assert [parts[day] for day in days] == [
        Decimal("3.34"), Decimal("3.33"), Decimal("3.33"),
    ]


def test_spread_of_zero_gives_no_days():
    """Ноль часов — это отсутствие строк, а не двадцать две строки с нулём."""
    from timesheets.spread import spread

    assert spread(Decimal("0"), [date(2026, 6, 1)]) == {}


def test_spread_without_days_refuses():
    from timesheets.spread import spread

    with pytest.raises(ValueError):
        spread(Decimal("8"), [])


# =============================================================================
# 2. Запись: переход строки на подневное хранение
# =============================================================================


@pytest.fixture
def one_row(web_env):
    """Строка табеля сида, к которой можно придираться. Дни сносятся заранее."""
    from core.models import Timesheet, TimesheetDay

    row = (
        Timesheet.objects.select_related("employee")
        .filter(period=JUNE)
        .order_by("employee__external_id")
        .first()
    )
    TimesheetDay.objects.filter(timesheet=row).delete()
    before = dict(row.hours)
    insured = row.insured_hours
    yield row
    # Возвращаем строку к исходному виду: в этой же базе живут тесты сумм.
    # База для взносов возвращается вместе с часами — иначе оставленное здесь
    # расхождение остановит расчёт в следующем тесте, и он будет красным не
    # по своей причине.
    TimesheetDay.objects.filter(timesheet=row).delete()
    Timesheet.objects.filter(pk=row.pk).update(hours=before, insured_hours=insured)


def test_first_edit_materializes_whole_row(one_row):
    """Правится одна ячейка — на дни переходит вся строка, а не только она.

    Иначе строка осталась бы наполовину подневной, и «итог равен сумме дней»
    перестало бы быть проверяемым утверждением.
    """
    from core.models import TimesheetDay
    from timesheets import store

    kinds_before = {k for k, v in one_row.hours.items() if Decimal(v) != 0}
    assert kinds_before, "у строки сида нет ни одного типа часов — тест бессмысленен"

    store.set_cell(timesheet=one_row, hour_type="vacation", hours=Decimal("8.00"))

    kinds_after = set(
        TimesheetDay.objects.filter(timesheet=one_row).values_list("hour_type", flat=True)
    )
    assert kinds_before <= kinds_after
    assert "vacation" in kinds_after


def test_total_equals_sum_of_days(one_row):
    from timesheets import store

    store.set_cell(timesheet=one_row, hour_type="regular", hours=Decimal("176.00"))
    for kind, total in store.daily_totals(one_row).items():
        assert Decimal(one_row.hours[kind]) == total, kind


def test_repeated_save_changes_nothing(one_row):
    from core.models import TimesheetDay
    from timesheets import store

    store.set_cell(timesheet=one_row, hour_type="sick", hours=Decimal("16.00"))
    first = sorted(
        TimesheetDay.objects.filter(timesheet=one_row, hour_type="sick")
        .values_list("work_date", "hours")
    )
    store.set_cell(timesheet=one_row, hour_type="sick", hours=Decimal("16.00"))
    second = sorted(
        TimesheetDay.objects.filter(timesheet=one_row, hour_type="sick")
        .values_list("work_date", "hours")
    )
    assert first == second


def test_zero_clears_days(one_row):
    from core.models import TimesheetDay
    from timesheets import store

    store.set_cell(timesheet=one_row, hour_type="sick", hours=Decimal("16.00"))
    store.set_cell(timesheet=one_row, hour_type="sick", hours=Decimal("0"))
    assert not TimesheetDay.objects.filter(timesheet=one_row, hour_type="sick").exists()
    assert Decimal(one_row.hours.get("sick", "0")) == 0


def test_negative_hours_refused(one_row):
    from timesheets import store

    with pytest.raises(store.CellRefused):
        store.set_cell(timesheet=one_row, hour_type="regular", hours=Decimal("-1"))


def test_unknown_hour_type_refused(one_row):
    """Тип часа, которого нет в правилах страны, движок посчитать не сможет."""
    from timesheets import store

    with pytest.raises(store.CellRefused):
        store.set_cell(timesheet=one_row, hour_type="teleportation", hours=Decimal("8"))


def test_timesheet_for_reads_days(one_row):
    """Контракт блока: то, что принимает движок, собирается из подневных данных."""
    from timesheets import store

    store.set_cell(timesheet=one_row, hour_type="regular", hours=Decimal("176.00"))
    sheet = store.timesheet_for(one_row.employee_id, JUNE)
    assert sheet.hours["regular"] == Decimal("176.00")
    assert sheet.insured_hours == one_row.insured_hours
    assert sheet.norm_hours == one_row.norm_hours


# =============================================================================
# 2а. База для взносов (T061)
#
# Она вход движка наравне с часами: по ней считаются взносы и бруто. Значит
# после правки часов она обязана либо пойти за ними, либо громко разойтись —
# устаревшая база даёт правдоподобно неверный расчёт без единого падения.
# =============================================================================


def test_insured_base_follows_hours_when_it_tracked_them(one_row):
    """База шла за часами — идёт и дальше. Ровно случай сверки: +8,5 часа."""
    from timesheets import store

    known = store.hour_types(one_row.tenant_id, JUNE, store.country_of(one_row.tenant_id))
    before = store.insured_base(one_row.hours or {}, known)
    from core.models import Timesheet as Row

    Row.objects.filter(pk=one_row.pk).update(insured_hours=before)
    one_row.insured_hours = before

    store.set_cell(timesheet=one_row, hour_type="holiday", hours=Decimal("8.50"))

    one_row.refresh_from_db()
    assert store.insured_base(one_row.hours, known) == before + Decimal("8.50")
    assert one_row.insured_hours == before + Decimal("8.50")


def test_insured_base_set_apart_is_never_guessed(one_row):
    """База, заданная отдельно от часов, — число бухгалтера, а не наше.

    Пересчитать её от часов значило бы придумать правило за него (Q005).
    Поэтому строка остаётся как есть, а расхождение показывается и мешает
    считать — см. `test_calculation_refuses_on_stale_insured_base`.
    """
    from core.models import Timesheet as Row
    from timesheets import store

    Row.objects.filter(pk=one_row.pk).update(insured_hours=Decimal("40.00"))
    one_row.insured_hours = Decimal("40.00")

    store.set_cell(timesheet=one_row, hour_type="holiday", hours=Decimal("8.50"))

    one_row.refresh_from_db()
    assert one_row.insured_hours == Decimal("40.00")


def test_insured_base_counts_only_insured_hour_types():
    """Какие типы часов входят в базу, объявляет пресет, а не код интерфейса."""
    from timesheets.store import insured_base

    known = {
        "regular": {"insured": True},
        "sick": {"insured": False},
        "vacation": {},  # без флага — считаем входящим, занижать базу опаснее
    }
    hours = {"regular": "100", "sick": "20", "vacation": "8", "unknown": "999"}
    assert insured_base(hours, known) == Decimal("108")


def test_grid_marks_row_where_base_diverges_from_hours(client):
    from core.models import Timesheet as Row

    login_as(client, "director")
    url = grid_url(client)
    row = Row.objects.filter(period=JUNE).order_by("employee__external_id").first()
    Row.objects.filter(pk=row.pk).update(insured_hours=Decimal("3.00"))
    try:
        html = body(client.get(url))
        assert "insured-mismatch" in html
    finally:
        # Расхождение останавливает расчёт периода — оставленное здесь, оно
        # красит следующие тесты в красный не по их вине.
        Row.objects.filter(pk=row.pk).update(insured_hours=row.insured_hours)


def test_cell_answer_refreshes_base_on_screen(client, period_restored):
    """После правки часов новая база приезжает на экран, а не остаётся старой."""
    from core.models import Timesheet as Row

    login_as(client, "director")
    url = grid_url(client)
    html = body(client.get(url))
    row_id, kind = _first_cell(html)
    row = Row.objects.get(pk=row_id)

    response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "9.00"})
    assert response.status_code == 200
    answer = body(response)
    assert f'id="insured-{row_id}"' in answer
    row.refresh_from_db()
    assert f"{row.insured_hours:.2f}".replace(".", ",") in answer


def _row_with_scheme(scheme: str):
    """Строка табеля сотрудника, которого считают по этой схеме.

    Схема берётся так же, как её берёт расчёт: у условий найма, а если там
    пусто — у группы.
    """
    from core.models import EmploymentTerm
    from core.models import Timesheet as Row

    for row in Row.objects.filter(period=JUNE).select_related("employee"):
        term = (
            EmploymentTerm.objects.filter(employee_id=row.employee_id)
            .select_related("group")
            .order_by("-valid_from")
            .first()
        )
        if term and (term.scheme or term.group.scheme) == scheme:
            return row
    raise AssertionError(f"в сиде нет никого со схемой {scheme} — тест бессмысленен")


def test_calculation_refuses_on_stale_insured_base(web_env):
    """Расхождение базы и часов не даёт посчитать период молча."""
    from core.models import Timesheet as Row
    from payrun.calc import calculate_period
    from payrun.errors import PayrunRefused

    all_ledgers = ["official", "supplementary", "internal"]
    victim = _row_with_scheme("standard")
    before = victim.insured_hours
    Row.objects.filter(pk=victim.pk).update(insured_hours=before + Decimal("7"))
    try:
        with pytest.raises(PayrunRefused) as refusal:
            calculate_period(
                tenant_id=victim.tenant_id, period=JUNE, visible_ledgers=all_ledgers
            )
        assert "база" in str(refusal.value).lower()
        assert victim.employee.external_id in refusal.value.details
    finally:
        Row.objects.filter(pk=victim.pk).update(insured_hours=before)


def test_calculation_ignores_base_where_scheme_does_not_use_it(web_env):
    """У схем без базы взносов (курьеры, временные) расхождение ничего не портит.

    Требовать от них сходимости значило бы блокировать расчёт из-за числа,
    которое ни в одну формулу не входит.
    """
    from conftest import wipe_payruns
    from core.models import Timesheet as Row
    from payrun.calc import calculate_period

    all_ledgers = ["official", "supplementary", "internal"]
    victim = _row_with_scheme("direct")
    before = victim.insured_hours
    Row.objects.filter(pk=victim.pk).update(insured_hours=before + Decimal("7"))
    try:
        wipe_payruns(web_env)
        outcome = calculate_period(
            tenant_id=victim.tenant_id, period=JUNE, visible_ledgers=all_ledgers
        )
        assert outcome.components > 0
    finally:
        Row.objects.filter(pk=victim.pk).update(insured_hours=before)
        wipe_payruns(web_env)


# =============================================================================
# Расчёт после перехода на подневное хранение
# =============================================================================


def test_calculation_survives_materialization(web_env, period_restored):
    """Перевод всех строк на дни не двигает ни одной суммы.

    Проверка ровно того, чем можно испортить принятую вторую очередь: раскладка
    сохраняет итог, значит движок получает тот же вход. Если тест красный —
    подневное хранение вносит арифметику, которой не должно быть.
    """
    from conftest import wipe_payruns
    from core.models import Timesheet, TimesheetDay
    from payrun.calc import calculate_period

    all_ledgers = ["official", "supplementary", "internal"]
    tenant_id = Timesheet.objects.filter(period=JUNE).first().tenant_id

    wipe_payruns(web_env)
    before = calculate_period(tenant_id=tenant_id, period=JUNE, visible_ledgers=all_ledgers)
    totals_before = _component_totals(before.payrun_id)

    from timesheets import store

    rows = list(Timesheet.objects.filter(period=JUNE))
    for row in rows:
        store.materialize(row)
    assert TimesheetDay.objects.filter(timesheet__in=rows).exists()

    wipe_payruns(web_env)
    after = calculate_period(tenant_id=tenant_id, period=JUNE, visible_ledgers=all_ledgers)
    assert _component_totals(after.payrun_id) == totals_before
    assert after.components == before.components


def _component_totals(payrun_id) -> dict:
    from django.db.models import Sum

    from core.models import PayComponent

    return {
        row["code"]: row["total"]
        for row in PayComponent.objects.filter(payslip__payrun_id=payrun_id)
        .values("code").annotate(total=Sum("amount")).order_by("code")
    }


# =============================================================================
# 3. Экран
# =============================================================================
# Каждый тест, который пишет в табель с экрана, берёт `period_restored`
# (`conftest.py`): база `web_env` одна на весь прогон, и оставленное здесь
# число двигает контрольные суммы у всех, кто считает период после. Пока
# возврата не было, прогон держался на случайности — `test_timesheet_import.py`
# сортируется по имени раньше этого файла и успевал посчитать до порчи.


def grid_url(client) -> str:
    import re

    from conftest import period_url

    match = re.search(r"([0-9a-f-]{36})", period_url(client))
    return f"/timesheets/{match.group(1)}/"


def test_grid_shows_every_employee_of_the_period(client):
    login_as(client, "director")
    html = body(client.get(grid_url(client)))
    # Ячейка — обычное поле ввода: Tab-порядок и фокус браузер даёт сам.
    assert html.count('data-kind=') >= 35 * 4
    assert "Отработанные" in html and "Больничный" in html


def test_grid_needs_login(client):
    response = client.get(grid_url_anonymous(client))
    assert response.status_code == 302
    assert "/login/" in response["Location"]


def grid_url_anonymous(client) -> str:
    """Адрес сетки без входа: берём его директором, затем выходим."""
    login_as(client, "director")
    url = grid_url(client)
    client.post("/logout/")
    return url


def test_cell_saves_and_survives_reload(client, period_restored):
    """Ввёл, ушёл со страницы, вернулся — число на месте."""
    login_as(client, "director")
    url = grid_url(client)
    html = body(client.get(url))
    row_id, kind = _first_cell(html)

    response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "111.25"})
    assert response.status_code == 200
    # Значение возвращается заголовком, а не в теле: тело подменяет только итоги,
    # чтобы ответ сервера не вырывал поле из-под курсора.
    assert response["X-Cell-Value"] == "111.25"
    assert "hx-swap-oob" in body(response)

    again = body(client.get(url))
    assert 'value="111.25"' in again


def test_cell_accepts_comma(client, period_restored):
    """«8,5» — то, что наберёт человек с русской или сербской раскладкой."""
    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))

    response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "8,5"})
    assert response.status_code == 200
    assert response["X-Cell-Value"] == "8.50"


def test_cell_refuses_negative(client):
    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))

    response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "-5"})
    assert response.status_code == 422
    # Именно в поле ввода: подстрока «-5» встречается в любом uuid на странице.
    assert 'value="-5"' not in body(client.get(url))


def test_cell_refuses_text(client):
    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))

    response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "восемь"})
    assert response.status_code == 422


def test_grid_explains_missing_rules_instead_of_500(client):
    """Правил на период в базе нет — табель объясняет это, а не падает (T062).

    Страница периода этот случай уже объясняла; табель отдавал 500, и при
    выключенной отладке человек видел бы голое «Server Error».
    """
    from core.models import RulePreset

    login_as(client, "director")
    url = grid_url(client)
    RulePreset.objects.update(valid_from=date(2030, 1, 1))
    try:
        response = client.get(url)
        assert response.status_code == 409
        html = body(response)
        assert "нет правил расчёта" in html
        assert "load_presets" in html
    finally:
        RulePreset.objects.update(valid_from=date(2026, 1, 1))


def test_cell_explains_missing_rules_instead_of_500(client, period_restored):
    """Тот же отказ, но на записи ячейки, а не на открытии страницы (T073).

    Сценарий узкий и оттого незаметный: страница открылась, когда правила ещё
    действовали, а значение ячейки уходит на сервер уже после того, как они
    перестали. `grid` это объясняет с T062, `cell` падал пятисоткой — то есть
    показывал «Server Error» ровно тому, кто набирал часы.

    Проверяется здесь и механизм показа: отказ приходит теми же тремя вещами,
    что и остальные отказы ячейки, — код ответа, текст для человека и значение,
    оставшееся в базе, заголовком. Третьего способа объяснить отказ на этом
    экране быть не должно.
    """
    from core.models import RulePreset, Timesheet

    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))
    client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "12.00"})

    RulePreset.objects.update(valid_from=date(2030, 1, 1))
    try:
        response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "9"})
        assert response.status_code == 409, "правил нет — это отказ, а не ошибка сервера"
        text = response.content.decode()
        assert "нет правил расчёта" in text
        assert "load_presets" in text
        assert response["X-Cell-Value"] == "12.00"
    finally:
        RulePreset.objects.update(valid_from=date(2026, 1, 1))

    # Отказ обязан ничего не менять: иначе в базе осталось бы число, посчитать
    # которое всё равно нечем.
    stored = Timesheet.objects.get(pk=row_id).hours or {}
    assert Decimal(str(stored.get(kind, 0))) == Decimal("12.00")


def test_refused_cell_answers_with_value_from_base(client, period_restored):
    """Отказ возвращает то, что осталось в базе: экран не показывает непринятое.

    Без этого в поле оставался введённый мусор, и человек видел на экране
    число, которого в базе нет (T066).
    """
    login_as(client, "director")
    url = grid_url(client)
    html = body(client.get(url))
    row_id, kind = _first_cell(html)
    client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "12.00"})

    response = client.post(f"{url}cell/", {"row": row_id, "kind": kind, "hours": "восемь"})
    assert response.status_code == 422
    assert response["X-Cell-Value"] == "12.00"


def test_manager_sees_only_own_units(client):
    """Управляющий не должен получить редактируемые чужие часы.

    Полное разграничение по точкам в базе — задача T022; здесь проверяется, что
    экран не открывает дыру, которой в нём быть не должно.
    """
    from core.models import Membership, Timesheet

    login_as(client, "manager")
    url = grid_url(client)
    html = body(client.get(url))

    membership = Membership.objects.get(user_id=_seed_user_id("manager"))
    own = set(membership.unit_ids or [])
    assert own, "у управляющего сида нет точки — тест бессмысленен"

    for row in Timesheet.objects.filter(period=JUNE).select_related("employee"):
        present = str(row.id) in html
        assert present == (row.unit_id in own), row.employee.external_id


def test_manager_cannot_write_to_another_unit(client):
    from core.models import Membership, Timesheet

    own = set(Membership.objects.get(user_id=_seed_user_id("manager")).unit_ids or [])
    alien = Timesheet.objects.filter(period=JUNE).exclude(unit_id__in=own).first()
    assert alien is not None

    login_as(client, "manager")
    url = grid_url(client)
    response = client.post(
        f"{url}cell/", {"row": str(alien.id), "kind": "regular", "hours": "8"}
    )
    assert response.status_code == 404
    alien.refresh_from_db()
    assert Decimal(alien.hours.get("regular", "0")) != Decimal("8")


def _seed_user_id(code: str):
    from core.models import User

    return User.objects.get(username=code).pk


def _first_cell(html: str) -> tuple[str, str]:
    import re

    match = re.search(r'data-row="([0-9a-f-]{36})" data-kind="([a-z]+)"', html)
    assert match, "в сетке не нашлось ни одной ячейки"
    return match.group(1), match.group(2)


# =============================================================================
# 4. Доступ на уровне базы — ролью app_user
# =============================================================================


def _day_row(conn, tenant: str, unit: str | None = None) -> str:
    """Строка табеля с одним подневным днём. Кладётся владельцем, мимо политик."""
    employee = conn.execute(
        """insert into employees (tenant_id, external_id, first_name, last_name)
           values (%s, %s, 'Днев', 'Ной') returning id""",
        (tenant, f"day-{tenant}"),
    ).fetchone()[0]
    sheet = conn.execute(
        """insert into timesheets (tenant_id, employee_id, unit_id, period, norm_hours, hours)
           values (%s, %s, %s, %s, 176, '{}'::jsonb) returning id""",
        (tenant, employee, unit, "2026-06-01"),
    ).fetchone()[0]
    return conn.execute(
        """insert into timesheet_days (tenant_id, timesheet_id, work_date, hour_type, hours)
           values (%s, %s, '2026-06-01', 'regular', 8) returning id""",
        (tenant, sheet),
    ).fetchone()[0]


def test_days_isolated_between_tenants(db):
    mine = _day_row(db, T1)
    _day_row(db, T2)

    with as_app_user(db, USER_DIRECTOR) as conn:
        rows = conn.execute("select id from timesheet_days").fetchall()
    assert [row[0] for row in rows] == [mine]


def test_days_invisible_without_context(db):
    _day_row(db, T1)

    with as_app_user(db, None) as conn:
        assert conn.execute("select count(*) from timesheet_days").fetchone()[0] == 0


def test_days_of_another_tenant_cannot_be_written(db):
    import psycopg

    alien_sheet = db.execute(
        """select timesheet_id from timesheet_days where id = %s""", (_day_row(db, T2),)
    ).fetchone()[0]

    denied = psycopg.errors.InsufficientPrivilege
    with as_app_user(db, USER_DIRECTOR) as conn:
        # Точка сохранения: отказ политики обрывает транзакцию, и без отката
        # упало бы уже само возвращение роли, пряча то, что проверяем.
        conn.execute("savepoint attempt")
        with pytest.raises(denied):
            conn.execute(
                """insert into timesheet_days
                       (tenant_id, timesheet_id, work_date, hour_type, hours)
                   values (%s, %s, '2026-06-02', 'regular', 8)""",
                (T2, alien_sheet),
            )
        conn.execute("rollback to savepoint attempt")


def test_days_die_with_their_timesheet(db):
    """Удаление строки табеля уносит её дни в самой базе, а не в коде Django."""
    day = _day_row(db, T1)
    sheet = db.execute(
        "select timesheet_id from timesheet_days where id = %s", (day,)
    ).fetchone()[0]
    db.execute("delete from timesheets where id = %s", (sheet,))
    left = db.execute(
        "select count(*) from timesheet_days where id = %s", (day,)
    ).fetchone()[0]
    assert left == 0


def test_days_without_a_unit_stay_visible(db):
    """Строка без точки ничья, и её дни видны всем внутри тенанта.

    Здесь остался только этот случай. Тест, который утверждал обратное —
    «точка на видимость дней не влияет», — был заделом под T022 и обязан был
    упасть, когда она будет сделана: он и упал. Разграничение дней по точкам
    проверяется теперь в `tests/test_unit_closing.py`, вместе с закрытием.
    """
    _day_row(db, T1, unit=None)
    with as_app_user(db, USER_MANAGER) as conn:
        seen = conn.execute("select count(*) from timesheet_days").fetchone()[0]
    assert seen == 1
