<template>
  <button class="theme-toggle" type="button" aria-label="Toggle light/dark theme" @click="toggleTheme">
    <span class="sun">☀</span>
    <span class="moon">☾</span>
  </button>
</template>

<script setup lang="ts">
import { useTheme } from "@/composables/useTheme";
const { toggleTheme } = useTheme();
</script>
