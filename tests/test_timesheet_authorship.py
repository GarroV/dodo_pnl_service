"""Кто поставил эти часы и когда — и база для взносов, задаваемая с экрана (T143).

Две половины одной задачи, и обе про первичный документ.

**След правки (issue #52).** Табель — документ, по которому считаются деньги.
Вопрос «кто поставил 176» задают ровно тогда, когда числа разошлись: при
закрытии часов точки, при утверждении периода, при правке задним числом. До этой
задачи ответа не было ни на одном пути: `timesheet_days.source` говорил
«откуда» (`spread`, `manual`, `import`), но не говорил «кто». Восстановить это
задним числом нельзя — данных просто нет, поэтому проверок здесь больше, чем
кода: важно, что след появляется на **обоих** путях записи. Экран и загрузка
файла пишут в те же строки и договариваться между собой не обязаны — след,
поставленный на одном пути, оставил бы историю с дырами.

**Пусто — это ответ.** Ровная раскладка месячного числа по дням автора не имеет:
её никто не вводил. Приписать ей человека значило бы назвать его автором числа,
которого он не ставил, — и именно на такое приписывание тут стоит отдельная
проверка.

**База для взносов с экрана (issue #54).** По ней движок считает взносы и бруто.
Пока её нельзя было задать из продукта, законный случай (база, отличная от
отработанного) правился в базе руками — то есть мимо всего, что здесь написано,
включая след правки. Теперь это ячейка сетки с теми же правилами, что у часов:
право, закрытая точка, разбор ввода, автор.

Чего эта половина **не** закрывает и не притворяется, что закрывает: расчёт
по-прежнему отказывается считать, если база не сходится с часами
(`payrun.calc.check_insured_base`). Ответ на вопрос, законно ли она может
отличаться и по какому правилу, — за бухгалтером (Q005), и решать его экраном
ввода неправильно. Экран даёт человеку возможность поставить число самому;
правило, при котором расхождение перестаёт быть отказом, — отдельное решение.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import USER_DIRECTOR, body, login_as
from test_timesheets import _first_cell, grid_url, one_row  # noqa: F401

JUNE = date(2026, 6, 1)


# =============================================================================
# След правки: запись
# =============================================================================


def test_a_cell_edit_records_who_and_when(one_row):  # noqa: F811
    """Правка ячейки записывает автора и время — и на дне, и на строке."""
    from core.models import TimesheetDay
    from timesheets import store

    store.set_cell(
        timesheet=one_row, hour_type="holiday", hours=Decimal("8.00"),
        actor_id=USER_DIRECTOR,
    )

    days = TimesheetDay.objects.filter(timesheet=one_row, hour_type="holiday")
    assert days.exists(), "правка не завела ни одного дня"
    for day in days:
        assert str(day.edited_by) == str(USER_DIRECTOR), "у дня нет автора"
        assert day.edited_at is not None, "у дня нет времени правки"

    one_row.refresh_from_db()
    assert str(one_row.edited_by) == str(USER_DIRECTOR)
    assert one_row.edited_at is not None


def test_the_spread_of_an_old_total_has_no_author(one_row):  # noqa: F811
    """Раскладка прежнего итога автора не получает: её никто не вводил.

    Самая ценная проверка следа. Правится одна ячейка, а на дни переходит вся
    строка (`materialize`) — и если приписать автора всем дням разом, продукт
    начнёт отвечать «176 поставил директор» про часы, которых директор не
    касался. Ложный ответ хуже отсутствующего: его не перепроверяют.
    """
    from core.models import TimesheetDay
    from timesheets import store

    # Тип с непустыми часами: у пустого раскладка не заводит ни одного дня, и
    # проверка «раскладке не приписан автор» стояла бы на отсутствии строк.
    other = next(
        kind for kind, value in one_row.hours.items()
        if kind != "holiday" and Decimal(str(value)) > 0
    )

    store.set_cell(
        timesheet=one_row, hour_type="holiday", hours=Decimal("8.00"),
        actor_id=USER_DIRECTOR,
    )

    spread_days = TimesheetDay.objects.filter(timesheet=one_row, hour_type=other)
    assert spread_days.exists(), "строка не перешла на подневное хранение"
    for day in spread_days:
        assert day.source == "spread"
        assert day.edited_by is None, "раскладке приписан автор, который её не вводил"
        assert day.edited_at is None


def test_an_edit_without_an_actor_leaves_no_invented_trace(one_row):  # noqa: F811
    """Запись мимо продукта (сид, обслуживание) остаётся без автора, а не падает."""
    from core.models import TimesheetDay
    from timesheets import store

    store.set_cell(timesheet=one_row, hour_type="holiday", hours=Decimal("4.00"))

    for day in TimesheetDay.objects.filter(timesheet=one_row, hour_type="holiday"):
        assert day.edited_by is None


def test_the_import_path_records_the_author_too(one_row):  # noqa: F811
    """Второй путь записи — загрузка таблицы — оставляет тот же след.

    Без этого история была бы с дырами ровно там, где в табель попадает больше
    всего чисел: файл партнёра приносит месяц целиком.
    """
    from core.models import TimesheetDay
    from timesheets import store

    known = store.hour_types(one_row.tenant_id, JUNE, store.country_of(one_row.tenant_id))
    want = store.RowInput(
        hours={"regular": Decimal("120.00")},
        insured_hours=Decimal("120.00"),
        norm_hours=one_row.norm_hours,
    )
    assert store.store_row(
        timesheet=one_row, want=want, known=known, actor_id=USER_DIRECTOR,
        source="import",
    )

    days = TimesheetDay.objects.filter(timesheet=one_row, hour_type="regular")
    assert days.exists()
    for day in days:
        assert str(day.edited_by) == str(USER_DIRECTOR), "загрузка не оставила автора"
        assert day.edited_at is not None

    one_row.refresh_from_db()
    assert str(one_row.edited_by) == str(USER_DIRECTOR)


# =============================================================================
# След правки: экран
# =============================================================================


def test_the_grid_shows_who_set_the_hours(client, period_restored):
    """Человек читает автора правки на самом табеле, а не в базе.

    Записанный, но невидимый след отвечает на вопрос «кто поставил 176» ровно
    так же, как отсутствующий: спрашивают его глазами, глядя на сетку.
    """
    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))

    assert client.post(
        f"{url}cell/", {"row": row_id, "kind": kind, "hours": "121.00"}
    ).status_code == 200

    html = body(client.get(url))
    assert "Оперативный директор" in html, "имени того, кто правил, на экране нет"
    client.post("/logout/")


def test_the_grid_says_plainly_when_nobody_is_recorded(client):
    """Часы без записанного автора не выдаются за чьи-то: об этом сказано словами."""
    login_as(client, "director")
    html = body(client.get(grid_url(client)))
    assert "не записано" in html, "продукт молчит о том, что автор неизвестен"
    client.post("/logout/")


# =============================================================================
# База для взносов с экрана
# =============================================================================


def _insured_of(row_id):
    from core.models import Timesheet

    return Timesheet.objects.get(pk=row_id).insured_hours


def test_the_insured_base_is_set_from_the_screen(client, period_restored):
    """Число бухгалтера вводится в продукте, а не правится в базе руками (#54)."""
    login_as(client, "director")
    url = grid_url(client)
    row_id, _kind = _first_cell(body(client.get(url)))

    answer = client.post(f"{url}insured/", {"row": row_id, "insured": "140.50"})

    assert answer.status_code == 200, answer.status_code
    assert answer["X-Cell-Value"] == "140.50"
    assert _insured_of(row_id) == Decimal("140.50")
    client.post("/logout/")


def test_the_insured_base_records_its_author(client, period_restored):
    """У этой правки автор такой же обязательный, как у часов.

    Автор сверяется по имени на экране, а не по идентификатору: учётки базы
    стенда заводит `seed_dev`, и их ключи у этой базы свои — сверка с
    константой фикстуры проверяла бы совпадение двух разных сидов.
    """
    from core.models import Timesheet

    login_as(client, "director")
    url = grid_url(client)
    row_id, _kind = _first_cell(body(client.get(url)))

    client.post(f"{url}insured/", {"row": row_id, "insured": "140.50"})

    row = Timesheet.objects.get(pk=row_id)
    assert row.edited_by is not None, "у правки базы для взносов нет автора"
    assert row.edited_at is not None
    assert "Оперативный директор" in body(client.get(url))
    client.post("/logout/")


@pytest.mark.parametrize("bad", ["много", "-1", "9999", "8,333"])
def test_the_insured_base_refuses_what_it_cannot_store(client, period_restored, bad):
    """Разбор тот же, что у часов: непонятное — отказ, а не молчаливый ноль."""
    login_as(client, "director")
    url = grid_url(client)
    row_id, _kind = _first_cell(body(client.get(url)))
    before = _insured_of(row_id)

    answer = client.post(f"{url}insured/", {"row": row_id, "insured": bad})

    assert answer.status_code == 422, f"«{bad}»: ответ {answer.status_code}"
    assert _insured_of(row_id) == before, f"«{bad}» всё-таки легло в базу"
    client.post("/logout/")


def test_the_insured_base_needs_the_right_to_edit_the_timesheet(client, period_restored):
    """Право то же, что у часов: это тот же табель, и второго права ему не заводят."""
    # Строка берётся у того, кому сетка отдаётся полями: у роли на чтение полей
    # нет вовсе, и взять ключ строки с её страницы не из чего — это и есть
    # проверка ниже, а не помеха ей.
    login_as(client, "director")
    url = grid_url(client)
    row_id, _kind = _first_cell(body(client.get(url)))
    before = _insured_of(row_id)
    client.post("/logout/")

    login_as(client, "admin")   # видит табель, править не вправе (T072)

    answer = client.post(f"{url}insured/", {"row": row_id, "insured": "140.50"})

    assert answer.status_code == 403, answer.status_code
    assert _insured_of(row_id) == before
    client.post("/logout/")


def test_the_grid_offers_the_field_only_to_the_one_who_may_edit(client):
    """Роль на чтение видит число, а не поле ввода — как и у часов (T072)."""
    login_as(client, "admin")
    html = body(client.get(grid_url(client)))
    assert 'data-field="insured"' not in html
    client.post("/logout/")

    login_as(client, "director")
    html = body(client.get(grid_url(client)))
    assert 'data-field="insured"' in html, "директору поля базы для взносов не дали"
    client.post("/logout/")
