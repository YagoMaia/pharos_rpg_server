"""Aplicação transacional de rascunhos (Etapa 5 do plano).

Aceite do plano: *"Aprovação humana cria/atualiza destinos sem vazamento/efeito em
combate"*. Regras verificadas aqui:

- aplicar escreve no destino oficial numa **transação única**: se qualquer escrita
  falha, nada fica (sem aplicação parcial);
- retry com a mesma `idempotencyKey` devolve o mesmo resultado sem duplicar;
- rascunho já aplicado não é aplicado de novo sem a chave do replay;
- atualização exige que o destino não tenha mudado desde a proposta (409);
- conteúdo que vai para campo visível a jogador exige reconhecimento explícito do
  mestre no pedido, porque `campaign_sessions` não tem coluna de visibilidade;
- aplicar não toca o transporte de combate nem inicia encontro.
"""

import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from combat_guard import no_combat_transport
from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.routers.preparation import get_preparation_store
from app.services.campaign_store import CampaignStore
from app.services.preparation_store import PreparationStore

COLLECTION = "campanha-victor"
OFFICIAL_TABLES = (
    "campaign_sessions",
    "campaign_encounters",
    "campaign_npcs",
    "campaign_quests",
    "journal_entries",
)


NPC_PAYLOAD = {
    "kind": "npc",
    "name": "Ferrarin",
    "role": "ferreiro",
    "gmNotes": "esconde que traiu a irmandade",
}
SESSION_PAYLOAD = {
    "kind": "session",
    "sessionNumber": 9,
    "title": "A forja quebrada",
    "summary": "os heróis procuram o ferreiro",
}

