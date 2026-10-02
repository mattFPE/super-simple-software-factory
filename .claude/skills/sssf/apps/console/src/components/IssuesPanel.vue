<script setup lang="ts">
import { computed, onMounted, ref } from 'vue'
import type { IssueLink, IssueRerun, ReadyIssue } from '../lib/types'
import { issues, issuesError, issuesLoading, loadIssuesOnce, refreshIssues } from '../lib/issues'
import { launches } from '../lib/launches'
import { hrefFor } from '../lib/router'

defineProps<{ disabled: boolean }>()
/** Pick launches it afresh; a Rerun follows what its failed Run's outcome comment says to type. */
const emit = defineEmits<{ pick: [issue: ReadyIssue, rerun?: IssueRerun] }>()

onMounted(loadIssuesOnce)

const flashed = ref<number | null>(null)

/** Listed issue numbers, so a link to one jumps within the list instead of leaving for GitHub. */
const listed = computed(() => new Set(issues.value?.issues.map((i) => i.number) ?? []))

const runnable = (issue: ReadyIssue): boolean => issue.verdict === 'runnable'

/**
 * The Launch holding this issue until its Claim lands. The Launches are
 * polled with the Runs, so this stays live without asking GitHub again.
 */
function heldBy(issue: ReadyIssue): string | null {
  const live = launches.value.find((l) => l.adw_id === issue.held_by || l.issue === issue.number)
  return live ? (live.holds_issue ? live.adw_id : null) : issue.held_by
}

/** A Spec's Runnable Tickets, or all of them while none is. */
const ticketsOf = (issue: ReadyIssue): IssueLink[] => (issue.run_instead.length ? issue.run_instead : issue.tickets)

