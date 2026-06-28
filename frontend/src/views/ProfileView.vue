<template>
  <section class="panel">
    <!-- Action bar -->
    <div class="surface bar">
      <p class="muted" style="margin:0;">Stored once and reused to autofill job applications. Nothing here is shared until you apply.</p>
      <div class="bar-actions">
        <button type="button" class="ghost" :disabled="busy" @click="onImport">Import from resume</button>
        <label class="overwrite" title="When on, the import overwrites fields that already have a value; otherwise it only fills empty ones">
          <input v-model="overwrite" type="checkbox" /> overwrite filled fields
        </label>
        <button type="button" :disabled="busy" @click="onSave">Save profile</button>
        <span class="status" :class="statusClass">{{ status }}</span>
      </div>
    </div>

    <!-- Contact & links -->
    <div class="surface">
      <h2>Contact &amp; links</h2>
      <div class="grid2">
        <div v-for="[key, label, ph] in CONTACT" :key="key" class="field">
          <label>{{ label }}</label>
          <input v-model="s[key]" type="text" :placeholder="ph || ''" />
        </div>
      </div>
    </div>

    <!-- Work authorization -->
    <div class="surface">
      <h2>Work authorization</h2>
      <div class="grid2">
        <div class="field">
          <label>Work authorization status</label>
          <input v-model="s.work_authorization" type="text" placeholder="e.g. US citizen, H-1B, EU national" />
        </div>
        <div v-for="[key, label] in BOOL_FIELDS" :key="key" class="field">
          <label>{{ label }}</label>
          <select v-model="s[key]">
            <option value="">No answer</option><option value="true">Yes</option><option value="false">No</option>
          </select>
        </div>
      </div>
    </div>

    <!-- Job preferences -->
    <div class="surface">
      <h2>Job preferences</h2>
      <div class="grid2">
        <div class="field"><label>Desired salary</label><input v-model="s.desired_salary" type="text" placeholder="e.g. 140000" /></div>
        <div class="field"><label>Currency</label><input v-model="s.salary_currency" type="text" placeholder="USD" /></div>
        <div class="field">
          <label>Remote preference</label>
          <select v-model="s.remote_preference">
            <option value="">No preference</option><option value="remote">Remote</option>
            <option value="hybrid">Hybrid</option><option value="onsite">Onsite</option>
          </select>
        </div>
        <div class="field"><label>Preferred locations</label><input v-model="s.preferred_locations" type="text" placeholder="Berlin, EU remote" /></div>
        <div class="field"><label>Earliest start date</label><input v-model="s.earliest_start_date" type="text" placeholder="e.g. Immediately, 2026-08-01" /></div>
        <div class="field"><label>Notice period</label><input v-model="s.notice_period" type="text" placeholder="e.g. 4 weeks" /></div>
      </div>
    </div>

    <!-- Education -->
    <div class="surface">
      <div class="bar"><h2 style="margin:0;">Education</h2><button type="button" class="ghost" @click="addEdu">Add education</button></div>
      <div v-for="(e, i) in education" :key="i" class="editor">
        <div class="grid2">
          <div class="field"><label>School</label><input v-model="e.school" type="text" /></div>
          <div class="field"><label>Degree</label><input v-model="e.degree" type="text" /></div>
          <div class="field"><label>Field of study</label><input v-model="e.field_of_study" type="text" /></div>
          <div class="field"><label>GPA</label><input v-model="e.gpa" type="text" /></div>
          <div class="field"><label>Start</label><MonthYearPicker v-model="e.start_date" /></div>
          <div class="field"><label>End</label><MonthYearPicker v-model="e.end_date" /></div>
        </div>
        <div class="field"><label>Location</label><input v-model="e.location" type="text" /></div>
        <div class="field"><label>Description</label><textarea v-model="e.description" rows="2"></textarea></div>
        <div class="bar"><span></span><button type="button" class="ghost" @click="education.splice(i, 1)">Remove</button></div>
      </div>
    </div>

    <!-- Work history -->
    <div class="surface">
      <div class="bar"><h2 style="margin:0;">Work history</h2><button type="button" class="ghost" @click="addExp">Add experience</button></div>
      <div v-for="(x, i) in experience" :key="i" class="editor">
        <div class="grid2">
          <div class="field"><label>Company</label><input v-model="x.company" type="text" /></div>
          <div class="field"><label>Title</label><input v-model="x.title" type="text" /></div>
          <div class="field"><label>Location</label><input v-model="x.location" type="text" /></div>
          <div class="field current">
            <label>&nbsp;</label>
            <label class="check"><input v-model="x.is_current" type="checkbox" @change="onCurrent(x)" /> Current role</label>
          </div>
          <div class="field"><label>Start</label><MonthYearPicker v-model="x.start_date" /></div>
          <div class="field"><label>End</label><MonthYearPicker v-model="x.end_date" :disabled="x.is_current" /></div>
        </div>
        <div class="field"><label>Description</label><textarea v-model="x.description" rows="2"></textarea></div>
        <div class="bar"><span></span><button type="button" class="ghost" @click="experience.splice(i, 1)">Remove</button></div>
      </div>
    </div>

    <!-- EEO -->
    <div class="surface">
      <h2>Voluntary self-identification (EEO)</h2>
      <p class="muted">Optional. These use the standard options most US application sites present; leave as "Prefer not to say" to skip.</p>
      <div class="grid2">
        <div v-for="[key, label, opts] in EEO_FIELDS" :key="key" class="field">
          <label>{{ label }}</label>
          <select v-model="s[key]">
            <option value="">Prefer not to say</option>
            <option v-for="o in opts" :key="o" :value="o">{{ o }}</option>
          </select>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ApiError } from "@/api/client";
