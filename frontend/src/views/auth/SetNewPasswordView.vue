<template>
  <AuthShell>
    <h1>Set a new password</h1>
    <p class="subtitle">You signed in with a temporary password. Choose a new one to finish.</p>

    <form @submit.prevent="onSubmit">
      <label for="password">New password</label>
      <input id="password" v-model="password" type="password" autocomplete="new-password" />
      <p class="muted hint">At least 8 characters, including a letter and a number.</p>
      <p v-if="error" class="err">{{ error }}</p>
      <button type="submit" :disabled="busy" class="full">Save and continue</button>
    </form>
  </AuthShell>
</template>

<script setup lang="ts">
import { ref } from "vue";
import AuthShell from "@/components/AuthShell.vue";
import { errorText, postJson } from "@/utils/authApi";

const password = ref("");
const error = ref("");
const busy = ref(false);

async function onSubmit(): Promise<void> {
  error.value = "";
  busy.value = true;
  try {
    const r = await postJson("/api/auth/set-new-password", { new_password: password.value });
    if (r.ok) {
      window.location.assign("/app");
      return;
    }
    if (r.status === 401) {
      window.location.assign("/app/login"); // no valid session (e.g. opened directly)
      return;
    }
    error.value = errorText(r.data?.detail) || "Something went wrong";
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
h1 { margin: 0 0 4px; }
.subtitle { margin: 0 0 22px; color: var(--text-secondary); font-size: 13px; }
label { display: block; font-size: 13px; color: var(--muted); }
input { width: 100%; }
.hint { margin: 6px 0 0; font-size: 12px; }
.err { color: var(--text-error); font-size: 13px; margin: 12px 0 0; }
.full { width: 100%; margin-top: 18px; }
</style>
