import enum
import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Self

if TYPE_CHECKING:
    import discord

    from ballsdex.core.models import BallInstance, Player

@dataclass(slots=True)
class BattleBall:
    instance: "BallInstance"
    health: int
    attack: int

    def copy(self) -> Self:
        return copy.deepcopy(self)

@dataclass(slots=True)
class BattleUser:
    user: "discord.User | discord.Member"
    player: "Player"
    proposal: list["BattleBall"] = field(default_factory=list)
    locked: bool = False
    cancelled: bool = False
    accepted: bool = False
    blacklisted: bool | None = None

    def copy(self) -> "BattleUser":
        return BattleUser(
            self.user,
            self.player,
            [x.copy() for x in self.proposal],
            self.locked,
            self.cancelled,
            self.accepted,
            self.blacklisted
        )

class BattleType(enum.IntEnum):
    STANDARD = 1
    QUICK_MATCH = 2
