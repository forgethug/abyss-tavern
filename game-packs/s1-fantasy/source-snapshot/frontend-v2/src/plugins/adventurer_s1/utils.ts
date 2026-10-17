// 深渊酒馆 S1 前端共享展示工具：稀有度/状态标签与颜色、性格文本、HP 百分比等。

/** 不在建筑分区 UI 展示的系统占位（酒馆大堂只在「酒馆」tab 展示） */
export const BUILDING_CODES_HIDDEN_FROM_ZONE_UI = new Set(['tavern_hall'])

/** 越阶评定预览建筑：前端固定投影，兼容正式服尚未重跑 seed 的旧模板快照。 */
export const TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY = 'transcendent_court_preview'
export const TRANSCENDENT_ASSESSMENT_PREVIEW_CODE = 'transcendent_assessment_court'
export const TRANSCENDENT_ASSESSMENT_PREVIEW_NAME = '无冕终庭'
export const TRANSCENDENT_ASSESSMENT_PREVIEW_DESCRIPTION =
  '黑石终庭无钟无座，只为常阶之外的功业点燃庭火。这里铸下的不是佩在衣襟上的虚衔，而是将传遍酒馆、城邦与敌阵，令素未谋面之人也能认出你的世界铭号。'
export const TRANSCENDENT_ASSESSMENT_PREVIEW_REQUIRED_LEVEL = 20

export const RARITY_COLORS: Record<string, string> = {
  common: '#8b949e',
  rare: '#58a6ff',
  epic: '#bc8cff',
  legendary: '#f0883e',
}
export const RARITY_LABELS: Record<string, string> = {
  common: '普通',
  rare: '稀有',
  epic: '史诗',
  legendary: '传说',
}
export function rarityColor(r?: string): string {
  return RARITY_COLORS[r ?? 'common'] ?? RARITY_COLORS.common
}
export function rarityLabel(r?: string): string {
  return RARITY_LABELS[r ?? 'common'] ?? (r ?? '')
}

/** 与后端 calculate_pet_evolution_gold_cost 对齐的下一阶嬗变费用。 */
export function calculatePetEvolutionGoldCost(baseCost: number, evolutionStage: number): number {
  const base = Math.max(0, Number(baseCost) || 0)
  const stage = Math.max(0, Math.trunc(Number(evolutionStage) || 0))
  const multiplier = stage < 3 ? 1 : 2 ** (stage - 2)
  return Math.round(base * multiplier * 100) / 100
}

type EquipmentCarrier<T> = {
  id: number
  equipment?: T[] | null
}

/** 嬗变物品选择优先使用当前候选的完整详情，详情未就绪时安全回退到候选摘要。 */
export function resolvePetEvolutionEquipment<T>(
  detail: EquipmentCarrier<T> | null | undefined,
  candidate: EquipmentCarrier<T> | null | undefined,
): T[] {
  if (detail && detail.id === candidate?.id) {
    return detail.equipment ?? []
  }
  return candidate?.equipment ?? []
}

export function formatPetEvolutionDispatchConfirm(input: {
  adventurerName: string
  companionName: string
  currentStage: number
  goldCost: number
  currentGold: number
  auxiliaryMaterialName?: string | null
}): string {
  const stage = Math.max(0, Math.trunc(Number(input.currentStage) || 0))
  const cost = Math.max(0, Number(input.goldCost) || 0)
  const refund = Math.round(cost * 50) / 100
  const money = (value: number) => value.toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })
  const materialLine = input.auxiliaryMaterialName
    ? `辅助素材「${input.auxiliaryMaterialName}」会在派遣成功时立即消耗，无论嬗变结果如何都不返还。`
    : '本次未选择辅助素材。'
  return [
    `确认让「${input.adventurerName}」将「${input.companionName}」从 ${stage} 阶嬗变至 ${stage + 1} 阶？`,
    `本次将先扣除 ${money(cost)} 金币（当前持有 ${money(Math.max(0, Number(input.currentGold) || 0))}）。`,
    `若嬗变失败，将返还 ${money(refund)} 金币，伙伴保持原样。`,
    materialLine,
  ].join('\n\n')
}

/** 是否为隐藏 Boss 追查线索（仅认服务端 clue_label，不依赖副本/Boss 字段）。 */
export function isHiddenBossClueItem(item: { clue_label?: string | null } | null | undefined): boolean {
  return !!String(item?.clue_label || '').trim()
}

/** 线索序号文案（如「线索 2/4」）。 */
export function hiddenBossClueItemHint(item: { clue_label?: string | null } | null | undefined): string {
  if (!isHiddenBossClueItem(item)) return ''
  return String(item!.clue_label).trim()
}

/** 线索物品展示名：绰号（如「巫息灰烬」），永不用 clue_label 当标题。 */
export function hiddenBossClueItemDisplayName(item: {
  name?: string
  clue_label?: string | null
} | null | undefined): string {
  if (!isHiddenBossClueItem(item)) {
    return String(item?.name || '').trim()
  }
  const label = String(item?.clue_label || '').trim()
  const flavor = String(item?.name || '').trim()
  if (flavor && flavor !== label) return flavor
  return '追查线索'
}

