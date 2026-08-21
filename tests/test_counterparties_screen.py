"""Экран контрагентов: кто читает, кто ведёт, как ищет (T150).

Проверки идут настоящими запросами к продукту, а не вызовом функций: у этого
экрана главное свойство — **список открыт всем ролям, а правка одному**, и
проверить его можно только тем, что видит человек. Разграничение при этом стоит
в базе (`tests/test_counterparties.py`, роль `app_user`), здесь — слова и кнопки.

За собой тест убирает: база `web_env` живёт весь прогон и одна на все модули, а
раздел справочников показывает счётчик контрагентов — оставленная строка сдвинула
бы его соседям.
"""
from __future__ import annotations

import pytest

from conftest import body, login_as

LIST = "/directory/counterparties/"
NEW = "/directory/counterparties/new/"


@pytest.fixture
def no_counterparties(web_env):
    """Справочник до теста и после него пуст: сид его не наполняет."""
    from core.models import Counterparty

    Counterparty.objects.all().delete()
    yield
    Counterparty.objects.all().delete()


def add(client, **fields):
    body_ = {"title": "EPS Elektro", "valid_from": "2026-01-01"}
    body_.update(fields)
    return client.post(NEW, body_)


# --- кто читает и кто ведёт ---------------------------------------------------


@pytest.mark.parametrize("role", ["director", "accountant", "manager", "admin"])
def test_every_role_reads_the_directory(client, no_counterparties, role):
    """Список открыт каждому: контрагента выбирают, внося счёт."""
    login_as(client, role)
    answer = client.get(LIST)
    assert answer.status_code == 200
    assert "Контрагенты" in body(answer)


def test_only_the_network_admin_is_offered_the_button(client, no_counterparties):
    """Кнопка «Завести» есть у того, кто ведёт справочники, и только у него."""
    login_as(client, "admin")
    assert "Завести контрагента" in body(client.get(LIST))

    login_as(client, "accountant")
    assert "Завести контрагента" not in body(client.get(LIST))


def test_the_form_refuses_with_words(client, no_counterparties):
    """Адрес формы не спрятан: он отвечает отказом, который можно прочитать."""
    login_as(client, "accountant")
    answer = client.get(NEW)
    assert answer.status_code == 403
    assert "Ведение справочников" in body(answer)

    # И запись тоже — иначе отказ был бы косметикой.
    assert add(client).status_code == 403
    from core.models import Counterparty
    assert Counterparty.objects.count() == 0


# --- ведение ------------------------------------------------------------------


def test_the_admin_adds_a_counterparty(client, no_counterparties):
    login_as(client, "admin")
    answer = add(client, tax_number="100 111 222", external_id="vendor-11",
                 aliases="EPS, EPS DISTRIBUCIJA AD")
    assert answer.status_code == 302

    shown = body(client.get(LIST))
    assert "EPS Elektro" in shown
    assert "vendor-11" in shown

    from core.models import Counterparty
    saved = Counterparty.objects.get(title="EPS Elektro")
    assert saved.aliases == ["EPS", "EPS DISTRIBUCIJA AD"]
    # Автор строки проставлен: «кто это завёл» — первый вопрос к незнакомому
    # поставщику.
    assert saved.created_by is not None


def test_the_same_title_is_refused_with_words(client, no_counterparties):
    """Второе написание того же поставщика — отказ формы, а не пятисотка.

    Ровно тот класс отказа, ради которого заведён `web/dbrefusal.py`: правило
    живёт в базе, а человек читает про своё поле.
    """
    login_as(client, "admin")
    add(client)
    answer = add(client, tax_number="другой")
    # 400, как у остальных форм справочника (`tests/test_constraint_refusal.py`):
    # отказ базы по ограничению — это «введено не то», а не «положение дел».
    assert answer.status_code == 400, body(answer)
    assert "Название" in body(answer)
    # Введённое остаётся в полях: набирать заново из-за одной опечатки не должен.
    assert "другой" in body(answer)


def test_closing_by_date_keeps_the_card(client, no_counterparties):
    """Контрагент закрывается датой: карточка остаётся, дата видна."""
    login_as(client, "admin")
    add(client)

    from core.models import Counterparty
    card = f"/directory/counterparties/{Counterparty.objects.get().id}/"
    assert client.post(card, {
        "title": "EPS Elektro", "valid_from": "2026-01-01", "valid_to": "2026-07-01",
    }).status_code == 302
    assert "2026-07-01" in body(client.get(LIST))


def test_dates_that_run_backwards_are_refused(client, no_counterparties):
    """Закрыт раньше, чем начал, — отказ ФОРМЫ, словами, до записи.

    Не отказом базы: проверка `23514` в `web/dbrefusal.py` намеренно не
    переводится в слова — она означает либо дефект кода, либо правило, которое
    форма обязана объяснить сама. Вежливое «поправьте ввод» на её месте прятало
    бы поломку в журнал. Проверка в базе при этом остаётся гарантией на все
    пути записи (`tests/test_counterparties.py`).
    """
    login_as(client, "admin")
    answer = add(client, valid_from="2026-06-01", valid_to="2026-01-01")
    assert answer.status_code == 400, body(answer)
    assert "Закрыт с" in body(answer)


def test_an_unknown_card_answers_the_same_as_a_stranger(client, no_counterparties):
    """Выдуманный номер — 404, тот же ответ, что у контрагента чужого партнёра."""
    login_as(client, "admin")
    assert client.get(
        "/directory/counterparties/00000000-0000-4000-8000-000000000009/"
    ).status_code == 404


# --- поиск --------------------------------------------------------------------


def test_search_finds_by_spelling_not_only_by_title(client, no_counterparties):
    """Строка из выписки находит карточку: ради этого написания и заводят."""
    login_as(client, "admin")
    add(client, aliases="EPS DISTRIBUCIJA AD")
    add(client, title="Metro", tax_number="100 333 444")

    found = body(client.get(LIST, {"q": "DISTRIBUCIJA"}))
    assert "EPS Elektro" in found
    assert ">Metro<" not in found

    assert "Metro" in body(client.get(LIST, {"q": "100 333"}))


def test_search_that_finds_nothing_says_so(client, no_counterparties):
    """Пустой ответ поиска называется пустым ответом, а не «справочник пуст»."""
    login_as(client, "admin")
    add(client)
    shown = body(client.get(LIST, {"q": "никого"}))
    assert "Ничего не нашлось" in shown
    assert "EPS Elektro" not in shown