class PreparationApplyTests(unittest.TestCase):
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
        self.client.post("/campaigns/join", json={"inviteCode": invite}, headers=player)
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

    def draft_path(self, draft_id=None, suffix=""):
        base = f"/campaigns/{self.campaign_id}/preparation/drafts"
        base = base if draft_id is None else f"{base}/{draft_id}"
        return base + suffix

    def create_draft(self, payload, **extra):
        return self.client.post(self.draft_path(), headers=self.gm, json={"payload": payload, **extra})

    def apply(self, draft_id, **body):
        return self.client.post(
            self.draft_path(draft_id, "/apply"), headers=self.gm, json=body
        )

    def official_counts(self):
        with closing(self.store._connect()) as conn:
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in OFFICIAL_TABLES
            }

    def official_row(self, table: str, record_id: str) -> dict:
        """Le o registro oficial direto no banco: prova a escrita, nao so a resposta."""
        with closing(self.store._connect()) as conn:
            row = conn.execute(
                f"SELECT * FROM {table} WHERE id = ?", (record_id,)
            ).fetchone()
        self.assertIsNotNone(row, f"{table}/{record_id} nao existe")
        return dict(row)

    # ── criar destino oficial a partir do rascunho ─────────────────────────

    def test_apply_creates_the_official_record_and_keeps_the_source_binding(self):
        source = self.import_source("o ferreiro traiu a irmandade")
        draft = self.create_draft(
            NPC_PAYLOAD, sourceRefs=[{"sourceId": source["id"], "revision": 1}]
        ).json()
        before = self.official_counts()

        applied = self.apply(draft["id"], idempotencyKey="ap-1")
        self.assertEqual(applied.status_code, 201, applied.text)
        result = applied.json()
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["kind"], "npc")
        self.assertTrue(result["recordId"])
        self.assertEqual(
            result["sourceRefs"], [{"sourceId": source["id"], "revision": 1}]
        )

        npc = self.official_row("campaign_npcs", result["recordId"])
        self.assertEqual(npc["name"], "Ferrarin")
        self.assertEqual(npc["visibility"], "gm")
        self.assertEqual(self.official_counts()["campaign_npcs"], before["campaign_npcs"] + 1)

        stored = self.client.get(self.draft_path(draft["id"]), headers=self.gm).json()
        self.assertEqual(stored["status"], "approved")
        self.assertEqual(stored["application"]["applicationId"], result["applicationId"])

    def test_apply_does_not_touch_the_combat_transport(self):
        """Aplicar encontro grava planejamento; lançar combate é outra rota."""
        draft = self.create_draft(
            {
                "kind": "encounter",
                "name": "Emboscada na ponte",
                "tactics": "dois arqueiros no telhado",
                "monstersData": [{"name": "bandido", "count": 2}],
            }
        ).json()
        with no_combat_transport():
            applied = self.apply(draft["id"], idempotencyKey="ap-enc")
        self.assertEqual(applied.status_code, 201, applied.text)
        self.assertEqual(self.official_counts()["campaign_encounters"], 1)

    # ── visibilidade de jogador exige decisão humana explícita ─────────────

    def test_session_summary_requires_explicit_acknowledgement(self):
        """`campaign_sessions` não tem coluna de visibilidade: summary é público."""
        draft = self.create_draft(SESSION_PAYLOAD).json()
        refused = self.apply(draft["id"], idempotencyKey="ap-noack")
        self.assertEqual(refused.status_code, 422, refused.text)
        self.assertIn("summary", refused.text)
        self.assertEqual(self.official_counts()["campaign_sessions"], 0)

        applied = self.apply(
            draft["id"], idempotencyKey="ap-ack", acknowledgePlayerVisibility=True
        )
        self.assertEqual(applied.status_code, 201, applied.text)
        self.assertEqual(self.official_counts()["campaign_sessions"], 1)

    def test_npc_draft_meant_for_the_gm_needs_no_acknowledgement(self):
        draft = self.create_draft(NPC_PAYLOAD).json()
        self.assertEqual(self.apply(draft["id"], idempotencyKey="ap-gm").status_code, 201)

    # ── atualização do destino existente ───────────────────────────────────

    def test_apply_updates_existing_target(self):
        npc = self.client.post(
            f"/campaigns/{self.campaign_id}/npcs",
            headers=self.gm,
            json={"name": "Ferrarin", "role": "ferreiro"},
        ).json()
        draft = self.create_draft(
            {**NPC_PAYLOAD, "role": "ferreiro e informante"}, targetId=npc["id"]
        ).json()
        before = self.official_counts()

        applied = self.apply(draft["id"], idempotencyKey="ap-up")
        self.assertEqual(applied.status_code, 201, applied.text)
        self.assertEqual(applied.json()["action"], "updated")
        self.assertEqual(applied.json()["recordId"], npc["id"])
        self.assertEqual(self.official_counts()["campaign_npcs"], before["campaign_npcs"])

        updated = self.official_row("campaign_npcs", npc["id"])
        self.assertEqual(updated["role"], "ferreiro e informante")

    def test_apply_refuses_when_the_destination_changed_after_the_draft(self):
        npc = self.client.post(
            f"/campaigns/{self.campaign_id}/npcs",
            headers=self.gm,
            json={"name": "Ferrarin", "role": "ferreiro"},
        ).json()
        draft = self.create_draft(NPC_PAYLOAD, targetId=npc["id"]).json()

        self.client.put(
            f"/campaigns/{self.campaign_id}/npcs/{npc['id']}",
            headers=self.gm,
            json={"name": "Ferrarin", "role": "ferreiro e contrabandista"},
        ).raise_for_status()

        conflict = self.apply(draft["id"], idempotencyKey="ap-conflict")
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("mudou", conflict.text)
        self.assertEqual(
            self.official_row("campaign_npcs", npc["id"])["role"],
            "ferreiro e contrabandista",
        )

    # ── transação única, replay e reaplicação ──────────────────────────────

    def test_retry_with_the_same_key_returns_the_same_result_without_duplicating(self):
        draft = self.create_draft(NPC_PAYLOAD).json()
        first = self.apply(draft["id"], idempotencyKey="ap-retry").json()
        before = self.official_counts()

        replay = self.apply(draft["id"], idempotencyKey="ap-retry")
        self.assertEqual(replay.status_code, 201, replay.text)
        self.assertEqual(replay.json(), first)
        self.assertEqual(self.official_counts(), before)

    def test_applying_an_already_applied_draft_without_its_key_is_refused(self):
        draft = self.create_draft(NPC_PAYLOAD).json()
        self.apply(draft["id"], idempotencyKey="ap-once").raise_for_status()
        before = self.official_counts()

        refused = self.apply(draft["id"])
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("já foi aplicado", refused.text)
        self.assertEqual(self.official_counts(), before)

    def test_partial_failure_rolls_back_every_official_write(self):
        """A escrita oficial e o registro da aplicação estão na MESMA transação."""
        draft = self.create_draft(NPC_PAYLOAD).json()
        before = self.official_counts()

        with patch(
            "app.services.preparation_apply.PreparationApplyStore._record_application",
            side_effect=RuntimeError("falha no meio da aplicação"),
        ), self.assertRaises(RuntimeError):
            self.apply(draft["id"], idempotencyKey="ap-boom")

        self.assertEqual(self.official_counts(), before)
        stored = self.client.get(self.draft_path(draft["id"]), headers=self.gm).json()
        self.assertEqual(stored["status"], "draft")
        self.assertIsNone(stored["application"])

    def test_duplicate_session_number_is_refused_instead_of_silently_duplicated(self):
        self.client.post(
            f"/campaigns/{self.campaign_id}/sessions",
            headers=self.gm,
            json={"sessionNumber": 9, "title": "Sessao 9 existente"},
        ).raise_for_status()
        draft = self.create_draft(SESSION_PAYLOAD).json()

        refused = self.apply(
            draft["id"], idempotencyKey="ap-num", acknowledgePlayerVisibility=True
        )
        self.assertEqual(refused.status_code, 409, refused.text)
        self.assertIn("9", refused.text)
        self.assertEqual(self.official_counts()["campaign_sessions"], 1)

    # ── autorização ────────────────────────────────────────────────────────

    def test_player_cannot_apply_a_draft(self):
        draft = self.create_draft(NPC_PAYLOAD).json()
        player = self.player_headers()
        response = self.client.post(
            self.draft_path(draft["id"], "/apply"),
            headers=player,
            json={"idempotencyKey": "ap-player"},
        )
        self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(self.official_counts()["campaign_npcs"], 0)

    def test_a_draft_from_another_campaign_is_not_applicable(self):
        other = self.client.post(
            "/campaigns", headers=self.gm, json={"name": "Outra"}
        ).json()["id"]
        draft = self.create_draft(NPC_PAYLOAD).json()
        response = self.client.post(
            f"/campaigns/{other}/preparation/drafts/{draft['id']}/apply",
            headers=self.gm,
            json={"idempotencyKey": "ap-other"},
        )
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(self.official_counts()["campaign_npcs"], 0)


if __name__ == "__main__":
    unittest.main()
