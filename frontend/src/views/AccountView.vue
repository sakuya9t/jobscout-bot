<template>
  <section class="panel">
    <div class="surface">
      <h2>Account</h2>
      <p class="muted">Change the password you use to log in. Use at least 8 characters,
        including a letter and a number.</p>

      <form class="form" @submit.prevent="onSubmit">
        <div class="field">
          <label for="curPass">Current password</label>
          <input id="curPass" v-model="current" type="password" autocomplete="current-password" />
        </div>
        <div class="field">
          <label for="newPass">New password</label>
          <input id="newPass" v-model="next" type="password" autocomplete="new-password" />
        </div>
        <div class="field">
          <label for="newPass2">Confirm new password</label>
          <input id="newPass2" v-model="confirm" type="password" autocomplete="new-password" />
        </div>
        <div class="actions">
          <button type="submit" :disabled="busy">Change password</button>
          <span class="status" :class="{ ok: done }">{{ status }}</span>
        </div>
      </form>
    </div>
  </section>
</template>

<script setup lang="ts">
import { ref } from "vue";
import { ApiError } from "@/api/client";
import { useAuthStore } from "@/stores/auth";

const auth = useAuthStore();

const current = ref("");
const next = ref("");
const confirm = ref("");
const status = ref("");
const busy = ref(false);
const done = ref(false);

async function onSubmit(): Promise<void> {
  done.value = false;
  if (!current.value || !next.value) {
    status.value = "Fill in your current and new password.";
    return;
  }
  if (next.value !== confirm.value) {
    status.value = "New passwords don't match.";
    return;
  }
  busy.value = true;
  status.value = "Saving…";
  try {
    await auth.changePassword(current.value, next.value);
    status.value = "Password changed.";
    done.value = true;
    current.value = next.value = confirm.value = "";
  } catch (e) {
    status.value = e instanceof ApiError ? e.message : "Could not change password";
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.form { display: grid; gap: 14px; margin-top: 14px; max-width: 420px; }
.field { display: grid; gap: 6px; }
.field > label { font-size: 13px; color: var(--muted); }
.actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.status { font-size: 13px; color: var(--muted); }
.status.ok { color: var(--text-success); }
</style>
