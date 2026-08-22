"""Отказ, показанный человеку страницей продукта, а не каркасом (T099).

Что проверяется и почему именно так.

**Не «код 404», а то, что человек видит.** Кодом отказа продукт отвечал
правильно и раньше — а на экране была техническая страница Django с текстом
исключения, перечнем маршрутов проекта и настройками (issue #82). Поэтому
проверки смотрят на содержимое: есть ли навигация, нет ли трассировки, нет ли
служебных слов.

**Оба положения отладки.** `DJANGO_DEBUG=1` — умолчание, с которым продукт
поднимают локально и показывают коллеге, `0` — площадка. Дефект жил ровно
между ними: `handler404` при включённой отладке Django не зовёт вовсе.

**Страница отказа как поверхность утечки.** 404 у нас отвечает одинаково на
несуществующее и на чужое, и любая разница в словах выдала бы существование
чужих данных (D023). Отсюда проверки «два разных промаха дают одну и ту же
страницу» и «ни регистров, ни точек».
"""
from __future__ import annotations

import re

import pytest
from django.test.utils import override_settings

from conftest import body, login_as

BOOM = "tests.boom_urls"

# Слова технической страницы Django и трассировки. Любое из них на экране
# означает, что человек снова читает страницу разработчика.
TECHNICAL = [
    "Traceback",
    "URLconf",
    "Django tried these URL patterns",
    "Request Method:",
    "Exception Type:",
    "DJANGO_SETTINGS_MODULE",
]

# То, что страница отказа не смеет называть (D023): регистры учёта и точки.
# Названия — как они написаны в продукте и в сиде.
SECRETS = [
    "official", "supplementary", "internal",
    "Официальный", "Дополнительный", "Внутренний",
    "BG1", "NS1", "NS2",
]

TRANSLATED = ["en", "sr-latn"]
CYRILLIC = re.compile(r"[а-яёА-ЯЁ]")


def missing_page(client) -> str:
    return body(client.get("/no-such-address/"))


def visible(html: str) -> str:
    """Что человек видит глазами: без разметки, стилей и комментариев."""
    text = re.sub(r"<!--.*?-->", "", html, flags=re.S)
    text = re.sub(r"<style\b.*?</style>", "", text, flags=re.S | re.I)
    return re.sub(r"<[^>]+>", " ", text)


# --- 404 ----------------------------------------------------------------------


@pytest.mark.parametrize("debug", [False, True])
def test_a_missing_address_answers_with_a_page_of_the_product(client, web_env, debug):
    """И на площадке, и на стенде с отладкой — экран продукта, а не каркаса."""
    with override_settings(DEBUG=debug):
        response = client.get("/no-such-address/")
        html = body(response)

    assert response.status_code == 404, response.status_code
    # Признак страницы продукта — марка в шапке. Не `<header`: техническая
    # страница Django тоже начинается с `<header id="summary">`, и проверка по
    # тегу зеленела бы ровно на том, ради чего написана.
    assert 'class="brand"' in html, "на странице нет шапки продукта"
    assert 'href="/periods/"' in html, "с отказа некуда уйти: нет дороги в работу"
    for word in TECHNICAL:
        assert word not in html, f"на странице отказа техническая подробность: {word}"


def test_the_missing_page_does_not_show_the_address_back(client, web_env):
    """Адрес не пишется на страницу словами: он ничего не объясняет и он же ввод.

    В разметке он остаётся ровно в одном месте — в скрытом поле `next` у формы
    переключателя языка, общей для всех страниц. Это штатный механизм Django:
    значение экранируется, а на переходе проверяется, что адрес свой. Убирать
    его на странице отказа значило бы, что смена языка уводит человека с той
    страницы, на которой он стоит.
    """
    text = visible(body(client.get("/no-such-address-with-marker-42/")))
    assert "no-such-address-with-marker-42" not in text


def test_two_different_misses_give_the_same_page(client, web_env):
    """Несуществующее и чужое обязаны выглядеть одинаково — слово в слово.

    На этом стоит вся видимость: 404 отвечает одинаково и на строку, которой
    нет, и на строку чужой точки (см. `tests/test_reports_trace.py`). Разница
    хоть в одном слове — это ответ на вопрос «а она вообще существует».
    """
    login_as(client, "manager")
    first = client.get("/payslips/00000000-0000-4000-8000-000000000001/trace/")
    second = client.get("/payslips/00000000-0000-4000-8000-000000000002/trace/")
    assert first.status_code == second.status_code == 404
    assert visible(body(first)) == visible(body(second))


def test_the_missing_page_names_neither_ledgers_nor_units(client, web_env):
    """Аноним не должен уносить с неё вообще ничего о данных."""
    html = missing_page(client)
    for word in SECRETS:
        assert word not in html, f"страница отказа называет {word}"


