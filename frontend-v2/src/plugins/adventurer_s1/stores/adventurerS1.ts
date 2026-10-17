import { defineStore } from 'pinia'
import api, { getS1AdminRealmTarget, getS1PlayerRealmView } from '@/composables/useApi'
import type { S1AdminRealmTarget, S1RealmContext } from '@/composables/useApi'
import { trackEvent } from '@/utils/analytics'
import {
  confirmStartDormRoomUpgrade,
  STAMINA_RECOVERY_PER_MINUTE,
  type TraitRecoverySnapshot,
} from '../utils'

export interface AdminManualMatchingResult {
  realm_code: S1AdminRealmTarget
  updated_count: number
  party_preference: 'manual'
}

export interface S1ExportArchive {
  filename: string
  directory: string
  relative_path: string
  size: number
  size_mb: number
  created_at: string
  counts?: Record<string, number>
}

const BUILDINGS_FETCH_MIN_INTERVAL_MS = 120_000
const ADMIN_FULL_SCAN_TIMEOUT_MS = 120_000
const S1_MEMBERSHIP_REQUEST = { s1RealmContext: 'membership' } as const
const S1_SELECTED_REQUEST = { s1RealmContext: 'selected' } as const
let _buildingsFetchAt = 0
let _buildingsFetchPending: Promise<any> | null = null

const HOME_BADGES_FETCH_MIN_INTERVAL_MS = 60_000
let _homeBadgesFetchAt = 0
let _homeBadgesFetchPending: Promise<void> | null = null
const TASK_BADGES_FETCH_MIN_INTERVAL_MS = 60_000
let _taskBadgesFetchAt = 0
let _taskBadgesFetchPending: Promise<void> | null = null
const LOGS_FETCH_MIN_INTERVAL_MS = 10_000
let _logsFetchAt = 0
let _logsFetchKey = ''
let _logsLastResponse: any = null
let _logsFetchPending: {
  key: string
  promise: Promise<any>
  refreshRequested: boolean
  discardResponse: boolean
} | null = null
let _browseAdventurersFetchSeq = 0
const SECRET_AUCTION_SUMMARY_FETCH_MIN_INTERVAL_MS = 60_000
let _secretAuctionSummaryFetchAt = 0

const ADVENTURERS_FETCH_MIN_INTERVAL_MS = 8000
let _adventurersFetchAt = 0
let _adventurersFetchOwner: number | null = null
let _adventurersFetchGeneration = 0

/** fetchLogDetail 并发序号：仅最后一次请求可写入 currentLog */
let _logDetailFetchSeq = 0
let _adventurersFetchPending: Promise<any> | null = null

const PARTIES_BOARD_FETCH_MIN_INTERVAL_MS = 30_000
let _partiesBoardFetchAt = 0
let _partiesBoardFetchPending: Promise<any> | null = null
const _partiesBoardVersions: Record<string, string> = {}

const SPECIAL_DUNGEONS_FETCH_MIN_INTERVAL_MS = 60_000
let _specialDungeonsFetchAt = 0
let _specialDungeonsLastAdvId: number | undefined = undefined
let _specialDungeonsLastData: any = null
let _specialDungeonsFetchPending: Promise<any> | null = null

const TEA_HOUSE_SEATS_FETCH_MIN_INTERVAL_MS = 60_000
let _teaHouseSeatsFetchAt = 0
let _teaHouseSeatsFetchPending: Promise<any> | null = null

const INIT_FETCH_MIN_INTERVAL_MS = 60_000
let _initFetchAt = 0
let _initFetchOwner: number | null = null
let _initFetchView: ReturnType<typeof getS1PlayerRealmView> = null
let _initFetchGeneration = 0
let _initFetchPending: Promise<any> | null = null

const TAVERN_SEATS_FETCH_MIN_INTERVAL_MS = 5000
let _tavernSeatsLastRead: { key: string; at: number; data: any } | null = null
let _tavernSeatsPending: {
  key: string
  promise: Promise<any>
  refreshRequested: boolean
} | null = null

function tavernSeatsReadKey(userId: number | null, realm: S1RealmCode | null): string {
  return JSON.stringify([userId, getS1PlayerRealmView(), realm])
}

function logsReadKey(userId: number | null, realm: S1RealmCode | null, query = ''): string {
  return JSON.stringify([userId, getS1PlayerRealmView(), realm, query])
}

const SQUADS_FETCH_MIN_INTERVAL_MS = 5_000
let _squadsFetchAt = 0
let _squadsFetchKey = ''
let _squadsFetchPending: Promise<any> | null = null
let _squadsFetchGeneration = 0
let _squadRefreshPending: Promise<SquadRefreshResult> | null = null
let _squadRefreshOwner: number | null = null
let _localSquadSocketEcho: {
  action: string
  adventurerId: number
  expiresAt: number
  refreshComplete: boolean
} | null = null

function resetSquadFetchCache() {
  _squadsFetchAt = 0
  _squadsFetchKey = ''
  _squadsFetchGeneration += 1
  _squadsFetchPending = null
}

function resetSquadRefresh() {
  _squadRefreshPending = null
  _squadRefreshOwner = null
  _localSquadSocketEcho = null
}

function rememberLocalSquadSocketEcho(action: string, adventurerId: number) {
  _localSquadSocketEcho = {
    action,
    adventurerId,
    expiresAt: Date.now() + 5_000,
    refreshComplete: false,
  }
}

function completeLocalSquadSocketEcho(action: string, adventurerId: number) {
  if (
    _localSquadSocketEcho?.action === action
    && _localSquadSocketEcho.adventurerId === adventurerId
  ) {
    _localSquadSocketEcho.refreshComplete = true
  }
}

function clearLocalSquadSocketEcho(action: string, adventurerId: number) {
  if (
    _localSquadSocketEcho?.action === action
    && _localSquadSocketEcho.adventurerId === adventurerId
  ) {
    _localSquadSocketEcho = null
  }
}

function consumeLocalSquadSocketEcho(
  payload: any,
): 'pending' | 'complete' | null {
  const pendingEcho = _localSquadSocketEcho
  if (!pendingEcho) return null
  if (pendingEcho.expiresAt < Date.now()) {
    _localSquadSocketEcho = null
    return null
  }
  const ids = Array.isArray(payload?.ids)
    ? payload.ids.map((value: unknown) => Number(value))
    : []
  if (
    String(payload?.action || '') !== pendingEcho.action
    || !ids.includes(pendingEcho.adventurerId)
  ) {
    return null
  }
  _localSquadSocketEcho = null
  return pendingEcho.refreshComplete ? 'complete' : 'pending'
}

function invalidateSessionOwnedFetches() {
  _logsFetchAt = 0
  _logsFetchKey = ''
  _logsLastResponse = null
  _logsFetchPending = null
  _initFetchAt = 0
  _initFetchOwner = null
  _initFetchView = null
  _initFetchGeneration += 1
  _initFetchPending = null
  _tavernSeatsLastRead = null
  _tavernSeatsPending = null
  _adventurersFetchAt = 0
  _adventurersFetchOwner = null
  _adventurersFetchGeneration += 1
  _adventurersFetchPending = null
  _detailFetchGeneration += 1
  for (const key of Object.keys(_detailFetchAt)) delete _detailFetchAt[Number(key)]
  for (const key of Object.keys(_detailFetchPending)) delete _detailFetchPending[Number(key)]
  for (const key of Object.keys(_detailFetchPendingFresh)) delete _detailFetchPendingFresh[Number(key)]
  resetSquadRefresh()
}

function attachSquadRefreshResult(data: any, refresh: SquadRefreshResult) {
  return data && typeof data === 'object'
    ? { ...data, squad_refresh: refresh }
    : data
}

const DETAIL_FETCH_MIN_INTERVAL_MS = 30_000
const _detailFetchAt: Record<number, number> = {}
const _detailFetchPending: Partial<Record<number, Promise<any>>> = {}
const _detailFetchPendingFresh: Partial<Record<number, boolean>> = {}
let _detailFetchGeneration = 0

const DORMITORY_FETCH_MIN_INTERVAL_MS = 60_000
const DORMITORY_PAGE_SIZE = 12
let _dormitoryFetchAt = 0
let _dormitoryFetchPending: Promise<any> | null = null
let _dormitoryFetchPendingPage: number | null = null
let _dormitoryFetchRequestId = 0

const DORMITORY_STATUS_FETCH_MIN_INTERVAL_MS = 10000
let _dormitoryStatusFetchAt = 0
let _dormitoryStatusFetchPending: Promise<any> | null = null

const MATCHMAKING_FETCH_MIN_INTERVAL_MS = 60_000
let _matchmakingFetchAt = 0
let _matchmakingBoardVersion: string | null = null
let _matchMyTicketsPending: Promise<any> | null = null
let _matchPublicTicketsPending: Promise<any> | null = null
let _matchDungeonOptionsPending: Promise<any> | null = null
let _matchmakingBoardPending: Promise<any> | null = null
let _matchMyTicketsLoaded = false
let _matchPublicTicketsLoaded = false
let _matchInvitesLoaded = false
let _matchDungeonOptionsLoaded = false
let _matchDungeonOptionsLevel: number | null = null

function _playableAdventurerLevel(adventurers: AdventurerData[]): number | null {
  const adv = adventurers.find(
    a => a.is_player_created && a.status !== 'dead' && a.status !== 'archived',
  )
  return adv?.level ?? null
}

export interface SkillTemplate {
  id: number
  name: string
  description: string
  is_active?: boolean
}

export interface StarterItem {
  id: number
  name: string
  type: string
  description: string
  rarity: string
  is_starter?: boolean
}

export type S1RealmCode = 'region_1' | 'region_2'

export interface RealmAssignment {
  realm_code: S1RealmCode
  realm_label: string
  is_locked: boolean
}

function realmCodeFromResponseHeader(value: unknown): S1RealmCode | null {
  return value === 'region_1' || value === 'region_2' ? value : null
}

export interface CreationOptions {
  skills: SkillTemplate[]
  items: StarterItem[]
  genders: string[]
  personalities: string[]
  max_per_player: number
  name_max_length: number
  realm_assignment?: RealmAssignment
}

export interface SupportVoteStatus {
  title: string
  content: string
  deadline: string | null
  version: string | null
  is_active: boolean
  is_expired: boolean
  has_voted: boolean
  can_vote: boolean
  my_option_key: string | null
  options: Array<{
    key: string
    label: string
    description: string
  }>
  vote_count: number | null
  admin_vote_count?: number | null
}

export interface SupportVoteAdminStats {
  current_vote: Pick<SupportVoteStatus, 'title' | 'content' | 'deadline' | 'version' | 'is_active' | 'is_expired' | 'options'>
  vote_count: number | null
  option_counts: Array<{ option_key: string; label: string; count: number }>
  recent_votes: Array<{
    id: number
    user_id: number
    nickname: string | null
    adventurer_id: number | null
    adventurer_name: string | null
    option_key: string
    created_at: string | null
  }>
}

export interface SupportVoteAdminPayload {
  title?: string
  content?: string
  deadline?: string | null
  options?: Array<{ label: string; description?: string }>
  clear?: boolean
}

export interface CreatePayload {
  name: string
  gender: string
  personality: string
  skill_template_id: number
  item_ids: number[]
}

export interface Reservation {
  id: number
  user_id?: number
  username?: string
  nickname?: string | null
  name: string
  gender: string
  personality: string
  skill_template_id: number
  item_ids: number[]
  priority?: string
  status: string
  reject_reason?: string | null
  created_at: string | null
  last_login_at?: string | null
  draw_eligible?: boolean
  draw_eligible_via_boost?: boolean
  priority_boost_active?: boolean
  priority_boost_until?: string | null
  can_boost_priority?: boolean
  is_unarchive_restore?: boolean
  restore_adventurer_id?: number | null
  realm_code?: S1RealmCode
}

export interface ReservationAppearanceHistory {
  items: {
    incarnation: number
    appeared_at: string
    location: string
    party_name: string | null
    excerpts: string[]
  }[]
  total: number
  page: number
  per_page: number
  pages: number
  has_next: boolean
  has_prev: boolean
}

export interface ArchiveAppeal {
  id: number
  user_id?: number
  username?: string
  nickname?: string | null
  adventurer_id: number
  adventurer_name?: string | null
  reason: string
  status: string
  reservation_id?: number | null
  reject_reason?: string | null
  archive_reason?: string | null
  archived_at?: string | null
  reviewed_at?: string | null
  created_at: string | null
}

export interface ReservationCapacity {
  alive_count: number
  total_beds: number
  empty_beds: number
  duplicate_beds?: number
}

export interface TavernUpgradeProgress {
  tavern_level: number
  max_level: number
  alive_adventurer_count: number
  next_level: number | null
  next_level_requirement: number | null
  progress_current: number
  progress_required: number
  progress_percent: number
  requirement_met: boolean
  awaiting_admin_unlock: boolean
  building_upgrade_unlocked: boolean
  building_max_level: number
  upgrade_ui_enabled: boolean
}

export interface ReservationAltMember {
  user_id: number
  username: string
  nickname: string
  register_ip: string | null
  last_login_ip: string | null
  is_frozen?: boolean
  reservation_id: number
  adventurer_name: string
  priority: string
  status: string
  active_adventurer_count?: number
  adventurers?: { id: number; name: string; status: string; level: number }[]
}

export interface ReservationAltGroup {
  group_id: number
  member_count: number
  pending_count?: number
  drawn_count?: number
  link_summary: string
  link_types: string[]
  members: ReservationAltMember[]
}

export interface ReservationAltFlaggedMember extends ReservationAltMember {
  group_id: number
  link_summary: string
}

export interface ReservationAltDetectResult {
  groups: ReservationAltGroup[]
  user_group_map: Record<number, number>
  flagged_members?: ReservationAltFlaggedMember[]
  total_pending: number
  total_drawn?: number
  total_scanned?: number
  group_count: number
  flagged_count: number
}

export interface RelationshipEntry {
  adventurer_id: number
  adventurer_name: string
}

export interface ItemInstance {
  id: number
  name: string
  type?: string
  desc?: string
  rarity: string
  quantity?: number
  uses_left?: number
  recovery_minutes?: number
  recovering?: boolean
  recovery_ends_at?: string | null
  damage_stage?: string
  damage_note?: string
  evolution_stage?: number
  is_player_locked?: boolean
  clue_label?: string | null
  created_at?: string | null
}

export function isPlayableAdventurer(adv: Pick<AdventurerData, 'status'> | null | undefined): boolean {
  return !!adv && adv.status !== 'dead' && adv.status !== 'archived'
}

export interface AdventurerTitle {
  id: number
  code: string
  name: string
  description: string
  frame_key: 'standard' | 'fate' | 'fate_companion' | 'hidden_relic'
  is_system?: boolean
  is_enabled?: boolean
  manual_grant_count?: number
  exchange_grant_count?: number
  automatic_holder_count?: number
}

export interface TitleExchangeIngredient {
  item_name: string
  rarity: string
  required: number
}

export interface AdventurerTitlePreview extends AdventurerTitle {
  condition: string
  progress_current: number
  progress_required: number
  condition_completed: boolean
  is_available: boolean
  exchange_ingredients?: TitleExchangeIngredient[]
}

export interface SquadEmblemData {
  frame: string
  pattern: string
  sigil: string
  primary: string
  secondary: string
  accent: string
}

export interface SquadBadgeData {
  squad_id?: number
  squad_name?: string
  name?: string
  emblem?: SquadEmblemData
  frame?: string
  pattern?: string
  sigil?: string
  primary?: string
  secondary?: string
  accent?: string
}

export interface SquadCatalogOption {
  key: string
  label: string
  value?: string
}

export interface SquadEmblemCatalog {
  frames: SquadCatalogOption[]
  patterns: SquadCatalogOption[]
  sigils: SquadCatalogOption[]
  colors: SquadCatalogOption[]
  default_emblem: SquadEmblemData
}

export interface SquadFeature {
  create_gold_cost: number
  member_slots: number
  total_capacity: number
  name_max_length: number
  catalog: SquadEmblemCatalog
}

export interface SquadMember {
  adventurer_id?: number
  id?: number
  name: string
  level?: number
  character_class?: string | null
  is_leader?: boolean
  role?: 'leader' | 'member'
}

export interface SquadData {
  id: number
  name: string
  emblem: SquadEmblemData
  leader_adventurer_id?: number
  leader_id?: number
  leader_name?: string
  member_count?: number
  member_slots?: number
  total_capacity?: number
  is_full?: boolean
  members?: SquadMember[]
  role?: 'leader' | 'member'
  is_leader?: boolean
}

export interface SquadOverview {
  feature: SquadFeature
  my_squad: SquadData | null
  squads: {
    items: SquadData[]
    total: number
    page: number
    per_page: number
    pages: number
    has_next: boolean
    has_prev: boolean
  }
}

export interface SquadRefreshResult {
  ok: boolean
  squads_ok: boolean
  adventurers_ok: boolean
  message?: string
}

export interface AdventurerData {
  id: number
  name: string
  realm_code?: S1RealmCode
  gender: string
  character_class: string | null
  level: number
  status: string
  hp: number
  max_hp: number
  hp_updated_at?: string | null
  dorm_room_id?: number | null
  strength: number
  agility: number
  intelligence: number
  luck: number
  personality: string[]
  gold: number
  /** 仅疗养所、医院、深渊手术室和茶馆可用的独立临时余额。 */
  low_consumption_gold?: number
  low_consumption_gold_cap?: number
  traits: Array<{
    id?: number
    name: string
    desc: string
    created_at?: string | null
    is_pure_positive?: boolean
    equipped?: boolean
    recovery_policy?: string
    recovery_stage?: string
    recovery_progress?: number
    recovery_required?: number | null
    recovery?: TraitRecoverySnapshot
  }>
  trait_recovery_computed_at?: string | null
  equipment: ItemInstance[]
  /** 私人库存仅在本人详情中返回，公共详情和列表不下发。 */
  warehouse?: ItemInstance[]
  warehouse_capacity_limit?: number
  materials?: Array<{ name: string; quantity: number; display_name: string; icon: string }>
  skills?: Array<{
    id: number
    name: string
    description: string
    equipped: boolean
    recovery_minutes?: number
    recovering?: boolean
    recovery_ends_at?: string | null
  }>
  history_story?: string
  /** 非管理员详情不下发全文，改走 history-story 分页接口 */
  history_entry_count?: number
  is_player_created: boolean
  /** 抽中时使用了内部占位名；仅本人可见，成功命名一次后永久关闭。 */
  initial_rename_available?: boolean
  owner_id: number | null
  squad_badge?: SquadBadgeData | null
  active_title?: AdventurerTitle | null
  available_titles?: AdventurerTitle[]
  title_previews?: AdventurerTitlePreview[]
  party_preference?: string
  recovery_time: string | null
  rest_start_time: string | null
  bed_index?: number | null
  trait_recovery_cycle_hours?: number
  latest_score: number | null
  praise_count?: number
  building_instance_id: number | null
  building_instance_status: string | null
  building_handler_key: string | null
  stamina: number
  max_stamina: number
  stamina_updated_at: string | null
  /** 累计已用修炼次数（力量/敏捷/智力三处共用） */
  training_count?: number
  /** 修炼总上限（随等级成长） */
  training_cap?: number
  insurance_active?: boolean
  empty_return_insurance_active?: boolean
  comrade_memorial_insurance_active?: boolean
  is_followed: boolean
  tavern_seated?: boolean
  tea_house_seated?: boolean
  /** 所属宿舍正在升级；酒馆/茶舍等外出恢复可显式覆盖该暂停态。 */
  dorm_recovery_paused?: boolean
  /** 当前宿舍床位的恢复速率；场所专属 stamina_recovery_per_minute 可临时覆盖。 */
  dorm_stamina_recovery_per_minute?: number
  stamina_recovery_per_minute?: number
  expedition_buff?: {
    label?: string
  } | null
  // 详情接口附加：最亲密 / 最不和各 1 人（仅身份，后端不下发具体好感度数值）
  relationships?: {
    best: RelationshipEntry | null
    worst: RelationshipEntry | null
  }
}

export interface PaginationMeta {
  current_page: number
  pages: number
  total: number
}

export interface PartyData {
  id: number
  name: string
  status: string
  target_dungeon: string | null
  target_dungeon_level_desc?: string | null
  target_size: number
  departure_time: string | null
  member_count: number
  members: Array<{
    id: number
    name: string
    hp: number
    max_hp: number
    active_title?: AdventurerTitle | null
  }>
  departure_vote_count?: number
  departure_voted_adventurer_ids?: number[]
}

export interface MatchTicketData {
  id: number
  is_owner?: boolean
  can_edit_description?: boolean
  status: string
  queue_type: string
  access_mode?: string
  allow_solo_fill?: boolean
  invite_code?: string | null
  preferred_dungeon: string | null
  preferred_dungeons?: string[]
  preferred_dungeon_level_desc?: string | null
  description?: string
  campaign_task_title?: string | null
  campaign_departure_deadline?: string | null
  match_error?: string
  created_at: string | null
  queued_at?: string | null
  member_count: number
  queue_parties_ahead?: number
  ready_member_threshold?: number
  matched_party_id?: number | null
  matched_party_status?: string | null
  matched_party_dungeon?: string | null
  matched_party_dungeon_level_desc?: string | null
  matched_party_departure_time?: string | null
  has_fate_dungeon_risk?: boolean
  fate_risk_member_names?: string[]
  pending_fate_risk_member_names?: string[]
  fate_risk_confirmation_required?: boolean
  members: Array<{
    adventurer_id: number
    name?: string | null
    level?: number | null
    active_title?: AdventurerTitle | null
  }>
}

export interface MatchDungeonOption {
  name: string
  level_desc?: string | null
  rank?: number
}

export interface MatchInviteCandidate {
  adventurer_id: number
  name: string
  level: number
  status: string
  is_followed: boolean
}

export interface MatchInviteData {
  id: number
  created_at: string | null
  ticket?: {
    invite_code?: string | null
    preferred_dungeon?: string | null
    description?: string
    queue_type?: string
    member_count?: number
  } | null
}

export interface ExpeditionLogSummary {
  id: number
  party_id: number | null
  party_name: string | null
  target_dungeon: string
  hidden_dungeon?: string | null
  temporary_location_name?: string | null
  result_summary: string | null
  status: string
  special_event_theme?: string | null
  is_favorited: boolean
  created_at: string | null
}

export type AiFeedbackTargetType = 'expedition_log' | 'item_assembly' | 'ember_kitchen' | 'skill_training'
  | 'appeal_first_review' | 'appeal_second_review'

/** 玩家白名单投影；AI 路由、模型和请求标识只存在于管理员接口。 */
export interface AiFeedbackTarget {
  target_type: AiFeedbackTargetType
  target_id: number
  content_label: string
  quality_score: number | null
  preference_score: number | null
}

// 个人交易市场挂单；status 等成交回执字段仅「我的挂单」返回
export interface MarketListing {
  id: number
  item_name: string
  clue_label?: string | null
  item_type: string
  rarity: string
  quantity?: number
  uses_left: number | null
  evolution_stage?: number
  desc: string
  listing_type: 'barter' | 'sell'
  price?: number | null  // sell 模式的售价
  want_note?: string | null  // barter 卖家备注想要什么
  offers_count?: number  // 待出价数（卖家视角）
  seller_adventurer_id: number
  seller_name: string | null
  created_at: string | null
  status?: string
  traded_item_name?: string | null
  traded_clue_label?: string | null
  buyer_name?: string | null
  seller_fee_paid?: number
  completed_at?: string | null
}

export interface MarketOffer {
  id: number
  listing_id: number
  buyer_adventurer_id: number
  buyer_name?: string | null
  offered_item_name: string
  offered_clue_label?: string | null
  offered_item_type: string
  offered_rarity: string
  offered_quantity?: number
  listing_quantity?: number | null
  offered_uses_left?: number | null
  offered_evolution_stage?: number | null
  offered_desc?: string
  status: 'pending' | 'accepted' | 'rejected' | 'cancelled'
  created_at: string | null
  listing?: {
    id: number
    item_name: string
    clue_label?: string | null
    item_type?: string
    rarity: string
    evolution_stage?: number
    status?: string
    seller_name?: string
  }
}

export interface MarketBuyOrder {
  id: number
  buyer_adventurer_id: number
  buyer_name: string | null
  note?: string | null
  prices: Record<string, number>
  keep_open: boolean
  offers_count?: number
  created_at: string | null
  status?: 'active' | 'completed' | 'cancelled'
  accepted_offer_id?: number | null
  price_paid?: number | null
  seller_income?: number | null
  market_fee?: number
  completed_at?: string | null
  accepted_offer?: MarketBuyOffer | null
  accepted_offers?: MarketBuyOffer[]
}

export interface MarketBuyOffer {
  id: number
  buy_order_id: number
  seller_adventurer_id: number
  seller_name?: string | null
  item_name: string
  clue_label?: string | null
  item_type: string
  rarity: string
  desc?: string
  quantity: number
  uses_left?: number | null
  evolution_stage?: number | null
  status: 'pending' | 'accepted' | 'rejected' | 'cancelled'
  created_at: string | null
  buy_order?: {
    id: number
    buyer_name?: string | null
    note?: string | null
    prices: Record<string, number>
    keep_open?: boolean
    status?: string
  }
}

export interface SecretAuctionLot {
  id: number
  item_name: string
  item_type: string
  rarity: string
  quantity?: number
  desc: string
  ends_at: string | null
  bid_count: number
  reserve_price: number
  is_immutable: boolean
  my_bid_amount?: number | null
}

export interface SecretAuctionLotHistory {
  id: number
  item_name: string
  rarity: string
  quantity?: number
  desc: string
  ends_at: string | null
  status: 'settled_won' | 'settled_pass' | 'cancelled'
  bid_count: number
  is_immutable: boolean
  winner_name?: string | null
  my_result?: 'won' | 'lost' | null
}

export interface SecretAuctionBidAdmin {
  id: number
  adventurer_id: number
  adventurer_name: string | null
  amount?: number | null
  status: string
  created_at: string | null
}

export interface SecretAuctionLotAdmin extends SecretAuctionLot {
  status: string
  uses_left?: number | null
  winning_amount?: number | null
  winner_adventurer_id?: number | null
  created_at?: string | null
  created_by_user_id?: number | null
  bids?: SecretAuctionBidAdmin[]
  bid_amounts_hidden?: boolean
}

// 探险好感度变化摘要
export interface RelationshipChangeEntry {
  adventurer_a_id: number
  adventurer_a_name: string
  adventurer_b_id: number
  adventurer_b_name: string
  direction: 'up' | 'down'
  delta?: number
}

export interface S1ObjectiveOutline {
  objective_situation?: string
  event_chain?: Array<{ stage?: string; event?: string } | string>
  success_conditions?: string[]
  failure_modes?: string[]
  reward_sources?: string[]
  risk_tags?: string[]
  core_conflict?: string
  threat_source?: string
  entry_path?: string
  turning_points?: string[]
  exit_condition?: string
}

export interface ExpeditionSettlementConflictRepairCandidate {
  repair_kind: 'remove_conflicting_consumption' | 'remove_duplicate_new_after_stable_update'
  adventurer_id: number
  adventurer_name: string
  equipment_id: number
  source_name: string
  updated_name: string
  consumed_name: string | null
  quantity: number
  semantic_confirmation_required?: boolean
}


export interface ExpeditionSettlementConflictRepairBlocker {
  code: string
  message: string
  context?: Record<string, unknown>
}

export interface ExpeditionSettlementConflictRepairPreview {
  log_id: number
  party_id: number | null
  log_status: string
  mode: 'apply' | 'retry' | 'none'
  repair_id?: string | null
  repairable: boolean
  can_restore: boolean
  conflict_count?: number
  candidate_count: number
  candidates: ExpeditionSettlementConflictRepairCandidate[]
  blockers: ExpeditionSettlementConflictRepairBlocker[]
  last_replay_error?: string | null
  preview_token: string | null
  execution_available: boolean
}

export interface ExpeditionLogDetail extends ExpeditionLogSummary {
  full_log: any
  comment_counts?: ExpeditionCommentCounts
  special_event_event_index?: number | null
  /** 剧情生成前客观局势草案 AI 输出（仅管理员） */
  objective_outline?: S1ObjectiveOutline | null
  /** 本次探险成员名字（顶层下发，供名字高亮；不在裁剪后的 full_log 内） */
  members?: string[]
  relationship_changes?: RelationshipChangeEntry[]
  ai_feedback?: AiFeedbackTarget
  like_info?: {
    can_like: boolean
    reason: string
    from_adventurer_id: number | null
    liked_adventurer_id: number | null
    liked_adventurer_name: string | null
    candidates: Array<{ id: number; name: string; praise_count: number }>
  }
}

export type ExpeditionCommentTargetType = 'event' | 'member_update'

export interface ExpeditionCommentCounts {
  event: Record<string, number>
  member_update: Record<string, number>
}

export interface ExpeditionComment {
  id: number
  nickname: string
  active_title: string | null
  content: string
  created_at: string | null
}

export interface ExpeditionCommentPage {
  items: ExpeditionComment[]
  page: number
  per_page: number
  total: number
  pages: number
  has_next: boolean
  has_prev: boolean
}

export interface ExpeditionObserverReview {
  id: number
  expedition_log_id: number
  model_used: string
  correlation_id: string | null
  effective_model_used: string | null
  status: 'pending' | 'processing' | 'completed' | 'failed'
  model_contract_status?: 'pending' | 'processing' | 'completed' | 'failed'
  consumption_status?: 'pending' | 'succeeded' | 'failed' | 'not_applicable' | null
  consumption_failure_stage?: string | null
  consumption_error_code?: string | null
  result: Record<string, unknown> | null
  preview?: {
    field: string
    before: unknown
    after: unknown
    changed: boolean
  }[]
  raw_response: string | null
  error_message: string | null
  retry_count: number
  created_at: string | null
  updated_at: string | null
}

export interface ChatMessage {
  id: number
  nickname: string
  active_title: string | null
  content: string
  created_at: string | null
}

export interface DormBed {
  adventurer_id: number
  adventurer_name: string
  owner_id?: number
  status: string
  building_instance_status?: string | null
  tavern_seated?: boolean
}

export interface DormRoom {
  id: number
  name: string
  capacity: number
  level: number
  beds: (DormBed | null)[]
  is_mine?: boolean
  has_followed?: boolean
  can_move_in?: boolean
  // 一次性命名权：完工时授予综合贡献最大的玩家，naming_used=true 后失效
  naming_user_id: number | null
  naming_used: boolean
  // Lv2 升级完工时在住客中授予的一次性命名权
  upgrade_naming_user_id: number | null
  upgrade_naming_used: boolean
  // 来源建筑实例 ID（用于查看贡献者；种子预建房间为 null）
  building_instance_id: number | null
  // 玩家自建宿舍关联的建筑升级状态（种子预置房间无这些字段）
  upgrade_level?: number
  building_status?: string | null
  upgrade_description?: string | null
  upgrade_required_materials?: Record<string, number>
  upgrade_donations_summary?: Record<string, number>
  my_upgrade_donations?: Record<string, number>
  upgrade_donations_complete?: boolean
  can_start_upgrade?: boolean
  upgrade_progress_percent?: number
  upgrade_remaining_seconds?: number
  upgrade_total_seconds?: number
  upgrade_complete_at?: string | null
  upgrade_speedup_minutes?: number
  upgrade_helpers?: { adventurer_id: number; adventurer_name: string; owner_id?: number }[]
  upgrade_helper_slots?: number
}

export type BuildingCategory = 'general' | 'dormitory' | 'medical' | 'equipment' | 'leisure' | 'gathering' | 'training' | 'market' | 'auction' | 'work' | 'task' | 'memorial'

