-- Трендвотчер конкурентов — начальная схема базы.
-- Описание полей обычными словами: docs/db-schema.md.
-- Запуск: Supabase → SQL Editor, целиком, один раз.
--
-- Персональные данные не храним: в схеме нет полей для имён, ников,
-- телефонов, email и ссылок на профили людей. Сырые ответы источников не сохраняются.

begin;

-- ============================================================ sources
-- Одна строка — один адрес, который опрашивает сборщик.
-- Новый адрес заводится строкой, без правки программы.

create table sources (
    id                  text        primary key
                                    check (id ~ '^[a-z0-9_]+$'),
    company             text        not null,
    kind                text        not null
                                    check (kind in ('vacancies', 'articles', 'cases', 'site')),
    method              text        not null
                                    check (method in ('hh_api', 'rss', 'sitemap', 'sitemap_index')),
    url                 text        not null unique
                                    check (url ~ '^https://\S+$'),
    settings            jsonb       not null default '{}'
                                    check (jsonb_typeof(settings) = 'object'),
    note                text,

    -- состояние
    enabled             boolean     not null default true,
    last_checked_at     timestamptz,
    last_success_at     timestamptz,
    consecutive_errors  integer     not null default 0
                                    check (consecutive_errors >= 0),
    last_error          text,
    baseline_done_at    timestamptz,

    created_at          timestamptz not null default now()
);

comment on table  sources                    is 'Источники: адреса и их состояние. Описание — docs/db-schema.md';
comment on column sources.settings           is 'Настройки способа загрузки: employer_id, classify, default_signal';
comment on column sources.enabled            is 'Выключенный источник сборщик пропускает';
comment on column sources.last_checked_at    is 'Последнее обращение, с любым исходом';
comment on column sources.last_success_at    is 'Последний успешный ответ';
comment on column sources.consecutive_errors is 'Ошибок подряд; успех сбрасывает в 0';
comment on column sources.baseline_done_at   is 'Немой прогон пройден; пусто — следующий прогон событий не рождает';

-- ============================================================ digests
-- Одна строка — одна недельная сводка в Telegram.

create table digests (
    id                   bigint      generated always as identity primary key,
    period_start         date        not null unique,
    period_end           date        not null,
    body                 text        not null,
    created_at           timestamptz not null default now(),
    sent_at              timestamptz,
    telegram_message_id  bigint,

    check (period_end >= period_start),
    check (telegram_message_id is null or sent_at is not null)
);

comment on table  digests              is 'Отправленные сводки. chat_id получателя здесь не хранится — он в .env';
comment on column digests.period_start is 'Уникален: вторую сводку за ту же неделю база не примет';
comment on column digests.sent_at      is 'Пусто — собрана, но не доставлена';

-- ============================================================ events
-- Одна строка — один материал, включая шум. Ключ — каноническая ссылка:
-- повторный прогон с той же ссылкой вторую строку не создаст.
-- Обновления страниц не отслеживаются: lastmod не храним, page_changed нет.

create table events (
    url            text        primary key
                               check (url ~ '^https://\S+$' and url !~ '/$'),
    source_id      text        not null references sources (id),
    signal         text        not null
                               check (signal in ('vacancy_ai', 'vacancy_other', 'article',
                                                 'case', 'new_page')),
    verdict        text        not null
                               check (verdict in ('event', 'keep', 'noise')),
    reason         text,
    title          text        not null,
    published_at   timestamptz,
    discovered_at  timestamptz not null,   -- ставит сборщик; значения по умолчанию нет намеренно
    is_baseline    boolean     not null default false,
    duplicate_of   text        references events (url) on delete set null
                               check (duplicate_of <> url),

    -- поля материала
    city           text,
    experience     text,
    industry       text,
    co_companies   text[],

    digest_id      bigint      references digests (id) on delete set null,
    useful         boolean
);

create index events_source_id_idx    on events (source_id);
create index events_digest_id_idx    on events (digest_id);
create index events_duplicate_of_idx on events (duplicate_of);
-- кандидаты в ближайшую сводку
create index events_pending_idx      on events (discovered_at)
    where digest_id is null and verdict <> 'noise' and not is_baseline;

comment on table  events               is 'Всё, что видел сборщик, включая шум. Заодно список уже виденных ссылок';
comment on column events.url           is 'Каноническая ссылка: https, без utm_*/hhtmFrom, без / в конце';
comment on column events.published_at  is 'Дата публикации; пусто, если источник её не даёт';
comment on column events.discovered_at is 'Когда сборщик увидел впервые';
comment on column events.is_baseline   is 'Увидено в немом прогоне — запомнено, в сводку не идёт';
comment on column events.duplicate_of  is 'Та же статья из другого источника (dedup_groups) — ссылка на основную запись';
comment on column events.co_companies  is 'Компании-соавторы на Хабре. Только названия компаний, ники авторов не храним';
comment on column events.digest_id     is 'В какую сводку попало; пусто — ещё не отправлялось';
comment on column events.useful        is 'Метрика паспорта: переслал команде или обсудил';

-- ============================================================ доступ
-- Схема public открыта через API Supabase. RLS без политик: анонимный ключ
-- не видит ничего, сборщик работает с service_role-ключом, который RLS обходит.

alter table sources enable row level security;
alter table digests enable row level security;
alter table events  enable row level security;

commit;
