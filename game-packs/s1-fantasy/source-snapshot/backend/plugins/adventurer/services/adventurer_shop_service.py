import json
import random
import requests
from flask import current_app

__all__ = [
    '_weighted_pick_unique_shop_items',
    '_build_visible_shop_items',
    '_get_shop_buy_probability',
    'clean_polluted_history_story',
    'initialize_shop_items',
    'restock_basic_consumables',
    'ask_deepseek_for_new_items',
    'ai_restock_items',
    '_do_ai_restock_items',
    'adventurer_shop_for_items',
]

from db_repository.manager import db_manager
from plugins.adventurer.services.adventurer_config_service import (
    BASIC_CONSUMABLES,
    INITIAL_EQUIPMENT_SEED,
    RARITY_STOCK_LIMITS,
    RARITY_AI_RESTOCK_THRESHOLD,
    RARITY_PRICE_RANGES,
    DEFAULT_SKILLS,
    _coerce_int,
    _coerce_float,
    _clean_compact_text,
    _random_price_by_rarity,
)


def _weighted_pick_unique_shop_items(shop_items: list, count: int, weight_func) -> list:
    """按权重从候选商店物品中去重抽样，避免高价货长期挤占全部可见栏位。"""
    remaining_items = list(shop_items or [])
    picked_items: list = []
    target_count = max(0, int(count or 0))
    while remaining_items and len(picked_items) < target_count:
        weights = [max(0.0, float(weight_func(shop_item))) for shop_item in remaining_items]
        if not any(weights):
            break
        picked_item = random.choices(remaining_items, weights=weights, k=1)[0]
        picked_items.append(picked_item)
        remaining_items = [shop_item for shop_item in remaining_items if shop_item is not picked_item]
    return picked_items


def _build_visible_shop_items(available_shop_items: list, adventurer, budget: float, has_healing: bool) -> list:
    """为本次逛店挑选可见商品，优先保证能看到买得起且有用的货。"""
    in_stock_items = [
        shop_item for shop_item in (available_shop_items or [])
        if _coerce_int(getattr(shop_item, 'stock', 0), 0) > 0
    ]
    if not in_stock_items:
        return []

    visible_count = random.randint(4, 7)
    current_gold = _coerce_float(getattr(adventurer, 'gold', 0), 0.0)
    spend_cap = max(0.0, min(current_gold, _coerce_float(budget, 0.0)))
    selected_items: list = []

    def _append_unique(shop_item) -> None:
        if shop_item and all(existing is not shop_item for existing in selected_items):
            selected_items.append(shop_item)

    if not has_healing:
        healing_items = [
            shop_item for shop_item in in_stock_items
            if '治疗药水' in shop_item.item.name or '绷带' in shop_item.item.name
        ]
        if not healing_items:
            healing_items = [
                shop_item for shop_item in in_stock_items
                if shop_item.item.type == 'consumable'
            ]
        if healing_items:
            _append_unique(random.choice(healing_items))

    affordable_regular_items = [
        shop_item for shop_item in in_stock_items
        if shop_item.item.rarity not in {'epic', 'legendary'}
        and _coerce_float(getattr(shop_item, 'price', 0), 0.0) <= max(current_gold, spend_cap)
    ]
    guaranteed_regular_count = max(1, min(2, visible_count - len(selected_items)))
    guaranteed_regular_items = _weighted_pick_unique_shop_items(
        [
            shop_item for shop_item in affordable_regular_items
            if all(existing is not shop_item for existing in selected_items)
        ],
        guaranteed_regular_count,
        lambda shop_item: 4.0 if shop_item.item.rarity == 'common' else 2.5
    )
    for shop_item in guaranteed_regular_items:
        _append_unique(shop_item)

    remaining_count = max(0, visible_count - len(selected_items))
    if remaining_count <= 0:
        return selected_items

    rarity_visibility_weights = {
        'common': 6.0,
        'rare': 4.0,
        'epic': 0.7,
        'legendary': 0.2
    }

    def _visibility_weight(shop_item) -> float:
        rarity = _clean_compact_text(getattr(shop_item.item, 'rarity', ''), 20).lower()
        price = _coerce_float(getattr(shop_item, 'price', 0), 0.0)
        weight = rarity_visibility_weights.get(rarity, 1.0)
        if price <= max(current_gold, spend_cap):
            weight *= 2.2
        elif price <= max(current_gold * 0.8, spend_cap * 1.5):
            weight *= 1.1
        else:
            weight *= 0.25
        return weight

    weighted_items = _weighted_pick_unique_shop_items(
        [shop_item for shop_item in in_stock_items if all(existing is not shop_item for existing in selected_items)],
        remaining_count,
        _visibility_weight
    )
    for shop_item in weighted_items:
        _append_unique(shop_item)

    selected_items.sort(
        key=lambda shop_item: (
            {'common': 0, 'rare': 1, 'epic': 2, 'legendary': 3}.get(shop_item.item.rarity, 0),
            -_coerce_float(getattr(shop_item, 'price', 0), 0.0)
        )
    )
    return selected_items


