<script setup lang="ts">
import { computed, onMounted, reactive, ref, shallowRef, watch } from 'vue'
import type { AdwOption, IssueRerun, LaunchPreview, LaunchRequest, ReadyIssue } from '../lib/types'
import { issueNamed } from '@shared/issues'
import { fetchAdws, previewLaunch, startLaunch } from '../lib/api'
import { addLaunch, catalog, continuing, resumingAdws } from '../lib/launches'
import { messageOf } from '../lib/format'
import IssuesPanel from './IssuesPanel.vue'

const LAST_ADW = 'sssf.console.last-adw'

const loadError = ref<string | null>(null)
const pane = ref<HTMLElement | null>(null)
const chosen = ref('')
// The form, split by kind so each input binds to a value of its own type.
const texts = reactive<Record<string, string>>({})
const flags = reactive<Record<string, boolean>>({})
const preview = shallowRef<LaunchPreview | null>(null)
const formError = ref<string | null>(null)
const busy = ref(false)

/**
 * Resuming ADWs continue an earlier Run, so they are never a fresh Launch:
 * the picker offers them only while the form is continuing a Run.
 */
const launchable = computed(() =>
  continuing.value
    ? resumingAdws.value
    : (catalog.value?.adws ?? []).filter((a) => !a.description?.resumes),
)
const adw = computed(() => launchable.value.find((a) => a.name === chosen.value) ?? null)

/** The prompt and any other positionals, then the options; the id is the Console's to mint. */
const positionals = computed(() => adw.value?.description?.options.filter((o) => o.flag === null) ?? [])
/** The positional an issue reference goes into: the ADW's prompt. */
const promptOption = computed(() => positionals.value[0] ?? null)

/** The issue the prompt names, when the chosen ADW won't claim or close it because it commits nothing. */
const readOnlyIssue = computed(() => {
  const issue = promptOption.value ? issueNamed(texts[promptOption.value.name]) : null
  return issue !== null && adw.value?.description?.commits === false ? issue : null
})

const options = computed(
  () => adw.value?.description?.options.filter((o) => o.flag !== null && o.name !== 'adw_id') ?? [],
)

/**
 * The issue Rerun the form holds, as its failed Run's outcome comment gives it:
 * that Run's ADW under its adw_id, or afresh with `--force`. Choosing another
 * ADW, or a fresh Launch, drops it.
 */
const rerunning = shallowRef<(IssueRerun & { issue: number }) | null>(null)

function remembered(): string | null {
  try {
    return localStorage.getItem(LAST_ADW)
  } catch {
    return null
  }
}

function remember(name: string): void {
  try {
    localStorage.setItem(LAST_ADW, name)
  } catch {
    /* a private window: nothing to remember with */
  }
}

/** The fresh-Launch ADW: the one last chosen, else the first that can describe itself. */
function freshChoice(): string {
  const names = launchable.value.filter((a) => a.description).map((a) => a.name)
  const last = remembered()
  return last && names.includes(last) ? last : (names[0] ?? '')
}

function clearForm(): void {
  for (const key of Object.keys(texts)) delete texts[key]
  for (const key of Object.keys(flags)) delete flags[key]
  preview.value = null
  formError.value = null
}

onMounted(async () => {
  try {
    catalog.value = await fetchAdws()
    chosen.value = continuing.value?.adw ?? freshChoice()
  } catch (err) {
    loadError.value = messageOf(err)
  }
})

/** The issue pickIssue is switching the ADW for: it goes into the new ADW's form once the form clears. */
let picked: { prompt: string; force: boolean } | null = null

/** Fill the form with a picked issue: its reference, and `--force` exactly when its Rerun says so. */
function fillPicked(pick: { prompt: string; force: boolean }): void {
  if (promptOption.value) texts[promptOption.value.name] = pick.prompt
  const force = options.value.find((o) => o.flag === '--force' && o.kind === 'flag')
  if (force) flags[force.name] = pick.force
}

watch(chosen, (name, before) => {
  // A picked issue stays the prompt when the engineer tries another ADW for it.
  const was = launchable.value.find((a) => a.name === before)?.description?.options.find((o) => o.flag === null)
  const carried = was && issueNamed(texts[was.name]) !== null ? texts[was.name]! : null
  clearForm()
  if (picked) fillPicked(picked)
  else if (carried !== null && promptOption.value) texts[promptOption.value.name] = carried
  // An issue's ADW was chosen for the issue, not by the engineer, so it isn't remembered;
  // nor is a Resuming ADW, picked per Run, ever the fresh-Launch default.
  if (name && picked === null && !continuing.value) remember(name)
  // A Rerun is its failed Run's own ADW: another one is a fresh Launch.
  if (picked === null) rerunning.value = null
  picked = null
})

/**
 * Picking an issue fills the prompt with its reference and selects the default
 * issue ADW. Its Rerun instead selects the ADW its failed Run ran, and either
 * reruns under that Run's adw_id or ticks `--force`.
 */
