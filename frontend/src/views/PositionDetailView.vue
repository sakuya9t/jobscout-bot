<template>
  <main class="detail">
    <DetailTopbar back-to="/app/jobs" back-label="Back to jobs" />

    <div class="content">
      <div v-if="loadState === 'loading'" class="surface center"><span class="spinner"></span>Loading position…</div>
      <div v-else-if="loadState === 'notfound'" class="surface empty">
        This position isn't in your job list. <RouterLink to="/app/jobs">Back to your jobs</RouterLink>.
      </div>
      <div v-else-if="loadState === 'error'" class="surface errbox">Could not load this position.</div>

      <template v-else-if="detail">
        <!-- Header -->
        <div class="surface">
          <div class="head-row">
            <div class="job-main">
              <CompanyMark :name="detail.company" :size="48" />
              <div>
                <h1>{{ detail.title }}</h1>
                <div class="meta">{{ metaBits }}</div>
              </div>
            </div>
            <div class="head-right">
              <span v-if="detail.non_matching" class="pill bad">not a match</span>
              <div v-else class="score-block">
                <div class="score-number">{{ detail.match_score }}<span class="score-suffix">/100</span></div>
                <div class="score-win">win {{ detail.win_probability }}%</div>
              </div>
              <div v-if="detail.salary_display" class="pay">{{ detail.salary_display }}</div>
            </div>
          </div>
          <div v-if="detail.removed" class="pill neutral removed-pill">No longer listed — this posting has been removed from the company's board. Your application record is kept.</div>
          <div class="actions">
            <button type="button" class="apply-btn" :class="{ applied }" :aria-pressed="applied" @click="onToggleApplied">
              {{ applied ? "✓ Applied" : "Mark applied" }}
            </button>
            <a v-if="detail.url" :href="detail.url" target="_blank" rel="noopener">Go to posting →</a>
          </div>
        </div>

        <!-- How you line up -->
        <div class="surface">
          <div class="fit-head">
            <h2>How you line up</h2>
            <div class="reeval-wrap">
              <button type="button" class="reeval-btn" :disabled="detail.removed || reevalBusy"
                      :title="detail.removed ? 'This posting is no longer listed — re-evaluation is disabled.' : 'Re-run the AI scoring for this posting'"
                      @click="onReevaluate">↻ Re-evaluate</button>
              <span class="reeval-status">
                <template v-if="reevalBusy"><span class="spinner"></span>Re-evaluating…</template>
                <template v-else-if="reevalError">{{ reevalError }}</template>
              </span>
            </div>
          </div>
          <template v-if="hasFit">
            <div v-if="showBreakdown" class="breakdown-grid">
              <div class="score-orb" :style="{ '--score-pct': clamp(detail.match_score) + '%' }">
                <div class="score-orb-inner">
                  <div class="score-orb-score">{{ clamp(detail.match_score) }}</div>
                  <div class="score-orb-label">match score</div>
                </div>
              </div>
              <div class="subscore-list">
                <div v-for="(item, i) in detail.score_breakdown" :key="i" class="subscore-row">
                  <div class="subscore-head">
                    <div class="subscore-label">{{ item.label || "Aspect" }}</div>
                    <div class="subscore-value">{{ clamp(item.score) }}/100</div>
                  </div>
                  <div class="subscore-track"><div class="subscore-fill" :style="{ '--subscore-pct': clamp(item.score) + '%' }"></div></div>
                  <div v-if="item.rationale" class="subscore-note">{{ item.rationale }}</div>
                </div>
              </div>
            </div>
            <div v-if="detail.reasoning" class="fit-summary">{{ detail.reasoning }}</div>
            <div class="fit-columns">
              <section class="fit-card is-winning">
                <h3>Winning</h3>
                <ul v-if="detail.strengths.length"><li v-for="(s, i) in detail.strengths" :key="i">{{ s }}</li></ul>
                <p v-else class="fit-empty">No clear winning signals were identified yet.</p>
              </section>
              <section class="fit-card is-risk">
                <h3>Risks</h3>
                <ul v-if="detail.gaps.length"><li v-for="(s, i) in detail.gaps" :key="i">{{ s }}</li></ul>
                <p v-else class="fit-empty">No major risk areas were identified.</p>
              </section>
            </div>
          </template>
          <p v-else class="fit-empty">No fit breakdown yet — re-evaluate to score this posting.</p>
        </div>

        <!-- Application kit -->
        <div class="surface">
          <h2>AI application kit</h2>
          <p class="muted">A tailored cover letter, a revised resume, what this role is looking for, and draft
            answers to its likely application questions — generated from your resume and this posting.</p>
          <div class="kit-controls">
            <button type="button" :disabled="detail.removed || kitGenerating" @click="onGenerate">
              {{ kitGenerating ? "Generating…" : kit ? "Regenerate" : "Generate application kit" }}
            </button>
            <span class="muted kit-status">
              <template v-if="detail.removed && !kit">This posting is no longer listed — kit generation is disabled.</template>
              <template v-else-if="kitGenerating"><span class="spinner"></span>Writing your kit — this takes ~30-60s. You can leave and come back.</template>
              <template v-else-if="kit && kit.model">Generated with {{ kit.model }}.</template>
            </span>
          </div>

          <div v-if="kit && kit.status === 'error'" class="errbox kit-body">
            Generation failed: {{ kit.error_detail || "unknown error" }}<br>
            Check your LLM provider settings, then click <b>Regenerate</b>.
          </div>
          <div v-else-if="kit && kit.status === 'ok'" class="kit-body">
            <div v-if="kit.looking_for.length" class="section-gap">
              <h3>What this role is looking for</h3>
              <ul class="tight"><li v-for="(s, i) in kit.looking_for" :key="i">{{ s }}</li></ul>
            </div>
            <div v-if="kit.open_questions.length" class="section-gap">
              <h3>Application questions</h3>
              <div v-for="(q, i) in kit.open_questions" :key="i" class="qa">
                <div class="q">{{ q.question }}</div>
                <div v-if="q.advice" class="advice">{{ q.advice }}</div>
                <DocBlock v-if="q.suggested_answer" :text="q.suggested_answer" />
              </div>
            </div>
            <div v-if="kit.cover_letter" class="section-gap">
              <h3>Cover letter</h3>
              <DocBlock :text="kit.cover_letter" />
            </div>
            <div v-if="kit.revised_resume" class="section-gap">
              <h3>Tailored resume</h3>
              <div class="doc">
                <div class="doc-bar">
                  <button type="button" class="copy" @click="downloadPdf">Download PDF</button>
                  <DocBlock :text="kit.revised_resume" inline-button label="Copy Markdown" />
                </div>
                <div class="resume-rendered" v-html="resumeHtml"></div>
              </div>
              <p v-if="kit.resume_optimization" class="advice">{{ kit.resume_optimization }}</p>
            </div>
            <div v-if="kitEmpty" class="empty">The model returned an empty kit. Try Regenerate.</div>
          </div>
        </div>
      </template>
    </div>

    <!-- Print-only resume mount (shown in place of the page by the print stylesheet) -->
    <Teleport to="body">
      <div id="printRoot"><div class="resume-rendered" v-html="resumeHtml"></div></div>
    </Teleport>
  </main>
