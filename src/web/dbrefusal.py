"""Отказ базы по ограничению — словами формы, а не пятисоткой (T136).

**Зачем это отдельным местом.** Разграничение доступа и целостность данных у нас
намеренно живут в базе (D014), и «пусть база отвергнет» — обычный способ здесь
писать. Значит любая форма, принимающая от человека код или ссылку, однажды
получит отказ базы вместо ответа. Пока такой отказ обрабатывался поштучно, он и
чинился поштучно: у расхода объяснён (issue #98, `web/cash.py`), у статьи
расходов — белая страница 500 с текстом `duplicate key value violates unique
constraint "expense_items_tenant_code_uniq"` (issue #109). У каждой таблицы
справочника свои уникальные ключи и свои ссылки, поэтому чинится это классом:
один помощник, через который идёт запись каждой формы.

**Три вещи, которые делает `saving()` и без которых 500 возвращается.**

1. *Точка сохранения.* Весь запрос идёт одной транзакцией
   (`DbContextMiddleware`, `ATOMIC_REQUESTS`), и отказ без точки сохранения
   обрывает её целиком — дальше нельзя ни собрать страницу, ни даже спросить
   базу, кем вошли.
2. *`set constraints all immediate`.* Внешние ключи Django объявляет
   `deferrable initially deferred`, то есть проверяет их **на коммите** — уже за
   пределами любого `try` внутри представления. Без этой строки отказ по ссылке
   приходит туда, где его некому объяснить (issue #98).
3. *Перевод в слова.* Отказ базы называет ограничение по имени
   (`units_tenant_code_uniq`), а человеку нужно поле его формы.

**Переводится не всё, и это главное решение здесь.** Ограничение, нарушенное
**вводом человека**, становится отказом формы; ограничение, нарушенное **дефектом
кода**, обязано падать громко. Граница проведена по классу отказа:

| код | что это | как отвечаем |
|---|---|---|
| `23505` | такой ключ уже есть | отказ формы: поле занято |
| `23503` | ссылка в никуда | отказ формы: строка не найдена |
| `23P01` | пересечение периодов действия | отказ формы: поправьте даты |
| всё остальное | пустое поле, чужой тип, сторож-триггер, дефект | падает как падало |

Ровно эти три класса и есть «человек набрал не то»: они возможны при исправном
коде и правильном вводе кого-то другого. Пустое поле, которого форма не
спрашивает (`23502`), или проверка `23514` — это дефект либо правило, которое
форма обязана объяснить **до** записи своими словами; вежливое «поправьте ввод»
на их месте пряталo бы поломку в журнал.

**Поле называется по колонке, а не по модели.** `verbose_name` у полей не
заполняется (модели описаны кодом, а не подписями), поэтому имя поля человеку
даёт словарь ниже — один на продукт. Колонки повторяются между таблицами
(`code`, `title`, `period`), так что словарь маленький и не растёт вместе со
схемой. Колонки ограничения спрашиваются **у самой базы** (каталог), а не
выводятся из имени: тогда работают и ограничения, написанные руками в
миграциях, — а таких у нас большинство.
"""
from __future__ import annotations

from contextlib import contextmanager

from django.db import DatabaseError, connection, transaction
from django.utils.translation import gettext as _
from django.utils.translation import gettext_noop

__all__ = ["BadInput", "ConstraintRefused", "saving"]


class BadInput(Exception):
    """Введено не то. Отдельно от `DirectoryRefused`: там данные, здесь форма.

    Живёт здесь, а не в `directory_views`, потому что отказ базы ниже — его
    частный случай, а модуль экранов импортировать отсюда нельзя (он сам зовёт
    `saving`). Прежний адрес остался рабочим: `directory_views` берёт класс
    отсюда и отдаёт дальше, так что все `from .directory_views import BadInput`
    продолжают получать тот же самый класс, а не второй такой же.
    """

    http_status = 400

    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class ConstraintRefused(BadInput):
    """Отказ базы, переведённый в слова формы.

    Наследник `BadInput` не ради удобства: его уже ловят все шесть форм, вызов
    по HTTP (`web/api.py`) и разбор ввода расхода. Отдельный класс без родства
    остался бы неперехваченным ровно там, где перехватывать некому, — и снова
    стал бы пятисоткой. А своё имя нужно, чтобы форма, отвечающая на обычный
    `BadInput` кодом 200, отдала на отказ базы честные 400.
    """


# Отказы базы, возможные при исправном коде и вводе человека. Список закрытый:
# всё, чего в нём нет, падает громко (см. разбор в шапке модуля).
UNIQUE = "23505"
FOREIGN_KEY = "23503"
EXCLUSION = "23P01"