export {
  ITEM_TYPE_LABELS,
  S1_ITEM_TYPES,
  S1_ITEM_TYPE_SET,
  S1_DEFAULT_ITEM_TYPE,
  S1_ITEM_TYPE_COMPANION,
  STARTER_ITEM_TYPE_ORDER,
  ITEM_TYPE_OPTIONS,
  itemTypeLabel,
  isKnownItemType,
  type S1ItemType,
} from './itemTypes'

export const STATUS_LABELS: Record<string, string> = {
  idle: '空闲',
  in_party: '组队中',
  exploring: '探险中',
  activity: '参加酒馆活动中',
  skill_school_queue: '等候导师指点',
  building_queue: '等候设施空位',
  resting: '休息',
  dead: '已阵亡',
  building: '建造中',
  working: '工作中',
}
export const STATUS_COLORS: Record<string, string> = {
  idle: '#198754',
  in_party: '#0d6efd',
  exploring: '#fd7e14',
  activity: '#d9a653',
  skill_school_queue: '#d29922',
  building_queue: '#b8863b',
  resting: '#6f42c1',
  dead: '#dc3545',
  building: '#20c997',
  working: '#17a2b8',
}
export function statusLabel(
  s?: string,
  buildingInstanceStatus?: string | null,
  handlerKey?: string | null,
  inTavern?: boolean,
): string {
  if (inTavern && (s === 'resting' || s === 'idle')) {
    return '在酒馆'
  }
  if (s === 'building' && buildingInstanceStatus === 'upgrading') {
    return '升级帮工'
  }
  if (s === 'building' && buildingInstanceStatus === 'building') {
    return '建造帮工'
  }
  if (s === 'building' && buildingInstanceStatus === 'built') {
    // 根据 handler 类型返回沉浸式状态标签
    const handlerLabels: Record<string, string> = {
      healing_house: '治疗中',
      chapel: '祈祷中',
      blacksmith: '锻造中',
      tailor_workshop: '裁制中',
      alchemy_workshop: '炼金中',
      ember_kitchen: '烹饪中',
      training: '修炼中',
      herbalist_shop: '购物中',
      armory_shop: '购物中',
      forge_supply_shop: '购物中',
      task_board: '执行任务',
      tavern_hall: '喝酒中',
      massage_parlor: '按摩中',
      foot_spa: '泡脚中',
      music_pavilion: '听曲中',
      hospital: '住院中',
      sanatorium: '疗养中',
      dining_hall: '用餐中',
      skill_school: '学艺中',
    }
    return handlerLabels[handlerKey ?? ''] ?? '工作中'
  }
  return STATUS_LABELS[s ?? ''] ?? (s ?? '')
}
export function statusColor(s?: string, inTavern?: boolean): string {
  if (inTavern && (s === 'resting' || s === 'idle')) return '#d97706'
  return STATUS_COLORS[s ?? ''] ?? '#8b949e'
}

export interface SpecialEventPresentation {
  label: string
  icon: string
  cardClass: string
  accentColor: string
}

const SPECIAL_EVENT_PRESENTATIONS: Record<string, SpecialEventPresentation> = {
  special_event: { label: '特殊事件', icon: '⚠️', cardClass: 'se-theme-special', accentColor: '#dc2626' },
  local_anomaly: { label: '局部异象', icon: '✨', cardClass: 'se-theme-local', accentColor: '#dc2626' },
  regional_incident: { label: '区域异变', icon: '🌩️', cardClass: 'se-theme-regional', accentColor: '#dc2626' },
  world_fact_catalyst: { label: '世界涟漪', icon: '🌐', cardClass: 'se-theme-world', accentColor: '#dc2626' },
  extreme_crisis: { label: '极端危机', icon: '☄️', cardClass: 'se-theme-crisis', accentColor: '#dc2626' },
  abyss: { label: '深渊降临', icon: '🕳️', cardClass: 'se-theme-abyss', accentColor: '#9333ea' },
  stranger: { label: '陌生来客', icon: '👁️', cardClass: 'se-theme-stranger', accentColor: '#9333ea' },
  unbeatable_horror: { label: '绝对死境', icon: '☠️', cardClass: 'se-theme-horror', accentColor: '#52525b' },
}

export function getSpecialEventPresentation(theme?: string | null): SpecialEventPresentation | null {
  if (!theme) return null
  return SPECIAL_EVENT_PRESENTATIONS[theme] ?? SPECIAL_EVENT_PRESENTATIONS.special_event
}

export function specialEventCardClass(theme?: string | null): string | null {
  return getSpecialEventPresentation(theme)?.cardClass ?? null
}

export function specialEventStyle(theme?: string | null): Record<string, string> | undefined {
  const presentation = getSpecialEventPresentation(theme)
  return presentation ? { '--se-accent': presentation.accentColor } : undefined
}

