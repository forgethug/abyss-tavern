import { tavernStorageKey } from '@/runtime/activeTavern'
import { onUnmounted, ref, watch, type Ref } from 'vue'
import api from '@/composables/useApi'
import { useUserStore } from '@/stores/user'
import { useAdventurerS1Store } from '../stores/adventurerS1'

export type AmbientNpcItemSource = 'carry' | 'warehouse'
export type AmbientNpcConfirmationLevel = 'normal' | 'valuable' | 'irreversible'

export interface AmbientNpcItemOption {
  source: AmbientNpcItemSource
  id: number
  name: string
  type: string
  description: string
  rarity: string
  quantity: number
  created_at?: string | null
  uses_left?: number | null
  evolution_stage?: number | null
  confirmation_level: AmbientNpcConfirmationLevel
  warning_text: string | null
  is_player_locked?: boolean
}

export interface AmbientNpcActionView {
  key: string
  label: string
  requires_item: boolean
  completed: boolean
  last_text: string | null
  available?: boolean
  unavailable_reason?: string | null
  next_available_at?: string | null
}

export interface AmbientNpcEncounterView {
  id: string
  surface_key: string
  npc: {
    key: string
    name: string
    breed: string
    icon_key: string
    icon: string
    short_intro: string
    public_background?: string | null
    presence_text: string
  }
  actions: AmbientNpcActionView[]
}

interface AmbientNpcCurrentPayload {
  encounter: AmbientNpcEncounterView | null
  next_refresh_at: string | null
}

export interface AmbientNpcInteractionResult {
  encounter_id: string
  action_key: string
  text: string
  consumed_item: {
    source: AmbientNpcItemSource
    id: number
    name: string
    quantity: number
    remaining_quantity?: number | null
    removed?: boolean
  } | null
  completed: true
}

interface AmbientNpcInteractionPayload extends AmbientNpcCurrentPayload {
  interaction: AmbientNpcInteractionResult
  refresh_required: boolean
}

export interface AmbientNpcPendingAction {
  userId: number
  encounterId: string
  actionKey: string
  surface: string
  requestId: string
  item: AmbientNpcItemOption | null
  createdAt: number
  expiresAt: number
}

interface CachedCurrent {
  payload: AmbientNpcCurrentPayload
  expiresAt: number
}

interface AmbientNpcMutationEvent {
  userId: number
  surface: string
  npcKey: string
  payload: AmbientNpcInteractionPayload
}

interface PendingActionMutationEvent {
  kind: 'upsert' | 'remove'
  userId: number
  encounterId: string
  actionKey: string
  requestId: string
  external: boolean
}

type AmbientNpcRetryDisposition =
  | 'retry_same_request'
  | 'resolved_by_other_request'
  | 'terminal'

interface AmbientNpcStructuredErrorData {
  error_code: string
  retry_disposition: AmbientNpcRetryDisposition
  request_id: string
  encounter_id: string
  action_key: string
  surface: string
  canonical_payload?: unknown
}

type AmbientNpcErrorResolution =
  | { kind: 'retry_same_request' | 'terminal' | 'unknown' }
  | { kind: 'resolved_by_other_request'; payload: AmbientNpcInteractionPayload }

interface CurrentInFlight {
  epoch: number
  promise: Promise<AmbientNpcCurrentPayload>
}

const currentCache = new Map<string, CachedCurrent>()
const currentInFlight = new Map<string, CurrentInFlight>()
const currentEpoch = new Map<string, number>()
const mutationListeners = new Set<(event: AmbientNpcMutationEvent) => void>()
const pendingActionLocks = new Map<string, AmbientNpcPendingAction>()
const pendingActionListeners = new Set<(event: PendingActionMutationEvent) => void>()
const AMBIENT_NPC_ACTION_TIMEOUT_MS = 60_000
const AMBIENT_NPC_PENDING_ACTION_TTL_MS = 24 * 60 * 60 * 1_000
const AMBIENT_NPC_PENDING_STORAGE_BASE = tavernStorageKey('s1:ambient-npc:pending-action:')
const AMBIENT_NPC_PENDING_STORAGE_PREFIX = tavernStorageKey('s1:ambient-npc:pending-action:v1:')
const AMBIENT_NPC_PENDING_LOCK_PREFIX = tavernStorageKey('s1-ambient-npc-pending-action-v1')
const REQUEST_ID_PATTERN = /^[A-Za-z0-9_-]{8,64}$/
let pendingSyncInitialized = false

function currentKey(userId: number, surface: string): string {
  return `${userId}:${surface}`
}

function epochFor(key: string): number {
  return currentEpoch.get(key) || 0
}

function bumpEpoch(key: string): number {
  const next = epochFor(key) + 1
  currentEpoch.set(key, next)
  return next
}

function apiErrorMessage(error: unknown, fallback: string): string {
  const typed = error as {
    message?: string
    response?: { data?: { message?: string } }
  }
  return typed?.response?.data?.message || typed?.message || fallback
}

function newRequestId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID().replace(/-/g, '')
  }
  return `npc_${Date.now().toString(36)}_${Math.random().toString(36).slice(2, 14)}`
}

