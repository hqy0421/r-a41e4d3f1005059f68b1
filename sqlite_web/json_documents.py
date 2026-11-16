"""Reversible relational storage for nested JSON documents.

The ordinary JSON importer expects a flat list of records.  This module
implements an opt-in representation for arbitrary JSON: each document becomes
a row in the table chosen by the user and its structure is stored as a tree of
ordinary rows in a companion ``<table>_json_nodes`` table.  A small
``<table>_json_meta`` table records the top-level shape and the name of the
document key column.

Because the nodes are plain rows they can be browsed and edited with the
Content/Insert/Update tabs or from the SQL console.  JSON is rebuilt from the
current rows every time it is exported -- nothing is cached -- so edits made
after the import are always reflected in the export.

Representation
==============

``<table>``
    One row per document.  The table must have a single-column primary key
    with INTEGER affinity; its values order the documents.  Tables created by
    sqlite-web (``id INTEGER PRIMARY KEY``) already satisfy this.

``<table>_json_nodes``
    ``id``           node primary key.
    ``document_id``  owning document (references ``<table>``).
    ``parent_id``    parent node, NULL for a document's single root node.
    ``member_name``  object member name; NULL for array elements and roots.
    ``ordinal``      0-based array position; NULL for object members and roots.
    ``kind``         one of object, array, string, number, boolean, null.
    ``value``        scalar text: the exact JSON literal for numbers, 'true' or
                     'false' for booleans, the string contents for strings and
                     NULL for null / container nodes.

The encoding keeps every distinction the JSON data model requires:

* a missing member has no row at all;
* an explicit null is a row with ``kind = 'null'``;
* ``[]`` and ``{}`` are container rows without children;
* booleans, strings and numbers are distinguished by ``kind``, so ``true``,
  ``"true"`` and ``1`` never collide;
* numbers are stored as the literal text from the input file, so large
  integers and high-precision decimals are never rounded.

Export refuses, with an explicit error, to produce output from a graph that
cannot be represented faithfully: dangling parent nodes, cycles, duplicate
member names, invalid ordinals and scalar nodes that grew children are all
rejected rather than silently dropped.
"""

import json
import re


FORMAT = 'sqlite-web.json-documents'
FORMAT_VERSION = '1'
NODE_KINDS = ('object', 'array', 'string', 'number', 'boolean', 'null')
ROOT_MODES = ('list', 'single')
DEFAULT_INDENT = 2

_NODE_COLUMNS = ('id', 'document_id', 'parent_id', 'member_name', 'ordinal',
                 'kind', 'value')

_NUMBER_RE = re.compile(
    r'-?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][+-]?[0-9]+)?')


class JsonDocumentError(Exception):
    """A structured JSON import/export cannot proceed without data loss."""


def nodes_table(table):
    return '%s_json_nodes' % table


def meta_table(table):
    return '%s_json_meta' % table


def _quote(name):
    return '"%s"' % name.replace('"', '""')


def _columns(dataset, table):
    return dataset._database.get_columns(table)


def _tables(dataset):
    if hasattr(dataset, 'cached_tables'):
        return dataset.cached_tables()
    return dataset.tables


def _is_view(dataset, table):
    cursor = dataset.query(
        'SELECT 1 FROM sqlite_master WHERE type = ? AND name = ?',
        ('view', table))
    return cursor.fetchone() is not None


def _read_meta(dataset, table):
    """Return the metadata mapping, or None if there is no metadata table."""
    mtable = meta_table(table)
    if mtable not in _tables(dataset):
        return None
    try:
        cursor = dataset.query(
            'SELECT "key", "value" FROM %s' % _quote(mtable))
        return dict(cursor.fetchall())
    except Exception as exc:
        raise JsonDocumentError(
            'Could not read structured JSON metadata from "%s": %s' % (
                mtable, exc))


def _write_meta(dataset, table, values):
    mtable = meta_table(table)
    for key, value in values.items():
        dataset.query(
            'INSERT OR REPLACE INTO %s ("key", "value") VALUES (?, ?)' % (
                _quote(mtable)), (key, value))


