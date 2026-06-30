<template>
  <section class="panel">
    <div class="job-list-shell">
      <div v-if="store.loading && !store.loaded" class="job-list">
        <SkeletonRow v-for="n in 3" :key="n" />
      </div>

      <div v-else-if="store.error" class="empty error">{{ store.error }}</div>

      <template v-else-if="store.count">
        <div class="stat-line">
          <span class="stat">{{ store.total }} application{{ store.total === 1 ? "" : "s" }}</span>
        </div>
        <div class="job-list">
          <ApplicationRow
            v-for="a in store.items"
            :key="a.position_id"
            :a="a"
            @unmark="onUnmark"
          />
        </div>
        <Pager
          v-if="store.total > store.pageSize"
          :total="store.total"
          :page-offset="store.pageOffset"
          :page-size="store.pageSize"
          @go="store.goToPage"
          @size="store.setPageSize"
        />
      </template>

      <div v-else class="empty">
        You haven’t marked any positions as applied yet. Open a job and use “Mark applied” to track it here.
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { onMounted } from "vue";
import { useApplicationsStore } from "@/stores/applications";
import ApplicationRow from "@/components/jobs/ApplicationRow.vue";
import Pager from "@/components/Pager.vue";
import SkeletonRow from "@/components/SkeletonRow.vue";

const store = useApplicationsStore();

async function onUnmark(positionId: number) {
  try {
    await store.unmark(positionId);
  } catch (e) {
    alert(e instanceof Error ? e.message : "Could not update application status");
  }
}

onMounted(() => {
  void store.load();
});
</script>

<style scoped>
.job-list-shell { margin-top: 4px; }
.job-list { display: grid; gap: 16px; }
.stat-line { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; margin-bottom: 16px; }
.stat {
  display: inline-flex; padding: 5px 9px; border-radius: 999px; background: var(--surface-soft);
  color: var(--text-secondary); font-size: 12px; line-height: 16px; font-weight: 600;
}
.empty {
  border: 1px dashed var(--line); border-radius: 8px; padding: 20px; color: var(--muted);
  background: var(--surface-raised);
}
.empty.error { border-color: var(--border-error); color: var(--text-error); background: var(--bg-badge-error); }
</style>
