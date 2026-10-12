import random
from functools import lru_cache
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings
from app.managers.connection import manager
from app.models.domain import CombatSession, Combatant
from app.services.campaign_store import CampaignStore

router = APIRouter()
bearer = HTTPBearer(auto_error=False)


@lru_cache(maxsize=1)
def get_campaign_store() -> CampaignStore:
    return CampaignStore(settings.CAMPAIGN_DB_PATH)


def current_user(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
    store: Annotated[CampaignStore, Depends(get_campaign_store)],
) -> dict:
    user = store.user_for_token(credentials.credentials) if credentials else None
    if not user:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Autenticação necessária.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


class RegisterBody(BaseModel):
    email: str = Field(
        min_length=5, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )
    displayName: str = Field(min_length=1, max_length=80)
    password: str = Field(min_length=8, max_length=128)

    @field_validator("email", "displayName", mode="before")
    @classmethod
    def trim_identity(cls, value: str) -> str:
        return value.strip() if isinstance(value, str) else value


class LoginBody(BaseModel):
    email: str
    password: str


class TableLoginBody(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    pin: str = Field(min_length=1, max_length=80)


class CampaignBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=80)


class JoinBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    inviteCode: str = Field(min_length=1, max_length=128)


class CharacterBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    characterId: str = Field(min_length=1, max_length=128)
    characterData: dict | None = None


class CharacterSyncBody(BaseModel):
    characterData: dict


class SessionBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    sessionNumber: int = Field(ge=1, le=1000)
    title: str = Field(min_length=1, max_length=120)
    summary: str = Field(default="", max_length=20000)
    scheduledAt: str | None = Field(default=None, max_length=64)
    status: Literal["planned", "active", "completed"] = "planned"


class QuestBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=20000)
    reward: str = Field(default="", max_length=1000)
    visibility: Literal["party", "gm"] = "party"
    status: Literal["active", "completed", "failed"] = "active"


class JournalEntryBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=10000)
    visibility: Literal["gm", "party"]


class EncounterBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=20000)
    tactics: str = Field(default="", max_length=20000)
    rewards: str = Field(default="", max_length=1000)
    status: Literal["planned", "ready", "completed"] = "planned"
    monstersData: list[dict] = Field(default_factory=list)


class NpcBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1, max_length=120)
    role: str = Field(default="", max_length=120)
    attitude: Literal["friendly", "neutral", "hostile", "unknown"] = "neutral"
    description: str = Field(default="", max_length=20000)
    gmNotes: str = Field(default="", max_length=20000)
    combatData: dict | None = None
    visibility: Literal["party", "gm"] = "party"


class NpcTemplateBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    id: str | None = None
    name: str = Field(min_length=1, max_length=120)
    level: int = Field(default=1, ge=1, le=30)
    templateData: dict = Field(default_factory=dict)
    updatedAt: str | None = None


class NpcTemplateSyncBody(BaseModel):
    templates: list[NpcTemplateBody] = Field(default_factory=list)


Store = Annotated[CampaignStore, Depends(get_campaign_store)]
User = Annotated[dict, Depends(current_user)]


@router.post("/auth/register", status_code=201)
def register(body: RegisterBody, store: Store):
    return {"accessToken": store.register(body.email, body.displayName, body.password)}


@router.post("/auth/login")
def login(body: LoginBody, store: Store):
    return {"accessToken": store.login(body.email, body.password)}


@router.post("/auth/table-login")
def table_login(body: TableLoginBody, store: Store):
    return {"accessToken": store.table_login(body.name, body.pin)}


@router.post("/auth/logout", status_code=204)
def logout(
    user: User,
    store: Store,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
):
    if not credentials:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Autenticação necessária.")
    store.revoke_token(user["id"], credentials.credentials)
    return Response(status_code=204)


@router.post("/campaigns", status_code=201)
def create_campaign(body: CampaignBody, user: User, store: Store):
    return store.create_campaign(user["id"], body.name)


