# Готовые решения: что не писать самим

Исследование фазы «готовые решения» для зарплатного модуля dodo_pnl_service.
Дата: 2026-08-06. Ограничение по всем находкам: **self-hosted на одной машине
(Windows + Docker), без облаков третьих лиц**.

Пометки о глубине чтения: **[читал целиком]** — прочитан README/код/док;
**[по заголовку]** — просмотрен список/описание, в детали не ходил.

---

## 1. Мультитенантность на Postgres RLS в Python

### Главный вывод: готовой «золотой» библиотеки под SQLAlchemy практически нет

Поиск по GitHub (`gh search repos "multi-tenant row level security sqlalchemy"`,
`"postgres RLS fastapi tenant"`, `"rls postgres python"`) даёт **пустые выдачи** или
репозитории с 0–3 звёздами. Экосистема Python по RLS+пул сильно беднее, чем
по Django-схемам. Значит: **паттерн переиспользуем, библиотеку — с осторожностью**.

### fastapi-rls — точное попадание по задаче, но 0 звёзд

- https://github.com/kdpisda/fastapi-rls · PyPI `fastapi-rls` 0.1.0 (2026-07-31)
- Лицензия BSD-3-Clause. Звёзд: **0**. Последний коммит 2026-08-01 (активен, но это
  первый релиз одного автора). Доки: https://fastapi-rls.com
- **[читал целиком]** README + список файлов репозитория.

Что даёт ровно под нас: политики объявляются на моделях SQLAlchemy 2.0
(`TenantPolicy("tenant_isolation", column="tenant_id")`), генерируются в DDL,
раскатываются либо CLI (`fastapi-rls sync/plan/audit`), либо директивами Alembic
(`op.enable_rls`, `op.create_policy`). Есть sync и async, FastAPI-dependency,
`RESTRICTIVE`-политики (`permissive=False`) — последнее нам понадобится для
регистров учёта поверх tenant-политики.

Репозиторий не вапорварь: 20 модулей пакета + `tests/integration/test_postgres_rls.py`,
`tests/security/test_sql_injection.py`, `tests/security/test_context_immutability.py`,
`examples/init/01-app-role.sql`.

**Риск:** 0 звёзд, один автор, версия 0.1.0, ноль внешних потребителей. Ставить в
фундамент мультитенантности — значит подписаться на его поддержку самим.

### Что именно переиспользуем — паттерн, описанный в его README (раздел «How it works»)

Это и есть ответ на вопрос «как не утечёт при пулинге» — четыре независимых слоя:

1. **Предикат политики оборачивает `current_setting` в скалярный подзапрос:**
   `tenant_id = (SELECT NULLIF(current_setting('rls.tenant_id', true), '')::integer)`.
   Не косметика: без подзапроса планировщик зовёт `current_setting` построчно.
   README называет это «the documented RLS performance pattern».
2. **`SET LOCAL` внутри транзакции на запрос**, а не `SET` на сессию. Коммит/роллбэк
   сбрасывает значение автоматически — соединение возвращается в пул чистым
   *по устройству Postgres*, а не потому что кто-то не забыл прибраться.
3. **Скраб на checkout из пула** (`reset_context_on_checkout=True`) — второй рубеж,
   на случай если п.2 где-то обошли.
4. **`FORCE ROW LEVEL SECURITY` + подключение не суперюзером.** Суперюзер и роль с
   `BYPASSRLS` игнорируют RLS полностью; владелец таблицы — тоже, если не `FORCE`.
   Это самая частая дыра: политики есть, а приложение ходит владельцем.

Плюс режим `require_context=True` — падать, если контекст не выставлен, вместо
тихой выдачи нуля строк. **Fail-closed по умолчанию у нас должен быть включён.**

**Контрапункт по `SET LOCAL`.** В чужом проекте нашлась прямо противоположная
инструкция: «**Never `SET LOCAL`** (it caused a prod 500; the RLS test asserts its
absence)» — https://github.com/Trending-Media-Service/Agency-OS,
`docs/plans/connections-c5-epic.md` **[по заголовку — фрагмент из code search]**.
Причина известна: `SET LOCAL` вне явной транзакции молча ничего не делает (Postgres
только предупреждает). То есть паттерн безопасен **строго при гарантии транзакции на
запрос**; если часть кода ходит в autocommit — контекст не выставится, а с
`require_context=False` это выглядит как «просто нет данных». Отсюда практическое
правило для нас: `set_config('rls.tenant_id', v, true)` (третий аргумент = local)
внутри явной транзакции **плюс** `require_context=True`, чтобы промах был громким.

### django-rls-tenants — зрелее, но привязан к Django ORM

- https://github.com/dvoraj75/django-rls-tenants · MIT · 13 звёзд ·
  последний коммит 2026-06-28 · PyPI `django-rls-tenants` · доки
  https://dvoraj75.github.io/django-rls-tenants/
- **[читал целиком]** README.

Полезен как **источник сравнительной таблицы механизмов изоляции** (RLS-политики vs
отдельные схемы `django-tenants` vs переписывание запросов ORM `django-multitenant`).
Ключевые строки: «Raw SQL protected» и «Fail-closed on missing context» есть только у
RLS. У `django-multitenant` fail-closed нет вовсе — то есть забытый фильтр = утечка.
Требует PostgreSQL >= 15 (у нас 17 — ок).

**Не подходит**, если бэкенд не на Django. Становится релевантным только как аргумент
в развилке «Django vs FastAPI» (см. «Развилки»).

### Что заведомо не берём

- **django-multitenant** (Citus) — https://github.com/citusdata/django-multitenant,
  последний релиз на PyPI **2023-12-18** (>2 лет — **заброшен по нашему критерию**).
  Механизм — переписывание запросов в ORM, не RLS: не fail-closed.
- **django-tenants** — https://github.com/django-tenants/django-tenants, MIT, живой
  (релиз 3.14.0 от 2026-08-05). Изоляция через **отдельную схему на тенанта**. Для нас
  плохо ложится: миграции × N тенантов, кросс-тенантные отчёты (консолидация P&L в EUR)
  становятся больно, роутинг соединений. Отвергаем осознанно, а не по незнанию.

### ⚠️ Побочная находка: в нашем `db/migrations/0003_rls.sql` не работает изоляция слоёв учёта

Не входило в задание, но всплыло при сверке найденного паттерна с нашим кодом.
Файл `/Users/garva/Documents/projects/dodo_pnl_service/db/migrations/0003_rls.sql`.

Первоисточник — документация PostgreSQL 17, `CREATE POLICY`
(https://www.postgresql.org/docs/17/sql-createpolicy.html) **[читал целиком, раздел о
комбинировании политик]**:
> «all the **PERMISSIVE** policy expressions are combined using **OR**, all the
> **RESTRICTIVE** policy expressions are combined using **AND**, and the results are
> combined using AND. […] Note that, for the purposes of combining multiple policies,
> **ALL policies are treated as having the same type** as whichever other type of
> policy is being applied.»

В миграции на `pay_components` и `allocation_rules` создаются две **пермиссивные**
политики (`AS PERMISSIVE` — умолчание):

```sql
create policy tenant_isolation on pay_components
    for all using (tenant_id in (select auth_tenant_ids())) ...;

create policy layer_visibility on pay_components
    for select using (layer = any (auth_visible_layers(tenant_id)));
```

На `SELECT` они складываются через **OR**, а не AND. Последствие:

