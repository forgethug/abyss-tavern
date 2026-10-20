"""Frozen expedition roster, escrow, public rolls, and atomic physical delivery."""
from __future__ import annotations
from runtime_core.time import utc_now as _portable_utc_now

import copy
import json
import logging
import re
import secrets
from contextlib import contextmanager
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from hashlib import sha256

from flask import current_app

from db_repository import db_manager
from db_repository.exceptions import DatabaseError
from db_repository.s1_team_loot_repository import S1TeamLootRepository as Repository
from plugins.adventurer_s1.models import AdventurerS1
from plugins.adventurer_s1.s1_item_types import companion_allows_duplicate_name_instances
from plugins.adventurer_s1.team_loot_models import AdventurerS1TeamLootLot
from plugins.adventurer_s1.services.adventurer_s1_currency_service import S1CurrencyService
from plugins.adventurer_s1.services.s1_item_grant_service import grant_raw_item
from plugins.adventurer_s1.services.s1_team_loot_contract import (
    TEAM_LOOT_FIELD, normalize_team_loot, team_loot_source_key, team_loot_registration_hash,
)
from utils.time_utils import to_utc_iso

logger = logging.getLogger(__name__)
TEAM_LOOT_REVIEW_RECEIPT = '_team_loot_review_receipt'


class TeamLootError(ValueError):
    pass


@contextmanager
def _transaction():
    try:
        with db_manager.transaction():
            yield
    except DatabaseError as error:
        if isinstance(error.original_error, TeamLootError):
            raise error.original_error from error
        raise


def team_loot_hash(value) -> str:
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                             separators=(',', ':')).encode('utf-8')).hexdigest()


def build_team_loot_review_receipt(items: list, events: list) -> dict:
    return {'version': 1, 'event_count': len(events), 'items_hash': team_loot_hash(items),
            'events_hash': team_loot_hash(events)}


def team_loot_appeal_source_hash(full_log: dict) -> str:
    return team_loot_hash({'events': full_log.get('events') or [], 'roster': [
        {key: row.get(key) for key in ('adventurer_id', 'adventurer_name', 'owner_id')}
        for row in full_log.get('audit_member_snapshot') or [] if isinstance(row, dict)
    ]})


def _roster_hash(rows: list) -> str:
    return team_loot_hash([{key: row.get(key) for key in
                           ('adventurer_id', 'adventurer_name', 'owner_id')}
                          for row in rows if isinstance(row, dict)])


def _source_hash(items: list, events: list) -> str:
    return team_loot_hash({'items': items, 'events': events})


def authorize_team_loot_original_release(candidate: dict, original: dict, *,
                                         roster: list, log_id: int, attempt: int):
    """Called only by the existing original-story release owner after validation.

    It grants mechanical original-proposal authority, never Observer approval.
    Failed Observer overrides must have no influence on the stock or its events.
    """
    rows, events = candidate.get(TEAM_LOOT_FIELD) or [], candidate.get('events') or []
    if not rows:
        return
    _, errors = normalize_team_loot(rows, events=events)
    if errors or _source_hash(rows, events) != _source_hash(
            original.get(TEAM_LOOT_FIELD) or [], original.get('events') or []):
        raise TeamLootError('原稿共同实物或事件已变化，不能签发故障放行凭证')
    candidate['audit_member_snapshot'] = copy.deepcopy(roster)
    candidate[TEAM_LOOT_REVIEW_RECEIPT] = {
        **build_team_loot_review_receipt(rows, events), 'version': 2,
        'authority_kind': 'original_story_release', 'expedition_log_id': log_id,
        'attempt': attempt, 'event_index_offset': 0, 'roster_hash': _roster_hash(roster),
        'source_hash': _source_hash(rows, events), 'original_source_hash': _source_hash(rows, events),
    }


def capture_team_loot_review(full_log: dict) -> dict | None:
    receipt = full_log.get(TEAM_LOOT_REVIEW_RECEIPT)
    if not isinstance(receipt, dict):
        return None
    count = receipt.get('event_count')
    events = full_log.get('events') or []
    if type(count) is not int or count < 1 or count > len(events):
        return None
    rows = full_log.get(TEAM_LOOT_FIELD) or []
    expected = build_team_loot_review_receipt(rows, events[:count])
    if receipt.get('version') == 2:
        offset = receipt.get('event_index_offset')
        if (receipt.get('authority_kind') != 'original_story_release'
                or type(offset) is not int or offset < 0 or offset >= count
                or type(receipt.get('expedition_log_id')) is not int or receipt['expedition_log_id'] <= 0
                or type(receipt.get('attempt')) is not int or receipt['attempt'] <= 0
                or not isinstance(receipt.get('original_source_hash'), str)):
            return None
        original_rows = copy.deepcopy(rows)
        for row in original_rows:
            if (not isinstance(row, dict) or type(row.get('source_event_index')) is not int
                    or row['source_event_index'] < offset
                    or any(type(i) is not int or i < offset for i in row.get('evidence_event_indexes') or [])):
                return None
            row['source_event_index'] -= offset
            row['evidence_event_indexes'] = [i - offset for i in row['evidence_event_indexes']]
        expected.update({
            'version': 2, 'authority_kind': 'original_story_release',
            'expedition_log_id': receipt['expedition_log_id'], 'attempt': receipt['attempt'],
            'event_index_offset': offset,
            'roster_hash': _roster_hash(full_log.get('audit_member_snapshot') or []),
            'source_hash': _source_hash(original_rows, events[offset:count]),
            'original_source_hash': receipt['original_source_hash'],
        })
    return copy.deepcopy(receipt) if receipt == expected else None