</template>

<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import { useRoute } from "vue-router";
import { ApiError } from "@/api/client";
import { usePositionStore } from "@/stores/position";
import { usePolling } from "@/composables/usePolling";
import { fmtListed } from "@/utils/format";
import { mdToHtml } from "@/utils/markdown";
import DetailTopbar from "@/components/DetailTopbar.vue";
import DocBlock from "@/components/DocBlock.vue";
import CompanyMark from "@/components/CompanyMark.vue";
import type { ApplicationKitOut, PositionDetailOut } from "@/api/types";

const route = useRoute();
const store = usePositionStore();
const id = Number(route.params.id);

const detail = ref<PositionDetailOut | null>(null);
const kit = ref<ApplicationKitOut | null>(null);
const applied = ref(false);
const loadState = ref<"loading" | "ok" | "notfound" | "error">("loading");
const reevalBusy = ref(false);
const reevalError = ref<string | null>(null);

const metaBits = computed(() => {
  const d = detail.value;
  if (!d) return "";
  return [d.company, d.location, d.department, fmtListed(d.listed_at)].filter(Boolean).join(" · ");
});
const showBreakdown = computed(() => {
  const d = detail.value;
  return !!d && !d.non_matching && d.score_breakdown.length > 0 && d.match_score != null;
});
const hasFit = computed(() => {
  const d = detail.value;
  return !!d && (showBreakdown.value || !!d.reasoning || d.strengths.length > 0 || d.gaps.length > 0);
});
const kitGenerating = computed(() => kit.value?.status === "generating");
const kitEmpty = computed(() =>
  !!kit.value && !kit.value.looking_for.length && !kit.value.open_questions.length && !kit.value.cover_letter && !kit.value.revised_resume,
);
const resumeHtml = computed(() => (kit.value?.revised_resume ? mdToHtml(kit.value.revised_resume) : ""));

