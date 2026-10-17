<script setup lang="ts">
defineProps<{
  pagination: {
    current_page: number
    pages: number
    total: number
  }
}>()

defineEmits<{ go: [page: number] }>()
</script>

<template>
  <nav v-if="pagination && pagination.pages > 1" class="pagination">
    <button
      type="button"
      :disabled="pagination.current_page <= 1"
      @click="$emit('go', pagination.current_page - 1)"
    >
      上一页
    </button>
    <span>{{ pagination.current_page }} / {{ pagination.pages }}</span>
    <button
      type="button"
      :disabled="pagination.current_page >= pagination.pages"
      @click="$emit('go', pagination.current_page + 1)"
    >
      下一页
    </button>
  </nav>
</template>