def is_document_table(dataset, table):
    """True when ``table`` was set up by :func:`import_documents`."""
    if table not in _tables(dataset):
        return False
    try:
        meta = _read_meta(dataset, table)
    except Exception:
        return False
    return bool(meta) and meta.get('format') == FORMAT


def _document_key(dataset, table):
    columns = _columns(dataset, table)
    keys = [column for column in columns if column.primary_key]
    if len(keys) != 1:
        raise JsonDocumentError(
            'Table "%s" must have a single-column primary key to store JSON '
            'documents.' % table)
    column = keys[0]
    data_type = (column.data_type or '').upper()
    if 'INT' not in data_type:
        raise JsonDocumentError(
            'The primary key of "%s" must have INTEGER affinity to key JSON '
            'documents (found %s).' % (
                table, column.data_type or 'unknown type'))
    return column.name


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise JsonDocumentError(
                'Duplicate object member name in input: %r.' % (key,))
        result[key] = value
    return result


class _Number(str):
    """Number literal from the input, kept as text to avoid any rounding."""
    __slots__ = ()


def _reject_constant(token):
    raise JsonDocumentError('Invalid JSON number: %s.' % token)


def _parse(stream):
    try:
        return json.load(
            stream,
            parse_float=_Number,
            parse_int=_Number,
            parse_constant=_reject_constant,
            object_pairs_hook=_unique_object)
    except JsonDocumentError:
        raise
    except RecursionError:
        raise JsonDocumentError('JSON document is nested too deeply.')
    except (ValueError, UnicodeDecodeError) as exc:
        raise JsonDocumentError('Invalid JSON: %s' % exc)


def _check_text(value):
    # A lone surrogate passed through \uXXXX escapes in the input cannot be
    # stored in SQLite as UTF-8. Reject it during the import rather than
    # midway through the insert.
    try:
        value.encode('utf8')
    except UnicodeEncodeError:
        raise JsonDocumentError(
            'String value %r contains a lone surrogate and cannot be '
            'stored.' % (value,))


def _walk(value, document_id, parent_id, member_name, ordinal, rows, next_id):
    """Flatten one JSON value into node rows, returning the next node id."""
    if isinstance(value, dict):
        kind, scalar, children = 'object', None, value.items()
    elif isinstance(value, list):
        kind, scalar, children = 'array', None, enumerate(value)
    elif isinstance(value, _Number):
        kind, scalar, children = 'number', str(value), ()
    elif value is None:
        kind, scalar, children = 'null', None, ()
    elif value is True:
        kind, scalar, children = 'boolean', 'true', ()
    elif value is False:
        kind, scalar, children = 'boolean', 'false', ()
    elif isinstance(value, str):
        _check_text(value)
        kind, scalar, children = 'string', value, ()
    else:
        # parse_float/parse_int hand numbers over as their literal text.
        kind, scalar, children = 'number', str(value), ()

    if member_name is not None:
        _check_text(member_name)

    node_id = next_id
    next_id += 1
    rows.append((node_id, document_id, parent_id, member_name, ordinal,
                 kind, scalar))

    if kind == 'object':
        for name, child in children:
            next_id = _walk(child, document_id, node_id, name, None, rows,
                            next_id)
    elif kind == 'array':
        for index, child in children:
            next_id = _walk(child, document_id, node_id, None, index, rows,
                            next_id)
    return next_id


def _validate_aux_tables(dataset, table):
    ntable = nodes_table(table)
    if ntable not in _tables(dataset):
        raise JsonDocumentError(
            'Structured JSON table "%s" is missing its node table "%s".' % (
                table, ntable))
    present = set(column.name for column in _columns(dataset, ntable))
    missing = [name for name in _NODE_COLUMNS if name not in present]
    if missing:
        raise JsonDocumentError(
            'Node table "%s" is missing required column(s): %s.' % (
                ntable, ', '.join(missing)))


def _ensure_cleanup_trigger(dataset, table, key_column):
    # Deleting a document must take its structure with it.  The foreign key
    # only cascades when the connection enables the foreign_keys pragma, so a
    # trigger keeps the tables consistent either way.
    ntable = nodes_table(table)
    dataset.query(
        'CREATE TRIGGER IF NOT EXISTS %s AFTER DELETE ON %s BEGIN '
        'DELETE FROM %s WHERE "document_id" = OLD.%s; END' % (
            _quote(ntable + '_cleanup'), _quote(table), _quote(ntable),
            _quote(key_column)))


