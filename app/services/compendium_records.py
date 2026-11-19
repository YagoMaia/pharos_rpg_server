"""Operações SQLite para Compêndio (Bestiário & Herbário) e notas privadas."""

from datetime import UTC, datetime
import json
import sqlite3
from uuid import uuid4

from app.services.errors import CampaignError


def _parse_member_ids(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [str(x) for x in parsed if isinstance(x, str)]
    except json.JSONDecodeError:
        return []


def serialize_compendium_entry(
    row: sqlite3.Row,
    *,
    is_gm: bool,
    player_note: str = "",
) -> dict:
    payload = {
        "id": row["id"],
        "campaignId": row["campaign_id"],
        "slug": row["slug"],
        "kind": row["kind"],
        "name": row["name"],
        "subtitle": row["subtitle"],
        "region": row["region"],
        "subtype": row["subtype"],
        "dangerLabel": row["danger_label"],
        "materialLabel": row["material_label"],
        "lore": row["lore"],
        "image": row["image"],
        "visibility": row["visibility"],
        "playerNote": player_note,
        "updatedAt": row["updated_at"],
    }
    if is_gm:
        payload["visibleToMemberIds"] = _parse_member_ids(
            row["visible_to_member_ids_json"]
        )
        payload["combatTemplateId"] = row["combat_template_id"]
    return payload


def list_compendium_rows(
    conn: sqlite3.Connection,
    campaign_id: str,
    user_id: str,
    *,
    is_gm: bool,
) -> list[dict]:
    rows = conn.execute(
        """SELECT * FROM campaign_compendium_entries
        WHERE campaign_id = ?
        ORDER BY updated_at DESC, name ASC""",
        (campaign_id,),
    ).fetchall()
    notes_rows = conn.execute(
        """SELECT entry_id, note FROM campaign_compendium_notes
        WHERE campaign_id = ? AND user_id = ?""",
        (campaign_id, user_id),
    ).fetchall()
    notes_map = {r["entry_id"]: r["note"] for r in notes_rows}

    result: list[dict] = []
    for row in rows:
        if not is_gm:
            vis = row["visibility"]
            if vis == "gm":
                continue
            if vis == "selected":
                allowed = _parse_member_ids(row["visible_to_member_ids_json"])
                if user_id not in allowed:
                    continue
        result.append(
            serialize_compendium_entry(
                row,
                is_gm=is_gm,
                player_note=notes_map.get(row["id"], ""),
            )
        )
    return result


def upsert_compendium_entry(
    conn: sqlite3.Connection,
    campaign_id: str,
    data: dict,
    *,
    entry_id: str | None = None,
) -> dict:
    now = datetime.now(UTC).isoformat()
    slug = (data.get("slug") or "").strip()
    existing = None
    if entry_id:
        existing = conn.execute(
            "SELECT * FROM campaign_compendium_entries WHERE id = ? AND campaign_id = ?",
            (entry_id, campaign_id),
        ).fetchone()
        if not existing:
            raise CampaignError(404, "Entrada do compêndio não encontrada.")
    elif slug:
        existing = conn.execute(
            "SELECT * FROM campaign_compendium_entries WHERE campaign_id = ? AND slug = ?",
            (campaign_id, slug),
        ).fetchone()

    visibility = data.get("visibility", "gm")
    member_ids = (
        json.dumps(data.get("visible_to_member_ids") or [])
        if visibility == "selected"
        else "[]"
    )

    if existing:
        target_id = existing["id"]
        conn.execute(
            """UPDATE campaign_compendium_entries
            SET slug = ?, kind = ?, name = ?, subtitle = ?, region = ?, subtype = ?,
                danger_label = ?, material_label = ?, lore = ?, image = ?,
                visibility = ?, visible_to_member_ids_json = ?, combat_template_id = ?,
                updated_at = ?
            WHERE id = ? AND campaign_id = ?""",
            (
                slug,
                data["kind"],
                data["name"].strip(),
                data.get("subtitle", "").strip(),
                data.get("region", "").strip(),
                data.get("subtype", "").strip(),
                data.get("danger_label", "").strip(),
                data.get("material_label", "").strip(),
                data.get("lore", "").strip(),
                data.get("image"),
                visibility,
                member_ids,
                data.get("combat_template_id"),
                now,
                target_id,
                campaign_id,
            ),
        )
    else:
        target_id = str(uuid4())
        conn.execute(
            """INSERT INTO campaign_compendium_entries (
                id, campaign_id, slug, kind, name, subtitle, region, subtype,
                danger_label, material_label, lore, image, visibility,
                visible_to_member_ids_json, combat_template_id, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                target_id,
                campaign_id,
                slug,
                data["kind"],
                data["name"].strip(),
                data.get("subtitle", "").strip(),
                data.get("region", "").strip(),
                data.get("subtype", "").strip(),
                data.get("danger_label", "").strip(),
                data.get("material_label", "").strip(),
                data.get("lore", "").strip(),
                data.get("image"),
                visibility,
                member_ids,
                data.get("combat_template_id"),
                now,
            ),
        )
    row = conn.execute(
        "SELECT * FROM campaign_compendium_entries WHERE id = ?", (target_id,)
    ).fetchone()
    return serialize_compendium_entry(row, is_gm=True)


def save_compendium_note(
    conn: sqlite3.Connection,
    campaign_id: str,
    entry_id: str,
    user_id: str,
    note: str,
    *,
    is_gm: bool,
) -> dict:
    row = conn.execute(
        "SELECT * FROM campaign_compendium_entries WHERE id = ? AND campaign_id = ?",
        (entry_id, campaign_id),
    ).fetchone()
    if not row:
        raise CampaignError(404, "Entrada do compêndio não encontrada.")
    if not is_gm:
        vis = row["visibility"]
        allowed = _parse_member_ids(row["visible_to_member_ids_json"])
        if vis == "gm" or (vis == "selected" and user_id not in allowed):
            raise CampaignError(404, "Entrada do compêndio não encontrada.")

    now = datetime.now(UTC).isoformat()
    conn.execute(
        """INSERT INTO campaign_compendium_notes (campaign_id, entry_id, user_id, note, updated_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(campaign_id, entry_id, user_id)
        DO UPDATE SET note = excluded.note, updated_at = excluded.updated_at""",
        (campaign_id, entry_id, user_id, note, now),
    )
    return {"entryId": entry_id, "note": note, "updatedAt": now}
