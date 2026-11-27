"""Isolate each PostgreSQL test in a separate migrated schema."""

import os
from uuid import uuid4

import psycopg
import pytest
from psycopg import conninfo, sql

from backend.db import connect, migrate


@pytest.fixture
def dsn():
    base_dsn = os.environ.get("TEST_DATABASE_URL") or os.environ.get("DATABASE_URL")
    if not base_dsn:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    schema = f"ii_test_{uuid4().hex}"
    options = conninfo.conninfo_to_dict(base_dsn).get("options", "")
    scoped_dsn = conninfo.make_conninfo(base_dsn, options=f"{options} -c search_path={schema}")
    with psycopg.connect(base_dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        try:
            migrate(scoped_dsn)
            yield scoped_dsn
        finally:
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


@pytest.fixture
def db(dsn):
    with connect(dsn) as conn:
        yield conn
