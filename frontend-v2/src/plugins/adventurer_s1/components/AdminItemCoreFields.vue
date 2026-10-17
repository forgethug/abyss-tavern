<template>
  <div class="admin-item-core-fields">
    <label v-if="showName" class="form-label">
      物品名称
      <input
        v-model="form.name"
        type="text"
        class="advs1-input"
        placeholder="输入物品名称"
        maxlength="100"
        :disabled="nameDisabled"
      />
    </label>
    <label v-if="showDescription" class="form-label">
      描述
      <textarea
        v-model="form.description"
        class="advs1-input"
        rows="3"
        placeholder="物品描述"
        :disabled="descriptionDisabled"
      />
    </label>
    <div v-if="showTypeRow" class="form-row">
      <label v-if="showRarity" class="form-label">
        稀有度
        <select v-model="form.rarity" class="advs1-input" :disabled="rarityDisabled">
          <option value="common">普通</option>
          <option value="rare">稀有</option>
          <option value="epic">史诗</option>
          <option value="legendary">传说</option>
        </select>
      </label>
      <label v-if="showType" class="form-label">
        类型
        <select v-model="form.type" class="advs1-input" :disabled="typeDisabled">
          <option v-for="t in ITEM_TYPE_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
        </select>
      </label>
    </div>
    <AdminItemUsesLeftField
      v-if="showUsesLeft"
      v-model="form.uses_left"
      :label="usesLeftLabel"
      :hint="usesLeftHint"
      :disabled="usesLeftDisabled"
    />
  </div>
</template>

<script setup lang="ts">
import AdminItemUsesLeftField from './AdminItemUsesLeftField.vue'
import { ITEM_TYPE_OPTIONS } from '../utils'

export interface AdminItemCoreForm {
  name: string
  description: string
  type: string
  rarity: string
  uses_left: number | null
}

defineProps<{
  form: AdminItemCoreForm
  showName?: boolean
  showDescription?: boolean
  showType?: boolean
  showRarity?: boolean
  showTypeRow?: boolean
  showUsesLeft?: boolean
  nameDisabled?: boolean
  descriptionDisabled?: boolean
  typeDisabled?: boolean
  rarityDisabled?: boolean
  usesLeftDisabled?: boolean
  usesLeftLabel?: string
  usesLeftHint?: string
}>()
</script>

<style scoped>
.form-label {
  display: flex;
  flex-direction: column;
  gap: 0.3rem;
  margin-bottom: 0.75rem;
  font-size: 0.9rem;
  font-weight: 500;
}
.form-row {
  display: flex;
  gap: 0.75rem;
}
.form-row .form-label {
  flex: 1;
}
</style>
