"""Rotas de preparação: fontes brutas do mestre e rascunhos organizados.

Camada fina: valida o formato do pedido e delega às stores de preparação, que são
onde a autorização e as regras de conteúdo vivem. Manter a autorização nas stores
é o que impede que um futuro adaptador MCP, com outro transporte, esqueça de
checar papel e campanha.

Etapas 2 e 4 do plano `plans/mcp-preparacao-campanha.md` (ai-memory: workspace
`pharos`, projeto `intregração`). Não há rota de aplicação de rascunho aqui: isso
é a etapa 5, e exige aprovação humana no app, não a credencial do agent.
"""

from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.routers.campaigns import Store, User, get_campaign_store
from app.services.preparation_drafts import PreparationDraftStore, build_draft_store
from app.services.preparation_store import PreparationStore

router = APIRouter()


@lru_cache(maxsize=1)
def get_preparation_store() -> PreparationStore:
    return PreparationStore(settings.CAMPAIGN_DB_PATH, get_campaign_store())


Preparation = Annotated[PreparationStore, Depends(get_preparation_store)]


class ImportSourceBody(BaseModel):
    """Conteúdo bruto lido pelo agent do mestre; o servidor não o reformatar."""

    model_config = ConfigDict(str_strip_whitespace=False)

    collectionId: str
    relativePath: str
    title: str
    content: str
    idempotencyKey: str | None = None
    expectedRevision: int | None = Field(default=None, ge=1)


@router.post("/campaigns/{campaign_id}/preparation/sources/import", status_code=201)
def import_source(
    campaign_id: str, body: ImportSourceBody, user: User, preparation: Preparation
):
    return preparation.import_source(
        user["id"],
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
    user: User,
    preparation: Preparation,
    q: str | None = Query(default=None, max_length=300),
    collectionId: str | None = Query(default=None, max_length=120),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
):
    return preparation.list_sources(
        user["id"], campaign_id, search=q, collection_id=collectionId, limit=limit, cursor=cursor
    )


@router.get("/campaigns/{campaign_id}/preparation/sources/{source_id}/revisions/{revision}")
def read_source_revision(
    campaign_id: str,
    source_id: str,
    revision: int,
    user: User,
    preparation: Preparation,
):
    return preparation.read_revision(user["id"], campaign_id, source_id, revision)


# ── rascunhos (etapa 4): organizar sem tocar nas fontes nem nos registros ────


class DraftBody(BaseModel):
    """Proposta tipada. O `payload` é validado pelo store, pelo tipo de `kind`."""

    model_config = ConfigDict(str_strip_whitespace=False, extra="forbid")

    payload: dict
    targetId: str | None = Field(default=None, max_length=64)
    sourceRefs: list[dict] | None = None
    uncertainties: list[str] | None = None


class DraftUpdateBody(DraftBody):
    """Edição exige a revisão lida; `applied` só existe a partir da etapa 5."""

    expectedRevision: int = Field(ge=1)
    status: Literal["draft", "approved", "rejected"] | None = None


def get_draft_store(campaigns: Store, preparation: Preparation) -> PreparationDraftStore:
    """Derivado das stores injetadas, para herdar banco e autorizações existentes."""
    return build_draft_store(preparation.path, campaigns, preparation)


Drafts = Annotated[PreparationDraftStore, Depends(get_draft_store)]


@router.post("/campaigns/{campaign_id}/preparation/drafts", status_code=201)
def create_draft(campaign_id: str, body: DraftBody, user: User, drafts: Drafts):
    return drafts.create_draft(
        user["id"],
        campaign_id,
        payload=body.payload,
        target_id=body.targetId,
        source_refs=body.sourceRefs,
        uncertainties=body.uncertainties,
    )


@router.get("/campaigns/{campaign_id}/preparation/drafts")
def list_drafts(
    campaign_id: str,
    user: User,
    drafts: Drafts,
    kind: Literal["session", "encounter", "npc", "quest"] | None = Query(default=None),
    status: Literal["draft", "approved", "rejected"] | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=100),
    cursor: str | None = Query(default=None, max_length=200),
):
    return drafts.list_drafts(
        user["id"], campaign_id, kind=kind, status=status, limit=limit, cursor=cursor
    )


@router.get("/campaigns/{campaign_id}/preparation/drafts/{draft_id}")
def read_draft(campaign_id: str, draft_id: str, user: User, drafts: Drafts):
    return drafts.read_draft(user["id"], campaign_id, draft_id)


@router.put("/campaigns/{campaign_id}/preparation/drafts/{draft_id}")
def update_draft(
    campaign_id: str, draft_id: str, body: DraftUpdateBody, user: User, drafts: Drafts
):
    return drafts.update_draft(
        user["id"],
        campaign_id,
        draft_id,
        payload=body.payload,
        expected_revision=body.expectedRevision,
        target_id=body.targetId,
        source_refs=body.sourceRefs,
        uncertainties=body.uncertainties,
        status=body.status,
    )
