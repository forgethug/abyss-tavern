<template>
  <div class="modal-mask s1-mobile-surface s1-mobile-overlay" @click.self="$emit('close')">
    <div class="s1-mobile-dialog" :class="['modal-box', 'room-level-' + displayLevel]">
      <template v-if="room">
        <div class="room-modal-head s1-mobile-dialog-header">
          <span class="room-modal-icon">{{ dormRoomIcon(room) }}</span>
          <div class="room-modal-titles">
            <h4 class="room-modal-name">{{ room.name }}</h4>
            <div class="room-modal-badges">
              <span class="level-badge">Lv.{{ displayLevel }} {{ levelInfo.name }}</span>
              <span class="cap-badge">🛏️ {{ occupiedCount }}/{{ room.capacity }}</span>
            </div>
          </div>
          <button type="button" class="room-close" aria-label="关闭宿舍详情" @click="$emit('close')">✕</button>
        </div>
        <p class="room-modal-desc">
          {{ levelInfo.desc }}
        </p>

        <!-- 建造完工命名权 -->
        <div v-if="canNameBuild" class="room-naming">
          <template v-if="naming && namingKind === 'build'">
            <input
              v-model="namingInput"
              class="naming-input"
              maxlength="20"
              placeholder="给这栋楼起个名字（≤20字）"
              @keyup.enter="doNameRoom"
            />
            <div class="naming-actions">
              <button
                type="button"
                class="btn-naming-confirm"
                :disabled="namingSubmitting || !namingInput.trim()"
                @click="doNameRoom"
              >{{ namingSubmitting ? '提交中…' : '确认' }}</button>
              <button type="button" class="btn-naming-cancel" @click="naming = false">取消</button>
            </div>
          </template>
          <button v-else type="button" class="btn-naming" @click="startNaming('build')">
            ✒️ 你是这栋楼的最大贡献者，点击命名
          </button>
        </div>

        <!-- Lv2 升级完工命名权（在住客中升级贡献最高） -->
        <div v-if="canNameUpgrade" class="room-naming room-naming-upgrade">
          <template v-if="naming && namingKind === 'upgrade'">
            <input
              v-model="namingInput"
              class="naming-input"
              maxlength="20"
              placeholder="升级后重新命名（≤20字）"
              @keyup.enter="doNameRoom"
            />
            <div class="naming-actions">
              <button
                type="button"
                class="btn-naming-confirm"
                :disabled="namingSubmitting || !namingInput.trim()"
                @click="doNameRoom"
              >{{ namingSubmitting ? '提交中…' : '确认' }}</button>
              <button type="button" class="btn-naming-cancel" @click="naming = false">取消</button>
            </div>
          </template>
          <button v-else type="button" class="btn-naming btn-naming-upgrade" @click="startNaming('upgrade')">
            ✒️ 你是本次升级的最大贡献住客，点击重新命名
          </button>
        </div>

        <div class="beds-section">
          <h5 class="section-title">床位布局</h5>
          <div v-if="swapMode && swapFirst" class="swap-hint-inline">
            已选：{{ swapFirst.name }}（空床位可直接搬，别人床位需对方同意）
          </div>
          <div class="room-beds">
            <div
              v-for="(bed, idx) in room.beds"
              :key="idx"
              class="bed-slot"
              :class="{
                occupied: bed !== null,
                'swap-empty-target': swapMode && !bed,
                'swap-other-target': swapMode && bed && bed.adventurer_id !== swapFirst?.id,
                selected: swapFirst && bed && bed.adventurer_id === swapFirst.id,
              }"
              @click="$emit('bedClick', bed, room.id, idx)"
            >
              <template v-if="bed">
                <span class="bed-icon">{{ isBedHome(bed.status, bed.tavern_seated) ? '🏠' : '🚪' }}</span>
                <span class="bed-name">{{ bed.adventurer_name }}</span>
                <small :class="['bed-status', isBedHome(bed.status, bed.tavern_seated) ? 'home' : 'away']">
                  {{ isBedHome(bed.status, bed.tavern_seated) ? '在家' : statusLabel(bed.status, bed.building_instance_status, undefined, bed.tavern_seated) }}
                </small>
                <button
                  v-if="isUpgrading && bed.status === 'building' && canRecallUpgradeHelper(bed)"
                  type="button"
                  class="btn-recall btn-recall-bed"
                  :disabled="recallingId === bed.adventurer_id"
                  @click.stop="recallUpgradeHelper(bed.adventurer_id)"
                >
                  {{ recallingId === bed.adventurer_id ? '召回中…' : '召回' }}
                </button>
              </template>
              <template v-else>
                <span class="bed-icon empty">🛏️</span>
                <span class="bed-name empty">
                  空床位
                </span>
              </template>
            </div>
          </div>
        </div>

        <div v-if="showUpgradeSection" class="room-upgrade-section">
          <h5 class="section-title">⬆️ 宿舍升级</h5>
          <div class="room-upgrade-level">
            当前 Lv.{{ buildingLevel }}
            <span v-if="buildingLevel < 2">→ Lv.{{ buildingLevel + 1 }}</span>
          </div>
          <p v-if="room.upgrade_description" class="room-upgrade-hint">{{ room.upgrade_description }}</p>
          <p v-if="showUpgradeLock" class="room-upgrade-lock">
            🔒 需管理员解锁酒馆 Lv.2（全服存活冒险者达 {{ tavernUpgradeRequirement }} 名）
          </p>

          <template v-else-if="isUpgrading">
            <p class="room-upgrade-hint">升级期间在宿舍床位上的冒险者暂停恢复精力、生命与特质；外出、酒馆等地不受影响。</p>
            <div class="room-upgrade-progress">
              <div class="room-upgrade-progress-head">
                <span>升级进度 {{ liveUpgradeProgress }}%</span>
                <span v-if="(room.upgrade_helpers?.length ?? 0) > 0">
                  · 帮工 {{ room.upgrade_helpers?.length }}/{{ room.upgrade_helper_slots ?? 2 }}
                </span>
              </div>
              <div class="room-upgrade-progress-bar">
                <div class="room-upgrade-progress-fill" :style="{ width: liveUpgradeProgress + '%' }" />
              </div>
              <div class="room-upgrade-remaining">剩余：{{ displayUpgradeRemaining }}</div>
            </div>
            <div class="upgrade-helpers-section">
              <h5 class="section-title">在位冒险者（{{ room.upgrade_helpers?.length ?? 0 }}/{{ room.upgrade_helper_slots ?? 2 }}）</h5>
              <div v-if="room.upgrade_helpers && room.upgrade_helpers.length > 0" class="helpers-list">
                <div v-for="h in room.upgrade_helpers" :key="h.adventurer_id" class="helper-item">
                  <div class="helper-info">
                    <span class="helper-name">{{ h.adventurer_name }}</span>
                  </div>
                  <button
                    v-if="canRecallUpgradeHelper(h)"
                    type="button"
                    class="btn-recall"
                    :disabled="recallingId === h.adventurer_id"
                    @click="recallUpgradeHelper(h.adventurer_id)"
                  >
                    {{ recallingId === h.adventurer_id ? '召回中…' : '召回' }}
                  </button>
                </div>
              </div>
              <div v-else class="empty-helpers">暂无冒险者在位升级</div>
            </div>
            <button
              type="button"
              class="btn-upgrade"
              :disabled="helping"
              @click="doHelpUpgrade"
            >{{ helping ? '派遣中…' : '🤝 派遣帮工' }}</button>
          </template>

          <template v-else>
            <div v-if="upgradeMaterialEntries.length" class="donate-list">
              <div
                v-for="[mat, need] in upgradeMaterialEntries"
                :key="mat"
                class="donate-row"
              >
                <div class="donate-row-head">
                  <span class="donate-mat-icon">{{ matIcon(mat) }}</span>
                  <span class="donate-mat-name">{{ matLabel(mat) }}</span>
                  <span class="donate-mat-progress">
                    {{ room.upgrade_donations_summary?.[mat] ?? 0 }} / {{ need }}
                    <span v-if="(room.my_upgrade_donations?.[mat] ?? 0) > 0" class="donate-mine">
                      （我 {{ room.my_upgrade_donations?.[mat] }}）
                    </span>
                  </span>
                </div>
                <div v-if="(room.upgrade_donations_summary?.[mat] ?? 0) < need" class="donate-form">
                  <span v-if="myMaterialBalance(mat) > 0" class="donate-have">
                    持有 {{ myMaterialBalance(mat) }}
                  </span>
                  <span v-else class="donate-empty">无库存</span>
                  <input
                    v-model.number="donateAmounts[mat]"
                    type="number"
                    min="1"
                    :max="Math.min(myMaterialBalance(mat), need - (room.upgrade_donations_summary?.[mat] ?? 0))"
                    class="donate-input"
                    placeholder="数量"
                  />
                  <button
                    type="button"
                    class="btn-donate"
                    :disabled="donating || !donateAmounts[mat] || donateAmounts[mat]! <= 0"
                    @click="submitDonate(mat)"
                  >{{ donating ? '…' : '捐献' }}</button>
                </div>
                <div v-else class="donate-full">✅ 已捐满</div>
              </div>
            </div>
            <button
              v-if="canStartUpgrade"
              type="button"
              class="btn-upgrade"
              :disabled="upgrading"
              @click="doUpgrade"
            >{{ upgrading ? '启动中…' : `🔨 开始升级至 Lv.${buildingLevel + 1}` }}</button>
            <p
              v-else-if="room.can_start_upgrade && room.building_status === 'built' && !myAdventurerLivesHere"
              class="room-upgrade-hint"
            >只有住在该宿舍的冒险者才能申请升级</p>
            <p v-else class="room-upgrade-hint">材料捐满后，由在住冒险者申请升级并派遣帮工</p>
          </template>
        </div>

        <div class="room-modal-actions s1-mobile-dialog-footer">
          <S1MoreActions
            v-if="room.building_instance_id || (showUpgradeSection && !showUpgradeLock && isUpgrading && isAdmin)"
            :key="room.id"
          >
            <button
              v-if="room.building_instance_id"
              type="button"
              class="btn-contributors"
              @click="showContributors = true"
            >📊 贡献者</button>
            <button
              v-if="showUpgradeSection && !showUpgradeLock && isUpgrading && isAdmin"
              type="button"
              class="btn-gm"
              :disabled="completingUpgrade"
              @click="completeUpgrade"
            >{{ completingUpgrade ? '完成中…' : '⏩ GM 一键升级完成' }}</button>
          </S1MoreActions>
          <button type="button" class="btn-cancel" @click="$emit('close')">关闭</button>
        </div>
      </template>
      <template v-else>
        <div class="room-missing">房间不存在或已被移除</div>
        <div class="room-modal-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-cancel" @click="$emit('close')">关闭</button>
        </div>
      </template>

      <ContributorsModal
        v-if="showContributors && room?.building_instance_id"
        :building-id="room.building_instance_id"
        show-naming-crown
        @close="showContributors = false"
      />
    </div>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, reactive } from 'vue'