export function isSpecialEventPresentationStatus(status?: string | null): boolean {
  return status === 'in_progress' || status === 'completed'
}

export const PARTY_STATUS_LABELS: Record<string, string> = {
  forming: '集结中',
  exploring: '探险中',
  disbanded: '已解散',
  completed: '已完成',
}
export function partyStatusLabel(s?: string): string {
  return PARTY_STATUS_LABELS[s ?? ''] ?? (s ?? '')
}

export const LOG_STATUS_LABELS: Record<string, string> = {
  generating: '生成中',
  in_progress: '进行中',
  completed: '已完成',
  failed: '失败',
}
export function logStatusLabel(s?: string): string {
  return LOG_STATUS_LABELS[s ?? ''] ?? (s ?? '')
}

/** 生成中日志无 departure_msg 时的兜底（旧数据） */
export const EXPEDITION_GENERATING_FALLBACK = '队伍正在赶往目标地牢……'

export function getExpeditionDepartureText(fullLog: unknown): string {
  if (!fullLog || typeof fullLog !== 'object') return EXPEDITION_GENERATING_FALLBACK
  const fl = fullLog as { departure_msg?: string; summary?: string }
  return fl.departure_msg || fl.summary || EXPEDITION_GENERATING_FALLBACK
}

/** 判断是否已超过 GM 可重试 generating 的等待时间 */
export function isGeneratingStuckForGmRetry(
  createdAt: string | undefined,
  thresholdMinutes: number,
): boolean {
  if (!createdAt || thresholdMinutes <= 0) return false
  const created = /[Zz]$|[+-]\d{2}:\d{2}$/.test(createdAt)
    ? new Date(createdAt).getTime()
    : new Date(`${createdAt}Z`).getTime()
  if (Number.isNaN(created)) return false
  return Date.now() - created >= thresholdMinutes * 60_000
}

// 货币/材料流水来源类型中文映射（description 为空时的兜底）
export const SOURCE_TYPE_LABELS: Record<string, string> = {
  expedition: '探险',
  expedition_reward: '探险结算',
  expedition_loss: '探险损失',
  data_appeal_ai: '数据申诉',
  data_appeal_team_pool: '随队探险补偿',
  shop_buy: '商店购买',
  buyback_sell: '物品收购',
  gambling_bet: '赌坊下注',
  gambling_win: '赌坊赢钱',
  gambling_refund: '赌坊流局退款',
  lucky_wheel_spin: '大转盘抽奖',
  lucky_wheel_refund: '大转盘退款',
  market_fee: '市场手续费',
  market_sell: '个人市场售出',
  market_buy_order_sale: '响应市场求购售出',
  hidden_boss_clue_redeem: '隐藏 Boss 线索兑换',
  building_usage_cost: '建筑使用费',
  blacksmith_usage_cost: '铁匠铺锻造费',
  tailor_workshop_usage_cost: '绮罗裁坊裁制费',
  alchemy_workshop_usage_cost: '炼金工房费用',
  ember_kitchen_usage_cost: '余烬灶坊烹饪费',
  skill_school_custom_learn: '百艺阁·另学一艺',
  skill_school_custom_advance: '百艺阁·磨炼旧招',
  skill_training_system_refund: '百艺阁·授艺未成退还',
  building_usage_refund: '建筑费用退还',
  building_usage_complete: '建筑结算',
  building_settle: '建筑结算',
  tavern_drink: '酒馆点酒',
  tavern_treat: '酒馆请客',
  task_reward: '任务奖励',
  s1_tavern_relief: '酒馆补贴申请',
  data_appeal_tavern_subsidy: '申诉酒馆补贴',
  low_consumption_accrual: '低消费临时金分流',
  low_consumption_split: '低消费临时金分流',
  low_consume_overcredit_reclaim: '历史重复增发回收',
  low_consume_overcredit_restore: '撤销历史重复增发回收',
  dormitory_donation: '宿舍捐献',
  dormitory_cancel_refund: '宿舍退款',
  admin_adjust: '管理员调整',
  gold_rail_sync: '金币同步',
  migration_from_currency: '数据迁移',
  transfer: '转账',
  init: '初始资金',
  gathering: '采集所得',
  building_help_contribution: '帮工贡献',
  help_contrib: '帮工贡献',
  help_contrib_build: '帮工贡献',
  help_contrib_upgrade: '帮工贡献（升级）',
  donate_contrib: '捐献贡献',
  donate_contrib_build: '捐献贡献',
  donate_contrib_upgrade: '捐献贡献（升级）',
  donation_contribution_build: '捐献贡献',
  donation_contribution_upgrade: '捐献贡献（升级）',
  commission_reward: '委托奖励',
  ceremony_birthday: '星灯生日宴',
  blacksmith: '铁匠铺锻造',
  tailor_workshop: '绮罗裁坊裁制',
  alchemy_workshop: '炼金工房',
  ember_kitchen: '余烬灶坊',
  contribution: '贡献度',
  contribution_total: '累计贡献',
}
export function sourceTypeLabel(s?: string): string {
  return SOURCE_TYPE_LABELS[s ?? ''] ?? (s ?? '')
}

