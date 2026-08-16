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


# =============================================================================
# Автор базы взносов — тот, кто её поставил (T156, находка Н6 сверки 8)
# =============================================================================
#
# Правишь соседнюю ячейку часов — подсказка у базы взносов говорит «поставил
# Бухгалтер», хотя базу он не трогал: она пересчиталась сама (`set_cell` бережёт
# связь «база идёт за часами»), а автором записался тот, кто правил строку.
#
# Это тот же класс, что закрывала первая половина T143: вопрос «кто поставил это
# число» задают ровно тогда, когда числа разошлись, и неверное имя хуже
# отсутствующего — его не перепроверяют. База взносов при этом не производная
# величина, а самостоятельный вход расчёта (issue #54): она законно отличается
# от часов, и спор о взносах решается именно вопросом «кто её такой поставил».


def insured_title(html: str, row_id: str) -> str:
    """Подсказка ячейки «База взносов» — так, как её видит мышь."""
    import re

    found = re.search(
        rf'<td id="insured-{row_id}"[^>]*title="([^"]*)"', html
    )
    assert found, f"на сетке нет ячейки базы взносов строки {row_id}"
    return found.group(1)


def test_an_hours_edit_does_not_make_the_editor_the_author_of_the_insured_base(
    client, period_restored,
):
    """ГЛАВНАЯ ПРОВЕРКА: правка часов не приписывает человеку базу взносов.

    База пересчитывается — и это верно, — но поставил её не человек, а пересчёт.
    Продукт обязан так и сказать.
    """
    from core.models import Timesheet

    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))
    client.post("/logout/")

    login_as(client, "accountant")
    try:
        assert client.post(
            f"{url}cell/", {"row": row_id, "kind": kind, "hours": "8"}
        ).status_code == 200
        title = insured_title(body(client.get(url)), row_id)
    finally:
        client.post("/logout/")

    assert "Бухгалтер" not in title, (
        f"база взносов приписана тому, кто правил часы: «{title}»"
    )
    row = Timesheet.objects.get(pk=row_id)
    assert row.insured_by is None, "у пересчитанной базы появился автор"
    # След строки при этом остаётся: строку правил именно он.
    assert row.edited_by is not None


def test_the_recalculated_insured_base_says_it_was_counted_from_the_hours(
    client, period_restored,
):
    """Пусто — это ответ, и ответ здесь известен: «посчитано по часам табеля».

    «Кто поставил, не записано» тут было бы правдой наполовину: продукт знает,
    откуда взялось число, и молчать об этом незачем.
    """
    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))
    try:
        assert client.post(
            f"{url}cell/", {"row": row_id, "kind": kind, "hours": "8"}
        ).status_code == 200
        title = insured_title(body(client.get(url)), row_id)
    finally:
        client.post("/logout/")

    assert "по часам" in title, f"подсказка не говорит, откуда число: «{title}»"


def test_the_author_appears_only_after_the_base_itself_is_edited(client, period_restored):
    """Имя появляется от правки самой ячейки — и переживает правку часов рядом.

    Вторая половина важнее первой: база, заданная руками, автоподстановке больше
    не подчиняется (это уже работало), и её автор обязан остаться тем же, кто её
    задал, даже когда соседнюю ячейку правит другой человек.
    """
    from core.models import Timesheet

    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))

    assert client.post(f"{url}insured/", {"row": row_id, "insured": "80"}).status_code == 200
    title = insured_title(body(client.get(url)), row_id)
    assert "Оперативный директор" in title, (
        f"автор правки самой базы не назван: «{title}»"
    )
    client.post("/logout/")

    login_as(client, "accountant")
    try:
        assert client.post(
            f"{url}cell/", {"row": row_id, "kind": kind, "hours": "4"}
        ).status_code == 200
        after = insured_title(body(client.get(url)), row_id)
    finally:
        client.post("/logout/")

    assert "Оперативный директор" in after and "Бухгалтер" not in after, (
        f"автор базы перешёл к тому, кто правил часы: «{after}»"
    )
    assert Timesheet.objects.get(pk=row_id).insured_hours == Decimal("80.00"), (
        "заданная руками база поехала за часами"
    )