function pickIssue(issue: ReadyIssue, rerun?: IssueRerun): void {
  const pick = { prompt: `#${issue.number}`, force: rerun?.force ?? false }
  const target = rerun?.adw ?? catalog.value?.default_issue_adw
  rerunning.value = rerun ? { ...rerun, issue: issue.number } : null
  if (target && target !== chosen.value) {
    picked = pick
    chosen.value = target
  } else {
    preview.value = null
    formError.value = null
    fillPicked(pick)
  }
  pane.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

// A card's "Continue with…" picked a Run and an ADW: the form takes both, and comes into view.
watch(continuing, (next, before) => {
  if (next?.adwId === before?.adwId && next?.adw === before?.adw) return
  clearForm()
  rerunning.value = null
  chosen.value = next ? next.adw : freshChoice()
  if (next) pane.value?.scrollIntoView({ behavior: 'smooth', block: 'start' })
})

/** A landing flag unchecks the others in its group: at most one may be set. */
function toggle(option: AdwOption, on: boolean): void {
  flags[option.name] = on
  if (!on) return
  for (const group of adw.value?.description?.mutually_exclusive ?? []) {
    if (!group.includes(option.flag!)) continue
    for (const other of options.value) {
      if (other !== option && other.flag && group.includes(other.flag)) flags[other.name] = false
    }
  }
}

function placeholder(option: AdwOption): string {
  return option.default === null || option.default === undefined ? '' : `default: ${String(option.default)}`
}

function formValues(): Record<string, string | boolean> {
  return { ...texts, ...flags }
}

/** One request at a time; a refusal shows the server's own message under the form. */
async function submitting(work: () => Promise<void>): Promise<void> {
  busy.value = true
  formError.value = null
  try {
    await work()
  } catch (err) {
    formError.value = messageOf(err)
  } finally {
    busy.value = false
  }
}

function startFresh(): void {
  continuing.value = null
  rerunning.value = null
}

/** A fresh Launch is matched to its preview by the id it minted; a continuing one or a Rerun by its Run's. */
function request(adwId?: string): LaunchRequest {
  const req: LaunchRequest = { adw: adw.value!.name, values: formValues() }
  if (continuing.value) req.continues = continuing.value.adwId
  else if (rerunning.value?.adw_id) req.reruns = rerunning.value.adw_id
  else if (adwId) req.adw_id = adwId
  return req
}

function review(): Promise<void> {
  return submitting(async () => {
    if (!adw.value) return
    preview.value = await previewLaunch(request())
  })
}

function confirm(): Promise<void> {
  return submitting(async () => {
    if (!adw.value || !preview.value) return
    addLaunch(await startLaunch(request(preview.value.adw_id)))
    preview.value = null
    for (const p of positionals.value) texts[p.name] = ''
    continuing.value = null
    rerunning.value = null
  })
}

/** The form is locked while its command is up for review, so what launches is what was shown. */
const locked = computed(() => busy.value || preview.value !== null)
</script>

<template>
  <section ref="pane" class="launch">
    <h2 class="pane-title">{{ continuing ? 'Continue' : rerunning ? 'Rerun' : 'Launch' }}</h2>

    <div v-if="loadError" class="error-bar">couldn't read this repo's ADWs — {{ loadError }}</div>
    <div v-else-if="!catalog" class="empty-state">reading this repo's ADWs…</div>

    <div v-else-if="!launchable.length" class="empty-state">no ADWs in this repo's adws/ to launch</div>

    <div v-else-if="catalog.read_only" class="banner">
      This repo's ADWs predate <code>--describe</code>, so the Console can only watch Runs here.
      Run <code>just sssf-update</code> to launch from the Console.
    </div>

    <form v-else class="form" @submit.prevent="review">
      <div v-if="continuing" class="continuing">
        <span class="inline">
          <span class="flag">--adw-id</span>
          <span class="run-id">{{ continuing.adwId }}</span>
        </span>
        <span class="help">The Resuming ADW picks up this Run's work, under the same Run in the trace.</span>
        <div>
          <button type="button" :disabled="busy" @click="startFresh">Back to a fresh Launch</button>
        </div>
      </div>

      <div v-else-if="rerunning" class="continuing">
        <template v-if="rerunning.adw_id">
          <span class="inline">
            <span class="flag">--adw-id</span>
            <span class="run-id">{{ rerunning.adw_id }}</span>
          </span>
          <span class="help">
            Reruns #{{ rerunning.issue }} under its failed Run, picking that Run’s kept worktree back up.
          </span>
        </template>
        <span v-else class="help">
          Reruns #{{ rerunning.issue }} afresh with <span class="flag">--force</span>: its failed Run left
          <a :href="rerunning.pr ?? undefined" target="_blank" rel="noopener">{{ rerunning.pr }}</a> open. Close it,
          or fix it instead of rerunning.
        </span>
        <div>
          <button type="button" :disabled="busy" @click="startFresh">Back to a fresh Launch</button>
        </div>
      </div>

      <label class="field">
        <span class="label">ADW</span>
        <select v-model="chosen" :disabled="locked">
          <option v-for="a in launchable" :key="a.name" :value="a.name" :disabled="!a.description">
            {{ a.name }}{{ a.description ? '' : ' (can’t describe itself)' }}
          </option>
        </select>
      </label>

      <div v-if="adw" class="adw-about">
        <div class="summary">{{ adw.summary }}</div>
        <div v-if="adw.phases" class="phases">{{ adw.phases }}</div>
        <div v-if="adw.error" class="error-text">{{ adw.error }}</div>
      </div>

      <template v-if="adw?.description">
        <label v-for="p in positionals" :key="p.name" class="field">
          <span class="label">{{ p.name }}</span>
          <textarea
            v-model="texts[p.name]"
            rows="5"
            :placeholder="p.help ?? ''"
            :disabled="locked"
          />
        </label>

        <div v-for="o in options" :key="o.name" class="field">
          <label v-if="o.kind === 'flag'" class="check">
            <input
              type="checkbox"
              :checked="flags[o.name] === true"
              :disabled="locked"
              @change="toggle(o, ($event.target as HTMLInputElement).checked)"
            />
            <span class="flag">{{ o.flag }}</span>
          </label>
          <label v-else-if="o.kind === 'choice' && o.choices" class="inline">
            <span class="flag">{{ o.flag }}</span>
            <select v-model="texts[o.name]" :disabled="locked">
              <option value="">{{ placeholder(o) || '—' }}</option>
              <option v-for="c in o.choices" :key="c" :value="c">{{ c }}</option>
            </select>
          </label>
          <!-- "value" and any kind this Console doesn't know yet: a plain text field. -->
          <label v-else class="inline">
            <span class="flag">{{ o.flag }}</span>
            <input v-model="texts[o.name]" type="text" :placeholder="placeholder(o)" :disabled="locked" />
          </label>
          <span v-if="o.help" class="help">{{ o.help }}</span>
        </div>

        <div v-if="readOnlyIssue !== null" class="note">
          {{ adw.name }} commits nothing, so it won't claim #{{ readOnlyIssue }} or close it: the issue stays
          open and Ready.
        </div>

        <div v-if="formError" class="error-text">{{ formError }}</div>

        <div v-if="preview" class="confirm">
          <span class="label">This will run, from the repo root:</span>
          <pre class="command">{{ preview.command }}</pre>
          <div class="actions">
            <button type="button" class="primary" :disabled="busy" @click="confirm">Launch</button>
            <button type="button" :disabled="busy" @click="preview = null">Edit</button>
          </div>
        </div>
        <div v-else class="actions">
          <button type="submit" class="primary" :disabled="busy">Review command…</button>
        </div>
      </template>
    </form>

    <!-- A typed prompt launches whether or not the issues are available. -->
    <IssuesPanel v-if="catalog && !catalog.read_only && !continuing" :disabled="locked" @pick="pickIssue" />
  </section>
</template>

<style scoped>
.launch {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 16px 24px 28px;
  min-width: 0;
}

.pane-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--dim);
}

