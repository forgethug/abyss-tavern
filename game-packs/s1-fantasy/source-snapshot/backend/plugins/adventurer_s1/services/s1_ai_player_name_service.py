"""Project frozen player names into AI aliases and restore responses before validation.

The mapping comes only from frozen player identity fields. Protocol keys and stable
references are not names; this adapter does not infer identities from prose.
"""
from __future__ import annotations

import copy
import json
import re
from contextvars import ContextVar
from dataclasses import replace
from functools import wraps
from inspect import signature


_active_aliases: ContextVar[dict[str, str] | None] = ContextVar(
    's1_player_name_aliases', default=None,
)
_active_history_names: ContextVar[frozenset[str]] = ContextVar(
    's1_history_identity_only_names', default=frozenset(),
)
_PROTOCOL_FIELDS = frozenset({
    'role', 'task', 'type', 'status', 'decision', 'verdict', 'kind', 'source_kind',
    'owner_kind', 'rarity', 'knowledge_level', 'regeneration_basis', 'source_layer',
    'event_source', 'review_mode', 'resolution_basis', 'ending_type', 'life_basis',
    'direct_terminal_state', 'terminal', 'field', 'fields', 'target_field',
    'activation_sources', 'decisive_results', 'affected_results', 'targeted_repair_types',
    'character_class', 'class_name', 'new_class', 'gender', 'personality',
    'op', 'operation', 'asset_location',
})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate JSON key')
        result[key] = value
    return result


def _replace_exact_names(
    text: str, aliases: dict[str, str], *, identity_only_names=(),
) -> str:
    if not aliases:
        return text
    # One substitution pass: a replacement token can itself contain a short name.
    protected = set(aliases.values())
    def token(name):
        escaped = re.escape(name)
        if len(name) == 1 and not name.isascii():
            # A historical one-character name is not evidence that the same
            # character inside an ordinary word denotes that player. Exact
            # identity values and standalone mentions still round-trip.
            return rf'(?<!\w){escaped}(?!\w)'
        if re.fullmatch(r'[A-Za-z0-9_]+', name):
            return rf'(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])'
        return escaped
    pattern = '|'.join(token(name) for name in sorted(
        set(aliases) | protected, key=len, reverse=True,
    ))
    return re.sub(pattern, lambda match: (
        match.group(0)
        if match.group(0) in protected or match.group(0) in identity_only_names
        else aliases[match.group(0)]
    ), text)


