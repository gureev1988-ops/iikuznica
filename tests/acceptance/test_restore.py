"""Restore a consistent PostgreSQL backup into a second, fresh database."""

import hashlib
import os
import shutil
import subprocess
from uuid import uuid4

import psycopg
import pytest
from psycopg import conninfo, sql

from backend.db import connect, migrate


def pg_env(dsn):
    values = conninfo.conninfo_to_dict(dsn)
    return dict(os.environ, **{f"PG{key.upper()}": value for key, value in values.items() if key != "dbname"},
                PGDATABASE=values["dbname"])


def test_restore_fragment_with_its_conversation(dsn, tmp_path):
    if not all(shutil.which(tool) for tool in ("pg_dump", "pg_restore")):
        pytest.skip("Install the PostgreSQL client tools to run backup acceptance")
    database = "ii_restore_" + uuid4().hex
    src, rev, frag, chat, case, turn = (uuid4() for _ in range(6))
    html = "<p>Синтетический документ, срок 7 дней.</p>"
    checksum = hashlib.sha256(html.encode()).hexdigest()
    with connect(dsn) as conn:
        schema = conn.execute("select current_schema() as s").fetchone()["s"]
        conn.execute("insert into conversations (id,owner_id) values (%s,'restore-test')", (chat,))
        conn.execute("insert into cases (id,conversation_id) values (%s,%s)", (case,chat))
        conn.execute("insert into sources (id,source_system,source_key,external_id) values (%s,'document','fixture','page')", (src,))
        conn.execute("insert into source_revisions (id,source_id,version,title,storage_body,checksum,indexed_at) values (%s,%s,1,'Тест',%s,%s,now())",
                     (rev,src,html,checksum))
        conn.execute("insert into source_fragments (id,revision_id,ordinal,anchor,text) values (%s,%s,0,'p1','7 дней')", (frag,rev))
        conn.execute("update sources set current_revision_id=%s,status='active' where id=%s", (rev,src))
        conn.execute("""insert into turns (id,conversation_id,case_id,request_id,question,status,result)
                        values (%s,%s,%s,'c1','Срок?','completed',jsonb_build_object('fragment_id',%s::text))""",
                     (turn,chat,case,frag))
    backup = tmp_path / "backup.dump"
    subprocess.run(["pg_dump", "--format=custom", "--schema", schema, "--file", str(backup)],
                   env=pg_env(dsn), check=True)
    target = conninfo.make_conninfo(dsn, dbname=database)
    with psycopg.connect(dsn, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(database)))
        try:
            subprocess.run(["pg_restore", "--exit-on-error", "--no-owner", "--no-acl", "--dbname", database, str(backup)],
                           env=pg_env(target), check=True)
            assert migrate(target) == ()
            with connect(target) as conn:
                row = conn.execute("""
                    select r.storage_body, r.checksum, f.text, c.owner_id from turns t
                    join conversations c on c.id=t.conversation_id
                    join source_fragments f on f.id=(t.result->>'fragment_id')::uuid
                    join source_revisions r on r.id=f.revision_id
                    join sources s on s.current_revision_id=r.id
                    where t.id=%s""", (turn,)).fetchone()
                assert row == dict(storage_body=html, checksum=checksum, text="7 дней", owner_id="restore-test")
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(database)))
