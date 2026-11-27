from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.assembly.corpus import seed
from backend.assembly.quality_seed import QUESTION, seed_quality
from backend.assembly.web import Assembly
from backend.db import connect


def test_guest_and_expert_quality_loop(dsn, tmp_path):
    seed(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    origin = "https://quality.example"
    assembly = Assembly(
        SimpleNamespace(dsn=dsn, public_base_url=origin),
        credentials={
            "II_JUDGE_LOGIN": "operator",
            "II_JUDGE_PASSWORD": "operator-password-long",
            "II_BOT_PASSWORD": "bot-password-long",
            "II_QUALITY_PASSWORD": "quality-password-long",
            "II_DEMO_EXPERT_LOGIN": "admin",
            "II_DEMO_EXPERT_PASSWORD": "admin",
        },
    )
    app = FastAPI()

    class Dialog:
        def ensure_conversation(self, conn, actor, conversation):
            conn.execute(
                "INSERT INTO conversations(id,owner_id) VALUES (%s,%s) ON CONFLICT DO NOTHING",
                (conversation, actor.id),
            )

    assembly.mount(app, SimpleNamespace(dialog=Dialog(), assembly=assembly, dsn=dsn))
    api = TestClient(app, base_url=origin, headers={"Origin": origin})
    path = "/v1/browser/quality"
    assert api.get(path + "/cards").status_code == 401
    conversation = api.post("/v1/browser/session").json()["conversation_id"]
    (card,) = api.get(path + "/cards").json()
    assert (card["uses"], card["status"], card["question"]) == (5, "active", QUESTION)
    (mail,) = api.get(path + "/threads?q=точной").json()
    assert (mail["subject"], mail["open"]) == ("Обращение ОК1", False)
    original = api.get(path + "/threads/" + mail["id"]).json()
    assert len(original["messages"]) == 2
    assert original["reply_message_id"] is None
    cmd = {"command_id": str(uuid4())}
    report = api.post(path + "/checks", json=cmd).json()
    assert "7 рабочих дней" in report["text"] and "6 рабочих дней" in report["text"]
    assert api.post(path + "/checks", json=cmd).json() == report
    (checked,) = api.get(path + "/cards").json()
    assert checked["status"] == "review" and checked["evidence"]["anchor"] == "1.6"
    assert "7 рабочих дней" in checked["evidence"]["quote"]
    assert api.post(path + "/checks", json={"command_id": str(uuid4())}).status_code == 200
    assert api.get(path + "/cards").json()[0]["updated_at"] == checked["updated_at"]
    card_path = path + "/cards/" + card["id"]
    assert api.post(card_path + "/withdraw", json={"version": checked["version"]}).status_code == 401
    assert api.post("/v1/browser/mail/login", json={"login": "admin", "password": "admin"}).status_code == 200
    assert api.get("/v1/browser/mail/inbox").status_code == 401
    unsigned = {"question": QUESTION, "answer": card["answer"], "version": checked["version"]}
    assert api.post(card_path + "/edit", json=unsigned).json()["detail"]["code"] == "confirm_conflict"
    edit = api.post(card_path + "/edit", json={**unsigned, "confirm_conflict": True}).json()
    assert edit["version"] > checked["version"] and edit["status"] == "review"
    assert api.post(card_path + "/edit", json=unsigned).status_code == 409
    repaired = api.post(
        card_path + "/edit",
        json={
            **unsigned,
            "version": edit["version"],
            "answer": card["answer"].replace("6 рабочих", "7 рабочих"),
        },
    ).json()
    assert repaired["status"] == "active"
    assert api.get(path + "/threads/" + mail["id"]).json() == original
    withdrawn = api.post(card_path + "/withdraw", json={"version": repaired["version"]}).json()
    assert withdrawn["status"] == "withdrawn"
    resumed = api.post(card_path + "/resume", json={"version": withdrawn["version"]}).json()
    assert resumed["status"] == "active" and resumed["evidence"]["outcome"] == "consistent"
    assert any(h["action"] == "withdraw" for h in resumed["history"])
    reply = dict(command_id=str(uuid4()), message_id=original["messages"][0]["message_id"], text="Повтор")
    assert api.post("/v1/browser/mail/reply", json=reply).status_code == 409
    assert api.post("/v1/browser/mail/logout").status_code == 200
    assert api.post("/v1/browser/session").json() == {
        "conversation_id": conversation,
        "mail": False,
        "standard_demo_login": True,
    }
    with connect(dsn) as conn:
        versions = conn.execute("""SELECT r.version,f.text FROM source_revisions r JOIN source_fragments f
            ON r.id=f.revision_id JOIN sources s ON s.id=r.source_id WHERE s.external_id='DB' AND f.anchor='1.6'
            ORDER BY r.version""").fetchall()
        assert [r["version"] for r in versions] == [1, 2]
        assert "5 рабочих дней" in versions[0]["text"] and "7 рабочих дней" in versions[1]["text"]
        conn.execute("UPDATE mail_requests SET channel='web' WHERE id=%s", (card["request_id"],))
    assert api.get(path + "/cards").json() == []
    assert api.get(path + "/threads/" + mail["id"]).status_code == 404


def test_review_is_optimistic_and_keeps_evidence_on_provider_failure(dsn, tmp_path, monkeypatch):
    import pytest
    from fastapi import HTTPException
    from backend.assembly import quality
    from backend.assembly.quality_checks import evidence

    seed(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    with connect(dsn) as conn:
        quality.run_checks(conn, uuid4())
        original = conn.execute("SELECT * FROM assembly_quality_checks").fetchone()
        # A changed catalog forces a fresh check even for a previously confirmed verdict.
        conn.execute("UPDATE assembly_quality_checks SET catalog='{}'")

    def offline(conn, question, answer, conditions):
        with connect(dsn) as reader:
            assert reader.execute("SELECT pg_try_advisory_xact_lock_shared(31007) AS free").fetchone()["free"]
        return {**evidence("unconfirmed", "Сервис проверки временно недоступен."), "unavailable": True}

    monkeypatch.setattr(quality, "judge", offline)
    with connect(dsn) as conn:
        report = quality.run_checks(conn, uuid4())
        saved = conn.execute("SELECT * FROM assembly_quality_checks").fetchone()
        assert saved["outcome"] == "conflict" and saved["fragment_id"] == original["fragment_id"]
        assert "недоступен" in report["text"]
        version = conn.execute("SELECT version FROM faq_entries").fetchone()["version"]

    def concurrent_edit(conn, question, answer, conditions):
        with connect(dsn) as other:
            other.execute("UPDATE faq_entries SET version=version+1")
        return evidence("no_source", "Подходящий регламент не найден.")

    monkeypatch.setattr(quality, "judge", concurrent_edit)
    with pytest.raises(HTTPException) as changed:
        with connect(dsn) as conn:
            quality.change_card(
                conn,
                saved["faq_id"],
                quality.Edit(version=version, question=QUESTION, answer="Иная норма"),
                "edit",
                "Эксперт",
            )
    assert changed.value.status_code == 409
    with connect(dsn) as conn:
        assert conn.execute("SELECT outcome FROM assembly_quality_checks").fetchone()["outcome"] == "conflict"


def test_multiple_conflicting_clauses_remain_counted_once_per_card(dsn, tmp_path, monkeypatch):
    from backend.assembly import quality
    from backend.assembly.quality_checks import verified_claim
    from backend.assembly.statistics import summary

    seed(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    with connect(dsn) as conn:
        row = conn.execute(quality.CARD_SELECT, quality.SCOPE).fetchone()
        norms = conn.execute("""SELECT fr.id,fr.anchor,fr.text,r.title,r.id AS revision_id,s.id AS source_id
            FROM source_fragments fr JOIN source_revisions r ON r.id=fr.revision_id
            JOIN sources s ON s.current_revision_id=r.id WHERE s.external_id='DB' AND fr.anchor IN ('1.5','1.6')
            ORDER BY fr.anchor""").fetchall()
        answer = "Утверждение А. Утверждение Б."
        first, second = [
            verified_claim(
                dict(index=i, quote=n["text"].splitlines()[0], claim=claim, reason="Требуется пересмотр."),
                norms,
                answer,
            )
            for i, (n, claim) in enumerate(zip(norms, ["Утверждение А.", "Утверждение Б."]))
        ]
        assert (
            verified_claim(
                dict(index=0, quote=norms[0]["text"], claim="Чужая формулировка", reason=""), norms, answer
            )
            is None
        )
        monkeypatch.setattr(quality, "judge", lambda *args: {**first, "additional_conflicts": [second]})
        edited = quality.change_card(
            conn,
            row["id"],
            quality.Edit(version=row["version"], question=QUESTION, answer=answer, confirm_conflict=True),
            "edit",
            "Эксперт",
        )
        assert edited["additional_conflicts"][0]["claim"] == "Утверждение Б."
        quality.run_checks(conn, uuid4())
        problems = summary(conn, "guest:review")["problems"]
        assert sorted((p["anchor"], p["conflicts"]) for p in problems) == [("1.5", 1), ("1.6", 1)]
        quality.change_card(
            conn, row["id"], quality.Version(version=edited["version"]), "withdraw", "Эксперт"
        )
        assert summary(conn, "guest:review")["problems"] == []


def test_legacy_faq_editions_survive_first_new_expert_action(dsn, tmp_path):
    from backend.assembly import quality
    from backend.mailfaq.faq import replace_faq

    seed(dsn, tmp_path)
    seed_quality(dsn, tmp_path)
    with connect(dsn) as conn:
        original = conn.execute(quality.CARD_SELECT, quality.SCOPE).fetchone()
        replace_faq(conn, original["id"], original["version"], "Исправленный архивный ответ.")
        before = quality.serialize(conn, quality.get_card(conn, original["id"]))
        assert [(entry["version"], entry["answer"]) for entry in before["history"]] == [
            (1, original["answer"]),
            (2, "Исправленный архивный ответ."),
        ]
        after = quality.change_card(
            conn, original["id"], quality.Version(version=before["version"]), "withdraw", "Эксперт"
        )
        assert [entry["version"] for entry in after["history"]] == [1, 2, 3]
        assert after["history"][0]["action"] == "archived_revision"
        assert after["history"][-1]["action"] == "withdraw"
