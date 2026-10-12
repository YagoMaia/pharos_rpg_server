"""API de campanhas: autenticada, separada da sala de combate WebSocket."""

from functools import lru_cache
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import settings
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


class JournalEntryBody(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    title: str = Field(min_length=1, max_length=120)
    body: str = Field(min_length=1, max_length=10000)
    visibility: Literal["gm", "party"]


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
    return store.link_character(user["id"], campaign_id, body.characterId)


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
