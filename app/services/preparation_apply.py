"""Aplicação transacional de rascunhos revisados (Etapa 5 do plano).

Aceite do plano `plans/mcp-preparacao-campanha.md`: *aprovação humana cria/atualiza
destinos sem vazamento nem efeito em combate*. Este módulo é o único caminho que
transforma rascunho em registro oficial, e faz isso sob regras duras:

- **Uma transação só**: escrita no destino oficial + registro da aplicação +
  aprovação do rascunho. Se qualquer passo falha, nada fica — sem registro órfão
  e sem rascunho aprovado sem registro.
- **Idempotência**: a mesma `idempotencyKey` devolve o mesmo resultado guardado,
  sem escrever de novo. Retries do agent são esperados, não excepcionais.
- **Sem sobrescrita cega**: rascunho que atualiza um destino existente só aplica se
  o destino estiver exatamente como quando a proposta foi escrita
  (`preparation_targets.assert_target_unchanged`).
- **Visibilidade é decisão humana**: conteúdo que vai para campo lido pelos
  jogadores exige `acknowledgePlayerVisibility=true` no pedido. O servidor sabe o
  risco (`campaign_sessions` não tem coluna de visibilidade), mas quem decide é o
  mestre.
- **Nada de combate**: só `INSERT`/`UPDATE` nas tabelas de planejamento. Iniciar
  encontro continua sendo a rota de lançamento, com WebSocket e Redis próprios.

Autorização vem de `CampaignStore.assert_gm`, antes de qualquer leitura.
"""

import json
import sqlite3
from collections.abc import Callable
from contextlib import closing
from functools import lru_cache
from typing import Any

from app.core.database import open_connection
from app.services import campaign_records
from app.services.campaign_store import CampaignError, CampaignStore
from app.services.preparation_applications import (
    application_by_key,
    latest_application,
    record_application,
    stored_result,
)
from app.services.preparation_draft_payloads import exposure_warnings, parse_payload
from app.services.preparation_drafts import PreparationDraftStore, build_draft_store
from app.services.preparation_store import PreparationStore
from app.services.preparation_targets import assert_target_unchanged

# campo do payload (camelCase) -> coluna oficial. Lista fechada: campo que não
# está aqui não chega ao banco, então aprovar um rascunho não cria coluna nova.
COLUMNS_BY_KIND = {
    "session": {
        "sessionNumber": "session_number",
        "title": "title",
        "summary": "summary",
        "scheduledAt": "scheduled_at",
        "status": "status",
    },
    "npc": {
        "name": "name",
        "role": "role",
        "attitude": "attitude",
        "description": "description",
        "gmNotes": "gm_notes",
        "combatData": "combat_data",
        "visibility": "visibility",
    },
    "quest": {
        "title": "title",
        "description": "description",
        "reward": "reward",
        "status": "status",
        "visibility": "visibility",
    },
    "encounter": {
        "name": "name",
        "description": "description",
        "tactics": "tactics",
        "rewards": "rewards",
        "status": "status",
        "monstersData": "monsters_data",
    },
}

INSERTERS: dict[str, Callable[[sqlite3.Connection, str, dict], sqlite3.Row]] = {
    "session": campaign_records.insert_session,
    "npc": campaign_records.insert_npc,
    "quest": campaign_records.insert_quest,
    "encounter": campaign_records.insert_encounter,
}
UPDATERS: dict[str, Callable[[sqlite3.Connection, str, str, dict], sqlite3.Row]] = {
    "session": campaign_records.update_session,
    "npc": campaign_records.update_npc,
    "quest": campaign_records.update_quest,
    "encounter": campaign_records.update_encounter,
}


