<template>
  <main class="detail">
    <DetailTopbar back-to="/app/companies" back-label="Back to companies" />

    <div class="content">
      <div v-if="loadState === 'loading'" class="surface center"><span class="spinner"></span>Loading company…</div>
      <div v-else-if="loadState === 'notfound'" class="surface empty">
        This company isn't on your watchlist. <RouterLink to="/app/companies">Back to your watchlist</RouterLink>.
      </div>
      <div v-else-if="loadState === 'error'" class="surface errbox">Could not load this company.</div>

      <template v-else-if="detail">
        <!-- Header -->
        <div class="surface">
          <div class="head-row">
            <div>
              <h1>{{ detail.name }}</h1>
              <div class="meta">
                <span class="pill">{{ detail.ats_type }}</span>
                <span class="muted"> · {{ detail.is_preset ? "Preset company" : "Custom company" }} · last scraped {{ fmtDate(detail.last_scraped_at) }}</span>
              </div>
              <div v-if="detail.careers_url" class="meta">
                <a :href="detail.careers_url" target="_blank" rel="noopener">{{ detail.careers_url }}</a>
              </div>
            </div>
            <span v-if="detail.requires_account" class="pill" :class="detail.account_attached ? 'good' : 'warn'">
              {{ detail.account_attached ? "account attached" : "account needed" }}
            </span>
          </div>
        </div>

        <!-- Account -->
        <div class="surface">
          <h2>Application account</h2>
          <template v-if="!detail.requires_account">
            <div v-if="detail.is_preset" class="note">
              Applying to {{ detail.name }} doesn't require a registered account — you can apply directly from each posting, so there's nothing to store here.
            </div>
            <div v-else class="note">
              This is a custom company you added. Saved application accounts and auto-apply are only supported for built-in preset companies, so there's nothing to store here.
            </div>
          </template>
          <template v-else>
            <p class="muted">{{ detail.name }} requires a registered account to submit an application. Save the credentials you
              apply with — they're <b>encrypted</b> before storage and reused for auto-apply.</p>
            <div class="field">
              <label for="acPortal">Application portal URL</label>
              <input id="acPortal" v-model="form.portal_url" type="text" placeholder="https://…" />
            </div>
            <div class="row">
              <div class="field">
                <label for="acUser">Username / email</label>
                <input id="acUser" v-model="form.username" type="text" autocomplete="off" placeholder="you@example.com" />
              </div>
              <div class="field">
                <label for="acPass">Password</label>
                <input id="acPass" v-model="form.password" type="password" autocomplete="new-password" :placeholder="pwPlaceholder" />
              </div>
            </div>
            <div class="field">
              <label for="acNotes">Notes (optional)</label>
              <textarea id="acNotes" v-model="form.notes" rows="2" placeholder="Anything to remember about this login"></textarea>
            </div>
            <div class="actions">
              <button type="button" :disabled="busy" @click="onSave">Save account</button>
              <button v-if="detail.account_attached" type="button" class="danger" :disabled="busy" @click="onRemove">Remove account</button>
              <span class="status" :class="{ ok: statusOk }">{{ status }}</span>
            </div>
          </template>
        </div>
      </template>
    </div>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { useRoute } from "vue-router";
import { ApiError } from "@/api/client";
import { useCompaniesStore } from "@/stores/companies";
import { fmtDate } from "@/utils/format";
import DetailTopbar from "@/components/DetailTopbar.vue";
import type { CompanyAccountIn, CompanyDetailOut } from "@/api/types";

const route = useRoute();
const store = useCompaniesStore();
const id = Number(route.params.id);

const detail = ref<CompanyDetailOut | null>(null);
const loadState = ref<"loading" | "ok" | "notfound" | "error">("loading");
const form = reactive({ portal_url: "", username: "", password: "", notes: "" });
const status = ref("");
const statusOk = ref(false);
const busy = ref(false);

const pwPlaceholder = computed(() =>
  detail.value?.account_has_password ? "A password is saved — leave blank to keep it" : "Password for the application portal",
);

function syncForm(d: CompanyDetailOut): void {
  form.portal_url = d.account_portal_url ?? "";
  form.username = d.account_username ?? "";
  form.password = "";
  form.notes = d.account_notes ?? "";
}

async function load(): Promise<void> {
  try {
    detail.value = await store.loadDetail(id);
    syncForm(detail.value);
    loadState.value = "ok";
  } catch (e) {
    loadState.value = e instanceof ApiError && e.status === 404 ? "notfound" : "error";
  }
}

function body(): CompanyAccountIn {
  const b: CompanyAccountIn = {
    username: form.username.trim() || null,
    portal_url: form.portal_url.trim() || null,
    notes: form.notes.trim() || null,
  };
  if (form.password.trim()) b.password = form.password; // blank = keep saved password
  return b;
}

async function onSave(): Promise<void> {
  busy.value = true;
  status.value = "Saving…";
  statusOk.value = false;
  try {
    detail.value = await store.saveAccount(id, body());
    syncForm(detail.value);
    status.value = "Saved.";
    statusOk.value = true;
  } catch (e) {
    status.value = e instanceof ApiError ? e.message : "Save failed";
  } finally {
    busy.value = false;
  }
}

async function onRemove(): Promise<void> {
  if (!window.confirm("Remove the saved account for this company?")) return;
  busy.value = true;
  status.value = "Removing…";
  statusOk.value = false;
  try {
    await store.removeAccount(id);
    await load();
  } catch {
    status.value = "Could not remove the account.";
  } finally {
    busy.value = false;
  }
}

onMounted(() => void load());
</script>

<style scoped>
.content { max-width: 760px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.center { display: flex; align-items: center; gap: 10px; color: var(--muted); }
h1 { font-size: 22px; margin: 0; }
h2 { margin: 0 0 8px; }
.head-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.meta { margin-top: 6px; font-size: 13px; display: flex; flex-wrap: wrap; align-items: center; gap: 6px; }
.note { color: var(--muted); font-size: 14px; line-height: 20px; }
.field { display: grid; gap: 6px; margin-top: 12px; }
.field > label { font-size: 13px; color: var(--muted); }
.row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin-top: 14px; }
.pill { display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: var(--bg-tag); color: var(--brand-primary); }
.pill.good { background: var(--bg-badge-success); color: var(--text-success); }
.pill.warn { background: var(--bg-badge-warning); color: var(--text-warning); }
.status { font-size: 13px; color: var(--muted); }
.status.ok { color: var(--text-success); }
.spinner { display: inline-block; width: 15px; height: 15px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -2px; margin-right: 7px; }
@keyframes spin { to { transform: rotate(360deg); } }
.empty { color: var(--muted); }
.errbox { border: 1px solid var(--border-error); background: var(--bg-badge-error); color: var(--text-error); border-radius: 8px; padding: 12px 14px; }
@media (max-width: 560px) { .row { grid-template-columns: 1fr; } }
</style>
