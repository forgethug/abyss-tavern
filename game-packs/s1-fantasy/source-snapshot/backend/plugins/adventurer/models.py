from runtime_core.time import utc_now as _portable_utc_now
from datetime import datetime

from flask import current_app
from extensions import db
from utils.time_utils import to_utc_iso

from plugins.adventurer.utils import (  # noqa: F401
    UNKNOWN_TRAIT_DESCRIPTIONS,
    normalize_adventurer_item_name,
    split_name_desc_colon_suffix,
    sanitize_equipment_name,
    is_reserved_adventurer_item_name,
    merge_adventurer_text_fragments,
    split_adventurer_trait_name_suffix,
    sanitize_adventurer_name,
    is_unknown_trait_description,
    resolve_adventurer_trait_description,
    normalize_adventurer_trait_name_and_desc,
    normalize_adventurer_trait_entry,
    normalize_adventurer_traits_payload,
    collapse_redundant_adventurer_equipment_desc,
    merge_adventurer_upgraded_equipment_desc,
    split_adventurer_traits,
)


class Adventurer(db.Model):
    __tablename__ = 'adventurers'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    gender = db.Column(db.String(10), nullable=False, default='未知')
    character_class = db.Column(db.String(50), nullable=False)  # 职业：战士、法师等
    level = db.Column(db.Integer, nullable=False, default=1)
    status = db.Column(db.String(20), nullable=False, default='idle', index=True)  # idle, in_party, exploring, resting, dead
    hp = db.Column(db.Integer, nullable=False, default=100)
    max_hp = db.Column(db.Integer, nullable=False, default=100)
    strength = db.Column(db.Integer, nullable=False, default=10) # 力量
    agility = db.Column(db.Integer, nullable=False, default=10) # 敏捷
    intelligence = db.Column(db.Integer, nullable=False, default=10) # 智力
    luck = db.Column(db.Integer, nullable=False, default=10) # 幸运
    personality = db.Column(db.String(100), nullable=False)  # 性格：勇敢、谨慎等，可逗号分隔
    gold = db.Column(db.Float, nullable=False, default=100.0)  # 拥有的金币
    traits = db.Column(db.JSON, nullable=False, default=list)  # 特质列表
    skills = db.Column(db.JSON, nullable=False, default=list)  # 技能列表，结构：[{"name": "...", "desc": "..."}]
    equipment = db.Column(db.JSON, nullable=False, default=dict)  # 装备道具
    investors = db.Column(db.JSON, nullable=False, default=dict)  # 投资人列表
    icu_fund = db.Column(db.Float, nullable=False, default=0.0)  # ICU治疗基金，上限1000w
    history_story = db.Column(db.Text, nullable=False, default='') # 冒险者历史传记
    recovery_time = db.Column(db.DateTime, nullable=True)  # 受伤恢复完成时间
    exp = db.Column(db.Integer, nullable=False, default=0)  # 经验值
    owner_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True, index=True)  # 绑定的玩家ID
    party_preference = db.Column(db.String(20), nullable=False, default='auto')  # 队伍偏好：auto, idle, rest
    rest_start_time = db.Column(db.DateTime, nullable=True)  # 满血且处于休息偏好的起始时间
    warehouse = db.Column(db.JSON, nullable=False, default=list)  # 仓库
    latest_score = db.Column(db.Float, nullable=True)  # 最近一次探险的评分
    closure_choice = db.Column(db.String(20), nullable=True)  # 闭馆选择：retire / defend / null
    closure_choice_at = db.Column(db.DateTime, nullable=True)  # 闭馆选择写入时间
    closure_resolution = db.Column(db.String(20), nullable=True)  # 闭馆归档结果
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_dict(self, item_dict=None, trait_dict=None, current_user_id=None, is_admin=False, is_followed=None):
        enriched_equipment = {}
        for eq_name, eq_data in (self.equipment or {}).items():
            if is_reserved_adventurer_item_name(eq_name):
                continue
            clean_name = sanitize_equipment_name(eq_name)
            safe_eq_data = dict(eq_data) if isinstance(eq_data, dict) else {}
            enriched_equipment[clean_name] = safe_eq_data
            if item_dict and clean_name in item_dict:
                enriched_equipment[clean_name]['desc'] = collapse_redundant_adventurer_equipment_desc(item_dict[clean_name])
            else:
                enriched_equipment[clean_name]['desc'] = collapse_redundant_adventurer_equipment_desc(
                    safe_eq_data.get('desc')
                )

        sanitized_warehouse = []
        for item in self.warehouse or []:
            if isinstance(item, dict):
                item_name = normalize_adventurer_item_name(item.get('name'))
                if is_reserved_adventurer_item_name(item_name):
                    continue
                sanitized_item = dict(item)
                sanitized_item['name'] = sanitize_equipment_name(item_name)
            else:
                item_name = normalize_adventurer_item_name(item)
                if is_reserved_adventurer_item_name(item_name):
                    continue
                sanitized_item = {
                    'name': sanitize_equipment_name(item_name),
                    'desc': '',
                }
            sanitized_item['desc'] = collapse_redundant_adventurer_equipment_desc(sanitized_item.get('desc'))
            sanitized_warehouse.append(sanitized_item)

        enriched_traits, archived_traits = split_adventurer_traits(self.traits or [], trait_dict)

        if not is_admin:
            enriched_traits = [
                {k: v for k, v in t.items() if k in ('name', 'desc')}
                for t in enriched_traits
            ]

        if is_followed is not None:
            _is_followed = is_followed
        else:
            _is_followed = False
            if current_user_id:
                from db_repository.manager import db_manager
                _is_followed = db_manager.adventurer_follow.is_followed(current_user_id, self.id)

        res = {
            'id': self.id,
            'name': sanitize_adventurer_name(self.name),
            'gender': self.gender,
            'character_class': self.character_class,
            'level': self.level,
            'status': self.status,
            'hp': self.hp,
            'max_hp': self.max_hp,
            'strength': self.strength,
            'agility': self.agility,
            'intelligence': self.intelligence,
            'luck': self.luck,
            'personality': self.personality.split(',') if self.personality else [],
            'gold': self.gold,
            'icu_fund': self.icu_fund,
            'traits': enriched_traits,
            'equipment': enriched_equipment,
            'investors': self.investors,
            'history_story': self.history_story,
            'recovery_time': to_utc_iso(self.recovery_time) if self.recovery_time else None,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'is_followed': _is_followed,
            'owner_id': self.owner_id,
            'party_preference': self.party_preference,
            'rest_start_time': to_utc_iso(self.rest_start_time) if self.rest_start_time else None,
            'warehouse': sanitized_warehouse,
            'warehouse_capacity_limit': int(current_app.config.get('ADVENTURER_WAREHOUSE_CAPACITY', 100) or 100),
            'latest_score': self.latest_score,
            'closure_choice': self.closure_choice,
            'closure_choice_at': to_utc_iso(self.closure_choice_at) if self.closure_choice_at else None,
            'closure_resolution': self.closure_resolution,
        }

        if is_admin:
            res['exp'] = self.exp
            res['skills'] = self.skills
            res['archived_traits'] = archived_traits
            def get_exp_required(level):
                if level >= 10:
                    extra_penalty = (level - 9) * 1000
                    return level * 100 + extra_penalty
                return level * 100

            res['exp_required'] = get_exp_required(self.level)

        return res

