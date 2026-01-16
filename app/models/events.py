from typing import Literal, Optional, Union

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


ActionCostType = Literal["standard", "bonus", "reaction", "free", "none"]


class RemoveCombatantPayload(BaseModel):
    combatantId: str


class ResolveActionPayload(BaseModel):
    attackerId: str
    targetId: Optional[str] = None  # Pode ser null (ex: buff em si mesmo)

    actionName: str  # Ex: "Espada Longa", "Bola de Fogo", "Cura"

    # CUSTOS
    costType: ActionCostType = "standard"
    focusCost: int = 0  # 0 para ataques básicos, >0 para skills

    # EFEITOS (Calculados no frontend)
    damageAmount: int = 0
    healingAmount: int = 0