import { useAdventurerS1Store, type DormBed, type DormRoom } from '../stores/adventurerS1'
import { useUserStore } from '@/stores/user'
import { useAdminSecondaryAccess } from '@/composables/useAdminSecondaryAccess'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useBuildingConstructionProgress } from '../composables/useBuildingConstructionProgress'
import {
  dormLevelInfo,
  dormDisplayLevel,
  dormRoomIcon,
  isBedHome,
  statusLabel,
  formatBuildingRemaining,
  type BuildingProgressSnapshot,
} from '../utils'
import ContributorsModal from './ContributorsModal.vue'
import S1MoreActions from './S1MoreActions.vue'

const props = defineProps<{
  roomId: number
  swapMode: boolean
  swapFirst: { id: number; name: string } | null
  currentUserId: number | null
}>()

const emit = defineEmits<{
  close: []
  bedClick: [bed: DormBed | null, roomId: number, bedIndex: number]
}>()

const store = useAdventurerS1Store()
const userStore = useUserStore()
const message = useGlobalMessage()
const { canUseFullAdminUi: isAdmin } = useAdminSecondaryAccess()

// 从 store 反查房间：互换/升级/命名后 fetchDormitory 刷新，弹窗内容跟着更新
const room = computed<DormRoom | undefined>(() => store.dormitory.find(r => r.id === props.roomId))
const displayLevel = computed(() => dormDisplayLevel(room.value ?? { level: 1 }))
const levelInfo = computed(() => dormLevelInfo(displayLevel.value))
const occupiedCount = computed(() => room.value?.beds.filter(b => b !== null).length ?? 0)
const canNameBuild = computed(() =>
  !!room.value && room.value.naming_user_id === props.currentUserId && !room.value.naming_used
)
const canNameUpgrade = computed(() =>
  !!room.value
  && room.value.upgrade_naming_user_id === props.currentUserId
  && !room.value.upgrade_naming_used
)
const buildingLevel = computed(() => room.value?.upgrade_level ?? 1)
const buildingUpgradeUnlocked = computed(() => !!store.tavernUpgrade?.building_upgrade_unlocked)
const tavernUpgradeRequirement = computed(() => store.tavernUpgrade?.next_level_requirement ?? 72)
const showUpgradeSection = computed(() =>
  buildingLevel.value < 2
)
const showUpgradeLock = computed(() => showUpgradeSection.value && !buildingUpgradeUnlocked.value)
const isUpgrading = computed(() => room.value?.building_status === 'upgrading')
const upgradeProgressSource = computed((): BuildingProgressSnapshot => {
  const r = room.value
  if (!r || r.building_status !== 'upgrading') {
    return {
      status: 'built',
      remaining_seconds: 0,
      total_build_seconds: 0,
      progress_percent: 0,
      build_complete_at: null,
      helpers: [],
      template: { speedup_minutes: 60 },
    }
  }
  return {
    status: 'upgrading',
    remaining_seconds: r.upgrade_remaining_seconds ?? 0,
    total_build_seconds: r.upgrade_total_seconds ?? 0,
    progress_percent: r.upgrade_progress_percent ?? 0,
    build_complete_at: null,
    upgrade_complete_at: r.upgrade_complete_at ?? null,
    helpers: (r.upgrade_helpers ?? []).map((helper) => ({ ...helper, stamina: null })),
    template: { speedup_minutes: r.upgrade_speedup_minutes ?? 60 },
  }
})
const {
  liveRemaining: liveUpgradeRemaining,
  liveProgress: liveUpgradeProgress,
  isProgressPaused: upgradeProgressPaused,
} = useBuildingConstructionProgress(upgradeProgressSource)
const displayUpgradeRemaining = computed(() => {
  const text = formatBuildingRemaining(liveUpgradeRemaining.value)
  if (upgradeProgressPaused.value) return `${text}（无人帮忙，暂停）`
  return text
})
const myAdventurerLivesHere = computed(() => {
  if (!room.value || !props.currentUserId) return false
  return room.value.beds.some(b => b !== null && b.owner_id === props.currentUserId)
})
const canStartUpgrade = computed(() =>
  myAdventurerLivesHere.value
  && !!room.value?.can_start_upgrade
  && room.value?.building_status === 'built'
)
const upgradeMaterialEntries = computed(() =>
  Object.entries(room.value?.upgrade_required_materials ?? {})
)

