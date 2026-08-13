"""Значения следа расчёта — числами, а не внутренним представлением (T103).

След — экран, ради которого продукт вообще заводили: человек приходит сюда из
ячейки ведомости, чтобы повторить сумму на калькуляторе. Значит каждая величина
здесь обязана быть читаемой рукой. `{'sick': Decimal('20')}` рукой не читается:
это `repr` питоновского объекта, вышедший на экран через `str(value)`.

Дефект жил ровно в одном месте: у шага «доплата до минимума» вход `часов` —
не одно число, а раскладка по видам часов, и разбора словаря в показе не было.
В базе значение хранится нормально (`"hours": {"sick": {"$dec": "20"}}`) —
ломается именно вывод.

Проверки две и они разные по назначению. Первая — на самой функции показа: она
называет дефект по имени и краснеет мгновенно. Вторая — на живых страницах всех
следов месяца: она ловит **любое** значение любого будущего типа, для которого
показ снова забудут, и не зависит от того, догадался ли автор проверки про
словарь.
"""
from __future__ import annotations

import re
from decimal import Decimal

import pytest

from conftest import body, login_as, period_url, wipe_payruns

# Признаки внутреннего представления на экране. Ищутся и в сыром, и в
# экранированном виде: разметка превращает кавычку в `&#x27;`, и проверка,
# знающая только про `'`, прошла бы мимо ровно того случая, который был.
RAW_PYTHON = re.compile(r"Decimal\(|&#x27;|\{&#39;|\{'|\bdict\(|OrderedDict")


# --- 1. сама функция показа --------------------------------------------------


def test_a_split_value_is_shown_as_numbers_not_as_a_dictionary(web_env):
    """Раскладка по видам часов — это числа с подписями, а не `repr` словаря."""
    from web.views import input_value

    shown = input_value("hours", {"sick": Decimal("20")})
    assert not RAW_PYTHON.search(shown), (
        f"на экран следа выехало внутреннее представление: {shown!r}"
    )
    assert "20" in shown, f"число потерялось при показе: {shown!r}"
    assert "sick" in shown, (
        f"вид часов потерялся: без него непонятно, чьи это 20 часов — {shown!r}"
    )


def test_a_split_value_keeps_the_format_of_its_own_kind(web_env):
    """Часы внутри раскладки печатаются как часы, а не как деньги.

    Иначе на одном экране рядом стоят `часов 20,00` и `часов sick 20` — и
    человек, который сверяет след с табелем, теряет секунды на догадку, одно ли
    это число.
    """
    from web.views import input_value

    single = input_value("hours", Decimal("20"))
    split = input_value("hours", {"sick": Decimal("20")})
    assert single in split, (
        f"одно и то же число показано по-разному: {single!r} и {split!r}"
    )


def test_a_list_of_values_does_not_leak_representation_either(web_env):
    """Список — вторая дорога к тому же дефекту, и она закрыта той же функцией."""
    from web.views import input_value

    shown = input_value("hours", [Decimal("20"), Decimal("8.5")])
    assert not RAW_PYTHON.search(shown), f"список показан как есть: {shown!r}"


# --- 2. живые страницы всех следов месяца ------------------------------------


@pytest.fixture
def calculated_june(client, web_env):
    wipe_payruns(web_env)
    login_as(client, "director")
    response = client.post(period_url(client) + "calculate/", follow=True)
    assert response.status_code == 200
    return None


def trace_urls(client, user: str) -> list[str]:
    login_as(client, user)
    html = body(client.get(period_url(client)))
    found = re.findall(r'<a class="trace" href="([^"]+)"', html)
    assert found, "в ведомости нет ни одной ссылки на след расчёта"
    return [url.replace("&amp;", "&") for url in found]


def test_no_trace_page_of_the_month_shows_internal_representation(
    client, calculated_june
):
    """Обход всех следов месяца — проверка, не знающая, что именно сломается.

    Проверка первого рода знает про словарь, потому что словарь уже нашли.
    Следующий тип значения (кортеж, множество, вложенный список) она пропустит,
    и экран снова покажет питоновский объект. Эта — не пропустит: она смотрит
    не на тип, а на признак «на странице внутреннее представление».
    """
    urls = trace_urls(client, "director")
    problems = []
    for url in urls:
        html = body(client.get(url))
        for hit in set(RAW_PYTHON.findall(html)):
            где = html[max(0, html.find(hit) - 60):html.find(hit) + 60]
            problems.append(f"{url}: {hit!r} рядом с …{где.strip()}…")
    assert not problems, (
        f"внутреннее представление на экране следа ({len(problems)} из "
        f"{len(urls)} страниц):\n" + "\n".join(problems[:10])
    )
