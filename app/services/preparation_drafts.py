"""Rascunhos tipados de preparação, vinculados às revisões exatas das fontes.

Etapa 4 do plano `plans/mcp-preparacao-campanha.md` (ai-memory: workspace
`pharos`, projeto `intregração`). Aceite do plano: organizar o material bruto em
propostas **sem alterar as fontes nem os registros oficiais**. Aplicar um rascunho
é a etapa 5; nada neste módulo escreve em `campaign_sessions`,
`campaign_encounters`, `campaign_npcs` ou `campaign_quests`.

Garantias deste módulo:

- Cada rascunho referencia `(sourceId, revision)` exatos. A revisão é imutável,
  então a proposta continua apontando para o texto em que foi baseada mesmo depois
  de a fonte evoluir.
- Edição exige `expectedRevision`: divergência devolve 409 e não sobrescreve.
- `status` aceita apenas `draft`, `approved`, `rejected`. `applied` pertence à
  etapa 5, junto da aprovação humana no app.
- Entidade equivalente já existente é sugerida como `targetId` em vez de ser
  duplicada em silêncio.

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

from app.services.campaign_store import CampaignError, CampaignStore
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

MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 25
MAX_TARGET_SUGGESTIONS = 5

TABLE_BY_KIND = {
    "session": "campaign_sessions",
    "encounter": "campaign_encounters",
    "npc": "campaign_npcs",
    "quest": "campaign_quests",
}
MATCH_COLUMN_BY_KIND = {
    "session": "session_number",
    "encounter": "name",
    "npc": "name",
    "quest": "title",
}
MATCH_VALUE_KEY_BY_KIND = {
    "session": "sessionNumber",
    "encounter": "name",
    "npc": "name",
    "quest": "title",
}

SCHEMA = """
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


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class PreparationDraftStore:
    """Persistência e consulta de rascunhos; autorização herdada do `CampaignStore`."""

    def __init__(self, path: str | Path, campaigns: CampaignStore, sources: PreparationStore):
        self.path = str(path)
        self.campaigns = campaigns
        self.sources = sources
        with closing(self._connect()) as conn, conn:
            conn.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

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
        self._validated_target(campaign_id, parsed.kind, target_id)
        with closing(self._connect()) as conn, conn:
            draft_id = self._insert(conn, campaign_id, parsed, target_id, uncertainties)
            self._replace_refs(conn, draft_id, refs)
            return self._read(conn, campaign_id, draft_id)

    def _insert(
        self,
        conn: sqlite3.Connection,
        campaign_id: str,
        parsed: BaseModel,
        target_id: str | None,
        uncertainties: list[str] | None,
    ) -> str:
        draft_id, now = str(uuid4()), utc_now()
        values = self._payload_values(parsed, target_id, clean_uncertainties(uncertainties))
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
        self, parsed: BaseModel, target_id: str | None, uncertainties: list[str]
    ) -> dict[str, Any]:
        """Colunas que criação e edição têm em comum.

        Os nomes vêm deste dicionário, nunca do pedido, então montar o SQL a partir
        deles não abre porta para injeção.
        """
        return {
            "kind": parsed.kind,
            "target_id": target_id,
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
        self._validated_target(campaign_id, parsed.kind, target_id)
        with closing(self._connect()) as conn, conn:
            current = self._require_current(conn, campaign_id, draft_id, expected_revision)
            self._overwrite(
                conn, campaign_id, draft_id, parsed, target_id, uncertainties, status, current
            )
            self._replace_refs(conn, draft_id, refs)
            return self._read(conn, campaign_id, draft_id)

    def _require_current(
        self, conn: sqlite3.Connection, campaign_id: str, draft_id: str, expected_revision: int
    ) -> sqlite3.Row:
        current = conn.execute(
            "SELECT * FROM preparation_drafts WHERE id = ? AND campaign_id = ?",
            (draft_id, campaign_id),
        ).fetchone()
        if not current:
            raise CampaignError(404, "Rascunho não encontrado.")
        if current["revision"] != expected_revision:
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
    ) -> None:
        merged = (
            clean_uncertainties(uncertainties)
            if uncertainties is not None
            else json.loads(current["uncertainties"])
        )
        values = self._payload_values(parsed, target_id, merged)
        assignments = ", ".join(f"{column} = ?" for column in values)
        conn.execute(
            f"""UPDATE preparation_drafts
            SET {assignments}, status = ?, revision = revision + 1, updated_at = ?
            WHERE id = ? AND campaign_id = ?""",
            (*values.values(), status or current["status"], utc_now(), draft_id, campaign_id),
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
        return [self._validated_ref(campaign_id, raw) for raw in source_refs]

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

    def _validated_target(self, campaign_id: str, kind: str, target_id: str | None) -> None:
        """Atualização só aponta para entidade existente, do tipo declarado."""
        if not target_id:
            return
        table = TABLE_BY_KIND[kind]
        with closing(self._connect()) as conn:
            found = conn.execute(
                f"SELECT 1 FROM {table} WHERE id = ? AND campaign_id = ?",
                (target_id, campaign_id),
            ).fetchone()
        if not found:
            raise CampaignError(
                422,
                f"targetId {target_id} não é um(a) {kind} desta campanha. Sem "
                "targetId o rascunho cria; com targetId ele atualiza.",
            )

    # ── serialização ───────────────────────────────────────────────────────

    def _replace_refs(self, conn: sqlite3.Connection, draft_id: str, refs: list[dict]) -> None:
        conn.execute("DELETE FROM preparation_draft_sources WHERE draft_id = ?", (draft_id,))
        conn.executemany(
            "INSERT INTO preparation_draft_sources VALUES (?, ?, ?)",
            [(draft_id, ref["sourceId"], ref["revision"]) for ref in refs],
        )

    def _refs(self, conn: sqlite3.Connection, draft_id: str) -> list[dict]:
        rows = conn.execute(
            """SELECT source_id, source_revision FROM preparation_draft_sources
            WHERE draft_id = ? ORDER BY source_id, source_revision""",
            (draft_id,),
        ).fetchall()
        return [
            {"sourceId": row["source_id"], "revision": row["source_revision"]} for row in rows
        ]

    def _summary(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        payload = json.loads(row["payload"])
        return {
            "id": row["id"],
            "campaignId": row["campaign_id"],
            "kind": row["kind"],
            "targetId": row["target_id"],
            "title": row["title"],
            "status": row["status"],
            "revision": row["revision"],
            "uncertainties": json.loads(row["uncertainties"]),
            "sourceRefs": self._refs(conn, row["id"]),
            "warnings": exposure_warnings(row["kind"], payload),
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }

    def _read(self, conn: sqlite3.Connection, campaign_id: str, draft_id: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT * FROM preparation_drafts WHERE id = ? AND campaign_id = ?",
            (draft_id, campaign_id),
        ).fetchone()
        if not row:
            raise CampaignError(404, "Rascunho não encontrado.")
        payload = json.loads(row["payload"])
        summary = self._summary(conn, row)
        summary["payload"] = payload
        summary["possibleTargets"] = self._suggestions(
            conn, campaign_id, row["kind"], payload, row["target_id"]
        )
        return summary

    def _suggestions(
        self,
        conn: sqlite3.Connection,
        campaign_id: str,
        kind: str,
        payload: dict[str, Any],
        target_id: str | None,
    ) -> list[dict[str, str]]:
        """Entidade equivalente já existente: sugerir `targetId`, nunca duplicar.

        O plano (§7) pede reutilizar NPCs/missões existentes e pedir decisão nos
        casos ambíguos. Só sinalizamos: fundir automaticamente seria o servidor
        decidindo quem é a mesma pessoa.
        """
        if target_id:
            return []
        column = MATCH_COLUMN_BY_KIND[kind]
        value = payload.get(MATCH_VALUE_KEY_BY_KIND[kind])
        if value in (None, ""):
            return []
        comparison = "session_number = ?" if kind == "session" else f"lower({column}) = lower(?)"
        rows = conn.execute(
            f"""SELECT id, {column} AS matched FROM {TABLE_BY_KIND[kind]}
            WHERE campaign_id = ? AND {comparison} LIMIT ?""",
            (campaign_id, value, MAX_TARGET_SUGGESTIONS),
        ).fetchall()
        return [{"kind": kind, "id": row["id"], "label": str(row["matched"])} for row in rows]


@lru_cache(maxsize=8)
def build_draft_store(
    path: str, campaigns: CampaignStore, sources: PreparationStore
) -> PreparationDraftStore:
    """Uma instância por par de stores, para não reexecutar o DDL a cada pedido.

    Comum à REST e ao futuro adaptador MCP: a validação vive aqui, então trocar de
    transporte não afrouxa o contrato.
    """
    return PreparationDraftStore(path, campaigns, sources)