export interface BuildingTemplate {
  id: number
  code: string
  name: string
  icon: string
  description: string
  building_type: 'unique' | 'multiple'
  category: BuildingCategory
  build_hours: number
  helper_slots: number
  stamina_cost_per_help: number
  speedup_minutes: number
  is_active: boolean
  sort_order: number
  settlement_config?: {
    reward_type?: string
    reward_amount?: number
    reward_material_name?: string
    stat_name?: string
    usage_stamina_cost?: number
    usage_duration_minutes?: number
    stamina_cost_per_hour?: number
    rest_like_recovery?: boolean
    daily_limit?: number
    required_stat?: string
    required_stat_min?: number
    required_level?: number
    description?: string
    gold_cost?: number
    gold_cost_per_hp?: number
    usage_duration_minutes_per_hp?: number
    cooldown_hours?: number
    gold_cost_per_hour?: number
    gold_cost_per_minute?: number
    hp_percent_per_minute?: number
    usage_slots?: number
    unlimited_usage?: boolean
    max_search_hours?: number
    result_source?: string
    input_materials?: Record<string, number>
  } | null
  produced_beds?: number | null
  required_materials?: Record<string, number> | null
  handler_key?: string | null
}

export interface BuildingHelper {
  adventurer_id: number
  adventurer_name: string
  owner_id?: number
  stamina?: number
  building_assigned_at?: string | null
  building_usage_extra?: {
    usage_duration_minutes?: number
    missing_hp?: number
    skill_code?: string
    skill_name?: string
    operation?: 'learn' | 'advance'
    direction?: string
    target_skill_id?: number
    target_skill_name?: string
    stamina_cost?: number
    companion_id?: number
    auxiliary_material?: { name?: string } | null
    search_hours?: number
    rental_start_at?: string
  } | null
}

export interface ShopCatalogItem {
  id: number
  name: string
  type: string
  description: string
  rarity: string
  price: number
  stock: number
  immutable?: boolean
  /** 单次购买入账数量（如箭矢一组 10 支） */
  quantity?: number
  can_buy: boolean
  reject_reason: string | null
}

export interface ShopCatalog {
  building_id: number
  shop_name: string
  shop_code: string
  sale_ends_at?: string | null
  sale_starts_at?: string | null
  server_now?: string
  sale_closed_reason?: string | null
  adventurer: { id: number; name: string; gold: number } | null
  items: ShopCatalogItem[]
}

export interface CatalogHistorySyncCounts {
  equipment: number
  warehouse: number
  market_listings: number
  market_offers: number
  market_buy_offers: number
  secret_auction_lots: number
  market_total: number
  total: number
}

export interface CatalogHistorySyncOldSnapshotInput {
  name: string
  type: string
  description: string
  rarity: string
  is_immutable?: boolean
  default_uses_left?: number
}

export interface CatalogHistorySyncSnapshot {
  id?: number
  name: string
  type: string
  description: string
  rarity: string
  has_is_immutable?: boolean
  is_immutable?: boolean | null
  has_default_uses_left?: boolean
  default_uses_left?: number | null
}

export interface CatalogHistorySyncSample {
  target: keyof Omit<CatalogHistorySyncCounts, 'market_total' | 'total'>
  target_id: number
  before: {
    name: string
    type: string
    rarity: string
    description: string | null
    uses_left: number | null
    is_immutable?: boolean | null
  }
  after: {
    name: string
    type: string
    rarity: string
    description: string | null
    uses_left: number | null
    is_immutable?: boolean
  }
  content_changed: boolean
  default_uses_changed: boolean
  immutable_changed: boolean
}

export interface CatalogHistorySyncPreview {
  preview_token: string
  preview_id: string
  catalog_item: CatalogHistorySyncSnapshot & { id: number }
  old_snapshot: CatalogHistorySyncSnapshot
  counts: CatalogHistorySyncCounts
  content_counts: CatalogHistorySyncCounts
  default_uses_counts: CatalogHistorySyncCounts
  scan: {
    exact_old_rows: number
    already_current_rows: number
  }
  samples: CatalogHistorySyncSample[]
  market_rarity_conflicts: number
  blocked_reason: string | null
  has_backup: boolean
  latest_backup_run_id?: string | null
  latest_backup_summary?: {
    run_id: string
    status: 'active' | 'partially_restored'
    created_at: string
    note: string
    remaining_entries: number
    old_snapshot: CatalogHistorySyncSnapshot | null
    current_snapshot: CatalogHistorySyncSnapshot | null
    repaired: CatalogHistorySyncCounts
  } | null
  historical_market_rows_updated: 0
}

export interface CatalogHistorySyncExecuteResult {
  run_id: string | null
  catalog_item_id: number
  catalog_name: string
  repaired: CatalogHistorySyncCounts
  content_updated: CatalogHistorySyncCounts
  default_uses_updated: CatalogHistorySyncCounts
  already_applied: boolean
}

export interface CatalogHistorySyncRestoreResult {
  run_id: string
  catalog_item_id: number
  restored: CatalogHistorySyncCounts
  skipped_changed: number
  skipped_frozen_history: number
  skipped_missing: number
  has_backup: boolean
  already_restored?: boolean
}

export interface BuybackSellableItem {
  id: number
  source: 'equipment' | 'warehouse'
  name: string
  type: string
  description: string
  rarity: string
  uses_left: number | null
  created_at: string | null
  quantity?: number
  recycle_price: number
  recycle_total?: number
  can_sell: boolean
  reject_reason: string | null
  is_player_locked?: boolean
}

export interface BuybackCatalog {
  building_id: number
  shop_name: string
  adventurer: { id: number; name: string; gold: number } | null
  carry_items: BuybackSellableItem[]
  warehouse_items: BuybackSellableItem[]
}

export interface BuybackSellRequest {
  source: 'equipment' | 'warehouse'
  item_id: number
  quantity?: number
}

export interface GamblingBetPublic {
  adventurer_id: number
  adventurer_name: string | null
  bet_amount: number
  choice: 'high' | 'low'
  choice_label: string
  won: boolean | null
  payout: number
  created_at: string | null
  refunded?: boolean
  result_label?: string
}

export interface GamblingRoundView {
  round_id: number
  round_epoch: number
  status: 'betting' | 'settled' | 'cancelled'
  closes_at: string
  seconds_remaining: number
  bets: GamblingBetPublic[]
  bet_count: number
  my_bet: GamblingBetPublic | null
  dice?: [number, number] | null
  dice_sum?: number | null
  outcome?: 'high' | 'low' | 'house' | 'cancelled' | null
  outcome_label?: string
  settled_at?: string | null
}

export interface GamblingSeat {
  index: number
  adventurer: { id: number; name: string; owner_id?: number } | null
}

export interface GamblingDealer {
  id: number
  name: string
  owner_id?: number
  tips_received: number
}

export interface GamblingStatus {
  building_id: number
  hall_name: string
  is_open: boolean
  active_dealers: number
  dealers: GamblingDealer[]
  tip_tiers: number[]
  linked_work_name: string
  seat_count: number
  seated_count: number
  seats: GamblingSeat[]
  min_bet: number
  max_bet: number
  round_seconds: number
  rules: string
  adventurer: {
    id: number
    name: string
    gold: number
    is_seated: boolean
    can_sit: boolean
    reject_reason: string | null
  } | null
  current_round: GamblingRoundView | null
  last_round: GamblingRoundView | null
}

export interface GamblingBetResult {
  bet: GamblingBetPublic | null
  gold: number
  status: GamblingStatus
}

export interface SkillSchoolOffer {
  code: string
  name: string
  description: string
  gold_cost: number
  learned: boolean
  can_learn: boolean
  reject_reason: string | null
}

export interface SkillSchoolOwnedSkill {
  id: number
  name: string
  description: string
  equipped: boolean
}

export interface SkillSchoolCustomOperation {
  gold_cost: number
  can_start: boolean
  reject_reason: string | null
}

export interface SkillSchoolRetryOpportunity {
  operation?: 'learn' | 'advance'
  direction?: string
  target_skill?: {
    name: string
    description: string
    recovery_minutes: number
  } | null
  gold_cost?: number
  stamina_cost?: number
  usage_duration_minutes?: number
  created_at?: string
  can_retry: boolean
  reject_reason: string | null
}

export interface SkillSchoolCustomTraining {
  direction_max_length: number
  usage_duration_minutes: number
  stamina_cost: number
  cooldown_hours: number
  cooldown_remaining_seconds: number
  helper_slots: number
  current_users: number
  queue: {
    waiting_count: number
    position: number | null
    is_waiting: boolean
  }
  queued_request?: {
    operation: 'learn' | 'advance'
    direction: string
    target_skill_name: string | null
  } | null
  operation_config: {
    learn: SkillSchoolCustomOperation
    advance: SkillSchoolCustomOperation
  }
  skills: SkillSchoolOwnedSkill[]
  retry_opportunity: SkillSchoolRetryOpportunity | null
}

export interface SkillSchoolCatalog {
  building_code: string
  usage_duration_minutes: number
  helper_slots: number
  current_users: number
  adventurer: { id: number; name: string; gold: number } | null
  skills: SkillSchoolOffer[]
  custom_training?: SkillSchoolCustomTraining
}

export interface LuckyWheelPoolItem {
  name: string
  type: string
  description: string
  rarity: string
  world_unique?: boolean
  text_locked?: boolean
  item_kind?: string
  grants_skill?: string
  is_fallback?: boolean
}

export interface LuckyWheelLegendaryRotation {
  current: LuckyWheelPoolItem | null
  next: LuckyWheelPoolItem | null
  fallback?: LuckyWheelPoolItem | null
  on_fallback?: boolean
  rotation_index: number
  rotation_total: number
}

export interface LuckyWheelPoolTier {
  rarity: string
  probability: number
  probability_percent: number
  items: LuckyWheelPoolItem[]
}

export interface LuckyWheelPool {
  ticket_item_name: string
  batch_size: number
  tiers: LuckyWheelPoolTier[]
  legendary_rotation: LuckyWheelLegendaryRotation
  adventurer: {
    id: number
    name: string
    gold: number
    ticket_count: number
    last_spin_id: number
    can_spin?: boolean
    spin_block_reason?: string | null
  } | null
}

export interface LuckyWheelSpinResult {
  rarity: string
  item_name: string
  item_type: string
  item_description: string
  item_rarity: string
  item?: LuckyWheelPoolItem
  grant_destination?: 'equipment' | 'warehouse' | null
  payment: 'gold' | 'ticket'
  gold_cost: number
  ticket_item_name: string
  legendary_rotation?: LuckyWheelLegendaryRotation
  legendary_tier?: LuckyWheelPoolTier
  adventurer?: {
    id: number
    name: string
    gold: number
    ticket_count: number
    last_spin_id: number
    can_spin?: boolean
    spin_block_reason?: string | null
  }
}

export interface LuckyWheelBatchResult {
  results: LuckyWheelSpinResult[]
  requested_count: number
  completed_count: number
  stop_reason: string | null
  pool: LuckyWheelPool
}

export interface LuckyWheelHistoryItem {
  id: number
  item_name: string
  item_rarity: string
  payment: 'gold' | 'ticket'
  gold_cost: number
  created_at: string | null
}

