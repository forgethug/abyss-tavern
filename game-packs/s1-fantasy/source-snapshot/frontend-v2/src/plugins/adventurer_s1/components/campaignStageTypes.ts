export type CapabilityRank = 'very_low' | 'low' | 'baseline' | 'high' | 'very_high' | 'exceptional'
export interface CampaignOpponent {
  subject: string
  scope: 'individual' | 'group'
  basis_level: number
  typical_profile: Record<'force' | 'mobility' | 'cognition' | 'durability' | 'perception', CapabilityRank>
  current_profile: CampaignOpponent['typical_profile']
  condition: 'impaired' | 'normal' | 'enhanced'
  number_pressure: 'none' | 'low' | 'medium' | 'high' | 'overwhelming'
  capabilities: string[]
  limitations: string[]
}
export interface CampaignChallenge {
  recommended_level: number
  public_brief: string
  opponents: CampaignOpponent[]
  battlefield: string
  preparation_effects: string
}
export interface CampaignActionWindow {
  anchor: 'campaign_started' | 'campaign_discovered'
  after_hours: number
  condition: string
  development: string
}
export interface CampaignStageNode {
  key: string
  dungeon: string
  title: string
  objective: string
  success_condition: string
  failure_condition: string
  challenge?: CampaignChallenge | null
  action_window?: CampaignActionWindow | null
  revelation?: { discovery: string; investigation: string; clue_dungeons: string[] }
}
export interface CampaignStage {
  key: string
  title: string
  public_title?: string
  nodes: CampaignStageNode[]
  success_next: string
  failure_next: string | null
  completion_nodes?: string[]
}
export type CampaignAdvanceMode = 'auto' | 'manual'
export interface CampaignRevelationPatch {
  stage_key: string
  node_key: string
  revelation: NonNullable<CampaignStageNode['revelation']>
}
export interface CampaignStagePlan { advance_mode?: CampaignAdvanceMode; revelation_enabled?: boolean; continuity_enabled?: boolean; continuation_directive?: string; stages: CampaignStage[] }
export type CampaignNodeResult = 'pending' | 'confirmed' | 'failed'
export interface CampaignNodeProgress {
  knowledge: 'hidden' | 'hinted' | 'discovered'
  result: CampaignNodeResult
  evidence_log_id: number | null
  evidence_revision: number | null
  evidence_text: string
}
export interface CampaignStageState {
  plan: CampaignStagePlan
  stage_key: string
  stage_instance: number
  progress: Record<string, CampaignNodeProgress>
  status: 'active' | 'ready' | 'ended'
}
