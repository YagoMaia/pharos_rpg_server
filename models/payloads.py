from typing import Literal, Union

from pydantic import BaseModel


class JoinSessionPayload(BaseModel):
    sessionId: str
    character: dict  # Recebe o objeto Character completo do TS


class UpdateHPPayload(BaseModel):
    combatantId: str
    newHp: int


class ChangeStancePayload(BaseModel):
    combatantId: str
    stanceId: str
    newAC: int  # O front calcula o novo AC e o back confia (ou revalida)


class WebSocketMessage(BaseModel):
    type: Literal[
        "JOIN_SESSION", "UPDATE_HP", "CHANGE_STANCE", "NEXT_TURN", "USE_ACTION"
    ]
    payload: Union[JoinSessionPayload, UpdateHPPayload, ChangeStancePayload, dict]