def _create_aux_tables(dataset, table, key_column, root_mode):
    ntable = nodes_table(table)
    mtable = meta_table(table)
    dataset.query(
        'CREATE TABLE %s ('
        '"id" INTEGER PRIMARY KEY, '
        '"document_id" INTEGER NOT NULL REFERENCES %s(%s) ON DELETE CASCADE, '
        '"parent_id" INTEGER REFERENCES %s("id") ON DELETE CASCADE, '
        '"member_name" TEXT, '
        '"ordinal" INTEGER, '
        '"kind" TEXT NOT NULL CHECK ("kind" IN (%s)), '
        '"value" TEXT)' % (
            _quote(ntable),
            _quote(table),
            _quote(key_column),
            _quote(ntable),
            ', '.join("'%s'" % kind for kind in NODE_KINDS)))
    dataset.query(
        'CREATE INDEX IF NOT EXISTS %s ON %s ("document_id")' % (
            _quote(ntable + '_document_id_idx'), _quote(ntable)))
    dataset.query(
        'CREATE INDEX IF NOT EXISTS %s ON %s ("parent_id")' % (
            _quote(ntable + '_parent_id_idx'), _quote(ntable)))
    dataset.query(
        'CREATE TABLE %s ("key" TEXT PRIMARY KEY, "value" TEXT NOT NULL)' % (
            _quote(mtable)))
    _ensure_cleanup_trigger(dataset, table, key_column)
    _write_meta(dataset, table, {
        'format': FORMAT,
        'version': FORMAT_VERSION,
        'document_key': key_column,
        'root_mode': root_mode,
    })


