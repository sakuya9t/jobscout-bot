<template>
  <section class="panel">
    <div class="surface">
      <h2>Interests</h2>
      <p class="muted">What roles you're after and how strict the scoring is. Keywords and
        notes steer the model; the minimum score only filters the report.</p>

      <form class="form" @submit.prevent="onSubmit">
        <div class="row">
          <div class="field"><label>Label</label><input v-model="form.label" type="text" placeholder="e.g. Backend roles" /></div>
          <div class="field narrow"><label>Min score</label><input v-model.number="form.min_score" type="number" min="0" max="100" /></div>
        </div>
        <div class="row">
          <div class="field"><label>Title keywords</label><input v-model="form.title_keywords" type="text" placeholder="backend, platform" /></div>
          <div class="field"><label>Locations</label><input v-model="form.locations" type="text" placeholder="remote, NYC" /></div>
        </div>
        <div class="row">
          <div class="field"><label>Seniority</label><input v-model="form.seniority" type="text" placeholder="senior, staff" /></div>
          <div class="field"><label>Exclude keywords</label><input v-model="form.exclude_keywords" type="text" placeholder="manager, sales" /></div>
        </div>
        <div class="field"><label>Notes for the model</label><textarea v-model="form.notes" rows="3" placeholder="Anything else that defines a good fit…"></textarea></div>
        <div class="actions">
          <button type="submit" :disabled="busy">{{ editingId ? "Save changes" : "Add interest" }}</button>
          <button v-if="editingId" type="button" class="ghost" :disabled="busy" @click="resetForm">Cancel</button>
          <span class="status err">{{ error }}</span>
        </div>
      </form>

      <div class="list">
        <div v-for="x in store.interests" :key="x.id" class="list-item">
          <span><b>{{ x.label }}</b>
            <span class="muted">min {{ x.min_score }} · {{ x.title_keywords || "any" }} · {{ x.locations || "any" }}</span>
          </span>
          <span class="row-actions">
            <button type="button" class="ghost" @click="edit(x)">Edit</button>
            <button type="button" class="ghost" @click="onDelete(x.id)">Delete</button>
          </span>
        </div>
        <div v-if="!store.interests.length" class="empty">No interests configured.</div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ApiError } from "@/api/client";
import { useInterestsStore } from "@/stores/interests";
import { useJobsStore } from "@/stores/jobs";
import { useRescoreOnLeave } from "@/composables/useRescoreOnLeave";
import type { InterestIn, InterestOut } from "@/api/types";

const store = useInterestsStore();
const jobs = useJobsStore();
useRescoreOnLeave();

// Fields that change what the model matches against (mirrors the API's _SCORING_FIELDS).
// Editing only the label or min_score threshold doesn't warrant a re-score.
const SCORING_FIELDS = ["title_keywords", "locations", "seniority", "exclude_keywords", "notes"] as const;

const form = reactive({ label: "", min_score: 70, title_keywords: "", locations: "", seniority: "", exclude_keywords: "", notes: "" });
const editingId = ref<number | null>(null);
const busy = ref(false);
const error = ref("");

function body(): InterestIn {
  return {
    label: form.label.trim(),
    title_keywords: form.title_keywords.trim(),
    locations: form.locations.trim(),
    seniority: form.seniority.trim(),
    exclude_keywords: form.exclude_keywords.trim(),
    notes: form.notes.trim(),
    min_score: Number(form.min_score) || 70,
  };
}

function resetForm(): void {
  Object.assign(form, { label: "", min_score: 70, title_keywords: "", locations: "", seniority: "", exclude_keywords: "", notes: "" });
  editingId.value = null;
  error.value = "";
}

function edit(x: InterestOut): void {
  form.label = x.label;
  form.min_score = x.min_score;
  form.title_keywords = x.title_keywords ?? "";
  form.locations = x.locations ?? "";
  form.seniority = x.seniority ?? "";
  form.exclude_keywords = x.exclude_keywords ?? "";
  form.notes = x.notes ?? "";
  editingId.value = x.id;
}

async function onSubmit(): Promise<void> {
  const payload = body();
  if (!payload.label) {
    error.value = "Give the interest a label.";
    return;
  }
  const prev = editingId.value ? store.interests.find((i) => i.id === editingId.value) ?? null : null;
  const criteriaChanged = !prev || SCORING_FIELDS.some((f) => (payload[f] ?? "") !== (prev[f] ?? ""));
  busy.value = true;
  error.value = "";
  try {
    if (editingId.value) await store.update(editingId.value, payload);
    else await store.create(payload);
    resetForm();
    if (criteriaChanged) jobs.markRescorePending();
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Could not save the interest";
  } finally {
    busy.value = false;
  }
}

async function onDelete(id: number): Promise<void> {
  try {
    await store.remove(id);
    if (editingId.value === id) resetForm();
    jobs.markRescorePending();
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Could not delete the interest";
  }
}

onMounted(() => void store.load());
</script>

<style scoped>
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.form { display: grid; gap: 12px; margin: 14px 0; max-width: 640px; }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.field { display: grid; gap: 6px; min-width: 0; }
.field.narrow { max-width: 140px; }
.field > label { font-size: 13px; color: var(--muted); }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.status.err { font-size: 13px; color: var(--text-error); }
.list { display: grid; gap: 8px; margin-top: 6px; }
.list-item {
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 10px 14px; border: 1px solid var(--line); border-radius: 8px; font-size: 14px;
}
.row-actions { display: flex; gap: 6px; }
.empty {
  border: 1px dashed var(--line); border-radius: 8px; padding: 20px; color: var(--muted);
  background: var(--surface-raised);
}
@media (max-width: 560px) { .row { grid-template-columns: 1fr; } }
</style>