function clamp(v: number | null): number {
  const n = Number(v);
  return Number.isNaN(n) ? 0 : Math.max(0, Math.min(100, Math.trunc(n)));
}

async function load(): Promise<void> {
  try {
    detail.value = await store.loadDetail(id);
    kit.value = detail.value.kit;
    applied.value = detail.value.applied;
    loadState.value = "ok";
    if (kit.value?.status === "generating") kitPoll.start();
  } catch (e) {
    loadState.value = e instanceof ApiError && e.status === 404 ? "notfound" : "error";
  }
}

const kitPoll = usePolling(async () => {
  kit.value = await store.getKit(id);
  return kit.value.status === "generating" ? 4000 : null;
});

const reevalPoll = usePolling(async () => {
  const st = await store.rescoreStatus(id);
  if (st.in_progress) return 3000;
  reevalBusy.value = false;
  reevalError.value = st.error;
  if (!st.error) await load();
  return null;
});

async function onGenerate(): Promise<void> {
  if (detail.value?.removed) return;
  try {
    kit.value = await store.generateKit(id);
    if (kit.value.status === "generating") kitPoll.start();
  } catch (e) {
    reevalError.value = null; // generation errors surface in the kit body via reload
    if (e instanceof ApiError) alert(e.message);
  }
}

async function onReevaluate(): Promise<void> {
  if (detail.value?.removed || reevalBusy.value) return;
  reevalBusy.value = true;
  reevalError.value = null;
  try {
    await store.rescore(id);
    reevalPoll.start();
  } catch (e) {
    reevalBusy.value = false;
    reevalError.value = e instanceof ApiError ? e.message : "Re-evaluation failed";
  }
}

async function onToggleApplied(): Promise<void> {
  const next = !applied.value;
  applied.value = next;
  try {
    await store.setApplied(id, next);
  } catch (e) {
    applied.value = !next;
    if (e instanceof ApiError) alert(e.message);
  }
}

function downloadPdf(): void {
  document.body.classList.add("printing");
  setTimeout(() => window.print(), 30);
}
function afterPrint(): void {
  document.body.classList.remove("printing");
}

onMounted(() => {
  window.addEventListener("afterprint", afterPrint);
  void load();
});
onUnmounted(() => {
  window.removeEventListener("afterprint", afterPrint);
  document.body.classList.remove("printing");
});
</script>

