# parser

Трендвотчер конкурентов: сборщик RSS-лент и карт сайтов в Supabase. Подробности — `SPEC.md`,
схема базы — `docs/db-schema.md`.

Только стандартная библиотека Python 3.10+.

```
python collector.py       # прогон: ленты и карты сайтов из таблицы sources → таблица events; настройки в .env,
                          # служебные адреса карт сайтов — в sitemap_exclude.txt
python -m unittest        # тесты, без сети и без базы
```
