"""Credencial de preparação do mestre (Etapa 1 do plano).

Aceite do plano: o agent remoto do Victor precisa autenticar sem senha de
jogador e sem receber o token de sessão do app — e **sem poder publicar nada
sozinho**. Regras verificadas aqui:

- o GM emite um token de preparação por campanha; o valor em claro aparece uma
  única vez na resposta de criação;
- no banco fica apenas o hash (SHA-256), então vazamento do banco não vazam
  credenciais utilizáveis;
- o token autentica **apenas** rotas de preparação da campanha dele;
- o token não autentica rotas comuns de campanha (sessões, NPCs, diário);
- token de uma campanha não alcança a preparação de outra;
- revogar corta na hora;
- jogador não emite nem lista tokens;
- **token de preparação não aplica rascunho**: aplicar é decisão humana no app
  (§5 do plano: "approved enviado pelo modelo não comprova consentimento").
"""

import unittest
from contextlib import closing
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.routers.preparation import get_preparation_store
from app.services.campaign_store import CampaignStore
from app.services.preparation_store import PreparationStore

COLLECTION = "campanha-victor"


class PreparationTokenTests(unittest.TestCase):
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

    def issue(self, campaign_id=None, label="agent do Victor", headers=None):
        return self.client.post(
            f"/campaigns/{campaign_id or self.campaign_id}/preparation/tokens",
            headers=headers or self.gm,
            json={"label": label},
        )

    def list_tokens(self, campaign_id=None, headers=None):
        return self.client.get(
            f"/campaigns/{campaign_id or self.campaign_id}/preparation/tokens",
            headers=headers or self.gm,
        )

    def revoke(self, token_id, campaign_id=None, headers=None):
        return self.client.delete(
            f"/campaigns/{campaign_id or self.campaign_id}/preparation/tokens/{token_id}",
            headers=headers or self.gm,
        )

    def as_token(self, token):
        return {"Authorization": f"Bearer {token}"}

    def import_source(self, content, headers=None, campaign_id=None):
        return self.client.post(
            f"/campaigns/{campaign_id or self.campaign_id}/preparation/sources/import",
            headers=headers or self.gm,
            json={
                "collectionId": COLLECTION,
                "relativePath": "notas.md",
                "title": "Notas",
                "content": content,
            },
        )

    def token_rows(self):
        with closing(self.store._connect()) as conn:
            rows = conn.execute("SELECT * FROM preparation_tokens").fetchall()
        return [dict(row) for row in rows]

    # ── emissão ────────────────────────────────────────────────────────────

    def test_the_gm_issues_a_token_and_sees_the_value_only_once(self):
        created = self.issue()
        self.assertEqual(created.status_code, 201, created.text)
        body = created.json()
        self.assertTrue(body["token"].startswith("pharos_prep_"))
        self.assertTrue(body["id"])
        self.assertEqual(body["label"], "agent do Victor")
        self.assertIsNone(body["lastUsedAt"])

        stored = self.token_rows()
        self.assertEqual(len(stored), 1)
        self.assertNotEqual(stored[0]["token_hash"], body["token"])
        self.assertEqual(
            stored[0]["token_hash"], sha256(body["token"].encode("utf-8")).hexdigest()
        )
        self.assertEqual(stored[0]["campaign_id"], self.campaign_id)

        listed = self.list_tokens()
        self.assertNotIn(stored[0]["token_hash"], created.text)
        self.assertNotIn(body["token"], listed.text)

    def test_each_issue_produces_a_different_secret(self):
        first = self.issue(label="agent").json()["token"]
        second = self.issue(label="notebook").json()["token"]
        self.assertNotEqual(first, second)
        self.assertGreaterEqual(len(first), 40)

    def test_a_player_cannot_issue_list_or_revoke_tokens(self):
        player = self.player_headers()
        self.assertEqual(self.issue(headers=player).status_code, 403)
        self.assertEqual(self.list_tokens(headers=player).status_code, 403)
        self.assertEqual(
            self.client.delete(
                f"/campaigns/{self.campaign_id}/preparation/tokens/inexistente",
                headers=player,
            ).status_code,
            403,
        )

    # ── uso: só preparação, só a campanha dele ─────────────────────────────

    def test_the_token_authenticates_preparation_reads(self):
        token = self.issue().json()["token"]
        self.import_source("notas do agent", headers=self.as_token(token)).raise_for_status()
        listed = self.client.get(
            f"/campaigns/{self.campaign_id}/preparation/sources",
            headers=self.as_token(token),
        )
        self.assertEqual(listed.status_code, 200, listed.text)
        self.assertEqual(len(listed.json()["items"]), 1)

    def test_the_token_does_not_authenticate_regular_campaign_routes(self):
        """Preparação é escopo: token não vira passe livre para a campanha toda."""
        token = self.issue().json()["token"]
        for path in (
            f"/campaigns/{self.campaign_id}/sessions",
            f"/campaigns/{self.campaign_id}/npcs",
            f"/campaigns/{self.campaign_id}/entries",
        ):
            with self.subTest(path=path):
                response = self.client.get(path, headers=self.as_token(token))
                self.assertEqual(response.status_code, 401, response.text)

    def test_the_token_cannot_reach_another_campaign(self):
        other = self.client.post(
            "/campaigns", headers=self.gm, json={"name": "Outra campanha"}
        ).json()["id"]
        token = self.issue().json()["token"]

        refused = self.client.get(
            f"/campaigns/{other}/preparation/sources", headers=self.as_token(token)
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertEqual(
            self.import_source("nao entra", headers=self.as_token(token), campaign_id=other).status_code,
            403,
        )

    def test_the_token_cannot_apply_a_draft(self):
        """Aplicar é humano: a credencial do agent cria e propõe, nunca publica."""
        token = self.issue().json()["token"]
        source = self.import_source("ferreiro traiu", headers=self.as_token(token)).json()["source"]
        draft = self.client.post(
            f"/campaigns/{self.campaign_id}/preparation/drafts",
            headers=self.as_token(token),
            json={
                "payload": {"kind": "npc", "name": "Ferrarin", "role": "ferreiro"},
                "sourceRefs": [{"sourceId": source["id"], "revision": 1}],
            },
        )
        self.assertEqual(draft.status_code, 201, draft.text)

        refused = self.client.post(
            f"/campaigns/{self.campaign_id}/preparation/drafts/{draft.json()['id']}/apply",
            headers=self.as_token(token),
            json={"idempotencyKey": "agent-tenta"},
        )
        self.assertEqual(refused.status_code, 403, refused.text)
        self.assertIn("humana", refused.text.lower())
        with closing(self.store._connect()) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM campaign_npcs").fetchone()[0], 0)

    # ── revogação e uso ────────────────────────────────────────────────────

    def test_revoking_a_token_stops_it_immediately(self):
        issued = self.issue(label="laptop").json()
        headers = self.as_token(issued["token"])
        self.import_source("antes da revogacao", headers=headers).raise_for_status()

        revoked = self.revoke(issued["id"])
        self.assertEqual(revoked.status_code, 200, revoked.text)
        self.assertTrue(revoked.json()["revokedAt"])

        refused = self.import_source("depois da revogacao", headers=headers)
        self.assertEqual(refused.status_code, 401, refused.text)
        self.assertEqual(
            len(self.client.get(
                f"/campaigns/{self.campaign_id}/preparation/sources", headers=self.gm
            ).json()["items"]),
            1,
        )

    def test_listing_reports_usage_and_never_the_secret(self):
        issued = self.issue(label="agente").json()
        self.import_source("uso real", headers=self.as_token(issued["token"])).raise_for_status()

        listed = self.list_tokens().json()["items"]
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["label"], "agente")
        self.assertTrue(listed[0]["lastUsedAt"])
        self.assertIsNone(listed[0]["revokedAt"])
        self.assertNotIn("token", listed[0])
        self.assertNotIn(issued["token"], str(listed))

    def test_unknown_and_empty_bearer_values_are_rejected(self):
        for value in ("pharos_prep_nao-existe", "lixo"):
            with self.subTest(value=value):
                response = self.client.get(
                    f"/campaigns/{self.campaign_id}/preparation/sources",
                    headers=self.as_token(value),
                )
                self.assertEqual(response.status_code, 401, response.text)


if __name__ == "__main__":
    unittest.main()
