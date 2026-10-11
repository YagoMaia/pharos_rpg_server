from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field

AttributeName = Literal[
    "Constituição", "Força", "Carisma", "Sabedoria", "Inteligência", "Destreza"
]

CharacterClass = Literal[
    "Guerreiro", "Corsário", "Vanguarda", "Mago", "Apóstata", "Atirador", "Orador"
]

ActionType = Literal["Padrão", "Movimento", "Reação", "Completa", "Livre", "Ação Bônus"]


class Attribute(BaseModel):
    name: AttributeName
    value: int
    modifier: int


class StatsPair(BaseModel):
    current: int
    max: int


class TurnActions(BaseModel):
    standard: bool = True
    bonus: bool = True
    reaction: bool = True


class Skill(BaseModel):
    id: str
    name: str
    cost: int
    actionType: ActionType
    description: str
    level: int

    usesWeaponDamage: Optional[bool] = False
    weaponType: Optional[str] = None
    bonusDamage: Optional[str] = None
    damageType: Optional[str] = None

    isHealing: Optional[bool] = False
    healFormula: Optional[str] = None

    # Se quiser tipar o dicionário do saveRequest:
    saveRequest: Optional[Dict[str, Any]] = None


class Stance(BaseModel):
    id: str
    name: str
    benefit: str
    restriction: str
    maneuver: str
    recovery: Optional[str] = None
    acBonus: Optional[int] = 0
    acAttribute: Optional[AttributeName] = None


# --- SISTEMA DE CONDIÇÕES ---
ConditionName = Literal[
    "Cego", "Surdo", "Prostrado", "Confuso", "Amedrontado",
    "Intimidado", "Incapacitado", "Paralisado", "Envenenado",
    "Agarrado", "Lento", "Atordoado"
]


class ActiveCondition(BaseModel):
    id: str  # ID único da aplicação da condição
    name: ConditionName
    appliedAtTurn: int  # Turno em que foi aplicada
    durationTurns: Optional[int] = None  # None = permanente até remoção manual
    sourceId: str  # Quem aplicou
    description: str  # Efeito mecânico


class Spell(BaseModel):
    id: str
    name: str
    school: str = "Universal"
    circle: int = 1
    description: str = ""
    effect: str = ""
    cost: int = 0

    isAttack: bool = False
    damageFormula: Optional[str] = None  # Ex: "1d10"
    actionType: str = "standard"

    isHealing: bool = False
    healFormula: Optional[str] = None


class DeathSaves(BaseModel):
    successes: int = 0
    failures: int = 0


class CombatWeaponData(BaseModel):
    name: str
    damage: str
    attribute: AttributeName
    attackBonus: int
    range: str
    damageBonus: int = 0
    damageIncludesAttribute: Optional[bool] = None


class NpcPhase(BaseModel):
    name: Optional[str] = None
    image: Optional[str] = None
    maxHp: Optional[int] = Field(default=None, gt=0)
    armorClass: Optional[int] = Field(default=None, ge=0)
    stances: Optional[List[Stance]] = None
    skills: Optional[List[Skill]] = None
    weapons: Optional[Dict[str, CombatWeaponData]] = None


class CombatFeat(BaseModel):
    id: str
    name: str
    category: str
    benefit: str
    objective: Optional[str] = None
    prerequisite: Optional[str] = None
    slotLevel: Optional[int] = None
    attributeChoices: Optional[List[AttributeName]] = None
    manualBenefits: Optional[bool] = None


class Combatant(BaseModel):
    model_config = {"extra": "ignore"}

    id: str
    name: str
    baseName: Optional[str] = None
    image: Optional[str] = None
    feats: List[CombatFeat] = Field(default_factory=list)
    factionFeat: Optional[CombatFeat] = None
    specialFeat: Optional[CombatFeat] = None

    initiative: int
    hp: StatsPair
    focus: StatsPair
    type: Literal["player", "npc", "pet"]
    ownerId: Optional[str] = None  # ID do player dono (para pets)

    armorClass: int
    baseArmorClass: Optional[int] = None
    speed: Optional[str] = None

    attributes: Dict[str, Attribute] = {}

    equipment: Optional[str] = None
    actions: Optional[str] = None

    weapons: Optional[dict[str, CombatWeaponData]] = None

    stances: List[Stance] = []
    skills: List[Skill] = []
    spells: List[Spell] = []

    activeStanceId: Optional[str] = None
    phases: List[NpcPhase] = Field(default_factory=list)
    currentPhaseIndex: int = Field(default=0, ge=0)

    # Condições ativas (ex: Cego, Prostrado, Confuso)
    conditions: List[ActiveCondition] = []

    turnActions: TurnActions
    deathSaves: DeathSaves = DeathSaves()


class CombatSession(BaseModel):
    session_id: str
    turn_index: int = 0
    # A ordem dos IDs define a iniciativa
    turn_order: List[str]
    # Mapa de ID -> Combatant
    combatants: Dict[str, Combatant]
    logs: List[str] = []
    last_event: Optional[dict] = None
    gmClientId: Optional[str] = None
    combatantOwners: Dict[str, str] = Field(default_factory=dict)
