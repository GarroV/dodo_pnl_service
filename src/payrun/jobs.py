"""Расчёт периода фоновой задачей: постановка, исполнение, ход работы (T024).

Задача в очереди — это тот же расчёт, что и синхронный, с тремя отличиями, и
каждое из них здесь объяснено, потому что каждое легко сделать неправильно.

**1. Задача сама представляется базе.** У неё нет HTTP-запроса, значит,
`DbContextMiddleware` не сработает и контекст пользователя выставить некому. Без
контекста соединение остаётся ролью подключения — а в разработке это владелец
базы, которого политики не ограничивают. Поэтому первое, что делает задача, —
`db_context(user_id)`: та же роль `app_user` и тот же `app.user_id`, что были бы
в запросе. Из этого следует, что «запустил расчёт» с точки зрения базы — тот
человек, который нажал кнопку, а не безличная система.

**2. Права и регистры перечитываются в момент исполнения.** Между постановкой в
очередь и работой задачи роль могли поменять. Полезная нагрузка очереди — это
заявление («меня зовут вот так»), а проверка — политики базы и `memberships`.
Ни одно значение из очереди не используется как право.

**3. Ход работы пишется по другому соединению.** Расчёт идёт одной транзакцией, а
незакоммиченная транзакция снаружи не видна: прогресс, записанный внутри неё,
появился бы на экране ровно тогда, когда он уже не нужен. Автономных транзакций
в Postgres нет, поэтому отметки идут через алиас `progress` своими короткими
транзакциями. **Транзакция расчёта не трогает `payrun_jobs`** — иначе она
заблокировала бы строку задания, канал прогресса встал бы на этой блокировке, и
снаружи это выглядело бы как зависший расчёт.

Идемпотентность держит база, а не дисциплина здесь: частичный уникальный индекс
`payrun_jobs_active_uniq` (незавершённое задание на период ровно одно) и перевод
`queued → running` условным `update` — задача, не сумевшая перевести строку, не
делает ничего.
"""
from __future__ import annotations

from datetime import timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from core.models import PayrunJob

from .calc import calculate_period
from .errors import PayrunRefused

__all__ = [
    "CalculationBusy",
    "JobUnavailable",
    "Reporter",
    "active_job",
    "last_job",
    "run_calculation",
    "run_job",
    "start",
    "state_of",
]

QUEUED = "queued"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# Полный путь к точке входа: очередь хранит имя функции строкой и импортирует её
# в рабочем процессе сама.
TASK = "payrun.jobs.run_job"

BUSY_REFUSAL = (
    "Расчёт этого периода уже идёт. Дождитесь, пока он закончится: "
    "второй запуск не ускорит его, а ведомость всё равно будет одна."
)

STUCK_HINT = (
    "Задача принята, но её до сих пор не взяли в работу — похоже, рабочий "
    "процесс очереди не запущен. Расчёт можно выполнить прямо сейчас, не "
    "дожидаясь очереди."
)

BROKEN = (
    "Расчёт оборвался из-за внутренней ошибки. Данные периода не изменились; "
    "покажите этот период тому, кто ведёт сервис."
)


class CalculationBusy(PayrunRefused):
    """Расчёт этого периода уже запущен. Второй запускать нечего."""

    http_status = 409


class JobUnavailable(RuntimeError):
    """Задания не видно под контекстом того, от чьего имени пришла задача.

    Не отказ пользователю, а поломка исполнения: писать в задание нечего, потому
    что его не видно. Такое бывает, если задание успели удалить или если в
    очередь попал чужой идентификатор — второе политики базы как раз и ловят.
    """


# --- ход работы --------------------------------------------------------------


class Reporter:
    """Кто рассказывает о ходе работы и по какому соединению.

    `background=True` — отметки идут по алиасу `progress`, каждая своей короткой
    транзакцией. Иначе они попали бы внутрь транзакции расчёта и стали бы видны
    только после его конца.

    `background=False` — синхронный расчёт: человек ждёт ответа страницы, и
    рассказывать некому. Отметки пишутся тем же соединением, что и всё
    остальное, — их увидит только тот, кто откроет страницу потом.
    """

    def __init__(self, job_id, user_id, *, background: bool):
        self.job_id = job_id
        self.user_id = user_id
        self.background = background

    def say(self, stage: str, done: int = 0, total: int = 0) -> None:
        self.write(stage=stage, done=done, total=total)

    def write(self, _where: dict | None = None, **fields) -> int:
        where = {"pk": self.job_id, **(_where or {})}
        if not self.background:
            return PayrunJob.objects.filter(**where).update(**fields)

        from web.dbcontext import db_context

        with db_context(self.user_id, using="progress"):
            return PayrunJob.objects.using("progress").filter(**where).update(**fields)

    def claim(self) -> bool:
        """Перевести задание `queued → running`. Победитель ровно один.

        Условие по прежнему статусу — не украшение: ту же задачу очередь может
        выдать второй раз (решив, что рабочий процесс потерялся), и то же
        задание может подхватить синхронный расчёт со страницы. Ноль изменённых
        строк — «работа уже чья-то», и делать нечего. Проверка «а какой там
        статус» отдельным запросом была бы гонкой: между ней и записью успевает
        вклиниться второй исполнитель.
        """
        return bool(
            self.write(
                {"status": QUEUED},
                status=RUNNING, started_at=timezone.now(), error="", details=[],
            )
        )

    def finish(self, *, error: str = "", details=()) -> None:
        self.write(
            status=FAILED if error else DONE,
            stage="" if error else "Готово",
            error=error,
            details=list(details),
            finished_at=timezone.now(),
        )