@router.get("/campaigns")
def list_campaigns(user: User, store: Store):
    return store.list_campaigns(user["id"])


@router.post("/campaigns/join")
def join_campaign(body: JoinBody, user: User, store: Store):
    return store.join_campaign(user["id"], body.inviteCode)


@router.get("/campaigns/{campaign_id}")
def get_campaign(campaign_id: str, user: User, store: Store):
    return store.get_campaign(user["id"], campaign_id)


@router.post("/campaigns/{campaign_id}/invites", status_code=201)
def create_invite(campaign_id: str, user: User, store: Store):
    return store.create_invite(user["id"], campaign_id)


@router.put("/campaigns/{campaign_id}/character")
def link_character(campaign_id: str, body: CharacterBody, user: User, store: Store):
    return store.link_character(
        user["id"], campaign_id, body.characterId, body.characterData
    )


@router.put("/campaigns/{campaign_id}/character/sheet")
def sync_character(
    campaign_id: str, body: CharacterSyncBody, user: User, store: Store
):
    return store.sync_character(user["id"], campaign_id, body.characterData)


@router.get("/campaigns/{campaign_id}/members/{member_id}/sheet")
def get_member_sheet(
    campaign_id: str, member_id: str, user: User, store: Store
):
    return store.get_member_sheet(user["id"], campaign_id, member_id)


@router.delete("/campaigns/{campaign_id}/character")
def unlink_character(campaign_id: str, user: User, store: Store):
    return store.unlink_character(user["id"], campaign_id)


@router.get("/campaigns/{campaign_id}/sessions")
def list_sessions(campaign_id: str, user: User, store: Store):
    return store.list_sessions(user["id"], campaign_id)


@router.post("/campaigns/{campaign_id}/sessions", status_code=201)
def create_session(
    campaign_id: str, body: SessionBody, user: User, store: Store
):
    return store.create_session(
        user["id"],
        campaign_id,
        body.sessionNumber,
        body.title,
        body.summary,
        body.scheduledAt,
        body.status,
    )


@router.put("/campaigns/{campaign_id}/sessions/{session_id}")
def update_session(
    campaign_id: str,
    session_id: str,
    body: SessionBody,
    user: User,
    store: Store,
):
    return store.update_session(
        user["id"],
        campaign_id,
        session_id,
        body.sessionNumber,
        body.title,
        body.summary,
        body.scheduledAt,
        body.status,
    )


@router.delete("/campaigns/{campaign_id}/sessions/{session_id}", status_code=204)
def delete_session(
    campaign_id: str, session_id: str, user: User, store: Store
):
    store.delete_session(user["id"], campaign_id, session_id)
    return Response(status_code=204)


@router.get("/campaigns/{campaign_id}/quests")
def list_quests(campaign_id: str, user: User, store: Store):
    return store.list_quests(user["id"], campaign_id)


@router.post("/campaigns/{campaign_id}/quests", status_code=201)
def create_quest(campaign_id: str, body: QuestBody, user: User, store: Store):
    return store.create_quest(
        user["id"],
        campaign_id,
        body.title,
        body.description,
        body.reward,
        body.visibility,
        body.status,
    )


@router.put("/campaigns/{campaign_id}/quests/{quest_id}")
def update_quest(
    campaign_id: str, quest_id: str, body: QuestBody, user: User, store: Store
):
    return store.update_quest(
        user["id"],
        campaign_id,
        quest_id,
        body.title,
        body.description,
        body.reward,
        body.visibility,
        body.status,
    )


@router.delete("/campaigns/{campaign_id}/quests/{quest_id}", status_code=204)
def delete_quest(campaign_id: str, quest_id: str, user: User, store: Store):
    store.delete_quest(user["id"], campaign_id, quest_id)
    return Response(status_code=204)


@router.get("/campaigns/{campaign_id}/entries")
def list_entries(campaign_id: str, user: User, store: Store):
    return store.list_entries(user["id"], campaign_id)


