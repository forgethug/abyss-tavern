"""S1 奖励物品的玩家端预览元数据组装。"""

from collections.abc import Iterable


def reward_item_entry_name(entry: object) -> str | None:
    if isinstance(entry, str):
        name = entry.strip()
        return name or None
    if isinstance(entry, dict):
        raw = entry.get('name') or entry.get('item_name')
        if raw is None:
            return None
        name = str(raw).strip()
        return name or None
    return None


def collect_reward_item_names(entries: Iterable[object] | None) -> set[str]:
    names: set[str] = set()
    for entry in entries or []:
        name = reward_item_entry_name(entry)
        if name:
            names.add(name)
    return names


def load_item_preview_map(item_names: Iterable[str]) -> dict[str, dict]:
    names = {str(name).strip() for name in item_names if str(name).strip()}
    if not names:
        return {}

    from plugins.adventurer_s1.models import AdventurerS1Item

    rows = AdventurerS1Item.query.filter(
        AdventurerS1Item.name.in_(list(names)),
    ).all()
    return {
        row.name: {
            'rarity': row.rarity,
            'type': row.type,
            'description': row.description,
        }
        for row in rows
    }


def enrich_reward_item_entries(
    entries: Iterable[object] | None,
    preview_by_name: dict[str, dict] | None = None,
) -> list:
    items = list(entries or [])
    preview_map = (
        preview_by_name
        if preview_by_name is not None
        else load_item_preview_map(collect_reward_item_names(items))
    )

    enriched: list = []
    for entry in items:
        name = reward_item_entry_name(entry)
        if not name:
            continue
        preview = preview_map.get(name) or {}
        preview = {
            key: preview[key] for key in ('rarity', 'type', 'description')
            if isinstance(preview.get(key), str) and preview[key]
        }
        if isinstance(entry, str):
            payload = {'name': name, 'quantity': 1}
            payload.update({key: value for key, value in preview.items() if value})
            enriched.append(payload if preview else entry)
            continue
        if isinstance(entry, dict):
            payload = {
                key: entry[key] for key in ('name', 'item_name', 'quantity', 'rarity', 'type', 'description')
                if key in entry and isinstance(entry[key], (str, int, float, type(None)))
            }
            payload['name'] = name
            if 'description' not in payload and isinstance(entry.get('desc'), str):
                payload['description'] = entry['desc']
            payload.update({key: value for key, value in preview.items() if value})
            enriched.append(payload)
            continue
    return enriched