**Ограничение по слою учёта не работает вообще.** Строка чёрного слоя в моём тенанте
проходит по `tenant_isolation` → `USING` вернул true → строка видна, независимо от
`layer_visibility`. То есть требование «**бухгалтер не видит чёрную кассу**» сейчас
не выполняется, хотя политика для него написана и выглядит рабочей. Это худший вид
бага: защита есть на бумаге и отсутствует в действительности.

**Проверено отдельно — меж-тенантной утечки здесь НЕ происходит.** Соблазнительно
заключить, что OR пускает и чужие строки, но нет: `auth_visible_layers(p_tenant)`
параметризована `tenant_id` **самой строки** и для чужого тенанта не находит
membership, возвращая `'{}'` через `coalesce`; `layer = any('{}')` → false.
Изоляция по тенанту устояла. Это не отменяет п. выше, но масштаб — «не работают слои
учёта», а не «текут тенанты».

Починка ровно та, что описана выше в паттерне: `create policy layer_visibility …
**as restrictive** for select using (…)`. Плюс два сопутствующих:

- политики навешиваются через `enable row level security`, но **без `force`** —
  владелец таблицы (а под ним обычно и ходит приложение, если роль не разделили)
  обходит RLS целиком;
- `auth.uid()` — функция Supabase. При уходе с Supabase (а мы уходим: self-hosted
  Postgres 17) её надо заменить на чтение сессионной переменной
  `current_setting('rls.user_id', true)`, иначе миграция просто не применится.

Отдельно стоит завести регрессионные тесты «строка чужого тенанта не видна» и
«строка невидимого слоя не видна» — у
`fastapi-rls` такие есть в `tests/integration/test_postgres_rls.py` и
`tests/security/test_context_immutability.py`, можно взять как образец постановки.

> Оформить это issue не получилось: **у репозитория нет git remote**
> (`git remote -v` пусто), беклога на GitHub пока не существует.
> Заводить беклог — отдельное действие, вынесено в «Развилки».

### alembic_utils — берём почти наверняка

- https://github.com/olirice/alembic_utils · PyPI `alembic-utils` 0.8.8 (2025-04-10)
- MIT. **[по заголовку]** — метаданные PyPI, README не читал целиком.

Alembic сам не умеет автогенерить не-табличные сущности Postgres. `alembic_utils`
добавляет функции, вью, триггеры и **политики** в `--autogenerate`. Без него
RLS-политики придётся вести руками в `op.execute()` и следить, чтобы код и БД не
разъехались. Если решим не брать `fastapi-rls`, это минимальная замена его
Alembic-части. Свежесть релиза — апрель 2025, чуть больше года: живой, но не бурлит.

## 2. Авторизация и роли без Supabase

Задачу надо разрезать надвое, иначе выбор не сходится:
**(A) аутентификация** — кто пользователь;
**(B) авторизация** — какие точки и какие регистры учёта ему видны.
Для (B) готовые IdP не годятся в принципе: это не «роль admin», а **набор строк данных**,
и решаться он должен там же, где RLS.

### (A) Аутентификация

#### FastAPI Users — ВАЖНО: переведён в maintenance mode

- https://github.com/fastapi-users/fastapi-users · **MIT** · **6209 звёзд** ·
  последний коммит 2026-07-20 · PyPI 15.0.5 (2026-03-27)
- **[читал целиком]** заголовок README и раздел Features.

Прямо в README, в самом верху:
> «**This project is now in maintenance mode.** While we'll continue to provide security
> updates and dependency maintenance, **no new features will be added**. […] We're
> currently working on a new Python authentication toolkit that will ultimately
> supersede FastAPI Users.»

Это меняет оценку. Функционально даёт ровно то, что нужно (готовые роуты
регистрации/логина/сброса пароля, куки или Authorization-заголовок, стратегии
JWT/database/Redis, бэкенд на SQLAlchemy async, подключаемая валидация пароля,
расширяемая модель пользователя). Но закладывать в фундамент библиотеку, которая
официально не будет развиваться и чей автор обещает преемника, — значит
запланировать миграцию. **Вердикт: использовать можно, но зная это; либо взять из
неё паттерн и не тащить зависимость.**

#### Authlib — если делегируем внешнему IdP

- https://github.com/authlib/authlib · **BSD-3-Clause** · 5392 звезды ·
  коммит 2026-07-30 · PyPI 1.7.2 (2026-05-06). **[по заголовку]**.

Клиент и сервер OAuth2/OIDC. Нужен, только если решим ставить отдельный IdP
(Keycloak/authentik) и ходить в него по OIDC. Сам по себе задачу «логин в продукт»
не закрывает — это протокольный слой.

#### Keycloak — мощно и тяжело

- https://github.com/keycloak/keycloak · **Apache-2.0** · **36036 звёзд** ·
  коммит 2026-08-06. **[по заголовку]**.
- Python-клиент: https://pypi.org/project/python-keycloak/ · MIT · 7.1.1 (2026-02-15).

Java/Quarkus, отдельный контейнер + своя БД. Даёт готовый UI логина, MFA, федерацию,
управление пользователями «из коробки» — сисадминская работа вместо программистской.
Расплата: JVM-контейнер на той же машине, где уже крутится Postgres 17 и наш бэкенд,
плюс отдельный контур обновлений и бэкапов.

#### authentik — тот же класс, известный аппетит

- https://github.com/goauthentik/authentik · лицензия **NOASSERTION** (GitHub не
  распознал; в описании GH стоит `other` — **лицензию надо проверить руками перед
  внедрением**, у authentik исторически MIT-ядро + отдельная Enterprise-часть) ·
  **22987 звёзд** · коммит 2026-08-06.
- **[читал целиком]** страницу требований
  https://docs.goauthentik.io/install-config/install/docker-compose/:
  > «Requirements: A host with at least **2 CPU cores and 2 GB of RAM**»

И это только под сам authentik: его compose поднимает server + worker + **собственный
PostgreSQL** + **Redis**. На одной домашней машине под Windows+Docker, где уже живёт
наш стек, это заметная доля ресурсов ради логина 30 человек.

#### Ory Kratos — headless, UI писать самим

- https://github.com/ory/kratos · **Apache-2.0** · **13814 звёзд** ·
  коммит 2026-07-29. **[по заголовку]** README (TOC + фрагмент фич).
- Косвенное, но решающее подтверждение: Ory держит отдельный репозиторий
  https://github.com/ory/kratos-selfservice-ui-react-nextjs — «**A full reference
  implementation for designing your own** login, registration, recovery, verification,
  … pages using Ory Kratos' APIs» (Apache-2.0, 161 звезда, коммит 2026-07-25).

То есть Kratos экономит серверную логику, но **экраны логина всё равно наши** — а это
как раз та часть, которую мы надеялись не писать. Плюс Next.js-фронт в стеке, который
мы хотим держать лёгким. Для 30 пользователей на тенанта размен не в нашу пользу.

#### Zitadel / Logto — упомянуть и отложить

- https://github.com/zitadel/zitadel · **AGPL-3.0** · 14660 звёзд · активен.
- https://github.com/logto-io/logto · **MPL-2.0** · 14297 звёзд · активен.
- **[по заголовку]** оба.

Оба заявляют мультитенантность из коробки и оба ставятся Docker-ом. AGPL у Zitadel
для внутреннего продукта не блокирует (нет распространения), но требует решения
юриста — лишний разговор. Logto под MPL-2.0 мягче. Держим как запасной вариант, если
Keycloak окажется тяжёл, а свой логин — узок.

### (B) Авторизация: точки + регистры учёта

#### Casbin (pycasbin) — живой, но не решает нашу задачу

- https://github.com/casbin/pycasbin · **Apache-2.0** · 1757 звёзд ·
  коммит 2026-08-06 (живой; PyPI 1.43.0 от 2025-05-10). **[по заголовку]**.

