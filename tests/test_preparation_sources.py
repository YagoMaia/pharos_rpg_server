"""Fontes brutas de preparação: importação idempotente, revisões imutáveis, busca.

Cobriu a Etapa 2 do plano `plans/mcp-preparacao-campanha.md` (ai-memory,
workspace pharos / projeto intregração). Regras centrais verificadas aqui:

- o servidor preserva o texto exatamente como recebeu (sem trim/reformatação);
- o checksum é do conteúdo recebido, não uma prova sobre o arquivo local;
- reimportar conteúdo idêntico não cria revisão; retry não duplica;
- a importação é uma seção crítica: chave idempotente, fonte e próxima revisão são
  lidos e escritos na MESMA transação, então duas chamadas juntas não geram fonte
  duplicada nem 500 de `IntegrityError`;
- uma `idempotencyKey` representa um pedido (coleção + caminho + conteúdo); usá-la
  para outro pedido devolve 409;
- caminho relativo é metadado: nunca vira leitura de filesystem, e caractere de
  controle é recusado em vez de removido pelo `strip()`;
- apenas o mestre da campanha enxerga as fontes.
"""

import hashlib
import json
import sqlite3
import unittest
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Thread
from time import sleep
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import app
from app.routers.campaigns import get_campaign_store
from app.routers.preparation import get_preparation_store
from app.services.campaign_store import CampaignError, CampaignStore
from app.services.preparation_store import PreparationStore

COLLECTION = "campanha-victor"
# atraso para duas importações concorrentes lerem o mesmo estado antes de escrever
WINDOW_SECONDS = 0.35


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


    # ── atomicidade e identidade da chave (correções da revisão da etapa 2) ──

    def import_through_the_store(self, content, relative_path="notas.md", **extra):
        """Importa direto no store, para poder disparar duas importações juntas."""
        user_id = self.store.user_for_token(self.gm["Authorization"][7:])["id"]
        try:
            result = self.preparation.import_source(
                user_id,
                self.campaign_id,
                collection_id=COLLECTION,
                relative_path=relative_path,
                title="Sessão 8",
                content=content,
                **extra,
            )
            return result["status"]
        except CampaignError as error:
            return str(error.status)

    def run_together(self, jobs):
        """Roda cada job em sua thread, todos disparados no mesmo instante."""
        started = Barrier(len(jobs))
        outcomes: list[str] = []

        def run(content, **extra):
            started.wait()
            outcomes.append(self.import_through_the_store(content, **extra))

        threads = [Thread(target=run, kwargs=kwargs) for kwargs in jobs]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return outcomes

    @contextmanager
    def widened_write_window(self) -> Iterator[None]:
        """Abre a janela entre ler a fonte e escrever, onde a corrida acontecia.

        Sem atraso as threads se serializam sozinhas e o teste verde por acaso. O
        atraso entra depois da leitura da fonte e antes do `INSERT`.
        """
        original = PreparationStore._resolve_revision_precondition

        def slow(existing, expected_revision):
            sleep(WINDOW_SECONDS)
            return original(existing, expected_revision)

        with patch.object(
            PreparationStore, "_resolve_revision_precondition", staticmethod(slow)
        ):
            yield

    def preparation_rows(self, table):
        with closing(self.store._connect()) as conn:
            return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_concurrent_imports_of_one_file_do_not_collide(self):
        """Duas importações juntas do mesmo caminho: uma cria, a outra acrescenta.

        Sem a transação de escrita, as duas liam "não existe fonte" e disputavam o
        `INSERT`; o índice único de identidade impedia a duplicação, mas a perdedora
        voltava com `IntegrityError` (500) em vez de um resultado válido.
        """
        with self.widened_write_window():
            outcomes = self.run_together([{"content": "texto A"}, {"content": "texto B"}])
        self.assertEqual(sorted(outcomes), ["imported", "new_revision"])
        self.assertEqual(self.preparation_rows("preparation_sources"), 1)
        self.assertEqual(self.preparation_rows("preparation_source_revisions"), 2)

    def test_concurrent_retries_of_one_key_import_once(self):
        """Mesma chave em paralelo: uma importação, o retry devolve o mesmo resultado."""
        with self.widened_write_window():
            outcomes = self.run_together(
                [
                    {"content": "mesmo texto", "idempotency_key": "k-paralela"},
                    {"content": "mesmo texto", "idempotency_key": "k-paralela"},
                ]
            )
        self.assertEqual(outcomes, ["imported", "imported"])
        self.assertEqual(self.preparation_rows("preparation_sources"), 1)
        self.assertEqual(self.preparation_rows("preparation_source_revisions"), 1)
        self.assertEqual(self.preparation_rows("preparation_imports"), 1)

    def test_a_key_is_the_same_identity_on_read_and_on_write(self):
        """A chave é normalizada uma vez: o retry idêntico devolve o resultado original.

        Como a consulta normalizava e a gravação guardava o texto cru, o retry do
        mesmo pedido não encontrava o resultado anterior e respondia `unchanged`.
        """
        first = self.import_source("notas com moldura", idempotencyKey=" spaced-key ")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(first.json()["status"], "imported")

        retry = self.import_source("notas com moldura", idempotencyKey=" spaced-key ")
        self.assertEqual(retry.status_code, 201, retry.text)
        self.assertEqual(retry.json(), first.json())
        self.assertEqual(self.preparation_rows("preparation_source_revisions"), 1)

    def test_reusing_a_key_for_another_import_conflicts(self):
        """Chave identifica um pedido: reaproveitá-la para outra fonte é 409.

        Sem isso, a mesma chave com `relativePath` diferentes criava duas fontes e
        um único resultado idempotente — o retry deixava de ser retry.
        """
        first = self.import_source("primeira importação", idempotencyKey="chave-unica")
        self.assertEqual(first.status_code, 201, first.text)

        conflict = self.import_source(
            "outro conteúdo",
            relative_path="sessoes/outra.md",
            idempotencyKey="chave-unica",
        )
        self.assertEqual(conflict.status_code, 409, conflict.text)
        self.assertIn("idempotencyKey", conflict.text)
        self.assertEqual(self.preparation_rows("preparation_sources"), 1)

    def test_a_key_from_before_the_fingerprint_column_still_replays(self):
        """Linha antiga (sem impressão digital) não vira conflito do nada.

        A coluna `request_fingerprint` chega por `ALTER TABLE` num banco que já
        está em produção, então linhas anteriores ficam sem valor e o contrato
        antigo — devolver o resultado gravado — precisa continuar valendo.
        """
        legacy = {
            "status": "imported",
            "revision": 1,
            "source": {},
            "possibleDuplicates": [],
        }
        with closing(self.store._connect()) as conn, conn:
            conn.execute(
                """INSERT INTO preparation_imports
                (campaign_id, idempotency_key, result, created_at)
                VALUES (?, ?, ?, ?)""",
                (self.campaign_id, "chave-antiga", json.dumps(legacy), "2026-01-01T00:00:00+00:00"),
            )

        replayed = self.import_through_the_store("qualquer conteúdo", idempotency_key="chave-antiga")
        self.assertEqual(replayed, "imported")
        self.assertEqual(self.preparation_rows("preparation_sources"), 0)

    def test_control_characters_in_the_path_are_rejected_not_stripped(self):
        """O `strip()` não pode esconder caminho corrompido: controle é recusado."""
        for path in ("\nnotas.md\n", "notas\t.md", "notas\x00.md"):
            with self.subTest(path=path):
                refused = self.import_source("conteúdo", relative_path=path)
                self.assertEqual(refused.status_code, 422, refused.text)
                self.assertIn("controle", refused.text)
        self.assertEqual(self.preparation_rows("preparation_sources"), 0)