function payloadExpiry(payload: AmbientNpcCurrentPayload): number {
  const candidates = [payload.next_refresh_at]
  for (const action of payload.encounter?.actions || []) {
    candidates.push(action.next_available_at || null)
  }
  const timestamps = candidates
    .map(value => Date.parse(value || ''))
    .filter(value => Number.isFinite(value) && value > Date.now())
  return timestamps.length > 0 ? Math.min(...timestamps) : Date.now() + 30_000
}

function normalizeConfirmationLevel(value: unknown): AmbientNpcConfirmationLevel {
  if (value === 'normal' || value === 'valuable' || value === 'irreversible') return value
  // 风险字段缺失或出现未知值时按最高等级确认，不能让契约漂移降低保护。
  return 'irreversible'
}

function optionalFiniteNumber(value: unknown): number | null {
  if (value == null || value === '') return null
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed : null
}

function normalizeItemOption(value: unknown): AmbientNpcItemOption | null {
  if (!value || typeof value !== 'object') return null
  const item = value as Record<string, unknown>
  const source = item.source
  const id = Number(item.id)
  const quantity = Number(item.quantity)
  if ((source !== 'carry' && source !== 'warehouse') || !Number.isInteger(id) || id <= 0) {
    return null
  }
  if (!Number.isFinite(quantity) || quantity <= 0) return null
  return {
    source,
    id,
    name: String(item.name || '').trim() || '未命名物品',
    type: String(item.type || '').trim(),
    description: String(item.description || '').trim(),
    rarity: String(item.rarity || 'common').trim() || 'common',
    quantity,
    created_at: String(item.created_at || '').trim() || null,
    uses_left: optionalFiniteNumber(item.uses_left),
    evolution_stage: optionalFiniteNumber(item.evolution_stage),
    confirmation_level: normalizeConfirmationLevel(item.confirmation_level),
    warning_text: String(item.warning_text || '').trim() || null,
    is_player_locked: item.is_player_locked === true,
  }
}

function pendingItemSnapshot(item: AmbientNpcItemOption): AmbientNpcItemOption {
  return {
    source: item.source,
    id: item.id,
    name: item.name.slice(0, 120),
    type: item.type.slice(0, 80),
    // 自定义描述可能很长；未决锁只保存关联与确认所需的最小快照。
    description: '',
    rarity: item.rarity.slice(0, 40),
    quantity: item.quantity,
    uses_left: item.uses_left ?? null,
    evolution_stage: item.evolution_stage ?? null,
    confirmation_level: item.confirmation_level,
    warning_text: item.warning_text?.slice(0, 240) || null,
  }
}

class PendingActionConflictError extends Error {
  readonly pendingAction: AmbientNpcPendingAction

  constructor(pendingAction: AmbientNpcPendingAction) {
    super(
      pendingAction.item
        ? `上一份「${pendingAction.item.name}」的赠礼结果尚未确认，不能改送其他物品`
        : '上一次互动的结果尚未确认，请沿用原请求继续核对',
    )
    this.name = 'PendingActionConflictError'
    this.pendingAction = pendingAction
  }
}

class AmbientNpcApiEnvelopeError extends Error {
  readonly response: { status: number; data: unknown }

  constructor(status: number, body: unknown) {
    const envelope = body && typeof body === 'object'
      ? body as Record<string, unknown>
      : null
    super(typeof envelope?.message === 'string' ? envelope.message : '互动失败')
    this.name = 'AmbientNpcApiEnvelopeError'
    this.response = { status, data: body }
  }
}

class AmbientNpcReceiptMismatchError extends Error {
  constructor() {
    super('服务器回执与本次互动不一致，页面没有采纳该结果；请沿用原选择重新核对')
    this.name = 'AmbientNpcReceiptMismatchError'
  }
}

export class AmbientNpcTerminalActionError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'AmbientNpcTerminalActionError'
  }
}

function pendingActionKey(userId: number, encounterId: string, actionKey: string): string {
  return JSON.stringify([userId, encounterId, actionKey])
}

function pendingStorageKey(userId: number, encounterId: string, actionKey: string): string {
  return `${AMBIENT_NPC_PENDING_STORAGE_PREFIX}${userId}:${encodeURIComponent(encounterId)}:${encodeURIComponent(actionKey)}`
}

function sameItemRef(
  left: AmbientNpcItemOption | null,
  right: AmbientNpcItemOption | null,
): boolean {
  if (left === null || right === null) return left === right
  return left.source === right.source && left.id === right.id
}