// 宿舍房间等级元数据：预览卡与房间详情弹窗共用
export const DORM_LEVELS = [
  { level: 1, name: '普通', icon: '🏚️', desc: '简陋的房间，只有最基本的床铺和家具' },
  { level: 2, name: '整洁', icon: '🏠', desc: '经过修缮，干净整洁，住起来舒适不少' },
  { level: 3, name: '精良', icon: '🏡', desc: '木质地板、暖色灯具，冒险者们很满意' },
  { level: 4, name: '豪华', icon: '🏘️', desc: '配套设施齐全，堪称酒馆中的贵宾房' },
  { level: 5, name: '传说', icon: '🏰', desc: '金碧辉煌，连巨龙都想来住一晚' },
]
export function dormLevelInfo(level: number) {
  return DORM_LEVELS[level - 1] || DORM_LEVELS[0]
}

/** 宿舍展示等级：装修 level 与建筑 upgrade_level 取较高者（升级完工后两者应同步） */
export function dormDisplayLevel(room: { level?: number; upgrade_level?: number | null }): number {
  return Math.max(room.level ?? 1, room.upgrade_level ?? 1)
}

export function dormRoomIcon(room: {
  level?: number
  upgrade_level?: number | null
}): string {
  return dormLevelInfo(dormDisplayLevel(room)).icon
}

// 床位上的冒险者是否「在家」（仅休息/空闲算在家，其余视为外出；在酒馆大堂不算在家）
export function isBedHome(status: string, inTavern?: boolean): boolean {
  if (inTavern) return false
  return status === 'resting' || status === 'idle'
}

/** 探险中禁止玩家主动操作（前后端均需拦截） */
export const EXPLORING_BLOCKED_HINT = '探险中无法进行此操作'

export function isExploringStatus(status?: string | null): boolean {
  return status === 'exploring'
}

/** 允许调整背包/仓库/携带技能的状态（与后端 INVENTORY_EDITABLE_STATUSES 对齐） */
export function canEditInventory(status?: string | null): boolean {
  return status === 'resting' || status === 'idle' || status === 'in_party'
}

export function personalityText(p: unknown): string {
  return Array.isArray(p) ? p.join('、') : String(p ?? '')
}

export const DORM_HP_RECOVERY_MINUTES = 10
export const STAMINA_RECOVERY_PER_MINUTE = 1

/** 建筑使用计时起点（疗养所特质周期会重置 building_assigned_at，须读 rental_start_at） */
export function buildingUsageStartMs(
  user: {
    building_assigned_at?: string | null
    building_usage_extra?: { rental_start_at?: string } | null
  },
  handlerKey?: string | null,
): number | null {
  if (handlerKey === 'sanatorium') {
    const raw = user.building_usage_extra?.rental_start_at
    if (raw) {
      const ms = new Date(raw).getTime()
      if (Number.isFinite(ms)) return ms
    }
  }
  const assigned = user.building_assigned_at
  if (!assigned) return null
  const ms = new Date(assigned).getTime()
  return Number.isFinite(ms) ? ms : null
}

export function liveStaminaOf(
  adv: {
    stamina?: number
    max_stamina?: number
    status?: string
    stamina_updated_at?: string | null
    building_assigned_at?: string | null
    building_handler_key?: string | null
    tavern_seated?: boolean
    tea_house_seated?: boolean
    dorm_recovery_paused?: boolean
    dorm_stamina_recovery_per_minute?: number
    stamina_recovery_per_minute?: number
  },
  nowMs: number,
  recoveryPerMinute = STAMINA_RECOVERY_PER_MINUTE,
): number {
  const maxStamina = adv.max_stamina ?? 100
  const baseStamina = Math.max(0, adv.stamina ?? 0)
  const effectiveRecoveryPerMinute = adv.tavern_seated === true
    ? recoveryPerMinute
    : (adv.stamina_recovery_per_minute
      ?? adv.dorm_stamina_recovery_per_minute
      ?? recoveryPerMinute)

  const venueRecoveryActive = (
    (adv.status === 'resting' || adv.status === 'idle')
    && (adv.tavern_seated === true || adv.tea_house_seated === true)
  )
  const recoveryPaused = adv.dorm_recovery_paused === true && !venueRecoveryActive
  const hasActiveRecoverySource = (
    adv.status === 'resting'
    || venueRecoveryActive
  )
  if (hasActiveRecoverySource && adv.stamina_updated_at && !recoveryPaused) {
    const updatedAtMs = new Date(adv.stamina_updated_at).getTime()
    if (Number.isFinite(updatedAtMs)) {
      const elapsed = Math.max(0, (nowMs - updatedAtMs) / 60000)
      return Math.min(maxStamina, Math.floor(baseStamina + elapsed * effectiveRecoveryPerMinute))
    }
  }

  if (adv.building_handler_key === 'sanatorium' && adv.building_assigned_at && adv.status !== 'resting') {
    const assignedAtMs = new Date(adv.building_assigned_at).getTime()
    if (Number.isFinite(assignedAtMs)) {
      const elapsed = Math.max(0, (nowMs - assignedAtMs) / 60000)
      return Math.min(maxStamina, Math.floor(baseStamina + elapsed * recoveryPerMinute))
    }
  }

  return Math.min(maxStamina, baseStamina)
}

