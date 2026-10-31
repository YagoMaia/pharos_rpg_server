"""MCP de preparação: transporte Streamable HTTP real, credencial e ferramentas.

Etapa 3 do plano `plans/mcp-preparacao-campanha.md` (ai-memory: workspace
`pharos`, projeto `intregração`). Aceite do plano: o agent lê notas locais e envia
o conteúdo por ferramentas MCP remotas, consultando o resultado depois, sem
instalar nada na máquina do Victor.

Estes testes sobem o app ASGI do MCP e falam com ele pelo **cliente MCP oficial**
(cliente Streamable HTTP de verdade, não chamada direta da função Python), porque o
que tem de valer é transporte + autenticação + schema, não a função isolada.

Regras verificadas:

- nenhuma chamada do protocolo passa sem credencial de preparação (401 no HTTP);
- revogar a credencial derruba a conexão em uso, sem reabrir nada;
- Host estranho é recusado (a proteção contra DNS rebinding continua ligada);
- o inventário é o do plano: sem aplicar, sem excluir, sem combate;
- `campaignId` não é argumento de ferramenta: a campanha vem da credencial;
- o conteúdo viaja fiel (acentos, CRLF, tab, emoji) e a resposta não repete o doc;
- leitura devolve janela com truncamento explícito, nunca corte silencioso;
- fonte de outra campanha não é alcançável daqui;
- nada do que o MCP faz escreve registro oficial da campanha.
"""

import asyncio
import json
import sqlite3
import unittest
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx2
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.core.config import Settings
from app.main import app
from app.preparation_mcp.server import (
    TOOL_NAMES,
    PreparationMcpApp,
    build_preparation_mcp_asgi,
    mount_preparation_mcp,
)
from app.services.campaign_store import CampaignStore
from app.services.preparation_store import PreparationStore
from app.services.preparation_tokens import PreparationTokenStore

COLLECTION = "campanha-victor"
HOST = "internal"
ENDPOINT = "/mcp"
NOTE = "Sessão 8\tanotada às pressas\r\nFerrarin mentiu sobre o porto — çãé 🎲"
PLANNED_TOOLS = (
    "connection_status",
    "import_source",
    "list_sources",
    "search_sources",
    "read_source",
    "read_campaign_context",
    "save_preparation_draft",
    "read_preparation_draft",
)


def _lifespan(app):
    """O app do MCP controla o próprio ciclo de vida; o host FastAPI não."""
    if isinstance(app, PreparationMcpApp):
        return app.lifespan()
    return app.router.lifespan_context(app)


class PreparationMcpTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        database = Path(self.directory.name) / "mcp.sqlite3"
        self.campaigns = CampaignStore(database)
        self.sources = PreparationStore(database, self.campaigns)
        self.tokens = PreparationTokenStore(database, self.campaigns)
        self.app = build_preparation_mcp_asgi(
            lambda: self.campaigns,
            lambda: self.sources,
            allowed_hosts=[HOST],
            endpoint_path=ENDPOINT,
        )
        gm_session = self.campaigns.register("gm@example.com", "Mestre", "safe-password-123")
        self.gm_id = self.campaigns.user_for_token(gm_session)["id"]
        self.campaign_id = self.campaigns.create_campaign(self.gm_id, "Pharos")["id"]
        issued = self.tokens.issue(self.gm_id, self.campaign_id, "agent do Victor")
        self.token = issued["token"]
        self.token_id = issued["id"]

    def tearDown(self):
        self.app = None
        self.directory.cleanup()

    # ── cliente MCP sobre o transporte ─────────────────────────────────────

    def _run(self, app, host, token, action):
        async def one_session():
            async with _lifespan(app):
                http = httpx2.AsyncClient(
                    transport=httpx2.ASGITransport(app=app),
                    base_url=f"http://{host}",
                    headers={"Authorization": f"Bearer {token}"},
                )
                async with http, streamable_http_client(
                    f"http://{host}{ENDPOINT}", http_client=http
                ) as streams, ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    return await action(session)

        return asyncio.run(one_session())

    def call(self, tool, arguments=None, token=None, host=HOST, app=None):
        app = app or self.app
        return self._run(
            app, host, token or self.token,
            lambda session: session.call_tool(tool, arguments or {}),
        )

    def tool_names(self, app=None):
        return sorted(tool.name for tool in self._run(app or self.app, HOST, self.token,
                                                      lambda session: session.list_tools()).tools)

    def schemas(self):
        return {tool.name: tool.input_schema for tool in
                self._run(self.app, HOST, self.token, lambda session: session.list_tools()).tools}

    def descriptions(self):
        return {tool.name: tool.description for tool in
                self._run(self.app, HOST, self.token, lambda session: session.list_tools()).tools}

    def http_probe(self, host=HOST, token=None, app=None, endpoint=ENDPOINT):
        """`initialize` cru, para olhar status e headers sem o cliente MCP."""
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "agent", "version": "1"},
                },
            }
        )
        headers = {"content-type": "application/json", "accept": "application/json"}
        if token:
            headers["authorization"] = f"Bearer {token}"

        async def probe():
            async with _lifespan(app or self.app), httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=app or self.app),
                base_url=f"http://{host}",
            ) as http:
                return await http.post(endpoint, headers=headers, content=body)

        return asyncio.run(probe())

    # ── montagens de conveniência ──────────────────────────────────────────

    def as_json(self, result) -> dict:
        return json.loads(result.content[0].text)

    def text_of(self, result) -> str:
        return "\n".join(block.text for block in result.content)

    def import_arguments(self, content: str = NOTE, **overrides) -> dict:
        arguments = {
            "collectionId": COLLECTION,
            "relativePath": "sessoes/sessao-08.md",
            "title": "Sessão 8 — anotações brutas",
            "content": content,
            "idempotencyKey": "tentativa-1",
        }
        # o SDK ignora argumento sobrando: sem esta trava um nome errado virava
        # teste verde por acidente (aconteceu com `relative_path`)
        unexpected = sorted(set(overrides) - set(arguments))
        if unexpected:
            raise AssertionError(f"argumento fora do contrato: {unexpected}")
        arguments.update(overrides)
        return arguments

    def imported_source_id(self, content: str = NOTE, **extra) -> str:
        result = self.call("import_source", self.import_arguments(content, **extra))
        self.assertFalse(result.is_error, self.text_of(result))
        return self.as_json(result)["source"]["id"]

    def other_campaign_with_token(self) -> tuple[str, str]:
        other_session = self.campaigns.register(
            "outro@example.com", "Outro Mestre", "safe-password-123"
        )
        other_id = self.campaigns.user_for_token(other_session)["id"]
        campaign = self.campaigns.create_campaign(other_id, "Outra campanha")["id"]
        return campaign, self.tokens.issue(other_id, campaign, "outro agent")["token"]

    def official_counts(self) -> dict[str, int]:
        with closing(sqlite3.connect(self.sources.path)) as conn:
            tables = ("campaign_sessions", "campaign_npcs", "campaign_quests", "campaign_encounters")
            return {
                table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in tables
            }

    # ── autenticação e transporte ──────────────────────────────────────────

    def test_no_protocol_call_survives_without_a_credential(self):
        response = self.http_probe()
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response.headers.get("www-authenticate", ""))
        self.assertNotIn(self.token, response.text)
        self.assertNotIn("token_hash", response.text)
        self.assertNotIn("pharos_prep_", response.text, "a recusa não pode ecoar um token")

    def test_a_revoked_credential_stops_mid_connection(self):
        """Revogar vale na hora, inclusive para a sessão MCP já aberta."""
        self.tokens.revoke(self.gm_id, self.campaign_id, self.token_id)
        response = self.http_probe(token=self.token)
        self.assertEqual(response.status_code, 401)

    def test_a_foreign_host_is_refused(self):
        """DNS rebinding: com credencial válida, Host estranho ainda não passa."""
        response = self.http_probe(host="evil.example.com", token=self.token)
        self.assertEqual(response.status_code, 421)

    def test_the_endpoint_travels_with_the_host_app(self):
        """Produção é `app.mount` no FastAPI: o lifespan do transporte entra junto."""
        host_app = FastAPI()
        mount_preparation_mcp(
            host_app,
            lambda: self.campaigns,
            lambda: self.sources,
            path=ENDPOINT,
            allowed_hosts=[HOST],
        )
        status = self.call("connection_status", app=host_app)
        self.assertFalse(status.is_error, self.text_of(status))
        self.assertEqual(self.as_json(status)["campaign"]["name"], "Pharos")

    def test_the_host_endpoint_answers_with_or_without_the_trailing_slash(self):
        """/mcp é o endpoint publicado no túnel: 307 de travessia não é opção."""
        host_app = FastAPI()
        mount_preparation_mcp(
            host_app,
            lambda: self.campaigns,
            lambda: self.sources,
            path=ENDPOINT,
            allowed_hosts=[HOST],
        )
        for endpoint in (ENDPOINT, f"{ENDPOINT}/"):
            with self.subTest(endpoint=endpoint):
                response = self.http_probe(app=host_app, token=self.token, endpoint=endpoint)
                self.assertEqual(response.status_code, 200, response.text)

    def test_the_mount_is_dead_outside_the_lifecycle(self):
        """Sem lifespan entrado não há sessão: responder erro, não atender no escuro."""
        host_app = FastAPI()
        mount_preparation_mcp(
            host_app,
            lambda: self.campaigns,
            lambda: self.sources,
            path=ENDPOINT,
            allowed_hosts=[HOST],
        )

        async def probe():
            async with httpx2.AsyncClient(
                transport=httpx2.ASGITransport(app=host_app), base_url=f"http://{HOST}"
            ) as http:
                return await http.post(ENDPOINT, headers={"content-type": "application/json"})

        self.assertEqual(asyncio.run(probe()).status_code, 503)

    # ── inventário de ferramentas ──────────────────────────────────────────

    def test_the_inventory_is_exactly_the_plan(self):
        self.assertEqual(self.tool_names(), sorted(PLANNED_TOOLS))
        self.assertEqual(sorted(TOOL_NAMES), sorted(PLANNED_TOOLS))

    def test_every_tool_explains_itself_to_the_model(self):
        for name, description in self.descriptions().items():
            self.assertTrue(description and description.strip(), f"{name} sem descrição")

    def test_no_tool_takes_a_campaign_identifier(self):
        """Campanha vem da credencial: o modelo não pode escolher a mesa."""
        for name, schema in self.schemas().items():
            self.assertNotIn("campaignId", schema.get("properties", {}), f"{name} aceita campaignId")
            self.assertNotIn("userId", schema.get("properties", {}), f"{name} aceita userId")

    def test_the_surface_has_no_apply_no_delete_no_combat(self):
        forbidden = ("apply", "delete", "remove", "launch", "combat", "journal", "publish")
        for name in TOOL_NAMES:
            for word in forbidden:
                self.assertNotIn(word, name.lower(), f"{name} expõe {word}")

    def test_calling_a_tool_that_is_not_exposed_does_nothing(self):
        for name in ("apply_preparation_draft", "launch_encounter", "delete_source"):
            with self.subTest(name=name):
                result = self.call(name, {})
                self.assertTrue(result.is_error)
                self.assertIn("unknown tool", self.text_of(result).lower())

    # ── fluxo do Victor: importar e reler ──────────────────────────────────

    def test_the_agent_imports_a_note_and_reads_it_back(self):
        imported = self.call("import_source", self.import_arguments())
        self.assertFalse(imported.is_error, self.text_of(imported))
        body = self.as_json(imported)
        self.assertEqual(body["status"], "imported")
        self.assertEqual(body["revision"], 1)
        self.assertEqual(len(body["source"]["checksum"]), 64)
        self.assertNotIn(NOTE, self.text_of(imported), "a resposta não deve repetir o documento")

        listed = self.as_json(self.call("list_sources"))
        self.assertEqual([item["relativePath"] for item in listed["items"]], ["sessoes/sessao-08.md"])
        self.assertEqual(listed["items"][0]["currentRevision"], 1)

        read = self.as_json(self.call("read_source", {"sourceId": body["source"]["id"]}))
        self.assertEqual(read["content"], NOTE)

    def test_the_note_arrives_byte_for_byte_through_the_transport(self):
        """Acentos, CRLF, tab e emoji chegam intactos — é o contrato da camada."""
        raw = "linha 1\r\n\tlinha 2 com acentuação çãé\xbb — emoji 🎲\r\n   espaços   \r\n"
        source_id = self.imported_source_id(raw)
        read = self.as_json(self.call("read_source", {"sourceId": source_id}))
        self.assertEqual(read["content"], raw)
        self.assertEqual(read["contentChars"], len(raw))
        self.assertFalse(read["truncated"])
        self.assertIsNone(read["nextOffset"])

    def test_search_finds_the_note_before_importing_it_twice(self):
        source_id = self.imported_source_id("Ferrarin esconde os escudos no porão")
        found = self.as_json(self.call("search_sources", {"search": "escudos"}))
        self.assertEqual([item["id"] for item in found["items"]], [source_id])
        self.assertEqual(self.as_json(self.call("search_sources", {"search": "dragão"}))["items"], [])

    def test_read_source_returns_a_window_and_marks_truncation(self):
        raw = "x" * 900
        source_id = self.imported_source_id(raw)
        window = self.as_json(
            self.call("read_source", {"sourceId": source_id, "maxChars": 300})
        )
        self.assertEqual(window["content"], "x" * 300)
        self.assertTrue(window["truncated"])
        self.assertEqual(window["contentChars"], 900)
        self.assertEqual(window["returnedChars"], 300)
        self.assertEqual(window["nextOffset"], 300)

        rest = self.as_json(
            self.call("read_source", {"sourceId": source_id, "offset": 300, "maxChars": 300})
        )
        self.assertEqual(rest["content"], "x" * 300)
        self.assertEqual(rest["nextOffset"], 600)

    def test_read_source_without_a_revision_reads_the_current_one(self):
        """O agent aponta para a fonte; o servidor sabe qual é a revisão atual."""
        source_id = self.imported_source_id("primeira versão")
        self.imported_source_id("segunda versão", relativePath="sessoes/sessao-08.md",
                                idempotencyKey="tentativa-2")
        current = self.as_json(self.call("read_source", {"sourceId": source_id}))
        self.assertEqual(current["revision"], 2)
        self.assertEqual(current["content"], "segunda versão")
        older = self.as_json(self.call("read_source", {"sourceId": source_id, "revision": 1}))
        self.assertEqual(older["content"], "primeira versão")

    def test_an_oversized_note_is_refused_without_echoing_the_content(self):
        result = self.call("import_source", self.import_arguments(content="c" * 200_001))
        self.assertTrue(result.is_error)
        self.assertIn("200000", self.text_of(result))
        self.assertNotIn("c" * 100, self.text_of(result), "o erro não devolve o recusado")

    def test_a_malicious_path_is_refused_by_the_same_rules_as_the_rest(self):
        result = self.call("import_source", self.import_arguments(relativePath="../segredo.md"))
        self.assertTrue(result.is_error)
        self.assertIn("não é permitido", self.text_of(result))

    # ── escopo entre campanhas ─────────────────────────────────────────────

    def test_a_credential_only_reaches_its_own_campaign(self):
        _, foreign_token = self.other_campaign_with_token()
        imported = self.call("import_source", self.import_arguments(), token=foreign_token)
        self.assertFalse(imported.is_error, self.text_of(imported))
        mine = self.as_json(self.call("list_sources"))
        self.assertEqual(mine["items"], [], "a outra campanha não vazou para cá")

    def test_a_source_reference_from_another_campaign_is_refused(self):
        _, foreign_token = self.other_campaign_with_token()
        foreign = self.as_json(self.call("import_source", self.import_arguments(), token=foreign_token))
        draft = self.call(
            "save_preparation_draft",
            {
                "payload": {"kind": "npc", "name": "Ferrarin", "role": "ferreiro"},
                "sourceRefs": [{"sourceId": foreign["source"]["id"], "revision": 1}],
            },
        )
        self.assertTrue(draft.is_error)
        self.assertIn("fonte inexistente", self.text_of(draft))

    def test_connection_status_never_shows_the_credential(self):
        status = self.call("connection_status")
        body = self.text_of(status)
        self.assertNotIn(self.token, body)
        payload = self.as_json(status)
        self.assertEqual(payload["campaign"]["id"], self.campaign_id)
        self.assertFalse(payload["permissions"]["applyDrafts"])
        self.assertFalse(payload["permissions"]["startCombat"])
        self.assertNotIn("token", json.dumps(payload).lower().replace("tokenid", ""))

    # ── rascunhos propostos pelo agent ─────────────────────────────────────

    def test_the_agent_saves_a_draft_linked_to_the_exact_revision(self):
        source_id = self.imported_source_id()
        saved = self.call(
            "save_preparation_draft",
            {
                "payload": {"kind": "npc", "name": "Ferrarin", "role": "ferreiro"},
                "sourceRefs": [{"sourceId": source_id, "revision": 1}],
                "uncertainties": ["quantidade de escudos não está na nota"],
            },
        )
        self.assertFalse(saved.is_error, self.text_of(saved))
        draft = self.as_json(saved)
        self.assertEqual(draft["sourceRefs"], [{"sourceId": source_id, "revision": 1}])
        self.assertEqual(draft["status"], "draft")

        read = self.as_json(self.call("read_preparation_draft", {"draftId": draft["id"]}))
        self.assertEqual(read["payload"]["name"], "Ferrarin")
        self.assertEqual(read["uncertainties"], ["quantidade de escudos não está na nota"])

    def test_revising_a_draft_without_the_read_revision_conflicts(self):
        created = self.as_json(
            self.call("save_preparation_draft", {"payload": {"kind": "npc", "name": "Ferrarin"}})
        )
        self.assertTrue(
            self.call(
                "save_preparation_draft",
                {
                    "draftId": created["id"],
                    "expectedRevision": 1,
                    "payload": {"kind": "npc", "name": "Ferrarin", "role": "ferreiro"},
                },
            ).is_error
            is False
        )
        stale = self.call(
            "save_preparation_draft",
            {
                "draftId": created["id"],
                "expectedRevision": 1,
                "payload": {"kind": "npc", "name": "Ferrarin", "role": "informante"},
            },
        )
        self.assertTrue(stale.is_error)
        self.assertIn("409", self.text_of(stale))

    def test_revising_a_draft_without_saying_which_revision_was_seen_is_refused(self):
        created = self.as_json(
            self.call("save_preparation_draft", {"payload": {"kind": "npc", "name": "Ferrarin"}})
        )
        result = self.call(
            "save_preparation_draft",
            {"draftId": created["id"], "payload": {"kind": "npc", "name": "Ferrarin"}},
        )
        self.assertTrue(result.is_error)
        self.assertIn("expectedRevision", self.text_of(result))

    def test_read_campaign_context_shows_existing_entities_to_reuse(self):
        npc = self.campaigns.create_npc(self.gm_id, self.campaign_id, "Ferrarin", "ferreiro")
        listed = self.as_json(self.call("read_campaign_context", {"kind": "npcs"}))
        self.assertEqual([item["id"] for item in listed["items"]], [npc["id"]])
        self.assertEqual(listed["items"][0]["name"], "Ferrarin")
        self.assertEqual(listed["counts"], {"npcs": 1})

    def test_read_campaign_context_keeps_gm_only_details_out_of_the_window(self):
        self.campaigns.create_npc(
            self.gm_id, self.campaign_id, "Ferrarin", "ferreiro", gm_notes="secredo do mestre"
        )
        body = self.text_of(self.call("read_campaign_context", {"kind": "npcs"}))
        self.assertNotIn("secredo do mestre", body)

    # ── nada disto toca o que os jogadores veem ────────────────────────────

    def test_preparation_through_mcp_writes_no_official_record(self):
        source_id = self.imported_source_id()
        self.assertFalse(
            self.call(
                "save_preparation_draft",
                {
                    "payload": {
                        "kind": "encounter",
                        "name": "Emboscada",
                        "monstersData": [{"name": "bandido"}],
                    },
                    "sourceRefs": [{"sourceId": source_id, "revision": 1}],
                },
            ).is_error
        )
        self.call("read_campaign_context", {"kind": "encounters"})
        self.assertEqual(self.official_counts(), dict.fromkeys(self.official_counts(), 0))