function jump(link: IssueLink, event: MouseEvent): void {
  if (!listed.value.has(link.number)) return   // not in the list: the link goes to GitHub
  event.preventDefault()
  document.getElementById(`ready-issue-${link.number}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  flashed.value = link.number
  setTimeout(() => {
    if (flashed.value === link.number) flashed.value = null
  }, 1600)
}
</script>

<template>
  <section class="issues">
    <div class="head">
      <span class="label">Ready issues</span>
      <span v-if="issues?.repo" class="repo">{{ issues.repo }}</span>
      <button type="button" class="refresh" :disabled="issuesLoading" @click="refreshIssues">
        {{ issuesLoading ? 'reading…' : 'Refresh' }}
      </button>
    </div>

    <div v-if="issuesError" class="error-text">couldn't read the issues — {{ issuesError }}</div>
    <div v-else-if="!issues" class="dim">reading this repo's Ready issues…</div>
    <div v-else-if="!issues.available" class="unavailable">
      Issues aren't available: {{ issues.unavailable?.reason }}. {{ issues.unavailable?.fix }}
    </div>
    <div v-else-if="!issues.issues.length" class="dim">
      No open issues labelled <code>{{ issues.ready_label }}</code>.
    </div>

    <ul v-else class="rows">
      <li
        v-for="issue in issues.issues"
        :id="`ready-issue-${issue.number}`"
        :key="issue.number"
        class="row"
        :class="{ greyed: !runnable(issue) && !issue.rerun, flashed: flashed === issue.number }"
      >
        <div class="line">
          <a class="number" :href="issue.url" target="_blank" rel="noopener">#{{ issue.number }}</a>
          <span class="title">{{ issue.title }}</span>
          <button
            v-if="issue.rerun"
            type="button"
            class="pick"
            :disabled="disabled || heldBy(issue) !== null"
            :title="issue.rerun.force ? 'Start afresh despite the open PR its failed Run left' : 'Pick its failed Run’s kept worktree back up'"
            @click="emit('pick', issue, issue.rerun)"
          >
            {{ issue.rerun.force ? 'Rerun with --force' : 'Rerun' }}
          </button>
          <button
            v-if="runnable(issue)"
            type="button"
            :class="{ pick: !issue.rerun }"
            :disabled="disabled || heldBy(issue) !== null"
            @click="emit('pick', issue)"
          >
            {{ issue.rerun ? 'Fresh Launch' : 'Pick' }}
          </button>
        </div>

        <div v-if="heldBy(issue)" class="reason">
          Launch <a :href="hrefFor(heldBy(issue))">{{ heldBy(issue) }}</a> is Starting on it — wait for its Claim
        </div>

        <div v-else-if="issue.verdict === 'blocked'" class="reason" :title="issue.why ?? ''">
          blocked by
          <template v-for="(b, n) in issue.blocked_by" :key="b.number">
            <a :href="b.url" target="_blank" rel="noopener" @click="jump(b, $event)">#{{ b.number }}</a>{{ n < issue.blocked_by.length - 1 ? ', ' : '' }}
          </template>
        </div>

        <div v-else-if="issue.verdict === 'spec'" class="reason" :title="issue.why ?? ''">
          a Spec —
          {{ issue.run_instead.length ? 'run its Tickets instead:' : 'none of its Tickets is Runnable yet:' }}
          <template v-for="(t, n) in ticketsOf(issue)" :key="t.number">
            <a :href="t.url" target="_blank" rel="noopener" @click="jump(t, $event)">#{{ t.number }}</a>{{ n < ticketsOf(issue).length - 1 ? ', ' : '' }}
          </template>
        </div>

        <div v-else-if="issue.verdict === 'claimed'" class="reason" :title="issue.why ?? ''">
          <template v-if="issue.run">
            claimed by Run <a :href="hrefFor(issue.run.adw_id)">{{ issue.run.adw_id }}</a>{{ ' ' }}
            <span v-if="issue.run.status" class="dim">({{ issue.run.status }})</span>
          </template>
          <template v-else>claimed by a Run this trace doesn't have</template>
        </div>

        <div v-else-if="issue.verdict === 'open_pr'" class="reason" :title="issue.why ?? ''">
          <template v-if="issue.rerun && issue.run">
            Run <a :href="hrefFor(issue.run.adw_id)">{{ issue.run.adw_id }}</a> failed, leaving an open PR — fix it, or
            rerun afresh:
          </template>
          <template v-else>an open PR already closes it:</template>
          <template v-for="(pr, n) in issue.prs" :key="pr">
            <a :href="pr" target="_blank" rel="noopener">{{ pr.replace(/^.*\/pull\//, 'PR #') }}</a>{{ n < issue.prs.length - 1 ? ', ' : '' }}
          </template>
        </div>

        <div v-else-if="!runnable(issue)" class="reason">{{ issue.why }}</div>

        <div v-else-if="issue.run" class="reason last-run">
          last Run <a :href="hrefFor(issue.run.adw_id)">{{ issue.run.adw_id }}</a>{{ ' ' }}
          <span class="dim">({{ issue.run.status }})</span>
          <template v-if="issue.rerun">— its worktree is kept, and Rerun picks it back up</template>
        </div>
      </li>
    </ul>

    <div v-if="issues?.available && issues.truncated" class="dim">
      Only the first {{ issues.issues.length }} Ready issues are listed: there are more on the Tracker.
    </div>
  </section>
</template>

<style scoped>
.issues {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 16px 20px;
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
  min-width: 0;
}

.head {
  display: flex;
  align-items: center;
  gap: 10px;
}

.label {
  color: var(--dim);
}

.repo {
  flex: 1;
  min-width: 0;
  font-family: var(--mono);
  color: var(--faint);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.refresh {
  margin-left: auto;
}

.rows {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 0;
  padding: 0;
  list-style: none;
}

.row {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  border-radius: 10px;
  transition: background 0.3s;
}

.row.greyed .line {
  opacity: 0.5;
}

.row.flashed {
  background: rgba(200, 155, 255, 0.14);
}

.line {
  display: flex;
  align-items: baseline;
  gap: 8px;
  min-width: 0;
}

.number {
  font-family: var(--mono);
  color: var(--cyan);
}

.title {
  flex: 1;
  min-width: 0;
  overflow-wrap: anywhere;
}

.reason {
  color: var(--faint);
  overflow-wrap: anywhere;
}

.reason a {
  color: var(--dim);
  font-family: var(--mono);
}

.unavailable {
  color: var(--amber);
  overflow-wrap: anywhere;
}

.dim {
  color: var(--faint);
}

button {
  padding: 4px 12px;
  border: 1px solid var(--border);
  border-radius: 8px;
  background: transparent;
  color: var(--text);
  font: inherit;
  cursor: pointer;
}

button.pick {
  border-color: rgba(200, 155, 255, 0.6);
}

button:disabled {
  opacity: 0.55;
  cursor: default;
}

.error-text {
  color: var(--red);
  overflow-wrap: anywhere;
}
</style>