def import_documents(dataset, table, stream):
    """Store the JSON read from ``stream`` as documents in ``table``.

    Returns ``(document_count, node_count)``.  The whole import runs in a
    single transaction: any failure rolls back the inserted rows *and* the
    helper tables created for the import, leaving previously existing tables
    untouched.
    """
    if getattr(dataset, 'is_readonly', False):
        raise JsonDocumentError('The database is read-only.')
    if table not in _tables(dataset):
        raise JsonDocumentError('Table "%s" does not exist.' % table)
    if _is_view(dataset, table):
        raise JsonDocumentError(
            'Table "%s" is a view and cannot store JSON documents.' % table)

    key_column = _document_key(dataset, table)

    # Parse before touching the database so malformed input leaves it alone.
    payload = _parse(stream)
    if isinstance(payload, list):
        root_mode = 'list'
        documents = payload
    else:
        root_mode = 'single'
        documents = [payload]

    with dataset.transaction():
        meta = _read_meta(dataset, table)
        if meta is None:
            if nodes_table(table) in _tables(dataset):
                raise JsonDocumentError(
                    'Table "%s" already has a "%s" table but no structured '
                    'JSON metadata; refusing to overwrite it.' % (
                        table, nodes_table(table)))
            existing = dataset.query(
                'SELECT COUNT(*) FROM %s' % _quote(table)).fetchone()[0]
            if existing:
                raise JsonDocumentError(
                    'Table "%s" already contains %s row(s).  Structured JSON '
                    'documents require an empty table (or a table previously '
                    'imported with this feature).' % (table, existing))
            _create_aux_tables(dataset, table, key_column, root_mode)
        else:
            if meta.get('format') != FORMAT:
                raise JsonDocumentError(
                    'Metadata table "%s" belongs to a different format; '
                    'refusing to overwrite it.' % meta_table(table))
            if meta.get('version') != FORMAT_VERSION:
                raise JsonDocumentError(
                    'Metadata table "%s" uses an unsupported version.' % (
                        meta_table(table),))
            if meta.get('document_key') != key_column:
                raise JsonDocumentError(
                    'Metadata expects document key "%s" but "%s" has "%s".' % (
                        meta.get('document_key'), table, key_column))
            _validate_aux_tables(dataset, table)
            _ensure_cleanup_trigger(dataset, table, key_column)
            if meta.get('root_mode') != root_mode:
                existing = dataset.query(
                    'SELECT COUNT(*) FROM %s' % _quote(table)).fetchone()[0]
                if existing:
                    raise JsonDocumentError(
                        'Table "%s" already stores %s document(s) with root '
                        'mode "%s"; cannot append a file with root mode '
                        '"%s".' % (table, existing, meta.get('root_mode'),
                                   root_mode))
                _write_meta(dataset, table, {'root_mode': root_mode})
            elif root_mode == 'single':
                existing = dataset.query(
                    'SELECT COUNT(*) FROM %s' % _quote(table)).fetchone()[0]
                if existing:
                    raise JsonDocumentError(
                        'Table "%s" already stores a single JSON document; '
                        'the input file holds one document too.  Remove the '
                        'existing document first, or import a JSON list.' % (
                            table,))

        max_document = dataset.query(
            'SELECT MAX(%s) FROM %s' % (
                _quote(key_column), _quote(table))).fetchone()[0]
        if max_document is None:
            max_document = 0
        elif not isinstance(max_document, int) or isinstance(max_document,
                                                             bool):
            raise JsonDocumentError(
                'Document key column "%s" contains a non-integer value: '
                '%r.' % (key_column, max_document))
        else:
            max_document = max(0, max_document)

        max_node = dataset.query(
            'SELECT MAX("id") FROM %s' % _quote(nodes_table(table))
        ).fetchone()[0]
        next_node = (max_node if isinstance(max_node, int) else 0) + 1
        next_document = max_document + 1

        rows = []
        for document in documents:
            document_id = next_document
            next_document += 1
            dataset.query(
                'INSERT INTO %s (%s) VALUES (?)' % (
                    _quote(table), _quote(key_column)), (document_id,))
            try:
                next_node = _walk(
                    document, document_id, None, None, None, rows, next_node)
            except RecursionError:
                raise JsonDocumentError(
                    'JSON document is nested too deeply.')

        if rows:
            conn = dataset._database.connection()
            try:
                conn.executemany(
                    'INSERT INTO %s ("id", "document_id", "parent_id", '
                    '"member_name", "ordinal", "kind", "value") '
                    'VALUES (?, ?, ?, ?, ?, ?, ?)' % (
                        _quote(nodes_table(table))),
                    rows)
            except Exception as exc:
                raise JsonDocumentError(
                    'Could not store JSON structure: %s' % exc)

    dataset.update_cache()
    return len(documents), len(rows)


class _Node(object):
    __slots__ = ('id', 'document_id', 'parent_id', 'member_name', 'ordinal',
                 'kind', 'value', 'children')

    def __init__(self, row):
        (self.id, self.document_id, self.parent_id, self.member_name,
         self.ordinal, self.kind, self.value) = row
        self.children = []


class _Object(object):
    __slots__ = ('pairs',)

    def __init__(self, pairs):
        self.pairs = pairs


class _Array(object):
    __slots__ = ('items',)

    def __init__(self, items):
        self.items = items


class _Raw(object):
    """An already-encoded JSON token (a number or a boolean literal)."""
    __slots__ = ('text',)

    def __init__(self, text):
        self.text = text


