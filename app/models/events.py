from typing import List, Literal, Optional

from pydantic import BaseModel

ActionCostType = Literal["standard", "bonus", "reaction", "free", "none"]

ConditionName = Literal[
    "Cego",
    "Surdo",
    "Prostrado",
    "Confuso",
    "Amedrontado",
    "Intimidado",
    "Incapacitado",
    "Paralisado",
    "Envenenado",
    "Agarrado",
    "Lento",
    "Atordoado",
]


class RemoveCombatantPayload(BaseModel):
    combatantId: str


class ActionTarget(BaseModel):
    """Dano/cura individual por alvo em ataques multi-target."""

    targetId: str
    damageAmount: int = 0
    healingAmount: int = 0


# --- SISTEMA DE CONDIÇÕES ---


class ConditionPayload(BaseModel):
    """Condição a ser aplicada em um alvo durante uma ação."""

    targetId: str
    conditionName: ConditionName
    durationTurns: Optional[int] = None  # None = permanente
    description: str


class ApplyConditionPayload(BaseModel):
    """GM aplica manualmente uma condição."""

    combatantId: str
    conditionName: ConditionName
    durationTurns: Optional[int] = None
    description: str


class RemoveConditionPayload(BaseModel):
    """GM ou sistema remove uma condição pelo ID."""

    combatantId: str
    conditionId: str


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

    # CONDIÇÕES a aplicar nos alvos
    appliedConditions: Optional[List[ConditionPayload]] = None


# --- SISTEMA DE SALVAGUARDA (SAVING THROWS) ---


class RequestSavingThrowPayload(BaseModel):
    attackerId: str
    actionName: str
    attribute: str
    saveDC: int
    baseDamage: int = 0
    halfOnSave: bool = True
    targetIds: List[str]
    costType: ActionCostType = "standard"
    focusCost: int = 0
    requestId: Optional[str] = None


class SubmitSavingThrowPayload(BaseModel):
    combatantId: str
    rollValue: int
    requestId: Optional[str] = None
    attribute: Optional[str] = None
    saveDC: Optional[int] = None
    baseDamage: Optional[int] = 0
    halfOnSave: Optional[bool] = True
    applyDamage: Optional[bool] = False
    passed: Optional[bool] = None
    damageAmount: Optional[int] = None