# --- чтение состояния --------------------------------------------------------


def active_job(tenant_id, period) -> PayrunJob | None:
    """Незавершённое задание периода. Их не бывает двух — следит база."""
    return (
        PayrunJob.objects.filter(
            tenant_id=tenant_id, period=period, status__in=[QUEUED, RUNNING]
        )
        .order_by("-created_at")
        .first()
    )


def last_job(tenant_id, period) -> PayrunJob | None:
    """Последнее задание периода — незавершённое или уже отвечавшее."""
    return (
        PayrunJob.objects.filter(tenant_id=tenant_id, period=period)
        .order_by("-created_at")
        .first()
    )


def is_stuck(job: PayrunJob | None) -> bool:
    """Задачу не взяли в работу дольше положенного.

    Наблюдаемый факт, а не догадка о живости очереди. Спрашивать у самой очереди
    было бы вторым источником истины: у ORM-брокера `django-q2` статистика
    кластера лежит в кэше Django, а он по умолчанию живёт в памяти процесса —
    веб не видит статистики рабочего процесса вовсе.
    """
    if job is None or job.status != QUEUED or not job.background:
        return False
    stale = timedelta(seconds=settings.PAYRUN_QUEUE_STALE_SECONDS)
    return timezone.now() - job.created_at > stale


def state_of(job: PayrunJob | None) -> dict:
    """Состояние расчёта, пригодное и для страницы, и для ответа JSON.

    Одно место на оба потребителя: расходящиеся ответы страницы и опроса — это
    полоса прогресса, которая говорит не то, что написано рядом с ней.
    """
    if job is None:
        return {
            "status": None, "stage": "", "done": 0, "total": 0, "percent": 0,
            "background": False, "stuck": False, "finished": True,
            "error": "", "details": [], "hint": "",
        }
    stuck = is_stuck(job)
    return {
        "status": job.status,
        "stage": job.stage,
        "done": job.done,
        "total": job.total,
        # Доля считается здесь, а не в шаблоне и не в скрипте: два места делили
        # бы одно правило, и полоса на странице разошлась бы с полосой,
        # перерисованной опросом.
        "percent": int(job.done * 100 / job.total) if job.total else 0,
        "background": job.background,
        "stuck": stuck,
        "finished": job.status in (DONE, FAILED),
        "error": job.error,
        "details": list(job.details or []),
        # Слова про застрявшую задачу — одни и те же на странице и в опросе.
        "hint": STUCK_HINT if stuck else "",
    }


# --- постановка --------------------------------------------------------------


def start(*, tenant_id, period, actor_id, background: bool):
    """Запустить расчёт: в очередь или прямо здесь.

    Возвращает задание. В фоновом режиме оно `queued` и работа ещё не начата; в
    синхронном — уже завершено, и его `error` (если он есть) поднят исключением,
    чтобы страница показала отказ так же, как раньше.

    Второй незавершённой задачи не заводится: за этим следит база, а не проверка
    перед вставкой. Проверка «нет ли уже» до вставки была бы гонкой — между ней
    и вставкой успевает вклиниться второе нажатие.
    """
    if not background:
        return _inline(tenant_id=tenant_id, period=period, actor_id=actor_id)

    job = _create(tenant_id=tenant_id, period=period, actor_id=actor_id, background=True)
    from django_q.tasks import async_task

    # Задача ставится в той же транзакции, что и строка задания: очередь живёт в
    # той же базе, поэтому рабочий процесс увидит задачу ровно тогда, когда
    # увидит задание, — и ни мгновением раньше.
    job.task_id = async_task(TASK, str(job.pk), str(actor_id))
    PayrunJob.objects.filter(pk=job.pk).update(task_id=job.task_id)
    return job


