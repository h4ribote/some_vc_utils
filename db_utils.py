import sqlite3
from typing import Optional
from db_structs import RewardPool, RewardConfig, UserRewardCooldown

class Database:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.conn = None

    def __enter__(self):
        self.conn = sqlite3.connect(self.db_path)
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
