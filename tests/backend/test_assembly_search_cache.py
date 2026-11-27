from concurrent.futures import Future, ThreadPoolExecutor
from threading import Event

import pytest

from backend.assembly import search


def test_reusing_exact_ranking_preserves_the_pool_and_retries_failures(monkeypatch, tmp_path):
    worker = search.Search(None, tmp_path)
    monkeypatch.setattr(search, "load_model", lambda _: (None, None))
    calls = []

    def rank(_session, _tokenizer, question, passages):
        calls.append(question)
        if len(calls) == 1:
            raise ValueError("inference failed")
        return [len(passage["text"]) for passage in passages]

    monkeypatch.setattr(search, "score_pairs", rank)
    pool = [dict(title="Regulation", section="1", text="Rule"), dict(title="Regulation", section="2", text="Exception")]
    with pytest.raises(ValueError, match="inference failed"):
        worker.rank("Question", pool)
    assert worker.rank("Question", pool) == (4, 9)
    assert worker.rank("Question", [dict(passage) for passage in pool]) == (4, 9)
    assert worker.rank("Question", list(reversed(pool))) == (9, 4)
    assert worker.rank("Question", [dict(pool[0], text="New rule"), pool[1]]) == (8, 9)
    assert worker.rank("Different question", pool) == (4, 9)
    assert calls == ["Question"] * 4 + ["Different question"]


def test_identical_concurrent_requests_share_inference(monkeypatch, tmp_path):
    worker = search.Search(None, tmp_path)
    monkeypatch.setattr(search, "load_model", lambda _: (None, None))
    started, joined, release = Event(), Event(), Event()
    calls = []

    class ObservedFuture(Future):
        def result(self, timeout=None):
            joined.set()
            return super().result(timeout)

    def rank(*_):
        calls.append(1)
        started.set()
        assert release.wait(2)
        return [1.0]

    monkeypatch.setattr(search, "score_pairs", rank)
    monkeypatch.setattr(search, "Future", ObservedFuture)
    pool = [dict(title="Regulation", section="1", text="Rule")]
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(worker.rank, "Question", pool)
        try:
            assert started.wait(2)
            second = workers.submit(worker.rank, "Question", pool)
            assert joined.wait(2)
        finally:
            release.set()
        assert first.result() == second.result() == (1.0,)
    assert calls == [1]
