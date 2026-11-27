"""Stock PostgreSQL BM25 and the pinned A1 MiniLM reranker."""
from collections import OrderedDict, defaultdict
from concurrent.futures import Future
from contextvars import ContextVar
from functools import cached_property, lru_cache
from hashlib import sha256
import json
import logging
import re
from pathlib import Path
from threading import Lock
from time import perf_counter

from backend.assembly.local_steps import local_step
from backend.db import connect
from eval.rerank.rerank import include_condition, load_model, score_pairs
from psycopg.types.json import Jsonb

WORDS = re.compile(r"[\w-]+")
SEARCH_TURN = ContextVar("assembly_search_turn", default=None)
LATENCY_LOG = logging.getLogger("backend.assembly.latency")
RANKING_CACHE_SIZE = 128
DIRECTORATES = {
    "DB": r"дбрцт|департамент[а-я]* бизнес-решений",
    "IB": r"(?<![а-яё])(?:иб|диб)(?![а-яё])|информационн[а-я]* безопасност",
    "TECH": r"тех(?:ническ[а-я]*)?\.?\s+дирекци",
}
BM25 = """
WITH allowed AS (
    SELECT * FROM assembly_passages WHERE fragment_id = ANY(%s::uuid[])
), tokens AS (
    SELECT token, max(weight) AS weight FROM (
        SELECT unnest(tsvector_to_array(to_tsvector('russian', %s))) AS token, 1.0 AS weight
        UNION ALL SELECT unnest(tsvector_to_array(to_tsvector('russian', %s))), 0.35
    ) q GROUP BY token
), stats AS (
    SELECT p.lexeme, ln(1.0 + ((SELECT count(*) FROM allowed) - count(*) + 0.5) /
                                  (count(*) + 0.5)) AS idf
    FROM tokens q JOIN assembly_postings p ON q.token = p.lexeme
    JOIN allowed a USING (fragment_id) GROUP BY p.lexeme
), average AS (SELECT avg(length) AS len FROM allowed)
SELECT a.fragment_id, sum(q.weight * s.idf * p.tf * 2.2 /
    (p.tf + 1.2 * (0.25 + 0.75 * a.length / NULLIF(average.len, 0))) *
    (CASE WHEN p.in_heading THEN 1.35 ELSE 1 END)) AS score
FROM tokens q JOIN stats s ON q.token = s.lexeme
JOIN assembly_postings p ON p.lexeme = s.lexeme JOIN allowed a USING (fragment_id)
CROSS JOIN average
WHERE cardinality(%s::text[]) = 0 OR a.doc = ANY(%s) OR a.doc NOT IN ('DB','IB','PRE','TECH')
GROUP BY a.fragment_id, a.doc, a.ref
ORDER BY score DESC, array_position(ARRAY['DB','IB','PRE','TECH'], a.doc),
         string_to_array(a.ref, '.')::int[], a.fragment_id
LIMIT 30
"""


def encode(text, glossary):
    return WORDS.sub(lambda m: m[0] + " аббр" + m[0] if m[0] in glossary else m[0], text).lower().replace("ё", "е")


def read_glossary(conn):
    return {row["abbreviation"]: row["expansion"] for row in conn.execute("SELECT * FROM assembly_glossary")}


def index_passages(conn, passages):
    glossary = read_glossary(conn)
    rows = [dict(id=str(fragment_id), doc=p["doc"], ref=p["ref"], section=p["section"],
                 text=encode(p["text"], glossary), heading=encode(p["section"], glossary))
            for fragment_id, p in passages]
    conn.execute("""
        INSERT INTO assembly_passages (fragment_id, doc, ref, section, length)
        SELECT id, doc, ref, section, 0
        FROM jsonb_to_recordset(%s) p(id uuid, doc text, ref text, section text)
        ON CONFLICT DO NOTHING
    """, (Jsonb(rows),))
    conn.execute("""
        INSERT INTO assembly_postings (fragment_id, lexeme, tf, in_heading)
        SELECT id, t.lexeme, cardinality(t.positions), vectors.heading @@ plainto_tsquery('simple', t.lexeme)
        FROM jsonb_to_recordset(%s) p(id uuid, text text, heading text)
        CROSS JOIN LATERAL (SELECT to_tsvector('russian', p.text) AS body,
                                   to_tsvector('russian', p.heading) AS heading) vectors
        CROSS JOIN LATERAL unnest(vectors.body || vectors.heading) t
        ON CONFLICT DO NOTHING
    """, (Jsonb(rows),))
    conn.execute("""
        UPDATE assembly_passages a SET length = n.total FROM
        (SELECT fragment_id, sum(tf) AS total FROM assembly_postings
         WHERE fragment_id = ANY(%s::uuid[]) GROUP BY fragment_id) n
        WHERE a.fragment_id = n.fragment_id
    """, ([fragment_id for fragment_id, _ in passages],))


