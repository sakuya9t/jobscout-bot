<template>
  <section class="panel">
    <div class="surface">
      <h2>Companies</h2>
      <p class="muted">Career sites included in each scan. Pick a preset for a one-click add,
        or enter a custom company and its ATS.</p>

      <form class="form" @submit.prevent="onAdd">
        <div class="field">
          <label>Preset</label>
          <select v-model="form.preset" @change="applyPreset">
            <option value="">Choose a preset</option>
            <option v-for="p in store.availablePresets" :key="p.key" :value="p.key">{{ p.name }}</option>
          </select>
        </div>
        <div class="row">
          <div class="field"><label>Name</label><input v-model="form.name" type="text" :disabled="locked" /></div>
          <div class="field"><label>Careers URL</label><input v-model="form.careers_url" type="text" :disabled="locked" /></div>
        </div>
        <div class="row">
          <div class="field">
            <label>ATS</label>
            <select v-model="form.ats_type" :disabled="locked">
              <option v-for="a in ATS_TYPES" :key="a" :value="a">{{ a }}</option>
            </select>
          </div>
          <div class="field"><label>ATS token</label><input v-model="form.ats_token" type="text" :disabled="locked" /></div>
        </div>
        <p v-if="locked" class="muted hint">Preset companies are shared and centrally managed — these fields are fixed.</p>
        <div class="actions">
          <button type="submit" :disabled="busy">Add company</button>
          <span class="status err">{{ error }}</span>
        </div>
      </form>

      <div class="list">
        <div v-for="c in store.companies" :key="c.id" class="list-item">
          <span class="company-meta">
            <RouterLink :to="`/app/companies/${c.id}`"><b>{{ c.name }}</b></RouterLink>
            <span class="pill">{{ c.ats_type }}</span>
            <span v-if="c.requires_account" class="pill" :class="c.account_attached ? 'good' : 'warn'">
              {{ c.account_attached ? "account attached" : "account needed" }}
            </span>
            <span v-if="c.careers_url" class="muted url">{{ c.careers_url }}</span>
          </span>
          <button type="button" class="ghost" :disabled="busy" @click="onRemove(c.id)">Remove</button>
        </div>
        <div v-if="!store.companies.length" class="empty">No companies on the watchlist.</div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ApiError } from "@/api/client";
import { useCompaniesStore } from "@/stores/companies";
import { useJobsStore } from "@/stores/jobs";
import { useRescoreOnLeave } from "@/composables/useRescoreOnLeave";
import type { CompanyIn } from "@/api/types";

const store = useCompaniesStore();
const jobs = useJobsStore();
useRescoreOnLeave();

const ATS_TYPES = ["auto", "greenhouse", "lever", "ashby", "google", "eightfold", "html"];

const form = reactive({ preset: "", name: "", careers_url: "", ats_type: "auto", ats_token: "" });
const locked = computed(() => !!form.preset);
const busy = ref(false);
const error = ref("");

// A preset describes a shared, centrally-managed company — fill and lock its fields; the
// backend subscribes via preset_key and ignores the submitted values. Clearing re-enables.
function applyPreset(): void {
  const p = store.presets.find((x) => x.key === form.preset);
  if (!p) {
    Object.assign(form, { name: "", careers_url: "", ats_type: "auto", ats_token: "" });
    return;
  }
  form.name = p.name;
  form.careers_url = p.careers_url;
  form.ats_type = p.ats_type;
  form.ats_token = p.ats_token ?? "";
}

function resetForm(): void {
  Object.assign(form, { preset: "", name: "", careers_url: "", ats_type: "auto", ats_token: "" });
}

async function onAdd(): Promise<void> {
  const body: CompanyIn = {
    name: form.name.trim(),
    careers_url: form.careers_url.trim() || null,
    ats_type: form.ats_type,
    ats_token: form.ats_token.trim() || null,
    preset_key: form.preset || null,
  };
  busy.value = true;
  error.value = "";
  try {
    await store.add(body);
    resetForm();
    jobs.markRescorePending();
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Could not add the company";
  } finally {
    busy.value = false;
  }
}

async function onRemove(id: number): Promise<void> {
  busy.value = true;
  error.value = "";
  try {
    await store.remove(id);
    jobs.markRescorePending();
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Could not remove the company";
  } finally {
    busy.value = false;
  }
}

onMounted(() => {
  void store.load();
  void store.loadPresets();
});
</script>

<style scoped>
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.form { display: grid; gap: 12px; margin: 14px 0; max-width: 640px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field { display: grid; gap: 6px; min-width: 0; }
.field > label { font-size: 13px; color: var(--muted); }
.hint { font-size: 12px; margin: 0; }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.status.err { font-size: 13px; color: var(--text-error); }
.list { display: grid; gap: 8px; margin-top: 6px; }
.list-item {
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 10px 14px; border: 1px solid var(--line); border-radius: 8px; font-size: 14px;
}
.company-meta { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; min-width: 0; }
.company-meta .url { word-break: break-all; }
.pill {
  display: inline-flex; padding: 2px 8px; border-radius: 999px; font-size: 12px; font-weight: 600;
  background: var(--surface-soft); color: var(--text-secondary);
}
.pill.good { background: var(--bg-badge-success); color: var(--text-success); }
.pill.warn { background: var(--bg-badge-warning); color: var(--text-warning); }
.empty {
  border: 1px dashed var(--line); border-radius: 8px; padding: 20px; color: var(--muted);
  background: var(--surface-raised);
}
@media (max-width: 560px) { .row { grid-template-columns: 1fr; } }
</style>
