"""深渊酒馆 S1 个人页支持投票服务。"""

from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import json
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import joinedload

from db_repository import db_manager
from extensions import db
from plugins.adventurer_s1.models import AdventurerS1, AdventurerS1Settings, AdventurerS1SupportVote
from utils.time_utils import beijing_str_to_utc, to_utc_iso


_SUPPORT_VOTE_CONFIG_KEY = 'support_vote_current'


class AdventurerS1SupportVoteService:
    """管理 S1 个人页支持投票；有存活冒险者的玩家可随时改票。"""

    @staticmethod
    def _parse_deadline(value) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value.replace(tzinfo=None)
        text = str(value).strip()
        if 'Z' not in text and '+' not in text:
            return beijing_str_to_utc(text)
        try:
            return datetime.fromisoformat(text.replace('Z', '+00:00')).replace(tzinfo=None)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _get_config_row() -> AdventurerS1Settings | None:
        return AdventurerS1Settings.query.filter_by(key=_SUPPORT_VOTE_CONFIG_KEY).first()

    @staticmethod
    def _empty_config() -> dict:
        return {
            'title': '',
            'content': '',
            'options': [],
            'deadline': None,
            'version': None,
            'is_active': False,
            'is_expired': False,
        }

    @staticmethod
    def _load_config() -> dict:
        row = AdventurerS1SupportVoteService._get_config_row()
        if not row or not row.value:
            return AdventurerS1SupportVoteService._empty_config()
        try:
            raw = json.loads(row.value)
        except (TypeError, ValueError):
            return AdventurerS1SupportVoteService._empty_config()

        options = []
        for idx, item in enumerate(raw.get('options') or [], start=1):
            if isinstance(item, dict):
                label = str(item.get('label') or '').strip()
                description = str(item.get('description') or '').strip()
            else:
                label = str(item or '').strip()
                description = ''
            if not label:
                continue
            options.append({
                'key': f'opt_{idx}',
                'label': label,
                'description': description,
            })

        deadline = AdventurerS1SupportVoteService._parse_deadline(raw.get('deadline'))
        is_expired = bool(deadline and deadline <= _portable_utc_now())
        title = str(raw.get('title') or '').strip()
        content = str(raw.get('content') or '').strip()
        is_active = bool(title and options and not is_expired)
        version = raw.get('version') or (to_utc_iso(row.updated_at) if row.updated_at else None)

        return {
            'title': title,
            'content': content,
            'options': options,
            'deadline': to_utc_iso(deadline) if deadline else None,
            'version': version,
            'is_active': is_active,
            'is_expired': is_expired,
        }

    @staticmethod
    def _active_owned_adventurer(user_id: int) -> AdventurerS1 | None:
        return (
            AdventurerS1.query
            .filter(
                AdventurerS1.owner_id == user_id,
                AdventurerS1.is_player_created == True,
                AdventurerS1.status.notin_(AdventurerS1.TAVERN_EXCLUDED_STATUSES),
            )
            .order_by(AdventurerS1.id)
            .first()
        )

    @staticmethod
    def get_status(user_id: int, *, is_admin: bool = False) -> tuple[dict, str | None]:
        config = AdventurerS1SupportVoteService._load_config()
        vote_count = AdventurerS1SupportVote.query.count()
        my_vote = AdventurerS1SupportVote.query.filter_by(user_id=user_id).first()
        can_vote = AdventurerS1SupportVoteService._active_owned_adventurer(user_id) is not None
        options = list(config['options']) if can_vote and config['is_active'] else []
        my_option_key = my_vote.option_key if my_vote and config['is_active'] else None
        return {
            'title': config['title'],
            'content': config['content'],
            'deadline': config['deadline'],
            'version': config['version'],
            'is_active': config['is_active'],
            'is_expired': config['is_expired'],
            'has_voted': my_vote is not None,
            'can_vote': can_vote,
            'my_option_key': my_option_key,
            'options': options,
            'vote_count': vote_count if vote_count > 0 else None,
            'admin_vote_count': vote_count if is_admin and vote_count > 0 else None,
        }, None

    @staticmethod
    def cast_vote(user_id: int, option_key: str) -> tuple[dict | None, str | None]:
        option_key = (option_key or '').strip()
        config = AdventurerS1SupportVoteService._load_config()
        if not config['is_active']:
            return None, '当前没有可参与的投票'
        option_keys = {item['key'] for item in config['options']}
        if option_key not in option_keys:
            return None, '投票选项不存在或已关闭'

        adv = AdventurerS1SupportVoteService._active_owned_adventurer(user_id)
        if not adv:
            return None, '需要拥有存活冒险者才可以投票'

        vote = AdventurerS1SupportVote.query.filter_by(user_id=user_id).first()
        if vote:
            vote.adventurer_id = adv.id
            vote.option_key = option_key
        else:
            vote = AdventurerS1SupportVote(user_id=user_id, adventurer_id=adv.id, option_key=option_key)
            db_manager.add(vote)
        ok, err = db_manager.commit()
        if not ok:
            db_manager.rollback()
            return None, err or '投票失败'
        return AdventurerS1SupportVoteService.get_status(user_id, is_admin=False)

    @staticmethod
    def get_admin_stats() -> tuple[dict, str | None]:
        config = AdventurerS1SupportVoteService._load_config()
        vote_count = AdventurerS1SupportVote.query.count()
        rows = (
            AdventurerS1SupportVote.query
            .with_entities(AdventurerS1SupportVote.option_key, func.count(AdventurerS1SupportVote.id))
            .group_by(AdventurerS1SupportVote.option_key)
            .all()
        ) if vote_count > 0 else []
        counts = {key: count for key, count in rows}
        if vote_count > 0:
            known_keys = {item['key'] for item in config['options']}
            option_counts = [
                {'option_key': item['key'], 'label': item['label'], 'count': counts.get(item['key'], 0)}
                for item in config['options']
            ]
            option_counts.extend(
                {'option_key': key, 'label': key, 'count': count}
                for key, count in counts.items()
                if key not in known_keys
            )
        else:
            option_counts = [
                {'option_key': item['key'], 'label': item['label'], 'count': 0}
                for item in config['options']
            ]
        recent_votes = (
            AdventurerS1SupportVote.query
            .options(
                joinedload(AdventurerS1SupportVote.user),
                joinedload(AdventurerS1SupportVote.adventurer),
            )
            .order_by(AdventurerS1SupportVote.created_at.desc(), AdventurerS1SupportVote.id.desc())
            .limit(50)
            .all()
        ) if vote_count > 0 else []
        return {
            'current_vote': config,
            'vote_count': vote_count if vote_count > 0 else None,
            'option_counts': option_counts,
            'recent_votes': [v.to_admin_dict() for v in recent_votes],
        }, None

    @staticmethod
    def admin_set_current_vote(payload: dict, *, admin_user_id: int | None = None) -> tuple[dict | None, str | None]:
        clear = bool(payload.get('clear'))
        title = (payload.get('title') or '').strip()
        content = (payload.get('content') or '').strip()
        deadline_raw = payload.get('deadline')
        raw_options = payload.get('options') or []

        if clear:
            try:
                with db_manager.transaction():
                    row = AdventurerS1SupportVoteService._get_config_row()
                    if row:
                        row.value = ''
                        row.updated_by_user_id = admin_user_id
                        row.updated_at = _portable_utc_now()
                    AdventurerS1SupportVote.query.delete(synchronize_session=False)
            except Exception as e:
                return None, f'清空投票失败：{e}'
            return AdventurerS1SupportVoteService.get_admin_stats()

        if not title:
            return None, '投票标题不能为空'
        if len(title) > 100:
            return None, '投票标题不能超过 100 字'
        if len(content) > 1000:
            return None, '投票内容不能超过 1000 字'
        if not isinstance(raw_options, list):
            return None, '投票选项格式不正确'

        options: list[dict] = []
        for item in raw_options:
            if isinstance(item, dict):
                label = (item.get('label') or '').strip()
                description = (item.get('description') or '').strip()
            else:
                label = str(item or '').strip()
                description = ''
            if not label:
                continue
            if len(label) > 100:
                return None, '投票选项不能超过 100 字'
            if len(description) > 300:
                return None, '投票选项说明不能超过 300 字'
            options.append({'label': label, 'description': description})
        if not options:
            return None, '至少需要 1 个投票选项'
        if len(options) > 20:
            return None, '投票选项不能超过 20 个'

        deadline = AdventurerS1SupportVoteService._parse_deadline(deadline_raw)
        if deadline_raw and not deadline:
            return None, '截止日期格式无效'
        if deadline and deadline <= _portable_utc_now():
            return None, '截止日期必须晚于当前时间'

        now = _portable_utc_now()
        config = {
            'title': title,
            'content': content,
            'options': options,
            'deadline': to_utc_iso(deadline) if deadline else None,
            'version': to_utc_iso(now),
        }
        try:
            with db_manager.transaction():
                row = AdventurerS1SupportVoteService._get_config_row()
                value = json.dumps(config, ensure_ascii=False)
                if row:
                    row.value = value
                    row.description = '深渊酒馆 S1 当前个人页投票配置'
                    row.updated_by_user_id = admin_user_id
                    row.updated_at = now
                else:
                    db_manager.add(AdventurerS1Settings(
                        key=_SUPPORT_VOTE_CONFIG_KEY,
                        value=value,
                        description='深渊酒馆 S1 当前个人页投票配置',
                        updated_by_user_id=admin_user_id,
                        updated_at=now,
                    ))
                AdventurerS1SupportVote.query.delete(synchronize_session=False)
        except Exception as e:
            return None, f'保存投票失败：{e}'

        return AdventurerS1SupportVoteService.get_admin_stats()
