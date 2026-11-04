"""Limites compartilhados entre workers, sem derivar o PIN após esgotar a cota."""

import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.core.database import open_connection
from app.main import app
from app.routers.campaigns import get_campaign_store
from app.services import campaign_store
from app.services.campaign_store import CampaignStore


class AdjustableClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class TableLoginLimitsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = CampaignStore(Path(self.directory.name) / "limits.sqlite3")
        self.store.set_table_pin("Victor", "pin-de-teste-victor")
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        self.client = TestClient(app, client=("192.0.2.10", 1234))

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        self.directory.cleanup()

    def login(self, name: str = "Victor", peer: str = "192.0.2.10") -> int:
        with TestClient(app, client=(peer, 1234)) as client:
            return client.post(
                "/auth/table-login", json={"name": name, "pin": "pin-incorreto"}
            ).status_code

    def test_eleventh_attempt_is_denied_before_deriving_the_pin(self) -> None:
        for _ in range(10):
            self.assertEqual(self.login(), 401)
        with patch.object(campaign_store, "_verify_secret") as verify:
            response = self.client.post(
                "/auth/table-login",
                json={"name": "Victor", "pin": "pin-de-teste-victor"},
            )
        self.assertEqual(response.status_code, 429, response.text)
        self.assertGreater(int(response.headers["Retry-After"]), 0)
        verify.assert_not_called()

    def test_name_limit_ignores_case_spaces_and_changes_of_peer(self) -> None:
        for _ in range(10):
            self.assertEqual(self.login(), 401)
        self.assertEqual(self.login(" vIcToR ", "192.0.2.11"), 429)
        self.assertEqual(self.login("Gabriel", "192.0.2.11"), 401)

    def test_peer_limit_cannot_be_bypassed_by_changing_names_or_headers(self) -> None:
        for index in range(30):
            self.assertEqual(self.login(f"desconhecido-{index}"), 401)
        response = self.client.post(
            "/auth/table-login",
            json={"name": "outro-desconhecido", "pin": "pin-incorreto"},
            headers={"X-Forwarded-For": "192.0.2.99", "CF-Connecting-IP": "192.0.2.98"},
        )
        self.assertEqual(response.status_code, 429, response.text)

    def test_successful_logins_also_consume_the_quota(self) -> None:
        for _ in range(10):
            response = self.client.post(
                "/auth/table-login",
                json={"name": "Victor", "pin": "pin-de-teste-victor"},
            )
            self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.login(), 429)

    def test_window_expires_without_sleeping_and_old_counters_are_removed(self) -> None:
        from app.services.table_login_throttle import (
            TableLoginThrottle,
            TableLoginThrottled,
        )

        clock = AdjustableClock()
        throttle = TableLoginThrottle(self.store.path, clock=clock)
        for _ in range(10):
            throttle.consume("Victor", "192.0.2.10")
        clock.now += 59
        with self.assertRaises(TableLoginThrottled) as denied:
            throttle.consume("Victor", "192.0.2.11")
        self.assertEqual(denied.exception.retry_after, 1)
        clock.now += 1
        throttle.consume("Victor", "192.0.2.11")
        with closing(open_connection(self.store.path)) as conn:
            stale = conn.execute(
                "SELECT COUNT(*) FROM table_login_attempts WHERE expires_at <= ?",
                (clock.now,),
            ).fetchone()[0]
        self.assertEqual(stale, 0)

    def test_concurrent_workers_cannot_exceed_the_shared_name_quota(self) -> None:
        from app.services.table_login_throttle import (
            TableLoginThrottle,
            TableLoginThrottled,
        )

        clock = AdjustableClock()

        def attempt(index: int) -> bool:
            throttle = TableLoginThrottle(self.store.path, clock=clock)
            try:
                throttle.consume("Victor", f"192.0.2.{index}")
                return True
            except TableLoginThrottled:
                return False

        with ThreadPoolExecutor(max_workers=4) as workers:
            admitted = list(workers.map(attempt, range(16)))
        self.assertEqual(sum(admitted), 10)
