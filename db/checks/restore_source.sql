-- Откат db/checks/break_source.sql: возвращает исходный адрес источника.
-- Запуск: Supabase → SQL Editor, целиком.
-- Повторный запуск безопасен: если адрес уже исходный, ничего не меняется.
--
-- Возвращается только url. consecutive_errors и last_error сбросит
-- следующий успешный прогон сборщика — он же запишет last_success_at.

update sources
set url = replace(url, 'nordclan.invalid', 'nordclan.com')
where id = 'nordclan_blog';

select id, url, enabled, last_checked_at, last_success_at, consecutive_errors, last_error
from sources
where id = 'nordclan_blog';
