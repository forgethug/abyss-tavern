"""S1 冒险者职业（character_class）管理员定向修复。

历史 bug：探险 AI 在正文写明某成员转职/进阶成功，却漏填 member_updates.new_class，
结算 ``apply_update_to_s1_adventurer`` 拿不到 new_class，character_class 仍为空——玩家
看到剧情转职成功、数据却没有职业。根因已在生成 / 剧情审核（Observer 可经
override_fields 补正 new_class）Prompt 补落账闭环；本服务订正存量受影响冒险者，不裸改库。

职业丢失没有结构化证据（new_class 没输出 = 审计表也没有职业变更操作），无法像代号还原那样
自动重建，因此设计为**管理员定向单个修复**：
- :meth:`preview`  —— 只读，返回冒险者当前职业 + 传记 history_story 尾段（转职剧情写在
                       传记里），供管理员核对该补什么职业。
- :meth:`repair`   —— 行锁该冒险者，备份原 character_class 后写入新职业；幂等。
- :meth:`restore`  —— 把所有被本服务改过的冒险者职业回退到修复前原值，并清空备份。

备份存 :class:`AdventurerS1Settings`（key=``class_repair_backup``）——不新建表。
"""
from runtime_core.time import utc_now as _portable_utc_now
import json
import re
from datetime import datetime
from typing import Optional, Tuple

from flask import current_app

from db_repository import db_manager
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Settings

_BACKUP_KEY = 'class_repair_backup'
# 职业名：2~7 个中文或英文字母（与生成 prompt「2~6 字」一致，留 1 字容错），
# 防止管理员误把描述 / 整句话填进职业字段。
_CLASS_NAME_RE = re.compile(r'^[一-鿿a-zA-Z]{2,7}$')
_HISTORY_TAIL_CHARS = 1500


class S1ClassRepairService:
    """管理员定向补 / 改冒险者职业，备份原值可一键还原。"""

    @staticmethod
    def _normalize_class(value) -> Optional[str]:
        if not isinstance(value, str):
            return None
        v = value.strip()
        return v if _CLASS_NAME_RE.match(v) else None

    @staticmethod
    def _load_backup() -> dict:
        rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
        if not rec or not rec.value:
            return {'entries': []}
        try:
            data = json.loads(rec.value)
        except (json.JSONDecodeError, TypeError):
            return {'entries': []}
        if not isinstance(data, dict) or not isinstance(data.get('entries'), list):
            return {'entries': []}
        return data

    @classmethod
    def preview(cls, adventurer_id: int) -> Tuple[Optional[dict], Optional[str]]:
        """只读：返回该冒险者当前职业 + 传记尾段，供管理员核对该补什么职业。"""
        try:
            adv = (
                AdventurerS1.query
                .with_entities(
                    AdventurerS1.id, AdventurerS1.name, AdventurerS1.character_class,
                    AdventurerS1.level, AdventurerS1.status, AdventurerS1.owner_id,
                    AdventurerS1.history_story,
                )
                .filter(AdventurerS1.id == adventurer_id)
                .first()
            )
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 职业修复 preview 失败 adv=%s', adventurer_id)
            return None, f'预览失败：{e}'
        if adv is None:
            return None, '冒险者不存在'
        story = adv.history_story or ''
        tail = story[-_HISTORY_TAIL_CHARS:]
        backup = cls._load_backup()
        backed = next(
            (e for e in backup['entries'] if e.get('adventurer_id') == adv.id), None)
        return {
            'id': adv.id,
            'name': adv.name,
            'current_class': adv.character_class,
            'level': adv.level,
            'status': adv.status,
            'owner_id': adv.owner_id,
            'history_story_tail': tail,
            'history_truncated': len(story) > len(tail),
            'backed_up_old_class': backed.get('old_class') if backed else None,
            'has_backup': bool(backup['entries']),
        }, None

    @classmethod
    def repair(cls, adventurer_id: int, new_class: str,
               updated_by_user_id: Optional[int] = None) -> Tuple[Optional[dict], Optional[str]]:
        """给指定冒险者写入职业；先备份原 character_class 可还原。幂等。"""
        norm = cls._normalize_class(new_class)
        if norm is None:
            return None, '职业名不合法：需为 2~7 个中文或英文字母，且不含空格 / 标点'
        try:
            with db_manager.transaction():
                adv = (
                    AdventurerS1.query
                    .with_for_update()
                    .filter(AdventurerS1.id == adventurer_id)
                    .first()
                )
                if adv is None:
                    return None, '冒险者不存在'
                old_class = adv.character_class
                if old_class == norm:
                    return {
                        'changed': False, 'id': adv.id, 'name': adv.name,
                        'current_class': old_class, 'message': '当前职业已是目标值',
                    }, None

                # 备份：保留该冒险者最初 old_class（多次修复不覆盖最初真实值）
                data = cls._load_backup()
                entries = data['entries']
                existing = next(
                    (e for e in entries if e.get('adventurer_id') == adv.id), None)
                now_iso = _portable_utc_now().isoformat() + 'Z'
                if existing is None:
                    entries.append({
                        'adventurer_id': adv.id,
                        'name': adv.name,
                        'old_class': old_class,
                        'new_class': norm,
                        'at': now_iso,
                    })
                else:
                    existing['new_class'] = norm
                    existing['at'] = now_iso
                payload = json.dumps(data, ensure_ascii=False)
                rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if rec is None:
                    db_manager.add(AdventurerS1Settings(
                        key=_BACKUP_KEY,
                        value=payload,
                        description='冒险者职业修复前备份（character_class 原值，供一键还原）',
                        updated_by_user_id=updated_by_user_id,
                    ))
                else:
                    rec.value = payload
                    rec.updated_by_user_id = updated_by_user_id

                adv.character_class = norm
                result = {
                    'changed': True, 'id': adv.id, 'name': adv.name,
                    'old_class': old_class, 'new_class': norm,
                }
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 职业修复写库失败 adv=%s', adventurer_id)
            return None, f'修复失败：{e}'

        current_app.logger.info(
            'S1 职业修复：冒险者 %s 职业 %r → %r', adventurer_id, old_class, norm)
        return result, None

    @classmethod
    def restore(cls) -> Tuple[Optional[dict], Optional[str]]:
        """把所有被本服务改过的冒险者职业回退到修复前原值，并清空备份。"""
        data = cls._load_backup()
        entries = data.get('entries') or []
        if not entries:
            return None, '没有可用的职业修复备份'
        id_to_old = {
            int(e['adventurer_id']): e.get('old_class')
            for e in entries if e.get('adventurer_id') is not None
        }
        try:
            with db_manager.transaction():
                advs = (
                    AdventurerS1.query
                    .with_for_update()
                    .filter(AdventurerS1.id.in_(list(id_to_old.keys())))
                    .all()
                )
                restored = 0
                for adv in advs:
                    adv.character_class = id_to_old.get(adv.id)
                    restored += 1
                rec = AdventurerS1Settings.query.filter_by(key=_BACKUP_KEY).first()
                if rec is not None:
                    rec.value = json.dumps({'entries': []}, ensure_ascii=False)
        except Exception as e:  # noqa: BLE001
            current_app.logger.exception('S1 职业修复还原失败')
            return None, f'还原失败：{e}'
        current_app.logger.info('S1 职业修复还原完成：回退 %s 个冒险者', restored)
        return {'restored': restored}, None
