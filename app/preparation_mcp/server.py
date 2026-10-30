"""Montagem do servidor MCP de preparação sobre ASGI.

Uma instância do `MCPServer` oficial dentro do próprio app FastAPI: mesmo processo,
mesmas stores, mesmo SQLite, sem segundo serviço no VPS. O transporte responde em
`/mcp` (Streamable HTTP sem estado, resposta JSON), e o middleware de credencial
envolve o app do MCP — nenhuma chamada do protocolo chega às ferramentas sem
identidade.

Duas coisas que o SDK não faz por nós e que ficaram explícitas aqui:

- **lifespan**: o app interno do MCP tem um gerenciador de sessões que precisa ser
  entrando; montado com `app.mount` ele não entra sozinho, então o lifespan do
  host é composto com o dele;
- **Host/Origin**: a proteção contra DNS rebinding do SDK vem ligada, com a lista
  de hosts vindos de configuração. Sem isso o transporte responde 421.
"""

from collections.abc import Callable, Sequence
from contextlib import asynccontextmanager
from typing import Annotated, Any, Literal

from fastapi import FastAPI
from mcp.server.mcpserver import Context, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.preparation_mcp.identity import (
    PreparationActor,
    PreparationAuthMiddleware,
    actor_from_scope,
)
from app.preparation_mcp.tools import (
    DEFAULT_CONTEXT_ITEMS,
    DEFAULT_READ_WINDOW_CHARS,
    PreparationTools,
)
from app.services.campaign_store import CampaignStore
from app.services.preparation_drafts import build_draft_store
from app.services.preparation_store import PreparationStore
from app.services.preparation_tokens import PreparationTokenStore

SERVER_NAME = "pharos-preparation"
DEFAULT_MCP_PATH = "/mcp"

# ordem e nomes são contrato: os testes e o piloto do Victor dependem deles
TOOL_NAMES = (
    "connection_status",
    "import_source",
    "list_sources",
    "search_sources",
    "read_source",
    "read_campaign_context",
    "save_preparation_draft",
    "read_preparation_draft",
)

INSTRUCTIONS = (
    "Preparação da campanha Pharos. O agent lê as notas locais com as ferramentas "
    "do seu próprio harness e envia o conteúdo por aqui; o servidor nunca abre "
    "arquivo nem disco. Regras: (1) enviar o texto integral da nota, sem resumir, "
    "corrigir ou reformatar; (2) uma idempotencyKey nova por tentativa de "
    "importação — repetir o mesmo pedido devolve o resultado anterior; (3) se a "
    "nota é grande, reler por partes usando truncated/nextOffset em vez de assumir "
    "que viu tudo; (4) organizar acontece em rascunhos, que citam (sourceId, "
    "revision) exatos; (5) nenhum rascunho é aplicado por aqui: quem aprova e "
    "aplica é o mestre, no app. Não existe ferramenta de combate, publicação, "
    "exclusão ou aplicação nesta conexão."
)

CampaignStoreFactory = Callable[[], CampaignStore]
PreparationStoreFactory = Callable[[], PreparationStore]


def _actor_of(ctx: Context) -> PreparationActor:
    """Identidade da requisição, lida do scope escrito pelo middleware."""
    return actor_from_scope(ctx.request_context.request.scope)


