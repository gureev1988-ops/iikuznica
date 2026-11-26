# Backend API v1

Use `openapi.v1.json` for the HTTP operations and `openapi.v1.d.ts` for the corresponding TypeScript request and response types. The `EventView` type describes the JSON in the SSE `data` field. Resume a stream with the last received event ID. After an HTTP 410 or an SSE `reset` event, read the conversation snapshot and resume at its cursor. The reset event carries `EventCursorExpired` data.

Regenerate from the repository root after registering the API routers.

```sh
python -m backend.contracts.export
npm ci --prefix contracts
npm run generate --prefix contracts
```

Verify that the artifacts match the application and that the declarations compile.

```sh
python -m backend.contracts.export --check
npm run check --prefix contracts
```
