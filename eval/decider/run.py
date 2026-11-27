"""Transport for the Decider and answer generator."""
import json
import os
import sys
import time
from pathlib import Path
from urllib import request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from openrouter import complete
if __package__:
    from .policy import ENDPOINT
else:
    from policy import ENDPOINT

def decide(body, *, timeout=150):
    headers = {"Authorization": "Bearer " + os.environ["OPENROUTER_API_KEY"], "Content-Type": "application/json"}
    req = request.Request(ENDPOINT, data=json.dumps(body, ensure_ascii=False).encode(), headers=headers)
    start = time.monotonic()
    with request.urlopen(req, timeout=timeout) as response:
        result = json.load(response)
        return {"response": result, "http": response.status, "usage": result.get("usage") or {},
                "latency_s": round(time.monotonic() - start, 3)}
