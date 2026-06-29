<template>
  <header class="topbar">
    <RouterLink class="brand" to="/app/jobs">JobScout</RouterLink>
    <div class="right">
      <RouterLink class="back" :to="backTo">← {{ backLabel }}</RouterLink>
      <ThemeToggle />
    </div>
  </header>
</template>

<script setup lang="ts">
import ThemeToggle from "@/components/ThemeToggle.vue";

defineProps<{ backTo: string; backLabel: string }>();
</script>

<style scoped>
.topbar {
  display: flex; align-items: center; justify-content: space-between; gap: 16px;
  padding: 14px 24px; border-bottom: 1px solid var(--line); background: var(--surface);
  position: sticky; top: 0; z-index: 5;
}
.brand { font-weight: 800; font-size: 16px; color: var(--text-heading); }
.right { display: flex; align-items: center; gap: 16px; }
.back { font-size: 14px; color: var(--text-link); }
</style>
