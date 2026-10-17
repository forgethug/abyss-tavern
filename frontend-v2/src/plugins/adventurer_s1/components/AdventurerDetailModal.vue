<template>
  <Teleport to="body">
<div class="modal-overlay s1-mobile-surface s1-mobile-overlay" :class="{ elevated: elevated }" @click.self="emit('close')">
<div class="modal-card s1-mobile-dialog" :class="titleFrameClass">
<span v-if="titleFrameKind === 'fate'" class="detail-fate-atmosphere" aria-hidden="true"></span>
<span v-if="titleFrameKind === 'fate_companion'" class="detail-companion-atmosphere" aria-hidden="true"></span>
<span v-if="titleFrameClass" class="title-frame-ornaments" aria-hidden="true">
<TitleFrameArtwork :kind="titleFrameKind!" variant="detail" />
</span>
      <header class="modal-head s1-mobile-dialog-header">
        <h3 class="modal-title">
          <SquadEmblem
            v-if="detail?.squad_badge"
            :emblem="detail.squad_badge.emblem ?? detail.squad_badge"
            :size="24"
            :title="detail.squad_badge.squad_name || '纹章小队徽记'"
          />
          {{ detail ? detail.name : '冒险者详情' }}
          <span v-if="detail" class="title-lv">Lv.{{ detail.level }}</span>
          <span v-if="detail?.active_title" class="active-title-badge" :title="detail.active_title.description">{{ detail.active_title.name }}</span>
        </h3>
        <button type="button" class="modal-close" aria-label="关闭冒险者详情" @click="emit('close')">✕</button>
      </header>

      <div ref="detailBody" class="modal-body s1-mobile-dialog-body">
        <div v-if="loading" class="state">加载中…</div>

        <template v-else-if="detail">
          <div class="d-top">
            <span
              class="d-status"
              :style="{ color: statusColor(detail.status, detail.tavern_seated), background: statusColor(detail.status, detail.tavern_seated) + '22' }"
            >{{ statusLabel(detail.status, detail.building_instance_status, detail.building_handler_key, detail.tavern_seated) }}</span>
            <span v-if="detail.character_class" class="d-tag">🎭 {{ detail.character_class }}</span>
            <span class="d-tag">{{ detail.gender }}</span>
            <span v-if="personalityText(detail.personality)" class="d-tag">💭 {{ personalityText(detail.personality) }}</span>
            <label v-if="isOwner" class="title-picker">
              <span>称号</span>
              <select
                :value="detail.active_title?.id ?? ''"
                :disabled="busy"
                @change="onTitleChange"
              >
                <option value="">不展示称号</option>
                <option v-for="title in detail.available_titles ?? []" :key="title.id" :value="title.id">
                  {{ title.name }} · {{ title.description }}
                </option>
              </select>
            </label>
            <button
              type="button"
              class="history-btn"
              @click="emit('view-logs', { id: props.advId, name: detail.name })"
            >📜 探险日志</button>
            <button
              v-if="isOwner" type="button" class="history-btn"
              :disabled="exporting" @click="exportText"
            >{{ exporting ? '导出中…' : '导出资料 TXT' }}</button>
            <button
              v-if="!isOwner"
              type="button"
              class="follow-btn"
              :class="{ active: detail.is_followed }"
              :disabled="busy"
              @click="toggleFollow"
            >{{ detail.is_followed ? '⭐ 已关注' : '☆ 关注' }}</button>
          </div>

          <div class="d-hp">
            <div class="d-hp-fill" :style="{ width: liveHpPercent + '%' }"></div>
            <span class="d-hp-text">❤️ {{ liveHp }} / {{ detail.max_hp }}<small v-if="detail.max_hp >= S1_MAX_HP_CAP" class="cap-tag">满</small></span>
          </div>

          <div v-if="detail.stamina != null" class="d-stamina">
            <div class="d-stamina-fill" :style="{ width: staminaPercent + '%' }"></div>
            <span class="d-stamina-text">⚡ {{ liveStamina }} / {{ detail.max_stamina ?? STAMINA_MAX }}</span>
          </div>

          <div class="d-stats">
            <div class="stat"><span class="k">⚔️ 力量</span><span class="v">{{ detail.strength }}<small class="cap-hint">/{{ S1_ATTR_CAP }}</small></span></div>
            <div class="stat"><span class="k">🏃 敏捷</span><span class="v">{{ detail.agility }}<small class="cap-hint">/{{ S1_ATTR_CAP }}</small></span></div>
            <div class="stat"><span class="k">🧠 智力</span><span class="v">{{ detail.intelligence }}<small class="cap-hint">/{{ S1_ATTR_CAP }}</small></span></div>
            <div class="stat"><span class="k">🍀 幸运</span><span class="v">{{ detail.luck }}<small class="cap-hint">/{{ S1_ATTR_CAP }}</small></span></div>
            <div class="stat"><span class="k">💰 金币</span><span class="v">{{ formatMoney(detail.gold) }}</span></div>
          </div>

          <nav class="detail-nav" aria-label="冒险者档案导航">
            <button type="button" :class="{ active: activeDetailSection === 'detail-profile' }" :aria-current="activeDetailSection === 'detail-profile' ? 'location' : undefined" @click="scrollDetailSection('detail-profile')"><span>档案</span></button>
            <button v-if="isOwner || displaySkills.length" type="button" :class="{ active: activeDetailSection === 'detail-combat' }" :aria-current="activeDetailSection === 'detail-combat' ? 'location' : undefined" @click="scrollDetailSection('detail-combat')"><span>战备</span></button>
            <button v-if="isOwner" type="button" :class="{ active: activeDetailSection === 'detail-assets' }" :aria-current="activeDetailSection === 'detail-assets' ? 'location' : undefined" @click="scrollDetailSection('detail-assets')"><span>资产</span></button>
            <button v-if="showHistorySection" type="button" :class="{ active: activeDetailSection === 'detail-history' }" :aria-current="activeDetailSection === 'detail-history' ? 'location' : undefined" @click="scrollDetailSection('detail-history')"><span>传记</span></button>
          </nav>

          <p v-if="ownerActionLocked" class="exploring-block-hint">
            探险中无法修改组队偏好、背包、仓库、特质或技能配置，请等待探险结束。
          </p>

          <FateJourneyPanel
            v-if="isOwner && detail.level >= 15 && detail.status !== 'dead'"
            :adventurer-id="props.advId"
          />

          <!-- 组队偏好（仅自己） -->
          <section v-if="isOwner" id="detail-profile" class="d-section detail-section-profile">
            <h4>组队偏好</h4>
            <div class="pref-toggle">
              <button
                type="button"
                class="pref-btn"
                :class="{ active: detail.party_preference === 'auto' }"
                :disabled="busy || ownerActionLocked || detail.party_preference === 'auto'"
                @click="setPreference('auto')"
              >自动单人匹配</button>
              <button
                type="button"
                class="pref-btn"
                :class="{ active: detail.party_preference !== 'auto' }"
                :disabled="busy || ownerActionLocked || detail.party_preference !== 'auto'"
                @click="setPreference('manual')"
              >手动匹配</button>
            </div>
            <p class="pref-hint muted small">
              {{ detail.party_preference === 'auto'
                ? '精力足够时会自动报名随机地图的单人匹配，不看血量。'
                : '不会自动报名，需要你在匹配面板手动操作。' }}
            </p>
          </section>

          <!-- 特质 -->
          <section v-if="detail.traits?.length" :id="isOwner ? undefined : 'detail-profile'" class="d-section detail-section-profile">
            <h4>
              <span class="section-heading-label">
                特质
                <small class="muted">共 {{ traitEntries.length }} 项</small>
                <small class="trait-count trait-count-equipped">已携带 {{ equippedTraitCount }}</small>
                <small class="trait-count trait-count-unequipped">已卸下 {{ unequippedTraitCount }}</small>
              </span>
              <button
                type="button"
                class="section-collapse-btn"
                :aria-expanded="!traitCollapsed"
                aria-controls="detail-traits-content"
                @click="traitCollapsed = !traitCollapsed"
              >
                <span aria-hidden="true">{{ traitCollapsed ? '▸' : '▾' }}</span>
                {{ traitCollapsed ? '展开' : '收起' }}
              </button>
            </h4>
            <div id="detail-traits-content" v-show="!traitCollapsed" class="section-collapse-content">
              <div class="trait-toolbar">
                <label>
                  排序
                  <select v-model="traitSort">
                    <option value="name">携带状态与名称</option>
                    <option value="newest">新获得优先</option>
                    <option value="oldest">先获得优先</option>
                  </select>
                </label>
                <button
                  v-if="isOwner"
                  type="button"
                  class="mini-btn"
                  :disabled="busy || ownerActionLocked || removableTraitCount === 0"
                  @click="confirmUnequipAll = true"
                >一键卸下可卸特质（{{ removableTraitCount }}）</button>
              </div>
              <div
                class="entry"
                :class="{ 'trait-unequipped': item.trait.is_pure_positive && item.trait.equipped === false }"
                v-for="item in traitEntries"
                :key="item.trait.id ?? item.trait.name"
              >
                <div class="entry-line">
                  <strong>{{ item.trait.name }}</strong>
                  <span
                    v-if="item.trait.is_pure_positive"
                    class="entry-actions"
                  >
                    <span
                      class="trait-loadout-state"
                      :class="item.trait.equipped === false ? 'unequipped' : 'equipped'"
                    >
                      纯正面 · 可卸下 · {{ item.trait.equipped === false ? '未携带' : '已携带' }}
                    </span>
                    <button
                      v-if="isOwner && item.trait.id != null"
                      type="button"
                      class="mini-btn"
                      :disabled="busy || ownerActionLocked"
                      @click="onSetTraitEquipped(Number(item.trait.id), item.trait.equipped === false)"
                    >{{ item.trait.equipped === false ? '携带' : '卸下' }}</button>
                  </span>
                  <span
                    v-else
                    class="trait-loadout-state trait-not-removable"
                    title="当前分类不支持卸下；不代表这项特质一定含有负面效果。"
                  >当前不可卸下</span>
                </div>
                <span class="entry-desc">{{ item.trait.desc }}</span>
                <div
                  v-if="item.recovery"
                  class="trait-recovery"
                  :class="{ active: item.recovery.accumulating }"
                >
                  <span class="trait-recovery-status">{{ item.recovery.statusText }}</span>
                  <span class="trait-recovery-hint">{{ item.recovery.hintText }}</span>
                </div>
              </div>
            </div>
          </section>

          <!-- 人际关系：只展示最亲密 / 最不和两人，不展示具体好感度数值 -->
          <section v-if="bestRelation || worstRelation" class="d-section">
            <h4>人际关系</h4>
            <div class="relation-grid">
              <div v-if="bestRelation" class="relation-tile">
                <span class="relation-icon">💖</span>
                <span class="relation-label">最亲密</span>
                <span class="relation-name">{{ bestRelation.adventurer_name }}</span>
              </div>
              <div v-if="worstRelation" class="relation-tile">
                <span class="relation-icon">💢</span>
                <span class="relation-label">最不和</span>
                <span class="relation-name">{{ worstRelation.adventurer_name }}</span>
              </div>
            </div>
          </section>

          <!-- 技能（自己可管理：携带/未携带切换） -->
          <section v-if="isOwner || displaySkills.length" id="detail-combat" class="d-section detail-section-combat">
            <h4>
              <span class="section-heading-label">
                技能
                <small class="muted">共 {{ displaySkills.length }} 项</small>
                <small v-if="isOwner" class="muted">{{ equippedCount }}/{{ MAX_EQUIPPED_SKILLS }} 已携带</small>
              </span>
              <button type="button" class="section-collapse-btn" :aria-expanded="!skillsCollapsed" aria-controls="detail-skills-content" @click="skillsCollapsed = !skillsCollapsed">
                <span aria-hidden="true">{{ skillsCollapsed ? '▸' : '▾' }}</span>
                {{ skillsCollapsed ? '展开' : '收起' }}
              </button>
            </h4>
            <div id="detail-skills-content" v-show="!skillsCollapsed" class="section-collapse-content">
            <div v-if="isOwner && equippedSkills.length === 0" class="muted small">未携带任何技能</div>
            <div class="entry" v-for="sk in equippedSkills" :key="'eq-' + sk.id">
              <div class="entry-line">
                <strong>⚡ {{ sk.name }}</strong>
                <span v-if="isOwner" class="entry-actions">
                  <button type="button" class="mini-btn" :disabled="busy || ownerActionLocked" @click="onUnequipSkill(sk.id)">卸下</button>
                </span>
              </div>
              <span v-if="assetRecoveryText(sk)" class="asset-recovery">{{ assetRecoveryText(sk) }}</span>
              <span class="entry-desc">{{ skillDesc(sk) }}</span>
            </div>

            <template v-if="isOwner && unequippedSkills.length > 0">
              <div class="skill-divider">未携带（{{ unequippedSkills.length }}）</div>
              <div class="entry skill-unequipped" v-for="sk in unequippedSkills" :key="'un-' + sk.id">
                <div class="entry-line">
                  <strong>{{ sk.name }}</strong>
                  <span class="entry-actions">
                    <button
                      type="button"
                      class="mini-btn"
                      :disabled="busy || ownerActionLocked || equippedCount >= MAX_EQUIPPED_SKILLS"
                      @click="onEquipSkill(sk.id)"
                    >携带</button>
                  </span>
                </div>
                <span v-if="assetRecoveryText(sk)" class="asset-recovery">{{ assetRecoveryText(sk) }}</span>
                <span class="entry-desc">{{ skillDesc(sk) }}</span>
              </div>
            </template>
            </div>
          </section>

          <!-- 随身装备 -->
          <section class="d-section detail-section-combat">
            <h4>
              <span class="section-heading-label">随身装备 <small class="muted">{{ equipmentList.length }} 件</small></span>
              <button
                v-if="equipmentList.length > 0"
                type="button"
                class="section-collapse-btn"
                :aria-expanded="!equipmentCollapsed"
                aria-controls="detail-equipment-content"
                @click="equipmentCollapsed = !equipmentCollapsed"
              >
                <span aria-hidden="true">{{ equipmentCollapsed ? '▸' : '▾' }}</span>
                {{ equipmentCollapsed ? '展开' : '收起' }}
              </button>
            </h4>
            <div id="detail-equipment-content" v-show="!equipmentCollapsed" class="section-collapse-content">
              <div v-if="equipmentList.length === 0" class="muted small">暂无随身装备</div>
              <div class="entry" v-for="eq in equipmentList" :key="'eq-' + eq.id">
                <div class="entry-line">
                  <strong :style="{ color: rarityColor(eq.rarity) }">{{ hiddenBossClueItemDisplayName(eq) }}</strong>
                  <span v-if="eq.clue_label" class="rarity-pill clue-index-pill">{{ eq.clue_label }}</span>
                  <span class="rarity-pill" :style="{ color: rarityColor(eq.rarity), borderColor: rarityColor(eq.rarity) }">
                    {{ rarityLabel(eq.rarity) }}
                  </span>
                  <span v-if="eq.is_player_locked" class="item-protection-badge" title="防止误出售、交易或转化">🔒 已保护</span>
                  <span v-if="(eq.quantity ?? 1) > 1" class="uses">×{{ eq.quantity }}</span>
                  <span v-if="eq.uses_left != null" class="uses">剩余 {{ eq.uses_left }} 次</span>
                  <span
                    v-if="isOwner"
                    class="entry-actions equipment-actions"
                    :class="{ 'has-quantity': (eq.quantity ?? 1) > 1 }"
                  >
                    <input
                      v-if="(eq.quantity ?? 1) > 1"
                      type="number" class="qty-input" :min="1" :max="eq.quantity ?? 1"
                      :value="qtyFor('eq', eq.id, eq.quantity ?? 1)" :disabled="busy || ownerActionLocked"
                      @input="onQtyInput('eq', eq.id, eq.quantity ?? 1, $event)"
                    />
                    <button type="button" class="mini-btn" :disabled="busy || ownerActionLocked" @click="unequip(eq.id, eq.quantity ?? 1)">卸下</button>
                    <button
                      type="button"
                      class="mini-btn protection-btn"
                      :class="{ active: eq.is_player_locked }"
                      :disabled="busy || ownerActionLocked"
                      @click="toggleItemProtection('equipment', eq)"
                    >{{ eq.is_player_locked ? '解锁' : '保护' }}</button>
                  </span>
                </div>
                <span v-if="assetRecoveryText(eq)" class="asset-recovery">{{ assetRecoveryText(eq) }}</span>
                <span v-if="eq.desc" class="entry-desc">{{ eq.desc }}</span>
              </div>
            </div>
          </section>

          <!-- 收藏室（仅自己） -->
          <section v-if="isOwner" id="detail-assets" class="d-section detail-section-assets">
            <h4>
              <span class="section-heading-label">
                收藏室
                <small
                  class="capacity-badge"
                  :class="{ 'capacity-full': warehouseAtCapacity, 'capacity-near': warehouseNearCapacity && !warehouseAtCapacity }"
                >
                  {{ warehouseList.length }} / {{ warehouseCapacityLimit }} 格
                </small>
              </span>
              <button
                v-if="warehouseList.length > 0"
                type="button"
                class="section-collapse-btn"
                :aria-expanded="!warehouseCollapsed"
                aria-controls="detail-warehouse-content"
                @click="warehouseCollapsed = !warehouseCollapsed"
              >
                <span aria-hidden="true">{{ warehouseCollapsed ? '▸' : '▾' }}</span>
                {{ warehouseCollapsed ? '展开' : '收起' }}
              </button>
            </h4>
            <p v-if="warehouseAtCapacity" class="muted small capacity-hint">仓库已满，新获得的物品将无法存入，请先清理或上架出售。</p>
            <div id="detail-warehouse-content" v-show="!warehouseCollapsed" class="section-collapse-content">
              <div v-if="warehouseList.length === 0" class="muted small">收藏室空空如也</div>
              <template v-else>
                <InventoryListToolbar
                  v-model:rarity="warehouseRarityFilter"
                  v-model:sort="warehouseSort"
                  :items="warehouseList"
                  aria-label="按稀有度筛选收藏室物品"
                />
                <div v-if="filteredWarehouseList.length === 0" class="warehouse-filter-empty">
                  当前稀有度下没有物品
                </div>
              </template>
              <div class="entry" v-for="w in filteredWarehouseList" :key="'wh-' + w.id">
                <div class="entry-line">
                  <strong :style="{ color: rarityColor(w.rarity) }">{{ hiddenBossClueItemDisplayName(w) }}</strong>
                  <span v-if="w.clue_label" class="rarity-pill clue-index-pill">{{ w.clue_label }}</span>
                  <span class="rarity-pill" :style="{ color: rarityColor(w.rarity), borderColor: rarityColor(w.rarity) }">
                    {{ rarityLabel(w.rarity) }}
                  </span>
                  <span v-if="w.is_player_locked" class="item-protection-badge" title="防止误出售、交易或转化">🔒 已保护</span>
                  <span v-if="(w.quantity ?? 1) > 1" class="uses">×{{ w.quantity }}</span>
                  <span v-if="w.uses_left != null" class="uses">剩余 {{ w.uses_left }} 次</span>
                  <span class="entry-actions">
                    <input
                      v-if="(w.quantity ?? 1) > 1"
                      type="number" class="qty-input" :min="1" :max="w.quantity ?? 1"
                      :value="qtyFor('wh', w.id, w.quantity ?? 1)" :disabled="busy || ownerActionLocked"
                      @input="onQtyInput('wh', w.id, w.quantity ?? 1, $event)"
                    />
                    <button type="button" class="mini-btn" :disabled="busy || ownerActionLocked" @click="equip(w.id, w.quantity ?? 1)">装备</button>
                    <button
                      type="button"
                      class="mini-btn protection-btn"
                      :class="{ active: w.is_player_locked }"
                      :disabled="busy || ownerActionLocked"
                      @click="toggleItemProtection('warehouse', w)"
                    >{{ w.is_player_locked ? '解锁' : '保护' }}</button>
                  </span>
                </div>
                <span v-if="assetRecoveryText(w)" class="asset-recovery">{{ assetRecoveryText(w) }}</span>
                <span v-if="w.desc" class="entry-desc">{{ w.desc }}</span>
              </div>
            </div>
          </section>

          <!-- 材料仓库（仅自己）：采集所得，不可装备 / 不可入副本 -->
          <section v-if="isOwner" class="d-section">
            <h4>
              <span class="section-heading-label">材料仓库 <small class="muted">{{ materialList.length }} 种</small></span>
              <button
                v-if="materialList.length > 0"
                type="button"
                class="section-collapse-btn"
                :aria-expanded="!materialsCollapsed"
                aria-controls="detail-materials-content"
                @click="materialsCollapsed = !materialsCollapsed"
              >
                <span aria-hidden="true">{{ materialsCollapsed ? '▸' : '▾' }}</span>
                {{ materialsCollapsed ? '展开' : '收起' }}
              </button>
            </h4>
            <div id="detail-materials-content" v-show="!materialsCollapsed" class="section-collapse-content">
              <p class="muted small material-hint">采集所得，用于建筑建造。无法装备，也不会带入副本。</p>
              <div v-if="materialList.length === 0" class="muted small">还没有采集到任何材料</div>
              <div class="material-grid">
                <div class="material-tile" v-for="m in materialList" :key="m.name">
                  <span class="material-icon">{{ m.icon }}</span>
                  <span class="material-name">{{ m.display_name }}</span>
                  <span class="material-qty">×{{ m.quantity }}</span>
                </div>
              </div>
            </div>
          </section>

          <!-- 冒险史（分页，最新在前） -->
          <section v-if="showHistorySection" id="detail-history" class="d-section detail-section-history">
            <h4>
              冒险史
              <small v-if="historyTotal > 0" class="muted">共 {{ historyTotal }} 段</small>
            </h4>
            <div v-if="historyLoading" class="muted small">加载中…</div>
            <div v-else-if="historyLoadError" class="history-error">
              <p class="muted small">冒险史加载失败</p>
              <button type="button" class="btn-retry" @click="loadHistoryStory(historyPage)">重试</button>
            </div>
            <template v-else>
              <div
                v-for="(entry, idx) in historyEntries"
                :key="historyPage + '-' + idx"
                class="history-entry"
              >
                <p class="history">{{ entry }}</p>
              </div>
              <Pagination
                v-if="historyPagination && historyPagination.pages > 1"
                :pagination="historyPagination"
                @go="loadHistoryStory"
              />
            </template>
          </section>
        </template>

        <div v-else class="state">冒险者不存在或加载失败</div>
      </div>
    </div>
  </div>
  </Teleport>
  <S1ConfirmDialog
    v-if="confirmUnequipAll"
    title="卸下可卸特质"
    :message="`将卸下当前携带的 ${removableTraitCount} 项可卸特质，之后可以逐项重新携带。`"
    :busy="busy"
    confirm-label="确认卸下"
    @confirm="onUnequipAllTraits"
    @cancel="confirmUnequipAll = false"
  />