def refresh_team_loot_review_after_name_mapping(full_log: dict, captured: dict | None):
    """Only exact member-ID name restoration may carry a previously valid proof."""
    if captured is not None:
        refreshed = build_team_loot_review_receipt(
            full_log.get(TEAM_LOOT_FIELD) or [], (full_log.get('events') or [])[:captured['event_count']],
        )
        if captured.get('version') == 2:
            offset = captured['event_index_offset']
            rows = copy.deepcopy(full_log.get(TEAM_LOOT_FIELD) or [])
            for row in rows:
                row['source_event_index'] -= offset
                row['evidence_event_indexes'] = [i - offset for i in row['evidence_event_indexes']]
            refreshed.update({key: captured[key] for key in (
                'version', 'authority_kind', 'expedition_log_id', 'attempt',
                'event_index_offset', 'original_source_hash',
            )})
            refreshed['roster_hash'] = _roster_hash(full_log.get('audit_member_snapshot') or [])
            refreshed['source_hash'] = _source_hash(rows, full_log['events'][offset:captured['event_count']])
        full_log[TEAM_LOOT_REVIEW_RECEIPT] = refreshed


def restore_team_loot_authority_after_postprocessing(target: dict, source: dict, *, roster: list):
    """Carry valid internal authority across auxiliary frozen-context stripping."""
    if not source.get(TEAM_LOOT_FIELD) and not target.get(TEAM_LOOT_FIELD):
        return
    captured = capture_team_loot_review(source)
    if (captured is None or target.get(TEAM_LOOT_REVIEW_RECEIPT) != captured
            or target.get(TEAM_LOOT_FIELD) != source.get(TEAM_LOOT_FIELD)
            or target.get('events') != source.get('events')):
        raise TeamLootError('后处理改变了共同实物、正文或既有授权')
    if captured.get('version') == 2:
        if (_roster_hash(roster) != captured['roster_hash']
                or ('audit_member_snapshot' in target
                    and _roster_hash(target['audit_member_snapshot'] or []) != captured['roster_hash'])):
            raise TeamLootError('后处理的原出发名单与既有授权不一致')
        target['audit_member_snapshot'] = copy.deepcopy(roster)
    if capture_team_loot_review(target) is None:
        raise TeamLootError('后处理后共同实物授权复验失败')


def copy_team_loot_to_published_log(target: dict, source: dict, *, pre_events: list, log_id: int):
    """Losslessly rebase reviewed stock as the runner prepends departure events."""
    if not source.get(TEAM_LOOT_FIELD):
        return
    receipt = capture_team_loot_review(source)
    if receipt is None or (receipt.get('version') == 2 and receipt['expedition_log_id'] != log_id):
        raise TeamLootError('共同实物发布前缺少有效的同源授权')
    rows = copy.deepcopy(source[TEAM_LOOT_FIELD])
    offset = len(pre_events)
    for row in rows:
        row['source_event_index'] += offset
        row['evidence_event_indexes'] = [i + offset for i in row['evidence_event_indexes']]
    target[TEAM_LOOT_FIELD] = rows
    rebased = build_team_loot_review_receipt(rows, target['events'][:offset + receipt['event_count']])
    if receipt.get('version') == 2:
        rebased.update({key: receipt[key] for key in (
            'version', 'authority_kind', 'expedition_log_id', 'attempt',
            'roster_hash', 'source_hash', 'original_source_hash',
        )})
        rebased['event_index_offset'] = offset + receipt['event_index_offset']
    target[TEAM_LOOT_REVIEW_RECEIPT] = rebased
    if capture_team_loot_review(target) is None:
        raise TeamLootError('共同实物发布投影改变了来源或原出发名单')


def _configured(key: str) -> int:
    # Required defaults are supplied by plugin.py; do not hide configuration drift.
    return max(1, int(current_app.config[key]))


def _auction_fee_rate(lot=None, *, freeze=False) -> Decimal:
    provenance = (lot.provenance or {}) if lot is not None else {}
    value = (provenance['auction_fee_rate'] if 'auction_fee_rate' in provenance
             else current_app.config['ADVENTURER_S1_TEAM_LOOT_AUCTION_FEE_RATE'])
    try:
        rate = Decimal(str(value))
        if not rate.is_finite() or not 0 <= rate <= 1:
            raise InvalidOperation
    except (InvalidOperation, ValueError):
        raise TeamLootError('竞价手续费率配置无效，暂不成交') from None
    if freeze and lot is not None and 'auction_fee_rate' not in provenance:
        lot.provenance = {**provenance, 'auction_fee_rate': str(rate)}
    return rate


