"""Find evidence through a claim register. The same interface accepts unseen questions."""
import argparse
from collections import Counter
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import tempfile
from time import perf_counter

HERE = Path(__file__).resolve().parent
RETRIEVAL = HERE.parent / "retrieval"
sys.path.insert(0, str(RETRIEVAL))
from extract import CORPUS, TITLES, clauses
from registry import CLAIMS, validate

LIMIT = 5
TOKEN = re.compile(r"[а-яёa-z0-9]+(?:-[а-яёa-z0-9]+)*", re.I)
STOP = set("""а без бы был была были было быть в вам вас весь во вот все всего вы где да для до его её ее
если есть еще же за здесь и из или им их к как какие каким какими каких какого какой каком какую какая
когда кому кто ли мне может можем можно мы на над надо наш наши не него нет ни но нужно он она они от
по под подскажите пожалуйста после при про с сам свой сколько со так такой там то того только том тот тут
ты у уже хочу чем через что чтобы это этот я""".split())
# Generic intent expands ordinary time questions to clauses containing periods,
# including rules that express a deadline without using the word «срок».
TIME_QUERY = re.compile(r"\b(?:когда|срок\w*|сколько|долго|времени)\b", re.I)
TIME_SOURCE = re.compile(r"\b(?:срок\w*|в течение|не позднее|\d+\s+(?:рабоч\w+\s+)?(?:дн\w*|ден\w*|час\w*|минут\w*|недел\w*|месяц\w*))\b", re.I)
DIRECTORATES = (
    (re.compile(r"\b(?:дбрцт|департамент\w*\s+бизнес-решений)\b", re.I), {"DB"}),
    (re.compile(r"\b(?:иб|диб|информационн\w*\s+безопасност\w*)\b", re.I), {"IB", "PRE"}),
    (re.compile(r"\bтех(?:ническ\w*)?\.?\s+дирекци\w*\b", re.I), {"TECH"}),
)


