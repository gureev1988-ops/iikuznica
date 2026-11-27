"""Administrative document inventory commands."""

from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException

from .connections import ConnectionCheck, ConnectionPaused, ConnectionStatus
from .sync import DocumentSync, SyncResult


@dataclass(frozen=True)
class SyncAccepted:
    run_id: UUID


@dataclass(frozen=True)
class ConnectionList:
    connections: list[ConnectionStatus]


def make_router(service: DocumentSync, require_admin: Callable) -> APIRouter:
    router = APIRouter(prefix='/v1/ops/documents', tags=['documents'], dependencies=[Depends(require_admin)])

    def for_enabled_source(operation, source_key):
        try:
            return operation(source_key)
        except KeyError:
            raise HTTPException(404, 'Unknown document source') from None
        except ConnectionPaused:
            raise HTTPException(409, 'Document connection is paused') from None

    @router.get('/connections')
    def connections() -> ConnectionList:
        return ConnectionList(service.connections.statuses())

    @router.post('/{source_key}/check')
    def check_connection(source_key: str) -> ConnectionCheck:
        return for_enabled_source(service.connections.check, source_key)

    @router.post('/{source_key}/sync', status_code=202)
    def request_sync(source_key: str) -> SyncAccepted:
        return SyncAccepted(for_enabled_source(service.request_sync, source_key))

    @router.get('/sync/{run_id}')
    def sync_result(run_id: UUID) -> SyncResult:
        result = service.result(run_id)
        if result is None:
            raise HTTPException(404, 'Unknown document synchronization run')
        return result

    return router
