"""As operações por trás das ferramentas MCP de preparação.

Camada fina de propósito: cada método só resolve a identidade da credencial,
chama a mesma store que a REST já usa e traduz `CampaignError` para o erro de
ferramenta do MCP. Nada aqui decide regra de negócio — se decidessem, o contrato
da REST e o do MCP divergiriam e a revisão humana passaria a valer menos.

O que esta camada garante:

- campanha vem do ator, nunca do argumento (o modelo não escolhe a mesa);
- limite de tamanho nas respostas: `read_source` devolve janela com marcador de
  truncamento, `read_campaign_context` devolve o suficiente para o agent apontar
  uma entidade existente sem receber o registro oficial inteiro;
- log estruturado e sanitizado: ferramenta, campanha, credencial e resultado —
  nunca o conteúdo da nota nem o token.
"""

import json
import logging
from collections.abc import Callable, Sequence
from functools import partial
from typing import Any, TypeVar

import anyio
from mcp.server.mcpserver.exceptions import ToolError

from app.preparation_mcp.identity import PreparationActor
from app.services.campaign_store import CampaignStore
from app.services.errors import CampaignError
from app.services.preparation_drafts import MAX_SOURCE_REFS, PreparationDraftStore
from app.services.preparation_store import MAX_CONTENT_CHARS, PreparationStore

logger = logging.getLogger("pharos.preparation_mcp")

# janela de leitura de fonte: o agent relê por partes, não recebe o documento todo
DEFAULT_READ_WINDOW_CHARS = 8_000
MAX_READ_WINDOW_CHARS = 20_000
# entidades oficiais devolvidas por tipo para o agent escolher o alvo de um update
DEFAULT_CONTEXT_ITEMS = 25
MAX_CONTEXT_ITEMS = 50

ResultT = TypeVar("ResultT")

CONTEXT_KINDS = ("sessions", "encounters", "npcs", "quests")


