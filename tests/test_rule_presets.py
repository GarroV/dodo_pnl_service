"""
Правила расчёта приезжают из базы, а не из YAML-файла (T011), и приезжают теми
же (T012).

Два разных вопроса, поэтому и два набора проверок:

1. **Сборка на дату и по слоям.** Пресет страны лежит в `rule_presets`, поверх
   него ложатся переопределения из `rule_overrides` — страна, партнёр, группа,
   человек, каждый следующий сильнее предыдущего. Всё это на дату: правило,
   вступающее в силу в июле, не имеет права трогать июнь.
2. **Перенос ничего не потерял.** Расчёт на правилах из базы обязан давать ровно
   то же, что расчёт из YAML, по всем 32 людям обезличенного набора и всем
   четырём схемам. Сравниваются два источника между собой, а не каждый с
   ожидаемым числом: тихо потерянное при переносе правило иначе прошло бы мимо.

Гоняются на живом Postgres с сидом (фикстура `web_env`); без Postgres
пропускаются вместе с остальными тестами схемы.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

JUNE = date(2026, 6, 1)
JULY = date(2026, 7, 1)
MAY = date(2026, 5, 1)


# --- окружение ---------------------------------------------------------------


@pytest.fixture
def rules_db(web_env):
    """Django поверх сида, но всё написанное тестом откатывается.

    База у веб-тестов общая на прогон, а этот модуль заводит переопределения
    правил. Оставленное после себя переопределение сдвинуло бы суммы в
    `test_payrun`, и разбирались бы с этим уже как с плавающим дефектом.
    """
    from django.db import transaction

    atomic = transaction.atomic()
    atomic.__enter__()
    try:
        yield web_env
    finally:
        transaction.set_rollback(True)
        atomic.__exit__(None, None, None)


@pytest.fixture
def tenant(rules_db):
    from core.models import Tenant

    return Tenant.objects.get(code="rs-dev")


def override(tenant, *, level, path, value, scope_id=None, valid_from=date(2020, 1, 1),
             valid_to=None):
    from core.models import RuleOverride

    return RuleOverride.objects.create(
        tenant=tenant, scope_type=level, scope_id=scope_id, path=path,
        value=value, valid_from=valid_from, valid_to=valid_to,
    )


def group_id(tenant, code: str):
    from core.models import EmployeeGroup

    return EmployeeGroup.objects.get(tenant=tenant, code=code).id


def employee_id(tenant, external_id: str):
    from core.models import Employee

    return Employee.objects.get(tenant=tenant, external_id=external_id).id


def any_employee(tenant):
    from core.models import Employee

    return Employee.objects.filter(tenant=tenant).order_by("external_id").first()


# --- пресет страны в базе ----------------------------------------------------


def test_preset_is_loaded_from_the_database_and_matches_the_file(tenant):
    """Первичная загрузка страны — из YAML, дальше источник один: база."""
    from core.rules import load_preset_at
    from payroll import load_preset
    from payroll.presets import to_jsonable

    preset = load_preset_at(tenant.id, "RS", JUNE)
    assert preset["preset"] == "serbia-2026"
    assert preset == to_jsonable(load_preset("serbia-2026"))


def test_importing_the_file_twice_does_not_double_the_preset(rules_db):
    """Загрузка идемпотентна: та же страна, та же дата — та же строка."""
    from core.models import RulePreset
    from core.rules import import_presets

    before = RulePreset.objects.filter(code="serbia-2026").count()
    import_presets()
    import_presets()
    assert RulePreset.objects.filter(code="serbia-2026").count() == before == 1


def test_country_without_rules_is_refused_loudly(tenant):
    """Считать наугад нечем — отказ с названием страны, а не пустой пресет."""
    from core.rules import PresetNotFound, load_preset_at

    with pytest.raises(PresetNotFound) as exc:
        load_preset_at(tenant.id, "ZZ", JUNE)
    assert "ZZ" in str(exc.value)


def test_the_run_refuses_when_the_table_is_empty_instead_of_falling_back_to_the_file(tenant):
    """Доказательство, что расчёт действительно читает базу, а не YAML.

    Молчаливый откат на файл — худший из возможных исходов: расчёт прошёл бы
    мимо переопределений партнёра и выглядел бы при этом правильным.
    """
    from core.models import RulePreset
    from payrun.calc import calculate_period
    from payrun.errors import PayrunRefused

    RulePreset.objects.all().delete()
    with pytest.raises(PayrunRefused) as exc:
        calculate_period(tenant_id=tenant.id, period=JUNE,
                         visible_ledgers=["official", "supplementary", "internal"])
    assert "load_presets" in str(exc.value)


def test_preset_that_starts_later_is_not_used_for_an_earlier_period(tenant):
    """Правила 2026 года не применяются к периоду 2025-го."""
    from core.rules import PresetNotFound, load_preset_at

    with pytest.raises(PresetNotFound):
        load_preset_at(tenant.id, "RS", date(2025, 6, 1))


def test_the_version_in_force_on_the_date_wins(tenant):
    """Две версии пресета — берётся та, что действует на дату расчёта."""
    from core.models import RulePreset
    from core.rules import load_preset_at

    current = RulePreset.objects.get(code="serbia-2026")
    current.valid_to = JULY
    current.save(update_fields=["valid_to"])
    RulePreset.objects.create(
        code="serbia-2026", title="Сербия со второго полугодия", country_code="RS",
        body={**current.body, "constants": {**current.body["constants"],
                                            "min_hourly_rate": 400.0}},
        valid_from=JULY,
    )

    assert load_preset_at(tenant.id, "RS", JUNE)["constants"]["min_hourly_rate"] == 371.0
    assert load_preset_at(tenant.id, "RS", JULY)["constants"]["min_hourly_rate"] == 400.0


# --- четыре уровня переопределения -------------------------------------------
# Каждый следующий уровень бьёт предыдущий. Проверяется по одному правилу на
# уровень плюс порядок «сильнее/слабее» между соседними.


def test_country_level_override_beats_the_preset_body(tenant):
    from core.rules import load_preset_at

    override(tenant, level="country", path="constants.min_hourly_rate", value=380)
    assert load_preset_at(tenant.id, "RS", JUNE)["constants"]["min_hourly_rate"] == 380


def test_tenant_level_override_beats_the_country_level(tenant):
    from core.rules import load_preset_at

    override(tenant, level="country", path="constants.min_hourly_rate", value=380)
    override(tenant, level="tenant", path="constants.min_hourly_rate", value=390)
    assert load_preset_at(tenant.id, "RS", JUNE)["constants"]["min_hourly_rate"] == 390


def test_group_level_override_beats_the_tenant_level(tenant):
    from core.rules import load_preset_at

    kitchen = group_id(tenant, "kitchen")
    override(tenant, level="tenant", path="hour_types.sick.pay_percent", value=0.7)
    override(tenant, level="group", scope_id=kitchen,
             path="hour_types.sick.pay_percent", value=0.8)

    assert load_preset_at(tenant.id, "RS", JUNE)["hour_types"]["sick"]["pay_percent"] == 0.7
    scoped = load_preset_at(tenant.id, "RS", JUNE, group_id=kitchen)
    assert scoped["hour_types"]["sick"]["pay_percent"] == 0.8


def test_employee_level_override_beats_the_group_level(tenant):
    from core.rules import load_preset_at

    kitchen = group_id(tenant, "kitchen")
    person = any_employee(tenant)
    override(tenant, level="group", scope_id=kitchen,
             path="hour_types.sick.pay_percent", value=0.8)
    override(tenant, level="employee", scope_id=person.id,
             path="hour_types.sick.pay_percent", value=0.9)

    scoped = load_preset_at(tenant.id, "RS", JUNE, group_id=kitchen, employee_id=person.id)
    assert scoped["hour_types"]["sick"]["pay_percent"] == 0.9


def test_a_group_override_does_not_leak_into_another_group(tenant):
    from core.rules import load_preset_at

    override(tenant, level="group", scope_id=group_id(tenant, "kitchen"),
             path="hour_types.sick.pay_percent", value=0.8)
    other = load_preset_at(tenant.id, "RS", JUNE, group_id=group_id(tenant, "office"))
    assert other["hour_types"]["sick"]["pay_percent"] == 0.65


def test_an_override_of_another_tenant_does_not_apply(tenant):
    """Переопределение партнёра — его собственное, чужой расчёт оно не двигает."""
    from core.models import Tenant
    from core.rules import load_preset_at

    stranger = Tenant.objects.create(
        code="xx-dev", title="Другой партнёр", country_code="RS",
        base_currency="RSD", report_currency="EUR",
    )
    override(stranger, level="tenant", path="constants.min_hourly_rate", value=999)

    assert load_preset_at(tenant.id, "RS", JUNE)["constants"]["min_hourly_rate"] == 371.0
    assert load_preset_at(stranger.id, "RS", JUNE)["constants"]["min_hourly_rate"] == 999


# --- даты действия -----------------------------------------------------------


def test_an_override_starting_later_does_not_touch_an_earlier_period(tenant):
    """Главное свойство версионирования: правка будущим числом не ломает июнь."""
    from core.rules import load_preset_at

    override(tenant, level="tenant", path="constants.min_hourly_rate",
             value=500, valid_from=JULY)

    assert load_preset_at(tenant.id, "RS", JUNE)["constants"]["min_hourly_rate"] == 371.0
    assert load_preset_at(tenant.id, "RS", JULY)["constants"]["min_hourly_rate"] == 500


def test_an_override_that_has_expired_stops_applying(tenant):
    """Конец периода не входит: «по 1 июля» и «с 1 июля» стыкуются, а не спорят."""
    from core.rules import load_preset_at

    override(tenant, level="tenant", path="constants.min_hourly_rate",
             value=350, valid_from=MAY, valid_to=JULY)

    assert load_preset_at(tenant.id, "RS", JUNE)["constants"]["min_hourly_rate"] == 350
    assert load_preset_at(tenant.id, "RS", JULY)["constants"]["min_hourly_rate"] == 371.0


# --- откуда взялось значение (фундамент следа расчёта, D025) ------------------


def test_the_preset_remembers_which_layer_set_each_value(tenant):
    from core.models import RulePreset
    from core.rules import load_preset_at

    row = override(tenant, level="tenant", path="constants.min_hourly_rate", value=390)
    preset = load_preset_at(tenant.id, "RS", JUNE)

    changed = preset.origin_of("constants.min_hourly_rate")
    assert changed.level == "tenant"
    assert changed.version_id == row.id

    untouched = preset.origin_of("rates.net_factor")
    assert untouched.level == "country"
    assert untouched.version_id == RulePreset.objects.get(code="serbia-2026").id


def test_an_override_of_a_whole_branch_covers_the_values_inside_it(tenant):
    """Переопределили узел целиком — след ведёт к нему, а не в пустоту."""
    from core.rules import load_preset_at

    row = override(tenant, level="tenant", path="hour_types.sick",
                   value={"title": "Больничный", "pay_percent": 0.75,
                          "counts_as_worked": False, "insured": True})
    preset = load_preset_at(tenant.id, "RS", JUNE)
    assert preset.origin_of("hour_types.sick.pay_percent").version_id == row.id


def test_an_override_without_an_object_is_refused_instead_of_ignored(tenant):
    """Правило заведено, но применить его не к чему — это отказ, а не пропуск.

    Молча пропущенная строка — худший исход: в списке правил она есть, в расчёте
    её нет, и объяснить разницу нечем.
    """
    from core.rules import load_rules_at

    override(tenant, level="group", scope_id=None,
             path="hour_types.sick.pay_percent", value=0.8)
    with pytest.raises(ValueError, match="не к чему применить"):
        load_rules_at(tenant.id, "RS", JUNE)


# --- сборка пресета: чистые проверки без базы --------------------------------


def test_a_path_that_runs_into_a_value_is_refused():
    """Внутри числа переопределять нечего — сказать это надо, а не упасть в KeyError."""
    from payroll.presets import set_path

    with pytest.raises(ValueError, match="переопределять внутри нечего"):
        set_path({"constants": {"min_hourly_rate": 371}},
                 "constants.min_hourly_rate.extra", 1)


def test_overriding_a_whole_branch_forgets_the_trail_inside_it():
    """След не имеет права указывать на правило, которое больше не действует."""
    from payroll.presets import Origin, build_preset

    child = Origin(level="country", version_id="child")
    parent = Origin(level="tenant", version_id="parent")
    preset = build_preset(
        {"hour_types": {"sick": {"pay_percent": 0.65}}},
        levels=[
            ("country", [("hour_types.sick.pay_percent", 0.7, child)]),
            ("tenant", [("hour_types.sick", {"pay_percent": 0.9}, parent)]),
        ],
    )
    assert preset["hour_types"]["sick"]["pay_percent"] == 0.9
    assert preset.origin_of("hour_types.sick.pay_percent") == parent


def test_a_deep_copy_of_a_preset_keeps_its_trail():
    """`copy.deepcopy` обычного наследника dict теряет атрибуты молча."""
    import copy

    from payroll.presets import Origin, build_preset

    where = Origin(level="tenant", version_id="v1")
    preset = build_preset({"rates": {"income_tax": 0.1}},
                          levels=[("tenant", [("rates.income_tax", 0.2, where)])])
    clone = copy.deepcopy(preset)
    assert clone["rates"]["income_tax"] == 0.2
    assert clone.origin_of("rates.income_tax") == where


def test_validity_date_reads_the_same_from_a_file_and_from_json():
    """Из YAML приезжает дата, из jsonb — строка. Разбор один на оба случая."""
    from payroll import load_preset
    from payroll.presets import preset_valid_from, to_jsonable

    from_file = load_preset("serbia-2026")
    assert preset_valid_from(from_file) == preset_valid_from(to_jsonable(dict(from_file)))


def test_asking_for_a_preset_file_that_does_not_exist_says_which_ones_there_are():
    from payroll import load_preset

    with pytest.raises(FileNotFoundError, match="serbia-2026"):
        load_preset("atlantis-2026")


def test_the_load_presets_command_fills_the_table_on_a_clean_database():
    """DoD: YAML остаётся источником первичной загрузки и покрыт тестом.

    Команда гоняется подпроцессом на чистой базе — ровно так, как её запустит
    человек, вместе с настройками и каркасом проекта.
    """
    import psycopg

    from conftest import run_manage, temp_database

    with temp_database("presets") as dsn:
        with psycopg.connect(dsn) as conn:
            assert conn.execute("select count(*) from rule_presets").fetchone()[0] == 0

        out = run_manage(dsn, "load_presets").stdout
        assert "serbia-2026" in out

        with psycopg.connect(dsn) as conn:
            rows = conn.execute(
                "select code, country_code, valid_from from rule_presets"
            ).fetchall()
    assert rows == [("serbia-2026", "RS", date(2026, 1, 1))]


# --- T012: перенос ничего не потерял -----------------------------------------


def calculate_both(tenant, rows):
    """Один и тот же вход, два источника правил. Возвращает пары листков."""
    from core.rules import load_preset_at
    from payroll import PayrollEngine, load_preset

    from_file = PayrollEngine(load_preset("serbia-2026"))
    from_db = PayrollEngine(load_preset_at(tenant.id, "RS", JUNE))
    return [
        (row, from_file.calculate(row.employee, row.timesheet),
         from_db.calculate(row.employee, row.timesheet))
        for row in rows
    ]


def compare(pairs) -> list[str]:
    """Расхождения между двумя источниками — по итогам и по компонентам."""
    from _payroll_checks import FIELDS

    problems = []
    for row, yaml_slip, db_slip in pairs:
        for field in FIELDS:
            a, b = getattr(yaml_slip, field), getattr(db_slip, field)
            if a != b:
                problems.append(f"{row.sheet} / {row.name}: {field} {a:.2f} против {b:.2f}")
        a_comp = {c.code: (c.amount, c.ledger, c.channel, c.taxable) for c in yaml_slip.components}
        b_comp = {c.code: (c.amount, c.ledger, c.channel, c.taxable) for c in db_slip.components}
        if a_comp != b_comp:
            problems.append(
                f"{row.sheet} / {row.name}: состав компонентов разошёлся — "
                f"{sorted(set(a_comp) ^ set(b_comp)) or 'значения'}"
            )
    return problems


def test_the_sample_covers_every_scheme_before_we_compare(sample_rows):
    """Сверка на наборе без одной из схем ничего не доказывает."""
    from _payroll_checks import check_schemes_covered

    check_schemes_covered(sample_rows, minimum=30)


def test_rules_from_the_database_calculate_exactly_like_the_file(tenant, sample_rows):
    problems = compare(calculate_both(tenant, sample_rows))
    assert not problems, "перенос правил в базу изменил расчёт:\n  " + "\n  ".join(problems)


def test_that_comparison_is_not_decorative(tenant, sample_rows):
    """Сверка обязана падать на испорченном пресете, иначе она ничего не ловит."""
    override(tenant, level="tenant", path="rates.employer_contributions", value=0.2)
    problems = compare(calculate_both(tenant, sample_rows))
    assert problems, "подмена ставки взносов не заметна — сверка фиктивная"


def test_components_still_sum_to_net_on_database_rules(tenant, sample_rows):
    from core.rules import load_preset_at
    from payroll import PayrollEngine

    engine = PayrollEngine(load_preset_at(tenant.id, "RS", JUNE))
    for row in sample_rows:
        slip = engine.calculate(row.employee, row.timesheet)
        total = sum((c.amount for c in slip.components), Decimal(0))
        assert abs(total - slip.net) < Decimal("0.01"), f"{row.name}: компоненты не сходятся"
