from typing import Any, Dict, Optional

from app.models.domain import Combatant

RELEVANT_SAVE_ATTRIBUTES = [
    "Inteligência",
    "Sabedoria",
    "Carisma",
    "Destreza",
    "Força",
]


def calculate_save_dc(attacker: Combatant, attribute_name: Optional[str] = None) -> int:
    """Calcula a Classe de Dificuldade (CD) para salvaguarda: 8 + Proficiência + Modificador."""
    level = getattr(attacker, "level", 1) or 1
    prof = max(2, (level - 1) // 4 + 2)
    if attribute_name and attribute_name in attacker.attributes:
        return 8 + prof + attacker.attributes[attribute_name].modifier

    mods = [
        attacker.attributes[attr].modifier
        for attr in RELEVANT_SAVE_ATTRIBUTES
        if attr in attacker.attributes
    ]
    best_mod = max(mods) if mods else 0
    return 8 + prof + best_mod


def get_target_save_modifier(target: Combatant, attribute_name: str) -> int:
    """Extrai o modificador do atributo do alvo para salvaguarda."""
    attr = target.attributes.get(attribute_name)
    return attr.modifier if attr else 0


def resolve_saving_throw(
    target: Combatant, attribute_name: str, save_dc: int, roll_result: int
) -> Dict[str, Any]:
    """Avalia o resultado da salvaguarda considerando críticos (20 nat passa, 1 nat falha)."""
    modifier = get_target_save_modifier(target, attribute_name)
    total = roll_result + modifier
    is_crit_success = roll_result == 20
    is_crit_fail = roll_result == 1
    passed = is_crit_success or (not is_crit_fail and total >= save_dc)
    return {
        "roll": roll_result,
        "modifier": modifier,
        "total": total,
        "passed": passed,
        "is_crit_success": is_crit_success,
        "is_crit_fail": is_crit_fail,
    }


def calculate_area_damage(
    base_damage: int, passed: bool, half_on_save: bool = True
) -> int:
    """Calcula dano de área: metade arredondada para baixo (ou zero) se passou na salvaguarda."""
    if passed:
        return base_damage // 2 if half_on_save else 0
    return base_damage
