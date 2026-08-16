"""«Завести месяц» не переписывает уже заведённый (T155, находка Н5 сверки 8).

Производственный календарь был **единственным** справочником продукта, где
занятый ключ не отказ: точка, касса, группа и статья на повторный код отвечают
400 и «Такая запись уже есть» (T136, `web/dbrefusal.py`), а форма «Новый месяц
календаря» на уже заведённый месяц отвечала 302 и молча заменяла прежние числа —
176/22 на 99/9, — ничего об этом не сказав.

Почему это важнее обычной опечатки: календарь **общий на страну**, и продукт сам
об этом предупреждает на обеих формах. Промахнувшийся месяцем администратор
одного партнёра переписывал норму часов всем остальным.

Чего здесь НЕ проверяется, потому что этого не происходит: сдвига денег. Норму
по каждому сотруднику держит `timesheets.norm_hours`, а необлагаемый минимум
движок берёт из пресета страны — подмена календаря ведомость не двигает
(проверено сверкой). Беда была в молчаливой замене, и проверяется именно она.

Правка существующего месяца остаётся правкой: у неё своя форма и свой адрес
(`/directory/calendar/2026-06/`), и она обязана и дальше работать — иначе норму
часов закрытого месяца стало бы нечем поправить (T121, D020).
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import body, login_as
from test_directory import sql  # noqa: F401

NEW = "/directory/calendar/new/"
MONTH = "2026-06"
PERIOD = "2026-06-01"


@pytest.fixture
def calendar_restored(sql):  # noqa: F811
    """Июнь календаря возвращается на место, чем бы тест ни кончился.

    Календарь — общий справочник страны, и оставленные им 99 часов уехали бы
    дальше по прогону в чужие тесты нормы часов.
    """
    was = sql.execute(
        "select norm_hours, working_days from calendars "
        "where country_code = 'RS' and period = %s", (PERIOD,)
    ).fetchone()
    assert was is not None, "в сиде нет июня календаря — проверять нечего"
    yield was
    sql.execute(
        "update calendars set norm_hours = %s, working_days = %s "
        "where country_code = 'RS' and period = %s", (*was, PERIOD),
    )


def stored(sql):  # noqa: F811
    return sql.execute(
        "select norm_hours, working_days from calendars "
        "where country_code = 'RS' and period = %s", (PERIOD,)
    ).fetchone()


def test_an_occupied_month_is_refused_in_words_not_overwritten(
    client, sql, calendar_restored,  # noqa: F811
):
    """ГЛАВНАЯ ПРОВЕРКА: занятый месяц — отказ формы, а прежние числа целы."""
    login_as(client, "admin")
    try:
        answer = client.post(NEW, {"month": MONTH, "norm_hours": "99", "working_days": "9"})

        assert answer.status_code == 400, (
            f"занятый месяц принят с ответом {answer.status_code} — "
            "молчаливая замена вернулась"
        )
        html = body(answer)
        assert "уже есть" in html, f"отказ не сказал, что случилось:\n{html[:800]}"
        assert "Месяц" in html, "отказ не назвал поле"
        for leak in ("Traceback", "IntegrityError", "duplicate key", "_uniq"):
            assert leak not in html, f"наружу вылезло «{leak}»"

        assert stored(sql) == calendar_restored, "занятый месяц всё-таки переписан"
    finally:
        client.post("/logout/")


def test_the_existing_month_is_still_edited_by_its_own_form(
    client, sql, calendar_restored,  # noqa: F811
):
    """Правка заведённого месяца работает по-прежнему: отказ не съел её.

    Иначе норму часов закрытого месяца стало бы нечем поправить, а именно ради
    этого T121 и открывала дорогу правке задним числом.
    """
    login_as(client, "admin")
    try:
        answer = client.post(f"/directory/calendar/{MONTH}/", {
            "norm_hours": "100", "working_days": "12",
        })
        assert answer.status_code == 302, body(answer)
        assert stored(sql) == (Decimal("100.00"), 12), "правка месяца не записалась"
    finally:
        client.post("/logout/")


def test_a_free_month_is_still_created(client, sql):  # noqa: F811
    """Незанятый месяц заводится как раньше — отказ стоит только на занятом."""
    login_as(client, "admin")
    free = "2029-11"
    try:
        answer = client.post(NEW, {"month": free, "norm_hours": "168", "working_days": "21"})
        assert answer.status_code == 302, body(answer)
        assert sql.execute(
            "select norm_hours, working_days from calendars "
            "where country_code = 'RS' and period = '2029-11-01'"
        ).fetchone() == (Decimal("168.00"), 21)
    finally:
        sql.execute(
            "delete from calendars where country_code = 'RS' and period = '2029-11-01'"
        )
        client.post("/logout/")


@pytest.mark.parametrize("days", ["40", "32"])
def test_more_working_days_than_the_month_has_is_refused(client, sql, days):  # noqa: F811
    """В месяце не бывает 40 рабочих дней: такое число — опечатка, а не данные.

    Найдено той же сверкой рядом с главной находкой: форма принимала их с 302 и
    без вопросов, а рабочие дни — вход расчёта недоработки.
    """
    login_as(client, "admin")
    free = "2029-12"
    try:
        answer = client.post(NEW, {"month": free, "norm_hours": "168", "working_days": days})
        assert answer.status_code == 400, f"{days} рабочих дней приняты: {answer.status_code}"
        assert sql.execute(
            "select count(*) from calendars "
            "where country_code = 'RS' and period = '2029-12-01'"
        ).fetchone()[0] == 0, "отвергнутый месяц всё-таки завёлся"
    finally:
        sql.execute(
            "delete from calendars where country_code = 'RS' and period = '2029-12-01'"
        )
        client.post("/logout/")