</template>

<script lang="ts">
const DETAIL_BACKGROUND_PAUSE_CLASS = 's1-adventurer-detail-open'
let openAdventurerDetailModalCount = 0

function pauseBackgroundTitleAnimations() {
  if (typeof document === 'undefined') return
  openAdventurerDetailModalCount += 1
  document.documentElement.classList.add(DETAIL_BACKGROUND_PAUSE_CLASS)
}

function resumeBackgroundTitleAnimations() {
  if (typeof document === 'undefined') return
  openAdventurerDetailModalCount = Math.max(0, openAdventurerDetailModalCount - 1)
  if (openAdventurerDetailModalCount === 0) {
    document.documentElement.classList.remove(DETAIL_BACKGROUND_PAUSE_CLASS)
  }
}
</script>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { getS1PlayerRealmView } from '@/composables/useApi'
import { useSocket } from '@/composables/useSocket'
import { formatMoney } from '@/utils/format'
import Pagination from '@/components/Pagination.vue'
import InventoryListToolbar from './InventoryListToolbar.vue'
import FateJourneyPanel from './FateJourneyPanel.vue'
import { useInventoryListControls } from '../composables/useInventoryListControls'
import {
  statusLabel, statusColor, rarityColor, rarityLabel, personalityText,
  liveHpOf, hpPercent, liveTraitRecoveryView, liveStaminaOf, isTraitRecoveryDueForRefresh,
  type TraitRecoveryTickContext,
  hiddenBossClueItemDisplayName, isExploringStatus, EXPLORING_BLOCKED_HINT,
  S1_ATTR_CAP, S1_MAX_HP_CAP,
} from '../utils'
import { useLiveTick } from '../composables/useLiveTick'
import TitleFrameArtwork from './TitleFrameArtwork.vue'
import SquadEmblem from './SquadEmblem.vue'
import S1ConfirmDialog from './S1ConfirmDialog.vue'
import { adventurerTextExport, downloadAdventurerText } from '../utils/adventurerExport'