function normalizePendingAction(
  value: unknown,
  { allowExpired = false }: { allowExpired?: boolean } = {},
): AmbientNpcPendingAction | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const userId = Number(raw.userId)
  const encounterId = String(raw.encounterId || '').trim()
  const actionKey = String(raw.actionKey || '').trim()
  const surface = String(raw.surface || '').trim()
  const requestId = String(raw.requestId || '').trim()
  const createdAt = Number(raw.createdAt)
  const expiresAt = Number(raw.expiresAt)
  if (
    !Number.isInteger(userId)
    || userId <= 0
    || !encounterId
    || encounterId.length > 128
    || !actionKey
    || actionKey.length > 64
    || !surface
    || surface.length > 128
    || !REQUEST_ID_PATTERN.test(requestId)
    || !Number.isFinite(createdAt)
    || !Number.isFinite(expiresAt)
    || createdAt <= 0
    || createdAt > Date.now() + 60_000
    || expiresAt <= createdAt
    || expiresAt > createdAt + AMBIENT_NPC_PENDING_ACTION_TTL_MS + 1_000
    || (!allowExpired && expiresAt <= Date.now())
  ) return null

  const normalizedItem = raw.item == null ? null : normalizeItemOption(raw.item)
  const item = normalizedItem ? pendingItemSnapshot(normalizedItem) : null
  return {
    userId,
    encounterId,
    actionKey,
    surface,
    requestId,
    item,
    createdAt,
    expiresAt,
  }
}

function readStoredPendingAction(
  userId: number,
  encounterId: string,
  actionKey: string,
): AmbientNpcPendingAction | null {
  if (typeof window === 'undefined') return null
  const key = pendingStorageKey(userId, encounterId, actionKey)
  try {
    const raw = window.localStorage.getItem(key)
    if (!raw) return null
    const pending = normalizePendingAction(JSON.parse(raw), { allowExpired: true })
    if (
      !pending
      || pending.userId !== userId
      || pending.encounterId !== encounterId
      || pending.actionKey !== actionKey
    ) {
      window.localStorage.removeItem(key)
      return null
    }
    return pending
  } catch {
    try {
      window.localStorage.removeItem(key)
    } catch {
      // localStorage 被浏览器策略禁用时仍保留当前页面内的安全锁。
    }
    return null
  }
}

function notifyPendingAction(
  pending: AmbientNpcPendingAction,
  kind: PendingActionMutationEvent['kind'],
  external: boolean,
) {
  const event: PendingActionMutationEvent = {
    kind,
    userId: pending.userId,
    encounterId: pending.encounterId,
    actionKey: pending.actionKey,
    requestId: pending.requestId,
    external,
  }
  for (const listener of pendingActionListeners) listener(event)
}

function persistPendingAction(pending: AmbientNpcPendingAction) {
  pendingActionLocks.set(
    pendingActionKey(pending.userId, pending.encounterId, pending.actionKey),
    pending,
  )
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.setItem(
        pendingStorageKey(pending.userId, pending.encounterId, pending.actionKey),
        JSON.stringify(pending),
      )
    } catch {
      // Safari 隐私模式等场景退化为同页锁，服务端继续承担最终幂等闸门。
    }
  }
  notifyPendingAction(pending, 'upsert', false)
}

function clearPendingAction(pending: AmbientNpcPendingAction) {
  const key = pendingActionKey(pending.userId, pending.encounterId, pending.actionKey)
  const stored = readStoredPendingAction(
    pending.userId,
    pending.encounterId,
    pending.actionKey,
  )
  const inMemory = pendingActionLocks.get(key)
  if (
    (stored && stored.requestId !== pending.requestId)
    || (inMemory && inMemory.requestId !== pending.requestId)
  ) return
  pendingActionLocks.delete(key)
  if (typeof window !== 'undefined') {
    try {
      window.localStorage.removeItem(
        pendingStorageKey(pending.userId, pending.encounterId, pending.actionKey),
      )
    } catch {
      // 当前页面内的锁已经清除；持久化残留会受 TTL 与身份校验约束。
    }
  }
  notifyPendingAction(pending, 'remove', false)
}

function getPendingAction(
  userId: number,
  encounterId: string,
  actionKey: string,
): AmbientNpcPendingAction | null {
  const key = pendingActionKey(userId, encounterId, actionKey)
  const stored = readStoredPendingAction(userId, encounterId, actionKey)
  const pending = stored || pendingActionLocks.get(key) || null
  if (!pending) return null
  if (pending.expiresAt <= Date.now()) {
    clearPendingAction(pending)
    return null
  }
  pendingActionLocks.set(key, pending)
  return pending
}

function applyExternalPendingAction(
  pending: AmbientNpcPendingAction,
  kind: PendingActionMutationEvent['kind'],
) {
  const key = pendingActionKey(pending.userId, pending.encounterId, pending.actionKey)
  const current = pendingActionLocks.get(key)
  if (kind === 'upsert' && pending.expiresAt > Date.now()) {
    if (current?.requestId === pending.requestId && current.expiresAt === pending.expiresAt) return
    pendingActionLocks.set(key, pending)
    notifyPendingAction(pending, 'upsert', true)
    return
  }
  if (!current || current.requestId !== pending.requestId) return
  pendingActionLocks.delete(key)
  notifyPendingAction(pending, 'remove', true)
}

function cleanupStoredPendingActions() {
  if (typeof window === 'undefined') return
  try {
    const keys: string[] = []
    for (let index = 0; index < window.localStorage.length; index += 1) {
      const key = window.localStorage.key(index)
      if (key?.startsWith(AMBIENT_NPC_PENDING_STORAGE_BASE)) keys.push(key)
    }
    for (const key of keys) {
      const raw = window.localStorage.getItem(key)
      let keep = key.startsWith(AMBIENT_NPC_PENDING_STORAGE_PREFIX) && Boolean(raw)
      if (keep) {
        try {
          const pending = normalizePendingAction(JSON.parse(raw as string), { allowExpired: true })
          keep = Boolean(pending && pending.expiresAt > Date.now())
        } catch {
          keep = false
        }
      }
      if (!keep) window.localStorage.removeItem(key)
    }
  } catch {
    // 浏览器禁用存储时不影响当前页面内锁与后端幂等。
  }
}

