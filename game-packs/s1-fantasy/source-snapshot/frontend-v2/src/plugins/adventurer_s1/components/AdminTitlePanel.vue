<template>
  <div class="title-admin-panel">
    <div class="panel-head">
      <div>
        <h6>🏷️ 称号与边框</h6>
        <p>系统资格由 uid=3 与命途完成状态自动判定；管理员授予不会覆盖自动资格。</p>
      </div>
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="loadTitles">刷新</button>
    </div>

    <form class="create-workbench" @submit.prevent="createTitle">
      <div class="workbench-heading">
        <div>
          <span class="section-kicker">NEW TITLE</span>
          <h6>新建称号</h6>
          <p>创建目录条目后，再从下方为冒险者授予使用资格。</p>
        </div>
        <span class="frame-preview" :class="`preview-${draft.frame_key}`">{{ frameLabel(draft.frame_key) }}</span>
      </div>
      <div class="create-form">
        <label class="field-group">
          <span>称号代码 <em>必填</em></span>
          <input v-model.trim="draft.code" maxlength="50" placeholder="例如 abyss_watcher" />
        </label>
        <label class="field-group">
          <span>称号名称 <em>必填</em></span>
          <input v-model.trim="draft.name" maxlength="50" placeholder="玩家看到的称号名称" />
        </label>
        <label class="field-group description-field">
          <span>称号描述</span>
          <input v-model.trim="draft.description" maxlength="200" placeholder="简要说明来源或身份含义" />
        </label>
        <label class="field-group">
          <span>边框样式</span>
          <select v-model="draft.frame_key">
            <option v-for="frame in frameOptions" :key="frame" :value="frame">{{ frameLabel(frame) }}</option>
          </select>
        </label>
        <button type="submit" class="btn-primary btn-sm create-button" :disabled="saving">新增称号</button>
      </div>
    </form>

    <div class="title-grid">
      <article v-for="title in titles" :key="title.id" class="title-card" :class="`preview-${title.frame_key}`">
        <header class="title-card-head">
          <div class="title-main">
            <span class="frame-mark" aria-hidden="true">◇</span>
            <div>
              <strong>{{ title.name }}</strong>
              <code>{{ title.code }}</code>
            </div>
          </div>
          <div class="title-badges">
            <span v-if="title.is_system" class="system-badge">系统称号</span>
            <span class="state-badge" :class="{ disabled: !title.is_enabled }">{{ title.is_enabled ? '使用中' : '已停用' }}</span>
          </div>
        </header>
        <div class="title-fields">
          <label class="field-group">
            <span>称号名称</span>
            <input v-model.trim="title.name" maxlength="50" />
          </label>
          <label class="field-group title-description-field">
            <span>称号描述</span>
            <input v-model.trim="title.description" maxlength="200" />
          </label>
          <label class="field-group">
            <span>边框样式</span>
            <select v-model="title.frame_key">
              <option v-for="frame in frameOptions" :key="frame" :value="frame">{{ frameLabel(frame) }}</option>
            </select>
          </label>
        </div>
        <footer class="title-card-footer">
          <div class="title-meta">
            <label class="enabled"><input v-model="title.is_enabled" type="checkbox" /> 允许玩家使用</label>
            <span class="counts"><strong>{{ title.automatic_holder_count ?? 0 }}</strong> 自动资格 · <strong>{{ title.manual_grant_count ?? 0 }}</strong> 手工授予 · <strong>{{ title.exchange_grant_count ?? 0 }}</strong> 藏品兑换</span>
          </div>
          <div class="row-actions">
            <button type="button" class="btn-primary btn-sm" :disabled="saving" @click="saveTitle(title)">保存修改</button>
            <button v-if="!title.is_system" type="button" class="btn-danger btn-sm" :disabled="saving" @click="deleteTitle(title)">删除称号</button>
          </div>
        </footer>
      </article>
      <p v-if="!loading && titles.length === 0" class="empty">暂无称号。</p>
    </div>

    <section class="grant-workbench">
      <div class="workbench-heading compact-heading">
        <div>
          <span class="section-kicker">TITLE ACCESS</span>
          <h6>授予与撤销</h6>
          <p>搜索冒险者后维护其授予或兑换的称号；系统自动资格只能查看，不能人工撤销。</p>
        </div>
      </div>
      <div class="search-row">
        <input v-model.trim="adventurerSearch" placeholder="冒险者名称" @keyup.enter="searchAdventurers" />
        <button type="button" class="btn-secondary btn-sm" :disabled="saving" @click="searchAdventurers">搜索</button>
      </div>
      <div v-if="adventurerCandidates.length" class="candidate-list">
        <button
          v-for="adv in adventurerCandidates"
          :key="adv.id"
          type="button"
          :class="{ active: selectedAdventurerId === adv.id }"
          @click="selectAdventurer(adv.id)"
        ><span>#{{ adv.id }}</span>{{ adv.name }}</button>
      </div>

      <template v-if="adventurerState">
        <div class="selected-name">
          <span>当前冒险者</span>
          <strong>#{{ adventurerState.adventurer.id }} {{ adventurerState.adventurer.name }}</strong>
          <small v-if="adventurerState.active_title">正在展示「{{ adventurerState.active_title.name }}」</small>
          <small v-else>当前未展示称号</small>
        </div>
        <div class="grant-list">
          <label v-for="title in adventurerState.titles" :key="title.id" :class="{ disabled: !title.is_enabled }">
            <input
              type="checkbox"
              :checked="hasTitle(title)"
              :disabled="saving || !title.is_enabled || isAutomatic(title)"
              @change="toggleGrant(title)"
            />
            <span class="grant-title-name">{{ title.name }}</span>
            <small v-if="isAutomatic(title)">自动资格</small>
            <small v-else-if="isExchanged(title)">藏品兑换</small>
            <small v-else-if="isManual(title)">管理员授予</small>
          </label>
        </div>
      </template>
    </section>
  </div>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useAdventurerS1Store, type AdventurerData, type AdventurerTitle } from '../stores/adventurerS1'