<style scoped>
.content { max-width: 920px; margin: 0 auto; padding: 24px; display: grid; gap: 16px; }
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs);
}
.center { display: flex; align-items: center; gap: 10px; color: var(--muted); }
h1 { font-size: 22px; margin: 0; }
h2 { margin: 0 0 4px; }
.meta { color: var(--muted); font-size: 13px; margin-top: 4px; }
.head-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 16px; }
.job-main { display: flex; align-items: flex-start; gap: 14px; min-width: 0; }
.head-right { display: flex; flex-direction: column; align-items: flex-end; gap: 6px; flex-shrink: 0; }
.score-block { text-align: right; }
.score-number { font-size: 26px; font-weight: 800; color: var(--text-score-primary, var(--accent)); }
.score-suffix { font-size: 14px; font-weight: 600; color: var(--muted); }
.score-win { font-size: 12px; color: var(--muted); font-weight: 700; }
.pay { color: var(--ink); font-size: 14px; font-weight: 650; white-space: nowrap; }
.removed-pill { margin-top: 8px; }
.actions { display: flex; align-items: center; gap: 14px; flex-wrap: wrap; margin-top: 16px; }
.apply-btn {
  background: transparent; color: var(--interactive-outline-text); border: 1.5px solid var(--interactive-outline-border);
  border-radius: 8px; padding: 8px 14px; font-size: 14px; font-weight: 600;
}
.apply-btn.applied { background: var(--bg-badge-success); color: var(--text-success); border-color: transparent; }
.pill { display: inline-flex; align-items: center; padding: 3px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: var(--bg-tag); color: var(--brand-primary); }
.pill.bad { background: var(--bg-badge-error); color: var(--text-error); }
.pill.neutral { background: var(--bg-badge-neutral, var(--surface-soft)); color: var(--text-secondary); }
.fit-head { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; margin-bottom: 12px; }
.reeval-wrap { display: flex; flex-direction: column; align-items: flex-end; gap: 4px; flex-shrink: 0; }
.reeval-btn { background: transparent; color: var(--text-link); border: 1px solid var(--line); border-radius: 8px; padding: 5px 11px; font-size: 12.5px; font-weight: 600; white-space: nowrap; }
.reeval-btn:hover:not(:disabled) { border-color: var(--accent); color: var(--accent); }
.reeval-btn:disabled { opacity: .6; }
.reeval-status { color: var(--text-secondary); font-size: 12px; max-width: 220px; text-align: right; }
.breakdown-grid { display: grid; grid-template-columns: 160px 1fr; gap: 22px; align-items: center; margin-bottom: 18px; }
.score-orb {
  width: 160px; height: 160px; border-radius: 50%; display: grid; place-items: center; justify-self: center;
  background: conic-gradient(var(--brand-primary) var(--score-pct), var(--bg-input-hover) 0); position: relative; box-shadow: var(--shadow-sm);
}
.score-orb::before { content: ""; position: absolute; inset: 14px; border-radius: 50%; background: var(--surface); border: 1px solid var(--line); }
.score-orb-inner { position: relative; z-index: 1; display: grid; justify-items: center; line-height: 1; }
.score-orb-score { color: var(--text-score-primary, var(--accent)); font-size: 36px; font-weight: 800; }
.score-orb-label { color: var(--text-secondary); font-size: 12px; font-weight: 700; margin-top: 6px; }
.subscore-list { display: grid; gap: 12px; }
.subscore-row { display: grid; gap: 6px; }
.subscore-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.subscore-label { color: var(--text-heading); font-size: 13px; font-weight: 700; }
.subscore-value { color: var(--text-score-primary, var(--accent)); font-size: 13px; font-weight: 800; }
.subscore-track { height: 8px; border-radius: 999px; background: var(--bg-input-hover); overflow: hidden; }
.subscore-fill { height: 100%; width: var(--subscore-pct); background: var(--brand-primary); }
.subscore-note { color: var(--text-secondary); font-size: 12px; line-height: 18px; }
.fit-summary { margin-top: 16px; padding: 14px 16px; border: 1px solid var(--line); border-radius: 12px; background: var(--bg-input, var(--surface-soft)); }
.fit-columns { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; margin-top: 16px; }
.fit-card { border: 1px solid var(--line); border-radius: 12px; padding: 16px; background: var(--surface-raised); }
.fit-card h3 { display: flex; align-items: center; gap: 8px; margin: 0 0 10px; font-size: 14px; }
.fit-card h3::before { content: ""; width: 9px; height: 9px; border-radius: 50%; background: var(--brand-primary); }
.fit-card.is-winning h3::before { background: var(--text-success); }
.fit-card.is-risk h3::before { background: var(--text-warning); }
.fit-card ul { margin: 0; padding-left: 18px; }
.fit-empty { color: var(--text-secondary); font-size: 13px; margin: 0; }
.kit-controls { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; margin-bottom: 6px; }
.kit-status { display: inline-flex; align-items: center; }
.kit-body { margin-top: 8px; }
.section-gap { margin-top: 18px; }
.qa { border-top: 1px solid var(--line); padding: 14px 0; }
.qa:first-child { border-top: 0; }
.qa .q { font-weight: 700; }
.qa .advice, .advice { color: var(--muted); font-size: 13px; margin: 6px 0; }
.doc { background: var(--bg-input, var(--surface-soft)); border: 1px solid var(--line); border-radius: 8px; padding: 10px 14px 14px; margin-top: 8px; }
.doc-bar { display: flex; justify-content: flex-end; gap: 8px; margin-bottom: 6px; }
.doc .copy { font-size: 12px; padding: 5px 10px; border-radius: 6px; background: transparent; color: var(--text-secondary); border: 1px solid var(--line); }
.doc .copy:hover { border-color: var(--accent); color: var(--accent); }
ul.tight { margin: 6px 0; padding-left: 20px; }
ul.tight li { margin: 3px 0; }
.spinner { display: inline-block; width: 15px; height: 15px; border: 2px solid var(--line); border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite; vertical-align: -2px; margin-right: 7px; }
@keyframes spin { to { transform: rotate(360deg); } }
.empty { border: 1px dashed var(--line); border-radius: 8px; padding: 18px; color: var(--muted); background: var(--bg-input, var(--surface-soft)); }
.errbox { border: 1px solid var(--border-error); background: var(--bg-badge-error); color: var(--text-error); border-radius: 8px; padding: 12px 14px; }
@media (max-width: 640px) {
  .breakdown-grid { grid-template-columns: 1fr; }
  .fit-columns { grid-template-columns: 1fr; }
  .score-orb { width: 140px; height: 140px; }
}
</style>