def _map_text_values(
    value, aliases: dict[str, str], field: str = '', *,
    identity_only_names=(), _path=(),
):
    if isinstance(value, dict):
        columns, rows = value.get('columns'), value.get('rows')
        if (isinstance(columns, list) and all(isinstance(key, str) for key in columns)
                and len(set(columns)) == len(columns) and isinstance(rows, list)
                and all(isinstance(row, list) and len(row) == len(columns) for row in rows)):
            # A wire table retains each cell's original field semantics. Treating
            # all cells as "rows" would anonymize protected enums and signed refs.
            projected = {
                key: _map_text_values(item, aliases, str(key),
                                      identity_only_names=identity_only_names, _path=(*_path, str(key)))
                for key, item in value.items()
                if key not in {'columns', 'rows', 'defaults', 'row_extras'}
            }
            projected['columns'] = list(columns)
            projected['rows'] = [[
                _map_text_values(item, aliases, key,
                                 identity_only_names=identity_only_names, _path=(*_path, key))
                for key, item in zip(columns, row)
            ] for row in rows]
            for key in ('defaults', 'row_extras'):
                if key in value:
                    projected[key] = _map_text_values(
                        value[key], aliases, field,
                        identity_only_names=identity_only_names, _path=_path,
                    )
            return projected
        named_collection = field in {'new_traits', 'new_skills', 'new_equipments'}
        return {
            _replace_exact_names(key, aliases, identity_only_names=identity_only_names)
            if named_collection and isinstance(key, str) else key:
            _map_text_values(
                item, aliases, '' if named_collection else str(key),
                identity_only_names=identity_only_names, _path=(*_path, str(key)),
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        # An array contains asset objects with protocol keys. Only the mapping
        # form of these collections uses asset names as keys.
        item_field = '' if field in {'new_traits', 'new_skills', 'new_equipments'} else field
        return [_map_text_values(
            item, aliases, item_field, identity_only_names=identity_only_names, _path=_path,
        ) for item in value]
    if isinstance(value, str):
        # A historical roster proves a display name existed, not that every
        # homograph in rules or abilities denotes that person. Only explicit
        # player identity fields authorize replacing historical-only names.
        identity_field = (
            field == 'adventurer_name'
            or (len(_path) >= 2 and _path[-2] in {
                '_member_id_to_name', 'member_id_to_name',
            })
            or (field == 'name' and len(_path) >= 2 and _path[-2] in {
                'party_roster', 'member_alias_scope', 'appeal_adventurer',
                'audit_member_snapshot',
            })
            or _path[-3:] in {
                ('waiting_player_npc_injection', 'candidates', 'display_name'),
                ('cross_realm_mirror_injection', 'candidate', 'display_name'),
            }
        )
        if identity_field and value in identity_only_names:
            return aliases.get(value, value)
        if field in _PROTOCOL_FIELDS or field.endswith((
            '_id', '_ids', '_ref', '_refs', '_key', '_keys', '_hash', '_version',
            '_sha256', '_fingerprint', '_digest', '_fields', '_type', '_kind',
            '_mode', '_status', '_code',
        )):
            return value
        return _replace_exact_names(value, aliases, identity_only_names=identity_only_names)
    return value


def _prompt_json_spans(text: str):
    decoder = json.JSONDecoder(object_pairs_hook=_unique_object)
    cursor = 0
    while cursor < len(text):
        if text[cursor] not in '{[':
            cursor += 1
            continue
        try:
            value, end = decoder.raw_decode(text, cursor)
        except ValueError:
            cursor += 1
            continue
        yield cursor, end, value
        cursor = end


def anonymize_prompt_text(
    text: str, aliases: dict[str, str], *, identity_only_names=(),
) -> str:
    """Preserve embedded JSON keys, numbers, enums and refs in mixed text prompts."""
    if not text or not aliases:
        return text
    parts: list[str] = []
    plain_start = 0
    for start, end, value in _prompt_json_spans(text):
        parts.append(_replace_exact_names(
            text[plain_start:start], aliases, identity_only_names=identity_only_names,
        ))
        projected = _map_text_values(value, aliases, identity_only_names=identity_only_names)
        parts.append(
            text[start:end] if projected == value
            else json.dumps(projected, ensure_ascii=False, separators=(',', ':'))
        )
        plain_start = end
    parts.append(_replace_exact_names(
        text[plain_start:], aliases, identity_only_names=identity_only_names,
    ))
    return ''.join(parts)


def frozen_player_names(value) -> list[str]:
    """Read explicit player identity fields, never infer names from story prose."""
    names: dict[str, None] = {}

    def add(name):
        if isinstance(name, str) and name:
            names.setdefault(name, None)

    def visit(value):
        if isinstance(value, dict):
            add(value.get('adventurer_name'))
            for row in value.get('party_roster') or []:
                if isinstance(row, dict):
                    add(row.get('name'))
            for row in value.get('member_alias_scope') or []:
                if isinstance(row, dict):
                    add(row.get('name'))
            applicant = value.get('appeal_adventurer')
            if isinstance(applicant, dict):
                add(applicant.get('name'))
            snapshot = value.get('audit_member_snapshot')
            if isinstance(snapshot, list):
                for row in snapshot:
                    if not isinstance(row, dict):
                        continue
                    member_id = row.get('adventurer_id', row.get('id'))
                    if type(member_id) is int:
                        add(row.get('adventurer_name', row.get('name')))
            member_names = value.get('member_id_to_name') or value.get('_member_id_to_name')
            if isinstance(member_names, dict):
                for name in member_names.values():
                    add(name)
            waiting = value.get('waiting_player_npc_injection')
            if isinstance(waiting, dict):
                for candidate in waiting.get('candidates') or []:
                    if isinstance(candidate, dict):
                        add(candidate.get('display_name'))
            mirror = value.get('cross_realm_mirror_injection')
            candidate = mirror.get('candidate') if isinstance(mirror, dict) else None
            if isinstance(candidate, dict):
                add(candidate.get('display_name'))
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(value)
    return list(names)


def _aliases_from_messages(messages: list[dict], player_names=()) -> dict[str, str]:
    names = {name: None for name in player_names if isinstance(name, str) and name}
    reserved_text = list(_string_leaves(messages))
    for message in messages:
        if message.get('role') != 'user':
            continue
        content = message.get('content')
        if not isinstance(content, str):
            continue
        for _start, _end, value in _prompt_json_spans(content):
            reserved_text.extend(_string_leaves(value))
            for name in frozen_player_names(value):
                names.setdefault(name, None)
    return _allocate_aliases(names, '\n'.join(reserved_text))


def _allocate_aliases(names, source_text: str) -> dict[str, str]:
    names = list(dict.fromkeys(names))
    aliases: dict[str, str] = {}
    next_index = 1
    for name in names:
        # Equal display names remain equal; IDs continue to distinguish members.
        if name in aliases:
            continue
        candidate = f'【人物{next_index}】'
        while candidate in source_text or candidate in names:
            next_index += 1
            candidate = f'【人物{next_index}】'
        aliases[name] = candidate
        next_index += 1
    return aliases


def _string_leaves(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield from _string_leaves(key)
            yield from _string_leaves(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _string_leaves(item)


def historical_player_names(
    expedition_log_id, context, *, member_ids=(), source_text: str | None = None,
) -> list[str]:
    """Find names through source identities, never through semantic text guessing."""
    if type(expedition_log_id) is not int or expedition_log_id <= 0:
        return []
    from flask import current_app
    from db_repository.s1_player_name_history_repository import iter_player_name_history
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin

    members = {value for value in member_ids if type(value) is int and value > 0}
    locations: dict[int, int] = {}
    npc_lives: set[tuple[int, int]] = set()
    source_logs: set[int] = set()

    def visit(value):
        if isinstance(value, dict):
            member_names = value.get('member_id_to_name') or value.get('_member_id_to_name')
            if isinstance(member_names, dict):
                for member_id in member_names:
                    if isinstance(member_id, str) and member_id.isascii() and member_id.isdecimal():
                        member_id = int(member_id)
                    if type(member_id) is int and member_id > 0:
                        members.add(member_id)
            for row in value.get('audit_member_snapshot') or []:
                if isinstance(row, dict):
                    member_id = row.get('adventurer_id', row.get('id'))
                    if type(member_id) is int and member_id > 0:
                        members.add(member_id)
            snapshot = value.get('location_state_snapshot')
            if isinstance(snapshot, dict):
                for row in snapshot.get('locations') or []:
                    if not isinstance(row, dict):
                        continue
                    location_id, revision = row.get('location_id'), row.get('revision')
                    if type(location_id) is int and type(revision) is int:
                        locations[location_id] = max(locations.get(location_id, 0), revision)
            waiting = value.get('waiting_player_npc_injection')
            if isinstance(waiting, dict):
                for row in waiting.get('candidates') or []:
                    if not isinstance(row, dict):
                        continue
                    actor_id, incarnation = row.get('actor_id'), row.get('incarnation')
                    if type(actor_id) is int and type(incarnation) is int:
                        npc_lives.add((actor_id, incarnation))
            for field in ('source_log_id', 'expedition_log_id', 'last_seen_log_id'):
                source_id = value.get(field)
                if type(source_id) is int and 0 < source_id < expedition_log_id:
                    source_logs.add(source_id)
            for item in value.values():
                visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)

    visit(context)
    if not (members or locations or npc_lives or source_logs):
        return []
    if source_text is None:
        source_text = '\n'.join(_string_leaves(context))
    page_key = 'ADVENTURER_S1_AI_NAME_HISTORY_PAGE_SIZE'
    page_size = max(1, int(current_app.config.get(
        page_key, AdventurerS1Plugin.default_config[page_key],
    )))
    names: dict[str, None] = {}
    for snapshot in iter_player_name_history(
        expedition_log_id=expedition_log_id, member_ids=sorted(members),
        location_revisions=locations, npc_lives=sorted(npc_lives),
        source_log_ids=sorted(source_logs), page_size=page_size,
    ):
        for name in frozen_player_names(snapshot):
            if name in source_text:
                names.setdefault(name, None)
    return list(names)


def append_historical_name_aliases(
    name_alias_pairs, names, source_text: str, codename_to_real_out: dict[str, str],
):
    """Extend the existing server-only main-story map without reassigning aliases."""
    aliases = dict(reversed(name_alias_pairs))
    aliases.update({name: alias for alias, name in codename_to_real_out.items()
                    if name not in aliases})
    next_index = 1
    for name in names:
        if name not in aliases:
            alias = f'【G{next_index}】'
            while alias in source_text or alias in codename_to_real_out or alias in aliases.values():
                next_index += 1
                alias = f'【G{next_index}】'
            aliases[name] = alias
            codename_to_real_out[alias] = name
            next_index += 1
        if not any(existing == name for existing, _alias in name_alias_pairs):
            name_alias_pairs.append((name, aliases[name]))


def with_player_name_aliases(function=None, *, include_history=False):
    """Scope the same frozen mapping across the main review and nested repairs."""
    if function is None:
        return lambda function: with_player_name_aliases(function, include_history=include_history)
    function_signature = signature(function)
    @wraps(function)
    def wrapped(*args, **kwargs):
        bound = function_signature.bind(*args, **kwargs).arguments
        messages = bound.get('messages') or []
        aliases = _active_aliases.get()
        history_names = _active_history_names.get()
        if not aliases:
            source = {key: value for key, value in bound.items() if not key.endswith('_out')}
            names = frozen_player_names(source)
            from .adventurer_s1_config_service import is_s1_independent_dungeon
            if include_history and not is_s1_independent_dungeon(bound.get('target_dungeon')):
                # Only frozen backend context may select historical identities;
                # model output can supply text to mask, never additional sources.
                historical = historical_player_names(
                    bound.get('expedition_log_id'),
                    {'cached_full_log': bound.get('cached_full_log'),
                     'member_id_to_name': bound.get('member_id_to_name')},
                    source_text='\n'.join(_string_leaves(source)),
                )
                history_names = frozenset(historical).difference(names)
                names.extend(historical)
            aliases = _allocate_aliases(
                names, '\n'.join(_string_leaves(source)),
            ) or _aliases_from_messages(messages)
        token = _active_aliases.set(aliases)
        history_token = _active_history_names.set(history_names)
        try:
            return function(*args, **kwargs)
        finally:
            _active_history_names.reset(history_token)
            _active_aliases.reset(token)
    return wrapped


def _restore_content(content: str, aliases: dict[str, str]) -> str:
    try:
        parsed = json.loads(content, object_pairs_hook=_unique_object)
    except ValueError:
        # Malformed/duplicate-key JSON remains malformed; existing parsing owns it.
        return _replace_exact_names(content, aliases)
    return json.dumps(_map_text_values(parsed, aliases), ensure_ascii=False)


def _restore_response_json(value, aliases: dict[str, str]):
    result = copy.deepcopy(value)
    if not isinstance(result, dict) or 'choices' not in result:
        return _map_text_values(result, aliases)
    for choice in result.get('choices') or []:
        message = choice.get('message') if isinstance(choice, dict) else None
        if not isinstance(message, dict):
            continue
        content = message.get('content')
        if isinstance(content, str):
            message['content'] = _restore_content(content, aliases)
        elif isinstance(content, list):
            message['content'] = _map_text_values(content, aliases)
    return result


def _restore_decoded_response(decoded, aliases: dict[str, str]):
    """The gateway validator receives frozen S1AiHttpJsonResponseResult, not HTTP."""
    def result_view(result):
        return replace(
            result,
            data=_map_text_values(result.data, aliases),
            content=_restore_content(result.content, aliases),
        )
    return replace(
        decoded,
        json_result=result_view(decoded.json_result),
        http_result=replace(
            decoded.http_result,
            response_json=_restore_response_json(decoded.http_result.response_json, aliases),
            content_result=result_view(decoded.http_result.content_result),
        ),
    )


class _RestoredPlayerNameResponse:
    """Read-only response view; keep the gateway's raw response and metadata intact."""
    def __init__(self, response, reverse_aliases: dict[str, str]):
        self._response = response
        self._reverse_aliases = reverse_aliases

    def __getattr__(self, name):
        return getattr(self._response, name)

    @property
    def text(self):
        try:
            return json.dumps(self.json(), ensure_ascii=False)
        except (TypeError, ValueError):
            return _replace_exact_names(self._response.text, self._reverse_aliases)

    def json(self, **kwargs):
        return _restore_response_json(self._response.json(**kwargs), self._reverse_aliases)


def player_alias_response_evidence(response):
    """Return detached raw/restored JSON and this response's issued identity map."""
    if not isinstance(response, _RestoredPlayerNameResponse):
        return {}, {}, {}
    from plugins.adventurer_s1.services.s1_ai_json_response_service import (
        parse_s1_ai_http_json_response,
    )
    decoded = parse_s1_ai_http_json_response(response._response)
    result = decoded.json_result
    if result.error_code or not isinstance(result.data, dict):
        return {}, {}, {}
    aliases = dict(response._reverse_aliases)
    raw = copy.deepcopy(result.data)
    return raw, _map_text_values(raw, aliases), aliases


def post_s1_ai_request_with_player_aliases(*args, player_names=(), **kwargs):
    from plugins.adventurer_s1.services.s1_ai_runtime_config_service import post_s1_ai_request

    payload = kwargs.get('json') or {}
    aliases = _active_aliases.get() or _aliases_from_messages(
        payload.get('messages') or [], player_names,
    )
    if not aliases:
        return post_s1_ai_request(*args, **kwargs)
    history_names = _active_history_names.get()
    payload = copy.deepcopy(payload)
    for message in payload.get('messages') or []:
        if message.get('role') not in {'user', 'assistant'}:
            continue
        content = message.get('content')
        if not isinstance(content, str):
            continue
        try:
            parsed = json.loads(content, object_pairs_hook=_unique_object)
        except ValueError:
            message['content'] = anonymize_prompt_text(
                content, aliases, identity_only_names=history_names,
            )
        else:
            message['content'] = json.dumps(_map_text_values(
                parsed, aliases, identity_only_names=history_names,
            ), ensure_ascii=False)
    kwargs['json'] = payload
    reverse_aliases = {alias: name for name, alias in aliases.items()}
    validator = kwargs.get('response_validator')
    if validator is not None:
        kwargs['response_validator'] = lambda decoded: validator(
            _restore_decoded_response(decoded, reverse_aliases),
        )
    response = post_s1_ai_request(*args, **kwargs)
    return _RestoredPlayerNameResponse(response, reverse_aliases)