def _get_shop_buy_probability(rarity: str, current_gold: float) -> float:
    """按资产规模和稀有度给出基础购买概率，压低极品装备的冲动消费。"""
    normalized_rarity = _clean_compact_text(rarity, 20).lower()
    rich_threshold = 5000000
    if current_gold > rich_threshold:
        probability_map = {
            'legendary': 0.12,
            'epic': 0.22,
            'rare': 0.72,
            'common': 0.45
        }
    else:
        probability_map = {
            'common': 0.72,
            'rare': 0.32,
            'epic': 0.04,
            'legendary': 0.01
        }
    return probability_map.get(normalized_rarity, 0.3)


def clean_polluted_history_story():
    """在应用启动或更新时自动检测并清理被【战前准备】污染的冒险者传记"""
    import re
    adventurers = db_manager.adventurer.find_all()
    count = 0
    with db_manager.transaction():
        for adv in adventurers:
            if adv.history_story and '【战前准备】' in adv.history_story:
                lines = adv.history_story.split('\n')
                clean_lines = []
                skip_mode = False
                for line in lines:
                    if '【战前准备】' in line:
                        skip_mode = True
                        continue
                    if skip_mode:
                        if not line.strip():
                            continue
                        if not (line.startswith('【') and '】' in line):
                            skip_mode = False
                            clean_lines.append(line)
                        continue
                    clean_lines.append(line)

                cleaned = '\n'.join(clean_lines).strip()
                if '【战前准备】' in cleaned:
                    cleaned = re.sub(r'【战前准备】.*', '', cleaned, flags=re.DOTALL).strip()

                if adv.history_story != cleaned:
                    adv.history_story = cleaned
                    count += 1

    if count > 0:
        current_app.logger.info(f"版本更新修复: 成功清理了 {count} 个冒险者的受污染传记！")


_RARITY_PRIORITY_FOR_AUTO_STORE = {
    'common': 0,
    'rare': 1,
    'epic': 2,
    'legendary': 3,
}


def enforce_inventory_hard_limit_on_startup():
    """应用启动时自动把背包超过 ADVENTURER_INVENTORY_HARD_LIMIT 的冒险者多余装备存到收藏室。

    策略：
    - 仅对 status != 'dead' 的冒险者生效
    - 锁定装备 (`locked=true`) 绝对不动
    - 仅在非锁定装备里挑：按稀有度从低到高（common < rare < epic < legendary）排序，
      逐件移到 warehouse，直到背包剩 ≤ limit 件
    - 如果非锁定全部存完仍 > limit（理论上不会发生，因为锁定上限 10 < 30），
      log warning 并保持现状，不动锁定装备
    - 受影响的冒险者主人会收到邮件通知
    """
    from sqlalchemy.orm.attributes import flag_modified
    from services.mailbox_service import MailboxService
    limit = current_app.config.get('ADVENTURER_INVENTORY_HARD_LIMIT', 30)
    adventurers = [a for a in db_manager.adventurer.find_all() if a.status != 'dead']
    moved_total = 0
    affected_advs = 0
    stuck_advs = 0
    # 收集需要发邮件的：{owner_id: [(adv_name, moved_names), ...]}
    mail_map: dict[int, list[tuple[str, list[str]]]] = {}

    with db_manager.transaction():
        for adv in adventurers:
            equipment = dict(adv.equipment or {})
            if len(equipment) <= limit:
                continue

            non_locked = [
                (name, data) for name, data in equipment.items()
                if isinstance(data, dict) and not data.get('locked')
            ]
            non_locked.sort(key=lambda item: _RARITY_PRIORITY_FOR_AUTO_STORE.get(
                (item[1].get('rarity') or 'common').lower(), 0
            ))

            need_to_remove = len(equipment) - limit
            to_move = non_locked[:need_to_remove]
            if not to_move:
                stuck_advs += 1
                continue

            warehouse = list(adv.warehouse or [])
            moved_names = []
            for name, eq_data in to_move:
                warehouse.append({
                    'name': name,
                    'type': eq_data.get('type', 'consumable'),
                    'desc': eq_data.get('desc', ''),
                    'rarity': eq_data.get('rarity', 'common'),
                    'uses_left': eq_data.get('uses_left'),
                })
                del equipment[name]
                moved_names.append(name)
                moved_total += 1
            adv.equipment = equipment
            adv.warehouse = warehouse
            flag_modified(adv, 'equipment')
            flag_modified(adv, 'warehouse')
            affected_advs += 1

            # 记录需要发邮件的
            if adv.owner_id and moved_names:
                mail_map.setdefault(adv.owner_id, []).append((adv.name, moved_names))

            if len(equipment) > limit:
                # 非锁定全部存完后仍超限（说明锁定数本身就超 limit，违反 lock 上限 10 件的约束）
                stuck_advs += 1

    if affected_advs > 0:
        current_app.logger.info(
            '启动时背包整理完成: 影响 %s 名冒险者，共移动 %s 件物品到收藏室（上限 %s）',
            affected_advs, moved_total, limit,
        )
    if stuck_advs > 0:
        current_app.logger.warning(
            '启动时背包整理: %s 名冒险者锁定装备过多无法压到 %s 件以内，已跳过锁定不动',
            stuck_advs, limit,
        )

    # 给受影响的玩家发邮件通知
    for owner_id, adv_list in mail_map.items():
        lines = []
        for adv_name, moved_names in adv_list:
            lines.append(f'【{adv_name}】背包超限，以下 {len(moved_names)} 件物品已自动存入收藏室：')
            lines.append('  ' + '、'.join(moved_names))
        content = '\n'.join(lines)
        content += '\n\n请及时整理背包，将不需要的物品存入收藏室，保持背包在 30 件以内。'
        MailboxService.send_mail(
            user_id=owner_id,
            title='背包整理通知：部分物品已自动存入收藏室',
            content=content,
            sender_id=0,
            sender_name='系统',
            auto_commit=True,
            mail_type='system',
        )