def _member_aliases(people) -> dict[int, str]:
    # Preserve exact frozen M tokens; raw names are never public aliases.
    aliases = {}
    for person in people:
        match = re.fullmatch(r'(?:M([1-9][0-9]*)|【M([1-9][0-9]*)】)', person.adventurer_name or '')
        if match:
            alias = f'M{match.group(1) or match.group(2)}'
            if alias not in aliases.values():
                aliases[person.adventurer_id] = alias
    number = 1
    for person in sorted(people, key=lambda row: row.adventurer_id):
        if person.adventurer_id in aliases:
            continue
        while f'M{number}' in aliases.values():
            number += 1
        aliases[person.adventurer_id] = f'M{number}'
        number += 1
    return aliases


def _bid_block_reason(person, profiles):
    profile = profiles.get(person.adventurer_id)
    if profile is None or profile.owner_id != person.user_id:
        return '只有本次出发的原冒险者可以出价'
    if profile.status not in AdventurerS1.DISPATCHABLE_STATUSES:
        return '请在休息时调整出价'
    if round(float(profile.gold_balance), 2) < 0:
        return '金币余额为负，不能出价；已有出价可以撤回'
    return None


def _public_identity(lot, people, user_id, profiles):
    aliases = _member_aliases(people)
    names = dict(aliases)
    for person in people:
        profile = profiles.get(person.adventurer_id)
        if (person.user_id == user_id and profile is not None
                and profile.owner_id == user_id):
            names[person.adventurer_id] = profile.name
    replacements = {}
    frozen_names = (lot.provenance or {}).get('member_names') or {}
    for person in people:
        profile = profiles.get(person.adventurer_id)
        for raw in (person.adventurer_name, frozen_names.get(str(person.adventurer_id)),
                    profile.name if profile else None):
            if isinstance(raw, str) and raw and raw != names[person.adventurer_id]:
                replacements.setdefault(raw, set()).add(names[person.adventurer_id])
    mapping = {raw: next(iter(targets)) if len(targets) == 1 else '原队员'
               for raw, targets in replacements.items()}
    pattern = re.compile('|'.join(
        f'(?<![A-Za-z0-9]){re.escape(raw)}(?![A-Za-z0-9])' if re.fullmatch(r'M[1-9][0-9]*', raw)
        else re.escape(raw) for raw in sorted(mapping, key=len, reverse=True)
    )) if mapping else None

    def public_text(value):
        return (pattern.sub(lambda match: mapping[match.group()], value)
                if isinstance(value, str) and pattern is not None else value)
    return aliases, names, public_text


