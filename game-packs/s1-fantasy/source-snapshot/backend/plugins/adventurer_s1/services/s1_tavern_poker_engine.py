"""Deterministic no-limit hold'em tournament engine for S1 tavern activities.

The engine owns every gameplay fact. AI services may narrate the returned ledger,
but must never create or change cards, actions, chips, eliminations, or ranking.
"""

from __future__ import annotations

from itertools import combinations
import random
from typing import Any, Iterable


ENGINE_VERSION = 's1_holdem_state_machine_v1'
_SUITS = ('S', 'H', 'D', 'C')
_RANKS = tuple(range(2, 15))
_CATEGORY_NAMES = (
    '高牌', '一对', '两对', '三条', '顺子',
    '同花', '葫芦', '四条', '同花顺',
)


def _card_text(card: tuple[int, str]) -> str:
    rank, suit = card
    rank_text = {14: 'A', 13: 'K', 12: 'Q', 11: 'J', 10: 'T'}.get(rank, str(rank))
    return f'{rank_text}{suit}'


def _straight_high(ranks: Iterable[int]) -> int | None:
    unique = set(ranks)
    if 14 in unique:
        unique.add(1)
    ordered = sorted(unique, reverse=True)
    for high in ordered:
        if all(high - offset in unique for offset in range(5)):
            return high
    return None


def _evaluate_five(cards: tuple[tuple[int, str], ...]) -> tuple[int, ...]:
    ranks = sorted((card[0] for card in cards), reverse=True)
    counts = {rank: ranks.count(rank) for rank in set(ranks)}
    groups = sorted(
        ((count, rank) for rank, count in counts.items()),
        reverse=True,
    )
    flush = len({card[1] for card in cards}) == 1
    straight = _straight_high(ranks)
    if flush and straight is not None:
        return (8, straight)
    if groups[0][0] == 4:
        four_rank = groups[0][1]
        kicker = max(rank for rank in ranks if rank != four_rank)
        return (7, four_rank, kicker)
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, groups[0][1], groups[1][1])
    if flush:
        return (5, *ranks)
    if straight is not None:
        return (4, straight)
    if groups[0][0] == 3:
        trip_rank = groups[0][1]
        kickers = sorted((rank for rank in ranks if rank != trip_rank), reverse=True)
        return (3, trip_rank, *kickers)
    pair_ranks = sorted(
        (rank for rank, count in counts.items() if count == 2),
        reverse=True,
    )
    if len(pair_ranks) >= 2:
        kicker = max(rank for rank in ranks if rank not in pair_ranks[:2])
        return (2, pair_ranks[0], pair_ranks[1], kicker)
    if len(pair_ranks) == 1:
        pair_rank = pair_ranks[0]
        kickers = sorted((rank for rank in ranks if rank != pair_rank), reverse=True)
        return (1, pair_rank, *kickers)
    return (0, *ranks)


def evaluate_holdem(
    hole_cards: list[tuple[int, str]],
    board: list[tuple[int, str]],
) -> tuple[int, ...]:
    """Return the exact comparable rank of the strongest five-card hand."""
    cards = tuple(hole_cards + board)
    if len(cards) < 5:
        raise ValueError('至少需要五张牌才能比较牌型')
    return max(_evaluate_five(combo) for combo in combinations(cards, 5))


def _preflop_strength(hole_cards: list[tuple[int, str]]) -> float:
    first, second = sorted((card[0] for card in hole_cards), reverse=True)
    paired = first == second
    suited = hole_cards[0][1] == hole_cards[1][1]
    gap = first - second
    value = (first + second) / 28
    if paired:
        value += 0.28
    if suited:
        value += 0.06
    if gap <= 2:
        value += 0.05
    return min(1.0, value)


def _postflop_strength(
    hole_cards: list[tuple[int, str]],
    board: list[tuple[int, str]],
) -> float:
    if len(board) < 3:
        return _preflop_strength(hole_cards)
    hand_rank = evaluate_holdem(hole_cards, board)
    return min(1.0, hand_rank[0] / 8 + hand_rank[1] / 140)


def _ordered_after(seats: list[int], seat: int) -> list[int]:
    index = seats.index(seat)
    return seats[index + 1:] + seats[:index + 1]


def _next_live_seat(seats: list[int], seat: int, live: set[int]) -> int:
    for candidate in _ordered_after(seats, seat):
        if candidate in live:
            return candidate
    raise RuntimeError('牌桌没有可用座位')


def _commit_chips(
    player_id: int,
    amount: int,
    stacks: dict[int, int],
    street_committed: dict[int, int],
    total_committed: dict[int, int],
) -> int:
    paid = min(max(0, int(amount)), stacks[player_id])
    stacks[player_id] -= paid
    street_committed[player_id] += paid
    total_committed[player_id] += paid
    return paid


