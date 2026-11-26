"""Administrative document inventory commands."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from .sync import DocumentSync, SyncResult


@dataclass(frozen=True)
class SyncAccepted:
    run_id: UUID


def make_router(service: DocumentSync, require_admin: Callable) -> APIRouter:
    router = APIRouter(prefix='/v1/ops/documents', tags=['documents'], dependencies=[Depends(require_admin)])

    @router.post('/{source_key}/sync', status_code=202)
    def request_sync(source_key: str) -> SyncAccepted:
        try:
            return SyncAccepted(service.request_sync(source_key))
        except KeyError:
            raise HTTPException(404, 'Unknown document source') from None

    @router.get('/sync/{run_id}')
    def sync_result(run_id: UUID) -> SyncResult:
        result = service.result(run_id)
        if result is None:
            raise HTTPException(404, 'Unknown document synchronization run')
        return result

    return router
