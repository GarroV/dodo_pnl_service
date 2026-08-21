"""Разметка, которая есть только в демо (T163).

Тег живёт в приложении демо, а не в `web`, хотя зовёт его шапка продукта. Так, а
не иначе, по одному доводу: демо — отдельная поверхность со своими словами и
своим языком (всегда английский, D035), и в шаблонах продукта её текстов быть не
должно. Приложение `demo` установлено всегда и в продукте безвредно — маршруты
подключаются только при `DEMO_MODE=1`, а этот тег при выключенном демо не
отдаёт ничего.
"""
from __future__ import annotations

from django import template

from ..switching import offer

register = template.Library()


@register.inclusion_tag("demo/_switch.html")
def demo_switch(permission: str, url_name: str, label: str):
    """Ссылка «этот раздел ведёт другая роль — посмотреть ею».

    Ставится там, где шапка иначе не показала бы раздел вовсе:
    `{% demo_switch "directory.manage" "directory" nav_label %}`. В продукте
    отдаёт пустоту, то есть шапка остаётся точно такой, какой была.
    """
    return {"offer": offer(permission, url_name), "label": label}
