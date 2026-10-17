<template>
  <section class="exchange-panel">
    <header class="exchange-header">
      <div>
        <h5 class="section-title">🏛️ 秘境收藏馆</h5>
        <p>隐藏副本的藏品仍可交易、锻造或留存；点击物品名称可查看稀有度与完整描述。</p>
      </div>
      <select v-if="adventurers.length > 1" v-model.number="adventurerId" class="adv-select" :disabled="redeemingId !== null" @change="loadRecipes">
        <option v-for="adventurer in adventurers" :key="adventurer.id" :value="adventurer.id">
          {{ adventurer.name }}
        </option>
      </select>
    </header>

    <div v-if="loading" class="exchange-empty">加载藏品中…</div>
    <div v-else-if="!adventurerId" class="exchange-empty">你还没有可操作的冒险者</div>
    <div v-else class="recipe-grid">
      <article v-for="recipe in recipes" :key="recipe.id" class="recipe-card" :class="{ 'title-recipe': recipe.output_kind === 'title' }">
        <div class="recipe-output">
          <template v-if="recipe.output_kind === 'title'">
            <div class="exchange-title-artwork">
              <TitleFrameArtwork v-if="recipe.output_title.frame_key === 'hidden_relic'" kind="hidden_relic" />
              <div class="exchange-title-identity">
                <small>九境藏品 · 永久称号</small>
                <strong>{{ recipe.output_title.name }}</strong>
                <p>{{ recipe.output_title.description }}</p>
              </div>
            </div>
            <p class="title-recipe-note">兑换后解锁称号与十一印秘匣名片装饰，可在冒险者详情中装备。材料可交易；称号绑定当前冒险者，已拥有时不能重复兑换。</p>
          </template>
          <template v-else>
            <span>兑换</span>
            <div class="recipe-output-item">
              <button
                type="button"
                class="item-detail-button output-detail-button"
                :style="{ color: rarityColor(recipe.output_item_detail.rarity) }"
                :aria-label="`查看${recipe.output_item}详情`"
                @click="openItemPreview(recipe.output_item_detail)"
              >
                {{ recipe.output_item }} ×{{ recipe.output_quantity }}
              </button>
              <span
                class="rarity-badge"
                :style="{
                  color: rarityColor(recipe.output_item_detail.rarity),
                  borderColor: rarityColor(recipe.output_item_detail.rarity),
                }"
              >
                {{ rarityLabel(recipe.output_item_detail.rarity) }}
              </span>
            </div>
          </template>
        </div>
        <ul class="ingredient-list">
          <li v-for="ingredient in recipe.ingredients" :key="ingredient.item_name">
            <div class="ingredient-item">
              <button
                type="button"
                class="item-detail-button"
                :style="{ color: rarityColor(ingredient.rarity) }"
                :aria-label="`查看${ingredient.item_name}详情`"
                @click="openItemPreview(ingredient)"
              >
                {{ ingredient.item_name }}
              </button>
              <span
                class="rarity-badge ingredient-rarity"
                :style="{
                  color: rarityColor(ingredient.rarity),
                  borderColor: rarityColor(ingredient.rarity),
                }"
              >
                {{ rarityLabel(ingredient.rarity) }}
              </span>
            </div>
            <span class="ingredient-count" :class="{ missing: ingredient.owned < ingredient.required }">
              {{ ingredient.owned }} / {{ ingredient.required }}
            </span>
          </li>
        </ul>
        <button
          type="button"
          class="redeem-button"
          :disabled="redeemingId !== null || !recipe.can_redeem"
          @click="redeem(recipe)"
        >
          {{ redeemingId === recipe.id ? '兑换中…' : (recipe.output_kind === 'title' && recipe.already_owned ? '已拥有此称号' : (recipe.can_redeem ? '确认兑换' : '藏品不足')) }}
        </button>
      </article>
    </div>
    <RewardItemPreviewModal
      :item="itemPreview"
      kicker="兑换物品详情"
      @close="closeItemPreview"
    />
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import { useMessage } from 'naive-ui'
import api from '@/composables/useApi'
import { useAdventurerS1Store, type AdventurerTitle } from '../stores/adventurerS1'
import { type RewardPreviewItem } from '../itemTypes'
import { rarityColor, rarityLabel } from '../utils'
import RewardItemPreviewModal from './RewardItemPreviewModal.vue'
import TitleFrameArtwork from './TitleFrameArtwork.vue'