ACL/RBAC/ABAC по модели+политике. Проблема в том, что у нас правило доступа
**фильтрует строки**, а не разрешает действие: «этот пользователь видит точки {A,B} и
регистры {official, supplementary}». Если это живёт в Casbin, то фильтрация уезжает в
приложение — ровно то, от чего RLS должен был спасти, и появляются **два независимых
источника истины о доступе**. Классический способ получить расхождение.

#### oso — ЗАБРОШЕН/СНЯТ

- https://github.com/osohq/oso — описание репозитория теперь буквально
  «**Deprecated: See README**», последний push **2025-02-26**. Не рассматриваем.

#### Что делаем вместо: доступ выражаем теми же RLS-политиками

Механизм у нас уже есть из п.1 — сессионные переменные + политики. Роль
разворачивается в набор ключей контекста (`rls.tenant_id`, `rls.location_ids`,
`rls.ledgers`), а видимость — в `RESTRICTIVE`-политики поверх tenant-политики.
`fastapi-rls` это поддерживает напрямую: `CustomPolicy(using=…)`, `permissive=False`
и `registered_context_keys` (дополнительные ключи, которые чистятся при возврате
соединения в пул) — из его таблицы конфигурации в README **[читал целиком]**.

Ценность подхода: **бухгалтер не видит чёрную кассу** становится свойством базы, а не
дисциплиной каждого разработчика при написании очередного `SELECT`. Забытый фильтр в
новом отчёте не приводит к утечке — он приводит к пустому результату.

## 3. Версионирование правил по датам (temporal / bitemporal)

### Терминология, без которой разговор рассыпается

