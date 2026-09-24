#!/usr/bin/env python3
"""Тесты шага публикации в X (site/factory/post_x.py).

Сеть не используется: urlopen подменён. Проверяется ровно то, за что шаг отвечают:
длина по весам X, невзлом ссылок при обрезке, идемпотентность журнала, поведение
без секретов, dry-run, ретрай на 429 и постоянная ошибка на 4xx.
"""
import io
import json
import os
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "site", "factory"))

import post_x


def http_error(code, body=b"boom", headers=None):
    return urllib.error.HTTPError("https://api.twitter.com/2/tweets", code, "err",
                                  headers or {}, io.BytesIO(body))


class FakeResponse:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class TestWeights(unittest.TestCase):
    def test_url_counts_as_23_and_cyrillic_as_2(self):
        self.assertEqual(post_x.weighted_len("http://a.example/x"), post_x.URL_WEIGHT)
        self.assertEqual(post_x.weighted_len("abc"), 3)
        self.assertEqual(post_x.weighted_len("привет"), 6, "кириллица у X лёгкая: 1 за символ")
        self.assertEqual(post_x.weighted_len("🚀"), 2, "эмодзи — двойной вес")
        self.assertEqual(post_x.weighted_len("漢字"), 4, "CJK — двойной вес")

    def test_short_text_untouched(self):
        text = "заголовок\nhttps://example.com/a"
        out, truncated = post_x.clamp_digest(text)
        self.assertEqual(out, text)
        self.assertFalse(truncated)

    def test_long_digest_is_clamped_but_keeps_link(self):
        lines = ["Заголовок дайджеста"] + [f"{i}. Пул номер {i} вырос на {i}% за сутки, смотри график" for i in range(60)]
        lines.append("https://leo88q.github.io/content-/site")
        out, truncated = post_x.clamp_digest("\n".join(lines))
        self.assertTrue(truncated)
        self.assertTrue(post_x.fits(out), f"не влезло: {post_x.weighted_len(out)}")
        self.assertTrue(out.startswith("Заголовок дайджеста"), "первая строка обязана остаться")
        self.assertIn("https://leo88q.github.io/content-/site", out, "ссылка режется целой строкой, а не по буквам")

    def test_single_long_line_gets_ellipsis_not_overflow(self):
        out, truncated = post_x.clamp_digest("x" * 1000)
        self.assertTrue(truncated)
        self.assertTrue(post_x.fits(out))
        self.assertTrue(out.endswith("…"))


class TestPosting(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="postx-")
        self.digest = os.path.join(self.tmp, "2026-09-24.txt")
        with open(self.digest, "w", encoding="utf-8") as f:
            f.write("Заголовок\n1. Пул A +12%\nhttps://leo88q.github.io/content-/site")
        self.ledger = os.path.join(self.tmp, "x_posts.json")
        self.env = {
            "TWITTER_API_KEY": "ck", "TWITTER_API_SECRET": "cs",
            "TWITTER_ACCESS_TOKEN": "at", "TWITTER_ACCESS_SECRET": "ats",
        }
        patches = [
            mock.patch.dict(post_x.os.environ, self.env, clear=False),
            mock.patch.object(post_x, "LEDGER_PATH", self.ledger),
            mock.patch.object(post_x, "latest_digest", lambda day=None: (self.digest, "2026-09-24")),
            mock.patch.object(post_x, "latest_card_image", lambda: None),
            mock.patch.object(post_x.time, "sleep", lambda *_: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        for k in self.env:
            post_x.os.environ.pop(k, None)  # по умолчанию «секретов нет»

    def test_no_credentials_skips_without_network(self):
        with mock.patch.object(post_x.urllib.request, "urlopen",
                               side_effect=AssertionError("сеть недоступна в этом тесте")) as called:
            self.assertEqual(post_x.main([]), 0)
            self.assertEqual(called.call_count, 0)

    def test_dry_run_never_touches_network(self):
        post_x.os.environ.update(self.env)
        with mock.patch.object(post_x.urllib.request, "urlopen",
                               side_effect=AssertionError("dry-run не ходит в сеть")) as called:
            self.assertEqual(post_x.main(["--dry-run"]), 0)
            self.assertEqual(called.call_count, 0)
        self.assertFalse(os.path.exists(self.ledger), "dry-run не пишет в журнал")

    def test_post_then_skip_by_ledger(self):
        post_x.os.environ.update(self.env)
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return FakeResponse({"media_id_string": "m1"} if "upload" in req.full_url
                                else {"data": {"id": "111"}})

        with mock.patch.object(post_x.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(post_x.main([]), 0)
            self.assertEqual(post_x.main([]), 0, "второй прогон не должен падать")
        self.assertEqual(len([c for c in calls if "/2/tweets" in c]), 1, "твит должен быть один")
        with open(self.ledger, encoding="utf-8") as f:
            entries = json.load(f)["entries"]
        self.assertEqual(entries[0]["tweetId"], "111")
        self.assertTrue(entries[0]["url"].endswith("111"))

        # --force перебивает журнал
        with mock.patch.object(post_x.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertEqual(post_x.main(["--force"]), 0)
        self.assertEqual(len([c for c in calls if "/2/tweets" in c]), 2)

    def test_retry_on_429_then_success(self):
        post_x.os.environ.update(self.env)
        state = {"n": 0}

        def flaky(req, timeout=None):
            state["n"] += 1
            if state["n"] == 1:
                raise http_error(429, headers={"Retry-After": "1"})
            return FakeResponse({"data": {"id": "222"}})

        with mock.patch.object(post_x.urllib.request, "urlopen", side_effect=flaky):
            self.assertEqual(post_x.main(["--max-retries=2"]), 0)
        self.assertEqual(state["n"], 2, "429 обязан быть повторён")

    def test_permanent_4xx_fails_without_retry(self):
        post_x.os.environ.update(self.env)
        calls = {"n": 0}

        def bad(req, timeout=None):
            calls["n"] += 1
            raise http_error(401, body=b'{"detail":"Unauthorized"}')

        with mock.patch.object(post_x.urllib.request, "urlopen", side_effect=bad):
            self.assertEqual(post_x.main([]), 2)
        self.assertEqual(calls["n"], 1, "на 4xx долбить API нельзя")
        with open(self.ledger, encoding="utf-8") as f:
            self.assertIn("lastError", json.load(f))

    def test_secrets_never_in_output(self):
        post_x.os.environ.update({**self.env, "TWITTER_API_SECRET": "SUPER-SECRET"})
        buf_out, buf_err = io.StringIO(), io.StringIO()
        with mock.patch.object(post_x.urllib.request, "urlopen",
                               side_effect=lambda *a, **k: (_ for _ in ()).throw(http_error(400))), \
                mock.patch("sys.stdout", buf_out), mock.patch("sys.stderr", buf_err):
            code = post_x.main([])
        self.assertEqual(code, 2)
        self.assertNotIn("SUPER-SECRET", buf_out.getvalue() + buf_err.getvalue())


if __name__ == "__main__":
    unittest.main(verbosity=2)
