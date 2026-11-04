"""A derivação do PIN não deve bloquear escritores nem vencer uma rotação."""

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from app.core.database import open_connection
from app.services import campaign_store
from app.services.campaign_store import CampaignStore
from app.services.errors import CampaignError

TEST_PIN = "pin-original-de-teste"
ROTATED_PIN = "pin-novo-de-teste"


class ConcurrentWriterProbe:
    def __init__(self, store: CampaignStore, rotate: bool = False):
        self.store = store
        self.rotate = rotate
        self.verify = campaign_store._verify_secret

    def __call__(self, secret: str, stored: str) -> bool:
        if self.rotate:
            self.store.set_table_pin("Victor", ROTATED_PIN)
        else:
            with closing(open_connection(self.store.path)) as conn, conn:
                conn.execute("PRAGMA busy_timeout = 0")
                conn.execute("UPDATE users SET display_name = display_name")
        return self.verify(secret, stored)


class TableAuthSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = CampaignStore(Path(self.directory.name) / "auth.sqlite3")
        self.store.set_table_pin("Victor", TEST_PIN)

    def test_pin_derivation_does_not_hold_the_database_write_lock(self) -> None:
        probe = ConcurrentWriterProbe(self.store)
        with patch.object(campaign_store, "_verify_secret", side_effect=probe):
            token = self.store.table_login("Victor", TEST_PIN)
        self.assertIsNotNone(self.store.user_for_token(token))

    def test_rotation_during_verification_cannot_mint_an_old_pin_session(self) -> None:
        probe = ConcurrentWriterProbe(self.store, rotate=True)
        with (
            patch.object(campaign_store, "_verify_secret", side_effect=probe),
            self.assertRaises(CampaignError) as denied,
        ):
            self.store.table_login("Victor", TEST_PIN)
        self.assertEqual(denied.exception.status, 401)
        token = self.store.table_login("Victor", ROTATED_PIN)
        self.assertIsNotNone(self.store.user_for_token(token))

    def test_unknown_and_unprovisioned_names_still_verify_a_dummy_secret(self) -> None:
        with patch.object(
            campaign_store, "_verify_secret", wraps=campaign_store._verify_secret
        ) as verify:
            for name in ("Desconhecido", "Wayne"):
                with self.assertRaises(CampaignError):
                    self.store.table_login(name, TEST_PIN)
        self.assertEqual(verify.call_count, 2)
