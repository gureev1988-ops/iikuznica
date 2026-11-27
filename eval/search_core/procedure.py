"""Source widening for full procedures."""
from collections import Counter

import conversation
import evidence
import procedures

COVER = ("Сотрудник просит маршрут. Выбери утверждения в порядке процесса. Проверь по применимому "
         "разделу: кто отвечает за процесс в целом, что требуется до старта, кто делает и кому "
         "передаёт каждый шаг, какие сроки и контрольные действия остаются после него. "
         "Включи применимые запреты и исключения. Не добавляй шаги из другой дирекции и ветви, "
         "которые условия сотрудника исключают.")


def subqueries(question):
    chunks = conversation.fragments(question)
    if len(chunks) == 1:
        return []
    scopes = procedures.directories(question)
    labels = [label for doc, label in (("DB", "ДБРЦТ"), ("TECH", "Техническая дирекция"), ("IB", "Дирекция ИБ"))
              if doc in scopes]
    if labels == ["Дирекция ИБ"] and "пресейл" in question.lower():
        labels = ["Пресейл ИБ"]
    return [labels[0] + ". " + p.lstrip(" ,") for p in chunks] if len(labels) == 1 else chunks


def expand(sources, seeds, question, search, corpus):
    sections = procedures.section_neighbors(sources, seeds, corpus)
    passages = {evidence.reference(p): p for p in sections}
    for query in subqueries(question):
        results, _ = search(query)
        topics = Counter((doc, corpus[doc, ref]["section"]) for doc, ref in results)
        if topics:
            topic = topics.most_common(1)[0][0]
            extra = [p for k, p in corpus.items() if k not in passages and (k[0], p["section"]) == topic
                     and p["text"] != p["section"]][:8]
            passages.update((evidence.reference(p), p) for p in extra)
    return sections, list(passages.values())
