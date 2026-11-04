"""Vínculos privados do mestre sem alterar os dados narrativos do NPC."""

import sqlite3
from contextlib import closing

from app.core.database import write_transaction
from app.services.campaign_store import CampaignError, CampaignStore


def _assert_access(store: CampaignStore, conn: sqlite3.Connection, user_id: str, campaign_id: str, npc_id: str) -> None:
    if store._member(conn, campaign_id, user_id)["role"] != "gm":
        raise CampaignError(403, "Apenas o mestre pode acessar o vínculo do bestiário.")
    if not conn.execute("SELECT 1 FROM campaign_npcs WHERE id = ? AND campaign_id = ?", (npc_id, campaign_id)).fetchone():
        raise CampaignError(404, "NPC não encontrado.")


def read_npc_bestiary_link(store: CampaignStore, user_id: str, campaign_id: str, npc_id: str) -> str | None:
    """Lê o vínculo sem acessar a biblioteca ou alterar o NPC."""
    with closing(store._connect()) as conn:
        _assert_access(store, conn, user_id, campaign_id, npc_id)
        row = conn.execute("SELECT template_id FROM campaign_npc_bestiary_links WHERE npc_id = ?", (npc_id,)).fetchone()
        return row["template_id"] if row else None


def write_npc_bestiary_link(store: CampaignStore, user_id: str, campaign_id: str, npc_id: str, template_id: str | None) -> None:
    """Vincula uma ficha ou remove o vínculo quando template_id é None."""
    with write_transaction(store.path) as conn:
        _assert_access(store, conn, user_id, campaign_id, npc_id)
        if template_id is None:
            conn.execute("DELETE FROM campaign_npc_bestiary_links WHERE npc_id = ?", (npc_id,))
            return
        conn.execute("INSERT INTO campaign_npc_bestiary_links (npc_id, template_id) VALUES (?, ?) ON CONFLICT(npc_id) DO UPDATE SET template_id = excluded.template_id", (npc_id, template_id))
