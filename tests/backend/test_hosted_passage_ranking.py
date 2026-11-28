from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from backend.assembly import hosted_rank, models, search
from backend.contracts.engine import EngineUnavailable

QUESTION = "Кто в ДБРЦТ контролирует технические риски?"
POOL = [dict(title="ДБРЦТ", section=f"Глава {i//2}", ref=str(i+1), text=f"Оригинал {i}") for i in range(6)]

def setup(question=QUESTION, **kw):
    request = SimpleNamespace(**dict(dict(question=question, history=(), conditions={}, clarification=None,
                                          clarification_count=0, deadline=datetime.now(timezone.utc)+timedelta(seconds=15),
                                          attempts=None), **kw))
    return request, SimpleNamespace(request=request, profile=SimpleNamespace(is_local=False), remaining=lambda: 14,
                                    chat=Mock(return_value={"text":"BFDCEA"}))


def test_guards_for_primary_search_only(monkeypatch):
    request, calls = setup()
    monkeypatch.delenv("II_HOSTED_PASSAGE_RANKING", raising=False)
    assert not hosted_rank.eligible(request,calls)
    monkeypatch.setenv("II_HOSTED_PASSAGE_RANKING","1")
    assert hosted_rank.eligible(request,calls)
    calls.profile.is_local=True
    assert not hosted_rank.eligible(request,calls)
    for kw in ({"history":("earlier",)}, {"clarification":{"value":"ИБ"}}, {"conditions":{"key":"ИБ"}},
               {"clarification_count":1}):
        assert not hosted_rank.eligible(*setup(**kw))
    for q in ("Можно ли начать без договора?", "Можем выходить на конкурс до оценки?",
              "Её уже могут взять в работу?", "При каких условиях это возможно?", "Кто подписывает смету? Кто назначает команду?",
              "Как по шагам оформить новый проект?", "Что делать и кто утверждает смету?", "слово "*21):
        assert not hosted_rank.eligible(*setup(q))
    _,original=setup("Как получить документ? И что делать дальше?")
    assert not hosted_rank.eligible(request,original)


def test_all_visible_originals_and_complete_order():
    _, calls = setup()
    scores = hosted_rank.ranking(QUESTION,POOL,calls)
    assert scores == (1, 6, 3, 4, 2, 5)
    name,body,role=calls.chat.call_args.args
    assert (name,role,body['model']) == ('passages','passage-ranking',models.GEMMA)
    payload=json.loads(body['messages'][1]['content'])
    assert [payload['title'][i] for _,i,*_ in payload['passages']] == [p['title'] for p in POOL]
    assert [[payload['section'][row[2]],row[3],row[4]] for row in payload['passages']] == [[p['section'],p['ref'],p['text']] for p in POOL]
    assert len(payload['title'])==1 and len(payload['section'])==3


def test_only_bundled_demo_revisions_use_hosted_ranking():
    _, calls = setup()
    pool = [dict(p, doc="DB") for p in POOL]
    originals = {("DB", p["ref"]): hosted_rank.DB_TRAINING_REVISION[0] for p in pool}
    assert hosted_rank.rank_demo_passages(QUESTION, pool, calls=calls, originals=originals)
    for key, fragment in ((("DB", "1"), pool[0]), (("uploaded", "1"), dict(pool[0], doc="uploaded"))):
        calls.chat.reset_mock()
        assert hosted_rank.rank_demo_passages(
            QUESTION, [fragment, *pool[1:]], calls=calls, originals={**originals, key: "new content"},
        ) is None
        calls.chat.assert_not_called()


@pytest.mark.parametrize('response',[{'text':'BE'}, {'text':'ABACDF'}, {'text':'ABCDEFx'}, None, {}, {'text':123}])
def test_malformed_or_partial_order_returns_to_local(response):
    _,calls=setup(); calls.chat.return_value=response
    assert hosted_rank.ranking(QUESTION,POOL,calls) is None


def test_before_sending_and_after_provider_failure():
    _,calls=setup(); calls.chat.side_effect=EngineUnavailable('failure')
    assert hosted_rank.ranking(QUESTION,POOL,calls) is None
    assert calls.chat.call_count == 1
    assert hosted_rank.ranking(QUESTION, [dict(POOL[0],text='а'*20000)]*3, calls) is None
    assert hosted_rank.ranking(QUESTION, POOL*6, calls) is None
    assert hosted_rank.ranking('a followup', POOL, calls) is None
    calls.remaining=lambda: 5
    assert hosted_rank.ranking(QUESTION,POOL,calls) is None
    assert calls.chat.call_count == 1


def test_ranker_does_not_write_minilm_cache(monkeypatch):
    engine=search.Search(None,'.'); engine.__dict__['model']=(None,None)
    local=[.4]*len(POOL)
    monkeypatch.setattr(search,'score_pairs',Mock(return_value=local)); rank=Mock(return_value=tuple(range(len(POOL))))
    assert engine.rank(QUESTION,POOL,alternative=rank) == tuple(range(len(POOL)))
    assert not engine._rankings
    rank.return_value=None
    assert engine.rank(QUESTION,POOL,alternative=rank) == tuple(local)
    assert engine.rank(QUESTION,POOL,alternative=rank) == tuple(local)
    assert rank.call_count == 2
    assert search.score_pairs.call_count == 1


def test_rank_retry_is_bounded_and_uses_established_attempt_path(monkeypatch):
    request,_ = setup(); calls=models.ModelCalls(request)
    monkeypatch.setenv('OPENROUTER_API_KEY','stub')
    failure=models.UpstreamFailure('timeout',retryable=True)
    complete=Mock(side_effect=failure); monkeypatch.setattr(models,'complete',complete)
    assert hosted_rank.ranking(QUESTION,POOL,calls) is None
    assert complete.call_count == 1
    call=complete.call_args
    assert call.args[1]['provider']['only']==['deepinfra/fp8'] and call.kwargs['timeout']<=2