export function liveHpOf(
  adv: {
    hp?: number
    max_hp?: number
    status?: string
    dorm_room_id?: number | null
    hp_updated_at?: string | null
    building_assigned_at?: string | null
    building_handler_key?: string | null
    tavern_seated?: boolean
    dorm_recovery_paused?: boolean
    building_usage_extra?: { heal_hp?: number; hp_per_minute?: number; hp_percent_per_minute?: number } | null
  },
  nowMs: number,
  recoveryMinutesPerPoint = DORM_HP_RECOVERY_MINUTES,
): number {
  const maxHp = adv.max_hp ?? 100
  const baseHp = Math.max(0, adv.hp ?? 0)

  // 深渊手术室：按 building_assigned_at + 累计百分比治疗量推进
  if (adv.building_handler_key === 'clinic' && adv.building_assigned_at && adv.status !== 'resting') {
    const extra = adv.building_usage_extra
    const targetHeal = Math.max(0, Number(extra?.heal_hp ?? 0))
    if (targetHeal > 0) {
      const refMs = new Date(adv.building_assigned_at).getTime()
      if (Number.isFinite(refMs)) {
        const elapsedMinutes = Math.floor(Math.max(0, (nowMs - refMs) / 60000))
        const storedPercent = Number(extra?.hp_percent_per_minute)
        const completedHeal = Number.isFinite(storedPercent) && storedPercent > 0
          ? Math.floor(elapsedMinutes * maxHp * storedPercent / 100)
          : Math.floor(elapsedMinutes) * Math.max(1, Number(extra?.hp_per_minute ?? 1))
        return Math.min(maxHp, baseHp + Math.min(targetHeal, completedHeal))
      }
    }
  }

  // 疗养所：按 building_assigned_at 起算，与宿舍床位同速（仅限 sanatorium）
  if (adv.building_handler_key === 'sanatorium' && adv.building_assigned_at && adv.status !== 'resting') {
    const refMs = new Date(adv.building_assigned_at).getTime()
    if (Number.isFinite(refMs)) {
      const elapsed = Math.max(0, (nowMs - refMs) / 60000)
      return Math.min(maxHp, Math.floor(baseHp + elapsed / recoveryMinutesPerPoint))
    }
  }

  // 宿舍床位
  if (adv.status !== 'resting' || !adv.dorm_room_id || !adv.hp_updated_at) return Math.min(maxHp, baseHp)
  if (adv.tavern_seated === true || adv.dorm_recovery_paused === true) return Math.min(maxHp, baseHp)
  const updatedAtMs = new Date(adv.hp_updated_at).getTime()
  if (!Number.isFinite(updatedAtMs)) return Math.min(maxHp, baseHp)
  const elapsed = Math.max(0, (nowMs - updatedAtMs) / 60000)
  return Math.min(maxHp, Math.floor(baseHp + elapsed / recoveryMinutesPerPoint))
}

export function hpPercent(adv: { hp: number; max_hp: number }, nowMs?: number): number {
  const hp = nowMs != null ? liveHpOf(adv, nowMs) : adv.hp
  if (!adv.max_hp) return 0
  return Math.max(0, Math.min(100, Math.round((hp / adv.max_hp) * 100)))
}

/** 与后端 ADVENTURER_S1_TRAIT_RECOVERY_CYCLE_HOURS 默认值一致；详情接口也会下发 trait_recovery_cycle_hours */
export const TRAIT_RECOVERY_CYCLE_HOURS = 6

export function parseUtcMs(value: string | null | undefined): number | null {
  if (!value) return null
  const ms = /[Zz]$|[+-]\d{2}:\d{2}$/.test(value)
    ? new Date(value).getTime()
    : new Date(`${value}Z`).getTime()
  return Number.isFinite(ms) ? ms : null
}

/** 详情接口下发的特质恢复 snapshot（由后端计算，前端只做秒级 tick） */
export interface TraitRecoverySnapshot {
  progress: number
  required: number
  remaining_cycles: number
  accumulating: boolean
  seconds_to_next_cycle: number | null
  completed_cycles_this_rest: number
  cycle_hours: number
  status_text: string
  hint_text: string
}

export function formatDurationSec(seconds: number): string {
  const totalSec = Math.max(0, Math.floor(seconds))
  const h = Math.floor(totalSec / 3600)
  const m = Math.floor((totalSec % 3600) / 60)
  const s = totalSec % 60
  if (h > 0) return `${h}小时${m}分`
  if (m > 0) return `${m}分${s}秒`
  return `${s}秒`
}