def initialize_shop_items():
    """初始化商店基础消耗品和初始种子物品，同时对老数据进行迁移补充"""
    import copy
    from plugins.adventurer.models import AdventurerItem, AdventurerShopItem
    with db_manager.transaction():
        for item_data in BASIC_CONSUMABLES:
            existing = db_manager.adventurer_item.get_by_name(item_data["name"])
            if not existing:
                new_item = AdventurerItem(
                    name=item_data["name"],
                    type=item_data["type"],
                    description=item_data["description"],
                    rarity=item_data["rarity"]
                )
                db_manager.add(new_item)
                db_manager.flush()
                new_shop_item = AdventurerShopItem(
                    item_id=new_item.id,
                    price=item_data["price"],
                    stock=item_data["stock"]
                )
                db_manager.add(new_shop_item)
            else:
                existing.description = item_data["description"]
                if not existing.shop_item:
                    new_shop_item = AdventurerShopItem(
                        item_id=existing.id,
                        price=item_data["price"],
                        stock=item_data["stock"]
                    )
                    db_manager.add(new_shop_item)
                else:
                    if existing.shop_item.stock < RARITY_STOCK_LIMITS[existing.rarity]:
                        existing.shop_item.stock = RARITY_STOCK_LIMITS[existing.rarity]

        for item_data in INITIAL_EQUIPMENT_SEED:
            existing = db_manager.adventurer_item.get_by_name(item_data["name"])
            if not existing:
                new_item = AdventurerItem(
                    name=item_data["name"],
                    type=item_data["type"],
                    description=item_data["description"],
                    rarity=item_data["rarity"]
                )
                db_manager.add(new_item)
                db_manager.flush()
                new_shop_item = AdventurerShopItem(
                    item_id=new_item.id,
                    price=item_data["price"],
                    stock=item_data["stock"]
                )
                db_manager.add(new_shop_item)

        adventurers = db_manager.adventurer.find_all(status_in=['idle', 'exploring', 'resting'])
        for adv in adventurers:
            if not adv.skills or len(adv.skills) == 0:
                char_class = adv.character_class
                if char_class in DEFAULT_SKILLS:
                    adv.skills = copy.deepcopy(DEFAULT_SKILLS[char_class])


def restock_basic_consumables():
    """自动补充基础消耗品库存"""
    with db_manager.transaction():
        for item_data in BASIC_CONSUMABLES:
            item = db_manager.adventurer_item.get_by_name(item_data["name"])
            if item and item.shop_item and item.shop_item.stock < RARITY_STOCK_LIMITS[item.rarity]:
                item.shop_item.stock = RARITY_STOCK_LIMITS[item.rarity]


