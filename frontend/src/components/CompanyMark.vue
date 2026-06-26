<template>
  <div class="company-mark" aria-hidden="true">{{ initial }}</div>
</template>

<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{ name: string }>();
const initial = computed(() => {
  const t = (props.name || "").trim();
  return t ? t.charAt(0).toUpperCase() : "?";
});
</script>

<style scoped>
.company-mark {
  width: 44px; height: 44px; flex: 0 0 44px; border-radius: 10px;
  display: grid; place-items: center; background: var(--bg-tag); color: var(--brand-primary);
  font-size: 18px; line-height: 1; font-weight: 800; border: 1px solid var(--border-default);
}
</style>