interface AdminTitle extends AdventurerTitle {
  is_system: boolean
  is_enabled: boolean
  manual_grant_count: number
  exchange_grant_count: number
  automatic_holder_count: number
}

interface AdventurerTitleState {
  adventurer: { id: number; name: string; owner_id: number | null }
  active_title: AdventurerTitle | null
  manual_title_ids: number[]
  exchange_title_ids: number[]
  automatic_title_codes: string[]
  titles: AdminTitle[]
}

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const loading = ref(false)
const saving = ref(false)
const titles = ref<AdminTitle[]>([])
const frameOptions = ref<string[]>(['standard', 'fate', 'fate_companion', 'hidden_relic'])
const draft = ref({ code: '', name: '', description: '', frame_key: 'standard' })
const adventurerSearch = ref('')
const adventurerCandidates = ref<AdventurerData[]>([])
const selectedAdventurerId = ref<number | null>(null)
const adventurerState = ref<AdventurerTitleState | null>(null)

function frameLabel(frame: string) {
  return ({
    standard: '普通边框',
    fate: '命途边框',
    fate_companion: '执灯者边框',
    hidden_relic: '十一印秘匣边框',
  } as Record<string, string>)[frame] ?? frame
}

async function loadTitles() {
  loading.value = true
  try {
    const res = await store.adminFetchTitles()
    if (res.code !== 200) throw new Error(res.message || '加载失败')
    titles.value = res.data?.titles ?? []
    frameOptions.value = res.data?.frame_options ?? frameOptions.value
  } catch (error: any) {
    message.error(error?.message || '加载称号失败')
  } finally {
    loading.value = false
  }
}

async function createTitle() {
  if (!draft.value.code || !draft.value.name) {
    message.error('请填写 code 和称号名称')
    return
  }
  saving.value = true
  try {
    const res = await store.adminCreateTitle(draft.value)
    if (res.code !== 200) throw new Error(res.message || '创建失败')
    draft.value = { code: '', name: '', description: '', frame_key: 'standard' }
    await loadTitles()
    message.success('称号已创建')
  } catch (error: any) {
    message.error(error?.message || '创建失败')
  } finally {
    saving.value = false
  }
}

