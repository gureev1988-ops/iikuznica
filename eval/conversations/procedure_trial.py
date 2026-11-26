"""Compare source coverage and first-turn procedure replies on the public working bank."""
import argparse
from collections import Counter
import gzip
import json
from pathlib import Path
from statistics import median

import check
import conversation
import procedures

ask, evidence = check.ask, check.evidence
COVER = ("Сотрудник просит маршрут. Выбери утверждения в порядке процесса. Проверь по применимому "
         "разделу: кто отвечает за процесс в целом, что требуется до старта, кто делает и кому "
         "передаёт каждый шаг, какие сроки и контрольные действия остаются после него. "
         "Включи применимые запреты и исключения. Не добавляй шаги из другой дирекции и ветви, "
         "которые условия сотрудника исключают.")


def subqueries(question):
    chunks = conversation.fragments(question)
    if len(chunks) == 1:
        return []
    scopes = procedures.directories(question)
    labels = [label for doc, label in (("DB", "ДБРЦТ"), ("TECH", "Техническая дирекция"), ("IB", "Дирекция ИБ"))
              if doc in scopes]
    if labels == ["Дирекция ИБ"] and "пресейл" in question.lower():
        labels = ["Пресейл ИБ"]
    return [labels[0] + ". " + p.lstrip(" ,") for p in chunks] if len(labels) == 1 else chunks


def expand(sources, seeds, question, search, corpus):
    sections = procedures.section_neighbors(sources, seeds, corpus)
    passages = {evidence.reference(p): p for p in sections}
    for query in subqueries(question):
        results, _ = search(query)
        topics = Counter((doc, corpus[doc, ref]["section"]) for doc, ref in results)
        if topics:
            topic = topics.most_common(1)[0][0]
            extra = [p for k, p in corpus.items() if k not in passages and (k[0], p["section"]) == topic
                     and p["text"] != p["section"]][:8]
            passages.update((evidence.reference(p), p) for p in extra)
    return sections, list(passages.values())


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "calls").mkdir(exist_ok=True)
    corpus = ask.full_corpus()
    cache = {}
    for name in ("search.json.gz", "search-procedure-parts.json.gz"):
        packet = json.loads(gzip.decompress((check.HERE / name).read_bytes()))
        assert packet["corpus"] == check.fingerprint(list(corpus.values()))
        cache.update(packet["inputs"])
    def search(q):
        return cache[check.fingerprint(q)]
    report = []
    for case in check.load_bank():
        if not case["id"].startswith("P"):
            continue
        cid, turn = case["id"], case["turns"][0]
        question = turn["question"]
        seeds, pool = search(question)
        stats = {}
        def generator_call(_, body, dataset):
            def fetch():
                row = check.complete(cid, body, dataset)
                if not row["ok"]:
                    raise RuntimeError(f"{cid} incomplete model response, HTTP {row['http']}")
                return {"response": row}
            return check.cached_call(args.out, cid, "generator", body, fetch, lambda r: r["text"])["response"]
        def judge(body):
            def invoke(request):
                return check.cached_call(args.out, cid, "judge", request, lambda: check.decide(request), lambda r: r["answers"])
            return evidence.judge(body, call=invoke)
        def extra(sources, pool, data):
            candidates = evidence.alternatives(sources, pool, data)
            candidates = procedures.augment_sources(candidates, procedures.operation(question), question, [], data)
            original, expanded = expand(candidates, seeds, question, search, data)
            if not args.live:
                gold = {tuple(ref.split()) for part in turn["expect"] for ref, _ in part["steps"]}
                for label, group in (("sections", original), ("split_sections", expanded)):
                    refs = {evidence.reference(p) for p in group}
                    stats[label] = [len(group), gold <= refs]
                raise check.Boundary()
            return expanded
        try:
            result = ask.answer(question, search, generate_answer=lambda q, s, ins: evidence.generate(
                q, s, ins, call=generator_call), judge=judge, add_sources=extra, instruction=COVER, procedure_mode=True)
        except check.Boundary:
            result = None
        report.append({"id":cid, "coverage":stats} if not args.live else {"id":cid,"response":result})
        check.save(args.out / "report.json",json.dumps(report,ensure_ascii=False,indent=2))
    if not args.live:
        for label in ("sections","split_sections"):
            rows = [r["coverage"][label] for r in report]
            print(label, sum(hit for _, hit in rows), "/", len(rows), "median passages", median(n for n,_ in rows))