/** 建筑「使用并发位」量词：与后端 get_usage_slot_label 保持一致 */
const USAGE_SLOT_LABEL_BY_HANDLER: Record<string, string> = {
  hospital: '病床',
  sanatorium: '床位',
  dining_hall: '用餐位',
  skill_school: '传授位',
  blacksmith: '锻造位',
  tailor_workshop: '裁制位',
  alchemy_workshop: '炼金位',
  ember_kitchen: '灶位',
  healing_house: '治疗位',
  scavenge: '搜寻位',
  clue_bureau: '追查位',
  chapel: '忏悔位',
}

const USAGE_SLOT_LABEL_BY_CATEGORY: Record<string, string> = {
  work: '工位',
  gathering: '作业位',
  training: '训练位',
  leisure: '服务位',
}

export function getUsageSlotLabel(
  handlerKey?: string | null,
  category?: string | null,
): string {
  if (handlerKey && USAGE_SLOT_LABEL_BY_HANDLER[handlerKey]) {
    return USAGE_SLOT_LABEL_BY_HANDLER[handlerKey]
  }
  if (category && USAGE_SLOT_LABEL_BY_CATEGORY[category]) {
    return USAGE_SLOT_LABEL_BY_CATEGORY[category]
  }
  return '工位'
}

export function formatUsageSlotsFullMessage(
  current: number,
  maxSlots: number,
  handlerKey?: string | null,
  category?: string | null,
): string {
  const label = getUsageSlotLabel(handlerKey, category)
  return `${label}已满（${current}/${maxSlots}）`
}

/** 与后端 _effective_usage_slots 对齐：settlement_config.usage_slots > helper_slots，Lv2 叠加 usage_slots_bonus */
export function computeEffectiveUsageSlots(
  building: {
    upgrade_level?: number | null
    effective_usage_slots?: number | null
    template?: {
      helper_slots?: number
      upgrade_config?: { usage_slots_bonus?: number } | null
      settlement_config?: {
        usage_slots?: number
        unlimited_usage?: boolean
      } | null
    } | null
  },
  config?: {
    usage_slots?: number
    unlimited_usage?: boolean
  } | null,
): number {
  if (building.effective_usage_slots != null && building.effective_usage_slots > 0) {
    return building.effective_usage_slots
  }
  const cfg = config ?? building.template?.settlement_config ?? null
  if (cfg?.unlimited_usage) return 0
  let base = 0
  const fromCfg = cfg?.usage_slots
  if (fromCfg != null && fromCfg > 0) base = fromCfg
  else base = building.template?.helper_slots ?? 0
  const level = building.upgrade_level ?? 1
  if (level > 1) {
    const bonus = Number(building.template?.upgrade_config?.usage_slots_bonus ?? 0)
    base += bonus
  }
  return base
}

/** 建造中在位帮手数（仅统计仍有精力的 building 态冒险者） */
export function getBuildingActiveHelperCount(
  building: { helpers?: Array<{ stamina?: number | null }> | null },
): number {
  const helpers = building.helpers ?? []
  if (helpers.length === 0) return 0
  // 列表接口 for_list 只下发 id/name，不含 stamina；在场即视为活跃（精力耗尽由后端 tick 清场）
  const withStaminaField = helpers.filter(h => h.stamina != null)
  if (withStaminaField.length === 0) return helpers.length
  return withStaminaField.filter(h => (h.stamina ?? 0) > 0).length
}

/** 建造/升级进度倒计时用的后端快照字段 */
export type BuildingProgressSnapshot = {
  status: string
  remaining_seconds: number
  total_build_seconds: number
  progress_percent?: number
  build_complete_at: string | null
  upgrade_complete_at?: string | null
  helpers?: Array<{ stamina?: number | null }> | null
  template?: { build_hours?: number; speedup_minutes?: number } | null
}

/** 快照变化时重锚 ETA（状态 / 剩余工程量 / 帮工数 / 后端 ETA 字段） */
export function buildingProgressAnchorKey(b: BuildingProgressSnapshot): string {
  const active = getBuildingActiveHelperCount(b)
  const eta = b.status === 'upgrading'
    ? (b.upgrade_complete_at ?? '')
    : (b.build_complete_at ?? '')
  return `${b.status}|${b.remaining_seconds ?? 0}|${active}|${eta}`
}

/**
 * 从后端快照解析预计完工时刻（ms）。
 * 优先用 build_complete_at / upgrade_complete_at；缺失时用剩余工程量 + 帮工速率在 anchorNow 合成。
 */
