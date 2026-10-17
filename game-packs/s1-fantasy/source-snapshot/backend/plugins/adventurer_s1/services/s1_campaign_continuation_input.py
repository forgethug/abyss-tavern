"""Purpose-specific continuation view; full evidence remains in the frozen server snapshot."""
from copy import deepcopy


def challenge_brief(value):
    if not isinstance(value, dict):
        return None
    return {
        **{key: deepcopy(value[key]) for key in (
            'recommended_level', 'public_brief', 'battlefield', 'preparation_effects') if key in value},
        'opponents': [{key: deepcopy(row[key]) for key in (
            'subject', 'scope', 'basis_level', 'condition') if key in row}
            for row in value.get('opponents', [])],
    }


def project_stage(stage, *, original=None):
    result = deepcopy(stage)
    old = {n['key']: n for n in (original or stage)['nodes']}
    for node in result['nodes']:
        challenge = node.pop('challenge', None)
        if challenge is not None:
            previous = old.get(node['key'], {})
            # Operator-authored changes must still be reviewed in full.
            node['challenge_context'] = (challenge_brief(challenge)
                if challenge == previous.get('challenge') else challenge)
    return result


def compact_input(snapshot, proposal=None):
    current = snapshot['before']['stage_key']
    entered = set(snapshot['entered_stage_keys'])
    plan = snapshot['plan']
    result = {key: deepcopy(snapshot[key]) for key in (
        'input_hash', 'eligible_branches', 'contract_limits', 'timeline')}
    result['before'] = {key: deepcopy(snapshot['before'][key]) for key in (
        'stage_key', 'stage_instance', 'progress') if key in snapshot['before']}
    result['entered_stage_keys'] = list(snapshot['entered_stage_keys'])
    result['plan'] = {key: deepcopy(value) for key, value in plan.items() if key != 'stages'}
    result['plan']['stages'] = [project_stage(stage) for stage in plan['stages']
                              if stage['key'] == current or stage['key'] not in entered]
    # Settled tasks are immutable history: their receipts, not their old encounter
    # setup, explain the next transition. Pending optional tasks retain full planning context.
    progress = snapshot['before'].get('progress', {})
    for stage in result['plan']['stages']:
        if stage['key'] == current:
            stage['nodes'] = [
                {key: node[key] for key in ('key', 'dungeon', 'title', 'objective')}
                if progress.get(node['key'], {}).get('result') in ('confirmed', 'failed')
                else node for node in stage['nodes']]
    result['fronts'] = deepcopy(snapshot['fronts'])
    result['factions'] = deepcopy(snapshot['factions'])
    # Published result receipts already passed the expedition/world publication gate.
    # No keyword extraction, new summarization AI, or slicing of story paragraphs.
    result['evidence'] = [{key: deepcopy(row[key]) for key in (
        'log_id', 'dungeon', 'created_at', 'published_at', 'report_revision',
        'report', 'action_window_context') if key in row} for row in snapshot['evidence']]
    actor_fields = ('id', 'canonical_name', 'status', 'identity_summary', 'power_boundary',
                    'current_state', 'current_agenda', 'scope_keys', 'thread_ids')
    actors = {row['id']: {k: deepcopy(row[k]) for k in actor_fields if k in row}
              for row in snapshot['local_world']['actors']}
    for row in snapshot['actors']:
        actors.setdefault(row['id'], {}).update(
            {k: deepcopy(row[k]) for k in actor_fields if k in row})
    result['local_world'] = {key: deepcopy(snapshot['local_world'][key])
                             for key in ('dungeons', 'locations')}
    result['local_world']['actors'] = [actors[key] for key in sorted(actors)]
    if proposal is not None:
        from plugins.adventurer_s1.services.s1_campaign_continuation_contract import digest
        result['proposal'] = deepcopy(proposal)
        old = {stage['key']: stage for stage in plan['stages']}
        result['proposal']['stages'] = [
            project_stage(stage, original=old.get(stage['key'], {'nodes': []}))
            for stage in proposal['stages']]
        # The review binds the full server proposal, including inherited protected fields.
        result['proposal_hash'] = digest(proposal)
    return result


def inherit_generated_fields(raw, snapshot):
    """Only exact existing stage/node/map identities inherit immutable operator config."""
    if not isinstance(raw, dict):
        return raw
    result = deepcopy(raw)
    old_stages = {stage['key']: stage for stage in snapshot['plan']['stages']}
    if not isinstance(result.get('stages'), list):
        return result
    for stage in result['stages']:
        if not isinstance(stage, dict):
            continue
        old = old_stages.get(stage.get('key'), {})
        if 'completion_nodes' in old and 'completion_nodes' not in stage:
            stage['completion_nodes'] = deepcopy(old['completion_nodes'])
        nodes = {n['key']: n for n in old.get('nodes', [])}
        if not isinstance(stage.get('nodes'), list):
            continue
        for node in stage['nodes']:
            if not isinstance(node, dict):
                continue
            prior = nodes.get(node.get('key'), {})
            if prior.get('dungeon') != node.get('dungeon'):
                if prior.get('challenge') is not None or prior.get('action_window') is not None:
                    raise ValueError('自动衔接不能搬移运营挑战或运营行动窗口的地图绑定')
                continue
            for field in ('challenge', 'action_window'):
                if field in prior and field not in node:
                    node[field] = deepcopy(prior[field])
    return result