class PreparationMcpSettingsTests(unittest.TestCase):
    """A configuração decide se o endpoint existe e quais Hosts passam."""

    def test_hosts_are_a_comma_separated_list_without_empty_entries(self):
        settings = Settings(MCP_ALLOWED_HOSTS="a.example.com, b.example ,")
        self.assertEqual(settings.mcp_allowed_hosts(), ["a.example.com", "b.example"])
        self.assertEqual(settings.mcp_allowed_origins(), [])

    def test_empty_list_means_the_endpoint_is_off(self):
        self.assertEqual(Settings(MCP_ALLOWED_HOSTS="").mcp_allowed_hosts(), [])

    def test_building_without_hosts_fails_loudly_instead_of_refusing_everyone(self):
        with self.assertRaises(ValueError):
            build_preparation_mcp_asgi(lambda: None, lambda: None, allowed_hosts=[])


class ShippedAppEndpointTests(unittest.TestCase):
    def test_the_app_the_vps_runs_publishes_the_endpoint(self):
        """/mcp existe no app real e recusa quem chega sem credencial."""
        with TestClient(app) as client:
            response = client.post(
                "/mcp",
                headers={"content-type": "application/json", "accept": "application/json"},
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-06-18",
                        "capabilities": {},
                        "clientInfo": {"name": "curl", "version": "0"},
                    },
                },
            )
        self.assertEqual(response.status_code, 401)
        self.assertIn("Bearer", response.headers.get("www-authenticate", ""))


if __name__ == "__main__":
    unittest.main()
