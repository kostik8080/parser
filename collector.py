"""Сборщик трендвотчера: RSS-ленты и карты сайтов из таблицы sources → таблица events в Supabase.

Запуск:  python collector.py
Настройки — в .env (шаблон: .env.example), исключения для карт сайтов — в sitemap_exclude.txt.
Схема базы — docs/db-schema.md. Только стандартная библиотека Python, внешних зависимостей нет.
"""

import email.utils
import gzip
import http.client
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

USER_AGENT = "trendwatcher/1.0"
TIMEOUT_SEC = 15

# Метки рекламных кампаний и переходов — в ссылке события не нужны (SPEC.md, раздел 1)
TRACKING_PARAMS = {"hhtmfrom", "from", "yclid", "gclid", "fbclid"}

ATOM = "{http://www.w3.org/2005/Atom}"

# То же условие, что у events.url в базе, плюс непустой хост. Строку, которая его
# не проходит, база отклонит вместе со всей пачкой источника — отсеиваем заранее.
VALID_URL = re.compile(r"^https://[^/\s]+\S*$")

# Сбои сети, HTTP, разбора XML и ответов Supabase. Ломают один источник, а не весь прогон.
SOURCE_ERRORS = (urllib.error.URLError, OSError, http.client.HTTPException,
                 ET.ParseError, ValueError, RuntimeError)


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