def _betting_round(
    *,
    street: str,
    order: list[int],
    stacks: dict[int, int],
    hole_cards: dict[int, list[tuple[int, str]]],
    board: list[tuple[int, str]],
    folded: set[int],
    street_committed: dict[int, int],
    total_committed: dict[int, int],
    current_bet: int,
    minimum_raise: int,
    big_blind: int,
    rng: random.Random,
) -> tuple[int, list[dict[str, Any]]]:
    eligible = {
        player_id for player_id in order
        if player_id not in folded and stacks[player_id] > 0
    }
    pending = set(eligible)
    actions: list[dict[str, Any]] = []
    action_guard = 0
    while pending and len(set(order) - folded) > 1:
        action_guard += 1
        if action_guard > len(order) * 12:
            raise RuntimeError('牌局行动轮未能收敛')
        actor = next((player_id for player_id in order if player_id in pending), None)
        if actor is None:
            break
        to_call = max(0, current_bet - street_committed[actor])
        strength = (
            _preflop_strength(hole_cards[actor])
            if street == 'preflop'
            else _postflop_strength(hole_cards[actor], board)
        )
        decision_score = strength + rng.uniform(-0.22, 0.22)
        pressure = to_call / max(1, stacks[actor] + street_committed[actor])
        can_full_raise = stacks[actor] >= to_call + minimum_raise

        if to_call > 0 and decision_score < 0.27 + pressure * 0.8:
            folded.add(actor)
            pending.discard(actor)
            actions.append({
                'actor_id': actor,
                'action': 'fold',
                'amount': 0,
                'stack_after': stacks[actor],
            })
            continue

        if can_full_raise and decision_score > 0.72:
            raise_size = max(minimum_raise, big_blind * rng.choice((1, 2, 3)))
            target = min(
                street_committed[actor] + stacks[actor],
                current_bet + raise_size,
            )
            paid = _commit_chips(
                actor,
                target - street_committed[actor],
                stacks,
                street_committed,
                total_committed,
            )
            previous_bet = current_bet
            current_bet = street_committed[actor]
            minimum_raise = max(minimum_raise, current_bet - previous_bet)
            pending = {
                player_id for player_id in order
                if player_id != actor
                and player_id not in folded
                and stacks[player_id] > 0
                and street_committed[player_id] < current_bet
            }
            actions.append({
                'actor_id': actor,
                'action': 'raise',
                'amount': paid,
                'to': current_bet,
                'stack_after': stacks[actor],
            })
            continue

        paid = _commit_chips(
            actor,
            to_call,
            stacks,
            street_committed,
            total_committed,
        )
        pending.discard(actor)
        actions.append({
            'actor_id': actor,
            'action': 'check' if to_call == 0 else 'call',
            'amount': paid,
            'stack_after': stacks[actor],
        })
    return current_bet, actions


def _award_pots(
    *,
    total_committed: dict[int, int],
    folded: set[int],
    hand_ranks: dict[int, tuple[int, ...]],
    stacks: dict[int, int],
    payout_order: list[int],
) -> tuple[list[dict[str, Any]], dict[int, int]]:
    levels = sorted({amount for amount in total_committed.values() if amount > 0})
    previous = 0
    pots: list[dict[str, Any]] = []
    payouts = {player_id: 0 for player_id in total_committed}
    for level in levels:
        contributors = [
            player_id for player_id, amount in total_committed.items()
            if amount >= level
        ]
        amount = (level - previous) * len(contributors)
        previous = level
        eligible = [
            player_id for player_id in contributors
            if player_id not in folded
        ]
        if not eligible:
            raise RuntimeError('底池没有合法争夺者')
        best_rank = max(hand_ranks[player_id] for player_id in eligible)
        winners = [
            player_id for player_id in payout_order
            if player_id in eligible and hand_ranks[player_id] == best_rank
        ]
        share, remainder = divmod(amount, len(winners))
        for index, winner in enumerate(winners):
            won = share + (1 if index < remainder else 0)
            stacks[winner] += won
            payouts[winner] += won
        pots.append({
            'amount': amount,
            'eligible_ids': eligible,
            'winner_ids': winners,
        })
    return pots, payouts


def _result_note(rank: int, participant_count: int, elimination_hand: int | None) -> str:
    if rank == 1:
        return f'赢得牌桌最后的全部赛场筹码，取得本组第 1 名。'
    hand_text = f'第 {elimination_hand} 手牌' if elimination_hand else '牌桌收束时'
    return f'{hand_text}筹码归零，最终位列本组第 {rank} 名（共 {participant_count} 人）。'


