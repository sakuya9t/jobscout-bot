<template>
  <button v-if="inlineButton" type="button" class="copy" @click="copy">{{ btnLabel }}</button>
  <div v-else class="doc">
    <div class="doc-bar"><button type="button" class="copy" @click="copy">{{ btnLabel }}</button></div>
    <pre>{{ text }}</pre>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";

const props = withDefaults(
  defineProps<{ text: string; inlineButton?: boolean; label?: string }>(),
  { inlineButton: false, label: "Copy" },
);

const btnLabel = ref(props.label);

async function copy(): Promise<void> {
  try {
    await navigator.clipboard.writeText(props.text);
    btnLabel.value = "Copied!";
  } catch {
    btnLabel.value = "Copy failed";
  }
  setTimeout(() => { btnLabel.value = props.label; }, 1500);
}
</script>

<style scoped>
.doc { background: var(--bg-input, var(--surface-soft)); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px 14px; margin-top: 8px; }
.doc-bar { display: flex; justify-content: flex-end; gap: 8px; margin-bottom: 6px; }
.doc pre { margin: 0; white-space: pre-wrap; word-wrap: break-word; font: 14px/1.55 inherit; color: var(--ink); }
.copy { font-size: 12px; padding: 5px 10px; border-radius: 6px; background: transparent; color: var(--text-secondary); border: 1px solid var(--line); }
.copy:hover { border-color: var(--accent); color: var(--accent); }
</style>
