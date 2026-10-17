"""限时商店后台：当前区服内原子保存活动、商品清单与补货。"""
import hashlib
import json
from datetime import datetime, timezone
from uuid import uuid4
from flask import current_app

from db_repository import db_manager
from db_repository.s1_limited_shop_repository import LimitedShopRepository as Repo
from plugins.adventurer_s1.services.s1_limited_shop_service import limited_shop_catalog
from plugins.adventurer_s1.services.s1_mooncake_catalog import mooncake_catalog
from plugins.adventurer_s1.services.s1_player_error_service import player_operation_failure


def _revision(template):
    data = [template.name, template.description, template.is_active, template.settlement_config]
    return hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _utc(value):
    if not isinstance(value, str):
        raise ValueError('开放时间与结束时间必须包含时区')
    try:
        value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        if value.tzinfo is None:
            raise ValueError()
        return value.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        raise ValueError('时间格式无效，请使用带时区的日期时间') from None


def validate_shop_form(body):
    if not isinstance(body, dict):
        raise ValueError('商店配置必须是对象')
    name = body.get('name')
    description = body.get('description', '')
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 50:
        raise ValueError('商店名称须为 1 至 50 字')
    if not isinstance(description, str):
        raise ValueError('商店描述必须是文本')
    start, end = _utc(body.get('starts_at')), _utc(body.get('ends_at'))
    if start >= end:
        raise ValueError('结束时间必须晚于开放时间')
    enabled = body.get('enabled')
    if type(enabled) is not bool:
        raise ValueError('请选择商店是否开放')
    rows = body.get('items')
    limit = current_app.config['ADVENTURER_S1_LIMITED_SHOP_MAX_ITEMS']
    if not isinstance(rows, list) or not 1 <= len(rows) <= limit:
        raise ValueError(f'商品数量须为 1 至 {limit} 种')
    seen = set()
    presets = mooncake_catalog()
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError('商品格式错误')
        item_id, preset = row.get('item_id'), row.get('preset_name')
        if (type(item_id) is int and item_id > 0 and not preset):
            key = ('id', item_id)
        elif item_id is None and isinstance(preset, str) and preset in presets:
            key = ('preset', preset)
        else:
            raise ValueError('请选择图鉴商品或中秋预设')
        if key in seen:
            raise ValueError('同一商品不能重复上架')
        seen.add(key)
        if type(row.get('price')) is not int or not 1 <= row['price'] <= 2**31 - 1:
            raise ValueError('售价必须为正整数金币')
        if type(row.get('stock_delta')) is not int or not 0 <= row['stock_delta'] <= 2**31 - 1:
            raise ValueError('补货数量必须为非负整数')
    return name.strip(), description, start, end, enabled, rows


def list_shops(page=1):
    per_page = current_app.config['ADVENTURER_S1_LIMITED_SHOP_ADMIN_PAGE_SIZE']
    templates, total = Repo.list_page(page, per_page)
    return {'items': [{'id': t.id, 'name': t.name, 'code': t.code} for t in templates],
            'page': page, 'per_page': per_page, 'total': total, 'has_next': page * per_page < total}


def get_shop(template_id):
    template = Repo.get(template_id)
    if not template:
        return None, '限时商店不存在'
    config = template.settlement_config or {}
    catalog = limited_shop_catalog(template)
    names = tuple(catalog[2]) if catalog else ()
    items = Repo.catalog_by_names(names)
    listings = Repo.listings([item.id for item in items.values()])
    return {
        'id': template.id, 'code': template.code, 'name': template.name,
        'description': template.description, 'revision': _revision(template),
        'enabled': config.get('sale_enabled', True),
        'starts_at': config.get('sale_starts_at'), 'ends_at': config.get('sale_ends_at'),
        'items': [{'item_id': item.id, 'name': item.name, 'price': catalog[2][item.name],
                   'stock': listings[item.id].stock if item.id in listings else 0,
                   'immutable': (item.use_condition or {}).get('immutable') is True,
                   'rarity': item.rarity, 'description': item.description,
                   'stock_delta': 0}
                  for item in items.values()],
    }, None


