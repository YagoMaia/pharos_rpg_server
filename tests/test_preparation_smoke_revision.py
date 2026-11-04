"""O smoke deve enviar a revisão realmente lida, sem assumir que é 1."""

import unittest
from unittest.mock import patch

from tools.preparation_smoke import check_apply_needs_a_human


class FakeResponse:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class FakeRest:
    base = "https://localhost"
    token = "session"

    def call(self, method, path):
        assert method == "GET"
        assert path.endswith("/preparation/drafts/draft-1")
        return FakeResponse(200, {"revision": 7})


class FakeHttpClient:
    def __init__(self, **_kwargs):
        self.posts = []

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, path, json, headers):
        self.posts.append((path, json, headers))
        if headers["Authorization"] == "Bearer prep":
            return FakeResponse(403, {}, "aprovação humana")
        return FakeResponse(201, {}, "aplicado")

    def get(self, _path, headers):
        return FakeResponse(200, [{"name": "Ferrarin"}])


class FakeReporter:
    def __init__(self):
        self.results = []

    def check(self, _name, passed, _detail=""):
        self.results.append(passed)


class SmokeRevisionTests(unittest.TestCase):
    def test_apply_uses_the_current_reviewed_draft_revision(self):
        client = FakeHttpClient()
        report = FakeReporter()
        with patch("tools.preparation_smoke.httpx2.Client", return_value=client):
            check_apply_needs_a_human(FakeRest(), {"token": "prep"}, "camp-1", "draft-1", report)
        self.assertEqual(len(client.posts), 2)
        self.assertTrue(all(post[1]["expectedRevision"] == 7 for post in client.posts))
        self.assertTrue(all(report.results))