export interface LuckyWheelHistoryPage {
  items: LuckyWheelHistoryItem[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

export interface BuildingInstance {
  id: number
  /** 后端尚未 seed 时由建筑首页注入的只读预览占位，不得用于任何建筑接口。 */
  client_preview_only?: boolean
  template_id: number
  custom_name: string | null
  status: string
  upgrade_level: number
  applicant_user_id?: number | null
  build_started_at: string | null
  build_complete_at: string | null
  upgrade_started_at?: string | null
  upgrade_complete_at?: string | null
  donation_open?: boolean
  progress_percent: number
  remaining_seconds: number
  total_build_seconds: number
  helpers: BuildingHelper[]
  active_helpers_count?: number
  /** 列表/详情接口：含 Lv2 升级加成后的实际使用并发位 */
  effective_usage_slots?: number
  disabled?: boolean
  today_usage_count?: number
  /** 详情接口白名单字段；当前玩家冒险者在该建筑的下一次可用 UTC 时间，未冷却时为 null。 */
  cooldown_available_at?: string | null
  /** 支持候场的异步设施：只公开总人数与当前玩家自己的位置。 */
  usage_queue?: {
    waiting_count: number
    position: number | null
    is_waiting: boolean
  }
  template: BuildingTemplate & { upgrade_config?: Record<string, any> | null }
  donations_summary?: Record<string, number>
  my_donations?: Record<string, number>
  upgrade_required_materials?: Record<string, number>
  upgrade_donations_summary?: Record<string, number>
  my_upgrade_donations?: Record<string, number>
  upgrade_donations_complete?: boolean
  can_start_upgrade?: boolean
  upgrade_description?: string | null
  upgrade_target_level?: number
  upgrade_max_level?: number
  lv2_locked_preview?: boolean
  clue_bureau?: {
    targets: Array<{
      index: number
      label: string
      clue_eligible?: boolean
    }>
    pool_count: number
    pool_cap: number
    grant_eligible_count?: number
  }
  /** 详情接口附带：玩家绑定冒险者摘要（派遣用，非全量 list） */
  my_adventurer?: AdventurerData
  my_craft_inputs?: Array<{ name: string; quantity: number }>
}

export interface TombstoneMessage {
  id: number
  nickname: string
  content: string
  created_at: string | null
}

export interface TombstoneItem {
  id: number
  adventurer_id: number
  name: string
  character_class: string | null
  level: number
  epitaph: string
  died_at: string | null
  message_count?: number
  recent_messages?: TombstoneMessage[]
  tribute_count?: number
  tribute_summary?: TributeFlowerSummary[]
}

export interface TombstoneListResult {
  items: TombstoneItem[]
  total: number
  page: number
  page_size: number
  has_more: boolean
}

export interface TributeFlowerSummary {
  code: string
  name: string
  emoji: string
  total_quantity: number
}

export interface TombstoneTribute {
  id: number
  adventurer_name: string | null
  flower_name: string
  flower_emoji: string
  quantity: number
  message: string | null
  created_at: string | null
}

export interface TombstoneTributeListResult {
  items: TombstoneTribute[]
  total: number
  page: number
  page_size: number
  has_more: boolean
  flower_summary: TributeFlowerSummary[]
}

export interface FlowerCatalogItem {
  code: string
  name: string
  emoji: string
  price: number
  description: string
}

export interface FlowerShopAdventurer {
  id: number
  name: string
  status: string
  gold: number
}

export interface TimeContributor {
  adventurer_id: number
  adventurer_name: string
  effective_hours: number
}

export interface MaterialContributor {
  adventurer_id: number | null
  adventurer_name: string
  user_id: number | null
  materials: Record<string, number>  // 各材料捐献件数
  total_amount: number               // 加权材料贡献（与 donation_contribution 同源）
}

export interface CombinedContributor {
  user_id: number | null
  display_name: string
  effective_hours: number
  material_amount: number            // 加权材料贡献（与 donation_contribution 同源）
  time_share: number
  material_share: number
  score: number
}

export interface BuildingContributors {
  time_contributors: TimeContributor[]
  material_contributors: MaterialContributor[]
  // 按玩家归并的综合排行：工时占比×50% + 材料占比×50%
  combined_contributors: CombinedContributor[]
  /** 按阶段分开的排行（build=初次建造，upgrade=Lv2 升级）；顶层字段仍为合计 */
  by_purpose?: {
    build: Omit<BuildingContributors, 'by_purpose'>
    upgrade: Omit<BuildingContributors, 'by_purpose'>
  }
}

export interface DormitoryInProgressInstance extends BuildingInstance {
  applicant_nickname?: string | null
  applicant_adv_name?: string | null
  is_mine?: boolean
  // {material_name: 累计已捐数}
  donations_summary?: Record<string, number>
  // {material_name: 我捐了多少}
  my_donations?: Record<string, number>
}

export interface DormitoryMyStatus {
  my_materials: Record<string, number>
  in_progress_instances: DormitoryInProgressInstance[]
  built_instances: BuildingInstance[]
  concurrent: {
    current_building: number
    max_concurrent: number
  }
  total: {
    current: number
    max: number
    /** 全服宿舍房间总数（含种子），与下方房间列表一致 */
    room_count?: number
  }
}

export interface DormitorySettingItem {
  key: string
  value: any
  description?: string | null
  type: string
  min_value?: number | null
  max_value?: number | null
}

export interface BedSwapRequest {
  id: number
  requester_user_id: number
  requester_adv_id: number
  requester_adv_name: string | null
  requester_nickname: string | null
  target_adv_id: number
  target_adv_name: string | null
  target_room_id: number | null
  target_bed_index: number | null
  status: string
  created_at: string | null
  expires_at: string | null
}

export interface BuildingTemplateFormData {
  name: string
  icon: string
  description: string
  building_type: 'unique' | 'multiple'
  category: BuildingCategory
  build_hours: number
  helper_slots: number
  stamina_cost_per_help: number
  speedup_minutes: number
  sort_order: number
  handler_key: string
  is_active?: boolean
}

export interface CurrencyBalance {
  [code: string]: number
}

export interface CurrencyLog {
  id: number
  adventurer_id: number
  currency_code: string
  change_amount: number
  balance_after: number
  source_type: string
  related_id: string | null
  related_type: string | null
  description: string | null
  created_at: string | null
}

export interface CurrencyType {
  code: string
  name: string
  icon: string
}

export interface MaterialMeta {
  name: string
  icon: string
}

/** 建筑 AI 结算结果（BuildingUsageLog.result 持久化的 JSON，socket 弹窗同款结构） */
export interface BuildingUsageResult {
  type: string
  success?: boolean
  narrative?: string
  healed_traits?: string[]
  recovery_traits?: Array<{
    name: string
    new_name: string
    new_desc: string
    policy?: string
    stage?: string
    remaining_cycles?: number | null
  }>
  modified_traits?: Array<{
    name: string
    new_name: string
    new_desc: string
    policy?: string
    stage?: string
    remaining_cycles?: number | null
  }>
  gold_refunded?: number
  stamina_refunded?: number
  system_abort?: boolean
  operation?: 'learn' | 'advance'
  direction_fit?: 'not_requested' | 'accepted' | 'adjusted'
  reason?: string
  skill?: SkillSchoolOwnedSkill
  old_skill?: SkillSchoolOwnedSkill
  old_name?: string
  new_name?: string
  new_desc?: string
  uses_left?: number
  is_edible?: boolean
  ingredient_names?: string[]
  cookware_name?: string
  consumed?: boolean
  unreasonable_input?: boolean
  consumed_item_names?: string[]
  consumed_items?: Array<{
    slot_id: number
    position: string
    name: string
  }>
  items_preserved?: boolean
  undo?: {
    kind?: 'skill_advance' | 'blacksmith' | 'tailor_workshop' | 'pet_evolution'
    reverted_at?: string
  }
  pet_preserved?: boolean
  evolution_stage?: number | null
  auxiliary_material_consumed?: boolean
  auxiliary_material_name?: string | null
  input_items?: Array<{ role: 'primary' | 'auxiliary'; name: string; description?: string }>
  carried_item_name?: string
  item_consumed?: boolean
  ai_feedback?: AiFeedbackTarget
}

/** 建筑使用结算奖励（折叠进建筑动态条目，避免与材料/货币流水重复占行） */
export interface ActivityReward {
  kind: 'currency' | 'material'
  code: string
  display_name: string
  icon: string
  change_amount: number
  description?: string
}

export type ActivityFilter =
  | 'all'
  | 'building'
  | 'blacksmith'
  | 'tailoring'
  | 'alchemy'
  | 'assembly'
  | 'cooking'
  | 'healing'
  | 'training'
  | 'pet_evolution'
  | 'currency'
  | 'materials'
  | 'items'
  | 'trait_recovery'

/** 「我的」页签最近动态：货币/材料流水 + 建筑使用记录 + 物品变动的合并时间线条目 */
export interface ActivityEntry {
  kind: 'currency' | 'material' | 'building' | 'item' | 'trait_recovery'
  id: number
  created_at: string | null
  // currency / material
  code?: string
  display_name?: string
  icon?: string
  change_amount?: number
  balance_after?: number
  description?: string
  source_type?: string
  item_name?: string
  item_rarity?: string
  // building
  building_name?: string
  building_icon?: string
  result?: BuildingUsageResult | null
  ai_feedback?: AiFeedbackTarget
  undo_kind?: 'skill_advance' | 'blacksmith' | 'tailor_workshop' | 'pet_evolution'
  undo_available?: boolean
  undo_expires_at?: string | null
  undo_restore_mode?: 'complete' | 'legacy_known_fields'
  undo_item_options?: Array<{
    index: number
    name: string
    type: string
    rarity: string
  }>
  reward?: ActivityReward
  /** 建筑使用扣费（start_building_usage 时扣除的金币等），折叠进建筑条目 */
  cost?: ActivityReward
  /** 建筑使用中止或失败时的退款，折叠进原建筑条目 */
  refund?: ActivityReward
  /** 早期异步建筑在「结果持久化」上线前的历史记录：结果没存下来，已不可考 */
  legacy_no_result?: boolean
  // item
  item_type?: string
  change_type?: string // gain / consume / overflow / equip / unequip
  quantity?: number
  /** 申诉补发/归还物品对应的玩家可见探险日志；不下发内部 relation 字段。 */
  expedition_log_id?: number
  // trait_recovery
  adventurer_id?: number
  adventurer_name?: string
  trait_name?: string
}

export interface TaskTemplate {
  id: string
  name: string
  description: string
  icon: string
  category: string
  completion_type?: 'timer' | 'expedition' | 'building_help' | 'gathering' | 'work' | 'contribution' | 'treasure_hunt' | 'hidden_boss'
  required_dungeon?: string
  required_dungeon_min_level?: number
  required_dungeon_max_level?: number
  required_dungeon_label?: string
  required_random_solo_match?: boolean
  no_damage?: boolean
  help_required_seconds?: number
  gathering_required_seconds?: number
  work_required_sessions?: number
  required_contribution?: number
  contribution_required?: number
  repeatable?: boolean
  max_completions?: number
  completion_count?: number
  completion_remaining?: number
  hidden_boss_progress_count?: number
  hidden_boss_total_count?: number
  hidden_boss_remaining_count?: number
  is_ended?: boolean
  max_acceptances?: number
  acceptance_count?: number
  acceptance_remaining?: number
  reward_drop_mode?: 'random_one'
  requirements: {
    min_level?: number
    required_stat?: string
    required_stat_min?: number
    stamina_cost?: number
  }
  duration_minutes: number
  rewards: {
    gold?: number
    exp?: number
    materials?: Record<string, number>
    stat?: Record<string, number>
    drop_mode?: 'random_one'
    items?: Array<string | {
      name?: string
      item_name?: string
      quantity?: number
      rarity?: string
      type?: string
      description?: string
    }>
  }
  narrative: string
  can_accept?: boolean
  reject_reason?: string | null
}

export interface TaskAdminItem {
  id: string
  source_template_id?: string | null
  is_edition?: boolean
  disabled_task_id?: string
  completion_type?: TaskTemplate['completion_type']
  enabled: boolean
  max_completions?: number
  completion_count?: number
  completion_remaining?: number
  occupied_count?: number
  is_ended?: boolean
  template: {
    name?: string
    description?: string
    icon?: string
    duration_minutes?: number
    required_sessions?: number
    required_dungeon?: string
    no_damage?: boolean
    repeatable?: boolean
    max_completions?: number
    rewards?: TaskTemplate['rewards']
  }
}

export interface TaskInstance {
  id: number
  adventurer_id: number
  task_template_id: string
  building_instance_id: number | null
  status: string
  started_at: string | null
  completed_at: string | null
  created_at: string | null
  completion_type?: 'timer' | 'expedition' | 'building_help' | 'gathering' | 'work' | 'contribution' | 'treasure_hunt' | 'hidden_boss'
  required_dungeon?: string
  required_dungeon_min_level?: number
  required_dungeon_max_level?: number
  required_dungeon_label?: string
  required_random_solo_match?: boolean
  no_damage?: boolean
  help_required_seconds?: number
  help_progress_seconds?: number
  help_remaining_seconds?: number
  gathering_required_seconds?: number
  gathering_progress_seconds?: number
  gathering_remaining_seconds?: number
  work_required_sessions?: number
  work_progress_sessions?: number
  work_remaining_sessions?: number
  contribution_progress?: number
  contribution_required?: number
  contribution_remaining?: number
  hidden_boss_target_label?: string
  hidden_boss_progress_count?: number
  hidden_boss_total_count?: number
  hidden_boss_remaining_count?: number
  template?: TaskTemplate
}

// ── 酒馆大堂座位 / 点酒 ──
export interface TavernSeatAdventurer {
  id: number
  name: string
  is_mine: boolean
}

export interface TavernSeatDrink {
  key: string
  name: string
  icon: string
  price: number
  text: string
  progress_texts: string[]
}

export interface TavernSeatFood {
  key: string
  name: string
  icon: string
  price: number
  duration_minutes: number
  stamina_restore: number
  hp_restore: number
  text: string
  progress_texts?: string[]
  started_at: string
  expires_at: string
}

export interface TavernTreatRequest {
  id: number
  payer_adventurer_id: number
  payer_name: string | null
  target_adventurer_id: number
  target_name: string | null
  target_seat_id: number
  drink_key: string
  drink: TavernDrinkMenuItem | null
  status: string
  created_at: string | null
}

export interface TavernSeat {
  id: number
  zone: 'bar' | 'table'
  table_key: string | null
  table_label: string | null
  label: string
  adventurer: TavernSeatAdventurer | null
  drink: TavernSeatDrink | null
  food: TavernSeatFood | null
  chatter?: string | null
  incoming_treat_requests?: TavernTreatRequest[]
}

export interface TavernDrinkMenuItem {
  key: string
  name: string
  icon: string
  price: number
  desc: string
}

// ── 深渊茶舍座位 ──
export interface TeaHouseTeaMenuItem {
  key: string
  name: string
  icon: string
  price: number
  desc: string
}

export interface TeaHouseSeatTea {
  key: string
  name: string
  icon: string
  price: number
  text: string
}

export interface TeaHouseSeatAdventurer {
  id: number
  name: string
  is_mine: boolean
}

export interface TeaHouseSeat {
  id: number
  table_key: string
  table_label: string
  label: string
  locked?: boolean
  adventurer: TeaHouseSeatAdventurer | null
  tea: TeaHouseSeatTea | null
  chatter?: string | null
}

// ── 深渊温泉座位 ──
export interface HotSpringDrinkMenuItem {
  key: string
  name: string
  icon: string
  price: number
  desc: string
}

export interface HotSpringSeatDrink {
  key: string
  name: string
  icon: string
  price: number
  text: string
}

export interface HotSpringSeatAdventurer {
  id: number
  name: string
  is_mine: boolean
}

export interface HotSpringSeat {
  id: number
  table_key: string
  table_label: string
  label: string
  adventurer: HotSpringSeatAdventurer | null
  drink: HotSpringSeatDrink | null
  chatter?: string | null
}

export interface InsurancePolicyStatus {
  code: 'injury' | 'empty_return' | 'comrade_memorial' | 'no_gold' | 'no_item'
  name: string
  description: string
  premium: number
  payout?: number
  max_payout?: number
  active: boolean
  payout_samples?: Array<{
    loss_pct: number
    payout: number
    label: string
  }>
}

export interface InsuranceStatus {
  adventurer_id: number
  policies: InsurancePolicyStatus[]
  insurance_active: boolean
  premium: number
  max_payout: number
  payout_exponent: number
  payout_samples: Array<{
    loss_pct: number
    payout: number
    label: string
  }>
}

interface ListParams {
  page?: number
  per_page?: number
  status?: string
  search?: string
  sort_field?: string
  sort_order?: string
  exclude_mine?: boolean
}

function toPaginationMeta(d: any): PaginationMeta {
  return { current_page: d?.page ?? 1, pages: d?.pages ?? 1, total: d?.total ?? 0 }
}

/** 就地合并座位数据，避免 silent 刷新时整表替换导致 DOM 闪一下 */
function patchTavernSeatsInPlace(current: TavernSeat[], incoming: TavernSeat[]): boolean {
  if (current.length === 0 || incoming.length !== current.length) return false
  const byId = new Map(incoming.map((s) => [s.id, s]))
  for (let i = 0; i < current.length; i++) {
    const next = byId.get(current[i].id)
    if (!next) return false
    const cur = current[i]
    cur.zone = next.zone
    cur.table_key = next.table_key
    cur.table_label = next.table_label
    cur.label = next.label
    cur.adventurer = next.adventurer
    cur.drink = next.drink
    cur.food = next.food
    cur.chatter = next.chatter
    cur.incoming_treat_requests = next.incoming_treat_requests
  }
  return true
}

/** 根据大堂座位快照，同步各列表里的 tavern_seated 标记（入座不改变 status） */
function syncTavernSeatedFromSeats(
  seats: TavernSeat[],
  lists: AdventurerData[][],
) {
  const seatedIds = new Set(
    seats.map((s) => s.adventurer?.id).filter((id): id is number => id != null),
  )
  for (const list of lists) {
    for (const adv of list) {
      const seated = seatedIds.has(adv.id)
      if (seated) adv.tavern_seated = true
      else delete adv.tavern_seated
    }
  }
}

export interface TavernFoodMenuItem {
  key: string
  name: string
  icon: string
  price: number
  duration_minutes: number
  stamina_restore: number
  hp_restore: number
  desc: string
}

/** 根据茶舍座位快照同步实时精力恢复速率。茶舍速率替代宿舍床位速率。 */
function syncTeaHouseSeatedFromSeats(
  seats: TeaHouseSeat[],
  staminaPerMinute: number,
  lists: AdventurerData[][],
) {
  const seatedIds = new Set(
    seats.map((s) => s.adventurer?.id).filter((id): id is number => id != null),
  )
  for (const list of lists) {
    for (const adv of list) {
      if (seatedIds.has(adv.id)) {
        adv.tea_house_seated = true
        adv.stamina_recovery_per_minute = staminaPerMinute
      } else {
        delete adv.tea_house_seated
        delete adv.stamina_recovery_per_minute
      }
    }
  }
}

function isValidDrinkMenu(items: unknown): items is TavernDrinkMenuItem[] {
  return (
    Array.isArray(items)
    && items.length > 0
    && items.every(
      (d) => !!d && typeof d === 'object' && typeof (d as TavernDrinkMenuItem).key === 'string',
    )
  )
}

function isValidFoodMenu(items: unknown): items is TavernFoodMenuItem[] {
  return Array.isArray(items) && items.length > 0 && items.every(
    (item) => !!item && typeof item === 'object' && typeof (item as TavernFoodMenuItem).key === 'string',
  )
}

function patchTavernSeated(lists: AdventurerData[][], advId: number, seated: boolean) {
  for (const list of lists) {
    const adv = list.find((a) => a.id === advId)
    if (!adv) continue
    if (seated) {
      adv.tavern_seated = true
      delete adv.tea_house_seated
      delete adv.stamina_recovery_per_minute
    }
    else delete adv.tavern_seated
  }
}

function patchDormRecoveryPausedForRoom(
  lists: AdventurerData[][],
  roomId: number,
  paused: boolean,
) {
  for (const list of lists) {
    for (const adv of list) {
      if (adv.dorm_room_id === roomId) adv.dorm_recovery_paused = paused
    }
  }
}

function syncDormRecoveryPaused(
  rooms: DormRoom[],
  lists: AdventurerData[][],
) {
  const pauseByRoom = new Map(
    rooms.map((room) => [room.id, room.building_status === 'upgrading']),
  )
  for (const list of lists) {
    for (const adv of list) {
      if (adv.dorm_room_id == null) {
        adv.dorm_recovery_paused = false
      }
      else if (pauseByRoom.has(adv.dorm_room_id)) {
        adv.dorm_recovery_paused = pauseByRoom.get(adv.dorm_room_id) === true
      }
    }
  }
}

interface TavernRecoverySnapshot {
  adventurer_id: number
  stamina: number
  stamina_updated_at: string | null
  hp: number
  hp_updated_at: string | null
  dorm_recovery_paused: boolean
}

function patchTavernRecovery(
  lists: AdventurerData[][],
  snapshot: TavernRecoverySnapshot,
) {
  for (const list of lists) {
    const adv = list.find((a) => a.id === snapshot.adventurer_id)
    if (!adv) continue
    adv.stamina = snapshot.stamina
    adv.stamina_updated_at = snapshot.stamina_updated_at
    adv.hp = snapshot.hp
    adv.hp_updated_at = snapshot.hp_updated_at
    adv.dorm_recovery_paused = snapshot.dorm_recovery_paused
  }
}

export type FateSettlementRepairResourceType = 'skill' | 'trait'

export interface FateSettlementRepairItem {
  resource_type: FateSettlementRepairResourceType
  adventurer_id: number
  adventurer_name: string
  name: string
  description: string
  metadata: Record<string, unknown>
  incident_log_id: number
  last_evidence_log_id: number
  created_id?: number
  run_id?: string
}

export interface FateSettlementRepairAmbiguousItem {
  expedition_log_id: number
  adventurer_id: number
  resource_type: FateSettlementRepairResourceType
  name: string
  reason: 'frozen_snapshot_missing' | 'later_structured_change' | string
  later_log_id?: number
}

export interface FateSettlementUnreviewedDestructiveLog {
  expedition_log_id: number
  adventurer_id: number
  adventurer_name: string
  removed_traits: string[]
  removed_skills: string[]
  snapshot_missing_traits: string[]
  snapshot_missing_skills: string[]
  same_log_changed_traits: string[]
  same_log_changed_skills: string[]
  currently_missing_traits: string[]
  currently_missing_skills: string[]
}

export type FateHistoricalReviewDecision =
  | 'preserve_existing'
  | 'retire_without_successor'
  | 'replace_with_successors'

export interface FateHistoricalReviewEvent {
  event_index: number
  source: string
  content: string
}

export interface FateHistoricalReviewRemovedAsset {
  action_ref: string
  adventurer_id: number
  adventurer_name: string
  resource_type: FateSettlementRepairResourceType
  name: string
  description: string
  metadata: Record<string, unknown>
  snapshot_available: boolean
  snapshot_conflict: boolean
  same_log_changed: boolean
  same_log_successor_refs: string[]
  currently_missing: boolean
  later_structured_touches: Array<{
    expedition_log_id: number
    removed: boolean
    added: boolean
  }>
}

export interface FateHistoricalReviewEvidence {
  schema_version: number
  expedition_log_id: number
  party_name: string
  target_dungeon: string
  scan_max_log_id: number
  events: FateHistoricalReviewEvent[]
  removed_assets: FateHistoricalReviewRemovedAsset[]
  same_log_successors: Array<{
    successor_ref: string
    adventurer_id: number
    resource_type: FateSettlementRepairResourceType
    name: string
    description: string
  }>
  evidence_digest: string
}

export interface FateHistoricalReviewAction {
  action_ref: string
  decision: FateHistoricalReviewDecision
  successor_refs: string[]
  evidence_event_indexes: number[]
  reason: string
}

export interface FateHistoricalReviewResult {
  schema_version: number
  evidence_digest: string
  actions: FateHistoricalReviewAction[]
  source: string
  metadata: Record<string, string>
}

export interface FateHistoricalReviewEvidenceResponse {
  evidence_token: string
  evidence: FateHistoricalReviewEvidence
}

export interface FateHistoricalReviewPreviewResponse {
  review: FateHistoricalReviewResult
  repair_preview: FateSettlementRepairPreview
  excluded_items: Array<FateHistoricalReviewRemovedAsset & {
    expedition_log_id: number
    later_log_id?: number | null
    review_action: FateHistoricalReviewAction
    reason: string
  }>
}

export interface FateSettlementCompanionContinuityRiskItem {
  expedition_log_id: number
  party_name: string
  created_at: string | null
  adventurer_id: number
  adventurer_name: string
  consumed_name: string
  consumed_quantity: number
  departure_instance_count: number
  departure_stage: number
  departure_description_length: number
  reviewed_surviving_name: string | null
  new_item_count: number
  new_items: Array<{
    name: string
    type: string
    evolution_stage: number
    description_length: number
  }>
  new_items_truncated: boolean
  risk_codes: string[]
  auto_action: 'none'
}

export interface FateSettlementCompanionContinuityAudit {
  scanned_logs: number
  scan_limit: number
  scan_truncated: boolean
  reviewed_binding_count: number
  risk_count: number
  mapping_ambiguous_count: number
  stage_risk_count: number
  type_risk_count: number
  description_risk_count: number
  risk_samples: FateSettlementCompanionContinuityRiskItem[]
  risk_samples_truncated: boolean
  calculation_basis: string
}

export type FateSettlementRetryStatus =
  | 'reopen'
  | 'already_available'
  | 'later_completed'
  | 'ambiguous'

export interface FateSettlementRetryStatusItem {
  adventurer_id: number
  adventurer_name: string
  journey_id: number
  current_journey_id?: number
  incident_log_id: number
  retry_status: FateSettlementRetryStatus
  reason?: string
  current_status?: string
  later_final_log_id?: number
  expected_state?: Record<string, unknown>
}

export interface FateSettlementRepairPreview {
  scan_max_log_id: number
  scanned_logs: number
  incident_logs: number
  truncated: boolean
  candidate_count: number
  ambiguous_count: number
  items: FateSettlementRepairItem[]
  ambiguous_items: FateSettlementRepairAmbiguousItem[]
  retry_reopen_count: number
  retry_already_available_count: number
  retry_later_completed_count: number
  retry_ambiguous_count: number
  retry_status_items: FateSettlementRetryStatusItem[]
  later_completed_resource_count: number
  later_completed_resource_items: FateSettlementRepairItem[]
  unreviewed_scan_logs: number
  unreviewed_destructive_log_count: number
  unreviewed_destructive_member_count: number
  unreviewed_destructive_item_count: number
  unreviewed_current_missing_count: number
  unreviewed_snapshot_missing_count: number
  unreviewed_destructive_logs: FateSettlementUnreviewedDestructiveLog[]
  companion_continuity_audit: FateSettlementCompanionContinuityAudit
  preview_token: string
}

export interface FateSettlementRepairExecuteResult {
  run_id: string
  created_count: number
  skipped_count: number
  items: FateSettlementRepairItem[]
  reopened_final_count: number
  retry_skipped_count: number
  reopened_journeys: Array<Record<string, unknown>>
}

export interface FateSettlementRepairRestoreResult {
  run_id: string
  removed_count: number
  skipped_count: number
  restored_journey_count: number
  journey_skipped_count: number
}

export const useAdventurerS1Store = defineStore('adventurer_s1', {
  state: () => ({
    options: null as CreationOptions | null,
    adventurers: [] as AdventurerData[],
    currentDetail: null as AdventurerData | null,
    loading: false,
    detailLoading: false,
    // ── 酒馆只读层 ──
    notice: '',
    noticeVersion: null as string | null,
    noticeShouldShow: false,
    noticeAckLoading: false,
    tavernUpgrade: null as TavernUpgradeProgress | null,
    tavernUpgradeLoading: false,
    browseAdventurers: [] as AdventurerData[],
    browsePagination: null as PaginationMeta | null,
    browseLoading: false,
    parties: [] as PartyData[],
    partiesPagination: null as PaginationMeta | null,
    partiesLoading: false,
    myParties: [] as PartyData[],
    myPartiesLoading: false,
    matchTickets: [] as MatchTicketData[],
    matchTicketsLoading: false,
    campaignTaskCooldownUntil: null as string | null,
    campaignTaskNotice: null as { message: string; cancelled_at: string } | null,
    campaignTaskPolicy: null as { departure_timeout_minutes: number; timeout_cooldown_minutes: number } | null,
    matchQueuedCount: 0,
    matchQueuedMemberCount: 0,
    matchRandomQueuedMemberCount: 0,
    matchDungeonQueuedMemberCounts: {} as Record<string, number>,
    publicMatchTickets: [] as MatchTicketData[],
    matchDungeonOptions: [] as MatchDungeonOption[],
    matchInvites: [] as MatchInviteData[],
    matchInviteCandidates: [] as MatchInviteCandidate[],
    logs: [] as ExpeditionLogSummary[],
    logsPagination: null as PaginationMeta | null,
    logsLoading: false,
    _expeditionConfig: null as Record<string, any> | null,  // 管理员从 /logs 响应附带的探险配置缓存
    currentLog: null as ExpeditionLogDetail | null,
    chatMessages: [] as ChatMessage[],
    chatHasNext: false,
    chatPage: 1,
    // ── 酒馆大堂座位 ──
    tavernSeats: [] as TavernSeat[],
    tavernSeatsLoading: false,
    drinkMenu: [] as TavernDrinkMenuItem[],
    foodMenu: [] as TavernFoodMenuItem[],
    tavernServerOffsetMs: 0,
    // ── 深渊茶舍座位 ──
    teaHouseSeats: [] as TeaHouseSeat[],
    teaHouseSeatsLoading: false,
    teaHouseMenu: [] as TeaHouseTeaMenuItem[],
    teaHouseStaminaPerMinute: STAMINA_RECOVERY_PER_MINUTE,
    teaHouseEntryFee: 30,
    teaHouseLowConsumptionGold: 0,
    teaHouseLowConsumptionGoldCap: 100,
    // ── 深渊温泉座位 ──
    hotSpringSeats: [] as HotSpringSeat[],
    hotSpringSeatsLoading: false,
    hotSpringMenu: [] as HotSpringDrinkMenuItem[],
    // ── 个人交易市场 ──
    marketListings: [] as MarketListing[],
    marketPendingOfferCount: 0,
    marketPagination: null as PaginationMeta | null,
    marketFees: {} as Record<string, number>,
    marketSellPriceRange: {} as Record<string, { min: number; max: number }>,
    marketSellFeeRate: 0.2,
    marketLoading: false,
    marketSlotInfo: null as {
      active_count: number
      base_slots: number
      extra_slots: number
      max_slots: number
      extra_slots_expires_at: string | null
    } | null,
    myMarketActive: [] as MarketListing[],
    myMarketHistory: [] as MarketListing[],
    myOffers: [] as MarketOffer[],
    listingOffers: [] as MarketOffer[],
    listingOffersTarget: null as MarketListing | null,
    marketBuyOrders: [] as MarketBuyOrder[],
    marketBuyOrdersPagination: null as PaginationMeta | null,
    marketBuyOrderPendingOfferCount: 0,
    marketBuyOrdersLoading: false,
    marketBuyOrdersRequestId: 0,
    myMarketBuyOrdersActive: [] as MarketBuyOrder[],
    myMarketBuyOrdersHistory: [] as MarketBuyOrder[],
    myMarketBuyOffers: [] as MarketBuyOffer[],
    marketBuyOrderOffers: [] as MarketBuyOffer[],
    marketBuyOrderOffersTarget: null as MarketBuyOrder | null,
    marketBuyOrderOffersRequestId: 0,
    // ── 隐秘拍卖行 ──
    secretAuctionLots: [] as SecretAuctionLot[],
    secretAuctionPagination: null as PaginationMeta | null,
    secretAuctionLoading: false,
    secretAuctionHistory: [] as SecretAuctionLotHistory[],
    secretAuctionHistoryPagination: null as PaginationMeta | null,
    secretAuctionHistoryLoading: false,
    // ── 预约 ──
    myReservation: null as Reservation | null,
    myArchiveAppeal: null as ArchiveAppeal | null,
    reservations: [] as Reservation[],
    reservationsPagination: null as PaginationMeta | null,
    reservationsCapacity: null as ReservationCapacity | null,
    reservationsLoading: false,
    archiveAppeals: [] as ArchiveAppeal[],
    archiveAppealsPagination: null as PaginationMeta | null,
    archiveAppealsLoading: false,
    // ── 宿舍区 ──
    dormitory: [] as DormRoom[],
    dormitoryPagination: null as PaginationMeta | null,
    dormitoryLoading: false,
    dormitoryMyStatus: null as DormitoryMyStatus | null,
    dormitoryMyStatusLoading: false,
    swapRequestsIncoming: [] as BedSwapRequest[],
    swapRequestsOutgoing: [] as BedSwapRequest[],
    // ── 建筑 ──
    buildings: [] as BuildingInstance[],
    buildingsLoading: false,
    currentBuilding: null as BuildingInstance | null,
    buildingTemplates: [] as BuildingTemplate[],
    buildingTemplatesLoading: false,
    // ── 货币 ──
    currencyBalances: {} as CurrencyBalance,
    currencyLogs: [] as CurrencyLog[],
    currencyTypes: [] as CurrencyType[],
    // ── 材料 ──
    materialBalances: {} as Record<string, number>,
    materialMeta: {} as Record<string, MaterialMeta>,
    // ── 我的最近动态 ──
    myActivity: [] as ActivityEntry[],
    myActivityFilter: 'all' as ActivityFilter,
    myActivityLoadedFilter: 'all' as ActivityFilter,
    myActivityLoading: false,
    myActivityHasMore: false,
    myActivityNextBefore: null as string | null,
    // ── 任务栏摘要（建筑分区入口展示） ──
    taskAcceptableCount: null as number | null,
    taskAcceptableIds: [] as string[],
    taskAcceptableCountLoading: false,
    // ── 隐秘拍卖行摘要（建筑分区入口展示） ──
    secretAuctionOpenCount: null as number | null,
    secretAuctionOpenCountLoading: false,
    supportVoteStatus: null as SupportVoteStatus | null,
    supportVoteLoading: false,
    // ── 当前存活角色所在区与浏览区（预约不会锁定区服） ──
    realmAssignment: null as RealmAssignment | null,
    realmViewCode: null as S1RealmCode | null,
    realmVisitorMode: false,
    // ── 纹章小队（由 init 下发的服务端开关显式门控） ──
    squadFeatureEnabled: false,
    squadSessionUserId: null as number | null,
    squadFeature: null as SquadFeature | null,
    mySquad: null as SquadData | null,
    squadItems: [] as SquadData[],
    squadPagination: null as SquadOverview['squads'] | null,
    squadsLoading: false,
    squadsStale: true,
  }),

  actions: {
    // ── 创建选项 ──────────────────────────────────────────────────────
    async fetchCreationOptions() {
      this.loading = true
      try {
        const { data } = await api.get('/adventurer-s1/creation-options')
        if (data.code === 200 && data.data) {
          this.options = data.data as CreationOptions
          if (!this.realmVisitorMode) {
            this.realmAssignment = this.options.realm_assignment ?? this.realmAssignment
          }
        }
        return data
      } finally {
        this.loading = false
      }
    },

    async submitReservation(payload: CreatePayload) {
      const { data } = await api.post('/adventurer-s1/reserve', payload)
      if (data.code === 200 || data.code === 201) {
        this.myReservation = data.data as Reservation
        trackEvent('adventurer_s1_submit_reservation', { skill_template_id: payload.skill_template_id })
      }
      return data
    },

    async fetchReservationAppearances(page = 1) {
      const { data } = await api.get('/adventurer-s1/reservation/appearances', {
        params: { page, per_page: 5 },
      })
      return data as { code: number; message?: string; data?: ReservationAppearanceHistory }
    },

    async fetchReservationStatus() {
      const { data } = await api.get('/adventurer-s1/reservation-status')
      if (data.code === 200) {
        this.myReservation = data.data as Reservation | null
      }
      return data
    },

    async cancelReservation() {
      const { data } = await api.post('/adventurer-s1/reservation/cancel')
      if (data.code === 200) {
        this.myReservation = null
      }
      return data
    },

    async updateReservation(payload: Partial<CreatePayload>) {
      const { data } = await api.put('/adventurer-s1/reservation/update', payload)
      if (data.code === 200 && data.data) {
        this.myReservation = data.data as Reservation
      }
      return data
    },

    async renameInitialAdventurer(adventurerId: number, name: string) {
      const { data } = await api.put(`/adventurer-s1/${adventurerId}/initial-name`, { name })
      if (data.code === 200 && data.data) {
        const updated = data.data as AdventurerData
        const patch = (items: AdventurerData[]) => {
          const current = items.find(adv => adv.id === adventurerId)
          if (current) Object.assign(current, updated)
        }
        patch(this.adventurers)
        patch(this.browseAdventurers)
        if (this.currentDetail?.id === adventurerId) {
          Object.assign(this.currentDetail, updated)
        }
      }
      return data
    },

    async boostReservationPriority() {
      const { data } = await api.post('/adventurer-s1/reservation/boost-priority')
      if (data.code === 200 && data.data) {
        this.myReservation = data.data as Reservation
      }
      return data
    },

    async fetchMyArchiveAppeal() {
      const { data } = await api.get('/adventurer-s1/archive-appeals/mine')
      if (data.code === 200) {
        this.myArchiveAppeal = (data.data as ArchiveAppeal | null) ?? null
      }
      return data
    },

    async submitArchiveAppeal(adventurerId: number, reason: string) {
      const { data } = await api.post('/adventurer-s1/archive-appeals', {
        adventurer_id: adventurerId,
        reason,
      })
      if (data.code === 200 || data.code === 201) {
        this.myArchiveAppeal = data.data as ArchiveAppeal
      }
      return data
    },

    async fetchArchiveAppeals(params: { page?: number; per_page?: number; status?: string; search?: string } = {}) {
      this.archiveAppealsLoading = true
      try {
        const { data } = await api.get('/adventurer-s1/admin/archive-appeals', { params })
        if (data.code === 200 && data.data) {
          this.archiveAppeals = data.data.items ?? []
          this.archiveAppealsPagination = data.data.pagination ?? null
        }
        return data
      } finally {
        this.archiveAppealsLoading = false
      }
    },

    async approveArchiveAppeal(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/archive-appeals/${appealId}/approve`)
      return data
    },

    async rejectArchiveAppeal(appealId: number, reason: string) {
      const { data } = await api.post(`/adventurer-s1/admin/archive-appeals/${appealId}/reject`, { reason })
      return data
    },

    // ── 冒险者列表（带防重复） ──────────────────────────────────────────
    async fetchAdventurers(force = false) {
      const now = Date.now()
      const fetchOwner = this.squadSessionUserId
      if (
        !force
        && this.adventurers.length > 0
        && _adventurersFetchOwner === fetchOwner
        && now - _adventurersFetchAt < ADVENTURERS_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: this.adventurers }
      }
      if (_adventurersFetchPending) return _adventurersFetchPending

      const initial = this.adventurers.length === 0
      if (initial) this.loading = true

      const fetchGeneration = _adventurersFetchGeneration
      let pending!: Promise<any>
      pending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/list')
          if (
            fetchGeneration !== _adventurersFetchGeneration
            || fetchOwner !== this.squadSessionUserId
          ) {
            return {
              code: 0,
              message: '会话已变化，已忽略旧冒险者列表',
              stale_session: true,
            }
          }
          if (data.code === 200) {
            this.adventurers = data.data ?? []
            _adventurersFetchAt = Date.now()
            _adventurersFetchOwner = fetchOwner
            if (this.tavernSeats.length > 0) {
              syncTavernSeatedFromSeats(this.tavernSeats, [this.adventurers])
            }
            if (this.teaHouseSeats.length > 0) {
              syncTeaHouseSeatedFromSeats(
                this.teaHouseSeats,
                this.teaHouseStaminaPerMinute,
                [this.adventurers],
              )
            }
          }
          return data
        } finally {
          if (_adventurersFetchPending === pending) {
            if (initial) this.loading = false
            _adventurersFetchPending = null
          }
        }
      })()
      _adventurersFetchPending = pending
      return pending
    },

    // ── 批量初始化（首屏一次请求） ─────────────────────────────────────
    async fetchInit(force = false) {
      const now = Date.now()
      const fetchOwner = this.squadSessionUserId
      const fetchView = getS1PlayerRealmView()
      if (
        !force
        && this.adventurers.length > 0
        && _initFetchOwner === fetchOwner
        && _initFetchView === fetchView
        && now - _initFetchAt < INIT_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, message: 'ok', cached: true }
      }
      if (_initFetchPending && _initFetchView === fetchView) return _initFetchPending
      const fetchGeneration = ++_initFetchGeneration
      _initFetchAt = 0
      _initFetchView = fetchView
      const isCurrentFetch = () => (
        fetchGeneration === _initFetchGeneration
        && fetchOwner === this.squadSessionUserId
        && fetchView === getS1PlayerRealmView()
      )
      const applyRealmContext = (response: any): boolean => {
        if (!isCurrentFetch()) return false
        const membershipRealm = realmCodeFromResponseHeader(
          response?.headers?.['x-s1-membership-realm'],
        )
        const viewRealm = realmCodeFromResponseHeader(
          response?.headers?.['x-s1-view-realm'],
        )
        if (!membershipRealm && !viewRealm) return false
        if (membershipRealm) {
          this.realmAssignment = {
            realm_code: membershipRealm,
            realm_label: '当前酒馆',
            is_locked: response.headers['x-s1-membership-locked'] === 'true',
          }
        }
        this.realmViewCode = viewRealm
          ?? membershipRealm
          ?? this.realmViewCode
        const visitorMode = response?.headers?.['x-s1-visitor-mode']
        if (visitorMode === 'true' || visitorMode === 'false') {
          this.realmVisitorMode = visitorMode === 'true'
        }
        return true
      }
      let pending!: Promise<any>
      pending = (async () => {
        try {
          const response = await api.get('/adventurer-s1/init')
          const { data } = response
          if (!isCurrentFetch()) {
            return {
              code: 0,
              message: '会话已变化，已忽略旧酒馆快照',
              stale_session: true,
            }
          }
          const realmContextApplied = applyRealmContext(response)
          if (data.code === 200 && data.data) {
            const d = data.data
            // 公告
            this.notice = d.notice?.notice ?? ''
            this.noticeVersion = d.notice?.version ?? null
            this.noticeShouldShow = !!d.notice?.should_show
            if (!realmContextApplied) {
              this.realmAssignment = d.realm_assignment ?? null
              this.realmViewCode = this.realmAssignment?.realm_code ?? null
              this.realmVisitorMode = false
            }
            this.squadFeatureEnabled = d.squad_feature_enabled === true
            if (!this.squadFeatureEnabled) this.clearSquadState()
            // 冒险者
            this.adventurers = d.adventurers ?? []
            _adventurersFetchAt = Date.now()
            _adventurersFetchOwner = fetchOwner
            // 建筑
            this.buildings = d.buildings ?? []
            _buildingsFetchAt = Date.now()
            // 酒馆座位
            this.tavernSeats = d.tavern_seats ?? []
            syncTavernSeatedFromSeats(this.tavernSeats, [this.adventurers])
            _tavernSeatsLastRead = {
              key: tavernSeatsReadKey(fetchOwner, this.realmViewCode),
              at: Date.now(),
              data: { code: 200, data: { seats: this.tavernSeats } },
            }
            if (_tavernSeatsPending?.key === _tavernSeatsLastRead.key) {
              _tavernSeatsPending.refreshRequested = true
            }
            // 酒单（init 曾误传 (menu, err) 元组，需校验 key 字段）
            if (isValidDrinkMenu(d.drink_menu)) this.drinkMenu = d.drink_menu
            if (isValidFoodMenu(d.food_menu)) this.foodMenu = d.food_menu
            // 酒馆升级
            this.tavernUpgrade = d.tavern_upgrade ?? null
            // 最近动态（后端返回 {entries, has_more, next_before} 结构）
            if (this.myActivityFilter === 'all') {
              const actEntries = d.activity?.entries ?? []
              this.myActivity = actEntries
              this.myActivityHasMore = !!d.activity?.has_more
              this.myActivityNextBefore = d.activity?.next_before ?? null
              this.myActivityLoadedFilter = 'all'
            }
            // 材料
            this.materialBalances = d.materials ?? {}
            // 任务栏
            this.taskAcceptableCount = d.task_acceptable_count ?? 0
            this.taskAcceptableIds = d.task_acceptable_ids ?? []
            // 拍卖行
            this.secretAuctionOpenCount = d.auction_open_count ?? 0
            _initFetchAt = Date.now()
            _initFetchOwner = fetchOwner
            _homeBadgesFetchAt = Date.now()
            _taskBadgesFetchAt = Date.now()
          }
          return data
        } catch (error: any) {
          // Region 2 may be temporarily unavailable while the central gateway
          // still knows the authoritative membership and requested view.
          // Preserve that context so the UI never falls into a realm-less state.
          applyRealmContext(error?.response)
          throw error
        } finally {
          if (_initFetchPending === pending) {
            _initFetchPending = null
          }
        }
      })()
      _initFetchPending = pending
      return pending
    },

    // ── 纹章小队 ──────────────────────────────────────────────────────
    clearSquadState() {
      this.squadFeatureEnabled = false
      this.squadFeature = null
      this.mySquad = null
      this.squadItems = []
      this.squadPagination = null
      this.squadsLoading = false
      this.squadsStale = true
      resetSquadFetchCache()
      resetSquadRefresh()
    },

    prepareSquadSession(userId: number | null) {
      const nextUserId = userId != null && Number.isFinite(Number(userId))
        ? Number(userId)
        : null
      if (this.squadSessionUserId === nextUserId) return false
      this.squadSessionUserId = nextUserId
      invalidateSessionOwnedFetches()
      this.adventurers = []
      this.realmAssignment = null
      this.realmViewCode = null
      this.realmVisitorMode = false
      this.tavernSeats = []
      this.tavernSeatsLoading = false
      this.logs = []
      this.logsPagination = null
      this.logsLoading = false
      this._expeditionConfig = null
      this.tavernServerOffsetMs = 0
      this.currentDetail = null
      this.loading = false
      this.detailLoading = false
      this.clearSquadState()
      return true
    },

    markSquadsStale() {
      this.squadsStale = true
      this.squadsLoading = false
      resetSquadFetchCache()
    },

    consumeLocalSquadEcho(payload: any) {
      return consumeLocalSquadSocketEcho(payload)
    },

    async fetchSquads(page = 1, perPage = 8, force = false) {
      if (!this.squadFeatureEnabled) {
        return { code: 403, message: '纹章小队尚未开放' }
      }
      const fetchKey = `${page}:${perPage}`
      if (
        !force
        && !this.squadsStale
        && this.squadFeature
        && _squadsFetchKey === fetchKey
        && Date.now() - _squadsFetchAt < SQUADS_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, message: 'ok' }
      }
      if (_squadsFetchPending) return _squadsFetchPending
      this.squadsLoading = true
      const fetchGeneration = _squadsFetchGeneration
      let pending!: Promise<any>
      pending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/squads', {
            params: { page, per_page: perPage },
          })
          if (
            data.code === 200
            && data.data
            && fetchGeneration === _squadsFetchGeneration
            && this.squadFeatureEnabled
          ) {
            const overview = data.data as SquadOverview
            this.squadFeature = overview.feature
            this.mySquad = overview.my_squad
            this.squadItems = overview.squads?.items ?? []
            this.squadPagination = overview.squads ?? null
            this.squadsStale = false
            _squadsFetchKey = fetchKey
            _squadsFetchAt = Date.now()
          }
          return data
        } finally {
          if (_squadsFetchPending === pending) {
            this.squadsLoading = false
            _squadsFetchPending = null
          }
        }
      })()
      _squadsFetchPending = pending
      return pending
    },

    async refreshAdventurersAfterPreferenceChange() {
      // 等旧请求完成后再读一次，防止批量操作之前发出的响应覆盖新偏好。
      if (_adventurersFetchPending) await _adventurersFetchPending.catch(() => null)
      return this.fetchAdventurers(true)
    },

    async refreshSquadsAndAdventurers(page?: number, perPage?: number): Promise<SquadRefreshResult> {
      if (!this.squadFeatureEnabled) {
        this.clearSquadState()
        return {
          ok: false,
          squads_ok: false,
          adventurers_ok: false,
          message: '纹章小队尚未开放',
        }
      }
      const refreshOwner = this.squadSessionUserId
      if (_squadRefreshPending && _squadRefreshOwner === refreshOwner) {
        return _squadRefreshPending
      }
      const targetPage = page ?? this.squadPagination?.page ?? 1
      const targetPerPage = perPage ?? this.squadPagination?.per_page ?? 8
      let refreshPending!: Promise<SquadRefreshResult>
      refreshPending = (async () => {
        try {
          while (_squadsFetchPending || _adventurersFetchPending) {
            const pendingRequests = [
              _squadsFetchPending,
              _adventurersFetchPending,
            ].filter((pending): pending is Promise<any> => pending != null)
            await Promise.all(pendingRequests.map(pending => pending.catch(() => null)))
          }
          if (
            refreshOwner !== this.squadSessionUserId
            || !this.squadFeatureEnabled
          ) {
            return {
              ok: false,
              squads_ok: false,
              adventurers_ok: false,
              message: '会话或功能状态已变化',
            }
          }

          this.markSquadsStale()
          const [squadsResult, adventurersResult] = await Promise.allSettled([
            this.fetchSquads(targetPage, targetPerPage, true),
            this.fetchAdventurers(true),
          ])
          const ownerStillCurrent = refreshOwner === this.squadSessionUserId
          const squadsOk = ownerStillCurrent
            && squadsResult.status === 'fulfilled'
            && squadsResult.value?.code === 200
            && !this.squadsStale
          const adventurersOk = ownerStillCurrent
            && adventurersResult.status === 'fulfilled'
            && adventurersResult.value?.code === 200
          const ok = squadsOk && adventurersOk
          if (!ok && ownerStillCurrent && this.squadFeatureEnabled) {
            this.markSquadsStale()
          }
          return {
            ok,
            squads_ok: squadsOk,
            adventurers_ok: adventurersOk,
            message: ok ? undefined : '操作已完成，但纹章名册刷新失败，请稍后重新打开查看',
          }
        } finally {
          if (_squadRefreshPending === refreshPending) {
            _squadRefreshPending = null
            _squadRefreshOwner = null
          }
        }
      })()
      _squadRefreshPending = refreshPending
      _squadRefreshOwner = refreshOwner
      return refreshPending
    },

    async refreshSquadsAfterMutation(): Promise<SquadRefreshResult> {
      return this.refreshSquadsAndAdventurers()
    },

    async createSquad(adventurerId: number, name: string, emblem: SquadEmblemData) {
      const action = 'squad_created'
      rememberLocalSquadSocketEcho(action, adventurerId)
      try {
        const { data } = await api.post('/adventurer-s1/squads', {
          adventurer_id: adventurerId,
          name,
          emblem,
        })
        if (data.code === 200 || data.code === 201) {
          const refresh = await this.refreshSquadsAfterMutation()
          if (refresh.ok) completeLocalSquadSocketEcho(action, adventurerId)
          else clearLocalSquadSocketEcho(action, adventurerId)
          return attachSquadRefreshResult(data, refresh)
        }
        clearLocalSquadSocketEcho(action, adventurerId)
        return data
      } catch (error) {
        clearLocalSquadSocketEcho(action, adventurerId)
        throw error
      }
    },

    async joinSquad(squadId: number, adventurerId: number) {
      const action = 'squad_joined'
      rememberLocalSquadSocketEcho(action, adventurerId)
      try {
        const { data } = await api.post(`/adventurer-s1/squads/${squadId}/join`, {
          adventurer_id: adventurerId,
        })
        if (data.code === 200) {
          const refresh = await this.refreshSquadsAfterMutation()
          if (refresh.ok) completeLocalSquadSocketEcho(action, adventurerId)
          else clearLocalSquadSocketEcho(action, adventurerId)
          return attachSquadRefreshResult(data, refresh)
        }
        clearLocalSquadSocketEcho(action, adventurerId)
        return data
      } catch (error) {
        clearLocalSquadSocketEcho(action, adventurerId)
        throw error
      }
    },

    async leaveSquad(squadId: number, adventurerId: number) {
      const action = 'squad_left'
      rememberLocalSquadSocketEcho(action, adventurerId)
      try {
        const { data } = await api.post(`/adventurer-s1/squads/${squadId}/leave`, {
          adventurer_id: adventurerId,
        })
        if (data.code === 200) {
          const refresh = await this.refreshSquadsAfterMutation()
          if (refresh.ok) completeLocalSquadSocketEcho(action, adventurerId)
          else clearLocalSquadSocketEcho(action, adventurerId)
          return attachSquadRefreshResult(data, refresh)
        }
        clearLocalSquadSocketEcho(action, adventurerId)
        return data
      } catch (error) {
        clearLocalSquadSocketEcho(action, adventurerId)
        throw error
      }
    },

    async updateSquadEmblem(squadId: number, adventurerId: number, emblem: SquadEmblemData) {
      const action = 'squad_emblem_updated'
      rememberLocalSquadSocketEcho(action, adventurerId)
      try {
        const { data } = await api.put(`/adventurer-s1/squads/${squadId}/emblem`, {
          adventurer_id: adventurerId,
          emblem,
        })
        if (data.code === 200) {
          const refresh = await this.refreshSquadsAfterMutation()
          if (refresh.ok) completeLocalSquadSocketEcho(action, adventurerId)
          else clearLocalSquadSocketEcho(action, adventurerId)
          return attachSquadRefreshResult(data, refresh)
        }
        clearLocalSquadSocketEcho(action, adventurerId)
        return data
      } catch (error) {
        clearLocalSquadSocketEcho(action, adventurerId)
        throw error
      }
    },

    async disbandSquad(squadId: number, adventurerId: number) {
      const action = 'squad_disbanded'
      rememberLocalSquadSocketEcho(action, adventurerId)
      try {
        const { data } = await api.post(`/adventurer-s1/squads/${squadId}/disband`, {
          adventurer_id: adventurerId,
        })
        if (data.code === 200) {
          const refresh = await this.refreshSquadsAfterMutation()
          if (refresh.ok) completeLocalSquadSocketEcho(action, adventurerId)
          else clearLocalSquadSocketEcho(action, adventurerId)
          return attachSquadRefreshResult(data, refresh)
        }
        clearLocalSquadSocketEcho(action, adventurerId)
        return data
      } catch (error) {
        clearLocalSquadSocketEcho(action, adventurerId)
        throw error
      }
    },

    // ── 任务栏合并接口（tasks + active） ──────────────────────────────
    async fetchTaskBoard(adventurerId?: number) {
      const qs = adventurerId ? `?adventurer_id=${adventurerId}` : ''
      const { data } = await api.get(`/adventurer-s1/tasks/board${qs}`)
      return data
    },

    // ── 悬赏布告栏（委托） ──────────────────────────────────────────────
    async fetchCommissionBoard(adventurerId?: number) {
      const qs = adventurerId ? `?adventurer_id=${adventurerId}` : ''
      const { data } = await api.get(`/adventurer-s1/commissions${qs}`, S1_MEMBERSHIP_REQUEST)
      return data
    },

    async acceptCommission(commissionId: number, adventurerId?: number) {
      const payload: Record<string, unknown> = {}
      if (adventurerId != null) payload.adventurer_id = adventurerId
      const { data } = await api.post(
        `/adventurer-s1/commissions/${commissionId}/accept`,
        payload,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_commission_accept', { commission_id: commissionId })
      return data
    },

    async claimCommission(commissionId: number) {
      const { data } = await api.post(
        `/adventurer-s1/commissions/${commissionId}/claim`,
        {},
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_commission_claim', { commission_id: commissionId })
      return data
    },

    async cancelCommission(commissionId: number) {
      const { data } = await api.post(
        `/adventurer-s1/commissions/${commissionId}/cancel`,
        {},
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_commission_cancel', { commission_id: commissionId })
      return data
    },

    async fetchSupportVoteStatus() {
      const { data } = await api.get('/adventurer-s1/support-vote/status')
      if (data.code === 200) {
        this.supportVoteStatus = data.data as SupportVoteStatus
      }
      return data
    },

    async castSupportVote(optionKey: string) {
      this.supportVoteLoading = true
      try {
        const { data } = await api.post('/adventurer-s1/support-vote', { option_key: optionKey })
        if (data.code === 200) {
          this.supportVoteStatus = data.data as SupportVoteStatus
          trackEvent('adventurer_s1_support_vote')
        }
        return data
      } finally {
        this.supportVoteLoading = false
      }
    },

    async adminFetchSupportVoteStats() {
      const { data } = await api.get('/adventurer-s1/admin/support-vote/stats', S1_SELECTED_REQUEST)
      return data
    },

    async adminSetSupportVote(payload: SupportVoteAdminPayload) {
      const { data } = await api.post('/adventurer-s1/admin/support-vote', payload, S1_SELECTED_REQUEST)
      if (data.code === 200) {
        trackEvent('adventurer_s1_admin_set_support_vote')
      }
      return data
    },

    async adminReconcileContribution() {
      const { data } = await api.post('/adventurer-s1/admin/contribution/reconcile')
      return data
    },

    async adminPreviewLevelCapExpReset() {
      const { data } = await api.get('/adventurer-s1/admin/level-cap-exp-reset/preview', S1_SELECTED_REQUEST)
      return data
    },

    async adminResetLevelCapExp() {
      const { data } = await api.post('/adventurer-s1/admin/level-cap-exp-reset', {}, S1_SELECTED_REQUEST)
      return data
    },

    async adminOpenLevelCap15() {
      const { data } = await api.post('/adventurer-s1/admin/level-cap/open-15', {}, S1_SELECTED_REQUEST)
      return data
    },

    async adminPreviewDungeonRename() {
      const { data } = await api.get('/adventurer-s1/admin/dungeon-rename/preview')
      return data
    },

    async adminExecuteDungeonRename() {
      const { data } = await api.post('/adventurer-s1/admin/dungeon-rename', { confirm: true })
      return data
    },

    async adminPreviewRepairCodenames() {
      const { data } = await api.get('/adventurer-s1/admin/history-story/repair-codenames/preview')
      return data
    },

    async adminRepairCodenames() {
      const { data } = await api.post('/adventurer-s1/admin/history-story/repair-codenames', {})
      return data
    },

    async adminRestoreCodenames() {
      const { data } = await api.post('/adventurer-s1/admin/history-story/repair-codenames/restore', {})
      return data
    },

    async adminDescriptionCompressPreview(options: {
      assetKind?: 'trait' | 'equipment' | 'skill'
      statusFilter?: 'all' | 'ready' | 'summary_issue' | 'route_issue' | 'pending' | 'kept_original' | 'full_fallback'
      page?: number
      perPage?: number
    } = {}) {
      const {
        assetKind = 'trait',
        statusFilter = 'all',
        page = 1,
        perPage = 50,
      } = options
      const { data } = await api.get('/adventurer-s1/admin/descriptions/compress/preview', {
        params: {
          kind: assetKind,
          status: statusFilter,
          page,
          per_page: perPage,
        },
      })
      return data
    },

    async adminTraitCompressPreview(options: {
      page?: number
      perPage?: number
      onlyMissing?: boolean
    } = {}) {
      const { page = 1, perPage = 50, onlyMissing = false } = options
      const { data } = await api.get('/adventurer-s1/admin/traits/compress/preview', {
        params: {
          page,
          per_page: perPage,
          only_missing: onlyMissing ? '1' : '0',
        },
      })
      return data
    },

    async adminTraitCompressCompare(traitId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/traits/compress/compare/${traitId}`)
      return data
    },

    async adminTraitCompress(body: { trait_ids?: number[]; limit?: number; only_missing?: boolean } = {}) {
      const { data } = await api.post('/adventurer-s1/admin/traits/compress', body)
      return data
    },

    async adminPreviewRepairToolTypes() {
      const { data } = await api.get('/adventurer-s1/admin/items/repair-tool-types/preview')
      return data
    },

    async adminRepairToolTypes() {
      const { data } = await api.post('/adventurer-s1/admin/items/repair-tool-types', {})
      return data
    },

    async adminRestoreToolTypes() {
      const { data } = await api.post('/adventurer-s1/admin/items/repair-tool-types/restore', {})
      return data
    },

    async adminPreviewRepairUncommonRarity() {
      const { data } = await api.get('/adventurer-s1/admin/items/repair-uncommon-rarity/preview')
      return data
    },

    async adminRepairUncommonRarity() {
      const { data } = await api.post('/adventurer-s1/admin/items/repair-uncommon-rarity', {})
      return data
    },

    async adminRestoreUncommonRarity() {
      const { data } = await api.post('/adventurer-s1/admin/items/repair-uncommon-rarity/restore', {})
      return data
    },

    async adminPreviewRepairCompanionEvolutionStages() {
      const { data } = await api.get(
        '/adventurer-s1/admin/items/repair-companion-evolution-stages/preview',
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminPreviewCompanionEvolutionIntegrity() {
      const { data } = await api.post(
        '/adventurer-s1/admin/items/companion-evolution-integrity/preview',
        {},
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminRepairCompanionEvolutionStages() {
      const { data } = await api.post(
        '/adventurer-s1/admin/items/repair-companion-evolution-stages',
        {},
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminRestoreCompanionEvolutionStages() {
      const { data } = await api.post('/adventurer-s1/admin/items/repair-companion-evolution-stages/restore', {})
      return data
    },

    async adminCompanionContinuityReviewEvidence(expeditionLogIds: number[]) {
      const { data } = await api.post(
        '/adventurer-s1/admin/items/companion-continuity-repair/review-evidence',
        { expedition_log_ids: expeditionLogIds },
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminCompanionContinuityAiReviewPreview(evidenceToken: string) {
      const { data } = await api.post(
        '/adventurer-s1/admin/items/companion-continuity-repair/review-ai-preview',
        { evidence_token: evidenceToken },
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminCompanionContinuityExternalReviewPreview(
      evidenceToken: string,
      review: Record<string, unknown>,
    ) {
      const { data } = await api.post(
        '/adventurer-s1/admin/items/companion-continuity-repair/review-preview',
        { evidence_token: evidenceToken, review },
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminExecuteCompanionContinuityRepair(previewToken: string) {
      const { data } = await api.post(
        '/adventurer-s1/admin/items/companion-continuity-repair/execute',
        { preview_token: previewToken },
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminRestoreCompanionContinuityRepair(runId: string) {
      const { data } = await api.post(
        '/adventurer-s1/admin/items/companion-continuity-repair/restore',
        { run_id: runId },
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminPreviewCatalogHistorySync(
      itemId: number,
      oldSnapshot: CatalogHistorySyncOldSnapshotInput,
    ) {
      const { data } = await api.post(
        `/adventurer-s1/admin/items/catalog/${itemId}/history-sync/preview`,
        { old_snapshot: oldSnapshot },
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminExecuteCatalogHistorySync(
      itemId: number,
      previewToken: string,
      note = '',
    ) {
      const { data } = await api.post(
        `/adventurer-s1/admin/items/catalog/${itemId}/history-sync/execute`,
        {
          preview_token: previewToken,
          note,
        },
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminRestoreCatalogHistorySync(itemId: number, runId: string) {
      const { data } = await api.post(
        `/adventurer-s1/admin/items/catalog/${itemId}/history-sync/restore`,
        { run_id: runId },
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminPreviewCompanionEvolutionFeeReclaims() {
      const { data } = await api.get('/adventurer-s1/admin/items/companion-evolution-fee-reclaims/preview')
      return data
    },

    async adminExecuteCompanionEvolutionFeeReclaims(previewToken: string, note = '') {
      const { data } = await api.post('/adventurer-s1/admin/items/companion-evolution-fee-reclaims', {
        preview_token: previewToken,
        note,
      })
      return data
    },

    async adminPreviewCompanionEvolutionFeeReconciliation() {
      const { data } = await api.get(
        '/adventurer-s1/admin/items/companion-evolution-fee-reconciliation/preview',
        { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS },
      )
      return data
    },

    async adminExecuteCompanionEvolutionFeeReconciliation(previewToken: string, note = '') {
      const { data } = await api.post('/adventurer-s1/admin/items/companion-evolution-fee-reconciliation', {
        preview_token: previewToken,
        note,
      }, { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS })
      return data
    },

    async adminClassRepairPreview(adventurerId: number) {
      const { data } = await api.get('/adventurer-s1/admin/class-repair/preview', {
        params: { adventurer_id: adventurerId },
      })
      return data
    },

    async adminClassRepair(adventurerId: number, newClass: string) {
      const { data } = await api.post('/adventurer-s1/admin/class-repair', {
        adventurer_id: adventurerId,
        new_class: newClass,
      })
      return data
    },

    async adminClassRepairRestore() {
      const { data } = await api.post('/adventurer-s1/admin/class-repair/restore', {})
      return data
    },

    async adminClassSkillScrollPreview(adventurerId?: number) {
      const { data } = await api.get('/adventurer-s1/admin/class-skill-scroll/preview', {
        params: adventurerId ? { adventurer_id: adventurerId } : undefined,
      })
      return data
    },

    async adminClassSkillScrollRepair(adventurerIds?: number[]) {
      const { data } = await api.post('/adventurer-s1/admin/class-skill-scroll/repair', {
        adventurer_ids: adventurerIds?.length ? adventurerIds : undefined,
      })
      return data
    },

    async adminClassSkillScrollRestore() {
      const { data } = await api.post('/adventurer-s1/admin/class-skill-scroll/restore', {})
      return data
    },

    async adminSpecialDungeonTicketPenaltiesPreview() {
      const { data } = await api.get('/adventurer-s1/admin/special-dungeon-ticket-penalties/preview')
      return data
    },

    async adminGamblingGoldReconciliationPreview(adventurerId?: number) {
      const { data } = await api.get('/adventurer-s1/admin/gambling-gold-reconciliation/preview', {
        params: adventurerId !== undefined ? { adventurer_id: adventurerId } : undefined,
        timeout: ADMIN_FULL_SCAN_TIMEOUT_MS,
      })
      return data
    },

    async adminGamblingGoldReconciliationExecute(previewToken: string) {
      const { data } = await api.post('/adventurer-s1/admin/gambling-gold-reconciliation/execute', {
        preview_token: previewToken,
      }, { timeout: ADMIN_FULL_SCAN_TIMEOUT_MS })
      return data
    },

    async adminSpecialDungeonTicketPenaltiesExecute(previewToken = '') {
      const { data } = await api.post('/adventurer-s1/admin/special-dungeon-ticket-penalties/execute', {
        preview_token: previewToken,
      })
      return data
    },

    async adminExpeditionSkillRepairPreview(expeditionLogId?: number, adventurerId?: number) {
      const { data } = await api.get('/adventurer-s1/admin/expedition-skill-repair/preview', {
        params: {
          expedition_log_id: expeditionLogId || undefined,
          adventurer_id: adventurerId || undefined,
        },
      })
      return data
    },

    async adminExpeditionSkillRepair(expeditionLogId?: number, adventurerId?: number) {
      const { data } = await api.post('/adventurer-s1/admin/expedition-skill-repair/repair', {
        expedition_log_id: expeditionLogId || undefined,
        adventurer_id: adventurerId || undefined,
      })
      return data
    },

    async adminExpeditionSkillRepairRestore() {
      const { data } = await api.post('/adventurer-s1/admin/expedition-skill-repair/restore', {})
      return data
    },

    async adminExpeditionConsumptionRepairPreview(expeditionLogId?: number, adventurerId?: number) {
      const { data } = await api.get('/adventurer-s1/admin/expedition-consumption-repair/preview', {
        params: {
          expedition_log_id: expeditionLogId || undefined,
          adventurer_id: adventurerId || undefined,
        },
        timeout: 120000,
      })
      return data
    },

    async adminExpeditionConsumptionRepairExecute(previewToken: string) {
      const { data } = await api.post('/adventurer-s1/admin/expedition-consumption-repair/execute', {
        preview_token: previewToken,
      })
      return data
    },

    async adminExpeditionConsumptionRepairRestore() {
      const { data } = await api.post('/adventurer-s1/admin/expedition-consumption-repair/restore', {})
      return data
    },

    async adminCommissionSettlementRepairPreview(expeditionLogId: number) {
      const { data } = await api.get('/adventurer-s1/admin/commission-settlement-repair/preview', {
        params: { expedition_log_id: expeditionLogId },
      })
      return data
    },

    async adminCommissionSettlementRepair(expeditionLogId: number) {
      const { data } = await api.post('/adventurer-s1/admin/commission-settlement-repair/repair', {
        expedition_log_id: expeditionLogId,
      })
      return data
    },

    async adminFateSettlementRepairPreview(
      expeditionLogId?: number,
      includeUnreviewed = false,
    ) {
      const { data } = await api.get('/adventurer-s1/admin/fate-settlement-repair/preview', {
        params: {
          expedition_log_id: expeditionLogId || undefined,
          include_unreviewed: includeUnreviewed ? 1 : undefined,
        },
        timeout: 120000,
      })
      return data
    },

    async adminFateSettlementReviewEvidence(expeditionLogId: number) {
      const { data } = await api.get(
        '/adventurer-s1/admin/fate-settlement-repair/review-evidence',
        {
          params: { expedition_log_id: expeditionLogId },
          timeout: 120000,
        },
      )
      return data
    },

    async adminFateSettlementAiReviewPreview(evidenceToken: string) {
      const { data } = await api.post(
        '/adventurer-s1/admin/fate-settlement-repair/review-ai-preview',
        { evidence_token: evidenceToken },
        { timeout: 120000 },
      )
      return data
    },

    async adminFateSettlementExternalReviewPreview(
      evidenceToken: string,
      review: Record<string, unknown>,
    ) {
      const { data } = await api.post(
        '/adventurer-s1/admin/fate-settlement-repair/review-preview',
        { evidence_token: evidenceToken, review },
        { timeout: 120000 },
      )
      return data
    },

    async adminFateSettlementRepairExecute(previewToken: string) {
      const { data } = await api.post(
        '/adventurer-s1/admin/fate-settlement-repair/execute',
        { preview_token: previewToken },
        { timeout: 120000 },
      )
      return data
    },

    async adminFateSettlementRepairRestore(runId: string) {
      const { data } = await api.post(
        '/adventurer-s1/admin/fate-settlement-repair/restore',
        { run_id: runId },
        { timeout: 120000 },
      )
      return data
    },

    async adminPlaceholderSkillRecallPreview(adventurerId?: number) {
      const { data } = await api.get('/adventurer-s1/admin/placeholder-skill-recall/preview', {
        params: adventurerId ? { adventurer_id: adventurerId } : undefined,
      })
      return data
    },

    async adminPlaceholderSkillRecallSendOffers(adventurerIds?: number[], adminNote?: string) {
      const { data } = await api.post('/adventurer-s1/admin/placeholder-skill-recall/send-offers', {
        adventurer_ids: adventurerIds?.length ? adventurerIds : undefined,
        admin_note: adminNote || undefined,
      })
      return data
    },

    async fetchPendingPlaceholderSkillRecallOffers(limit = 10) {
      const { data } = await api.get(`/adventurer-s1/placeholder-skill-recall/pending?limit=${limit}`)
      return data
    },

    async acceptPlaceholderSkillRecallOffer(offerId: number) {
      const { data } = await api.post(`/adventurer-s1/placeholder-skill-recall/${offerId}/accept`, {})
      return data
    },

    async declinePlaceholderSkillRecallOffer(offerId: number) {
      const { data } = await api.post(`/adventurer-s1/placeholder-skill-recall/${offerId}/decline`, {})
      return data
    },

    async adminPreviewOrphanLv2Instances() {
      const { data } = await api.get('/adventurer-s1/admin/lv2-orphan-instances/preview')
      return data
    },

    async adminPurgeOrphanLv2Instances() {
      const { data } = await api.post('/adventurer-s1/admin/lv2-orphan-instances/purge', {})
      return data
    },

    async adminPreviewGhostInParty() {
      const { data } = await api.get('/adventurer-s1/admin/ghost-in-party/preview', S1_SELECTED_REQUEST)
      return data
    },

    async adminRepairGhostInParty() {
      const { data } = await api.post('/adventurer-s1/admin/ghost-in-party/repair', {}, S1_SELECTED_REQUEST)
      return data
    },

    async adminPreviewDailySoloTicketRepair(days: number) {
      const { data } = await api.get('/adventurer-s1/admin/daily-solo-ticket-repair/preview', {
        params: { days },
        s1RealmContext: 'selected',
      })
      return data
    },

    async adminRepairDailySoloTickets(days: number) {
      const { data } = await api.post('/adventurer-s1/admin/daily-solo-ticket-repair/repair', { days }, S1_SELECTED_REQUEST)
      return data
    },

    async adminPreviewLv2LockedDonationInstances() {
      const { data } = await api.get('/adventurer-s1/admin/lv2-locked-donations/preview')
      return data
    },

    async adminRefundLv2LockedBuildingDonations(instanceId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/building-instances/${instanceId}/refund-lv2-locked`, {})
      return data
    },

    async adminRefillCommissions() {
      const { data } = await api.post('/adventurer-s1/admin/commissions/refill', {}, S1_MEMBERSHIP_REQUEST)
      return data
    },

    async adminRefreshCommissions() {
      const { data } = await api.post('/adventurer-s1/admin/commissions/refresh', {}, S1_MEMBERSHIP_REQUEST)
      return data
    },

    async adminRefreshCommission(commissionId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/commissions/${commissionId}/refresh`, {}, S1_MEMBERSHIP_REQUEST)
      return data
    },

    // ── 冒险者详情 ────────────────────────────────────────────────────
    async fetchDetail(
      advId: number,
      opts?: { preserveDetail?: boolean; force?: boolean },
    ): Promise<any> {
      const now = Date.now()
      if (
        !opts?.force
        && this.currentDetail?.id === advId
        && _detailFetchAt[advId]
        && now - _detailFetchAt[advId] < DETAIL_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: this.currentDetail, message: 'ok' }
      }
      const activePending = _detailFetchPending[advId]
      if (activePending) {
        if (!opts?.force || _detailFetchPendingFresh[advId]) return activePending
        const waitingOwner = this.squadSessionUserId
        await activePending
        if (waitingOwner !== this.squadSessionUserId) {
          return { code: 0, message: '会话已变化，已忽略旧冒险者详情', stale_session: true }
        }
        return this.fetchDetail(advId, opts)
      }

      this.detailLoading = true
      const fetchGeneration = _detailFetchGeneration
      const fetchOwner = this.squadSessionUserId
      const sameAdv = this.currentDetail?.id === advId
      // 同冒险者软刷新时保留旧详情，避免倒计时因 refetch 闪跳
      if (!opts?.preserveDetail || !sameAdv) {
        this.currentDetail = null
      }
      const pending: Promise<any> = (async () => {
        try {
          const { data } = await api.get(`/adventurer-s1/${advId}`, {
            params: opts?.force ? { fresh: 1 } : undefined,
          })
          if (
            fetchGeneration !== _detailFetchGeneration
            || fetchOwner !== this.squadSessionUserId
          ) {
            return {
              code: 0,
              message: '会话已变化，已忽略旧冒险者详情',
              stale_session: true,
            }
          }
          if (data.code === 200) {
            this.currentDetail = data.data
            if (this.currentDetail && this.teaHouseSeats.length > 0) {
              syncTeaHouseSeatedFromSeats(
                this.teaHouseSeats,
                this.teaHouseStaminaPerMinute,
                [[this.currentDetail]],
              )
            }
            _detailFetchAt[advId] = Date.now()
          }
          return data
        } catch (e: any) {
          // 网络错误等异常时返回一个统一格式，让调用方能拿到 message
          const errData = e?.response?.data || { code: 500, message: '网络错误或服务器异常' }
          return errData
        } finally {
          if (
            fetchGeneration === _detailFetchGeneration
            && fetchOwner === this.squadSessionUserId
          ) {
            this.detailLoading = false
            delete _detailFetchPending[advId]
            delete _detailFetchPendingFresh[advId]
          }
        }
      })()
      _detailFetchPending[advId] = pending
      _detailFetchPendingFresh[advId] = Boolean(opts?.force)
      return pending
    },

    async fetchWaitingNpcOrigin(advId: number) {
      const { data } = await api.get(`/adventurer-s1/${advId}/waiting-npc-origin`)
      return data
    },

    async resetWaitingNpcOrigin(advId: number) {
      const { data } = await api.post(`/adventurer-s1/${advId}/waiting-npc-origin/reset`)
      if (data.code === 200) {
        trackEvent('adventurer_s1_waiting_npc_origin_reset', { adv_id: advId })
      }
      return data
    },

    async setActiveTitle(advId: number, titleId: number | null) {
      const { data } = await api.put(`/adventurer-s1/${advId}/active-title`, { title_id: titleId })
      if (data.code === 200 && data.data) {
        const activeTitle = data.data.active_title ?? null
        if (this.currentDetail?.id === advId) {
          this.currentDetail.active_title = activeTitle
          this.currentDetail.available_titles = data.data.available_titles ?? []
          this.currentDetail.title_previews = data.data.title_previews ?? []
        }
        const row = this.adventurers.find(item => item.id === advId)
        if (row) row.active_title = activeTitle
        for (const party of [...this.parties, ...this.myParties]) {
          const member = party.members.find(item => item.id === advId)
          if (member) member.active_title = activeTitle
        }
        for (const ticket of [...this.matchTickets, ...this.publicMatchTickets]) {
          const member = ticket.members.find(item => item.adventurer_id === advId)
          if (member) member.active_title = activeTitle
        }
      }
      return data
    },

    async fetchHistoryStory(advId: number, page = 1, perPage = 5) {
      const { data } = await api.get(
        `/adventurer-s1/${advId}/history-story?page=${page}&per_page=${perPage}`,
      )
      return data
    },

    // ── 装备操作 ──────────────────────────────────────────────────────
    async equipItem(advId: number, itemId: number, quantity?: number) {
      const body: Record<string, unknown> = { item_id: itemId }
      if (quantity != null) body.quantity = quantity
      const { data } = await api.post(`/adventurer-s1/${advId}/equip`, body)
      if (data.code === 200) {
        this.currentDetail = data.data
        trackEvent('adventurer_s1_equip_item', { adv_id: advId, item_id: itemId })
      }
      return data
    },

    async unequipItem(advId: number, itemId: number, quantity?: number) {
      const body: Record<string, unknown> = { item_id: itemId }
      if (quantity != null) body.quantity = quantity
      const { data } = await api.post(`/adventurer-s1/${advId}/unequip`, body)
      if (data.code === 200) {
        this.currentDetail = data.data
        trackEvent('adventurer_s1_unequip_item', { adv_id: advId, item_id: itemId })
      }
      return data
    },

    async setItemProtection(
      advId: number,
      source: 'equipment' | 'warehouse',
      itemId: number,
      locked: boolean,
    ) {
      const { data } = await api.post(`/adventurer-s1/${advId}/items/protection`, {
        source,
        item_id: itemId,
        locked,
      })
      if (data.code === 200) {
        if (this.currentDetail?.id === advId) {
          const items = source === 'equipment'
            ? this.currentDetail.equipment
            : this.currentDetail.warehouse
          const item = items?.find((entry) => entry.id === itemId)
          if (item) item.is_player_locked = Boolean(data.data?.is_player_locked)
        }
        trackEvent('adventurer_s1_set_item_protection', {
          adv_id: advId,
          source,
          item_id: itemId,
          locked,
        })
      }
      return data
    },

    async equipBatch(advId: number, itemIds: number[]) {
      const { data } = await api.post(`/adventurer-s1/${advId}/equip-batch`, { item_ids: itemIds })
      if (data.code === 200) {
        this.currentDetail = data.data
        trackEvent('adventurer_s1_equip_batch', { adv_id: advId })
      }
      return data
    },

    async unequipBatch(advId: number, itemIds: number[]) {
      const { data } = await api.post(`/adventurer-s1/${advId}/unequip-batch`, { item_ids: itemIds })
      if (data.code === 200) {
        this.currentDetail = data.data
        trackEvent('adventurer_s1_unequip_batch', { adv_id: advId })
      }
      return data
    },


    // ── 组队偏好 ──────────────────────────────────────────────────────
    async setPartyPreference(advId: number, preference: string) {
      const { data } = await api.post(`/adventurer-s1/${advId}/party-preference`, { preference })
      if (data.code === 200) {
        this.currentDetail = data.data
        trackEvent('adventurer_s1_set_party_preference', { adv_id: advId, preference })
      }
      return data
    },

    // ── 手动加入 / 离队（仅 forming 阶段）─────────────────────────────
    async joinParty(advId: number, partyId: number) {
      const { data } = await api.post(`/adventurer-s1/${advId}/join-party/${partyId}`)
      if (data.code === 200) trackEvent('adventurer_s1_join_party', { adv_id: advId, party_id: partyId })
      return data
    },

    async leaveParty(advId: number) {
      const { data } = await api.post(`/adventurer-s1/${advId}/leave-party`)
      if (data.code === 200) trackEvent('adventurer_s1_leave_party', { adv_id: advId })
      return data
    },

    async votePartyDeparture(advId: number, partyId: number) {
      const { data } = await api.post(`/adventurer-s1/${advId}/vote-party-departure/${partyId}`)
      if (data.code === 200) trackEvent('adventurer_s1_vote_party_departure', { adv_id: advId, party_id: partyId })
      return data
    },

    async enqueueMatchmaking(preferredDungeons?: string[]) {
      const { data } = await api.post('/adventurer-s1/matchmaking/enqueue', {
        preferred_dungeons: preferredDungeons?.length ? preferredDungeons : null,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_matchmaking_enqueue', {
          preferred_dungeons: preferredDungeons?.length ? preferredDungeons : ['random'],
        })
      }
      return data
    },

    async createMatchmakingParty(payload: { preferredDungeon?: string | null; description?: string; accessMode?: string; allowSoloFill?: boolean; campaignDispatchId?: number; campaignDispatchRevision?: number }) {
      const { data } = await api.post('/adventurer-s1/matchmaking/create-party', {
        preferred_dungeon: payload.preferredDungeon || null,
        description: payload.description || '',
        access_mode: payload.accessMode || 'public',
        allow_solo_fill: !!payload.allowSoloFill,
        campaign_dispatch_id: payload.campaignDispatchId,
        campaign_dispatch_revision: payload.campaignDispatchRevision,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_matchmaking_create_party', {
          preferred_dungeon: payload.preferredDungeon || 'random',
          access_mode: payload.accessMode || 'public',
          allow_solo_fill: !!payload.allowSoloFill,
        })
      }
      return data
    },

    async enqueueSoloMatchmaking(advId: number, preferredDungeon?: string | null) {
      const { data } = await api.post(`/adventurer-s1/${advId}/matchmaking/solo`, {
        preferred_dungeon: preferredDungeon || null,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_matchmaking_solo', {
          adv_id: advId,
          preferred_dungeon: preferredDungeon || 'random',
        })
      }
      return data
    },

    async fetchMatchmakingBoard(force = false) {
      const advLevel = _playableAdventurerLevel(this.adventurers)
      const now = Date.now()
      if (
        !force
        && _matchMyTicketsLoaded
        && _matchPublicTicketsLoaded
        && _matchInvitesLoaded
        && _matchDungeonOptionsLoaded
        && advLevel === _matchDungeonOptionsLevel
        && now - _matchmakingFetchAt < MATCHMAKING_FETCH_MIN_INTERVAL_MS
      ) {
        return {
          code: 200,
          data: {
            my_tickets: {
              items: this.matchTickets,
              campaign_task_cooldown_until: this.campaignTaskCooldownUntil,
              campaign_task_notice: this.campaignTaskNotice,
              campaign_task_policy: this.campaignTaskPolicy,
            },
            my_invites: { items: this.matchInvites },
            public_parties: { items: this.publicMatchTickets },
            available_dungeons: { items: this.matchDungeonOptions },
            queue_summary: {
              queued_count: this.matchQueuedCount,
              queued_member_count: this.matchQueuedMemberCount,
              random_member_count: this.matchRandomQueuedMemberCount,
              dungeon_member_counts: this.matchDungeonQueuedMemberCounts,
            },
          },
          message: 'ok',
        }
      }
      if (_matchmakingBoardPending) {
        if (!force) return _matchmakingBoardPending
        try {
          await _matchmakingBoardPending
        } catch {
          // 强制刷新不能被此前失败的后台请求吞掉。
        }
        if (_matchmakingBoardPending) return _matchmakingBoardPending
      }

      this.matchTicketsLoading = true
      _matchmakingBoardPending = (async () => {
        try {
          const q = _matchmakingBoardVersion
            ? `?version=${encodeURIComponent(_matchmakingBoardVersion)}`
            : ''
          const { data } = await api.get(`/adventurer-s1/matchmaking/board${q}`)
          if (data.code === 200 && data.data) {
            if (data.data.changed !== false) {
              this.matchTickets = data.data.my_tickets?.items ?? []
              this.campaignTaskCooldownUntil = data.data.my_tickets?.campaign_task_cooldown_until ?? null
              this.campaignTaskNotice = data.data.my_tickets?.campaign_task_notice ?? null
              this.campaignTaskPolicy = data.data.my_tickets?.campaign_task_policy ?? null
              this.matchInvites = data.data.my_invites?.items ?? []
              this.publicMatchTickets = data.data.public_parties?.items ?? []
              this.matchDungeonOptions = data.data.available_dungeons?.items ?? []
              this.matchQueuedCount = data.data.queue_summary?.queued_count ?? 0
              this.matchQueuedMemberCount = data.data.queue_summary?.queued_member_count ?? 0
              this.matchRandomQueuedMemberCount = data.data.queue_summary?.random_member_count ?? 0
              this.matchDungeonQueuedMemberCounts = data.data.queue_summary?.dungeon_member_counts ?? {}
            }
            _matchmakingBoardVersion = data.data.version ?? _matchmakingBoardVersion
            _matchMyTicketsLoaded = true
            _matchPublicTicketsLoaded = true
            _matchInvitesLoaded = true
            _matchDungeonOptionsLoaded = true
            _matchDungeonOptionsLevel = advLevel
            _matchmakingFetchAt = Date.now()
          }
          return data
        } finally {
          this.matchTicketsLoading = false
          _matchmakingBoardPending = null
        }
      })()
      return _matchmakingBoardPending
    },

    async fetchMyMatchTickets(force = false) {
      const now = Date.now()
      if (
        !force
        && _matchMyTicketsLoaded
        && now - _matchmakingFetchAt < MATCHMAKING_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: {
          items: this.matchTickets,
          campaign_task_cooldown_until: this.campaignTaskCooldownUntil,
          campaign_task_notice: this.campaignTaskNotice,
          campaign_task_policy: this.campaignTaskPolicy,
        }, message: 'ok' }
      }
      if (_matchMyTicketsPending) return _matchMyTicketsPending

      this.matchTicketsLoading = true
      _matchMyTicketsPending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/matchmaking/my')
          if (data.code === 200) {
            this.matchTickets = data.data.items ?? []
            this.campaignTaskCooldownUntil = data.data.campaign_task_cooldown_until ?? null
            this.campaignTaskNotice = data.data.campaign_task_notice ?? null
            this.campaignTaskPolicy = data.data.campaign_task_policy ?? null
            _matchMyTicketsLoaded = true
            _matchmakingFetchAt = Date.now()
          }
          return data
        } finally {
          this.matchTicketsLoading = false
          _matchMyTicketsPending = null
        }
      })()
      return _matchMyTicketsPending
    },

    async fetchPublicMatchTickets(force = false) {
      const now = Date.now()
      if (
        !force
        && _matchPublicTicketsLoaded
        && now - _matchmakingFetchAt < MATCHMAKING_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: { items: this.publicMatchTickets }, message: 'ok' }
      }
      if (_matchPublicTicketsPending) return _matchPublicTicketsPending

      _matchPublicTicketsPending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/matchmaking/public-parties')
          if (data.code === 200) {
            this.publicMatchTickets = data.data.items ?? []
            _matchPublicTicketsLoaded = true
            _matchmakingFetchAt = Date.now()
          }
          return data
        } finally {
          _matchPublicTicketsPending = null
        }
      })()
      return _matchPublicTicketsPending
    },

    async fetchMatchDungeonOptions(force = false) {
      const advLevel = _playableAdventurerLevel(this.adventurers)
      const now = Date.now()
      if (
        !force
        && _matchDungeonOptionsLoaded
        && advLevel === _matchDungeonOptionsLevel
        && now - _matchmakingFetchAt < MATCHMAKING_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: { items: this.matchDungeonOptions }, message: 'ok' }
      }
      if (_matchDungeonOptionsPending) return _matchDungeonOptionsPending

      _matchDungeonOptionsPending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/matchmaking/available-dungeons')
          if (data.code === 200) {
            this.matchDungeonOptions = data.data.items ?? []
            _matchDungeonOptionsLoaded = true
            _matchDungeonOptionsLevel = advLevel
            _matchmakingFetchAt = Date.now()
          }
          return data
        } finally {
          _matchDungeonOptionsPending = null
        }
      })()
      return _matchDungeonOptionsPending
    },

    async joinPublicMatchTicket(ticketId: number, confirmFateRisk = false) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/${ticketId}/join`, {
        confirm_fate_risk: confirmFateRisk,
      })
      if (data.code === 200) trackEvent('adventurer_s1_matchmaking_join_public', { ticket_id: ticketId })
      return data
    },

    async joinInviteMatchTicket(inviteCode: string) {
      const code = inviteCode.trim()
      const { data } = await api.post(`/adventurer-s1/matchmaking/code/${encodeURIComponent(code)}/join`)
      if (data.code === 200) trackEvent('adventurer_s1_matchmaking_join_invite_code', { invite_code: code })
      return data
    },

    async startMatchmakingTicket(ticketId: number) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/${ticketId}/start`)
      if (data.code === 200) trackEvent('adventurer_s1_matchmaking_start', { ticket_id: ticketId })
      return data
    },

    async stopMatchmakingTicket(ticketId: number) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/${ticketId}/stop`)
      if (data.code === 200) trackEvent('adventurer_s1_matchmaking_stop', { ticket_id: ticketId })
      return data
    },

    async makeMatchTicketPublic(ticketId: number) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/${ticketId}/make-public`)
      if (data.code === 200) trackEvent('adventurer_s1_matchmaking_make_public', { ticket_id: ticketId })
      return data
    },

    async setMatchTicketSoloFill(ticketId: number, allowSoloFill: boolean) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/${ticketId}/solo-fill`, {
        allow_solo_fill: allowSoloFill,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_matchmaking_solo_fill', {
          ticket_id: ticketId,
          allow_solo_fill: allowSoloFill,
        })
      }
      return data
    },

    async fetchMatchInviteCandidates(ticketId: number, search = '') {
      const q = new URLSearchParams()
      if (search.trim()) q.set('search', search.trim())
      const { data } = await api.get(`/adventurer-s1/matchmaking/${ticketId}/invite-candidates?${q.toString()}`)
      if (data.code === 200) this.matchInviteCandidates = data.data.items ?? []
      return data
    },

    async inviteMatchAdventurer(ticketId: number, adventurerId: number) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/${ticketId}/invite`, { adventurer_id: adventurerId })
      if (data.code === 200) trackEvent('adventurer_s1_matchmaking_invite', { ticket_id: ticketId, adventurer_id: adventurerId })
      return data
    },

    async fetchMyMatchInvites(force = false) {
      const now = Date.now()
      if (
        !force
        && _matchInvitesLoaded
        && now - _matchmakingFetchAt < MATCHMAKING_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: { items: this.matchInvites }, message: 'ok' }
      }
      return this.fetchMatchmakingBoard(force)
    },

    async acceptMatchInvite(inviteId: number) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/invites/${inviteId}/accept`)
      if (data.code === 200) trackEvent('adventurer_s1_matchmaking_invite_accept', { invite_id: inviteId })
      return data
    },

    async declineMatchInvite(inviteId: number) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/invites/${inviteId}/decline`)
      if (data.code === 200) trackEvent('adventurer_s1_matchmaking_invite_decline', { invite_id: inviteId })
      return data
    },

    async cancelMatchmaking(ticketId: number) {
      const { data } = await api.post(
        `/adventurer-s1/matchmaking/${ticketId}/cancel`,
        undefined,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) {
        this.matchTickets = this.matchTickets.filter((t) => t.id !== ticketId)
        trackEvent('adventurer_s1_matchmaking_cancel', { ticket_id: ticketId })
      }
      return data
    },

    async leaveMatchTicket(ticketId: number) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/${ticketId}/leave`)
      if (data.code === 200) {
        this.matchTickets = this.matchTickets.filter((t) => t.id !== ticketId)
        trackEvent('adventurer_s1_matchmaking_leave', { ticket_id: ticketId })
      }
      return data
    },

    // ── 门票制特殊副本 ─────────────────────────────────────────────────
    async listSpecialDungeons(advId?: number, force = false) {
      const now = Date.now()
      if (
        !force
        && _specialDungeonsLastAdvId === advId
        && _specialDungeonsLastData
        && now - _specialDungeonsFetchAt < SPECIAL_DUNGEONS_FETCH_MIN_INTERVAL_MS
      ) {
        return _specialDungeonsLastData
      }
      if (_specialDungeonsFetchPending) {
        if (!force) return _specialDungeonsFetchPending
        try {
          await _specialDungeonsFetchPending
        } catch {
          // 强制刷新不能被此前失败的后台请求吞掉。
        }
        if (_specialDungeonsFetchPending) return _specialDungeonsFetchPending
      }
      _specialDungeonsFetchPending = (async () => {
        try {
          const q = advId != null ? `?adventurer_id=${advId}` : ''
          const { data } = await api.get(`/adventurer-s1/special-dungeons${q}`)
          if (data.code === 200) {
            _specialDungeonsFetchAt = Date.now()
            _specialDungeonsLastAdvId = advId
            _specialDungeonsLastData = data
          }
          return data
        } finally {
          _specialDungeonsFetchPending = null
        }
      })()
      return _specialDungeonsFetchPending
    },

    async createSpecialParty(advId: number, dungeon: string) {
      const { data } = await api.post(`/adventurer-s1/${advId}/create-special-party`, { dungeon })
      if (data.code === 200) {
        trackEvent('adventurer_s1_create_special_party', { adv_id: advId, dungeon })
      }
      return data
    },

    async forceDispatchParty(
      partyId: number,
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await api.post(
        `/adventurer-s1/admin/party/${partyId}/force-dispatch`,
        undefined,
        { s1RealmContext },
      )
      if (data.code === 200) trackEvent('adventurer_s1_force_dispatch', { party_id: partyId })
      return data
    },

    async forceDisbandParty(
      partyId: number,
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await api.post(
        `/adventurer-s1/admin/party/${partyId}/force-disband`,
        undefined,
        { s1RealmContext },
      )
      if (data.code === 200) trackEvent('adventurer_s1_force_disband', { party_id: partyId })
      return data
    },

    async adminRemoveMember(
      partyId: number,
      adventurerId: number,
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await api.post(
        `/adventurer-s1/admin/party/${partyId}/remove-member`,
        { adventurer_id: adventurerId },
        { s1RealmContext },
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_remove_member', { party_id: partyId, adventurer_id: adventurerId })
      return data
    },

    async adminFetchPartyMatchOverview(params?: {
      party_status?: string
      ticket_status?: string
      party_limit?: number
      ticket_limit?: number
    }) {
      const { data } = await api.get('/adventurer-s1/admin/party-match-overview', {
        params,
        s1RealmContext: 'selected',
      })
      return data
    },

    // ── 特质 / 技能配置 ──────────────────────────────────────────────
    async equipTrait(advId: number, traitId: number) {
      const { data } = await api.put(`/adventurer-s1/${advId}/traits/${traitId}/equip`)
      if (data.code === 200) trackEvent('adventurer_s1_equip_trait', { adv_id: advId, trait_id: traitId })
      return data
    },

    async unequipTrait(advId: number, traitId: number) {
      const { data } = await api.put(`/adventurer-s1/${advId}/traits/${traitId}/unequip`)
      if (data.code === 200) trackEvent('adventurer_s1_unequip_trait', { adv_id: advId, trait_id: traitId })
      return data
    },

    async unequipAllTraits(advId: number) {
      const { data } = await api.put(`/adventurer-s1/${advId}/traits/unequip-all`)
      return data
    },

    async fetchSkills(advId: number) {
      const { data } = await api.get(`/adventurer-s1/${advId}/skills`)
      return data
    },

    async equipSkill(advId: number, skillId: number) {
      const { data } = await api.put(`/adventurer-s1/${advId}/skills/${skillId}/equip`)
      if (data.code === 200) trackEvent('adventurer_s1_equip_skill', { adv_id: advId, skill_id: skillId })
      return data
    },

    async unequipSkill(advId: number, skillId: number) {
      const { data } = await api.put(`/adventurer-s1/${advId}/skills/${skillId}/unequip`)
      if (data.code === 200) trackEvent('adventurer_s1_unequip_skill', { adv_id: advId, skill_id: skillId })
      return data
    },

    // ── 公告 ──────────────────────────────────────────────────────────
    async fetchNotice() {
      const { data } = await api.get('/adventurer-s1/notice', S1_MEMBERSHIP_REQUEST)
      if (data.code === 200 && data.data) {
        this.notice = data.data.notice ?? ''
        this.noticeVersion = data.data.version ?? null
        this.noticeShouldShow = !!data.data.should_show
      } else {
        this.notice = ''
        this.noticeVersion = null
        this.noticeShouldShow = false
      }
      return data
    },

    async adminFetchNotice() {
      const { data } = await api.get('/adventurer-s1/admin/notice', S1_SELECTED_REQUEST)
      if (data.code === 200 && data.data) {
        this.notice = data.data.notice ?? ''
        this.noticeVersion = data.data.version ?? null
      }
      return data
    },

    async adminExportAllPlayerData() {
      const { data } = await api.get('/adventurer-s1/admin/export/all', {
        ...S1_SELECTED_REQUEST,
        timeout: 600000,
      })
      trackEvent('adventurer_s1_admin_export_all_player_data')
      return data
    },

    async adminListExportArchives() {
      const { data } = await api.get('/adventurer-s1/admin/export/files', {
        ...S1_SELECTED_REQUEST,
      })
      return data
    },

    async ackNotice() {
      if (!this.noticeVersion || this.noticeAckLoading) return { code: 400, message: '无可确认的公告' }
      this.noticeAckLoading = true
      try {
        const { data } = await api.post('/adventurer-s1/notice/ack', { version: this.noticeVersion })
        if (data.code === 200) {
          this.noticeShouldShow = false
          trackEvent('adventurer_s1_notice_ack')
        }
        return data
      } finally {
        this.noticeAckLoading = false
      }
    },

    async adminSetNotice(notice: string) {
      const { data } = await api.put('/adventurer-s1/admin/notice', { notice }, S1_SELECTED_REQUEST)
      if (data.code === 200 && data.data) {
        this.notice = data.data.notice ?? ''
        this.noticeVersion = data.data.version ?? null
        this.noticeShouldShow = !!data.data.should_show
        trackEvent('adventurer_s1_admin_set_notice')
      }
      return data
    },

    async fetchTavernUpgradeProgress() {
      this.tavernUpgradeLoading = true
      try {
        const { data } = await api.get('/adventurer-s1/tavern/upgrade-progress', S1_MEMBERSHIP_REQUEST)
        if (data.code === 200 && data.data) {
          this.tavernUpgrade = data.data as TavernUpgradeProgress
        }
        return data
      } finally {
        this.tavernUpgradeLoading = false
      }
    },

    async adminFetchTavernUpgradeProgress(
      realm: S1AdminRealmTarget = getS1AdminRealmTarget(),
    ) {
      this.tavernUpgradeLoading = true
      try {
        const { data } = await api.get(
          '/adventurer-s1/admin/tavern/upgrade-progress',
          { ...S1_SELECTED_REQUEST, s1AdminRealmTarget: realm },
        )
        if (
          data.code === 200 && data.data
          && (!this.realmViewCode || this.realmViewCode === realm)
        ) {
          this.tavernUpgrade = data.data as TavernUpgradeProgress
        }
        return data
      } finally {
        this.tavernUpgradeLoading = false
      }
    },

    async refreshTavernAfterAdminUpgrade(realm: S1AdminRealmTarget): Promise<boolean> {
      if (this.realmViewCode !== realm) return true
      const results = await Promise.allSettled([
        this.adminFetchTavernUpgradeProgress(realm),
        this.fetchBuildings(true),
      ])
      return results.every(result => result.status === 'fulfilled' && result.value?.code === 200)
    },

    async adminUnlockTavernUpgrade(realm: S1AdminRealmTarget) {
      const { data } = await api.post(
        '/adventurer-s1/admin/tavern/unlock-upgrade',
        undefined,
        { ...S1_SELECTED_REQUEST, s1AdminRealmTarget: realm },
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_admin_unlock_tavern_upgrade')
        return { ...data, refresh_failed: !(await this.refreshTavernAfterAdminUpgrade(realm)) }
      }
      return data
    },

    async adminForceSetTavernLevel(
      realm: S1AdminRealmTarget,
      targetLevel?: number,
    ) {
      const { data } = await api.post(
        '/adventurer-s1/admin/tavern/force-set-level',
        targetLevel != null ? { target_level: targetLevel } : {},
        { ...S1_SELECTED_REQUEST, s1AdminRealmTarget: realm },
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_admin_force_set_tavern_level')
        return { ...data, refresh_failed: !(await this.refreshTavernAfterAdminUpgrade(realm)) }
      }
      return data
    },

    async setTavernUpgradeUiEnabled(
      enabled: boolean,
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await this.updateDormitorySetting(
        'tavern_upgrade_ui_enabled',
        enabled,
        s1RealmContext,
      )
      if (data.code === 200) {
        await (
          s1RealmContext === 'selected'
            ? this.adminFetchTavernUpgradeProgress()
            : this.fetchTavernUpgradeProgress()
        )
      }
      return data
    },

    async upgradeBuilding(buildingId: number) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/upgrade`)
      if (data.code === 200) trackEvent('adventurer_s1_upgrade_building', { building_id: buildingId })
      return data
    },

    async donateToBuildingUpgrade(buildingId: number, materialName: string, amount: number) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/donate-upgrade`, {
        material_name: materialName,
        amount,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_donate_building_upgrade', {
          building_id: buildingId,
          material_name: materialName,
        })
      }
      return data
    },

    // ── 全体冒险者浏览 ────────────────────────────────────────────────
    async fetchBrowseAdventurers(params: ListParams = {}) {
      const seq = ++_browseAdventurersFetchSeq
      const initial = this.browseAdventurers.length === 0
      if (initial) this.browseLoading = true
      try {
        const q = new URLSearchParams()
        const search = params.search?.trim()
        if (params.page) q.set('page', String(params.page))
        if (params.per_page) q.set('per_page', String(params.per_page))
        if (params.status) q.set('status', params.status)
        if (search) q.set('search', search)
        if (params.sort_field) q.set('sort_field', params.sort_field)
        if (params.sort_order) q.set('sort_order', params.sort_order)
        const { data } = await api.get(`/adventurer-s1/adventurers?${q.toString()}`)
        if (seq === _browseAdventurersFetchSeq && data.code === 200 && data.data) {
          this.browseAdventurers = data.data.items ?? []
          this.browsePagination = toPaginationMeta(data.data)
          if (this.tavernSeats.length > 0) {
            syncTavernSeatedFromSeats(this.tavernSeats, [this.browseAdventurers])
          }
        }
        return data
      } finally {
        if (initial && seq === _browseAdventurersFetchSeq) this.browseLoading = false
      }
    },

    // ── 小队 ──────────────────────────────────────────────────────────
    async fetchParties(params: ListParams = {}) {
      const initial = this.parties.length === 0
      if (initial) this.partiesLoading = true
      try {
        const q = new URLSearchParams()
        if (params.page) q.set('page', String(params.page))
        if (params.per_page) q.set('per_page', String(params.per_page))
        if (params.status) q.set('status', params.status)
        if (params.exclude_mine) q.set('exclude_mine', '1')
        const { data } = await api.get(`/adventurer-s1/parties?${q.toString()}`)
        if (data.code === 200 && data.data) {
          this.parties = data.data.items ?? []
          this.partiesPagination = toPaginationMeta(data.data)
        }
        return data
      } finally {
        if (initial) this.partiesLoading = false
      }
    },

    async fetchMyParties(status = 'active') {
      const initial = this.myParties.length === 0
      if (initial) this.myPartiesLoading = true
      try {
        const q = new URLSearchParams()
        q.set('mine', '1')
        q.set('status', status)
        q.set('per_page', '10')
        const { data } = await api.get(`/adventurer-s1/parties?${q.toString()}`)
        if (data.code === 200 && data.data) {
          this.myParties = data.data.items ?? []
        }
        return data
      } finally {
        if (initial) this.myPartiesLoading = false
      }
    },

    async fetchPartiesBoard(status = 'active', force = false) {
      const now = Date.now()
      const hasData = this.myParties.length > 0 || this.parties.length > 0
      if (
        !force
        && hasData
        && now - _partiesBoardFetchAt < PARTIES_BOARD_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: { my_parties: this.myParties, parties: { items: this.parties, ...this.partiesPagination } } }
      }
      if (_partiesBoardFetchPending) {
        if (!force) return _partiesBoardFetchPending
        try {
          await _partiesBoardFetchPending
        } catch {
          // 强制刷新不能被此前失败的后台请求吞掉。
        }
        // 另一个强制刷新可能已在等待期间启动，复用它即可。
        if (_partiesBoardFetchPending) return _partiesBoardFetchPending
      }

      const initial = !hasData
      if (initial) {
        this.myPartiesLoading = true
        this.partiesLoading = true
      }

      _partiesBoardFetchPending = (async () => {
        try {
          const q = new URLSearchParams({ status })
          const boardVersion = _partiesBoardVersions[status]
          if (boardVersion) q.set('version', boardVersion)
          const { data } = await api.get(`/adventurer-s1/parties/board?${q.toString()}`)
          if (data.code === 200 && data.data) {
            if (data.data.changed !== false) {
              this.myParties = data.data.my_parties ?? []
              const browse = data.data.parties
              if (browse) {
                this.parties = browse.items ?? []
                this.partiesPagination = toPaginationMeta(browse)
              }
            }
            if (data.data.version) _partiesBoardVersions[status] = data.data.version
            _partiesBoardFetchAt = Date.now()
          }
          return data
        } finally {
          if (initial) {
            this.myPartiesLoading = false
            this.partiesLoading = false
          }
          _partiesBoardFetchPending = null
        }
      })()
      return _partiesBoardFetchPending
    },

    // ── 个人交易市场 ──────────────────────────────────────────────────
    async fetchMarketListings(page = 1, rarity?: string, listingType?: string, search?: string) {
      const initial = this.marketListings.length === 0
      if (initial) this.marketLoading = true
      try {
        const q = new URLSearchParams()
        q.set('page', String(page))
        q.set('per_page', '20')
        if (rarity) q.set('rarity', rarity)
        if (listingType) q.set('listing_type', listingType)
        const keyword = search?.trim()
        if (keyword) q.set('q', keyword)
        const { data } = await api.get(`/adventurer-s1/market/listings?${q.toString()}`)
        if (data.code === 200 && data.data) {
          this.marketListings = data.data.items ?? []
          this.marketPendingOfferCount = data.data.my_pending_offer_count ?? 0
          this.marketPagination = toPaginationMeta(data.data)
          this.marketFees = data.data.fees ?? {}
          this.marketSellPriceRange = data.data.sell_price_range ?? {}
          this.marketSellFeeRate = data.data.sell_fee_rate ?? 0.2
        }
        return data
      } finally {
        if (initial) this.marketLoading = false
      }
    },

    async fetchMyMarketListings() {
      const { data } = await api.get('/adventurer-s1/market/my')
      if (data.code === 200 && data.data) {
        this.myMarketActive = data.data.active ?? []
        this.myMarketHistory = data.data.history ?? []
      }
      return data
    },

    async createMarketListing(
      advId: number, warehouseItemId: number,
      listingType: 'barter' | 'sell' = 'barter', price?: number, wantNote?: string,
      quantity?: number,
    ) {
      const body: Record<string, unknown> = {
        adventurer_id: advId, warehouse_item_id: warehouseItemId,
        listing_type: listingType,
      }
      if (listingType === 'sell' && price != null) body.price = price
      if (listingType === 'barter' && wantNote) body.want_note = wantNote
      if (quantity != null) body.quantity = quantity
      const { data } = await api.post('/adventurer-s1/market/listings', body)
      if (data.code === 201 || data.code === 200) {
        trackEvent('adventurer_s1_market_list', { adv_id: advId, warehouse_item_id: warehouseItemId, listing_type: listingType })
      }
      return data
    },

    async cancelMarketListing(listingId: number) {
      const { data } = await api.post(`/adventurer-s1/market/listings/${listingId}/cancel`)
      return data
    },

    async exchangeMarketListing(listingId: number, advId: number, offeredWarehouseItemId: number, quantity?: number) {
      const payload: Record<string, unknown> = {
        adventurer_id: advId, offered_warehouse_item_id: offeredWarehouseItemId,
      }
      if (quantity != null && quantity > 0) payload.quantity = quantity
      const { data } = await api.post(`/adventurer-s1/market/listings/${listingId}/exchange`, payload)
      if (data.code === 200) {
        trackEvent('adventurer_s1_market_exchange', { adv_id: advId, listing_id: listingId })
      }
      return data
    },

    async buyMarketListing(listingId: number, advId: number, quantity?: number) {
      const payload: { adventurer_id: number; quantity?: number } = { adventurer_id: advId }
      if (quantity != null && quantity > 0) payload.quantity = quantity
      const { data } = await api.post(`/adventurer-s1/market/listings/${listingId}/buy`, payload)
      if (data.code === 200) {
        trackEvent('adventurer_s1_market_buy', { adv_id: advId, listing_id: listingId })
      }
      return data
    },

    async fetchMarketSlotInfo(adventurerId: number) {
      const { data } = await api.get(`/adventurer-s1/market/slot-info?adventurer_id=${adventurerId}`)
      if (data.code === 200 && data.data) {
        this.marketSlotInfo = data.data
      }
      return data
    },

    async purchaseMarketSlots(adventurerId: number) {
      const { data } = await api.post('/adventurer-s1/market/purchase-slots', {
        adventurer_id: adventurerId,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_market_purchase_slots', { adv_id: adventurerId })
      }
      return data
    },

    // ── 以物易物出价 ────────────────────────────────────────────────────
    async updateWantNote(listingId: number, wantNote: string) {
      const { data } = await api.patch(`/adventurer-s1/market/listings/${listingId}`, {
        want_note: wantNote,
      })
      return data
    },

    async submitOffer(listingId: number, advId: number, offeredWarehouseItemId: number, quantity?: number) {
      const payload: Record<string, unknown> = {
        adventurer_id: advId, offered_warehouse_item_id: offeredWarehouseItemId,
      }
      if (quantity != null && quantity > 0) payload.quantity = quantity
      const { data } = await api.post(`/adventurer-s1/market/listings/${listingId}/offers`, payload)
      if (data.code === 201 || data.code === 200) {
        trackEvent('adventurer_s1_market_offer_submit', { adv_id: advId, listing_id: listingId })
      }
      return data
    },

    async cancelOffer(offerId: number) {
      const { data } = await api.post(`/adventurer-s1/market/offers/${offerId}/cancel`)
      return data
    },

    async acceptOffer(listingId: number, offerId: number) {
      const { data } = await api.post(`/adventurer-s1/market/listings/${listingId}/offers/${offerId}/accept`)
      if (data.code === 200) {
        trackEvent('adventurer_s1_market_offer_accept', { listing_id: listingId, offer_id: offerId })
      }
      return data
    },

    async updateMatchTicketDescription(ticketId: number, description: string) {
      const { data } = await api.put(`/adventurer-s1/matchmaking/${ticketId}/description`, { description })
      return data
    },

    async rejectOffer(listingId: number, offerId: number) {
      const { data } = await api.post(`/adventurer-s1/market/listings/${listingId}/offers/${offerId}/reject`)
      return data
    },

    async rejectMarketBuyOffer(orderId: number, offerId: number) {
      const { data } = await api.post(`/adventurer-s1/market/buy-orders/${orderId}/offers/${offerId}/reject`)
      return data
    },

    async fetchOffersForListing(listingId: number) {
      const { data } = await api.get(`/adventurer-s1/market/listings/${listingId}/offers`)
      if (data.code === 200 && data.data) {
        this.listingOffers = data.data.offers ?? []
        this.listingOffersTarget = data.data.listing ?? null
      }
      return data
    },

    async fetchMyOffers() {
      const { data } = await api.get('/adventurer-s1/market/my-offers')
      if (data.code === 200 && data.data) {
        this.myOffers = data.data.offers ?? []
      }
      return data
    },

    // ── 求购单 / 物品展示 ───────────────────────────────────────────────
    async fetchMarketBuyOrders(page = 1, rarity?: string, search?: string) {
      const requestId = ++this.marketBuyOrdersRequestId
      const initial = this.marketBuyOrders.length === 0
      if (initial) this.marketBuyOrdersLoading = true
      try {
        const q = new URLSearchParams({ page: String(page), per_page: '20' })
        if (rarity) q.set('rarity', rarity)
        const keyword = search?.trim()
        if (keyword) q.set('q', keyword)
        const { data } = await api.get(`/adventurer-s1/market/buy-orders?${q.toString()}`)
        if (data.code === 200 && data.data && requestId === this.marketBuyOrdersRequestId) {
          this.marketBuyOrders = data.data.items ?? []
          this.marketBuyOrdersPagination = toPaginationMeta(data.data)
          this.marketBuyOrderPendingOfferCount = data.data.my_pending_offer_count ?? 0
          this.marketSellPriceRange = data.data.sell_price_range ?? this.marketSellPriceRange
          this.marketSellFeeRate = data.data.sell_fee_rate ?? this.marketSellFeeRate
        }
        return data
      } finally {
        if (initial && requestId === this.marketBuyOrdersRequestId) {
          this.marketBuyOrdersLoading = false
        }
      }
    },

    async createMarketBuyOrder(
      advId: number,
      prices: Record<string, number>,
      note?: string,
      keepOpen = false,
    ) {
      const { data } = await api.post('/adventurer-s1/market/buy-orders', {
        adventurer_id: advId,
        prices,
        note: note || undefined,
        keep_open: keepOpen,
      })
      if (data.code === 201 || data.code === 200) {
        trackEvent('adventurer_s1_market_buy_order_create', {
          adv_id: advId,
          keep_open: keepOpen,
        })
      }
      return data
    },

    async fetchMyMarketBuyOrders() {
      const { data } = await api.get('/adventurer-s1/market/my-buy-orders')
      if (data.code === 200 && data.data) {
        this.myMarketBuyOrdersActive = data.data.active ?? []
        this.myMarketBuyOrdersHistory = data.data.history ?? []
      }
      return data
    },

    async cancelMarketBuyOrder(orderId: number) {
      const { data } = await api.post(`/adventurer-s1/market/buy-orders/${orderId}/cancel`)
      return data
    },

    async submitMarketBuyOffer(
      orderId: number,
      advId: number,
      warehouseItemId: number,
      quantity?: number,
    ) {
      const payload: Record<string, unknown> = {
        adventurer_id: advId,
        warehouse_item_id: warehouseItemId,
      }
      if (quantity != null && quantity > 0) payload.quantity = quantity
      const { data } = await api.post(
        `/adventurer-s1/market/buy-orders/${orderId}/offers`,
        payload,
      )
      if (data.code === 201 || data.code === 200) {
        trackEvent('adventurer_s1_market_buy_offer_submit', {
          adv_id: advId,
          buy_order_id: orderId,
        })
      }
      return data
    },

    async fetchMarketBuyOrderOffers(orderId: number) {
      const requestId = ++this.marketBuyOrderOffersRequestId
      if (this.marketBuyOrderOffersTarget?.id !== orderId) {
        this.marketBuyOrderOffers = []
        this.marketBuyOrderOffersTarget = null
      }
      const { data } = await api.get(`/adventurer-s1/market/buy-orders/${orderId}/offers`)
      if (
        requestId === this.marketBuyOrderOffersRequestId
        && data.code === 200
        && data.data?.buy_order?.id === orderId
      ) {
        this.marketBuyOrderOffers = data.data.offers ?? []
        this.marketBuyOrderOffersTarget = data.data.buy_order ?? null
      }
      return data
    },

    async acceptMarketBuyOffers(orderId: number, offerIds: number[]) {
      const { data } = await api.post(
        `/adventurer-s1/market/buy-orders/${orderId}/offers/accept`,
        { offer_ids: offerIds },
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_market_buy_offers_accept', {
          buy_order_id: orderId,
          offer_count: offerIds.length,
        })
      }
      return data
    },

    async acceptMarketBuyOffer(orderId: number, offerId: number) {
      return this.acceptMarketBuyOffers(orderId, [offerId])
    },

    async cancelMarketBuyOffer(offerId: number) {
      const { data } = await api.post(`/adventurer-s1/market/buy-offers/${offerId}/cancel`)
      return data
    },

    async fetchMyMarketBuyOffers() {
      const { data } = await api.get('/adventurer-s1/market/my-buy-offers')
      if (data.code === 200 && data.data) {
        this.myMarketBuyOffers = data.data.offers ?? []
      }
      return data
    },

    // ── 隐秘拍卖行 ──────────────────────────────────────────────────
    async fetchSecretAuctionLots(page = 1, adventurerId?: number) {
      const initial = this.secretAuctionLots.length === 0
      if (initial) this.secretAuctionLoading = true
      try {
        const q = new URLSearchParams()
        q.set('page', String(page))
        q.set('per_page', '20')
        if (adventurerId) q.set('adventurer_id', String(adventurerId))
        const { data } = await api.get(`/adventurer-s1/secret-auction/lots?${q.toString()}`)
        if (data.code === 200 && data.data) {
          this.secretAuctionLots = data.data.items ?? []
          this.secretAuctionPagination = toPaginationMeta(data.data)
        }
        return data
      } finally {
        if (initial) this.secretAuctionLoading = false
      }
    },

    async fetchSecretAuctionHistory(page = 1, adventurerId?: number) {
      const initial = this.secretAuctionHistory.length === 0
      if (initial) this.secretAuctionHistoryLoading = true
      try {
        const q = new URLSearchParams()
        q.set('page', String(page))
        q.set('per_page', '20')
        if (adventurerId) q.set('adventurer_id', String(adventurerId))
        const { data } = await api.get(`/adventurer-s1/secret-auction/history?${q.toString()}`)
        if (data.code === 200 && data.data) {
          this.secretAuctionHistory = (data.data.items ?? []) as SecretAuctionLotHistory[]
          this.secretAuctionHistoryPagination = toPaginationMeta(data.data)
        }
        return data
      } finally {
        if (initial) this.secretAuctionHistoryLoading = false
      }
    },

    async placeSecretAuctionBid(lotId: number, advId: number, amount: number) {
      const { data } = await api.post(`/adventurer-s1/secret-auction/lots/${lotId}/bid`, {
        adventurer_id: advId,
        amount,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_secret_auction_bid', { lot_id: lotId, adv_id: advId })
      }
      return data
    },

    async cancelSecretAuctionBid(lotId: number, advId: number) {
      const { data } = await api.delete(`/adventurer-s1/secret-auction/lots/${lotId}/bid`, {
        data: { adventurer_id: advId },
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_secret_auction_bid_cancel', { lot_id: lotId, adv_id: advId })
      }
      return data
    },

    async fetchAdminSecretAuctionLots(page = 1, status?: string) {
      const q = new URLSearchParams()
      q.set('page', String(page))
      q.set('per_page', '20')
      if (status) q.set('status', status)
      const { data } = await api.get(`/adventurer-s1/admin/secret-auction/lots?${q.toString()}`)
      return data
    },

    async adminCreateSecretAuctionLots(
      endsAt: string,
      lots: Record<string, unknown>[],
      reservePrice?: number,
    ) {
      const { data } = await api.post('/adventurer-s1/admin/secret-auction/lots', {
        ends_at: endsAt,
        reserve_price: reservePrice,
        lots,
      })
      return data
    },

    async adminCancelSecretAuctionLot(lotId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/secret-auction/lots/${lotId}/cancel`)
      return data
    },

    async adminPreviewUnfundedFix() {
      const { data } = await api.get('/adventurer-s1/admin/secret-auction/fix-unfunded-preview')
      return data
    },

    async adminApplyUnfundedFix() {
      const { data } = await api.post('/adventurer-s1/admin/secret-auction/fix-unfunded')
      return data
    },

    // ── 探险日志 ──────────────────────────────────────────────────────
    async fetchLogs(
      params: ListParams & {
        adventurer_id?: number
        category?: 'special' | 'favorite'
        exclude_failed?: boolean
        force?: boolean
      } = {},
    ) {
      const q = new URLSearchParams()
      const search = params.search?.trim()
      if (params.page) q.set('page', String(params.page))
      if (params.per_page) q.set('per_page', String(params.per_page))
      if (search) q.set('search', search)
      if (params.adventurer_id) q.set('adventurer_id', String(params.adventurer_id))
      if (params.status) q.set('status', params.status)
      if (params.exclude_failed) q.set('exclude_failed', '1')
      if (params.category) q.set('category', params.category)
      const query = q.toString()
      const currentKey = () => logsReadKey(this.squadSessionUserId, this.realmViewCode, query)
      const fetchKey = currentKey()
      if (params.force) _logsLastResponse = null
      if (_logsFetchPending?.key === fetchKey) {
        // Socket/手动刷新只标记补读，同一查询不会并发挤占连接。
        if (params.force) _logsFetchPending.refreshRequested = true
        return _logsFetchPending.promise
      }
      if (
        !params.force
        && _logsLastResponse
        && fetchKey === _logsFetchKey
        && Date.now() - _logsFetchAt < LOGS_FETCH_MIN_INTERVAL_MS
      ) {
        // 切回缓存页时，仍须使另一页尚未完成的请求失效。
        _logsFetchPending = null
        this.logsLoading = false
        return _logsLastResponse
      }

      this.logsLoading = this.logs.length === 0
      const request = {
        key: fetchKey, promise: null! as Promise<any>,
        refreshRequested: false, discardResponse: false,
      }
      const isCurrent = () => _logsFetchPending === request && currentKey() === fetchKey
      _logsFetchPending = request
      request.promise = (async () => {
        try {
          let data: any
          do {
            request.refreshRequested = false
            request.discardResponse = false
            const response = await api.get(`/adventurer-s1/logs?${query}`)
            data = response.data
            if (!isCurrent()) return { code: 0, stale_request: true }
            if (data.code !== 200 || !data.data) return data
            // 持续通知期间也展示已返回的当前页，避免每次响应都被后续刷新作废。
            // 收藏写入后发起的补读例外：旧响应不能撤销刚完成的收藏操作。
            if (!request.discardResponse) {
              this.logs = data.data.items ?? []
              this.logsPagination = toPaginationMeta(data.data)
              this.logsLoading = false
              if (data.data.expedition_config) {
                this._expeditionConfig = data.data.expedition_config
              }
              // 已收到更新通知的中间快照可展示，但不能当作新鲜缓存复用。
              if (!request.refreshRequested) {
                _logsFetchKey = fetchKey
                _logsFetchAt = Date.now()
                _logsLastResponse = data
              }
            }
          } while (request.refreshRequested && isCurrent())
          return data
        } finally {
          if (_logsFetchPending === request) {
            this.logsLoading = false
            _logsFetchPending = null
          }
        }
      })()
      return request.promise
    },

    // 名片「📜 探险」弹窗专用：按冒险者拉日志但不写 this.logs / logsPagination，
    // 避免覆盖「探险日志」tab 自身的列表状态。
    async fetchAdventurerLogs(
      params: ListParams & {
        adventurer_id: number
        category?: 'special' | 'favorite'
        exclude_failed?: boolean
      },
    ): Promise<{ items: ExpeditionLogSummary[]; pagination: PaginationMeta | null }> {
      const q = new URLSearchParams()
      const search = params.search?.trim()
      if (params.page) q.set('page', String(params.page))
      if (params.per_page) q.set('per_page', String(params.per_page))
      if (search) q.set('search', search)
      if (params.status) q.set('status', params.status)
      if (params.exclude_failed) q.set('exclude_failed', '1')
      if (params.category) q.set('category', params.category)
      q.set('adventurer_id', String(params.adventurer_id))
      const { data } = await api.get(
        `/adventurer-s1/logs?${q.toString()}`,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200 && data.data) {
        return { items: data.data.items ?? [], pagination: toPaginationMeta(data.data) }
      }
      return { items: [], pagination: null }
    },

    async fetchLogDetail(logId: number, options: { revealFull?: boolean } = {}) {
      const seq = ++_logDetailFetchSeq
      const suffix = options.revealFull ? '?reveal_full=1' : ''
      const { data } = await api.get(
        `/adventurer-s1/logs/${logId}${suffix}`,
        S1_MEMBERSHIP_REQUEST,
      )
      if (seq !== _logDetailFetchSeq) return data
      if (data.code === 200 && data.data) {
        this.currentLog = data.data as ExpeditionLogDetail
      }
      return data
    },

    async likeExpeditionLog(logId: number, toAdventurerId: number) {
      const { data } = await api.post(`/adventurer-s1/logs/${logId}/like`, {
        to_adventurer_id: toAdventurerId,
      })
      if (data.code === 200 && data.data?.log) {
        this.currentLog = data.data.log as ExpeditionLogDetail
      }
      return data
    },

    async submitAiFeedback(
      target: Pick<AiFeedbackTarget, 'target_type' | 'target_id'>,
      qualityScore: number,
      preferenceScore: number,
    ) {
      const { data } = await api.post('/adventurer-s1/ai-feedback', {
        target_type: target.target_type,
        target_id: target.target_id,
        quality_score: qualityScore,
        preference_score: preferenceScore,
      })
      return data
    },

    async adminFetchAiFeedback(params: {
      days?: number | 'all'
      target_type?: AiFeedbackTargetType | 'all'
      page?: number
      per_page?: number
    } = {}) {
      const query = new URLSearchParams()
      if (params.days != null) query.set('days', String(params.days))
      if (params.target_type && params.target_type !== 'all') {
        query.set('target_type', params.target_type)
      }
      if (params.page) query.set('page', String(params.page))
      if (params.per_page) query.set('per_page', String(params.per_page))
      const suffix = query.toString() ? `?${query.toString()}` : ''
      const { data } = await api.get(`/adventurer-s1/admin/ai-feedback${suffix}`)
      return data
    },

    async adminGetExpeditionObserverReview(logId: number) {
      const { data } = await api.get(
        `/adventurer-s1/admin/expedition/logs/${logId}/observer-review`,
        S1_MEMBERSHIP_REQUEST,
      )
      return data
    },

    async adminApproveExpeditionObserverReview(reviewId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/expedition/observer-reviews/${reviewId}/approve`,
        undefined,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200 && data.data?.log) {
        this.currentLog = data.data.log as ExpeditionLogDetail
      }
      return data
    },

    async adminRetryExpeditionObserverReview(logId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/expedition/logs/${logId}/observer-review/retry`,
        undefined,
        S1_MEMBERSHIP_REQUEST,
      )
      return data
    },

    // ── 聊天 ──────────────────────────────────────────────────────────
    async fetchChat(page: number = 1) {
      const { data } = await api.get(`/adventurer-s1/chat?page=${page}&per_page=30`)
      if (data.code === 200 && data.data) {
        const messages: ChatMessage[] = data.data.messages ?? []
        if (page === 1) {
          this.chatMessages = messages
        } else {
          this.chatMessages.push(...messages)
        }
        this.chatHasNext = data.data.has_next ?? false
        this.chatPage = page
      }
      return data
    },

    async postChat(content: string) {
      const { data } = await api.post('/adventurer-s1/chat', { content })
      if (data.code === 200 || data.code === 201) trackEvent('adventurer_s1_post_chat')
      return data
    },

    // ── 酒馆大堂座位 / 点酒 ───────────────────────────────────────────
    async fetchTavernSeats(silent = false, fresh = false) {
      const currentKey = () => tavernSeatsReadKey(this.squadSessionUserId, this.realmViewCode)
      const key = currentKey()
      if (fresh) _tavernSeatsLastRead = null
      if (_tavernSeatsPending?.key === key) {
        // A mutation/socket invalidation needs a read that starts after it.
        // Polls and repeated component reads can share the current request.
        if (fresh) _tavernSeatsPending.refreshRequested = true
        return _tavernSeatsPending.promise
      }
      if (
        !fresh && _tavernSeatsLastRead?.key === key
        && Date.now() - _tavernSeatsLastRead.at < TAVERN_SEATS_FETCH_MIN_INTERVAL_MS
      ) return _tavernSeatsLastRead.data

      const initial = this.tavernSeats.length === 0
      if (!silent && initial) this.tavernSeatsLoading = true
      const request = { key, promise: null! as Promise<any>, refreshRequested: false }
      const isCurrent = () => _tavernSeatsPending === request && currentKey() === key
      _tavernSeatsPending = request
      request.promise = (async () => {
        try {
          let data: any
          do {
            request.refreshRequested = false
            const response = await api.get('/adventurer-s1/tavern/seats')
            data = response.data
            if (!isCurrent()) return { code: 0, stale_session: true }
            if (data.code !== 200) return data
            // Do not overwrite a just-completed action with an older read.
            if (request.refreshRequested) continue
            if (data.code === 200 && data.data) {
              const incoming: TavernSeat[] = data.data.seats ?? []
              if (typeof data.data.server_time === 'string') {
                const serverMs = Date.parse(data.data.server_time)
                if (Number.isFinite(serverMs)) this.tavernServerOffsetMs = serverMs - Date.now()
              }
              if (!silent || !patchTavernSeatsInPlace(this.tavernSeats, incoming)) {
                this.tavernSeats = incoming
              }
              syncTavernSeatedFromSeats(this.tavernSeats, [this.adventurers, this.browseAdventurers])
              if (this.currentDetail) syncTavernSeatedFromSeats(this.tavernSeats, [[this.currentDetail]])
              _tavernSeatsLastRead = { key, at: Date.now(), data }
            }
          } while (request.refreshRequested && isCurrent())
          return data
        } finally {
          if (_tavernSeatsPending === request) {
            this.tavernSeatsLoading = false
            _tavernSeatsPending = null
          }
        }
      })()
      return request.promise
    },

    async fetchDrinkMenu() {
      if (isValidDrinkMenu(this.drinkMenu)) return { code: 200, cached: true }
      const { data } = await api.get('/adventurer-s1/tavern/drinks')
      if (data.code === 200 && data.data && isValidDrinkMenu(data.data.drinks)) {
        this.drinkMenu = data.data.drinks
      }
      return data
    },

    async fetchFoodMenu() {
      if (isValidFoodMenu(this.foodMenu)) return
      const { data } = await api.get('/adventurer-s1/tavern/foods')
      if (data.code === 200 && data.data && isValidFoodMenu(data.data.foods)) {
        this.foodMenu = data.data.foods
      }
    },

    async sitSeat(seatId: number, adventurerId: number) {
      const { data } = await api.post(`/adventurer-s1/tavern/seats/${seatId}/sit`, { adventurer_id: adventurerId })
      if (data.code === 200) {
        const lists = [
          this.adventurers,
          this.browseAdventurers,
          ...(this.currentDetail ? [[this.currentDetail]] : []),
        ]
        const snapshot = data.data as TavernRecoverySnapshot | undefined
        patchTavernSeated(lists, adventurerId, true)
        if (snapshot?.adventurer_id === adventurerId) {
          patchTavernRecovery(lists, snapshot)
        }
        trackEvent('adventurer_s1_tavern_sit', { seat_id: seatId })
      }
      return data
    },

    async leaveSeat(seatId: number) {
      const advId = this.tavernSeats.find((s) => s.id === seatId)?.adventurer?.id
      const { data } = await api.post(`/adventurer-s1/tavern/seats/${seatId}/leave`)
      if (data.code === 200) {
        const snapshot = data.data as TavernRecoverySnapshot | undefined
        const settledAdvId = snapshot?.adventurer_id ?? advId
        if (settledAdvId != null) {
          const lists = [
            this.adventurers,
            this.browseAdventurers,
            ...(this.currentDetail ? [[this.currentDetail]] : []),
          ]
          patchTavernSeated(lists, settledAdvId, false)
          if (snapshot?.adventurer_id === settledAdvId) {
            patchTavernRecovery(lists, snapshot)
          }
        }
      }
      return data
    },

    async setExpeditionLogFavorite(logId: number, favorited: boolean) {
      const ownerKey = logsReadKey(this.squadSessionUserId, this.realmViewCode)
      const { data } = favorited
        ? await api.put(`/adventurer-s1/logs/${logId}/favorite`)
        : await api.delete(`/adventurer-s1/logs/${logId}/favorite`)
      if (ownerKey !== logsReadKey(this.squadSessionUserId, this.realmViewCode)) return data
      if (data.code === 200 && data.data) {
        const value = Boolean(data.data.is_favorited)
        const item = this.logs.find(log => log.id === logId)
        if (item) item.is_favorited = value
        if (this.currentLog?.id === logId) this.currentLog.is_favorited = value
        _logsFetchKey = ''
        _logsFetchAt = 0
        _logsLastResponse = null
        if (_logsFetchPending) {
          _logsFetchPending.refreshRequested = true
          _logsFetchPending.discardResponse = true
        }
      }
      return data
    },

    async fetchExpeditionComments(
      logId: number,
      targetType: ExpeditionCommentTargetType,
      targetIndex: number,
      page: number = 1,
    ) {
      const query = new URLSearchParams({
        target_type: targetType,
        target_index: String(targetIndex),
        page: String(page),
        per_page: '20',
      })
      const { data } = await api.get(`/adventurer-s1/logs/${logId}/comments?${query.toString()}`)
      return data as { code: number; message?: string; data?: ExpeditionCommentPage }
    },

    async postExpeditionComment(
      logId: number,
      targetType: ExpeditionCommentTargetType,
      targetIndex: number,
      content: string,
    ) {
      const { data } = await api.post(`/adventurer-s1/logs/${logId}/comments`, {
        target_type: targetType,
        target_index: targetIndex,
        content,
      })
      if (data.code === 200 || data.code === 201) {
        const count = Number(data.data?.comment_count ?? 0)
        if (this.currentLog?.id === logId && this.currentLog.comment_counts) {
          this.currentLog.comment_counts[targetType][String(targetIndex)] = count
        }
      }
      return data as {
        code: number
        message?: string
        data?: { comment: ExpeditionComment; comment_count: number }
      }
    },

    async confirmMatchTicketFateRisk(ticketId: number) {
      const { data } = await api.post(`/adventurer-s1/matchmaking/${ticketId}/fate-risk/confirm`)
      if (data.code === 200) {
        trackEvent('adventurer_s1_matchmaking_fate_risk_confirm', { ticket_id: ticketId })
      }
      return data
    },

    async orderDrink(seatId: number, drinkKey: string) {
      const { data } = await api.post(`/adventurer-s1/tavern/seats/${seatId}/order`, { drink_key: drinkKey })
      if (data.code === 200) trackEvent('adventurer_s1_tavern_order_drink', { drink_key: drinkKey })
      return data
    },

    async orderFood(seatId: number, foodKey: string) {
      const { data } = await api.post(`/adventurer-s1/tavern/seats/${seatId}/order-food`, { food_key: foodKey })
      if (data.code === 200) trackEvent('adventurer_s1_tavern_order_food', { food_key: foodKey })
      return data
    },

    async requestTreat(seatId: number, drinkKey: string) {
      const { data } = await api.post(`/adventurer-s1/tavern/seats/${seatId}/treat`, { drink_key: drinkKey })
      if (data.code === 200) trackEvent('adventurer_s1_tavern_request_treat', { seat_id: seatId, drink_key: drinkKey })
      return data
    },

    async respondTreat(requestId: number, accept: boolean) {
      const { data } = await api.post(`/adventurer-s1/tavern/treat-requests/${requestId}/respond`, { accept })
      if (data.code === 200) trackEvent('adventurer_s1_tavern_respond_treat', { request_id: requestId, accept })
      return data
    },

    // ── 深渊茶舍座位 ──────────────────────────────────────────────────
    async fetchTeaHouseMenu() {
      if (this.teaHouseMenu.length > 0) return
      const { data } = await api.get('/adventurer-s1/tea-house/teas')
      if (data.code === 200 && data.data) {
        this.teaHouseMenu = data.data.teas ?? []
      }
    },

    async fetchTeaHouseSeats(silent: boolean = false, force: boolean = false) {
      const initial = this.teaHouseSeats.length === 0
      const now = Date.now()
      if (
        !force
        && !initial
        && now - _teaHouseSeatsFetchAt < TEA_HOUSE_SEATS_FETCH_MIN_INTERVAL_MS
      ) {
        return {
          code: 200,
          data: {
            seats: this.teaHouseSeats,
            entry_fee: this.teaHouseEntryFee,
            low_consumption_gold: this.teaHouseLowConsumptionGold,
            low_consumption_gold_cap: this.teaHouseLowConsumptionGoldCap,
          },
          message: 'cached',
        }
      }
      if (_teaHouseSeatsFetchPending) return _teaHouseSeatsFetchPending
      if (!silent && initial) this.teaHouseSeatsLoading = true
      _teaHouseSeatsFetchPending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/tea-house/seats')
          if (data.code === 200 && data.data) {
            this.teaHouseSeats = data.data.seats ?? []
            this.teaHouseStaminaPerMinute = Number(
              data.data.stamina_per_minute ?? STAMINA_RECOVERY_PER_MINUTE,
            )
            this.teaHouseEntryFee = Number(data.data.entry_fee ?? 30)
            this.teaHouseLowConsumptionGold = Number(data.data.low_consumption_gold ?? 0)
            this.teaHouseLowConsumptionGoldCap = Number(
              data.data.low_consumption_gold_cap ?? 100,
            )
            syncTeaHouseSeatedFromSeats(
              this.teaHouseSeats,
              this.teaHouseStaminaPerMinute,
              [this.adventurers, ...(this.currentDetail ? [[this.currentDetail]] : [])],
            )
            _teaHouseSeatsFetchAt = Date.now()
          }
          return data
        } finally {
          _teaHouseSeatsFetchPending = null
          if (!silent && initial) this.teaHouseSeatsLoading = false
        }
      })()
      return _teaHouseSeatsFetchPending
    },

    async sitTeaHouse(seatId: number, adventurerId: number) {
      const { data } = await api.post(`/adventurer-s1/tea-house/seats/${seatId}/sit`, { adventurer_id: adventurerId })
      if (data.code === 200) {
        trackEvent('adventurer_s1_tea_house_sit', { seat_id: seatId })
        await this.fetchTeaHouseSeats(true, true)
        await this.fetchAdventurers()
      }
      return data
    },

    async orderTeaHouse(seatId: number, teaKey: string) {
      const { data } = await api.post(`/adventurer-s1/tea-house/seats/${seatId}/order`, { tea_key: teaKey })
      if (data.code === 200) {
        trackEvent('adventurer_s1_tea_house_order_tea', { tea_key: teaKey })
        await this.fetchTeaHouseSeats(true, true)
        await this.fetchAdventurers()
      }
      return data
    },

    async leaveTeaHouse(seatId: number) {
      const { data } = await api.post(`/adventurer-s1/tea-house/seats/${seatId}/leave`)
      if (data.code === 200) {
        await this.fetchTeaHouseSeats(true, true)
        await this.fetchAdventurers()
      }
      return data
    },

    // ── 深渊温泉座位 ──────────────────────────────────────────────────
    async fetchHotSpringMenu() {
      if (this.hotSpringMenu.length > 0) return
      const { data } = await api.get('/adventurer-s1/hot-spring/drinks')
      if (data.code === 200 && data.data) {
        this.hotSpringMenu = data.data.drinks ?? []
      }
    },

    async fetchHotSpringSeats(silent: boolean = false) {
      const initial = this.hotSpringSeats.length === 0
      if (!silent && initial) this.hotSpringSeatsLoading = true
      try {
        const { data } = await api.get('/adventurer-s1/hot-spring/seats')
        if (data.code === 200 && data.data) {
          this.hotSpringSeats = data.data.seats ?? []
        }
        return data
      } finally {
        if (!silent && initial) this.hotSpringSeatsLoading = false
      }
    },

    async sitHotSpring(seatId: number, adventurerId: number) {
      const { data } = await api.post(`/adventurer-s1/hot-spring/seats/${seatId}/sit`, { adventurer_id: adventurerId })
      if (data.code === 200) {
        trackEvent('adventurer_s1_hot_spring_sit', { seat_id: seatId })
        await this.fetchHotSpringSeats(true)
        await this.fetchAdventurers()
      }
      return data
    },

    async orderHotSpring(seatId: number, drinkKey: string) {
      const { data } = await api.post(`/adventurer-s1/hot-spring/seats/${seatId}/order`, { drink_key: drinkKey })
      if (data.code === 200) {
        trackEvent('adventurer_s1_hot_spring_order_drink', { drink_key: drinkKey })
        await this.fetchHotSpringSeats(true)
        await this.fetchAdventurers()
      }
      return data
    },

    async leaveHotSpring(seatId: number) {
      const { data } = await api.post(`/adventurer-s1/hot-spring/seats/${seatId}/leave`)
      if (data.code === 200) {
        await this.fetchHotSpringSeats(true)
        await this.fetchAdventurers()
      }
      return data
    },

    // ── 关注 / 取关 ───────────────────────────────────────────────────
    async fetchInsuranceStatus(adventurerId: number) {
      const { data } = await api.get('/adventurer-s1/insurance/status', {
        params: { adventurer_id: adventurerId },
      })
      return data
    },

    async buyInsurance(adventurerId: number, insuranceType: InsurancePolicyStatus['code'] = 'injury') {
      const { data } = await api.post('/adventurer-s1/insurance/buy', {
        adventurer_id: adventurerId,
        insurance_type: insuranceType,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_insurance_buy', { adv_id: adventurerId, insurance_type: insuranceType })
        await this.fetchAdventurers()
      }
      return data
    },

    async follow(advId: number) {
      const { data } = await api.post(`/adventurer-s1/${advId}/follow`)
      if (data.code === 200) {
        this._setFollowState(advId, true)
        trackEvent('adventurer_s1_follow', { adv_id: advId })
      }
      return data
    },

    async unfollow(advId: number) {
      const { data } = await api.post(`/adventurer-s1/${advId}/unfollow`)
      if (data.code === 200) {
        this._setFollowState(advId, false)
        trackEvent('adventurer_s1_unfollow', { adv_id: advId })
      }
      return data
    },

    _setFollowState(advId: number, isFollowed: boolean) {
      const target = this.browseAdventurers.find((a) => a.id === advId)
      if (target) target.is_followed = isFollowed
      if (this.currentDetail && this.currentDetail.id === advId) {
        this.currentDetail.is_followed = isFollowed
      }
    },

    // ── 管理员：预约管理 ────────────────────────────────────────────────
    async fetchReservations(params: { page?: number; per_page?: number; status?: string; priority?: string; search?: string } = {}) {
      this.reservationsLoading = true
      try {
        const q = new URLSearchParams()
        if (params.page) q.set('page', String(params.page))
        if (params.per_page) q.set('per_page', String(params.per_page))
        if (params.status) q.set('status', params.status)
        if (params.priority) q.set('priority', params.priority)
        if (params.search) q.set('search', params.search)
        const { data } = await api.get(`/adventurer-s1/admin/reservations?${q.toString()}`)
        if (data.code === 200 && data.data) {
          this.reservations = data.data.items ?? []
          this.reservationsPagination = toPaginationMeta(data.data)
          this.reservationsCapacity = data.data.capacity ?? null
        }
        return data
      } finally {
        this.reservationsLoading = false
      }
    },

    async setReservationPriority(resId: number, priority: string) {
      const { data } = await api.post(`/adventurer-s1/admin/reservations/${resId}/priority`, { priority })
      return data
    },

    async rejectReservation(resId: number, reason: string) {
      const { data } = await api.post(`/adventurer-s1/admin/reservations/${resId}/reject`, { reason })
      return data
    },

    async restoreReservation(resId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/reservations/${resId}/restore`)
      return data
    },

    async drawAdventurers(count: number) {
      const { data } = await api.post('/adventurer-s1/admin/draw', { count })
      return data
    },

    async detectReservationAlts(segmentBits = 24) {
      const { data } = await api.get(`/adventurer-s1/admin/reservations/detect-alts?segment_bits=${segmentBits}`)
      return data
    },

    async autoLowerReservationAlts() {
      const { data } = await api.post('/adventurer-s1/admin/reservations/auto-lower-alts')
      return data
    },

    async repairDuplicateBeds() {
      const { data } = await api.post('/adventurer-s1/admin/reservations/repair-duplicate-beds')
      return data
    },

    async detectAdventurerAlts(segmentBits = 24) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/detect-alts?segment_bits=${segmentBits}`)
      return data
    },

    // ── 宿舍区 ──────────────────────────────────────────────────────────────
    async fetchDormitory(force = false, page?: number) {
      const targetPage = Math.max(1, page ?? this.dormitoryPagination?.current_page ?? 1)
      const now = Date.now()
      const cachedPagination = this.dormitoryPagination
      if (
        !force
        && this.dormitory.length > 0
        && cachedPagination?.current_page === targetPage
        && now - _dormitoryFetchAt < DORMITORY_FETCH_MIN_INTERVAL_MS
      ) {
        return {
          code: 200,
          data: {
            rooms: this.dormitory,
            page: cachedPagination.current_page,
            pages: cachedPagination.pages,
            total: cachedPagination.total,
          },
          message: 'ok',
        }
      }
      if (_dormitoryFetchPending && _dormitoryFetchPendingPage === targetPage) {
        return _dormitoryFetchPending
      }

      const initial = this.dormitory.length === 0
      if (initial) this.dormitoryLoading = true
      const requestId = ++_dormitoryFetchRequestId

      const pending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/dormitory', {
            params: { page: targetPage, per_page: DORMITORY_PAGE_SIZE },
          })
          if (data.code === 200 && data.data && requestId === _dormitoryFetchRequestId) {
            this.dormitory = data.data.rooms ?? []
            this.dormitoryPagination = {
              current_page: data.data.page ?? targetPage,
              pages: data.data.pages ?? 1,
              total: data.data.total ?? this.dormitory.length,
            }
            syncDormRecoveryPaused(this.dormitory, [
              this.adventurers,
              this.browseAdventurers,
              ...(this.currentDetail ? [[this.currentDetail]] : []),
            ])
            _dormitoryFetchAt = Date.now()
          }
          return data
        } finally {
          if (initial) this.dormitoryLoading = false
          if (requestId === _dormitoryFetchRequestId) {
            _dormitoryFetchPending = null
            _dormitoryFetchPendingPage = null
          }
        }
      })()

      _dormitoryFetchPending = pending
      _dormitoryFetchPendingPage = targetPage
      return pending
    },

    async moveToEmptyBed(adventurerId: number, roomId: number, bedIndex: number) {
      const { data } = await api.post('/adventurer-s1/dormitory/move', {
        adventurer_id: adventurerId,
        room_id: roomId,
        bed_index: bedIndex,
      })
      if (data.code === 200) trackEvent('adventurer_s1_move_to_empty_bed')
      return data
    },

    async sendSwapRequest(adventurerId: number, targetAdventurerId: number) {
      const { data } = await api.post('/adventurer-s1/dormitory/swap-request', {
        adventurer_id: adventurerId,
        target_adventurer_id: targetAdventurerId,
      })
      if (data.code === 200) trackEvent('adventurer_s1_send_swap_request')
      return data
    },

    async acceptSwapRequest(requestId: number) {
      const { data } = await api.post(`/adventurer-s1/dormitory/swap-request/${requestId}/accept`)
      if (data.code === 200) trackEvent('adventurer_s1_accept_swap_request')
      return data
    },

    async rejectSwapRequest(requestId: number) {
      const { data } = await api.post(`/adventurer-s1/dormitory/swap-request/${requestId}/reject`)
      if (data.code === 200) trackEvent('adventurer_s1_reject_swap_request')
      return data
    },

    async cancelSwapRequest(requestId: number) {
      const { data } = await api.post(`/adventurer-s1/dormitory/swap-request/${requestId}/cancel`)
      if (data.code === 200) trackEvent('adventurer_s1_cancel_swap_request')
      return data
    },

    async fetchSwapRequests() {
      try {
        const { data } = await api.get('/adventurer-s1/dormitory/swap-requests')
        if (data.code === 200 && data.data) {
          this.swapRequestsIncoming = data.data.incoming ?? []
          this.swapRequestsOutgoing = data.data.outgoing ?? []
        }
        return data
      } catch {
        // 静默失败，不影响宿舍主数据
      }
    },

    /** 宿舍 Lv2 开工；确认弹窗在 store 层统一拦截，避免遗漏入口。 */
    async upgradeRoom(roomId: number, roomName?: string) {
      const name = roomName ?? this.dormitory.find(r => r.id === roomId)?.name ?? '该宿舍'
      if (!confirmStartDormRoomUpgrade(name)) {
        return null
      }
      const { data } = await api.post(`/adventurer-s1/dormitory/rooms/${roomId}/upgrade`)
      if (data.code === 200) {
        patchDormRecoveryPausedForRoom([
          this.adventurers,
          this.browseAdventurers,
          ...(this.currentDetail ? [[this.currentDetail]] : []),
        ], roomId, true)
        trackEvent('adventurer_s1_upgrade_room', { room_id: roomId })
      }
      return data
    },

    async donateToDormRoomUpgrade(roomId: number, materialName: string, amount: number) {
      const { data } = await api.post(`/adventurer-s1/dormitory/rooms/${roomId}/donate-upgrade`, {
        material_name: materialName,
        amount,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_donate_dorm_room_upgrade', {
          room_id: roomId,
          material_name: materialName,
        })
      }
      return data
    },

    async helpDormRoomUpgrade(roomId: number) {
      const { data } = await api.post(`/adventurer-s1/dormitory/rooms/${roomId}/help-upgrade`)
      if (data.code === 200) trackEvent('adventurer_s1_help_dorm_room_upgrade', { room_id: roomId })
      return data
    },

    async nameDormRoom(roomId: number, name: string, kind: 'build' | 'upgrade' = 'build') {
      const { data } = await api.post(`/adventurer-s1/dormitory/rooms/${roomId}/name`, { name, kind })
      if (data.code === 200) trackEvent('adventurer_s1_name_dorm_room', { room_id: roomId, kind })
      return data
    },

    // ── 宿舍楼自建（材料 + 申请 + 开工） ───────────────────────────────────────
    async fetchDormitoryMyStatus(force = false) {
      const now = Date.now()
      if (
        !force
        && this.dormitoryMyStatus !== null
        && now - _dormitoryStatusFetchAt < DORMITORY_STATUS_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: this.dormitoryMyStatus, message: 'ok' }
      }
      if (_dormitoryStatusFetchPending) return _dormitoryStatusFetchPending

      const initial = this.dormitoryMyStatus === null
      if (initial) this.dormitoryMyStatusLoading = true

      _dormitoryStatusFetchPending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/dormitory/my-status')
          if (data.code === 200 && data.data) {
            this.dormitoryMyStatus = data.data as DormitoryMyStatus
            this.materialBalances = data.data.my_materials ?? {}
            _dormitoryStatusFetchAt = Date.now()
          }
          return data
        } finally {
          if (initial) this.dormitoryMyStatusLoading = false
          _dormitoryStatusFetchPending = null
        }
      })()

      return _dormitoryStatusFetchPending
    },

    async fetchMyMaterials() {
      const { data } = await api.get('/adventurer-s1/materials/my')
      if (data.code === 200 && data.data) {
        this.materialBalances = data.data.materials ?? {}
        this.materialMeta = data.data.meta ?? {}
      }
      return data
    },

    async requestNewDormitory() {
      const { data } = await api.post('/adventurer-s1/dormitory/request')
      if (data.code === 200) trackEvent('adventurer_s1_request_new_dormitory')
      return data
    },

    async donateToDormitory(
      buildingInstanceId: number,
      adventurerId: number | null,
      materialName: string,
      amount: number,
    ) {
      // adventurerId=null 时让后端自动取 owner 首个非死冒险者（MAX_PER_PLAYER=1 场景）
      const payload: Record<string, any> = {
        material_name: materialName,
        amount,
      }
      if (adventurerId !== null) payload.adventurer_id = adventurerId
      const { data } = await api.post(
        `/adventurer-s1/dormitory/${buildingInstanceId}/donate`,
        payload,
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_donate_to_dormitory', {
          building_instance_id: buildingInstanceId,
          material_name: materialName,
          amount: data.data?.donated_amount ?? amount,
        })
      }
      return data
    },

    async cancelDormitory(buildingInstanceId: number) {
      const { data } = await api.post(`/adventurer-s1/dormitory/${buildingInstanceId}/cancel`)
      if (data.code === 200) trackEvent('adventurer_s1_cancel_dormitory', { building_instance_id: buildingInstanceId })
      return data
    },

    async fetchDormitorySettings(
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await api.get(
        '/adventurer-s1/dormitory/settings',
        { s1RealmContext },
      )
      return data
    },

    async updateDormitorySetting(
      key: string,
      value: any,
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await api.put(
        '/adventurer-s1/dormitory/settings',
        { key, value },
        { s1RealmContext },
      )
      if (data.code === 200) trackEvent('adventurer_s1_update_dormitory_setting', { key, value })
      return data
    },

    async adminCancelMatchmaking(ticketId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/matchmaking/${ticketId}/cancel`,
        undefined,
        S1_SELECTED_REQUEST,
      )
      return data
    },

    // ── 建筑使用流程（伐木 / 采石 / 打工等） ────────────────────────────────────
    async startBuildingUsage(
      buildingId: number,
      adventurerId: number,
      extra?: {
        target_traits?: string[]
        equipment_name?: string
        material_names?: string[]
        item_refs?: Array<{ source: 'carry' | 'warehouse'; id?: number; name: string; quantity?: number }>
        forge_intent?: 'free' | 'reinforce' | 'remedy_limit' | 'merge_trait' | 'reshape' | 'conservative' | 'breakthrough'
        layout_mode?: string
        layout_slots?: Array<{ index: number; position: string; item_ref: { source: 'carry' | 'warehouse'; id: number; name: string; quantity?: number } }>
        cookware_code?: 'stew_pot' | 'frying_pan' | 'steamer' | 'baking_tray' | 'stone_mortar'
        item_ref?: { source: 'carry' | 'warehouse'; id: number; name?: string }
        treatment_gold?: number
        food_code?: string
        skill_code?: string
        operation?: 'learn' | 'advance'
        direction?: string
        target_skill_id?: number
        join_queue?: boolean
        seat_index?: number
        rental_hours?: number
        rental_minutes?: number
        companion_id?: number
        auxiliary_material_ref?: { source: 'carry' | 'warehouse'; id: number; name?: string }
      },
    ) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/use-start`, {
        adventurer_id: adventurerId,
        ...(extra ?? {}),
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_start_building_usage', { building_id: buildingId, adventurer_id: adventurerId })
        if (data.data) this.applyBuildingUsageResult(buildingId, data.data)
      }
      return data
    },

    async cancelBuildingUsageQueue(buildingId: number, adventurerId: number) {
      const { data } = await api.post(
        `/adventurer-s1/buildings/${buildingId}/usage-queue/cancel`,
        { adventurer_id: adventurerId },
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_cancel_building_usage_queue', {
          building_id: buildingId,
          adventurer_id: adventurerId,
        })
        if (data.data) this.applyBuildingUsageResult(buildingId, data.data)
      }
      return data
    },

    async completeBuildingUsage(
      buildingId: number,
      adventurerId: number,
      extra?: {
        equipment_name?: string
        material_names?: string[]
        item_refs?: Array<{ source: 'carry' | 'warehouse'; id?: number; name: string; quantity?: number }>
        layout_mode?: string
        layout_slots?: Array<{ index: number; position: string; item_ref: { source: 'carry' | 'warehouse'; id: number; name: string; quantity?: number } }>
        cookware_code?: 'stew_pot' | 'frying_pan' | 'steamer' | 'baking_tray' | 'stone_mortar'
        companion_id?: number
        auxiliary_material_ref?: { source: 'carry' | 'warehouse'; id: number; name?: string }
      },
    ) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/use-complete`, {
        adventurer_id: adventurerId,
        ...(extra ?? {}),
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_complete_building_usage', { building_id: buildingId, adventurer_id: adventurerId })
        if (data.data) this.applyBuildingUsageResult(buildingId, data.data)
      }
      return data
    },

    // ── 建筑 ────────────────────────────────────────────────────────────────
    async fetchBuildings(force = false) {
      const now = Date.now()
      if (
        !force
        && this.buildings.length > 0
        && now - _buildingsFetchAt < BUILDINGS_FETCH_MIN_INTERVAL_MS
      ) {
        return { code: 200, data: { buildings: this.buildings }, message: 'ok' }
      }
      if (_buildingsFetchPending) return _buildingsFetchPending

      const initial = this.buildings.length === 0
      if (initial) this.buildingsLoading = true

      _buildingsFetchPending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/buildings')
          if (data.code === 200 && data.data) {
            this.buildings = data.data.buildings ?? []
            _buildingsFetchAt = Date.now()
          }
          return data
        } catch (e: any) {
          console.error('[fetchBuildings]', e)
          return e?.response?.data || { code: 500, message: '加载建筑失败' }
        } finally {
          if (initial) this.buildingsLoading = false
          _buildingsFetchPending = null
        }
      })()

      return _buildingsFetchPending
    },

    async fetchBuildingDetail(buildingId: number) {
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}`)
      if (data.code === 200 && data.data) {
        this.currentBuilding = data.data
      }
      return data
    },

    patchBuildingInstance(buildingId: number, patch: Partial<BuildingInstance>) {
      const idx = this.buildings.findIndex(b => b.id === buildingId)
      if (idx >= 0) {
        this.buildings[idx] = { ...this.buildings[idx], ...patch }
      }
      if (this.currentBuilding?.id === buildingId) {
        this.currentBuilding = { ...this.currentBuilding, ...patch }
      }
    },

    /** use-start / use-complete 响应即时合并，避免等 120s 列表节流才看到占用变化 */
    applyBuildingUsageResult(
      buildingId: number,
      result: { building?: Partial<BuildingInstance>; adventurer?: Partial<AdventurerData> },
    ) {
      const building = result.building
      if (building) {
        this.patchBuildingInstance(building.id ?? buildingId, building)
      }
      const adv = result.adventurer
      if (adv?.id) {
        const idx = this.adventurers.findIndex(a => a.id === adv.id)
        if (idx >= 0) {
          this.adventurers[idx] = { ...this.adventurers[idx], ...adv }
        }
      }
    },

    async fetchShopItems(buildingId: number) {
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}/shop-items`)
      return data
    },

    async fetchDiningMenu(buildingId: number) {
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}/dining-menu`)
      return data
    },

    async fetchClueBureauTargets() {
      const { data } = await api.get('/adventurer-s1/clue-bureau/targets')
      return data
    },

    async fetchClueBureauTracePoolPreview() {
      const { data } = await api.get('/adventurer-s1/admin/clue-bureau/trace-pool/preview')
      return data
    },

    async adminSupplementClueBureauTracePool() {
      const { data } = await api.post('/adventurer-s1/admin/clue-bureau/trace-pool/supplement')
      if (data.code === 200) {
        trackEvent('adventurer_s1_admin_supplement_clue_bureau_trace_pool')
      }
      return data
    },

    async adminReleaseClueBureauBossLock(bossId: string) {
      const { data } = await api.post(`/adventurer-s1/admin/clue-bureau/bosses/${encodeURIComponent(bossId)}/release-lock`)
      return data
    },

    async fetchPartyClueProgress(partyId: number, dungeon: string) {
      const { data } = await api.get(`/adventurer-s1/parties/${partyId}/clue-progress`, {
        params: { dungeon },
      })
      return data
    },

    async buyFromShop(buildingId: number, shopItemId: number, adventurerId: number) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/buy`, {
        adventurer_id: adventurerId,
        shop_item_id: shopItemId,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_buy_from_shop', {
          building_id: buildingId,
          shop_item_id: shopItemId,
          adventurer_id: adventurerId,
        })
      }
      return data
    },

    async fetchBuybackItems(buildingId: number) {
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}/buyback-items`)
      return data
    },

    async sellToBuyback(
      buildingId: number,
      adventurerId: number,
      source: 'equipment' | 'warehouse',
      itemId: number,
      quantity?: number,
    ) {
      const payload: Record<string, unknown> = {
        adventurer_id: adventurerId,
        source,
        item_id: itemId,
      }
      if (quantity != null) {
        payload.quantity = quantity
      }
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/sell`, payload)
      if (data.code === 200) {
        trackEvent('adventurer_s1_sell_to_buyback', {
          building_id: buildingId,
          adventurer_id: adventurerId,
          source,
          item_id: itemId,
          quantity,
        })
      }
      return data
    },

    async sellBatchToBuyback(
      buildingId: number,
      adventurerId: number,
      items: BuybackSellRequest[],
    ) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/sell`, {
        adventurer_id: adventurerId,
        items,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_sell_batch_to_buyback', {
          building_id: buildingId,
          adventurer_id: adventurerId,
          item_count: items.length,
        })
      }
      return data
    },

    async fetchGamblingStatus(buildingId: number) {
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}/gambling/status`)
      return data
    },

    async playGambling(
      buildingId: number,
      adventurerId: number,
      betAmount: number,
      choice: 'high' | 'low',
    ) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/gambling/play`, {
        adventurer_id: adventurerId,
        bet_amount: betAmount,
        choice,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_gambling_play', {
          building_id: buildingId,
          adventurer_id: adventurerId,
          bet_amount: betAmount,
          choice,
          won: data.data?.won,
        })
      }
      return data
    },

    async tipGambling(
      buildingId: number,
      adventurerId: number,
      dealerId: number,
      amount: number,
    ) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/gambling/tip`, {
        adventurer_id: adventurerId,
        dealer_id: dealerId,
        amount,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_gambling_tip', {
          building_id: buildingId,
          adventurer_id: adventurerId,
          dealer_id: dealerId,
          amount,
        })
      }
      return data
    },

    async fetchLuckyWheelPool(buildingId: number, adventurerId?: number) {
      const params = adventurerId != null ? { adventurer_id: adventurerId } : undefined
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}/lucky-wheel/pool`, { params })
      return data
    },

    async fetchLuckyWheelHistory(
      buildingId: number,
      adventurerId: number,
      page: number = 1,
      pageSize: number = 10,
    ) {
      const { data } = await api.get(
        `/adventurer-s1/buildings/${buildingId}/lucky-wheel/history`,
        {
          params: {
            adventurer_id: adventurerId,
            page,
            page_size: pageSize,
          },
        },
      )
      return data
    },

    async fetchTombstones(
      buildingId: number,
      opts?: { page?: number; pageSize?: number; mineOnly?: boolean; search?: string },
    ) {
      const params: Record<string, string | number> = {
        page: opts?.page ?? 1,
        page_size: opts?.pageSize ?? 20,
      }
      if (opts?.mineOnly) params.mine = '1'
      const search = opts?.search?.trim()
      if (search) params.search = search
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}/tombstones`, { params })
      return data
    },

    async fetchTombstoneMessages(
      buildingId: number,
      tombstoneId: number,
      opts?: { page?: number; pageSize?: number },
    ) {
      const params: Record<string, number> = {
        page: opts?.page ?? 1,
        page_size: opts?.pageSize ?? 20,
      }
      const { data } = await api.get(
        `/adventurer-s1/buildings/${buildingId}/tombstones/${tombstoneId}/messages`,
        { params },
      )
      return data
    },

    async postTombstoneMessage(buildingId: number, tombstoneId: number, content: string) {
      const { data } = await api.post(
        `/adventurer-s1/buildings/${buildingId}/tombstones/${tombstoneId}/messages`,
        { content },
      )
      return data
    },

    async fetchTombstoneTributes(
      buildingId: number,
      tombstoneId: number,
      opts?: { page?: number; pageSize?: number },
    ) {
      const params: Record<string, number> = {
        page: opts?.page ?? 1,
        page_size: opts?.pageSize ?? 20,
      }
      const { data } = await api.get(
        `/adventurer-s1/buildings/${buildingId}/tombstones/${tombstoneId}/tributes`,
        { params },
      )
      return data
    },

    async postTombstoneTribute(
      buildingId: number,
      tombstoneId: number,
      payload: {
        adventurer_id: number
        flower_code: string
        quantity: number
        message?: string
      },
    ) {
      const { data } = await api.post(
        `/adventurer-s1/buildings/${buildingId}/tombstones/${tombstoneId}/tributes`,
        payload,
      )
      return data
    },

    async fetchFlowerCatalog() {
      const { data } = await api.get('/adventurer-s1/flower-catalog')
      return data
    },

    async spinLuckyWheelBatch(buildingId: number, adventurerId: number, count: number, expectedSpinId: number) {
      const realm = this.realmViewCode
      const userId = this.squadSessionUserId
      const { data } = await api.post('/adventurer-s1/buildings/' + buildingId + '/lucky-wheel/spin-batch', {
        adventurer_id: adventurerId,
        count,
        expected_spin_id: expectedSpinId,
      })
      if (data.code === 200 && realm === this.realmViewCode && userId === this.squadSessionUserId) {
        const result = data.data as LuckyWheelBatchResult
        const wallet = result.pool?.adventurer
        const mine = this.adventurers.find(a => a.id === wallet?.id)
        if (mine && wallet) mine.gold = wallet.gold
        trackEvent('adventurer_s1_lucky_wheel_spin_batch', {
          building_id: buildingId,
          adventurer_id: adventurerId,
          count: result.completed_count,
        })
      }
      return data
    },

    async spinLuckyWheel(buildingId: number, adventurerId: number, payment: 'ticket' = 'ticket') {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/lucky-wheel/spin`, {
        adventurer_id: adventurerId,
        payment,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_lucky_wheel_spin', {
          building_id: buildingId,
          adventurer_id: adventurerId,
          payment,
          item_name: data.data?.item_name,
          rarity: data.data?.rarity,
        })
        const wallet = data.data?.pool?.adventurer ?? data.data?.adventurer
        if (wallet?.id != null) {
          const mine = this.adventurers.find(a => a.id === wallet.id)
          if (mine) {
            mine.gold = wallet.gold
          }
        }
      }
      return data
    },

    async fetchSkillSchoolCatalog(buildingId: number) {
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}/skill-school/catalog`)
      return data
    },

    async retrySkillSchoolTraining(buildingId: number) {
      const { data } = await api.post(
        `/adventurer-s1/buildings/${buildingId}/skill-school/retry`,
        {},
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_retry_skill_school_training', {
          building_id: buildingId,
          operation: data.data?.operation,
        })
      }
      return data
    },

    async cancelSkillSchoolQueue(buildingId: number, adventurerId: number) {
      const { data } = await api.post(
        `/adventurer-s1/buildings/${buildingId}/skill-school/queue/cancel`,
        { adventurer_id: adventurerId },
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_cancel_skill_school_queue', {
          building_id: buildingId,
          adventurer_id: adventurerId,
        })
      }
      return data
    },

    // ── 管理员：幸运大转盘奖池管理 ──

    async fetchAdminLuckyWheelPool(params?: Record<string, unknown>) {
      const { data } = await api.get('/adventurer-s1/admin/lucky-wheel/pool', { params })
      return data
    },

    async addAdminLuckyWheelItem(payload: {
      name: string
      description?: string
      rarity: string
      item_type?: string
      world_unique?: boolean
      text_locked?: boolean
      uses_left?: number
      legendary_position?: 'current' | 'next'
      from_catalog?: boolean
    }) {
      const { data } = await api.post('/adventurer-s1/admin/lucky-wheel/pool', payload)
      return data
    },

    async removeAdminLuckyWheelItem(name: string, rarity: string) {
      const { data } = await api.delete(`/adventurer-s1/admin/lucky-wheel/pool/${encodeURIComponent(name)}?rarity=${encodeURIComponent(rarity)}`)
      return data
    },

    async restoreAdminLuckyWheelItem(name: string, rarity: string) {
      const { data } = await api.post(`/adventurer-s1/admin/lucky-wheel/pool/${encodeURIComponent(name)}/restore?rarity=${encodeURIComponent(rarity)}`)
      return data
    },

    async editAdminLuckyWheelItem(name: string, payload: {
      rarity: string
      new_name?: string
      description?: string
      item_type?: string
      world_unique?: boolean
      text_locked?: boolean
      uses_left?: number
    }) {
      const { data } = await api.put(`/adventurer-s1/admin/lucky-wheel/pool/${encodeURIComponent(name)}`, payload)
      return data
    },

    // ── 管理员：物品图鉴 CRUD ──

    async adminCreateCatalogItem(payload: {
      name: string
      type?: string
      description?: string
      rarity?: string
      world_unique?: boolean
      is_starter?: boolean
      uses_left?: number
      use_condition?: Record<string, unknown> | null
    }) {
      const { data } = await api.post('/adventurer-s1/admin/items/catalog', payload)
      return data
    },

    async adminUpdateCatalogItem(id: number, payload: {
      name?: string
      type?: string
      description?: string
      rarity?: string
      world_unique?: boolean
      is_starter?: boolean
      uses_left?: number
      use_condition?: Record<string, unknown> | null
    }) {
      const { data } = await api.put(`/adventurer-s1/admin/items/catalog/${id}`, payload)
      return data
    },

    async adminGetDungeonList(): Promise<string[]> {
      const { data } = await api.get('/adventurer-s1/admin/items/dungeon-list')
      return data?.data ?? []
    },

    async adminDeleteCatalogItem(id: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/items/catalog/${id}`)
      return data
    },

    async fetchBuildingContributors(buildingId: number): Promise<BuildingContributors | null> {
      const { data } = await api.get(`/adventurer-s1/buildings/${buildingId}/contributors`)
      if (data.code === 200 && data.data) {
        return data.data as BuildingContributors
      }
      return null
    },

    async startBuilding(buildingId: number) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/start`)
      if (data.code === 200) trackEvent('adventurer_s1_start_building', { building_id: buildingId })
      return data
    },

    async helpBuilding(buildingId: number, adventurerId: number) {
      const { data } = await api.post(`/adventurer-s1/buildings/${buildingId}/help`, {
        adventurer_id: adventurerId,
      })
      if (data.code === 200) {
        trackEvent('adventurer_s1_help_building', { building_id: buildingId, adventurer_id: adventurerId })
        if (data.data?.building) {
          this.patchBuildingInstance(buildingId, data.data.building)
        }
      }
      return data
    },

    async recallFromBuilding(adventurerId: number) {
      const { data } = await api.post(`/adventurer-s1/buildings/recall/${adventurerId}`)
      if (data.code === 200) {
        trackEvent('adventurer_s1_recall_from_building', { adventurer_id: adventurerId })
        const building = data.data?.building
        if (building?.id) {
          this.patchBuildingInstance(building.id, building)
        }
      }
      return data
    },

    // ── 管理员：建筑模板管理 ─────────────────────────────────────────────────
    async fetchBuildingTemplates() {
      this.buildingTemplatesLoading = true
      try {
        const { data } = await api.get('/adventurer-s1/admin/building-templates')
        if (data.code === 200 && data.data) {
          this.buildingTemplates = data.data.templates ?? []
        }
        return data
      } finally {
        this.buildingTemplatesLoading = false
      }
    },

    async createBuildingTemplate(payload: BuildingTemplateFormData) {
      const { data } = await api.post('/adventurer-s1/admin/building-templates', payload)
      return data
    },

    async updateBuildingTemplate(templateId: number, payload: Partial<BuildingTemplateFormData>) {
      const { data } = await api.put(`/adventurer-s1/admin/building-templates/${templateId}`, payload)
      return data
    },

    async deleteBuildingTemplate(templateId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/building-templates/${templateId}`)
      return data
    },

    async resetBuilding(instanceId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/building-instances/${instanceId}/reset`)
      return data
    },

    async completeBuilding(instanceId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/building-instances/${instanceId}/complete`)
      if (data.code === 200) trackEvent('adventurer_s1_admin_complete_building', { instance_id: instanceId })
      return data
    },

    async fillBuildingMaterials(instanceId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/building-instances/${instanceId}/fill-materials`)
      if (data.code === 200) trackEvent('adventurer_s1_admin_fill_materials', { instance_id: instanceId })
      return data
    },

    async createBuildingInstance(templateId: number) {
      const { data } = await api.post('/adventurer-s1/admin/building-instances', { template_id: templateId })
      return data
    },

    async deleteBuildingInstance(instanceId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/building-instances/${instanceId}`)
      return data
    },

    async renameBuildingInstance(instanceId: number, customName: string) {
      const { data } = await api.post(`/adventurer-s1/admin/building-instances/${instanceId}/rename`, { custom_name: customName })
      return data
    },

    async toggleBuildingDisabled(instanceId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/building-instances/${instanceId}/toggle-disable`)
      return data
    },

    async fetchAllBuildingInstances() {
      const { data } = await api.get('/adventurer-s1/admin/building-instances/all')
      return data
    },

    // ── 货币 ────────────────────────────────────────────────────────────
    async fetchCurrencyBalances(advId: number) {
      const { data } = await api.get(`/adventurer-s1/${advId}/currencies`)
      if (data.code === 200) {
        this.currencyBalances = data.data ?? {}
      }
      return data
    },

    async fetchCurrencyLogs(advId: number, currencyCode: string, limit: number = 50) {
      const { data } = await api.get(`/adventurer-s1/${advId}/currencies/${currencyCode}/logs?limit=${limit}`)
      if (data.code === 200) {
        this.currencyLogs = data.data ?? []
      }
      return data
    },

    async fetchCurrencyTypes() {
      const { data } = await api.get('/adventurer-s1/currency-types')
      if (data.code === 200) {
        this.currencyTypes = data.data ?? []
      }
      return data
    },

    // ── 我的最近动态 ──────────────────────────────────────────────────────
    /** reset=true 拉最新一页；reset=false 用 next_before 游标加载更早的记录 */
    async fetchMyActivity(reset: boolean = true) {
      if (this.myActivityLoading) return
      const requestedFilter = this.myActivityFilter
      if (reset && requestedFilter !== this.myActivityLoadedFilter) {
        this.myActivity = []
        this.myActivityHasMore = false
        this.myActivityNextBefore = null
      }
      this.myActivityLoading = true
      try {
        const q = new URLSearchParams({ limit: '20' })
        if (requestedFilter !== 'all') q.set('filter', requestedFilter)
        if (!reset && this.myActivityNextBefore) q.set('before', this.myActivityNextBefore)
        const { data } = await api.get(`/adventurer-s1/my/activity?${q.toString()}`)
        if (data.code === 200 && data.data && requestedFilter === this.myActivityFilter) {
          const entries = (data.data.entries ?? []) as ActivityEntry[]
          this.myActivity = reset ? entries : [...this.myActivity, ...entries]
          this.myActivityHasMore = !!data.data.has_more
          this.myActivityNextBefore = data.data.next_before ?? null
          this.myActivityLoadedFilter = requestedFilter
        }
        return data
      } finally {
        this.myActivityLoading = false
      }
    },

    async undoBuildingActivity(logId: number, itemIndex?: number) {
      const body = itemIndex == null ? {} : { item_index: itemIndex }
      const { data } = await api.post(`/adventurer-s1/my/activity/${logId}/undo`, body)
      return data
    },

    // ── 任务栏 ────────────────────────────────────────────────────────────
    async fetchTasks(adventurerId?: number) {
      const qs = adventurerId != null ? `?adventurer_id=${adventurerId}` : ''
      const { data } = await api.get(`/adventurer-s1/tasks${qs}`)
      return data
    },

    async acceptTask(taskId: string, adventurerId: number, buildingInstanceId: number) {
      const { data } = await api.post('/adventurer-s1/tasks/accept', {
        task_id: taskId,
        adventurer_id: adventurerId,
        building_instance_id: buildingInstanceId,
      })
      if (data.code === 200) trackEvent('adventurer_s1_accept_task', { task_id: taskId })
      return data
    },

    async completeTask(taskInstanceId: number) {
      const { data } = await api.post('/adventurer-s1/tasks/complete', {
        task_instance_id: taskInstanceId,
      })
      if (data.code === 200) trackEvent('adventurer_s1_complete_task', { task_instance_id: taskInstanceId })
      return data
    },

    async abandonTask(taskInstanceId: number) {
      const { data } = await api.post('/adventurer-s1/tasks/abandon', {
        task_instance_id: taskInstanceId,
      })
      if (data.code === 200) trackEvent('adventurer_s1_abandon_task', { task_instance_id: taskInstanceId })
      return data
    },

    async fetchActiveTask(adventurerId: number) {
      const { data } = await api.get(`/adventurer-s1/tasks/active?adventurer_id=${adventurerId}`)
      return data
    },

    async adminListTasks() {
      const { data } = await api.get('/adventurer-s1/admin/tasks', S1_MEMBERSHIP_REQUEST)
      return data
    },

    async adminUpdateTask(taskId: string, payload: { enabled?: boolean; overrides?: Record<string, unknown> }) {
      const { data } = await api.put(
        `/adventurer-s1/admin/tasks/${encodeURIComponent(taskId)}`,
        payload,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_update_task', { task_id: taskId })
      return data
    },

    async adminReenableTask(taskId: string, payload?: { max_completions?: number }) {
      const { data } = await api.post(
        `/adventurer-s1/admin/tasks/${encodeURIComponent(taskId)}/reenable`,
        payload ?? {},
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_reenable_task', { task_id: taskId })
      return data
    },

    async refreshHomeBadges(force = false): Promise<void> {
      const now = Date.now()
      if (
        !force
        && _initFetchAt > 0
        && now - _initFetchAt < INIT_FETCH_MIN_INTERVAL_MS
      ) {
        return
      }
      if (
        !force
        && _homeBadgesFetchAt > 0
        && now - _homeBadgesFetchAt < HOME_BADGES_FETCH_MIN_INTERVAL_MS
      ) {
        return
      }
      if (_homeBadgesFetchPending) return _homeBadgesFetchPending

      this.taskAcceptableCountLoading = true
      this.secretAuctionOpenCountLoading = true

      _homeBadgesFetchPending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/home-badges')
          if (data.code === 200 && data.data) {
            const d = data.data
            this.taskAcceptableCount = d.task_acceptable_count ?? 0
            this.taskAcceptableIds = d.task_acceptable_ids ?? []
            this.secretAuctionOpenCount = d.auction_open_count ?? 0
            _homeBadgesFetchAt = Date.now()
            _taskBadgesFetchAt = Date.now()
          }
        } catch (e) {
          console.error('[refreshHomeBadges]', e)
        } finally {
          this.taskAcceptableCountLoading = false
          this.secretAuctionOpenCountLoading = false
          _homeBadgesFetchPending = null
        }
      })()

      return _homeBadgesFetchPending
    },

    async refreshTaskBadgesOnly(force = false): Promise<void> {
      const now = Date.now()
      if (
        !force
        && _initFetchAt > 0
        && now - _initFetchAt < INIT_FETCH_MIN_INTERVAL_MS
      ) {
        return
      }
      if (
        !force
        && _taskBadgesFetchAt > 0
        && now - _taskBadgesFetchAt < TASK_BADGES_FETCH_MIN_INTERVAL_MS
      ) {
        return
      }
      if (_taskBadgesFetchPending) return _taskBadgesFetchPending

      this.taskAcceptableCountLoading = true
      _taskBadgesFetchPending = (async () => {
        try {
          const { data } = await api.get('/adventurer-s1/home-badges/tasks')
          if (data.code === 200 && data.data) {
            const d = data.data
            this.taskAcceptableCount = d.task_acceptable_count ?? 0
            this.taskAcceptableIds = d.task_acceptable_ids ?? []
            _taskBadgesFetchAt = Date.now()
          }
        } catch (e) {
          console.error('[refreshTaskBadgesOnly]', e)
        } finally {
          this.taskAcceptableCountLoading = false
          _taskBadgesFetchPending = null
        }
      })()

      return _taskBadgesFetchPending
    },

    async refreshTaskAcceptableCount(): Promise<number> {
      const previous = this.taskAcceptableCount ?? 0
      try {
        await this.refreshTaskBadgesOnly(true)
        return this.taskAcceptableCount ?? previous
      } catch (e) {
        console.error('[refreshTaskAcceptableCount]', e)
        return previous
      }
    },

    async refreshSecretAuctionOpenCount(force = false) {
      const now = Date.now()
      if (
        !force
        && _secretAuctionSummaryFetchAt > 0
        && now - _secretAuctionSummaryFetchAt < SECRET_AUCTION_SUMMARY_FETCH_MIN_INTERVAL_MS
      ) {
        return
      }
      try {
        const { data } = await api.get('/adventurer-s1/secret-auction/summary')
        if (data.code === 200 && data.data) {
          const summary = data.data
          this.secretAuctionOpenCount = summary.available
            ? (summary.open_count ?? 0)
            : 0
          _secretAuctionSummaryFetchAt = Date.now()
        }
      } catch (e) {
        console.error('[refreshSecretAuctionOpenCount]', e)
      }
    },

    // ── 管理员：探险管理 ──────────────────────────────────────────────
    async adminFormParties(
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await api.post(
        '/adventurer-s1/admin/expedition/form-parties',
        undefined,
        { s1RealmContext },
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_form_parties')
      return data
    },

    async adminDispatchDeparted(
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await api.post(
        '/adventurer-s1/admin/expedition/dispatch',
        undefined,
        { s1RealmContext },
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_dispatch')
      return data
    },

    async adminStepExpeditionLog(logId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/expedition/logs/${logId}/step`,
        undefined,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_step_expedition_log', { log_id: logId })
      return data
    },

    async adminForceCompleteExpeditionLog(logId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/expedition/logs/${logId}/force-complete`,
        undefined,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_force_complete_expedition_log', { log_id: logId })
      return data
    },

    async adminPreviewExpeditionSettlementConflictRepair(logId: number) {
      const { data } = await api.get(
        `/adventurer-s1/admin/expedition/logs/${logId}/settlement-conflict-repair/preview`,
      )
      return data
    },

    async adminExecuteExpeditionSettlementConflictRepair(
      logId: number,
      previewToken: string,
    ) {
      const { data } = await api.post(
        `/adventurer-s1/admin/expedition/logs/${logId}/settlement-conflict-repair/execute`,
        { preview_token: previewToken },
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_admin_execute_settlement_conflict_repair', {
          log_id: logId,
          status: data.data?.status,
        })
      }
      return data
    },

    async adminRestoreExpeditionSettlementConflictRepair(
      logId: number,
      previewToken: string,
    ) {
      const { data } = await api.post(
        `/adventurer-s1/admin/expedition/logs/${logId}/settlement-conflict-repair/restore`,
        { preview_token: previewToken },
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_admin_restore_settlement_conflict_repair', {
          log_id: logId,
        })
      }
      return data
    },

    async adminRegenerateExpeditionLog(logId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/expedition/logs/${logId}/regenerate`,
        undefined,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_regenerate_expedition_log', { log_id: logId })
      return data
    },

    async adminEditLogText(logId: number, payload: {
      result_summary?: string
      full_log_patches?: { path: string; value: unknown }[]
    }) {
      const { data } = await api.put(
        `/adventurer-s1/admin/expedition/logs/${logId}/edit-text`,
        payload,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_edit_log_text', { log_id: logId })
      return data
    },

    async adminRetryGeneratingExpeditionLog(logId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/expedition/logs/${logId}/retry-generating`,
        undefined,
        S1_MEMBERSHIP_REQUEST,
      )
      if (data.code === 200) trackEvent('adventurer_s1_admin_retry_generating_expedition_log', { log_id: logId })
      return data
    },

    async adminGetExpeditionConfig(
      s1RealmContext: S1RealmContext = 'membership',
    ) {
      const { data } = await api.get(
        '/adventurer-s1/admin/expedition/config',
        { s1RealmContext },
      )
      return data
    },

    async adminUpdateExpeditionConfig(payload: Record<string, number>) {
      const { data } = await api.post('/adventurer-s1/admin/expedition/config', payload)
      if (data.code === 200) trackEvent('adventurer_s1_admin_update_config')
      return data
    },

    async adminFetchAdventurers(params: { page?: number; per_page?: number; status?: string; search?: string } = {}) {
      const q = new URLSearchParams()
      if (params.page) q.set('page', String(params.page))
      if (params.per_page) q.set('per_page', String(params.per_page))
      if (params.status) q.set('status', params.status)
      if (params.search) q.set('search', params.search)
      const { data } = await api.get(`/adventurer-s1/admin/adventurers?${q.toString()}`)
      return data
    },

    async adminSwitchAllPlayersToManual(realm: S1AdminRealmTarget) {
      const { data } = await api.post<{ code: number; message: string; data: AdminManualMatchingResult | null }>(
        '/adventurer-s1/admin/adventurers/party-preference/manual',
        { confirmed: true },
        { ...S1_SELECTED_REQUEST, s1AdminRealmTarget: realm },
      )
      if (data.code === 200) {
        trackEvent('adventurer_s1_admin_switch_all_players_to_manual', {
          realm_code: data.data?.realm_code,
          updated_count: data.data?.updated_count,
        })
      }
      return data
    },

    async adminFetchInactiveAdventurers(params: { page?: number; per_page?: number } = {}) {
      const q = new URLSearchParams()
      if (params.page) q.set('page', String(params.page))
      if (params.per_page) q.set('per_page', String(params.per_page))
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/inactive?${q.toString()}`)
      return data
    },

    async adminArchiveAdventurer(advId: number, reason?: string) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/archive`, { reason })
      if (data.code === 200) trackEvent('adventurer_s1_admin_archive_adventurer', { adv_id: advId })
      return data
    },

    async adminUnarchiveAdventurer(advId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/unarchive`)
      if (data.code === 200) trackEvent('adventurer_s1_admin_unarchive_adventurer', { adv_id: advId })
      return data
    },

    async adminForceEvictBuilding(advId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/force-evict-building`)
      if (data.code === 200) trackEvent('adventurer_s1_admin_force_evict_building', { adv_id: advId })
      return data
    },

    async adminGetAdventurer(advId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/${advId}`)
      return data
    },

    async adminGetFateResetState(advId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/${advId}/fate-reset`)
      return data
    },

    async adminRequestFateReset(advId: number, reason: string) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/fate-reset`, { reason })
      if (data.code === 200) trackEvent('adventurer_s1_admin_request_fate_reset', { adv_id: advId })
      return data
    },

    async adminUpdateAdventurer(advId: number, payload: Record<string, any>) {
      const { data } = await api.put(`/adventurer-s1/admin/adventurers/${advId}`, payload)
      if (data.code === 200) trackEvent('adventurer_s1_admin_update_adventurer', { adv_id: advId })
      return data
    },

    // ── 管理员：特质 CRUD ─────────────────────────────────────────────
    async adminListTraits(advId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/${advId}/traits`)
      return data
    },

    async adminAddTrait(advId: number, name: string, description: string) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/traits`, { name, description })
      return data
    },

    async adminUpdateTrait(traitId: number, payload: { name?: string; description?: string; is_archived?: boolean }) {
      const { data } = await api.put(`/adventurer-s1/admin/traits/${traitId}`, payload)
      return data
    },

    async adminDeleteTrait(traitId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/traits/${traitId}`)
      return data
    },

    // ── 管理员：技能 CRUD ─────────────────────────────────────────────
    async adminListSkills(advId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/${advId}/skills`)
      return data
    },

    async adminAddSkill(advId: number, name: string, description: string, equipped = true) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/skills`, { name, description, equipped })
      return data
    },

    async adminUpdateSkill(skillId: number, payload: { name?: string; description?: string; equipped?: boolean }) {
      const { data } = await api.put(`/adventurer-s1/admin/skills/${skillId}`, payload)
      return data
    },

    async adminDeleteSkill(skillId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/skills/${skillId}`)
      return data
    },

    // ── 管理员：装备（背包）CRUD ────────────────────────────────────────
    async adminListEquipment(advId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/${advId}/equipment`)
      return data
    },

    async adminAddEquipment(advId: number, payload: Record<string, any>) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/equipment`, payload)
      return data
    },

    async adminUpdateEquipment(eqId: number, payload: Record<string, any>) {
      const { data } = await api.put(`/adventurer-s1/admin/equipment/${eqId}`, payload)
      return data
    },

    async adminDeleteEquipment(eqId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/equipment/${eqId}`)
      return data
    },

    // ── 管理员：仓库物品 CRUD ───────────────────────────────────────────
    async adminListWarehouse(advId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/${advId}/warehouse`)
      return data
    },

    async adminAddWarehouse(advId: number, payload: Record<string, any>) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/warehouse`, payload)
      return data
    },

    async adminUpdateWarehouse(whId: number, payload: Record<string, any>) {
      const { data } = await api.put(`/adventurer-s1/admin/warehouse/${whId}`, payload)
      return data
    },

    async adminDeleteWarehouse(whId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/warehouse/${whId}`)
      return data
    },

    // ── 管理员：隐藏剧情线 ───────────────────────────────────────────────
    async adminListStoryThreads(advId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/${advId}/story-threads`)
      return data
    },

    async adminStoryThreadStats() {
      const { data } = await api.get('/adventurer-s1/admin/story-threads/stats')
      return data
    },

    async adminSearchStoryThreads(params?: {
      search?: string
      status?: string
      adventurer_id?: number
      page?: number
      per_page?: number
    }) {
      const q = new URLSearchParams()
      if (params?.search) q.set('search', params.search)
      if (params?.status) q.set('status', params.status)
      if (params?.adventurer_id != null) q.set('adventurer_id', String(params.adventurer_id))
      if (params?.page) q.set('page', String(params.page))
      if (params?.per_page) q.set('per_page', String(params.per_page))
      const { data } = await api.get(`/adventurer-s1/admin/story-threads?${q.toString()}`)
      return data
    },

    async adminCreateStoryThread(payload: {
      adventurer_id: number
      title: string
      summary: string
      tags?: string[]
      status?: string
      weight?: number
    }) {
      const { data } = await api.post('/adventurer-s1/admin/story-threads', payload)
      return data
    },

    async adminUpdateStoryThread(threadId: number, payload: {
      title?: string
      summary?: string
      tags?: string[]
      status?: string
      weight?: number
    }) {
      const { data } = await api.put(`/adventurer-s1/admin/story-threads/${threadId}`, payload)
      return data
    },

    async adminDeleteStoryThread(threadId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/story-threads/${threadId}`)
      return data
    },

    // ── 管理员：物品图鉴搜索 ────────────────────────────────────────────
    async adminSearchItemCatalog(params?: { search?: string; type?: string; rarity?: string }) {
      const q = new URLSearchParams()
      if (params?.search) q.set('search', params.search)
      if (params?.type) q.set('type', params.type)
      if (params?.rarity) q.set('rarity', params.rarity)
      const { data } = await api.get(`/adventurer-s1/admin/items/catalog?${q.toString()}`)
      return data
    },

    async adminSearchItemHolders(params: {
      search: string
      match?: 'contains' | 'exact'
      page?: number
      per_page?: number
    }) {
      const q = new URLSearchParams()
      q.set('search', params.search)
      if (params.match) q.set('match', params.match)
      if (params.page) q.set('page', String(params.page))
      if (params.per_page) q.set('per_page', String(params.per_page))
      const { data } = await api.get(`/adventurer-s1/admin/items/holders?${q.toString()}`)
      return data
    },

    // ── 数据异常申诉 ─────────────────────────────────────────────────
    async fetchTextCorrectionPreview(advId: number) {
      const { data } = await api.get(`/adventurer-s1/${advId}/text-correction/preview`, {
        timeout: 30000,
      })
      return data
    },

    async applyTextCorrection(
      advId: number,
      selections: {
        occurrence_id: string
        replacements: { token: string; name: string }[]
      }[],
    ) {
      const { data } = await api.post(`/adventurer-s1/${advId}/text-correction/apply`, { selections })
      return data
    },

    async restoreTextCorrection(advId: number) {
      const { data } = await api.post(`/adventurer-s1/${advId}/text-correction/restore`)
      return data
    },

    async dismissTextCorrectionOccurrence(advId: number, occurrenceId: string) {
      const { data } = await api.post(`/adventurer-s1/${advId}/text-correction/dismiss`, {
        occurrence_id: occurrenceId,
      })
      return data
    },

    async fetchDataAppealTargets() {
      const { data } = await api.get('/adventurer-s1/data-appeals/targets')
      return data
    },

    async fetchTavernReliefPreview(clientRequestId?: string, appealId?: number | null) {
      const { data } = await api.get('/adventurer-s1/tavern-relief', {
        headers: clientRequestId ? { 'Idempotency-Key': clientRequestId } : undefined,
        params: appealId ? { appeal_id: appealId } : undefined,
      })
      return data
    },

    async claimTavernRelief(clientRequestId: string) {
      const { data } = await api.post('/adventurer-s1/tavern-relief', undefined, {
        headers: { 'Idempotency-Key': clientRequestId },
      })
      return data
    },

    async fetchAppealProjects() {
      const { data } = await api.get('/adventurer-s1/appeal-projects')
      return data
    },

    async previewEquipmentIntegrityAppealProject() {
      const { data } = await api.get(
        '/adventurer-s1/appeal-projects/equipment-integrity/preview',
      )
      return data
    },

    async executeEquipmentIntegrityAppealProject(previewToken: string) {
      const { data } = await api.post(
        '/adventurer-s1/appeal-projects/equipment-integrity/execute',
        { preview_token: previewToken },
      )
      return data
    },

    async restoreEquipmentIntegrityAppealProject() {
      const { data } = await api.post(
        '/adventurer-s1/appeal-projects/equipment-integrity/restore',
      )
      return data
    },

    async claimPetEvolutionAppealProject() {
      const { data } = await api.post('/adventurer-s1/appeal-projects/pet-evolution-refund')
      return data
    },

    async repurchaseBuybackAppealProject(token: string) {
      const { data } = await api.post('/adventurer-s1/appeal-projects/buyback-repurchase', {
        token,
      })
      return data
    },

    async submitDataAppeal(payload: {
      targets: { type: string; id: number }[]
      player_note: string
      appeal_category?: string
      expedition_evidence?: { expedition_log_id: number; event_index?: number | null }
    }) {
      const { data } = await api.post('/adventurer-s1/data-appeals', payload)
      return data
    },

    async fetchMyDataAppeals(limit = 20) {
      const { data } = await api.get(`/adventurer-s1/data-appeals/mine?limit=${limit}`)
      return data
    },

    async submitDataAppealAiReview(appealId: number, requestText: string) {
      const { data } = await api.post(`/adventurer-s1/data-appeals/${appealId}/ai-review`, {
        request: requestText,
      })
      return data
    },

    async adminListDataAppeals(params?: { status?: string; search?: string; page?: number; per_page?: number }) {
      const q = new URLSearchParams()
      if (params?.status) q.set('status', params.status)
      if (params?.search?.trim()) q.set('search', params.search.trim())
      if (params?.page) q.set('page', String(params.page))
      if (params?.per_page) q.set('per_page', String(params.per_page))
      const { data } = await api.get(`/adventurer-s1/admin/data-appeals?${q.toString()}`)
      return data
    },

    async adminGetDataAppeal(appealId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/data-appeals/${appealId}`)
      return data
    },

    async adminApproveDataAppeal(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/data-appeals/${appealId}/approve`)
      return data
    },

    async adminRejectDataAppeal(appealId: number, rejectReason?: string) {
      const { data } = await api.post(`/adventurer-s1/admin/data-appeals/${appealId}/reject`, {
        reject_reason: rejectReason || '',
      })
      return data
    },

    async continueDataAppeal(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/data-appeals/${appealId}/continue`)
      return data
    },

    async cancelDataAppeal(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/data-appeals/${appealId}/cancel`)
      return data
    },

    async adminCancelDataAppeal(appealId: number, reason?: string) {
      const { data } = await api.post(`/adventurer-s1/admin/data-appeals/${appealId}/cancel`, {
        reason: reason || '',
      })
      return data
    },

    async adminRefundRarityReviewGuardFailure(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/data-appeals/${appealId}/refund-rarity-review`)
      return data
    },

    async adminPreviewRarityReviewGuardFailures(limit = 50) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/rarity-review-guard-failures/preview', {
        limit,
      })
      return data
    },

    async adminBulkRefundRarityReviewGuardFailures(payload?: {
      appeal_ids?: number[]
      limit?: number
      notify_player?: boolean
    }) {
      const { data } = await api.post(
        '/adventurer-s1/admin/data-appeals/rarity-review-guard-failures/refund',
        payload || {},
      )
      return data
    },

    async adminRetryDataAppealAi(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/data-appeals/${appealId}/retry-ai`)
      return data
    },

    async adminRevertDataAppeal(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/data-appeals/${appealId}/revert`)
      return data
    },

    async adminPreviewCrossCharacterDataAppeals(limit = 100) {
      const { data } = await api.post(
        '/adventurer-s1/admin/data-appeals/cross-character/preview',
        { limit },
        { timeout: 120000 },
      )
      return data
    },

    async adminFetchTitles() {
      const { data } = await api.get('/adventurer-s1/admin/titles')
      return data
    },

    async adminCreateTitle(payload: {
      code: string
      name: string
      description: string
      frame_key: string
    }) {
      const { data } = await api.post('/adventurer-s1/admin/titles', payload)
      return data
    },

    async adminUpdateTitle(titleId: number, payload: Record<string, unknown>) {
      const { data } = await api.put(`/adventurer-s1/admin/titles/${titleId}`, payload)
      return data
    },

    async adminDeleteTitle(titleId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/titles/${titleId}`)
      return data
    },

    async adminFetchAdventurerTitles(advId: number) {
      const { data } = await api.get(`/adventurer-s1/admin/adventurers/${advId}/titles`)
      return data
    },

    async adminGrantAdventurerTitle(advId: number, titleId: number) {
      const { data } = await api.post(`/adventurer-s1/admin/adventurers/${advId}/titles/${titleId}`, {})
      return data
    },

    async adminRevokeAdventurerTitle(advId: number, titleId: number) {
      const { data } = await api.delete(`/adventurer-s1/admin/adventurers/${advId}/titles/${titleId}`)
      return data
    },

    async adminExecuteCrossCharacterDataAppealReverts(appealIds: number[]) {
      const { data } = await api.post(
        '/adventurer-s1/admin/data-appeals/cross-character/execute',
        { appeal_ids: appealIds },
        { timeout: 120000 },
      )
      return data
    },

    async adminSendDataAppealRevertOffer(appealId: number, adminNote?: string) {
      const { data } = await api.post(
        `/adventurer-s1/admin/data-appeals/${appealId}/send-revert-offer`,
        { admin_note: adminNote || '' },
      )
      return data
    },

    async adminPreviewDataAppealRevertOffers(payload?: {
      appeal_ids?: number[]
      limit?: number
      only_likely_mistake?: boolean
    }) {
      const { data } = await api.post(
        '/adventurer-s1/admin/data-appeals/send-revert-offers/preview',
        payload || {},
      )
      return data
    },

    async adminBulkSendDataAppealRevertOffers(payload?: {
      appeal_ids?: number[]
      limit?: number
      only_likely_mistake?: boolean
      admin_note?: string
    }) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/send-revert-offers', payload || {})
      return data
    },

    async adminPreviewMisclassifiedWithdrawals(limit = 50) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/misclassified-withdrawals/preview', {
        limit,
      })
      return data
    },

    async adminExecuteMisclassifiedWithdrawals(payload?: {
      appeal_ids?: number[]
      limit?: number
      notify_player?: boolean
    }) {
      const { data } = await api.post(
        '/adventurer-s1/admin/data-appeals/misclassified-withdrawals/execute',
        payload || {},
      )
      return data
    },

    async adminFixMisclassifiedWithdrawal(appealId: number, notifyPlayer = true) {
      const { data } = await api.post(
        `/adventurer-s1/admin/data-appeals/${appealId}/fix-misclassified-withdrawal`,
        { notify_player: notifyPlayer },
      )
      return data
    },

    async adminPreviewStuckRevertRequests(limit = 50) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/stuck-revert-requests/preview', {
        limit,
      })
      return data
    },

    async adminRetryStuckRevertRequests(appealIds: number[]) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/stuck-revert-requests/retry', {
        appeal_ids: appealIds,
      })
      return data
    },

    async adminRetryRevertRequest(appealId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/data-appeals/${appealId}/retry-revert-request`,
      )
      return data
    },

    async adminResetRevertRequest(appealId: number) {
      const { data } = await api.post(
        `/adventurer-s1/admin/data-appeals/${appealId}/reset-revert-request`,
      )
      return data
    },

    async fetchPendingDataAppealRevertOffers(limit = 20) {
      const { data } = await api.get(`/adventurer-s1/data-appeals/pending-revert-offers?limit=${limit}`)
      return data
    },

    async fetchDataAppealRevertOfferStatus(appealId: number) {
      const { data } = await api.get(`/adventurer-s1/data-appeals/${appealId}/revert-offer-status`)
      return data
    },

    async acceptDataAppealRevertOffer(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/data-appeals/${appealId}/accept-revert-offer`)
      return data
    },

    async declineDataAppealRevertOffer(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/data-appeals/${appealId}/decline-revert-offer`)
      return data
    },

    async requestDataAppealRevert(appealId: number, playerNote: string) {
      const { data } = await api.post(`/adventurer-s1/data-appeals/${appealId}/request-revert`, {
        player_note: playerNote,
      })
      return data
    },

    async confirmDataAppealRevertRequest(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/data-appeals/${appealId}/confirm-revert-request`)
      return data
    },

    async declineDataAppealRevertRequest(appealId: number) {
      const { data } = await api.post(`/adventurer-s1/data-appeals/${appealId}/decline-revert-request`)
      return data
    },

    async adminPreviewDataAppealBugReclaims(limit = 50) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/bug-reclaims/preview', { limit }, {
        timeout: 120000,
      })
      return data
    },

    async adminPreviewClassScrollReclaims(limit = 20) {
      const { data } = await api.post('/adventurer-s1/admin/class-scroll-reclaims/preview', { limit }, {
        timeout: 120000,
      })
      return data
    },

    async adminExecuteClassScrollReclaims(limit = 20, previewToken = '') {
      const { data } = await api.post('/adventurer-s1/admin/class-scroll-reclaims/execute', {
        limit,
        preview_token: previewToken,
      }, {
        timeout: 120000,
      })
      return data
    },

    async adminExecuteDataAppealBugReclaims(limit = 50, note = '', previewToken = '') {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/bug-reclaims/execute', {
        limit,
        note,
        preview_token: previewToken,
      }, {
        timeout: 120000,
      })
      return data
    },

    async adminDismissDataAppealBugReclaim(payload: {
      issue_key: string
      note?: string
      item?: Record<string, unknown>
    }) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/bug-reclaims/dismiss', payload)
      return data
    },

    async adminAdjustDataAppealBugReclaimQuantity(payload: {
      issue_key: string
      illegal_quantity: number
      note?: string
      item?: Record<string, unknown>
    }) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/bug-reclaims/adjust-quantity', payload)
      return data
    },

    async adminAdjustDataAppealBugReclaimText(payload: {
      issue_key: string
      corrected_name: string
      corrected_desc?: string
      corrected_quantity?: number | null
      note?: string
      item?: Record<string, unknown>
      revert?: boolean
    }) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/bug-reclaims/adjust-text', payload)
      return data
    },

    async adminListAppealItemChangeCatalog(params?: { limit?: number; sort?: string }) {
      const q = new URLSearchParams()
      if (params?.limit) q.set('limit', String(params.limit))
      if (params?.sort) q.set('sort', params.sort)
      const qs = q.toString()
      const { data } = await api.get(
        `/adventurer-s1/admin/data-appeals/bug-reclaims/item-change-catalog${qs ? '?' + qs : ''}`,
      )
      return data
    },

    async adminListDataAppealBugReclaims(params?: { page?: number; per_page?: number }) {
      const q = new URLSearchParams()
      if (params?.page) q.set('page', String(params.page))
      if (params?.per_page) q.set('per_page', String(params.per_page))
      const { data } = await api.get(`/adventurer-s1/admin/data-appeals/bug-reclaims/history?${q.toString()}`)
      return data
    },

    async adminGetDataAppealBugReclaim(itemId: number) {
      const { data } = await api.get(
        `/adventurer-s1/admin/data-appeals/bug-reclaims/history/${itemId}`,
      )
      return data
    },

    async adminRevertDataAppealBugReclaim(itemId: number, note = '') {
      const { data } = await api.post(
        `/adventurer-s1/admin/data-appeals/bug-reclaims/history/${itemId}/revert`,
        { note },
      )
      return data
    },

    async adminPreviewDataAppealQtyShortfalls(limit = 50) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/qty-shortfalls/preview', { limit }, {
        timeout: 120000,
      })
      return data
    },

    async adminExecuteDataAppealQtyShortfalls(limit = 50, note = '', previewToken = '') {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/qty-shortfalls/execute', {
        limit,
        note,
        preview_token: previewToken,
      }, {
        timeout: 120000,
      })
      return data
    },

    async adminDismissDataAppealQtyShortfall(payload: {
      issue_key: string
      note?: string
      item?: Record<string, unknown>
    }) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/qty-shortfalls/dismiss', payload)
      return data
    },

    async adminAdjustDataAppealQtyShortfallQuantity(payload: {
      issue_key: string
      shortfall_quantity: number
      note?: string
      item?: Record<string, unknown>
    }) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/qty-shortfalls/adjust-quantity', payload)
      return data
    },

    async adminListDataAppealQtyShortfalls(params?: { page?: number; per_page?: number }) {
      const q = new URLSearchParams()
      if (params?.page) q.set('page', String(params.page))
      if (params?.per_page) q.set('per_page', String(params.per_page))
      const { data } = await api.get(`/adventurer-s1/admin/data-appeals/qty-shortfalls/history?${q.toString()}`)
      return data
    },

    async adminPreviewCatalogDescPollution(limit = 100) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/catalog-desc-pollution/preview', { limit }, {
        timeout: 120000,
      })
      return data
    },

    async adminExecuteCatalogDescPollution(limit = 100, note = '', previewToken = '') {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/catalog-desc-pollution/execute', {
        limit,
        note,
        preview_token: previewToken,
      }, {
        timeout: 120000,
      })
      return data
    },

    async adminRestoreCatalogDescPollution() {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/catalog-desc-pollution/restore', {}, {
        timeout: 120000,
      })
      return data
    },

    async adminDismissCatalogDescPollution(payload: { issue_key: string; note?: string }) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/catalog-desc-pollution/dismiss', payload)
      return data
    },

    async adminGetAppealQuota(params: { user_id?: string | number; username?: string }) {
      const q = new URLSearchParams()
      if (params.user_id !== undefined && params.user_id !== '') q.set('user_id', String(params.user_id))
      if (params.username) q.set('username', params.username)
      const { data } = await api.get(`/adventurer-s1/admin/data-appeals/quota?${q.toString()}`)
      return data
    },

    async adminResetAppealQuota(userId: number, quotaPool: 'other' | 'standard') {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/quota/reset', {
        user_id: userId,
        quota_pool: quotaPool,
      })
      return data
    },

    async adminAddAppealQuota(
      userId: number,
      count: number,
      quotaPool: 'other' | 'standard',
    ) {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/quota/add', {
        user_id: userId,
        count,
        quota_pool: quotaPool,
      })
      return data
    },

    async adminResetAllAppealQuota(quotaPool: 'other' | 'standard') {
      const { data } = await api.post('/adventurer-s1/admin/data-appeals/quota/reset-all', {
        quota_pool: quotaPool,
      })
      return data
    },

    // ── 管理员：S1 收益异常监控 ────────────────────────────────────
    async adminListGainAnomalies(params?: {
      status?: string
      asset_kind?: string
      severity?: string
      search?: string
      page?: number
      per_page?: number
    }) {
      const q = new URLSearchParams()
      if (params?.status) q.set('status', params.status)
      if (params?.asset_kind) q.set('asset_kind', params.asset_kind)
      if (params?.severity) q.set('severity', params.severity)
      if (params?.search?.trim()) q.set('search', params.search.trim())
      if (params?.page) q.set('page', String(params.page))
      if (params?.per_page) q.set('per_page', String(params.per_page))
      const { data } = await api.get(`/adventurer-s1/admin/gain-anomalies?${q.toString()}`)
      return data
    },

    async adminUpdateGainAnomaly(anomalyId: number, status: string, note = '') {
      const { data } = await api.put(`/adventurer-s1/admin/gain-anomalies/${anomalyId}`, {
        status,
        note,
      })
      return data
    },

    async adminPreviewGainAnomalyBackfill(lookbackDays: number, assetKind: string) {
      const { data } = await api.post('/adventurer-s1/admin/gain-anomalies/backfill/preview', {
        lookback_days: lookbackDays,
        asset_kind: assetKind,
      }, {
        timeout: 120000,
      })
      return data
    },

    async adminExecuteGainAnomalyBackfill(snapshotToken: string) {
      const { data } = await api.post('/adventurer-s1/admin/gain-anomalies/backfill/execute', {
        snapshot_token: snapshotToken,
      }, {
        timeout: 120000,
      })
      return data
    },

  },
})
