import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional, Union
from db_structs import RewardPool, RewardConfig, UserRewardCooldown, RewardReservation, PoolUnitChange
from settings import DB_PATH

# Seconds a connection waits for another connection's lock (for example a running backup) before failing.
BUSY_TIMEOUT_SECONDS = 15


def init_database(db_path: Union[str, Path, None] = None) -> None:
    """Create the database file and any missing tables; existing tables are left untouched."""
    with Database(db_path) as db:
        db.create_tables()


class Database:
    def __init__(self, db_path: Union[str, Path, None] = None):
        self.db_path = Path(db_path) if db_path is not None else DB_PATH
        self.conn = None

    def __enter__(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.db_path, timeout=BUSY_TIMEOUT_SECONDS)
        self.conn.row_factory = self._dict_factory
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.conn:
            if exc_type:
                self.conn.rollback()
            else:
                self.conn.commit()
            self.conn.close()

    @staticmethod
    def _dict_factory(cursor, row):
        d = {}
        for idx, col in enumerate(cursor.description):
            d[col[0]] = row[idx]
        return d

    def create_tables(self):
        cursor = self.conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reward_pools (
                guild_id INTEGER PRIMARY KEY,
                unit TEXT NOT NULL,
                pool_balance INTEGER NOT NULL DEFAULT 0
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS reward_configs (
                config_id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                reward_type TEXT NOT NULL,
                amount INTEGER NOT NULL,
                cooldown_seconds INTEGER DEFAULT 0,
                FOREIGN KEY (guild_id) REFERENCES reward_pools(guild_id) ON DELETE CASCADE,
                UNIQUE (guild_id, reward_type)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_reward_cooldowns (
                user_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                reward_type TEXT NOT NULL,
                last_triggered_timestamp INTEGER NOT NULL,
                PRIMARY KEY (user_id, guild_id, reward_type)
            )
        """)
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS receive_msg (
                user_id INTEGER PRIMARY KEY
            )
        """)
        self.conn.commit()
        cursor.close()

    def get_receive_msg_user(self, user_id: int) -> Optional[dict]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM receive_msg WHERE user_id = ?", (user_id,))
        return cursor.fetchone()

    def add_receive_msg_user(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute("INSERT OR IGNORE INTO receive_msg (user_id) VALUES (?)", (user_id,))

    def remove_receive_msg_user(self, user_id: int):
        cursor = self.conn.cursor()
        cursor.execute("DELETE FROM receive_msg WHERE user_id = ?", (user_id,))

    def get_reward_pool(self, guild_id: int) -> Optional[RewardPool]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM reward_pools WHERE guild_id = ?", (guild_id,))
        return RewardPool.from_dict(cursor.fetchone())

    def update_reward_pool_unit(self, guild_id: int, unit: str):
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE reward_pools SET unit = ?, pool_balance = 0 WHERE guild_id = ?",
            (unit, guild_id)
        )

    def init_reward_pool(self, guild_id: int, unit: str):
        cursor = self.conn.cursor()
        cursor.execute(
            "INSERT INTO reward_pools (guild_id, unit) VALUES (?, ?) ON CONFLICT(guild_id) DO UPDATE SET unit=excluded.unit",
            (guild_id, unit)
        )

    def deposit_to_reward_pool(self, guild_id: int, amount: int):
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE reward_pools SET pool_balance = pool_balance + ? WHERE guild_id = ?",
            (amount, guild_id)
        )

    def withdraw_from_reward_pool(self, guild_id: int, amount: int):
        cursor = self.conn.cursor()
        cursor.execute(
            "UPDATE reward_pools SET pool_balance = pool_balance - ? WHERE guild_id = ?",
            (amount, guild_id)
        )

    def get_reward_configs(self, guild_id: int) -> list[RewardConfig]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM reward_configs WHERE guild_id = ?", (guild_id,))
        return [RewardConfig.from_dict(c) for c in cursor.fetchall()]

    def get_reward_config(self, guild_id: int, reward_type: str) -> Optional[RewardConfig]:
        cursor = self.conn.cursor()
        cursor.execute("SELECT * FROM reward_configs WHERE guild_id = ? AND reward_type = ?", (guild_id, reward_type))
        return RewardConfig.from_dict(cursor.fetchone())

    def set_reward_config(self, guild_id: int, reward_type: str, amount: int, cooldown_seconds: int):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO reward_configs (guild_id, reward_type, amount, cooldown_seconds) VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, reward_type) DO UPDATE SET amount=excluded.amount, cooldown_seconds=excluded.cooldown_seconds
            """,
            (guild_id, reward_type.lower(), amount, cooldown_seconds)
        )

    def delete_reward_config(self, guild_id: int, reward_type: str) -> int:
        cursor = self.conn.cursor()
        cursor.execute(
            "DELETE FROM reward_configs WHERE guild_id = ? AND reward_type = ?",
            (guild_id, reward_type.lower())
        )
        return cursor.rowcount

    def get_user_reward_cooldown(self, user_id: int, guild_id: int, reward_type: str) -> Optional[UserRewardCooldown]:
        cursor = self.conn.cursor()
        cursor.execute(
            "SELECT * FROM user_reward_cooldowns WHERE user_id = ? AND guild_id = ? AND reward_type = ?",
            (user_id, guild_id, reward_type)
        )
        return UserRewardCooldown.from_dict(cursor.fetchone())

    def update_user_reward_cooldown(self, user_id: int, guild_id: int, reward_type: str, timestamp: int):
        cursor = self.conn.cursor()
        cursor.execute(
            """
            INSERT INTO user_reward_cooldowns (user_id, guild_id, reward_type, last_triggered_timestamp) VALUES (?, ?, ?, ?)
            ON CONFLICT(user_id, guild_id, reward_type) DO UPDATE SET last_triggered_timestamp=excluded.last_triggered_timestamp
            """,
            (user_id, guild_id, reward_type, timestamp)
        )

    @contextmanager
    def immediate_transaction(self) -> Iterator[None]:
        """Run the block in a BEGIN IMMEDIATE transaction, which takes the write lock before the first read."""
        if self.conn.in_transaction:
            self.conn.commit()
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self.conn.rollback()
            raise
        else:
            self.conn.commit()

    def reserve_reward(self, guild_id: int, user_id: int, reward_type: str, now: int) -> Optional[RewardReservation]:
        """Atomically check rule, pool balance and cooldown, then deduct the pool and record the cooldown; None when no reward is due."""
        with self.immediate_transaction():
            config = self.get_reward_config(guild_id, reward_type)
            if not config or config.amount <= 0:
                return None
            pool = self.get_reward_pool(guild_id)
            if not pool or pool.pool_balance < config.amount:
                return None
            cooldown = self.get_user_reward_cooldown(user_id, guild_id, reward_type)
            if cooldown and (now - cooldown.last_triggered_timestamp) < config.cooldown_seconds:
                return None
            self.withdraw_from_reward_pool(guild_id, config.amount)
            self.update_user_reward_cooldown(user_id, guild_id, reward_type, now)
            return RewardReservation(
                guild_id=guild_id,
                user_id=user_id,
                reward_type=reward_type,
                unit=pool.unit,
                amount=config.amount,
                timestamp=now,
                previous_timestamp=cooldown.last_triggered_timestamp if cooldown else None,
            )

    def release_reward(self, reservation: RewardReservation) -> tuple[bool, bool]:
        """Undo a reservation whose payment failed; returns whether the pool and the cooldown were restored."""
        r = reservation
        with self.immediate_transaction():
            # The amount goes back only while the pool still holds the reserved unit.
            pool_restored = self.conn.execute(
                "UPDATE reward_pools SET pool_balance = pool_balance + ? WHERE guild_id = ? AND unit = ?",
                (r.amount, r.guild_id, r.unit)
            ).rowcount == 1
            # The cooldown is reverted only if no later reward has overwritten the timestamp written by this reservation.
            if r.previous_timestamp is None:
                cursor = self.conn.execute(
                    "DELETE FROM user_reward_cooldowns WHERE user_id = ? AND guild_id = ? AND reward_type = ? AND last_triggered_timestamp = ?",
                    (r.user_id, r.guild_id, r.reward_type, r.timestamp)
                )
            else:
                cursor = self.conn.execute(
                    "UPDATE user_reward_cooldowns SET last_triggered_timestamp = ? WHERE user_id = ? AND guild_id = ? AND reward_type = ? AND last_triggered_timestamp = ?",
                    (r.previous_timestamp, r.user_id, r.guild_id, r.reward_type, r.timestamp)
                )
            return pool_restored, cursor.rowcount == 1

    def change_reward_pool_unit(self, guild_id: int, unit: str) -> Optional[PoolUnitChange]:
        """Set the pool unit; when a non-empty pool switches unit its balance is zeroed and returned for refunding."""
        with self.immediate_transaction():
            pool = self.get_reward_pool(guild_id)
            if pool and pool.pool_balance > 0 and pool.unit != unit:
                self.update_reward_pool_unit(guild_id, unit)
                return PoolUnitChange(guild_id=guild_id, old_unit=pool.unit, old_balance=pool.pool_balance, new_unit=unit)
            self.init_reward_pool(guild_id, unit)
            return None

    def revert_reward_pool_unit(self, change: PoolUnitChange) -> bool:
        """Restore the old unit and balance after a failed refund, provided the pool is still empty in the new unit."""
        with self.immediate_transaction():
            return self.conn.execute(
                "UPDATE reward_pools SET unit = ?, pool_balance = ? WHERE guild_id = ? AND unit = ? AND pool_balance = 0",
                (change.old_unit, change.old_balance, change.guild_id, change.new_unit)
            ).rowcount == 1

    def deposit_to_reward_pool_if_unit(self, guild_id: int, unit: str, amount: int) -> tuple[bool, Optional[str]]:
        """Deposit only if the pool still uses `unit`; returns whether it was deposited and the pool's current unit."""
        with self.immediate_transaction():
            pool = self.get_reward_pool(guild_id)
            current_unit = pool.unit if pool else None
            if current_unit != unit:
                return False, current_unit
            self.deposit_to_reward_pool(guild_id, amount)
            return True, current_unit
