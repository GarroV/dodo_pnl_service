"""Подсказка о подозрительных числах — на обоих путях ввода (T118).

Спека даёт это **must**-стори: «Как бухгалтер, хочу видеть подсказку о
подозрительных числах (часов больше нормы, сотрудник без часов, отрицательное
значение), чтобы ловить опечатки до расчёта».

Выполнена она была наполовину: проверки жили в импортёре
(`timesheets/importer._check_data`) и работали только на пути «загрузили файл».
На сетке — главном пути ввода наравне с импортом (D011) — не было ничего: 400
часов при норме 88 сохранялись молча, без цвета, подписи и подсказки.

Что здесь проверяется:

1. **Подсказка есть на сетке** — и на самой странице, и в ответе на запись
   ячейки: иначе человек увидел бы её только после перезагрузки, то есть уже
   забыв, что набирал.
2. **Подсказка не мешает сохранить.** Это подсказка, а не запрет: у человека
   бывает месяц без часов, и норму перерабатывают. Ошибкой был бы молчаливый
   табель, а не само число.
3. **Правило одно на оба пути.** Проверяется не «код общий» — проверяется, что
   одни и те же числа дают на сетке и в отчёте импорта **дословно одинаковый**
   текст. Две копии проверки разъедутся молча, и разъедутся именно в ту
   сторону, где их никто не сравнивает.
4. **Сдельной строке отсутствие часов в упрёк не ставится**: её деньги считает
   не время, и «нет ни одного часа» там — норма, а не опечатка.
"""
from __future__ import annotations

from decimal import Decimal

import pytest

from conftest import body, login_as, period_url


@pytest.fixture
def grid_url(client, web_env):
    """Адрес табеля июня — тем же путём, каким до него доходит человек."""
    login_as(client, "director")
    period_id = period_url(client).strip("/").split("/")[-1]
    yield f"/timesheets/{period_id}/"
    client.post("/logout/")


def first_row(web_env):
    from core.models import Timesheet

    return (
        Timesheet.objects.filter(period="2026-06-01")
        .select_related("employee")
        .order_by("employee__last_name")
        .first()
    )


def post_cell(client, grid_url, row, *, kind="regular", hours="400"):
    return client.post(grid_url + "cell/", {
        "row": str(row.id), "kind": kind, "hours": hours,
    })


def test_the_grid_marks_hours_over_the_norm(client, grid_url, period_restored, web_env):
    """400 часов при норме 88 сетка больше не принимает молча."""
    row = first_row(web_env)
    row.norm_hours = Decimal("88.00")
    row.save(update_fields=["norm_hours"])

    saved = post_cell(client, grid_url, row)
    assert saved.status_code == 200, saved.status_code

    html = body(client.get(grid_url))
    assert "400,00 ч при норме 88,00 ч" in html, (
        "сетка приняла 400 часов при норме 88 и ничего не сказала"
    )


def test_the_hint_arrives_with_the_answer_to_the_cell(
    client, grid_url, period_restored, web_env,
):
    """Подсказка приходит в ответе на саму ячейку, а не после перезагрузки.

    Человек набирает число и уходит на следующую ячейку. Подсказка, которую
    видно только после обновления страницы, доходит до него тогда, когда он уже
    не помнит, что вводил, — то есть не доходит.
    """
    row = first_row(web_env)
    row.norm_hours = Decimal("88.00")
    row.save(update_fields=["norm_hours"])

    answer = body(post_cell(client, grid_url, row))
    assert "400,00 ч при норме 88,00 ч" in answer, (
        f"ответ на запись ячейки молчит о подозрительном числе:\n{answer}"
    )
    # Не только подсказкой мыши у ячейки: подсказка мыши требует догадаться
    # навести. Фраза целиком обязана приехать в сводку над таблицей — тем же
    # ответом, а не после перезагрузки.
    summary = answer.split('id="grid-hints"', 1)
    assert len(summary) == 2, f"ответ не подменяет сводку подсказок:\n{answer}"
    assert "400,00 ч при норме 88,00 ч" in summary[1], (
        f"сводка приехала пустой — фразу целиком человек не увидит:\n{answer}"
    )