def test_a_base_nobody_set_and_nobody_counted_says_it_is_not_recorded(
    client, period_restored,
):
    """База, разошедшаяся с часами и без автора, честно говорит «не записано».

    Так выглядит число, пришедшее мимо продукта: сидом, обслуживанием, правкой в
    самой базе. Сказать про него «посчитано по часам» значило бы соврать — по
    часам выходит другое.
    """
    from core.models import Timesheet

    login_as(client, "director")
    url = grid_url(client)
    row_id, _kind = _first_cell(body(client.get(url)))
    try:
        Timesheet.objects.filter(pk=row_id).update(
            insured_hours=Decimal("13.00"), insured_by=None, insured_at=None,
        )
        title = insured_title(body(client.get(url)), row_id)
    finally:
        client.post("/logout/")

    assert "не записано" in title, f"подсказка выдумала происхождение числа: «{title}»"


def test_an_uploaded_insured_base_carries_the_one_who_brought_the_file(one_row):  # noqa: F811
    """У базы из загруженной таблицы автор есть: её принёс человек, а не пересчёт.

    В таблице партнёра база для взносов — **своя** колонка, а не производная от
    часов (Q005), и загрузка равноправна вводу с экрана. Подпись под сеткой
    обещает имя и за загрузку тоже, поэтому «не записано» здесь было бы неправдой.
    """
    from timesheets import store

    store.store_row(
        timesheet=one_row,
        want=store.RowInput(
            hours={kind: Decimal(str(value)) for kind, value in (one_row.hours or {}).items()},
            insured_hours=Decimal("123.00"),
            norm_hours=one_row.norm_hours,
        ),
        actor_id=USER_DIRECTOR,
    )

    one_row.refresh_from_db()
    assert one_row.insured_hours == Decimal("123.00")
    assert str(one_row.insured_by) == str(USER_DIRECTOR), "у базы из файла нет автора"
    assert one_row.insured_at is not None


def test_a_hand_set_base_that_matched_the_hours_loses_its_author_on_recount(
    client, period_restored,
):
    """Самый тонкий случай: база совпала с часами, потом часы поправили.

    Пока база сходится с часами, `set_cell` бережёт связь и **пересчитывает**
    её. Новое число посчитала машина, а имя у ячейки осталось бы от того, кто
    когда-то задал прежнее, — и продукт снова отвечал бы на «кто поставил это
    число» именем человека, который этого числа не выбирал.

    Проверка заведена после порчи: без неё снятие автора при пересчёте можно
    было убрать, и все остальные проверки оставались зелёными.
    """
    from core.models import Timesheet
    from timesheets.store import country_of, hour_types, insured_base

    login_as(client, "director")
    url = grid_url(client)
    row_id, kind = _first_cell(body(client.get(url)))
    row = Timesheet.objects.get(pk=row_id)
    known = hour_types(row.tenant_id, row.period, country_of(row.tenant_id))
    tracked = insured_base(row.hours or {}, known)

    # Директор задаёт руками ровно то, что и так выходит по часам: связь «база
    # идёт за часами» при этом сохраняется, а автор появляется.
    assert client.post(
        f"{url}insured/", {"row": row_id, "insured": f"{tracked}"}
    ).status_code == 200
    assert "Оперативный директор" in insured_title(body(client.get(url)), row_id)
    client.post("/logout/")

    login_as(client, "accountant")
    try:
        assert client.post(
            f"{url}cell/", {"row": row_id, "kind": kind, "hours": "7"}
        ).status_code == 200
        title = insured_title(body(client.get(url)), row_id)
    finally:
        client.post("/logout/")

    assert Timesheet.objects.get(pk=row_id).insured_by is None, (
        "у пересчитанной базы остался прежний автор"
    )
    assert "поставил" not in title, f"пересчитанное число приписано человеку: «{title}»"
    assert "по часам" in title, title
