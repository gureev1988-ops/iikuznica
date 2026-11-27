"""Search uses CPU limits without changing the pinned ranking calculation."""
from concurrent.futures import ThreadPoolExecutor
from threading import BoundedSemaphore, Event, Lock

from eval.rerank import rerank


def test_cgroup_quota_and_worker_affinity_both_limit_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(rerank.os, "sched_getaffinity", lambda _: set(range(6)), raising=False)
    membership = tmp_path / "membership"
    membership.write_text("0::/service/worker\n")
    groups = tmp_path / "cgroup"
    worker = groups / "service" / "worker"
    worker.mkdir(parents=True)
    (groups / "cpu.max").write_text("max 100000\n")
    (worker.parent / "cpu.max").write_text("250000 100000\n")
    (worker / "cpu.max").write_text("400000 100000\n")
    assert rerank.cpu_budget(groups, membership) == 2
    monkeypatch.setattr(rerank.os, "sched_getaffinity", lambda _: {0}, raising=False)
    assert rerank.cpu_budget(groups, membership) == 1


def test_legacy_cgroup_quota_handles_fractional_and_unlimited_cpu(tmp_path, monkeypatch):
    monkeypatch.setattr(rerank.os, "sched_getaffinity", lambda _: set(range(4)), raising=False)
    membership = tmp_path / "membership"
    membership.write_text("2:cpu,cpuacct:/worker\n")
    groups = tmp_path / "cgroup"
    worker = groups / "cpu,cpuacct" / "worker"
    worker.mkdir(parents=True)
    (worker / "cpu.cfs_period_us").write_text("100000\n")
    quota = worker / "cpu.cfs_quota_us"
    quota.write_text("50000\n")
    assert rerank.cpu_budget(groups, membership) == 1
    quota.write_text("-1\n")
    assert rerank.cpu_budget(groups, membership) == 4


def test_ranker_caps_live_calls_without_changing_their_scores(monkeypatch):
    slots = BoundedSemaphore(2)
    lock = Lock()
    third_waiting, two_running, release = Event(), Event(), Event()
    attempts = running = peak = 0

    class TrackedSlots:
        def __enter__(self):
            nonlocal attempts
            with lock:
                attempts += 1
                if attempts == 3:
                    third_waiting.set()
            return slots.__enter__()

        def __exit__(self, *exc):
            return slots.__exit__(*exc)

    def score(session, tokenizer, question, sources):
        nonlocal running, peak
        with lock:
            running += 1
            peak = max(peak, running)
            if running == 2:
                two_running.set()
        try:
            assert release.wait(timeout=3)
            return [float(question)]
        finally:
            with lock:
                running -= 1

    monkeypatch.setattr(rerank, "_RANK_SLOTS", TrackedSlots())
    monkeypatch.setattr(rerank, "_score_pairs", score)
    with ThreadPoolExecutor(max_workers=3) as workers:
        results = [workers.submit(rerank.score_pairs, None, None, str(i), [i]) for i in range(3)]
        try:
            assert two_running.wait(timeout=3) and third_waiting.wait(timeout=3)
            with lock:
                assert peak == 2
        finally:
            release.set()
        assert [future.result() for future in results] == [[0.0], [1.0], [2.0]]
