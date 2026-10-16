"""Тесты сборщика. Без сети и без базы: загрузка и Supabase подменяются заглушками.

Запуск из корня проекта:  python -m unittest
"""

import contextlib
import http.client
import io
import re
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import collector  # noqa: E402

# Условие events.url из db/001_init.sql
DB_URL_CHECK = re.compile(r"^https://\S+$")


def db_accepts(url: str) -> bool:
    return bool(DB_URL_CHECK.match(url)) and not url.endswith("/")


def source(**fields) -> dict:
    row = {"id": "test_src", "url": "https://example.ru/feed/", "settings": {},
           "baseline_done_at": None, "consecutive_errors": 0}
    row.update(fields)
    return row


def entry(link, title=None, published=None) -> dict:
    return {"title": title, "link": link, "published": published, "summary": None}


# ---------------------------------------------------------------- ссылки

class NormalizeUrlTest(unittest.TestCase):

    def test_tracking_params_fragment_and_slash_removed(self):
        self.assertEqual(
            collector.normalize_url("http://Habr.com/ru/articles/1/?utm_source=rss&hhtmFrom=x&from=y#c"),
            "https://habr.com/ru/articles/1")

    def test_habr_rss_link(self):
        self.assertEqual(
            collector.normalize_url("https://habr.com/ru/companies/alfa/articles/947754/"
                                    "?utm_source=habrahabr&utm_medium=rss&utm_campaign=corporate_blog"),
            "https://habr.com/ru/companies/alfa/articles/947754")

    def test_meaningful_query_kept(self):
        self.assertEqual(collector.normalize_url("https://a.ru/blog/?id=5&page=2"),
                         "https://a.ru/blog?id=5&page=2")

    def test_spaces_and_cyrillic_encoded(self):
        url = collector.normalize_url("https://a.ru/путь с пробелом/")
        self.assertEqual(url, "https://a.ru/%D0%BF%D1%83%D1%82%D1%8C%20%D1%81%20"
                              "%D0%BF%D1%80%D0%BE%D0%B1%D0%B5%D0%BB%D0%BE%D0%BC")
        self.assertTrue(db_accepts(url))

    def test_idempotent(self):
        for raw in ["https://a.ru/x%20y", "https://a.ru/путь", "https://a.ru/blog?id=5",
                    "https://nordclan.com/blog/ti2ta5n1r1-upravlenie"]:
            once = collector.normalize_url(raw)
            self.assertEqual(collector.normalize_url(once), once, raw)


class TitleFromUrlTest(unittest.TestCase):

    def test_last_segment_with_spaces(self):
        self.assertEqual(collector.title_from_url("https://a.ru/success-stories/ii-recruter-industry"),
                         "ii recruter industry")

    def test_encoded_segment_decoded(self):
        self.assertEqual(collector.title_from_url("https://a.ru/%D0%BA%D0%B5%D0%B9%D1%81"), "кейс")

    def test_no_path_falls_back_to_url(self):
        self.assertEqual(collector.title_from_url("https://a.ru"), "https://a.ru")


# ---------------------------------------------------------------- даты

class ParseDateTest(unittest.TestCase):

    def test_rfc822(self):
        self.assertEqual(collector.parse_date("Tue, 07 Oct 2026 10:00:00 +0300"),
                         "2026-10-07T10:00:00+03:00")

    def test_rfc822_gmt(self):
        self.assertEqual(collector.parse_date("Tue, 07 Oct 2026 10:00:00 GMT"),
                         "2026-10-07T10:00:00+00:00")

    def test_iso_with_z(self):
        self.assertEqual(collector.parse_date("2026-10-07T10:00:00Z"), "2026-10-07T10:00:00+00:00")

    def test_date_without_zone_is_utc(self):
        self.assertEqual(collector.parse_date("2026-10-07"), "2026-10-07T00:00:00+00:00")

    def test_unparseable_is_none(self):
        for value in [None, "", "   ", "garbage"]:
            self.assertIsNone(collector.parse_date(value), repr(value))


# ---------------------------------------------------------------- ленты

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>
  <title>Блог</title>
  <item>
    <title> Первая статья </title>
    <link>https://a.ru/blog/one/</link>
    <pubDate>Tue, 07 Oct 2026 10:00:00 GMT</pubDate>
    <description>Описание</description>
    <dc:creator>someone (KTS)</dc:creator>
  </item>
  <item turbo="true">
    <title>Кейс с Tilda</title>
    <link>https://r77.ai/cases/tpost/abc-case</link>
  </item>
</channel></rss>""".encode("utf-8")

ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry><title>A</title><link rel="self" href="https://a.ru/self"/><link href="https://a.ru/a"/>
         <published>2026-10-01T00:00:00Z</published><updated>2026-10-05T00:00:00Z</updated></entry>
  <entry><title>B</title><link rel="edit" href="https://a.ru/edit"/>
         <link rel="alternate" href="https://a.ru/b"/><updated>2026-10-02T00:00:00Z</updated></entry>
  <entry><title>C</title><link rel="self" href="https://a.ru/only-self"/></entry>
  <entry><title>D</title></entry>
</feed>"""