def test_the_hint_does_not_block_saving(client, grid_url, period_restored, web_env):
    """Это подсказка, а не запрет: число сохранено и переживает перезагрузку."""
    row = first_row(web_env)
    row.norm_hours = Decimal("88.00")
    row.save(update_fields=["norm_hours"])

    saved = post_cell(client, grid_url, row)
    assert saved.status_code == 200
    assert saved["X-Cell-Value"] == "400.00", saved["X-Cell-Value"]

    row.refresh_from_db()
    assert Decimal(str(row.hours.get("regular"))) == Decimal("400")


def test_an_employee_without_hours_is_marked(client, grid_url, period_restored, web_env):
    """Пустая строка — вторая половина той же стори, и она не про опечатку."""
    row = first_row(web_env)
    for kind in list(row.hours or {}):
        assert post_cell(client, grid_url, row, kind=kind, hours="0").status_code == 200

    html = body(client.get(grid_url))
    assert "ни одного часа" in html, "строка без часов на сетке ничем не помечена"


def test_negative_hours_are_marked_on_the_grid(client, grid_url, period_restored, web_env):
    """Отрицательные часы сетка не примет, но пришедшие файлом обязана назвать.

    Проверка не лишняя: ввод с клавиатуры отвергается (`parse_hours`), а вот
    загрузка таблицы партнёра отрицательные числа **принимает** и помечает
    подсказкой. Строка живёт в том же табеле, и на сетке она должна выглядеть
    так же, а не чисто.
    """
    from core.models import Timesheet

    row = first_row(web_env)
    Timesheet.objects.filter(pk=row.pk).update(hours={"regular": "-5.00"})

    html = body(client.get(grid_url))
    assert "отрицательные часы" in html, "сетка не назвала отрицательные часы"

    refused = post_cell(client, grid_url, row, hours="-5")
    assert refused.status_code == 422, "запрет на ввод отрицательных часов пропал"


def test_the_wording_is_the_same_on_both_paths(web_env):
    """Одни и те же числа — один и тот же текст на сетке и в отчёте импорта.

    Главная проверка задачи: правило одно, а не две копии. Копии разъезжаются
    молча, и первым это заметит бухгалтер, у которого файл и сетка сказали
    про одного человека разное.
    """
    from timesheets import suspicion
    from timesheets.importer import _check_data
    from timesheets.store import RowInput

    who = "ПЕТРОВ Иван"
    hours = {"regular": Decimal("400")}
    norm = Decimal("88")

    on_grid = suspicion.hints(who=who, hours=hours, norm_hours=norm)

    class Row:
        sheet = "BG1"

    class Employee:
        first_name, last_name = "Иван", "ПЕТРОВ"
        external_id = "dev-1"
        dismissed_at = None

    on_import = _check_data(
        Row(), Employee(),
        RowInput(hours=hours, insured_hours=Decimal(0), norm_hours=norm),
        __import__("datetime").date(2026, 6, 1),
    )

    assert [hint.text for hint in on_grid] == [note.text for note in on_import], (
        "сетка и импорт говорят о подозрительном разными словами"
    )
    assert [hint.kind for hint in on_grid] == [note.kind for note in on_import]


def test_a_piecework_row_without_hours_is_not_blamed():
    """Сдельной строке отсутствие часов — не подозрительно, а нормально."""
    from timesheets import suspicion

    silent = suspicion.hints(
        who="Курьер Ана", hours={}, norm_hours=Decimal("176"), piecework=True,
    )
    assert silent == [], f"сдельной строке напрасно предъявили: {silent}"

    noisy = suspicion.hints(who="Курьер Ана", hours={}, norm_hours=Decimal("176"))
    assert [hint.kind for hint in noisy] == ["no_hours"]