def register_tools(mcp: MCPServer, tools: PreparationTools) -> None:
    """Registra as oito ferramentas do plano; o `ctx` não aparece no schema."""

    @mcp.tool(
        name="connection_status",
        description=(
            "Estado da conexão: qual campanha esta credencial serve, o que ela "
            "pode e não pode fazer e os limites de tamanho. A credencial em si "
            "nunca é devolvida."
        ),
    )
    async def connection_status(ctx: Context) -> dict[str, Any]:
        return await tools.connection_status(_actor_of(ctx))

    @mcp.tool(
        name="import_source",
        description=(
            "Importa conteúdo bruto de uma nota local lida pelo agent. Enviar o "
            "texto integral, sem resumo nem reformatar; `relativePath` é metadado "
            "de origem (ex.: sessoes/sessao-08.md), nunca caminho do servidor. "
            "Gera revisão apenas se o texto mudou. Enviar `expectedRevision` com a "
            "revisão que foi lida, para não sobrescrever edição concorrente. A "
            "campanha vem da credencial: não há como escolher outra."
        ),
    )
    async def import_source(
        collectionId: Annotated[str, Field(min_length=1, max_length=120)],
        relativePath: Annotated[str, Field(min_length=1, max_length=512)],
        title: Annotated[str, Field(min_length=1, max_length=200)],
        content: str,
        ctx: Context,
        idempotencyKey: Annotated[str | None, Field(max_length=120)] = None,
        expectedRevision: Annotated[int | None, Field(ge=1)] = None,
    ) -> dict[str, Any]:
        return await tools.import_source(
            _actor_of(ctx),
            collection_id=collectionId,
            relative_path=relativePath,
            title=title,
            content=content,
            idempotency_key=idempotencyKey,
            expected_revision=expectedRevision,
        )

    @mcp.tool(
        name="list_sources",
        description=(
            "Lista as fontes brutas desta campanha com paginação por cursor. "
            "Devolve metadados (caminho, título, revisão atual, checksum, "
            "tamanho), não o texto."
        ),
    )
    async def list_sources(
        ctx: Context,
        collectionId: Annotated[str | None, Field(max_length=120)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await tools.list_sources(
            _actor_of(ctx), collection_id=collectionId, limit=limit, cursor=cursor
        )

    @mcp.tool(
        name="search_sources",
        description=(
            "Busca texto nas fontes desta campanha (título, caminho e conteúdo). "
            "Usar antes de importar para não duplicar nota já existente."
        ),
    )
    async def search_sources(
        search: Annotated[str, Field(min_length=1, max_length=200)],
        ctx: Context,
        collectionId: Annotated[str | None, Field(max_length=120)] = None,
        limit: Annotated[int, Field(ge=1, le=100)] = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await tools.list_sources(
            _actor_of(ctx),
            collection_id=collectionId,
            search=search,
            limit=limit,
            cursor=cursor,
        )

    @mcp.tool(
        name="read_source",
        description=(
            "Lê uma janela de uma revisão da fonte, na ordem em que foi guardada. "
            "Sem `revision` lê a revisão atual. `truncated`/`nextOffset` indicam "
            "que há mais texto: reler a partir do offset em vez de presumir que o "
            "documento inteiro foi visto."
        ),
    )
    async def read_source(
        sourceId: Annotated[str, Field(min_length=1, max_length=64)],
        ctx: Context,
        revision: Annotated[int | None, Field(ge=1)] = None,
        offset: Annotated[int, Field(ge=0)] = 0,
        maxChars: Annotated[int, Field(ge=1, le=20_000)] = DEFAULT_READ_WINDOW_CHARS,
    ) -> dict[str, Any]:
        return await tools.read_source(
            _actor_of(ctx), source_id=sourceId, revision=revision, offset=offset, max_chars=maxChars
        )

    @mcp.tool(
        name="read_campaign_context",
        description=(
            "O que já existe na campanha (sessões, encontros, NPCs, missões): id, "
            "título/nome e estado. Consultar antes de propor rascunho, para "
            "atualizar o registro certo em vez de criar duplicado. Não devolve "
            "notas do mestre nem dados de combate. A campanha vem da credencial."
        ),
    )
    async def read_campaign_context(
        ctx: Context,
        kind: Literal["all", "sessions", "encounters", "npcs", "quests"] = "all",
        limit: Annotated[int, Field(ge=1, le=50)] = DEFAULT_CONTEXT_ITEMS,
    ) -> dict[str, Any]:
        return await tools.read_campaign_context(_actor_of(ctx), kind=kind, limit=limit)

    @mcp.tool(
        name="save_preparation_draft",
        description=(
            "Cria (sem `draftId`) ou revisa (com `draftId` e `expectedRevision` "
            "lidos antes) uma proposta tipada: session/encounter/npc/quest. A "
            "proposta cita `(sourceId, revision)` exatos e lista incertezas. Isto "
            "não aplica nada: um humano revisa e aplica no app do mestre."
        ),
    )
    async def save_preparation_draft(
        payload: Annotated[dict[str, Any], Field(description="objeto com 'kind'")],
        ctx: Context,
        draftId: Annotated[str | None, Field(max_length=64)] = None,
        expectedRevision: Annotated[int | None, Field(ge=1)] = None,
        targetId: Annotated[str | None, Field(max_length=64)] = None,
        sourceRefs: Annotated[
            list[dict[str, Any]] | None, Field(max_length=12)
        ] = None,
        uncertainties: Annotated[
            list[Annotated[str, Field(max_length=500)]] | None, Field(max_length=20)
        ] = None,
    ) -> dict[str, Any]:
        return await tools.save_preparation_draft(
            _actor_of(ctx),
            payload=payload,
            draft_id=draftId,
            expected_revision=expectedRevision,
            target_id=targetId,
            source_refs=sourceRefs,
            uncertainties=uncertainties,
        )

    @mcp.tool(
        name="read_preparation_draft",
        description=(
            "Lê um rascunho: payload, revisão atual, fontes citadas, alvos "
            "possíveis e avisos de visibilidade para jogadores."
        ),
    )
    async def read_preparation_draft(
        draftId: Annotated[str, Field(min_length=1, max_length=64)], ctx: Context
    ) -> dict[str, Any]:
        return await tools.read_preparation_draft(_actor_of(ctx), draft_id=draftId)


class PreparationMcpApp:
    """ASGI callable que ganha um app MCP novo a cada ciclo de vida.

    Serve sozinho (`async with app.lifespan()`) ou montado em um host maior; nos
    dois casos o transporte é reconstruído quando o ciclo de vida recomeça, porque
    o gerenciador de sessões do SDK aceita `run()` uma única vez por instância.
    """

    def __init__(self, build: Callable[[], Starlette], endpoint_path: str = "/"):
        self._build = build
        self._endpoint = endpoint_path
        self._active: Starlette | None = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "lifespan":
            await self._idle(scope, receive, send)
            return
        if scope["type"] == "http" and self._needs_path(scope):
            # atendida por rota exata do host ou por mount: o caminho que chega
            # pode vir vazio ou com barra final. Normalizar evita o 307 do
            # roteador interno, que cliente nenhum é obrigado a seguir.
            scope["path"] = self._endpoint
        if self._active is None:
            await JSONResponse(
                {"detail": "Endpoint MCP fora do ciclo de vida do servidor."},
                status_code=503,
            )(scope, receive, send)
            return
        await self._active(scope, receive, send)

    def _needs_path(self, scope: Scope) -> bool:
        path = scope.get("path", "")
        return path in ("", "/", f"{self._endpoint}/")

    @asynccontextmanager
    async def lifespan(self):
        """Constrói o transporte, entra no lifespan dele e desmonta depois."""
        app = self._build()
        self._active = app
        try:
            async with app.router.lifespan_context(app):
                yield app
        finally:
            self._active = None

    @staticmethod
    async def _idle(scope: Scope, receive: Receive, send: Send) -> None:
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({"type": "lifespan.startup.complete"})
            elif message["type"] == "lifespan.shutdown":
                await send({"type": "lifespan.shutdown.complete"})
                return


def build_preparation_mcp_asgi(
    campaigns: CampaignStoreFactory,
    sources: PreparationStoreFactory,
    *,
    allowed_hosts: Sequence[str],
    allowed_origins: Sequence[str] = (),
    endpoint_path: str = "/",
) -> PreparationMcpApp:
    """App ASGI do MCP: credencial + transporte Streamable HTTP + ferramentas.

    O app interno do SDK é montado dentro de `lifespan()`, não aqui: o gerenciador
    de sessões do SDK aceita `run()` **uma vez por instância**, então religar o
    mesmo app no mesmo processo (reload, segundo `TestClient`, restart de ASGI)
    morria em `RuntimeError`. Construir por ciclo de vida fecha essa armadilha.
    """
    hosts = [host.strip() for host in allowed_hosts if host.strip()]
    if not hosts:
        raise ValueError(
            "allowed_hosts obrigatório: sem ele o transporte MCP recusaria tudo (421)"
        )
    return PreparationMcpApp(
        lambda: _preparation_transport(
            campaigns, sources, hosts, list(allowed_origins), endpoint_path
        ),
        endpoint_path,
    )


def _preparation_transport(
    campaigns: CampaignStoreFactory,
    sources: PreparationStoreFactory,
    hosts: Sequence[str],
    allowed_origins: Sequence[str],
    endpoint_path: str,
) -> Starlette:
    """Uma instância do transporte, com as stores injetadas pelas fábricas."""
    campaign_store = campaigns()
    source_store = sources()
    tools = PreparationTools(
        campaigns=campaign_store,
        sources=source_store,
        drafts=build_draft_store(str(source_store.path), campaign_store, source_store),
    )
    mcp = MCPServer(name=SERVER_NAME, instructions=INSTRUCTIONS)
    register_tools(mcp, tools)
    app = mcp.streamable_http_app(
        streamable_http_path=endpoint_path,
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(hosts),
            allowed_origins=[origin.strip() for origin in allowed_origins if origin.strip()],
        ),
    )
    app.add_middleware(
        PreparationAuthMiddleware,
        tokens=PreparationTokenStore(campaign_store.path, campaign_store),
    )
    return app


def mount_preparation_mcp(
    host: FastAPI,
    campaigns: CampaignStoreFactory,
    sources: PreparationStoreFactory,
    *,
    path: str = DEFAULT_MCP_PATH,
    allowed_hosts: Sequence[str],
    allowed_origins: Sequence[str] = (),
) -> PreparationMcpApp:
    """Monta o MCP no app existente e entra no lifespan dele junto com o host.

    O transporte responde no próprio `path` (o app interno fica em `/`), então o
    endpoint do VPS é `https://<host>/mcp` sem redirect.
    """
    mcp_app = build_preparation_mcp_asgi(
        campaigns,
        sources,
        allowed_hosts=allowed_hosts,
        allowed_origins=allowed_origins,
        endpoint_path=path,
    )
    # rota exata, não `mount`: montado, o caminho sem barra virava 307 para
    # "/mcp/", e cliente que não segue redirect ficaria pelo caminho. Aqui as duas
    # formas atendem direto.
    host.add_route(path, mcp_app)
    host.add_route(f"{path}/", mcp_app)
    host_lifespan = host.router.lifespan_context

    @asynccontextmanager
    async def combined_lifespan(app: FastAPI):
        # o gerenciador de sessões do MCP vive com o app: sem isto o transporte
        # montado responde erro em vez de atender
        async with host_lifespan(app), mcp_app.lifespan():
            yield

    host.router.lifespan_context = combined_lifespan
    return mcp_app
