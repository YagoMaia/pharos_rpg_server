"""Registro de aplicações de rascunho: trilha de auditoria e idempotência.

Etapa 5 do plano `plans/mcp-preparacao-campanha.md`. Uma aplicação responde a três
perguntas que a REST e o futuro MCP precisam poder fazer depois: *este rascunho já
foi aplicado?*, *esta `idempotencyKey` já produziu algo?* e *quem criou este
registro oficial, a partir de qual revisão de fonte?*

Por que registro separado e não `status = 'applied'` no rascunho: o CHECK de
`status` em `preparation_drafts` não pode ser alterado sem reconstruir a tabela, e
um único campo não daria para distinguir "aprovado" de "aplicado duas vezes por
retry".

Todas as funções recebem a conexão de quem chama — `preparation_apply` escreve o
destino oficial e este registro na **mesma transação**, para que uma falha no meio
não deixe rascunho aprovado sem registro, nem registro sem registro oficial.
"""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

MAX_IDEMPOTENCY_KEY_CHARS = 120

APPLICATIONS_SCHEMA = """
CREATE TABLE IF NOT EXISTS preparation_applications (
    id TEXT PRIMARY KEY,
    campaign_id TEXT NOT NULL REFERENCES campaigns(id),
    draft_id TEXT NOT NULL REFERENCES preparation_drafts(id),
    draft_revision INTEGER NOT NULL,
    kind TEXT NOT NULL,
    action TEXT NOT NULL CHECK (action IN ('created','updated')),
    record_id TEXT NOT NULL,
    source_refs TEXT NOT NULL,
    idempotency_key TEXT,
    result TEXT NOT NULL,
    applied_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS preparation_application_key
    ON preparation_applications(campaign_id, idempotency_key)
    WHERE idempotency_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS preparation_application_draft
    ON preparation_applications(draft_id);
"""


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def latest_application(conn: sqlite3.Connection, draft_id: str) -> dict[str, Any] | None:
    """Aplicação mais recente do rascunho, ou `None` se ele nunca foi aplicado."""
    return _as_dict(
        conn.execute(
            """SELECT * FROM preparation_applications WHERE draft_id = ?
            ORDER BY applied_at DESC, id DESC LIMIT 1""",
            (draft_id,),
        ).fetchone()
    )


def application_by_key(
    conn: sqlite3.Connection, campaign_id: str, idempotency_key: str
) -> dict[str, Any] | None:
    """O que esta chave já produziu nesta campanha: base do replay."""
    return _as_dict(
        conn.execute(
            """SELECT * FROM preparation_applications
            WHERE campaign_id = ? AND idempotency_key = ?""",
            (campaign_id, idempotency_key),
        ).fetchone()
    )


def stored_result(conn: sqlite3.Connection, application_id: str) -> dict[str, Any] | None:
    """Resultado serializado de uma aplicação; é exatamente o que o replay devolve."""
    row = conn.execute(
        "SELECT result FROM preparation_applications WHERE id = ?", (application_id,)
    ).fetchone()
    return None if row is None else json.loads(row["result"])


def record_application(
    conn: sqlite3.Connection,
    *,
    campaign_id: str,
    draft_id: str,
    draft_revision: int,
    kind: str,
    action: str,
    record_id: str,
    source_refs: list[dict],
    idempotency_key: str | None,
    result: dict[str, Any],
) -> dict[str, Any]:
    """Grava a aplicação e devolve o resultado que será devolvido ao cliente.

    `applicationId` e `appliedAt` nascem aqui porque o resultado é guardado
    serializado: o retry devolve o mesmo JSON em vez de inventar outra aplicação.
    """
    application_id, now = str(uuid4()), utc_now()
    stored = {**result, "applicationId": application_id, "appliedAt": now}
    conn.execute(
        """INSERT INTO preparation_applications
        (id, campaign_id, draft_id, draft_revision, kind, action, record_id,
         source_refs, idempotency_key, result, applied_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            application_id,
            campaign_id,
            draft_id,
            draft_revision,
            kind,
            action,
            record_id,
            json.dumps(source_refs),
            idempotency_key,
            json.dumps(stored),
            now,
        ),
    )
    return stored


def _as_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "applicationId": row["id"],
        "draftId": row["draft_id"],
        "draftRevision": row["draft_revision"],
        "kind": row["kind"],
        "action": row["action"],
        "recordId": row["record_id"],
        "sourceRefs": json.loads(row["source_refs"]),
        "idempotencyKey": row["idempotency_key"],
        "appliedAt": row["applied_at"],
    }
