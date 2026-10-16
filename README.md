# parser

Трендвотчер конкурентов: сборщик RSS-лент в Supabase. Подробности — `SPEC.md`,
схема базы — `docs/db-schema.md`.

Только стандартная библиотека Python 3.10+.

```
python collector.py       # прогон: ленты из таблицы sources → таблица events; настройки в .env
python -m unittest        # тесты, без сети и без базы
```
