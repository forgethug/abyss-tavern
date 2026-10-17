"""秘境收藏馆：把隐藏副本普通掉落按固定配方兑换为珍贵物品或永久称号。"""

from __future__ import annotations

from sqlalchemy import exists, func
from sqlalchemy.orm import joinedload

from db_repository import db_manager
from db_repository.decorators import with_transaction
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1Equipment,
    AdventurerS1Item,
    AdventurerS1Title,
    AdventurerS1TitleGrant,
    AdventurerS1WarehouseItem,
)
from plugins.adventurer_s1.services.adventurer_s1_config_service import (
    S1_HIDDEN_RELIC_TITLE_CODE,
    S1_HIDDEN_RELIC_TITLE_RECIPE_ID,
    get_s1_hidden_relic_title_ingredients,
)
from plugins.adventurer_s1.services.s1_title_service import S1TitleService
from plugins.adventurer_s1.services.s1_item_grant_service import grant_catalog_item
from plugins.adventurer_s1.services.s1_item_instance_service import (
    consume_item_stack,
    lock_warehouse_aggregate,
)
from plugins.adventurer_s1.services.s1_item_quantity_service import item_quantity_of


HIDDEN_DUNGEON_EXCHANGE_RECIPES: tuple[dict, ...] = (
    {
        'id': 'class_change_ticket',
        'output_item': '转职试炼凭证',
        'ingredients': (
            {'item_name': '炼狱王心', 'rarity': 'legendary', 'quantity': 1},
            {'item_name': '断罪锁芯', 'rarity': 'epic', 'quantity': 2},
            {'item_name': '巨人战骨', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'class_change_ticket_creation',
        'output_item': '转职试炼凭证',
        'ingredients': (
            {'item_name': '云庭虹羽', 'rarity': 'epic', 'quantity': 3},
            {'item_name': '腐鳞菌壳', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '冥铸魂渣', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'trait_relief_ticket',
        'output_item': '祛厄引路符',
        'ingredients': (
            {'item_name': '创世净尘', 'rarity': 'legendary', 'quantity': 1},
            {'item_name': '黄昏魂露', 'rarity': 'epic', 'quantity': 2},
            {'item_name': '时隙星砂', 'rarity': 'rare', 'quantity': 8},
        ),
    },
    {
        'id': 'trait_relief_ticket_infernal',
        'output_item': '祛厄引路符',
        'ingredients': (
            {'item_name': '零重星壳', 'rarity': 'legendary', 'quantity': 2},
            {'item_name': '废律刻片', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '沉祭雾珠', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'insight_ticket',
        'output_item': '顿悟之钥',
        'ingredients': (
            {'item_name': '混沌王座残片', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '熔心明焰', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'class_skill_scroll',
        'output_item': '职业通用技能卷轴',
        'ingredients': (
            {'item_name': '蓝潮矿膜', 'rarity': 'rare', 'quantity': 1},
            {'item_name': '迁徙角纹', 'rarity': 'rare', 'quantity': 1},
            {'item_name': '风琴鸣晶', 'rarity': 'epic', 'quantity': 1},
            {'item_name': '曦影鳞粉', 'rarity': 'epic', 'quantity': 1},
            {'item_name': '星葬花芯', 'rarity': 'epic', 'quantity': 1},
        ),
    },
    {
        'id': 'knight_class_invitation',
        'output_item': '骑士转职邀请函',
        'ingredients': (
            {'item_name': '誓蜡封核', 'rarity': 'epic', 'quantity': 3},
        ),
    },
    {
        'id': 'ranger_class_invitation',
        'output_item': '游侠转职邀请函',
        'ingredients': (
            {'item_name': '蓝潮矿膜', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '孢压菌囊', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'cleric_class_invitation',
        'output_item': '牧师转职邀请函',
        'ingredients': (
            {'item_name': '星萤花露', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '腐鳞菌壳', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'wizard_class_invitation',
        'output_item': '法师转职邀请函',
        'ingredients': (
            {'item_name': '骨轮轴髓', 'rarity': 'epic', 'quantity': 3},
        ),
    },
    {
        'id': 'fighter_class_invitation',
        'output_item': '战士转职邀请函',
        'ingredients': (
            {'item_name': '缄声锁芯', 'rarity': 'epic', 'quantity': 6},
            {'item_name': '逆潮卵膜', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'barbarian_class_invitation',
        'output_item': '野蛮人转职邀请函',
        'ingredients': (
            {'item_name': '冥铸魂渣', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '蚀光煤晶', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '废律刻片', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'monk_class_invitation',
        'output_item': '武僧转职邀请函',
        'ingredients': (
            {'item_name': '蜃海琉砂', 'rarity': 'epic', 'quantity': 3},
            {'item_name': '孢压菌囊', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'rogue_class_invitation',
        'output_item': '盗贼转职邀请函',
        'ingredients': (
            {'item_name': '蓝潮矿膜', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '星萤花露', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '逆潮卵膜', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'bard_class_invitation',
        'output_item': '吟游诗人转职邀请函',
        'ingredients': (
            {'item_name': '蛇母晶蜕', 'rarity': 'epic', 'quantity': 6},
            {'item_name': '极光冰绡', 'rarity': 'epic', 'quantity': 3},
            {'item_name': '蚀光煤晶', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '月蚀狼髓', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'druid_class_invitation',
        'output_item': '德鲁伊转职邀请函',
        'ingredients': (
            {'item_name': '孢压菌囊', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '腐鳞菌壳', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '逆潮卵膜', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '冥铸魂渣', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '废律刻片', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '圣骸磷晶', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'paladin_class_invitation',
        'output_item': '圣武士转职邀请函',
        'ingredients': (
            {'item_name': '重排界核', 'rarity': 'legendary', 'quantity': 2},
            {'item_name': '蚀光煤晶', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '无旗陨铁', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'sorcerer_class_invitation',
        'output_item': '术士转职邀请函',
        'ingredients': (
            {'item_name': '蝎王尾晶', 'rarity': 'epic', 'quantity': 6},
            {'item_name': '熔蚀风晶', 'rarity': 'epic', 'quantity': 3},
            {'item_name': '星萤花露', 'rarity': 'rare', 'quantity': 4},
            {'item_name': '黑铁母矿', 'rarity': 'rare', 'quantity': 4},
        ),
    },
    {
        'id': 'black_sun_judicator_class_invitation',
        'output_item': '黑日誓裁者转职邀请函',
        'ingredients': (
            {'item_name': '重排界核', 'rarity': 'legendary', 'quantity': 2},
            {'item_name': '雷眼晶核', 'rarity': 'epic', 'quantity': 3},
            {'item_name': '无面镜银', 'rarity': 'epic', 'quantity': 3},
            {'item_name': '迁徙角纹', 'rarity': 'rare', 'quantity': 8},
        ),
    },
    {
        'id': 'star_abyss_weaver_class_invitation',
        'output_item': '星渊织法者转职邀请函',
        'ingredients': (
            {'item_name': '零重星壳', 'rarity': 'legendary', 'quantity': 2},
            {'item_name': '永冻王髓', 'rarity': 'epic', 'quantity': 6},
            {'item_name': '镜莲清露', 'rarity': 'rare', 'quantity': 8},
        ),
    },
)
_RECIPE_BY_ID = {recipe['id']: recipe for recipe in HIDDEN_DUNGEON_EXCHANGE_RECIPES}
_HANDLER_KEY = 'hidden_dungeon_exchange'


class S1HiddenDungeonExchangeService:
    @staticmethod
    def _title_recipe() -> dict:
        return {
            'id': S1_HIDDEN_RELIC_TITLE_RECIPE_ID,
            'output_kind': 'title',
            'output_title_code': S1_HIDDEN_RELIC_TITLE_CODE,
            'ingredients': get_s1_hidden_relic_title_ingredients(),
        }

    @staticmethod
    def _validate_building(building_instance_id: int):
        instance = (
            AdventurerS1BuildingInstance.query
            .options(joinedload(AdventurerS1BuildingInstance.template))
            .filter_by(id=building_instance_id)
            .first()
        )
        if not instance:
            return None, '建筑不存在'
        if instance.status != 'built':
            return None, '建筑尚未建成'
        if not instance.template or instance.template.handler_key != _HANDLER_KEY:
            return None, '该建筑不是秘境收藏馆'
        return instance, None

    @staticmethod
    def _get_owned_adventurer(adventurer_id: int, user_id: int, *, is_admin: bool, for_update: bool):
        query = AdventurerS1.query.filter_by(id=adventurer_id)
        if for_update:
            query = query.populate_existing().with_for_update()
        adventurer = query.first()
        if not adventurer:
            return None, '冒险者不存在'
        if not is_admin and adventurer.owner_id != user_id:
            return None, '无权操作该冒险者'
        can_dispatch, reason = adventurer.can_dispatch()
        if not can_dispatch:
            return None, reason or '该冒险者当前无法兑换'
        return adventurer, None

    @staticmethod
    def _owned_counts(adventurer_id: int, item_names: set[str]) -> dict[str, int]:
        counts = {name: 0 for name in item_names}
        for model in (AdventurerS1Equipment, AdventurerS1WarehouseItem):
            rows = (
                model.query.with_entities(model.name, func.coalesce(func.sum(model.quantity), 0))
                .filter(model.adventurer_id == adventurer_id, model.name.in_(item_names))
                .group_by(model.name)
                .all()
            )
            for name, quantity in rows:
                counts[name] = counts.get(name, 0) + int(quantity or 0)
        return counts

    @staticmethod
    def _catalog_item_details(item_names: set[str]) -> dict[str, dict]:
        rows = (
            AdventurerS1Item.query
            .with_entities(
                AdventurerS1Item.name,
                AdventurerS1Item.type,
                AdventurerS1Item.rarity,
                AdventurerS1Item.description,
            )
            .filter(AdventurerS1Item.name.in_(item_names))
            .all()
        )
        return {
            name: {
                'item_name': name,
                'type': type_,
                'rarity': rarity,
                'description': description or '',
            }
            for name, type_, rarity, description in rows
        }

    @staticmethod
    def _serialize_recipes(adventurer_id: int) -> list[dict]:
        title_recipe = S1HiddenDungeonExchangeService._title_recipe()
        title_row = AdventurerS1Title.query.with_entities(
            AdventurerS1Title,
            exists().where(
                AdventurerS1TitleGrant.adventurer_id == adventurer_id,
                AdventurerS1TitleGrant.title_id == AdventurerS1Title.id,
            ),
        ).filter(
            AdventurerS1Title.code == S1_HIDDEN_RELIC_TITLE_CODE,
            AdventurerS1Title.is_enabled.is_(True),
        ).first()
        recipes = ((title_recipe,) if title_row else ()) + HIDDEN_DUNGEON_EXCHANGE_RECIPES
        ingredient_names = {
            ingredient['item_name']
            for recipe in recipes
            for ingredient in recipe['ingredients']
        }
        output_names = {
            recipe['output_item'] for recipe in HIDDEN_DUNGEON_EXCHANGE_RECIPES
        }
        counts = S1HiddenDungeonExchangeService._owned_counts(
            adventurer_id,
            ingredient_names,
        )
        catalog_details = S1HiddenDungeonExchangeService._catalog_item_details(
            ingredient_names | output_names,
        )

        def item_detail(item_name: str, fallback_rarity: str | None = None) -> dict:
            detail = catalog_details.get(item_name) or {}
            return {
                'item_name': item_name,
                'type': str(detail.get('type') or ''),
                'rarity': str(detail.get('rarity') or fallback_rarity or ''),
                'description': str(detail.get('description') or ''),
            }

        payload: list[dict] = []
        for recipe in recipes:
            ingredients = []
            for ingredient in recipe['ingredients']:
                ingredient_payload = item_detail(
                    ingredient['item_name'],
                    ingredient['rarity'],
                )
                ingredient_payload.update({
                    'required': ingredient['quantity'],
                    'owned': counts.get(ingredient['item_name'], 0),
                })
                ingredients.append(ingredient_payload)
            if recipe.get('output_kind') == 'title':
                title, already_owned = title_row
                payload.append({
                    'id': recipe['id'],
                    'output_kind': 'title',
                    'output_item': title.name,
                    'output_title': title.to_public_dict(),
                    'output_quantity': 1,
                    'ingredients': ingredients,
                    'already_owned': bool(already_owned),
                    'can_redeem': not already_owned and all(
                        row['owned'] >= row['required'] for row in ingredients
                    ),
                })
                continue
            payload.append({
                'output_kind': 'item',
                'id': recipe['id'],
                'output_item': recipe['output_item'],
                'output_item_detail': item_detail(recipe['output_item']),
                'output_quantity': 1,
                'ingredients': ingredients,
                'can_redeem': all(row['owned'] >= row['required'] for row in ingredients),
            })
        return payload

    @staticmethod
    def get_recipes(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        *,
        is_admin: bool = False,
    ):
        _, err = S1HiddenDungeonExchangeService._validate_building(building_instance_id)
        if err:
            return None, err
        adventurer, err = S1HiddenDungeonExchangeService._get_owned_adventurer(
            adventurer_id, user_id, is_admin=is_admin, for_update=False,
        )
        if err:
            return None, err
        return {
            'adventurer': {'id': adventurer.id, 'name': adventurer.name},
            'recipes': S1HiddenDungeonExchangeService._serialize_recipes(adventurer.id),
        }, None

    @staticmethod
    def redeem(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        recipe_id: str,
        *,
        is_admin: bool = False,
    ):
        result, err = S1HiddenDungeonExchangeService._redeem_in_transaction(
            building_instance_id, adventurer_id, user_id, recipe_id, is_admin=is_admin,
        )
        if not err and result.get('output_kind') == 'title':
            # 事务提交成功后才失效名片/详情/小队缓存并通知客户端。
            S1TitleService._after_change(adventurer_id)
        return result, err

    @staticmethod
    @with_transaction(auto_commit=True)
    def _redeem_in_transaction(
        building_instance_id: int,
        adventurer_id: int,
        user_id: int,
        recipe_id: str,
        *,
        is_admin: bool = False,
    ):
        _, err = S1HiddenDungeonExchangeService._validate_building(building_instance_id)
        if err:
            return None, err
        normalized_recipe_id = str(recipe_id or '').strip()
        title = None
        if normalized_recipe_id == S1_HIDDEN_RELIC_TITLE_RECIPE_ID:
            recipe = S1HiddenDungeonExchangeService._title_recipe()
            # 与管理员停用的锁序一致：称号目录 -> 冒险者 -> 仓库聚合 -> 材料。
            # 共享锁让不同冒险者可同时兑换，同时防止扣料期间称号被停用。
            title = AdventurerS1Title.query.filter_by(
                code=S1_HIDDEN_RELIC_TITLE_CODE,
            ).populate_existing().with_for_update(read=True).first()
            if not title or not title.is_enabled:
                return None, '该称号暂不可兑换'
        else:
            recipe = _RECIPE_BY_ID.get(normalized_recipe_id)
        if not recipe:
            return None, '兑换配方不存在'
        adventurer, err = S1HiddenDungeonExchangeService._get_owned_adventurer(
            adventurer_id, user_id, is_admin=is_admin, for_update=True,
        )
        if err:
            return None, err

        if title is not None and AdventurerS1TitleGrant.query.filter_by(
            adventurer_id=adventurer.id, title_id=title.id,
        ).first() is not None:
            return None, '已拥有该称号，无需重复兑换'
        output_name = title.name if title is not None else recipe['output_item']

        required_by_name = {
            ingredient['item_name']: int(ingredient['quantity'])
            for ingredient in recipe['ingredients']
        }
        names = set(required_by_name)
        lock_warehouse_aggregate(adventurer.id)
        rows = []
        for model, source_rank in (
            (AdventurerS1Equipment, 0),
            (AdventurerS1WarehouseItem, 1),
        ):
            locked_rows = (
                model.query
                .filter(model.adventurer_id == adventurer.id, model.name.in_(names))
                .order_by(model.name.asc(), model.id.asc())
                .populate_existing().with_for_update()
                .all()
            )
            rows.extend((source_rank, row) for row in locked_rows)
        rows.sort(key=lambda pair: (pair[1].name, pair[0], pair[1].id))
        available = {name: 0 for name in names}
        for _, row in rows:
            available[row.name] += item_quantity_of(row)
        missing = [
            f'{name} {available.get(name, 0)}/{required}'
            for name, required in required_by_name.items()
            if available.get(name, 0) < required
        ]
        if missing:
            return None, '材料不足：' + '、'.join(missing)

        remaining = dict(required_by_name)
        for _, row in rows:
            need = remaining.get(row.name, 0)
            if need <= 0:
                continue
            amount = min(need, item_quantity_of(row))
            _, consume_err = consume_item_stack(
                row,
                amount,
                source_type='hidden_dungeon_exchange',
                description=f'兑换「{output_name}」消耗「{row.name}」',
                related_id=(f'{building_instance_id}:{recipe["id"]}' if title is not None else None),
                related_type=('hidden_title_exchange' if title is not None else None),
            )
            if consume_err:
                raise RuntimeError(consume_err)
            remaining[row.name] -= amount

        if title is not None:
            # 与扣料流水同一事务；唯一约束和冒险者行锁共同防止重复领取。
            grant = AdventurerS1TitleGrant(
                adventurer_id=adventurer.id,
                title_id=title.id,
                granted_by_user_id=None,
                note=f'hidden_dungeon_exchange:{recipe["id"]};building:{building_instance_id}',
            )
            ok, grant_err = db_manager.add(grant)
            if not ok:
                raise RuntimeError(grant_err or '解锁称号失败')
            return {
                'recipe_id': recipe['id'],
                'output_kind': 'title',
                'output_item': title.name,
                'output_title': title.to_public_dict(),
                'output_quantity': 1,
                'recipes': S1HiddenDungeonExchangeService._serialize_recipes(adventurer.id),
            }, None

        _, grant_err = grant_catalog_item(
            adventurer,
            recipe['output_item'],
            quantity=1,
            prefer_warehouse=True,
            source_type='hidden_dungeon_exchange',
            description=f'秘境收藏馆兑换「{recipe["output_item"]}」',
            related_id=str(building_instance_id),
            related_type='building_instance',
        )
        if grant_err:
            raise RuntimeError(grant_err)
        return {
            'recipe_id': recipe['id'],
            'output_item': recipe['output_item'],
            'output_quantity': 1,
            'recipes': S1HiddenDungeonExchangeService._serialize_recipes(adventurer.id),
        }, None
