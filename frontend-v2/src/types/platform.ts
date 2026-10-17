export interface User {
  id: number
  player_id: number
  username: string
  display_name: string
  is_admin: boolean
  is_active?: boolean
  created_at: string | null
}

export interface Tavern {
  id: number
  slug: string
  display_name: string
  status: 'draft' | 'active' | 'disabled'
  game_pack: string
  game_pack_release: string
  membership_role?: string
  is_public: boolean
  allow_self_join: boolean
  can_self_join?: boolean
  runtime_mode?: string
  runtime_namespace?: string
  runtime_port?: number | null
  runtime_status?: string
  runtime_pid?: number | null
  runtime_error?: string | null
  created_at: string | null
}

export interface GamePackDefinition {
  pack_id: string
  display_name: string
  platform_api_version: string
  runtime_api_version: string
  pack_version: string
  frontend_plugin: string
}

export interface TavernContext {
  tavern: Tavern
  runtime: {
    game_pack: string
    release: string
    runtime_api_version: string
    frontend_plugin: string
    entry_path: string
    migration_status: string
  }
}

export interface NarrativeDescriptor {
  pack_id: string
  contract_version: string
  labels: {
    entity: string
    fact: string
    story_thread: string
    candidate: string
    snapshot: string
  }
  entity_kinds: string[]
  fact_kinds: string[]
  thread_kinds: string[]
  candidate_types: string[]
  supports_generic_writes: boolean
  compatibility_mode?: string
  preview_mode?: string
  custom_views?: Array<{
    key: string
    label: string
    path?: string
  }>
}

export interface NarrativeOverview {
  entity_count: number
  fact_count: number
  story_thread_count: number
  pending_candidate_count: number
  shared_entity_count: number
  shared_fact_count: number
  shared_story_thread_count: number
}

export interface NarrativePage<T> {
  items: T[]
  page: number
  page_size: number
  total: number
}

export type NarrativeRecord = Record<string, unknown>

export interface NarrativeCandidate extends NarrativeRecord {
  id: number
  candidate_type: string
  status: 'pending' | 'accepted' | 'rejected'
  normalized_payload: Record<string, unknown>
  source_run_key: string | null
  decision_reason: string | null
}
