<script setup lang="ts">
import { computed, onMounted, onBeforeUnmount, ref } from 'vue'
import axios from 'axios'
import api from '@/composables/useApi'
import { getActiveTavernSlug } from '@/runtime/activeTavern'

interface Credential {
  id: string; name: string; enabled: boolean; api_key: string; clear_api_key: boolean
  api_key_configured?: boolean
}
interface Profile {
  id: string; name: string; provider: string; api_base: string; model: string
  api_key: string; clear_api_key: boolean; api_key_configured?: boolean
  stream_enabled: boolean; json_object_mode: string; backup_profile_id: string
  backup_api_base: string; backup_model: string; backup_api_key: string
  clear_backup_api_key: boolean; backup_api_key_configured?: boolean
  backup_api_keys: Credential[]
}
interface Catalog {
  profiles: Profile[]; default_profile_id: string; assignments: Record<string, string>
  reasoning_efforts: Record<string, string>; revision: string; features: {key: string; label: string}[]
  managed_by: string; blocked_credential_count: number; balance_revision: string
}
const tavern = getActiveTavernSlug()
const catalog = ref<Catalog | null>(null)
const baseline = ref(''), busy = ref(false), error = ref(''), notice = ref('')
const search = ref(''), bulkTarget = ref('')
let mounted = true
const providers = [
  ['deepseek', 'DeepSeek'], ['openai_compatible', 'OpenAI 兼容'],
  ['anthropic_compatible', 'Anthropic 兼容'], ['mimo', 'MiMo'],
  ['openrouter_deepseek_v4_pro_official', 'OpenRouter · DeepSeek 官方'],
  ['openrouter_deepseek_v4_pro_baidu', 'OpenRouter · DeepSeek 百度'],
]
const efforts = [['default', '跟随模型默认'], ['none', '关闭'], ['low', '低'], ['medium', '中'], ['high', '高'], ['max', '最高']]
const dirty = computed(() => catalog.value !== null && JSON.stringify(catalog.value) !== baseline.value)
const features = computed(() => catalog.value?.features.filter(f => `${f.label} ${f.key}`.toLowerCase().includes(search.value.toLowerCase())) ?? [])
function accept(data: Catalog) {
  catalog.value = {...data, profiles: data.profiles.map(p => ({...p,
    api_key: '', clear_api_key: false, backup_api_key: '', clear_backup_api_key: false,
    backup_api_keys: (p.backup_api_keys ?? []).map(k => ({...k, api_key: '', clear_api_key: false})),
  }))}
  baseline.value = JSON.stringify(catalog.value)
  bulkTarget.value = data.default_profile_id
}
function failure(e: unknown) {
  if (axios.isCancel(e) || !mounted) return
  error.value = axios.isAxiosError(e) ? e.response?.data?.message || '请求失败，请检查连接后重试' : '配置操作失败'
}
async function load() {
  if (dirty.value && !window.confirm('刷新会放弃当前未保存的编辑，是否继续？')) return
  busy.value = true; error.value = ''; notice.value = ''
  try { const {data} = await api.get('/adventurer-s1/admin/ai/config'); if (mounted) accept(data.data) }
  catch (e) { failure(e) } finally { if (mounted) busy.value = false }
}
async function save() {
  if (!catalog.value || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  const c = catalog.value
  try {
    const {data} = await api.put('/adventurer-s1/admin/ai/config', {
      revision: c.revision, profiles: c.profiles, default_profile_id: c.default_profile_id,
      assignments: c.assignments, reasoning_efforts: c.reasoning_efforts,
    })
    if (mounted) { accept(data.data); notice.value = '已保存到当前酒馆。后续 AI 请求使用新配置，无需重启；已经开始的请求继续使用原配置。' }
  } catch (e) { failure(e) } finally { if (mounted) busy.value = false }
}
function addProfile() {
  const id = `model-${crypto.randomUUID().slice(0, 8)}`
  catalog.value?.profiles.push({id, name: '新模型预设', provider: 'openai_compatible',
    api_base: '', model: '', api_key: '', clear_api_key: false, stream_enabled: false,
    json_object_mode: 'auto', backup_profile_id: '', backup_api_base: '', backup_model: '',
    backup_api_key: '', clear_backup_api_key: false, backup_api_keys: []})
}
function removeProfile(p: Profile) {
  const c = catalog.value!
  if (c.default_profile_id === p.id || Object.values(c.assignments).includes(p.id) || c.profiles.some(x => x.backup_profile_id === p.id)) {
    error.value = '这个预设仍被默认模型、用途或备用线路引用，请先调整分配再移除。'; return
  }
  c.profiles = c.profiles.filter(x => x.id !== p.id)
}
function addCredential(p: Profile) {
  p.backup_api_keys.push({id: `key-${crypto.randomUUID().slice(0, 8)}`, name: '备用 Key', enabled: true, api_key: '', clear_api_key: false})
}
function assignAll() {
  if (catalog.value) for (const f of features.value) catalog.value.assignments[f.key] = bulkTarget.value
}
async function resume() {
  if (!catalog.value || dirty.value || busy.value) return
  busy.value = true; error.value = ''; notice.value = ''
  try {
    const {data} = await api.post('/adventurer-s1/admin/ai/balance/resume', {revision: catalog.value.revision, balance_revision: catalog.value.balance_revision})
    if (mounted) { accept(data.data); notice.value = '当前酒馆的余额不足暂停已解除，下一次业务请求会重新尝试；本操作不调用模型。' }
  } catch (e) { failure(e) } finally { if (mounted) busy.value = false }
}
onMounted(load)
onBeforeUnmount(() => { mounted = false; catalog.value = null })
</script>

<template>
  <section class="model-config" aria-label="AI 模型配置">
    <header>
      <div><h6>S1 AI 模型配置</h6><p>当前酒馆：<strong>{{ tavern }}</strong>。模型、Key 和用途分配只保存在当前酒馆。</p><p>API Key 只写不可读，留空保持原值。保存后直接生效，无需命令行或重启。</p></div>
      <button type="button" :disabled="busy" @click="load">刷新模型目录</button>
    </header>
    <p class="recommendation">中文剧情正文唯一推荐 <strong>deepseek-v4-pro-0423</strong>。根据本项目测试经验，后续新版本的中文剧情效果较差。</p>
    <p v-if="error" role="alert" class="error">{{ error }}</p>
    <p v-if="notice" role="status" class="success">{{ notice }}</p>
    <p v-if="!catalog">{{ busy ? '加载模型目录…' : '模型目录未加载，请重试。' }}</p>
    <template v-else>
      <p v-if="catalog.managed_by === 'deployment_defaults'" class="hint">尚未保存独立配置，当前显示安装时的初始设置。首次保存后，此酒馆使用自己的配置。</p>
      <fieldset :disabled="busy">
        <div class="toolbar"><h6>模型预设</h6><button type="button" @click="addProfile">＋ 添加模型预设</button></div>
        <article v-for="(p, i) in catalog.profiles" :key="p.id" class="profile" :aria-label="`模型预设 ${i + 1}`">
          <div class="toolbar"><strong>{{ p.name || '未命名预设' }}</strong><button type="button" @click="removeProfile(p)">移除预设</button></div>
          <div class="fields">
            <label>预设名称<input v-model="p.name" maxlength="80" /></label>
            <label>厂商协议<select v-model="p.provider"><option v-for="[v, label] in providers" :key="v" :value="v">{{ label }}</option></select></label>
            <label>API 接口地址<input v-model="p.api_base" placeholder="https://api.example.com/v1" spellcheck="false" /></label>
            <label>模型名称<input v-model="p.model" placeholder="填写服务商提供的模型 ID" spellcheck="false" /></label>
            <label>API Key<input v-model="p.api_key" type="password" autocomplete="new-password" :placeholder="p.api_key_configured ? '已配置，留空保持原值' : '尚未配置，请输入 Key'" /></label>
            <label>结构化 JSON<select v-model="p.json_object_mode"><option value="auto">跟随厂商能力</option><option value="enabled">启用</option><option value="disabled">禁用</option></select></label>
          </div>
          <div class="toggles"><label><input v-model="p.clear_api_key" type="checkbox" />清空已保存的主 Key</label><label><input v-model="p.stream_enabled" type="checkbox" />启用流式输出</label></div>
          <details>
            <summary>备用线路与备用 Key</summary>
            <p class="hint">优先引用完整备用预设；同线路备用 Key 用于同一接口和模型的凭证切换。</p>
            <label>完整备用预设<select v-model="p.backup_profile_id"><option value="">不使用完整备用预设</option><option v-for="other in catalog.profiles.filter(x => x.id !== p.id)" :key="other.id" :value="other.id">{{ other.name }} · {{ other.model }}</option></select></label>
            <div class="fields"><label>兼容备用接口地址<input v-model="p.backup_api_base" placeholder="留空沿用主接口" /></label><label>兼容备用模型<input v-model="p.backup_model" placeholder="留空沿用主模型" /></label><label>兼容备用 API Key<input v-model="p.backup_api_key" type="password" autocomplete="new-password" :placeholder="p.backup_api_key_configured ? '已配置，留空保持' : '可选'" /></label></div>
            <label class="check"><input v-model="p.clear_backup_api_key" type="checkbox" />清空兼容备用 Key</label>
            <div v-for="k in p.backup_api_keys" :key="k.id" class="credential">
              <label>备用 Key 名称<input v-model="k.name" /></label><label>备用 Key<input v-model="k.api_key" type="password" autocomplete="new-password" :placeholder="k.api_key_configured ? '已配置，留空保持' : '请输入备用 Key'" /></label>
              <label class="check"><input v-model="k.enabled" type="checkbox" />启用</label><label class="check"><input v-model="k.clear_api_key" type="checkbox" />清空 Key</label><button type="button" @click="p.backup_api_keys = p.backup_api_keys.filter(x => x.id !== k.id)">移除此 Key</button>
            </div><button type="button" @click="addCredential(p)">＋ 添加同线路备用 Key</button>
          </details>
        </article>
        <label class="default-profile">默认模型预设<select v-model="catalog.default_profile_id"><option v-for="p in catalog.profiles" :key="p.id" :value="p.id">{{ p.name }} · {{ p.model }}</option></select></label>
        <h6>AI 用途分配</h6><p class="hint">可分别为剧情、大纲、审核、建筑和申诉等用途选择模型。思考强度是否受支持取决于厂商协议。</p>
        <div class="bulk"><label>搜索用途<input v-model="search" placeholder="剧情、建筑、申诉…" /></label><label>批量目标预设<select v-model="bulkTarget"><option v-for="p in catalog.profiles" :key="p.id" :value="p.id">{{ p.name }}</option></select></label><button type="button" :disabled="!bulkTarget" @click="assignAll">将当前 {{ features.length }} 项分配到此预设</button></div>
        <div class="assignments"><div v-for="f in features" :key="f.key" class="assignment"><span>{{ f.label }}</span><select v-model="catalog.assignments[f.key]" :aria-label="`${f.label}模型`"><option v-for="p in catalog.profiles" :key="p.id" :value="p.id">{{ p.name }} · {{ p.model }}</option></select><select v-model="catalog.reasoning_efforts[f.key]" :aria-label="`${f.label}思考强度`"><option v-for="[v, label] in efforts" :key="v" :value="v">{{ label }}</option></select></div></div>
      </fieldset>
      <footer><span>{{ dirty ? '有未保存的更改' : '已与服务器同步' }}</span><button class="primary" type="button" :disabled="busy || !dirty" @click="save">{{ busy ? '处理中…' : '保存模型与用途配置' }}</button></footer>
      <div class="recovery"><p>余额不足暂停：{{ catalog.blocked_credential_count }} 个凭证。充值完成后，可在此恢复当前酒馆的调用。</p><button type="button" :disabled="busy || dirty || !catalog.blocked_credential_count" @click="resume">充值后恢复调用</button></div>
    </template>
  </section>
</template>

<style scoped>
.model-config { border: 1px solid var(--color-border); border-radius: 10px; padding: 16px; background: var(--color-bg); color: var(--color-text); min-width: 0; }
header, .toolbar, footer { display: flex; gap: 12px; align-items: center; justify-content: space-between; flex-wrap: wrap; }
h6 { font-size: 14px; margin: 0 0 8px; } p { font-size: 12px; line-height: 1.7; margin: 6px 0; }
.hint, header p, footer span { color: var(--color-text-secondary); }
.recommendation { margin: 12px 0; padding: 10px; border-left: 3px solid var(--color-primary, #c9a55c); background: var(--color-surface); }
fieldset { border: 0; margin: 0; padding: 0; min-width: 0; }
.profile { margin: 12px 0; border: 1px solid var(--color-border); border-radius: 8px; padding: 14px; background: var(--color-surface); }
.fields { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 12px 0; }
label { display: flex; flex-direction: column; gap: 6px; font-size: 12px; min-width: 0; }
input, select { box-sizing: border-box; width: 100%; min-width: 0; min-height: 36px; padding: 7px 9px; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 6px; }
input[type=checkbox] { width: auto; min-height: 0; }
button { color: var(--color-text); border: 1px solid var(--color-border); background: var(--color-surface); border-radius: 6px; padding: 8px 12px; cursor: pointer; font-size: 12px; }
button:disabled, fieldset:disabled { opacity: .55; } button:disabled { cursor: default; }
.primary { background: var(--color-primary, #c9a55c); color: #141414; font-weight: 700; }
.toggles { display: flex; gap: 18px; flex-wrap: wrap; margin: 10px 0; } .toggles label, .check { flex-direction: row; align-items: center; }
details { margin-top: 14px; border-top: 1px solid var(--color-border); padding-top: 12px; } summary { cursor: pointer; font-size: 13px; margin-bottom: 10px; }
.credential { display: flex; gap: 10px; align-items: end; flex-wrap: wrap; margin: 12px 0; padding: 10px; border: 1px solid var(--color-border); border-radius: 6px; }
.default-profile { max-width: 440px; margin: 18px 0; }
.bulk { display: flex; gap: 12px; align-items: end; flex-wrap: wrap; margin: 12px 0; }
.assignment { display: grid; grid-template-columns: minmax(150px, 1fr) minmax(180px, 1.4fr) minmax(120px, .7fr); align-items: center; gap: 12px; padding: 8px 0; border-bottom: 1px solid var(--color-border); font-size: 12px; }
footer { position: sticky; bottom: 0; background: var(--color-bg); padding: 12px 0; margin-top: 12px; border-top: 1px solid var(--color-border); font-size: 12px; }
.recovery { margin-top: 16px; border-top: 1px solid var(--color-border); padding-top: 10px; }
.error { color: #ff9292; } .success { color: #8ddbb1; }
@media(max-width: 680px) { .fields, .assignment { grid-template-columns: 1fr; } .bulk > *, .credential > label { width: 100%; } }
</style>
