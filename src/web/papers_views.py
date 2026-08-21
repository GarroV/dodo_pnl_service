"""Экраны бумаг с точки: скинуть накладную или чек и разобрать их (T174).

Правила о деньгах живут не здесь, а в `web/papers.py` и `web/suppliers.py`:
здесь разбор ввода и показ. Три вещи, которые видно на экране и которые решены
нарочно.

**Форма управляющего короткая.** Вид бумаги, дата, точка, файл — и всё; поставщик
и сумма необязательны, статьи расхода и периода учёта нет вовсе. Его работа —
донести бумагу (D047), а не догадаться, чем это станет в P&L. Поле, которое он
заполнил бы наугад, дороже пустого: бухгалтеру пришлось бы перепроверять его
целиком.

**Точку отвергает база, а не форма** (D014). Список точек в форме — удобство;
чужую отвергает политика `unit_visibility` на `source_documents`, а бумагу без
точки — ограничение `source_documents_paper_names_its_unit`. Защита, написанная
в двух местах, однажды разойдётся молча.

**Разбор — это внесение счёта, а не отдельный механизм.** Поля и разбор ввода
берутся у карточки счёта (`suppliers_views.invoice_fields` / `parse_invoice`), и
запись идёт той же `suppliers.record_invoice` под ключом самой бумаги. Отсюда
бесплатно приезжают идемпотентность, защита закрытого месяца (D020) и инбокс для
строки без статьи. Своя запись рядом означала бы второй путь в `facts` — то
есть второй набор правил о деньгах.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

from django.contrib.auth.decorators import login_required
from django.http import Http404, HttpResponse, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext as _

from core.models import Unit

from . import cash, papers, suppliers, suppliers_views
from .counterparties_views import found as counterparties_found
from .dbrefusal import BadInput
from .directory_views import _number, _select, _text
from .format import EMPTY, money
from .i18n import month_title
from .principal import get_current_principal
from .suppliers_views import (
    _month_or_none,
    _no_membership,
    _required_date,
)

# Как называется вид бумаги на экране. Словарём, а не в шаблоне: то же название
# нужно и списку, и карточке, и подписи в инбоксе.
KIND_TITLES = {
    papers.INVOICE: lambda: _("Накладная"),
    papers.RECEIPT: lambda: _("Чек"),
}

# Что показывает поле файла. `image/*` первым: с телефона бумагу фотографируют,
# и камера должна открываться сразу.
ACCEPT = "image/*,application/pdf"


def kind_title(kind: str) -> str:
    """Вид бумаги на языке страницы. Незнакомый — как есть, а не пустотой."""
    known = KIND_TITLES.get(kind)
    return known() if known is not None else kind


# --- список -------------------------------------------------------------------


@login_required
def paper_list(request):
    """Бумаги с точек: что принесли и что из этого ещё не разобрано.

    Число «ждут разбора» стоит сверху и приезжает готовым: очередь бумаг — это
    работа, о которой бухгалтер должен узнать сам, а не обнаружить, пролистав
    список. Управляющий на том же экране видит только свои бумаги (срез делает
    база) и по нему же понимает, дошла ли бумага до разбора.
    """
    who = get_who(request)
    if who is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому бумаги вам некуда "
            "нести. Попросите администратора сети добавить вас."
        ))

    rows = paper_rows(who)
    waiting = [row for row in rows if row["waiting"]]
    stated = sum((row["amount"] for row in waiting), Decimal("0"))
    return render(request, "web/suppliers/papers.html", {
        "rows": rows,
        "counted": len(rows),
        "waiting": len(waiting),
        "stated_raw": f"{stated}", "stated_text": money(stated),
        "add_url": reverse("paper-new"),
        "back_url": reverse("invoices"),
        "inbox_url": reverse("inbox"),
    })


def get_who(request):
    """Вошедший, у которого есть партнёр. Иначе None — и отказ словами."""
    who = get_current_principal(request)
    return None if who is None or who.tenant_id is None else who


def paper_rows(who, *, only_waiting: bool = False) -> list[dict]:
    """Строки списка бумаг. Одна выборка: из неё же считается «ждут разбора».

    Второй запрос ради числа сверху разошёлся бы с таблицей молча — например,
    потому что политика сузила его не так, как эту выборку.
    """
    found = papers.papers(who, only_waiting=only_waiting)
    files = papers.files_of(found)
    handled = _handled(found)
    rows = []
    for document in found:
        lines = handled.get(str(document.id), [])
        stated = document.total_amount
        rows.append({
            "id": str(document.id),
            "kind": kind_title(document.kind),
            "date": document.doc_date.isoformat() if document.doc_date else "",
            "unit": document.unit.code if document.unit else EMPTY,
            "counterparty": (document.counterparty.title if document.counterparty
                             else EMPTY),
            "note": papers.note_of(document),
            # Сумма со слов управляющего. В P&L её нет и быть не может: там
            # только факты, а у неразобранной бумаги их ноль.
            "amount": Decimal(stated or 0),
            "amount_raw": f"{stated}" if stated is not None else "",
            "amount_text": money(stated) if stated is not None else EMPTY,
            "media_type": (files.get(str(document.id)) or {}).get("media_type", ""),
            "waiting": not lines,
            "line_period": month_title(lines[0].period) if lines else "",
            "url": reverse("paper", args=[document.id]),
            "file_url": reverse("paper-file", args=[document.id]),
        })
    return rows


def _handled(found) -> dict[str, list]:
    """Строки, которыми бумаги разобрали, — одним запросом на весь список.

    По одному запросу на строку список из двадцати бумаг делал бы двадцать
    обращений к базе; здесь это не про скорость, а про то, что такой список
    начинают «оптимизировать» отказом от проверки — и он перестаёт показывать,
    что разобрано.
    """
    from core.models import Fact

    ids = [document.id for document in found]
    grouped: dict[str, list] = {}
    for row in (
        Fact.objects.filter(document_id__in=ids, superseded_at__isnull=True)
        .exclude(allocation="allocated")
        .order_by("created_at")
    ):
        grouped.setdefault(str(row.document_id), []).append(row)
    return grouped


# --- приём бумаги -------------------------------------------------------------


@login_required
def paper_new(request):
    """Скинуть накладную или чек: короткая форма и файл."""
    who = get_who(request)
    if who is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому бумаги вам некуда "
            "нести. Попросите администратора сети добавить вас."
        ))

    error, status = "", 200
    entered = request.POST if request.method == "POST" else {}
    if request.method == "POST":
        try:
            handed = papers.hand_over(
                who,
                entry_key=cash.parse_entry_key(request.POST.get("entry_key", "")),
                **parse_paper(request, who),
            )
            return redirect(reverse("paper", args=[handed.document_id]) + "?handed=1")
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except cash.UnitRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except cash.CashRefused as refusal:
            error, status = refusal.message, refusal.http_status

    return render(request, "web/suppliers/paper_new.html", {
        "error": error,
        "entry_key": cash.new_entry_key(),
        "fields": paper_fields(who, entered),
        "back_url": reverse("papers"),
    }, status=status)


def parse_paper(request, who) -> dict:
    """Разобрать форму бумаги. Ничего, кроме того, что человек видит на экране."""
    on = _required_date(request, "date", _("Дата на бумаге"))
    upload = request.FILES.get("scan")
    amount = _number(request, "amount", _("Сумма с бумаги"), required=False)
    if amount == 0:
        # Ноль в поле суммы — это не «сумма неизвестна», а опечатка: неизвестную
        # сумму человек оставляет пустой, и разница видна в инбоксе прочерком.
        raise BadInput(
            _("«%(label)s»: ноль не сумма. Оставьте поле пустым, если сумма "
              "неизвестна.") % {"label": _("Сумма с бумаги")}
        )
    return {
        "kind": _kind(request),
        "on": on,
        "unit_id": _paper_unit(request, who),
        "counterparty": _counterparty_or_none(request, on),
        "amount": amount,
        "note": _text(request, "note", _("Что это"), required=False),
        "file_name": (upload.name if upload is not None else "")[:200],
        "data": upload.read() if upload is not None else b"",
    }


def _kind(request) -> str:
    raw = (request.POST.get("kind") or "").strip()
    if raw not in papers.PAPER_KINDS:
        raise BadInput(
            _("Поле «%(label)s» обязательно.") % {"label": _("Что за бумага")}
        )
    return raw


def _paper_unit(request, who):
    """Точка бумаги. «Вся сеть» здесь не бывает: бумагу приносят с точки.

    Пусто и человек ограничен одной точкой — подставляем её: выбирать ему не из
    чего. Всё остальное уходит в базу как есть — чужую точку отвергнет политика,
    а не эта функция (D014).
    """
    from uuid import UUID

    raw = (request.POST.get("unit") or "").strip()
    if raw == cash.NETWORK_UNIT:
        raise BadInput(_(
            "Бумагу приносят с точки, а не со всей сети: выберите точку, "
            "с которой она пришла."
        ))
    if not raw and len(who.unit_ids) == 1:
        return who.unit_ids[0]
    if not raw:
        raise BadInput(_("Поле «%(label)s» обязательно.") % {"label": _("Точка")})
    try:
        return UUID(raw)
    except ValueError:
        # Тот же отказ, что у чужой точки: по ответу нельзя понять, существует
        # ли она (D023).
        raise cash.UnitRefused() from None


def _counterparty_or_none(request, on: date):
    """Поставщик — необязательно: на чеке из магазина его может не быть вовсе."""
    if not (request.POST.get("counterparty") or "").strip():
        return None
    return suppliers_views._counterparty(request, on)


def paper_fields(who, entered) -> list[dict]:
    """Поля формы бумаги. Шесть, и больше не нужно."""
    today = date.today()
    return [
        _select(
            "kind", _("Что за бумага"),
            [(code, kind_title(code)) for code in papers.PAPER_KINDS],
            entered.get("kind") or papers.INVOICE, required=True,
            help=_("Накладная — бумага поставщика на товар. Чек — то, что "
                   "заплатили на месте."),
        ),
        {"kind": "date", "name": "date", "label": _("Дата на бумаге"),
         "required": True, "value": entered.get("date") or today.isoformat(),
         "help": _("Дата, которая напечатана на самой бумаге.")},
        _paper_unit_field(who, entered),
        {"kind": "file", "name": "scan", "label": _("Фотография или PDF"),
         "required": True, "accept": ACCEPT,
         "help": _("Снимок с телефона годится. Главное — чтобы читались "
                   "поставщик, дата и сумма.")},
        _select(
            "counterparty", _("Поставщик"),
            counterparties_found(open_on=today).values_list("id", "title"),
            entered.get("counterparty"), required=False,
            empty_label=_("Не знаю или нет в списке"),
            help=_("Можно не выбирать: поставщика назначит бухгалтер при разборе."),
        ),
        {"kind": "number", "name": "amount", "label": _("Сумма с бумаги"),
         "value": entered.get("amount") or "",
         "help": _("Необязательно. Это сумма со слов, а не расход: в P&L она "
                   "попадёт только после разбора.")},
        {"kind": "text", "name": "note", "label": _("Что это"),
         "value": entered.get("note") or "",
         "help": _("Одной строкой: от кого бумага и за что. Поможет бухгалтеру "
                   "найти поставщика.")},
    ]


def _paper_unit_field(who, entered) -> dict:
    """Точка бумаги: только настоящие точки, без варианта «вся сеть»."""
    units = Unit.objects.order_by("code")
    if who.unit_ids:
        units = units.filter(pk__in=who.unit_ids)
    rows = list(units.values_list("id", "code"))
    chosen = entered.get("unit") or (who.unit_ids[0] if len(who.unit_ids) == 1 else "")
    return _select(
        "unit", _("Точка"), rows, chosen, required=True,
        help=_("С какой точки бумага. Не куда ляжет расход — это решает "
               "бухгалтер при разборе."),
    )


# --- карточка и разбор --------------------------------------------------------


@login_required
def paper(request, document_id):
    """Карточка бумаги: сама фотография, слова управляющего и разбор."""
    who = get_who(request)
    if who is None:
        return _no_membership(request, _(
            "Вас ещё не завели ни к одному партнёру, поэтому бумаг у вас нет."
        ))

    document = papers.paper_or_none(document_id)
    if document is None:
        # Чужая бумага и выдуманный номер отвечают одинаково (D023).
        raise Http404("бумага не найдена")

    lines = papers.lines_of(document)
    error, status = "", 200
    entered = request.POST if request.method == "POST" else _entered(document)
    if request.method == "POST":
        try:
            return redirect(_sort_out(request, who, document))
        except BadInput as bad:
            error, status = bad.message, bad.http_status
        except cash.UnitRefused as refusal:
            error, status = refusal.message, refusal.http_status
        except cash.CashRefused as refusal:
            error, status = refusal.message, refusal.http_status

    kept = papers.file_of(document)
    return render(request, "web/suppliers/paper.html", {
        "error": error,
        "notice": _card_notice(request),
        "heading": _("%(kind)s с точки %(unit)s") % {
            "kind": kind_title(document.kind),
            "unit": document.unit.code if document.unit else EMPTY,
        },
        "paper": {
            "kind": kind_title(document.kind),
            "date": document.doc_date.isoformat() if document.doc_date else "",
            "unit": document.unit.code if document.unit else EMPTY,
            "counterparty": (document.counterparty.title if document.counterparty
                             else EMPTY),
            "note": papers.note_of(document),
            "stated_text": (money(document.total_amount)
                            if document.total_amount is not None else EMPTY),
            "handed_over": (document.handed_over_at.date().isoformat()
                            if document.handed_over_at else ""),
        },
        "shown_inline": bool(kept and kept.media_type in papers.SHOWN_INLINE),
        "file_url": reverse("paper-file", args=[document.id]),
        "file_size": _size(kept),
        "waiting": not lines,
        "lines": [_line(row) for row in lines],
        "invoice_url": reverse("invoice", args=[document.id]) if lines else "",
        "fields": suppliers_views.invoice_fields(who, entered),
        "closed_note": _closed_note(who, document),
        "back_url": reverse("papers"),
    }, status=status)


def _entered(document) -> dict:
    """Что подставить в поля разбора: всё, что управляющий уже сказал.

    Заполнять заново то, что уже написано на карточке, — работа, которую продукт
    обязан сделать за человека. Чего управляющий не знал (статья, период учёта),
    остаётся пустым: подставленное наугад значение бухгалтер не отличит от
    выбранного.
    """
    on = document.doc_date
    return {
        "date": on.isoformat() if on else "",
        "period": f"{on:%Y-%m}" if on else "",
        "counterparty": str(document.counterparty_id or ""),
        "unit": str(document.unit_id or ""),
        "amount": f"{document.total_amount}" if document.total_amount is not None else "",
        "note": papers.note_of(document),
    }


def _sort_out(request, who, document) -> str:
    """Разобрать бумагу: записать строку счёта на ТОТ ЖЕ документ.

    Ключ записи — ключ самой бумаги, поэтому `upsert_document` обновляет её
    шапку, а не заводит вторую. Иначе бумага осталась бы стоять в инбоксе с
    фотографией, но без строк, а деньги уехали бы в документ-двойник.
    """
    entered = suppliers_views.parse_invoice(request, who)
    recorded = suppliers.record_invoice(
        who, entry_key=papers.document_key(document), **entered,
    )
    landed = (reverse("paper", args=[document.id])
              + f"?sorted={recorded.landing.period:%Y-%m}")
    if recorded.landing.moved_from is not None:
        landed += f"&moved={recorded.landing.moved_from:%Y-%m}"
    if entered["unit_id"] is None:
        # Счёт на всю сеть разносится сразу, как и с карточки счёта: узнать
        # через месяц, что сумма висела нераспределённой, — худший из ответов.
        cash.spread_now(recorded.fact_id)
    return landed


def _card_notice(request) -> str:
    """Что случилось — словами, с месяцем, в который легла строка."""
    if request.GET.get("handed"):
        return _(
            "Бумага принята и ждёт разбора бухгалтером. В P&L её ещё нет: "
            "сумма появится там, когда бухгалтер назначит статью и период."
        )
    said = []
    month = _month_or_none(request.GET.get("sorted"))
    if month is not None:
        said.append(_("Бумага разобрана: строка учтена в периоде %(month)s.")
                    % {"month": month_title(month)})
    moved = _month_or_none(request.GET.get("moved"))
    if moved is not None:
        said.append(_(
            "Месяц %(month)s закрыт, поэтому строка легла в открытый период — "
            "с исходной датой и не сдвинув закрытый месяц."
        ) % {"month": month_title(moved)})
    return " ".join(said)


def _closed_note(who, document) -> str:
    """Что случится с разбором бумаги за закрытый месяц — сказано ДО кнопки."""
    on = document.doc_date
    if on is None or not cash.month_is_closed(who.tenant_id, on.replace(day=1)):
        return ""
    return _(
        "Месяц %(month)s закрыт. Разбор его не тронет: строка ляжет в открытый "
        "период с исходной датой документа."
    ) % {"month": month_title(on.replace(day=1))}


def _line(row) -> dict:
    """Строка, которой бумагу разобрали."""
    return {
        "title": row.title,
        "period": month_title(row.period),
        "amount_text": money(row.amount),
        "amount_raw": f"{row.amount}",
        "unit": row.unit.code if row.unit else _("Вся сеть"),
    }


def _size(kept) -> str:
    """Размер файла человеческими словами. Нет файла — пусто, а не «0 КБ»."""
    if kept is None:
        return ""
    return _("%(size)s КБ") % {"size": max(1, round(kept.byte_size / 1024))}


# --- сам файл -----------------------------------------------------------------


@login_required
def paper_file(request, document_id):
    """Отдать файл бумаги. Кому его видно, решает база.

    Ни одной проверки прав здесь нет и быть не может: файл ищется под
    политиками смотрящего (`follows_its_document` зовёт сам документ), и чужой
    просто не находится. Своя проверка рядом была бы вторым ответом на тот же
    вопрос — тем, который однажды разойдётся с первым (D014).

    Картинка показывается в странице, остальное отдаётся файлом на сохранение:
    HEIC браузеры не рисуют, а показывать PDF внутри страницы значит запускать
    его просмотрщик на чужом файле. `nosniff` — чтобы браузер не решил сам, что
    это на самом деле.
    """
    if request.method not in ("GET", "HEAD"):
        return HttpResponseNotAllowed(["GET", "HEAD"])

    who = get_who(request)
    if who is None:
        raise Http404("бумага не найдена")

    document = papers.paper_or_none(document_id)
    kept = papers.file_of(document) if document is not None else None
    if kept is None:
        raise Http404("файл не найден")

    answer = HttpResponse(bytes(kept.content), content_type=kept.media_type)
    shown = "inline" if kept.media_type in papers.SHOWN_INLINE else "attachment"
    answer["Content-Disposition"] = f'{shown}; filename="{_file_name(document, kept)}"'
    answer["X-Content-Type-Options"] = "nosniff"
    # Файл партнёра не должен попасть ни в один общий кэш: адрес угадать нельзя,
    # но кэш посредника про политики базы ничего не знает.
    answer["Cache-Control"] = "private, no-store"
    return answer


def _file_name(document, kept) -> str:
    """Имя файла на сохранение: без имени браузер предложит номер документа.

    Собирается из вида бумаги и даты, а не берётся у загруженного файла:
    `IMG_2481.jpg` из телефона не говорит ни о чём, а в кавычках заголовка
    чужое имя ещё и надо было бы вычищать от кавычек.
    """
    stamp = document.doc_date.isoformat() if document.doc_date else "paper"
    suffix = {
        "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp",
        "image/heic": "heic", "application/pdf": "pdf",
    }.get(kept.media_type, "bin")
    return f"{document.kind}-{stamp}.{suffix}"


__all__ = [
    "kind_title",
    "paper",
    "paper_fields",
    "paper_file",
    "paper_list",
    "paper_new",
    "paper_rows",
    "parse_paper",
]