interface ItemDetail extends RewardPreviewItem {
  item_name: string
  rarity: string
  type: string
  description: string
}

interface Ingredient extends ItemDetail {
  required: number
  owned: number
}

interface RecipeBase {
  id: string
  output_item: string
  output_quantity: number
  ingredients: Ingredient[]
  can_redeem: boolean
}

type Recipe = RecipeBase & (
  { output_kind: 'item'; output_item_detail: ItemDetail }
  | { output_kind: 'title'; output_title: AdventurerTitle; already_owned: boolean }
)

const props = defineProps<{ building: { id: number } }>()
const emit = defineEmits<{ refreshed: [] }>()
const store = useAdventurerS1Store()
const message = useMessage()
const loading = ref(false)
const redeemingId = ref<string | null>(null)
const recipes = ref<Recipe[]>([])
const itemPreview = ref<RewardPreviewItem | null>(null)
const adventurerId = ref<number | null>(null)
const adventurers = computed(() => store.adventurers.filter(adventurer => (
  (adventurer.status === 'idle' || adventurer.status === 'resting')
  && !adventurer.building_instance_id
)))

function openItemPreview(item: ItemDetail) {
  itemPreview.value = {
    item_name: item.item_name,
    type: item.type,
    rarity: item.rarity,
    description: item.description,
  }
}

function closeItemPreview() {
  itemPreview.value = null
}

async function loadRecipes() {
  if (!adventurerId.value) return
  loading.value = true
  try {
    const { data } = await api.get(
      `/adventurer-s1/buildings/${props.building.id}/hidden-dungeon-exchange/recipes`,
      { params: { adventurer_id: adventurerId.value } },
    )
    if (data.code !== 200) throw new Error(data.message || '加载兑换配方失败')
    recipes.value = data.data?.recipes ?? []
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '加载兑换配方失败')
  } finally {
    loading.value = false
  }
}

async function redeem(recipe: Recipe) {
  const exchangingAdventurerId = adventurerId.value
  if (!exchangingAdventurerId || redeemingId.value) return
  redeemingId.value = recipe.id
  try {
    const { data } = await api.post(
      `/adventurer-s1/buildings/${props.building.id}/hidden-dungeon-exchange/redeem`,
      { adventurer_id: exchangingAdventurerId, recipe_id: recipe.id },
    )
    if (data.code !== 200) throw new Error(data.message || '兑换失败')
    recipes.value = data.data?.recipes ?? recipes.value
    message.success(data.data.output_kind === 'title'
      ? `已解锁「${data.data.output_title.name}」，可在冒险者详情中装备`
      : `已兑换「${data.data.output_item}」并存入仓库`)
    await store.fetchAdventurers(true)
    if (data.data.output_kind === 'title' && store.currentDetail?.id === exchangingAdventurerId) {
      await store.fetchDetail(exchangingAdventurerId, { preserveDetail: true, force: true })
    }
    emit('refreshed')
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '兑换失败')
    await loadRecipes()
  } finally {
    redeemingId.value = null
  }
}

onMounted(async () => {
  if (store.adventurers.length === 0) await store.fetchAdventurers()
  adventurerId.value = adventurers.value[0]?.id ?? null
  await loadRecipes()
})
</script>

