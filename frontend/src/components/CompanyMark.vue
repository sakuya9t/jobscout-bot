<template>
  <div class="company-mark" :class="{ 'has-logo': logo }" :style="{ '--mark-size': size + 'px' }" aria-hidden="true">
    <img v-if="logo" :src="logo" alt="" loading="lazy" />
    <span v-else>{{ initial }}</span>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { companyLogo } from "@/utils/companyLogo";

const props = withDefaults(defineProps<{ name: string; size?: number }>(), { size: 44 });

const initial = computed(() => {
  const t = (props.name || "").trim();
  return t ? t.charAt(0).toUpperCase() : "?";
});
const logo = computed(() => companyLogo(props.name));
</script>

<style scoped>
.company-mark {
  width: var(--mark-size, 44px); height: var(--mark-size, 44px); flex: 0 0 var(--mark-size, 44px);
  border-radius: 10px; overflow: hidden;
  display: grid; place-items: center; background: var(--bg-tag); color: var(--brand-primary);
  font-size: calc(var(--mark-size, 44px) * 0.41); line-height: 1; font-weight: 800;
  border: 1px solid var(--border-default);
}
/* Logos render on a fixed light tile so brand colors stay legible in both themes. */
.company-mark.has-logo { background: #fff; padding: calc(var(--mark-size, 44px) * 0.16); }
.company-mark img { width: 100%; height: 100%; object-fit: contain; display: block; }
</style>
