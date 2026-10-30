"""Rotas de preparação da campanha (fontes brutas importadas pelo mestre).

Camada fina: valida o formato do pedido e delega ao `PreparationStore`, que é
onde a autorização e as regras de fidelidade do conteúdo vivem. Manter a
autorização no store é o que impede que um futuro adaptador MCP, com outro
transporte, esqueça de checar papel e campanha.

Etapa 2 do plano `plans/mcp-preparacao-campanha.md` (ai-memory: workspace
`pharos`, projeto `intregração`).
"""

from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.routers.campaigns import User, get_campaign_store
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
