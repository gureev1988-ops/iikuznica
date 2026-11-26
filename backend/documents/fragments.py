"""Text and stable section anchors from Confluence storage HTML."""

import re
import unicodedata
from html.parser import HTMLParser

from .ports import Fragment


_HEADINGS = {'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
_BLOCKS = {'p', 'div', 'li', 'tr', 'br', 'section', 'pre', 'blockquote', 'hr'}
_HIDDEN = {'script', 'style', 'ac:parameter'}


def _anchor(text: str) -> str:
    normalized = unicodedata.normalize('NFKC', text).casefold()
    return re.sub(r'[^\w]+', '-', normalized).strip('-') or 'section'


def _text(parts: list[str]) -> str:
    lines = (' '.join(line.split()) for line in ''.join(parts).splitlines())
    return '\n'.join(line for line in lines if line)


class _StorageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.fragments: list[Fragment] = []
        self._used_anchors: set[str] = set()
        self._path: list[tuple[int, str]] = []
        self._section_anchor = 'intro'
        self._parts: list[str] = []
        self._heading: list[str] | None = None
        self._heading_anchor: str | None = None
        self._hidden: list[str] = []

    def _flush(self) -> None:
        content = _text(self._parts)
        if content:
            anchor = self._section_anchor
            suffix = 2
            while anchor in self._used_anchors:
                anchor = f'{self._section_anchor}-{suffix}'
                suffix += 1
            self._used_anchors.add(anchor)
            self.fragments.append(Fragment(anchor, tuple(title for _, title in self._path), content))
        self._parts = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in _HIDDEN:
            self._hidden.append(tag)
        if self._hidden:
            return
        if tag in _HEADINGS:
            self._flush()
            self._heading = []
            self._heading_anchor = dict(attrs).get('id')
        elif tag in _BLOCKS:
            self._parts.append('\n')
        elif tag in {'td', 'th'}:
            self._parts.append(' ')

    def handle_endtag(self, tag: str) -> None:
        if self._hidden:
            if self._hidden[-1] == tag:
                self._hidden.pop()
            return
        if tag in _HEADINGS and self._heading is not None:
            heading = _text(self._heading)
            if heading:
                level = int(tag[1])
                while self._path and self._path[-1][0] >= level:
                    self._path.pop()
                self._path.append((level, heading))
                self._section_anchor = self._heading_anchor or _anchor(heading)
                self._parts.extend((heading, '\n'))
            self._heading = None
        elif tag in _BLOCKS:
            self._parts.append('\n')

    def handle_data(self, data: str) -> None:
        if self._hidden:
            return
        if self._heading is not None:
            self._heading.append(data)
        else:
            self._parts.append(data)

    def unknown_decl(self, data: str) -> None:
        if data.startswith('CDATA['):
            self.handle_data(data.removeprefix('CDATA['))

    def finish(self) -> tuple[Fragment, ...]:
        self.close()
        if self._heading is not None:
            self._parts.extend(self._heading)
        self._flush()
        return tuple(self.fragments)


def extract_fragments(storage_html: str) -> tuple[Fragment, ...]:
    parser = _StorageParser()
    parser.feed(storage_html)
    return parser.finish()
