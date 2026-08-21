"""Каркас интерфейса: компоненты, из которых собираются экраны (T016).

Зачем это существует. За три очереди экраны писались по одному, и каждый нёс
свою копию одной и той же разметки: плашки, контейнер широкой таблицы, метка
регистра, «действие или объяснение, почему его нет». Копии стоили дорого не
объёмом, а тем, что в них живут инварианты, купленные дефектами: ведомость,
которая обязана влезать в 1440; кнопка, которая не смеет пропасть молча; метка
регистра, которая не смеет назвать регистр, чужой для роли (D023).
Разъехавшаяся копия такого инварианта — это не «некрасиво», это вернувшийся
дефект.

Почему теги с телом, а не `{% include %}` со строкой в параметре. Текст плашки
остаётся **в шаблоне**, а не приезжает из Python: локализация (T017) оборачивает
строки шаблона штатным механизмом переводов, а строку, собранную в
представлении, ей не достать. Заодно тело тега принимает разметку — ссылку,
`<code>`, список — и её не приходится помечать безопасной руками.

Что здесь НЕ живёт: подписи, которые сами по себе данные (текст отказа приходит
из `permissions.explain`). Они передаются параметрами компонентов-`include` в
`templates/web/components/` — см. `components/README.md`.
"""
from __future__ import annotations

from django import template
from django.template.base import Node, token_kwargs
from django.utils.html import escape, format_html
from django.utils.safestring import mark_safe

register = template.Library()


# Плашки. Три вида, и разница между ними не декоративная:
#   ok    — то, что уже случилось по воле человека;
#   alert — то, что помешало или требует внимания;
#   empty — пустое состояние: данных нет, и надо объяснить почему.
# Четвёртый вид не заводится, пока нельзя объяснить, чем он отличается от этих.
NOTICE_KINDS = ("ok", "alert", "empty")


class NoticeNode(Node):
    def __init__(self, kind, kwargs, nodelist):
        self.kind = kind
        self.kwargs = kwargs
        self.nodelist = nodelist

    def render(self, context):
        kind = self.kind.resolve(context)
        if kind not in NOTICE_KINDS:
            raise template.TemplateSyntaxError(
                f"{{% notice %}}: вид «{kind}» не из {NOTICE_KINDS}"
            )
        values = {name: value.resolve(context) for name, value in self.kwargs.items()}
        # Тело уже отрисовано движком шаблонов, то есть экранировано по его
        # правилам. Заголовок приходит переменной — его экранируем сами.
        body = self.nodelist.render(context).strip()
        title = values.get("title") or ""
        extra = values.get("extra") or ""
        classes = f"{kind} {escape(extra)}".strip() if extra else kind
        # Пустое состояние — абзац, а не блок: это текст на месте данных, и
        # рамка вокруг него уже нарисована стилем `.empty`.
        tag = "p" if kind == "empty" else "div"
        head = format_html("<strong>{}</strong> ", title) if title else ""
        return mark_safe(f'<{tag} class="{classes}">{head}{body}</{tag}>')


@register.tag("notice")
def do_notice(parser, token):
    """`{% notice "alert" title="Не вышло." %}текст{% endnotice %}`."""
    bits = token.split_contents()
    if len(bits) < 2:
        raise template.TemplateSyntaxError(
            "{% notice %} требует вид: ok, alert или empty"
        )
    kind = parser.compile_filter(bits[1])
    kwargs = token_kwargs(bits[2:], parser) if len(bits) > 2 else {}
    nodelist = parser.parse(("endnotice",))
    parser.delete_first_token()
    return NoticeNode(kind, kwargs, nodelist)


class ScrollNode(Node):
    def __init__(self, nodelist):
        self.nodelist = nodelist

    def render(self, context):
        return mark_safe(f'<div class="scroll">{self.nodelist.render(context)}</div>')


@register.tag("scroll")
def do_scroll(parser, token):
    """Контейнер широкой таблицы: `{% scroll %}<table …>…</table>{% endscroll %}`.

    Прокручивается контейнер, а не страница. Это не оформление: требование
    «ведомость читается на 1440 без горизонтальной прокрутки» проверяется у
    контейнера — страница не едет никогда, и первый смоук, мерявший страницу,
    был зелёным при 191px перелива (журнал блока `reports`). Прокрутка внутри
    контейнера остаётся правильной деградацией для страны с бо́льшим набором
    компонентов.
    """
    if len(token.split_contents()) != 1:
        raise template.TemplateSyntaxError("{% scroll %} параметров не принимает")
    nodelist = parser.parse(("endscroll",))
    parser.delete_first_token()
    return ScrollNode(nodelist)


@register.filter("may")
def may(who, code: str) -> bool:
    """Есть ли у вошедшего это право: `{% if principal|may:"directory.manage" %}`.

    Нужно шапке и любому меню: ссылка на экран, который откажет, — это то же
    самое, что кнопка, которая откажет (T072), только хуже — человек уходит со
    своей страницы, чтобы прочитать отказ. Объяснять здесь нечего: отсутствие
    целого раздела у роли, которая его не ведёт, — не пропажа кнопки посреди
    работы, а нормальное устройство продукта.

    Фильтром, а не полем контекста: контекст шапки собирает `principal.principal`
    — файл блока `auth`, и дописывать туда поле на каждое новое право значило бы
    править чужой контракт ради своей разметки. Решение о показе при этом
    по-прежнему одно на продукт: и фильтр, и `permissions.explain` спрашивают
    один и тот же `permissions.has`.
    """
    from web import permissions

    return permissions.has(who, code)


# Экраны, живущие под своим корнем адреса, но принадлежащие чужому разделу
# шапки. След расчёта, расчётный лист и табель открываются из ведомости и
# остаются «Периодами»; накладная и платёж — «Счетами». Карта короткая
# намеренно: она про адреса, а не про права, и разрастание здесь означало бы,
# что разделы шапки разъехались с устройством адресов.
NAV_BELONGS = {
    "payslips": "periods",
    "timesheets": "periods",
    "payments": "invoices",
    "inbox": "invoices",
}


def _nav_root(path: str) -> str:
    """Корень адреса, приведённый к разделу шапки."""
    parts = [part for part in (path or "").split("/") if part]
    root = parts[0] if parts else ""
    return NAV_BELONGS.get(root, root)


@register.filter("in_section")
def in_section(request_path: str, section_url: str) -> bool:
    """Человек сейчас в этом разделе шапки: `{% if request.path|in_section:url %}`.

    Сравниваются корни адресов, а не адреса целиком. Иначе выделение пропадало
    бы на каждом вложенном экране — на следе расчёта, в табеле, на карточке
    счёта, — а пункт, потерявший выделение, читается как «вы ушли из раздела»,
    хотя человек внутри него.

    Фильтром, а не полем контекста: раздел — это оформление шапки, и
    дописывать его в контекст, который собирает блок доступа, значило бы
    править чужой контракт ради своей разметки.
    """
    return bool(section_url) and _nav_root(request_path) == _nav_root(section_url)


@register.simple_tag
def ledger(title):
    """Метка регистра учёта: `{% ledger row.ledger %}`.

    Метка, а не заливка строки (D023): заливка мешает читать числа и спорит с
    подсветкой отклонений. Показывается **название**, уже отобранное для этой
    роли; сам компонент ничего не берёт из справочника регистров — иначе он
    однажды назовёт роли регистр, которого она не видит.
    """
    return format_html('<span class="ledger">{}</span>', title)
