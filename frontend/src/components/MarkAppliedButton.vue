<template>
  <button
    type="button"
    class="apply-btn"
    :class="{ applied }"
    :aria-pressed="applied ? 'true' : 'false'"
    :disabled="busy"
    @click="onClick"
  >
    {{ applied ? "✓ Applied" : "Mark applied" }}
  </button>
</template>

<script setup lang="ts">
import { ref } from "vue";
const props = defineProps<{ applied: boolean }>();
const emit = defineEmits<{ (e: "toggle", next: boolean): void }>();
const busy = ref(false);

async function onClick(): Promise<void> {
  busy.value = true;
  try {
    emit("toggle", !props.applied);
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.apply-btn {
  background: transparent; color: var(--interactive-outline-text);
  border: 1.5px solid var(--interactive-outline-border);
  border-radius: 8px; padding: 8px 14px; font-size: 13px; min-height: 36px;
}
.apply-btn:hover:not(.applied):not(:disabled) {
  background: var(--interactive-outline-hover-bg); color: var(--interactive-outline-text);
}
.apply-btn.applied { background: var(--bg-badge-success); color: var(--text-success); border-color: transparent; }
</style>
