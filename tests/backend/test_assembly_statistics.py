from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from psycopg.types.json import Jsonb

from backend.assembly.analytics_web import mount_analytics
from backend.assembly.corpus import seed
from backend.assembly.gap_groups import classify_gap
from backend.assembly.statistics import GROUP_JOB, record_facts
from backend.assembly.web import Assembly
from backend.config import Settings
from backend.db import connect
from backend.dialog.service import DialogueService
from backend.mailfaq.transport import MailConfig
from backend.runtime import claim, finish

BASE = "https://assembly.test"


def browser_api(dsn, monkeypatch):
    from backend.assembly.web import GUEST_COOKIE, digest
    settings = Settings(database_url=dsn, profile="assembly", public_base_url=BASE)
    browser = Assembly(settings, credentials={"II_JUDGE_LOGIN": "judge", "II_JUDGE_PASSWORD": "a"*20,
        "II_BOT_PASSWORD": "b"*20, "II_QUALITY_PASSWORD": "c"*20})
    dialog = DialogueService(dsn=dsn, engine=None, mail_config=MailConfig())
    app = FastAPI()
    mount_analytics(app, SimpleNamespace(assembly=browser, dsn=dsn, dialog=dialog))
    client = TestClient(app, base_url=BASE, headers={"Origin": BASE})
    token = str(uuid4())
    with connect(dsn) as conn:
        row = conn.execute("""INSERT INTO guest_sessions(token_sha256,expires_at)
            VALUES (%s,now()+interval '1 hour') RETURNING id,conversation_id""",(digest(token),)).fetchone()
        actor = "guest:"+str(row["id"])
        conn.execute("INSERT INTO conversations(id,owner_id) VALUES(%s,%s)", (row["conversation_id"],actor))
    client.cookies.set(GUEST_COOKIE, token)
    return client, row["conversation_id"], actor


def turn(conn, conversation, question, public, case=None):
    if case is None:
        case = conn.execute("INSERT INTO cases(conversation_id) VALUES (%s) RETURNING id",
                            (conversation,)).fetchone()["id"]
    t = conn.execute("""INSERT INTO turns(conversation_id,case_id,request_id,question,status,result)
        VALUES(%s,%s,%s,%s,'completed',%s) RETURNING id,case_id""",
        (conversation,case,str(uuid4()),question,Jsonb(public))).fetchone()
    m = conn.execute("""INSERT INTO messages(conversation_id,case_id,turn_id,role,text)
        VALUES(%s,%s,%s,'assistant','reply') RETURNING id""",(conversation,case,t["id"])).fetchone()
    return t, str(m["id"])


def test_ratings_issues_history_original_and_acl(dsn, tmp_path, monkeypatch):
    monkeypatch.setenv("II_ORIGINALS",str(tmp_path))
    seed(dsn, tmp_path)
    client, cid, owner = browser_api(dsn,monkeypatch)
    stranger, _, _ = browser_api(dsn,monkeypatch)
    catalog = client.get("/v1/browser/documents").json()
    assert len(catalog)==4
    doc = catalog[0]
    ref_url = doc["original_url"].removesuffix("/original")
    snapshot = client.get(ref_url).json()
    assert not snapshot["historical"] and len(snapshot["fragments"])>3
    assert client.get(doc["original_url"]).content.startswith(b"PK")
    def cite(i):
        f=snapshot["fragments"][i]
        return dict(kind="document",document_id=doc["document_id"],revision_id=doc["revision_id"],
                    fragment_id=f["fragment_id"],quote=f["text"],url="/citation")
    refs = [cite(1),cite(2)]
    with connect(dsn) as conn:
        _, message = turn(conn,cid,"Кто принимает решение?",dict(action="answer",sources=refs))
        _, navigation = turn(conn,cid,"Где регламент?",dict(action="answer",sources=[dict(refs[0],claim="Открыть регламент")]))
        _, system = turn(conn,cid,"да",dict(action="answer",sources=[]))
        turn(conn,cid,"Выбор",dict(action="clarify",sources=refs))
    ratings_url=f"/v1/browser/conversations/{cid}/ratings"
    rating_url=f"/v1/browser/messages/{message}/rating"
    assert client.get(ratings_url).json()==[dict(message_id=message,rating=None)]
    for id_ in (system,navigation):
        assert client.put(f"/v1/browser/messages/{id_}/rating",json={"rating":"up"}).status_code==404
    assert stranger.get(ratings_url).status_code==404
    assert stranger.put(rating_url,json={"rating":"up"}).status_code==404
    assert client.put(rating_url,json={"rating":"up"}).status_code==200
    assert client.get("/v1/browser/statistics").json()["ratings"]==dict(up=1,down=0)
    for _ in range(2):
        assert client.put(rating_url,json={"rating":"down"}).status_code==200
    stats=client.get("/v1/browser/statistics").json()
    assert stats["documents"]==[dict(document_id=doc["document_id"],revision_id=doc["revision_id"],
                                      title=doc["title"],version=1,issues=2)]
    assert stats["ratings"]==dict(up=0,down=1)
    assert client.get(ratings_url).json()==[dict(message_id=message,rating="down")]
    seed(dsn,tmp_path,version=2)
    assert client.get(ref_url).json()["historical"]
    assert client.get(doc["original_url"]).status_code==200
    with connect(dsn) as conn:
        conn.execute("UPDATE source_revisions SET acl_actor_ids=ARRAY['private'] WHERE id=%s",(doc["revision_id"],))
    assert client.get(ref_url).status_code==404
    assert client.get(doc["original_url"]).status_code==404
    assert client.get(ratings_url).json()==[]
    assert client.get("/v1/browser/statistics").json()["documents"]==[]