def _load_nodes(dataset, table):
    """Read and validate every node; return (documents, nodes, roots, meta)."""
    meta = _read_meta(dataset, table)
    if not meta:
        raise JsonDocumentError(
            'Table "%s" is not a structured JSON document table.' % table)
    if meta.get('format') != FORMAT:
        raise JsonDocumentError(
            'Table "%s" is not a structured JSON document table.' % table)
    if meta.get('version') != FORMAT_VERSION:
        raise JsonDocumentError(
            'Structured JSON metadata for "%s" uses an unsupported '
            'version.' % table)
    root_mode = meta.get('root_mode')
    if root_mode not in ROOT_MODES:
        raise JsonDocumentError(
            'Structured JSON metadata for "%s" has an invalid root mode '
            '%r.' % (table, root_mode))

    columns = set(column.name for column in _columns(dataset, table))
    key_column = meta.get('document_key')
    if not key_column or key_column not in columns:
        raise JsonDocumentError(
            'Structured JSON metadata for "%s" refers to a missing document '
            'key column.' % table)

    _validate_aux_tables(dataset, table)
    ntable = nodes_table(table)

    documents = []
    for (document_id,) in dataset.query(
            'SELECT %s FROM %s ORDER BY %s' % (
                _quote(key_column), _quote(table), _quote(key_column))):
        if isinstance(document_id, bool) or not isinstance(document_id, int):
            raise JsonDocumentError(
                'Document key %r is not an integer.' % (document_id,))
        documents.append(document_id)
    document_ids = set(documents)

    nodes = {}
    for row in dataset.query(
            'SELECT "id", "document_id", "parent_id", "member_name", '
            '"ordinal", "kind", "value" FROM %s ORDER BY "id"' % (
                _quote(ntable))):
        node = _Node(row)
        if not isinstance(node.id, int) or isinstance(node.id, bool):
            raise JsonDocumentError(
                'Node id %r is not an integer.' % (node.id,))
        if node.id in nodes:
            raise JsonDocumentError('Duplicate node id %r.' % (node.id,))
        if node.kind not in NODE_KINDS:
            raise JsonDocumentError(
                'Node %s has unsupported kind %r.' % (node.id, node.kind))
        if node.document_id not in document_ids:
            raise JsonDocumentError(
                'Node %s references missing document %r.' % (
                    node.id, node.document_id))
        nodes[node.id] = node

    roots = {}
    for node in nodes.values():
        if node.parent_id is None:
            roots.setdefault(node.document_id, []).append(node)
        else:
            parent = nodes.get(node.parent_id)
            if parent is None:
                raise JsonDocumentError(
                    'Node %s has a dangling parent_id %r.' % (
                        node.id, node.parent_id))
            if parent.document_id != node.document_id:
                raise JsonDocumentError(
                    'Node %s and its parent %s belong to different '
                    'documents.' % (node.id, parent.id))
            parent.children.append(node)

    # Cycle detection using a three-state traversal of the parent chain.
    state = {}
    for start in nodes.values():
        chain = []
        current = start
        while current is not None and state.get(current.id) is None:
            state[current.id] = 1  # visiting
            chain.append(current.id)
            current = nodes.get(current.parent_id)
        if current is not None and state.get(current.id) == 1:
            raise JsonDocumentError(
                'Cycle detected in the node graph at node %s.' % current.id)
        for node_id in chain:
            state[node_id] = 2  # done

    if root_mode == 'single':
        if not documents:
            raise JsonDocumentError(
                'Table "%s" contains no document to export.' % table)
        if len(documents) > 1:
            raise JsonDocumentError(
                'Table "%s" has root mode "single" but contains %s '
                'documents.' % (table, len(documents)))

    for document_id in documents:
        document_roots = roots.get(document_id, [])
        if not document_roots:
            raise JsonDocumentError(
                'Document %r has no root node.' % (document_id,))
        if len(document_roots) > 1:
            raise JsonDocumentError(
                'Document %r has %s root nodes; exactly one is required.' % (
                    document_id, len(document_roots)))
        root = document_roots[0]
        if root.member_name is not None or root.ordinal is not None:
            raise JsonDocumentError(
                'Root node %s of document %r must not have a member name or '
                'ordinal.' % (root.id, document_id))

    for node in nodes.values():
        if node.kind == 'object':
            if node.value is not None:
                raise JsonDocumentError(
                    'Object node %s must not have a value.' % (node.id,))
            names = set()
            node.children.sort(key=lambda child: child.id)
            for child in node.children:
                if child.member_name is None:
                    raise JsonDocumentError(
                        'Child %s of object node %s has no member name.' % (
                            child.id, node.id))
                if child.ordinal is not None:
                    raise JsonDocumentError(
                        'Child %s of object node %s must not have an '
                        'ordinal.' % (child.id, node.id))
                if child.member_name in names:
                    raise JsonDocumentError(
                        'Duplicate member name %r in object node %s.' % (
                            child.member_name, node.id))
                names.add(child.member_name)
        elif node.kind == 'array':
            if node.value is not None:
                raise JsonDocumentError(
                    'Array node %s must not have a value.' % (node.id,))
            ordinals = []
            for child in node.children:
                if child.member_name is not None:
                    raise JsonDocumentError(
                        'Child %s of array node %s must not have a member '
                        'name.' % (child.id, node.id))
                if isinstance(child.ordinal, bool) or \
                   not isinstance(child.ordinal, int):
                    raise JsonDocumentError(
                        'Child %s of array node %s has an invalid ordinal '
                        '%r.' % (child.id, node.id, child.ordinal))
                ordinals.append(child.ordinal)
            if sorted(ordinals) != list(range(len(ordinals))):
                raise JsonDocumentError(
                    'Array node %s has invalid ordinals %r; expected 0..%s '
                    'exactly once.' % (
                        node.id, ordinals, len(ordinals) - 1))
            node.children.sort(key=lambda child: child.ordinal)
        else:
            if node.children:
                raise JsonDocumentError(
                    'Scalar node %s (kind %r) must not have children.' % (
                        node.id, node.kind))
            if node.kind == 'null':
                if node.value is not None:
                    raise JsonDocumentError(
                        'Null node %s must not have a value.' % (node.id,))
                continue
            if not isinstance(node.value, str):
                raise JsonDocumentError(
                    'Node %s (kind %r) must have a text value, found %r.' % (
                        node.id, node.kind, node.value))
            if node.kind == 'boolean':
                if node.value not in ('true', 'false'):
                    raise JsonDocumentError(
                        'Boolean node %s has an invalid value %r.' % (
                            node.id, node.value))
            elif node.kind == 'number':
                if not _NUMBER_RE.fullmatch(node.value):
                    raise JsonDocumentError(
                        'Number node %s has an invalid JSON number %r.' % (
                            node.id, node.value))

    return documents, nodes, roots, root_mode