function ensurePendingActionSync() {
  if (pendingSyncInitialized || typeof window === 'undefined') return
  pendingSyncInitialized = true
  cleanupStoredPendingActions()
  window.addEventListener('storage', (event) => {
    if (!event.key?.startsWith(AMBIENT_NPC_PENDING_STORAGE_PREFIX)) return
    const raw = event.newValue || event.oldValue
    if (!raw) return
    try {
      const pending = normalizePendingAction(JSON.parse(raw), { allowExpired: true })
      if (!pending) return
      applyExternalPendingAction(pending, event.newValue ? 'upsert' : 'remove')
    } catch {
      // 不采纳其他标签页写入的损坏或旧版本数据。
    }
  })
}

async function withPendingClaimLock<T>(key: string, callback: () => T | Promise<T>): Promise<T> {
  if (typeof navigator === 'undefined') return callback()
  const lockManager = (navigator as Navigator & {
    locks?: { request: <R>(name: string, callback: () => R | Promise<R>) => Promise<R> }
  }).locks
  if (!lockManager?.request) return callback()
  return lockManager.request(`${AMBIENT_NPC_PENDING_LOCK_PREFIX}:${key}`, callback)
}

async function claimPendingAction(input: {
  userId: number
  encounterId: string
  actionKey: string
  surface: string
  item: AmbientNpcItemOption | null
}): Promise<{ pending: AmbientNpcPendingAction; created: boolean }> {
  const key = pendingActionKey(input.userId, input.encounterId, input.actionKey)
  return withPendingClaimLock(key, () => {
    const existing = getPendingAction(input.userId, input.encounterId, input.actionKey)
    if (existing) {
      if (existing.surface !== input.surface || !sameItemRef(existing.item, input.item)) {
        throw new PendingActionConflictError(existing)
      }
      return { pending: existing, created: false }
    }
    const createdAt = Date.now()
    const pending: AmbientNpcPendingAction = {
      userId: input.userId,
      encounterId: input.encounterId,
      actionKey: input.actionKey,
      surface: input.surface,
      requestId: newRequestId(),
      item: input.item ? pendingItemSnapshot(input.item) : null,
      createdAt,
      expiresAt: createdAt + AMBIENT_NPC_PENDING_ACTION_TTL_MS,
    }
    persistPendingAction(pending)
    return { pending, created: true }
  })
}

function isOptionalNullableString(value: unknown): boolean {
  return value === undefined || value === null || typeof value === 'string'
}

function isNullableString(value: unknown): boolean {
  return value === null || typeof value === 'string'
}

function validateEncounterActions(
  value: unknown,
  expectedActionKey: string,
  expectedText: string,
) {
  if (!Array.isArray(value)) throw new AmbientNpcReceiptMismatchError()
  const actionKeys = new Set<string>()
  let matchedCompletedAction = false
  for (const rawAction of value) {
    if (!rawAction || typeof rawAction !== 'object') {
      throw new AmbientNpcReceiptMismatchError()
    }
    const action = rawAction as Record<string, unknown>
    if (
      typeof action.key !== 'string'
      || !action.key.trim()
      || actionKeys.has(action.key)
      || typeof action.label !== 'string'
      || !action.label.trim()
      || typeof action.requires_item !== 'boolean'
      || typeof action.completed !== 'boolean'
      || !isNullableString(action.last_text)
      || (action.available !== undefined && typeof action.available !== 'boolean')
      || !isOptionalNullableString(action.unavailable_reason)
      || !isOptionalNullableString(action.next_available_at)
    ) throw new AmbientNpcReceiptMismatchError()
    actionKeys.add(action.key)
    if (action.key === expectedActionKey) {
      matchedCompletedAction = (
        action.completed === true
        && action.last_text === expectedText
      )
    }
  }
  if (!matchedCompletedAction) throw new AmbientNpcReceiptMismatchError()
}

