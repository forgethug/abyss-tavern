/** 管理员新建/编辑物品表单的共享逻辑（图鉴、大转盘、拍卖行等复用）。 */

export const DEFAULT_USES_LEFT_KEY = 'default_uses_left'

export interface CatalogUseConditionFormSlice {
  text_locked: boolean
  uc_enabled: boolean
  uc_dungeon: string
  uc_target_class: string
  uses_left: number | null
}

/** 从图鉴 use_condition 或 API default_uses_left 读取默认使用次数。 */
export function extractDefaultUsesLeft(
  uc?: Record<string, unknown> | null,
  explicit?: number | null,
): number | null {
  if (explicit != null && !Number.isNaN(explicit)) return explicit
  if (uc && uc[DEFAULT_USES_LEFT_KEY] !== undefined) {
    const n = Number(uc[DEFAULT_USES_LEFT_KEY])
    return Number.isFinite(n) ? n : null
  }
  return null
}

/** 写入图鉴 use_condition.default_uses_left（null/空 = 清除，走类型默认）。 */
export function mergeDefaultUsesLeftIntoCondition(
  cond: Record<string, unknown> | null | undefined,
  usesLeft: number | null | undefined,
): Record<string, unknown> | null {
  const next: Record<string, unknown> = { ...(cond || {}) }
  if (usesLeft == null || Number.isNaN(usesLeft) || usesLeft < 0) {
    delete next[DEFAULT_USES_LEFT_KEY]
  } else {
    next[DEFAULT_USES_LEFT_KEY] = usesLeft
  }
  return Object.keys(next).length ? next : null
}

/** 根据表单生成图鉴 use_condition（含 default_uses_left）。 */
export function buildCatalogUseCondition(form: CatalogUseConditionFormSlice): Record<string, unknown> | null {
  const cond: Record<string, unknown> = {}
  if (form.text_locked) cond.immutable = true
  if (form.uc_enabled) {
    const dungeon = form.uc_dungeon.trim()
    if (dungeon) {
      cond.expedition_dungeon = dungeon
      const targetClass = form.uc_target_class.trim()
      if (targetClass) cond.target_class = targetClass
    }
  }
  return mergeDefaultUsesLeftIntoCondition(Object.keys(cond).length ? cond : null, form.uses_left)
}

/** 从图鉴 use_condition 回填表单（含 uses_left）。 */
export function parseCatalogUseConditionToForm(
  uc: Record<string, unknown> | null | undefined,
  form: CatalogUseConditionFormSlice,
) {
  form.text_locked = uc?.immutable === true || uc?.text_locked === true
  if (uc && typeof uc === 'object' && uc.expedition_dungeon) {
    form.uc_enabled = true
    form.uc_dungeon = String(uc.expedition_dungeon)
    form.uc_target_class = uc.target_class ? String(uc.target_class) : ''
  } else {
    form.uc_enabled = false
    form.uc_dungeon = ''
    form.uc_target_class = ''
  }
  form.uses_left = extractDefaultUsesLeft(uc)
}

/** API payload：显式设置 uses_left 时写入（留空则不传，后端按类型默认）。 */
export function appendUsesLeftPayload(
  payload: Record<string, unknown>,
  usesLeft: number | null | undefined,
) {
  if (usesLeft != null && !Number.isNaN(usesLeft) && usesLeft >= 0) {
    payload.uses_left = usesLeft
  }
}
