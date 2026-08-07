"""
Учётки: таблица `users`, её политики и права роли приложения.

Почему учётка закрыта той же RLS, что и данные. В таблице лежат хэши паролей и
список того, кто вообще есть в системе; если бы `app_user` читал её целиком,
любая дыра в отчёте отдавала бы этот список наружу. Поэтому по умолчанию не
видно ни строки, а открываются ровно две:

- **своя** — `id = app_user_id()`: её читает Django на каждом запросе, сверяя
  сессию, и её же правит человек, меняя пароль;
- **та, которой сейчас входят** — `username = current_setting('app.login_username')`.
  Без этой ветки вход был бы невозможен: чтобы проверить пароль, нужно
  прочитать строку, а пользователь ещё не представился. Разрешение живёт одну
  транзакцию и выставляется только кодом входа (`web/auth.py`).

Ни `insert`, ни `delete` роли приложения не даны: учётки заводит и удаляет
администратор — тем же путём, что миграции. Права на них при этом отзываются
явно, а не «просто нет политики»: два запрета лучше одного.

Функции контекста здесь не участвуют, кроме `app_user_id()` — она читает
настройку сеанса и в таблицы не ходит. Это важно: политика, которая звала бы
`app_tenant_ids()`, добавила бы ещё одно место, где нужен владелец с правом
обходить RLS (issue #44).
"""
from django.db import migrations, models

POLICIES = """
alter table users enable row level security;
alter table users force row level security;

comment on table users is
    'Учётка человека: логин и хэш пароля. id тот же, что в memberships.user_id '
    'и в app.user_id — второй таблицы соответствий нарочно нет.';
comment on column users.username is 'Логин. Уникален во всей системе, а не внутри тенанта.';
comment on column users.password is 'Хэш штатного механизма Django. Пароля в базе нет.';
comment on column users.full_name is 'Как показывать человека в интерфейсе.';
comment on column users.is_active is 'Отключённая учётка не входит: увольнение, а не удаление.';

-- Своя строка: читать и править (пароль, отметка последнего входа).
create policy users_own_row on users
    for select using (id = app_user_id());

create policy users_change_own_row on users
    for update using (id = app_user_id()) with check (id = app_user_id());

-- Строка, которой прямо сейчас входят. Разрешение действует до конца
-- транзакции и выставляется только кодом входа.
create policy users_login_attempt on users
    for select using (
        nullif(current_setting('app.login_username', true), '') is not null
        and username = current_setting('app.login_username', true)
    );

revoke insert, delete on users from app_user;
grant select, update on users to app_user;
"""

BACKWARD = """
drop policy if exists users_login_attempt on users;
drop policy if exists users_change_own_row on users;
drop policy if exists users_own_row on users;
alter table users no force row level security;
alter table users disable row level security;
"""


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0009_payslip_ledgers'),
    ]

    operations = [
        migrations.CreateModel(
            name='User',
            fields=[
                ('password', models.CharField(max_length=128, verbose_name='password')),
                ('last_login', models.DateTimeField(blank=True, null=True, verbose_name='last login')),
                ('id', models.UUIDField(db_default=models.Func(function='gen_random_uuid'), primary_key=True, serialize=False)),
                ('username', models.TextField(unique=True)),
                ('full_name', models.TextField(db_default='')),
                ('email', models.TextField(db_default='')),
                ('is_active', models.BooleanField(db_default=True)),
                ('created_at', models.DateTimeField(db_default=models.Func(function='now'))),
            ],
            options={
                'db_table': 'users',
            },
        ),
        migrations.RunSQL(POLICIES, BACKWARD),
    ]
