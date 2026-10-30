"""Fontes brutas de preparação: importação idempotente, revisões imutáveis, busca.

Cobriu a Etapa 2 do plano `plans/mcp-preparacao-campanha.md` (ai-memory,
workspace pharos / projeto intregração). Regras centrais verificadas aqui:

- o servidor preserva o texto exatamente como recebeu (sem trim/reformatação);
- o checksum é do conteúdo recebido, não uma prova sobre o arquivo local;
- reimportar conteúdo idêntico não cria revisão; retry não duplica;
- caminho relativo é metadado: nunca vira leitura de filesystem;
- apenas o mestre da campanha enxerga as fontes.
"""

import hashlib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.routers.preparation import get_preparation_store
from app.services.campaign_store import CampaignStore
from app.services.preparation_store import PreparationStore

COLLECTION = "campanha-victor"


def checksum_of(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class PreparationSourceTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / "test.sqlite3"
        self.store = CampaignStore(self.database)
        self.preparation = PreparationStore(self.database, self.store)
        app.dependency_overrides[get_campaign_store] = lambda: self.store
        app.dependency_overrides[get_preparation_store] = lambda: self.preparation
        self.client = TestClient(app)
        self.gm = self.register("gm@example.com", "Mestre")
        self.campaign_id = self.campaign()

    def tearDown(self):
        self.client.close()
        app.dependency_overrides.clear()
        self.directory.cleanup()

    def register(self, email, name="Jogador"):
        response = self.client.post(
            "/auth/register",
            json={"email": email, "displayName": name, "password": "safe-password-123"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return {"Authorization": f"Bearer {response.json()['accessToken']}"}

    def campaign(self):
        response = self.client.post(
            "/campaigns", headers=self.gm, json={"name": "Pharos"}
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["id"]

    def player_headers(self):
        player = self.register("player@example.com")
        invite = self.client.post(
            f"/campaigns/{self.campaign_id}/invites", headers=self.gm
        ).json()["code"]
        self.client.post(
            "/campaigns/join", json={"inviteCode": invite}, headers=player
        )
        return player

    def import_source(self, content, relative_path="sessoes/sessao-08.md", **extra):
        payload = {
            "collectionId": COLLECTION,
            "relativePath": relative_path,
            "title": "Sessão 8",
            "content": content,
        }
        payload.update(extra)
        return self.client.post(
            f"/campaigns/{self.campaign_id}/preparation/sources/import",
            headers=self.gm,
            json=payload,
        )

    # ── preservação do conteúdo bruto ───────────────────────────────────────

    def test_content_is_retrieved_exactly_as_received(self):
        raw = (
            "# Sessão 8 — anotações\n"
            "  espaços   preservados\t\ttabs\n"
            "acentuação: ç, ã, é, ü — quebras\n\n"
            "última linha sem newline\n"
        )
        imported = self.import_source(raw)
        self.assertEqual(imported.status_code, 201, imported.text)
        body = imported.json()
        self.assertEqual(body["status"], "imported")
        self.assertEqual(body["source"]["currentRevision"], 1)
        self.assertEqual(body["source"]["checksum"], checksum_of(raw))

        stored = self.client.get(
            f"/campaigns/{self.campaign_id}/preparation/sources/"
            f"{body['source']['id']}/revisions/1",
            headers=self.gm,
        )
        self.assertEqual(stored.status_code, 200, stored.text)
        self.assertEqual(stored.json()["content"], raw)
        self.assertEqual(stored.json()["checksum"], checksum_of(raw))
        self.assertEqual(stored.json()["contentChars"], len(raw))

    def test_whitespace_and_escapes_survive_without_normalization(self):
        raw = "  \t\n  leading e trailing  \n\n"
        body = self.import_source(raw).json()
        stored = self.client.get(
            f"/campaigns/{self.campaign_id}/preparation/sources/"
            f"{body['source']['id']}/revisions/1",
            headers=self.gm,
        ).json()
        self.assertEqual(stored["content"], raw)

    # ── revisões, idempotência e conflitos ──────────────────────────────────

    def test_reimporting_identical_content_creates_no_new_revision(self):
        raw = "conteúdo estável"
        first = self.import_source(raw, idempotencyKey="k1").json()
        second = self.import_source(raw, idempotencyKey="k2").json()
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["source"]["id"], first["source"]["id"])
        self.assertEqual(second["source"]["currentRevision"], 1)

    def test_changed_content_adds_revision_and_keeps_previous(self):
        first = self.import_source("rascunho antigo").json()
        source_id = first["source"]["id"]
        changed = self.import_source(
            "rascunho novo", expectedRevision=first["source"]["currentRevision"]
        ).json()
        self.assertEqual(changed["status"], "new_revision")
        self.assertEqual(changed["revision"], 2)

        path = f"/campaigns/{self.campaign_id}/preparation/sources/{source_id}/revisions/"
        old = self.client.get(path + "1", headers=self.gm).json()
        new = self.client.get(path + "2", headers=self.gm).json()
        self.assertEqual(old["content"], "rascunho antigo")
        self.assertEqual(new["content"], "rascunho novo")

    def test_replayed_idempotency_key_returns_same_result_without_duplicate(self):
        first = self.import_source("conteúdo", idempotencyKey="retry-1").json()
        replay = self.import_source("conteúdo", idempotencyKey="retry-1").json()
        self.assertEqual(replay, first)

        listed = self.client.get(
            f"/campaigns/{self.campaign_id}/preparation/sources", headers=self.gm
        ).json()
        self.assertEqual(len(listed["items"]), 1)
        self.assertEqual(listed["items"][0]["currentRevision"], 1)

    def test_stale_expected_revision_conflicts_and_writes_nothing(self):
        first = self.import_source("primeiro").json()
        source_id = first["source"]["id"]
        conflict = self.import_source("segundo", expectedRevision=7)
        self.assertEqual(conflict.status_code, 409, conflict.text)
        current = self.client.get(
            f"/campaigns/{self.campaign_id}/preparation/sources/{source_id}/revisions/1",
            headers=self.gm,
        ).json()
        self.assertEqual(current["content"], "primeiro")

    # ── caminhos são metadados, nunca instruções de filesystem ─────────────

    def test_absolute_and_traversal_paths_are_rejected_without_touching_disk(self):
        for path in (
            "C:\\Campanha\\notas.md",
            "/etc/passwd",
            "../segredo.md",
            "sessoes/../../segredo.md",
            "\\\\servidor\\compartilhado\\notas.md",
            "sessoes/\x00.md",
            "",
        ):
            with self.subTest(path=path):
                response = self.import_source("conteúdo", relative_path=path)
                self.assertEqual(response.status_code, 422, response.text)

    def test_windows_separators_are_canonicalized_to_one_identity(self):
        first = self.import_source("conteúdo", relative_path="sessoes\\sub\\a.md").json()
        second = self.import_source("conteúdo", relative_path="sessoes/sub/a.md").json()
        self.assertEqual(second["status"], "unchanged")
        self.assertEqual(second["source"]["id"], first["source"]["id"])
        self.assertEqual(second["source"]["relativePath"], "sessoes/sub/a.md")

    def test_same_content_in_another_path_is_flagged_as_possible_duplicate(self):
        raw = "mesmo texto em dois lugares"
        self.import_source(raw, relative_path="a.md")
        other = self.import_source(raw, relative_path="b.md").json()
        self.assertEqual(other["status"], "imported")
        duplicates = other["possibleDuplicates"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["relativePath"], "a.md")

    def test_oversized_content_is_rejected_with_expected_limit(self):
        response = self.import_source("x" * 200_001)
        self.assertEqual(response.status_code, 422, response.text)
        self.assertIn("200000", response.text)

    # ── listagem, busca e paginação estável ────────────────────────────────

    def test_list_searches_by_text_and_pages_with_a_stable_cursor(self):
        for index in range(3):
            self.import_source(f"emboscada na floresta {index}", relative_path=f"e{index}.md")
        self.import_source("ferraria e contratos", relative_path="npcs.md")

        searched = self.client.get(
            f"/campaigns/{self.campaign_id}/preparation/sources",
            headers=self.gm,
            params={"q": "emboscada", "limit": 2},
        ).json()
        self.assertEqual(len(searched["items"]), 2)
        self.assertTrue(searched["nextCursor"])
        for item in searched["items"]:
            self.assertNotIn("content", item)

        page_two = self.client.get(
            f"/campaigns/{self.campaign_id}/preparation/sources",
            headers=self.gm,
            params={"q": "emboscada", "limit": 2, "cursor": searched["nextCursor"]},
        ).json()
        self.assertEqual(len(page_two["items"]), 1)
        self.assertIsNone(page_two["nextCursor"])

        seen = {i["id"] for i in searched["items"]} | {i["id"] for i in page_two["items"]}
        self.assertEqual(len(seen), 3)

    def test_preparation_never_touches_combat_transport(self):
        """Fontes de preparação não podem alcançar o Redis/WS do combate.

        Guard-rail: se alguém no futuro conectar importação a broadcast, este
        teste pega. O contrato do plano é que preparação nunca emite evento de
        combate nem altera estado vivo.
        """
        from combat_guard import no_combat_transport

        with no_combat_transport():
            imported = self.import_source("nada de combate")
            self.client.get(
                f"/campaigns/{self.campaign_id}/preparation/sources", headers=self.gm
            )
        self.assertEqual(imported.status_code, 201)

    def test_sources_persist_across_store_instances(self):
        imported = self.import_source("conteúdo persistente").json()
        reopened = PreparationStore(self.database, self.store)
        restored = reopened.read_revision(
            self.store.user_for_token(self.gm["Authorization"][7:])["id"],
            self.campaign_id,
            imported["source"]["id"],
            1,
        )
        self.assertEqual(restored["content"], "conteúdo persistente")

    # ── autorização ────────────────────────────────────────────────────────

    def test_player_cannot_import_list_or_read_sources(self):
        source_id = self.import_source("segredo do mestre").json()["source"]["id"]
        player = self.player_headers()

        actions = [
            self.client.post(
                f"/campaigns/{self.campaign_id}/preparation/sources/import",
                headers=player,
                json={
                    "collectionId": COLLECTION,
                    "relativePath": "x.md",
                    "title": "x",
                    "content": "y",
                },
            ),
            self.client.get(
                f"/campaigns/{self.campaign_id}/preparation/sources", headers=player
            ),
            self.client.get(
                f"/campaigns/{self.campaign_id}/preparation/sources/"
                f"{source_id}/revisions/1",
                headers=player,
            ),
        ]
        for response in actions:
            with self.subTest(status=response.status_code):
                self.assertEqual(response.status_code, 403, response.text)

    def test_source_is_never_reachable_from_another_campaign(self):
        source_id = self.import_source("conteúdo").json()["source"]["id"]
        other_campaign = self.client.post(
            "/campaigns", headers=self.gm, json={"name": "Outra"}
        ).json()["id"]
        response = self.client.get(
            f"/campaigns/{other_campaign}/preparation/sources/{source_id}/revisions/1",
            headers=self.gm,
        )
        self.assertEqual(response.status_code, 404, response.text)
        listed = self.client.get(
            f"/campaigns/{other_campaign}/preparation/sources", headers=self.gm
        ).json()
        self.assertEqual(listed["items"], [])


class PreparationMigrationTests(unittest.TestCase):
    """O deploy adiciona as tabelas novas sem tocar no que já existe no banco."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / "existing.sqlite3"

    def tearDown(self):
        self.directory.cleanup()

    def test_existing_rows_survive_and_revisions_are_immutable(self):
        store = CampaignStore(self.database)
        token = store.register("gm@example.com", "Mestre", "safe-password-123")
        user_id = store.user_for_token(token)["id"]
        campaign = store.create_campaign(user_id, "Pharos")

        preparation = PreparationStore(self.database, store)
        imported = preparation.import_source(
            user_id,
            campaign["id"],
            collection_id=COLLECTION,
            relative_path="ideias.md",
            title="Ideias",
            content="primeira versão",
        )
        source_id = imported["source"]["id"]
        preparation.import_source(
            user_id,
            campaign["id"],
            collection_id=COLLECTION,
            relative_path="ideias.md",
            title="Ideias",
            content="segunda versão",
        )

        reopened = CampaignStore(self.database)
        self.assertEqual(reopened.list_campaigns(user_id)[0]["name"], "Pharos")
        self.assertEqual(reopened.user_for_token(token)["display_name"], "Mestre")

        fresh = PreparationStore(self.database, reopened)
        first = fresh.read_revision(user_id, campaign["id"], source_id, 1)
        latest = fresh.read_revision(user_id, campaign["id"], source_id, 2)
        self.assertEqual(first["content"], "primeira versão")
        self.assertEqual(latest["content"], "segunda versão")


if __name__ == "__main__":
    unittest.main()
