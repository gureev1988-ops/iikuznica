"""Resolve a cited cross-document link on the immutable, authorized source revision."""
import re

from backend.contracts.engine import SourceFragment

from .fragments import links_in_fragment
from .http_json import valid_https_url


# Visible prose delimits URLs; validate the full source token before consulting the quotation.
_URL = re.compile(r"""(?<![\w/:])https://[^\s<>"«»“”]+""", re.IGNORECASE)
# A source can put a bare URL next to the title, as in «Положение» (https://…).
_PUNCTUATION = r'''[ \t«»"'“”()\[\]:–—-]*'''
_SEPARATOR = re.compile(_PUNCTUATION + r'(?:(?:по (?:ссылке|адресу)|см\.)' + _PUNCTUATION + r')?', re.I)


def _visible_urls(text):
    for match in _URL.finditer(text):
        url = match[0].rstrip('.,;:!?')
        while url.endswith(')') and url.count(')') > url.count('('):
            url = url[:-1]
        while url.endswith(']') and url.count(']') > url.count('['):
            url = url[:-1]
        if valid_https_url(url):
            yield match.start(), match.start() + len(url), url


def related_document_url(conn, fragment: SourceFragment, quote: str, name: str) -> str | None:
    if fragment.kind != 'document' or not name.strip() or name not in quote or quote not in fragment.text:
        return None
    row = conn.execute("""
        SELECT r.storage_body FROM source_revisions r JOIN source_fragments f ON f.revision_id=r.id
        WHERE r.source_id=%s AND r.id=%s AND f.id=%s AND f.anchor=%s AND f.text=%s
        """, (fragment.document_id, fragment.revision_id, fragment.fragment_id, fragment.anchor, fragment.text)).fetchone()
    if not row:
        return None
    markup = []
    if '<a' in row['storage_body'].lower():
        markup = links_in_fragment(row['storage_body'], fragment.anchor, fragment.text)
    visible = list(_visible_urls(fragment.text))
    resolved = set()
    # A repeated quotation must identify the same link at every occurrence in this fragment.
    for occurrence in re.finditer(re.escape(quote), fragment.text):
        for mention in re.finditer(r'(?<!\w)' + re.escape(name) + r'(?!\w)', quote):
            start, end = occurrence.start() + mention.start(), occurrence.start() + mention.end()
            candidates = set()
            for left, right, href in markup:
                if left <= start and end <= right and valid_https_url(href):
                    candidates.add(href)
            # For prose without a hyperlink, require a single URL on the quoted line.
            line_start = max(occurrence.start(), fragment.text.rfind('\n', 0, start) + 1)
            line_end = fragment.text.find('\n', end, occurrence.end())
            if line_end < 0:
                line_end = occurrence.end()
            addresses = [(left, right, url) for left, right, url in visible if line_start <= left and right <= line_end]
            if len(addresses) == 1:
                left, right, url = addresses[0]
                if end <= left and _SEPARATOR.fullmatch(fragment.text[end:left]):
                    candidates.add(url)
                elif right <= start and _SEPARATOR.fullmatch(fragment.text[right:start]):
                    candidates.add(url)
            if len(candidates) != 1:
                return None
            resolved.update(candidates)
    return resolved.pop() if len(resolved) == 1 else None
