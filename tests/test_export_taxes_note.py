"""Файл «Строки для P&L» без налогов говорит об этом вслух (T141).

Что было. У директора и бухгалтера в файле 34 строки и 6 налоговых на
982 484,80; у управляющего точки — 13 строк и **ни одной** налоговой
(issue #90). Причина законная: налог и взносы посчитаны по строке ведомости
целиком и живут в `payslip_totals`, а те видны только роли, которой видна вся
строка (T050, T071). Приписать налог одному регистру нельзя.

Дефект не в том, что налогов нет, а в том, что об этом **нигде не сказано**:
файл называется «Строки для P&L», и человек соберёт из него P&L без налогов на
зарплату, ничего не заподозрив. Тот же класс, что закрывали для бухгалтера в
T088, — только теперь про роль, у которой набор регистров неполный (D031).

**Чего надпись не смеет сказать.** Ни сумм, ни имён, ни названий регистров,
которых роли не видно (D023, D014). «Итоги расчёта вашей роли не отданы» — это
факт о правах, а не о данных: он верен и тогда, когда в скрытом регистре нет ни
одной строки, поэтому вычесть из него нечего.

**Почему причин три, а не одна.** Налогов в файле не бывает по трём разным
поводам: выбран разрез (налог не принадлежит регистру), итоги не отданы роли,
и налоги в расчёте не посчитаны вовсе. Одна фраза на все три была бы неправдой
в двух случаях из трёх — ровно та ошибка, которую в этом блоке уже чинили
(T120, T134).
"""
from __future__ import annotations

import io

import openpyxl
import pytest

from conftest import body, login_as
from test_directory import payruns_restored, sql  # noqa: F401

JUNE = "2026-06-01"

# Куски надписи, по которым её узнают. Не вся фраза целиком: тест не должен
# краснеть от правки запятой, но обязан краснеть от исчезнувшего смысла.
SAID_MISSING = "Налоговая часть"
SAID_RIGHTS = "вашей роли не отданы"
SAID_CUT = "разрез"


@pytest.fixture
def june_calculated(client, web_env, sql, payruns_restored):  # noqa: F811
    """Июнь посчитан — иначе налогам неоткуда взяться ни у кого."""
    from conftest import wipe_payruns

    wipe_payruns(web_env)
    login_as(client, "director")
    assert client.post(
        page_url(sql) + "calculate/", {"inline": "1"}, follow=True
    ).status_code == 200
    yield
    client.post("/logout/")


def page_url(sql) -> str:  # noqa: F811
    period_id = sql.execute(
        "select p.id from periods p join tenants t on t.id = p.tenant_id "
        "where t.code = 'rs-dev' and p.period = %s", (JUNE,)
    ).fetchone()[0]
    return f"/periods/{period_id}/"


def pnl_file(client, sql, query: str = "") -> tuple[list[tuple], str]:  # noqa: F811
    """Строки файла и его текстовая часть — заголовок с примечанием."""
    response = client.get(page_url(sql) + "export/pnl/" + query)
    assert response.status_code == 200, response.status_code
    sheet = openpyxl.load_workbook(io.BytesIO(response.content)).active
    rows = [row for row in sheet.iter_rows(values_only=True) if any(row)]
    words = " ".join(
        str(value) for row in rows for value in row if isinstance(value, str)
    )
    return rows, words


def taxes_in(rows) -> list[tuple]:
    return [row for row in rows if row[3] in ("Налог", "Взносы")]


def test_the_manager_is_told_that_the_tax_part_is_not_in_the_file(
    client, sql, june_calculated,  # noqa: F811
):
    """Роль с неполным набором регистров читает в файле, чего в нём нет и почему."""
    client.post("/logout/")
    login_as(client, "manager")
    try:
        rows, words = pnl_file(client, sql)
        assert rows, "у управляющего пустой файл — проверять нечего"
        assert not taxes_in(rows), "у управляющего внезапно появились налоговые строки"

        assert SAID_MISSING in words, f"файл молчит о налогах:\n{words[:400]}"
        assert SAID_RIGHTS in words, f"не названа причина:\n{words[:400]}"
    finally:
        client.post("/logout/")


def test_the_note_names_no_ledger_no_person_and_no_amount(
    client, sql, june_calculated,  # noqa: F811
):
    """Надпись говорит о правах, а не о скрытых данных (D023).

    Названия скрытого регистра в ней быть не может: оно и есть то, что скрывают.
    """
    client.post("/logout/")
    login_as(client, "manager")
    try:
        rows, _words = pnl_file(client, sql)
        note = " ".join(
            str(value) for row in rows[:4] for value in row if isinstance(value, str)
        )
        assert "Внутренний" not in note, note
        assert "982" not in note, "в надписи оказалась сумма налогов"
    finally:
        client.post("/logout/")


def test_the_file_with_taxes_says_nothing(client, sql, june_calculated):  # noqa: F811
    """Директору говорить нечего: налоговая часть в его файле есть.

    Надпись, стоящая всегда, — это шум, который перестают читать; и она соврала
    бы ровно там, где всё в порядке.
    """
    rows, words = pnl_file(client, sql)
    assert taxes_in(rows), "у директора нет налоговых строк — условие теста не то"
    assert SAID_MISSING not in words, f"лишняя надпись:\n{words[:400]}"


def test_a_cut_file_explains_its_own_missing_taxes(client, sql, june_calculated):  # noqa: F811
    """В разрезе налогов нет по своей причине, и она называется своей же фразой."""
    rows, words = pnl_file(client, sql, "?ledger=official")
    assert not taxes_in(rows), "в разрезе появились налоги"
    assert SAID_MISSING in words, f"файл разреза молчит о налогах:\n{words[:400]}"
    assert SAID_CUT in words, f"причина названа не та:\n{words[:400]}"
    assert SAID_RIGHTS not in words, "разрез объяснён правами — это неправда"


def test_the_screen_and_the_file_of_a_cut_say_the_same(client, sql, june_calculated):  # noqa: F811
    """Экран разреза говорит то же, что файл разреза.

    Найдено смоуком на стенде: экран спрашивал «есть ли у периода налоги»
    (у директора — есть) и молчал, а файл того же разреза приезжал без налогов и
    объяснял это надписью. Расхождение экрана и файла — ровно то, против чего
    написана вся задача, поэтому проверка стоит отдельно.
    """
    _rows, in_file = pnl_file(client, sql, "?ledger=official")
    on_screen = body(client.get(page_url(sql) + "?ledger=official"))

    assert SAID_MISSING in in_file
    assert SAID_MISSING in on_screen, "экран разреза молчит о том, о чём говорит файл"
    assert SAID_CUT in on_screen


def test_the_screen_says_it_too(client, sql, june_calculated):  # noqa: F811
    """Экран говорит то же самое: узнать об этом, уже открыв файл, — поздно."""
    client.post("/logout/")
    login_as(client, "manager")
    try:
        html = body(client.get(page_url(sql)))
        assert SAID_MISSING in html, "экран управляющего молчит о налогах"
        assert SAID_RIGHTS in html
    finally:
        client.post("/logout/")

    login_as(client, "director")
    assert SAID_MISSING not in body(client.get(page_url(sql))), "лишняя надпись у директора"