def ask_deepseek_for_new_items(rarity, count=3, is_useless=False):
    """请求AI生成新物品"""
    api_key = current_app.config.get('DEEPSEEK_API_KEY')
    api_base = current_app.config.get('DEEPSEEK_API_BASE')

    if not api_key or not api_base:
        current_app.logger.warning("DeepSeek API key or base URL not configured")
        return []

    existing_items = db_manager.adventurer_item.find_all()

    reference_items_str = """- 【破旧的皮甲】(common): 打满补丁的皮甲，提供微弱的防护。
- 【劣质草药】(common): 勉强能止血的草药，带有泥土的腥味。
- 【生锈的铁剑】(common): 哥布林使用的粗糙武器，砍杀效果一般。
- 【地精火枪】(rare): 地精工程学的产物，威力不错但偶尔会炸膛。
- 【微光戒指】(rare): 镶嵌着微小魔石的戒指，能略微提升佩戴者的魔法感知。
- 【血色巨刃】(epic): 饮满鲜血的巨剑，挥舞时会散发血气，但会逐渐侵蚀使用者的理智。
- 【秘银胸甲】(epic): 大师级工匠用纯净秘银打造的胸甲，轻便且坚不可摧。
- 【时空撕裂者】(legendary): 能够斩断空间法则的匕首，使用过度会导致自身被卷入时空乱流。
- 【星界守护结界】(legendary): 远古星灵赐予的庇护之物，能抵挡致命一击，纯粹的守护力量。"""

    existing_names = [item.name for item in existing_items]
    # existing_names 是 DeepSeek prompt cache 杀手：每次进货都不同会让 system prompt 整段 miss，
    # 因此从 system prompt 抽出来挪到 user prompt 末尾，让 system prompt 完全静态可缓存。
    existing_names_block = (
        f"\n\n绝对不要使用以下已存在的物品名称：{', '.join(existing_names[-50:])}"
        if existing_names else ''
    )

    if is_useless:
        system_prompt = """
你是一个奇幻世界最疯狂的炼金术士和铁匠大师，专门打造极其昂贵的"奢侈垃圾"。

【特别指令】：本次进货，你必须**全部**生成【华而不实的小垃圾】或【奢侈生活废品】。
这些物品【价格参考：50万 - 500万】标价极高，但绝对没有任何战斗属性或实质作用，纯粹是为了掏空那些暴发户冒险者的钱包！
例如：价值连城的"绣着金丝的真丝手帕"、"镶钻的剔牙签"、"极其华丽但毫无防护力的晚礼服"、"纯金打造但一敲就碎的装饰剑"。

绝对不要生成治疗药水、解毒草等基础消耗品，也绝对不要生成任何有真实护甲、攻击力的装备。

请严格按照JSON格式输出，不要有任何其他内容。"""
    else:
        system_prompt = f"""
你是一个奇幻世界最疯狂的炼金术士和铁匠大师，负责为地下城外的黑市商店打造各种极具创意的装备和道具。

以下是我们商店中近期售卖的一些物品示例，请参考它们的强度平衡、描述风格以及稀有度对应的效果层级：
{reference_items_str}

物品稀有度、价格参考与设计方向要求：
- common (普通): 【价格参考：3万 - 10万】效果一般。多设计一些市井气或怪物生态的低级物品，如哥布林的粗糙武器、劣质的草药、破旧的村民工具。
- rare (稀有): 【价格参考：10万 - 50万】效果较强。可以带有微弱元素附魔的装备、矮人工程学的精良产物、稀有魔物器官制成的特殊药水。也可以设计一些【极其精美的稀有首饰】，虽然好看但战斗完全没用。
- epic (史诗): 【价格参考：300万 - 1000万】[随机选择，50%概率选A，50%概率选B]
  A. [强大但有代价] 效果很强，伴随一定代价。上古战争遗留的残破神器、大恶魔的身体部位制成的防具、炼金大师的禁忌药剂。通常伴随着一定的代价（如：使用者会陷入狂暴、轻微降低某项属性、引来怪物仇恨），但非强制。
  B. [稍弱但无代价] 效果也很强大，但稍弱于 A 选项，且**完全没有副作用**。可以是大师精心打造的精良神器、稳定的魔法增幅装备等。
- legendary (传说): 【价格参考：1000万 - 4000万】[随机选择，50%概率选A，50%概率选B]
  A. [极强但代价极大] 效果极强，代价极大。神明陨落时留下的权柄碎片、扭曲现实的时空道具、能够斩断因果的灭世武器。通常伴随着极其可怕的代价（如：反噬生命、永久大幅降低多项属性、可能导致即死）。
  B. [强但无代价] 效果也非常强大，但稍弱于 A 选项，且**完全没有副作用**。可以是远古神灵的祝福圣物、完美稳定的神器等。

设计约束：
1. 物品名称必须极具想象力，不要使用"铁剑"、"皮甲"等烂大街的常见词汇。可以融合深渊、星界、亡灵、蒸汽朋克、古神等元素。
2. 效果描述不要太啰嗦（大概在30个字左右，不要太短也不要太长）。
3. 如果是【华而不实的奢侈品】，请在描述中极力渲染它的昂贵材质和做工，同时毫不留情地点出它在战斗中是个废物。
4. 【副作用规则】：只有特别强大（如传奇级 legendary）或具有逆天改命效果的物品，才必须附带严重的副作用或使用代价。对于常见、稀有甚至普通的史诗物品，如果不涉及禁忌力量，可以不带副作用。不要为了加副作用而加副作用。
5. 物品类型只能是：weapon（武器）、armor（防具）、consumable（消耗品）。（衣服首饰算 armor，纸巾算 consumable）
6. 绝对不要生成治疗药水、解毒草等基础消耗品。

请严格按照JSON格式输出，不要有任何其他内容。"""

    user_prompt = f"""请发挥你天马行空的想象力，生成{count}个【{rarity if not is_useless else '极其昂贵但毫无用处'}】级别的全新奇幻物品，格式如下：
[
    {{
        "name": "物品名称",
        "type": "weapon/armor/consumable",
        "description": "描述效果，严格控制在30个字左右，不要太短也不要太长（若为史诗或传说，可以用更多的字写出代价）",
        "rarity": "{rarity if not is_useless else 'common'}",
        "price": 物品的建议价格（数字格式，根据参考范围合理定价）,
        "stock": {1 if is_useless or rarity in ['epic', 'legendary'] else 6}
    }}
]{existing_names_block}"""

    db_manager.remove()

    try:
        response = requests.post(
            f"{api_base}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json"
            },
            json={
                "model": current_app.config.get('DEEPSEEK_MODEL', 'deepseek-v4-pro'),
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                "temperature": 0.95,
                "max_tokens": 20000
            },
            timeout=120
        )

        if response.status_code == 200:
            result = response.json()
            content = result["choices"][0]["message"]["content"].strip()

            if content.startswith('```json'):
                content = content[7:].strip()
                if content.endswith('```'):
                    content = content[:-3].strip()
            elif content.startswith('```'):
                content = content[3:].strip()
                if content.endswith('```'):
                    content = content[:-3].strip()

            try:
                items = json.loads(content)
                return items
            except json.JSONDecodeError as e:
                current_app.logger.error(f"JSON Parse Error. AI raw output was:\n{content}")
                current_app.logger.error(f"Error details: {str(e)}")
                return []
        else:
            current_app.logger.error(f"Failed to get AI items: {response.status_code}, {response.text}")
            return []
    except Exception as e:
        current_app.logger.error(f"Error asking AI for items: {str(e)}")
        return []


