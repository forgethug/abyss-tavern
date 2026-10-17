"""Administrator-authored public epilogues; gameplay settlement stays closed."""
from flask import current_app

from db_repository.s1_campaign_stage_repository import S1CampaignStageRepository as Repo
from utils.time_utils import to_utc_iso


def ending_snapshot(dispatch):
    return {'public_title': dispatch.public_title, 'public_summary': dispatch.public_summary,
            'ended_at': to_utc_iso(dispatch.ended_at)}


def ending_proposal(dispatch, state, fronts, request):
    from plugins.adventurer_s1.plugin import AdventurerS1Plugin

    if not dispatch.discovered_at or not dispatch.ended_at or state.status != 'ended':
        raise ValueError('只能为已经公开并正式结束的战役整理结局')
    if dispatch.active_log_id or Repo.busy([front.id for front in fronts]):
        raise ValueError('仍有小队执行或世界事实待处理，请等待战报发布')
    title, summary = request.get('public_title'), request.get('public_summary')
    summary_key = 'ADVENTURER_S1_CAMPAIGN_ENDING_TEXT_LIMIT'
    summary_limit = int(current_app.config.get(summary_key, AdventurerS1Plugin.default_config[summary_key]))
    if not isinstance(title, str) or not 1 <= len(title.strip()) <= 120:
        raise ValueError('请填写结局标题，最多 120 字')
    if not isinstance(summary, str) or not 1 <= len(summary.strip()) <= summary_limit:
        raise ValueError(f'请填写完整结局，最多 {summary_limit} 字')
    title, summary = title.strip(), summary.strip()
    if (title, summary) == (dispatch.public_title, dispatch.public_summary):
        raise ValueError('结局内容没有变化')
    ids = request.get('evidence_log_ids')
    evidence_key = 'ADVENTURER_S1_CAMPAIGN_ENDING_MAX_EVIDENCE'
    evidence_limit = int(current_app.config.get(evidence_key, AdventurerS1Plugin.default_config[evidence_key]))
    if (not isinstance(ids, list) or not 1 <= len(ids) <= evidence_limit
            or any(type(value) is not int or value < 1 for value in ids)
            or len(set(ids)) != len(ids)):
        raise ValueError(f'请引用 1 至 {evidence_limit} 份不重复的已发布战报 ID')
    rows = Repo.public_ending_evidence(dispatch.id, ids)
    if len(rows) != len(ids):
        raise ValueError('引用的战报未发布、探险未完成或不属于本战役')
    evidence = []
    for row in rows:
        if any(not isinstance(value, str) or not value.strip()
               for value in (row.public_title, row.public_summary, row.outcome)):
            raise ValueError('引用的战报缺少公开内容，请先核查原始记录')
        evidence.append({
            'expedition_log_id': row.expedition_log_id, 'revision': row.revision,
            'party_name': row.party_name, 'published_at': to_utc_iso(row.published_at),
            'title': row.public_title, 'summary': row.public_summary, 'outcome': row.outcome,
        })
    # The administrator reviews public facts. This path never infers story
    # outcomes, calls an AI, or changes the original reports/world state.
    return {'public_title': title, 'public_summary': summary,
            'ended_at': to_utc_iso(dispatch.ended_at), 'evidence': evidence}
