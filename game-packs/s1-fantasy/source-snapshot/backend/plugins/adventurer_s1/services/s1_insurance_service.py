"""深渊酒馆 S1 保险服务。"""

from plugins.adventurer_s1.services.s1_player_error_service import (
    player_database_result, player_operation_failure,
)
import logging
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.services.adventurer_s1_config_service import is_s1_special_dungeon

logger = logging.getLogger(__name__)

INJURY_INSURANCE = 'injury'
EMPTY_RETURN_INSURANCE = 'empty_return'
COMRADE_MEMORIAL_INSURANCE = 'comrade_memorial'
NO_GOLD_INSURANCE = 'no_gold'
NO_ITEM_INSURANCE = 'no_item'

_POLICY_SPECS = {
    INJURY_INSURANCE: {
        'name': '受伤保险',
        'active_field': 'insurance_active',
        'premium_config': 'ADVENTURER_S1_INSURANCE_PREMIUM',
        'premium_default': 10,
        'description': '生还时按本次损失生命占最大生命的比例非线性赔付。',
    },
    EMPTY_RETURN_INSURANCE: {
        'name': '空手归途险',
        'active_field': 'empty_return_insurance_active',
        'premium_config': 'ADVENTURER_S1_EMPTY_RETURN_INSURANCE_PREMIUM',
        'premium_default': 100,
        'payout_config': 'ADVENTURER_S1_EMPTY_RETURN_INSURANCE_PAYOUT',
        'payout_default': 500,
        'description': '普通探险中生还，未升级且无正金币、无新物品、无新技能或特质、无职业或属性成长时赔付；基础经验、评分和携带物品的消耗不算收益，特殊机缘副本不赔付。',
    },
    COMRADE_MEMORIAL_INSURANCE: {
        'name': '同袍抚恤险',
        'active_field': 'comrade_memorial_insurance_active',
        'premium_config': 'ADVENTURER_S1_COMRADE_MEMORIAL_INSURANCE_PREMIUM',
        'premium_default': 100,
        'payout_config': 'ADVENTURER_S1_COMRADE_MEMORIAL_INSURANCE_PAYOUT',
        'payout_default': 800,
        'description': '投保者生还且本次至少一名其他队友阵亡时赔付。',
    },
    NO_GOLD_INSURANCE: {
        'name': '零钱兜底险',
        'active_field': 'no_gold_insurance_active',
        'premium_config': 'ADVENTURER_S1_NO_GOLD_INSURANCE_PREMIUM',
        'premium_default': 10,
        'payout_config': 'ADVENTURER_S1_NO_GOLD_INSURANCE_PAYOUT',
        'payout_default': 50,
        'description': '普通副本中没有获得正金币收益时赔付；特殊机缘副本不赔付。',
    },
    NO_ITEM_INSURANCE: {
        'name': '空囊补偿险',
        'active_field': 'no_item_insurance_active',
        'premium_config': 'ADVENTURER_S1_NO_ITEM_INSURANCE_PREMIUM',
        'premium_default': 10,
        'payout_config': 'ADVENTURER_S1_NO_ITEM_INSURANCE_PAYOUT',
        'payout_default': 50,
        'description': '普通副本中没有获得任何物品或材料时赔付；特殊机缘副本不赔付。',
    },
}

_DIRECT_GROWTH_FIELDS = (
    'strength_increase',
    'agility_increase',
    'intelligence_increase',
    'luck_increase',
    'max_hp_increase',
)
_COLLECTION_GROWTH_FIELDS = ('new_equipments', 'new_skills', 'new_traits')


def _config_float(key: str, default: float) -> float:
    return float(current_app.config.get(key, default))


def _premium() -> float:
    return _config_float('ADVENTURER_S1_INSURANCE_PREMIUM', 10)


def _max_payout() -> float:
    return _config_float('ADVENTURER_S1_INSURANCE_MAX_PAYOUT', 500)


def _payout_exponent() -> float:
    return _config_float('ADVENTURER_S1_INSURANCE_PAYOUT_EXPONENT', 2.5)


