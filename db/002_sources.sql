-- Трендвотчер конкурентов — начальное наполнение таблицы sources.
-- Только ленты (ярус 1) со статусом «Проверен» из карты источников
-- (passport_and_sources.xlsx, лист «Карта источников»). Остальные ярусы — позже.
-- id — как в sources.yaml. Адреса перенесены из карты без изменений.
-- Поля состояния не заполняются: их проставит сборщик.
--
-- Безопасен для повторного запуска: строка с уже заведённым адресом пропускается.

insert into sources (id, company, kind, method, url, settings, enabled) values
    ('kts_blog_habr',        'KTS',           'articles', 'rss', 'https://habr.com/ru/rss/companies/kts/articles/',         '{"default_signal": "article"}', true),
    ('rmr_blog_habr',        'red_mad_robot', 'articles', 'rss', 'https://habr.com/ru/rss/companies/redmadrobot/articles/', '{"default_signal": "article"}', true),
    ('siberian_blog',        'Siberian.pro',  'articles', 'rss', 'https://siberian.pro/feed/',                              '{"default_signal": "article"}', true),
    ('simbirsoft_blog_habr', 'SimbirSoft',    'articles', 'rss', 'https://habr.com/ru/rss/companies/simbirsoft/articles/',  '{"default_signal": "article"}', true),
    ('agima_blog_habr',      'AGIMA',         'articles', 'rss', 'https://habr.com/ru/rss/companies/agima/articles/',       '{"default_signal": "article"}', true),
    ('r77_cases',            'R77 AI',        'cases',    'rss', 'https://r77.ai/rss-feed-599747680771.xml',                '{"default_signal": "case"}',    true),
    ('nordclan_blog',        'Nord Clan',     'articles', 'rss', 'https://nordclan.com/rss-feed-111193114321.xml',          '{"default_signal": "article"}', true)
on conflict (url) do nothing;