import { useProfileStore } from "@/stores/profile";
import MonthYearPicker from "@/components/profile/MonthYearPicker.vue";
import type { ApplicantProfileOut, ProfileEducation, ProfileExperience } from "@/api/types";

const store = useProfileStore();

// ── Field config (drives both rendering and gather/import) ──
const CONTACT: [string, string, string?][] = [
  ["first_name", "First name"], ["last_name", "Last name"],
  ["preferred_name", "Preferred name"], ["pronouns", "Pronouns"],
  ["email", "Email"], ["phone", "Phone"],
  ["address_line1", "Address line 1"], ["address_line2", "Address line 2"],
  ["city", "City"], ["state_region", "State / region"],
  ["postal_code", "Postal code"], ["country", "Country"],
  ["linkedin_url", "LinkedIn URL", "https://linkedin.com/in/…"], ["github_url", "GitHub URL", "https://github.com/…"],
  ["portfolio_url", "Portfolio / website"], ["other_url", "Other link"],
];
const BOOL_FIELDS: [string, string][] = [
  ["authorized_to_work", "Authorized to work (in the role's country)"],
  ["requires_sponsorship", "Requires visa sponsorship"],
  ["open_to_relocation", "Open to relocation"],
];
const EEO_FIELDS: [string, string, string[]][] = [
  ["gender", "Gender", ["Male", "Female", "Non-binary"]],
  ["race_ethnicity", "Race / ethnicity", ["American Indian or Alaska Native", "Asian", "Black or African American", "Hispanic or Latino", "Native Hawaiian or Other Pacific Islander", "White", "Two or More Races"]],
  ["hispanic_latino", "Hispanic / Latino", ["Yes", "No"]],
  ["veteran_status", "Veteran status", ["I am not a protected veteran", "I identify as one or more of the classifications of a protected veteran"]],
  ["disability_status", "Disability status", ["Yes, I have a disability, or have had one in the past", "No, I do not have a disability and have not had one in the past"]],
];
const BOOL_KEYS = BOOL_FIELDS.map(([k]) => k);
// Every string-valued scalar (text inputs + EEO/remote selects) — everything except the bools.
const STRING_KEYS = [
  ...CONTACT.map(([k]) => k), "work_authorization",
  "desired_salary", "salary_currency", "remote_preference", "preferred_locations", "earliest_start_date", "notice_period",
  ...EEO_FIELDS.map(([k]) => k),
];

