"""Rotas de preparação: fontes brutas do mestre e rascunhos organizados.

Camada fina: valida o formato do pedido e delega às stores de preparação, que são
onde a autorização e as regras de conteúdo vivem. Manter a autorização nas stores
é o que impede que um futuro adaptador MCP, com outro transporte, esqueça de
checar papel e campanha.

Etapas 1, 2, 4 e 5 do plano `plans/mcp-preparacao-campanha.md` (ai-memory:
workspace `pharos`, projeto `intregração`). Duas identidades entram aqui:

- o **GM logado no app** (token de sessão normal), que pode tudo na preparação,
  inclusive aplicar rascunho;
- a **credencial de preparação** (`pharos_prep_...`), emitida pelo GM para o agent
  dele: importa fontes e cria/edita rascunhos da própria campanha, e é recusada na
  rota de aplicação — publicar continua sendo decisão humana no app.

Todas as rotas deste arquivo aceitam as duas; nenhuma outra rota da API aceita a
credencial de preparação.
"""

from functools import lru_cache
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.routers.campaigns import Store, bearer, get_campaign_store
from app.services.preparation_applications import MAX_IDEMPOTENCY_KEY_CHARS
from app.services.preparation_apply import PreparationApplyStore, build_apply_store
from app.services.preparation_draft_payloads import MAX_SQLITE_INTEGER
from app.services.preparation_drafts import PreparationDraftStore, build_draft_store
from app.services.preparation_store import PreparationStore
from app.services.preparation_tokens import (
    MAX_LABEL_CHARS,
    PreparationTokenStore,
)

router = APIRouter()


@lru_cache(maxsize=1)
def get_preparation_store() -> PreparationStore:
    return PreparationStore(settings.CAMPAIGN_DB_PATH, get_campaign_store())


Preparation = Annotated[PreparationStore, Depends(get_preparation_store)]


@lru_cache(maxsize=8)
def get_token_store(campaigns: Store) -> PreparationTokenStore:
    """Derivado da store injetada: mesmo banco, mesma autorização de GM."""
    return PreparationTokenStore(campaigns.path, campaigns)


Tokens = Annotated[PreparationTokenStore, Depends(get_token_store)]


def preparation_actor(
    campaign_id: Annotated[str, Path()],
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    campaigns: Store,
    tokens: Tokens,
) -> dict[str, Any]:
    """Quem está chamando a preparação: o GM no app ou uma credencial de preparação.

    A credencial resolve para o usuário do GM, então os stores continuam checando
    `assert_gm` — o token não aumenta privilégio nenhum. O que muda é o alcance:
    só as rotas deste arquivo aceitam credencial, e `viaPreparationToken` fica no
    resultado para a rota de aplicação recusar publicação automática.
    """
    raw = credentials.credentials if credentials else ""
    actor = tokens.resolve(raw)
    if actor:
        if actor["campaignId"] != campaign_id:
            raise HTTPException(
                status.HTTP_403_FORBIDDEN,
                "esta credencial de preparação pertence a outra campanha.",
            )
        return {"id": actor["userId"], "viaPreparationToken": actor["tokenId"]}
    user = campaigns.user_for_token(raw) if raw else None
    if not user:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Autenticação necessária.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


Actor = Annotated[dict, Depends(preparation_actor)]


class ImportSourceBody(BaseModel):
    """Conteúdo bruto lido pelo agent do mestre; o servidor não o reformatar."""

    model_config = ConfigDict(str_strip_whitespace=False)

    collectionId: str
    relativePath: str
    title: str
    content: str
    idempotencyKey: str | None = None
    expectedRevision: int | None = Field(default=None, ge=1, le=MAX_SQLITE_INTEGER)


@router.post("/campaigns/{campaign_id}/preparation/sources/import", status_code=201)
def import_source(
    campaign_id: str, body: ImportSourceBody, actor: Actor, preparation: Preparation
):
    return preparation.import_source(
        actor["id"],
        campaign_id,
        collection_id=body.collectionId,
        relative_path=body.relativePath,
        title=body.title,
        content=body.content,
        idempotency_key=body.idempotencyKey,
        expected_revision=body.expectedRevision,
    )


@router.get("/campaigns/{campaign_id}/preparation/sources")
def list_sources(
    campaign_id: str,
    actor: Actor,
    preparation: Preparation,
    q: str | None = Query(default=None, max_length=300),
    collectionId: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
):
    return preparation.list_sources(
        actor["id"], campaign_id, search=q, collection_id=collectionId, limit=limit, cursor=cursor
    )


@router.get("/campaigns/{campaign_id}/preparation/sources/{source_id}/revisions/{revision}")
def read_source_revision(
    campaign_id: str,
    source_id: str,
    revision: Annotated[int, Path(ge=1, le=MAX_SQLITE_INTEGER)],
    actor: Actor,
    preparation: Preparation,
):
    return preparation.read_revision(actor["id"], campaign_id, source_id, revision)


# ── rascunhos (etapa 4): organizar sem tocar nas fontes nem nos registros ────