@router.post("/campaigns/{campaign_id}/entries", status_code=201)
def create_entry(campaign_id: str, body: JournalEntryBody, user: User, store: Store):
    return store.create_entry(
        user["id"], campaign_id, body.title, body.body, body.visibility
    )


@router.put("/campaigns/{campaign_id}/entries/{entry_id}")
def update_entry(
    campaign_id: str, entry_id: str, body: JournalEntryBody, user: User, store: Store
):
    return store.update_entry(
        user["id"], campaign_id, entry_id, body.title, body.body, body.visibility
    )


@router.delete("/campaigns/{campaign_id}/entries/{entry_id}", status_code=204)
def delete_entry(campaign_id: str, entry_id: str, user: User, store: Store):
    store.delete_entry(user["id"], campaign_id, entry_id)
    return Response(status_code=204)


# --- ENCOUNTERS ---
@router.get("/campaigns/{campaign_id}/encounters")
def list_encounters(campaign_id: str, user: User, store: Store):
    return store.list_encounters(user["id"], campaign_id)


@router.post("/campaigns/{campaign_id}/encounters", status_code=201)
def create_encounter(campaign_id: str, body: EncounterBody, user: User, store: Store):
    return store.create_encounter(
        user["id"],
        campaign_id,
        body.name,
        body.description,
        body.tactics,
        body.rewards,
        body.status,
        body.monstersData,
    )


@router.put("/campaigns/{campaign_id}/encounters/{encounter_id}")
def update_encounter(
    campaign_id: str, encounter_id: str, body: EncounterBody, user: User, store: Store
):
    return store.update_encounter(
        user["id"],
        campaign_id,
        encounter_id,
        body.name,
        body.description,
        body.tactics,
        body.rewards,
        body.status,
        body.monstersData,
    )


@router.delete("/campaigns/{campaign_id}/encounters/{encounter_id}", status_code=204)
def delete_encounter(campaign_id: str, encounter_id: str, user: User, store: Store):
    store.delete_encounter(user["id"], campaign_id, encounter_id)
    return Response(status_code=204)


@router.post("/campaigns/{campaign_id}/encounters/{encounter_id}/launch")
async def launch_encounter(
    campaign_id: str, encounter_id: str, user: User, store: Store
):
    encounter = store.get_encounter(user["id"], campaign_id, encounter_id)
    session_id = f"campaign-{campaign_id}".lower()
    state = await manager.get_state(session_id)
    if not state:
        state = CombatSession(
            session_id=session_id,
            turn_order=[],
            combatants={},
            logs=[f"Sessão de combate {session_id} iniciada pelo Mestre."],
        )

    added_count = 0
    monsters_data = encounter.get("monstersData", [])
    for m in monsters_data:
        name = m.get("name", "Monstro")
        count = max(1, int(m.get("count", 1)))
        hp_val = int(m.get("hp", 10))
        ac_val = int(m.get("ac", 10))
        init_bonus = int(
            m.get("initiativeBonus")
            or m.get("attributes", {}).get("Destreza", {}).get("modifier", 0)
        )

        for i in range(count):
            combatant_name = f"{name} #{i + 1}" if count > 1 else name
            combatant_id = f"npc:{uuid4()}"
            init_roll = random.randint(1, 20) + init_bonus

            weapons_dict = {}
            raw_weapons = m.get("weapons", {})
            if isinstance(raw_weapons, dict):
                weapons_dict = raw_weapons
            elif isinstance(raw_weapons, list):
                for w_idx, w in enumerate(raw_weapons):
                    w_id = w.get("id", f"w_{w_idx}")
                    weapons_dict[w_id] = {
                        "name": w.get("name", "Ataque"),
                        "damage": w.get("damage", "1d6"),
                        "attribute": w.get("attribute", "Força"),
                        "attackBonus": int(w.get("bonus", w.get("attackBonus", init_bonus))),
                        "range": w.get("range", "1.5m"),
                        "damageBonus": int(w.get("damageBonus", 0)),
                    }
            if not weapons_dict:
                weapons_dict = {
                    "default": {
                        "name": "Ataque Básico",
                        "damage": "1d6",
                        "attribute": "Força",
                        "attackBonus": init_bonus,
                        "range": "1.5m",
                        "damageBonus": 0,
                    }
                }

            combatant = Combatant(
                id=combatant_id,
                name=combatant_name,
                type="npc",
                armorClass=ac_val,
                baseArmorClass=ac_val,
                initiative=init_roll,
                hp={"current": hp_val, "max": hp_val},
                focus={"current": 0, "max": 0},
                turnActions={"standard": True, "bonus": True, "reaction": True},
                weapons=weapons_dict,
                attributes=m.get("attributes", {}),
                skills=m.get("skills", []),
                stances=m.get("stances", []),
            )
            state.combatants[combatant_id] = combatant
            state.turn_order.append(combatant_id)
            state.logs.append(
                f"(Encontro) {combatant_name} entrou na iniciativa ({init_roll})."
            )
            added_count += 1

    state.turn_order.sort(
        key=lambda x: state.combatants[x].initiative, reverse=True
    )

    store.update_encounter(
        user["id"],
        campaign_id,
        encounter_id,
        encounter["name"],
        encounter["description"],
        encounter["tactics"],
        encounter["rewards"],
        "ready",
        monsters_data,
    )

    await manager.broadcast_state(session_id, state)
    return {
        "status": "launched",
        "encounterId": encounter_id,
        "combatantsAdded": added_count,
    }


