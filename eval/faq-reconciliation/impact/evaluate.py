"""Rank the entire FAQ collection for a wiki edit. Uses Python 3.9+ stdlib."""
import argparse
from collections import Counter
import json
import math
from pathlib import Path
import re

K = 3


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def grams(text):
    words = re.findall(r'[а-яa-z0-9]+', text.lower().replace('ё', 'е'))
    return Counter(g for word in words for g in
                   ([word] if len(word) < 4 else
                    [word[i:i + 4] for i in range(len(word) - 3)]))


def changed_sentences(before, after):
    split = lambda t: [s for s in re.split(r'(?<=[.!?])\s+', t.strip()) if s]
    old, new = split(before), split(after)
    return [s for s in old if s not in new], [s for s in new if s not in old]


def run(data):
    previous = {v['id']: v['text'] for v in data['previous']}
    index = {}
    links = {}
    for f in data['faqs']:
        evidence = f['prior_evidence']
        assert f['id'] not in index
        for e in evidence:
            assert e['quote'] and e['quote'] in previous[e['clause_id']]
        links[f['id']] = evidence
        index[f['id']] = grams(' '.join([f['question'], f['answer']] +
                               [e['clause_id'] + ' ' + e['quote'] for e in evidence]))
    df = Counter(g for vector in index.values() for g in vector)
    idf = {g: 1 + math.log((1 + len(index)) / (1 + n)) for g, n in df.items()}

    def weights(counter):
        return {g: (1 + math.log(n)) * idf[g] for g, n in counter.items() if g in idf}

    def cosine(q, item):
        den = math.sqrt(sum(x*x for x in q.values()) * sum(y*y for y in item.values()))
        return sum(x*item.get(g, 0) for g, x in q.items()) / den if den else 0.0

    vectors = {fid: weights(vec) for fid, vec in index.items()}
    ranks = {}
    for edit in data['changes']:
        cid = edit['clause_id']
        assert edit['before'] == previous.get(cid, '')
        old, new = changed_sentences(edit['before'], edit['after'])
        query = weights(grams(' '.join([cid] + old + new)))
        raw = {fid: cosine(query, v) for fid, v in vectors.items()}

        def dependency(fid):
            anchors = [e['quote'] for e in links[fid] if e['clause_id'] == cid]
            hit = any(a in s or s in a for a in anchors for s in old)
            return 2 if hit else int(bool(anchors))

        lexical = sorted(index, key=lambda fid: (-raw[fid], fid))
        linked = sorted(index, key=lambda fid: (-dependency(fid), -raw[fid], fid))
        ranks[edit['id']] = {'raw': lexical[:K], 'linked': linked[:K]}
    return ranks


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inputs', type=Path, default=Path(__file__).with_name('inputs.json'))
    parser.add_argument('--gold', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    data = read(args.inputs)
    ranks = run(data)
    result = {'k': K, 'pool': len(data['faqs']), 'ranks': ranks}
    if args.gold:
        rows = read(args.gold)['changes']
        assert {r['id'] for r in rows} == set(ranks) and len(rows) == len(ranks)
        valid_ids = {f['id'] for f in data['faqs']}
        scores = {}
        for arm in ['raw', 'linked']:
            hits = extras = misses = 0
            per = {}
            for item in rows:
                expected = set(item['affected'])
                assert expected <= valid_ids and len(item['affected']) == len(expected)
                selected = set(ranks[item['id']][arm])
                found, extra, lost = selected & expected, selected - expected, expected - selected
                hits += len(found)
                extras += len(extra)
                misses += len(lost)
                per[item['id']] = {'found': sorted(found), 'extra': sorted(extra), 'missed': sorted(lost)}
            scores[arm] = {'hits': hits, 'irrelevant_candidates': extras, 'misses': misses,
                           'recall_at_k': hits / (hits + misses) if hits + misses else None,
                           'precision_at_k': hits / (hits + extras) if hits + extras else None,
                           'per_change': per}
        result['scores'] = scores
    args.out.write_text(json.dumps(result, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