class PreparationTools:
    """Mesmas stores da REST, chamadas pelo MCP. Injetadas, nunca importadas."""

    def __init__(
        self,
        campaigns: CampaignStore,
        sources: PreparationStore,
        drafts: PreparationDraftStore,
    ):
        self.campaigns = campaigns
        self.sources = sources
        self.drafts = drafts

    # ── estado da conexão ──────────────────────────────────────────────────

    async def connection_status(self, actor: PreparationActor) -> dict[str, Any]:
        """O que esta credencial alcança — sem nunca repetir a credencial."""
        campaign = await self._call(
            "connection_status", actor, self.campaigns.get_campaign, actor.user_id, actor.campaign_id
        )
        return {
            "campaign": {
                "id": campaign["id"],
                "name": campaign["name"],
                "role": campaign["role"],
            },
            "credential": {"tokenId": actor.token_id, "scope": "preparation"},
            "permissions": {
                "importSources": True,
                "readSources": True,
                "saveDrafts": True,
                "applyDrafts": False,
                "startCombat": False,
                "publishToPlayers": False,
            },
            "limits": {
                "maxContentChars": MAX_CONTENT_CHARS,
                "maxReadWindowChars": MAX_READ_WINDOW_CHARS,
                "maxDraftSourceRefs": MAX_SOURCE_REFS,
            },
        }

    # ── fontes brutas ──────────────────────────────────────────────────────

    async def import_source(
        self,
        actor: PreparationActor,
        collection_id: str,
        relative_path: str,
        title: str,
        content: str,
        idempotency_key: str | None = None,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        """Guarda o texto exatamente como chegou; a resposta não o repete."""
        return await self._call(
            "import_source",
            actor,
            self.sources.import_source,
            actor.user_id,
            actor.campaign_id,
            collection_id=collection_id,
            relative_path=relative_path,
            title=title,
            content=content,
            idempotency_key=idempotency_key,
            expected_revision=expected_revision,
        )

    async def list_sources(
        self,
        actor: PreparationActor,
        collection_id: str | None = None,
        search: str | None = None,
        limit: int = 25,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        return await self._call(
            "list_sources",
            actor,
            self.sources.list_sources,
            actor.user_id,
            actor.campaign_id,
            search=search,
            collection_id=collection_id,
            limit=limit,
            cursor=cursor,
        )

    async def read_source(
        self,
        actor: PreparationActor,
        source_id: str,
        revision: int | None = None,
        offset: int = 0,
        max_chars: int = DEFAULT_READ_WINDOW_CHARS,
    ) -> dict[str, Any]:
        """Janela de uma revisão; sem número, a atual. Truncamento é explícito.

        O agent relê por partes em vez de receber o documento inteiro de uma vez.
        O texto da janela é o que está guardado, sem recorte silencioso: quando
        sobra conteúdo, `truncated`/`nextOffset` dizem onde continuar.
        """
        window = max(1, min(int(max_chars or 1), MAX_READ_WINDOW_CHARS))
        start = max(0, int(offset or 0))
        if start and revision is None:
            # `nextOffset` sem `revision` releria a revisão *atual*, que pode ter
            # mudado entre uma janela e outra: as metades viriam de revisões
            # diferentes e o agent montaria um texto que nunca existiu.
            raise ToolError(
                "422: ao continuar uma janela, reenviar o revision da leitura "
                "anterior junto com o offset."
            )
        stored = await self._call(
            "read_source", actor, self.sources.read_revision,
            actor.user_id, actor.campaign_id, source_id, revision,
        )
        body = stored.pop("content")
        # passar do fim é trazido para o fim: `offset == contentChars` e janela vazia
        # dizem "terminou", enquanto `offset` enorme com `truncated: false` leria
        # "documento vazio aqui" e o agent juraria que a fonte está em branco.
        start = min(start, len(body))
        chunk = body[start : start + window]
        stored["content"] = chunk
        stored["offset"] = start
        stored["returnedChars"] = len(chunk)
        stored["truncated"] = start + len(chunk) < len(body)
        stored["nextOffset"] = start + len(chunk) if stored["truncated"] else None
        return stored

    # ── contexto oficial da campanha ───────────────────────────────────────

    async def read_campaign_context(
        self, actor: PreparationActor, kind: str = "all", limit: int = DEFAULT_CONTEXT_ITEMS
    ) -> dict[str, Any]:
        """O que já existe na mesa, para o rascunho apontar para o registro certo.

        Devolve identidade e estado, não o registro inteiro: `gmNotes`,
        `combatData` e descrições ficam de fora para a resposta caber na conversa.
        """
        page = max(1, min(int(limit or 1), MAX_CONTEXT_ITEMS))
        listers = {
            "sessions": self.campaigns.list_sessions,
            "encounters": self.campaigns.list_encounters,
            "npcs": self.campaigns.list_npcs,
            "quests": self.campaigns.list_quests,
        }
        items: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for name, lister in listers.items():
            if kind != "all" and kind != name:
                continue
            rows = await self._call(name, actor, lister, actor.user_id, actor.campaign_id)
            counts[name] = len(rows)
            items.extend(self._brief(name, row) for row in rows[:page])
        return {
            "kind": kind,
            "counts": counts,
            "truncated": any(total > page for total in counts.values()),
            "items": items,
        }

    @staticmethod
    def _brief(kind: str, row: dict[str, Any]) -> dict[str, Any]:
        identity = {"id": row["id"], "kind": kind, "status": row.get("status", "")}
        if kind == "sessions":
            identity |= {
                "sessionNumber": row["sessionNumber"],
                "title": row["title"],
                "scheduledAt": row.get("scheduledAt"),
            }
        elif kind == "quests":
            identity |= {"title": row["title"], "visibility": row["visibility"]}
        elif kind == "npcs":
            identity |= {
                "name": row["name"],
                "role": row["role"],
                "visibility": row["visibility"],
            }
        else:
            identity |= {"name": row["name"]}
        return identity

    # ── rascunhos propostos pelo agent ─────────────────────────────────────

    async def save_preparation_draft(
        self,
        actor: PreparationActor,
        payload: dict[str, Any],
        draft_id: str | None = None,
        expected_revision: int | None = None,
        target_id: str | None = None,
        source_refs: Sequence[dict[str, Any]] | None = None,
        uncertainties: Sequence[str] | None = None,
    ) -> dict[str, Any]:
        """Cria (sem `draftId`) ou revise (com `draftId` + `expectedRevision`)."""
        if draft_id is None:
            return await self._call(
                "save_preparation_draft",
                actor,
                self.drafts.create_draft,
                actor.user_id,
                actor.campaign_id,
                payload=payload,
                target_id=target_id,
                source_refs=list(source_refs or []),
                uncertainties=list(uncertainties or []),
            )
        if expected_revision is None:
            raise ToolError(
                "422: expectedRevision obrigatório para revisar um rascunho. Ler o "
                "rascunho antes e enviar a revisão que foi vista."
            )
        if source_refs is None:
            # `update_draft` substitui os vínculos: sem esta trava o agent conserta
            # um título e perde a procedência que a revisão humana confere.
            raise ToolError(
                "422: sourceRefs obrigatório para revisar um rascunho. Ler o "
                "rascunho antes e reenviar as fontes citadas; para desvincular, "
                "enviar sourceRefs com lista vazia."
            )
        if target_id is None:
            # Omitir o alvo não pode transformar "atualiza o NPC X" em "cria um NPC
            # novo": a intenção do rascunho é o que o mestre revisa antes de aplicar.
            seen = await self._call(
                "read_preparation_draft", actor, self.drafts.read_draft,
                actor.user_id, actor.campaign_id, draft_id,
            )
            target_id = seen.get("targetId")
        return await self._call(
            "save_preparation_draft",
            actor,
            self.drafts.update_draft,
            actor.user_id,
            actor.campaign_id,
            draft_id,
            payload=payload,
            expected_revision=expected_revision,
            target_id=target_id,
            source_refs=list(source_refs),
            # `None` passa direto: a store preserva as incertezas já escritas, e
            # `[]` continua sendo a única forma de limpar a lista.
            uncertainties=None if uncertainties is None else list(uncertainties),
        )

    async def read_preparation_draft(
        self, actor: PreparationActor, draft_id: str
    ) -> dict[str, Any]:
        return await self._call(
            "read_preparation_draft", actor, self.drafts.read_draft,
            actor.user_id, actor.campaign_id, draft_id,
        )

    # ── execução ───────────────────────────────────────────────────────────

    async def _call(
        self,
        tool: str,
        actor: PreparationActor,
        operation: Callable[..., ResultT],
        *args: Any,
        **kwargs: Any,
    ) -> ResultT:
        """Store síncrona fora do event loop, erro de domínio legível para o agent."""
        try:
            outcome = await anyio.to_thread.run_sync(partial(operation, *args, **kwargs))
        except CampaignError as error:
            self._log(tool, actor, outcome=f"{error.status}")
            raise ToolError(f"{error.status}: {error}") from error
        self._log(tool, actor, outcome=_outcome_of(outcome))
        return outcome

    @staticmethod
    def _log(tool: str, actor: PreparationActor, outcome: str) -> None:
        """Sem conteúdo de nota e sem token: só o bastante para auditar a mesa.

        O ator entra como argumento explícito: `args[0]` das stores é o `user_id`
        (uma string), e ler `campaign_id` dela devolvia `None` — a auditoria ficava
        Registrada sem dizer em qual mesa nem com qual credencial.
        """
        logger.info(
            "preparation_mcp %s",
            json.dumps(
                {
                    "tool": tool,
                    "campaignId": getattr(actor, "campaign_id", None),
                    "tokenId": getattr(actor, "token_id", None),
                    "outcome": outcome,
                },
                ensure_ascii=False,
            ),
        )


def _outcome_of(outcome: Any) -> str:
    if isinstance(outcome, dict):
        return str(outcome.get("status") or "ok")
    return "ok" if outcome is not None else "empty"
