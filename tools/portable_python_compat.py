"""Mechanical Python/SQLAlchemy modernization, preserving strings and formatting.

Used by the S1 exporter, never as a runtime monkey patch. Only direct datetime
imports and recognized primary-key queries qualify. Loader, refresh and lock
options are retained explicitly; filtered queries are left alone.
"""
from __future__ import annotations

import ast
from functools import lru_cache


def primary_key_query(node):
    """Translate only supported generative Query modifiers to Session.get args."""
    options = []
    flags = {}
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        method = node.func.attr
        if method == 'options' and not node.keywords:
            options[0:0] = [ast.unparse(arg) for arg in node.args]
        elif method == 'populate_existing' and not node.args and not node.keywords:
            flags.setdefault('populate_existing', 'True')
        elif method == 'with_for_update' and not node.args and all(k.arg for k in node.keywords):
            flags.setdefault('with_for_update', ('{' + ', '.join(repr(k.arg) + ': ' + ast.unparse(k.value) for k in node.keywords) + '}') if node.keywords else 'True')
        else:
            return None
        node = node.func.value
    if not (isinstance(node, ast.Attribute) and node.attr == 'query' and isinstance(node.value, ast.Name)):
        return None
    if options:
        flags['options'] = '(' + ', '.join(options) + ',)'
    return node.value.id, flags


@lru_cache(maxsize=1)
def modernize_python(source: str) -> str:
    tree = ast.parse(source)
    datetime_names = {
        alias.asname or alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module == 'datetime'
        for alias in node.names if alias.name == 'datetime'
    }
    lines = source.encode('utf-8').splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    edits = []
    clock_names = set()
    for node in ast.walk(tree):
        replacement = None
        if (isinstance(node, ast.Attribute) and node.attr in {'utcnow', 'utcfromtimestamp'}
                and isinstance(node.value, ast.Name) and node.value.id in datetime_names):
            helper = 'utc_now' if node.attr == 'utcnow' else 'utc_from_timestamp'
            replacement = '_portable_' + helper
            clock_names.add(helper)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
              and node.func.attr == 'get' and len(node.args) == 1 and not node.keywords
              and (query := primary_key_query(node.func.value))):
            # Replace only the callee and opening parenthesis, so nested argument
            # expressions (including another clock/get call) remain independent.
            model, flags = query
            start = offsets[node.func.lineno - 1] + node.func.col_offset
            end = offsets[node.func.end_lineno - 1] + node.func.end_col_offset
            raw = source.encode('utf-8')
            opening = raw.index(b'(', end)
            spacing = '' if raw[opening + 1:opening + 2] in (b'\n', b'\r') else ' '
            edits.append((start, opening + 1, f'{model}.query.session.get({model},{spacing}'.encode()))
            if flags:
                # Insert before an optional trailing comma/comment, not before
                # the closing parenthesis of a multiline call.
                closing = offsets[node.args[0].end_lineno - 1] + node.args[0].end_col_offset
                edits.append((closing, closing, (', ' + ', '.join(key + '=' + value for key, value in flags.items())).encode()))
        if replacement is not None:
            edits.append((offsets[node.lineno - 1] + node.col_offset,
                          offsets[node.end_lineno - 1] + node.end_col_offset,
                          replacement.encode()))
    if clock_names:
        # Preserve module docstring and __future__ placement.
        insertion = 0
        for node in tree.body:
            if ((isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
                 and isinstance(node.value.value, str))
                    or (isinstance(node, ast.ImportFrom) and node.module == '__future__')):
                insertion = offsets[node.end_lineno]
            else:
                break
        declaration = 'from runtime_core.time import ' + ', '.join(name + ' as _portable_' + name for name in sorted(clock_names)) + '\n'
        edits.append((insertion, insertion, declaration.encode()))
    output = source.encode('utf-8')
    for start, end, replacement in sorted(edits, reverse=True):
        output = output[:start] + replacement + output[end:]
    result = output.decode('utf-8')
    ast.parse(result)
    return result