function validateInteractionPayload(
  value: unknown,
  expected: {
    encounterId: string
    actionKey: string
    surface: string
    npcKey: string
    itemRef: { source: AmbientNpcItemSource; id: number } | null
    allowDifferentConsumedItem?: boolean
  },
): AmbientNpcInteractionPayload {
  if (!value || typeof value !== 'object') throw new AmbientNpcReceiptMismatchError()
  const payload = value as Record<string, unknown>
  const interaction = payload.interaction
  if (!interaction || typeof interaction !== 'object') throw new AmbientNpcReceiptMismatchError()
  const result = interaction as Record<string, unknown>
  if (
    result.encounter_id !== expected.encounterId
    || result.action_key !== expected.actionKey
    || result.completed !== true
    || typeof result.text !== 'string'
    || !result.text.trim()
  ) throw new AmbientNpcReceiptMismatchError()

  const consumed = result.consumed_item
  if (expected.itemRef) {
    if (!consumed || typeof consumed !== 'object') throw new AmbientNpcReceiptMismatchError()
    const item = consumed as Record<string, unknown>
    if (
      item.quantity !== 1
      || (item.source !== 'carry' && item.source !== 'warehouse')
      || typeof item.id !== 'number'
      || !Number.isInteger(item.id)
      || item.id <= 0
      || typeof item.name !== 'string'
      || !item.name.trim()
      || (
        item.remaining_quantity !== undefined
        && item.remaining_quantity !== null
        && (
          typeof item.remaining_quantity !== 'number'
          || !Number.isInteger(item.remaining_quantity)
          || item.remaining_quantity < 0
        )
      )
      || (item.removed !== undefined && typeof item.removed !== 'boolean')
      || (
        !expected.allowDifferentConsumedItem
        && (
          item.source !== expected.itemRef.source
          || item.id !== expected.itemRef.id
        )
      )
    ) throw new AmbientNpcReceiptMismatchError()
  } else if (consumed !== null) {
    throw new AmbientNpcReceiptMismatchError()
  }

  if (typeof payload.refresh_required !== 'boolean') throw new AmbientNpcReceiptMismatchError()
  const responseEncounter = payload.encounter
  if (payload.refresh_required) {
    if (responseEncounter !== null) throw new AmbientNpcReceiptMismatchError()
  } else {
    if (!responseEncounter || typeof responseEncounter !== 'object') {
      throw new AmbientNpcReceiptMismatchError()
    }
    const current = responseEncounter as Record<string, unknown>
    const npc = current.npc
    const requiredNpcStringFields = [
      'key',
      'name',
      'breed',
      'icon_key',
      'icon',
      'short_intro',
      'presence_text',
    ] as const
    if (
      current.id !== expected.encounterId
      || current.surface_key !== expected.surface
      || !npc
      || typeof npc !== 'object'
      || (npc as Record<string, unknown>).key !== expected.npcKey
      || requiredNpcStringFields.some(field => (
        typeof (npc as Record<string, unknown>)[field] !== 'string'
      ))
      || !isOptionalNullableString(
        (npc as Record<string, unknown>).public_background,
      )
    ) throw new AmbientNpcReceiptMismatchError()
    validateEncounterActions(current.actions, expected.actionKey, result.text)
  }
  if (payload.next_refresh_at !== null && typeof payload.next_refresh_at !== 'string') {
    throw new AmbientNpcReceiptMismatchError()
  }
  return payload as unknown as AmbientNpcInteractionPayload
}

function normalizeStructuredErrorData(value: unknown): AmbientNpcStructuredErrorData | null {
  if (!value || typeof value !== 'object') return null
  const raw = value as Record<string, unknown>
  const disposition = raw.retry_disposition
  if (
    typeof raw.error_code !== 'string'
    || !raw.error_code
    || (
      disposition !== 'retry_same_request'
      && disposition !== 'resolved_by_other_request'
      && disposition !== 'terminal'
    )
    || typeof raw.request_id !== 'string'
    || typeof raw.encounter_id !== 'string'
    || typeof raw.action_key !== 'string'
    || typeof raw.surface !== 'string'
  ) return null
  return {
    error_code: raw.error_code,
    retry_disposition: disposition,
    request_id: raw.request_id,
    encounter_id: raw.encounter_id,
    action_key: raw.action_key,
    surface: raw.surface,
    canonical_payload: raw.canonical_payload,
  }
}

function classifyInteractionError(
  error: unknown,
  pending: AmbientNpcPendingAction,
  expected: { surface: string; npcKey: string },
): AmbientNpcErrorResolution {
  const response = (error as {
    response?: { status?: unknown; data?: unknown }
  })?.response
  const status = Number(response?.status)
  const envelope = response?.data && typeof response.data === 'object'
    ? response.data as Record<string, unknown>
    : null
  const rawDetails = envelope?.data
  const hasStructuredMarker = Boolean(
    rawDetails
    && typeof rawDetails === 'object'
    && (
      'error_code' in (rawDetails as Record<string, unknown>)
      || 'retry_disposition' in (rawDetails as Record<string, unknown>)
    )
  )
  if (hasStructuredMarker) {
    const details = normalizeStructuredErrorData(rawDetails)
    if (
      !details
      || !Number.isInteger(status)
      || status < 400
      || status >= 500
      || details.request_id !== pending.requestId
      || details.encounter_id !== pending.encounterId
      || details.action_key !== pending.actionKey
      || details.surface !== pending.surface
      || details.surface !== expected.surface
    ) return { kind: 'unknown' }
    if (details.retry_disposition === 'retry_same_request') {
      return { kind: 'retry_same_request' }
    }
    if (details.retry_disposition === 'terminal') {
      return { kind: 'terminal' }
    }
    if (
      details.error_code !== 'ambient_npc_action_completed_by_other_request'
      || details.canonical_payload == null
    ) return { kind: 'unknown' }
    try {
      return {
        kind: 'resolved_by_other_request',
        payload: validateInteractionPayload(details.canonical_payload, {
          encounterId: pending.encounterId,
          actionKey: pending.actionKey,
          surface: expected.surface,
          npcKey: expected.npcKey,
          itemRef: pending.item
            ? { source: pending.item.source, id: pending.item.id }
            : null,
          allowDifferentConsumedItem: true,
        }),
      }
    } catch {
      return { kind: 'unknown' }
    }
  }

  // 路由内业务拒绝均有结构化处置；这里只为路由前的认证/限流保留明确终态。
  if ([401, 403, 404, 410, 422, 429].includes(status)) return { kind: 'terminal' }
  return { kind: 'unknown' }
}