def _policy_premium(spec: dict) -> float:
    return _config_float(spec['premium_config'], spec['premium_default'])


def _policy_payout(spec: dict) -> int | None:
    key = spec.get('payout_config')
    if not key:
        return None
    return int(round(_config_float(key, spec['payout_default'])))


def calc_payout(loss_pct: float) -> int:
    """非线性赔付：payout = max_payout * (loss_pct / 100) ^ exponent。"""
    if loss_pct <= 0:
        return 0
    return int(round(_max_payout() * (min(loss_pct, 100) / 100) ** _payout_exponent()))


def has_effective_growth(
    member_update: dict | None, *, level_before: int, level_after: int,
    settlement_member_receipt: dict | None = None,
) -> bool:
    """仅按结构化结算字段判断是否有有效成长；基础经验、评分和好感变化不计。"""
    if level_after > level_before:
        return True
    update = member_update if isinstance(member_update, dict) else {}
    for field in _DIRECT_GROWTH_FIELDS:
        try:
            if float(update.get(field, 0) or 0) > 0:
                return True
        except (TypeError, ValueError):
            continue
    if str(update.get('new_class') or '').strip():
        return True
    for field in _COLLECTION_GROWTH_FIELDS:
        if not update.get(field):
            continue
        if (
            field == 'new_traits'
            and isinstance(settlement_member_receipt, dict)
            and settlement_member_receipt.get('status') == 'passed'
            and 'traits' in settlement_member_receipt.get('checked', [])
            and isinstance(settlement_member_receipt.get('asset_mutations'), list)
        ):
            # new_traits 同时承载同名更新；原样回传没有新增或变化，不能拒赔。
            if not any(
                isinstance(mutation, dict)
                and mutation.get('asset_type') == 'trait'
                and mutation.get('operation') in ('created', 'updated')
                for mutation in settlement_member_receipt['asset_mutations']
            ):
                continue
        return True
    return False