interface SkillEntry {
  id: number
  name: string
  description?: string
  desc?: string
  equipped?: boolean
  recovery_minutes?: number
  recovering?: boolean
  recovery_ends_at?: string | null
}

const props = withDefaults(defineProps<{
  advId: number
  currentUserId?: number | null
  /** 叠在探险日志等高层弹窗之上 */
  elevated?: boolean
}>(), {
  elevated: false,
})
const emit = defineEmits<{ close: []; changed: []; 'view-logs': [{ id: number; name: string }] }>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const socket = useSocket()

const loading = ref(true)
const busy = ref(false)
const exporting = ref(false)
let exportDisposed = false
const ownerSkills = ref<SkillEntry[]>([])
const historyEntries = ref<string[]>([])
const historyTotal = ref(0)
const historyPage = ref(1)
const historyLoading = ref(false)
const historyLoadError = ref(false)
const historyPagination = ref<{ current_page: number; pages: number; total: number } | null>(null)

const showHistorySection = computed(() => {
  if (historyTotal.value > 0 || historyLoading.value || historyLoadError.value) return true
  const count = detail.value?.history_entry_count
  return typeof count === 'number' && count > 0
})

const STAMINA_MAX = 100
const MAX_EQUIPPED_SKILLS = 5
const { now } = useLiveTick()

const detail = computed(() => store.currentDetail)
const isOwner = computed(() => props.currentUserId != null && detail.value?.owner_id === props.currentUserId)
async function exportText() {
  if (exportDisposed || !isOwner.value || exporting.value) return
  const id = props.advId, ownerId = props.currentUserId
  const realmAtStart = getS1PlayerRealmView()
  exporting.value = true
  try {
    const response = await store.fetchDetail(id, { preserveDetail: true, force: true })
    if (exportDisposed || realmAtStart !== getS1PlayerRealmView()) return
    const snapshot = response.data
    if (response.code !== 200 || snapshot?.id !== id || snapshot.owner_id !== ownerId
      || props.advId !== id || props.currentUserId !== ownerId) {
      throw new Error(response.message || '本人资料暂时无法读取，请刷新后重试')
    }
    const result = await store.fetchSkills(id)
    if (result.code !== 200 || !Array.isArray(result.data)) throw new Error(result.message || '技能资料未读取完整，请重试')
    if (exportDisposed || props.advId !== id || props.currentUserId !== ownerId
      || realmAtStart !== getS1PlayerRealmView()) return
    const exportedAt = new Date()
    const text = adventurerTextExport({
      ...snapshot, hp: liveHpOf(snapshot, exportedAt.getTime()),
      stamina: liveStaminaOf(snapshot, exportedAt.getTime()),
    }, result.data, exportedAt)
    downloadAdventurerText(snapshot.name, text)
    message.success('资料已导出')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '导出失败，请稍后重试')
  } finally {
    exporting.value = false
  }
}
const titleFrameKind = computed<'fate' | 'fate_companion' | 'hidden_relic' | null>(() => {
  const frame = detail.value?.active_title?.frame_key
  return frame === 'fate' || frame === 'fate_companion' || frame === 'hidden_relic' ? frame : null
})
const titleFrameClass = computed(() => {
  return titleFrameKind.value ? `title-frame--${titleFrameKind.value}` : ''
})
const ownerActionLocked = computed(() => isOwner.value && isExploringStatus(detail.value?.status))

const liveHp = computed(() => {
  const d = detail.value
  if (!d) return 0
  return liveHpOf(d, now.value)
})
const liveHpPercent = computed(() => {
  const d = detail.value
  if (!d) return 0
  return hpPercent(d, now.value)
})

const liveStamina = computed(() => {
  const d = detail.value
  if (!d) return 0
  return liveStaminaOf(d, now.value)
})
const staminaPercent = computed(() => {
  const max = detail.value?.max_stamina ?? STAMINA_MAX
  return Math.max(0, Math.min(100, (liveStamina.value / max) * 100))
})

const activeDetailSection = ref('detail-profile')
const detailBody = ref<HTMLElement | null>(null)

function scrollDetailSection(sectionId: string) {
  activeDetailSection.value = sectionId
  const body = detailBody.value
  const section = body?.querySelector<HTMLElement>(`#${sectionId}`)
  if (!body || !section) return
  const marginTop = Number.parseFloat(getComputedStyle(section).scrollMarginTop) || 0
  const top = body.scrollTop + section.getBoundingClientRect().top
    - body.getBoundingClientRect().top - body.clientTop - marginTop
  body.scrollTo({ top: Math.max(0, top) })
}

const bestRelation = computed(() => detail.value?.relationships?.best ?? null)
const worstRelation = computed(() => detail.value?.relationships?.worst ?? null)

const equipmentList = computed(() => detail.value?.equipment ?? [])
const warehouseList = computed(() => detail.value?.warehouse ?? [])
const {
  rarityFilter: warehouseRarityFilter,
  sort: warehouseSort,
  visibleItems: filteredWarehouseList,
} = useInventoryListControls(warehouseList, hiddenBossClueItemDisplayName)
const warehouseCapacityLimit = computed(() => detail.value?.warehouse_capacity_limit ?? 100)
const warehouseAtCapacity = computed(
  () => warehouseList.value.length >= warehouseCapacityLimit.value,
)
const warehouseNearCapacity = computed(
  () => warehouseList.value.length >= Math.floor(warehouseCapacityLimit.value * 0.9),
)
const materialList = computed<{ name: string; quantity: number; display_name: string; icon: string }[]>(
  () => (detail.value as any)?.materials ?? [],
)
const LONG_SECTION_AUTO_COLLAPSE_COUNT = 12
const traitCollapsed = ref(false)
const skillsCollapsed = ref(false)
const equipmentCollapsed = ref(false)
const warehouseCollapsed = ref(false)
const materialsCollapsed = ref(false)
let collapseStateAdventurerId: number | null = null

