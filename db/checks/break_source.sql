-- Проверка устойчивости сборщика: ломает адрес одного источника.
-- Откат: db/checks/restore_source.sql.
-- Запуск: Supabase → SQL Editor, целиком.
--
-- Домен заменяется на зону .invalid — по RFC 6761 такие имена не существуют,
-- запрос гарантированно упадёт. Строка остаётся активной и проходит check по url.
-- Повторный запуск безопасен: если адрес уже сломан, ничего не меняется.
--
-- Ожидаемо после прогона сборщика: строка «… ошибка: …» по этому источнику,
-- в итоге «ошибок 1»; у строки обновятся last_checked_at и last_error,
-- consecutive_errors вырастет на 1, last_success_at и baseline_done_at не изменятся.

update sources
set url = replace(url, 'nordclan.com', 'nordclan.invalid')
where id = 'nordclan_blog';

select id, url, enabled, last_checked_at, last_success_at, consecutive_errors, last_error
from sources
where id = 'nordclan_blog';
