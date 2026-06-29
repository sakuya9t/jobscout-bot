<template>
  <span v-if="nonMatching" class="pill bad">not a match</span>
  <div v-else class="score-block">
    <div class="score-number">{{ score }}<span class="score-suffix">/100</span></div>
    <div class="score-win">win {{ win }}%</div>
  </div>
</template>

<script setup lang="ts">
defineProps<{ score: number; win: number; nonMatching: boolean }>();
</script>

<style scoped>
.score-block { text-align: right; white-space: nowrap; }
.score-number { color: var(--text-score-primary); font-size: 28px; line-height: 32px; font-weight: 700; }
.score-suffix { color: var(--text-secondary); font-size: 16px; font-weight: 500; }
.score-win {
  display: inline-flex; align-items: center; margin-top: 4px; padding: 2px 8px;
  border-radius: 999px; background: var(--bg-badge-success); color: var(--text-score-secondary);
  font-size: 13px; line-height: 16px; font-weight: 600;
}
.pill {
  display: inline-flex; align-items: center; min-height: 22px; padding: 3px 10px;
  border-radius: 999px; font-size: 12px; line-height: 16px; font-weight: 600;
  white-space: nowrap; flex-shrink: 0;
}
.pill.bad { background: var(--bg-badge-error); color: var(--text-error); }
</style>
