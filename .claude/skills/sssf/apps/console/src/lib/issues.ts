import { shallowRef, ref, watch } from 'vue'
import type { IssueListing } from './types'
import { fetchIssues } from './api'
import { messageOf } from './format'
import { launches } from './launches'

/**
 * The repo's Ready issues, shared so the list survives a trip into a Run's
 * trace. It is read when the Console opens, on the refresh button, and once
 * after each Launch settles — once it stops holding its issue, because its
 * Claim landed or it was Refused. Each read asks GitHub, so it is never polled.
 */
export const issues = shallowRef<IssueListing | null>(null)
export const issuesLoading = ref(false)
export const issuesError = ref<string | null>(null)

let inflight: Promise<void> | null = null

export function refreshIssues(): Promise<void> {
  inflight ??= (async () => {
    issuesLoading.value = true
    try {
      issues.value = await fetchIssues()
      issuesError.value = null
    } catch (err) {
      issuesError.value = messageOf(err)
    } finally {
      issuesLoading.value = false
      inflight = null
    }
  })()
  return inflight
}

/** Read once, the first time the Launch pane opens; later opens keep the list it has. */
export function loadIssuesOnce(): void {
  if (!issues.value && !inflight) void refreshIssues()
}

let holding = new Set<string>()
watch(launches, (now) => {
  const settled = now.some((l) => holding.has(l.adw_id) && !l.holds_issue)
  holding = new Set(now.filter((l) => l.holds_issue).map((l) => l.adw_id))
  if (settled) void refreshIssues()
})
