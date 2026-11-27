"""Controlled HTTP admission bursts followed by worker recovery on PostgreSQL."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from math import ceil
from uuid import uuid4

import httpx
import pytest

from backend.app import Backend
from backend.contracts.fixtures import seed_fixture_sources
from backend.db import connect
from .conftest import credentials


@pytest.mark.parametrize("count", [10, 20, 200])
def test_burst_with_paused_then_restarted_workers(deployment, dsn, count, live_http):
    settings, _, _ = deployment
    with connect(dsn) as conn:
        seed_fixture_sources(conn)
    backend = Backend(settings)
    try:
        with httpx.Client(base_url=live_http, trust_env=False) as client:
            async def submit_all():
                limits = httpx.Limits(max_connections=count, max_keepalive_connections=16)
                async with httpx.AsyncClient(base_url=live_http, limits=limits, timeout=30, trust_env=False) as callers:
                    return await asyncio.gather(*(callers.post(
                        f"/v1/conversations/{uuid4()}/messages", headers=credentials("first"),
                        json={"client_message_id": f"burst-{i}", "text": "Сколько дней рассматривают заявку?"}
                    ) for i in range(count)))

            # Pause workers through admission so all requests compete for bounded capacity.
            responses = asyncio.run(submit_all())
            accepted = [response.json()["turn_id"] for response in responses if response.status_code == 202]
            rejected = [response for response in responses if response.status_code != 202]
            assert len(accepted) == min(count, settings.max_pending)
            assert all(response.status_code == 503 and response.json()["detail"]["code"] == "interactive_queue_full" for response in rejected), [r.status_code for r in rejected]

            def drain():
                while backend.run_job("interactive") is not None:
                    pass

            with ThreadPoolExecutor(max_workers=4) as pool:
                futures = [pool.submit(drain) for _ in range(4)]
                for future in futures:
                    future.result()
            for turn_id in accepted:
                view = client.get("/v1/turns/" + turn_id, headers=credentials("first")).json()
                assert view["status"] == "completed" and view["result"]["sources"]
        with connect(dsn) as conn:
            measurements = conn.execute("""
                SELECT min(extract(epoch FROM e.created_at - t.created_at))
                          FILTER (WHERE e.kind='turn.completed') AS useful_s,
                       extract(epoch FROM t.updated_at - t.created_at) AS complete_s
                FROM turns t JOIN events e ON e.turn_id=t.id
                GROUP BY t.id ORDER BY t.id""").fetchall()
            states = conn.execute("SELECT state,count(*) AS n FROM jobs GROUP BY state").fetchall()
        assert states == [{"state": "succeeded", "n": len(accepted)}]
        assert len(measurements) == len(accepted)
        p95 = {key: round(float(sorted(row[key] for row in measurements)[ceil(.95 * len(accepted)) - 1]), 3)
               for key in ("useful_s", "complete_s")}
        print("BURST", json.dumps({"submitted": count, "accepted": len(accepted), "rejected": len(rejected),
                                   "completed": len(accepted), "http_clients": count, "workers": 4, "p95": p95}))
        if count == 10:
            assert p95["useful_s"] <= 5 and p95["complete_s"] <= 15
    finally:
        backend.close()