class AdventurerParty(db.Model):
    __tablename__ = 'adventurer_parties'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False)
    status = db.Column(db.String(20), nullable=False, default='forming', index=True)  # forming, exploring, disbanded
    target_dungeon = db.Column(db.String(100), nullable=True)
    target_size = db.Column(db.Integer, nullable=False, default=4)
    departure_time = db.Column(db.DateTime, nullable=True)
    sponsor_fund = db.Column(db.Float, nullable=False, default=0.0)
    sponsor_reduced_minutes = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    leader_id = db.Column(db.Integer, db.ForeignKey('adventurers.id', ondelete='SET NULL'), nullable=True)

    __table_args__ = {'extend_existing': True}

    members = db.relationship('Adventurer', secondary='party_members', lazy='selectin',
        backref=db.backref('parties', lazy=True))
    leader = db.relationship('Adventurer', foreign_keys=[leader_id])

    def to_dict(self, item_dict=None, trait_dict=None):
        from plugins.adventurer.services.adventurer_service import DUNGEONS
        dungeon_min_level = 1
        for d in DUNGEONS:
            if d['name'] == self.target_dungeon:
                dungeon_min_level = d['min_level']
                break

        return {
            'id': self.id,
            'name': self.name,
            'status': self.status,
            'target_dungeon': self.target_dungeon,
            'target_dungeon_min_level': dungeon_min_level,
            'target_size': self.target_size,
            'departure_time': to_utc_iso(self.departure_time) if self.departure_time else None,
            'sponsor_fund': self.sponsor_fund,
            'sponsor_reduced_minutes': self.sponsor_reduced_minutes,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'leader_id': self.leader_id,
            'members': [m.to_dict(item_dict=item_dict, trait_dict=trait_dict) for m in self.members]
        }


class PartyMember(db.Model):
    __tablename__ = 'party_members'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    party_id = db.Column(db.Integer, db.ForeignKey('adventurer_parties.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurers.id', ondelete='CASCADE'), nullable=False, index=True)

    __table_args__ = {'extend_existing': True}


