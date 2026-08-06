# Рынок и референсы: зарплатный модуль и P&L для сети общепита

> Исследование фазы Forge «рынок и референсы» для dodo_pnl_service.
> Дата: 2026-08-06. Каждое утверждение — со ссылкой. Помечено, что прочитано целиком (**полностью**),
> а что просмотрено (**обзорно**). Факты и выводы разделены.

### Что не удалось прочитать (честная оговорка)

Часть источников закрыта для машинного чтения — там, где так, это помечено в тексте,
и утверждение опирается на выдачу поиска, а не на первоисточник:

- **G2** — 403 на все страницы отзывов (R365, Ottimate, MarketMan). Цитаты пользователей
  брались с Capterra, где чтение прошло.
- **Gusto Help Center** (`support.gusto.com`) — 403. Шаги восстановлены по подробному
  стороннему разбору со скриншотами, заголовки статей справки сверены.
- **Factorial Help Center** — редирект на форму входа, справка закрыта.
- **7shifts KB**, **Remote Support** — 403.
- **Capterra по MarginEdge** — страница по искомому id отдаёт другой продукт;
  первичных цитат по MarginEdge нет.
- **Qvinci** — публична только маркетинговая страница; механика маппинга не документирована
  публично вообще.
- Балканских/сербских payroll-продуктов с публичной документацией по процессу закрытия
  периода найти не удалось — **направление не закрыто**, если оно важно, нужен отдельный
  заход через локальные источники.

Не проверялось на живом продукте ничего: демо-аккаунтов не заводилось, скриншоты
не снимались. Всё ниже — чтение документации и отзывов.

---

## 1. Аналоги-продукты (ресторанные ERP и P&L)

### Restaurant365 — как устроен поток «часы → зарплата → P&L»

Ключевая механика — **двухфазный учёт труда: оценка (accrual) и факт**. Это самый
близкий к нам паттерн, потому что решает ровно ту же проблему: P&L надо показывать
ежедневно, а зарплата закрывается раз в период.

