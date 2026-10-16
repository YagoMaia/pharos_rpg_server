from typing import Any, List, Literal, Optional

from pydantic import BaseModel, model_validator

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
    attackerName: Optional[str] = None
    actionName: str = "Ataque em Área"
    attribute: Optional[str] = None
    saveAttribute: Optional[str] = None
    saveDC: int
    baseDamage: int = 0
    halfOnSave: bool = True
    targetIds: List[str]
    costType: ActionCostType = "none"
    focusCost: int = 0
    requestId: Optional[str] = None
    attackId: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            attr = data.get("attribute") or data.get("saveAttribute") or "Destreza"
            data["attribute"] = attr
            data["saveAttribute"] = attr

            req_id = data.get("requestId") or data.get("attackId")
            if req_id:
                data["requestId"] = req_id
                data["attackId"] = req_id
        return data


class SubmitSavingThrowPayload(BaseModel):
    combatantId: Optional[str] = None
    targetId: Optional[str] = None
    combatantName: Optional[str] = None
    targetName: Optional[str] = None
    rollValue: Optional[int] = None
    roll: Optional[int] = None
    requestId: Optional[str] = None
    attackId: Optional[str] = None
    attribute: Optional[str] = None
    saveAttribute: Optional[str] = None
    saveDC: Optional[int] = None
    baseDamage: Optional[int] = 0
    halfOnSave: Optional[bool] = True
    applyDamage: Optional[bool] = False
    passed: Optional[bool] = None
    damageAmount: Optional[int] = None
    total: Optional[int] = None

    @model_validator(mode="before")
    @classmethod
    def normalize_aliases(cls, data: Any) -> Any:
        if isinstance(data, dict):
            data = dict(data)
            cid = data.get("combatantId") or data.get("targetId")
            if cid:
                data["combatantId"] = cid
                data["targetId"] = cid

            r_val = (
                data.get("rollValue")
                if data.get("rollValue") is not None
                else data.get("roll")
            )
            if r_val is not None:
                data["rollValue"] = r_val
                data["roll"] = r_val

            req_id = data.get("requestId") or data.get("attackId")
            if req_id:
                data["requestId"] = req_id
                data["attackId"] = req_id

            attr = data.get("attribute") or data.get("saveAttribute")
            if attr:
                data["attribute"] = attr
                data["saveAttribute"] = attr

            name = data.get("combatantName") or data.get("targetName")
            if name:
                data["combatantName"] = name
                data["targetName"] = name
        return data
