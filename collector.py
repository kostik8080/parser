"""Сборщик трендвотчера: RSS-ленты из таблицы sources → таблица events в Supabase.

Запуск:  python collector.py
Настройки — в .env (шаблон: .env.example). Схема базы — docs/db-schema.md.
Только стандартная библиотека Python, внешних зависимостей нет.
"""

import email.utils
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "trendwatcher/1.0"
TIMEOUT_SEC = 30

# Метки рекламных кампаний и переходов — в ссылке события не нужны (SPEC.md, раздел 1)
TRACKING_PARAMS = {"hhtmfrom", "from", "yclid", "gclid", "fbclid"}

ATOM = "{http://www.w3.org/2005/Atom}"


# ---------------------------------------------------------------- настройки

def load_env(path: Path) -> dict:
    """Читает KEY=VALUE из .env. Пустые строки и комментарии пропускает."""
    env = {}
    if not path.exists():
        return env
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


# ---------------------------------------------------------------- Supabase

class Supabase:
    """Минимальный клиент REST API Supabase (PostgREST) на urllib."""

    def __init__(self, url: str, key: str):
        self.base = url.rstrip("/") + "/rest/v1/"
        self.headers = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        }

    def request(self, method: str, path: str, body=None, prefer: str = ""):
        headers = dict(self.headers)
        if prefer:
            headers["Prefer"] = prefer
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            # Текст ошибки PostgREST объясняет причину лучше, чем «400 Bad Request»
            raise RuntimeError(f"Supabase {e.code}: {e.read().decode('utf-8', 'replace')[:300]}") from e
        return json.loads(raw) if raw else None

    def active_rss_sources(self) -> list:
        return self.request("GET", "sources?enabled=eq.true&method=eq.rss&select=*&order=id")

    def insert_new_events(self, rows: list) -> int:
        """Вставляет события; ссылки, которые уже есть в базе, пропускает. Возвращает число новых."""
        if not rows:
            return 0
        inserted = self.request(
            "POST", "events?on_conflict=url&select=url", rows,
            prefer="resolution=ignore-duplicates,return=representation",
        )
        return len(inserted or [])

    def update_source(self, source_id: str, fields: dict) -> None:
        self.request("PATCH", "sources?id=eq." + urllib.parse.quote(source_id), fields,
                     prefer="return=minimal")


# ---------------------------------------------------------------- лента

def fetch(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=TIMEOUT_SEC) as resp:
        return resp.read()


def normalize_url(url: str) -> str:
    """Канонический вид ссылки: https, хост в нижнем регистре, без меток кампаний,
    без якоря и без завершающего слеша."""
    parts = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = parts.path.rstrip("/")
    return urllib.parse.urlunsplit(
        ("https", parts.netloc.lower(), path, urllib.parse.urlencode(query), "")
    ).rstrip("/")


def parse_date(value: str | None) -> str | None:
    """RFC 822 (RSS) или ISO 8601 (Atom) → ISO-строка; при неудаче — None."""
    if not value:
        return None
    value = value.strip()
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def text(el, tag: str) -> str | None:
    found = el.find(tag)
    if found is None or found.text is None:
        return None
    return found.text.strip() or None


def parse_feed(xml: bytes) -> list[dict]:
    """Записи RSS 2.0 или Atom: заголовок, ссылка, дата публикации, описание.
    Автора не берём — это данные человека."""
    root = ET.fromstring(xml)
    entries = []
    for item in root.iter("item"):                       # RSS 2.0
        entries.append({
            "title": text(item, "title"),
            "link": text(item, "link"),
            "published": text(item, "pubDate"),
            "summary": text(item, "description"),
        })
    for item in root.iter(ATOM + "entry"):               # Atom
        link = item.find(ATOM + "link[@rel='alternate']")
        if link is None:
            link = item.find(ATOM + "link")
        entries.append({
            "title": text(item, ATOM + "title"),
            "link": link.get("href") if link is not None else None,
            "published": text(item, ATOM + "published") or text(item, ATOM + "updated"),
            "summary": text(item, ATOM + "summary"),
        })
    return entries


def title_from_url(url: str) -> str:
    """Запасной заголовок: последний сегмент пути, дефисы → пробелы (SPEC.md, раздел 1)."""
    segment = urllib.parse.urlsplit(url).path.rstrip("/").rsplit("/", 1)[-1]
    return urllib.parse.unquote(segment).replace("-", " ") or url


def to_events(entries: list[dict], source: dict, discovered_at: str) -> list[dict]:
    """Записи ленты → строки таблицы events. Дубликаты ссылок внутри ленты схлопываются.
    Описание (summary) не пишем: поля для него в схеме нет."""
    signal = (source.get("settings") or {}).get("default_signal", "article")
    is_baseline = source.get("baseline_done_at") is None
    rows = {}
    for entry in entries:
        if not entry["link"]:
            continue
        url = normalize_url(entry["link"])
        if url in rows:
            continue
        rows[url] = {
            "url": url,
            "source_id": source["id"],
            "signal": signal,
            "verdict": "event",
            "reason": "правила классификации ещё не подключены",
            "title": entry["title"] or title_from_url(url),
            "published_at": parse_date(entry["published"]),
            "discovered_at": discovered_at,
            "is_baseline": is_baseline,
        }
    return list(rows.values())


# ---------------------------------------------------------------- прогон

def run() -> int:
    env = load_env(Path(__file__).with_name(".env"))
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("Нет SUPABASE_URL или SUPABASE_SERVICE_ROLE_KEY в .env — см. .env.example")
        return 2

    db = Supabase(url, key)
    try:
        sources = db.active_rss_sources()
    except (urllib.error.URLError, OSError, RuntimeError) as e:
        print(f"Не удалось прочитать sources из Supabase: {e}")
        return 2

    total_found = total_written = errors = 0
    for source in sources:
        now = datetime.now(timezone.utc).isoformat()
        try:
            entries = parse_feed(fetch(source["url"]))
            written = db.insert_new_events(to_events(entries, source, now))
        except (urllib.error.URLError, OSError, ET.ParseError, ValueError, RuntimeError) as e:
            errors += 1
            # Ошибка не трогает события источника (SPEC.md, раздел 2, правило 2)
            db.update_source(source["id"], {
                "last_checked_at": now,
                "consecutive_errors": source["consecutive_errors"] + 1,
                "last_error": str(e)[:300],
            })
            print(f"{source['url']}  ошибка: {e}")
            continue

        state = {"last_checked_at": now, "last_success_at": now,
                 "consecutive_errors": 0, "last_error": None}
        if source.get("baseline_done_at") is None:
            state["baseline_done_at"] = now
        db.update_source(source["id"], state)

        total_found += len(entries)
        total_written += written
        mark = "  (немой прогон)" if source.get("baseline_done_at") is None else ""
        print(f"{source['url']}  найдено {len(entries)}, записано {written}{mark}")

    print(f"Итого: источников {len(sources)}, ошибок {errors}, "
          f"найдено {total_found}, записано {total_written}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run())
