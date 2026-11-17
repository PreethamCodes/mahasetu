"""Transaction state graph loaded from the authoritative contracts/v1/states.json."""
import json

from .settings import CONTRACTS

GRAPH = json.loads((CONTRACTS / "states.json").read_text(encoding="utf-8"))
ACTIVE = frozenset({"CREATED", "AUTHENTICATION_PENDING", "AUTHORIZED", "PROCESSING"})
TERMINAL = frozenset(state for state, exits in GRAPH.items() if not exits)


def can_move(source, target):
    return target in GRAPH.get(source, [])


def is_terminal(state):
    return state in TERMINAL
