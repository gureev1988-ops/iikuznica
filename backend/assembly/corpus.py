"""Import the supplied originals as immutable, indexed source revisions."""
from hashlib import sha256
from pathlib import Path
import os
import re
from tempfile import NamedTemporaryFile
from uuid import uuid5

from eval.retrieval.extract import CORPUS, TITLES, clauses

from backend.db import connect, lock_source_catalog
from backend.documents.catalog import activate_revision, stage_revision
from backend.documents.ports import PageSnapshot
from backend.mailfaq.faq import reconcile_document_revision

from .search import index_passages


def retain(directory, name, content):
    digest = sha256(content).hexdigest()
    path = directory / digest / name
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or sha256(path.read_bytes()).hexdigest() != digest:
        with NamedTemporaryFile(dir=path.parent, delete=False) as file:
            file.write(content)
        os.replace(file.name, path)
    return digest, path


def seed(dsn, directory, version=1):
    directory = Path(directory)
    glossary = CORPUS / "glossary.md"
    glossary_digest, _ = retain(directory, glossary.name, glossary.read_bytes())
    expansions = {}
    for line in glossary.read_text().splitlines():
        match = re.fullmatch(r"\| ([^|]+) \| ([^|]+) \|", line)
        if match and match[1] != "Сокращение":
            expansions.update(dict.fromkeys(match[1].split(", "), match[2]))
    with connect(dsn) as conn:
        lock_source_catalog(conn, write=True)
        existing = dict((row["abbreviation"], row["expansion"])
                        for row in conn.execute("SELECT * FROM assembly_glossary"))
        if existing and existing != expansions:
            raise ValueError("The glossary changed. Rebuild search postings before activating it.")
        with conn.cursor() as cursor:
            cursor.executemany("INSERT INTO assembly_glossary VALUES (%s, %s) ON CONFLICT DO NOTHING",
                               expansions.items())
        for key, title in TITLES.items():
            original = CORPUS / (title + ".docx")
            digest, retained = retain(directory, original.name, original.read_bytes())
            # Every paragraph/table row is kept in its original numbered clause.
            passages = list(clauses(key, title, retained))
            page = PageSnapshot(key, version, title, digest)
            revision, indexed = stage_revision(conn, "assembly-corpus", page)
            if not indexed:
                conn.execute("DELETE FROM source_fragments WHERE revision_id = %s", (revision.revision_id,))
                indexed_passages = [(uuid5(revision.revision_id, p["ref"]), p) for p in passages]
                with conn.cursor() as cursor:
                    cursor.executemany("""
                        INSERT INTO source_fragments (id, revision_id, ordinal, anchor, heading_path, text)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, [(fragment, revision.revision_id, i, p["ref"], [p["section"]], p["text"])
                          for i, (fragment, p) in enumerate(indexed_passages)])
                index_passages(conn, indexed_passages)
                conn.execute("INSERT INTO assembly_originals VALUES (%s,%s,%s,%s)",
                             (revision.revision_id, digest, original.name, str(retained.relative_to(directory))))
                conn.execute("UPDATE source_revisions SET indexed_at = now() WHERE id = %s",
                             (revision.revision_id,))
                activate_revision(conn, revision, page, reconcile_document_revision)
            print(key, version, digest, len(passages))
    print("glossary", glossary_digest, len(expansions))