# Как называются колонки на языке человека. Ключ — колонка, без хвоста `_id`:
# ссылка `legal_entity_id` в форме называется «Юрлицо», а не «Юрлицо (id)».
# Переводы объявлены `gettext_noop` и переводятся в момент показа: тело модуля
# выполняется раньше, чем известен язык страницы.
COLUMN_TITLES = {
    "code": gettext_noop("Код"),
    "title": gettext_noop("Название"),
    "external_id": gettext_noop("Сквозной ключ"),
    "period": gettext_noop("Месяц"),
    "valid_from": gettext_noop("Действует с"),
    "rate_date": gettext_noop("Дата курса"),
    "country_code": gettext_noop("Страна"),
    "legal_entity": gettext_noop("Юрлицо"),
    "unit": gettext_noop("Точка"),
    "employee": gettext_noop("Сотрудник"),
    "group": gettext_noop("Группа"),
    "pnl_item": gettext_noop("Строка P&L"),
    "expense_item": gettext_noop("Статья расхода"),
    "counterparty": gettext_noop("Контрагент"),
    "ledger": gettext_noop("Регистр учёта"),
    "role": gettext_noop("Роль"),
    "user": gettext_noop("Пользователь"),
    "payrun": gettext_noop("Расчёт"),
    "payslip": gettext_noop("Строка ведомости"),
}

# Колонки, которых человек не вводит: они есть почти в каждом уникальном ключе и
# в отказе только мешают. Партнёр в форме не выбирается — он берётся из того,
# кем вошли; страна — из партнёра (единственный её уникальный ключ сегодня у
# производственного календаря, и поля «Страна» на той форме нет вовсе, T155).
# Оставленная в отказе, она предлагала «задать другое значение» полю, которого
# человек не видит.
NOT_TYPED_BY_A_HUMAN = ("tenant", "country_code")


@contextmanager
def saving():
    """Запись формы: отказ базы по ограничению становится отказом формы.

    Пользоваться так, чтобы внутрь попала **вся** запись одной кнопки, а не
    один `save()`: отвергнутая форма не должна оставлять за собой половину
    сделанного (заведённую строку без правила, месяц без содержимого).
    """
    try:
        with transaction.atomic():
            yield
            with connection.cursor() as cursor:
                # Отложенные ключи — здесь и сейчас, пока отказ ещё внутри
                # точки сохранения и внутри `except` ниже.
                cursor.execute("set constraints all immediate")
    except DatabaseError as refusal:
        words = _words_for(refusal)
        if words is None:
            raise
        raise ConstraintRefused(words) from refusal


def _words_for(refusal: DatabaseError) -> str | None:
    """Слова отказа или `None`, если этот отказ переводить нельзя (и не надо)."""
    cause = getattr(refusal, "__cause__", None)
    state = getattr(cause, "sqlstate", "") or ""
    if state not in (UNIQUE, FOREIGN_KEY, EXCLUSION):
        return None

    diagnosis = getattr(cause, "diag", None)
    table = getattr(diagnosis, "table_name", "") or ""
    constraint = getattr(diagnosis, "constraint_name", "") or ""
    columns = _columns_of(table, constraint)
    if not columns:
        # Ограничение, о котором нечего сказать правды (база не назвала ни
        # таблицы, ни колонок), в вежливое сообщение не превращается: пусть
        # падает громко и чинится, а не притворяется ошибкой человека.
        return None

    fields = ", ".join(_quoted(title) for title in columns)
    if state == UNIQUE:
        return _(
            "Такая запись уже есть: %(fields)s не повторяется. "
            "Задайте другое значение."
        ) % {"fields": fields}
    if state == FOREIGN_KEY:
        # Теми же словами, что у чужой строки: по ответу нельзя понять,
        # существует ли она вообще (D023).
        return _("%(fields)s: строка не найдена. Выберите значение из списка.") % {
            "fields": fields
        }
    return _(
        "Этот период уже занят другой версией (%(fields)s). Поправьте даты."
    ) % {"fields": fields}


def _columns_of(table: str, constraint: str) -> list[str]:
    """Колонки ограничения — названиями для человека. Спрашиваются у базы.

    Каталог, а не разбор имени ограничения: половина ограничений написана руками
    в миграциях (`0130`, `0230` и соседние), и их имена не подчиняются правилам
    Django. Спрашивать после отказа можно: точка сохранения уже откачена, и
    соединение снова рабочее.
    """
    if not table or not constraint:
        return []
    with connection.cursor() as cursor:
        found = connection.introspection.get_constraints(cursor, table)
    columns = (found.get(constraint) or {}).get("columns") or []

    typed = [column for column in columns if _root(column) not in NOT_TYPED_BY_A_HUMAN]
    return [_title(column) for column in (typed or columns)]


def _quoted(title: str) -> str:
    """Название поля в кавычках — тех, что приняты в языке страницы.

    Кавычки внутри переводимой строки, а не приписаны в коде: у остальных
    отказов продукта они переводятся вместе с фразой («Поле «%(label)s»
    обязательно.» → `The "%(label)s" field is required.`), и приписанные снаружи
    ёлочки торчали бы на английской странице русской типографикой. Найдено
    смоуком: `already exists: «Code» cannot repeat`.
    """
    return _("«%(field)s»") % {"field": title}


def _root(column: str) -> str:
    return column[:-3] if column.endswith("_id") else column


def _title(column: str) -> str:
    known = COLUMN_TITLES.get(_root(column))
    return _(known) if known else column
