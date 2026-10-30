"""Rascunhos tipados de preparação, vinculados às revisões exatas das fontes.

Etapa 4 do plano `plans/mcp-preparacao-campanha.md` (ai-memory: workspace
`pharos`, projeto `intregração`). Aceite do plano: organizar o material bruto em
propostas **sem alterar as fontes nem os registros oficiais**. Nada neste módulo
escreve em `campaign_sessions`, `campaign_encounters`, `campaign_npcs` ou
`campaign_quests` — quem escreve é `preparation_apply`, e só depois da decisão
humana.

Garantias deste módulo:

- Cada rascunho referencia `(sourceId, revision)` exatos. A revisão é imutável,
  então a proposta continua apontando para o texto em que foi baseada mesmo depois
  de a fonte evoluir.
- Edição exige `expectedRevision`: divergência devolve 409 e não sobrescreve. A
  escrita ainda é condicionada à revisão lida (`WHERE ... AND revision = ?`), senão
  dois pedidos que leram a mesma versão se sobrescrevem em silêncio.
- `status` aceita apenas `draft`, `approved`, `rejected`. O fato de ter sido
  aplicado vive em `preparation_applications`: o CHECK de `status` não muda sem
  reconstruir a tabela, e um campo só não distinguiria "aprovado" de "aplicado
  duas vezes por retry".
- Mudar payload, alvo ou fontes devolve o rascunho para `draft`: aprovação vale
  para uma versão do conteúdo, não para o identificador do rascunho.
- Entidade equivalente já existente é sugerida como `targetId` em vez de ser
  duplicada em silêncio, e o `updated_at` observado na leitura fica registrado
  (`target_observed_at`) para a etapa 5 recusar sobrescrita cega.

O contrato dos payloads (modelos, limites, avisos de visibilidade) está em
`preparation_draft_payloads.py`; REST e o futuro MCP importam o mesmo validador.
"""

import json
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel

from app.core.database import open_connection, write_transaction
from app.services.campaign_store import CampaignError, CampaignStore
from app.services.preparation_applications import (
    APPLICATIONS_SCHEMA,
    latest_application,
)
from app.services.preparation_draft_payloads import (
    MAX_SOURCE_REFS,
    SourceRef,
    clean_uncertainties,
    exposure_warnings,
    parse_payload,
    title_of,
)
from app.services.preparation_store import (
    PreparationStore,
    decode_cursor,
    encode_cursor,
)
from app.services.preparation_targets import snapshot_target, suggest_targets

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25

DRAFTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS preparation_drafts (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    kind TEXT NOT NULL CHECK (kind IN ('session','encounter','npc','quest')),
    target_id TEXT,
    title TEXT NOT NULL,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'draft'
        CHECK (status IN ('draft','approved','rejected')),
    uncertainties TEXT NOT NULL DEFAULT '[]',
    revision INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS preparation_draft_recent
    ON preparation_drafts(campaign_id, updated_at DESC, id DESC);
CREATE TABLE IF NOT EXISTS preparation_draft_sources (
    draft_id TEXT NOT NULL REFERENCES preparation_drafts(id),
    source_id TEXT NOT NULL,
    source_revision INTEGER NOT NULL,
    PRIMARY KEY (draft_id, source_id, source_revision)
);
"""

# O registro de aplicações é criado junto: mesmo banco, e o store de rascunhos
# precisa lê-lo para responder "este rascunho já foi aplicado?".
SCHEMA = DRAFTS_SCHEMA + APPLICATIONS_SCHEMA

# `target_observed_at` chega depois da primeira versão da tabela e é só metadado
# de detecção de conflito, então `ALTER TABLE ADD COLUMN` basta.
DRAFT_OPTIONAL_COLUMNS = (("target_observed_at", "TEXT"),)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _changes_substance(
    current: sqlite3.Row,
    parsed: BaseModel,
    target_id: str | None,
    stored_refs: list[dict],
    new_refs: list[dict],
) -> bool:
    """Mudou payload, alvo ou fontes? Então a aprovação anterior não vale mais."""
    return (
        json.loads(current["payload"]) != json.loads(parsed.model_dump_json())
        or current["target_id"] != target_id
        or stored_refs != new_refs
    )


class PreparationDraftStore:
    """Persistência e consulta de rascunhos; autorização herdada do `CampaignStore`."""

    def __init__(self, path: str | Path, campaigns: CampaignStore, sources: PreparationStore):
        self.path = str(path)
        self.campaigns = campaigns
        self.sources = sources
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)
            self._ensure_optional_columns(conn)

    def _connect(self) -> sqlite3.Connection:
        return open_connection(self.path)

    def _ensure_optional_columns(self, conn: sqlite3.Connection) -> None:
        """Adiciona colunas criadas depois da primeira versão da tabela."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(preparation_drafts)")}
        for column, declaration in DRAFT_OPTIONAL_COLUMNS:
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE preparation_drafts ADD COLUMN {column} {declaration}"
                )

    # ── escrita ────────────────────────────────────────────────────────────

    def create_draft(
        self,
        user_id: str,
        campaign_id: str,
        *,
        payload: Any,
        target_id: str | None = None,
        source_refs: list[dict] | None = None,
        uncertainties: list[str] | None = None,
    ) -> dict[str, Any]:
        self.campaigns.assert_gm(user_id, campaign_id)
        parsed = parse_payload(payload)
        refs = self._validated_refs(campaign_id, source_refs)
        with write_transaction(self.path) as conn:
            observed = snapshot_target(conn, campaign_id, parsed.kind, target_id)
            draft_id = self._insert(conn, campaign_id, parsed, target_id, uncertainties, observed)
            self._replace_refs(conn, draft_id, refs)
            return self._read(conn, campaign_id, draft_id)

    def _insert(
        self,
        conn: sqlite3.Connection,
        campaign_id: str,
        parsed: BaseModel,
        target_id: str | None,
        uncertainties: list[str] | None,
        observed_at: str | None,
    ) -> str:
        draft_id, now = str(uuid4()), utc_now()
        values = self._payload_values(
            parsed, target_id, clean_uncertainties(uncertainties), observed_at
        )
        columns = ", ".join(values)
        placeholders = ", ".join("?" for _ in values)
        conn.execute(
            f"""INSERT INTO preparation_drafts
            (id, campaign_id, status, revision, created_at, updated_at, {columns})
            VALUES (?, ?, 'draft', 1, ?, ?, {placeholders})""",
            (draft_id, campaign_id, now, now, *values.values()),
        )
        return draft_id

    def _payload_values(
        self,
        parsed: BaseModel,
        target_id: str | None,
        uncertainties: list[str],
        observed_at: str | None,
    ) -> dict[str, Any]:
        """Colunas que criação e edição têm em comum.

        Os nomes vêm deste dicionário, nunca do pedido, então montar o SQL a partir
        deles não abre porta para injeção.
        """
        return {
            "kind": parsed.kind,
            "target_id": target_id,
            "target_observed_at": observed_at,
            "title": title_of(parsed),
            "payload": parsed.model_dump_json(),
            "uncertainties": json.dumps(uncertainties),
        }

    def update_draft(
        self,
        user_id: str,
        campaign_id: str,
        draft_id: str,
        *,
        payload: Any,
        expected_revision: int,
        target_id: str | None = None,
        source_refs: list[dict] | None = None,
        uncertainties: list[str] | None = None,
        status: str | None = None,
    ) -> dict[str, Any]:
        """Substitui o rascunho exigindo a revisão lida: nada de sobrescrever às cegas."""
        self.campaigns.assert_gm(user_id, campaign_id)
        parsed = parse_payload(payload)
        refs = self._validated_refs(campaign_id, source_refs)
        with write_transaction(self.path) as conn:
            current = self._require_current(conn, campaign_id, draft_id, expected_revision)
            if latest_application(conn, draft_id):
                raise CampaignError(
                    409,
                    "Rascunho já aplicado: editar a proposta aplicada criaria um "
                    "segundo registro. Abrir um novo rascunho para corrigir.",
                )
            observed = snapshot_target(conn, campaign_id, parsed.kind, target_id)
            self._overwrite(
                conn,
                campaign_id,
                draft_id,
                parsed,
                target_id,
                uncertainties,
                status,
                current,
                observed,
                # PUT substitui também os vínculos: omitir sourceRefs os apaga.
                replaces_content=_changes_substance(
                    current, parsed, target_id, self.source_refs(conn, draft_id), refs
                ),
            )
            self._replace_refs(conn, draft_id, refs)
            return self._read(conn, campaign_id, draft_id)

    def draft_row(self, conn: sqlite3.Connection, campaign_id: str, draft_id: str) -> sqlite3.Row:
        """Leitura crua para `preparation_apply`, que já autorizou no mesmo pedido."""
        return self._require_current(conn, campaign_id, draft_id, None)

    def _require_current(
        self,
        conn: sqlite3.Connection,
        campaign_id: str,
        draft_id: str,
        expected_revision: int | None,
    ) -> sqlite3.Row:
        current = conn.execute(
            "SELECT * FROM preparation_drafts WHERE id = ? AND campaign_id = ?",
            (draft_id, campaign_id),
        ).fetchone()
        if not current:
            raise CampaignError(404, "Rascunho não encontrado.")
        if expected_revision is not None and current["revision"] != expected_revision:
            raise CampaignError(
                409,
                "O rascunho mudou desde a leitura: revisão esperada "
                f"{expected_revision}, atual {current['revision']}. Reler antes.",
            )
        return current

    def _overwrite(
        self,
        conn: sqlite3.Connection,
        campaign_id: str,
        draft_id: str,
        parsed: BaseModel,
        target_id: str | None,
        uncertainties: list[str] | None,
        status: str | None,
        current: sqlite3.Row,
        observed_at: str | None,
        replaces_content: bool,
    ) -> None:
        merged = (
            clean_uncertainties(uncertainties)
            if uncertainties is not None
            else json.loads(current["uncertainties"])
        )
        values = self._payload_values(parsed, target_id, merged, observed_at)
        assignments = ", ".join(f"{column} = ?" for column in values)
        # A aprovação diz respeito a uma versão do conteúdo: mexer no payload, no
        # alvo ou nas fontes devolve o rascunho para `draft`. Reaprovar é explícito.
        kept = status or ("draft" if replaces_content else current["status"])
        written = conn.execute(
            f"""UPDATE preparation_drafts
            SET {assignments}, status = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND campaign_id = ? AND revision = ?""",
            (
                *values.values(),
                kept,
                utc_now(),
                draft_id,
                campaign_id,
                current["revision"],
            ),
        )
        if written.rowcount == 0:
            raise CampaignError(
                409,
                "o rascunho foi escrito por outro pedido depois desta leitura "
                f"(a revisão {current['revision']} não é mais a atual). Reler antes.",
            )

    # ── leitura ────────────────────────────────────────────────────────────

    def list_drafts(
        self,
        user_id: str,
        campaign_id: str,
        *,
        kind: str | None = None,
        status: str | None = None,
        limit: int = DEFAULT_PAGE_SIZE,
        cursor: str | None = None,
    ) -> dict[str, Any]:
        """Página estável por `(updated_at, id)`; o payload fica fora da listagem."""
        self.campaigns.assert_gm(user_id, campaign_id)
        page_size = max(1, min(int(limit or DEFAULT_PAGE_SIZE), MAX_PAGE_SIZE))
        where, params = self._filters(campaign_id, kind, status, cursor)
        with closing(self._connect()) as conn:
            rows = conn.execute(
                f"""SELECT * FROM preparation_drafts
                WHERE {' AND '.join(where)}
                ORDER BY updated_at DESC, id DESC
                LIMIT ?""",
                (*params, page_size + 1),
            ).fetchall()
            # os itens leem os vínculos na mesma conexão: fazer isso depois do
            # `closing` operaria numa conexão já fechada
            items = [self._summary(conn, row) for row in rows[:page_size]]
        return {
            "items": items,
            "nextCursor": encode_cursor(items[-1]) if len(rows) > page_size and items else None,
        }

    @staticmethod
    def _filters(
        campaign_id: str, kind: str | None, status: str | None, cursor: str | None
    ) -> tuple[list[str], list[Any]]:
        where = ["campaign_id = ?"]
        params: list[Any] = [campaign_id]
        for column, value in (("kind", kind), ("status", status)):
            if value:
                where.append(f"{column} = ?")
                params.append(value.strip())
        after = decode_cursor(cursor)
        if after:
            where.append("(updated_at, id) < (?, ?)")
            params.extend([after[0], after[1]])
        return where, params

    def read_draft(self, user_id: str, campaign_id: str, draft_id: str) -> dict[str, Any]:
        self.campaigns.assert_gm(user_id, campaign_id)
        with closing(self._connect()) as conn:
            return self._read(conn, campaign_id, draft_id)

    # ── validação de vínculos ──────────────────────────────────────────────

    def _validated_refs(self, campaign_id: str, source_refs: list[dict] | None) -> list[dict]:
        if not source_refs:
            return []
        if len(source_refs) > MAX_SOURCE_REFS:
            raise CampaignError(
                422,
                f"sourceRefs com {len(source_refs)} itens excede o limite de "
                f"{MAX_SOURCE_REFS}. Refine a proposta em vez de citar tudo.",
            )
        validated = [self._validated_ref(campaign_id, raw) for raw in source_refs]
        # citar duas vezes a mesma revisão produz o mesmo vínculo: deduplicar aqui
        # evita o IntegrityError na PK (draft_id, source_id, source_revision)
        return list({(ref["sourceId"], ref["revision"]): ref for ref in validated}.values())

    def _validated_ref(self, campaign_id: str, raw: dict) -> dict[str, int | str]:
        ref = self._parse_ref(raw)
        if not self.sources.has_revision(campaign_id, ref.sourceId, ref.revision):
            raise CampaignError(
                422,
                f"fonte inexistente nesta campanha ou revisão {ref.revision} não "
                f"existe para a fonte {ref.sourceId}. Ler as fontes antes.",
            )
        return {"sourceId": ref.sourceId, "revision": ref.revision}

    @staticmethod
    def _parse_ref(raw: dict) -> SourceRef:
        try:
            return SourceRef.model_validate(raw)
        except ValueError as error:  # ValidationError: erro de campo, não do pedido inteiro
            details = "; ".join(
                f"{'.'.join(str(part) for part in issue['loc'])}: {issue['msg']}"
                for issue in error.errors()
            )
            raise CampaignError(422, f"sourceRef inválido -> {details}") from error

    # ── serialização ───────────────────────────────────────────────────────

    def _replace_refs(self, conn: sqlite3.Connection, draft_id: str, refs: list[dict]) -> None:
        conn.execute("DELETE FROM preparation_draft_sources WHERE draft_id = ?", (draft_id,))
        conn.executemany(
            "INSERT INTO preparation_draft_sources VALUES (?, ?, ?)",
            [(draft_id, ref["sourceId"], ref["revision"]) for ref in refs],
        )

    def source_refs(self, conn: sqlite3.Connection, draft_id: str) -> list[dict]:
        """Revisões exatas nas quais o rascunho se apoia (o applier grava junto)."""
        rows = conn.execute(
            """SELECT source_id, source_revision FROM preparation_draft_sources
            WHERE draft_id = ? ORDER BY source_id, source_revision""",
            (draft_id,),
        ).fetchall()
        return [
            {"sourceId": row["source_id"], "revision": row["source_revision"]} for row in rows
        ]

    def mark_applied(self, conn: sqlite3.Connection, campaign_id: str, draft_id: str) -> None:
        """Aprova o rascunho dentro da transação de quem aplicou."""
        conn.execute(
            """UPDATE preparation_drafts
            SET status = 'approved', updated_at = ? WHERE id = ? AND campaign_id = ?""",
            (utc_now(), draft_id, campaign_id),
        )

    def _summary(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        return {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "kind": row["kind"],
            "targetId": row["target_id"],
            "targetObservedAt": row["target_observed_at"],
            "title": row["title"],
            "status": row["status"],
            "revision": row["revision"],
            "uncertainties": json.loads(row["uncertainties"]),
            "sourceRefs": self.source_refs(conn, row["id"]),
            "warnings": exposure_warnings(row["kind"], payload),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _read(self, conn: sqlite3.Connection, campaign_id: str, draft_id: str) -> dict[str, Any]:
        row = self.draft_row(conn, campaign_id, draft_id)
        payload = json.loads(row["payload"])
        summary = self._summary(conn, row)
        summary["payload"] = payload
        summary["application"] = latest_application(conn, draft_id)
        summary["possibleTargets"] = suggest_targets(
            conn, campaign_id, row["kind"], payload, row["target_id"]
        )
        return summary


@lru_cache(maxsize=8)
def build_draft_store(
    path: str, campaigns: CampaignStore, sources: PreparationStore
) -> PreparationDraftStore:
    """Uma instância por par de stores, para não reexecutar o DDL a cada pedido.

    Comum à REST e ao futuro adaptador MCP: a validação vive aqui, então trocar de
    transporte não afrouxa o contrato.
    """
    return PreparationDraftStore(path, campaigns, sources)