Факты (источник — [Payroll Journal Entry Overview, docs.restaurant365.com](https://docs.restaurant365.com/docs/payroll-journal-entry-overview), прочитано **обзорно** через выжимку):

- **Фаза оценки.** При утверждении Daily Sales Summary (дневная сводка продаж из POS)
  система сама создаёт проводку по труду: часы из POS × ставка, привязанная к должности
  (job title). Кредитуется «Accrued Payroll Account», указанный в карточке точки на
  вкладке *Labor Estimates*. Так весь период копится обязательство.
- **Фаза факта.** Когда приходят данные от зарплатного провайдера, создаётся запись
  *Payroll Journal Entry* с полями *Payroll Start Date* / *Payroll End Date*. При её
  утверждении система дебетует Accrued Payroll Account, сторнируя оценки, и ставит
  фактические суммы.
- **Две вкладки для сверки**: *Distribution* — расхождения между оценкой и фактом
  в разрезе счетов; *Payroll Estimate Clearing* — сторнирующие проводки. Формулировка
  из доки: «the system trues up the estimated labor by debiting the Accrued Payroll
  Account and replacing the estimates with actual payroll amounts»; если всё сошлось,
  «the GL account should return to zero».
- **Типовые причины расхождений** прямо перечислены в доке: период выплаты пересекает
  границу учётного периода; часы утверждены после закрытия DSS; смена маппинга
  должность → счёт или ставки в середине периода.
- **Ошибка маппинга видна как «перекос по счетам»**: если должность Cook привязана
  к FOH Labor, а фактическая зарплата легла на BOH Labor, вкладка Distribution покажет
  полную сумму, уходящую в BOH и вычитаемую из FOH ([из выдачи поиска по docs.restaurant365.com](https://docs.restaurant365.com/docs/est-act-payroll-journal-entries)).
- Автосоздание проводок **опционально и отключаемо** — тогда данные зарплаты выгружаются
  и заносятся вручную ([docs.restaurant365.com/docs/aps-payroll-overview](https://docs.restaurant365.com/docs/aps-payroll-overview), **обзорно**).

Факты про отчёт по зарплате (источник — [Labor Payroll Review, docs.restaurant365.com](https://docs.restaurant365.com/docs/labor-payroll-review), прочитано **полностью** через выжимку):

- Отчёт живёт в приложении *Reports* → *My Reports*, запускается кнопкой **Run** или
  **Customize** (параметры до запуска).
- Параметры: диапазон дат (Start/End), должности (одна или несколько), тип должности,
  сортировка по имени/фамилии, формат **Summary** (итог часов на сотрудника) или
  **Detail** (разбивка часов по должностям), флаги *Identify Minors* и *Mask Payroll ID
  to Last 4 Digits*.
- Колонки: сотрудник, время смены, ставка, часы в разбивке regular / overtime /
  double-time, gross pay, штрафы за перерыв и split shift, чаевые, итого labor pay.
- В карточке сотрудника есть чекбоксы исключения — исключённые сотрудники не попадают
  ни в Labor Hours, ни в Labor $.

**Вывод (мой, не факт):** R365 — это отчёт, а не рабочее место закрытия. В доке
Labor Payroll Review **нет** ни статусов, ни утверждения, ни подсветки расхождений,
ни правил редактирования — эти вещи в R365 живут в отдельных сущностях (DSS, Payroll JE),
а «ведомость» остаётся выгрузкой. Для нас это значит: не копировать их разделение
«отчёт отдельно, утверждение отдельно» — бухгалтеру нужен один экран.

**Что берём:** идею «оценка → факт → явная вкладка расхождений, которая должна
схлопнуться в ноль». Это готовый паттерн объяснимости: не «поверь цифре», а
«вот остаток, который не сошёлся, и вот его разложение по счетам».

### Restaurant365 — на что жалуются (конкретика, не рейтинг)

Источник — [отзывы Capterra, стр. 1](https://capterra.com/p/139768/Restaurant365/reviews/)
и [стр. 2](https://capterra.com/p/139768/Restaurant365/reviews/?page=2), прочитано **обзорно**
(выжимка цитат). Цитаты приведены как есть, с ролью автора.

Про зарплату и труд:
- Payroll Manager (Chris C.): «the job codes from Toast will only flow over into the
  employee's profile in R365 for scheduling once they have clocked in using that job
  code. That makes no sense» — справочник должностей наполняется как побочный эффект
  факта, а не заводится заранее.
- CFO (Bruce N.): «Does not handle credit card tips paid out in server and bartender
  paychecks» — не покрыт целый компонент выплаты.

Про закрытие периода:
- Accounting Manager (Narie C.): «DSS will poll out of balance without explanation
  from Support» — **расхождение без объяснения**. Ровно тот антипаттерн, против
  которого мы строим объяснимость.
- Owner (Tracy L.): «we are still having problems with clearing bank items».

Про отчётность:
- CFO (Vincent C.): «Reports in the system are wonky and not very customizable».
- Accounting Manager (Narie C.): «Reports only pull information for up to 2 years back
  — very inconvenient».
- CFO (verified): «Ad Hoc reporting… not very intuitive».
- Controller (Robert M.): «We wish the search function was a little better. You can
  search anything but in order to get results you have to know how it was entered».

Про ввод и Excel:
- Staff Accountant (Michelle W.): «There's barely any Excel add-in functionality so
  manual data entry remains burdensome» — бухгалтер не готов расстаться с Excel,
  и продукт, который не даёт мост туда-обратно, проигрывает.

Про внедрение и общую надёжность:
- President (Jill M.): «The setup is hard and long and they are not the best teachers».
- Financial Analyst (Cailin H.): «I feel like I'm using an unfinished product».
- VP (Kerri L.): «The product is unreliable… same bugs return after updates».
- Catering Coordinator (Drew H.): «It is not a very user friendly interface».

Агрегированные темы жалоб на G2 (страница отдаёт 403 на прямое чтение, **не прочитано**;
взято из выдачи поиска по [g2.com/products/restaurant365/reviews](https://www.g2.com/products/restaurant365/reviews)):
сложная и долгая настройка, крутая кривая обучения, пробелы в интеграциях с зарплатой,
жёсткие финансовые отчёты, дорого.

**Вывод (мой):** три претензии повторяются у всех и напрямую бьют в наш дизайн —
(1) расхождение без объяснения, (2) негибкие отчёты, (3) отсутствие моста в Excel.
Все три дешевле заложить сразу, чем чинить потом.

### Crunchtime — «posting up»: закрытие периода как явное действие

Факт (источник — [crunchtime.com/blog/blog/franchise-management-software](https://www.crunchtime.com/blog/blog/franchise-management-software)
и [Platform Training Reference Guide, PDF](https://assets.crunchtime.com/image/upload/v1/docs/RG_-_Platform_Training_Reference_Guide.pdf),
прочитано **обзорно** через выжимку поиска):

- В конце недели точка закрывает учётный период: вносит финальные инвентаризации и
  закрывает открытые транзакции. По труду — менеджеры **проверяют отметки времени
  на корректность и затем закрывают труд за неделю**.
- Это действие называется **«posting up»**: закрывает неделю для дальнейших изменений
  и говорит системе начать пересчёт и агрегацию данных для отчётности.
- Франчайзинговая логика: данные вводятся один раз на уровне Enterprise и растекаются
  куда нужно; принцип сформулирован как «COGS is COGS is COGS — must be calculated the
  same way at all locations and the same in all reports».

**Что берём:** (1) закрытие периода — **явное действие пользователя**, а не наступление
даты; (2) закрытие **замораживает** период и триггерит пересчёт агрегатов; (3) правила
расчёта централизованы на уровне бренда, а не переизобретаются на точке — это буквально
наша стратегическая цель (сопоставимые цифры по сети).

### Ottimate (ex-Plate IQ) — автокодирование в план счетов и его пределы

Факты (источники: [обзор Ottimate на factura.ai](https://factura.ai/ottimate-review/),
[Ottimate Pros and Cons, G2](https://www.g2.com/products/ottimate/reviews?qs=pros-and-cons),
[Capterra](https://www.capterra.com/p/148741/Plate-IQ/reviews/) — всё **обзорно**, через
выжимку поиска; прямое чтение G2 блокируется 403):

- Механика: OCR по PDF/сканам + **AI-кодирование, которое расставляет GL-коды по
  выученным паттернам из истории счетов**, плюс настраиваемые маршруты согласования
  по поставщику, сумме и другим критериям.
- Жалобы по существу: система периодически неверно распознаёт поставщика или реквизиты,
  требуя ручных правок; плохо разносит **GL-splits** (разнесение одного документа
  на несколько счетов); автоклассификация не учитывает часть кодов поставщиков,
  особенно на сервисных счетах, и кодировку приходится править руками.
- Дубли: «sometimes the duplicate invoice is not caught by the system and is processed
  twice».
- Мультиточечность сделана слабо: **отдельный e-mail на каждую точку** вместо единого
  входящего с автомаршрутизацией.
- Задержка: до 24 часов от загрузки до готовности документа — если нужно в тот же день,
  всё равно вводят руками.

**Вывод (мой):** автокодирование ценно, но только если рядом стоит дешёвая ручная
правка и **обучение на правках**. И: единый инбокс с маршрутизацией по точке — наше
конкурентное преимущество почти даром, раз у лидера этого нет.

### MarginEdge и Marketman — короткие заметки

**MarginEdge.** Прямое чтение отзывов Capterra не удалось (страница по искомому id отдаёт
чужой продукт). Всё ниже — **вторичные источники**, точность ниже, чем у цитат по R365:
хвалят автоматическую обработку счетов и простой интерфейс, ругают то, что это не полноценная
инвентаризационная платформа и что настройка требует времени
([selecthub: MarginEdge vs Restaurant365](https://www.selecthub.com/restaurant-management-software/marginedge-vs-restaurant365/),
[g2.com/compare/marginedge-vs-restaurant365](https://g2.com/compare/marginedge-vs-restaurant365) —
**обзорно**). Отдельно отмечают, что R365 при автообработке счетов «relies more on user
inputs for proper categorization», оставляя место ошибкам на сложных документах.

**Marketman.** Факты (**обзорно**, [Capterra](https://www.capterra.com/p/136439/Marketman-Restaurant-Inventory/reviews/),
[G2 pros and cons](https://www.g2.com/products/marketman/reviews?qs=pros-and-cons),
[research.com](https://research.com/software/reviews/market-man-review) — прямое чтение G2
блокируется):
- «Setting up is very tedious, especially for a company that has a central commissary
  that distributes to branches» — многоточечная конфигурация оказалась узким местом.
- «Invoice scanning did not work 50% of the time and support would take days to respond».
- Проблемы синхронизации с POS (Square — дублирование товаров); нет интеграции
  с Restaurant365/Compeat.
- «Reporting, invoice processing, and integrations constantly have issues, making even
  simple tasks time-consuming».

**Вывод (мой):** у нас мультиточечность и «центральная кухня → точки» — не край, а норма
с первого дня. Это то, на чём конкуренты спотыкаются, и это надо закладывать в модель
данных, а не докручивать.

### Франчайзинговая консолидация (стратегически ближайший класс)

Факты:
- Qvinci: «Qvinci's patented mapping automatically aligns each franchisee's Chart of
  Accounts to a brand-defined SCoA» (Standard Chart of Accounts); онбординг описан как
  «map to the Standard Chart of Accounts with help from our patented mapping technology»,
  то есть **первичный маппинг делает команда вендора**
  ([qvinci.com/franchise-solution](https://www.qvinci.com/franchise-solution), прочитано
  **полностью** — страница маркетинговая).
  **Чего на странице нет** (проверено): кто принимает решение по маппингу дальше,
  что происходит при появлении нового счёта у франчайзи, как разрешаются конфликты,
  как именно собираются данные (API/синк/выгрузка), как ловятся расхождения.
- metiRi (Revenue Management Solutions): консолидирует P&L «from any accounting platform»
  в стандартизованные отчёты с бенчмаркингом между точками
  ([revenuemanage.com/metiri](https://www.revenuemanage.com/metiri/), **обзорно**).
- Общий тезис отрасли: единый план счетов и единые соглашения по именованию — фундамент
  сопоставимых роллапов; без этого сравнение невозможно
  ([reachreporting.com/blog/consolidated-franchise-reporting](https://reachreporting.com/blog/consolidated-franchise-reporting), **обзорно**).

**Вывод (мой):** весь этот класс продуктов решает задачу **задом наперёд** — франчайзи
ведёт учёт как хочет (обычно в QuickBooks), а платформа потом мучительно маппит его
план счетов в бренд-стандарт, и маппинг делают руками консультанты вендора. Наш заход
принципиально сильнее: **первичка собирается сразу в бренд-формате**, маппинг не нужен,
потому что нет чужого плана счетов. Это стоит зафиксировать как позиционирование.
Обратная сторона: партнёр, у которого уже есть своя бухгалтерия, всё равно потребует
выгрузки в свой формат — мост наружу заложить придётся.


---

## 2. Payroll-продукты: процесс закрытия периода

### Gusto — линейный визард «часы → отпуска/удержания → сводка → отправить»

Прямое чтение справки Gusto блокируется (403 на `support.gusto.com`), поэтому шаги взяты
из подробного стороннего разбора со скриншотами
([merchantmaverick.com/how-to-use-gusto-payroll](https://www.merchantmaverick.com/how-to-use-gusto-payroll/),
прочитано **полностью**) и сверены с заголовками статей справки
([Run a regular payroll](https://support.gusto.com/article/999754831000000/Run-a-regular-payroll-for-admins),
[Set up approvals for payroll](https://support.gusto.com/article/240829150046240/Set-up-approvals-for-payroll),
[Run an off-cycle payroll](https://support.gusto.com/article/999908231000000/run-an-off-cycle-payroll-for-admins) —
**не прочитано**, только заголовки из выдачи).

Факты:
- Расчёт — **линейный визард из трёх экранов**, а не одна большая таблица:
  1. *Review Hours & Earnings* — по каждому сотруднику: обычные часы, оклад, сверхурочные,
     бонусы, комиссии, чаевые, возмещения. Удержания правятся через меню из трёх точек
     в колонке *Actions* → *Edit Deductions*. Кнопки *Save and Continue* / *Cancel*.
  2. *Time Off, Benefits & Deductions* — отпуска/больничные, ручной ввод часов в серые
     поля, поддержка дробных значений. *View Details* раскрывает редкие категории
     (Bereavement, Custom Policies, Floating Holidays, Jury Duty). **Рядом с каждой
     политикой показан остаток часов** — так подсвечивается превышение лимита.
  3. *Review and Submit* — сводка с тремя **раскрывающимися** блоками:
     *What Gets Taxed and Debited*, *What Your Employees Worked & Take Home*,
     *What Your Company Pays*. Кнопка *Submit Payroll*.
- Согласование: у Gusto есть настраиваемое утверждение расчёта, но оно **не работает для
  off-cycle и extra pay** — там сразу *Review summary* → *Submit payroll*
  (из выдачи по статьям справки, **обзорно**).
- Отсутствующие в расчёте сотрудники — доку советует проверить «employment status, pay
  schedule, and payroll setup»; **явной валидации/предупреждений в разборе не описано**.

**Вывод (мой):** сильная идея — **три разреза одной суммы на финальном экране**:
что удержано, что получил сотрудник, что заплатила компания. Это и есть «объяснимость
для непрофессионала» в одном экране. Слабая идея — визард: для 30 человек и ежемесячного
цикла бухгалтеру нужна одна плотная таблица со всем сразу, а не листание.

### Zoho Payroll — явный конечный автомат статусов и обратимость

Источники (все прочитаны **полностью**, docs публичные):
[Regular Payroll](https://www.zoho.com/us/payroll/help/employer/pay-runs/regular-payroll.html),
[Configure Approval Workflows](https://www.zoho.com/in/payroll/help/employer/settings/configure-approvals.html),
[Reverting Payroll (KB)](https://www.zoho.com/in/payroll/kb/employer/pay-runs/how-to-revert-payroll.html).

Факты:
- Четыре стадии описаны прямо в доке как этапы: *Processing* → *Reviewing and updating*
  → *Submitting and approving* → *Recording payment*.
- Статусы: **Draft** (после *Process Pay Run*, правки разрешены) → **Payment Due**
  (после утверждения) → **Paid** (после *Mark as Paid*). Плюс состояние **Rejected**
  при откате.
- Кнопка зависит от прав: у админа/финансов — *Submit and Approve*, у обычного
  пользователя — *Submit for Approval*.
- В драфте правится в том числе **«Add Hours for Additional Job Roles»**: у сотрудника
  с несколькими ролями часы задаются по каждой роли отдельно (regular / overtime /
  double-time). После правки система «will automatically recalculate the employee's
  wages and taxes».
- Жёсткое правило: **«Once a regular payroll is approved, it cannot be edited or deleted»**.
- Но есть **обратимость через явный откат**: *Delete Recorded Payment* → *Reject Approval*,
  после чего «the pay run will be moved to the rejected state automatically» и его можно
  редактировать или удалить и создать заново. Причина отката, судя по доке, **не требуется**.
- Согласование настраивается для двух сущностей: **Pay Runs** и **Salary Revisions**.
  Три типа: *Simple Approval* (один уровень), *Multi-Level Approval* (`+ Add New Level`,
  сколько угодно уровней), *Custom Approval* (условные правила на каждого согласующего).
  Настройка живёт в *Settings > Approvals*.

**Вывод (мой):** это самая близкая к нам модель. Ключевое: утверждённый период
**неизменяем**, но **обратим явным действием с аудит-следом**, а не «админ полез в базу».
Нам добавить то, чего у Zoho нет: **обязательная причина отката** — иначе через три месяца
никто не вспомнит, почему июнь пересчитывали.

### Retro pay: как правки задним числом не ломают закрытый период

Факты (источники: [Oracle HCM — Recalculate Payroll for Retroactive Changes](https://docs.oracle.com/en/cloud/saas/human-resources/faagp/recalculate-payroll-for-retroactive-changes.html),
[Oracle JD Edwards — Process Retroactive Payroll](https://docs.oracle.com/cd/E26228_01/doc.93/e21936/proc_retro_payrl.htm),
[SAP Help — Retroactive Payroll Processing](https://help.sap.com/docs/SAP_Best_Practices/be58cb8ae308424088abd7e0bb7ed0f6/ade4ceec53f445d5afc043ce7e7f1234.html),
[Paychex — Retro Pay Guide](https://www.paychex.com/glossary/what-is-retroactive-pay) —
**обзорно**, через выжимку поиска):

- Отраслевой стандарт: **закрытый период не переписывается**. Система пересчитывает
  прошлый период «в уме», сравнивает новый результат со старым, и разница —
  **retro delta** — становится отдельной строкой начисления/удержания **в текущем периоде**.
- Retro-выплата помечается как supplemental wages для корректного налогообложения.
- Триггер пересчёта — «retro event» (изменение ставки, задним числом внесённые часы,
  пропущенные сверхурочные), система сама определяет затронутые периоды.

Уточнение по Oracle HCM (страница «Recalculate Payroll for Retroactive Changes» прочитана
**полностью**) — механика описана буквально:

- Retro event — изменение после того, как расчёт уже прогнан (смена оклада и т.п.);
  система выпускает **retroactive notification**, помечающее сотрудника к пересчёту.
- Процесс пересчёта берёт **только помеченных** сотрудников, которые были в исходном
  прогоне, считает заново по новым данным и сравнивает с исходным результатом.
- **«Never overwrites historical data — it creates new entries instead.»** Три случая:
  результат есть в обоих прогонах → *retroactive pay run result*; был в исходном, нет
  в новом → тоже *retroactive pay run result*; не было в исходном, появился в новом →
  *retroactive pay element entry*.
- Дата ретро-записей — **начало того периода, в который попадает Process Date**
  (пример из доки: Process Date 31-JUL-21 → записи с датой начала 01-JUL-21, считаются
  в июльском расчёте).
- Важная деталь UX: «Retroactive element entries are no different from the entries in the
  original payroll… **display on the UI in the same manner**» — **визуально ретро-строки
  не отличаются от обычных**.

**Вывод (мой):** последнее — это, наоборот, ошибка, которую копировать не надо. Бухгалтер,
сверяющий с таблицей, обязан видеть, что строка — перерасчёт за прошлый месяц, иначе
итог «не сходится» без причины. Помечать ретро-строки визуально и давать ссылку
на исходный период.

**Вывод (мой):** для нас это ответ на «версионирование по датам везде» из CLAUDE.md.
Два варианта поведения — пересчитать закрытый период на месте или вынести дельту
в текущий — и второй вариант почти наверняка правильный: он совместим с уже выплаченной
ведомостью и с уже сданным P&L. Но у нас три контура, и дельта должна ложиться в тот же
контур, в котором была исходная строка.

---

## 3. UX табличного ввода часов

### NetSuite Weekly Timesheets — эталон недельной сетки

Источники (Oracle, публичные, прочитаны **полностью**):
[Weekly Timesheets (обзор)](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/section_4671374137.html),
[Using Weekly Timesheets](https://docs.oracle.com/en/cloud/saas/netsuite/ns-online-help/subsect_161131809908.html).

Факты:
- Ввод — на подвкладке **Enter Time**. Строка = объект учёта (клиент/проект),
  колонки = дни недели. «For each day of the week, enter the duration or total hours
  in the designated cell.»
- **Прогрессивное раскрытие в ячейке**: по умолчанию вводится только длительность,
  а плюсик в правом нижнем углу ячейки раскрывает *Start Time*, *End Time* (формат
  `h:mm am/pm`) и *Memo*. То есть быстрый путь — одно число, детальный — по требованию.
- Строки добавляются кнопкой **Add**, по одной на каждый проект/клиента.
- Сохранение: **Save**; при включённых Advanced Approvals — **Save & Submit**.
- **Гранулярная блокировка**: табель целиком можно редактировать, даже если он частично
  или полностью утверждён, **но отдельные записи в статусе approved / pending approval
  заблокированы**. В полностью утверждённый табель можно **дописывать новые записи**.
  Любая правка отправленного табеля требует **повторной отправки** на утверждение.
- Есть отдельная функция «Copying a Previous Weekly Timesheet» (механика в отдельной
  статье, **не прочитана**).

**Вывод (мой):** гранулярная блокировка на уровне строки, а не всего периода — прямо
то, что нам нужно: бухгалтер закрыл 28 человек, двое спорных остались открытыми,
и период не блокируется целиком.

### Sage People — валидация и правила ревью (сформулированы явно)

Источник: [разбор Sage People Timesheets](https://www.cleverence.com/articles/sage-documentation/timesheets-sage-people-help-center-home-5287/),
прочитано **полностью**. Оговорка: это **сторонний** справочный сайт, пересказывающий
Sage, а не first-party документация Sage — факты о продукте оттуда стоит считать
менее надёжными, чем сформулированные там же принципы UX (которые ценны сами по себе).

Факты/принципы:
- Два режима ввода: **дневной** (clock-style с быстрыми заметками) и **недельная сетка**
  для проектной работы, «multiple lines per day and easy copy-forward of recurring tasks».
- Мобильный ввод ускоряется «recent selections, favorites, and quick duration chips
  (e.g., 0.5, 1, 2 hours)» — **чипы типовых длительностей** вместо клавиатуры.
- Валидации, которые реально ставят: минимум/максимум часов в день; обязательный
  комментарий для отдельных типов времени (например, on-call); обязательный код проекта
  для billable; «day's allocations equal the total hours submitted» (сумма разнесений
  = введённому итогу); сверка с графиком — «can flag under- or over-reporting».
- **Баланс валидаций сформулирован прямо**: «too many hard stops cause support tickets,
  while too few mean messy corrections at payroll cut-off».
- Отказы должны быть структурными: «Use structured reasons — missing project code,
  mismatched schedule, overtime not pre-approved — so employees learn the pattern».
- Экран ревью менеджера должен показывать «total hours, exceptions, and **changes since
  last submission**»; менеджеру дают **bulk-approve для чистых табелей** и drill-down
  для аномалий.
- После утверждения: «lock records to prevent silent edits, and route any post-approval
  changes through an **auditable adjustment process**».
- Напоминания за T-48 / T-24 / T-8 часов до дедлайна.
- Выгрузка в зарплату: «stable file with persistent identifiers for employees, time types,
  and pay codes. Consistency is more important than clever formatting», плюс
  **контрактные тесты** — прогонять маленький пробный файл через интерфейс перед каждым
  расчётом.

**Вывод (мой):** «changes since last submission» и «bulk-approve чистых + drill-down
аномалий» — два готовых решения нашей главной задачи: 30 человек, из них 27 тривиальны.
Экран ревью должен по умолчанию показывать **только то, что требует внимания**.

### Toast Payroll / 7shifts — «закрыть табель» как шлюз

Факты (прямое чтение [7shifts KB](https://kb.7shifts.com/hc/en-us/articles/4417514170003-Toast-Payroll-Export)
заблокировано 403; взято из выдачи поиска, **обзорно**; подтверждается веткой
[Toast Community — Import Payroll Timesheets](https://community.toasttab.com/t5/florida/import-payroll-timesheets/m-p/15046)):

- На вкладке *Time Clocking* отметки можно **Approve** или **Edit**, есть **Approve All**.
- Перед выгрузкой в зарплату нужно утвердить все отметки и **закрыть табель**;
  «Only Admins can close all timesheets, a mandatory step to send payroll data to your
  integration successfully».

**Вывод (мой):** закрытие табеля — отдельный шлюз **до** расчёта, с правом только
у админа. У нас роль «управляющий точки» вводит часы, а «бухгалтер» закрывает —
шлюз ложится ровно на нашу ролевую модель.

---

## 4. Объяснимость расчёта

Здесь оказалось, что отрасль решает задачу «откуда взялось число» **не** объяснением
формулы, а **тремя другими приёмами**: drill-down по цепочке до первички, сравнение
с прошлым периодом (variance) и разложение итога на три разреза. Формулу почти никто
не показывает.

### Приём 1. Drill-down до первички (Xero, классические учётные системы)

Факт: в Xero клик по любой цифре в отчёте P&L открывает отчёт **Account Transactions**
с транзакциями, составляющими эту сумму
([Account Transactions report, Xero Central](https://central.xero.com/0/article/Account-Transactions-report-New);
подтверждение поведения — из выдачи поиска, **обзорно**).
Факт-контрапункт: пользователи жалуются, что часть drill-down пропала в новой версии
отчётов — «Xero got rid of this function with the new report update»
([Xero Product Ideas: P&L Tracking — Ability to drill down](https://productideas.xero.com/forums/967133-reports-tax/suggestions/46712989-profit-loss-tracking-ability-to-drill-down), **обзорно**).

**Вывод (мой):** drill-down — не фича, а **ожидание по умолчанию**; его отъём вызывает
отдельный поток жалоб. Для нас цепочка очевидна и должна быть кликабельна целиком:
строка P&L → ведомость периода → сотрудник → компоненты выплаты → введённые часы →
кто и когда их ввёл.

### Приём 2. Payroll Variance Report — сравнение с прошлым периодом

Это, похоже, главный инструмент проверки правильности расчёта в отрасли. Не «докажи
формулу», а «покажи, что изменилось против прошлого раза, и объясни только изменения».

Факты (источник — [PrismHR: Payroll Variance Analysis](https://ies.prismhr.com/docs/prismhr/docs/sp_help/Content/PD/Payroll/Dashboard/Payroll_Variance_Analysis.htm),
прочитано **полностью**):
- Сравнивается текущий расчёт в статусе **Calculated** с выбранным расчётом в статусе
  **Finalized**; сравниваемый период должен быть «within 180 days of the initialized
  Payroll».
- Поле **Payroll Variance Warning** выбирает тип порога: **Amounts** (абсолютное
  отклонение, без верхнего предела) или **Percentages** (0–100%). Значение порога
  по умолчанию — 10. Отдельное поле **Payroll Variance Percentage Warning** подсвечивает
  случаи, где отклонение ≥ заданного процента.
- Поле **Breakdown** переключает разрез и раскрывает список сотрудников; клик по ссылке
  **Voucher** показывает сравнение по конкретному сотруднику между двумя расчётами,
  а опция **Detail** — «a file audit for the payroll change», то есть **аудит изменений**.

Дополняющие факты (**обзорно**, из выдачи поиска):
- Типовые колонки такого отчёта: Department, Employee Number, Employee Name,
  Previous Gross, Current Gross, Previous Net, Current Net
  ([PR Payroll Variance Report, Spectrum](https://help.sprbrk.com/seven_help/7.18.7.0/Content/Cirrus/PR/Reports/PR_Payroll_Variance_Report.htm)).
- Порог задаётся числом 1–99%; сравниваются в первую очередь Gross Earnings,
  Additional Pay Earnings, Overtime Earnings
  ([Remote: How to use the Payroll Variance Report](https://support.remote.com/hc/en-us/articles/41134470354829-How-to-use-the-Payroll-Variance-Report) —
  прямое чтение **заблокировано 403**, взято из выдачи).
- Рекомендация задавать **разные пороги на разные компоненты**: «a 5% variance might be
  fine for overtime but not for base salaries»
  ([beebole.com/blog/payroll-variance-guide-finance-hr-managers](https://beebole.com/blog/payroll-variance-guide-finance-hr-managers), **обзорно**).
- ADP описывает pre-payroll стадию сверки: проверить, что учтены наймы/увольнения и что
  у всех верная ставка, **до** расчёта
  ([adp.com — payroll reconciliation](https://www.adp.com/resources/articles-and-insights/articles/p/payroll-reconciliation.aspx), **обзорно**).

**Вывод (мой):** это наш главный экран сверки, причём он бесплатно решает исходную
задачу «бухгалтер сверяет с Excel». Первый месяц он сверяет с таблицей, дальше —
с прошлым периодом внутри системы. Пороги должны быть **на компонент**, а не общие.

### Приём 3. Три разреза одной суммы (Gusto)

Уже описано в разделе 2: на экране *Review and Submit* итог разложен на
*What Gets Taxed and Debited* / *What Your Employees Worked & Take Home* /
*What Your Company Pays*. Одна и та же сумма, три точки зрения.

**Вывод (мой):** у нас разрезов тоже три, но другие — **по контурам** (белый/серый/чёрный),
**по точкам** и **по компонентам выплаты**. Плюс четвёртый, специфичный для нас: «что
уходит в P&L» против «что реально выдаётся на руки» — эти суммы у нас не совпадают
по определению.

### Приём 4. Payroll register — канонический плотный отчёт

Факты (**обзорно**): payroll register — строка на сотрудника за период, колонки —
элементы расчёта, показывает **полный gross-to-net** по каждому и служит для проверки,
что «the system correctly calculated gross-to-net amounts for employees and that the
correct employees are being paid»
([ADP: What is a Payroll Register](https://www.adp.com/resources/articles-and-insights/articles/w/what-is-a-payroll-register.aspx),
[Paychex: What Is a Payroll Register](https://www.paychex.com/articles/payroll-taxes/payroll-register),
[Oracle JD Edwards: Payroll Register Report](https://docs.oracle.com/en/applications/jd-edwards/human-capital/9.2/eoapy/payroll-register-report.html)).

**Вывод (мой):** формат «строка = сотрудник, колонка = компонент, слева-направо
от gross к net» — отраслевой стандарт, который бухгалтер уже знает. Не изобретать свой:
наша ведомость должна читаться как payroll register, просто с нашими компонентами.

### Чего в отрасли почти нет

Проверено поиском по запросам про показ формулы расчёта в payslip/payroll: находятся
только объяснялки «как читать расчётный листок» (например,
[Deel: How to Read a Paycheck Stub](https://www.deel.com/blog/how-to-read-a-paycheck/),
**обзорно**), но **не** продуктовая фича «покажи, из какого правила и какой версии ставки
получилось это число».

**Вывод (мой):** здесь у нас реальная возможность отличиться, и она дешёвая, потому что
у нас правила уже лежат в конфиге с версионированием по датам. «Показать след расчёта»
= показать, какие правила пресета сработали, с какими входами и в какой версии.
Это ровно то, что снимет вопрос «откуда взялось 0,701» из CLAUDE.md.

---

## 5. Референсы интерфейсов плотных финансовых таблиц

### Типографика чисел — правила, а не вкусовщина

Факты (источник — [A List Apart: Web Typography — Designing Tables to be Read, Not Looked At](https://alistapart.com/article/web-typography-tables/),
прочитано **полностью**):

- **Только lining figures + tabular figures**: «Use tabular lining numerals to provide
  your reader with the most effective way to reference vertically and horizontally in
  tables of data». В CSS: `font-variant-numeric: lining-nums tabular-nums;`
- Табличные цифры — это моноширинные цифры внутри пропорционального шрифта; не нужен
  моношрифт целиком.
- **Числа — вправо**: «Right-align numbers to help your reader make easier comparisons
  of magnitude when scanning down columns». Заголовок числовой колонки выравнивается так же.
- Одинаковая точность (число знаков после запятой) по колонке; при разной — выравнивание
  по десятичной точке (`text-align: "." center`).
- **Зебру — не использовать**: она «distorts the meaning of the data by highlighting
  every other row to the detriment of neighbouring rows». Линейки — только по необходимости
  и светлые. Группировать строк белым пространством, а не заливкой.
- Плотный паддинг ячейки из статьи: `0.125em 0.5em 0.25em 0.5em` с уменьшенным
  line-height.

Дополнение (**обзорно**, [dev.to: Tabular Numbers in CSS](https://dev.to/alanwest/tabular-numbers-in-css-font-variant-numeric-vs-monospace-hacks-25cn)):
`tabular-nums` работает в большинстве системных и веб-шрифтов; для финансовых таблиц
уместен ещё и **перечёркнутый ноль** (slashed zero), чтобы 0 не путали с O. Довод в пользу
правого выравнивания — числа сравниваются справа налево: сначала единицы, потом десятки
([Design Better Data Tables, Matthew Ström](https://medium.com/mission-log/design-better-data-tables-430a30a00d8c), **обзорно**).

### Плотность строк и механика таблицы

Факты (источник — [Pencil & Paper: UX Pattern Analysis — Enterprise Data Tables](https://www.pencilandpaper.io/articles/ux-pattern-analysis-enterprise-data-tables),
прочитано **полностью**):

- Три режима плотности как переключатель: **Condensed 40px / Regular 48px / Relaxed 56px**.
- Текст — влево, числа — вправо; исключение — «качественные» числа (даты, почтовые
  индексы) выравниваются влево.
- **Липкая шапка** при вертикальной прокрутке и **липкий футер с итогами** («rollups and
  totals»); панель массовых действий — тулбар, прикреплённый к низу окна.
- **Заморозка левой колонки** при горизонтальной прокрутке; управление колонками —
  скрытие/перестановка/ресайз.
- Редактируемость ячейки сигнализируется **курсором ввода на ховере**.
- Массовые действия появляются только после выделения чекбоксами — до этого не засоряют экран.
- Пять способов показать детали строки: раскрывающаяся строка, тултип, модалка,
  **боковая панель** («most scalable for complex information»), полноэкранный режим.
- Сортировка по умолчанию — «самое свежее» или «самое требующее действия».
- Разделение строк: зебра — плохо (конфликтует с интерактивными состояниями), лучше
  линейка 1px светло-серым либо карточный фон.
- Оговорка честности: **статья не приводит реальных продуктов как примеры** — Google Sheets
  упомянут один раз и в негативном ключе.

### Продуктовые референсы

**Linear** — источник плотности без визуального шума.
Первоисточник ([linear.app/now/how-we-redesigned-the-linear-ui](https://linear.app/now/how-we-redesigned-the-linear-ui),
прочитано **полностью**):
- Цель редизайна сформулирована как «reduce visual noise, maintain visual alignment,
  and increase the hierarchy and density of navigation elements».
- Много работы ушло на выравнивание лейблов, иконок и кнопок по вертикали и горизонтали:
  «isn't something you'll immediately see but rather something that you'll feel after
  a few minutes».
- Перешли с HSL на **LCH** ради перцептивной равномерности («a red and a yellow color
  with lightness 50 will appear roughly equally light to the human eye») и **сократили
  переменные темы с 98 до трёх**: base color, accent color, contrast.
- Подняли общий контраст, убрав синеву из расчётов; Inter Display для заголовков,
  обычный Inter для текста.

Реверс-инжиниринг токенов Linear ([awesome-design-md / linear.app / DESIGN.md](https://github.com/voltagent/awesome-design-md/blob/main/design-md/linear.app/DESIGN.md),
прочитано **полностью**). **Осторожно:** это community-документ по **маркетинговому сайту**
Linear, а не официальная спека приложения — брать как ориентир, не как истину.
- Шаг спейсинга — 4px: 4 / 8 / 12 / 16 / 24 / 32 / 48 / 96.
- Радиусы: 4 / 6 / 8 / 12 / 16 / 24 / pill.
- **Теней нет вообще.** Глубина — «лестница поверхностей» из четырёх шагов
  (#0f1011 → #141516 → #18191a → #191a1b) плюс 1px hairline-границы
  (#23252a → #34343a → #3e3e44). Правило: «skipping levels breaks the system».
- Текст: ink #f7f8f8, muted #d0d6e0, subtle #8a8f98 — три уровня, не больше.
- Акцент #5e6ad2 «reserves exclusivity for brand mark, primary CTA, focus, and link
  emphasis — never as fill».
- Тип-шкала мелкая и с отрицательным трекингом на крупных кеглях; body 16/1.5,
  body-sm 14/1.5, caption 12/1.4.

Аналитика по восприятию скорости (**обзорно**, вторичные источники —
[925studios: Linear Design Breakdown](https://www.925studios.co/blog/linear-design-breakdown-saas-ui-2026),
[identityforge.io: The Linear design system, read as constraints](https://identityforge.io/learn/linear-design-system)):
высокая плотность читается чистой из-за жёсткой сетки, приглушённого цвета и
прогрессивного раскрытия; «hairline borders plus surface steps replace shadows, which
buys density and costs pre-attentive depth»; оптимистичные апдейты (запись появляется
в списке сразу, откатывается при ошибке) создают ощущение нулевой задержки.

**Ramp** — как выглядит финансовый продукт, не похожий на дашборд-по-номерам.
Факты (**обзорно**, вторичные источники: [Ramp DESIGN.md](https://www.designmd.co/d/ramp),
[Refero Styles: Ramp design system](https://styles.refero.design/style/b38702a0-75ab-474c-9106-00b624535825),
[themasterly.com: Fintech Dashboard Design](https://www.themasterly.com/blog/fintech-dashboard-design-guide)):
- Чёрно-белая «редакционная» система с **одним** акцентом (шартрез #e4f222), который
  появляется только там, «where money moves» — CTA, живые счётчики, активные состояния.
- Тёплый off-white холст, белые карточки, hairline-границы, **без теней** — карточки
  держатся на границах.
- Один неогротеск в одном начертании (Lausanne 400), крупные дисплейные кегли,
  плотный интерлиньяж, положительный трекинг на мелких капс-лейблах.
- Продуктовый принцип, который стоит запомнить: «show the numbers competitors hide —
  fees, rates, limits» и «treat deliberate friction in high-stakes flows as a feature».
- Заявленный эффект редизайна флоу проверки расходов (данные Ramp за 2024, **не проверено
  независимо**): 33% быстрее проверка транзакций, 20% мемо предлагаются автоматически.

**Общий вывод (мой):** для нас складывается очень конкретный визуальный контракт —
светлая тема (бухгалтер работает днём и печатает), hairline-границы вместо теней,
трёхступенчатая иерархия текста, **один** акцентный цвет только на действии и на
отклонении, tabular-nums везде, где есть деньги, зебры нет, итоги — в липком футере,
детали сотрудника — в боковой панели, а не в модалке. Три контура учёта (белый/серый/
чёрный) просятся быть **не** тремя цветами строк (это как раз зебра-антипаттерн),
а переключателем разреза плюс сдержанной меткой в отдельной колонке.

---

## 6. Антипаттерны и жалобы

Сводка повторяющихся претензий. Конкретные цитаты пользователей по R365, MarginEdge
и Ottimate — в разделе 1, здесь то, что общее для класса бухгалтерских систем.

1. **Расхождение без объяснения.** «DSS will poll out of balance without explanation
   from Support» (Accounting Manager, [Capterra / Restaurant365](https://capterra.com/p/139768/Restaurant365/reviews/?page=2)).
   Система показывает, что не сходится, но не показывает — где. Это худший из возможных
   вариантов: работы больше, чем в Excel.

2. **Нельзя откатить.** Классика — отсутствие undo для сверки в QuickBooks Online:
   в стандартной версии функции нет, она доступна только через QuickBooks Online
   Accountant, а обычный пользователь вынужден вручную снимать признак «R» у каждой
   транзакции в регистре ([Intuit Community: Why don't I have a reconcile undo option](https://quickbooks.intuit.com/learn-support/en-us/other-questions/why-don-t-i-have-a-reconcile-undo-option/00/242818),
   [Undo or remove transactions from reconciliations in QBO](https://quickbooks.intuit.com/learn-support/en-us/help-article/accounting-bookkeeping/undo-remove-transactions-reconciliations-online/L6ERlEXxn_US_en_US) —
   **обзорно**). Отдельный подвид: закрытие периода блокирует правки, а легального пути
   внести исправление нет ([Oracle ARCS: Locking Periods](https://docs.oracle.com/en/cloud/saas/account-reconcile-cloud/suarc/setup_period_close_lock_102xd5c346d0.html), **обзорно**).

3. **Данные легко ввести, трудно достать.** «One of the biggest complaints that people
   have with accounting software is being unable to export data»
   ([integrity-software.net: 8 Accounting Software Issues](https://www.integrity-software.net/blog/eight-common-accounting-software-complaints-and-how-to-avoid-them), **обзорно**).
   Подтверждается конкретикой по R365: «There's barely any Excel add-in functionality».

4. **Жёсткие, некастомизируемые отчёты** и произвольные ограничения (у R365 — глубина
   истории 2 года).

5. **Поиск, который требует знать, как именно ввели запись** («in order to get results
   you have to know how it was entered»).

6. **Долгое и мучительное внедрение, отсутствие обучения.** По опросу профильного
   издания, около 60% пользователей крупных продуктов ответили, что обучения
   от вендора **не получали** ([Journal of Accountancy: 2025 tax software survey](https://www.journalofaccountancy.com/issues/2025/sep/2025-tax-software-survey/), **обзорно**).

7. **Медленно на больших наборах данных**, ошибки автокатегоризации, требующие ручного
   пересмотра; поддержка отвечает по почте и медленно.

8. **Автоматизация, которая не доводит до конца.** Ottimate: дубли иногда проходят
   дважды; GL-splits разносятся плохо; до 24 часов задержки — «если нужно сегодня,
   вводи руками». Автоматизация без быстрого ручного обхода превращается в тормоз.

9. **Справочник наполняется как побочный эффект факта** — job codes появляются в профиле
   сотрудника только после того, как он по ним отбил смену. Мастер-данные должны заводиться
   явно, до операций.

10. **Ретро-строки, неотличимые от обычных.** Oracle прямо описывает это как поведение
    системы («display on the UI in the same manner»). Для сверяющего это источник
    «не сходится, а почему — непонятно».

**Итог (мой):** восемь из десяти пунктов — это не про функции, а про **обратимость,
объяснимость и выход наружу**. Если наш продукт хорошо делает только их, он уже лучше
того, на что жалуются.

---

## Развилки

Выборы, которые придётся сделать до того, как рисовать экраны. Терминология регистров —
по решению D009: `official` / `supplementary` / `internal`.

### Р1. Форма экрана расчёта: визард или одна плотная таблица

- **Суть.** Gusto ведёт по трём экранам подряд; payroll register — одна таблица
  «строка = сотрудник, колонки = компоненты, слева направо от gross к net».
- **Варианты.** (а) визард из 3 шагов; (б) одна таблица + боковая панель детали;
  (в) таблица с раскрывающимися строками.
- **Рекомендация: (б).** У нас месячный цикл, 30 человек и пользователь-бухгалтер,
  который сегодня видит всё сразу в Excel. Визард отнимает у него именно то, ради чего
  он терпит Excel, — обзор целиком. Боковая панель, по Pencil & Paper, «most scalable
  for complex information» и не рвёт контекст, в отличие от модалки.

### Р2. Правки задним числом: пересчёт закрытого периода или дельта в текущий

- **Суть.** Ставка изменилась задним числом, период уже утверждён и выплачен.
- **Варианты.** (а) пересчитать закрытый период на месте; (б) retro-delta отдельной
  строкой в текущем периоде (Oracle/SAP); (в) обязать откатить период целиком.
- **Рекомендация: (б) как основной путь, (в) — как явная аварийная процедура.**
  Пересчёт на месте ломает уже выданную ведомость и уже сданный P&L. Важное отличие
  от Oracle: **ретро-строки помечать визуально** и давать ссылку на исходный период —
  иначе бухгалтер получает «не сходится» без причины (см. антипаттерн 10).
  Дельта должна ложиться в **тот же регистр**, в котором была исходная строка.

### Р3. Гранулярность блокировки: период целиком или построчно

- **Суть.** 28 человек посчитаны, по двоим спор.
- **Варианты.** (а) блокируется период целиком; (б) блокировка на уровне строки
  сотрудника (модель NetSuite: табель редактируем, но утверждённые записи внутри
  заблокированы); (в) блокировка на уровне точки.
- **Рекомендация: (б) + (в).** Управляющий точки закрывает свою точку, бухгалтер
  закрывает период. Один спорный сотрудник не должен держать весь месяц.

### Р4. Обратимость утверждённого периода

- **Суть.** Zoho: «Once a regular payroll is approved, it cannot be edited or deleted»,
  но есть явный откат *Delete Recorded Payment* → *Reject Approval*, **без указания
  причины**. QuickBooks — противоположный полюс: undo сверки в обычной версии просто нет.
- **Варианты.** (а) необратимо; (б) обратимо свободно; (в) обратимо с обязательной
  причиной и аудит-следом.
- **Рекомендация: (в).** Необратимость порождает главную жалобу класса; свободный откат
  через полгода не объяснить. Причина отката — обязательное поле, откат виден в истории
  периода.

### Р5. Главный инструмент сверки

- **Суть.** Бухгалтер должен убедиться, что расчёт верный.
- **Варианты.** (а) сравнение с загруженной таблицей бухгалтера (наш текущий тест —
  по сути это); (б) variance-отчёт «текущий период против прошлого» с порогами;
  (в) только drill-down до первички.
- **Рекомендация: (а) на первый месяц + (б) как постоянный режим.** (а) — это разовый
  инструмент переезда, который окупается доверием; (б) — то, чем отрасль пользуется
  каждый цикл. Пороги задавать **на компонент**, а не общие: 5% нормально для сверхурочных
  и ненормально для оклада. (в) обязателен, но сам по себе не находит ошибку — он только
  объясняет найденную.

### Р6. Как показывать три регистра

- **Суть.** `official` / `supplementary` / `internal` с разной видимостью по ролям.
- **Варианты.** (а) три отдельные ведомости; (б) одна ведомость, регистр — колонка/метка
  и переключатель разреза; (в) цветовая заливка строк по регистру.
- **Рекомендация: (б).** (в) — прямой зебра-антипаттерн из A List Apart: заливка искажает
  чтение данных, а у нас ещё и конфликтует с подсветкой отклонений. (а) множит работу
  бухгалтера, у которого один человек может иметь строки в двух регистрах. Ключевое
  требование: при отсутствии доступа к регистру пользователь не должен видеть **ни строк,
  ни следов в итогах** — иначе итоги «не сойдутся» и он это заметит. Значит, итоги
  считаются по видимому срезу, а не маскируются на выводе.

### Р7. Форма ввода часов

- **Суть.** 30 человек, типы часов (обычные, праздничные, отпуск, больничный).
- **Варианты.** (а) сетка «сотрудник × тип часов» одной цифрой за период;
  (б) сетка «сотрудник × день» с типом внутри ячейки (NetSuite-подобно);
  (в) импорт файла; (г) поштучный ввод смен.
- **Рекомендация: (а) как основной ввод + (в) как обязательный дублёр, (б) — потом.**
  Партнёр сегодня ведёт итог за месяц, а не смены; требовать сразу подневной ввод —
  верный способ не получить данных. Но заложить модель хранения **подневную**, чтобы (б)
  и разнесение по точкам при переводе сотрудника в середине месяца не требовали миграции.
  Из NetSuite взять прогрессивное раскрытие ячейки: по умолчанию одно число, по плюсику —
  детали.

### Р8. Глубина маршрута согласования

- **Варианты.** (а) одноуровневое утверждение; (б) многоуровневое, как Zoho
  (*+ Add New Level*); (в) условные правила.
- **Рекомендация: (а) в интерфейсе, (б) в модели данных.** У партнёра сейчас один
  бухгалтер. Но таблица «шаги согласования» с одной строкой стоит дёшево, а переделка
  из «одно поле approved_by» — дорого.

### Р9. Мост в Excel

- **Суть.** Самая частая претензия класса — «ввести легко, достать нельзя».
- **Варианты.** (а) только интерфейс; (б) экспорт ведомости; (в) экспорт + импорт табеля
  + экспорт «как в их таблице».
- **Рекомендация: (в) с первого дня.** Это не уступка, а условие переезда: пока
  бухгалтер не может выгрузить и перепроверить в Excel, он не перенесёт расчёт.

### Р10. Объяснимость: докуда копать

- **Варианты.** (а) drill-down до введённых часов; (б) drill-down + **след расчёта**:
  какие правила пресета сработали, с какими входами и какой версией ставки;
  (в) только итоговая ведомость.
- **Рекомендация: (б).** Поиском не нашлось продукта, который это делает — а у нас
  правила уже лежат в конфиге с версионированием по датам, то есть след почти бесплатен.
  Это прямой ответ на «через полгода никто не вспомнит, откуда взялось 0,701».

### Р11. Как зарплата попадает в P&L

- **Суть.** R365 ведёт ежедневную оценку из POS и «true-up» фактом раз в период.
- **Варианты.** (а) только факт по закрытии периода; (б) оценка + факт со сторнированием.
- **Рекомендация: (а) сейчас, (б) заложить.** Ежедневный P&L по труду появится только
  после коннектора Dodo IS (пункт 6 порядка работ). Но счёт «начисленная зарплата»
  и понятие «период оценки» лучше завести сразу, иначе потом переписывать проводки.

### Р12. Позиционирование против франчайзинговых платформ

- **Суть.** Qvinci и подобные маппят чужой план счетов франчайзи в стандарт бренда,
  причём маппинг делают консультанты вендора.
- **Варианты.** (а) идти тем же путём (принимать выгрузки из чужих систем и маппить);
  (б) собирать первичку сразу в формате бренда; (в) оба.
- **Рекомендация: (б) как ядро, (в) как более поздний мост.** Наш заход сильнее ровно
  потому, что маппинга не возникает: нет чужого плана счетов. Это стоит записать в спеку
  как позиционирование, а не оставлять неявным.

---

## Что стоит украсть

Конкретные решения из чужих продуктов, которые имеет смысл повторить.

| Откуда | Что | Зачем нам |
|---|---|---|
| Restaurant365, вкладки *Distribution* / *Payroll Estimate Clearing* | Расхождение — **отдельная сущность с собственным экраном**, которая обязана схлопнуться в ноль | Готовая форма для «где разошлось с таблицей бухгалтера» |
| Crunchtime, «posting up» | Закрытие периода — **явное действие**, которое замораживает данные и триггерит пересчёт агрегатов | Убирает вопрос «когда цифры считаются окончательными» |
| Crunchtime, «COGS is COGS is COGS» | Правила расчёта централизованы на уровне бренда, а не на точке | Прямо наша цель: сопоставимые цифры по сети |
| Zoho Payroll | Явный конечный автомат: Draft → Payment Due → Paid, плюс Rejected; кнопка зависит от прав (*Submit and Approve* / *Submit for Approval*) | Готовая модель статусов ведомости |
| Zoho Payroll | Обратимость утверждённого периода отдельным действием, а не правкой в базе | Снимает главную жалобу класса — **добавить обязательную причину** |
| Zoho Payroll | «Add Hours for Additional Job Roles»: часы по каждой роли отдельно, автопересчёт после правки | У нас сотрудник может работать на двух точках/должностях |
| Gusto, *Review and Submit* | Одна сумма в **трёх раскрывающихся разрезах**: что удержано / что получил сотрудник / что заплатила компания | Наши разрезы: по регистрам, по точкам, по компонентам, «в P&L» vs «на руки» |
| Gusto, экран отпусков | Остаток по каждой политике **показан рядом с полем ввода** | Превышение лимита отпуска/больничного видно в момент ввода, а не на расчёте |
| NetSuite Weekly Timesheets | Прогрессивное раскрытие ячейки: одно число по умолчанию, плюсик раскрывает начало/конец/комментарий | Быстрый ввод не мешает детальному |
| NetSuite Weekly Timesheets | **Блокировка на уровне записи, а не табеля**; в утверждённый табель можно дописывать; правка требует переотправки | Один спорный сотрудник не держит весь месяц |
| Sage People (сторонний разбор) | Экран ревью показывает «total hours, exceptions, and **changes since last submission**» | Бухгалтер смотрит только изменения, а не 30 строк заново |
| Sage People | **Bulk-approve для чистых строк + drill-down для аномалий** | 27 из 30 строк тривиальны |
| Sage People | Структурные причины отказа из списка, а не свободный текст | Причины становятся статистикой: видно, где правила непонятны |
| Sage People | Напоминания T-48 / T-24 / T-8 до дедлайна | Месячный цикл легко проспать |
| Sage People | **Контрактный тест выгрузки перед каждым расчётом**: прогнать пробный файл через интерфейс | Дешёвая страховка от тихой поломки экспорта |
| PrismHR, Payroll Variance Analysis | Сравнение **Calculated против Finalized**, порог в *Amounts* или *Percentages*, поле *Breakdown* для разреза, drill-down до сотрудника и до **аудита изменений** | Наш главный экран сверки |
| Отраслевая практика variance | Разные пороги на разные компоненты («5% ок для overtime, не ок для оклада») | Иначе отчёт либо молчит, либо кричит на всё |
| Oracle HCM, retro pay | «Never overwrites historical data — it creates new entries instead»; дельта падает в период по Process Date | Закрытый период остаётся неприкосновенным |
| Oracle HCM, **от противного** | Ретро-строки визуально не отличаются от обычных | У нас — отличать и линковать на исходный период |
| Toast Payroll | «Закрыть табель» — отдельный шлюз перед расчётом, право только у админа | Ложится на нашу ролевую модель: управляющий вводит, бухгалтер закрывает |
| Xero | Клик по любой цифре отчёта ведёт в список транзакций, её составляющих | Цепочка P&L → ведомость → сотрудник → компоненты → часы → автор ввода |
| ADP, pre-payroll | Проверка «все наймы/увольнения учтены, у всех верная ставка» **до** расчёта | Чек-лист готовности периода как отдельный экран |
| Payroll register (ADP/Paychex/JD Edwards) | Формат «строка = сотрудник, колонки слева направо от gross к net» | Бухгалтер уже знает этот формат — не изобретать свой |
| A List Apart | `font-variant-numeric: lining-nums tabular-nums`, числа вправо, одинаковая точность, **без зебры**, группировка белым пространством | Базовая гигиена финансовой таблицы |
| Pencil & Paper | Переключатель плотности 40/48/56px, липкая шапка, **липкий футер с итогами**, заморозка левой колонки, боковая панель для деталей | Механика нашей главной таблицы |
| Linear | Три переменные темы вместо 98 (base / accent / contrast), LCH ради перцептивной равномерности | Мультиязычный интерфейс с тремя регистрами и так сложен — тема должна быть простой |
| Linear | Теней нет: глубина через лестницу поверхностей + hairline-границы | Даёт плотность, которая нужна финансовой таблице |
| Linear | Оптимистичные апдейты с откатом при ошибке | Ввод часов на 30 человек не должен ждать сервер на каждой ячейке |
| Ramp | Один акцентный цвет, появляющийся только «where money moves» | У нас акцент резервируем под **отклонение** и **действие**, больше ни на что |
| Ramp | «Show the numbers competitors hide»; осознанное трение в необратимых действиях | Утверждение периода — то самое место, где трение уместно |
| Ottimate, **от противного** | Отдельный e-mail на каждую точку вместо единого инбокса с маршрутизацией | Единый инбокс с разнесением по точкам — дешёвое преимущество |
| Ottimate | Автокодирование, обучающееся на истории, **плюс дешёвая ручная правка** | Классификация документов (пункт 7 порядка работ) без правок бесполезна |
