from dataclasses import replace

from backend.db import connect
from backend.documents import DocumentSync, PageSnapshot, get_fragment, list_fragments
from backend.documents.links import related_document_url


def test_resolve_link_on_exact_historical_fragment(dsn):
    title = 'Положение об оценке'
    body = f"""
        <h2 id="rules">Основание</h2>
        <p>По заявкам см. <a href="https://docs.example/one?a=1&amp;b=2"><b>Положение</b> об оценке</a>.</p>
        <p>Без согласования применяется {title}.</p>
        <h2 id="rules">Архив</h2>
        <p>Прежде использовали <a href="https://docs.example/older">{title}</a>.</p>
    """
    document = PageSnapshot('source', 1, 'Правила', body)
    newer = replace(document, version=2, storage_html=body.replace('/one?', '/two?'))
    documents = DocumentSync(dsn, {})
    documents.import_batch('fixture', (document,))
    with connect(dsn) as conn:
        old = next(f for f in list_fragments(conn, 'alice') if f.anchor == 'rules')
    documents.import_batch('fixture', (newer,))
    with connect(dsn) as conn:
        assert get_fragment(conn, old.revision_id, old.fragment_id, 'alice') == old
        assert related_document_url(conn, old, 'По заявкам см. ' + title, title) == 'https://docs.example/one?a=1&b=2'
        assert related_document_url(conn, old, 'Без согласования применяется ' + title, title) is None
        assert related_document_url(conn, old, title, title) is None  # Same words, only one occurrence is linked.
        current = next(f for f in list_fragments(conn, 'alice') if f.anchor == 'rules')
        assert related_document_url(conn, current, 'По заявкам см. ' + title, title) == 'https://docs.example/two?a=1&b=2'
        assert related_document_url(conn, replace(current, anchor='rules-2'), title, title) is None


def test_only_complete_associated_https_address_in_quoted_prose(dsn):
    title = 'Методика расчёта'
    url = 'https://docs.example/a-b/методы_(расчёта)?v=2#one'
    line = f'См. «{title}» ({url}).'
    source = PageSnapshot('procedures', 1, 'Процедуры', f"""
        <h2>Порядок</h2><p>{line}</p>
        <p>Памятка: https://docs.example/one или https://docs.example/two</p>
        <p>Чеклист по ссылке https://docs.example/check</p>
        <p>Отдельное название. Адрес другого документа https://docs.example/extra</p>
        <p><a href="http://docs.example/insecure">Неподтверждённая методика</a></p>
        <p><a href="https://user@docs.example/path">Методика доступа</a></p>
        <p><a href="https://docs.example/a">Свод правил</a> или <a href="https://docs.example/b">Свод правил</a></p>
    """)
    DocumentSync(dsn, {}).import_batch('fixture', (source,))
    with connect(dsn) as conn:
        fragment, = list_fragments(conn, 'alice')
        assert related_document_url(conn, fragment, line, title) == url
        assert related_document_url(conn, fragment, line.split('?')[0], title) is None
        assert related_document_url(conn, fragment, 'Чеклист по ссылке https://docs.example/check', 'Чеклист') == 'https://docs.example/check'
        assert related_document_url(conn, fragment, fragment.text, 'Памятка') is None
        assert related_document_url(conn, fragment, fragment.text, 'Отдельное название') is None
        assert related_document_url(conn, fragment, fragment.text, 'Неподтверждённая методика') is None
        assert related_document_url(conn, fragment, fragment.text, 'Методика доступа') is None
        assert related_document_url(conn, fragment, 'Свод правил', 'Свод правил') is None
