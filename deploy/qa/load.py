"""Measure real browser API admission and completion on the isolated release stand."""
import argparse
import asyncio
import json
from math import ceil
import os
import time
from uuid import uuid4

import httpx

QUESTIONS = (
    "Кто в дирекции ИБ на инициации при полных данных назначает руководителя проекта и за сколько?",
    "Сколько рабочих часов предусмотрено для назначения руководителя проекта в ДБРЦТ после запроса?",
    "За сколько рабочих дней в ДБРЦТ рекомендуют сделать бюджетную оценку без подрядчика?",
    "Кто проводит внутреннее установочное совещание в технической дирекции?",
    "Кто согласует форму договора заказчика по §7.3 в технической дирекции?",
)


def percentile(values, p):
    return round(sorted(values)[ceil(len(values) * p) - 1], 3) if values else None


async def run(url, verify, concurrency):
    async def session():
        client = httpx.AsyncClient(base_url=url, verify=verify, trust_env=False, timeout=20,
                                   headers={"Origin": url})
        response = await client.post("/v1/browser/session", json={})
        response.raise_for_status()
        return client, response.json()["conversation_id"]

    async def issue(client, conversation, question):
        start = time.monotonic()
        try:
            result = await client.post(f"/v1/conversations/{conversation}/messages", json={
                "client_message_id": str(uuid4()), "text": question,
            })
            if result.status_code != 202:
                return {"http": result.status_code}
            turn_id = result.json()["turn_id"]
            while time.monotonic() - start <= 120:
                turn = await client.get(f"/v1/turns/{turn_id}")
                turn.raise_for_status()
                view = turn.json()
                if view["status"] in {"completed", "technical_failure"}:
                    answer = view["result"]
                    seconds = time.monotonic() - start
                    return {"http": 202, "turn_id": turn_id, "status": view["status"],
                            "action": answer["action"] if answer else None, "seconds": seconds,
                            "useful": bool(answer and answer["sources"] and answer["action"] == "answer")}
                await asyncio.sleep(0.35)
            return {"http": 202, "turn_id": turn_id, "status": "timeout"}
        finally:
            await client.aclose()

    sessions = await asyncio.gather(*(session() for _ in range(concurrency)))
    rows = await asyncio.gather(*(issue(client, conversation, QUESTIONS[i % len(QUESTIONS)])
                                  for i, (client, conversation) in enumerate(sessions)))
    durations = [row["seconds"] for row in rows if row.get("useful")]
    http = {code: sum(row["http"] == code for row in rows) for code in sorted({row["http"] for row in rows})}
    print(json.dumps({"mode": "live OpenRouter + HTTPS/HTTP API + PostgreSQL; mail unused",
                      "submitted": concurrency, "http": http, "verified_answer": len(durations),
                      "complete": sum(row.get("status") == "completed" for row in rows),
                      "p50_useful_s": percentile(durations, .5), "p95_useful_s": percentile(durations, .95),
                      "turns": rows}, ensure_ascii=False))
    return all(row.get("status") == "completed" for row in rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://localhost:4443")
    parser.add_argument("--ca", default=os.getenv("SSL_CERT_FILE"))
    parser.add_argument("--count", type=int, choices=(1, 10), required=True)
    args = parser.parse_args()
    if not asyncio.run(run(args.url.rstrip("/"), args.ca or True, args.count)):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
