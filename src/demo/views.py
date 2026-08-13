"""
Точка входа в демо: посмотреть продукт за один клик, без регистрации.

Три решения, которые стоит понимать.

**Отдельного способа проверить личность здесь нет.** Кнопка подставляет пароль
демо-учётки и идёт тем же путём, что человек с клавиатурой (`web.auth`). Это
важно: демо-вход, умеющий входить мимо обычной проверки, был бы вторым входом в
продукт — то есть второй поверхностью, которую надо охранять.

**Маршрутов нет, пока демо не включено.** `DEMO_MODE=1` — переменная окружения
демо-стенда; в продукте её нет, и тогда `demo.urls` не подключается вовсе (см.
`config/urls.py`). Не «страница отвечает 403», а именно отсутствует: выключенная
функциональность, которая всё ещё отвечает, однажды ответит не то.

**Ключ-спидбамп — не секрет.** `DEMO_KEY` спасает от случайного прохожего и
поисковика, а не от злоумышленника: в демо-базе нет ничего, кроме выдуманных
людей, и охранять там нечего. Поэтому ключ можно писать прямо в ссылку и слать
в переписке.

Страницы этого модуля — единственное место демо, где есть свои слова. Всё
остальное посетитель видит на обычных экранах продукта.
"""
from __future__ import annotations

from datetime import date

from django.conf import settings
from django.http import Http404, HttpResponse
from django.shortcuts import redirect, render

from web.auth import login_with_password

from .accountant_table import build_accountant_table
from .seed import ROLES, demo_password
from .table import accountant_rows, full_slice

__all__ = ["accountant_file", "enter", "landing"]

# Куда попадает посетитель после входа. Список периодов, а не главная: смотреть
# в демо надо месяцы, и лишний клик по дороге к ним — потерянный посетитель.
LANDS_ON = "/periods/"

# Роль по умолчанию. Директор видит все три регистра учёта и все точки, то есть
# продукт целиком; с бухгалтера демо начинать нельзя — он увидит срезанные
# данные и решит, что это всё, что есть.
DEFAULT_ROLE = "director"

# За какой месяц демо отдаёт таблицу бухгалтера для сверки. Закрытый месяц, а не
# открытый: сверяют то, что уже посчитано и утверждено.
RECONCILE_PERIOD = date(2026, 6, 1)


def enabled() -> bool:
    return bool(getattr(settings, "DEMO_MODE", False))


def _key_ok(request) -> bool:
    """Спидбамп. Ключа не задано — открыто всем, и это осознанный режим."""
    key = (getattr(settings, "DEMO_KEY", "") or "").strip()
    if not key:
        return True
    return request.GET.get("key", "") == key or request.session.get("demo_key") == key


def _guard(request) -> None:
    if not enabled():
        raise Http404("demo is off")
    if not _key_ok(request):
        raise Http404("demo key required")
    key = (getattr(settings, "DEMO_KEY", "") or "").strip()
    if key:
        # Ключ запоминается в сессии, чтобы посетителю не пришлось таскать его
        # в каждой ссылке внутри демо.
        request.session["demo_key"] = key


def landing(request):
    """Титульная страница демо. Единственный экран со своими словами."""
    _guard(request)
    roles = [
        {"code": code, "title": title, "ledgers": ledgers, "unit": unit}
        for code, title, ledgers, unit, _permissions in ROLES
        # Администратор сети в демо не показывается кнопкой: данных он не
        # правит и ведомостей не видит, и посетитель, зашедший им первым,
        # решил бы, что продукт пуст.
        if code != "admin"
    ]
    return render(
        request,
        "demo/landing.html",
        {
            "roles": roles,
            "default_role": DEFAULT_ROLE,
            "reconcile_period": RECONCILE_PERIOD,
        },
    )


def enter(request, role: str = DEFAULT_ROLE):
    """Войти в демо выбранной ролью и оказаться в продукте."""
    _guard(request)
    known = {code for code, *_rest in ROLES}
    if role not in known:
        raise Http404("no such demo role")

    user = login_with_password(request, role, demo_password())
    if user is None:
        # Молчаливого «не пустило» быть не должно: это не ошибка посетителя, а
        # незаполненная демо-база, и человеку надо сказать именно это.
        return render(
            request,
            "demo/unavailable.html",
            {"role": role},
            status=503,
        )
    return redirect(LANDS_ON)


def accountant_file(request):
    """Таблица бухгалтера для экрана сверки — собранная из самих демо-данных.

    Зачем она вообще. Сверка сравнивает расчёт продукта с файлом, который ведёт
    бухгалтер; без файла экран показывает пустую форму, то есть не показывает
    ничего. Настоящую таблицу партнёра в демо класть нельзя (D028), а выдуманный
    файл, не связанный с демо-данными, разошёлся бы по всем строкам сразу — это
    не демонстрация, а шум.

    Поэтому файл **собирается из демо-расчёта** в момент скачивания и содержит
    ровно три расхождения, поставленных нарочно (см. `demo.table`): одно
    существенное, одно копеечное и один человек, которого в таблице нет. Так
    экран сверки показывает все свои состояния сразу, и после каждого сброса
    демо — те же самые.

    **Файл один и тот же для всех, кто его открыл** (T104, issue #88). Это
    решение, а не побочный эффект: таблицу ведёт бухгалтер у себя, и остальным
    она приходит письмом — артефакт, пришедший со стороны, не может зависеть от
    того, кто его скачал. Иначе сверка показывала бы разным ролям разные
    расхождения в ОДНОМ И ТОМ ЖЕ файле, то есть демо врало бы ровно про то, ради
    чего этот экран существует. Раньше файл собирался срезом скачивающего, и
    роли с неполным набором регистров получали 404 с текстом про непосчитанный
    период — при том что период посчитан и утверждён.

    Чем это ограничено, чтобы не стать дырой: маршрут живёт **только в
    демо-стенде** (`DEMO_MODE`, `config/urls.py`), а демо-база отдельная и
    населена выдуманными людьми (D016). В продукте такого маршрута нет, и
    переносить этот приём туда нельзя: там за строками стоят живые люди, и
    «файл вне продукта» перестанет быть правдой в тот же день.
    """
    _guard(request)

    # Ссылка на файл лежит на титульной странице, то есть посетитель может нажать
    # её раньше, чем войдёт. Данные при этом собираются под контекстом базы, и
    # без сессии их просто нет — человек получил бы пустой файл или отказ,
    # ничего не сделав неправильно. Поэтому вход подставляется тем же путём, что
    # и по кнопке: демо и так открыто одним кликом, отдельного смысла держать
    # эту ссылку закрытой нет.
    if not request.user.is_authenticated:
        if login_with_password(request, DEFAULT_ROLE, demo_password()) is None:
            return render(request, "demo/unavailable.html", {"role": DEFAULT_ROLE},
                          status=503)

    with full_slice():
        rows = accountant_rows(RECONCILE_PERIOD)
    if not rows:
        # Текст для того, кто чинит: раньше здесь стояло «demo period is not
        # calculated» — и это было неправдой, период был посчитан и утверждён,
        # а пуст был срез скачивающего. Теперь срез полный, поэтому пустой
        # список означает именно ненаполненное демо.
        raise Http404("demo data is not seeded: no calculated rows for the period")

    response = HttpResponse(
        build_accountant_table(rows),
        content_type=(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        ),
    )
    name = f"accountant-table-{RECONCILE_PERIOD:%Y-%m}.xlsx"
    response["Content-Disposition"] = f'attachment; filename="{name}"'
    return response