// ── Form state (all scalars held as strings; bools are "" | "true" | "false") ──
const s = reactive<Record<string, string>>(Object.fromEntries([...STRING_KEYS, ...BOOL_KEYS].map((k) => [k, ""])));
const education = ref<EduRow[]>([]);
const experience = ref<ExpRow[]>([]);
const overwrite = ref(false);
const status = ref("");
const statusOk = ref(false);
const busy = ref(false);
const statusClass = computed(() => (status.value && statusOk.value ? "ok" : ""));

interface EduRow { school: string; degree: string; field_of_study: string; gpa: string; location: string; description: string; start_date: string; end_date: string; }
interface ExpRow { company: string; title: string; location: string; description: string; start_date: string; end_date: string; is_current: boolean; }
const str = (v: unknown): string => (v == null ? "" : String(v));
const blankEdu = (): EduRow => ({ school: "", degree: "", field_of_study: "", gpa: "", location: "", description: "", start_date: "", end_date: "" });
const blankExp = (): ExpRow => ({ company: "", title: "", location: "", description: "", start_date: "", end_date: "", is_current: false });
const toEdu = (e: Partial<ProfileEducation>): EduRow => ({ school: str(e.school), degree: str(e.degree), field_of_study: str(e.field_of_study), gpa: str(e.gpa), location: str(e.location), description: str(e.description), start_date: str(e.start_date), end_date: str(e.end_date) });
const toExp = (x: Partial<ProfileExperience>): ExpRow => ({ company: str(x.company), title: str(x.title), location: str(x.location), description: str(x.description), start_date: str(x.start_date), end_date: str(x.end_date), is_current: !!x.is_current });

function fill(p: ApplicantProfileOut): void {
  const rec = p as unknown as Record<string, unknown>;
  STRING_KEYS.forEach((k) => { s[k] = str(rec[k]); });
  BOOL_KEYS.forEach((k) => { s[k] = rec[k] === true ? "true" : rec[k] === false ? "false" : ""; });
  education.value = p.education?.length ? p.education.map(toEdu) : [blankEdu()];
  experience.value = p.experience?.length ? p.experience.map(toExp) : [blankExp()];
}

function gather(): ApplicantProfileOut {
  const t = (v: string): string | null => v.trim() || null;
  const body: Record<string, unknown> = {};
  STRING_KEYS.forEach((k) => { body[k] = t(s[k]); });
  BOOL_KEYS.forEach((k) => { body[k] = s[k] === "" ? null : s[k] === "true"; });
  body.education = education.value.map((e) => ({
    school: t(e.school), degree: t(e.degree), field_of_study: t(e.field_of_study), gpa: t(e.gpa),
    location: t(e.location), description: t(e.description), start_date: e.start_date || null, end_date: e.end_date || null,
  }));
  body.experience = experience.value.map((x) => ({
    company: t(x.company), title: t(x.title), location: t(x.location), description: t(x.description),
    start_date: x.start_date || null, end_date: x.is_current ? null : x.end_date || null, is_current: x.is_current,
  }));
  return body as unknown as ApplicantProfileOut;
}

function addEdu(): void { education.value.push(blankEdu()); }
function addExp(): void { experience.value.push(blankExp()); }
function onCurrent(x: ExpRow): void { if (x.is_current) x.end_date = ""; }

async function onSave(): Promise<void> {
  busy.value = true;
  status.value = "Saving…";
  statusOk.value = false;
  try {
    fill(await store.save(gather()));
    status.value = "Saved.";
    statusOk.value = true;
  } catch (e) {
    status.value = e instanceof ApiError ? e.message : "Could not save the profile";
  } finally {
    busy.value = false;
  }
}