@pytest.mark.parametrize("language", TRANSLATED)
def test_the_missing_page_speaks_the_language_of_the_reader(client, web_env, language):
    """Все три языка, а не только исходный: отказ читают так же, как страницу."""
    client.cookies["django_language"] = language
    html = body(client.get("/no-such-address/"))
    # Разметку и стили выкидываем: кириллица в комментариях шаблона человеку
    # не видна, а вот в тексте — видна.
    # «Русский» в переключателе — не забытый перевод, а суть кнопки: человек
    # ищет глазами родное слово. Так же исключён он и в `test_i18n_screens.py`.
    text = visible(html).replace("Русский", "")
    left = CYRILLIC.findall(text)
    assert not left, f"{language}: на странице отказа остался русский текст: {text[:400]}"


# --- 403 ----------------------------------------------------------------------


def test_a_refusal_answers_with_a_page_of_the_product(client, web_env):
    with override_settings(ROOT_URLCONF=BOOM):
        response = client.get("/forbidden/")
        html = body(response)

    assert response.status_code == 403, response.status_code
    assert 'class="brand"' in html, "отказ показан без шапки продукта"
    for word in TECHNICAL:
        assert word not in html, f"на странице отказа техническая подробность: {word}"
    assert "нарочный отказ" not in html, "текст исключения уехал на экран"


def test_a_form_without_a_valid_key_is_told_what_to_do(web_env):
    """CSRF-отказ — страница продукта с советом, а не техническая страница."""
    from django.test import Client

    strict = Client(enforce_csrf_checks=True)
    response = strict.post("/login/", {"username": "director", "password": "dodo-dev"})
    html = body(response)

    assert response.status_code == 403, response.status_code
    assert 'class="brand"' in html, "CSRF-отказ показан без шапки продукта"
    assert "CSRF" not in html, "человеку показали служебное слово"
    for word in TECHNICAL:
        assert word not in html, f"на странице отказа техническая подробность: {word}"


# --- 500 ----------------------------------------------------------------------


def test_a_broken_view_answers_with_a_page_of_the_product(web_env):
    """На площадке (`DJANGO_DEBUG=0`) человек видит продукт, а не «Server Error»."""
    from django.test import Client

    quiet = Client(raise_request_exception=False)
    with override_settings(ROOT_URLCONF=BOOM, DEBUG=False):
        response = quiet.get("/boom/")
        html = body(response)

    assert response.status_code == 500, response.status_code
    assert 'class="brand"' in html, "500 показана без шапки продукта"
    assert "нарочная поломка" not in html, "текст исключения уехал на экран"
    for word in TECHNICAL:
        assert word not in html, f"на странице 500 техническая подробность: {word}"


def test_the_developer_keeps_the_traceback(web_env):
    """С включённой отладкой 500 остаётся трассировкой — её не отняли.

    Это вторая половина решения, и без неё первая была бы вредна: 500 — не
    положение дел, а дефект, и человек, который его чинит, обязан видеть, где
    он случился.
    """
    from django.test import Client

    quiet = Client(raise_request_exception=False)
    with override_settings(ROOT_URLCONF=BOOM, DEBUG=True):
        html = body(quiet.get("/boom/"))

    assert "нарочная поломка" in html, "трассировку у разработчика отняли"


# --- почему посредник не трогает лишнего ---------------------------------------


def test_a_piece_of_a_page_stays_a_piece(client, web_env):
    """Ответ на догрузку куска страницы не подменяется целым экраном.

    Табель досылает ячейки через htmx, и на отказ он ждёт короткий ответ, а не
    страницу с шапкой: страница, вставленная внутрь ячейки, — это сломанный
    экран, а не человеческий отказ.
    """
    login_as(client, "director")
    with override_settings(DEBUG=True):
        response = client.get("/no-such-address/", headers={"hx-request": "true"})
    assert response.status_code == 404
    assert 'class="brand"' not in body(response)


def test_the_missing_page_keeps_its_message_inside_the_plaque(client, web_env):
    """Текст отказа лежит ВНУТРИ плашки, а не под ней.

    Проверка разметки, а не оформления, и это принципиально: рамку рисует
    `.empty`, и по классу страница была правильной всё время, пока выглядела
    сломанной. `{% notice "empty" %}` рендерит абзац, а в тело шаблон кладёт
    `<h3>` — браузер закрывает `<p>` перед заголовком, и от плашки остаётся
    пустая пунктирная рамка, а совет и дорога назад оказываются снаружи
    (issue #141). Первый экран, который видит человек, промахнувшийся адресом,
    читался как поломка продукта.

    Общий запрет на блочное тело у пустых состояний стоит отдельно
    (`tests/test_web_components.py`); здесь проверяется собранная страница —
    правило может быть верным, а страница собранной мимо него.
    """
    import re as regex

    html = missing_page(client)
    plaque = regex.search(r'<p class="empty">(.*?)</p>', html, regex.S)
    assert plaque, "плашки пустого состояния на странице 404 нет вовсе"
    inside = plaque.group(1)

    assert "Здесь ничего нет" in inside, "заголовок плашки выпал из неё"
    assert "Проверьте адрес" in inside, "совет лежит снаружи плашки — она пустая рамка"
    assert 'href="/periods/"' in inside, "дорога назад лежит снаружи плашки"
    # Заголовка внутри абзаца быть не может по определению: именно он и
    # выталкивал текст наружу.
    assert "<h3" not in html, "заголовок внутри плашки-абзаца вернулся"
