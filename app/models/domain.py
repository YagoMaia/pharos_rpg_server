from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel

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


class Combatant(BaseModel):
    model_config = {"extra": "ignore"}

    id: str
    name: str
    baseName: Optional[str] = None
    image: Optional[str] = None

    initiative: int
    hp: StatsPair
    focus: StatsPair
    type: Literal["player", "npc", "pet"]
    ownerId: Optional[str] = None  # ID do player dono (para pets)

    armorClass: int
    speed: Optional[str] = None

    attributes: Dict[str, Attribute] = {}

    equipment: Optional[str] = None
    actions: Optional[str] = None

    weapons: Optional[dict[str, CombatWeaponData]] = None

    stances: List[Stance] = []
    skills: List[Skill] = []
    spells: List[Spell] = []

    activeStanceId: Optional[str] = None

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
