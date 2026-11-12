"""Temporal rules shared by initial itineraries and replacement tails."""
from dataclasses import dataclass
from datetime import datetime

@dataclass(frozen=True)
class TransferDecision:
    reachable: bool
    replan: bool
    slack_seconds: float


def transfer_decision(arrival: datetime, boarding: datetime, walking_seconds: float,
                      frequency: bool, delayed: bool) -> TransferDecision:
    slack = (boarding - arrival).total_seconds() - walking_seconds
    return TransferDecision(slack >= 0, slack < 0 or (frequency and delayed), slack)


def classify_coverage(eligible: float, covered: float, unverified: bool = False) -> str:
    if unverified:
        return 'incomplete'
    if eligible <= 0:
        return 'not_applicable'
    return 'sufficient' if covered / eligible >= .8 - 1e-12 else 'incomplete'
