// All document and original reads pass through the server's revision ACL.
export function documentReader({api, onCitation}) {
  const byId = (id) => document.getElementById(id);
  const quoteDialog = byId("dion-source");
  const documentDialog = byId("regulation-dialog");
  const prefix = "#/regulations/";
  let quoteRequest = 0;
  let documentRequest = 0;

  function documentHash(ref) {
    const url = new URLSearchParams();
    if (ref.fragment_id) url.set("fragment", ref.fragment_id);
    else if (ref.anchor) url.set("anchor", ref.anchor);
    return prefix + encodeURIComponent(ref.document_id) + "/" + encodeURIComponent(ref.revision_id)
      + (url.size ? "?" + url.toString() : "");
  }

  async function readDocument() {
    const current = ++documentRequest;
    if (!location.hash.startsWith(prefix)) {
      if (documentDialog.open) documentDialog.close();
      return;
    }
    const [parts, query] = location.hash.slice(prefix.length).split("?");
    const [documentId, revisionId] = parts.split("/");
    const target = new URLSearchParams(query);
    const body = byId("regulation-body");
    const original = byId("regulation-original");
    if (!documentDialog.open) documentDialog.showModal();
    byId("regulation-title").textContent = "Регламент";
    byId("regulation-meta").textContent = "";
    original.removeAttribute("href");
    original.hidden = true;
    body.textContent = "Загрузка редакции…";
    try {
      const snapshot = await api("/v1/browser/documents/" + encodeURIComponent(documentId)
        + "/revisions/" + encodeURIComponent(revisionId));
      if (current !== documentRequest) return;
      byId("regulation-title").textContent = snapshot.title;
      byId("regulation-meta").textContent = (snapshot.historical ? "Историческая редакция " : "Действующая редакция ") + snapshot.version;
      let selected;
      const paragraphs = snapshot.fragments.map((item) => {
        const row = document.createElement("p");
        const heading = document.createElement("strong");
        heading.textContent = [item.anchor, ...(item.heading_path || [])].filter(Boolean).join(" · ");
        row.append(heading, document.createElement("br"), item.text);
        row.className = "regulation-clause";
        if (item.fragment_id === target.get("fragment") || item.anchor && item.anchor === target.get("anchor")) {
          selected = row;
          row.classList.add("regulation-highlight");
        }
        return row;
      });
      body.replaceChildren(...paragraphs);
      body.scrollTop = 0;
      if (selected) selected.scrollIntoView({block: "start"});
      if (snapshot.original_url) {
        original.href = snapshot.original_url;
        original.hidden = false;
      }
    } catch (error) {
      if (current === documentRequest) body.textContent = error.status === 403 || error.status === 404
        ? "Эта редакция недоступна." : "Не удалось загрузить редакцию. Закройте окно и попробуйте ещё раз.";
    }
  }

  function openRegulation(ref) {
    const hash = documentHash(ref);
    if (hash === location.hash) return readDocument();
    location.hash = hash;
  }

  async function openSource(ref, {fromChat = false} = {}) {
    if (ref.claim === "Открыть регламент") return openRegulation(ref);
    const current = ++quoteRequest;
    const text = byId("source-text");
    const actions = byId("source-actions");
    byId("dion-source-title").textContent = "Источник";
    byId("source-meta").textContent = "";
    actions.replaceChildren();
    text.textContent = "Загрузка…";
    if (!quoteDialog.open) quoteDialog.showModal();
    try {
      const source = await api(ref.url);
      if (current !== quoteRequest) return;
      byId("dion-source-title").textContent = source.title;
      const documentSource = source.kind === "document";
      byId("source-meta").textContent = (documentSource ? "Пункт " + source.anchor : "Разъяснение Отдела качества")
        + " · редакция " + (source.version ?? source.revision_id);
      const start = ref.quote ? source.text.indexOf(ref.quote) : -1;
      if (start >= 0) {
        const mark = document.createElement("mark");
        mark.textContent = ref.quote;
        text.replaceChildren(source.text.slice(0, start), mark, source.text.slice(start + ref.quote.length));
      } else text.textContent = source.text;
      if (documentSource) {
        const link = document.createElement("a");
        link.href = documentHash(source);
        link.textContent = "Открыть регламент целиком";
        actions.append(link);
        if (fromChat) onCitation(source.fragment_id);
      }
    } catch (error) {
      if (current === quoteRequest) text.textContent = error.status === 403 || error.status === 404
        ? "Источник недоступен." : "Не удалось загрузить источник.";
    }
  }

  byId("dion-close-source").onclick = () => quoteDialog.close();
  byId("regulation-close").onclick = () => documentDialog.close();
  quoteDialog.addEventListener("close", () => { ++quoteRequest; });
  documentDialog.addEventListener("close", () => {
    ++documentRequest;
    if (location.hash.startsWith(prefix)) history.replaceState(history.state, "", location.pathname + location.search);
  });
  window.addEventListener("hashchange", readDocument);
  return {openRegulation, openSource, readDocument, documentHash};
}
