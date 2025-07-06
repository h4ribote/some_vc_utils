from dataclasses import dataclass
from typing import Optional

@dataclass
class RewardPool:
    guild_id: int
    unit: str
    pool_balance: int

    @classmethod
    def from_dict(cls, data: dict) -> Optional["RewardPool"]:
        if not data:
            return None
        return cls(
            guild_id=data['guild_id'],
            unit=data['unit'],
            pool_balance=data['pool_balance']
        )

@dataclass
class RewardConfig:
    config_id: int
    guild_id: int
    reward_type: str
    amount: int
    cooldown_seconds: int

    @classmethod
    def from_dict(cls, data: dict) -> Optional["RewardConfig"]:
        if not data:
            return None
        return cls(
            config_id=data['config_id'],
            guild_id=data['guild_id'],
            reward_type=data['reward_type'],
            amount=data['amount'],
            cooldown_seconds=data['cooldown_seconds']
        )

@dataclass
class UserRewardCooldown:
    user_id: int
    guild_id: int
    reward_type: str
    last_triggered_timestamp: int

    @classmethod
    def from_dict(cls, data: dict) -> Optional["UserRewardCooldown"]:
        if not data:
            return None
        return cls(
            user_id=data['user_id'],
            guild_id=data['guild_id'],
            reward_type=data['reward_type'],
            last_triggered_timestamp=data['last_triggered_timestamp']
        )