# --- NPCS ---
@router.get("/campaigns/{campaign_id}/npcs")
def list_npcs(campaign_id: str, user: User, store: Store):
    return store.list_npcs(user["id"], campaign_id)


@router.post("/campaigns/{campaign_id}/npcs", status_code=201)
def create_npc(campaign_id: str, body: NpcBody, user: User, store: Store):
    return store.create_npc(
        user["id"],
        campaign_id,
        body.name,
        body.role,
        body.attitude,
        body.description,
        body.gmNotes,
        body.combatData,
        body.visibility,
    )


@router.put("/campaigns/{campaign_id}/npcs/{npc_id}")
def update_npc(campaign_id: str, npc_id: str, body: NpcBody, user: User, store: Store):
    return store.update_npc(
        user["id"],
        campaign_id,
        npc_id,
        body.name,
        body.role,
        body.attitude,
        body.description,
        body.gmNotes,
        body.combatData,
        body.visibility,
    )


@router.delete("/campaigns/{campaign_id}/npcs/{npc_id}", status_code=204)
def delete_npc(campaign_id: str, npc_id: str, user: User, store: Store):
    store.delete_npc(user["id"], campaign_id, npc_id)
    return Response(status_code=204)


# --- NPC TEMPLATE LIBRARY (BESTIÁRIO DO MESTRE) ---
@router.get("/npc-library")
def list_npc_templates(user: User, store: Store):
    return store.list_npc_templates(user["id"])


@router.post("/npc-library", status_code=201)
def create_npc_template(body: NpcTemplateBody, user: User, store: Store):
    return store.create_npc_template(
        user["id"],
        name=body.name,
        level=body.level,
        template_data=body.templateData,
        template_id=body.id,
        updated_at=body.updatedAt,
    )


@router.put("/npc-library/{template_id}")
def update_npc_template(
    template_id: str, body: NpcTemplateBody, user: User, store: Store
):
    return store.update_npc_template(
        user["id"],
        template_id=template_id,
        name=body.name,
        level=body.level,
        template_data=body.templateData,
        updated_at=body.updatedAt,
    )


@router.delete("/npc-library/{template_id}", status_code=204)
def delete_npc_template(template_id: str, user: User, store: Store):
    store.delete_npc_template(user["id"], template_id)
    return Response(status_code=204)


@router.post("/npc-library/sync")
def sync_npc_templates(body: NpcTemplateSyncBody, user: User, store: Store):
    local_dicts = [t.model_dump() for t in body.templates]
    return store.sync_npc_templates(user["id"], local_dicts)


