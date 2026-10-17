-- Трендвотчер конкурентов — карты сайтов в таблице sources.
-- Ярус 2 из карты источников (sources.yaml, status: ok). id и адреса — как в sources.yaml.
-- Тип источника задаёт method: sitemap — карта (urlset), sitemap_index — индекс
-- со вложенными картами. Сборщик разбирает оба одной функцией и сам видит, что пришло.
-- Правила classify пока не переносим: сборщик их ещё не применяет, всё получает default_signal.
--
-- Первый прогон каждой карты немой (baseline_done_at пусто): адреса только запоминаются.
-- Безопасен для повторного запуска: строка с уже заведённым адресом пропускается.

insert into sources (id, company, kind, method, url, settings, enabled) values
    ('kts_site',        'KTS',         'site', 'sitemap',       'https://kts.tech/sitemap.xml',            '{"default_signal": "new_page"}', true),
    ('napoleonit_site', 'Napoleon IT', 'site', 'sitemap_index', 'https://napoleonit.ru/sitemap-index.xml', '{"default_signal": "new_page"}', true),
    ('justai_site',     'Just AI',     'site', 'sitemap_index', 'https://just-ai.com/sitemap.xml',         '{"default_signal": "new_page"}', true),
    ('agima_site',      'AGIMA',       'site', 'sitemap',       'https://www.agima.ru/sitemap.xml',        '{"default_signal": "new_page"}', true)
on conflict (url) do nothing;