class DraftBody(BaseModel):
    """Proposta tipada. O `payload` é validado pelo store, pelo tipo de `kind`."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    payload: dict
    targetId: str | None = Field(default=None, max_length=64)
    sourceRefs: list[dict] | None = None
    uncertainties: list[str] | None = None


class DraftUpdateBody(DraftBody):
    """Edição exige a revisão lida; rascunho já aplicado não é editável."""

    expectedRevision: int = Field(ge=1, le=MAX_SQLITE_INTEGER)
    status: Literal["draft", "approved", "rejected"] | None = None


class ApplyBody(BaseModel):
    """Pedido de aplicação: o mestre afirma que leu os avisos de visibilidade.

    `acknowledgePlayerVisibility` existe porque o servidor conhece o risco (resumo
    de sessão e `visibility=party` chegam aos jogadores) mas não pode decidir pelo
    mestre o que pode ser publicado.
    """

    model_config = ConfigDict(extra="forbid")

    idempotencyKey: str | None = Field(
        default=None, min_length=1, max_length=MAX_IDEMPOTENCY_KEY_CHARS
    )
    acknowledgePlayerVisibility: bool = False


def get_draft_store(campaigns: Store, preparation: Preparation) -> PreparationDraftStore:
    """Derivado das stores injetadas, para herdar banco e autorizações existentes."""
    return build_draft_store(preparation.path, campaigns, preparation)


Drafts = Annotated[PreparationDraftStore, Depends(get_draft_store)]


@router.post("/campaigns/{campaign_id}/preparation/drafts", status_code=201)
def create_draft(campaign_id: str, body: DraftBody, actor: Actor, drafts: Drafts):
    return drafts.create_draft(
        actor["id"],
        campaign_id,
        payload=body.payload,
        target_id=body.targetId,
        source_refs=body.sourceRefs,
        uncertainties=body.uncertainties,
    )


@router.get("/campaigns/{campaign_id}/preparation/drafts")
def list_drafts(
    campaign_id: str,
    actor: Actor,
    drafts: Drafts,
    kind: Literal["session", "encounter", "npc", "quest"] | None = Query(default=None),
    status: Literal["draft", "approved", "rejected"] | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
):
    return drafts.list_drafts(
        actor["id"], campaign_id, kind=kind, status=status, limit=limit, cursor=cursor
    )


@router.get("/campaigns/{campaign_id}/preparation/drafts/{draft_id}")
def read_draft(campaign_id: str, draft_id: str, actor: Actor, drafts: Drafts):
    return drafts.read_draft(actor["id"], campaign_id, draft_id)


@router.put("/campaigns/{campaign_id}/preparation/drafts/{draft_id}")
def update_draft(
    campaign_id: str, draft_id: str, body: DraftUpdateBody, actor: Actor, drafts: Drafts
):
    return drafts.update_draft(
        actor["id"],
        campaign_id,
        draft_id,
        payload=body.payload,
        expected_revision=body.expectedRevision,
        target_id=body.targetId,
        source_refs=body.sourceRefs,
        uncertainties=body.uncertainties,
        status=body.status,
    )


# ── aplicação (etapa 5): rascunho revisado vira registro oficial ─────────────


def get_apply_store(campaigns: Store, preparation: Preparation) -> PreparationApplyStore:
    """Derivado das mesmas stores: banco e autorização compartilhados."""
    return build_apply_store(preparation.path, campaigns, preparation)


Applies = Annotated[PreparationApplyStore, Depends(get_apply_store)]


@router.post("/campaigns/{campaign_id}/preparation/drafts/{draft_id}/apply", status_code=201)
def apply_draft(campaign_id: str, draft_id: str, body: ApplyBody, actor: Actor, apply: Applies):
    """Escreve o destino oficial e registra a aplicação numa transação só."""
    return apply.apply(
        actor["id"],
        campaign_id,
        draft_id,
        idempotency_key=body.idempotencyKey,
        acknowledge_player_visibility=body.acknowledgePlayerVisibility,
        # o agent pode preparar tudo; aplicar exige o mestre logado no app
        via_preparation_token=bool(actor.get("viaPreparationToken")),
    )


# ── credenciais de preparação (etapa 1): o agent entra, mas não publica ──────


class TokenBody(BaseModel):
    """Rótulo para humanizar a lista ("notebook do Victor"); nunca segredo."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(default="", max_length=MAX_LABEL_CHARS)


@router.post("/campaigns/{campaign_id}/preparation/tokens", status_code=201)
def issue_token(campaign_id: str, body: TokenBody, actor: Actor, tokens: Tokens):
    """Única vez em que o valor em claro existe; no banco fica o hash."""
    return tokens.issue(actor["id"], campaign_id, body.label)


@router.get("/campaigns/{campaign_id}/preparation/tokens")
def list_tokens(campaign_id: str, actor: Actor, tokens: Tokens):
    return tokens.list_tokens(actor["id"], campaign_id)


@router.delete("/campaigns/{campaign_id}/preparation/tokens/{token_id}")
def revoke_token(campaign_id: str, token_id: str, actor: Actor, tokens: Tokens):
    return tokens.revoke(actor["id"], campaign_id, token_id)