export function resolveBuildingProgressEtaMs(
  building: BuildingProgressSnapshot,
  anchorNowMs: number,
): number | null {
  const inProgress = building.status === 'building' || building.status === 'upgrading'
  if (!inProgress) return null

  const remainingWork = Math.max(0, building.remaining_seconds ?? 0)
  if (remainingWork <= 0) return anchorNowMs

  const activeCount = getBuildingActiveHelperCount(building)
  if (activeCount <= 0) return null

  const etaIso = building.status === 'upgrading'
    ? building.upgrade_complete_at
    : building.build_complete_at
  if (etaIso) {
    const ms = new Date(etaIso).getTime()
    if (!Number.isNaN(ms)) return ms
  }

  const speedupMinutes = building.template?.speedup_minutes ?? 60
  const progressRate = activeCount * speedupMinutes / 60
  if (progressRate <= 0) return null
  return anchorNowMs + (remainingWork / progressRate) * 1000
}

/** 宿舍 Lv2 开工确认（所有宿舍房间升级入口统一走此弹窗）。 */
export function confirmStartDormRoomUpgrade(roomName: string): boolean {
  const label = roomName.trim() || '该宿舍'
  return window.confirm(
    `开始升级「${label}」后，宿舍床位将暂时不可用，在床位上的冒险者暂停恢复精力、生命与特质（外出/酒馆等地不受影响）。确认开始升级？`,
  )
}

/**
 * 建造剩余时间（秒）。
 * - 有人在位：按 ETA 实时倒计时（绝对时间或 anchorNow 合成）
 * - 无人帮忙：返回剩余工程量（暂停）
 */
export function computeBuildingRemainingSeconds(
  building: BuildingProgressSnapshot,
  nowMs: number,
  anchorNowMs?: number,
): number {
  const inProgress = building.status === 'building' || building.status === 'upgrading'
  if (!inProgress) return building.remaining_seconds ?? 0

  const remainingWork = Math.max(0, building.remaining_seconds ?? 0)
  if (remainingWork <= 0) return 0

  const activeCount = getBuildingActiveHelperCount(building)
  if (activeCount <= 0) return remainingWork

  const etaMs = resolveBuildingProgressEtaMs(building, anchorNowMs ?? nowMs)
  if (etaMs == null) return remainingWork
  return Math.max(0, Math.floor((etaMs - nowMs) / 1000))
}

/** 将列表/详情/写操作返回的建造进度字段合并进本地副本（触发 ETA 重锚） */
export function mergeBuildingConstructionTiming<T extends BuildingProgressSnapshot>(
  target: T,
  patch: Partial<BuildingProgressSnapshot>,
): T {
  if (patch.status != null) target.status = patch.status
  if (patch.remaining_seconds != null) target.remaining_seconds = patch.remaining_seconds
  if (patch.total_build_seconds != null) target.total_build_seconds = patch.total_build_seconds
  if (patch.progress_percent != null) target.progress_percent = patch.progress_percent
  if (patch.build_complete_at !== undefined) target.build_complete_at = patch.build_complete_at
  if (patch.upgrade_complete_at !== undefined) target.upgrade_complete_at = patch.upgrade_complete_at
  if (patch.helpers) target.helpers = patch.helpers
  if (patch.template && target.template) {
    target.template = { ...target.template, ...patch.template }
  }
  return target
}

export function formatBuildingRemaining(seconds: number): string {
  if (seconds <= 0) return '建造完成'
  if (seconds < 60) return '即将完成'
  const h = Math.floor(seconds / 3600)
  const m = Math.floor((seconds % 3600) / 60)
  if (h > 0) return `${h}小时${m}分钟`
  return `${m}分钟`
}

/** 每位帮手每小时推进的建造小时数（speedup_minutes / 60） */
export function formatHelperSpeedupHours(speedupMinutes: number): string {
  const hours = speedupMinutes / 60
  if (Number.isInteger(hours)) return String(hours)
  return hours.toFixed(1).replace(/\.0$/, '')
}

/** 详情页本地推断：当前是否仍在累计恢复周期（不依赖可能过期的 snapshot.accumulating） */
export interface TraitRecoveryTickContext {
  status?: string
  dorm_room_id?: number | null
  bed_index?: number | null
  building_handler_key?: string | null
  tavern_seated?: boolean
}

const SANATORIUM_CYCLE_HOURS_DEFAULT = 3

export function isTraitRecoveryAccumulatingLocally(
  ctx: TraitRecoveryTickContext | null | undefined,
): boolean {
  if (!ctx || ctx.tavern_seated) return false
  if (
    ctx.status === 'resting'
    && ctx.dorm_room_id != null
    && ctx.bed_index != null
  ) {
    return true
  }
  if (ctx.status === 'building' && ctx.building_handler_key === 'sanatorium') {
    return true
  }
  return false
}

function sanatoriumSpeedMultiplier(cycleHours: number): number {
  return Math.max(1, Math.floor(cycleHours / SANATORIUM_CYCLE_HOURS_DEFAULT))
}

function traitRecoveryElapsedEffectiveSec(
  elapsedSec: number,
  ctx: TraitRecoveryTickContext | null | undefined,
  cycleHours: number,
): number {
  if (ctx?.status === 'building' && ctx?.building_handler_key === 'sanatorium') {
    return elapsedSec * sanatoriumSpeedMultiplier(cycleHours)
  }
  return elapsedSec
}