def jsonl(path):
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def write_jsonl(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as out:
        for row in rows:
            out.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp = Path(out.name)
    tmp.replace(path)


def tokens(text):
    result = []
    for token in TOKEN.findall(text):
        word = token.lower().replace("ё", "е")
        if token.isupper() and len(token) > 1:
            result.append(word)
        elif word not in STOP and len(word) > 1:
            # Prefixes allow morphological variation in this dependency-free ranker.
            result.append(word[:5] if len(word) > 5 and re.fullmatch("[а-я]+", word) else word)
    return result


def glossary():
    out = {}
    path = HERE.parents[1] / "corpus" / "glossary.md"
    for line in path.read_text().splitlines():
        cells = [v.strip() for v in line.strip("|").split("|")]
        if line.startswith("| ") and len(cells) == 2 and cells[0] != "Сокращение":
            for abbreviation in cells[0].split(", "):
                out[abbreviation] = cells[1]
    return out


def ref_of(source):
    return source["doc"] + " " + source["ref"]


def field_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        value = value.values()
    return " ".join(field_text(item) for item in value) if value else ""


def link_ref(doc, ref):
    if not isinstance(ref, str):
        raise ValueError(f"Invalid related anchor: {ref!r}")
    return ref if " " in ref else doc + " " + ref


class RegistrySearch:
    """Warm scoped search with a lazy full-text fallback supplied by the caller."""

    def __init__(self, path=HERE / "claims.jsonl", decisions=None):
        self.claims = validate() if path.resolve() == CLAIMS.resolve() else jsonl(path)
        if not self.claims:
            raise ValueError("Empty registry")
        self.decisions = {}
        if decisions:
            self.decisions = json.loads(decisions.read_text())
            if (self.decisions.get("registry_sha256") != hashlib.sha256(path.read_bytes()).hexdigest()
                    or self.decisions.get("complete") is not True):
                raise ValueError("Judge report is incomplete or belongs to another registry")
        self.blocked = set(self.decisions.get("blocked_refs", []))
        self.sources = {ref_of(p): p for doc, title in TITLES.items() for p in clauses(doc, title)}
        versions = {doc: hashlib.sha256((CORPUS / (title + ".docx")).read_bytes()).hexdigest()
                    for doc, title in TITLES.items()}
        self.words = glossary()
        self.freqs = []
        self.df = Counter()
        self.lengths = []
        self.links = []
        for c in self.claims:
            source = self.sources[ref_of(c)]
            if source["text"] != c["quote"] or c["source_sha256"] != versions[c["doc"]]:
                raise ValueError(f"Source mismatch for {c['id']}")
            links = [link_ref(c["doc"], ref) for ref in c["related_refs"]]
            if any(ref not in self.sources for ref in links):
                raise ValueError(f"Missing related source for {c['id']}")
            self.links.append(links)
            freq = Counter()
            for field, weight in (("statement", 2.2), ("quote", 1), ("section", 0.65), ("scope", 0.35)):
                for word, count in Counter(tokens(field_text(c[field]))).items():
                    freq[word] += weight * min(count, 3)
            if TIME_SOURCE.search(c["quote"]):
                freq["#time"] = 2
            self.freqs.append(freq)
            self.df.update(freq.keys())
            self.lengths.append(sum(freq.values()))
        self.avg_len = sum(self.lengths) / len(self.claims)
        self.idf = {t: math.log1p((len(self.claims) - n + 0.5) / (n + 0.5)) for t, n in self.df.items()}

    def rank(self, question):
        docs = set().union(*(docs for pat, docs in DIRECTORATES if pat.search(question)))
        # Directorate is a filter, so its abbreviation should not inflate match confidence.
        topic = question
        for pat, _ in DIRECTORATES:
            topic = pat.sub(" ", topic)
        primary = set(tokens(topic))
        query = dict.fromkeys(primary, 1.0)
        for abbreviation in TOKEN.findall(topic):
            for word in tokens(self.words.get(abbreviation, "")):
                query.setdefault(word, 0.35)
        if TIME_QUERY.search(question):
            query["#time"] = 1.0
        ranked = []
        for i, (claim, freq) in enumerate(zip(self.claims, self.freqs)):
            if docs and claim["doc"] not in docs:
                continue
            norm = 1.2 * (0.25 + 0.75 * self.lengths[i] / self.avg_len)
            score = sum(weight * self.idf[t] * freq[t] * 2.2 / (freq[t] + norm)
                        for t, weight in query.items() if t in freq)
            if score > 0:
                ranked.append((score, i))
        ranked.sort(key=lambda item: (-item[0], item[1]))
        return ranked, primary

    def search(self, question, fallback=None, baseline_lookup_ms=None, cached_fallback=False):
        start = perf_counter()
        ranked, primary = self.rank(question)
        selected = []
        ids = []
        reason = None
        visited = set()
        overflow = []
        for _, i in ranked:
            claim = self.claims[i]
            anchor = ref_of(claim)
            if anchor in visited:
                continue
            visited.add(anchor)
            bundle = list(dict.fromkeys([anchor] + self.links[i]))
            extra = [ref for ref in bundle if ref not in selected]
            if not extra:
                continue
            if len(selected) + len(extra) > LIMIT:
                overflow.append(anchor)
                continue
            ids.append(claim["id"])
            selected.extend(extra)
            if len(selected) == LIMIT:
                break
        if not selected:
            reason = "no_admissible_match"
        else:
            top = ranked[0][1]
            matched = len(primary.intersection(self.freqs[top].keys()))
            if not primary or matched < max(1, math.ceil(0.35 * len(primary))):
                reason = "low_lexical_coverage"
            if ref_of(self.claims[top]) in overflow:
                reason = "top_bundle_exceeds_limit"
        # Keep original candidate anchors for the audit, even when an owner must review them.
        matched_blocked = sorted(self.blocked.intersection(selected))
        open_pairs = [c for c in self.decisions.get("conflicts", []) if c["status"] == "open"]

        def flags_for(refs):
            return [c for c in open_pairs if set(c["refs"]).intersection(refs)]

        candidate_flags = flags_for(selected)
        if matched_blocked or candidate_flags:
            reason = "judge_flag"
        used_fallback = bool(reason and fallback is not None)
        if used_fallback:
            base_refs = [ref_of(p) for p in fallback()][:LIMIT]
        elif reason in {"judge_flag", "top_bundle_exceeds_limit"}:
            base_refs = []
        else:
            base_refs = selected
        # Check the actual fallback evidence as well. Preserve whole pairs before
        # filling the remaining budget, then guard newly introduced partner anchors.
        flags = flags_for(set(base_refs).union(selected))
        refs = []
        for flag in flags:
            additions = [r for r in flag["refs"] if r not in refs]
            if len(refs) + len(additions) <= LIMIT:
                refs.extend(additions)
        for ref in base_refs:
            if ref not in refs and len(refs) < LIMIT:
                refs.append(ref)
        sources = [self.sources[ref] for ref in refs]
        unresolved = flags_for(refs)
        flagged = {c["id"]: c for c in flags + unresolved}
        omitted = [key for key, c in flagged.items() if not set(c["refs"]).issubset(refs)]
        matched_blocked = sorted(self.blocked.intersection(set(selected).union(refs)))
        registry_ms = (perf_counter() - start) * 1000
        lookup_ms = registry_ms
        if used_fallback and cached_fallback:
            lookup_ms = registry_ms + baseline_lookup_ms if baseline_lookup_ms is not None else None
        trace = dict(route="original_fallback" if used_fallback else "registry",
                     fallback_reason=reason, candidate_refs=selected, retrieved_claim_ids=ids,
                     blocked_refs=matched_blocked, open_conflicts=list(flagged),
                     unresolved_conflicts=unresolved, omitted_conflicts=omitted,
                     forced_escalation=bool(flagged),
                     registry_source_refs=[ref_of(p) for p in sources],
                     registry_search_ms=registry_ms, lookup_ms=lookup_ms,
                     baseline_lookup_ms=baseline_lookup_ms,
                     fallback_lookup_ms=baseline_lookup_ms if used_fallback else 0, online_model_calls=0)
        return sources, trace


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--questions", type=Path, default=RETRIEVAL / "questions.jsonl")
    parser.add_argument("--registry", type=Path, default=HERE / "claims.jsonl")
    parser.add_argument("--baseline", type=Path, default=RETRIEVAL / "contexts.jsonl")
    parser.add_argument("--timings", type=Path, help="Baseline timing JSONL from search.mjs --timings")
    parser.add_argument("--decisions", type=Path, help="Complete judge/decisions.json for the current registry")
    parser.add_argument("--no-fallback", action="store_true")
    parser.add_argument("--output", type=Path, default=HERE / "contexts.jsonl")
    parser.add_argument("--trace", type=Path, default=HERE / "results" / "retrieval_trace.jsonl")
    args = parser.parse_args()
    init = perf_counter()
    engine = RegistrySearch(args.registry, args.decisions)
    init_ms = (perf_counter() - init) * 1000
    questions = jsonl(args.questions)
    if len({q["id"] for q in questions}) != len(questions):
        parser.error("Duplicate question IDs")
    base = {(c["id"], c["question"]): c["sources"] for c in jsonl(args.baseline)}
    timings = {(c["id"], c["question"]): c for c in jsonl(args.timings)} if args.timings else {}
    contexts, traces = [], []
    for q in questions:
        key = (q["id"], q["question"])
        if key not in base:
            parser.error(f"Regenerate the baseline on this question set before comparing: {key[0]}")
        b = base[key]
        for p in b:
            if p != engine.sources.get(ref_of(p)):
                parser.error(f"Baseline and the current corpus disagree for {ref_of(p)}")
        baseline_refs = [ref_of(p) for p in b]
        measured = timings.get(key)
        if args.timings and (not measured or measured["sources"] != baseline_refs):
            parser.error(f"Baseline timing does not match {key[0]}")
        sources, trace = engine.search(q["question"], None if args.no_fallback else lambda: b,
                                       measured["lookup_ms"] if measured else None, cached_fallback=True)
        contexts.append({"id": q["id"], "question": q["question"], "sources": sources})
        traces.append(dict(id=q["id"], question=q["question"], baseline_source_refs=baseline_refs,
                           init_ms=init_ms, judge_applied=bool(args.decisions), **trace))
    write_jsonl(args.output, contexts)
    write_jsonl(args.trace, traces)
    print(f"{len(contexts)} questions, {len(engine.claims)} claims; routes: {dict(Counter(t['route'] for t in traces))}")


if __name__ == "__main__":
    main()
