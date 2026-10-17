<template>
  <section class="limited-shop-admin">
    <h3>🥮 限时商店管理 · 当前酒馆</h3>
    <p>所有开放时间均为香港时间（UTC+8）。保存只影响当前酒馆。</p>
    <div class="toolbar">
      <button :disabled="busy" @click="newShop">新建商店</button>
      <button :disabled="busy" @click="loadMooncakes">载入中秋月饼预设</button>
      <button :disabled="busy" @click="loadList">刷新商店列表</button>
    </div>
    <div class="toolbar">
      <button v-for="shop in shops" :key="shop.id" :disabled="busy" @click="editShop(shop.id)">{{ shop.name }}</button>
      <button :disabled="busy || page <= 1" @click="changePage(-1)">上一页</button>
      <span>第 {{ page }} 页 · 共 {{ total }} 家</span>
      <button :disabled="busy || !hasNext" @click="changePage(1)">下一页</button>
    </div>
    <form v-if="editing" @submit.prevent="save">
      <fieldset :disabled="busy">
        <div class="form-grid">
          <label>名称<input v-model="form.name" required maxlength="50" /></label>
          <label>唯一标识<input v-model="form.code" required maxlength="50" pattern="[a-z0-9_-]+" :disabled="form.id != null" /></label>
          <label>开放时间（香港）<input v-model="startsLocal" type="datetime-local" step="1" required /></label>
          <label>结束时间（香港）<input v-model="endsLocal" type="datetime-local" step="1" required /></label>
        </div>
        <label>介绍<textarea v-model="form.description" rows="2" /></label>
        <label><input v-model="form.enabled" type="checkbox" /> 启用售卖（仍受开放时间限制）</label>
        <div class="toolbar">
          <input v-model="search" placeholder="输入图鉴物品名称" @keydown.enter.prevent="searchCatalog" />
          <button type="button" :disabled="!search.trim()" @click="searchCatalog">搜索图鉴</button>
        </div>
        <div class="search-results">
          <button v-for="item in results" :key="item.id" type="button" @click="addItem(item)">
            {{ item.name }} · {{ item.rarity }}{{ item.use_condition?.immutable ? ' · 不可变' : '' }} ＋
          </button>
        </div>
        <p>补货数量会加到最新库存，不会覆盖玩家刚买走的数量。同一图鉴商品的库存由商店共享，本店售价独立。移除商品只停止本店出售。</p>
        <div v-for="(item, index) in form.items" :key="item.item_id ?? item.preset_name" class="goods-row">
          <div><strong>{{ item.name }}</strong><span v-if="item.immutable"> · 不可变</span>
            <p>{{ item.description }}</p>
          </div>
          <label>售价（金币）<input v-model.number="item.price" type="number" required min="1" max="2147483647" step="1" /></label>
          <span>当前库存 {{ item.stock ?? '保存后读取' }}</span>
          <label>本次补货<input v-model.number="item.stock_delta" type="number" required min="0" max="2147483647" step="1" /></label>
          <button type="button" @click="form.items.splice(index, 1)">移除</button>
        </div>
        <p v-if="form.items.some(item => (item.stock ?? 0) + item.stock_delta === 0)">部分商品暂无库存或尚未读取库存，请核对补货数量后再开售。</p>
        <button type="submit" :disabled="!form.items.length">{{ busy ? '保存中…' : '保存当前区服商店' }}</button>
      </fieldset>
    </form>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import api, { getS1AdminRealmTarget } from '@/composables/useApi'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { hongKongInput, hongKongInstant } from '../utils/limitedShopTime'
import { useAdventurerS1Store } from '../stores/adventurerS1'

interface Goods {
  item_id?: number; preset_name?: string; name: string; description: string
  price: number; stock: number | null; stock_delta: number; immutable: boolean; rarity?: string
}
interface CatalogItem {
  id: number; name: string; description: string; rarity: string; use_condition?: { immutable?: boolean }
}
interface ShopForm {
  id?: number; code: string; name: string; description: string; enabled: boolean
  revision?: string; starts_at?: string; ends_at?: string; items: Goods[]
}
const realm = getS1AdminRealmTarget()
const requestConfig = { s1AdminRealmTarget: realm }
const message = useGlobalMessage()
const store = useAdventurerS1Store()
const busy = ref(false)
const editing = ref(false)
const shops = ref<Array<{ id: number; name: string }>>([])
const page = ref(1), total = ref(0), hasNext = ref(false)
const startsLocal = ref(''), endsLocal = ref(''), search = ref('')
const results = ref<CatalogItem[]>([])
const defaults = (): ShopForm => ({ name: '', code: '', description: '', enabled: false, items: [] })
const form = reactive<ShopForm>(defaults())

