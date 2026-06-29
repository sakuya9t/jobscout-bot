<template>
  <AuthShell>
    <h1>Forgot password</h1>
    <p class="subtitle">Enter your account email and we'll send a temporary password to your linked Telegram chat.</p>

    <form @submit.prevent="onSubmit">
      <label for="email">Email</label>
      <input id="email" v-model="email" type="email" autocomplete="username" />
      <p v-if="error" class="err">{{ error }}</p>
      <p v-if="sent" class="ok">If an account with that email has Telegram linked, a temporary password is on its way. It
        expires soon — log in with it to set a new one.</p>
      <button type="submit" :disabled="busy" class="full">Send temporary password</button>
    </form>

    <p class="footer">Remembered it? <RouterLink to="/app/login">Back to log in</RouterLink></p>
  </AuthShell>
</template>

<script setup lang="ts">
import { ref } from "vue";
import AuthShell from "@/components/AuthShell.vue";
import { errorText, postJson } from "@/utils/authApi";

const email = ref("");
const error = ref("");
const sent = ref(false);
const busy = ref(false);

async function onSubmit(): Promise<void> {
  error.value = "";
  sent.value = false;
  busy.value = true;
  try {
    const r = await postJson("/api/auth/forgot-password", { email: email.value });
    if (r.ok) sent.value = true;
    else error.value = errorText(r.data?.detail) || "Something went wrong";
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
.err { color: var(--text-error); font-size: 13px; margin: 12px 0 0; }
.ok { color: var(--text-success); font-size: 13px; margin: 12px 0 0; }
.full { width: 100%; margin-top: 18px; }
.footer { margin: 18px 0 0; color: var(--text-secondary); font-size: 13px; }
</style>
