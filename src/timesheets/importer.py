"""
Загрузка таблицы партнёра в табель (T020) и отчёт о ней (T021).

Разбор файла живёт отдельно (`payroll.importers.plata_xlsx`): формат сербский,
у следующего партнёра будет свой, а вот всё, что здесь, — общее. Здесь три
вещи, и ни одна не про Excel:

1. **Сопоставление** строки файла с сотрудником и точкой. Ключ — `external_id`,
   а не ФИО: имена в системах не совпадают, и совпадение по ним однажды свело бы
   двух разных людей в одного. Точка берётся из **условий найма**, а не из имени
   листа: соответствие «лист → точка» приблизительное и живёт только в сиде.
2. **Идемпотентность.** Повторная загрузка того же файла не меняет ничего — ни
   строк, ни чисел, ни идентификаторов дней. Достигается не «перезаписью тем же
   значением», а отказом писать: см. `store.row_differs`.
3. **Отчёт.** Всё, что не разобрано (`findings` разбора), всё, что не попало в
   табель (`unmatched_rows` — человека нет в справочнике; `skipped_rows` —
   человек найден, но строку не записали), и всё, что записано, но подозрительно
   (`warnings`). Конституция, принцип 1: молча загруженный наполовину табель
   посчитается и даст правдоподобно неверную зарплату.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from uuid import UUID

from django.db import transaction
from django.utils.translation import gettext as _

from core.models import Employee, EmploymentTerm, Timesheet, Unit
from payroll import d
from payroll.importers import Finding, read_plata_file
from payrun.calc import month_end

from . import suspicion
from .closing import open_closures
from .store import RowInput, country_of, hour_types, store_row

__all__ = ["ImportResult", "Note", "UnmatchedRow", "import_partner_table"]

# Причина, которая уезжает в след ручной правки, когда файл её принёс. След
# обязателен (D025): без «кто и почему» правка через полгода неотличима от
# ошибки ввода — а объяснять её придётся именно тогда.
CORRECTION_REASON = "загрузка таблицы партнёра: колонка «KOREKCIJA DO MINIMALCA»"


@dataclass(frozen=True)
class UnmatchedRow:
    """Строка файла, которая в табель не попала, — и почему.

    Поводов два, и путать их нельзя (T122). **Не сопоставлена** — человека из
    файла в справочнике партнёра нет вовсе; тут и правда неизвестно, о ком речь.
    **Сопоставлена, но не загружена** — человек найден и назван по фамилии, а
    строку не записали по другой причине (часы точки закрыты, условий найма на
    месяц нет). Один счётчик на оба повода врал про класс события: отчёт писал
    «Не сопоставлено 16» и тут же перечислял этих шестнадцати поимённо.
    """

    sheet: str
    name: str
    why: str


@dataclass(frozen=True)
class Note:
    """Загружено, но выглядит подозрительно: подсказка плюс место в файле.

    Отличается от `Finding` тем, что данные **приняты**: это не «не разобрано»,
    а «посмотрите». Смешивать их в один список нельзя — человек перестанет
    различать «этого в табеле нет» и «это в табеле есть, но странное».
    """

    kind: str          # over_norm | no_hours | dismissed | negative
    text: str
    where: str = ""
    # Ключ сотрудника, а не его имя: имя показывается человеку, а связать
    # подсказку со строкой табеля можно только по ключу.
    external_id: str = ""


@dataclass
class ImportResult:
    """Что сделала загрузка. Показывается человеку целиком."""

    matched: int = 0        # строк файла, сопоставленных сотруднику
    created: int = 0        # строк табеля заведено заново
    updated: int = 0        # строк табеля изменено
    unchanged: int = 0      # строк табеля, которых загрузка не коснулась
    unmatched_rows: list[UnmatchedRow] = field(default_factory=list)
    # Сопоставленные, но не записанные: см. `UnmatchedRow`. Отдельным списком, а
    # не полем внутри строки, потому что на экране это два разных раздела с
    # разными заголовками и разными следующими шагами.
    skipped_rows: list[UnmatchedRow] = field(default_factory=list)
    warnings: list[Note] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def total_rows(self) -> int:
        return self.matched + len(self.skipped_rows) + len(self.unmatched_rows)

    @property
    def clean(self) -> bool:
        return not (
            self.unmatched_rows or self.skipped_rows or self.warnings or self.findings
        )


def _terms_at(tenant_id: UUID, period: date) -> dict[UUID, EmploymentTerm]:
    """Условия найма, действующие в этом месяце. Та же выборка, что у расчёта.

    Повторяет `payrun.calc.collect_cases` намеренно узко — одним запросом, без
    импорта его внутренностей: у расчёта своя ответственность, и завязывать на
    неё загрузку значило бы получить в импорте отказы расчёта.
    """
    end = month_end(period)
    terms: dict[UUID, EmploymentTerm] = {}
    for term in (
        EmploymentTerm.objects.filter(tenant_id=tenant_id, valid_from__lte=end)
        .exclude(valid_to__lte=period)
        .order_by("valid_from")
    ):
        terms[term.employee_id] = term
    return terms


def _check_data(row, employee, want: RowInput, period: date) -> list[Note]:
    """Подсказки о подозрительном (T021). Загрузку они не отменяют.

    Само правило живёт в `timesheets/suspicion.py` — одно на оба пути ввода
    (T118). Здесь только то, что есть у загрузки и чего нет у сетки: лист файла,
    на котором строка стояла, и ключ сотрудника, по которому подсказку можно
    связать со строкой табеля.
    """
    who = f"{employee.last_name} {employee.first_name}".strip() or employee.external_id
    where = f"{row.sheet}: {who}"
    return [
        Note(hint.kind, hint.text, where, employee.external_id)
        for hint in suspicion.hints(
            who=who,
            hours=want.hours,
            norm_hours=want.norm_hours,
            dismissed_at=employee.dismissed_at,
            period=period,
        )
    ]


def import_partner_table(file, *, tenant_id: UUID, period: date,
                         actor_id: UUID | None = None) -> ImportResult:
    """Загрузить таблицу партнёра в табель периода.

    Транзакция одна на всю загрузку: половина загруженного файла — состояние, о
    котором человеку нечего сказать, и разбираться в нём пришлось бы построчно.
    """
    parsed = read_plata_file(file)
    result = ImportResult(findings=list(parsed.findings))

    known = hour_types(tenant_id, period, country_of(tenant_id))
    employees = {
        person.external_id: person
        for person in Employee.objects.filter(tenant_id=tenant_id)
    }
    terms = _terms_at(tenant_id, period)
    closures = open_closures(tenant_id, period)
    unit_codes = dict(
        Unit.objects.filter(pk__in=list(closures)).values_list("id", "code")
    )
    rows = {
        row.employee_id: row
        for row in Timesheet.objects.filter(tenant_id=tenant_id, period=period)
    }

    with transaction.atomic():
        for source in parsed.rows:
            person = employees.get(source.employee.ext_id)
            if person is None:
                result.unmatched_rows.append(UnmatchedRow(
                    source.sheet, source.name,
                    _("такого сотрудника нет в справочнике партнёра"),
                ))
                continue

            term = terms.get(person.id)
            if term is None:
                # Без условий найма неизвестна точка, а точка — ключ строки
                # табеля. Догадка здесь означала бы часы, записанные не туда.
                result.skipped_rows.append(UnmatchedRow(
                    source.sheet, source.name,
                    _("нет условий найма на этот месяц — неизвестно, на какой точке"),
                ))
                continue

            # Часы закрытой точки не пишутся (T022). Проверка здесь, а не
            # только в базе: транзакция у загрузки одна на весь файл, и отказ
            # политики уносил бы вместе с закрытой точкой все остальные строки,
            # а человек прочитал бы неправду о причине — «файл не удалось
            # прочитать». Строка называется поимённо и не теряется.
            if term.unit_id is not None and term.unit_id in closures:
                result.skipped_rows.append(UnmatchedRow(
                    source.sheet, source.name,
                    _("часы точки %(unit)s за этот месяц закрыты — строка не загружена")
                    % {"unit": unit_codes.get(term.unit_id, "")},
                ))
                continue

            result.matched += 1
            want = RowInput(
                hours={
                    kind: d(value) for kind, value in source.timesheet.hours.items()
                    # Тип, которого нет в правилах страны, движок не посчитает.
                    # Записать его молча значило бы завести часы, не попавшие
                    # ни в одну сумму; поэтому — находка, а не запись.
                    if kind in known
                },
                insured_hours=d(source.timesheet.insured_hours),
                norm_hours=d(source.timesheet.norm_hours),
                deduction=d(source.timesheet.deduction),
                cash_payout=d(source.timesheet.cash_payout),
                manual_correction=source.timesheet.manual_correction,
            )
            for kind in source.timesheet.hours:
                if kind not in known:
                    result.findings.append(Finding(
                        "column", source.sheet,
                        _("типа часов «%(kind)s» нет в правилах страны — "
                          "эти часы не загружены") % {"kind": kind},
                    ))

            result.warnings.extend(_check_data(source, person, want, period))

            row = rows.get(person.id)
            if row is None:
                row = Timesheet(
                    tenant_id=tenant_id, employee_id=person.id, unit_id=term.unit_id,
                    period=period, hours={}, norm_hours=want.norm_hours,
                    insured_hours=Decimal("0"), source="import",
                )
                row.save()
                rows[person.id] = row
                result.created += 1
                store_row(timesheet=row, want=want, known=known,
                          actor_id=actor_id, reason=CORRECTION_REASON)
                continue

            if store_row(timesheet=row, want=want, known=known,
                         actor_id=actor_id, reason=CORRECTION_REASON):
                result.updated += 1
            else:
                result.unchanged += 1

    return result