.banner {
  padding: 14px 16px;
  border: 1px solid rgba(232, 182, 74, 0.55);
  border-radius: 12px;
  background: rgba(232, 182, 74, 0.08);
  color: var(--amber);
}

.form {
  display: flex;
  flex-direction: column;
  gap: 14px;
  padding: 18px 20px;
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
}

.field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.label {
  color: var(--dim);
}

.inline,
.check {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.inline input,
.inline select {
  flex: 1;
  min-width: 0;
}

.flag {
  font-family: var(--mono);
  color: var(--cyan);
  white-space: nowrap;
}

.help {
  color: var(--faint);
}

.continuing {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding-bottom: 14px;
  border-bottom: 1px solid var(--border-soft);
}

.run-id {
  font-family: var(--mono);
  color: var(--text);
}

.adw-about {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.phases {
  font-family: var(--mono);
  color: var(--faint);
  overflow-wrap: anywhere;
}

select,
input[type='text'],
textarea {
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: var(--panel-3);
  color: var(--text);
  font: inherit;
}

textarea {
  resize: vertical;
  font-family: var(--mono);
}

input[type='checkbox'] {
  width: 18px;
  height: 18px;
  accent-color: var(--purple);
}

.confirm {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.command {
  color: var(--text);
}

.actions {
  display: flex;
  gap: 10px;
}

button {
  padding: 8px 16px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: transparent;
  color: var(--text);
  font: inherit;
  cursor: pointer;
}

button.primary {
  border-color: rgba(200, 155, 255, 0.6);
  background: rgba(200, 155, 255, 0.14);
}

button:disabled {
  opacity: 0.55;
  cursor: default;
}

.note {
  color: var(--amber);
  overflow-wrap: anywhere;
}

.error-text {
  color: var(--red);
  overflow-wrap: anywhere;
}
</style>