class PreparationMigrationTests(unittest.TestCase):
    """O deploy adiciona as tabelas novas sem tocar no que já existe no banco."""

    def setUp(self):
        self.directory = TemporaryDirectory()
        self.database = Path(self.directory.name) / "existing.sqlite3"

    def tearDown(self):
        self.directory.cleanup()

    def test_reopening_adds_the_fingerprint_column_and_keeps_old_rows(self):
        """O banco em produção tem `preparation_imports` sem a coluna nova.

        Reabrir a store adiciona `request_fingerprint` por `ALTER TABLE` e não
        toca nas linhas já gravadas — senão o deploy quebraria a mesa do Victor.
        """
        PreparationStore(self.database, CampaignStore(self.database))
        with closing(sqlite3.connect(self.database)) as legacy, legacy:
            legacy.execute("ALTER TABLE preparation_imports DROP COLUMN request_fingerprint")
            legacy.execute(
                """INSERT INTO preparation_imports
                (campaign_id, idempotency_key, result, created_at)
                VALUES ('campanha-antiga', 'chave', '{}', '2026-01-01T00:00:00+00:00')"""
            )

        PreparationStore(self.database, CampaignStore(self.database))

        with closing(sqlite3.connect(self.database)) as conn:
            columns = [row[1] for row in conn.execute("PRAGMA table_info(preparation_imports)")]
            keys = [row[0] for row in conn.execute("SELECT idempotency_key FROM preparation_imports")]
        self.assertIn("request_fingerprint", columns)
        self.assertEqual(keys, ["chave"])

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
