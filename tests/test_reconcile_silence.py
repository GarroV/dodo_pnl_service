"""Сверка объясняет молчание настоящей причиной, а не доступом (T120).

**Что было.** Когда сравнить не удалось ничего, подвал сверки говорил одно и то
же, независимо от причины:

> Деньги не сравнивались ни по одной строке: **итоги расчёта вам не отданы**.
> Ноль в такой сумме означал бы совпадение, поэтому её здесь нет.

Бухгалтеру после D036 итоги отданы — на нормальном файле та же сверка выводит
«К выплате по сверенным строкам». Причина нуля была другая: ни одна строка
файла не разобралась. Человек шёл выяснять права вместо того, чтобы посмотреть
на формат файла.

**Что при этом нельзя потерять.** Формулировка появилась в T100 и там была
верна: у роли с неполным набором регистров (управляющий, D031) итоги закрыты
политикой (T071), и молчать о причине нельзя — ноль в сумме читался бы как
совпадение. Поэтому проверок здесь две стороны сразу: бухгалтер деньги
сравнивает, управляющий по-прежнему не получает ни имён, ни чисел чужого
регистра, и причину своего нуля читает верную.

**Где живёт решение.** Причину называет ядро сверки (`Reconciliation.
nothing_compared`), а не разметка: разметка не знает, чего не хватило, и
догадка по пустому списку — это ровно тот способ, которым неправда сюда и
попала.
"""
from __future__ import annotations

import io
from decimal import Decimal

import openpyxl
import pytest
from django.core.files.uploadedfile import SimpleUploadedFile

from conftest import PLATA_SAMPLE, body, login_as, period_url, wipe_payruns
from test_reconcile_own_export import export_bytes, reconcile_with
from test_reports_reconcile_db import summary

D = Decimal

ACCESS_WORDS = "вам не отданы"


@pytest.fixture
def calculated_june(client, web_env):
    """Посчитанный июнь на данных сида — общий материал для проверок ниже.

    Своя копия в каждом файле проверок намеренно: фикстура, импортированная из
    соседнего модуля, тянет за собой его порядок и его материал, а стоит она
    пять строк.
    """
    wipe_payruns(web_env)
    login_as(client, "director")
    response = client.post(period_url(client) + "calculate/", follow=True)
    assert response.status_code == 200
    return None


def strangers(data: bytes) -> bytes:
    """Та же выгрузка, но про других людей: ни одна строка не сопоставится."""
    book = openpyxl.load_workbook(io.BytesIO(data))
    for ws in book.worksheets:
        for row in ws.iter_rows(min_row=2):
            name = str(row[1].value or "").strip()
            if name and name != "IME I PREZIME":
                row[1].value = f"{name} IZ DRUGE MREZE"
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


# --- ядро называет причину, а не одну на все случаи ---------------------------


def line(*, expected=None, actual=None):
    from reports.reconcile import Amount, Line

    return Line(
        key="k", name="PETROVIC MARKO", sheet="NS1",
        amounts=[Amount("net", expected, actual)],
    )


def test_nothing_matched_is_not_explained_by_access():
    """Ни одна строка файла не сопоставилась — про доступ здесь сказать нечего."""
    from reports.reconcile import NOTHING_MATCHED, Reconciliation

    assert Reconciliation(whole_run_visible=True).nothing_compared == NOTHING_MATCHED
    # И у роли с неполным доступом тоже: строк нет вовсе, значит и итогов её
    # роли никто не спрашивал.
    assert Reconciliation(whole_run_visible=False).nothing_compared == NOTHING_MATCHED


def test_hidden_totals_are_named_as_hidden():
    """Роль без всех регистров: причина именно в том, что итоги ей не отданы (T071)."""
    from reports.reconcile import TOTALS_HIDDEN, Reconciliation

    result = Reconciliation(lines=[line(expected=D(100))], whole_run_visible=False)
    assert result.nothing_compared == TOTALS_HIDDEN


def test_a_run_without_totals_is_not_blamed_on_access():
    """Итогов нет ни у кого, а роли отдано всё: доступ ни при чём."""
    from reports.reconcile import NO_RUN_TOTALS, Reconciliation

    result = Reconciliation(lines=[line(expected=D(100))], whole_run_visible=True)
    assert result.nothing_compared == NO_RUN_TOTALS


def test_a_file_without_numbers_is_named_as_such():
    """В файле нет ни одного сверяемого числа — это его свойство, а не наше."""
    from reports.reconcile import NO_FILE_NUMBERS, Reconciliation

    result = Reconciliation(lines=[line(actual=D(100))], whole_run_visible=True)
    assert result.nothing_compared == NO_FILE_NUMBERS


def test_silence_is_empty_when_something_was_compared():
    """Сравнили хоть что-то — объяснять нечего, и подвал показывает суммы."""
    from reports.reconcile import Reconciliation

    result = Reconciliation(lines=[line(expected=D(100), actual=D(100))])
    assert result.nothing_compared == ""


# --- на живых данных, через страницу ------------------------------------------


def test_the_accountant_compares_money_and_is_not_told_about_access(client, calculated_june):
    """Бухгалтеру после D036 итоги отданы — и сверка их сравнивает."""
    login_as(client, "accountant")
    with PLATA_SAMPLE.open("rb") as handle:
        html = body(client.post(
            period_url(client) + "reconcile/", {"table": handle}, follow=True
        ))

    assert "К выплате по сверенным строкам" in html, "деньги не сравнивались"
    assert ACCESS_WORDS not in html, "бухгалтеру сказано, что итоги ей не отданы"


def test_a_file_about_other_people_is_not_explained_by_access(client, calculated_june):
    """Причина нуля — несопоставленные строки, и названа она, а не доступ."""
    html = reconcile_with(
        client, "accountant", strangers(export_bytes(client, "accountant"))
    )
    counts = summary(html)

    assert counts["Сошлось до копейки"] == 0, counts
    assert counts["Есть в таблице, нет в вашей части расчёта"] == 35, counts
    assert ACCESS_WORDS not in html, "ноль объяснён доступом, которого хватает"
    assert "ни одна строка файла" in html, "настоящая причина нуля не названа"


def test_the_manager_still_reads_the_true_reason(client, calculated_june):
    """У управляющего итоги действительно закрыты — и об этом сказано прямо (T100)."""
    login_as(client, "manager")
    with PLATA_SAMPLE.open("rb") as handle:
        html = body(client.post(
            period_url(client) + "reconcile/", {"table": handle}, follow=True
        ))

    counts = summary(html)
    assert counts["Сверены только входы — деньги не сравнивались"] > 0, counts
    assert ACCESS_WORDS in html, "управляющему не сказано, почему деньги не сравнивались"
    assert "К выплате по сверенным строкам" not in html, (
        "показан итог по строкам, которых не сравнивали"
    )
    # Ноль в подвале читался бы как совпадение — его здесь нет ни в каком виде.
    assert "разница 0,00" not in html


def test_the_upload_field_takes_our_own_file(client, calculated_june):
    """Страховка от фиктивной зелени: проверки выше ходят настоящим полем формы."""
    upload = SimpleUploadedFile("empty.xlsx", b"not a workbook at all")
    login_as(client, "accountant")
    response = client.post(period_url(client) + "reconcile/", {"table": upload})
    assert response.status_code == 422, "нечитаемый файл принят как книга"