<style scoped>
.exchange-panel { margin-top: 18px; padding: 18px; border: 1px solid #4b3b68; border-radius: 14px; background: #161221; }
.exchange-header { display: flex; justify-content: space-between; gap: 16px; align-items: start; }
.exchange-header p { margin: 4px 0 14px; color: #aaa0b9; }
.adv-select { min-width: 150px; padding: 8px 10px; border: 1px solid #59466f; border-radius: 8px; background: #221a30; color: #eee7f6; }
.recipe-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 12px; }
.recipe-card { padding: 14px; border: 1px solid #433456; border-radius: 12px; background: #21192d; }
.recipe-output { display: flex; justify-content: space-between; gap: 10px; color: #cbb8e3; }
.recipe-output-item { display: flex; flex-wrap: wrap; justify-content: flex-end; align-items: center; gap: 7px; }
.item-detail-button { appearance: none; padding: 0; border: 0; background: transparent; font: inherit; font-weight: 600; text-align: left; cursor: pointer; }
.item-detail-button:hover { text-decoration: underline; text-underline-offset: 3px; }
.item-detail-button:focus-visible { outline: 2px solid currentColor; outline-offset: 3px; border-radius: 3px; }
.output-detail-button { font-weight: 700; text-align: right; }
.rarity-badge { display: inline-flex; align-items: center; min-height: 20px; padding: 1px 6px; border: 1px solid; border-radius: 999px; font-size: .72rem; font-weight: 700; line-height: 1; }
.ingredient-list { list-style: none; padding: 0; margin: 12px 0; }
.ingredient-list li { display: flex; justify-content: space-between; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid #342942; }
.ingredient-item { display: flex; min-width: 0; flex-wrap: wrap; align-items: center; gap: 6px; }
.ingredient-rarity { font-size: .66rem; }
.ingredient-count { flex: 0 0 auto; }
.missing { color: #ff7f7f; }
.redeem-button { width: 100%; padding: 9px; border: 0; border-radius: 8px; background: #7652a7; color: white; cursor: pointer; }
.redeem-button:disabled { opacity: .45; cursor: not-allowed; }
.exchange-empty { padding: 24px; text-align: center; color: #aaa0b9; }
@media (max-width: 640px) { .exchange-header { flex-direction: column; } .adv-select { width: 100%; } }
.title-recipe { display: grid; grid-column: 1 / -1; grid-template-columns: minmax(220px, .8fr) minmax(0, 1.2fr); gap: 18px; border-color: #a98959; background: radial-gradient(ellipse at 10% 0, rgba(185, 154, 232, .08), transparent 48%), var(--color-surface, #21192d); }
.title-recipe .recipe-output { display: block; grid-row: span 2; min-width: 0; }
.exchange-title-artwork { position: relative; min-height: 214px; overflow: hidden; border-radius: 10px; }
.exchange-title-identity { position: relative; z-index: 1; display: grid; gap: 12px; padding: 72px 24px 32px; }
.exchange-title-identity small { color: #aa91c7; font-size: 11px; letter-spacing: .1em; }
.exchange-title-identity strong { color: #e0bb7d; font-size: 24px; letter-spacing: .12em; }
.exchange-title-identity p { margin: 0; color: #c2b4ce; font-size: 12px; line-height: 1.8; }
.title-recipe-note { margin: 14px 8px 0; color: #a79daf; font-size: 12px; line-height: 1.8; }
.title-recipe .ingredient-list { margin: 0; }
.title-recipe .redeem-button { grid-column: 2; background: #78562a; }
:global([data-theme="light"] .title-recipe) { background: #faf6ef; }
:global([data-theme="light"] .exchange-title-identity strong) { color: #825820; }
:global([data-theme="light"] .exchange-title-identity p),
:global([data-theme="light"] .title-recipe-note) { color: #695c72; }
:global([data-theme="light"] .title-recipe .ingredient-count) { color: #514054; }
:global([data-theme="light"] .title-recipe .ingredient-count.missing) { color: #b12e38; }
:global([data-theme="light"] .title-recipe .ingredient-list li) { border-color: #dfd2bd; }
@media (max-width: 640px) {
  .title-recipe { grid-template-columns: minmax(0, 1fr); }
  .title-recipe .recipe-output { grid-row: auto; }
  .title-recipe .redeem-button { grid-column: auto; }
}
@media (max-width: 640px) {
  .recipe-grid { grid-template-columns: minmax(0, 1fr); }
  .recipe-card { min-width: 0; }
  .exchange-header { gap: 10px; }
}
</style>