def resolve_holdem_tournament(
    participants: list[dict[str, Any]],
    *,
    seed: int,
    starting_stack: int,
    small_blind: int,
    big_blind: int,
    blind_raise_every_hands: int,
    max_hands: int,
) -> dict[str, Any]:
    """Resolve a complete single-table tournament from a frozen seat list."""
    if not 2 <= len(participants) <= 10:
        raise ValueError('单桌参赛人数必须为 2 到 10 人')
    if len({int(row['adventurer_id']) for row in participants}) != len(participants):
        raise ValueError('参赛身份必须唯一')
    if starting_stack <= 0 or small_blind <= 0 or big_blind <= small_blind:
        raise ValueError('初始筹码与盲注配置无效')
    if blind_raise_every_hands <= 0 or max_hands <= 0:
        raise ValueError('盲注升级与手数上限配置无效')

    rng = random.Random(int(seed))
    seats = [int(row['adventurer_id']) for row in participants]
    stacks = {player_id: int(starting_stack) for player_id in seats}
    eliminations: list[dict[str, int]] = []
    hands: list[dict[str, Any]] = []
    dealer = seats[-1]

    for hand_number in range(1, max_hands + 1):
        live = {player_id for player_id in seats if stacks[player_id] > 0}
        if len(live) <= 1:
            break
        dealer = _next_live_seat(seats, dealer, live)
        live_order = [player_id for player_id in _ordered_after(seats, dealer) if player_id in live]
        if len(live) == 2:
            small_blind_seat = dealer
            big_blind_seat = _next_live_seat(seats, dealer, live)
        else:
            small_blind_seat = _next_live_seat(seats, dealer, live)
            big_blind_seat = _next_live_seat(seats, small_blind_seat, live)
        level = (hand_number - 1) // blind_raise_every_hands
        hand_small_blind = small_blind * (2 ** level)
        hand_big_blind = big_blind * (2 ** level)
        hand_start_stacks = dict(stacks)

        deck = [(rank, suit) for suit in _SUITS for rank in _RANKS]
        rng.shuffle(deck)
        deal_order = [player_id for player_id in _ordered_after(seats, dealer) if player_id in live]
        hole_cards = {player_id: [] for player_id in live}
        for _ in range(2):
            for player_id in deal_order:
                hole_cards[player_id].append(deck.pop())
        board: list[tuple[int, str]] = []
        folded: set[int] = set()
        total_committed = {player_id: 0 for player_id in live}
        street_committed = {player_id: 0 for player_id in live}
        actions_by_street: dict[str, list[dict[str, Any]]] = {}

        _commit_chips(
            small_blind_seat, hand_small_blind, stacks,
            street_committed, total_committed,
        )
        _commit_chips(
            big_blind_seat, hand_big_blind, stacks,
            street_committed, total_committed,
        )
        current_bet = max(street_committed.values())
        preflop_order = [
            player_id for player_id in _ordered_after(seats, big_blind_seat)
            if player_id in live
        ]
        _, actions_by_street['preflop'] = _betting_round(
            street='preflop',
            order=preflop_order,
            stacks=stacks,
            hole_cards=hole_cards,
            board=board,
            folded=folded,
            street_committed=street_committed,
            total_committed=total_committed,
            current_bet=current_bet,
            minimum_raise=hand_big_blind,
            big_blind=hand_big_blind,
            rng=rng,
        )

        for street, card_count in (('flop', 3), ('turn', 1), ('river', 1)):
            if len(live - folded) <= 1:
                break
            deck.pop()
            board.extend(deck.pop() for _ in range(card_count))
            street_committed = {player_id: 0 for player_id in live}
            postflop_order = [
                player_id for player_id in _ordered_after(seats, dealer)
                if player_id in live
            ]
            _, actions_by_street[street] = _betting_round(
                street=street,
                order=postflop_order,
                stacks=stacks,
                hole_cards=hole_cards,
                board=board,
                folded=folded,
                street_committed=street_committed,
                total_committed=total_committed,
                current_bet=0,
                minimum_raise=hand_big_blind,
                big_blind=hand_big_blind,
                rng=rng,
            )

        contenders = live - folded
        if len(contenders) == 1:
            sole_winner = next(iter(contenders))
            hand_ranks = {player_id: (0,) for player_id in live}
            hand_ranks[sole_winner] = (9,)
        else:
            while len(board) < 5:
                deck.pop()
                board.append(deck.pop())
            hand_ranks = {
                player_id: evaluate_holdem(hole_cards[player_id], board)
                for player_id in live
            }
        payout_order = [
            player_id for player_id in _ordered_after(seats, dealer)
            if player_id in live
        ]
        pots, payouts = _award_pots(
            total_committed=total_committed,
            folded=folded,
            hand_ranks=hand_ranks,
            stacks=stacks,
            payout_order=payout_order,
        )
        eliminated_this_hand = [
            player_id for player_id in live if stacks[player_id] == 0
        ]
        seat_distance = {
            player_id: index for index, player_id in enumerate(payout_order)
        }
        eliminated_this_hand.sort(key=lambda player_id: (
            hand_start_stacks[player_id],
            hand_ranks[player_id],
            -seat_distance[player_id],
        ))
        for player_id in eliminated_this_hand:
            eliminations.append({
                'adventurer_id': player_id,
                'hand_number': hand_number,
            })

        hands.append({
            'hand_number': hand_number,
            'dealer_id': dealer,
            'small_blind': hand_small_blind,
            'big_blind': hand_big_blind,
            'starting_stacks': {
                str(player_id): amount
                for player_id, amount in hand_start_stacks.items()
            },
            'hole_cards': {
                str(player_id): [_card_text(card) for card in hole_cards[player_id]]
                for player_id in payout_order
            },
            'board': [_card_text(card) for card in board],
            'actions': actions_by_street,
            'pots': pots,
            'payouts': {
                str(player_id): amount
                for player_id, amount in payouts.items()
            },
            'showdown': {
                str(player_id): {
                    'category': _CATEGORY_NAMES[min(8, hand_ranks[player_id][0])],
                    'rank_key': list(hand_ranks[player_id]),
                }
                for player_id in payout_order
                if player_id not in folded
            },
            'eliminated_ids': eliminated_this_hand,
            'ending_stacks': {
                str(player_id): amount
                for player_id, amount in stacks.items()
            },
        })
    else:
        raise RuntimeError('牌桌超过机械手数上限，未产生可发布赛果')

    survivors = [player_id for player_id in seats if stacks[player_id] > 0]
    if len(survivors) != 1 or len(eliminations) != len(seats) - 1:
        raise RuntimeError('机械牌局未形成唯一冠军与完整淘汰顺序')
    ordered_finish = [survivors[0], *reversed([
        row['adventurer_id'] for row in eliminations
    ])]
    elimination_hand_by_id = {
        row['adventurer_id']: row['hand_number'] for row in eliminations
    }
    ranking = [
        {
            'adventurer_id': player_id,
            'rank': rank,
            'result_note': _result_note(
                rank,
                len(seats),
                elimination_hand_by_id.get(player_id),
            ),
        }
        for rank, player_id in enumerate(ordered_finish, start=1)
    ]
    decisive_hand_numbers = {
        hand['hand_number'] for hand in hands if hand['eliminated_ids']
    }
    if hands:
        decisive_hand_numbers.add(max(
            hands,
            key=lambda hand: sum(pot['amount'] for pot in hand['pots']),
        )['hand_number'])
    highlights = [
        hand for hand in hands if hand['hand_number'] in decisive_hand_numbers
    ]
    return {
        'engine_version': ENGINE_VERSION,
        'seed': int(seed),
        'starting_stack': int(starting_stack),
        'small_blind': int(small_blind),
        'big_blind': int(big_blind),
        'blind_raise_every_hands': int(blind_raise_every_hands),
        'max_hands': int(max_hands),
        'total_chips': int(starting_stack) * len(seats),
        'hand_count': len(hands),
        'ranking': ranking,
        'eliminations': eliminations,
        'hands': hands,
        'highlights': highlights,
        'final_stacks': {
            str(player_id): amount
            for player_id, amount in stacks.items()
        },
    }


def verify_holdem_tournament_result(
    raw: object,
    participants: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Re-run the frozen seed and require an exact canonical gameplay ledger."""
    if not isinstance(raw, dict):
        return None
    allowed = {
        key: raw.get(key)
        for key in (
            'engine_version', 'seed', 'starting_stack', 'small_blind', 'big_blind',
            'blind_raise_every_hands', 'max_hands', 'total_chips', 'hand_count',
            'ranking', 'eliminations', 'hands', 'highlights', 'final_stacks',
        )
    }
    if allowed['engine_version'] != ENGINE_VERSION:
        return None
    try:
        expected = resolve_holdem_tournament(
            participants,
            seed=int(allowed['seed']),
            starting_stack=int(allowed['starting_stack']),
            small_blind=int(allowed['small_blind']),
            big_blind=int(allowed['big_blind']),
            blind_raise_every_hands=int(allowed['blind_raise_every_hands']),
            max_hands=int(allowed['max_hands']),
        )
    except (KeyError, TypeError, ValueError, RuntimeError):
        return None
    return expected if allowed == expected else None