async function fetchCurrentPayload(
  userId: number,
  surface: string,
  force: boolean,
): Promise<AmbientNpcCurrentPayload> {
  const key = currentKey(userId, surface)
  const cached = currentCache.get(key)
  if (!force && cached && cached.expiresAt > Date.now()) return cached.payload

  const epoch = epochFor(key)
  const existing = currentInFlight.get(key)
  if (existing?.epoch === epoch) return existing.promise

  const pending = (async () => {
    const { data } = await api.get('/adventurer-s1/ambient-npcs/current', {
      params: { surface },
    })
    if (data.code !== 200 || !data.data) {
      throw new Error(data.message || '偶遇加载失败')
    }
    const payload = data.data as AmbientNpcCurrentPayload
    if (epochFor(key) === epoch) {
      currentCache.set(key, { payload, expiresAt: payloadExpiry(payload) })
    }
    return payload
  })()
  currentInFlight.set(key, { epoch, promise: pending })
  try {
    const payload = await pending
    if (epochFor(key) !== epoch) {
      return fetchCurrentPayload(userId, surface, true)
    }
    return payload
  } finally {
    if (currentInFlight.get(key)?.promise === pending) currentInFlight.delete(key)
  }
}

function invalidateUserCache(userId: number) {
  const prefix = `${userId}:`
  const knownKeys = new Set([
    ...currentCache.keys(),
    ...currentInFlight.keys(),
    ...currentEpoch.keys(),
  ])
  for (const key of knownKeys) {
    if (!key.startsWith(prefix)) continue
    bumpEpoch(key)
    currentCache.delete(key)
  }
}

function publishMutation(event: AmbientNpcMutationEvent) {
  invalidateUserCache(event.userId)
  bumpEpoch(currentKey(event.userId, event.surface))
  if (event.payload.encounter) {
    currentCache.set(currentKey(event.userId, event.surface), {
      payload: event.payload,
      expiresAt: payloadExpiry(event.payload),
    })
  }
  for (const listener of mutationListeners) listener(event)
}

