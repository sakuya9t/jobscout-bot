<template>
  <section class="panel">
    <div class="surface">
      <h2>Telegram</h2>
      <p class="muted">Deliver the daily report over Telegram. Save your bot token, then DM
        the bot the start code and link the chat.</p>

      <form class="form" @submit.prevent="onSave">
        <div class="field">
          <label for="tgToken">Bot token</label>
          <input id="tgToken" v-model="token" type="password" autocomplete="off" />
          <span class="muted hint">{{ tokenHint }}</span>
        </div>

        <div class="link-status">
          <template v-if="config?.linked">
            ✅ Linked to chat <code>{{ config.chat_id }}</code>.
            <button type="button" class="ghost sm" :disabled="busy" @click="onRegen">Re-link with a new code</button>
          </template>
          <template v-else-if="config?.link_code">
            Not linked yet. DM your bot <code>/start {{ config.link_code }}</code>, then click <b>Link chat</b>.
            <button type="button" class="ghost sm" :disabled="busy" @click="onRegen">New code</button>
          </template>
          <template v-else>
            Not linked.
            <button type="button" class="ghost sm" :disabled="busy" @click="onRegen">Generate link code</button>
          </template>
        </div>

        <div class="actions">
          <button type="submit" :disabled="busy">Save</button>
          <button type="button" class="ghost" :disabled="busy" @click="onLink">Link chat</button>
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
import { useTelegramStore } from "@/stores/telegram";

const store = useTelegramStore();
const config = computed(() => store.config);

const token = ref("");
const status = ref("");
const statusOk = ref(false);
const busy = ref(false);

const tokenHint = computed(() =>
  config.value?.has_token
    ? "A bot token is saved — leave blank to keep it, or paste a new one to replace it."
    : "No bot token saved yet — paste your bot token and Save.",
);
const statusClass = computed(() => (status.value && statusOk.value ? "ok" : status.value ? "err" : ""));

async function run(label: string, fn: () => Promise<void>): Promise<void> {
  busy.value = true;
  status.value = label;
  statusOk.value = false;
  try {
    await fn();
  } catch (e) {
    status.value = e instanceof ApiError ? e.message : "Something went wrong";
  } finally {
    busy.value = false;
  }
}

function onSave(): Promise<void> {
  return run("Saving…", async () => {
    await store.save(token.value.trim() ? { bot_token: token.value } : {});
    token.value = "";
    status.value = "Saved.";
    statusOk.value = true;
  });
}

function onLink(): Promise<void> {
  return run("Looking for your /start message…", async () => {
    const r = await store.link();
    status.value = (r.ok ? "✓ " : "✗ ") + r.detail;
    statusOk.value = r.ok;
  });
}

function onTest(): Promise<void> {
  return run("Sending a test message…", async () => {
    const r = await store.test();
    status.value = (r.ok ? "✓ " : "✗ ") + r.detail;
    statusOk.value = r.ok;
  });
}

function onRegen(): Promise<void> {
  return run("", () => store.regenCode());
}

onMounted(() => void store.load());
</script>

<style scoped>
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.form { display: grid; gap: 14px; margin-top: 14px; max-width: 560px; }
.field { display: grid; gap: 6px; }
.field > label { font-size: 13px; color: var(--muted); }
.hint { font-size: 12px; }
.link-status {
  font-size: 13px; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--surface-soft); display: flex; flex-wrap: wrap; align-items: center; gap: 8px;
}
.link-status code { padding: 1px 5px; border-radius: 5px; background: var(--surface); }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.ghost.sm { padding: 4px 10px; min-height: 30px; font-size: 12px; }
.status { font-size: 13px; color: var(--muted); }
.status.ok { color: var(--text-success); }
.status.err { color: var(--text-error); }
</style>
