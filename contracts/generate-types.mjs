import { readFile, writeFile } from "node:fs/promises";
import openapiTS, { astToString } from "openapi-typescript";

const input = new URL("openapi.v1.json", import.meta.url);
const output = new URL("openapi.v1.d.ts", import.meta.url);
const spec = JSON.parse(await readFile(input, "utf8"));
const eventStream = spec.paths["/v1/conversations/{conversation_id}/events"]?.get;
const sse = eventStream?.responses["200"]?.content?.["text/event-stream"];
if (!sse?.["x-event-schema"] || !sse?.["x-reset-schema"]) {
    throw new Error("Missing SSE data schemas on the conversation events route");
}

spec.components ??= {};
spec.components.schemas ??= {};
spec.components.schemas.EventView = sse["x-event-schema"];
spec.components.schemas.EventCursorExpired = sse["x-reset-schema"];

const declarations = await openapiTS(spec, { alphabetize: true });
const content = astToString(declarations) +
    '\nexport type EventView = components["schemas"]["EventView"];\n' +
    'export type EventCursorExpired = components["schemas"]["EventCursorExpired"];\n';
if (process.argv.includes("--check")) {
    const saved = await readFile(output, "utf8");
    if (content !== saved) {
        throw new Error("Run npm run generate to refresh openapi.v1.d.ts");
    }
} else {
    await writeFile(output, content, "utf8");
}
