"""Use the self-hosted minimum party size throughout ordinary matchmaking."""

from pathlib import PurePosixPath


def replace(source, old, new, count=1):
    if source.count(old) != count:
        raise ValueError(f'Matchmaking source drift: {old[:90]!r}')
    return source.replace(old, new)


def project_matchmaking(relative, raw):
    name = PurePosixPath(relative).name
    if relative == 'backend/plugins/adventurer_s1/routes.py':
        source = raw.decode('utf-8-sig').replace('\r\n', '\n')
        return replace(source, '    S1SettingsService.sync_expedition_settings_to_app_config()\n',
            '''    if 'party_min_size' in updated:
        from plugins.adventurer_s1.services.adventurer_s1_matchmaking_service import invalidate_matchmaking_read_cache
        invalidate_matchmaking_read_cache()
    S1SettingsService.sync_expedition_settings_to_app_config()
''').encode('utf-8')
    if name not in {'adventurer_s1_matchmaking_service.py', 'AdventurerS1View.vue'}:
        return raw
    source = raw.decode('utf-8-sig').replace('\r\n', '\n')
    if name == 'adventurer_s1_matchmaking_service.py':
        source = replace(source, 'import hashlib\n', '''import hashlib
from flask import g, has_request_context


def _match_min_size():
    """Read the tavern setting once per HTTP request; ticks read it afresh."""
    from plugins.adventurer_s1.services.adventurer_s1_settings_service import S1SettingsService
    cache_key = '_s1_match_min_size'
    if has_request_context() and hasattr(g, cache_key):
        return getattr(g, cache_key)
    value = int(S1SettingsService.get('party_min_size'))
    if has_request_context():
        setattr(g, cache_key, value)
    return value

''')
        source = replace(source, "        payload['ready_member_threshold'] = 4\n", '')
        source = replace(source, "    if ticket.status == 'queued':\n        ahead =",
                         "    payload['ready_member_threshold'] = _match_min_size()\n    if ticket.status == 'queued':\n        ahead =")
        source = replace(source, '优先 6 人，不足 6 时最低 4 人。', '优先补位，最低人数遵循酒馆设置。')
        source = replace(source, 'return selected_ids if member_total >= 4 else set()',
                         'return selected_ids if member_total >= _match_min_size() else set()')
        source = replace(source, '    def _match_one_party() -> int | None:\n',
                         '    def _match_one_party() -> int | None:\n        min_size = _match_min_size()\n')
        source = replace(source, 'if member_total < 4:', 'if member_total < min_size:')
    else:
        source = replace(source, "  | 'allow_solo_fill'\n", "  | 'allow_solo_fill'\n  | 'ready_member_threshold'\n")
        source = replace(source, '  const count = ticket.member_count ?? 0\n',
                         '  const count = ticket.member_count ?? 0\n  const minimum = ticket.ready_member_threshold ?? 1\n')
        source = replace(source, "ticket.queue_type === 'group' && count >= 4",
                         "ticket.queue_type === 'group' && count >= minimum")
        source = replace(source, '`${count} 人（4 人起成团，最多 6 人）`',
                         '`${count} 人（${minimum} 人起成团，最多 6 人）`')
        source = replace(source, "ticket.queue_type === 'group' && ticket.member_count >= 4",
                         "ticket.queue_type === 'group' && ticket.member_count >= (ticket.ready_member_threshold ?? 1)")
        source = replace(source, '若有同地图/兼容单排会补位至 5-6 人。',
                         '若有同地图/兼容单排可补位，最多 6 人。')
    return source.encode('utf-8')