const donateAmounts = reactive<Record<string, number | null>>({})
const donating = ref(false)
const upgrading = ref(false)
const helping = ref(false)
const completingUpgrade = ref(false)
const recallingId = ref<number | null>(null)

const MAT_META: Record<string, { icon: string; label: string }> = {
  wood: { icon: '🪵', label: '木材' },
  stone: { icon: '🪨', label: '石材' },
  iron_ingot: { icon: '🔩', label: '铁锭' },
}

function matIcon(mat: string): string {
  return MAT_META[mat]?.icon ?? '📦'
}

function matLabel(mat: string): string {
  return MAT_META[mat]?.label ?? mat
}

function myMaterialBalance(mat: string): number {
  return Number(store.materialBalances?.[mat] ?? store.dormitoryMyStatus?.my_materials?.[mat] ?? 0)
}

async function submitDonate(mat: string) {
  if (!room.value) return
  const amount = donateAmounts[mat]
  if (!amount || amount <= 0 || donating.value) return
  donating.value = true
  try {
    const res = await store.donateToDormRoomUpgrade(room.value.id, mat, amount)
    if (res.code === 200) {
      message.success(`捐献 ${res.data?.donated_amount ?? amount} ${matLabel(mat)}`)
      donateAmounts[mat] = null
      store.fetchMyMaterials()
      store.fetchDormitory(true)
    } else {
      message.error(res.message || '捐献失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '捐献失败')
  } finally {
    donating.value = false
  }
}

const showContributors = ref(false)

// ── 命名权 ──
const naming = ref(false)
const namingKind = ref<'build' | 'upgrade'>('build')
const namingInput = ref('')
const namingSubmitting = ref(false)

function startNaming(kind: 'build' | 'upgrade') {
  namingKind.value = kind
  naming.value = true
  namingInput.value = ''
}

async function doNameRoom() {
  if (!room.value) return
  const name = namingInput.value.trim()
  if (!name || namingSubmitting.value) return
  namingSubmitting.value = true
  try {
    const res = await store.nameDormRoom(room.value.id, name, namingKind.value)
    if (res.code === 200) {
      message.success(`已命名为「${name}」`)
      naming.value = false
      store.fetchDormitory(true)
    } else {
      message.error(res.message || '命名失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '命名失败')
  } finally {
    namingSubmitting.value = false
  }
}

// ── 开始升级 ──
async function doUpgrade() {
  if (!room.value || upgrading.value || !canStartUpgrade.value) return
  upgrading.value = true
  try {
    const res = await store.upgradeRoom(room.value.id, room.value.name)
    if (!res) return
    if (res.code === 200) {
      message.success(`已开始升级至 Lv.${buildingLevel.value + 1}，可继续派遣帮工加速`)
      store.fetchDormitory(true)
    } else {
      message.error(res.message || '升级失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '升级失败')
  } finally {
    upgrading.value = false
  }
}

function canRecallUpgradeHelper(h: { owner_id?: number }): boolean {
  if (isAdmin.value) return true
  const myId = userStore.user?.id
  return myId != null && h.owner_id === myId
}

async function recallUpgradeHelper(adventurerId: number) {
  recallingId.value = adventurerId
  try {
    const res = await store.recallFromBuilding(adventurerId)
    if (res.code === 200) {
      message.success('已召回')
      await store.fetchDormitory(true)
      await store.fetchAdventurers()
    } else {
      message.error(res.message || '召回失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '召回失败')
  } finally {
    recallingId.value = null
  }
}

async function doHelpUpgrade() {
  if (!room.value || helping.value) return
  helping.value = true
  try {
    const res = await store.helpDormRoomUpgrade(room.value.id)
    if (res.code === 200) {
      message.success(res.data?.message || '已派遣帮工')
      store.fetchDormitory(true)
    } else {
      message.error(res.message || '派遣失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '派遣失败')
  } finally {
    helping.value = false
  }
}

async function completeUpgrade() {
  if (!room.value?.building_instance_id || completingUpgrade.value) return
  if (!confirm('确定要 GM 一键完成升级吗？将立即跳过剩余升级时间并提升至下一等级。')) return
  completingUpgrade.value = true
  try {
    const res = await store.completeBuilding(room.value.building_instance_id)
    if (res.code === 200) {
      message.success('升级已完成')
      store.fetchDormitory(true)
      emit('close')
    } else {
      message.error(res.message || '升级完成失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '升级完成失败')
  } finally {
    completingUpgrade.value = false
  }
}
</script>

<style scoped>
.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}
.modal-box {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 24px;
  width: 95%;
  max-width: 460px;
  max-height: 85vh;
  overflow-y: auto;
}

.room-modal-head {
  display: flex;
  align-items: center;
  gap: 12px;
}
.room-modal-icon {
  font-size: 36px;
  line-height: 1;
}
.room-modal-titles {
  min-width: 0;
}
.room-modal-name {
  margin: 0;
  font-size: 16px;
  font-weight: 700;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.room-modal-badges {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 4px;
}
.level-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
  font-weight: 600;
}
.cap-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
}

.room-modal-desc {
  margin: 10px 0 14px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* ── 命名权 ── */
.room-naming {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 14px;
}
.btn-naming {
  width: 100%;
  padding: 8px 10px;
  font-size: 12px;
  font-weight: 700;
  border-radius: 6px;
  cursor: pointer;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-warning) 12%, var(--color-surface));
  border: 1px dashed var(--color-warning);
  transition: background 0.15s;
}
.btn-naming:hover {
  background: color-mix(in srgb, var(--color-warning) 22%, var(--color-surface));
}
.room-naming-upgrade {
  margin-top: 8px;
}
.naming-input {
  width: 100%;
  padding: 6px 10px;
  font-size: 13px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-bg);
  color: var(--color-text);
}
.naming-input:focus {
  outline: none;
  border-color: var(--color-primary);
}
.naming-actions {
  display: flex;
  gap: 6px;
}
.btn-naming-confirm {
  padding: 4px 14px;
  font-size: 13px;
  border-radius: 6px;
  cursor: pointer;
  color: #fff;
  background: var(--color-primary);
  border: 1px solid var(--color-primary);
}
.btn-naming-confirm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-naming-cancel {
  padding: 4px 12px;
  font-size: 13px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}
.btn-naming-cancel:hover {
  border-color: var(--color-primary);
}

/* ── 床位 ── */
.beds-section {
  margin-bottom: 14px;
}
.section-title {
  margin: 0 0 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  padding-bottom: 6px;
  border-bottom: 1px solid var(--color-border);
}
.swap-hint-inline {
  padding: 6px 10px;
  margin-bottom: 8px;
  font-size: 12px;
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  border: 1px solid var(--color-primary);
  border-radius: 6px;
}
.room-beds {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.bed-slot {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 13px;
  border: 1px solid transparent;
  transition: background 0.15s;
}
.bed-slot.occupied {
  background: var(--color-bg);
  border-color: var(--color-border);
  cursor: default;
}
.bed-slot.swap-empty-target {
  cursor: pointer;
  border: 1px dashed var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
}
.bed-slot.swap-empty-target:hover {
  background: color-mix(in srgb, var(--color-primary) 14%, transparent);
}
.bed-slot.swap-other-target {
  cursor: pointer;
  border: 1px dashed var(--color-warning);
  background: color-mix(in srgb, var(--color-warning) 6%, transparent);
}
.bed-slot.swap-other-target:hover {
  background: color-mix(in srgb, var(--color-warning) 14%, transparent);
}
.bed-slot.selected {
  border: 1px solid var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 14%, transparent);
}
.bed-icon {
  font-size: 15px;
}
.bed-icon.empty {
  opacity: 0.3;
}
.bed-name {
  flex: 1;
  min-width: 0;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.bed-name.empty {
  color: var(--color-text-secondary);
  font-weight: 400;
}
.bed-status {
  font-size: 11px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}
.bed-status.home {
  color: var(--color-success);
}
.bed-status.away {
  color: var(--color-warning);
}

.room-missing {
  text-align: center;
  padding: 20px 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.room-upgrade-section {
  margin-bottom: 14px;
  padding: 12px;
  border-radius: 8px;
  border: 1px solid var(--color-border);
  background: var(--color-bg);
}
.room-upgrade-level {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 6px;
}
.room-upgrade-hint {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.room-upgrade-lock {
  margin: 0 0 8px;
  font-size: 12px;
  color: var(--color-warning);
}
.room-upgrading,
.room-upgrade-progress {
  font-size: 12px;
  color: var(--color-primary);
  font-weight: 600;
  margin-bottom: 8px;
}
.room-upgrade-progress-head {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}
.room-upgrade-progress-bar {
  height: 8px;
  background: var(--color-border);
  border-radius: 4px;
  overflow: hidden;
  margin-bottom: 4px;
}
.room-upgrade-progress-fill {
  height: 100%;
  background: var(--color-primary);
  border-radius: 4px;
  transition: width 0.3s;
}
.room-upgrade-remaining {
  font-size: 11px;
  font-weight: 500;
  color: var(--color-warning);
}

.upgrade-helpers-section {
  margin-bottom: 10px;
}
.helpers-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.helper-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--color-surface);
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 12px;
}
.helper-info {
  display: flex;
  align-items: center;
  gap: 10px;
}
.helper-name {
  font-weight: 500;
  color: var(--color-text);
}
.btn-recall {
  background: rgba(220, 53, 69, 0.15);
  color: #dc3545;
  border: 1px solid #dc3545;
  padding: 4px 10px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 11px;
}
.btn-recall:hover:not(:disabled) {
  background: rgba(220, 53, 69, 0.3);
}
.btn-recall:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-recall-bed {
  margin-top: 4px;
  width: 100%;
}
.empty-helpers {
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 12px;
  padding: 8px;
}

.donate-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 10px;
}
.donate-row {
  padding: 8px 10px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  background: var(--color-surface);
}
.donate-row-head {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
}
.donate-mat-icon { font-size: 14px; }
.donate-mat-name { font-weight: 600; color: var(--color-text); }
.donate-mat-progress {
  margin-left: auto;
  color: var(--color-text-secondary);
}
.donate-mine { color: var(--color-primary); }
.donate-form {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-top: 6px;
  flex-wrap: wrap;
}
.donate-have, .donate-empty { font-size: 11px; color: var(--color-text-secondary); }
.donate-input {
  width: 72px;
  padding: 4px 8px;
  font-size: 13px;
  border-radius: 4px;
  border: 1px solid var(--color-border);
  background: var(--color-bg);
  color: var(--color-text);
}
.btn-donate {
  padding: 4px 10px;
  font-size: 12px;
  border-radius: 4px;
  cursor: pointer;
  color: #fff;
  background: var(--color-primary);
  border: 1px solid var(--color-primary);
}
.btn-donate:disabled { opacity: 0.5; cursor: not-allowed; }
.donate-full {
  margin-top: 4px;
  font-size: 11px;
  color: var(--color-success);
}

/* ── 底部操作 ── */
.room-modal-actions {
  display: flex;
  align-items: flex-start;
  justify-content: flex-end;
  gap: 8px;
  flex-wrap: wrap;
}
.btn-contributors {
  padding: 8px 14px;
  font-size: 13px;
  border-radius: 6px;
  cursor: pointer;
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 12%, transparent);
  border: 1px solid var(--color-primary);
}
.btn-contributors:hover {
  background: color-mix(in srgb, var(--color-primary) 22%, transparent);
}
.btn-upgrade {
  padding: 8px 14px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary);
  background: transparent;
  border: 1px solid var(--color-primary);
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;
}
.btn-upgrade:hover:not(:disabled) {
  background: var(--color-primary);
  color: #fff;
}
.btn-upgrade:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}
.btn-gm {
  background: rgba(255, 193, 7, 0.15);
  color: #ffc107;
  border: 1px solid #ffc107;
  padding: 8px 14px;
  border-radius: 6px;
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
}
.btn-gm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-cancel {
  padding: 8px 16px;
  font-size: 13px;
  background: var(--color-bg);
  color: var(--color-text);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
}
.btn-cancel:hover {
  border-color: var(--color-primary);
}

/* ── 移动端适配 ── */
@media (max-width: 640px) {
  .modal-box {
    padding: 16px;
    max-height: 90vh;
  }

  /* iOS Safari 表单控件字号 <16px 聚焦时会强制放大页面 */
  .naming-input {
    font-size: 16px;
  }
}
/* 宿舍详情等级主题：Lv.1 实用朴素，Lv.2 明显完成扩建。 */
.modal-box {
  --room-blue: color-mix(in srgb, #5d87b3 74%, var(--color-primary));
  position: relative;
  border-color: color-mix(in srgb, var(--room-blue) 24%, var(--color-border));
  border-radius: 18px;
  background: linear-gradient(145deg, color-mix(in srgb, var(--room-blue) 5%, var(--color-surface)), var(--color-surface));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent), 0 24px 64px rgba(0, 0, 0, 0.28);
  animation: dorm-room-in 0.24s ease-out both;
}
.modal-box::before {
  content: '';
  position: absolute;
  top: 0;
  left: 22px;
  right: 22px;
  height: 2px;
  background: var(--room-blue);
}
.room-modal-head {
  padding: 12px;
  border: 1px solid color-mix(in srgb, var(--room-blue) 16%, var(--color-border));
  border-radius: 12px;
  background: color-mix(in srgb, var(--room-blue) 5%, var(--color-bg));
}
.room-modal-icon {
  display: grid;
  width: 52px;
  height: 52px;
  flex: none;
  place-items: center;
  border-radius: 12px;
  background: color-mix(in srgb, var(--room-blue) 10%, var(--color-surface));
}
.room-level-2 {
  border-color: color-mix(in srgb, #86b5dc 52%, var(--color-border));
  background:
    radial-gradient(circle at 100% 0%, color-mix(in srgb, #8ebce1 16%, transparent), transparent 30%),
    linear-gradient(145deg, color-mix(in srgb, #6e9fc9 9%, var(--color-surface)), var(--color-surface));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, #90bee2 10%, transparent), 0 26px 70px rgba(0, 0, 0, 0.3);
}
.room-level-2::before {
  background: linear-gradient(90deg, transparent, #91c1e7, transparent);
  box-shadow: 0 0 12px color-mix(in srgb, #88b8dd 42%, transparent);
}
.room-level-2 .room-modal-icon {
  border: 1px solid color-mix(in srgb, #8ab8dc 42%, var(--color-border));
  box-shadow: 0 6px 16px color-mix(in srgb, #78a6cd 20%, transparent);
}
.room-level-2 .level-badge {
  border: 1px solid color-mix(in srgb, #82b1d6 45%, var(--color-border));
  background: color-mix(in srgb, #7ba8cd 14%, var(--color-bg));
}
@keyframes dorm-room-in { from { opacity: 0; transform: translateY(10px) scale(0.99); } }

@media (max-width: 640px) {
  .modal-mask { align-items: flex-end; }
  .modal-box { width: 100%; max-width: none; max-height: 94dvh; border-radius: 18px 18px 0 0; }
}
@media (prefers-reduced-motion: reduce) { .modal-box { animation: none; } }
.room-close {
  flex: none;
  margin-left: auto;
  width: 36px;
  min-height: 36px;
  border: 0;
  border-radius: 8px;
  background: transparent;
  color: var(--color-text);
  cursor: pointer;
}
@media (max-width: 640px) {
  .modal-box { padding: 14px; }
  .room-modal-head { gap: 8px; }
  .room-modal-titles { min-width: 0; flex: 1; }
  .room-modal-name { font-size: 17px; overflow-wrap: anywhere; }
  .room-modal-desc { font-size: 14px; line-height: 1.7; }
  .donate-form { flex-wrap: wrap; gap: 8px; }
  .room-naming { flex-wrap: wrap; }
  .naming-input { width: 100%; }
  .naming-actions { display: flex; width: 100%; gap: 8px; }
  .naming-actions > button { flex: 1; }
  .bed-name { overflow-wrap: anywhere; white-space: normal; }
}
</style>