def _create(*, tenant_id, period, actor_id, background: bool) -> PayrunJob:
    try:
        # Точка сохранения: `IntegrityError` ломает транзакцию целиком, а запрос
        # обёрнут в неё снаружи (ATOMIC_REQUESTS) — без вложенной транзакции
        # страница отказа уже ничего не смогла бы прочитать.
        with transaction.atomic():
            return PayrunJob.objects.create(
                tenant_id=tenant_id, period=period, requested_by=actor_id,
                background=background, status=QUEUED,
                stage="В очереди" if background else "Считаем",
            )
    except IntegrityError as clash:
        raise CalculationBusy(BUSY_REFUSAL) from clash


def _inline(*, tenant_id, period, actor_id) -> PayrunJob:
    """Посчитать прямо в запросе — и записать это в задание тем же путём.

    Зависшее задание не бросается, а **перехватывается**: иначе рядом с ним
    нельзя было бы завести новое (уникальный индекс), и человек остался бы без
    расчёта вовсе. Перехват — тот же условный перевод `queued → running`, что у
    фоновой задачи, поэтому одну работу не сделают дважды.
    """
    job = active_job(tenant_id, period)
    if job is None:
        job = _create(
            tenant_id=tenant_id, period=period, actor_id=actor_id, background=False
        )
    reporter = Reporter(job.pk, actor_id, background=False)
    if not reporter.claim():
        raise CalculationBusy(BUSY_REFUSAL)
    # Задание перехвачено у очереди: теперь оно считается здесь, и это должно
    # быть видно человеку, а не подменено молча.
    reporter.write(background=False, requested_by=actor_id)

    try:
        _calculate(job, actor_id, reporter)
    except PayrunRefused as refusal:
        reporter.finish(error=refusal.message, details=refusal.details or refusal.ledgers)
        raise
    reporter.finish()
    return PayrunJob.objects.filter(pk=job.pk).first()


# --- исполнение --------------------------------------------------------------


def run_job(job_id: str, user_id: str) -> None:
    """Точка входа очереди. Всё, что здесь есть, — контекст, захват и отчёт.

    Исключения наружу не выпускаются только для отказов, понятных человеку: они
    записаны в задание и показаны на странице. Всё остальное поднимается дальше,
    чтобы очередь пометила задачу упавшей, а не «выполненной».
    """
    from web.dbcontext import db_context

    reporter = Reporter(job_id, user_id, background=True)
    with db_context(user_id, using="progress"):
        job = PayrunJob.objects.using("progress").filter(pk=job_id).first()
        if job is None:
            # Под контекстом автора задания не видно. Писать отказ некуда:
            # политики не пропустят и его.
            raise JobUnavailable(
                f"задание {job_id} недоступно под контекстом пользователя {user_id}"
            )
    # Проверки «а не занято ли уже» здесь нет намеренно, хотя напрашивается.
    # Она была, и её удаление не роняло ни одного теста — потому что она ничего
    # не решала: захват ниже сам условный (`where status = 'queued'`), и работа
    # уже чья-то означает ноль изменённых строк. Отдельный запрос о статусе к
    # тому же был бы гонкой: между ним и записью успевает вклиниться второй
    # исполнитель. Гарантию держит один контур, и он проверяемый.
    if not reporter.claim():
        # Работа уже чья-то: очередь выдала задачу второй раз или задание
        # перехватил синхронный расчёт со страницы.
        return

    try:
        run_calculation(job_id, user_id, reporter=reporter)
    except PayrunRefused as refusal:
        reporter.finish(error=refusal.message, details=refusal.details or refusal.ledgers)
        return
    except Exception:
        reporter.finish(error=BROKEN)
        raise
    reporter.finish()


def run_calculation(job_id: str, user_id: str, *, reporter) -> None:
    """Сам расчёт под контекстом автора задания.

    Отдельно от `run_job`, чтобы захват задания и работа не смешивались: захват
    идёт по каналу прогресса, работа — по основному соединению, и это разные
    транзакции по построению, а не по случайности.
    """
    from web.dbcontext import db_context

    with db_context(user_id):
        job = PayrunJob.objects.filter(pk=job_id).first()
        if job is None:
            raise JobUnavailable(f"задание {job_id} недоступно под контекстом {user_id}")
        _calculate(job, user_id, reporter)


def _calculate(job: PayrunJob, user_id, reporter) -> None:
    """Проверить права здесь и сейчас — и посчитать."""
    from web import permissions
    from web.principal import principal_for_user

    who = principal_for_user(user_id)
    try:
        permissions.check(who, permissions.PAYRUN_CALCULATE)
    except permissions.PermissionRefused as refusal:
        # Отказ по правам — такой же понятный человеку отказ расчёта, как
        # недоступный регистр: он показывается на странице теми же словами.
        raise PayrunRefused(refusal.message) from refusal

    calculate_period(
        tenant_id=job.tenant_id,
        period=job.period,
        visible_ledgers=who.visible_ledgers,
        reporter=reporter,
    )