class ParseFeedTest(unittest.TestCase):

    def test_rss(self):
        entries = collector.parse_feed(RSS)
        self.assertEqual(len(entries), 2)
        self.assertEqual(entries[0], {"title": "Первая статья", "link": "https://a.ru/blog/one/",
                                      "published": "Tue, 07 Oct 2026 10:00:00 GMT",
                                      "summary": "Описание"})

    def test_rss_tilda_turbo_item(self):
        tilda = collector.parse_feed(RSS)[1]
        self.assertEqual(tilda["link"], "https://r77.ai/cases/tpost/abc-case")
        self.assertIsNone(tilda["published"])

    def test_author_not_taken(self):
        for e in collector.parse_feed(RSS):
            self.assertNotIn("someone", str(e))

    def test_atom_links(self):
        links = [e["link"] for e in collector.parse_feed(ATOM)]
        self.assertEqual(links, ["https://a.ru/a",          # без rel — alternate, self пропущен
                                 "https://a.ru/b",          # rel="alternate", edit пропущен
                                 "https://a.ru/only-self",  # другой ссылки нет
                                 None])

    def test_atom_published_preferred_over_updated(self):
        entries = collector.parse_feed(ATOM)
        self.assertEqual(entries[0]["published"], "2026-10-01T00:00:00Z")
        self.assertEqual(entries[1]["published"], "2026-10-02T00:00:00Z")

    def test_not_xml_raises(self):
        with self.assertRaises(collector.ET.ParseError):
            collector.parse_feed(b"<html><body>captcha")


# ---------------------------------------------------------------- строки events

class ToEventsTest(unittest.TestCase):

    def test_row_fields(self):
        rows, skipped = collector.to_events(
            [entry("https://a.ru/blog/one/", "Статья", "Tue, 07 Oct 2026 10:00:00 GMT")],
            source(settings={"default_signal": "case"}), "2026-10-16T00:00:00+00:00")
        self.assertEqual(skipped, 0)
        self.assertEqual(rows, [{
            "url": "https://a.ru/blog/one",
            "source_id": "test_src",
            "signal": "case",
            "verdict": "event",
            "reason": "правила классификации ещё не подключены",
            "title": "Статья",
            "published_at": "2026-10-07T10:00:00+00:00",
            "discovered_at": "2026-10-16T00:00:00+00:00",
            "is_baseline": True,
        }])

    def test_signal_defaults_to_article(self):
        rows, _ = collector.to_events([entry("https://a.ru/x")], source(settings=None), "now")
        self.assertEqual(rows[0]["signal"], "article")

    def test_baseline_only_on_first_run(self):
        rows, _ = collector.to_events([entry("https://a.ru/x")],
                                      source(baseline_done_at="2026-10-16T00:00:00+00:00"), "now")
        self.assertFalse(rows[0]["is_baseline"])

    def test_duplicates_collapsed(self):
        rows, _ = collector.to_events(
            [entry("https://a.ru/x/", "первая"), entry("http://A.ru/x?utm_source=rss", "вторая")],
            source(), "now")
        self.assertEqual([(r["url"], r["title"]) for r in rows], [("https://a.ru/x", "первая")])

    def test_relative_link_resolved_from_feed(self):
        rows, _ = collector.to_events([entry("/blog/rel")], source(), "now")
        self.assertEqual(rows[0]["url"], "https://example.ru/blog/rel")

    def test_unusable_links_skipped_and_counted(self):
        rows, skipped = collector.to_events(
            [entry(None), entry("mailto:x@example.ru"), entry("https://a.ru/ok")], source(), "now")
        self.assertEqual([r["url"] for r in rows], ["https://a.ru/ok"])
        self.assertEqual(skipped, 1)                      # без ссылки — не пропуск, а пустая запись

    def test_title_falls_back_to_url(self):
        rows, _ = collector.to_events([entry("https://a.ru/cases/tpost/abc-case")], source(), "now")
        self.assertEqual(rows[0]["title"], "abc case")

    def test_every_row_passes_db_check(self):
        links = ["https://a.ru/путь с пробелом/", "/relative", "http://a.ru/x/#y",
                 "  https://a.ru/spaces  ", "https://a.ru/?q=a b"]
        rows, _ = collector.to_events([entry(l) for l in links], source(), "now")
        self.assertEqual(len(rows), len(links))
        for r in rows:
            self.assertTrue(db_accepts(r["url"]), r["url"])


# ---------------------------------------------------------------- настройки

class LoadEnvTest(unittest.TestCase):

    def test_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("# комментарий\n\nA=1\nB = \"два\"\nC='3'\nD=x=y\nnot a pair\nE=\n",
                            encoding="utf-8")
            self.assertEqual(collector.load_env(path),
                             {"A": "1", "B": "два", "C": "3", "D": "x=y", "E": ""})

    def test_missing_file(self):
        self.assertEqual(collector.load_env(Path("нет_такого_файла.env")), {})


# ---------------------------------------------------------------- прогон

FEED = b"<rss><channel><item><title>t</title><link>https://x.ru/a</link></item></channel></rss>"