// ── Import-from-resume merge (mirrors the classic applyImport) ──
const isEmpty = (v: unknown): boolean => v == null || v === false || String(v).trim() === "";
const asRec = (o: object): Record<string, unknown> => o as Record<string, unknown>;
const rowIsBlank = (o: object): boolean => !Object.keys(asRec(o)).some((k) => k !== "is_current" && !isEmpty(asRec(o)[k]));
function mergeListAdd<T extends object>(current: T[], imported: T[], keyField: string): T[] {
  const out = current.map((r) => ({ ...r }));
  const used = out.map(() => false);
  const norm = (v: unknown) => (v == null ? "" : String(v).trim().toLowerCase());
  imported.forEach((imp) => {
    const key = norm(asRec(imp)[keyField]);
    const idx = key ? out.findIndex((r, i) => !used[i] && norm(asRec(r)[keyField]) === key) : -1;
    if (idx >= 0) {
      used[idx] = true;
      Object.keys(asRec(imp)).forEach((f) => { if (isEmpty(asRec(out[idx])[f]) && !isEmpty(asRec(imp)[f])) asRec(out[idx])[f] = asRec(imp)[f]; });
    } else {
      out.push(imp);
    }
  });
  return out;
}

function applyImport(draft: ApplicantProfileOut, mode: "add" | "replace"): void {
  const rec = draft as unknown as Record<string, unknown>;
  STRING_KEYS.forEach((k) => {
    const dv = rec[k];
    if (dv == null || String(dv).trim() === "") return;
    if (mode === "add" && s[k].trim() !== "") return;
    s[k] = String(dv);
  });
  if (draft.education?.length) {
    const imp = draft.education.map(toEdu);
    education.value = mode === "replace" ? imp : mergeListAdd(education.value.filter((r) => !rowIsBlank(r)), imp, "school");
  }
  if (draft.experience?.length) {
    const imp = draft.experience.map(toExp);
    experience.value = mode === "replace" ? imp : mergeListAdd(experience.value.filter((r) => !rowIsBlank(r)), imp, "company");
  }
}

async function onImport(): Promise<void> {
  const mode = overwrite.value ? "replace" : "add";
  const msg = overwrite.value
    ? "Overwrite fields that already have a value with information from your resume?"
    : "Fill in the empty fields from your resume (existing values are kept)?";
  if (!window.confirm(`${msg} You can review and edit before saving.`)) return;
  busy.value = true;
  status.value = "Reading your resume…";
  statusOk.value = false;
  try {
    applyImport(await store.importFromResume(), mode);
    status.value = (overwrite.value ? "Imported (overwrote filled fields)" : "Imported into empty fields") + " — review the fields, then Save profile.";
    statusOk.value = true;
  } catch (e) {
    status.value = e instanceof ApiError ? e.message : "Could not import from your resume";
  } finally {
    busy.value = false;
  }
}

onMounted(async () => fill(await store.load()));
</script>

<style scoped>
.surface {
  background: var(--surface); border: 1px solid var(--line); border-radius: 16px;
  padding: 20px; box-shadow: var(--shadow-xs); margin-bottom: 16px;
}
.bar { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 12px; }
.bar-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 12px; }
.overwrite { display: inline-flex; align-items: center; gap: 6px; font-size: 13px; white-space: nowrap; }
.overwrite input { width: auto; }
.grid2 { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 12px; }
.field { display: grid; gap: 6px; min-width: 0; }
.field > label { font-size: 13px; color: var(--muted); }
.field.current { align-content: end; }
.check { display: inline-flex; align-items: center; gap: 6px; font-size: 14px; color: var(--ink); }
.check input { width: auto; }
.editor { border: 1px solid var(--line); border-radius: 10px; padding: 14px; margin-top: 12px; display: grid; gap: 10px; }
.status { font-size: 13px; color: var(--muted); }
.status.ok { color: var(--text-success); }
@media (max-width: 620px) { .grid2 { grid-template-columns: 1fr; } }
</style>