class ExpeditionLog(db.Model):
    __tablename__ = 'expedition_logs'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    party_id = db.Column(db.Integer, db.ForeignKey('adventurer_parties.id', ondelete='SET NULL'), nullable=True, index=True)
    party_name = db.Column(db.String(100), nullable=True)
    target_dungeon = db.Column(db.String(100), nullable=False)
    result_summary = db.Column(db.Text, nullable=True)
    full_log = db.Column(db.JSON, nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    __table_args__ = {'extend_existing': True}

    def to_dict(self, is_admin=False):
        safe_full_log = self.full_log
        if not is_admin and isinstance(safe_full_log, dict) and 'member_updates' in safe_full_log:
            import copy
            safe_full_log = copy.deepcopy(self.full_log)
            for update in safe_full_log.get('member_updates', []):
                if 'exp_gain' in update:
                    del update['exp_gain']
                if 'exp' in update:
                    del update['exp']

        return {
            'id': self.id,
            'party_id': self.party_id,
            'party_name': self.party_name,
            'target_dungeon': self.target_dungeon,
            'result_summary': self.result_summary,
            'full_log': safe_full_log,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None
        }


class AdventurerItem(db.Model):
    __tablename__ = 'adventurer_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    type = db.Column(db.String(50), nullable=False)
    description = db.Column(db.Text, nullable=False, default='')
    rarity = db.Column(db.String(20), nullable=False, default='common')
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    shop_item = db.relationship('AdventurerShopItem', back_populates='item', uselist=False, cascade='all, delete-orphan')

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'type': self.type,
            'description': self.description,
            'rarity': self.rarity,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None
        }

class AdventurerShopItem(db.Model):
    __tablename__ = 'adventurer_shop_items'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    item_id = db.Column(db.Integer, db.ForeignKey('adventurer_items.id', ondelete='CASCADE'), nullable=False, unique=True)
    price = db.Column(db.Integer, nullable=False, default=0)
    stock = db.Column(db.Integer, nullable=False, default=0)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    item = db.relationship('AdventurerItem', back_populates='shop_item')

    def to_dict(self):
        display_rarity = self.item.rarity
        desc = self.item.description or ''
        if self.price > 500000 or ('华而不实' in desc or '昂贵' in desc or '毫无用处' in desc):
            display_rarity = 'rare'

        return {
            'id': self.id,
            'item_id': self.item_id,
            'name': self.item.name,
            'type': self.item.type,
            'description': self.item.description,
            'rarity': display_rarity,
            'price': self.price,
            'stock': self.stock,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None
        }

class AdventurerTrait(db.Model):
    __tablename__ = 'adventurer_traits'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    description = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = {'extend_existing': True}

    def to_dict(self):
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None
        }


class AdventurerFollow(db.Model):
    __tablename__ = 'adventurer_follows'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurers.id', ondelete='CASCADE'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'adventurer_id', name='_user_adventurer_uc'),
        {'extend_existing': True},
    )


class AdventurerClaim(db.Model):
    __tablename__ = 'adventurer_claims'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    adventurer_id = db.Column(db.Integer, db.ForeignKey('adventurers.id', ondelete='CASCADE'), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default='pending')
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now)
    reviewed_at = db.Column(db.DateTime, nullable=True)
    reviewed_by = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='SET NULL'), nullable=True)
    reject_reason = db.Column(db.Text, nullable=True)

    __table_args__ = (
        db.UniqueConstraint('user_id', 'adventurer_id', name='_user_adv_claim_uc'),
        {'extend_existing': True},
    )

    def to_dict(self):
        from models.user import User
        user = User.query.session.get(User, self.user_id)
        adv_name = '未知'
        if self.adventurer_id:
            adv = Adventurer.query.session.get(Adventurer, self.adventurer_id)
            if adv:
                adv_name = adv.name

        return {
            'id': self.id,
            'user_id': self.user_id,
            'username': user.username if user else '未知',
            'nickname': user.nickname if user else None,
            'adventurer_id': self.adventurer_id,
            'adventurer_name': adv_name,
            'status': self.status,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
            'reviewed_at': to_utc_iso(self.reviewed_at) if self.reviewed_at else None,
            'reject_reason': self.reject_reason
        }


class AdventurerChatMessage(db.Model):
    __tablename__ = 'adventurer_chat_messages'

    id = db.Column(db.Integer, primary_key=True, autoincrement=True)
    user_id = db.Column(db.Integer, db.ForeignKey('users.id', ondelete='CASCADE'), nullable=False, index=True)
    content = db.Column(db.String(300), nullable=False)
    created_at = db.Column(db.DateTime, nullable=False, default=_portable_utc_now, index=True)

    user = db.relationship('User', backref='adventurer_chat_messages')

    __table_args__ = {'extend_existing': True}

    def to_dict(self):
        return {
            'id': self.id,
            'user_id': self.user_id,
            'nickname': (self.user.nickname or self.user.username) if self.user else '未知',
            'active_title': self.user.active_title if self.user else None,
            'content': self.content,
            'created_at': to_utc_iso(self.created_at) if self.created_at else None,
        }