def ai_restock_items(force=False, custom_params=None):
    """提交 AI 进货的异步任务"""
    from services.async_task_service import AsyncTaskService
    AsyncTaskService.submit_task('ai_restock', {'force': force, 'custom_params': custom_params})


def _do_ai_restock_items(force=False, custom_params=None):
    """AI自动补充非基础消耗品。force=True 时无视阈值强制补充；custom_params 支持自定义进货参数。"""
    from plugins.adventurer.models import AdventurerItem, AdventurerShopItem
    from utils.tavern_cache import invalidate_item_cache
    basic_names = [item["name"] for item in BASIC_CONSUMABLES]

    total_new_items_added = 0

    if custom_params:
        req_rarity = custom_params['rarity']
        kinds = custom_params['kinds']
        stock_count = custom_params['stock']

        is_useless = (req_rarity == 'useless')
        actual_rarity = 'common' if is_useless else req_rarity

        new_items = ask_deepseek_for_new_items(actual_rarity, kinds, is_useless=is_useless)
        total_new_items_added += len(new_items)

        if new_items:
            new_shop_item_objs = []
            with db_manager.transaction():
                for item_data in new_items[:kinds]:
                    existing = db_manager.adventurer_item.get_by_name(item_data["name"])
                    price = _random_price_by_rarity('useless' if is_useless else actual_rarity)

                    if not existing:
                        new_item = AdventurerItem(
                            name=item_data["name"],
                            type=item_data["type"],
                            description=item_data["description"],
                            rarity=actual_rarity
                        )
                        db_manager.add(new_item)
                        db_manager.flush()
                        new_shop_item = AdventurerShopItem(
                            item_id=new_item.id,
                            price=price,
                            stock=stock_count
                        )
                        new_shop_item_objs.append(new_shop_item)
                    else:
                        if not existing.shop_item:
                            new_shop_item = AdventurerShopItem(
                                item_id=existing.id,
                                price=price,
                                stock=stock_count
                            )
                            new_shop_item_objs.append(new_shop_item)
                        else:
                            existing.shop_item.stock = stock_count

                if new_shop_item_objs:
                    db_manager.bulk_save_objects(new_shop_item_objs)
                    db_manager.commit()

        if total_new_items_added > 0:
            invalidate_item_cache()
        return total_new_items_added

    for rarity in ["legendary", "epic", "rare", "common"]:
        threshold = RARITY_AI_RESTOCK_THRESHOLD.get(rarity, 0)

        shop_items = db_manager.adventurer_shop_item.get_in_stock(rarity=rarity)
        non_basic_items = [si for si in shop_items if si.item.name not in basic_names]

        if rarity == 'common':
            non_basic_items = [si for si in non_basic_items
                               if not ((si.item.description and ('华而不实' in si.item.description or '昂贵' in si.item.description or '毫无用处' in si.item.description))
                                       or si.price > 500000)]

        if rarity in ["epic", "legendary"]:
            total_count = len(non_basic_items)
        else:
            total_count = sum(si.stock for si in non_basic_items)

        if force or total_count <= threshold:
            limit = RARITY_STOCK_LIMITS.get(rarity, 0)
            if rarity in ["epic", "legendary"]:
                count = limit - total_count
                if count > 0:
                    generate_count = min(count, 3)
                    new_items = ask_deepseek_for_new_items(rarity, generate_count)
                    total_new_items_added += len(new_items)

                    if new_items:
                        new_shop_item_objs = []
                        with db_manager.transaction():
                            for item_data in new_items[:generate_count]:
                                existing = db_manager.adventurer_item.get_by_name(item_data["name"])
                                price = _random_price_by_rarity(rarity)
                                if not existing:
                                    new_item = AdventurerItem(
                                        name=item_data["name"],
                                        type=item_data["type"],
                                        description=item_data["description"],
                                        rarity=item_data["rarity"]
                                    )
                                    db_manager.add(new_item)
                                    db_manager.flush()
                                    new_shop_item = AdventurerShopItem(
                                        item_id=new_item.id,
                                        price=price,
                                        stock=1
                                    )
                                    new_shop_item_objs.append(new_shop_item)
                                else:
                                    if not existing.shop_item:
                                        new_shop_item = AdventurerShopItem(
                                            item_id=existing.id,
                                            price=price,
                                            stock=1
                                        )
                                        new_shop_item_objs.append(new_shop_item)
                                    else:
                                        existing.shop_item.stock = 1

                        if new_shop_item_objs:
                            db_manager.bulk_save_objects(new_shop_item_objs)
                            db_manager.commit()
            else:
                limit = 30 if rarity == 'rare' else RARITY_STOCK_LIMITS.get(rarity, 0)
                count = limit - total_count
                if count > 0:
                    generate_kinds = min(15, max(1, count // 2)) if rarity == 'rare' else min(count, 3)
                    new_items = ask_deepseek_for_new_items(rarity, generate_kinds)
                    total_new_items_added += len(new_items)

                    if new_items:
                        new_shop_item_objs = []
                        with db_manager.transaction():
                            for item_data in new_items:
                                existing = db_manager.adventurer_item.get_by_name(item_data["name"])
                                price = _random_price_by_rarity(rarity)
                                if not existing:
                                    new_item = AdventurerItem(
                                        name=item_data["name"],
                                        type=item_data["type"],
                                        description=item_data["description"],
                                        rarity=item_data["rarity"]
                                    )
                                    db_manager.add(new_item)
                                    db_manager.flush()
                                    new_shop_item = AdventurerShopItem(
                                        item_id=new_item.id,
                                        price=price,
                                        stock=2 if rarity == 'rare' else 6
                                    )
                                    new_shop_item_objs.append(new_shop_item)
                                else:
                                    if not existing.shop_item:
                                        new_shop_item = AdventurerShopItem(
                                            item_id=existing.id,
                                            price=price,
                                            stock=2 if rarity == 'rare' else 6
                                        )
                                        new_shop_item_objs.append(new_shop_item)
                                    else:
                                        if existing.name in basic_names:
                                            existing.shop_item.stock = 50
                                        else:
                                            existing.shop_item.stock += 2 if rarity == 'rare' else 6

                        if new_shop_item_objs:
                            db_manager.bulk_save_objects(new_shop_item_objs)
                            db_manager.commit()

    useless_shop_items = db_manager.adventurer_shop_item.get_in_stock(rarity='common')
    useless_items = [si for si in useless_shop_items
                     if (si.item.description and
                         ('华而不实' in si.item.description or '昂贵' in si.item.description or '毫无用处' in si.item.description))
                     or (si.price > 500000)]
    total_useless_count = len(useless_items)

    if force or total_useless_count <= RARITY_AI_RESTOCK_THRESHOLD['useless']:
        count = RARITY_STOCK_LIMITS['useless'] - total_useless_count
        if count > 0:
            generate_count = 1
            new_items = ask_deepseek_for_new_items('common', generate_count, is_useless=True)
            total_new_items_added += len(new_items)

            if new_items:
                new_shop_item_objs = []
                with db_manager.transaction():
                    for item_data in new_items[:generate_count]:
                        existing = db_manager.adventurer_item.get_by_name(item_data["name"])
                        price = _random_price_by_rarity('useless')

                        if not existing:
                            new_item = AdventurerItem(
                                name=item_data["name"],
                                type=item_data["type"],
                                description=item_data["description"],
                                rarity='common'
                            )
                            db_manager.add(new_item)
                            db_manager.flush()
                            new_shop_item = AdventurerShopItem(
                                item_id=new_item.id,
                                price=price,
                                stock=1
                            )
                            new_shop_item_objs.append(new_shop_item)
                        else:
                            if not existing.shop_item:
                                new_shop_item = AdventurerShopItem(
                                    item_id=existing.id,
                                    price=price,
                                    stock=1
                                )
                                new_shop_item_objs.append(new_shop_item)
                            else:
                                existing.shop_item.stock = 1

                if new_shop_item_objs:
                    db_manager.bulk_save_objects(new_shop_item_objs)
                    db_manager.commit()

    if total_new_items_added > 0:
        invalidate_item_cache()
    return total_new_items_added


def adventurer_shop_for_items(adventurer):
    """让冒险者在商店购买物品"""
    current_gold = _coerce_float(getattr(adventurer, 'gold', 0), 0.0)
    adventurer.gold = current_gold
    if current_gold <= 0:
        return [], []

    available_shop_items = db_manager.adventurer_shop_item.get_in_stock()
    if not available_shop_items:
        return [], []

    purchased = []
    shopping_logs = []
    total_spent = 0
    items_bought = 0
    epic_legendary_bought = 0

    with db_manager.transaction():
        current_gold = _coerce_float(getattr(adventurer, 'gold', 0), 0.0)
        adventurer.gold = current_gold
        if current_gold > 5000000:
            budget = current_gold * random.uniform(0.5, 0.9)
        else:
            budget = current_gold * random.uniform(0.2, 0.5)

        has_healing = any('治疗药水' in name or '绷带' in name for name in adventurer.equipment.keys())
        visible_items = _build_visible_shop_items(
            available_shop_items=available_shop_items,
            adventurer=adventurer,
            budget=budget,
            has_healing=has_healing
        )

        for shop_item in visible_items:
            if total_spent >= budget:
                break

            if items_bought >= 2:
                is_healing = '治疗药水' in shop_item.item.name or '绷带' in shop_item.item.name
                if not is_healing:
                    continue

            if shop_item.stock <= 0:
                continue

            item_name = shop_item.item.name
            rarity = shop_item.item.rarity
            price = shop_item.price

            if rarity in ['epic', 'legendary'] and epic_legendary_bought >= 1:
                continue

            item_desc_lower = shop_item.item.description.lower()
            item_name_lower = item_name.lower()

            class_restrictions = {
                "法师": ["剑", "锤", "斧", "弓", "重甲", "物理伤害", "物理防护", "近战"],
                "牧师": ["剑", "斧", "弓", "匕首", "重甲", "物理伤害", "暗影", "死灵"],
                "战士": ["法杖", "魔杖", "长袍", "法术", "魔法伤害", "智力"],
                "骑士": ["法杖", "魔杖", "长袍", "匕首", "暗杀", "法术"],
                "盗贼": ["大剑", "巨剑", "战锤", "重甲", "法杖", "神圣"],
                "猎人": ["法杖", "重甲", "大剑", "战锤", "神圣"]
            }

            is_suitable = True
            if adventurer.character_class in class_restrictions:
                restricted_words = class_restrictions[adventurer.character_class]
                if shop_item.item.type in ["weapon", "armor"]:
                    for word in restricted_words:
                        if word in item_name_lower or word in item_desc_lower:
                            is_suitable = False
                            break

            if not is_suitable:
                continue

            rarity_color_map = {
                'legendary': '#ffc107',
                'epic': '#dc3545',
                'rare': '#0dcaf0',
                'common': '#6c757d'
            }
            color = rarity_color_map.get(rarity, '#ffffff')
            item_html = f"<span style='color:{color}; font-weight:bold;'>【{item_name}】</span>"

            is_must_buy = shop_item.item.type == "consumable" and not has_healing
            effective_budget = adventurer.gold if is_must_buy else budget

            if price > (effective_budget - total_spent):
                templates = [
                    f"【{adventurer.name}】摸了摸干瘪的钱袋，恋恋不舍地放下了标价{price}金币的{item_html}。",
                    f"【{adventurer.name}】看到了{item_html}，但高昂的价格让他/她倒吸一口凉气，只好作罢。",
                    f"【{adventurer.name}】试图和老板讨价还价购买标价{price}金币的{item_html}，被无情赶了出去。",
                    f"【{adventurer.name}】数了三遍自己身上的钢镚，最后绝望地看着{item_html}长叹一口气。",
                    f"【{adventurer.name}】指着{item_html}问能不能分期付款，老板指了指门外。",
                    f"【{adventurer.name}】把浑身上下的口袋都翻了个底朝天，凑出的钱甚至不够买{item_html}的一个零件。",
                    f"【{adventurer.name}】试图用自己昨晚吃剩的半块干粮换取{item_html}，被老板用扫帚赶出了商店。",
                    f"【{adventurer.name}】看着{item_html}后面那一长串的零，眼角流下了贫穷的泪水。",
                    f"【{adventurer.name}】悄悄问老板买{item_html}能不能把队友抵押在这里，被队友一脚踹飞。",
                    f"【{adventurer.name}】拿着{item_html}依依不舍地走到门口，最终还是因为没钱付账被保安拦了下来。"
                ]
                if random.random() < 0.3:
                    shopping_logs.append(random.choice(templates))
                continue

            level_req = {"legendary": 8, "epic": 5, "rare": 3, "common": 1}.get(rarity, 1)
            level_gap = level_req - adventurer.level
            can_bypass_level = False
            if level_gap > 0:
                bypass_prob = {
                    1: 0.30,
                    2: 0.15,
                    3: 0.05
                }.get(level_gap, 0.01)

                if random.random() < bypass_prob:
                    can_bypass_level = True
                    templates = [
                        f"【{adventurer.name}】虽然现在等级不够，但觉得{item_html}实在是太帅了，决定先买下来当传家宝。",
                        f"【{adventurer.name}】无视了{item_html}旁边的'等级不足会有反噬'的警告牌，执意要买。",
                        f"老板看【{adventurer.name}】等级太低本来不想卖，但看到他/她眼里的狂热，还是把{item_html}递了过去。"
                    ]
                    shopping_logs.append(random.choice(templates))
                else:
                    templates = [
                        f"【{adventurer.name}】看到了{item_html}，但因为自己实力低微，只能流着口水走开。",
                        f"【{adventurer.name}】试图拿起{item_html}，却发现自己根本无法驾驭它强大的力量。",
                        f"【{adventurer.name}】看中了{item_html}，但被老板嘲笑现在的等级还不配使用这种神器。",
                        f"【{adventurer.name}】刚摸到{item_html}的边缘，就被其散发的威压震退了好几步。",
                        f"【{adventurer.name}】踮起脚尖想看一眼柜台顶部的{item_html}，结果被老板一把按住脑袋：'小屁孩别看！'",
                        f"【{adventurer.name}】刚把手伸向{item_html}，就被上面附带的魔法静电电得头发直立，只好作罢。",
                        f"【{adventurer.name}】死活拔不出插在展示台上的{item_html}，在一众冒险者的嘲笑声中红着脸跑开了。",
                        f"【{adventurer.name}】盯着{item_html}看了半天，突然感到一阵头晕目眩，意识到这绝非凡人能驾驭之物。",
                        f"老板指了指{item_html}旁边写着'菜鸟勿碰'的牌子，【{adventurer.name}】只好悻悻地缩回了手。",
                        f"【{adventurer.name}】刚拿起{item_html}，就因为承受不住其重量直接一头栽倒在地。"
                    ]
                    if random.random() < 0.3:
                        shopping_logs.append(random.choice(templates))
                    continue

            if can_bypass_level:
                buy_probability = 1.0
            else:
                buy_probability = _get_shop_buy_probability(
                    rarity=rarity,
                    current_gold=_coerce_float(getattr(adventurer, 'gold', 0), 0.0)
                )
                if rarity in ['epic', 'legendary'] and price > budget:
                    buy_probability *= 0.4

            if shop_item.item.type == "consumable" and not has_healing:
                buy_probability = 1.0

            if random.random() > buy_probability:
                templates = [
                    f"【{adventurer.name}】仔细端详了{item_html}一番，觉得这玩意太丑了，决定不买。",
                    f"【{adventurer.name}】虽然买得起，但觉得{item_html}不符合自己今天的穿搭风格，摇了摇头。",
                    f"【{adventurer.name}】拿在手里试了试{item_html}，觉得手感不对，最终放回了货架。",
                    f"【{adventurer.name}】盯着{item_html}看了一会，感觉性价比不高，转头去看别的了。",
                    f"【{adventurer.name}】嫌弃地闻了闻{item_html}，觉得上面有一股地精的汗臭味，满脸嫌弃地拒绝了。",
                    f"【{adventurer.name}】刚准备付钱，突然觉得把钱留着去酒馆喝花酒更香，于是放下了{item_html}。",
                    f"【{adventurer.name}】仔细比划了一下，觉得{item_html}的颜色和自己的眼睛不搭，傲娇地走开了。",
                    f"【{adventurer.name}】拿起{item_html}掂了掂，吐槽道：'太轻了，完全没有手感！'，然后扔回了柜台。",
                    f"【{adventurer.name}】觉得{item_html}的名字实在太中二了，带出去实在丢人，坚决不买。",
                    f"【{adventurer.name}】看了看{item_html}，又看了看旁边便宜十倍的平替，最终理性战胜了消费欲。",
                    f"老板刚吹嘘完{item_html}有多厉害，【{adventurer.name}】就翻了个白眼：'忽悠谁呢，我看过测评了。'"
                ]
                if random.random() < 0.4:
                    shopping_logs.append(random.choice(templates))
                continue

            shop_item.stock = max(0, _coerce_int(getattr(shop_item, 'stock', 0), 0) - 1)
            adventurer.gold = max(0.0, _coerce_float(getattr(adventurer, 'gold', 0), 0.0) - price)
            total_spent += price

            uses_left = None
            if shop_item.item.type == "consumable":
                uses_left = random.randint(1, 3)
                if '治疗药水' in item_name or '绷带' in item_name:
                    has_healing = True
                else:
                    items_bought += 1
            else:
                items_bought += 1

            if rarity in ['epic', 'legendary']:
                epic_legendary_bought += 1

            item_entry = {
                "type": shop_item.item.type,
                "desc": shop_item.item.description,
                "rarity": shop_item.item.rarity
            }
            if uses_left is not None:
                item_entry["uses_left"] = uses_left

            new_equipment = dict(adventurer.equipment)
            new_equipment[item_name] = item_entry
            adventurer.equipment = new_equipment

            purchased.append(item_name)

            templates = [
                f"【{adventurer.name}】豪掷{price}金币，果断买下了{item_html}！",
                f"【{adventurer.name}】将{item_html}收入囊中，实力大增！老板笑得合不拢嘴。",
                f"【{adventurer.name}】花{price}金币带走了{item_html}，满意地走出了商店。",
                f"【{adventurer.name}】对{item_html}一见钟情，毫不犹豫地掏出{price}金币买下。",
                f"【{adventurer.name}】咬咬牙花费了{price}金币，买下了心仪已久的{item_html}。",
                f"【{adventurer.name}】像个暴发户一样把{price}金币砸在柜台上：'{item_html}给我包起来！'",
                f"【{adventurer.name}】一边肉痛地数着金币，一边把{item_html}死死抱在怀里生怕被人抢走。",
                f"【{adventurer.name}】眼睛都不眨一下，甩出一袋沉甸甸的金币，把{item_html}扛在了肩上！",
                f"【{adventurer.name}】小心翼翼地捧着刚买到的{item_html}，生怕磕着碰着一点皮。",
                f"【{adventurer.name}】买下{item_html}后，立刻在商店门口摆了三个自认为很帅的姿势。",
                f"【{adventurer.name}】与老板经过三天三夜的激烈砍价（虽然并没有降价），最终以{price}金币拿下了{item_html}。",
                f"【{adventurer.name}】刚发了笔横财，大手一挥买下了{item_html}，引得路人纷纷侧目。"
            ]
            shopping_logs.append(random.choice(templates))

    return shopping_logs, purchased
