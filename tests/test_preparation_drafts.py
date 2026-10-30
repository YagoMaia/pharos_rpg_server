"""Rascunhos de preparação: payload tipado, vínculo a revisões exatas, zero escrita oficial.

Etapa 4 do plano `plans/mcp-preparacao-campanha.md` (ai-memory: workspace
`pharos`, projeto `intregração`). Aceite do plano: "organizar sem alterar fontes
nem registros oficiais". Nada aqui escreve em campaign_sessions/npcs/quests/
encounters — aplicar é a etapa 5.

Regras verificadas:

- payload é discriminado por tipo de entidade e recusa chaves desconhecidas;
  um dicionário arbitrário não é contrato aceito;
- rascunho aponta para a **revisão exata** da fonte, não para a fonte em geral;
- NPCs e missões nascem privados (`gm`), diferente do default `party` das rotas
  existentes (`campaigns.py`, NpcBody/QuestBody), porque rascunho é material do
  mestre até a revisão humana;
- conflito de revisão no rascunho devolve 409 sem sobrescrever;
- jogador não enxerga rascunhos.
"""

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import ClassVar

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.routers.preparation import get_preparation_store
from app.services.campaign_store import CampaignStore
from app.services.preparation_drafts import build_draft_store
from app.services.preparation_store import PreparationStore

COLLECTION = "campanha-victor"