function resetLongSectionCollapseState() {
  collapseStateAdventurerId = null
  traitCollapsed.value = false
  skillsCollapsed.value = false
  equipmentCollapsed.value = false
  warehouseCollapsed.value = false
  materialsCollapsed.value = false
}

function initializeLongSectionCollapseState() {
  const d = detail.value
  if (!d || d.id !== props.advId || collapseStateAdventurerId === d.id) return
  traitCollapsed.value = (d.traits?.length ?? 0) > LONG_SECTION_AUTO_COLLAPSE_COUNT
  equipmentCollapsed.value = equipmentList.value.length > LONG_SECTION_AUTO_COLLAPSE_COUNT
  warehouseCollapsed.value = warehouseList.value.length > LONG_SECTION_AUTO_COLLAPSE_COUNT
  materialsCollapsed.value = materialList.value.length > LONG_SECTION_AUTO_COLLAPSE_COUNT
  collapseStateAdventurerId = d.id
}

watch(() => props.advId, resetLongSectionCollapseState)
watch(detail, initializeLongSectionCollapseState, { immediate: true })
const displaySkills = computed<SkillEntry[]>(() =>
  isOwner.value ? ownerSkills.value : ((detail.value?.skills as SkillEntry[]) ?? []),
)
const equippedSkills = computed<SkillEntry[]>(() =>
  displaySkills.value.filter(s => s.equipped !== false),
)
const unequippedSkills = computed<SkillEntry[]>(() =>
  displaySkills.value.filter(s => s.equipped === false),
)
const equippedCount = computed(() => equippedSkills.value.length)
const traitNameCollator = new Intl.Collator('zh-CN', { numeric: true, sensitivity: 'base' })
const traitSort = ref<'name' | 'newest' | 'oldest'>('name')
const confirmUnequipAll = ref(false)
watch(() => props.advId, () => { confirmUnequipAll.value = false })
// 时间排序覆盖所有特质；没有取得时间的旧记录稳定置底，不以 ID 代替时间。
const sortedTraits = computed(() => (detail.value?.traits ?? [])
  .map((trait, originalIndex) => ({ trait, originalIndex, time: Date.parse(trait.created_at ?? '') }))
  .sort((a, b) => {
    if (traitSort.value !== 'name') {
      const aKnown = Number.isFinite(a.time)
      const bKnown = Number.isFinite(b.time)
      if (aKnown !== bKnown) return aKnown ? -1 : 1
      if (aKnown && a.time !== b.time) return traitSort.value === 'newest' ? b.time - a.time : a.time - b.time
      return a.originalIndex - b.originalIndex
    }
    return Number(a.trait.equipped === false) - Number(b.trait.equipped === false)
      || traitNameCollator.compare(a.trait.name.trim(), b.trait.name.trim())
      || a.originalIndex - b.originalIndex
  })
  .map(item => item.trait))
const removableTraitCount = computed(() => sortedTraits.value.filter(
  trait => trait.id != null && trait.is_pure_positive && trait.equipped !== false,
).length)
const unequippedTraitCount = computed(() =>
  sortedTraits.value.filter(trait => trait.equipped === false).length,
)
const equippedTraitCount = computed(() => sortedTraits.value.length - unequippedTraitCount.value)

function traitRecoveryTickContext(d: typeof detail.value): TraitRecoveryTickContext | null {
  if (!d) return null
  return {
    status: d.status,
    dorm_room_id: d.dorm_room_id,
    bed_index: d.bed_index,
    building_handler_key: d.building_handler_key,
    tavern_seated: d.tavern_seated,
  }
}

const traitEntries = computed(() => {
  const d = detail.value
  if (!d || sortedTraits.value.length === 0) return []
  const computedAt = d.trait_recovery_computed_at
  const tickCtx = traitRecoveryTickContext(d)
  return sortedTraits.value.map(trait => ({
    trait,
    recovery: trait.recovery
      ? liveTraitRecoveryView(trait.recovery, computedAt, now.value, tickCtx)
      : null,
  }))
})

let traitRecoveryRefreshPending = false
let lastTraitRecoveryRefreshAt = 0
const TRAIT_RECOVERY_REFRESH_MIN_MS = 10_000

watch(
  [now, () => detail.value?.trait_recovery_computed_at, () => detail.value?.traits],
  () => {
    const d = detail.value
    if (!d?.traits?.length || traitRecoveryRefreshPending) return
    if (Date.now() - lastTraitRecoveryRefreshAt < TRAIT_RECOVERY_REFRESH_MIN_MS) return
    const computedAt = d.trait_recovery_computed_at
    const tickCtx = traitRecoveryTickContext(d)
    const due = d.traits.some(
      trait => trait.recovery && isTraitRecoveryDueForRefresh(trait.recovery, computedAt, now.value, tickCtx),
    )
    if (!due) return
    traitRecoveryRefreshPending = true
    lastTraitRecoveryRefreshAt = Date.now()
    store.fetchDetail(props.advId, { preserveDetail: true, force: true })
      .finally(() => { traitRecoveryRefreshPending = false })
  },
)

function skillDesc(sk: SkillEntry): string {
  return sk.desc ?? sk.description ?? ''
}

function formatAssetRecoveryMinutes(totalMinutes: number): string {
  const days = Math.floor(totalMinutes / 1440)
  const hours = Math.floor((totalMinutes % 1440) / 60)
  const minutes = totalMinutes % 60
  if (days > 0) return `${days}天${hours > 0 ? `${hours}小时` : ''}`
  if (hours > 0) return `${hours}小时${minutes > 0 ? `${minutes}分钟` : ''}`
  return `${Math.max(1, minutes)}分钟`
}

function assetRecoveryText(asset: {
  recovery_minutes?: number
  recovery_ends_at?: string | null
}): string | null {
  const configuredMinutes = Math.max(0, Math.trunc(Number(asset.recovery_minutes) || 0))
  if (configuredMinutes <= 0) return null
  const periodText = `恢复期 ${formatAssetRecoveryMinutes(configuredMinutes)}`

  if (!asset.recovery_ends_at) return periodText
  const endsAt = Date.parse(asset.recovery_ends_at)
  if (!Number.isFinite(endsAt)) return periodText
  const remainingSeconds = Math.ceil((endsAt - now.value) / 1000)
  if (remainingSeconds <= 0) return periodText

  const remainingText = formatAssetRecoveryMinutes(Math.ceil(remainingSeconds / 60))
  return `${periodText} · 恢复中，剩余 ${remainingText}`
}

let loadTimer: ReturnType<typeof setTimeout> | null = null
let socketRefreshTimer: ReturnType<typeof setTimeout> | null = null
let lastSocketRefreshAt = 0
let socketRefreshNeedsRelated = false
const SOCKET_DETAIL_REFRESH_MIN_INTERVAL_MS = 60_000

async function loadHistoryStory(page = 1) {
  historyLoading.value = true
  historyLoadError.value = false
  try {
    const res = await store.fetchHistoryStory(props.advId, page, 5)
    if (res?.code === 200 && res.data) {
      historyEntries.value = res.data.items ?? []
      historyTotal.value = res.data.total ?? 0
      historyPage.value = res.data.page ?? page
      historyPagination.value = {
        current_page: res.data.page ?? page,
        pages: res.data.pages ?? 1,
        total: res.data.total ?? 0,
      }
    } else {
      historyEntries.value = []
      historyPagination.value = null
      historyLoadError.value = true
    }
  } catch {
    historyEntries.value = []
    historyPagination.value = null
    historyLoadError.value = true
  } finally {
    historyLoading.value = false
  }
}

async function reloadDetail(opts: { includeRelated?: boolean } = {}) {
  const includeRelated = opts.includeRelated ?? true
  const res = await store.fetchDetail(props.advId, { preserveDetail: true })
  if (res?.stale_session) return res
  if (includeRelated && res?.code === 200 && store.currentDetail?.owner_id === props.currentUserId) {
    try {
      const skillRes = await store.fetchSkills(props.advId)
      if (skillRes?.code === 200) ownerSkills.value = skillRes.data ?? []
    } catch {
      // 技能加载失败不影响主详情展示
    }
  }
  if (includeRelated) await loadHistoryStory(1)
  lastSocketRefreshAt = Date.now()
  return res
}

function scheduleSocketDetailRefresh(includeRelated = false) {
  socketRefreshNeedsRelated = socketRefreshNeedsRelated || includeRelated
  if (socketRefreshTimer) return
  const elapsed = Date.now() - lastSocketRefreshAt
  const delay = Math.max(0, SOCKET_DETAIL_REFRESH_MIN_INTERVAL_MS - elapsed)
  socketRefreshTimer = setTimeout(async () => {
    socketRefreshTimer = null
    const needsRelated = socketRefreshNeedsRelated
    socketRefreshNeedsRelated = false
    await reloadDetail({ includeRelated: needsRelated })
  }, delay)
}

function onAdventurerChanged(payload: { action?: string; ids?: number[] }) {
  if (!['title_catalog_changed', 'catalog_item_changed', 'party_preferences_changed'].includes(payload?.action || '') && (
    !Array.isArray(payload?.ids) || !payload.ids.includes(props.advId)
  )) return
  scheduleSocketDetailRefresh(false)
}

function onExpeditionLogChanged(payload: { action?: string }) {
  if (payload?.action === 'completed') scheduleSocketDetailRefresh(true)
}

onMounted(async () => {
  pauseBackgroundTitleAnimations()
  const hasCachedDetail = store.currentDetail?.id === props.advId
  if (hasCachedDetail) {
    loading.value = false
  }

  socket.on('adventurer_changed', onAdventurerChanged)
  socket.on('expedition_log_changed', onExpeditionLogChanged)

  // 安全兜底：8 秒后无论如何结束加载态（防止 API 挂死导致永久 loading）
  loadTimer = setTimeout(() => { loading.value = false }, 8000)

  try {
    const res = await store.fetchDetail(props.advId, { preserveDetail: hasCachedDetail })
    if (res?.stale_session) return
    if (!res || res.code !== 200) {
      message.error(res?.message || '加载失败')
      return
    }
    // fetchDetail 成功后 currentDetail 已更新，此时再判断是否 owner
    try {
      if (store.currentDetail?.owner_id === props.currentUserId) {
        const skillRes = await store.fetchSkills(props.advId)
        if (skillRes?.code === 200) ownerSkills.value = skillRes.data ?? []
      }
    } catch {
      // 技能加载失败不影响主详情展示
    }
    await loadHistoryStory(1)
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载失败')
  } finally {
    loading.value = false
  }
})

