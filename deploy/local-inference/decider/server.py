"""Serve the native Perplexity decision head from a verified, offline checkpoint."""
import os
from contextlib import asynccontextmanager
from pathlib import Path
from secrets import compare_digest
from typing import Literal

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, JsonValue


MODEL_ID = "perplexity/pplx-decider-flash-v1.3"
CHECKPOINT = Path("/models/decider")


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["choice", "noul"]
    instructions: str | None = None
    criteria: dict[str, str | None] | None = None


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    state: str | dict[str, JsonValue] | list[JsonValue]
    questions: dict[str, Question] = Field(min_length=1)


def load_decider():
    if not (CHECKPOINT / "joint_config.json").is_file():
        raise RuntimeError(f"Missing native decision checkpoint in {CHECKPOINT}")
    # The image adds the pinned checkpoint's source/src to PYTHONPATH.
    from pplx_decider import Decider

    return Decider.from_pretrained(CHECKPOINT, device="cuda", locate=False)


def create_app(loader=load_decider):
    @asynccontextmanager
    async def lifespan(app):
        app.state.decider = loader()
        yield

    app = FastAPI(lifespan=lifespan, openapi_url=None)
    secret = os.environ.get("II_DECIDER_API_KEY", "")

    @app.get("/health")
    def health():
        return {"model": MODEL_ID, "ready": True}

    @app.post("/v1/decisions")
    def decide(request: DecisionRequest, authorization: str = Header(default="")):
        if secret and not compare_digest(authorization.encode(), f"Bearer {secret}".encode()):
            raise HTTPException(401, "Invalid bearer token")
        if request.model != MODEL_ID:
            raise HTTPException(404, "Model not served")
        try:
            native = app.state.decider.decide(request.model_dump(exclude={"model"}, exclude_none=True))
        except ValueError:
            # Native RequestError and LengthError are ValueErrors. Avoid echoing the employee's input.
            raise HTTPException(422, "Invalid decision request or context length") from None
        return {**native, "model": MODEL_ID}

    return app
