"""Каркас интерфейса: компоненты и приёмка T016.

Что здесь проверяется и почему именно так.

**Главная проверка задачи — не «компоненты существуют», а «экраны собраны из
них».** Библиотека компонентов, мимо которой продолжают писать разметку руками,
хуже её отсутствия: она создаёт впечатление порядка. Поэтому первый раздел —
обход всех шаблонов продукта с запретом на разметку, у которой компонент уже
есть.

**Компоненты проверяются по инвариантам, а не по внешнему виду.** В них живут
решения, купленные дефектами: кнопка не смеет пропасть молча (T072), контейнер
широкой таблицы прокручивается сам (1440), переключатель разреза собирается из
показанных данных, а не из справочника (D023). Проверяется именно это.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
COMPONENTS = ROOT / "src" / "web" / "templates" / "web" / "components"

# Все шаблоны продукта, кроме самих компонентов: компонентам их разметку писать
# как раз и положено.
SCREENS = sorted(
    path
    for path in ROOT.glob("src/*/templates/**/*.html")
    if COMPONENTS not in path.parents
)

# Разметка, у которой есть компонент. Ключ — что нашли, значение — чем
# заменять. Список намеренно короткий: сюда попадает только то, в чём живёт
# решение, а не всё похожее.
HAND_WRITTEN = {
    '<span class="ledger"': "{% ledger … %}",
    '<div class="scroll"': "{% scroll %}…{% endscroll %}",
    '<div class="ok"': '{% notice "ok" %}',
    '<div class="alert"': '{% notice "alert" %}',
    '<p class="alert"': '{% notice "alert" %}',
    '<p class="empty"': '{% notice "empty" %}',
    '<nav class="cuts"': "components/cuts.html",
    '<nav class="exports"': "components/exports.html",
    "htmx-2.": "components/htmx.html — версия названа один раз",
}


def test_screens_are_assembled_from_components_not_copy_paste():
    """Приёмка T016 целиком: разметки, у которой есть компонент, в экранах нет.

    Проверка живёт файлом, а не договорённостью: договорённость не падает.
    """
    assert SCREENS, "шаблоны не нашлись — проверка проверяет пустоту"
    found = []
    for path in SCREENS:
        text = path.read_text(encoding="utf-8")
        for pattern, replacement in HAND_WRITTEN.items():
            if pattern in text:
                found.append(f"{path.relative_to(ROOT)}: {pattern} → {replacement}")
    assert not found, "экраны пишут руками то, для чего есть компонент:\n" + "\n".join(found)


def test_every_component_explains_itself():
    """Компонент без объяснения «почему он один на продукт» — просто файл.

    Через полгода следующий человек либо повторит разметку рядом, либо
    выкинет компонент как лишний слой. Оба исхода возвращают копипасту.
    """
    silent = [
        path.name
        for path in sorted(COMPONENTS.glob("*.html"))
        if "{% comment %}" not in path.read_text(encoding="utf-8")
    ]
    assert not silent, f"компоненты без объяснения: {silent}"


# --- сами компоненты ---------------------------------------------------------


def render(template: str, **context) -> str:
    from django.template import Context, Template

    return Template("{% load ui %}" + template).render(Context(context))


@pytest.fixture
def django_templates(web_env):
    """Компонентам нужен настроенный Django, но не нужны данные."""
    return web_env


def test_notice_marks_what_happened_apart_from_what_went_wrong(django_templates):
    ok = render('{% notice "ok" %}Готово{% endnotice %}')
    alert = render('{% notice "alert" title="Не вышло." %}причина{% endnotice %}')
    empty = render('{% notice "empty" %}Данных нет{% endnotice %}')

    assert ok == '<div class="ok">Готово</div>'
    assert alert == '<div class="alert"><strong>Не вышло.</strong> причина</div>'
    # Пустое состояние — абзац: это текст на месте данных, а не блок поверх них.
    assert empty == '<p class="empty">Данных нет</p>'


def test_notice_refuses_an_unknown_kind_instead_of_inventing_one(django_templates):
    """Молча отрисованная плашка неизвестного вида не видна никому, кроме глаз."""
    from django.template import TemplateSyntaxError

    with pytest.raises(TemplateSyntaxError):
        render('{% notice "красная" %}что-то{% endnotice %}')


def test_notice_escapes_what_comes_from_data(django_templates):
    """Заголовок плашки бывает текстом отказа из базы — экранируется."""
    out = render('{% notice "alert" title=t %}тело{% endnotice %}', t="<b>ой</b>")
    assert "<b>ой</b>" not in out
    assert "&lt;b&gt;" in out


def test_scroll_wraps_the_table_so_the_page_never_moves(django_templates):
    out = render("{% scroll %}<table class=\"sheet\"></table>{% endscroll %}")
    assert out == '<div class="scroll"><table class="sheet"></table></div>'


def test_ledger_is_a_badge_and_escapes_its_title(django_templates):
    assert render("{% ledger v %}", v="Внутренний") == '<span class="ledger">Внутренний</span>'
    assert "<i>" not in render("{% ledger v %}", v="<i>x</i>")


def test_action_shows_the_button_when_the_right_is_there(django_templates):
    out = render(
        '{% include "web/components/action.html" with allowed=True url="/go/" label="Нажать" %}'
    )
    assert 'action="/go/"' in out and "Нажать" in out


def test_action_explains_itself_instead_of_disappearing(django_templates):
    """T072: исчезнувшая без объяснения кнопка читается как поломка."""
    out = render(
        '{% include "web/components/action.html" with allowed=False'
        ' denied="Права нет: расчёт ведёт бухгалтер." url="/go/" label="Нажать" %}'
    )
    assert "Права нет: расчёт ведёт бухгалтер." in out
    # Кнопки нет по-настоящему, а не спрятана стилем: иначе её нажали бы с
    # клавиатуры и получили 403 на действие, которое им же и предложили.
    assert "<button" not in out and "<form" not in out


def test_action_stays_silent_when_there_is_nothing_to_explain(django_templates):
    """Отказа нет — значит действие тут просто неуместно, и молчать правильно."""
    out = render(
        '{% include "web/components/action.html" with allowed=False denied="" '
        'url="/go/" label="Нажать" %}'
    )
    assert out.strip() == ""


def test_action_asks_for_a_reason_when_the_action_needs_one(django_templates):
    """Причина — условие действия, а не уточнение по желанию (D021)."""
    out = render(
        '{% include "web/components/action.html" with allowed=True url="/reopen/"'
        ' label="Открыть заново" reason_name="reason" reason_id="reason"'
        ' reason_label="Зачем открываете период" %}'
    )
    assert 'name="reason"' in out and "required" in out
    assert '<label for="reason">' in out


def test_field_binds_its_label_to_the_input(django_templates):
    out = render(
        '{% include "web/components/field.html" with id="id_login" name="login"'
        ' label="Логин" %}'
    )
    assert '<label for="id_login">' in out and 'id="id_login"' in out


def test_field_without_a_label_has_no_dangling_id(django_templates):
    """Поле причины в ведомости подписи не имеет: колонка узкая, смысл рядом."""
    out = render(
        '{% include "web/components/field.html" with name="reason"'
        ' placeholder="из-за чего спор" required=True %}'
    )
    assert "<label" not in out and 'id=""' not in out


def test_cuts_show_only_what_the_role_can_see(django_templates):
    """D023: регистра, которого роль не видит, нет даже пустой кнопкой."""
    cuts = [
        {"code": "", "title": "Все регистры", "url": "/p/", "selected": False},
        {"code": "official", "title": "Официальный", "url": "/p/?ledger=official",
         "selected": True},
    ]
    out = render('{% include "web/components/cuts.html" with cuts=cuts %}', cuts=cuts)
    assert "Официальный" in out and "Все регистры" in out
    assert "Дополнительный" not in out and "Внутренний" not in out
    # Выбранный разрез — не ссылка: ссылка на текущую страницу обманывает и
    # мышь, и клавиатуру.
    assert '<span class="cut current" aria-current="true">Официальный</span>' in out
    assert 'href="/p/?ledger=official"' not in out


def test_cuts_disappear_entirely_when_there_is_nothing_to_switch(django_templates):
    out = render('{% include "web/components/cuts.html" with cuts=empty %}', empty=[])
    assert out.strip() == ""


# --- на живой странице -------------------------------------------------------


def test_exports_carry_the_cut_the_person_is_looking_at(client):
    """Файл обязан содержать ровно то, что видно на экране (T031, D024).

    Разрез уезжает в адрес выгрузки из того же места, где он выбран. Если экран
    и файл разъедутся, заметят это по числам в чужой таблице, а не здесь.
    """
    from conftest import body, login_as, period_url

    login_as(client, "director")
    url = period_url(client)
    assert client.post(url + "calculate/", follow=True).status_code == 200

    whole = body(client.get(url))
    assert 'href="/periods/' in whole
    assert "/export/payout/?ledger=" not in whole, (
        "разрез «все регистры» не должен подмешивать регистр в адрес выгрузки"
    )

    one = body(client.get(url + "?ledger=official"))
    assert "/export/payout/?ledger=official" in one
    assert "/export/pnl/?ledger=official" in one
    assert "/export/partner/?ledger=official" in one
