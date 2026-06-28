<template>
  <div class="ym">
    <select :value="ym.month" :disabled="disabled" @change="onMonth">
      <option value="">Month</option>
      <option v-for="[v, n] in MONTHS" :key="v" :value="v">{{ n }}</option>
    </select>
    <select :value="ym.year" :disabled="disabled" @change="onYear">
      <option value="">Year</option>
      <option v-for="y in years" :key="y" :value="String(y)">{{ y }}</option>
    </select>
  </div>
</template>

<script setup lang="ts">
import { computed } from "vue";
import { MONTHS, combineYM, parseYM, yearRange } from "@/utils/yearMonth";

const props = defineProps<{ modelValue: string | null; disabled?: boolean }>();
const emit = defineEmits<{ (e: "update:modelValue", v: string): void }>();

const ym = computed(() => parseYM(props.modelValue));
const years = yearRange();

function onMonth(e: Event): void {
  emit("update:modelValue", combineYM(ym.value.year, (e.target as HTMLSelectElement).value));
}
function onYear(e: Event): void {
  emit("update:modelValue", combineYM((e.target as HTMLSelectElement).value, ym.value.month));
}
</script>

<style scoped>
.ym { display: flex; gap: 8px; }
.ym > select { flex: 1; min-width: 0; }
</style>
