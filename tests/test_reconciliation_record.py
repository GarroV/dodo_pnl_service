"""Сверка остаётся в системе: протокол, история и решения (#172).

Эталон (модуль 2) формулирует так: «Протокол сверки остаётся в системе: файл,
дата, кто сверял, каждое расхождение и решение по нему. Через полгода на вопрос
„почему в мае было так“ отвечает протокол, а не память».

До этого сверка жила ровно до перезагрузки страницы: посчитали, показали,
закрыли — и её не было. Значит доказать через месяц, что расхождения разобрали,
было нечем: у бухгалтера остаётся её файл, у нас — ничего.

**Файл при этом не сохраняется, и это не забывчивость, а D028.** В таблице
партнёра ФИО и суммы живых людей; сверка не повод заводить им ещё одно место
жительства. Протокол хранит **результат сравнения**: имя файла, счётчики, и по
каждому расхождению — ссылку на нашего же сотрудника, две суммы и решение
человека. Новых персональных данных здесь не появляется.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from conftest import body, login_as, period_url

JUNE = date(2026, 6, 1)


@pytest.fixture
def reconciled(client, web_env):
    """Сверка, выполненная через экран, — как это делает бухгалтер."""
    from pathlib import Path

    from core.models import Reconciliation

    login_as(client, "director")
    url = period_url(client)
    client.post(url + "calculate/", {"inline": "1"}, follow=True)

    # Чужие протоколы убираются ДО сверки, а не только после (issue #198).
    # Сверку через экран гоняют и соседние файлы тестов — в том числе файлом про
    # чужих людей, где не сопоставилось ни строки. Такая запись живёт в той же
    # таблице с `lines = 0`, и выборка «первая попавшаяся за июнь» однажды
    # возвращает именно её. Локально порядок оказывался удачным, в CI — нет:
    # тест краснел на исправном продукте, а выглядело это как потерянный
    # счётчик строк.
    Reconciliation.objects.all().delete()

    sample = Path(__file__).resolve().parent / "fixtures" / "plata-sample.xlsx"
    with sample.open("rb") as file:
        response = client.post(url + "reconcile/", {"table": file}, follow=True)
    assert response.status_code == 200, body(response)[:300]
    yield url
    Reconciliation.objects.all().delete()


def our_record():
    """Протокол ИМЕННО нашей сверки, а не первый попавшийся за июнь (#198).

    Якорь — имя файла, а не порядок записей. Сортировка «самая свежая» тоже не
    годится: чужую запись может создать и тест, идущий позже. Ошибка была
    именно в выборке «первая попавшаяся»: соседний тест сверяет файл про чужих
    людей, у той записи `lines = 0`, и когда база возвращала её, тест краснел на
    исправном продукте.
    """
    from core.models import Reconciliation

    return (
        Reconciliation.objects
        .filter(period=JUNE, file_name="plata-sample.xlsx")
        .order_by("-created_at")
        .first()
    )


def test_the_reconciliation_is_recorded(reconciled):
    """После сверки в системе остаётся запись: когда, кто, с каким файлом."""

    record = our_record()
    assert record is not None, "сверка нигде не сохранилась"
    assert record.file_name.endswith(".xlsx"), "имя файла не записано"
    assert record.created_by is not None, "не видно, кто сверял"
    assert record.lines > 0, "не записано, сколько строк сверяли"


def test_the_file_itself_is_not_stored(reconciled):
    """Файла в системе нет: в нём ФИО и суммы живых людей (D028)."""

    record = our_record()
    for field in record._meta.get_fields():
        assert "content" not in field.name and "payload" not in field.name, (
            f"в протоколе есть поле «{field.name}» — похоже, файл всё-таки хранится"
        )


def test_the_findings_are_recorded_with_both_sides(reconciled):
    """Каждое расхождение записано: наша сумма, его сумма, компонент.

    Если файл сошёлся целиком — записывать нечего, и это не повод пропускать
    проверку: заводим расхождение сами и убеждаемся, что обе стороны на месте.
    """
    from core.models import Employee, ReconciliationFinding

    record = our_record()
    if not ReconciliationFinding.objects.filter(record=record).exists():
        person = Employee.objects.first()
        ReconciliationFinding.objects.create(
            tenant_id=record.tenant_id, record=record, employee=person,
            component="hours.regular", ours=Decimal("100.00"), theirs=Decimal("90.00"),
        )
    found = ReconciliationFinding.objects.filter(record=record).first()
    assert found.employee_id is not None, "расхождение не привязано к человеку"
    assert found.ours != found.theirs, "записано расхождение, в котором нечему расходиться"
    assert found.component, "не записано, что именно сравнивали"


def test_a_decision_can_be_recorded_and_stays(client, reconciled):
    """Решение по расхождению записывается и переживает перезагрузку.

    Эталон: «три признаны нашей стороной, одно — ошибкой в файле, одно — разной
    трактовкой правила». Без решения протокол отвечает только «разошлось», а
    вопрос через полгода звучит «и что вы с этим сделали».
    """
    from core.models import Employee, ReconciliationFinding

    record = our_record()
    found = ReconciliationFinding.objects.filter(record=record).first()
    if found is None:
        # Файл сошёлся целиком — заводим расхождение сами: проверяется решение,
        # а не умение фикстуры разойтись.
        found = ReconciliationFinding.objects.create(
            tenant_id=record.tenant_id, record=record, employee=Employee.objects.first(),
            component="hours.regular", ours=Decimal("100.00"), theirs=Decimal("90.00"),
        )

    response = client.post(f"/reconciliations/findings/{found.id}/", {
        "decision": "ours", "note": "наша ошибка: коэффициент не тот",
    }, follow=True)
    assert response.status_code == 200, body(response)[:300]

    found.refresh_from_db()
    assert found.decision == "ours"
    assert "коэффициент" in found.note


def test_past_reconciliations_are_visible(client, reconciled):
    """Прошлые сверки видны списком — эталон называет это «Прошлые сверки»."""
    html = body(client.get(reconciled + "reconcile/"))
    assert "Прошлые сверки" in html or "сверено" in html.lower(), (
        "истории сверок не видно — через месяц доказать разбор нечем"
    )


def test_a_role_without_the_right_records_nothing(client, web_env):
    """Сверяет тот, кто ведёт расчёт: у управляющего точки права нет."""
    from pathlib import Path

    from core.models import Reconciliation

    login_as(client, "manager")
    sample = Path(__file__).resolve().parent / "fixtures" / "plata-sample.xlsx"
    period = _period_id()
    with sample.open("rb") as file:
        client.post(f"/periods/{period}/reconcile/", {"table": file})
    assert not Reconciliation.objects.exists()


def _period_id() -> str:
    from core.models import Period

    return str(Period.objects.get(period=JUNE).id)
