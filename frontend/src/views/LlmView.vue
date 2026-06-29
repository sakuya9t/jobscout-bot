<template>
  <section class="panel">
    <div class="surface">
      <h2>LLM provider</h2>
      <p class="muted">The model provider, API key, and the two models used to filter and
        score postings. Leave the API key blank to keep the one already saved.</p>

      <form class="form" @submit.prevent="onSave">
        <div class="row">
          <div class="field">
            <label for="llmProvider">Provider</label>
            <select id="llmProvider" v-model="provider">
              <option v-for="p in providers" :key="p.key" :value="p.key">{{ p.label }}</option>
            </select>
          </div>
          <div class="field">
            <label for="llmBaseUrl">Base URL</label>
            <input id="llmBaseUrl" :value="baseUrl" type="text" disabled />
          </div>
        </div>
        <div class="row">
          <div class="field">
            <label for="llmMain">Main model</label>
            <input id="llmMain" v-model="mainModel" type="text" />
          </div>
          <div class="field">
            <label for="llmLight">Light model</label>
            <input id="llmLight" v-model="lightModel" type="text" />
          </div>
        </div>
        <div class="field">
          <label for="llmKey">API key</label>
          <input id="llmKey" v-model="apiKey" type="password" autocomplete="off" />
          <span class="muted hint">{{ keyHint }}</span>
        </div>
        <div class="actions">
          <button type="submit" :disabled="busy">Save</button>
          <button type="button" class="ghost" :disabled="busy" @click="onTest">Test</button>
          <span class="status" :class="statusClass">{{ status }}</span>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ApiError } from "@/api/client";
import { useLlmStore } from "@/stores/llm";
import type { LlmConfigIn } from "@/api/types";

const store = useLlmStore();

const provider = ref("");
const mainModel = ref("");
const lightModel = ref("");
const apiKey = ref("");
const status = ref("");
const statusOk = ref(false);
const busy = ref(false);

const providers = computed(() => store.config?.providers ?? []);
// Base URL is derived from the provider (display-only; the server resolves it on save).
const baseUrl = computed(() => providers.value.find((p) => p.key === provider.value)?.base_url ?? "");
const keyHint = computed(() =>
  store.config?.has_api_key ? "An API key is saved — leave blank to keep it." : "No API key saved yet.",
);
const statusClass = computed(() => (status.value && statusOk.value ? "ok" : status.value ? "err" : ""));

function syncFromConfig(): void {
  const c = store.config;
  if (!c) return;
  provider.value = c.provider;
  mainModel.value = c.main_model;
  lightModel.value = c.light_model;
  apiKey.value = "";
}

function body(): LlmConfigIn {
  const b: LlmConfigIn = {
    provider: provider.value,
    main_model: mainModel.value,
    light_model: lightModel.value,
  };
  if (apiKey.value.trim()) b.api_key = apiKey.value;
  return b;
}

async function onSave(): Promise<void> {
  busy.value = true;
  status.value = "Saving…";
  statusOk.value = false;
  try {
    await store.save(body());
    syncFromConfig();
    status.value = "Saved.";
    statusOk.value = true;
  } catch (e) {
    status.value = e instanceof ApiError ? e.message : "Could not save settings";
  } finally {
    busy.value = false;
  }
}

async function onTest(): Promise<void> {
  busy.value = true;
  status.value = "Testing your models…";
  statusOk.value = false;
  try {
    const r = await store.test(body());
    status.value = (r.ok ? "✓ " : "✗ ") + r.detail;
    statusOk.value = r.ok;
  } catch (e) {
    status.value = e instanceof ApiError ? e.message : "Could not test the models";
  } finally {
    busy.value = false;
  }
}

onMounted(async () => {
  await store.load();
  syncFromConfig();
});
</script>

<style scoped>
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.form { display: grid; gap: 14px; margin-top: 14px; max-width: 640px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
.field { display: grid; gap: 6px; min-width: 0; }
.field > label { font-size: 13px; color: var(--muted); }
.hint { font-size: 12px; }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.status { font-size: 13px; color: var(--muted); }
.status.ok { color: var(--text-success); }
.status.err { color: var(--text-error); }
@media (max-width: 560px) { .row { grid-template-columns: 1fr; } }
</style>