def load_exclusions(path: Path) -> list[str]:
    """Читает шаблоны служебных адресов: по одному в строке, после # — комментарий.
    Решётка в шаблоне не нужна: якорь из ссылки и так убирается."""
    if not path.exists():
        return []
    patterns = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip().lower()
        if line:
            patterns.append(line)
    return patterns


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

    def active_sources(self) -> list:
        """Включённые источники тех способов загрузки, которые сборщик умеет разбирать."""
        methods = ",".join(READERS)
        return self.request("GET", f"sources?enabled=eq.true&method=in.({methods})&select=*&order=id")

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
    без якоря и без завершающего слеша. Пробелы и кириллица в пути кодируются
    (%20, %D0%BF…), уже закодированное не трогается."""
    parts = urllib.parse.urlsplit(url.strip())
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = urllib.parse.quote(parts.path.rstrip("/"), safe="/%:@!$&'()*+,;=~")
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
        # Ссылка на материал — rel="alternate"; без rel она alternate по стандарту.
        # rel="self", "edit" и прочие ведут на служебные адреса ленты.
        links = item.findall(ATOM + "link")
        link = next((l for l in links if l.get("rel", "alternate") == "alternate"),
                    links[0] if links else None)
        entries.append({
            "title": text(item, ATOM + "title"),
            "link": link.get("href") if link is not None else None,
            "published": text(item, ATOM + "published") or text(item, ATOM + "updated"),
            "summary": text(item, ATOM + "summary"),
        })
    return entries


def parse_sitemap(xml: bytes) -> tuple[list[dict], list[str]]:
    """Карта сайта → (записи, адреса вложенных карт). У urlset берём адрес страницы
    и дату изменения (lastmod), у sitemapindex — адреса вложенных карт.
    Пространство имён не проверяем: встречаются карты и с другим, и без него."""
    root = ET.fromstring(xml)
    kind = local_name(root.tag)
    if kind not in ("urlset", "sitemapindex"):
        raise ValueError(f"не карта сайта: корневой элемент <{kind}>")
    entries, children = [], []
    for item in root:
        loc = child_text(item, "loc")
        if local_name(item.tag) == "url":
            entries.append({"title": None, "link": loc,
                            "published": child_text(item, "lastmod"), "summary": None})
        elif local_name(item.tag) == "sitemap" and loc:
            children.append(loc)
    return entries, children


def local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def child_text(el, name: str) -> str | None:
    """Текст дочернего элемента по имени без пространства имён. CDATA парсер раскрывает сам."""
    for child in el:
        if local_name(child.tag) == name and child.text and child.text.strip():
            return child.text.strip()
    return None


def fetch_sitemap(url: str) -> tuple[list[dict], list[str]]:
    data = fetch(url)
    if data[:2] == b"\x1f\x8b":                         # sitemap.xml.gz
        data = gzip.decompress(data)
    return parse_sitemap(data)


def is_excluded(url: str, patterns: list[str]) -> bool:
    """Служебный ли адрес. Шаблон ищется подстрокой в пути и параметрах в нижнем
    регистре; к пути добавлен завершающий /, чтобы шаблон /feed/ поймал и …/feed,
    но не …/feedback."""
    parts = urllib.parse.urlsplit(normalize_url(url))
    target = urllib.parse.unquote(parts.path).lower().rstrip("/") + "/"
    if parts.query:
        target += "?" + urllib.parse.unquote(parts.query).lower()
    return any(p in target for p in patterns)


# Сегмент-идентификатор, а не слова: число, хэш, код записи Tilda
ID_SEGMENT = re.compile(r"^(\d+|(?=[0-9a-f]*\d)[0-9a-f]{8,}|index|amp)$")
# Префикс-идентификатор перед словами: «ti2ta5n1r1-upravlenie» у записей Tilda
ID_PREFIX = re.compile(r"^(?=[a-z0-9]*\d)(?=[a-z0-9]*[a-z])[a-z0-9]{8,}-(?=.)")
PAGE_EXTENSION = re.compile(r"\.(html?|php|aspx?)$")


def title_from_url(url: str) -> str:
    """Запасной заголовок: последний осмысленный сегмент пути, дефисы и подчёркивания →
    пробелы (SPEC.md, раздел 1). Числа, хэши и index пропускаются, расширение .html
    и префикс-идентификатор Tilda отрезаются."""
    for segment in reversed(urllib.parse.urlsplit(url).path.split("/")):
        segment = PAGE_EXTENSION.sub("", urllib.parse.unquote(segment))
        if not segment or ID_SEGMENT.match(segment.lower()):
            continue
        segment = ID_PREFIX.sub("", segment)
        return segment.replace("-", " ").replace("_", " ").strip() or url
    return url


def to_events(entries: list[dict], source: dict, discovered_at: str) -> tuple[list[dict], int]:
    """Записи ленты → строки таблицы events и число пропущенных ссылок.
    Дубликаты ссылок внутри ленты схлопываются. Относительная ссылка достраивается
    от адреса ленты; ссылка, которую база не примет (не http, без хоста), пропускается —
    иначе из-за неё отклонилась бы вся пачка источника.
    Описание (summary) не пишем: поля для него в схеме нет."""
    signal = (source.get("settings") or {}).get("default_signal", "article")
    is_baseline = source.get("baseline_done_at") is None
    rows = {}
    skipped = 0
    for entry in entries:
        if not entry["link"]:
            continue
        url = normalize_url(urllib.parse.urljoin(source["url"], entry["link"].strip()))
        if not VALID_URL.match(url) or url.endswith("/"):
            skipped += 1
            continue
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
    return list(rows.values()), skipped


# ---------------------------------------------------------------- способы загрузки
# Каждый получает строку sources и список исключений, возвращает записи
# (title, link, published, summary) и число отсечённых служебных адресов.
# Дальше всё общее: to_events, upsert, состояние источника, вывод.

def read_rss(source: dict, exclusions: list[str]) -> tuple[list[dict], int]:
    return parse_feed(fetch(source["url"])), 0


def read_sitemap(source: dict, exclusions: list[str]) -> tuple[list[dict], int]:
    """Карта сайта целиком. Вложенные карты открываются на один уровень, глубже — нет.
    Сбой любой вложенной карты — сбой всего источника: иначе её адреса не попали бы
    в немой прогон и в следующий раз пришли бы разом как новые события.
    Событие — появление нового адреса; lastmod на новизну не влияет."""
    entries, children = fetch_sitemap(source["url"])
    for child in children:
        child_entries, _ = fetch_sitemap(urllib.parse.urljoin(source["url"], child))
        entries.extend(child_entries)
    kept = [e for e in entries if e["link"] and not is_excluded(e["link"], exclusions)]
    return kept, sum(1 for e in entries if e["link"]) - len(kept)


READERS = {"rss": read_rss, "sitemap": read_sitemap, "sitemap_index": read_sitemap}


# ---------------------------------------------------------------- прогон

def save_state(db: Supabase, source: dict, fields: dict) -> bool:
    """Пишет поля состояния источника. Сбой не прерывает прогон: остальные источники
    всё равно опрашиваются, а состояние запишется в следующий раз."""
    try:
        db.update_source(source["id"], fields)
        return True
    except SOURCE_ERRORS as e:
        print(f"{source['url']}  состояние не сохранено: {e}")
        return False


def run() -> int:
    env = load_env(Path(__file__).with_name(".env"))
    url, key = env.get("SUPABASE_URL"), env.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("Нет SUPABASE_URL или SUPABASE_SERVICE_ROLE_KEY в .env — см. .env.example")
        return 2

    db = Supabase(url, key)
    try:
        sources = db.active_sources()
    except SOURCE_ERRORS as e:
        print(f"Не удалось прочитать sources из Supabase: {e}")
        return 2
    exclusions = load_exclusions(Path(__file__).with_name("sitemap_exclude.txt"))

    total_found = total_written = errors = 0
    for source in sources:
        now = datetime.now(timezone.utc).isoformat()
        try:
            read = READERS.get(source.get("method"))
            if read is None:
                raise ValueError(f"способ загрузки {source.get('method')!r} не поддерживается")
            entries, excluded = read(source, exclusions)
            rows, skipped = to_events(entries, source, now)
            written = db.insert_new_events(rows)
        except SOURCE_ERRORS as e:
            errors += 1
            print(f"{source['url']}  ошибка: {e}")
            # Ошибка не трогает события источника (SPEC.md, раздел 2, правило 2)
            save_state(db, source, {
                "last_checked_at": now,
                "consecutive_errors": source["consecutive_errors"] + 1,
                "last_error": str(e)[:300],
            })
            continue

        state = {"last_checked_at": now, "last_success_at": now,
                 "consecutive_errors": 0, "last_error": None}
        if source.get("baseline_done_at") is None:
            state["baseline_done_at"] = now
        if not save_state(db, source, state):
            errors += 1

        total_found += len(entries)
        total_written += written
        note = f", отсечено служебных {excluded}" if excluded else ""
        note += f", пропущено ссылок {skipped}" if skipped else ""
        mark = "  (немой прогон)" if source.get("baseline_done_at") is None else ""
        print(f"{source['url']}  найдено {len(entries)}, записано {written}{note}{mark}")

    print(f"Итого: источников {len(sources)}, ошибок {errors}, "
          f"найдено {total_found}, записано {total_written}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(run())
