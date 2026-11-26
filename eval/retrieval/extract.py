"""Extract numbered clauses, including continuation paragraphs and table rows."""
import json
from pathlib import Path
import re
from xml.etree import ElementTree as ET
from zipfile import ZipFile

CORPUS = Path(__file__).resolve().parents[2] / "corpus"
TITLES = {
    "DB": "Проектная деятельность ДБРЦТ",
    "IB": "Проектная деятельность Дирекции ИБ",
    "PRE": "Пресейловая деятельность Дирекции ИБ",
    "TECH": "Организация проектной деятельности Тех дирекции",
}
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
NUMBERED = re.compile(r"^(\d+(?:\.\d+)*)\.\s+")


def paragraphs(body):
    for block in body:
        if block.tag == W + "tbl":
            for row in block.findall(W + "tr"):
                cells = [" ".join(t.text or "" for t in cell.iter(W + "t")).strip()
                         for cell in row.findall(W + "tc")]
                yield " | ".join(cells)
        elif block.tag == W + "p":
            yield "".join(t.text or "" for t in block.iter(W + "t")).strip()


def clauses(doc, title):
    with ZipFile(CORPUS / (title + ".docx")) as archive:
        body = ET.fromstring(archive.read("word/document.xml")).find(W + "body")
    section = ""
    current = None
    seen = set()
    for line in paragraphs(body):
        if not line:
            continue
        match = NUMBERED.match(line)
        if not match:
            if current:
                current["text"] += "\n" + line
            continue
        if current:
            yield current
        ref = match.group(1)
        if ref in seen:
            raise ValueError(f"Duplicate clause {doc} {ref}")
        seen.add(ref)
        if "." not in ref:
            section = line
        current = dict(doc=doc, title=title, section=section, ref=ref, text=line)
    if current:
        yield current


if __name__ == "__main__":
    print(json.dumps([c for doc, title in TITLES.items() for c in clauses(doc, title)], ensure_ascii=False))
