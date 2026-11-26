import { readFile, writeFile } from "node:fs/promises";
import openapiTS, { astToString } from "openapi-typescript";

const input = new URL("openapi.v1.json", import.meta.url);
const output = new URL("openapi.v1.d.ts", import.meta.url);
const spec = JSON.parse(await readFile(input, "utf8"));
const eventStream = spec.paths["/v1/conversations/{id}/events"]?.get;
const eventSchema = eventStream?.responses["200"]?.content["text/event-stream"]?.["x-event-schema"];
if (!eventSchema) {
    throw new Error("Missing SSE event schema on the conversation events route");
}

spec.components ??= {};
spec.components.schemas ??= {};
spec.components.schemas.EventView = eventSchema;

const declarations = await openapiTS(spec, { alphabetize: true });
const content = astToString(declarations) + '\nexport type EventView = components["schemas"]["EventView"];\n';
if (process.argv.includes("--check")) {
    const saved = await readFile(output, "utf8");
    if (content !== saved) {
        throw new Error("Run npm run generate to refresh openapi.v1.d.ts");
    }
} else {
    await writeFile(output, content, "utf8");
}