class S1TeamLootService:
    @staticmethod
    def register_in_transaction(log, rows, members: dict, *, provenance: dict):
        """Caller owns the log/settlement transaction and exact review authority."""
        full_log = log.full_log or {}
        normalized, errors = normalize_team_loot(rows, events=full_log.get('events') or [])
        if errors:
            raise TeamLootError(';'.join(errors))
        if not normalized:
            return []
        if provenance.get('kind') not in {'observer', 'original_story_release', 'appeal'}:
            raise TeamLootError('共同实物缺少独立审核依据')
        snapshots = {int(row['adventurer_id']): row for row in full_log.get('audit_member_snapshot') or []
                     if isinstance(row, dict) and type(row.get('adventurer_id')) is int}
        roster = []
        for adv_id, snapshot in snapshots.items():
            adv = members.get(adv_id)
            if 'owner_id' not in snapshot or (adv is not None and adv.owner_id != snapshot['owner_id']):
                raise TeamLootError('原出发成员归属已改变，暂不登记共同实物')
            roster.append({'adventurer_id': adv_id, 'user_id': snapshot.get('owner_id'),
                           'adventurer_name': snapshot.get('adventurer_name') or (adv.name if adv else '原队员')})
        if not roster:
            raise TeamLootError('共同实物缺少本次出发名单')
        existing = {lot.source_key: lot for lot in Repository.source_lots(log.id)}
        # Use the same name locks as normal issuance. Pending physical stock
        # occupies world-unique names before a personal owner is selected.
        from plugins.adventurer_s1.services.s1_item_catalog_lock_service import (
            CatalogBusyError, lock_catalog_names,
        )
        from plugins.adventurer_s1.services.s1_item_world_unique_service import (
            load_world_unique_occupancy, resolve_world_unique_flag,
        )
        names = sorted({row['name'] for row in normalized
                        if (existing.get(team_loot_source_key(log.id, row['source_event_index'], row['name'])) is None
                            or existing[team_loot_source_key(log.id, row['source_event_index'], row['name'])].status == 'cancelled')})
        if names and not lock_catalog_names(names, shared=False, wait=False):
            raise CatalogBusyError()
        catalogs, occupancy = Repository.catalogs(names), load_world_unique_occupancy(names)
        frozen_names = full_log.get('_member_id_to_name') or {}
        registration_provenance = ({**copy.deepcopy(provenance),
            'auction_fee_rate': str(_auction_fee_rate()),
            'member_names': {str(row['adventurer_id']): (
                frozen_names.get(str(row['adventurer_id'])) or frozen_names.get(row['adventurer_id'])
                or row['adventurer_name']) for row in roster},
        } if names else None)
        reserved = set()
        now = _portable_utc_now()
        deadline = now + timedelta(hours=_configured('ADVENTURER_S1_TEAM_LOOT_VOTE_HOURS'))
        new_lots, resulting = [], []
        for row in normalized:
            source_key = team_loot_source_key(log.id, row['source_event_index'], row['name'])
            old = existing.get(source_key)
            if old is None or old.status == 'cancelled':
                catalog = catalogs.get(row['name'])
                unique = resolve_world_unique_flag(
                    row['name'], item_type=row['item']['type'],
                    catalog_flag=bool(catalog.world_unique) if catalog else None,
                )
                # One lot has one eventual holder; companions may retain several
                # same-name individuals without weakening cross-holder uniqueness.
                multiple_companions = companion_allows_duplicate_name_instances(
                    catalog.type if catalog else row['item']['type'],
                )
                if unique and ((row['item']['quantity'] != 1 and not multiple_companions)
                               or row['name'] in reserved
                               or occupancy.get(row['name'], {}).get('quantity_total', 0)):
                    raise TeamLootError(f'物品【{row["name"]}】的唯一实物库存冲突')
                reserved.add(row['name'])
            if old:
                if old.item_data != row['item'] or old.evidence_event_indexes != row['evidence_event_indexes']:
                    raise TeamLootError('同份共同实物的数量或内容与已登记记录冲突')
                if old.status == 'cancelled':
                    old.status, old.mode, old.provenance = 'voting', None, copy.deepcopy(registration_provenance)
                    old.deadline_at = old.next_action_at = deadline
                resulting.append(old)
                continue
            lot = AdventurerS1TeamLootLot(
                expedition_log_id=log.id, source_key=source_key,
                source_event_index=row['source_event_index'], item_name=row['name'], quantity=row['item']['quantity'],
                item_data=copy.deepcopy(row['item']),
                evidence_event_indexes=list(row['evidence_event_indexes']),
                provenance=copy.deepcopy(registration_provenance), party_name=log.party_name,
                target_dungeon=log.target_dungeon,
                status='voting', deadline_at=deadline, next_action_at=deadline,
                contender_ids=[], roll_rounds=[], winning_cents=0,
            )
            new_lots.append(lot)
            resulting.append(lot)
        if new_lots:
            Repository.add_lots(new_lots)
            Repository.add_participants([
                dict(member, lot_id=lot.id, bid_cents=0, held_cents=0)
                for lot in new_lots for member in roster
            ])
        return [lot.id for lot in resulting]

    @staticmethod
    def register_settlement_in_transaction(log, full_log, members):
        rows = full_log.get(TEAM_LOOT_FIELD) or []
        if not rows:
            return []
        receipt = capture_team_loot_review(full_log)
        if receipt is None or (receipt.get('version') == 2 and receipt['expedition_log_id'] != log.id):
            raise TeamLootError('共同实物缺少同源审核或原稿机械放行授权')
        # The final settlement may have appended system events; it must retain
        # the reviewed event identity before calling this method.
        log.full_log = copy.deepcopy(full_log)
        return S1TeamLootService.register_in_transaction(
            log, rows, members, provenance={'kind': receipt.get('authority_kind', 'observer'), **receipt},
        )

    @staticmethod
    def evidence_status(log_id):
        return [{'source_event_index': lot.source_event_index, 'name': lot.item_name,
                 'item': copy.deepcopy(lot.item_data), 'status': lot.status,
                 'source_key': lot.source_key, 'receipt': copy.deepcopy(lot.receipt)}
                for lot in Repository.source_lots(log_id)]

    @staticmethod
    def register_appeal_in_transaction(appeal, rows):
        """Execute only exact, independently approved v2 shared-stock rows."""
        from plugins.adventurer_s1.services.adventurer_s1_data_appeal_operation_contract import (
            effective_data_appeal_operation_decisions, normalize_data_appeal_number,
        )
        log = Repository.log(appeal.expedition_log_id)
        if log is None or log.status != 'completed':
            raise TeamLootError('共同实物仅能登记到关联的已完成探险')
        evidence = appeal.expedition_evidence or {}
        if evidence.get('_team_loot_source_hash') != team_loot_appeal_source_hash(log.full_log or {}):
            raise TeamLootError('共同实物的全文或出发名单已变化，需重新复核')
        review = appeal._parse_json_text(appeal.review_ai_raw_response) or {}
        from services.tavern_appeal_policy import payload as policy_payload
        policy = policy_payload(review, appeal)
        if not policy and review.get('verdict_contract_version') != 2:
            raise TeamLootError('共同实物尚未取得逐项执行授权')
        decisions = effective_data_appeal_operation_decisions(review.get('verdicts'))
        verdicts = {r.get('operation_id'): r for r in review.get('verdicts') or [] if isinstance(r, dict)}
        approved = {}
        for op in review.get('_backend_operation_manifest') or []:
            if not isinstance(op, dict) or op.get('section') != TEAM_LOOT_FIELD:
                continue
            verdict = verdicts.get(op.get('operation_id')) or {}
            if decisions.get(op.get('operation_id')) != 'allow':
                continue
            quantity = verdict.get('quantity_evidence') or {}
            quantity_indexes = quantity.get('evidence_event_indexes')
            quantity_value = normalize_data_appeal_number(quantity.get('value'))
            if (quantity.get('certainty') != 'exact' or quantity_value is None
                    or quantity_value != op.get('numeric_target')
                    or quantity.get('source') != 'explicit_narrative'
                    or not isinstance(quantity.get('evidence'), str) or not quantity['evidence'].strip()
                    or not isinstance(quantity_indexes, list) or not 1 <= len(quantity_indexes) <= 8
                    or any(type(i) is not int or i not in (op.get('evidence_event_indexes') or [])
                           for i in quantity_indexes)
                    or len(set(quantity_indexes)) != len(quantity_indexes)
                    or verdict.get('verified_evidence_event_indexes') != op.get('evidence_event_indexes')):
                raise TeamLootError('共同实物的数量或取得留存证据未闭合')
            approved[(op.get('audit_match') or {}).get('registration_hash')] = op.get('operation_id')
        if policy:
            approved = {(op.get('audit_match') or {}).get('registration_hash'): op['operation_id']
                for op in policy['operations'] if op.get('section') == TEAM_LOOT_FIELD}
        normalized, errors = normalize_team_loot(rows, events=(log.full_log or {}).get('events') or [])
        if errors or any(team_loot_registration_hash(row) not in approved for row in normalized):
            raise TeamLootError('共同实物提案与独立审核的精确操作不匹配')
        snapshots = (log.full_log or {}).get('audit_member_snapshot') or []
        if not any(isinstance(r, dict) and r.get('adventurer_id') == appeal.adventurer_id
                   and r.get('owner_id') == appeal.user_id for r in snapshots):
            raise TeamLootError('申诉者不属于本次出发小队')
        members = Repository.adventurers([r['adventurer_id'] for r in snapshots if isinstance(r, dict)])
        return S1TeamLootService.register_in_transaction(
            log, normalized, members, provenance={'kind': 'appeal', 'appeal_id': appeal.id,
            'source_hash': evidence['_team_loot_source_hash'],
            'operation_ids': [approved[team_loot_registration_hash(row)] for row in normalized]},
        )

    @staticmethod
    def cancel_appeal_registration_in_transaction(lot_id, appeal_id):
        # Revert owner prelocks these lots before any adventurer locks.
        lot = Repository.lot(lot_id)
        if lot is None or (lot.provenance or {}).get('appeal_id') != appeal_id:
            raise TeamLootError('无法精确还原：共同实物登记来源已变化')
        people = Repository.participants([lot.id], lock=True)
        if lot.status == 'cancelled':
            return
        if lot.status != 'voting' or any(p.vote is not None or p.held_cents for p in people):
            raise TeamLootError('无法精确还原：原队伍已开始分配此实物，不能由个人申诉撤回')
        lot.status = 'cancelled'

    @staticmethod
    def _lock(lot_id):
        lot = Repository.lot(lot_id, lock=True)
        if lot is None:
            raise TeamLootError('待分配物品不存在')
        people = Repository.participants([lot.id], lock=True)
        members = Repository.adventurers([p.adventurer_id for p in people], lock=True)
        eligible = {p.adventurer_id: p for p in people
                    if p.adventurer_id in members
                    and members[p.adventurer_id].status not in AdventurerS1.TAVERN_EXCLUDED_STATUSES
                    and members[p.adventurer_id].owner_id == p.user_id}
        return lot, people, members, eligible

    @staticmethod
    def _mine(user_id, people, eligible, adventurer_id=None):
        if adventurer_id is not None and (type(adventurer_id) is not int or adventurer_id <= 0):
            raise TeamLootError('请选择本次出发的冒险者')
        matches = [p for p in people if p.user_id == user_id and p.adventurer_id in eligible
                   and (adventurer_id is None or p.adventurer_id == adventurer_id)]
        if len(matches) != 1:
            raise TeamLootError('只有本次原队伍的存活冒险者可以参与分配')
        return matches[0]

    @staticmethod
    def _currency(lot, changes):
        if not changes:
            return
        _, error = S1CurrencyService.apply_changes_in_transaction('gold', [
            dict(change, related_id=lot.id, related_type='team_loot_lot',
                 description=f'队伍战利品分配：{lot.item_name}') for change in changes
        ])
        if error:
            raise TeamLootError(error)

    @staticmethod
    def _refund(lot, people, eligible, *, keep_id=None):
        changes = []
        forfeitures = copy.deepcopy((lot.provenance or {}).get('escrow_forfeitures') or [])
        for person in people:
            if person.held_cents <= 0 or person.adventurer_id == keep_id:
                continue
            # Death confiscates the original adventurer's assets, including
            # escrow. Never transfer a dead character's money to a replacement.
            if person.adventurer_id in eligible:
                changes.append({'adventurer_id': person.adventurer_id,
                                'change_amount': person.held_cents / 100,
                                'source_type': 'team_loot_bid_refund'})
            else:
                forfeitures.append({'adventurer_id': person.adventurer_id,
                                    'cents': person.held_cents, 'at': to_utc_iso(_portable_utc_now())})
            person.held_cents = 0
        S1TeamLootService._currency(lot, changes)
        if forfeitures:
            lot.provenance = {**(lot.provenance or {}), 'escrow_forfeitures': forfeitures}

    @staticmethod
    def _advance_locked(lot, people, members, eligible, now):
        if lot.status in {'settled', 'unclaimed'}:
            return
        if not eligible:
            S1TeamLootService._refund(lot, people, eligible)
            lot.status = 'unclaimed'
            return
        if lot.status in {'voting', 'bidding'}:
            _auction_fee_rate(lot, freeze=True)
        if lot.status == 'voting':
            voters = [p for p in eligible.values() if p.user_id is not None]
            all_voted = bool(voters) and all(p.vote in {'auction', 'roll'} for p in voters)
            if now < lot.deadline_at and not all_voted:
                return
            auction = sum(p.vote == 'auction' for p in eligible.values())
            roll = sum(p.vote == 'roll' for p in eligible.values())
            lot.mode = 'auction' if auction > roll and len(eligible) > 1 else 'roll'
            if lot.mode == 'auction':
                start = now if all_voted and now < lot.deadline_at else lot.deadline_at
                lot.status = 'bidding'
                lot.deadline_at = start + timedelta(hours=_configured('ADVENTURER_S1_TEAM_LOOT_BID_HOURS'))
                lot.next_action_at = lot.deadline_at
            else:
                lot.status, lot.contender_ids = 'rolling', sorted(eligible)
        if lot.status == 'bidding' and now >= lot.deadline_at:
            bids = [p for p in eligible.values() if p.bid_cents > 0 and p.held_cents == p.bid_cents]
            if bids:
                highest = max(p.bid_cents for p in bids)
                lot.winning_cents = highest
                lot.contender_ids = sorted(p.adventurer_id for p in bids if p.bid_cents == highest)
            else:
                lot.mode, lot.winning_cents = 'roll', 0
                lot.contender_ids = sorted(eligible)
            lot.status = 'rolling'
        if lot.status == 'claim_pending' and lot.winner_adventurer_id not in eligible:
            # Original winner died/lost ownership before delivery. Preserve the
            # old rolls; only the remaining original team can acquire the item.
            S1TeamLootService._refund(lot, people, eligible)
            lot.winner_adventurer_id, lot.winning_cents = None, 0
            lot.mode, lot.status, lot.contender_ids = 'roll', 'rolling', sorted(eligible)
        if lot.status == 'rolling':
            contenders = sorted(set(lot.contender_ids or []) & set(eligible))
            if not contenders:
                contenders = sorted(eligible)
                lot.mode, lot.winning_cents = 'roll', 0
            history = copy.deepcopy(lot.roll_rounds or [])
            sides = max(2, _configured('ADVENTURER_S1_TEAM_LOOT_ROLL_SIDES'))
            for _ in range(_configured('ADVENTURER_S1_TEAM_LOOT_ROLL_BATCH_ROUNDS')):
                if len(contenders) == 1:
                    break
                results = [{'adventurer_id': adv_id, 'value': secrets.randbelow(sides) + 1}
                           for adv_id in contenders]
                highest = max(row['value'] for row in results)
                contenders = [row['adventurer_id'] for row in results if row['value'] == highest]
                history.append({'number': len(history) + 1, 'results': results,
                                'highest': highest, 'tied': len(contenders) > 1})
            lot.roll_rounds, lot.contender_ids = history, contenders
            if len(contenders) == 1:
                lot.winner_adventurer_id = contenders[0]
                S1TeamLootService._refund(lot, people, eligible,
                                         keep_id=contenders[0] if lot.winning_cents else None)
                lot.status = 'claim_pending'
            lot.next_action_at = now

    @staticmethod
    def _public(lot, people, user_id, profiles):
        aliases, names, public_text = _public_identity(lot, people, user_id, profiles)
        mine = next((p for p in people if p.user_id == user_id), None)
        my_members = [p for p in people if p.user_id == user_id]

        receipt = None
        if lot.receipt is not None:
            raw_receipt = lot.receipt
            receipt = {key: copy.deepcopy(raw_receipt[key]) for key in (
                'mode', 'gross_amount', 'fee_amount', 'auction_fee_rate', 'paid_amount',
                'destination', 'claimed_at',
            ) if key in raw_receipt}
            receipt['winner_name'] = names.get(lot.winner_adventurer_id, '原队员')
            receipt['shares'] = []
            for share in raw_receipt.get('shares') or []:
                share_id = share.get('adventurer_id')
                if share_id not in names:
                    matches = [p.adventurer_id for p in people if p.adventurer_name == share.get('name')]
                    share_id = matches[0] if len(matches) == 1 else None
                receipt['shares'].append({'name': names.get(share_id, '原队员'), 'amount': share['amount']})
        rounds = [{'number': round_['number'], 'highest': round_['highest'], 'tied': round_['tied'],
                   'results': [{'name': names.get(row['adventurer_id'], '原队员'), 'value': row['value']}
                               for row in round_['results']]}
                  for round_ in lot.roll_rounds or []]
        return {'id': lot.id, 'expedition_log_id': lot.expedition_log_id,
                'party_name': public_text(lot.party_name), 'target_dungeon': public_text(lot.target_dungeon),
                'name': public_text(lot.item_name),
                'item': {key: public_text(lot.item_data[key]) for key in ('type', 'rarity', 'desc', 'quantity')},
                'status': lot.status, 'mode': lot.mode, 'deadline_at': to_utc_iso(lot.deadline_at),
                'members': [{'name': names[p.adventurer_id], 'codename': aliases[p.adventurer_id],
                             'is_me': p.user_id == user_id, 'vote': p.vote,
                             'has_bid': p.held_cents > 0} for p in people],
                'my_members': [{'adventurer_id': p.adventurer_id, 'name': names[p.adventurer_id],
                                 'codename': aliases[p.adventurer_id],
                                 'vote': p.vote, 'bid_amount': p.bid_cents / 100,
                                 'held_amount': p.held_cents / 100,
                                 'can_bid': _bid_block_reason(p, profiles) is None,
                                 'bid_block_reason': _bid_block_reason(p, profiles)} for p in my_members],
                'my_vote': mine.vote if mine else None,
                'my_bid_amount': mine.bid_cents / 100 if mine else 0,
                'my_held_amount': mine.held_cents / 100 if mine else 0,
                'roll_rounds': rounds, 'winner_name': names.get(lot.winner_adventurer_id),
                'winning_amount': lot.winning_cents / 100,
                'auction_fee_rate': (float((lot.receipt or {}).get('auction_fee_rate', 0))
                                     if lot.status == 'settled' else float(_auction_fee_rate(lot))),
                'can_claim': bool(any(p.adventurer_id == lot.winner_adventurer_id for p in my_members)
                                  and lot.status == 'claim_pending'),
                'delivery_hint': '请在休息时留出仓库位置，物品将自动送入仓库' if lot.status == 'claim_pending' else None,
                'receipt': receipt}

    @staticmethod
    def list_for_user(user_id, *, page=1, per_page=20, log_id=None, include_history=False):
        try:
            pagination = Repository.list_for_user(user_id, page=page, per_page=per_page,
                                                 log_id=log_id, include_history=include_history)
            people = Repository.participants([lot.id for lot in pagination.items])
            profiles = Repository.participant_profiles([person.adventurer_id for person in people])
            grouped = {}
            for person in people:
                grouped.setdefault(person.lot_id, []).append(person)
            return {'items': [S1TeamLootService._public(lot, grouped.get(lot.id, []), user_id, profiles)
                              for lot in pagination.items], 'page': pagination.page,
                    'per_page': pagination.per_page, 'total': pagination.total,
                    'pages': pagination.pages, 'has_next': pagination.has_next,
                    'has_prev': pagination.has_prev,
                    'has_history': Repository.has_history(user_id, log_id)}
        finally:
            db_manager.remove()

    @staticmethod
    def pending_summary(user_id):
        """Only aggregate the caller's original-party lots; never settle on read."""
        try:
            counts = Repository.pending_counts_for_user(user_id)
            return {key: int(counts.get(key, 0)) for key in
                    ('voting', 'bidding', 'rolling', 'claim_pending')}
        finally:
            db_manager.remove()

    @staticmethod
    def vote(user_id, lot_id, mode, adventurer_id=None):
        if mode not in ('auction', 'roll'):
            return None, '请选择竞价或掷点'
        try:
            with _transaction():
                lot, people, members, eligible = S1TeamLootService._lock(lot_id)
                mine = S1TeamLootService._mine(user_id, people, eligible, adventurer_id)
                now = _portable_utc_now()
                if lot.status != 'voting' or now >= lot.deadline_at:
                    raise TeamLootError('分配方式投票已截止')
                mine.vote = mode
                S1TeamLootService._advance_locked(lot, people, members, eligible, now)
                result = S1TeamLootService._public(lot, people, user_id,
                    Repository.participant_profiles([person.adventurer_id for person in people]))
            return result, None
        except TeamLootError as error:
            return None, str(error)

    @staticmethod
    def bid(user_id, lot_id, amount, adventurer_id=None):
        if isinstance(amount, bool) or not isinstance(amount, (int, float, str)):
            return None, '出价须为有效金币金额'
        try:
            decimal = Decimal(str(amount))
            if (not decimal.is_finite() or decimal < 0
                    or decimal > Decimal(_configured('ADVENTURER_S1_TEAM_LOOT_MAX_BID_GOLD'))):
                raise InvalidOperation
            cents = int((decimal * 100).quantize(Decimal('1')))
            if Decimal(cents) / 100 != decimal:
                raise InvalidOperation
        except (InvalidOperation, ValueError, OverflowError):
            return None, '出价须为非负金额，最多两位小数；0 表示撤回出价'
        try:
            with _transaction():
                lot, people, members, eligible = S1TeamLootService._lock(lot_id)
                mine = S1TeamLootService._mine(user_id, people, eligible, adventurer_id)
                if lot.status != 'bidding' or _portable_utc_now() >= lot.deadline_at:
                    raise TeamLootError('竞价已截止')
                if members[mine.adventurer_id].status not in AdventurerS1.DISPATCHABLE_STATUSES:
                    raise TeamLootError('请在休息时调整出价，避免影响正在进行的冒险')
                if cents > 0:
                    balance, error = S1CurrencyService.get_balance(mine.adventurer_id, 'gold')
                    if error:
                        raise TeamLootError(error)
                    if balance < 0:
                        raise TeamLootError('金币余额为负，不能出价；已有出价可以撤回')
                _auction_fee_rate(lot, freeze=True)
                delta = cents - mine.held_cents
                if delta:
                    S1TeamLootService._currency(lot, [{'adventurer_id': mine.adventurer_id,
                        'change_amount': -delta / 100,
                        'source_type': 'team_loot_bid_hold' if delta > 0 else 'team_loot_bid_refund'}])
                mine.bid_cents, mine.held_cents = cents, cents
                result = S1TeamLootService._public(lot, people, user_id,
                    Repository.participant_profiles([person.adventurer_id for person in people]))
            return result, None
        except TeamLootError as error:
            return None, str(error)

    @staticmethod
    def _deliver_locked(lot, people, members, eligible):
        winner = eligible.get(lot.winner_adventurer_id)
        if lot.status != 'claim_pending' or winner is None:
            raise TeamLootError('物品尚未选出可领取的原队员')
        adv = members[winner.adventurer_id]
        if adv.status not in AdventurerS1.DISPATCHABLE_STATUSES:
            raise TeamLootError('请在休息时领取；物品与分配结果会继续保留')
        item_out = {}
        destination, error = grant_raw_item(
            adv, lot.item_name, copy.deepcopy(lot.item_data),
            source_type='team_loot_award', force_destination='warehouse',
            related_id=str(lot.id), related_type='team_loot_lot',
            description=f'本次探险队伍分配：{lot.item_name}', granted_item_out=item_out,
            world_unique_team_loot_lot_id=lot.id,
        )
        if error:
            raise TeamLootError(error)
        shares, changes = [], []
        gross_cents = fee_cents = paid_cents = 0
        fee_rate = Decimal(0)
        if lot.winning_cents:
            if winner.held_cents != lot.winning_cents:
                raise TeamLootError('胜出出价托管记录不一致，暂不成交')
            others = sorted(set(eligible) - {winner.adventurer_id})
            if not others:
                changes.append({'adventurer_id': winner.adventurer_id,
                                'change_amount': winner.held_cents / 100,
                                'source_type': 'team_loot_bid_refund'})
            else:
                gross_cents = lot.winning_cents
                fee_rate = _auction_fee_rate(lot, freeze=True)
                fee_cents = int((Decimal(gross_cents) * fee_rate).quantize(
                    Decimal('1'), rounding=ROUND_HALF_UP))
                paid_cents = gross_cents - fee_cents
                share, remainder = divmod(paid_cents, len(others))
                for index, adv_id in enumerate(others):
                    value = share + int(index < remainder)
                    shares.append({'adventurer_id': adv_id,
                                   'name': eligible[adv_id].adventurer_name, 'amount': value / 100})
                    if value:
                        changes.append({'adventurer_id': adv_id, 'change_amount': value / 100,
                                        'source_type': 'team_loot_price_share'})
            winner.held_cents = 0
        S1TeamLootService._currency(lot, changes)
        lot.receipt = {'winner_name': winner.adventurer_name, 'mode': lot.mode,
                       'gross_amount': gross_cents / 100, 'fee_amount': fee_cents / 100,
                       'auction_fee_rate': float(fee_rate), 'paid_amount': paid_cents / 100,
                       'shares': shares, 'destination': destination,
                       'claimed_at': to_utc_iso(_portable_utc_now())}
        lot.status, lot.last_error = 'settled', None

    @staticmethod
    def claim(user_id, lot_id):
        try:
            with _transaction():
                lot, people, members, eligible = S1TeamLootService._lock(lot_id)
                mine = S1TeamLootService._mine(user_id, people, eligible, lot.winner_adventurer_id)
                if lot.status == 'settled':
                    return S1TeamLootService._public(lot, people, user_id,
                        Repository.participant_profiles([person.adventurer_id for person in people])), None
                if lot.winner_adventurer_id != mine.adventurer_id:
                    raise TeamLootError('只有本次胜出者可以领取')
                _, _, public_text = _public_identity(lot, people, user_id, members)
                try:
                    S1TeamLootService._deliver_locked(lot, people, members, eligible)
                except TeamLootError as error:
                    raise TeamLootError(public_text(str(error))) from error
                result = S1TeamLootService._public(lot, people, user_id,
                    Repository.participant_profiles([person.adventurer_id for person in people]))
            return result, None
        except TeamLootError as error:
            return None, str(error)

    @staticmethod
    def advance_lot(lot_id):
        with _transaction():
            lot, people, members, eligible = S1TeamLootService._lock(lot_id)
            now = _portable_utc_now()
            S1TeamLootService._advance_locked(lot, people, members, eligible, now)
            lot.next_action_at = now + timedelta(seconds=_configured('ADVENTURER_S1_TEAM_LOOT_RETRY_SECONDS'))
            if lot.status in {'voting', 'bidding'}:
                lot.next_action_at = lot.deadline_at

    @staticmethod
    def settle_due_lots():
        try:
            ids = Repository.due_ids(_portable_utc_now(), _configured('ADVENTURER_S1_TEAM_LOOT_JOB_BATCH_SIZE'))
        finally:
            db_manager.remove()
        for lot_id in ids:
            try:
                # Commit the choice/rolls before attempting a grant. Capacity
                # or catalog contention must never reroll an established win.
                S1TeamLootService.advance_lot(lot_id)
                with _transaction():
                    lot, people, members, eligible = S1TeamLootService._lock(lot_id)
                    if lot.status == 'claim_pending':
                        S1TeamLootService._deliver_locked(lot, people, members, eligible)
            except TeamLootError as error:
                logger.info('S1 team loot delivery deferred lot=%s: %s', lot_id, error)
            except Exception:
                logger.exception('S1 team loot advance failed lot=%s', lot_id)
            finally:
                db_manager.remove()
