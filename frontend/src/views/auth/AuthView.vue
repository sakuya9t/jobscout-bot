<template>
  <AuthShell>
    <h1>{{ isRegister ? "Create account" : "Welcome back" }}</h1>
    <p class="subtitle">{{ isRegister ? "Set up your private job search workspace." : "Log in to review matches and application kits." }}</p>

    <form @submit.prevent="onSubmit">
      <label for="email">Email</label>
      <input id="email" v-model="email" type="email" autocomplete="username" />

      <label for="password">Password</label>
      <input id="password" v-model="password" type="password" :autocomplete="isRegister ? 'new-password' : 'current-password'" />

      <p v-if="!isRegister" class="link-line"><RouterLink to="/app/forgot-password">Forgot password?</RouterLink></p>
      <template v-else>
        <p class="muted hint">At least 8 characters, including a letter and a number.</p>
        <label for="invite">Invitation code</label>
        <input id="invite" v-model="invite" type="text" autocomplete="off" placeholder="Required to register" />
      </template>

      <p v-if="error" class="err">{{ error }}</p>
      <button type="submit" :disabled="busy" class="full">{{ isRegister ? "Sign up" : "Log in" }}</button>
    </form>

    <p class="footer">
      <template v-if="isRegister">Already have an account? <RouterLink to="/app/login">Log in</RouterLink></template>
      <template v-else>New here? <RouterLink to="/app/register">Create an account</RouterLink></template>
    </p>
  </AuthShell>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useRoute } from "vue-router";
import AuthShell from "@/components/AuthShell.vue";
import { errorText, getMe, postJson, safeNext } from "@/utils/authApi";

const route = useRoute();
const isRegister = computed(() => route.meta.mode === "register");

const email = ref("");
const password = ref("");
const invite = ref("");
const error = ref("");
const busy = ref(false);

async function onSubmit(): Promise<void> {
  error.value = "";
  busy.value = true;
  try {
    const path = isRegister.value ? "/api/auth/register" : "/api/auth/login";
    const body = isRegister.value
      ? { email: email.value, password: password.value, invite_code: invite.value }
      : { email: email.value, password: password.value };
    const r = await postJson(path, body);
    if (!r.ok) {
      error.value = errorText(r.data?.detail) || "Something went wrong";
      return;
    }
    // A temp-password login forces a password change before the app is usable.
    const me = await getMe();
    const dest = me?.must_change_password ? "/app/set-new-password" : safeNext(route.query.next);
    window.location.assign(dest); // full nav so the auth store re-inits with the new session
  } finally {
    busy.value = false;
  }
}
</script>

<style scoped>
h1 { margin: 0 0 4px; }
.subtitle { margin: 0 0 22px; color: var(--text-secondary); font-size: 13px; }
label { display: block; margin-top: 12px; font-size: 13px; color: var(--muted); }
input { width: 100%; }
.hint { margin: 6px 0 0; font-size: 12px; }
.link-line { margin: 6px 0 0; font-size: 13px; }
.err { color: var(--text-error); font-size: 13px; margin: 12px 0 0; }
.full { width: 100%; margin-top: 18px; }
.footer { margin: 18px 0 0; color: var(--text-secondary); font-size: 13px; }
</style>
