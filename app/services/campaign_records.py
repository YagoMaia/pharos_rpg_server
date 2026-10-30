"""Escrita dos registros oficiais de campanha sobre uma conexão fornecida.

Existe porque a aplicação de rascunhos (etapa 5 do plano
`plans/mcp-preparacao-campanha.md`) precisa escrever no destino oficial **e**
registrar a aplicação na mesma transação. Os métodos de `campaign_store` abrem
cada um sua própria conexão, então encadear chamadas a eles produziria aplicação
parcial se uma falhasse no meio.

Regras destes helpers:

- Recebem `conn` e não abrem conexão: quem chama controla commit e rollback.
- Não autorizam nada. O chamador já verificou papel e campanha.
- Não iniciam combate nem fazem broadcast: são apenas `INSERT`/`UPDATE`. A rota de
  lançamento de encontro continua separada de propósito.
- Não normalizam texto (`strip`): política de entrada pertence a quem recebe o
  pedido, não à escrita.

Os nomes de tabela e coluna vêm das constantes deste módulo; valores vão sempre
parametrizados, então não há entrada externa em identificador SQL.
"""

import json
import sqlite3
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from app.services.errors import CampaignError

SESSIONS = "campaign_sessions"
NPCS = "campaign_npcs"
QUESTS = "campaign_quests"
ENCOUNTERS = "campaign_encounters"

SESSION_COLUMNS = (
    "campaign_id",
    "session_number",
    "title",
    "summary",
    "scheduled_at",
    "status",
)
NPC_COLUMNS = (
    "campaign_id",
    "name",
    "role",
    "attitude",
    "description",
    "gm_notes",
    "combat_data",
    "visibility",
)
QUEST_COLUMNS = ("campaign_id", "title", "description", "reward", "status", "visibility")
ENCOUNTER_COLUMNS = (
    "campaign_id",
    "name",
    "description",
    "tactics",
    "rewards",
    "status",
    "monsters_data",
)


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def insert_row(
    conn: sqlite3.Connection,
    table: str,
    columns: tuple[str, ...],
    values: dict[str, Any],
    label: str,
) -> sqlite3.Row:
    """Insere uma linha e devolve o registro lido, na conexão do chamador."""
    record_id = str(uuid4())
    now = utc_now()
    missing = set(columns) - set(values)
    if missing:
        raise CampaignError(422, f"{label} sem valores para {sorted(missing)}.")
    payload = {**values, "id": record_id, "created_at": now, "updated_at": now}
    names = ["id", *columns, "created_at", "updated_at"]
    conn.execute(
        f"INSERT INTO {table} ({', '.join(names)}) "
        f"VALUES ({', '.join('?' for _ in names)})",
        tuple(payload[column] for column in names),
    )
    return read_row(conn, table, record_id)


def read_row(conn: sqlite3.Connection, table: str, record_id: str) -> sqlite3.Row:
    row = conn.execute(f"SELECT * FROM {table} WHERE id = ?", (record_id,)).fetchone()
    if not row:
        raise CampaignError(404, f"registro {record_id} não existe em {table}.")
    return row


def update_row(
    conn: sqlite3.Connection,
    table: str,
    record_id: str,
    campaign_id: str,
    values: dict[str, Any],
    missing: str,
) -> sqlite3.Row:
    """Atualiza pela identidade (id + campaign_id): rascunho de outra campanha não sobrescreve."""
    assignments = ", ".join(f"{column} = ?" for column in values)
    updated = conn.execute(
        f"UPDATE {table} SET {assignments}, updated_at = ? WHERE id = ? AND campaign_id = ?",
        (*values.values(), utc_now(), record_id, campaign_id),
    )
    if updated.rowcount == 0:
        raise CampaignError(404, missing)
    return read_row(conn, table, record_id)


def _json_column(values: dict[str, Any], column: str) -> dict[str, Any]:
    """Colunas TEXT que guardam JSON: serializa aqui para não espalhar `json.dumps`."""
    value = values.get(column)
    return {**values, column: json.dumps(value) if value else None}


def list_json_column(values: dict[str, Any], column: str) -> dict[str, Any]:
    """Colunas TEXT que guardam uma lista JSON; nunca `NULL`, o schema exige `[]`."""
    return {**values, column: json.dumps(values.get(column) or [])}


def insert_session(conn: sqlite3.Connection, campaign_id: str, values: dict) -> sqlite3.Row:
    return insert_row(
        conn, SESSIONS, SESSION_COLUMNS, {"campaign_id": campaign_id, **values}, "Sessão"
    )


def update_session(
    conn: sqlite3.Connection, campaign_id: str, session_id: str, values: dict
) -> sqlite3.Row:
    return update_row(conn, SESSIONS, session_id, campaign_id, values, "Sessão não encontrada.")


def insert_npc(conn: sqlite3.Connection, campaign_id: str, values: dict) -> sqlite3.Row:
    values = _json_column({**values, "campaign_id": campaign_id}, "combat_data")
    return insert_row(conn, NPCS, NPC_COLUMNS, values, "NPC")


def update_npc(conn: sqlite3.Connection, campaign_id: str, npc_id: str, values: dict) -> sqlite3.Row:
    return update_row(
        conn, NPCS, npc_id, campaign_id, _json_column(values, "combat_data"), "NPC não encontrado."
    )


def insert_quest(conn: sqlite3.Connection, campaign_id: str, values: dict) -> sqlite3.Row:
    return insert_row(
        conn, QUESTS, QUEST_COLUMNS, {"campaign_id": campaign_id, **values}, "Missão"
    )


def update_quest(conn: sqlite3.Connection, campaign_id: str, quest_id: str, values: dict) -> sqlite3.Row:
    return update_row(conn, QUESTS, quest_id, campaign_id, values, "Missão não encontrada.")


def insert_encounter(conn: sqlite3.Connection, campaign_id: str, values: dict) -> sqlite3.Row:
    values = list_json_column({**values, "campaign_id": campaign_id}, "monsters_data")
    return insert_row(conn, ENCOUNTERS, ENCOUNTER_COLUMNS, values, "Encontro")


def update_encounter(
    conn: sqlite3.Connection, campaign_id: str, encounter_id: str, values: dict
) -> sqlite3.Row:
    values = list_json_column(values, "monsters_data")
    return update_row(
        conn, ENCOUNTERS, encounter_id, campaign_id, values, "Encontro não encontrado."
    )
