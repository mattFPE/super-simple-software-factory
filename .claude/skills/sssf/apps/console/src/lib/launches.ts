import { computed, shallowRef } from 'vue'
import type { AdwCatalog, Launch } from './types'

/** The repo's ADWs, read by the Launch pane and shared with each card's Continue menu. */
export const catalog = shallowRef<AdwCatalog | null>(null)

/** The ADWs that continue an earlier Run: offered on a Run's card, never as a fresh Launch. */
export const resumingAdws = computed(() =>
  catalog.value?.read_only ? [] : (catalog.value?.adws ?? []).filter((a) => a.description?.resumes),
)

/** The Run the Launch form is continuing, and the Resuming ADW picked for it; null for a fresh Launch. */
export const continuing = shallowRef<{ adwId: string; adw: string } | null>(null)

/**
 * The Launches this server started, shared by the Launch pane (which adds one
 * the moment it starts) and the Runs pane (which polls them alongside the
 * sessions). A started Launch is drawn by its session card, so only the
 * Starting and Refused ones are shown as Launches.
 */
export const launches = shallowRef<Launch[]>([])

export function addLaunch(launch: Launch): void {
  launches.value = [launch, ...launches.value.filter((l) => l.adw_id !== launch.adw_id)]
}
