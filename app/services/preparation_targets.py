"""Destino oficial de um rascunho: sugerir equivalente, validar o vínculo, detectar conflito.

Etapa 4 e 5 do plano `plans/mcp-preparacao-campanha.md`. Um rascunho pode criar ou
atualizar; quando atualiza, duas coisas precisam ser garantidas:

- o `targetId` é realmente do tipo declarado e **desta** campanha (senão um rascunho
  de uma campanha edita registros de outra);
- o destino não mudou desde que a proposta foi escrita. O carimbo
  `target_observed_at` é tirado na criação/edição do rascunho e comparado no
  momento de aplicar: sem ele, "atualizar" viraria sobrescrita cega do que o mestre
  editou enquanto o agent organizava as notas.

Nada aqui escreve: só `SELECT` e `CampaignError`. Os nomes de tabela e coluna vêm
dos mapas internos, nunca do pedido.
"""

import sqlite3
from typing import Any

from app.services import campaign_records
from app.services.campaign_store import CampaignError

MAX_TARGET_SUGGESTIONS = 5

TABLE_BY_KIND = {
    "session": campaign_records.SESSIONS,
    "encounter": campaign_records.ENCOUNTERS,
    "npc": campaign_records.NPCS,
    "quest": campaign_records.QUESTS,
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


def snapshot_target(
    conn: sqlite3.Connection, campaign_id: str, kind: str, target_id: str | None
) -> str | None:
    """Confirma o destino e devolve o `updated_at` observado nessa leitura."""
    if not target_id:
        return None
    row = _target_row(conn, campaign_id, kind, target_id)
    if row is None:
        raise CampaignError(
            422,
            f"targetId {target_id} não é um(a) {kind} desta campanha. Sem "
            "targetId o rascunho cria; com targetId ele atualiza.",
        )
    return row["updated_at"]


def assert_target_unchanged(
    conn: sqlite3.Connection,
    campaign_id: str,
    kind: str,
    target_id: str,
    observed_at: str | None,
) -> sqlite3.Row:
    """Recusa aplicar sobre destino que deixou de existir ou foi editado no meio do caminho.

    `observed_at` ausente (rascunho criado antes da coluna) também é recusado: sem
    carimbo não há como saber o que a proposta viu.
    """
    row = _target_row(conn, campaign_id, kind, target_id)
    if row is None:
        raise CampaignError(
            409,
            f"o destino do rascunho ({kind} {target_id}) não existe mais nesta "
            "campanha. Reindicar targetId ou deixar o rascunho criar.",
        )
    if observed_at is None or row["updated_at"] != observed_at:
        raise CampaignError(
            409,
            f"o {kind} destino mudou desde a leitura do rascunho "
            f"(observado {observed_at}, atual {row['updated_at']}). Reler o "
            "registro e repropor antes de aplicar.",
        )
    return row


def suggest_targets(
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


def _target_row(
    conn: sqlite3.Connection, campaign_id: str, kind: str, target_id: str
) -> sqlite3.Row | None:
    return conn.execute(
        f"SELECT * FROM {TABLE_BY_KIND[kind]} WHERE id = ? AND campaign_id = ?",
        (target_id, campaign_id),
    ).fetchone()
