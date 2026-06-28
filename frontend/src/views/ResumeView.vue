<template>
  <section class="panel">
    <div class="surface">
      <h2>Resume</h2>
      <p class="muted">The resume used to score postings. Uploading a new one replaces the
        current resume (and re-scores against it). PDF, DOCX, TXT, or Markdown.</p>

      <div class="upload">
        <input ref="fileInput" type="file" accept=".pdf,.docx,.txt,.md" />
        <button type="button" :disabled="busy" @click="onUpload">Upload</button>
        <span class="status err">{{ error }}</span>
      </div>

      <div v-if="active" class="resume-item">
        <span>
          <a href="#" @click.prevent="togglePreview"><b>{{ active.filename }}</b></a>
          <span class="muted"> · active resume · click to preview</span>
        </span>
        <button type="button" class="ghost" :disabled="busy" @click="onRemove">Remove</button>
      </div>
      <div v-else class="empty">No resume uploaded yet.</div>

      <div v-if="previewOpen" class="preview">
        <iframe v-if="isPdf" title="Resume preview" :src="`/api/resumes/${active!.id}/file`" />
        <div v-else-if="previewLoading" class="muted">Loading preview…</div>
        <div v-else-if="previewError" class="empty">Could not load a preview.</div>
        <pre v-else>{{ previewText }}</pre>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { ApiError } from "@/api/client";
import { useResumeStore } from "@/stores/resume";

const store = useResumeStore();
const active = computed(() => store.active);
const isPdf = computed(() => (active.value?.filename ?? "").toLowerCase().endsWith(".pdf"));

const fileInput = ref<HTMLInputElement | null>(null);
const busy = ref(false);
const error = ref("");

const previewOpen = ref(false);
const previewLoading = ref(false);
const previewError = ref(false);
const previewText = ref("");

async function onUpload(): Promise<void> {
  const file = fileInput.value?.files?.[0];
  if (!file) return;
  busy.value = true;
  error.value = "";
  try {
    await store.upload(file);
    if (fileInput.value) fileInput.value.value = "";
    previewOpen.value = false;
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Upload failed";
  } finally {
    busy.value = false;
  }
}

async function onRemove(): Promise<void> {
  if (!active.value) return;
  busy.value = true;
  error.value = "";
  try {
    await store.remove(active.value.id);
    previewOpen.value = false;
  } catch (e) {
    error.value = e instanceof ApiError ? e.message : "Could not remove the resume";
  } finally {
    busy.value = false;
  }
}

async function togglePreview(): Promise<void> {
  if (!active.value) return;
  if (previewOpen.value) {
    previewOpen.value = false;
    return;
  }
  previewOpen.value = true;
  if (isPdf.value) return; // the iframe renders the file directly
  previewLoading.value = true;
  previewError.value = false;
  try {
    const c = await store.content(active.value.id);
    previewText.value = c.content_text || "";
  } catch {
    previewError.value = true;
  } finally {
    previewLoading.value = false;
  }
}

onMounted(() => void store.load());
</script>

<style scoped>
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.upload { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; margin: 14px 0; }
.status.err { font-size: 13px; color: var(--text-error); }
.resume-item {
  display: flex; justify-content: space-between; align-items: center; gap: 12px;
  padding: 12px 14px; border: 1px solid var(--line); border-radius: 8px; font-size: 14px;
}
.empty {
  border: 1px dashed var(--line); border-radius: 8px; padding: 20px; color: var(--muted);
  background: var(--surface-raised);
}
.preview { margin-top: 12px; }
.preview iframe {
  width: 100%; height: 78vh; border: 1px solid var(--line); border-radius: 8px; background: var(--surface);
}
.preview pre {
  white-space: pre-wrap; word-wrap: break-word; background: var(--bg-input, var(--surface-soft));
  border: 1px solid var(--line); border-radius: 8px; padding: 14px; max-height: 78vh; overflow: auto;
}
</style>