def save_shop(body, template_id=None):
    try:
        name, description, start, end, enabled, rows = validate_shop_form(body)
        code = body.get('code', '')
        if template_id is None and (not isinstance(code, str) or not 1 <= len(code) <= 50
                                    or any(c not in 'abcdefghijklmnopqrstuvwxyz0123456789_-' for c in code)):
            raise ValueError('商店标识须为 1 至 50 位小写字母、数字、下划线或短横线')
        with db_manager.transaction():
            template = Repo.get(template_id, lock=True) if template_id else Repo.create_template(code, name, description)
            if not template:
                raise ValueError('限时商店不存在')
            if template_id and body.get('revision') != _revision(template):
                raise ValueError('商店已被修改，请重新加载后编辑；未重复补货')
            catalog = Repo.catalog_by_ids([row['item_id'] for row in rows if row.get('item_id')])
            presets = mooncake_catalog()
            preset_names = [row['preset_name'] for row in rows if row.get('preset_name')]
            from plugins.adventurer_s1.services.s1_item_catalog_lock_service import lock_catalog_names
            lock_catalog_names(preset_names, shared=False, wait=True)
            existing_presets = Repo.catalog_by_names(preset_names)
            new_items = Repo.create_items([
                presets[preset_name] for preset_name in preset_names if preset_name not in existing_presets
            ])
            existing_presets.update(new_items)
            new_item_ids = [item.id for item in new_items.values()]
            selected = []
            for row in rows:
                if row.get('preset_name'):
                    item = existing_presets.get(row['preset_name'])
                else:
                    item = catalog.get(row['item_id'])
                if item is None:
                    raise ValueError('所选图鉴商品不存在')
                # 精确预设身份校验，不凭名称关键词识别月饼；不可变要求不能被普通上架绕开。
                if item.name in presets:
                    uc = item.use_condition or {}
                    if (item.type != 'consumable' or item.rarity != 'legendary'
                            or uc.get('immutable') is not True or uc.get('default_uses_left') != 1):
                        raise ValueError(f'「{item.name}」必须为传说、不可变、一次性消耗品，请先核对图鉴')
                selected.append((item, row))
            if len({item.id for item, _ in selected}) != len(selected):
                raise ValueError('同一商品不能重复上架')
            # 模板 -> ShopItem 的固定锁序，与购买在模板之后扣库存的顺序一致。
            listings = Repo.listings([item.id for item, _ in selected], lock=True)
            for item, row in selected:
                listing = listings.get(item.id)
                if listing is None:
                    Repo.create_listing(item.id, row['price'], row['stock_delta'])
                else:
                    stock = int(listing.stock or 0) + row['stock_delta']
                    if not 0 <= stock <= 2**31 - 1:
                        raise ValueError('库存超出允许范围')
                    listing.stock = stock
                    # 既有共享 ShopItem 的标价不改；本店价格只由活动配置决定。
            template.name, template.description = name, description
            template.settlement_config = {
                **(template.settlement_config or {}),
                'sale_starts_at': start.isoformat().replace('+00:00', 'Z'),
                'sale_ends_at': end.isoformat().replace('+00:00', 'Z'),
                'sale_enabled': enabled,
                'sale_prices': {item.name: row['price'] for item, row in selected},
                'sale_revision': uuid4().hex,
            }
            saved_id = template.id
        from plugins.adventurer_s1.services.adventurer_s1_service import invalidate_buildings_list_cache
        invalidate_buildings_list_cache()
        from plugins.adventurer_s1.services.s1_item_catalog_service import S1ItemCatalogService
        for item_id in new_item_ids:
            S1ItemCatalogService.refresh_after_catalog_change(item_id)
        return get_shop(saved_id)
    except ValueError as exc:
        return None, str(exc)
    except Exception as exc:
        return None, player_operation_failure(exc, '保存限时商店失败')