/** 距下一恢复周期剩余秒数：snapshot 提供基准，前端按本地休息态自行 tick */
export function traitRecoveryRemainingSec(
  recovery: TraitRecoverySnapshot,
  computedAt: string | null | undefined,
  nowMs: number,
  ctx?: TraitRecoveryTickContext | null,
): number | null {
  if (recovery.seconds_to_next_cycle == null || !computedAt) {
    return null
  }
  const locallyAccumulating = (
    recovery.accumulating || isTraitRecoveryAccumulatingLocally(ctx)
  )
  if (!locallyAccumulating) {
    return recovery.seconds_to_next_cycle
  }
  const baseMs = parseUtcMs(computedAt)
  if (baseMs == null) return null
  const elapsedSec = Math.max(0, (nowMs - baseMs) / 1000)
  const cycleHours = recovery.cycle_hours || TRAIT_RECOVERY_CYCLE_HOURS
  const remainingEffective = Math.max(
    0,
    recovery.seconds_to_next_cycle - traitRecoveryElapsedEffectiveSec(elapsedSec, ctx, cycleHours),
  )
  if (ctx?.status === 'building' && ctx?.building_handler_key === 'sanatorium') {
    return remainingEffective / sanatoriumSpeedMultiplier(cycleHours)
  }
  return remainingEffective
}

/** 周期倒计时已归零，应拉详情触发后端结算/刷新 snapshot */
export function isTraitRecoveryDueForRefresh(
  recovery: TraitRecoverySnapshot,
  computedAt: string | null | undefined,
  nowMs: number,
  ctx?: TraitRecoveryTickContext | null,
): boolean {
  if (!recovery.accumulating && !isTraitRecoveryAccumulatingLocally(ctx)) {
    return false
  }
  const remaining = traitRecoveryRemainingSec(recovery, computedAt, nowMs, ctx)
  return remaining != null && remaining <= 0
}

function formatTraitRecoveryNextCycleLabel(remainingSec: number): string {
  if (remainingSec <= 0) return '即将结算'
  return formatDurationSec(remainingSec)
}

/** 基于 snapshot + trait_recovery_computed_at 本地刷新「下周期」文案 */
export function liveTraitRecoveryView(
  recovery: TraitRecoverySnapshot,
  computedAt: string | null | undefined,
  nowMs: number,
  ctx?: TraitRecoveryTickContext | null,
): TraitRecoverySnapshot & { statusText: string; hintText: string; remainingSec: number | null } {
  let statusText = recovery.status_text
  const hintText = recovery.hint_text
  const locallyAccumulating = (
    recovery.accumulating || isTraitRecoveryAccumulatingLocally(ctx)
  )
  const remainingSec = traitRecoveryRemainingSec(recovery, computedAt, nowMs, ctx)

  if (remainingSec != null && locallyAccumulating) {
    const progressText = `恢复进度 ${recovery.progress}/${recovery.required}`
    const policyLabel = recovery.status_text.includes('睡眠') ? '睡眠' : '静养'
    const inSanatorium = ctx?.status === 'building' && ctx?.building_handler_key === 'sanatorium'
    if (inSanatorium) {
      statusText = `🌿 疗养${policyLabel}中 · ${progressText} · 下周期 ${formatTraitRecoveryNextCycleLabel(remainingSec)}`
    } else {
      statusText = `🛏️ 宿舍${policyLabel}中 · ${progressText} · 下周期 ${formatTraitRecoveryNextCycleLabel(remainingSec)}`
    }
  }

  return {
    ...recovery,
    accumulating: locallyAccumulating,
    statusText,
    hintText,
    remainingSec,
  }
}

/** 打工区：超出属性要求每 5 点 +1 金币/次，参与计算的属性上限 40 */
export const WORK_STAT_OVERFLOW_STEP = 5
export const WORK_STAT_OVERFLOW_BONUS = 1
export const WORK_STAT_OVERFLOW_CAP = 40

/** 冒险者永久基础属性 / 生命上限封顶（与 backend plugin default_config 对齐） */
export const S1_ATTR_CAP = 200
export const S1_MAX_HP_CAP = 1000

export function calcWorkStatOverflowBonus(
  requiredStat?: string,
  requiredMin?: number,
  adventurer?: { strength?: number; agility?: number; intelligence?: number; luck?: number } | null,
): number {
  if (!requiredStat || !requiredMin || requiredMin <= 0 || !adventurer) return 0
  const raw = Number((adventurer as Record<string, number>)[requiredStat] ?? 0)
  const effective = Math.min(raw, WORK_STAT_OVERFLOW_CAP)
  const overflow = Math.max(0, effective - requiredMin)
  return Math.floor(overflow / WORK_STAT_OVERFLOW_STEP) * WORK_STAT_OVERFLOW_BONUS
}