class FakeDB:
    """Заглушка Supabase: запоминает вызовы, падает там, где сказано."""

    def __init__(self, sources, fail_insert=(), fail_update=()):
        self.sources = sources
        self.fail_insert, self.fail_update = fail_insert, fail_update
        self.inserted, self.updates = [], []

    def active_rss_sources(self):
        return self.sources

    def insert_new_events(self, rows):
        if rows and rows[0]["source_id"] in self.fail_insert:
            raise RuntimeError("Supabase 400: bad row")
        self.inserted.extend(rows)
        return len(rows)

    def update_source(self, source_id, fields):
        if source_id in self.fail_update:
            raise urllib.error.URLError("supabase down")
        self.updates.append((source_id, fields))


class RunTest(unittest.TestCase):

    def run_collector(self, db, fetch):
        out = io.StringIO()
        with mock.patch.object(collector, "load_env",
                               return_value={"SUPABASE_URL": "u", "SUPABASE_SERVICE_ROLE_KEY": "k"}), \
             mock.patch.object(collector, "Supabase", return_value=db), \
             mock.patch.object(collector, "fetch", side_effect=fetch), \
             contextlib.redirect_stdout(out):
            code = collector.run()
        return code, out.getvalue()

    def test_success_marks_baseline_and_resets_errors(self):
        db = FakeDB([source(id="a", url="https://x.ru/1", consecutive_errors=2)])
        code, out = self.run_collector(db, lambda url: FEED)
        self.assertEqual(code, 0)
        self.assertEqual(len(db.inserted), 1)
        (sid, state), = db.updates
        self.assertEqual(sid, "a")
        self.assertEqual(state["consecutive_errors"], 0)
        self.assertIsNone(state["last_error"])
        self.assertIn("baseline_done_at", state)
        self.assertIn("немой прогон", out)

    def test_second_run_does_not_touch_baseline(self):
        db = FakeDB([source(id="a", baseline_done_at="2026-10-16T00:00:00+00:00")])
        self.run_collector(db, lambda url: FEED)
        self.assertNotIn("baseline_done_at", db.updates[0][1])
        self.assertFalse(db.inserted[0]["is_baseline"])

    def test_fetch_error_counts_and_keeps_events(self):
        def fetch(url):
            raise http.client.IncompleteRead(b"partial")
        db = FakeDB([source(id="a", consecutive_errors=1)])
        code, _ = self.run_collector(db, fetch)
        self.assertEqual(code, 1)
        self.assertEqual(db.inserted, [])
        (sid, state), = db.updates
        self.assertEqual(state["consecutive_errors"], 2)
        self.assertIn("IncompleteRead", state["last_error"])
        self.assertNotIn("last_success_at", state)

    def test_one_failing_source_does_not_stop_others(self):
        sources = [source(id="net", url="https://x.ru/1"),
                   source(id="xml", url="https://x.ru/2"),
                   source(id="ins", url="https://x.ru/3"),
                   source(id="ok", url="https://x.ru/4")]

        def fetch(url):
            if url.endswith("/1"):
                raise http.client.RemoteDisconnected("closed")
            if url.endswith("/2"):
                return b"<html>captcha"
            return FEED

        db = FakeDB(sources, fail_insert=("ins",))
        code, out = self.run_collector(db, fetch)
        self.assertEqual(code, 1)
        self.assertEqual([r["source_id"] for r in db.inserted], ["ok"])
        self.assertEqual([sid for sid, _ in db.updates], ["net", "xml", "ins", "ok"])
        self.assertIn("ошибок 3", out)

    def test_state_write_failure_does_not_stop_run(self):
        db = FakeDB([source(id="a", url="https://x.ru/1"), source(id="b", url="https://x.ru/2")],
                    fail_update=("a",))
        code, out = self.run_collector(db, lambda url: FEED)
        self.assertEqual(code, 1)
        self.assertEqual([sid for sid, _ in db.updates], ["b"])
        self.assertEqual(len(db.inserted), 2)
        self.assertIn("состояние не сохранено", out)

    def test_state_write_failure_after_fetch_error(self):
        def fetch(url):
            raise urllib.error.URLError("timeout")
        db = FakeDB([source(id="a", url="https://x.ru/1"), source(id="b", url="https://x.ru/2")],
                    fail_update=("a", "b"))
        code, out = self.run_collector(db, fetch)
        self.assertEqual(code, 1)
        self.assertIn("ошибок 2", out)

    def test_cannot_read_sources(self):
        db = FakeDB([])
        db.active_rss_sources = mock.Mock(side_effect=RuntimeError("Supabase 401: bad key"))
        code, out = self.run_collector(db, lambda url: FEED)
        self.assertEqual(code, 2)
        self.assertIn("Не удалось прочитать sources", out)

    def test_missing_settings(self):
        out = io.StringIO()
        with mock.patch.object(collector, "load_env", return_value={}), \
             contextlib.redirect_stdout(out):
            self.assertEqual(collector.run(), 2)
        self.assertIn("SUPABASE_URL", out.getvalue())


if __name__ == "__main__":
    unittest.main()
