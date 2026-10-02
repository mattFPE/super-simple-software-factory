import { shallowRef } from 'vue'
import type { Launch } from './types'

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