<!-- Global (unscoped): the rendered resume + print isolation. .resume-rendered styles
     must reach the teleported #printRoot, which lives outside this component. -->
<style>
.resume-rendered {
  --resume-ink: #111827; --resume-muted: #6B7280; --resume-line: #E5E7EB; --resume-soft: #F3F4F6;
  background: #fff; color: var(--resume-ink); font: 13.5px/1.48 inherit; padding: 28px 34px;
  border-radius: 6px; box-shadow: 0 1px 0 rgba(16, 34, 29, .04); max-width: 780px; margin: 0 auto;
}
.resume-rendered h1 { font-size: 30px; line-height: 1.08; margin: 0; color: #111827; text-align: center; font-weight: 850; letter-spacing: .02em; }
.resume-rendered h1 + p { text-align: center; color: var(--resume-muted); font-size: 12.5px; margin: 6px auto 10px; max-width: 680px; }
.resume-rendered hr { border: 0; height: 3px; width: 86px; margin: 12px auto 18px; background: var(--brand-primary); border-radius: 999px; }
.resume-rendered h2 { font-size: 13px; margin: 19px 0 7px; color: var(--brand-primary); border-bottom: 1px solid var(--resume-line); padding-bottom: 4px; text-transform: uppercase; letter-spacing: .12em; font-weight: 850; }
.resume-rendered h3 { font-size: 15.5px; font-weight: 800; margin: 13px 0 1px; color: #111827; }
.resume-rendered h3 + p { margin-top: 0; color: var(--resume-muted); }
.resume-rendered h3 + p strong { color: #374151; font-weight: 750; }
.resume-rendered p { margin: 4px 0; }
.resume-rendered ul, .resume-rendered ol { margin: 6px 0 8px; padding-left: 0; list-style: none; }
.resume-rendered li { position: relative; margin: 4px 0; padding-left: 15px; break-inside: avoid; }
.resume-rendered li::before { content: ""; position: absolute; left: 0; top: .68em; width: 5px; height: 5px; border-radius: 50%; background: var(--brand-primary); }
.resume-rendered a { color: var(--brand-primary); }
.resume-rendered code { background: var(--resume-soft); padding: 1px 5px; border-radius: 5px; }
.resume-rendered strong { font-weight: 760; }
.resume-rendered em { color: var(--resume-muted); }
@page { margin: 13mm; }
#printRoot { display: none; }
@media print {
  body { background: #fff !important; }
  body.printing #app { display: none !important; }
  body.printing #printRoot { display: block !important; }
  #printRoot .resume-rendered { max-width: none; margin: 0; padding: 0; border-radius: 0; box-shadow: none; print-color-adjust: exact; -webkit-print-color-adjust: exact; }
  #printRoot .resume-rendered h2, #printRoot .resume-rendered h3 { break-after: avoid; }
}
</style>
