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

## AnswerEngine v1

The application supplies a read-only source snapshot and a request-scoped model attempt recorder on `AnswerRequest`. An engine that calls a model provider uses `request.attempts.call(invoke, role=..., model=..., route=...)` around each physical request. Put the wrapper inside retries and use it for judge calls as well. An optional `trace_id` can correlate related calls.

Return a `ModelAttemptResult` from the callback to record reported token counts, cost, actual model, and actual route. Raise `ModelAttemptFailure` to preserve the same metadata on a technical failure. An unreported cost stays unknown. The synthetic engines work entirely in memory and generate zero physical model attempts.
