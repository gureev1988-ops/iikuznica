"""Both quotations on an unresolved conflict must come from its authorized candidates."""
from uuid import uuid4

from backend.assembly.engine import A1Engine
from backend.contracts.engine import SourceFragment


def test_regulation_conflict_is_an_offer_with_two_verified_citations():
    first, second = ("A", "1"), ("B", "1")
    refs = {
        first: SourceFragment(uuid4(), uuid4(), uuid4(), "Срок составляет 3 рабочих дня.", "Регламент A"),
        second: SourceFragment(uuid4(), uuid4(), uuid4(), "Срок составляет 7 рабочих дней.", "Регламент B"),
    }
    candidates = {key: {"text": ref.text} for key, ref in refs.items()}
    basis = [{"doc": doc, "ref": ref, "quote": refs[doc, ref].text} for doc, ref in refs]
    question = "Какой срок при данных условиях?"
    result = A1Engine._conflict_result(basis, question, candidates, refs)
    assert result.action == "escalate" and not result.allow_faq
    assert result.escalation_question == question
    assert [source.revision_id for source in result.citations] == [refs[first].revision_id, refs[second].revision_id]
    assert A1Engine._conflict_result([basis[0], basis[0]], question, candidates, refs) is None
    assert A1Engine._conflict_result(basis, question, {first: candidates[first]}, refs) is None
    wrong = [basis[0], dict(basis[1], quote="Срок составляет 4 рабочих дня.")]
    assert A1Engine._conflict_result(wrong, question, candidates, refs) is None
