<template>
  <div class="url-lookup">
    <form class="lookup-form" @submit.prevent="onSubmit">
      <label for="lookupUrl" class="muted">Already have a posting URL?</label>
      <input
        id="lookupUrl"
        v-model="url"
        type="url"
        inputmode="url"
        placeholder="Paste a job URL to find it in your list…"
        autocomplete="off"
      />
      <button type="submit" :disabled="busy || !url.trim()">{{ busy ? "Checking…" : "Find" }}</button>
    </form>

    <p v-if="error" class="lookup-result err">{{ error }}</p>

    <p v-else-if="result && !result.matched" class="lookup-result miss">
      Not in your job list. If you follow its company, run a scan to score it; otherwise open the posting directly.
    </p>

    <p v-else-if="result && result.matched" class="lookup-result hit">
      <span v-if="result.applied" class="pill ok">✓ Already applied</span>
      <span v-else class="pill neutral">In your job list</span>
      <a class="lookup-link" :href="`/positions/${result.position_id}`">{{ result.title }}</a>
      <span class="muted">· {{ result.company }}</span>
      <span v-if="!result.applied && result.match_score != null" class="muted">· score {{ result.match_score }}</span>
      <span v-if="result.removed" class="muted">· closed</span>
    </p>
  </div>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { ApiError } from "@/api/client";
import { useJobsStore } from "@/stores/jobs";
import type { PositionLookupOut } from "@/api/types";

const store = useJobsStore();

const url = ref("");
const result = ref<PositionLookupOut | null>(null);
const error = ref<string | null>(null);
const busy = ref(false);

async function onSubmit(): Promise<void> {
  const value = url.value.trim();
  if (!value || busy.value) return;
  busy.value = true;
  error.value = null;
  result.value = null;
  try {
    result.value = await store.lookupByUrl(value);
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Could not check that URL.";
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.url-lookup { display: grid; gap: 10px; }
.lookup-form { display: flex; flex-wrap: wrap; align-items: center; gap: 10px; }
.lookup-form > label { margin: 0; white-space: nowrap; font-size: 13px; }
.lookup-form > input {
  flex: 1 1 280px; min-width: 0; padding: 8px 10px; font-size: 13px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--surface);
  color: var(--text); min-height: 38px;
}
.lookup-form > button {
  padding: 8px 16px; font-size: 13px; min-height: 38px;
  border: 1.5px solid var(--interactive-outline-border); border-radius: 8px;
  background: transparent; color: var(--interactive-outline-text);
}
.lookup-form > button:disabled { opacity: 0.6; cursor: default; }
.lookup-result { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin: 0; font-size: 13px; }
.lookup-result.err { color: var(--text-error); }
.lookup-result.miss { color: var(--muted); }
.lookup-link { font-weight: 600; }
.pill {
  display: inline-flex; padding: 3px 9px; border-radius: 999px;
  font-size: 12px; line-height: 16px; font-weight: 600;
}
.pill.ok { background: var(--bg-badge-success); color: var(--text-success); }
.pill.neutral { background: var(--surface-soft); color: var(--text-secondary); }
</style>