class RevisionIndexer:
    def __init__(self, dsn):
        self.dsn = dsn

    def index(self, revision):
        with connect(self.dsn) as conn:
            index_passages(conn, [
                (f.fragment_id, dict(doc=str(revision.document_id), ref=str(i),
                                     section=f.anchor, text=f.text))
                for i, f in enumerate(revision.fragments, 1)
            ])


class Search:
    def __init__(self, dsn, model_dir):
        self.dsn = dsn
        self.model_dir = Path(model_dir)
        self._rankings = OrderedDict()
        self._ranking_lock = Lock()

    @cached_property
    def model(self):
        return load_model(self.model_dir)

    def rank(self, question, pool):
        if not pool:
            return ()
        # Only the exact model inputs are reusable. The caller still builds the
        # pool from its own current authorized snapshot and verifies citations.
        inputs = [(p["title"], p["section"], p["text"]) for p in pool]
        key = sha256(json.dumps((question, inputs), ensure_ascii=False).encode("utf-8")).digest()
        with self._ranking_lock:
            existing = self._rankings.get(key)
            if existing is None:
                future = self._rankings[key] = Future()
                if len(self._rankings) > RANKING_CACHE_SIZE:
                    self._rankings.popitem(last=False)
            else:
                self._rankings.move_to_end(key)
        if existing is not None:
            with local_step("Повторное использование оценок MiniLM"):
                return existing.result()
        try:
            with local_step("Ранжирование · MiniLM"):
                scores = tuple(score_pairs(*self.model, question, pool))
        except BaseException as error:
            with self._ranking_lock:
                if self._rankings.get(key) is future:
                    del self._rankings[key]
            future.set_exception(error)
            raise
        future.set_result(scores)
        return scores

    def snapshot(self, fragments):
        started = perf_counter()
        by_id = {f.fragment_id: f for f in fragments if f.kind == "document"}
        with connect(self.dsn) as conn:
            metadata = conn.execute("""
                SELECT a.*, o.sha256 FROM assembly_passages a
                JOIN source_fragments f ON f.id = a.fragment_id
                LEFT JOIN assembly_originals o ON o.revision_id = f.revision_id
                WHERE a.fragment_id = ANY(%s::uuid[])
            """, (list(by_id),)).fetchall()
            glossary = read_glossary(conn)
        corpus = {}
        refs = {}
        originals = {}
        for row in metadata:
            fragment = by_id[row["fragment_id"]]
            corpus[row["doc"], row["ref"]] = dict(
                doc=row["doc"], ref=row["ref"], section=row["section"], text=fragment.text,
                title=fragment.title,
            )
            key = (row["doc"], row["ref"])
            refs[key] = fragment
            originals[key] = row["sha256"]
        eligible = {refs[key].fragment_id: key for key, p in corpus.items() if p["text"] != p["section"]}
        LATENCY_LOG.info("turn=%s stage=search_snapshot elapsed_s=%.3f", SEARCH_TURN.get(), perf_counter() - started)

        @lru_cache(maxsize=32)
        def lexical_pool(question):
            docs = [doc for doc, pattern in DIRECTORATES.items() if re.search(pattern, question, re.I)]
            if "IB" in docs:
                docs.append("PRE")
            expanded = " ".join(glossary[w] for w in WORDS.findall(question) if w in glossary)
            started = perf_counter()
            with local_step("Поиск A1 · PostgreSQL"), connect(self.dsn) as conn:
                rows = conn.execute(BM25, (list(eligible), encode(question, glossary),
                                          encode(expanded, glossary), docs, docs)).fetchall()
            LATENCY_LOG.info("turn=%s stage=search bm25_s=%.3f candidates=%s",
                             SEARCH_TURN.get(), perf_counter() - started, len(rows))
            return [corpus[eligible[row["fragment_id"]]] for row in rows]

        def lookup(question):
            pool = lexical_pool(question)
            started = perf_counter()
            scores = self.rank(question, pool)
            LATENCY_LOG.info("turn=%s stage=search rerank_s=%.3f candidates=%s",
                             SEARCH_TURN.get(), perf_counter() - started, len(pool))
            neural = sorted(range(2, len(scores)), key=lambda i: -scores[i])
            ranking = list(range(min(2, len(pool)))) + neural[:3]
            ranking = include_condition(ranking, pool)
            return [(pool[i]["doc"], pool[i]["ref"]) for i in ranking], [
                dict(p, logit=score) for p, score in zip(pool, scores)
            ]

        lookup.lexical_pool = lexical_pool
        return corpus, lookup, refs, originals


def linked_norms(originals, records):
    norms = defaultdict(list)
    for row in records:
        key = (row["doc"], row["ref"])
        if originals.get(key) == row["source_sha256"]:
            norms[key].append(row)
    return norms