export function useAmbientNpcEncounter(surface: Ref<string | null>) {
  ensurePendingActionSync()
  const userStore = useUserStore()
  const adventurerStore = useAdventurerS1Store()
  const encounter = ref<AmbientNpcEncounterView | null>(null)
  const loading = ref(false)
  const loadError = ref('')
  const busyActionKey = ref<string | null>(null)
  const itemOptions = ref<AmbientNpcItemOption[]>([])
  const itemOptionsLoading = ref(false)
  const itemOptionsError = ref('')
  const pendingActions = ref<AmbientNpcPendingAction[]>([])

  let requestSequence = 0
  let itemRequestSequence = 0
  let actionRequestSequence = 0
  let debounceTimer: ReturnType<typeof setTimeout> | null = null
  let refreshTimer: ReturnType<typeof setTimeout> | null = null

  function clearTimers() {
    if (debounceTimer) clearTimeout(debounceTimer)
    if (refreshTimer) clearTimeout(refreshTimer)
    debounceTimer = null
    refreshTimer = null
  }

  function refreshPendingActions() {
    const activeUserId = userStore.user?.id
    const activeEncounter = encounter.value
    const activeSurface = surface.value
    if (!activeUserId || !activeEncounter || !activeSurface) {
      pendingActions.value = []
      return
    }
    pendingActions.value = activeEncounter.actions
      .map((action) => {
        const pending = getPendingAction(activeUserId, activeEncounter.id, action.key)
        if (pending && (
          pending.surface !== activeSurface
          || pending.surface !== activeEncounter.surface_key
          || action.requires_item !== Boolean(pending.item)
        )) {
          clearPendingAction(pending)
          return null
        }
        return pending
      })
      .filter((pending): pending is AmbientNpcPendingAction => pending !== null)
  }

  function scheduleRefresh(payload: AmbientNpcCurrentPayload) {
    if (refreshTimer) clearTimeout(refreshTimer)
    const delay = Math.max(1_000, Math.min(2_147_000_000, payloadExpiry(payload) - Date.now() + 150))
    refreshTimer = setTimeout(() => void loadCurrent(true), delay)
  }

  async function loadCurrent(force = false) {
    const activeSurface = surface.value
    const activeUserId = userStore.user?.id
    itemOptions.value = []
    itemOptionsError.value = ''
    if (!activeSurface || !activeUserId) {
      encounter.value = null
      pendingActions.value = []
      return
    }
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return
    const sequence = ++requestSequence

    loading.value = true
    loadError.value = ''
    try {
      const payload = await fetchCurrentPayload(activeUserId, activeSurface, force)
      if (
        sequence !== requestSequence
        || surface.value !== activeSurface
        || userStore.user?.id !== activeUserId
      ) return
      encounter.value = payload.encounter
      refreshPendingActions()
      scheduleRefresh(payload)
    } catch (error) {
      if (sequence === requestSequence) {
        encounter.value = null
        pendingActions.value = []
        loadError.value = apiErrorMessage(error, '偶遇加载失败')
      }
    } finally {
      if (sequence === requestSequence) loading.value = false
    }
  }

  function scheduleLoad() {
    requestSequence += 1
    itemRequestSequence += 1
    actionRequestSequence += 1
    encounter.value = null
    itemOptions.value = []
    itemOptionsLoading.value = false
    itemOptionsError.value = ''
    pendingActions.value = []
    busyActionKey.value = null
    clearTimers()
    debounceTimer = setTimeout(() => void loadCurrent(), 180)
  }

  async function loadItems(action: AmbientNpcActionView) {
    const activeEncounter = encounter.value
    const activeSurface = surface.value
    const activeUserId = userStore.user?.id
    if (
      !activeEncounter
      || !activeSurface
      || !activeUserId
      || !action.requires_item
    ) return
    const locked = getPendingAction(activeUserId, activeEncounter.id, action.key)
    if (locked) {
      refreshPendingActions()
      itemOptionsLoading.value = false
      itemOptionsError.value = locked.item
        ? ''
        : '上一次互动仍在核对，但原物品信息已不可用'
      itemOptions.value = locked.item ? [{ ...locked.item }] : []
      return
    }
    if (action.completed || action.available === false) return
    const sequence = ++itemRequestSequence
    itemOptionsLoading.value = true
    itemOptionsError.value = ''
    itemOptions.value = []
    try {
      const { data } = await api.get(
        `/adventurer-s1/ambient-npcs/${activeEncounter.id}/actions/${action.key}/items`,
        { params: { surface: activeSurface } },
      )
      if (data.code !== 200 || !data.data) {
        throw new Error(data.message || '可赠送物品加载失败')
      }
      if (
        sequence !== itemRequestSequence
        || surface.value !== activeSurface
        || encounter.value?.id !== activeEncounter.id
      ) return
      itemOptions.value = (Array.isArray(data.data.items) ? data.data.items : [])
        .map(normalizeItemOption)
        .filter((item: AmbientNpcItemOption | null): item is AmbientNpcItemOption => (
          item !== null
        ))
    } catch (error) {
      if (sequence === itemRequestSequence) {
        itemOptionsError.value = apiErrorMessage(error, '可赠送物品加载失败')
      }
    } finally {
      if (sequence === itemRequestSequence) itemOptionsLoading.value = false
    }
  }

  function patchCompletedAction(result: AmbientNpcInteractionResult) {
    if (!encounter.value || encounter.value.id !== result.encounter_id) return
    encounter.value = {
      ...encounter.value,
      actions: encounter.value.actions.map(action => (
        action.key === result.action_key
          ? { ...action, completed: true, last_text: result.text }
          : action
      )),
    }
  }

  function refreshInventoryAfterConfirmedConsume(
    userId: number,
    payload: AmbientNpcInteractionPayload,
  ) {
    if (!payload.interaction.consumed_item || userStore.user?.id !== userId) return
    void (async () => {
      try {
        await adventurerStore.fetchAdventurers(true)
      } finally {
        // 第一次可能复用赠礼前已在途的请求；再拉一次才能保证是写后快照。
        await adventurerStore.fetchAdventurers(true)
      }
    })().catch(() => {
      // 互动已有权威回执；库存刷新失败不应把已完成操作误报为失败。
    })
  }

  async function performAction(
    action: AmbientNpcActionView,
    item?: AmbientNpcItemOption,
  ): Promise<AmbientNpcInteractionResult | null> {
    const activeEncounter = encounter.value
    const activeSurface = surface.value
    const activeUserId = userStore.user?.id
    const existingPending = activeEncounter && activeUserId
      ? getPendingAction(activeUserId, activeEncounter.id, action.key)
      : null
    if (
      !activeEncounter
      || !activeSurface
      || !activeUserId
      || ((action.completed || action.available === false) && !existingPending)
      || busyActionKey.value
    ) return null
    if (action.requires_item && !item) {
      throw new Error('请先选择要送出的物品')
    }
    if (!action.requires_item && item) {
      throw new Error('这个互动不接受物品')
    }
    const claim = await claimPendingAction({
      userId: activeUserId,
      encounterId: activeEncounter.id,
      actionKey: action.key,
      surface: activeSurface,
      item: item || null,
    })
    const pending = claim.pending
    if (
      surface.value !== activeSurface
      || encounter.value?.id !== activeEncounter.id
      || userStore.user?.id !== activeUserId
    ) {
      if (claim.created) clearPendingAction(pending)
      return null
    }
    const sequence = ++actionRequestSequence
    busyActionKey.value = action.key
    try {
      const body: Record<string, unknown> = {
        surface: activeSurface,
        request_id: pending.requestId,
        action_input: pending.item
          ? { item_ref: { source: pending.item.source, id: pending.item.id } }
          : {},
      }
      const { data } = await api.post(
        `/adventurer-s1/ambient-npcs/${activeEncounter.id}/actions/${action.key}`,
        body,
        {
          headers: { 'Idempotency-Key': pending.requestId },
          timeout: AMBIENT_NPC_ACTION_TIMEOUT_MS,
        },
      )
      if (data.code !== 200) {
        throw new AmbientNpcApiEnvelopeError(Number(data.code), data)
      }
      const payload = validateInteractionPayload(data.data, {
        encounterId: activeEncounter.id,
        actionKey: action.key,
        surface: activeSurface,
        npcKey: activeEncounter.npc.key,
        itemRef: pending.item
          ? { source: pending.item.source, id: pending.item.id }
          : null,
      })
      clearPendingAction(pending)
      publishMutation({
        userId: activeUserId,
        surface: activeSurface,
        npcKey: activeEncounter.npc.key,
        payload,
      })
      if (
        surface.value === activeSurface
        && encounter.value?.id === activeEncounter.id
      ) {
        itemOptions.value = []
      }
      refreshInventoryAfterConfirmedConsume(activeUserId, payload)
      return payload.interaction
    } catch (error) {
      const resolution = classifyInteractionError(error, pending, {
        surface: activeSurface,
        npcKey: activeEncounter.npc.key,
      })
      if (resolution.kind === 'resolved_by_other_request') {
        clearPendingAction(pending)
        publishMutation({
          userId: activeUserId,
          surface: activeSurface,
          npcKey: activeEncounter.npc.key,
          payload: resolution.payload,
        })
        refreshInventoryAfterConfirmedConsume(activeUserId, resolution.payload)
        if (
          surface.value === activeSurface
          && encounter.value?.id === activeEncounter.id
          && userStore.user?.id === activeUserId
        ) itemOptions.value = []
        // 409 仍不是本 request 的成功；这里只把已校验的 canonical interaction
        // 返回给组件用于关闭旧确认 UI，权威状态已经通过 publishMutation 单独采纳。
        return resolution.payload.interaction
      }
      if (resolution.kind === 'terminal') {
        clearPendingAction(pending)
        if (
          surface.value === activeSurface
          && encounter.value?.id === activeEncounter.id
          && userStore.user?.id === activeUserId
        ) void loadCurrent(true)
        throw new AmbientNpcTerminalActionError(
          apiErrorMessage(error, '这次互动未能完成，请稍后重新尝试'),
        )
      }
      // retry_same_request、超时、断网、5xx、未知枚举与回执校验失败均保留原礼物/request_id。
      throw error
    } finally {
      if (sequence === actionRequestSequence) busyActionKey.value = null
    }
  }

  function onVisibilityChange() {
    if (typeof document !== 'undefined' && document.visibilityState === 'visible') {
      void loadCurrent()
    }
  }

  function onMutation(event: AmbientNpcMutationEvent) {
    const activeSurface = surface.value
    if (!activeSurface || userStore.user?.id !== event.userId) return
    if (activeSurface === event.surface) {
      if (event.payload.refresh_required) {
        patchCompletedAction(event.payload.interaction)
        if (refreshTimer) clearTimeout(refreshTimer)
        refreshTimer = setTimeout(() => void loadCurrent(true), 2_500)
        return
      }
      if (event.payload.encounter?.id === event.payload.interaction.encounter_id) {
        encounter.value = event.payload.encounter
      } else {
        patchCompletedAction(event.payload.interaction)
      }
      refreshPendingActions()
      loadError.value = ''
      scheduleRefresh(event.payload)
      return
    }
    if (encounter.value?.npc.key === event.npcKey) void loadCurrent(true)
  }

  function onPendingActionMutation(event: PendingActionMutationEvent) {
    const activeEncounter = encounter.value
    if (
      userStore.user?.id !== event.userId
      || !activeEncounter
      || activeEncounter.id !== event.encounterId
    ) return
    refreshPendingActions()
    if (event.external && event.kind === 'remove') void loadCurrent(true)
  }

  function playerAdventurerScope(): string {
    return adventurerStore.adventurers
      .filter(adventurer => adventurer.is_player_created)
      .map(adventurer => `${adventurer.id}:${adventurer.status}`)
      .sort()
      .join('|')
  }

  function onActorScopeChange() {
    const userId = userStore.user?.id
    if (userId) invalidateUserCache(userId)
    scheduleLoad()
  }

  mutationListeners.add(onMutation)
  pendingActionListeners.add(onPendingActionMutation)
  watch(surface, scheduleLoad, { immediate: true })
  watch(
    [() => userStore.user?.id ?? null, playerAdventurerScope],
    onActorScopeChange,
    { immediate: true },
  )
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibilityChange)
  }
  onUnmounted(() => {
    requestSequence += 1
    clearTimers()
    mutationListeners.delete(onMutation)
    pendingActionListeners.delete(onPendingActionMutation)
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibilityChange)
    }
  })

  return {
    encounter,
    loading,
    loadError,
    busyActionKey,
    itemOptions,
    itemOptionsLoading,
    itemOptionsError,
    pendingActions,
    loadCurrent,
    loadItems,
    performAction,
  }
}
