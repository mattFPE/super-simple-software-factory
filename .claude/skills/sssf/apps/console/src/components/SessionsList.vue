<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref, shallowRef } from 'vue'
import type { SessionSummary } from '../lib/types'
import { fetchLaunches, fetchSessions } from '../lib/api'
import { launches, syncLaunches } from '../lib/launches'
import { ts } from '../lib/format'
import LaunchCard from './LaunchCard.vue'
import SessionCard from './SessionCard.vue'

const sessions = shallowRef<SessionSummary[]>([])
const apiError = ref<string | null>(null)
const loaded = ref(false)
const nowMs = ref(Date.now())

let timer: ReturnType<typeof setInterval> | undefined
let inflight = false

async function tick() {
  if (inflight) return
  inflight = true
  try {
    const [rows, launched] = await Promise.all([fetchSessions(), fetchLaunches()])
    sessions.value = rows
    syncLaunches(launched)
    nowMs.value = Date.now()
    apiError.value = null
    loaded.value = true
  } catch (err) {
    apiError.value = err instanceof Error ? err.message : String(err)
  } finally {
    inflight = false
  }
}

onMounted(() => {
  void tick()
  timer = setInterval(() => void tick(), 500)
})

onUnmounted(() => clearInterval(timer))

/** Optimistic removal; an empty id means the write failed, so re-sync instead. */
function onArchived(adwId: string) {
  if (!adwId) {
    void tick()
    return
  }
  sessions.value = sessions.value.filter((s) => s.adw_id !== adwId)
}

/** In-flight Launches go on top; a started one is drawn by its session card instead. */
const pending = computed(() => launches.value.filter((l) => l.state !== 'started'))

const ordered = computed(() =>
  sessions.value.toSorted((a, b) => (ts(b.started_at) || 0) - (ts(a.started_at) || 0)),
)
</script>

<template>
  <div class="sessions">
    <div v-if="apiError" class="error-bar">api unreachable — retrying {{ apiError }}</div>

    <div class="list-head">
      <h2 class="pane-title">Runs</h2>
      <span v-if="ordered.length" class="dim">{{ ordered.length }} runs</span>
    </div>

    <div v-if="pending.length" class="cards">
      <LaunchCard v-for="l in pending" :key="l.adw_id" :launch="l" />
    </div>

    <div v-if="ordered.length" class="cards">
      <SessionCard
        v-for="s in ordered"
        :key="s.adw_id"
        :session="s"
        :now-ms="nowMs"
        @archived="onArchived"
      />
    </div>
    <div v-else-if="loaded && !pending.length" class="empty-state">no Runs yet — launch one, or run an ADW in a terminal, to see it here</div>
    <div v-else-if="!loaded && !apiError" class="empty-state">loading Runs…</div>
  </div>
</template>

<style scoped>
.sessions {
  display: flex;
  flex-direction: column;
}

.list-head {
  display: flex;
  align-items: baseline;
  gap: 14px;
  padding: 16px 24px 0;
  font-size: 16px;
}

.pane-title {
  margin: 0;
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.04em;
  color: var(--dim);
}

.cards {
  /* Uniform grid: every card the same width and (fixed in SessionCard) height,
     independent of content. */
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(460px, 1fr));
  gap: 18px;
  padding: 16px 24px 28px;
}





</style>