function installForm(data: ShopForm) {
  Object.assign(form, defaults(), { id: undefined, revision: undefined }, data)
  startsLocal.value = hongKongInput(data.starts_at)
  endsLocal.value = hongKongInput(data.ends_at)
  results.value = []
  editing.value = true
}
function newShop() { installForm(defaults()) }
async function perform(action: () => Promise<void>) {
  if (busy.value) return
  busy.value = true
  try {
    if (getS1AdminRealmTarget() !== realm) throw new Error('区服已切换，请刷新管理界面')
    await action()
  } catch (e: any) { message.error(e?.response?.data?.message || e?.message || '操作失败') }
  finally { busy.value = false }
}
function requireData(response: any) {
  if (![200, 201].includes(response.data.code)) throw new Error(response.data.message || '操作失败')
  return response.data.data
}
async function refreshList() {
  const data = requireData(await api.get('/adventurer-s1/admin/limited-shops', { ...requestConfig, params: { page: page.value } }))
  shops.value = data.items; total.value = data.total; hasNext.value = data.has_next
}
function loadList() { return perform(refreshList) }
function changePage(delta: number) { page.value += delta; void loadList() }
function editShop(id: number) {
  return perform(async () => installForm(requireData(await api.get(`/adventurer-s1/admin/limited-shops/${id}`, requestConfig))))
}
function loadMooncakes() {
  return perform(async () => installForm({ ...requireData(await api.get('/adventurer-s1/admin/limited-shop-mooncakes', requestConfig)), enabled: false }))
}
function searchCatalog() {
  return perform(async () => {
    results.value = requireData(await api.get('/adventurer-s1/admin/items/catalog', { ...requestConfig, params: { search: search.value.trim() } }))
  })
}
function addItem(item: CatalogItem) {
  if (form.items.some(row => row.item_id === item.id || row.name === item.name)) return
  form.items.push({ item_id: item.id, name: item.name, description: item.description || '',
    price: 1, stock: null, stock_delta: 0, immutable: item.use_condition?.immutable === true, rarity: item.rarity })
}
function save() {
  return perform(async () => {
    const starts = hongKongInstant(startsLocal.value), ends = hongKongInstant(endsLocal.value)
    if (Date.parse(starts) >= Date.parse(ends)) throw new Error('结束时间必须晚于开放时间')
    const payload = { ...form, starts_at: starts, ends_at: ends }
    const response = form.id
      ? await api.put(`/adventurer-s1/admin/limited-shops/${form.id}`, payload, requestConfig)
      : await api.post('/adventurer-s1/admin/limited-shops', payload, requestConfig)
    installForm(requireData(response))
    message.success('当前区服的限时商店已保存')
    await refreshList()
    await store.fetchBuildings(true)
  })
}
onMounted(loadList)
</script>

<style scoped>
.limited-shop-admin { padding: 16px; margin-bottom: 18px; border: 1px solid var(--color-border); border-radius: 10px; }
.toolbar, .search-results { display: flex; gap: 8px; flex-wrap: wrap; margin: 12px 0; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
fieldset { border: 0; padding: 0; min-width: 0; }
label { display: block; margin: 8px 0; }
input:not([type=checkbox]), textarea { display: block; max-width: 100%; box-sizing: border-box; padding: 8px; color: var(--color-text); background: var(--color-bg); border: 1px solid var(--color-border); border-radius: 5px; }
textarea { width: 100%; }
button { cursor: pointer; padding: 7px 12px; color: var(--color-text); background: var(--color-bg-secondary); border: 1px solid var(--color-border); border-radius: 5px; }
button:disabled { cursor: default; opacity: .5; }
.goods-row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; padding: 12px 0; border-top: 1px solid var(--color-border); }
.goods-row > div { flex: 1 1 280px; min-width: 0; overflow-wrap: anywhere; }
.goods-row input { width: 130px; }
p { font-size: .88em; color: var(--color-text-secondary); line-height: 1.6; }
</style>
