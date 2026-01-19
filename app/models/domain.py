from typing import Dict, List, Literal, Optional

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


class Combatant(BaseModel):
    id: str
    name: str
    baseName: Optional[str] = None
    initiative: int
    hp: StatsPair
    type: Literal["player", "npc"]

    armorClass: int
    maxFocus: int
    currentFocus: int

    attributes: Dict[str, Attribute] = {}

    equipment: Optional[str] = None  # String simplificada para combate
    actions: Optional[str] = None

    stances: List[Stance] = []
    skills: List[Skill] = []
    spells: List[Spell] = []

    activeStanceId: Optional[str] = None

    turnActions: TurnActions


class CombatSession(BaseModel):
    session_id: str
    turn_index: int = 0
    # A ordem dos IDs define a iniciativa
    turn_order: List[str]
    # Mapa de ID -> Combatant
    combatants: Dict[str, Combatant]
    logs: List[str] = []
