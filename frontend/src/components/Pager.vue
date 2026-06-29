<template>
  <div class="pager">
    <button class="ghost page-num" :disabled="current <= 1" @click="emit('go', current - 1)">‹ Prev</button>
    <span>
      <template v-for="(p, i) in buttons" :key="i">
        <span v-if="p === '…'" class="page-ellipsis">…</span>
        <button
          v-else
          class="ghost page-num"
          :class="{ active: p === current }"
          :disabled="p === current"
          @click="emit('go', p as number)"
        >
          {{ p }}
        </button>
      </template>
    </span>
    <button class="ghost page-num" :disabled="current >= totalPages" @click="emit('go', current + 1)">Next ›</button>
    <select :value="pageSize" aria-label="Items per page" @change="onSize">
      <option :value="10">10 / page</option>
      <option :value="20">20 / page</option>
      <option :value="50">50 / page</option>
    </select>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";

const props = defineProps<{ total: number; pageOffset: number; pageSize: number }>();
const emit = defineEmits<{ (e: "go", page: number): void; (e: "size", size: number): void }>();

const totalPages = computed(() => Math.max(1, Math.ceil(props.total / props.pageSize)));
const current = computed(() => Math.min(Math.floor(props.pageOffset / props.pageSize) + 1, totalPages.value));

// Windowed page list with first/last anchors and ellipses: 1 … 4 5 6 … 20.
// (Ported from pageButtons() in app/templates/dashboard.html.)
const buttons = computed<(number | "…")[]>(() => {
  const tp = totalPages.value;
  const cur = current.value;
  if (tp <= 7) return Array.from({ length: tp }, (_, i) => i + 1);
  let start = Math.max(2, cur - 1);
  let end = Math.min(tp - 1, cur + 1);
  if (cur <= 3) { start = 2; end = 4; }
  if (cur >= tp - 2) { start = tp - 3; end = tp - 1; }
  const out: (number | "…")[] = [1];
  if (start > 2) out.push("…");
  for (let i = start; i <= end; i++) out.push(i);
  if (end < tp - 1) out.push("…");
  out.push(tp);
  return out;
});

function onSize(e: Event): void {
  emit("size", parseInt((e.target as HTMLSelectElement).value, 10) || 10);
}
</script>

<style scoped>
.pager { display: flex; flex-wrap: wrap; gap: 6px; align-items: center; justify-content: flex-end; margin-top: 16px; }
.page-num { min-width: 36px; padding: 8px 11px; font-size: 13px; border-radius: 8px; }
.page-num.active { background: var(--accent); color: var(--accent-ink); border-color: var(--accent); cursor: default; }
.page-ellipsis { padding: 0 4px; color: var(--muted); }
.pager select { width: auto; padding: 8px 10px; font-size: 13px; margin-left: 8px; }
</style>
