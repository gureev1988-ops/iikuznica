"""Bind the four source-only annotations to the exact numbered clauses of the registered DOCX."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "retrieval"))
from extract import CORPUS, TITLES, clauses

CLAIMS = HERE / "claims.jsonl"
SCOPES = {"DB": "ДБРЦТ", "IB": "Дирекция ИБ", "PRE": "Дирекция ИБ", "TECH": "Техническая дирекция"}


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as output:
        temp = Path(output.name)
        try:
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def render(records):
    return "".join(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n" for row in records)


def compile_records():
    pins = json.loads((HERE / "versions.json").read_text(encoding="utf-8"))
    if set(pins) != set(TITLES):
        raise ValueError("Source manifest differs from the corpus")
    entries = []
    for doc, title in TITLES.items():
        version = hashlib.sha256((CORPUS / (title + ".docx")).read_bytes()).hexdigest()
        if version != pins[doc]:
            raise ValueError(f"{doc}: source changed. Review the annotations and their dependent rules before repinning.")
        source = {item["ref"]: item for item in clauses(doc, title)}
        eligible = {ref for ref, item in source.items() if item["text"] != item["section"]}
        seen = Counter()
        statements = set()
        rows = (HERE / "annotations" / (doc + ".tsv")).read_text(encoding="utf-8").splitlines()
        for n, line in enumerate(rows, 1):
            fields = line.split("\t")
            if len(fields) == 2:
                fields.append("")
            if len(fields) != 3:
                raise ValueError(f"{doc}:{n}: expected ref, statement, and optional related references")
            ref, statement, links = [field.strip() for field in fields]
            refs = [] if links == "-" else [v.strip() for v in links.split(",") if v.strip()]
            if ref not in eligible or not statement or (ref, statement) in statements:
                raise ValueError(f"{doc}:{n}: unknown, duplicate or empty claim at {ref}")
            if len(refs) != len(set(refs)) or ref in refs or set(refs) - source.keys():
                raise ValueError(f"{doc}:{n}: invalid links from {ref}: {refs}")
            seen[ref] += 1
            statements.add((ref, statement))
            clause = source[ref]
            entries.append(dict(id=f"{doc}:{ref}:{seen[ref]}", doc=doc, ref=ref,
                                scope=SCOPES[doc], section=clause["section"], title=title,
                                statement=statement, quote=clause["text"], related_refs=refs,
                                source_sha256=version, status="indexed"))
        if eligible != set(seen):
            raise ValueError(f"{doc}: missing source clauses: {sorted(eligible - seen.keys())}")
    return entries


def load_claims():
    return [json.loads(line) for line in CLAIMS.read_text(encoding="utf-8").splitlines()]


def validate():
    expected = compile_records()
    if CLAIMS.read_bytes() != render(expected).encode("utf-8"):
        raise ValueError("The generated registry differs from the annotations or sources; run registry.py --build")
    return expected


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--build", action="store_true")
    command.add_argument("--check", action="store_true")
    args = parser.parse_args()
    if args.build:
        atomic_write(CLAIMS, render(compile_records()))
    records = validate()
    print(f"{len(records)} indexed statements, {len({(r['doc'], r['ref']) for r in records})} source clauses, "
          f"{len(TITLES)} verified originals. Semantic review is a separate step.")


if __name__ == "__main__":
    main()