Из README `temporal_tables` **[читал целиком, раздел Introduction]**
(https://github.com/arkhipov/temporal_tables):

- **application period** (valid-time / business-time) — когда факт верен *в жизни*.
  Заполняется приложением. Это наши «ставка действует с 1 июля».
- **system period** (transaction-time) — когда строка была верна *в базе*.
  Заполняется системой. Это наш аудит правок.

Нам нужны **оба** и они не совпадают: «01.07 подняли ставку задним числом, узнали 20.07».
Это и есть **bitemporal**. Первое даёт правильный расчёт, второе — ответ на вопрос
«почему в утверждённой ведомости стояла другая цифра».

### Ключевой факт по версии Postgres: нативной поддержки у нас НЕТ

SQL:2011-темпоральные ограничения (`PRIMARY KEY ... WITHOUT OVERLAPS`, `UNIQUE ...
WITHOUT OVERLAPS`, `FOREIGN KEY ... PERIOD`) появились **только в PostgreSQL 18** —
release notes 18: «Temporal constraints, or constraints over ranges, for PRIMARY KEY,
UNIQUE, and FOREIGN KEY constraints. This is specified by WITHOUT OVERLAPS ... and by
PERIOD for foreign keys» (https://www.postgresql.org/docs/18/release-18.html).
Проверено: в документации PG **17** (`sql-createtable.html`) строки `WITHOUT OVERLAPS`
нет вообще (grep дал 0 вхождений). **[читал целиком — сверял по первоисточнику]**

Практический вывод: на заявленном PG 17 непересечение периодов действия правила
делается классикой — `btree_gist` + `EXCLUDE USING gist (scope_id WITH =, valid
WITH &&)` по `daterange`. Это работает с PG 9.2 и является тем же самым по смыслу.
Либо **поднять стенд до PG 18** и получить это в синтаксисе стандарта.

### `periods` — SQL:2011-периоды на старых Postgres

- https://github.com/xocolatl/periods · лицензия PostgreSQL · **318 звёзд** ·
  последний коммит **2025-10-08** (~10 месяцев — живой, но не бурлит)
- **[читал целиком]** первые 60 строк README (что такое период, add_period, unique).

Даёт `periods.add_period('example','validity','start_date','end_date')` и
периодные UNIQUE/PK/FK через функции+вью+триггеры, потому что расширение не может
менять грамматику. README заявляет совместимость **9.5–15** — про 16/17 не сказано,
это надо проверять фактически, прежде чем закладывать.

**Не берём как обязательную зависимость.** Причина простая: это C/SQL-расширение,
которое надо ставить в образ Postgres на self-hosted Windows+Docker. Одна кастомная
сборка образа ради синтаксического сахара над `EXCLUDE`. Берём **идею** (именованный
период + непересечение), реализуем на `EXCLUDE`, который есть в стоке.

### `temporal_tables` — system-time версионирование триггером

- https://github.com/arkhipov/temporal_tables · BSD-2-Clause · **1047 звёзд** ·
  последний коммит **2026-01-12** (живой)
- **[читал целиком]** README до раздела Installation.

Триггер архивирует старую версию строки в history-таблицу при UPDATE/DELETE.
Честно пишут: «Currently, Temporal Tables Extension supports the **system-period**
temporal tables only» — то есть application period (нашу «дату вступления в силу»)
он не закрывает вообще. Тоже C-расширение (PGXN/make install).

**Что берём:** только паттерн history-таблицы. **Не подходит** как решение задачи
«правило действует с даты» — он про другую половину bitemporal.

### `pg_bitemporal` — ЗАБРОШЕН

- https://github.com/scalegenius/pg_bitemporal · BSD-3-Clause · 163 звезды
- Последний **push — 2022-04-20**. Больше четырёх лет. По нашему критерию мёртв.
  (Осторожно: `gh search` показывает `updatedAt: 2026-06-12` — это метаданные репо,
  а не коммиты. Сверять надо `pushed_at`.) Не берём.

### `SQLAlchemy-Continuum` — аудит правок, не бизнес-даты

- https://github.com/sqlalchemy-continuum/sqlalchemy-continuum · PyPI 1.7.0,
  релиз **2026-07-03** (свежий) · доки https://sqlalchemy-continuum.github.io/
- **[по заголовку]** — метаданные PyPI и summary, код не читал.

Делает версионирование и аудит на уровне SQLAlchemy: версии строк + транзакционные
таблицы. Закрывает **system-time** половину (кто и когда поменял правило) без
C-расширения в образе Postgres. Это ровно то, чего не хватает `temporal_tables`
в self-hosted-ограничении.

**Оговорка перед принятием:** Continuum ловит изменения через ORM. Всё, что идёт
мимо ORM (миграции, ручной SQL, импорт), в историю не попадёт. Для «правил»
это приемлемо (они меняются только через интерфейс), для первичных данных — нет.

### Промышленный образец модели «слои правил» — Payroll Engine

Не библиотека, а **референс схемы**, и он попадает в нашу задачу почти дословно.
Из https://payrollengine.org/Concepts/PayrollModel/ **[читал целиком]**:

- объект `Payroll` собирает несколько **Payroll Layer**; порядок вычисления задаётся
  парой **level (первичная сортировка) + priority (вторичная)**; число слоёв не
  ограничено;
- «Objects from lower payroll layers can be **overridden using the override key**.
  The values of all layers are **dynamically merged into a single composed object**»;
- сливаются не только значения, но и локализации, атрибуты, кластеры, лукапы, тексты.

Это прямая калька нашего `страна → партнёр → группа → сотрудник`, только у них
`base → country → industry → company`. **Забираем два конкретных решения:**
(1) двухключевую сортировку `level + priority` вместо жёсткого перечисления уровней —
даёт вставить промежуточный уровень (например, «юрлицо») без миграции семантики;
(2) явный **override key** как способ адресовать перекрываемый объект, вместо
неявного матчинга по имени.

Их же принцип по времени, из README головного репозитория **[читал целиком]**:
«**Time model:** Traditional payroll software — *overwrites existing data*;
Payroll Engine — *Time-stamped values with full history*»
(https://github.com/Payroll-Engine/PayrollEngine). И в структуре доков есть отдельная
страница **Retro Corrections** и блог «Travel Through Time Data» — то есть
ретро-пересчёт у них first-class, а не заплатка.

## 4. Payroll-системы с открытым кодом: модель данных и процесс

### Payroll Engine — главная находка направления

- https://github.com/Payroll-Engine/PayrollEngine · **MIT** · 120 звёзд ·
  последний коммит **2026-07-14** (живой) · доки https://payrollengine.org
- Стек: **.NET 10 + SQL Server**, Docker-образы в ghcr.io.
- **[читал целиком]** README головного репозитория; страницы доков Payroll Model,
  Payrun Overview, Job Lifecycle, Retro Corrections.

Позиционирование дословно совпадает с нашим: «Open-source framework for
**regulation-driven** payroll applications — **multi-tenant, multi-country**, API-first».
И главный тезис: «**payroll logic is not hardcoded**. Instead, business rules are
defined in configurable regulation layers that can be stacked, overridden, and shared
between tenants» — это буквально наш принцип «страновая специфика живёт в
конфигурации, а не в коде».

**Код не берём** (C#/.NET + SQL Server против нашего Python + Postgres 17; тащить
SQL Server в self-hosted-стенд — отдельная инфраструктура ради чужого движка, а наш
движок уже написан и сходится с таблицей). **Берём модель данных и процесс.**

#### 4.1. Цикл периода — забираем статусную машину почти как есть

Из https://payrollengine.org/Concepts/Payrun/JobLifecycle/ **[читал целиком]**.
Статусы payrun job:

| Статус | Тип | Смысл |
|---|---|---|
| `Draft` | Working | Черновой прогон для просмотра |
| `Release` | Working | Отпущен в обработку |
| `Process` | Working | В обработке |
| `Complete` | Final | Успешно завершён |
| `Forecast` | Final | Прогнозный прогон |
| `Abort` | Final | Прерван **до** релиза |
| `Cancel` | Final | Упал **во время** обработки |

Два конкретных правила, которые стоит скопировать:

1. «For statutory payruns, **only one job in `Draft` status is allowed per payrun type
   and pay period**» — единственность черновика на период обеспечивается инвариантом
   в модели, а не дисциплиной пользователя. У нас это уникальный частичный индекс
   `unique (tenant_id, period_id) where status='draft'`.
2. **Разделение Abort и Cancel по фазе.** Мы бы на старте сделали один `cancelled` и
   потеряли различие «отменили сами до утверждения» vs «расчёт упал». Разные причины,
   разные действия оператора.

#### 4.2. Три типа прогона — то, до чего сами дошли бы не сразу

Preview / Forecast / Legal различаются **персистентностью**, а не флагом:

| | Preview | Forecast | Legal |
|---|---|---|---|
| Persistence | Non-persistent — только ответ API | Persistent, **отдельно** от боевых | Persistent, обязывающий |
| Сотрудников | ровно один | один и более | один и более |
| Прогонов на период | не ограничено | не ограничено | **один активный** |
| Видно в отчётах | нет | да (прогнозные) | да |
| Retro | HTTP 422, если нужен | полная поддержка | полная поддержка |

Preview («посчитай мне вот этого одного и ничего не пиши») — то, что нужно экрану
ввода часов: пересчитал строку — сразу видишь сумму, база не засоряется.
Forecast — «а если поднять ставку». Реализуется тем же движком, стоит дёшево, если
заложено в модель сразу, и дорого — если персистентность прибита гвоздями.

#### 4.3. Ретро-пересчёт закрытого периода — самая ценная страница

Из https://payrollengine.org/Concepts/Payrun/RetroCorrections/ **[читал целиком]**.

Механика: у каждого прогона есть **`evaluationDate`**, у каждого значения — **`created`**
(когда внесли) отдельно от `start`/`end` (за какой период). Движок сравнивает набор
значений, видимых на `evaluationDate` исходного прогона, с видимыми сейчас; при
расхождении заводит ретро-прогоны на затронутые прошлые периоды. Это работающая
bitemporal-схема, и она ровно отвечает на наше требование «закрытый период не
пересчитывается»: закрытый период **не переписывается** — он **перепрогоняется в
копию**, а разница садится в текущий период отдельной строкой.

`RetroValue = Σ (ConsolidatedValue(m) − OriginalValue(m))` за m = 1..текущий−1, где
`OriginalValue(m)` достаётся тем же запросом с `EvaluationDate = начало m+1`.

Три предостережения оттуда, каждое — сэкономленная неделя:

- **Ретро-разницу нельзя считать внутри расчёта периода.** Циклическая зависимость:
  коррекция зависит от сохранённых результатов, которые текущий прогон как раз
  сейчас и порождает. Плюс это дорогая мультипериодная агрегация в горячем пути.
  Считать надо отдельным проходом.
- **`storeEmptyResults: true`.** Если за период результата не сохранили, движок
  считает прошлое значение нулём и в коррекцию уходит **вся** пересчитанная сумма
  вместо дельты. То есть базовая строка нужна на каждого сотрудника в каждом
  периоде, даже нулевая. Классические грабли, на которые наступаешь в проде.
- **`RetroValue` и `YtdValue` — независимые колонки разными путями агрегации.**
  Не пытаться вывести одно из другого.

#### 4.4. Компоненты выплаты: wage types и collectors

Из https://payrollengine.org/Concepts/Payrun/Overview/ **[читал целиком]** —
рекомендуемый порядок моделирования, читается как готовая инструкция:

1. определить **выходные** значения (данные расчётного листа, обязательные по
   закону, выгрузки в смежные системы);
2. разложить их на **wage types** — по одному шагу расчёта на каждый — и
   **collectors** (агрегаты);
3. определить порядок обработки wage types;
4. определить collectors и приписать к ним wage types;
5. (опционально) кластеры/теги на wage types.

Мы это переносим как: строка ведомости = набор компонентов (`wage_type`), каждый со
своим порядком вычисления, плюс агрегаты (`collector`) поверх них — «начислено»,
«удержано», «к выплате». **Порядок обработки — атрибут данных, а не порядок строк в
коде.** Это то, что позволит правилам жить в БД.

Отдельно: страница **Period Totals** называется «Year-to-date accumulation — **why it
belongs in reports, not payruns**» **[по заголовку]**. Год-к-дате не хранить в
результате прогона, а считать в отчёте.

### Frappe HRMS — крупнейший живой open-source payroll

- https://github.com/frappe/hrms · **GPL-3.0** · **8510 звёзд** · коммит 2026-08-06
- **[по заголовку]** — метаданные репозитория, код и модель не читал.

Самый популярный. **GPL-3.0 — стоп для внутреннего продукта Dodo Brands**, если бы мы
брали код; как референс модели читать можно. Тянет за собой весь фреймворк Frappe
(своя ORM, свой UI, свой планировщик) — это не библиотека, это платформа.
**Не берём.** Годится только как источник наименований документов
(Salary Structure / Salary Slip / Payroll Entry), если понадобится сверить словарь.

### OCA/payroll (Odoo) — референс схемы «правило зарплаты»

- https://github.com/OCA/payroll · **AGPL-3.0** · 122 звезды · коммит 2026-07-28
- **[по заголовку]**.

Каноническая модель Odoo: `hr.salary.rule` (правило с условием и формулой),
`hr.payslip` (расчётный лист), `hr.payslip.line` (строка = компонент),
`hr.contract` (условия найма). AGPL — код не берём, словарь и связи полезны.
Наш движок уже написан, так что ценность ограничена именами сущностей.

### Ever Gauzy — не то

- https://github.com/ever-co/ever-gauzy · AGPL-3.0 · 4288 звёзд · активен.
  **[по заголовку]**. Полноценная ERP/CRM/HRM на NestJS+Angular. AGPL и масштаб
  платформы делают её неприменимой ни как код, ни как компактный референс.

### Horilla — тоже платформа, не библиотека

- https://github.com/horilla/horilla-hr · LGPL-2.1 · 1313 звёзд · коммит 2026-08-06.
  **[по заголовку]**. Django-приложение HR целиком. LGPL мягче GPL, но это законченный
  продукт, а не компонент. Может быть интересен только в сценарии «взять Horilla и
  допилить», который противоречит тому, что движок у нас уже свой и сходится.

## 5. Табличный ввод в вебе (Excel-подобный грид)

Требование: 30 сотрудников × типы часов, вставка из Excel, клавиатура, отмена —
и **без тяжёлого фронтенд-стека**. Значит критерии: (1) лицензия допускает
коммерческое использование, (2) работает как обычный `<script>` на серверном шаблоне,
без сборки React/Angular, (3) Excel-фичи в бесплатной части.

### Tabulator — рекомендация

- https://github.com/tabulator-tables/tabulator · **MIT** · **7738 звёзд** ·
  коммит **2026-08-06** (очень живой) · доки http://tabulator.info
- **[читал целиком]** README (setup) + **фактический листинг модулей** через
  GitHub API: `src/js/modules`.

Подключение — ровно то, что нужно под серверные шаблоны, без сборки:
```html
<link href="dist/css/tabulator.min.css" rel="stylesheet">
<script src="dist/js/tabulator.min.js"></script>
```
```js
var table = new Tabulator("#example-table", {});
```

Решающий аргумент — **список модулей в исходниках** (все под MIT, ничего не
отрезано в платную часть):

`Clipboard` · `SelectRange` · `Spreadsheet` · `Keybindings` · `Edit` · `Validate` ·
`History` · `Localize` · `Import` · `Export` · `Download` · `ColumnCalcs` ·
`FrozenColumns` · `FrozenRows` · `Persistence`

То есть в бесплатной MIT-части лежат ровно те четыре вещи, ради которых люди обычно
покупают лицензию: **вставка из буфера**, **выделение диапазона**, **отмена/повтор**
(`History`) и **валидация ячеек**. Плюс `ColumnCalcs` (итоги по колонке — «всего часов
за месяц» бесплатно) и `Localize` — **встроенная локализация грида**, что напрямую
закрывает часть требования ru/en/sr (см. п.6). README прямо говорит: «built to work
with all the major front end JavaScript frameworks» — то есть фреймворк опционален,
а не обязателен.

### Jspreadsheet CE — альтернатива, если нужен именно «лист Excel»

- https://github.com/jspreadsheet/ce · **MIT** · **7212 звёзд** ·
  коммит **2026-04-10** (живой). **[по заголовку]** — метаданные и описание.

Позиционируется как «lightweight JavaScript data grid component … with advanced
spreadsheet controls». Ближе к метафоре листа, чем Tabulator (который всё-таки
таблица данных). **Осторожность:** это Community Edition при существующей коммерческой
Pro-версии — набор фич в CE надо сверять по факту перед выбором, а не по описанию.

### RevoGrid — если захочется веб-компонент

- https://github.com/revolist/revogrid · **MIT** · 3431 звезда ·
  коммит 2026-08-04. **[по заголовку]**.

Собран как web component (Stencil), поэтому втыкается в любой стек как обычный тег.
Меньше сообщество, чем у Tabulator, при сопоставимых задачах — берём только если
упрёмся в производительность на больших объёмах (грид виртуализованный).

### Handsontable — НЕЛЬЗЯ, платная лицензия

- https://github.com/handsontable/handsontable · 22005 звёзд, активен, но лицензия
  на GitHub — `NOASSERTION`. **[читал целиком]** `LICENSE.txt`:
  > «dual-licensed — depending on whether your use for **commercial purposes**, meaning
  > intended for or resulting in commercial advantage or monetary compensation, or not.
  > If your use is **strictly personal or solely for evaluation purposes** … […]
  > Your use of this software for **commercial purposes is subject to the terms included
  > in an applicable license agreement**.»

Внутренний продукт коммерческой компании — это коммерческое использование.
**Требует покупки.** Самый популярный ответ в интернете на наш вопрос — и он нам
не подходит. Хорошо, что проверили лицензию, а не звёзды.

### AG Grid — Excel-фичи именно в платной части

- https://github.com/ag-grid/ag-grid · лицензия `NOASSERTION`, 15520 звёзд, активен.
- **[читал целиком]** `LICENSE.txt` корня: «There are two license types: MIT and
  Commercial. […] The following packages are MIT licensed: … `ag-grid-community` …
  The following packages are **Commercial licensed**: `ag-grid-enterprise`».
- **Проверено по факту, а не по маркетингу** — листинг `packages/ag-grid-enterprise/src`
  через GitHub API содержит каталоги: **`clipboard`**, **`rangeSelection`**,
  **`excelExport`**, `batch-edit`, `formula`, `find`, `pivot`, `masterDetail`.

То есть ровно «как в Excel» (вставка из буфера, выделение диапазона, экспорт в xlsx)
в AG Grid — **платная часть**. Community сгодился бы для read-only таблицы, но не для
нашего экрана ввода. Не берём.

### Отсеяно

- **x-spreadsheet** — https://github.com/myliang/x-spreadsheet, 14591 звезда, но
  описание репозитория: «The project has been **migrated** to @wolf-table/table»,
  последний push **2024-08-07** (~2 года). Мёртв в этом виде.
- **Univer** — https://github.com/dream-num/univer, Apache-2.0, 14059 звёзд, активен.
  «full-stack framework for creating and editing spreadsheets / word processor /
  presentation». **[по заголовку]**. Это Google Sheets целиком, а нам нужна форма
  ввода на 30 строк. Кратно избыточно.
- **Glide Data Grid** — https://github.com/glideapps/glide-data-grid, MIT, 5294 звезды,
  коммит 2026-01-21 (полгода — живой, но темп упал). **[по заголовку]**. Требует React
  («outrageously fast **react** data grid»), а это ровно тот тяжёлый фронт, которого
  мы избегаем. Не берём, пока не решим уходить в React.

### Побочно: чтение xlsx — у нас `openpyxl`, и он на грани по свежести

В `pyproject.toml` уже стоит `openpyxl>=3.1`, на нём написан
`src/payroll/importers/plata_xlsx.py`.

- **openpyxl** — MIT, PyPI 3.1.5, релиз **2024-06-28** (два года назад — ровно на
  границе нашего критерия «заброшено»). Репозиторий не на GitHub, а на Heptapod
  (https://foss.heptapod.net/openpyxl/openpyxl). Проверил по их API: последние коммиты
  — **2025-10-01**, то есть разработка идёт, но **релизов больше года нет**.
  **[читал целиком — лента коммитов через API Heptapod]**
- **python-calamine** — https://github.com/dimastbk/python-calamine · MIT ·
  PyPI 0.8.2 (**2026-07-13**, свежий). **[по заголовку]**. Биндинг к растовой
  `calamine`, читает xlsx/xls/ods, кратно быстрее и устойчивее к кривым файлам.
  Только чтение — записи нет.
- **XlsxWriter** — https://github.com/jmcnamara/XlsxWriter · BSD-2-Clause ·
  PyPI 3.2.9 (2025-09-16). **[по заголовку]**. Только запись, но быстрее и с
  форматированием лучше openpyxl. Кандидат под «выгрузку ведомости».

Менять сейчас ничего не нужно — импортер работает и тест сходится. Но при
расширении на «формат у каждого партнёра свой» стоит развести: чтение — `calamine`,
запись — `XlsxWriter`, а `openpyxl` оставить только там, где нужен доступ к
формулам/стилям существующего файла.

## 6. i18n для Python-веба

### Ответ короткий: gettext + Babel. Альтернативы проиграли по свежести

#### Babel — берём

- https://github.com/python-babel/babel · **BSD-3-Clause** · 1460 звёзд ·
  коммит **2026-07-31** (живой) · PyPI 2.18.0 (2026-02-01)
- **[читал целиком]** https://babel.pocoo.org/en/latest/messages.html

Явная позиция из документации: «As gettext provides a solid and well supported
foundation for translating application messages, **Babel does not reinvent the wheel**,
but rather reuses this infrastructure, and makes it easier to build message catalogs
for Python applications». То есть Babel — не конкурент gettext (который лежит в
стандартной библиотеке Python), а тулинг вокруг него.

Рабочий цикл, описанный там же: `xgettext`-подобное извлечение → POT → копия под
локаль → PO → `msgfmt` → MO → `msgmerge` при обновлениях. Важное отличие от `xgettext`:
«the routines for message extraction in **Babel operate on directories**», а не на
одном файле, и умеют расширяться на другие форматы файлов — то есть шаблоны в
извлечение попадают тем же проходом, что и Python.

**Второе, за что берём Babel — и это часто забывают:** он несёт данные CLDR, то есть
форматирование дат, чисел и **валют** по локали. Для нас это не украшение: сербский
формат числа `1.234,56` и вывод RSD/EUR — ровно та часть, где хардкод в шаблоне
превращается в баг в ведомости.

#### Jinja2 — извлечение из шаблонов уже есть, проверено по исходникам

- https://github.com/pallets/jinja · BSD-3-Clause · 11724 звезды ·
  последний коммит **2025-06-14** (~год — живой, но темп низкий; для настолько
  стабильной библиотеки это нормально).
- **[читал целиком — по исходнику]** `src/jinja2/ext.py`: в файле присутствуют
  `class InternationalizationExtension`, `def babel_extract(...)` (строка 752),
  `def extract_from_ast(...)`, алиас `i18n = InternationalizationExtension`,
  а также `_make_new_gettext`/`_make_new_ngettext`/`_make_new_pgettext`/
  `_make_new_npgettext` — то есть контекстные формы (`pgettext`) поддержаны.

Практический вывод: связка «Jinja2 + `jinja2.ext.i18n` + `pybabel extract`» —
не рецепт из блога, а функция в коде библиотеки. Тег `{% trans %}` извлекается
Babel'ом из коробки. Ничего писать не надо.

`pgettext` пригодится буквально: «Period» как «период расчёта» и «Period» как «точка
в конце предложения» переводятся по-разному, а в зарплатной ведомости таких коротких
многозначных слов много.

#### Project Fluent — НЕ берём, рантайм протух

- https://github.com/projectfluent/python-fluent · лицензия `NOASSERTION` (Apache-2.0
  по метаданным PyPI) · 243 звезды · репозиторий пушился 2026-06-01
- **но** PyPI-пакет `fluent.runtime` — версия **0.4.0, релиз 2023-03-16**.
  Это **больше трёх лет** без выпуска. **[по заголовку]**.

Fluent как формат лучше gettext на сложной морфологии (а сербский с падежами —
как раз такой случай). Но ставить в продукт рантайм, который три года не
выпускался, нельзя. Расхождение «репозиторий живой, релиза нет» — типичный признак
проекта на поддержке. Отмечаем и проходим мимо.

#### Фронтенд: два разных слоя, не путать

1. **Тексты интерфейса** — рендерятся сервером из тех же PO/MO. При серверных
   шаблонах отдельного фронтового i18n-стека **не нужно вообще**. Это главный
   аргумент в пользу серверного рендеринга для нашего случая (см. «Развилки»).
2. **Тексты внутри грида** (кнопки, «нет данных», подписи пагинации) — у Tabulator
   свой модуль `Localize` (см. п.5, подтверждено листингом `src/js/modules`).
   Его словарь заполняется из наших же переводов при рендере страницы.

Отдельно: **язык демо-стенда по глобальному правилу — всегда английский**,
независимо от языка продукта. То есть ru/en/sr нужны в продукте, а демо
показывается только на `en`.

## 7. Демо-режим и сид данных

### Генерация правдоподобных данных

#### Faker — берём

- https://github.com/joke2k/faker · **MIT** · **19359 звёзд** ·
  коммит **2026-08-03** (очень живой) · PyPI 40.36.0 (2026-07-24). **[по заголовку]**
  README; **[читал целиком]** — фактический листинг локалей через GitHub API.

**Конкретная проверка по нашей стране.** `faker/providers/person` содержит **86**
каталогов локалей. Полный список: `ar_AA ar_DZ ar_PS ar_SA az_AZ bg_BG bn_BD cs_CZ
da_DK de_AT de_CH de_DE de_LI de_LU el_GR en en_GB en_IE en_IN en_KE en_NG en_NZ
en_PK en_TH en_US es es_AR es_CA es_CL es_CO es_ES es_MX et_EE fa_IR fi_FI fr_BE
fr_CA fr_CH fr_DZ fr_FR fr_QC ga_IE gu_IN ha_NG he_IL hi_IN hr_HR hu_HU hy_AM id_ID
ig_NG is_IS it_IT ja_JP ka_GE ko_KR lt_LT lv_LV mk_MK mr_IN ne_NP nl_BE nl_NL no_NO
or_IN pl_PL pt_BR pt_PT ro_RO ru_RU sk_SK sl_SI sv_SE sw ta_IN th_TH tr_TR tw_GH
uk_UA uz_UZ vi_VN yo_NG zh_CN zh_TW zu_ZA`.

**Сербской локали (`sr_RS`) в Faker НЕТ.** Есть соседние `hr_HR`, `sl_SI`, `mk_MK`,
`bg_BG`. То же самое в `company` и `address` — только `ru_RU` из интересующего нас
ряда. Для нас это **не блокер**, а наоборот удача: демо по глобальному правилу
делается **на английском**, то есть `en_US` и есть нужная локаль. Но если для тестов
захочется сербских имён — придётся либо брать `hr_HR` как ближайшее (латиница,
похожая антропонимика), либо писать свой провайдер. Знать это лучше сейчас, чем
на этапе «а почему у нас в сербском демо люди зовутся John Smith».

#### Mimesis — альтернатива, тот же пробел

- https://github.com/lk-geimfari/mimesis · **MIT** · 4836 звёзд · коммит **2026-08-05**
  (живой) · PyPI 21.0.0 (2026-07-16). **[читал целиком]** — листинг
  `mimesis/datasets/*` через API.

Локали: `ar-* az bin cs da de-at de-ch de el en-au en-ca en-gb en es-mx es et fa fi
fr global hr hu int is it ja kk ko nl-be nl no pl pt-br pt ru sk sv tr uk zh`.
**Сербского тоже нет.** Mimesis быстрее Faker и типизирован, но у Faker кратно больше
провайдеров и сообщества. При отсутствии `sr` у обоих различие теряет вес — **берём
Faker** как более распространённый.

#### factory_boy / polyfactory — не для демо-сида

- https://github.com/FactoryBoy/factory_boy · MIT · PyPI 3.3.3 (**2025-02-03**,
  ~1,5 года без релиза). **[по заголовку]**.
- https://github.com/litestar-org/polyfactory · MIT · PyPI 3.3.0 (2026-02-22,
  свежий). **[по заголовку]**.

Обе — фабрики объектов для **тестов**, а не сид для стенда. Полезны в `tests/`, но
демо-данные должны быть **связными** (сотрудники → часы → расчёт → ведомость сходится),
а фабрики генерируют объекты независимо. Демо-сид всё равно пишется руками поверх
Faker для отдельных полей.

### Самовосстанавливающийся стенд: механизм есть в самом Postgres

Поиски по GitHub (`gh search repos "demo environment reset seed"`,
`"sandbox demo data reset postgres"`, `"seed demo data idempotent"`) дали **пустые
выдачи**. Готовой библиотеки под «самовосстанавливающееся демо» не существует —
это паттерн, а не пакет.

Правильный механизм — **шаблонные базы Postgres**, из документации PG 17
https://www.postgresql.org/docs/17/manage-ag-templatedbs.html **[читал целиком]**:
> «`CREATE DATABASE` actually works by **copying an existing database**. By default,
> it copies the standard system database named `template1`. […] By instructing
> `CREATE DATABASE` to copy `template0` instead of `template1`, you can create a
> "**pristine**" user database».

Отсюда паттерн, который ничего не стоит написать и который работает секунды, а не
минуты:

1. Один раз собрать эталон: пустая база → миграции → демо-сид (Faker, английский) →
   прогон зарплатного периода, чтобы данные были **уже посчитанные**, а не сырые.
   Это база `demo_template`.
2. Сброс = `DROP DATABASE demo; CREATE DATABASE demo TEMPLATE demo_template;`
   Копирование файлов на уровне СУБД — быстрее любого прогона сидера, и результат
   **побайтово одинаковый** каждый раз (никакого дрейфа от накопленных правок).
3. Триггер сброса — по расписанию/простою, **автоматически**, не кнопкой.
   На нашем стенде это уже решённая задача: планировщик Windows там используется
   для бэкапов Postgres, тот же механизм подойдёт.

Ограничение, о котором надо знать заранее: `CREATE DATABASE ... TEMPLATE X`
**требует, чтобы к X не было других подключений**. Значит перед сбросом надо
разорвать сессии демо-базы (`pg_terminate_backend`), а сам `demo_template` держать
закрытым от приложения. Также документация отдельно предупреждает, что
`CREATE DATABASE` **не копирует database-level GRANT** — права на демо-базу надо
выдавать заново после каждого пересоздания. Это ровно тот шаг, который забудут и
получат «демо не открывается по понедельникам».

**Альтернатива, если полное пересоздание окажется неудобным:** демо как отдельный
`tenant_id` в общей базе + периодический `DELETE`+пересид в транзакции. Дешевле по
инфраструктуре, но **опаснее**: демо-данные оказываются в одной базе с боевыми, и
единственное, что их разделяет, — те самые RLS-политики. При ошибке в политике
пересид может задеть не тот тенант. Изоляция отдельной базой такого класса ошибок
не допускает вообще.

---

## Развилки

Ниже — выборы, которые придётся сделать до начала стройки. Формат: суть / варианты /
рекомендация с обоснованием.

### Р1. Чем прокидывать tenant-контекст в RLS

**Суть.** Ставить `fastapi-rls` в фундамент или написать ~150 строк своих по его же
паттерну.

**Варианты.**
- **A. Зависимость `fastapi-rls`.** Готовы политики-как-код, Alembic-директивы, CLI
  `sync/plan/audit` (дрейф схемы ловится автоматом), скраб пула, тесты на утечку.
  Цена: **0 звёзд, версия 0.1.0, один автор** — при заброшенности мы наследуем
  поддержку критичного для безопасности кода.
- **B. Свой тонкий слой.** `set_config('rls.tenant_id', v, true)` в зависимости
  FastAPI внутри явной транзакции + `event.listens_for(pool, "checkin")` на скраб +
  политики руками в SQL-миграциях. Плюс `alembic_utils` (MIT, зрелая), чтобы
  политики попадали в autogenerate.
- **C. Django + `django-rls-tenants`.** MIT, 13 звёзд, живой, fail-closed из коробки,
  админка и i18n Django бесплатно. Цена — переезд всего бэкенда на Django.

**Рекомендация: B, читая `fastapi-rls` как эталон.** Причина не в снобизме: RLS —
единственная линия обороны между тенантами и между слоями учёта, и половина её
у нас **уже не работает** (см. побочную находку в п.1). Класть эту линию на пакет без единого
внешнего потребителя — менять один риск на другой. При этом весь ценный материал у
`fastapi-rls` в README и тестах, и он забирается бесплатно: скалярный подзапрос вокруг
`current_setting`, `SET LOCAL` в транзакции, скраб на checkout, `FORCE RLS`,
не-суперюзерская роль, `require_context=True`.
Вариант A остаётся живым, если к моменту старта у пакета появятся сторонние
потребители. Вариант C — только если победит Р5 в пользу Django.

### Р2. Версия PostgreSQL: 17 или 18

**Суть.** Темпоральные ограничения (`WITHOUT OVERLAPS`, FK `PERIOD`) есть **только в
PG 18**; в 17 их нет.

**Варианты.**
- **A. Остаться на 17**, непересечение периодов действия правил делать через
  `btree_gist` + `EXCLUDE USING gist (scope_id WITH =, valid WITH &&)`.
- **B. Поднять стенд до 18** и писать стандартным синтаксисом, с темпоральными
  внешними ключами в придачу.

**Рекомендация: A на старте, B как плановое обновление.** `EXCLUDE` даёт тот же
инвариант и работает с PG 9.2 — то есть решение не зависит от версии стенда и не
блокирует стройку. Но записать в план обновление до 18: темпоральные **FK** руками на
`EXCLUDE` не делаются, а они понадобятся, когда правило начнёт ссылаться на другое
версионированное правило (ставка → группа → юрлицо). Главное — **не тащить
C-расширения** (`periods`, `temporal_tables`, `pg_bitemporal`): каждое означает свою
сборку образа Postgres на Windows+Docker, а два из трёх ещё и не закрывают нашу
половину задачи (`temporal_tables` — только system-time, `pg_bitemporal` мёртв с 2022).

### Р3. Аутентификация: свой логин или отдельный IdP

**Суть.** Ставить Keycloak/authentik рядом или держать логин в приложении.

**Варианты.**
- **A. Свой** (сессии/JWT в приложении, роли и членства в наших же таблицах).
  Один процесс, один бэкап, нулевой оверхед. Цена — сброс пароля, MFA, блокировка
  перебора пишутся руками.
- **B. Keycloak / authentik.** Готовый UI, MFA, федерация. Цена измерена:
  authentik официально требует **2 CPU + 2 GB RAM** и поднимает свой Postgres и Redis;
  Keycloak — JVM-контейнер со своей БД. На одной домашней машине, где уже живут
  Postgres 17 и наш бэкенд, это заметная доля.
- **C. Ory Kratos.** Легче по ресурсам, но headless — экраны логина всё равно наши
  (Ory сам держит отдельный репозиторий-референс UI). Экономия не там, где болит.

**Рекомендация: A, с оговоркой на будущее.** Пользователей — десятки, не тысячи; это
внутренний продукт за периметром, а не публичный SaaS. Три контейнера ради логина 30
человек не окупаются, и каждый — отдельный контур обновлений на машине, которую
нельзя надолго выключать. Ключевое проектное требование: **держать
аутентификацию за узким интерфейсом** (`get_current_principal() -> Principal`), чтобы
подмена на OIDC через `Authlib` была заменой одной реализации, а не переписыванием.
`fastapi-users` не берём как зависимость — он официально в maintenance mode и автор
обещает преемника; но его набор роутов годится как список того, что надо не забыть.

### Р4. Где живёт авторизация: в RLS или в отдельном движке политик

**Суть.** «Роль даёт набор точек и набор видимых регистров» — это фильтр строк или
проверка прав?

**Варианты.**
- **A. Всё в RLS.** Роль разворачивается в ключи контекста, видимость —
  `RESTRICTIVE`-политики поверх tenant-политики.
- **B. Casbin** (Apache-2.0, живой) поверх приложения.

**Рекомендация: A, однозначно.** При B фильтрация уезжает в код приложения и
появляются **два источника истины о доступе**, которые обязательно разъедутся. При A
забытый фильтр в новом отчёте даёт пустой результат, а не утечку. `oso`, который был
бы третьим вариантом, помечен автором как Deprecated и отпадает.
**Критично:** политики видимости слоёв обязаны быть `AS RESTRICTIVE` — пермиссивные
складываются через OR и не сужают, а **расширяют** доступ (см. побочную находку в п.1).

### Р5. Фронтенд: серверные шаблоны или SPA

**Суть.** От этого зависят сразу i18n, грид и объём работы.

**Варианты.**
- **A. Серверный рендер (Jinja2) + Tabulator + HTMX-подход.** i18n целиком в gettext/
  Babel на сервере, отдельного фронтового i18n-стека нет вообще. Грид — MIT, все
  Excel-фичи в бесплатной части.
- **B. SPA (React/Vue) + API.** Гибче, но появляется второй контур переводов, сборка,
  и грид с полным Excel-поведением придётся либо покупать (Handsontable, AG Grid
  Enterprise), либо брать Glide Data Grid и тянуть React.

**Рекомендация: A.** Продукт — формы ввода и ведомости, а не интерактивное приложение.
Tabulator под MIT закрывает всё, ради чего обычно покупают лицензию: `Clipboard`,
`SelectRange`, `History` (отмена), `Validate`, `ColumnCalcs`, `Localize` — проверено
по листингу модулей, а не по маркетингу. При B тот же набор в AG Grid лежит в
`ag-grid-enterprise` (каталоги `clipboard`, `rangeSelection`, `excelExport` — проверено
по исходникам), а Handsontable требует коммерческой лицензии по тексту `LICENSE.txt`.
Telegram-клиент по плану всё равно идёт поверх собственного API, так что API появится
и без SPA.

### Р6. Как сбрасывается демо-стенд

**Суть.** Демо должно **само** возвращаться к чистому наполненному виду.

**Варианты.**
- **A. Отдельная база + `CREATE DATABASE demo TEMPLATE demo_template`.** Сброс —
  копирование файлов средствами СУБД, секунды, побайтово одинаковый результат.
- **B. Отдельный `tenant_id` в общей базе + `DELETE` и пересид.**

**Рекомендация: A.** B ставит демо-данные в одну базу с боевыми, и единственное, что
их разделяет, — те самые RLS-политики, в которых мы только что нашли неработающую
половину. Ошибка в политике при пересиде задевает не тот тенант. A такого класса ошибок не допускает
физически. Два пункта, которые надо заложить сразу, иначе демо сломается тихо:
перед сбросом рвать соединения к `demo` (`pg_terminate_backend`), а после
пересоздания **заново выдавать GRANT** — документация PG прямо предупреждает, что
`CREATE DATABASE` не копирует database-level GRANT.
Планировщик Windows на стенде уже используется для бэкапов Postgres — тот же
механизм подходит под расписание сброса.

### Р7. Насколько буквально копировать модель Payroll Engine

**Суть.** Их модель попадает в нашу задачу почти дословно, но это .NET + SQL Server.

**Варианты.**
- **A. Взять их модель целиком** (payrun job со статусами, три типа прогона,
  `evaluationDate`, wage types + collectors, слои `level`+`priority`+override key).
- **B. Взять только статусы периода**, остальное проектировать самим.

**Рекомендация: A по перечисленным пяти узлам, без остального.** Причина: каждый из
пяти — не архитектурный вкус, а **ответ на грабли**, которые у них уже задокументированы
как пройденные. Особенно три: ретро-разницу нельзя считать внутри расчёта периода
(циклическая зависимость + дорогая агрегация в горячем пути); `storeEmptyResults` —
без базовой строки за период коррекция уезжает на всю сумму вместо дельты; YTD
считается в отчётах, а не хранится в результате прогона. Их код (C#/SQL Server) не
берём — наш движок уже написан и сходится с таблицей до копейки.

### Р8. Беклог: его сейчас негде вести

**Суть.** У репозитория **нет git remote** — беклога на GitHub не существует, а
найденная дыра в RLS не может быть оформлена issue.

**Варианты.**
- **A. Завести приватный репозиторий на GitHub** и перенести туда находки как issues.
- **B. Продолжать локально.**

**Рекомендация: A, до начала стройки.** Это не бюрократия: находка уровня «изоляция
слоёв учёта не работает» из отчёта, который прочитали и закрыли, теряется — а она
касается одного из трёх заявленных принципов продукта. Первым issue — починка
`0003_rls.sql` (`AS RESTRICTIVE`, `FORCE RLS`, замена `auth.uid()`), вторым —
регрессионные тесты на меж-тенантную видимость и на видимость слоёв.

---

## Сводка: что берём, что нет

| Направление | Берём | Не берём и почему |
|---|---|---|
| RLS-мультитенантность | **паттерн** `fastapi-rls` + `alembic_utils` (MIT) | сам `fastapi-rls` (0 звёзд); `django-multitenant` (заброшен, не fail-closed); `django-tenants` (схема на тенанта) |
| Аутентификация | свой слой за узким интерфейсом; `Authlib` про запас | Keycloak/authentik (2 CPU + 2 GB и свои Postgres/Redis); Kratos (UI всё равно наш); `fastapi-users` (maintenance mode) |
| Авторизация | `RESTRICTIVE`-политики в RLS | Casbin (второй источник истины); oso (Deprecated) |
| Версионирование по датам | `btree_gist` + `EXCLUDE`; модель слоёв Payroll Engine; `SQLAlchemy-Continuum` на аудит | `periods`/`temporal_tables` (C-расширения в образ); `pg_bitemporal` (мёртв с 2022) |
| Payroll-модель | модель и процесс **Payroll Engine** (MIT) | их код (.NET+SQL Server); frappe/hrms (GPL-3.0, платформа); OCA/payroll, Gauzy (AGPL) |
| Табличный ввод | **Tabulator** (MIT, все Excel-фичи бесплатны) | Handsontable (коммерческая лицензия); AG Grid (Excel-фичи в Enterprise); x-spreadsheet (мёртв); Univer (избыточен) |
| i18n | **gettext + Babel + `jinja2.ext.i18n`**; `Localize` у Tabulator | `fluent.runtime` (релиза нет с 2023) |
| Демо | **Faker** (`en_US`) + шаблонная база Postgres | `factory_boy`/`polyfactory` (это про тесты); готовой библиотеки «демо-сброс» не существует |
| xlsx | оставить `openpyxl`; при росте — `calamine` на чтение, `XlsxWriter` на запись | — |