async function saveTitle(title: AdminTitle) {
  saving.value = true
  try {
    const res = await store.adminUpdateTitle(title.id, {
      name: title.name,
      description: title.description,
      frame_key: title.frame_key,
      is_enabled: title.is_enabled,
    })
    if (res.code !== 200) throw new Error(res.message || '保存失败')
    await loadTitles()
    if (selectedAdventurerId.value) await selectAdventurer(selectedAdventurerId.value)
    message.success('称号已保存')
  } catch (error: any) {
    message.error(error?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

async function deleteTitle(title: AdminTitle) {
  if (!window.confirm(`确认删除称号「${title.name}」及其手工授予记录？`)) return
  saving.value = true
  try {
    const res = await store.adminDeleteTitle(title.id)
    if (res.code !== 200) throw new Error(res.message || '删除失败')
    await loadTitles()
    adventurerState.value = null
    message.success('称号已删除')
  } catch (error: any) {
    message.error(error?.message || '删除失败')
  } finally {
    saving.value = false
  }
}

async function searchAdventurers() {
  saving.value = true
  try {
    const res = await store.adminFetchAdventurers({ search: adventurerSearch.value, page: 1, per_page: 20 })
    if (res.code !== 200) throw new Error(res.message || '搜索失败')
    adventurerCandidates.value = res.data?.items ?? []
  } catch (error: any) {
    message.error(error?.message || '搜索失败')
  } finally {
    saving.value = false
  }
}

async function selectAdventurer(adventurerId: number) {
  selectedAdventurerId.value = adventurerId
  saving.value = true
  try {
    const res = await store.adminFetchAdventurerTitles(adventurerId)
    if (res.code !== 200) throw new Error(res.message || '读取失败')
    adventurerState.value = res.data
  } catch (error: any) {
    message.error(error?.message || '读取称号状态失败')
  } finally {
    saving.value = false
  }
}

function isAutomatic(title: AdminTitle) {
  return adventurerState.value?.automatic_title_codes.includes(title.code) ?? false
}

function isManual(title: AdminTitle) {
  return adventurerState.value?.manual_title_ids.includes(title.id) ?? false
}

function isExchanged(title: AdminTitle) {
  return adventurerState.value?.exchange_title_ids?.includes(title.id) ?? false
}

function hasTitle(title: AdminTitle) {
  return isAutomatic(title) || isManual(title) || isExchanged(title)
}

async function toggleGrant(title: AdminTitle) {
  const advId = selectedAdventurerId.value
  if (!advId || isAutomatic(title)) return
  saving.value = true
  try {
    const res = (isManual(title) || isExchanged(title))
      ? await store.adminRevokeAdventurerTitle(advId, title.id)
      : await store.adminGrantAdventurerTitle(advId, title.id)
    if (res.code !== 200) throw new Error(res.message || '操作失败')
    adventurerState.value = res.data
    await loadTitles()
  } catch (error: any) {
    message.error(error?.message || '操作失败')
  } finally {
    saving.value = false
  }
}

onMounted(loadTitles)
</script>

<style scoped lang="scss">
.title-admin-panel { display: grid; gap: 16px; min-width: 0; max-width: 100%; }
.panel-head { display: flex; justify-content: space-between; gap: 12px; align-items: flex-start; min-width: 0; }
.panel-head > div { min-width: 0; }
.panel-head h6, .workbench-heading h6 { margin: 0 0 4px; color: var(--color-text); }
.panel-head p, .workbench-heading p { margin: 0; color: var(--color-text-secondary); font-size: 12px; line-height: 1.55; overflow-wrap: anywhere; }
.section-kicker { display: block; margin-bottom: 2px; color: var(--color-primary); font-size: 9px; font-weight: 800; letter-spacing: .14em; }
.create-workbench, .grant-workbench { min-width: 0; padding: 14px; border: 1px solid var(--color-border); border-radius: 12px; background: var(--color-bg); }
.create-workbench { background: linear-gradient(135deg, color-mix(in srgb, var(--color-primary) 7%, var(--color-bg)), var(--color-bg) 70%); }
.workbench-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; min-width: 0; }
.workbench-heading > div { min-width: 0; }
.frame-preview { flex: 0 0 auto; padding: 5px 9px; border: 1px solid var(--color-border); border-radius: 999px; color: var(--color-text-secondary); font-size: 11px; white-space: nowrap; }
.frame-preview.preview-fate { border-color: color-mix(in srgb, #d9b96e 68%, #8b78c9); }
.frame-preview.preview-fate_companion { border-color: color-mix(in srgb, #54d3b1 68%, #f0c25c); }
.create-form { display: grid; grid-template-columns: minmax(130px, .8fr) minmax(150px, 1fr) minmax(220px, 1.6fr) minmax(130px, .8fr) auto; gap: 10px; align-items: end; min-width: 0; }
.field-group { display: grid; gap: 5px; min-width: 0; color: var(--color-text-secondary); font-size: 11px; }
.field-group em { color: var(--color-primary); font-style: normal; }
input, select { width: 100%; min-width: 0; max-width: 100%; box-sizing: border-box; padding: 8px 9px; border: 1px solid var(--color-border); border-radius: 8px; background: var(--color-bg); color: var(--color-text); }
.create-button { min-height: 37px; white-space: nowrap; }
.title-grid { display: grid; gap: 10px; min-width: 0; }
.title-card { min-width: 0; overflow: hidden; border: 1px solid var(--color-border); border-left: 4px solid var(--color-primary); border-radius: 12px; background: var(--color-bg); box-shadow: 0 4px 14px rgba(15, 23, 42, .04); }
.title-card.preview-fate { border-color: color-mix(in srgb, #d9b96e 68%, #8b78c9); }
.title-card.preview-fate_companion { border-color: color-mix(in srgb, #54d3b1 68%, #f0c25c); }
.title-card-head, .title-card-footer { display: flex; align-items: center; justify-content: space-between; gap: 12px; padding: 10px 12px; }
.title-card-head { border-bottom: 1px solid var(--color-border); background: var(--color-surface, var(--color-bg)); }
.title-main { display: flex; align-items: center; gap: 9px; min-width: 0; }
.title-main > div { display: grid; gap: 2px; min-width: 0; }
.title-main strong, .title-main code { min-width: 0; overflow-wrap: anywhere; }
.title-main code { color: var(--color-text-secondary); font-size: 10px; }
.frame-mark { display: grid; flex: 0 0 30px; width: 30px; height: 30px; place-items: center; border: 1px solid currentColor; border-radius: 9px; color: var(--color-primary); }
.title-badges { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 5px; }
.system-badge, .state-badge { padding: 2px 7px; border-radius: 999px; font-size: 10px; white-space: nowrap; }
.system-badge { background: rgba(97, 217, 255, .14); color: #2787a4; }
.state-badge { background: rgba(25, 135, 84, .12); color: var(--color-success, #198754); }
.state-badge.disabled { background: rgba(108, 117, 125, .12); color: var(--color-text-secondary); }
.title-fields { display: grid; grid-template-columns: minmax(140px, .8fr) minmax(240px, 1.7fr) minmax(130px, .7fr); gap: 10px; padding: 12px; min-width: 0; }
.title-card-footer { border-top: 1px solid var(--color-border); }
.title-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 14px; min-width: 0; }
.enabled { display: inline-flex; align-items: center; gap: 5px; color: var(--color-text); font-size: 12px; }
.enabled input { width: auto; }
.counts { color: var(--color-text-secondary); font-size: 11px; overflow-wrap: anywhere; }
.counts strong { color: var(--color-text); }
.row-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 6px; }
.grant-workbench { padding-top: 14px; }
.compact-heading { margin-bottom: 10px; }
.search-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 8px; }
.candidate-list { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 8px; }
.candidate-list button { display: inline-flex; gap: 5px; min-width: 0; padding: 6px 9px; border: 1px solid var(--color-border); border-radius: 8px; background: var(--color-bg); color: var(--color-text); cursor: pointer; overflow-wrap: anywhere; }
.candidate-list button span { color: var(--color-text-secondary); font-size: 11px; }
.candidate-list button.active { border-color: var(--color-primary); background: color-mix(in srgb, var(--color-primary) 8%, var(--color-bg)); }
.selected-name { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 8px 12px; margin: 12px 0 8px; padding: 9px 11px; border: 1px solid var(--color-border); border-radius: 9px; color: var(--color-text); }
.selected-name > span, .selected-name small { color: var(--color-text-secondary); font-size: 11px; }
.selected-name strong, .selected-name small { min-width: 0; overflow-wrap: anywhere; }
.grant-list { display: grid; grid-template-columns: repeat(auto-fit, minmax(min(190px, 100%), 1fr)); gap: 7px; }
.grant-list label { display: flex; gap: 7px; align-items: center; min-width: 0; padding: 9px; border: 1px solid var(--color-border); border-radius: 8px; }
.grant-list input { width: auto; flex: 0 0 auto; }
.grant-title-name { min-width: 0; overflow-wrap: anywhere; }
.grant-list small { margin-left: auto; color: var(--color-text-secondary); font-size: 10px; text-align: right; }
.grant-list .disabled { opacity: .55; }
.empty { color: var(--color-text-secondary); }
@media (max-width: 980px) {
  .create-form { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .description-field { grid-column: 1 / -1; }
  .create-button { width: 100%; }
  .title-fields { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .title-description-field { grid-column: 1 / -1; grid-row: 2; }
}
@media (max-width: 620px) {
  .panel-head, .workbench-heading, .title-card-head, .title-card-footer { align-items: stretch; flex-direction: column; }
  .panel-head > button { width: 100%; min-height: 40px; }
  .frame-preview { align-self: flex-start; }
  .create-workbench, .grant-workbench { padding: 11px; }
  .create-form, .title-fields, .search-row { grid-template-columns: 1fr; }
  .description-field, .title-description-field { grid-column: auto; grid-row: auto; }
  .create-button, .search-row button { width: 100%; min-height: 40px; }
  .title-badges { justify-content: flex-start; }
  .title-fields { padding: 10px; }
  .title-card-footer { padding: 10px; }
  .title-meta { display: grid; grid-template-columns: 1fr; }
  .row-actions { display: grid; grid-template-columns: 1fr; width: 100%; }
  .row-actions button { width: 100%; min-height: 40px; }
  .candidate-list { display: grid; grid-template-columns: 1fr; }
  .candidate-list button { width: 100%; min-height: 40px; align-items: center; }
  .selected-name { grid-template-columns: 1fr; }
  .grant-list { grid-template-columns: 1fr; }
}

.frame-preview.preview-hidden_relic, .title-card.preview-hidden_relic { border-color: #a98959; }

</style>
