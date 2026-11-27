from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import ContextVar
from threading import Barrier, BoundedSemaphore, Event, current_thread
import unittest
from unittest.mock import Mock, patch

import dialogue_state as state
from eval.search_core import core


FIRST = "Кто согласует договор в ДБРЦТ?"
SECOND = "Кто подписывает договор в Технической дирекции при отсутствии гарантии?"


def reply(doc, message):
    return dict(action="answer", message=message + " [1]", sources=[dict(doc=doc, ref="1.1")],
                offer_quality_request=False)


class ParallelPartsTests(unittest.TestCase):
    def test_parallel_parts_overlap_and_assemble_in_request_order(self):
        first = "А в Дирекции ИБ?"
        past = [dict(user=FIRST, resolved=FIRST, assistant="Согласует АМ.", action="answer")]
        barrier = Barrier(2)
        second_done = Event()
        request_scope = ContextVar("request_scope", default=None)
        request_scope.set("turn-1")

        def respond(query, search, **options):
            self.assertEqual(request_scope.get(), "turn-1")
            barrier.wait(timeout=3)
            if query == SECOND:
                second_done.set()
                return reply("TECH", "При отсутствии гарантии действует условие")
            self.assertTrue(second_done.wait(timeout=3))
            self.assertEqual(query, "Дирекция ИБ. Кто согласует договор?")
            return reply("IB", "Действие по регламенту ИБ")

        question = f"{first} {SECOND}"
        result = state.answer(question, Mock(side_effect=AssertionError), past=past, respond=respond,
                              planner=Mock(side_effect=AssertionError), corpus={}, parallel_parts=True)
        self.assertEqual(result["resolved_question"], question)
        self.assertEqual([part["text"] for part in result["requests"]], [first, SECOND])
        self.assertEqual(result["sources"], [dict(doc="IB", ref="1.1"), dict(doc="TECH", ref="1.1")])
        self.assertEqual(result["message"], f"{first}\nДействие по регламенту ИБ [1]\n\n"
                         f"{SECOND}\nПри отсутствии гарантии действует условие [2]")

    def test_dependent_and_legacy_mode_run_on_calling_thread(self):
        dependent = f"{FIRST} Куда его отправить в Технической дирекции?"
        explicit = f"{FIRST} {SECOND}"
        caller = current_thread()
        seen = []

        def respond(query, search, **options):
            self.assertIs(current_thread(), caller)
            seen.append(query)
            return reply("DB", "Действие по регламенту")

        state.answer(dependent, None, respond=respond, corpus={}, parallel_parts=True)
        self.assertEqual(seen, [dependent])
        seen.clear()
        core_answer = Mock(side_effect=respond)
        with patch.object(core.ask, "answer", core_answer):
            core.answer(explicit, None, corpus={}, parallel_parts=False)
        self.assertEqual(seen, [FIRST, SECOND])

    def test_multiple_turns_share_the_worker_limit(self):
        occupied = Event()
        release = Event()
        second_threads = []

        def respond_while_occupied(query, search, **options):
            if query == SECOND:
                occupied.set()
                self.assertTrue(release.wait(timeout=3))
            return reply("DB", "Действие по регламенту")

        def respond_without_a_slot(query, search, **options):
            second_threads.append(current_thread())
            return reply("DB", "Действие по регламенту")

        question = f"{FIRST} {SECOND}"
        with patch.object(state, "_PAIR_SLOTS", BoundedSemaphore(1)), ThreadPoolExecutor(max_workers=1) as turns:
            active = turns.submit(state.answer, question, None, respond=respond_while_occupied,
                                  corpus={}, parallel_parts=True)
            try:
                self.assertTrue(occupied.wait(timeout=3))
                state.answer(question, None, respond=respond_without_a_slot, corpus={}, parallel_parts=True)
                self.assertEqual(second_threads, [current_thread(), current_thread()])
            finally:
                release.set()
            active.result(timeout=3)

    def test_failure_propagates_after_sibling_finishes(self):
        barrier = Barrier(2)
        release = Event()
        failing = Event()
        finished = Event()

        def respond(query, search, **options):
            barrier.wait(timeout=3)
            if query == FIRST:
                failing.set()
                raise TimeoutError("model timed out")
            self.assertTrue(release.wait(timeout=3))
            finished.set()
            return reply("TECH", "Действие по регламенту")

        with ThreadPoolExecutor(max_workers=1) as turns:
            future = turns.submit(state.answer, f"{FIRST} {SECOND}", None, respond=respond,
                                  corpus={}, parallel_parts=True)
            try:
                self.assertTrue(failing.wait(timeout=3))
                self.assertFalse(future.done())
            finally:
                release.set()
            with self.assertRaisesRegex(TimeoutError, "model timed out"):
                future.result(timeout=3)
            self.assertTrue(finished.is_set())

    def test_core_shares_search_for_concurrent_lookups_in_one_turn(self):
        joined = Event()
        seen = []

        class CacheEntry(Future):
            def result(self, timeout=None):
                joined.set()
                return super().result(timeout)

        def search(query):
            seen.append(query)
            self.assertTrue(joined.wait(timeout=3))
            return [("DB", "1.1")], []

        def respond(query, lookup, **options):
            seeds, _ = lookup(query)
            self.assertEqual(seeds, [("DB", "1.1")])
            return reply("DB", "Действие по регламенту")

        with patch.object(core, "Future", CacheEntry), patch.object(core.ask, "answer", respond):
            result = core.answer(f"{FIRST} {FIRST}", search, corpus={})
        self.assertEqual(seen, [FIRST])
        self.assertEqual(len(result["requests"]), 2)
        self.assertEqual(result["sources"], [dict(doc="DB", ref="1.1")])