def _render(value, indent, level=0):
    if isinstance(value, _Raw):
        return value.text
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, _Object):
        if not value.pairs:
            return '{}'
        if indent is None:
            return '{%s}' % ', '.join(
                '%s: %s' % (json.dumps(name, ensure_ascii=False),
                            _render(child, indent))
                for name, child in value.pairs)
        pad = ' ' * (indent * (level + 1))
        body = ',\n'.join(
            '%s%s: %s' % (pad, json.dumps(name, ensure_ascii=False),
                          _render(child, indent, level + 1))
            for name, child in value.pairs)
        return '{\n%s\n%s}' % (body, ' ' * (indent * level))
    if isinstance(value, _Array):
        if not value.items:
            return '[]'
        if indent is None:
            return '[%s]' % ', '.join(
                _render(child, indent) for child in value.items)
        pad = ' ' * (indent * (level + 1))
        body = ',\n'.join(
            '%s%s' % (pad, _render(child, indent, level + 1))
            for child in value.items)
        return '[\n%s\n%s]' % (body, ' ' * (indent * level))
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if value is None:
        return 'null'
    raise JsonDocumentError('Cannot serialize value %r.' % (value,))


def export_documents(dataset, table, indent=DEFAULT_INDENT):
    """Rebuild the JSON document(s) stored in ``table`` from current rows.

    Raises :class:`JsonDocumentError` on any structure that cannot be
    represented faithfully, instead of dropping rows.
    """
    if table not in _tables(dataset):
        raise JsonDocumentError('Table "%s" does not exist.' % table)
    documents, nodes, roots, root_mode = _load_nodes(dataset, table)

    def build(node):
        if node.kind == 'object':
            return _Object([(child.member_name, build(child))
                            for child in node.children])
        if node.kind == 'array':
            return _Array([build(child) for child in node.children])
        if node.kind == 'null':
            return None
        if node.kind in ('boolean', 'number'):
            return _Raw(node.value)
        return node.value

    try:
        if root_mode == 'list':
            payload = _Array([build(roots[document_id][0])
                              for document_id in documents])
        else:
            payload = build(roots[documents[0]][0])
        return _render(payload, indent)
    except RecursionError:
        raise JsonDocumentError(
            'Document nesting is too deep to export.')