class PreparationDraftTests(unittest.TestCase):
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

    # ── helpers ────────────────────────────────────────────────────────────

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

    def import_source(self, content, relative_path="notas.md"):
        response = self.client.post(
            f"/campaigns/{self.campaign_id}/preparation/sources/import",
            headers=self.gm,
            json={
                "collectionId": COLLECTION,
                "relativePath": relative_path,
                "title": "Notas",
                "content": content,
            },
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()["source"]

    def draft_path(self, draft_id=None):
        base = f"/campaigns/{self.campaign_id}/preparation/drafts"
        return base if draft_id is None else f"{base}/{draft_id}"

    def create_draft(self, payload, **extra):
        return self.client.post(
            self.draft_path(), headers=self.gm, json={"payload": payload, **extra}
        )

    def official_counts(self):
        with closing(self.store._connect()) as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in (
                    "campaign_sessions",
                    "campaign_encounters",
                    "campaign_npcs",
                    "campaign_quests",
                    "journal_entries",
                )
            }

    NPC_PAYLOAD: ClassVar[dict] = {
        "kind": "npc",
        "name": "Ferrarin",
        "role": "ferreiro",
        "description": "forja runas antigas",
        "gmNotes": "esconde que traiu a irmandade",
    }
    SESSION_PAYLOAD: ClassVar[dict] = {
        "kind": "session",
        "sessionNumber": 9,
        "title": "A forja quebrada",
        "summary": "os heróis procuram o ferreiro",
    }

    # ── payload tipado, não dicionário arbitrário ──────────────────────────

    def test_draft_is_bound_to_the_exact_source_revision(self):
        source = self.import_source("primeira versao")
        self.import_source("segunda versao", relative_path=source["relativePath"])
        created = self.create_draft(
            self.NPC_PAYLOAD,
            sourceRefs=[{"sourceId": source["id"], "revision": 1}],
        )
        self.assertEqual(created.status_code, 201, created.text)
        draft = created.json()
        self.assertEqual(
            draft["sourceRefs"], [{"sourceId": source["id"], "revision": 1}]
        )
        self.assertEqual(draft["revision"], 1)
        self.assertEqual(draft["status"], "draft")

        listed = self.client.get(self.draft_path(), headers=self.gm).json()
        self.assertEqual(len(listed["items"]), 1)
        self.assertNotIn("payload", listed["items"][0], "listagem não deve pagar o payload")

    def test_unknown_keys_and_unknown_kind_are_rejected(self):
        for payload, hint in (
            ({**self.NPC_PAYLOAD, "statblock": {"hp": 999}}, "statblock"),
            ({"kind": "monstro", "name": "x"}, "kind"),
            ({"kind": "npc"}, "name"),
            ("não é objeto", "payload"),
        ):
            with self.subTest(hint=hint):
                response = self.create_draft(payload)
                self.assertEqual(response.status_code, 422, response.text)

    def test_npc_and_quest_drafts_default_to_gm_visibility(self):
        """Diferente das rotas existentes, que defaulteam `party`."""
        created = self.create_draft(self.NPC_PAYLOAD).json()
        self.assertEqual(created["payload"]["visibility"], "gm")
        quest = self.create_draft(
            {"kind": "quest", "title": "A bigorna perdida"}
        ).json()
        self.assertEqual(quest["payload"]["visibility"], "gm")

    def test_draft_never_touches_official_records_or_source_revisions(self):
        source = self.import_source("material bruto")
        before = self.official_counts()
        self.create_draft(
            self.SESSION_PAYLOAD,
            sourceRefs=[{"sourceId": source["id"], "revision": 1}],
        ).raise_for_status()
        self.create_draft(self.NPC_PAYLOAD).raise_for_status()
        self.assertEqual(self.official_counts(), before)

        after = self.client.get(
            f"/campaigns/{self.campaign_id}/preparation/sources", headers=self.gm
        ).json()
        self.assertEqual(after["items"][0]["currentRevision"], source["currentRevision"])

    # ── referências validadas: nada de IDs inventados ──────────────────────

    def test_source_ref_must_exist_at_that_revision(self):
        source = self.import_source("conteudo")
        for ref, hint in (
            ({"sourceId": "00000000-0000-0000-0000-000000000000", "revision": 1}, "inexistente"),
            ({"sourceId": source["id"], "revision": 7}, "revisao futura"),
        ):
            with self.subTest(hint=hint):
                response = self.create_draft(self.NPC_PAYLOAD, sourceRefs=[ref])
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("fonte", response.text.lower())

    def test_update_target_must_exist_and_match_the_kind(self):
        existing_session = self.session()["id"]
        cases = (
            (self.NPC_PAYLOAD, "00000000-0000-0000-0000-000000000000", "id inexistente"),
            (self.NPC_PAYLOAD, existing_session, "sessao apontada como npc"),
            (self.SESSION_PAYLOAD, "00000000-0000-0000-0000-000000000000", "sessao inexistente"),
        )
        for payload, target_id, hint in cases:
            with self.subTest(hint=hint):
                response = self.create_draft(payload, targetId=target_id)
                self.assertEqual(response.status_code, 422, response.text)

    def session(self):
        response = self.client.post(
            f"/campaigns/{self.campaign_id}/sessions",
            headers=self.gm,
            json={"sessionNumber": 1, "title": "Sessao 1"},
        )
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    # ── concorrência e estado ──────────────────────────────────────────────

    def test_update_requires_expected_revision_and_bumps_it(self):
        created = self.create_draft(self.NPC_PAYLOAD).json()
        draft_id = created["id"]
        changed = self.client.put(
            self.draft_path(draft_id),
            headers=self.gm,
            json={
                "payload": {**self.NPC_PAYLOAD, "role": "ferreiro e informante"},
                "expectedRevision": created["revision"],
            },
        )
        self.assertEqual(changed.status_code, 200, changed.text)
        self.assertEqual(changed.json()["revision"], 2)

        stale = self.client.put(
            self.draft_path(draft_id),
            headers=self.gm,
            json={"payload": self.NPC_PAYLOAD, "expectedRevision": created["revision"]},
        )
        self.assertEqual(stale.status_code, 409, stale.text)
        current = self.client.get(self.draft_path(draft_id), headers=self.gm).json()
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["payload"]["role"], "ferreiro e informante")

    def test_draft_cannot_be_marked_applied_in_this_stage(self):
        """Aplicação é etapa 5; aqui rascunho não vira registro oficial."""
        created = self.create_draft(self.NPC_PAYLOAD).json()
        before = self.official_counts()
        response = self.client.put(
            self.draft_path(created["id"]),
            headers=self.gm,
            json={"payload": self.NPC_PAYLOAD, "status": "applied", "expectedRevision": 1},
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(self.official_counts(), before)

    # ── autorização e isolamento ───────────────────────────────────────────

    def test_player_cannot_create_list_or_read_drafts(self):
        draft_id = self.create_draft(self.NPC_PAYLOAD).json()["id"]
        player = self.player_headers()
        actions = [
            self.client.post(
                self.draft_path(), headers=player, json={"payload": self.NPC_PAYLOAD}
            ),
            self.client.get(self.draft_path(), headers=player),
            self.client.get(self.draft_path(draft_id), headers=player),
            self.client.put(
                self.draft_path(draft_id),
                headers=player,
                json={"payload": self.NPC_PAYLOAD, "expectedRevision": 1},
            ),
        ]
        for response in actions:
            with self.subTest(status=response.status_code):
                self.assertEqual(response.status_code, 403, response.text)

    def test_server_surfaces_visibility_exposure_of_a_session_summary(self):
        """`campaign_sessions` não tem coluna de visibilidade: summary vaza ao aplicar."""
        session = self.create_draft(
            {**self.SESSION_PAYLOAD, "status": "planned", "summary": "detalhes secretos"}
        ).json()
        self.assertTrue(any("summary" in warning for warning in session["warnings"]))

        npc = self.create_draft({**self.NPC_PAYLOAD, "visibility": "party"}).json()
        self.assertEqual(npc["payload"]["visibility"], "party")
        self.assertTrue(any("party" in warning for warning in npc["warnings"]))

        private_npc = self.create_draft(self.NPC_PAYLOAD).json()
        self.assertEqual(private_npc["warnings"], [])

    def test_drafts_survive_reopening_and_add_tables_to_an_existing_database(self):
        created = self.create_draft(self.NPC_PAYLOAD).json()
        reopened = PreparationStore(self.database, self.store)
        drafts = build_draft_store(str(self.database), self.store, reopened)
        restored = drafts.read_draft(
            self.store.user_for_token(self.gm["Authorization"][7:])["id"],
            self.campaign_id,
            created["id"],
        )
        self.assertEqual(restored["payload"], created["payload"])
        self.assertEqual(restored["revision"], 1)

    def test_existing_entity_is_suggested_inst_of_duplicated(self):
        """O plano §7: reutilizar NPCs existentes, não duplicar."""
        npc = self.client.post(
            f"/campaigns/{self.campaign_id}/npcs",
            headers=self.gm,
            json={"name": "Ferrarin", "role": "ferreiro"},
        )
        self.assertEqual(npc.status_code, 201, npc.text)
        existing_id = npc.json()["id"]

        draft = self.create_draft({**self.NPC_PAYLOAD, "name": " ferrarin "}).json()
        suggestions = draft["possibleTargets"]
        self.assertEqual(len(suggestions), 1)
        self.assertEqual(suggestions[0]["id"], existing_id)
        self.assertEqual(suggestions[0]["kind"], "npc")

        redirected = self.create_draft(
            self.NPC_PAYLOAD, targetId=existing_id
        ).json()
        self.assertEqual(redirected["targetId"], existing_id)
        self.assertEqual(redirected["possibleTargets"], [])

    def test_drafts_do_not_reach_the_combat_transport(self):
        from unittest.mock import patch

        with patch("app.managers.connection.manager") as transport:
            self.create_draft(self.SESSION_PAYLOAD).raise_for_status()
            self.client.get(self.draft_path(), headers=self.gm)
        transport.get_state.assert_not_called()
        transport.broadcast_state.assert_not_called()
        transport.broadcast_event.assert_not_called()


if __name__ == "__main__":
    unittest.main()
