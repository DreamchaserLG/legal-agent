from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, text


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import app.core.sqlite_init as sqlite_init
import app.service.user_service as user_service


class FreshSQLiteAuthInitializationTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="sqlite-auth-init-")
        database_path = Path(self.temp_dir.name) / "auth.sqlite3"
        self.engine = create_engine(
            f"sqlite+pysqlite:///{database_path}",
            future=True,
            connect_args={"check_same_thread": False},
        )
        self.original_init_engine = sqlite_init.engine
        self.original_is_sqlite = sqlite_init.is_sqlite
        self.original_user_engine = user_service.engine
        sqlite_init.engine = self.engine
        sqlite_init.is_sqlite = lambda: True
        user_service.engine = self.engine

    def tearDown(self):
        sqlite_init.engine = self.original_init_engine
        sqlite_init.is_sqlite = self.original_is_sqlite
        user_service.engine = self.original_user_engine
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_fresh_database_registration_creates_profile(self):
        sqlite_init.init_sqlite_tables()
        user = user_service.register_user(
            username="fresh_sqlite_user",
            password="FreshSqlite123",
            email="fresh.sqlite@example.test",
        )

        self.assertEqual(user["username"], "fresh_sqlite_user")
        self.assertEqual(user["real_name"], "")
        with self.engine.connect() as connection:
            profile = connection.execute(
                text("SELECT user_id, real_name FROM user_profiles WHERE user_id = :user_id"),
                {"user_id": int(user["id"])},
            ).mappings().one()
        self.assertEqual(int(profile["user_id"]), int(user["id"]))
        self.assertEqual(profile["real_name"], "")

    def test_sqlite_initialization_is_idempotent(self):
        sqlite_init.init_sqlite_tables()
        sqlite_init.init_sqlite_tables()
        with self.engine.connect() as connection:
            table_count = connection.execute(
                text(
                    "SELECT COUNT(*) FROM sqlite_master "
                    "WHERE type = 'table' AND name IN ('users', 'user_profiles')"
                )
            ).scalar_one()
        self.assertEqual(int(table_count), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
