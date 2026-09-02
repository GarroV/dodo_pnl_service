"""Коннекторы включаются по одному, и режим сверки в расчёт не идёт (D063).

Механизм заведён ради конкретного сомнения владельца: «я не уверен, что та же
себестоимость считается нормально в додо ис». Проверить чужую цифру можно
только на живом месяце — но если она при этом попала в расчёт, проверка уже
опоздала.

Здесь проверяется то, на чём механизм держится:

1. **Умолчание — выключено.** Поток, о котором никто ничего не сказал, не
   участвует ни в чём. Строки в таблице нет — значит `off`, а не «непонятно».
2. **`trial` не пускает данные в расчёт.** Это его единственный смысл: цифру
   видно, но она ни на один итог не влияет.
3. **Отказ вместо молчаливого недосчёта.** Часы из невзятого источника не
   пропускаются тихо — расчёт останавливается и называет людей. Молчаливый
   пропуск дал бы неполный итог, неотличимый от правильного.
4. **`on` пускает.** Иначе включить поток было бы нельзя, и вся конструкция
   свелась бы к запрету.
"""
from __future__ import annotations

from datetime import date

import psycopg
import pytest

from conftest import T2, USER_DIRECTOR, as_app_user

JUNE = date(2026, 6, 1)


@pytest.fixture
def feeds_clean(web_env):
    """Состояния потоков возвращаются к исходному: таблица общая на сессию."""
    from core.models import DataFeed

    before = list(DataFeed.objects.values_list("id", flat=True))
    yield
    DataFeed.objects.exclude(id__in=before).delete()


def _tenant():
    from core.models import Tenant

    return Tenant.objects.get(code="rs-dev")


def test_an_unmentioned_feed_is_off(web_env, feeds_clean):
    """Строки нет — поток выключен, а не «неизвестен»."""
    from core import feeds

    tenant = _tenant()
    assert feeds.state(tenant.id, "cogs") == feeds.OFF
    assert feeds.trusted(tenant.id, "cogs") is False


def test_an_unknown_feed_is_refused(web_env):
    """Опечатка в названии потока не должна читаться как «выключено»."""
    from core import feeds

    with pytest.raises(ValueError):
        feeds.state(_tenant().id, "sebestoimost")


def test_trial_is_not_trusted(web_env, feeds_clean):
    """Режим сверки виден, но в расчёт не идёт — в этом весь его смысл."""
    from core import feeds
    from core.models import DataFeed

    tenant = _tenant()
    DataFeed.objects.create(tenant=tenant, feed="hours", state=feeds.TRIAL)

    assert feeds.state(tenant.id, "hours") == feeds.TRIAL
    assert feeds.trusted(tenant.id, "hours") is False
    assert "dodo_is" not in feeds.trusted_sources(tenant.id)


def test_switching_on_makes_it_trusted(web_env, feeds_clean):
    """Включённый поток попадает в список доверенных источников."""
    from core import feeds
    from core.models import DataFeed

    tenant = _tenant()
    DataFeed.objects.create(tenant=tenant, feed="hours", state=feeds.ON)

    assert feeds.trusted(tenant.id, "hours") is True
    assert "dodo_is" in feeds.trusted_sources(tenant.id)


def test_hand_entered_hours_are_always_trusted(web_env, feeds_clean):
    """То, что ввёл человек продукта, внешним состоянием не управляется."""
    from core import feeds

    sources = feeds.trusted_sources(_tenant().id)
    assert "manual" in sources and "import" in sources


def test_untrusted_hours_stop_the_payrun_by_name(web_env, feeds_clean, period_restored):
    """Часы невзятого источника не пропускаются молча — расчёт отказывает.

    Проверяется именно отказ, а не отсутствие строки в итоге: молчаливый
    пропуск дал бы недосчитанную ведомость, которую не отличить от правильной.
    """
    from core.models import Timesheet
    from payrun.calc import PayrunRefused, collect_cases

    tenant = _tenant()
    sheet = Timesheet.objects.filter(tenant=tenant, period=JUNE).first()
    assert sheet is not None, "в июне нет ни одного табеля — проверять нечего"

    was = sheet.source
    Timesheet.objects.filter(pk=sheet.pk).update(source="dodo_is")
    try:
        with pytest.raises(PayrunRefused) as refusal:
            collect_cases(tenant.id, JUNE)
        assert sheet.employee.external_id in (refusal.value.details or []), (
            "отказ должен называть людей, чьи часы не взяты"
        )
    finally:
        Timesheet.objects.filter(pk=sheet.pk).update(source=was)


def test_switched_on_hours_reach_the_payrun(web_env, feeds_clean, period_restored):
    """Включили источник — те же часы считаются как обычно."""
    from core import feeds
    from core.models import DataFeed, Timesheet
    from payrun.calc import collect_cases

    tenant = _tenant()
    sheet = Timesheet.objects.filter(tenant=tenant, period=JUNE).first()
    was = sheet.source
    Timesheet.objects.filter(pk=sheet.pk).update(source="dodo_is")
    DataFeed.objects.create(tenant=tenant, feed="hours", state=feeds.ON)
    try:
        cases = collect_cases(tenant.id, JUNE)
        assert any(c.employee_id == sheet.employee_id for c in cases), (
            "включённый источник обязан попадать в расчёт"
        )
    finally:
        Timesheet.objects.filter(pk=sheet.pk).update(source=was)


def test_the_table_is_isolated_by_tenant(db):
    """Состояние потоков — данные партнёра, и режет их база, а не приложение.

    Гоняется ролью `app_user`: владелец таблиц политики обходит, и проверка
    прошла бы зелёной при неверно написанной политике.
    """
    with as_app_user(db, USER_DIRECTOR) as conn:
        # Своё видно — иначе продукт не смог бы прочитать собственное состояние.
        conn.execute("select count(*) from data_feeds").fetchone()

        # Чужому партнёру включить поток нельзя: политика проверяет и `with
        # check`, а не только чтение. Иначе один партнёр включал бы коннектор
        # другому — и тот узнал бы об этом по изменившемуся расчёту.
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            with conn.transaction():
                conn.execute(
                    "insert into data_feeds (tenant_id, feed, state) "
                    "values (%s, 'hours', 'on')",
                    (T2,),
                )

    # И чужие строки не видны: заводим их владельцем схемы, читаем ролью.
    db.execute(
        "insert into data_feeds (tenant_id, feed, state) values (%s, 'cogs', 'trial')",
        (T2,),
    )
    with as_app_user(db, USER_DIRECTOR) as conn:
        seen = conn.execute(
            "select count(*) from data_feeds where tenant_id = %s", (T2,)
        ).fetchone()[0]
    assert seen == 0, "видно состояние потоков чужого партнёра"
