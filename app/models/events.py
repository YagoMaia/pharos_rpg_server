from typing import List, Literal, Optional

from pydantic import BaseModel

ActionCostType = Literal["standard", "bonus", "reaction", "free", "none"]


class RemoveCombatantPayload(BaseModel):
    combatantId: str


class ActionTarget(BaseModel):
    """Dano/cura individual por alvo em ataques multi-target."""
    targetId: str
    damageAmount: int = 0
    healingAmount: int = 0


class ResolveActionPayload(BaseModel):
    attackerId: str
    targetId: Optional[str] = None  

    # Multi-target: lista opcional de alvos individuais
    targets: Optional[List[ActionTarget]] = None

    actionName: str 

    # CUSTOS
    costType: ActionCostType = "standard"
    focusCost: int = 0  # 0 para ataques básicos, >0 para skills

    # EFEITOS single-target (Calculados no frontend)
    damageAmount: int = 0
    healingAmount: int = 0
