from typing import Literal, Optional

from pydantic import BaseModel

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