class PreparationApplyStore:
    """Aplica um rascunho aprovado; escreve destino e registro numa transação só."""

    def __init__(self, path: str, campaigns: CampaignStore, drafts: PreparationDraftStore):
        self.path = str(path)
        self.campaigns = campaigns
        self.drafts = drafts

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path)

    def apply(
        self,
        user_id: str,
        campaign_id: str,
        draft_id: str,
        *,
        idempotency_key: str | None = None,
        acknowledge_player_visibility: bool = False,
    ) -> dict[str, Any]:
        self.campaigns.assert_gm(user_id, campaign_id)
        with closing(self._connect()) as conn, conn:
            draft = self.drafts.draft_row(conn, campaign_id, draft_id)
            replayed = self._replay(conn, campaign_id, draft_id, idempotency_key)
            if replayed is not None:
                return replayed
            if latest_application(conn, draft_id):
                raise CampaignError(
                    409,
                    "este rascunho já foi aplicado. Repetir sem a idempotencyKey "
                    "da aplicação criaria um segundo registro.",
                )
            payload = parse_payload(json.loads(draft["payload"]))
            self._require_visibility_ack(payload, acknowledge_player_visibility)
            record, action = self._write(conn, campaign_id, draft, payload)
            return self._record_application(
                conn,
                draft,
                self._result(conn, draft, payload, record, action, idempotency_key),
                idempotency_key,
            )

    def _replay(
        self,
        conn: sqlite3.Connection,
        campaign_id: str,
        draft_id: str,
        idempotency_key: str | None,
    ) -> dict[str, Any] | None:
        """Resultado já produzido por esta chave, se houver."""
        if not idempotency_key:
            return None
        application = application_by_key(conn, campaign_id, idempotency_key)
        if application is None:
            return None
        if application["draftId"] != draft_id:
            raise CampaignError(
                409,
                f"a idempotencyKey {idempotency_key} já aplicou o rascunho "
                f"{application['draftId']}. Reaplicar aqui exige outra chave.",
            )
        result = stored_result(conn, application["applicationId"])
        if result is None:  # a linha veio do mesmo registro: inatingível na prática
            raise CampaignError(409, "aplicação registrada sem resultado; relançar a aplicação.")
        return result

    def _require_visibility_ack(self, payload: Any, acknowledged: bool) -> None:
        """Recusa publicar para os jogadores sem o mestre confirmar no pedido."""
        exposed = _player_visible_fields(payload)
        if exposed and not acknowledged:
            raise CampaignError(
                422,
                "aplicar entrega aos jogadores: "
                f"{'; '.join(exposed)}. Revisar o texto e reenviar com "
                "acknowledgePlayerVisibility=true, ou manter privado "
                "(summary vazio / visibility=gm).",
            )

    def _write(
        self, conn: sqlite3.Connection, campaign_id: str, draft: sqlite3.Row, payload: Any
    ) -> tuple[sqlite3.Row, str]:
        kind = draft["kind"]
        values = _official_values(payload)
        if draft["target_id"]:
            assert_target_unchanged(
                conn, campaign_id, kind, draft["target_id"], draft["target_observed_at"]
            )
            return UPDATERS[kind](conn, campaign_id, draft["target_id"], values), "updated"
        if kind == "session":
            self._require_free_session_number(conn, campaign_id, values["session_number"])
        return INSERTERS[kind](conn, campaign_id, values), "created"

    def _require_free_session_number(
        self, conn: sqlite3.Connection, campaign_id: str, session_number: int
    ) -> None:
        """Número de sessão é identidade para o grupo: duplicar é perguntar, não supor."""
        taken = conn.execute(
            """SELECT id FROM campaign_sessions
            WHERE campaign_id = ? AND session_number = ? LIMIT 1""",
            (campaign_id, session_number),
        ).fetchone()
        if taken:
            raise CampaignError(
                409,
                f"a sessão {session_number} já existe nesta campanha "
                f"({taken['id']}). Escolher outro sessionNumber ou apontar o "
                "rascunho para a sessão existente com targetId.",
            )

    def _result(
        self,
        conn: sqlite3.Connection,
        draft: sqlite3.Row,
        payload: Any,
        record: sqlite3.Row,
        action: str,
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """O que a aplicação devolve — e o que o replay devolve idêntico depois."""
        dumped = payload.model_dump()
        return {
            "campaignId": draft["campaign_id"],
            "draftId": draft["id"],
            "draftRevision": draft["revision"],
            "kind": draft["kind"],
            "action": action,
            "recordId": record["id"],
            "sourceRefs": self.drafts.source_refs(conn, draft["id"]),
            "idempotencyKey": idempotency_key,
            "warnings": exposure_warnings(draft["kind"], dumped),
        }

    def _record_application(
        self,
        conn: sqlite3.Connection,
        draft: sqlite3.Row,
        result: dict[str, Any],
        idempotency_key: str | None,
    ) -> dict[str, Any]:
        """Terceira escrita da mesma transação: trilha de auditoria + rascunho aprovado."""
        stored = record_application(
            conn,
            campaign_id=draft["campaign_id"],
            draft_id=draft["id"],
            draft_revision=draft["revision"],
            kind=draft["kind"],
            action=result["action"],
            record_id=result["recordId"],
            source_refs=result["sourceRefs"],
            idempotency_key=idempotency_key,
            result=result,
        )
        self.drafts.mark_applied(conn, draft["campaign_id"], draft["id"])
        return stored


def _official_values(payload: Any) -> dict[str, Any]:
    dumped = payload.model_dump()
    return {column: dumped[field] for field, column in COLUMNS_BY_KIND[payload.kind].items()}


def _player_visible_fields(payload: Any) -> list[str]:
    """Campos deste payload que ficam visíveis aos jogadores ao aplicar.

    `campaign_sessions` não tem coluna de visibilidade e `list_sessions` é lida por
    qualquer membro: resumo de sessão é público, qualquer que seja o `status`.
    """
    dumped = payload.model_dump()
    if payload.kind == "session":
        if dumped.get("summary"):
            return [
                "summary e title da sessão (toda sessão aparece para os jogadores)"
            ]
        return []
    if payload.kind in ("npc", "quest") and dumped.get("visibility") == "party":
        return [f"{payload.kind} com visibility=party"]
    return []


@lru_cache(maxsize=8)
def build_apply_store(
    path: str, campaigns: CampaignStore, sources: PreparationStore
) -> PreparationApplyStore:
    """Store de aplicação derivado das mesmas stores da REST e do futuro MCP."""
    return PreparationApplyStore(path, campaigns, build_draft_store(path, campaigns, sources))