onUnmounted(() => {
  exportDisposed = true
  resumeBackgroundTitleAnimations()
  if (loadTimer) clearTimeout(loadTimer)
  if (socketRefreshTimer) clearTimeout(socketRefreshTimer)
  socket.off('adventurer_changed', onAdventurerChanged)
  socket.off('expedition_log_changed', onExpeditionLogChanged)
})

async function refreshSkills() {
  const res = await store.fetchSkills(props.advId)
  if (res.code === 200) ownerSkills.value = res.data ?? []
}

async function toggleFollow() {
  if (!detail.value || busy.value) return
  busy.value = true
  try {
    const res = detail.value.is_followed
      ? await store.unfollow(props.advId)
      : await store.follow(props.advId)
    if (res.code === 200) emit('changed')
    else message.error(res.message || '操作失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    busy.value = false
  }
}

async function onTitleChange(event: Event) {
  if (busy.value) return
  const value = (event.target as HTMLSelectElement).value
  const titleId = value ? Number(value) : null
  busy.value = true
  try {
    const res = await store.setActiveTitle(props.advId, titleId)
    if (res.code === 200) {
      message.success(titleId == null ? '已卸下称号' : '称号与边框已更换')
      emit('changed')
    } else {
      message.error(res.message || '称号更换失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '称号更换失败')
  } finally {
    busy.value = false
  }
}

async function setPreference(pref: string) {
  if (busy.value || ownerActionLocked.value || detail.value?.party_preference === pref) {
    if (ownerActionLocked.value) message.error(EXPLORING_BLOCKED_HINT)
    return
  }
  busy.value = true
  try {
    const res = await store.setPartyPreference(props.advId, pref)
    if (res.code === 200) {
      message.success(pref === 'auto' ? '已设为自动随机地图单人匹配' : '已设为手动匹配')
      emit('changed')
    } else {
      message.error(res.message || '设置失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '设置失败')
  } finally {
    busy.value = false
  }
}

// 取出/存入数量：默认 1，可改到堆叠上限；key 区分随身(eq)/收藏室(wh)
const moveQty = ref<Record<string, number>>({})
function qtyKey(prefix: 'eq' | 'wh', id: number) {
  return `${prefix}-${id}`
}
function qtyFor(prefix: 'eq' | 'wh', id: number, max = 1) {
  const v = moveQty.value[qtyKey(prefix, id)]
  if (v == null) return 1
  return Math.min(max, Math.max(1, v))
}
function onQtyInput(prefix: 'eq' | 'wh', id: number, max: number, event: Event) {
  const input = event.target as HTMLInputElement
  let value = parseInt(input.value, 10)
  if (Number.isNaN(value)) value = 1
  value = Math.min(max, Math.max(1, value))
  moveQty.value = { ...moveQty.value, [qtyKey(prefix, id)]: value }
  if (String(value) !== input.value) input.value = String(value)
}

async function equip(itemId: number, stackQty = 1) {
  const qty = stackQty > 1 ? qtyFor('wh', itemId, stackQty) : undefined
  await runManage(() => store.equipItem(props.advId, itemId, qty), '已装备')
}

async function unequip(itemId: number, stackQty = 1) {
  const qty = stackQty > 1 ? qtyFor('eq', itemId, stackQty) : undefined
  await runManage(() => store.unequipItem(props.advId, itemId, qty), '已卸下')
}

async function toggleItemProtection(
  source: 'equipment' | 'warehouse',
  item: { id: number; is_player_locked?: boolean },
) {
  const locked = !item.is_player_locked
  await runManage(
    () => store.setItemProtection(props.advId, source, item.id, locked),
    locked ? '已加保护锁' : '已解除保护锁',
  )
}

async function onEquipSkill(skillId: number) {
  if (busy.value || ownerActionLocked.value) {
    if (ownerActionLocked.value) message.error(EXPLORING_BLOCKED_HINT)
    return
  }
  busy.value = true
  try {
    const res = await store.equipSkill(props.advId, skillId)
    if (res.code === 200) {
      await refreshSkills()
    } else {
      message.error(res.message || '装备失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '装备失败')
  } finally {
    busy.value = false
  }
}

async function onUnequipSkill(skillId: number) {
  if (busy.value || ownerActionLocked.value) {
    if (ownerActionLocked.value) message.error(EXPLORING_BLOCKED_HINT)
    return
  }
  busy.value = true
  try {
    const res = await store.unequipSkill(props.advId, skillId)
    if (res.code === 200) {
      await refreshSkills()
    } else {
      message.error(res.message || '卸下失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '卸下失败')
  } finally {
    busy.value = false
  }
}

async function runManage(fn: () => Promise<any>, okMsg: string) {
  if (busy.value || ownerActionLocked.value) {
    if (ownerActionLocked.value) message.error(EXPLORING_BLOCKED_HINT)
    return
  }
  busy.value = true
  try {
    const res = await fn()
    if (res.code === 200) {
      message.success(okMsg)
      emit('changed')
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    busy.value = false
  }
}

async function onUnequipAllTraits() {
  if (busy.value || ownerActionLocked.value || !isOwner.value || !removableTraitCount.value) return
  busy.value = true
  const advId = props.advId
  try {
    const res = await store.unequipAllTraits(advId)
    if (props.advId !== advId) return
    if (res.code === 200) {
      confirmUnequipAll.value = false
      await store.fetchDetail(props.advId, { preserveDetail: true, force: true })
      message.success(`已卸下 ${res.data?.unequipped_count ?? 0} 项特质`)
      emit('changed')
    } else {
      message.error(res.message || '特质配置失败')
    }
  } catch {
    message.error('特质配置失败，请刷新后重试')
  } finally {
    busy.value = false
  }
}

async function onSetTraitEquipped(traitId: number, equipped: boolean) {
  if (busy.value || ownerActionLocked.value) {
    if (ownerActionLocked.value) message.error(EXPLORING_BLOCKED_HINT)
    return
  }
  busy.value = true
  try {
    const res = equipped
      ? await store.equipTrait(props.advId, traitId)
      : await store.unequipTrait(props.advId, traitId)
    if (res.code === 200) {
      await store.fetchDetail(props.advId, { preserveDetail: true, force: true })
      message.success(equipped ? '已携带该特质' : '已卸下该特质')
      emit('changed')
    } else {
      message.error(res.message || '特质配置失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '特质配置失败')
  } finally {
    busy.value = false
  }
}
</script>

<style scoped lang="scss">
.trait-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 12px;
  label { display: flex; align-items: center; gap: 8px; }
  select {
    min-height: 44px;
    max-width: 100%;
    border: 1px solid var(--color-border, #30363d);
    border-radius: 6px;
    background: var(--color-surface, #161b22);
    color: var(--color-text, #e6edf3);
  }
  .mini-btn { min-height: 44px; }
}
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1000;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(0, 0, 0, 0.55);

  &.elevated {
    z-index: 1450;
  }
}

.modal-card {
  width: 100%;
  min-width: 0;
  max-width: 600px;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  color: #e6edf3;
  color: var(--color-text, #e6edf3);
  border: 1px solid #30363d;
  border-color: var(--color-border, #30363d);
  border-radius: 12px;
  overflow: hidden;
  opacity: 1;
  box-shadow: 0 18px 56px rgba(0, 0, 0, 0.42);
}

.modal-card.title-frame--fate {
  border-color: #d9b96e;
  box-shadow: inset 0 0 0 1px rgba(139, 120, 201, 0.22), 0 18px 56px rgba(139, 120, 201, 0.2);
}

.modal-card.title-frame--fate_companion {
  border-color: #54d3b1;
  box-shadow: inset 0 0 0 1px rgba(240, 194, 92, 0.2), 0 18px 56px rgba(40, 168, 139, 0.2);
}

:global([data-theme="light"] .modal-card.title-frame--fate) {
  border-color: #9a6905;
  background:
    radial-gradient(circle at 72% 0, rgba(93, 58, 154, .085), transparent 32%),
    radial-gradient(circle at 10% 90%, rgba(167, 116, 8, .065), transparent 40%),
    linear-gradient(145deg, #fbf8f0, #ffffff 64%);
  box-shadow: inset 0 0 0 1px rgba(93, 58, 154, .15), 0 18px 56px rgba(82, 53, 0, .17);
}

:global([data-theme="light"] .modal-card.title-frame--fate_companion) {
  border-color: #167e69;
  background:
    radial-gradient(circle at 12% 9%, rgba(22, 126, 105, .085), transparent 34%),
    radial-gradient(circle at 90% 92%, rgba(165, 112, 0, .065), transparent 40%),
    linear-gradient(145deg, #f1faf7, #ffffff 64%);
  box-shadow: inset 0 0 0 1px rgba(165, 112, 0, .14), 0 18px 56px rgba(13, 101, 83, .16);
}

.modal-card.title-frame--fate .modal-head {
  border-bottom-color: color-mix(in srgb, #d9b96e 60%, #8b78c9);
  background: linear-gradient(
    90deg,
    var(--color-surface, #161b22) 0 58%,
    color-mix(in srgb, var(--color-surface, #161b22) 76%, transparent) 70%,
    color-mix(in srgb, var(--color-surface, #161b22) 18%, transparent) 83%,
    transparent 100%
  );
}

.modal-card.title-frame--fate_companion .modal-head {
  border-bottom-color: color-mix(in srgb, #54d3b1 62%, #f0c25c);
  background: linear-gradient(
    90deg,
    var(--color-surface, #161b22) 0 58%,
    color-mix(in srgb, var(--color-surface, #161b22) 76%, transparent) 70%,
    color-mix(in srgb, var(--color-surface, #161b22) 18%, transparent) 83%,
    transparent 100%
  );
}
.modal-card.title-frame--fate .modal-title,
.modal-card.title-frame--fate_companion .modal-title {
  max-width: calc(100% - 250px);
}


.modal-head {
  display: flex;
  flex-shrink: 0;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border);
}

.modal-title {
  display: flex;
  flex: 1;
  min-width: 0;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin: 0;
  font-size: 17px;
  color: var(--color-text);
  overflow-wrap: anywhere;

  .title-lv {
    font-size: 13px;
    color: var(--color-text-secondary);
    margin-left: 0;
  }
}

.modal-close {
  flex-shrink: 0;
  border: none;
  background: transparent;
  font-size: 18px;
  color: var(--color-text-secondary);
  cursor: pointer;

  &:hover {
    color: var(--color-text);
  }
}

.modal-body {
  flex: 1;
  min-height: 0;
  padding: 16px 18px;
  overflow-y: auto;
  -webkit-overflow-scrolling: touch;
  overflow-x: hidden;
  overscroll-behavior: contain;
}

.state {
  padding: 32px 0;
  text-align: center;
  color: var(--color-text-secondary);
}

.d-top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.d-status,
.d-tag {
  font-size: 12px;
  padding: 2px 10px;
  border-radius: 10px;
}

.d-tag {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
}

.history-btn,
.follow-btn {
  font-size: 12px;
  padding: 3px 12px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
    color: var(--color-text);
  }
}

.follow-btn {
  margin-left: auto;

  &.active {
    color: #f0883e;
    border-color: #f0883e;
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.d-hp {
  position: relative;
  height: 18px;
  background: var(--color-bg);
  border-radius: 9px;
  overflow: hidden;
  margin-bottom: 12px;
}

.d-hp-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #dc3545, #fd7e14);
}

.d-hp-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  text-shadow: 0 0 2px rgba(0, 0, 0, 0.6);
}

.d-stamina {
  position: relative;
  height: 14px;
  background: var(--color-bg);
  border-radius: 7px;
  overflow: hidden;
  margin-bottom: 12px;
}

.d-stamina-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: linear-gradient(90deg, #0d6efd, #20c997);
  transition: width 0.3s;
}

.d-stamina-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  text-shadow: 0 0 2px rgba(0, 0, 0, 0.6);
}

.d-stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(90px, 1fr));
  gap: 8px;
  margin-bottom: 8px;
}

.stat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 6px 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;

  .k {
    font-size: 11px;
    color: var(--color-text-secondary);
  }

  .v {
    font-size: 14px;
    font-weight: 700;
    color: var(--color-text);

    .cap-hint {
      margin-left: 2px;
      font-size: 11px;
      font-weight: 500;
      color: var(--color-text-secondary);
    }
  }
}

.cap-tag {
  margin-left: 4px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.exploring-block-hint {
  margin: 12px 0 0;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-warning, #e6a817) 12%, var(--color-surface));
  border: 1px solid color-mix(in srgb, var(--color-warning, #e6a817) 35%, var(--color-border));
}

.d-section {
  margin-top: 16px;

  h4 {
    margin: 0 0 8px;
    font-size: 14px;
    color: var(--color-text);
  }
}

.muted {
  color: var(--color-text-secondary);
  font-weight: 400;

  &.small {
    font-size: 12px;
  }
}

.capacity-badge {
  margin-left: 6px;
  font-size: 12px;
  font-weight: 500;
  color: var(--color-text-secondary);
}

.capacity-near {
  color: var(--color-warning);
}

.capacity-full {
  color: var(--color-danger);
}

.capacity-hint {
  margin: 0 0 8px;
}

.section-heading-label {
  display: inline-flex;
  min-width: 0;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 5px;
}

.trait-count {
  padding: 0 6px;
  border: 1px solid currentColor;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
  line-height: 1.6;
  white-space: nowrap;
}

.trait-count-equipped {
  color: var(--color-success);
}

.trait-count-unequipped {
  color: var(--color-text-secondary);
  border-style: dashed;
}

.section-collapse-btn {
  display: inline-flex;
  min-height: 28px;
  align-items: center;
  gap: 4px;
  margin-left: auto;
  padding: 3px 9px;
  border: 1px solid color-mix(in srgb, var(--detail-blue) 28%, var(--color-border));
  border-radius: 999px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  font-size: 11px;
  font-weight: 650;
  cursor: pointer;
}

.section-collapse-btn:hover {
  border-color: var(--detail-blue);
  color: var(--detail-blue);
}

.section-collapse-btn:focus-visible {
  outline: 2px solid var(--detail-blue);
  outline-offset: 2px;
}

.section-collapse-content {
  min-width: 0;
}

.active-title-badge {
  display: inline-flex;
  margin-left: 8px;
  padding: 2px 7px;
  border: 1px solid currentColor;
  border-radius: 999px;
  color: #d9b96e;
  font-size: 11px;
  vertical-align: middle;
}

.title-picker {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 6px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.title-picker select {
  min-width: 0;
  max-width: 240px;
  padding: 4px 8px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 8px;
  background: var(--color-bg, #0d1117);
  color: var(--color-text, #e6edf3);
}

.warehouse-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
  padding: 9px;
  border: 1px solid color-mix(in srgb, var(--detail-blue) 16%, var(--color-border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--detail-blue) 5%, var(--color-surface));
}

.rarity-filter {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  min-width: 0;
}

.filter-chip {
  padding: 5px 8px;
  border: 1px solid var(--color-border);
  border-radius: 999px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  font-size: 11px;
  cursor: pointer;
  transition: border-color 0.16s ease, background-color 0.16s ease, color 0.16s ease, transform 0.16s ease;

  span {
    margin-left: 3px;
    opacity: 0.72;
  }

  &:hover {
    border-color: var(--rarity-filter-color, var(--detail-blue));
    color: var(--rarity-filter-color, var(--detail-blue));
    transform: translateY(-1px);
  }

  &.active {
    border-color: var(--rarity-filter-color, var(--detail-blue));
    background: color-mix(in srgb, var(--rarity-filter-color, var(--detail-blue)) 14%, var(--color-bg));
    color: var(--rarity-filter-color, var(--detail-blue));
    font-weight: 700;
  }
}

.warehouse-sort {
  display: flex;
  flex: none;
  align-items: center;
  gap: 6px;
  color: var(--color-text-secondary);
  font-size: 11px;

  select {
    min-height: 30px;
    padding: 4px 28px 4px 8px;
    border: 1px solid var(--color-border);
    border-radius: 8px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 12px;
  }
}

.warehouse-filter-empty {
  margin-bottom: 8px;
  padding: 18px 12px;
  border: 1px dashed var(--color-border);
  border-radius: 8px;
  color: var(--color-text-secondary);
  font-size: 12px;
  text-align: center;
}

.entry {
  min-width: 0;
  padding: 8px 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  margin-bottom: 6px;
}

.entry-line {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  min-width: 0;
}

.entry-line > strong,
.entry-desc {
  min-width: 0;
  overflow-wrap: anywhere;
}

.asset-recovery {
  display: inline-block;
  width: fit-content;
  margin-top: 5px;
  padding: 2px 7px;
  border: 1px solid rgba(111, 66, 193, 0.45);
  border-radius: 999px;
  color: #8d64c7;
  background: rgba(111, 66, 193, 0.08);
  font-size: 11px;
  font-weight: 600;
}

.clue-index-pill {
  color: var(--color-accent, #58a6ff);
  border-color: var(--color-accent, #58a6ff);
  font-weight: 600;
}

.entry-desc {
  display: block;
  margin-top: 4px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
}

.trait-recovery {
  margin-top: 6px;
  padding: 6px 8px;
  border-radius: 6px;
  border: 1px dashed var(--color-border);
  background: var(--color-surface);
}

.trait-recovery.active {
  border-color: #6f42c1;
  background: rgba(111, 66, 193, 0.08);
}

.trait-recovery-status {
  display: block;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
}

.trait-recovery.active .trait-recovery-status {
  color: #6f42c1;
}

.trait-recovery-hint {
  display: block;
  margin-top: 2px;
  font-size: 11px;
  line-height: 1.4;
  color: var(--color-text-secondary);
}

.entry-actions {
  margin-left: auto;
  display: flex;
  gap: 6px;
  min-width: 0;
}

.mini-btn {
  font-size: 11px;
  padding: 2px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-text);
  cursor: pointer;

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.qty-input {
  width: 46px;
  font-size: 11px;
  padding: 2px 4px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-text);
  text-align: center;

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.skill-divider {
  margin: 10px 0 6px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.entry.skill-unequipped,
.entry.trait-unequipped {
  opacity: 0.75;
  border-style: dashed;
}

.trait-not-removable {
  color: #f0aaa0;
  border: 1px solid rgba(240, 170, 160, 0.3);
}

.trait-loadout-state {
  align-self: center;
  padding: 0 6px;
  border: 1px solid currentColor;
  border-radius: 10px;
  font-size: 11px;
  line-height: 1.6;
  white-space: nowrap;

  &.equipped {
    color: var(--color-success);
  }

  &.unequipped {
    color: var(--color-text-secondary);
    border-style: dashed;
  }
}

.pref-toggle {
  display: flex;
  gap: 8px;
}

.pref-btn {
  flex: 1;
  font-size: 13px;
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;

  &.active {
    border-color: var(--color-primary);
    color: var(--color-primary);
    font-weight: 600;
  }

  &:disabled {
    cursor: default;
  }

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
  }
}

.pref-hint {
  margin: 6px 0 0;
  line-height: 1.5;
}

.rarity-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 0 6px;
  border: 1px solid currentColor;
  border-radius: 10px;
}

.uses {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.history {
  margin: 0;
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text-secondary);
  white-space: pre-wrap;
}

.history-entry {
  padding: 8px 10px;
  margin-bottom: 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;

  &:last-of-type {
    margin-bottom: 0;
  }
}

.history-error {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;

  p {
    margin: 0;
  }
}

.btn-retry {
  font-size: 12px;
  padding: 4px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
  cursor: pointer;

  &:hover {
    border-color: var(--color-primary);
    color: var(--color-primary);
  }
}

.relation-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 8px;
}

.relation-tile {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}

.relation-icon {
  font-size: 16px;
}

.relation-label {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.relation-name {
  flex: 1;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  text-align: right;
}

.material-hint {
  margin: -4px 0 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.material-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(140px, 1fr));
  gap: 8px;
}

.material-tile {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}

.material-icon {
  font-size: 18px;
}

.material-name {
  flex: 1;
  font-size: 13px;
  color: var(--color-text);
}

.material-qty {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-primary);
}

/* ── 移动端适配 ── */
@media (max-width: 640px) {
  .modal-overlay {
    padding: 8px;
  }

  .modal-card {
    max-height: 92vh;
  }

  .modal-head {
    padding: 12px 14px;
  }

  .modal-body {
    padding: 12px 14px;
  }

  .d-stats {
    grid-template-columns: repeat(3, 1fr);
  }

  .trait-recovery {
    padding: 8px 10px;
  }

  .trait-recovery-status {
    font-size: 13px;
    line-height: 1.45;
  }

  .trait-recovery-hint {
    font-size: 12px;
    line-height: 1.45;
  }
}

.item-protection-badge {
  color: #e0b45d;
  border: 1px solid rgba(224, 180, 93, 0.55);
  border-radius: 999px;
  padding: 1px 6px;
  font-size: 0.72rem;
  line-height: 1.35;
  white-space: nowrap;
}

.protection-btn.active {
  color: #e8c77f;
  border-color: rgba(224, 180, 93, 0.65);
}

/* 冒险者档案视觉层 */
.modal-overlay { --detail-blue: color-mix(in srgb, #5d87b3 76%, var(--color-primary)); }
.modal-card {
  max-width: 720px;
  border-color: color-mix(in srgb, var(--detail-blue) 26%, var(--color-border));
  border-radius: 18px;
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 5%, transparent), 0 26px 72px rgba(0, 0, 0, 0.3);
  animation: adventurer-dossier-in 0.26s cubic-bezier(0.2, 0.8, 0.2, 1) both;
}

/*
 * color-mix() 在旧 WebView 中可能到 computed-value 阶段才失效。
 * 若直接用 background 简写，会把上方的实色 background-color 一并重置为透明。
 */
@supports (color: color-mix(in srgb, black, white)) {
  .modal-card {
    background:
      radial-gradient(circle at 92% 0%, color-mix(in srgb, var(--detail-blue) 10%, transparent), transparent 24%),
      var(--color-surface);
  }
}
.modal-head {
  position: relative;
  min-height: 72px;
  padding: 17px 20px;
  border-bottom-color: color-mix(in srgb, var(--detail-blue) 22%, var(--color-border));
  background: linear-gradient(115deg, color-mix(in srgb, var(--detail-blue) 10%, var(--color-bg)), var(--color-surface));
}
.modal-head::before {
  content: 'ADVENTURER DOSSIER';
  position: absolute;
  left: 20px;
  bottom: 8px;
  color: var(--detail-blue);
  font-size: 8px;
  font-weight: 800;
  letter-spacing: 0.16em;
}
.modal-title { padding-bottom: 11px; font-size: 20px; letter-spacing: 0.02em; }
.modal-title .title-lv {
  padding: 2px 7px;
  border: 1px solid color-mix(in srgb, var(--detail-blue) 40%, var(--color-border));
  border-radius: 5px;
  color: var(--detail-blue);
}
.modal-close {
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid color-mix(in srgb, var(--detail-blue) 20%, var(--color-border));
  border-radius: 9px;
  background: var(--color-bg);
}
.d-top {
  padding: 10px;
  border: 1px solid color-mix(in srgb, var(--detail-blue) 15%, var(--color-border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--detail-blue) 4%, var(--color-bg));
}
.d-hp,
.d-stamina { border: 1px solid color-mix(in srgb, var(--detail-blue) 12%, var(--color-border)); border-radius: 5px; }
.d-hp-fill { background: linear-gradient(90deg, #a9324c, #d45c55, #e28a5e); }
.d-stamina-fill { background: linear-gradient(90deg, #4779ad, #5b9fba, #70b7a8); }
.stat {
  min-width: 0;
  padding: 9px 10px;
  border-color: color-mix(in srgb, var(--detail-blue) 14%, var(--color-border));
  background: color-mix(in srgb, var(--detail-blue) 4%, var(--color-bg));
}
.detail-nav {
  position: sticky;
  top: -16px;
  z-index: 4;
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(72px, 1fr));
  gap: 7px;
  margin: 12px -4px 4px;
  padding: 8px;
  border: 1px solid color-mix(in srgb, var(--detail-blue) 34%, var(--color-border));
  border-radius: 11px;
  background: color-mix(in srgb, var(--color-surface) 96%, var(--detail-blue));
  box-shadow: 0 7px 18px rgba(0, 0, 0, 0.12), inset 0 1px color-mix(in srgb, var(--color-text) 5%, transparent);
  backdrop-filter: blur(8px);
}
.detail-nav button {
  position: relative;
  min-width: 0;
  min-height: 38px;
  padding: 8px 7px;
  border: 1px solid color-mix(in srgb, var(--detail-blue) 22%, var(--color-border));
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
  font-size: 12px;
  font-weight: 700;
  cursor: pointer;
  box-shadow: 0 1px 2px rgba(0, 0, 0, 0.08);
  transition: border-color 0.16s ease, background-color 0.16s ease, color 0.16s ease, box-shadow 0.16s ease, transform 0.16s ease;
}
.detail-nav button::after {
  content: '';
  position: absolute;
  right: 8px;
  bottom: 5px;
  left: 8px;
  height: 2px;
  border-radius: 99px;
  background: transparent;
}
.detail-nav button:hover {
  border-color: color-mix(in srgb, var(--detail-blue) 65%, var(--color-border));
  background: color-mix(in srgb, var(--detail-blue) 9%, var(--color-bg));
  color: var(--detail-blue);
  transform: translateY(-1px);
}
.detail-nav button:focus-visible {
  outline: 2px solid var(--detail-blue);
  outline-offset: 2px;
}
.detail-nav button:active { transform: translateY(0); }
.detail-nav button.active {
  border-color: var(--detail-blue);
  background: color-mix(in srgb, var(--detail-blue) 16%, var(--color-bg));
  color: var(--detail-blue);
  box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--detail-blue) 25%, transparent), 0 3px 9px color-mix(in srgb, var(--detail-blue) 14%, transparent);
}
.detail-nav button.active::after { background: var(--detail-blue); }
.d-section {
  scroll-margin-top: 62px;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--detail-blue) 14%, var(--color-border));
  border-radius: 12px;
  background: linear-gradient(110deg, color-mix(in srgb, var(--detail-blue) 4%, var(--color-bg)), var(--color-bg));
}
.d-section h4 {
  display: flex;
  align-items: center;
  gap: 8px;
  padding-bottom: 8px;
  border-bottom: 1px solid color-mix(in srgb, var(--detail-blue) 12%, var(--color-border));
}
.d-section h4::before {
  content: '';
  width: 16px;
  height: 2px;
  flex: none;
  background: var(--detail-blue);
  box-shadow: 0 0 8px color-mix(in srgb, var(--detail-blue) 35%, transparent);
}
.entry { border-color: color-mix(in srgb, var(--detail-blue) 12%, var(--color-border)); background: color-mix(in srgb, var(--color-surface) 46%, var(--color-bg)); }
.entry:hover { border-color: color-mix(in srgb, var(--detail-blue) 34%, var(--color-border)); }
.detail-section-history .history-entry {
  position: relative;
  margin-left: 10px;
  padding: 9px 11px 9px 18px;
  border-left: 2px solid color-mix(in srgb, var(--detail-blue) 42%, var(--color-border));
  background: color-mix(in srgb, var(--detail-blue) 3%, var(--color-bg));
}
.detail-section-history .history-entry::before {
  content: '';
  position: absolute;
  top: 15px;
  left: -6px;
  width: 8px;
  height: 8px;
  border: 2px solid var(--color-surface);
  border-radius: 50%;
  background: var(--detail-blue);
}
@keyframes adventurer-dossier-in { from { opacity: 0; transform: translateY(12px) scale(0.988); } }

@media (max-width: 640px) {
  .modal-overlay { align-items: flex-end; padding: 0; }
  .modal-card {
    max-width: none;
    max-height: 96dvh;
    border-right: 0;
    border-bottom: 0;
    border-left: 0;
    border-radius: 18px 18px 0 0;
  }
  .modal-head { padding: 12px 14px; }
  .modal-head::before { left: 14px; }
  .modal-card.title-frame--fate .modal-title,
  .modal-card.title-frame--fate_companion .modal-title {
    max-width: calc(100% - 156px);
  }
  .modal-body { padding: 10px 12px max(14px, env(safe-area-inset-bottom)); }
  .detail-nav { top: -12px; margin-inline: -2px; }
  .detail-nav button { padding-inline: 3px; }
  .d-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .d-section { padding: 10px; scroll-margin-top: 58px; }
  .entry { padding: 8px; }
  .entry-line { align-items: center; gap: 5px 6px; }
  .entry-actions { width: auto; margin-left: auto; justify-content: flex-end; }
  .warehouse-toolbar { align-items: stretch; flex-direction: column; }
  .warehouse-sort { justify-content: space-between; }
  .warehouse-sort select { flex: 1; min-width: 0; }
  .equipment-actions.has-quantity { flex-basis: 100%; }
  .rarity-pill { padding-inline: 5px; }
  .qty-input { width: 52px; min-height: 34px; box-sizing: border-box; }
  .mini-btn { min-height: 34px; }
}

.modal-card.title-frame--fate {
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  background:
    repeating-linear-gradient(135deg, rgba(217, 185, 110, 0.022) 0 1px, transparent 1px 28px),
    radial-gradient(circle at 84% 14%, rgba(217, 185, 110, 0.11), transparent 32%),
    radial-gradient(circle at 10% 90%, rgba(139, 120, 201, 0.1), transparent 40%),
    linear-gradient(145deg, rgba(139, 120, 201, 0.03), transparent 64%),
    var(--color-surface, #161b22);
}

.modal-card.title-frame--fate_companion {
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  background:
    radial-gradient(circle at 14% 10%, rgba(84, 211, 177, 0.13), transparent 33%),
    radial-gradient(circle at 88% 88%, rgba(240, 194, 92, 0.11), transparent 39%),
    repeating-linear-gradient(124deg, rgba(84, 211, 177, 0.022) 0 1px, transparent 1px 30px),
    linear-gradient(145deg, rgba(42, 148, 125, 0.04), transparent 64%),
    var(--color-surface, #161b22);
}

.detail-companion-atmosphere {
  position: absolute;
  z-index: 0;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  background:
    radial-gradient(ellipse at 14% 12%, rgba(84, 211, 177, 0.13), transparent 36%),
    radial-gradient(ellipse at 86% 88%, rgba(240, 194, 92, 0.11), transparent 42%);
}


.title-frame-ornaments {
  position: absolute;
  z-index: 1;
  inset: 0;
  overflow: visible;
  border-radius: inherit;
  pointer-events: none;
}

.modal-card > :not(.title-frame-ornaments) {
  position: relative;
  z-index: 2;
}

.modal-card > .detail-fate-atmosphere {
  position: absolute;
  z-index: 0;
  inset: 0;
  overflow: hidden;
  pointer-events: none;
  background:
    radial-gradient(ellipse at 80% 12%, rgba(217, 185, 110, 0.11), transparent 35%),
    radial-gradient(ellipse at 12% 88%, rgba(139, 120, 201, 0.1), transparent 42%);
}

.detail-fate-atmosphere::before,
.detail-fate-atmosphere::after {
  content: '';
  position: absolute;
  pointer-events: none;
}

.detail-fate-atmosphere::before {
  top: -58%;
  right: -34%;
  width: 96%;
  aspect-ratio: 1;
  border-radius: 50%;
  background:
    conic-gradient(
      from 34deg,
      transparent 0 18%,
      rgba(217, 185, 110, 0.07) 23%,
      transparent 31% 56%,
      rgba(139, 120, 201, 0.1) 62%,
      transparent 70% 100%
    );
  animation: detail-fate-orbit 22s linear infinite;
}

.detail-fate-atmosphere::after {
  inset: -24%;
  background-image:
    radial-gradient(circle, rgba(255, 240, 181, 0.78) 0 1px, transparent 1.8px),
    radial-gradient(circle, rgba(166, 145, 224, 0.68) 0 1.1px, transparent 2px),
    radial-gradient(circle, rgba(217, 185, 110, 0.42) 0 0.9px, transparent 1.7px);
  background-position: 18px 30px, 74px 46px, 126px 88px;
  background-size: 96px 96px, 151px 151px, 213px 213px;
  opacity: 0.46;
  transform: translate3d(4%, -4%, 0);
  animation: detail-fate-stars 23s linear infinite;
}

[data-theme="light"] .detail-fate-atmosphere {
  background:
    radial-gradient(ellipse at 80% 12%, rgba(151, 101, 0, 0.07), transparent 35%),
    radial-gradient(ellipse at 12% 88%, rgba(93, 58, 154, 0.06), transparent 42%);
}

[data-theme="light"] .detail-fate-atmosphere::after {
  background-image:
    radial-gradient(circle, rgba(126, 81, 0, 0.46) 0 1px, transparent 1.8px),
    radial-gradient(circle, rgba(93, 58, 154, 0.4) 0 1.1px, transparent 2px),
    radial-gradient(circle, rgba(151, 101, 0, 0.28) 0 0.9px, transparent 1.7px);
  opacity: 0.34;
}
.modal-card.title-frame--fate,
.modal-card.title-frame--fate_companion { position: relative; }

.title-frame-rail,
.title-frame-edge-mark,
.title-frame-fate-weave { position: absolute; display: block; }

.title-frame-fate-weave {
  inset: 0;
  width: 100%;
  height: 100%;
}

.fate-frame-line {
  fill: none;
  vector-effect: non-scaling-stroke;
  stroke-linecap: round;
  stroke-linejoin: round;
}

.fate-frame-line-outer {
  stroke: #d9b96e;
  stroke-width: 4.5;
  filter: drop-shadow(0 0 6px rgba(217, 185, 110, 0.58));
}

.fate-frame-line-inner {
  stroke: #8b78c9;
  stroke-width: 2.4;
  filter: drop-shadow(0 0 5px rgba(139, 120, 201, 0.66));
}

.rail-top,
.rail-bottom {
  left: 0;
  width: 100%;
  height: 18px;
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='52' height='18' viewBox='0 0 52 18'%3E%3Cpath d='M0 9 Q13 1 26 9 T52 9' fill='none' stroke='white' stroke-width='7' stroke-linecap='round'/%3E%3C/svg%3E") center / 52px 18px repeat-x;
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='52' height='18' viewBox='0 0 52 18'%3E%3Cpath d='M0 9 Q13 1 26 9 T52 9' fill='none' stroke='white' stroke-width='7' stroke-linecap='round'/%3E%3C/svg%3E") center / 52px 18px repeat-x;
}
.rail-top { top: 0; }
.rail-bottom { bottom: 0; transform: scaleY(-1); }
.rail-left,
.rail-right {
  top: 0;
  width: 18px;
  height: 100%;
  -webkit-mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='52' viewBox='0 0 18 52'%3E%3Cpath d='M9 0 Q17 13 9 26 T9 52' fill='none' stroke='white' stroke-width='7' stroke-linecap='round'/%3E%3C/svg%3E") center / 18px 52px repeat-y;
  mask: url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='18' height='52' viewBox='0 0 18 52'%3E%3Cpath d='M9 0 Q17 13 9 26 T9 52' fill='none' stroke='white' stroke-width='7' stroke-linecap='round'/%3E%3C/svg%3E") center / 18px 52px repeat-y;
}
.rail-left { left: 0; }
.rail-right { right: 0; transform: scaleX(-1); }

.title-frame--fate .title-frame-edge-mark {
  top: 86px;
  right: -34px;
  left: auto;
  width: 48px;
  height: 66px;
  border: 1px solid color-mix(in srgb, #d9b96e 76%, #8b78c9);
  border-right: 0;
  border-radius: 55% 0 0 55%;
  background: radial-gradient(circle at 42% 50%, #5c4d7f 0 18%, #29243d 20% 52%, #b9974f 54% 58%, #201c30 61%);
  box-shadow: -4px 0 16px rgba(139, 120, 201, 0.44), inset 3px 0 8px rgba(255, 240, 181, 0.2);
}

.title-frame--fate .title-frame-edge-mark::before {
  content: '';
  position: absolute;
  top: 13px;
  left: 10px;
  width: 27px;
  height: 37px;
  border: 1px solid rgba(255, 240, 181, 0.72);
  border-radius: 50%;
  background: conic-gradient(from 22.5deg, transparent 0 12%, rgba(217, 185, 110, 0.75) 13% 15%, transparent 16% 37%, rgba(139, 120, 201, 0.9) 38% 41%, transparent 42% 100%);
}

.title-frame--fate .title-frame-edge-mark::after {
  content: '';
  position: absolute;
  top: 27px;
  left: 19px;
  width: 10px;
  height: 10px;
  border: 1px solid #fff0b5;
  background: #8b78c9;
  box-shadow: 0 0 8px rgba(255, 240, 181, 0.72);
  transform: rotate(45deg);
}

@keyframes title-frame-flow-x { to { background-position: 200% 0; } }
@keyframes title-frame-flow-y { to { background-position: 0 200%; } }
@keyframes detail-fate-orbit { to { transform: rotate(360deg); } }
@keyframes detail-fate-stars {
  to {
    transform: translate3d(-4%, 4%, 0);
  }
}

:global(html.s1-adventurer-detail-open .adv-card-shell),
:global(html.s1-adventurer-detail-open .adv-card-shell *),
:global(html.s1-adventurer-detail-open .adv-card::before),
:global(html.s1-adventurer-detail-open .adv-card::after),
:global(html.s1-adventurer-detail-open .title-card-atmosphere::before),
:global(html.s1-adventurer-detail-open .title-card-atmosphere::after),
:global(html.s1-adventurer-detail-open .pq-member-crest[class*="title-mini--"]),
:global(html.s1-adventurer-detail-open .pq-member-crest[class*="title-mini--"]::before),
:global(html.s1-adventurer-detail-open .pq-member-crest[class*="title-mini--"]::after) {
  animation-play-state: paused;
}

@media (prefers-reduced-motion: reduce) {
  .modal-card,
  .title-frame-rail,
  .detail-fate-atmosphere::before,
  .detail-fate-atmosphere::after { animation: none; }
}

.modal-card.title-frame--hidden_relic {
  position: relative;
  border-color: #a98959;
  background: radial-gradient(ellipse at 88% 0, rgba(185, 154, 232, .09), transparent 36%), var(--color-surface, #161b22);
  box-shadow: inset 0 0 0 1px rgba(185, 154, 232, .12), 0 18px 56px rgba(55, 32, 75, .22);
}
.modal-card.title-frame--hidden_relic .modal-title { max-width: calc(100% - 190px); }
.modal-card.title-frame--hidden_relic .modal-head {
  border-bottom-color: rgba(209, 175, 113, .38);
  background: linear-gradient(90deg, var(--color-surface, #161b22) 0 56%, transparent 86%);
}
.modal-card.title-frame--hidden_relic .active-title-badge { color: #e0bb7d; border-color: #9d7d51; }
:global([data-theme="light"] .modal-card.title-frame--hidden_relic) {
  border-color: #a3824d;
  background: radial-gradient(ellipse at 88% 0, rgba(121, 80, 174, .07), transparent 36%), var(--color-surface, #fff);
}
:global([data-theme="light"] .modal-card.title-frame--hidden_relic .active-title-badge) { color: #825820; }
@media (max-width: 640px) {
  .modal-card.title-frame--hidden_relic .modal-title { max-width: calc(100% - 142px); }
}

@media (max-width: 640px) {
  .modal-title { min-width: 0; overflow-wrap: anywhere; }
  .modal-head { gap: 8px; }
  .title-picker { width: 100%; }
  .title-picker select { flex: 1; width: 0; max-width: none; }
  .detail-nav { position: static; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 4px; }
  .detail-nav button { min-width: 0; min-height: 32px; padding: 5px 8px; border-radius: 6px; font-size: 12px; white-space: normal; }
  .d-section { scroll-margin-top: 12px; }
  .d-section h4 { font-size: 14px; flex-wrap: wrap; gap: 6px; }
  .entry-line { flex-wrap: wrap; align-items: flex-start; }
  .entry-actions, .equipment-actions.has-quantity { width: 100%; min-width: 0; flex-wrap: wrap; justify-content: flex-end; gap: 6px; margin-left: 0; }
  .entry-actions > button { flex: 0 1 auto; min-height: 30px; padding: 4px 8px; font-size: 12px; }
  .qty-input { width: 68px; }
  .rarity-filter { gap: 6px; }
  .filter-chip { flex: 0 1 auto; min-height: 28px; padding: 4px 7px; font-size: 11px; }
  .entry-desc { font-size: 13px; line-height: 1.65; }
  .relation-grid, .material-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
}
</style>
