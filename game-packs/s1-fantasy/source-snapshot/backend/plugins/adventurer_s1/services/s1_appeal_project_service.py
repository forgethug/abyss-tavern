"""S1 数据申诉中的确定性专项项目：历史漏退补偿与收购物品买回。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

from datetime import datetime, timedelta
from typing import Optional

from flask import current_app
from itsdangerous import BadData, URLSafeSerializer

from db_repository import DatabaseError, db_manager
from plugins.adventurer_s1.models import (
    AdventurerS1,
    AdventurerS1BuildingInstance,
    AdventurerS1BuildingTemplate,
    AdventurerS1BuildingUsageLog,
    AdventurerS1Currency,
    AdventurerS1CurrencyLog,
    AdventurerS1Item,
    AdventurerS1ItemLog,
)
from plugins.adventurer_s1.services.adventurer_s1_currency_service import (
    S1CurrencyService,
)
from utils.time_utils import to_utc_iso


_BUYBACK_SALE_SCHEMA = 's1_buyback_sale_v1'
_BUYBACK_TOKEN_SALT = 's1-appeal-project-buyback-v1'
_PET_EVOLUTION_REFUND_SOURCE = 'appeal_pet_evolution_refund'
_BUYBACK_REPURCHASE_SOURCE = 'appeal_buyback_repurchase'
_VALID_ITEM_SOURCES = frozenset({'carry', 'warehouse'})
_VALID_RARITIES = frozenset({'common', 'rare', 'epic', 'legendary'})


class _ProjectBusinessError(RuntimeError):
    """可安全返回给玩家的专项项目业务错误。"""


class S1AppealProjectService:
    """普通 AI 申诉之外的结构化、可核对、可幂等执行项目。"""

    @staticmethod
    def _scan_limit() -> int:
        try:
            value = int(current_app.config.get(
                'ADVENTURER_S1_APPEAL_PROJECT_SCAN_LIMIT', 200,
            ))
        except (TypeError, ValueError):
            value = 200
        return max(20, min(1000, value))

    @staticmethod
    def _legacy_match_hours() -> float:
        try:
            value = float(current_app.config.get(
                'ADVENTURER_S1_APPEAL_PROJECT_LEGACY_MATCH_HOURS', 24,
            ))
        except (TypeError, ValueError):
            value = 24
        return max(1.0, min(168.0, value))

    @staticmethod
    def _buyback_markup_rate() -> float:
        try:
            value = float(current_app.config.get(
                'ADVENTURER_S1_APPEAL_BUYBACK_MARKUP_RATE', 0.20,
            ))
        except (TypeError, ValueError):
            value = 0.20
        return max(0.0, min(5.0, value))

    @staticmethod
    def _buyback_repurchase_hours() -> float:
        try:
            value = float(current_app.config.get(
                'ADVENTURER_S1_APPEAL_BUYBACK_REPURCHASE_HOURS', 24,
            ))
        except (TypeError, ValueError):
            value = 24
        return max(1.0, min(168.0, value))

    @staticmethod
    def _buyback_expires_at(
        sale_log: AdventurerS1CurrencyLog,
    ) -> Optional[datetime]:
        return sale_log.business_expires_at

    @staticmethod
    def _player_adventurer(
        user_id: int,
        *,
        lock: bool = False,
    ) -> tuple[Optional[AdventurerS1], Optional[str]]:
        query = (
            AdventurerS1.query
            .filter(
                AdventurerS1.owner_id == user_id,
                AdventurerS1.is_player_created.is_(True),
                AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .order_by(AdventurerS1.id.asc())
        )
        if lock:
            query = query.populate_existing().with_for_update()
        adventurer = query.first()
        if not adventurer:
            return None, '你没有可用的冒险者'
        return adventurer, None

    @staticmethod
    def _catalogs_by_name(names: set[str]) -> dict[str, AdventurerS1Item]:
        clean_names = sorted({str(name or '').strip() for name in names if str(name or '').strip()})
        if not clean_names:
            return {}
        rows = AdventurerS1Item.query.filter(AdventurerS1Item.name.in_(clean_names)).all()
        return {str(row.name): row for row in rows}

    @staticmethod
    def _public_item_name(name: str, catalog: Optional[AdventurerS1Item]) -> str:
        if catalog is None:
            return name
        from plugins.adventurer_s1.services.s1_item_use_condition_service import (
            is_hidden_boss_clue_item,
            normalize_use_condition,
            player_clue_item_flavor_name,
        )

        use_condition = normalize_use_condition(catalog.use_condition)
        if is_hidden_boss_clue_item(use_condition):
            return player_clue_item_flavor_name(name, use_condition)
        return name

    @staticmethod
    def _pet_evolution_refund_related_ids(adventurer_id: int, source_ids: list[int]) -> set[int]:
        if not source_ids:
            return set()
        rows = (
            AdventurerS1ItemLog.query
            .with_entities(AdventurerS1ItemLog.related_id)
            .filter(
                AdventurerS1ItemLog.adventurer_id == adventurer_id,
                AdventurerS1ItemLog.source_type == _PET_EVOLUTION_REFUND_SOURCE,
                AdventurerS1ItemLog.related_type == 'item_log',
                AdventurerS1ItemLog.related_id.in_([str(value) for value in source_ids]),
            )
            .all()
        )
        result = set()
        for (related_id,) in rows:
            try:
                result.add(int(related_id))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _legacy_pet_evolution_candidates(
        adventurer_id: int,
        *,
        only_source_ids: Optional[set[int]] = None,
    ) -> list[dict]:
        """匹配旧版无会话快照的嬗变技术中止；正常玩法成败一律排除。"""
        limit = S1AppealProjectService._scan_limit()
        consume_query = AdventurerS1ItemLog.query.filter(
            AdventurerS1ItemLog.adventurer_id == adventurer_id,
            AdventurerS1ItemLog.change_type == 'consume',
            AdventurerS1ItemLog.source_type == 'pet_evolution',
        )
        consume_logs = (
            consume_query
            .order_by(AdventurerS1ItemLog.created_at.desc(), AdventurerS1ItemLog.id.desc())
            .limit(limit)
            .all()
        )
        consume_logs = [row for row in consume_logs if row.created_at is not None]
        if not consume_logs:
            return []

        already_refunded = S1AppealProjectService._pet_evolution_refund_related_ids(
            adventurer_id,
            [int(row.id) for row in consume_logs],
        )
        # 新版会话由 recovery/queue 按原始快照退款，不进入旧版时间匹配。
        # 已补退及新版消耗仍须保留在时间轴中，防止领取后窗口扩大，
        # 把后一次作业的中止退款重新配给前一次消耗。
        eligible_source_ids = {
            int(row.id) for row in consume_logs
            if int(row.id) not in already_refunded
            and not row.related_type
            and not row.related_id
        }
        if not eligible_source_ids:
            return []

        consume_logs.sort(key=lambda row: (row.created_at, row.id))
        match_delta = timedelta(hours=S1AppealProjectService._legacy_match_hours())
        earliest = consume_logs[0].created_at
        latest = consume_logs[-1].created_at + match_delta

        abort_refunds = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
                AdventurerS1CurrencyLog.currency_code == 'gold',
                AdventurerS1CurrencyLog.source_type == 'building_usage_abort_refund',
                AdventurerS1CurrencyLog.related_type == 'building_instance',
                AdventurerS1CurrencyLog.change_amount > 0,
                AdventurerS1CurrencyLog.created_at >= earliest,
                AdventurerS1CurrencyLog.created_at <= latest,
            )
            .order_by(AdventurerS1CurrencyLog.created_at.asc(), AdventurerS1CurrencyLog.id.asc())
            .limit(limit * 2)
            .all()
        )
        if not abort_refunds:
            return []

        building_ids = set()
        for row in abort_refunds:
            try:
                building_ids.add(int(row.related_id))
            except (TypeError, ValueError):
                continue
        pet_evolution_building_ids = {
            int(row.id)
            for row in (
                AdventurerS1BuildingInstance.query
                .with_entities(AdventurerS1BuildingInstance.id)
                .join(
                    AdventurerS1BuildingTemplate,
                    AdventurerS1BuildingTemplate.id == AdventurerS1BuildingInstance.template_id,
                )
                .filter(
                    AdventurerS1BuildingInstance.id.in_(building_ids),
                    AdventurerS1BuildingTemplate.handler_key == 'pet_evolution',
                )
                .all()
            )
        }
        filtered_refunds = []
        for row in abort_refunds:
            try:
                building_id = int(row.related_id)
            except (TypeError, ValueError):
                continue
            if building_id in pet_evolution_building_ids and row.created_at is not None:
                filtered_refunds.append((row, building_id))
        if not filtered_refunds:
            return []

        terminal_logs = (
            AdventurerS1BuildingUsageLog.query
            .filter(
                AdventurerS1BuildingUsageLog.adventurer_id == adventurer_id,
                AdventurerS1BuildingUsageLog.building_instance_id.in_(pet_evolution_building_ids),
                AdventurerS1BuildingUsageLog.created_at >= earliest,
                AdventurerS1BuildingUsageLog.created_at <= latest,
            )
            .order_by(
                AdventurerS1BuildingUsageLog.created_at.asc(),
                AdventurerS1BuildingUsageLog.id.asc(),
            )
            .limit(limit * 2 + 1)
            .all()
        )
        # 不完整的终态证据不能证明“没有正常结算”。
        if len(terminal_logs) > limit * 2:
            return []
        terminal_logs = [
            row for row in terminal_logs
            if isinstance(row.result, dict) and row.result.get('type') == 'pet_evolution'
        ]

        used_abort_ids: set[int] = set()
        paired: list[dict] = []
        for index, consume in enumerate(consume_logs):
            if int(consume.id) not in eligible_source_ids:
                continue
            upper = consume.created_at + match_delta
            if index + 1 < len(consume_logs):
                upper = min(upper, consume_logs[index + 1].created_at)
            matched_refund = None
            matched_building_id = None
            for refund, building_id in filtered_refunds:
                if int(refund.id) in used_abort_ids:
                    continue
                if refund.created_at < consume.created_at or refund.created_at >= upper:
                    continue
                matched_refund = refund
                matched_building_id = building_id
                break
            if matched_refund is None or matched_building_id is None:
                continue

            # 正常 AI 世界内成败都会留下 terminal usage；技术中止会删除该记录。
            has_terminal = any(
                row.building_instance_id == matched_building_id
                and row.created_at is not None
                and consume.created_at <= row.created_at <= matched_refund.created_at
                for row in terminal_logs
            )
            if has_terminal:
                continue

            used_abort_ids.add(int(matched_refund.id))
            paired.append({
                'source_log': consume,
                'abort_refund_log': matched_refund,
            })

        catalogs = S1AppealProjectService._catalogs_by_name({
            str(row['source_log'].item_name or '') for row in paired
        })
        result = []
        for row in paired:
            source_log = row['source_log']
            if only_source_ids and int(source_log.id) not in only_source_ids:
                continue
            name = str(source_log.item_name or '').strip()
            item_type = str(source_log.item_type or '').strip()
            rarity = str(source_log.item_rarity or 'common').strip().lower()
            quantity = int(source_log.quantity or 0)
            catalog = catalogs.get(name)
            if (
                not name
                or not item_type
                or rarity not in _VALID_RARITIES
                or quantity <= 0
                or catalog is None
                or str(catalog.type or '').strip() != item_type
            ):
                continue
            result.append({
                '_source_log_id': int(source_log.id),
                '_abort_refund_log_id': int(row['abort_refund_log'].id),
                '_snapshot': {
                    'source': 'warehouse',
                    'name': name,
                    'type': item_type,
                    'rarity': rarity,
                    'quantity': quantity,
                    'uses_left': None,
                    'custom_desc': None,
                    'evolution_stage': 0,
                },
                'name': S1AppealProjectService._public_item_name(name, catalog),
                'rarity': rarity,
                'quantity': quantity,
                'occurred_at': to_utc_iso(source_log.created_at),
            })
        result.sort(key=lambda row: (row['occurred_at'] or '', row['_source_log_id']), reverse=True)
        return result

    @staticmethod
    def _parse_buyback_description(description: str) -> Optional[tuple[str, int]]:
        """解析本项目固定生成的尾部：出售「名称」或出售「名称」×N。"""
        text = str(description or '').strip()
        marker_pos = text.rfind('出售「')
        if marker_pos < 0:
            return None
        tail = text[marker_pos + len('出售「'):]
        close_pos = tail.rfind('」')
        if close_pos <= 0:
            return None
        name = tail[:close_pos].strip()
        suffix = tail[close_pos + 1:]
        if not name:
            return None
        if not suffix:
            return name, 1
        if suffix.startswith('×') and suffix[1:].isdigit():
            quantity = int(suffix[1:])
            return (name, quantity) if quantity > 0 else None
        return None

    @staticmethod
    def _normalize_snapshot(raw: object) -> Optional[dict]:
        if not isinstance(raw, dict):
            return None
        source = str(raw.get('source') or '').strip()
        name = str(raw.get('name') or '').strip()
        item_type = str(raw.get('type') or '').strip()
        rarity = str(raw.get('rarity') or 'common').strip().lower()
        try:
            quantity = int(raw.get('quantity') or 0)
            evolution_stage = int(raw.get('evolution_stage') or 0)
        except (TypeError, ValueError):
            return None
        if (
            source not in _VALID_ITEM_SOURCES
            or not name
            or not item_type
            or rarity not in _VALID_RARITIES
            or quantity <= 0
            or evolution_stage < 0
        ):
            return None
        custom_desc = raw.get('custom_desc')
        if custom_desc is not None and not isinstance(custom_desc, str):
            return None
        uses_left = raw.get('uses_left')
        if uses_left is not None:
            try:
                uses_left = int(uses_left)
            except (TypeError, ValueError):
                return None
        snapshot = {
            'source': source,
            'name': name,
            'type': item_type,
            'rarity': rarity,
            'quantity': quantity,
            'uses_left': uses_left,
            'custom_desc': custom_desc,
            'evolution_stage': evolution_stage,
        }
        if source == 'carry':
            snapshot['damage_stage'] = raw.get('damage_stage')
            snapshot['damage_note'] = raw.get('damage_note')
        return snapshot

    @staticmethod
    def _buyback_serializer() -> URLSafeSerializer:
        secret = current_app.secret_key or current_app.config.get('SECRET_KEY')
        if not secret:
            raise RuntimeError('服务端签名配置缺失')
        return URLSafeSerializer(secret_key=secret, salt=_BUYBACK_TOKEN_SALT)

    @staticmethod
    def _encode_buyback_token(currency_log_id: int) -> str:
        return S1AppealProjectService._buyback_serializer().dumps({
            'v': 1,
            'currency_log_id': int(currency_log_id),
        })

    @staticmethod
    def _decode_buyback_token(token: str) -> Optional[int]:
        if not isinstance(token, str) or not token.strip() or len(token) > 1000:
            return None
        try:
            data = S1AppealProjectService._buyback_serializer().loads(token)
        except BadData:
            return None
        if not isinstance(data, dict) or data.get('v') != 1:
            return None
        try:
            value = int(data.get('currency_log_id'))
        except (TypeError, ValueError):
            return None
        return value if value > 0 else None

    @staticmethod
    def _repurchased_sale_ids(adventurer_id: int, sale_ids: list[int]) -> set[int]:
        if not sale_ids:
            return set()
        rows = (
            AdventurerS1CurrencyLog.query
            .with_entities(AdventurerS1CurrencyLog.related_id)
            .filter(
                AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
                AdventurerS1CurrencyLog.source_type == _BUYBACK_REPURCHASE_SOURCE,
                AdventurerS1CurrencyLog.related_type == 'buyback_currency_log',
                AdventurerS1CurrencyLog.related_id.in_([str(value) for value in sale_ids]),
            )
            .all()
        )
        result = set()
        for (related_id,) in rows:
            try:
                result.add(int(related_id))
            except (TypeError, ValueError):
                continue
        return result

    @staticmethod
    def _build_buyback_candidates(
        sale_logs: list[AdventurerS1CurrencyLog],
        *,
        include_token: bool,
        now: Optional[datetime] = None,
    ) -> list[dict]:
        checked_at = now or _portable_utc_now()
        expires_by_id = {}
        active_sale_logs = []
        for row in sale_logs:
            expires_at = S1AppealProjectService._buyback_expires_at(row)
            if expires_at is None or expires_at <= checked_at:
                continue
            expires_by_id[int(row.id)] = expires_at
            active_sale_logs.append(row)
        sale_logs = active_sale_logs

        parsed_by_id = {
            int(row.id): S1AppealProjectService._parse_buyback_description(row.description)
            for row in sale_logs
        }
        names = {
            parsed[0]
            for parsed in parsed_by_id.values()
            if parsed is not None
        }
        for row in sale_logs:
            payload = row.business_payload if isinstance(row.business_payload, dict) else {}
            snapshot = payload.get('item_snapshot')
            if isinstance(snapshot, dict) and snapshot.get('name'):
                names.add(str(snapshot.get('name')).strip())
        catalogs = S1AppealProjectService._catalogs_by_name(names)
        markup = S1AppealProjectService._buyback_markup_rate()
        result = []
        for sale_log in sale_logs:
            parsed = parsed_by_id.get(int(sale_log.id))
            if parsed is None:
                continue
            parsed_name, parsed_quantity = parsed
            payload = sale_log.business_payload if isinstance(sale_log.business_payload, dict) else {}
            exact = payload.get('schema') == _BUYBACK_SALE_SCHEMA
            if exact:
                snapshot = S1AppealProjectService._normalize_snapshot(payload.get('item_snapshot'))
                try:
                    sale_total = round(float(payload.get('gold_total')), 2)
                    unit_price = round(float(payload.get('unit_price')), 2)
                except (TypeError, ValueError):
                    snapshot = None
                    sale_total = 0.0
                    unit_price = 0.0
                if (
                    snapshot is None
                    or snapshot['name'] != parsed_name
                    or snapshot['quantity'] != parsed_quantity
                    or unit_price <= 0
                    or round(unit_price * snapshot['quantity'], 2) != sale_total
                    or round(float(sale_log.change_amount or 0), 2) != sale_total
                ):
                    continue
            else:
                catalog = catalogs.get(parsed_name)
                source = {
                    'equipment': 'carry',
                    'warehouse': 'warehouse',
                }.get(str(sale_log.related_type or ''))
                if catalog is None or source is None:
                    continue
                rarity = str(catalog.rarity or 'common').strip().lower()
                if rarity not in _VALID_RARITIES or not str(catalog.type or '').strip():
                    continue
                snapshot = {
                    'source': source,
                    'name': parsed_name,
                    'type': str(catalog.type).strip(),
                    'rarity': rarity,
                    'quantity': parsed_quantity,
                    'uses_left': None,
                    'custom_desc': None,
                    'evolution_stage': 0,
                }
                sale_total = round(float(sale_log.change_amount or 0), 2)

            if sale_total <= 0:
                continue
            catalog = catalogs.get(snapshot['name'])
            if catalog is None:
                continue
            # 新出售流水已经冻结了可核验的完整实例快照。图鉴后续订正
            # type/rarity 时，仍应按该快照开放误售买回；类型一致性只属于
            # 没有快照、必须依赖当前图鉴重建的旧流水边界。
            if (
                not exact
                and str(catalog.type or '').strip() != snapshot['type']
            ):
                continue
            from plugins.adventurer_s1.services.s1_item_use_condition_service import (
                is_hidden_boss_clue_item,
                normalize_use_condition,
            )
            if is_hidden_boss_clue_item(normalize_use_condition(catalog.use_condition)):
                current_rarity = str(catalog.rarity or 'common').strip().lower()
                if current_rarity in _VALID_RARITIES:
                    snapshot['rarity'] = current_rarity

            repurchase_cost = round(sale_total * (1.0 + markup), 2)
            projected = {
                '_sale_log_id': int(sale_log.id),
                '_snapshot': snapshot,
                'name': S1AppealProjectService._public_item_name(snapshot['name'], catalog),
                'rarity': snapshot['rarity'],
                'quantity': snapshot['quantity'],
                'sold_at': to_utc_iso(sale_log.created_at),
                'expires_at': to_utc_iso(expires_by_id[int(sale_log.id)]),
                'sale_total': sale_total,
                'repurchase_cost': repurchase_cost,
                'snapshot_quality': 'exact' if exact else 'catalog_reconstructed',
            }
            if include_token:
                projected['token'] = S1AppealProjectService._encode_buyback_token(sale_log.id)
            result.append(projected)
        result.sort(key=lambda row: (row['sold_at'] or '', row['_sale_log_id']), reverse=True)
        return result

    @staticmethod
    def _available_buybacks(adventurer_id: int) -> list[dict]:
        limit = S1AppealProjectService._scan_limit()
        logs = (
            AdventurerS1CurrencyLog.query
            .filter(
                AdventurerS1CurrencyLog.adventurer_id == adventurer_id,
                AdventurerS1CurrencyLog.currency_code == 'gold',
                AdventurerS1CurrencyLog.source_type == 'buyback_sell',
                AdventurerS1CurrencyLog.change_amount > 0,
            )
            .order_by(AdventurerS1CurrencyLog.created_at.desc(), AdventurerS1CurrencyLog.id.desc())
            .limit(limit)
            .all()
        )
        repurchased = S1AppealProjectService._repurchased_sale_ids(
            adventurer_id,
            [int(row.id) for row in logs],
        )
        return S1AppealProjectService._build_buyback_candidates(
            [row for row in logs if int(row.id) not in repurchased],
            include_token=True,
        )

    @staticmethod
    def get_overview(user_id: int) -> tuple[Optional[dict], Optional[str]]:
        adventurer, err = S1AppealProjectService._player_adventurer(user_id)
        if err or adventurer is None:
            return None, err
        mutation_items = S1AppealProjectService._legacy_pet_evolution_candidates(adventurer.id)
        buyback_items = S1AppealProjectService._available_buybacks(adventurer.id)
        currency = AdventurerS1Currency.query.filter_by(
            adventurer_id=adventurer.id,
            currency_code='gold',
        ).first()
        gold = round(float(currency.amount if currency is not None else adventurer.gold or 0), 2)
        markup_percent = round(S1AppealProjectService._buyback_markup_rate() * 100, 2)
        repurchase_hours = round(S1AppealProjectService._buyback_repurchase_hours(), 2)
        return {
            'gold': gold,
            'projects': [
                {
                    'code': 'pet_evolution_refund',
                    'title': '嬗变故障漏退补偿',
                    'description': '核对旧版嬗变技术中止流水，补回已扣但未退的辅助物品。',
                    'available_count': len(mutation_items),
                    'items': [
                        {key: value for key, value in row.items() if not key.startswith('_')}
                        for row in mutation_items
                    ],
                },
                {
                    'code': 'buyback_repurchase',
                    'title': '收购商行误售买回',
                    'description': (
                        f'按原收购总价加价 {markup_percent:g}% 买回；'
                        f'每条出售流水只能在 {repurchase_hours:g} 小时内买回一次。'
                    ),
                    'markup_percent': markup_percent,
                    'repurchase_hours': repurchase_hours,
                    'available_count': len(buyback_items),
                    'items': [
                        {key: value for key, value in row.items() if not key.startswith('_')}
                        for row in buyback_items
                    ],
                },
            ],
        }, None

    @staticmethod
    def _restore_one_pet_evolution_item(
        user_id: int,
        source_log_id: int,
    ) -> tuple[Optional[dict], Optional[str]]:
        try:
            with db_manager.transaction():
                adventurer, err = S1AppealProjectService._player_adventurer(user_id, lock=True)
                if err or adventurer is None:
                    raise _ProjectBusinessError(err or '你没有可用的冒险者')
                source_log = (
                    AdventurerS1ItemLog.query
                    .filter_by(id=source_log_id, adventurer_id=adventurer.id)
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if source_log is None:
                    raise _ProjectBusinessError('这条嬗变记录已经不存在')
                replay = AdventurerS1ItemLog.query.filter_by(
                    adventurer_id=adventurer.id,
                    source_type=_PET_EVOLUTION_REFUND_SOURCE,
                    related_id=str(source_log_id),
                    related_type='item_log',
                ).first()
                if replay is not None:
                    return {
                        'name': replay.item_name,
                        'quantity': int(replay.quantity or 0),
                        'already_refunded': True,
                    }, None
                matches = S1AppealProjectService._legacy_pet_evolution_candidates(
                    adventurer.id,
                    only_source_ids={source_log_id},
                )
                if not matches:
                    raise _ProjectBusinessError('这条记录已不满足自动补退条件')
                candidate = matches[0]
                snapshot = candidate['_snapshot']

                from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
                destination, restore_err = grant_raw_item(
                    adventurer,
                    snapshot['name'],
                    {
                        'type': snapshot['type'],
                        'rarity': snapshot['rarity'],
                        'quantity': snapshot['quantity'],
                        'uses_left': snapshot.get('uses_left'),
                        'desc': snapshot.get('custom_desc'),
                        'evolution_stage': snapshot.get('evolution_stage', 0),
                    },
                    source_type=_PET_EVOLUTION_REFUND_SOURCE,
                    description='专项申诉核对旧嬗变技术中止流水，补退辅助物品',
                    force_destination='warehouse',
                    equipment_first=False,
                    allow_equipment_overflow=True,
                    register_catalog=False,
                    related_id=str(source_log_id),
                    related_type='item_log',
                    allow_same_name_stack_merge=True,
                )
                if restore_err or destination is None:
                    raise _ProjectBusinessError(restore_err or '物品补退失败')
                return {
                    'name': candidate['name'],
                    'quantity': snapshot['quantity'],
                    'destination': destination,
                    'already_refunded': False,
                }, None
        except DatabaseError as exc:
            original = getattr(exc, 'original_error', None)
            if isinstance(original, _ProjectBusinessError):
                return None, str(original)
            current_app.logger.exception('S1 嬗变专项补退失败 source_log=%s', source_log_id)
            return None, '物品补退失败，事务已回滚'

    @staticmethod
    def claim_pet_evolution_refunds(user_id: int) -> tuple[Optional[dict], Optional[str]]:
        adventurer, err = S1AppealProjectService._player_adventurer(user_id)
        if err or adventurer is None:
            return None, err
        candidates = S1AppealProjectService._legacy_pet_evolution_candidates(adventurer.id)
        if not candidates:
            return {
                'refunded_count': 0,
                'failed_count': 0,
                'items': [],
                'message': '没有发现可自动补退的嬗变辅助物品',
            }, None

        results = []
        for candidate in candidates:
            restored, restore_err = S1AppealProjectService._restore_one_pet_evolution_item(
                user_id,
                int(candidate['_source_log_id']),
            )
            if restore_err:
                results.append({
                    'name': candidate['name'],
                    'quantity': candidate['quantity'],
                    'status': 'failed',
                    'message': restore_err,
                })
            else:
                results.append({
                    'name': restored['name'],
                    'quantity': restored['quantity'],
                    'status': 'refunded',
                    'destination': restored.get('destination'),
                })
        refunded_count = sum(1 for row in results if row['status'] == 'refunded')
        failed_count = len(results) - refunded_count
        return {
            'refunded_count': refunded_count,
            'failed_count': failed_count,
            'items': results,
            'message': (
                f'已补退 {refunded_count} 条嬗变辅助物品'
                if failed_count == 0
                else f'已补退 {refunded_count} 条，另有 {failed_count} 条未能自动处理'
            ),
        }, None

    @staticmethod
    def repurchase_buyback_sale(
        user_id: int,
        token: str,
    ) -> tuple[Optional[dict], Optional[str]]:
        sale_log_id = S1AppealProjectService._decode_buyback_token(token)
        if sale_log_id is None:
            return None, '买回凭据无效，请刷新后重试'

        try:
            with db_manager.transaction():
                adventurer, err = S1AppealProjectService._player_adventurer(user_id, lock=True)
                if err or adventurer is None:
                    raise _ProjectBusinessError(err or '你没有可用的冒险者')
                sale_log = (
                    AdventurerS1CurrencyLog.query
                    .filter_by(
                        id=sale_log_id,
                        adventurer_id=adventurer.id,
                        currency_code='gold',
                        source_type='buyback_sell',
                    )
                    .populate_existing()
                    .with_for_update()
                    .first()
                )
                if sale_log is None:
                    raise _ProjectBusinessError('原出售流水不存在或不属于你的冒险者')

                replay = (
                    AdventurerS1CurrencyLog.query
                    .filter_by(
                        adventurer_id=adventurer.id,
                        source_type=_BUYBACK_REPURCHASE_SOURCE,
                        related_id=str(sale_log.id),
                        related_type='buyback_currency_log',
                    )
                    .order_by(AdventurerS1CurrencyLog.id.asc())
                    .first()
                )
                if replay is not None:
                    payload = replay.business_payload if isinstance(replay.business_payload, dict) else {}
                    return {
                        'name': str(payload.get('public_name') or '原出售物品'),
                        'quantity': int(payload.get('quantity') or 1),
                        'repurchase_cost': round(abs(float(replay.change_amount or 0)), 2),
                        'gold': round(float(replay.balance_after or 0), 2),
                        'destination': payload.get('destination'),
                        'already_redeemed': True,
                        'message': '这件物品已经买回，无需重复扣款',
                    }, None

                checked_at = _portable_utc_now()
                expires_at = S1AppealProjectService._buyback_expires_at(sale_log)
                if expires_at is None or expires_at <= checked_at:
                    hours = S1AppealProjectService._buyback_repurchase_hours()
                    raise _ProjectBusinessError(
                        f'已超过 {hours:g} 小时买回期限，无法再买回这件物品'
                    )

                candidates = S1AppealProjectService._build_buyback_candidates(
                    [sale_log],
                    include_token=False,
                    now=checked_at,
                )
                if not candidates:
                    raise _ProjectBusinessError('原出售记录缺少可核对的物品资料，无法自动买回')
                candidate = candidates[0]
                snapshot = candidate['_snapshot']
                cost = round(float(candidate['repurchase_cost']), 2)
                new_balance, deduct_err, charge_log = S1CurrencyService.deduct_in_transaction(
                    adventurer.id,
                    'gold',
                    cost,
                    source_type=_BUYBACK_REPURCHASE_SOURCE,
                    related_id=sale_log.id,
                    related_type='buyback_currency_log',
                    description=f'专项申诉买回收购商行误售的「{candidate["name"]}」',
                    allow_negative=False,
                )
                if deduct_err or charge_log is None:
                    if deduct_err and '余额不足' in deduct_err:
                        raise _ProjectBusinessError(f'金币不足，需要 {cost:g} 金币才能买回')
                    raise _ProjectBusinessError(deduct_err or '买回扣款失败')

                from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
                destination, grant_err = grant_raw_item(
                    adventurer,
                    snapshot['name'],
                    {
                        'type': snapshot['type'],
                        'rarity': snapshot['rarity'],
                        'quantity': snapshot['quantity'],
                        'uses_left': snapshot.get('uses_left'),
                        'desc': snapshot.get('custom_desc'),
                        'evolution_stage': snapshot.get('evolution_stage', 0),
                        **(
                            {
                                'damage_stage': snapshot.get('damage_stage'),
                                'damage_note': snapshot.get('damage_note'),
                            }
                            if snapshot['source'] == 'carry' else {}
                        ),
                    },
                    source_type=_BUYBACK_REPURCHASE_SOURCE,
                    description='专项申诉从收购商行买回误售物品',
                    force_destination=(
                        'warehouse' if snapshot['source'] == 'warehouse' else None
                    ),
                    equipment_first=snapshot['source'] == 'carry',
                    allow_equipment_overflow=True,
                    register_catalog=False,
                    related_id=str(sale_log.id),
                    related_type='buyback_currency_log',
                    allow_same_name_stack_merge=False,
                )
                if grant_err or destination is None:
                    raise _ProjectBusinessError(grant_err or '物品买回失败')

                charge_log.business_payload = {
                    'schema': 's1_buyback_repurchase_v1',
                    'sale_log_id': int(sale_log.id),
                    'public_name': candidate['name'],
                    'quantity': snapshot['quantity'],
                    'destination': destination,
                    'snapshot_quality': candidate['snapshot_quality'],
                }
                return {
                    'name': candidate['name'],
                    'quantity': snapshot['quantity'],
                    'repurchase_cost': cost,
                    'gold': round(float(new_balance or 0), 2),
                    'destination': destination,
                    'already_redeemed': False,
                    'message': f'已花费 {cost:g} 金币买回「{candidate["name"]}」',
                }, None
        except DatabaseError as exc:
            original = getattr(exc, 'original_error', None)
            if isinstance(original, _ProjectBusinessError):
                return None, str(original)
            current_app.logger.exception('S1 收购商行专项买回失败 sale_log=%s', sale_log_id)
            return None, '买回失败，扣款与物品变更均已回滚'
