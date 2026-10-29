import sqlite3

from conftest import query
from db_utils import Database, init_database

TABLES = ["reward_pools", "reward_configs", "user_reward_cooldowns", "receive_msg"]


def schema(path):
    return query(path, "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY name")


def counts(path):
    return {t: query(path, f"SELECT count(*) FROM {t}")[0][0] for t in TABLES}


def dump(path):
    conn = sqlite3.connect(path)
    try:
        return list(conn.iterdump())
    finally:
        conn.close()


def test_create_tables_on_empty_db(tmp_path):
    path = tmp_path / "nested" / "dir" / "database.db"
    init_database(path)
    names = {row[1] for row in schema(path) if row[0] == "table"}
    assert set(TABLES) <= names
    assert counts(path) == {t: 0 for t in TABLES}
    cols = [r[1] for r in query(path, "PRAGMA table_info(reward_configs)")]
    assert cols == ["config_id", "guild_id", "reward_type", "amount", "cooldown_seconds"]
    before = schema(path)
    init_database(path)
    assert schema(path) == before


def test_production_copy_unchanged_by_create_tables(prod_db_copy):
    before_schema = schema(prod_db_copy)
    before_version = query(prod_db_copy, "PRAGMA schema_version")
    before_dump = dump(prod_db_copy)
    assert counts(prod_db_copy) == {"reward_pools": 8, "reward_configs": 5, "user_reward_cooldowns": 42, "receive_msg": 3}

    init_database(prod_db_copy)
    with Database(prod_db_copy) as db:
        db.create_tables()

    assert schema(prod_db_copy) == before_schema
    assert query(prod_db_copy, "PRAGMA schema_version") == before_version
    assert counts(prod_db_copy) == {"reward_pools": 8, "reward_configs": 5, "user_reward_cooldowns": 42, "receive_msg": 3}
    assert dump(prod_db_copy) == before_dump


def test_reserve_and_release_round_trip_on_production_copy(prod_db_copy):
    rows = query(prod_db_copy, "SELECT c.guild_id, c.reward_type, c.amount FROM reward_configs c JOIN reward_pools p USING (guild_id) WHERE p.pool_balance >= c.amount")
    assert rows, "the production copy has a funded reward rule"
    guild_id, reward_type, amount = rows[0]
    before = dump(prod_db_copy)
    with Database(prod_db_copy) as db:
        reservation = db.reserve_reward(guild_id, 1, reward_type, 2_000_000_000)
    assert reservation is not None and reservation.amount == amount
    assert dump(prod_db_copy) != before
    with Database(prod_db_copy) as db:
        assert db.release_reward(reservation) == (True, True)
    assert dump(prod_db_copy) == before