class S1InsuranceService:
    """可叠加保单的购买、状态查询与探险结算。"""

    @staticmethod
    def _ensure_insurance_shop_built() -> Optional[str]:
        from plugins.adventurer_s1.models import (
            AdventurerS1BuildingInstance,
            AdventurerS1BuildingTemplate,
        )

        exists = (
            AdventurerS1BuildingInstance.query
            .join(AdventurerS1BuildingTemplate)
            .filter(
                AdventurerS1BuildingTemplate.handler_key == 'insurance_shop',
                AdventurerS1BuildingTemplate.is_active.is_(True),
                AdventurerS1BuildingInstance.status == 'built',
            )
            .first()
        )
        return None if exists else '保险局尚未建成'

    @staticmethod
    def _serialize_policy(adventurer, code: str, spec: dict) -> dict:
        payload = {
            'code': code,
            'name': spec['name'],
            'description': spec['description'],
            'premium': _policy_premium(spec),
            'active': bool(getattr(adventurer, spec['active_field'], False)),
        }
        payout = _policy_payout(spec)
        if payout is not None:
            payload['payout'] = payout
        if code == INJURY_INSURANCE:
            payload['max_payout'] = int(_max_payout())
            payload['payout_samples'] = [
                {'loss_pct': pct, 'payout': calc_payout(pct), 'label': f'损失{pct}%'}
                for pct in (30, 50, 70, 90, 100)
            ]
        return payload

    @staticmethod
    def get_status(user_id: int, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        from plugins.adventurer_s1.models import AdventurerS1

        built_err = S1InsuranceService._ensure_insurance_shop_built()
        if built_err:
            return None, built_err
        adv = AdventurerS1.query.session.get(AdventurerS1, adventurer_id)
        if not adv:
            return None, '冒险者不存在'
        if adv.owner_id != user_id:
            return None, '只能查看自己的冒险者保险状态'

        policies = [
            S1InsuranceService._serialize_policy(adv, code, spec)
            for code, spec in _POLICY_SPECS.items()
        ]
        injury = policies[0]
        return {
            'adventurer_id': adv.id,
            'policies': policies,
            # 兼容旧前端字段。
            'insurance_active': injury['active'],
            'premium': injury['premium'],
            'max_payout': injury['max_payout'],
            'payout_exponent': _payout_exponent(),
            'payout_samples': injury['payout_samples'],
        }, None

    @staticmethod
    def buy_insurance(
        user_id: int,
        adventurer_id: int,
        insurance_type: str = INJURY_INSURANCE,
    ) -> Tuple[bool, Optional[str]]:
        """购买指定保单；不同保单允许同时生效，同一保单不可重复购买。"""
        from plugins.adventurer_s1.models import AdventurerS1
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
        from plugins.adventurer_s1.services.adventurer_s1_service import AdventurerS1Service

        spec = _POLICY_SPECS.get(str(insurance_type or '').strip())
        if not spec:
            return False, '未知的保险类型'
        try:
            built_err = S1InsuranceService._ensure_insurance_shop_built()
            if built_err:
                return False, built_err

            adv = AdventurerS1.query.filter_by(id=adventurer_id).with_for_update().first()
            if not adv:
                return False, '冒险者不存在'
            if adv.owner_id != user_id:
                return False, '只能为自己的冒险者购买保险'
            if adv.status not in AdventurerS1Service.INVENTORY_EDITABLE_STATUSES:
                if adv.status == 'exploring':
                    return False, '探险中无法购买保险'
                if adv.status == 'building':
                    return False, '执行任务中无法购买保险'
                return False, '当前状态无法购买保险'
            if bool(getattr(adv, spec['active_field'], False)):
                return False, f"该冒险者已有有效的{spec['name']}，无需重复购买"

            premium = _policy_premium(spec)
            _, err = S1CurrencyService.deduct(
                adv.id, 'gold', premium,
                source_type='insurance_premium',
                related_type='insurance_policy',
                description=f"购买{spec['name']}（{premium:g} 金币）",
                auto_commit=False,
            )
            if err:
                db_manager.rollback()
                return False, f'扣款失败: {err}'

            setattr(adv, spec['active_field'], True)
            ok, err = player_database_result(db_manager.commit())
            if not ok:
                return False, err
            logger.info('冒险者 %s 购买%s成功（扣 %s 金币）', adv.id, spec['name'], premium)
            return True, None
        except Exception as exc:
            db_manager.rollback()
            return False, player_operation_failure(exc, '操作失败')

    @staticmethod
    def _grant_payout(
        adventurer,
        *,
        amount: int,
        policy_name: str,
        source_type: str,
        description: str,
        expedition_log_id: int | None,
    ) -> Optional[str]:
        from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService

        adventurer_id = int(adventurer.id)
        _, err = S1CurrencyService.add(
            adventurer_id, 'gold', float(amount),
            source_type=source_type,
            related_id=expedition_log_id,
            related_type='expedition_log',
            description=description,
            auto_commit=False,
        )
        if err:
            # add/flush 失败后 ORM 对象可能已过期，日志不得再次触发属性懒加载，
            # 否则会用 PendingRollbackError 掩盖真正的数据库错误。
            logger.warning('%s赔付失败 adv=%s err=%s', policy_name, adventurer_id, err)
        return err

    @staticmethod
    def settle_insurance(
        adventurer,
        hp_before: int,
        hp_after: int,
        *,
        member_update: dict | None = None,
        level_before: int | None = None,
        level_after: int | None = None,
        teammate_died: bool = False,
        target_dungeon: str | None = None,
        expedition_log_id: int | None = None,
        settlement_member_receipt: dict | None = None,
    ) -> Tuple[float, Optional[str]]:
        """消费本次适用的有效保单，并按结构化结算结果分别理赔。"""
        active = {
            code: bool(getattr(adventurer, spec['active_field'], False))
            for code, spec in _POLICY_SPECS.items()
        }
        if not any(active.values()):
            return 0, None
        empty_return_excluded = is_s1_special_dungeon(target_dungeon)
        for spec in _POLICY_SPECS.values():
            setattr(adventurer, spec['active_field'], False)

        alive = hp_after > 0
        total_payout = 0

        if active[INJURY_INSURANCE] and alive:
            max_hp = int(adventurer.max_hp or 100)
            if max_hp > 0:
                loss_pct = (max(0, hp_before - hp_after) / max_hp) * 100
                payout = calc_payout(loss_pct)
                if payout > 0:
                    err = S1InsuranceService._grant_payout(
                        adventurer,
                        amount=payout,
                        policy_name='受伤保险',
                        source_type='insurance_payout',
                        description=f'受伤保险赔付（损失{loss_pct:.0f}%，赔付{payout}金币）',
                        expedition_log_id=expedition_log_id,
                    )
                    if err:
                        return float(total_payout), err
                    total_payout += payout

        before_level = int(level_before if level_before is not None else adventurer.level or 1)
        after_level = int(level_after if level_after is not None else adventurer.level or 1)
        gold_change = 0.0
        if isinstance(member_update, dict):
            try:
                gold_change = float(member_update.get('gold_change', 0) or 0)
            except (TypeError, ValueError):
                gold_change = 0.0
        empty_return = (
            alive
            and gold_change <= 0
            and not has_effective_growth(
                member_update,
                level_before=before_level,
                level_after=after_level,
                settlement_member_receipt=settlement_member_receipt,
            )
        )
        if active[EMPTY_RETURN_INSURANCE] and not empty_return_excluded and empty_return:
            payout = _policy_payout(_POLICY_SPECS[EMPTY_RETURN_INSURANCE]) or 0
            err = S1InsuranceService._grant_payout(
                adventurer,
                amount=payout,
                policy_name='空手归途险',
                source_type='empty_return_insurance_payout',
                description=f'空手归途险赔付（本次生还但无有效收益或成长，赔付{payout}金币）',
                expedition_log_id=expedition_log_id,
            )
            if err:
                return float(total_payout), err
            total_payout += payout

        if active[COMRADE_MEMORIAL_INSURANCE] and alive and teammate_died:
            payout = _policy_payout(_POLICY_SPECS[COMRADE_MEMORIAL_INSURANCE]) or 0
            err = S1InsuranceService._grant_payout(
                adventurer,
                amount=payout,
                policy_name='同袍抚恤险',
                source_type='comrade_memorial_payout',
                description=f'同袍抚恤险赔付（本次有队友阵亡，赔付{payout}金币）',
                expedition_log_id=expedition_log_id,
            )
            if err:
                return float(total_payout), err
            total_payout += payout

        if active[NO_GOLD_INSURANCE] and not empty_return_excluded and gold_change <= 0:
            payout = _policy_payout(_POLICY_SPECS[NO_GOLD_INSURANCE]) or 0
            err = S1InsuranceService._grant_payout(
                adventurer,
                amount=payout,
                policy_name='零钱兜底险',
                source_type='no_gold_insurance_payout',
                description=f'零钱兜底险赔付（本次无正金币收益，赔付{payout}金币）',
                expedition_log_id=expedition_log_id,
            )
            if err:
                return float(total_payout), err
            total_payout += payout

        update = member_update if isinstance(member_update, dict) else {}
        if (
            active[NO_ITEM_INSURANCE]
            and not empty_return_excluded
            and not bool(update.get('new_equipments'))
        ):
            payout = _policy_payout(_POLICY_SPECS[NO_ITEM_INSURANCE]) or 0
            err = S1InsuranceService._grant_payout(
                adventurer,
                amount=payout,
                policy_name='空囊补偿险',
                source_type='no_item_insurance_payout',
                description=f'空囊补偿险赔付（本次未获得物品或材料，赔付{payout}金币）',
                expedition_log_id=expedition_log_id,
            )
            if err:
                return float(total_payout), err
            total_payout += payout

        logger.info('冒险者 %s 本次保险合计赔付 %s 金币', adventurer.id, total_payout)
        return float(total_payout), None