def test_repeat_groups_case_period_and_private_question(dsn, monkeypatch):
    client, cid, owner = browser_api(dsn,monkeypatch)
    calls=[]
    def decide(_dsn,job,gap,candidates,_search):
        calls.append((gap["question"],gap["conditions"]))
        return (candidates[0]["id"] if candidates else None, "Срок назначения руководителя проекта")
    monkeypatch.setattr("backend.assembly.gap_groups.choose_group",decide)
    missed=dict(action="escalate",sources=[])
    with connect(dsn) as conn:
        for text, scope in [("Когда назначат РП?","ДБРЦТ"),
                            ("Сколько ждать назначения руководителя проекта?","ДБРЦТ"),
                            ("Когда назначат РП?","Дирекция ИБ")]:
            t,msg=turn(conn,cid,text,missed)
            for _ in range(2):
                record_facts(conn,t,missed,{"directorate":scope},text)
        t,msg=turn(conn,cid,"Уточнение к последнему вопросу",missed,case=t["case_id"])
        record_facts(conn,t,missed,{"directorate":"Дирекция ИБ"},"тот же вопрос")
    for _ in range(3):
        job=claim(dsn,"background",active_kinds=[GROUP_JOB])
        classify_gap(dsn,None,job)
        classify_gap(dsn,None,job)
        with connect(dsn) as conn:
            finish(conn,job)
    assert len(calls)==3
    stats=client.get("/v1/browser/statistics").json()
    assert stats["gaps_total"]==3
    assert len(stats["groups"])==1
    group=stats["groups"][0]
    assert group["count"]==2 and group["conditions"]=={"directorate":"ДБРЦТ"}
    assert stats["problems"][0]["gaps"]==4 and stats["problems"][0]["fragment_id"] is None
    assert "Когда назначат" not in str(stats)
    url=f"/v1/browser/statistics/groups/{group['id']}/questions"
    assert client.get(url).status_code==401
    monkeypatch.setattr(Assembly,"judge_active",lambda self,request,**kwargs:True)
    assert {q["question"] for q in client.get(url).json()}=={
        "Когда назначат РП?","Сколько ждать назначения руководителя проекта?"}


def test_only_unresolved_parts_and_latest_active_conflict_count(dsn, tmp_path, monkeypatch):
    from backend.assembly.quality_seed import seed_quality
    from backend.documents import list_fragments

    seed(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    client, cid, owner = browser_api(dsn, monkeypatch)
    with connect(dsn) as conn:
        fragments = list_fragments(conn, owner)
        doc = next(f for f in fragments if f.kind == "document")
        faq = next(f for f in fragments if f.kind == "faq")
        def ref(f):
            return dict(kind=f.kind, document_id=str(f.document_id), revision_id=str(f.revision_id),
                        fragment_id=str(f.fragment_id), quote=f.text)
        entry = conn.execute("SELECT faq_id FROM assembly_quality_seed").fetchone()["faq_id"]
        for used_as_basis in (False, True):
            faq_ref = ref(faq)
            if not used_as_basis:
                faq_ref["claim"] = "Альтернативное разъяснение"
            reply = dict(action="answer", sources=[ref(doc), faq_ref])
            t, _ = turn(conn, cid, "Какой срок?", reply)
            for _ in range(2):
                record_facts(conn, t, reply, {}, None if used_as_basis else "Уточнить расхождение?")
        assert conn.execute("SELECT count(*) n FROM assembly_faq_uses").fetchone()["n"] == 1
        partial = dict(action="answer", sources=[ref(doc)],
                       parts=[dict(action="answer", sources=[ref(doc)]), dict(action="escalate", sources=[])])
        t, _ = turn(conn, cid, "Два вопроса", partial)
        record_facts(conn, t, partial, {}, "Вторая часть?")
        unresolved = dict(action="escalate", sources=[ref(doc)])
        t, _ = turn(conn, cid, "В каком регламенте описана процедура?", unresolved)
        record_facts(conn, t, unresolved, {}, "В каком регламенте описана процедура?")
        conn.execute("""INSERT INTO assembly_quality_checks(faq_id,faq_revision_id,outcome,fragment_id,reason,catalog)
                        VALUES(%s,%s,'conflict',%s,'проверено','{}')""", (entry,faq.revision_id,doc.fragment_id))
    def problems():
        return {r["fragment_id"]: (r["gaps"],r["conflicts"]) for r in client.get("/v1/browser/statistics").json()["problems"]}
    assert problems() == {None:(1,0),str(doc.fragment_id):(1,1)}
    with connect(dsn) as conn:
        conn.execute("UPDATE faq_entries SET status='withdrawn' WHERE id=%s",(entry,))
    assert problems() == {None:(1,0),str(doc.fragment_id):(1,0)}
    with connect(dsn) as conn:
        conn.execute("UPDATE turns SET updated_at=now()-interval '31 days'")
    assert problems() == {}
