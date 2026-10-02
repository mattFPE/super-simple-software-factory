<script setup lang="ts">
import { ref } from 'vue'
import type { Launch } from '../lib/types'
import { dismissLaunch, fetchLaunchLog } from '../lib/api'
import { removeLaunch } from '../lib/launches'
import { messageOf } from '../lib/format'
import ShellCommand from './ShellCommand.vue'

const props = defineProps<{ launch: Launch }>()

const log = ref<string | null>(null)
const logError = ref<string | null>(null)
const dismissError = ref<string | null>(null)
const dismissing = ref(false)

async function dismiss(): Promise<void> {
  dismissing.value = true
  dismissError.value = null
  try {
    await dismissLaunch(props.launch.adw_id)
    removeLaunch(props.launch.adw_id)
  } catch (err) {
    dismissError.value = messageOf(err)
  } finally {
    dismissing.value = false
  }
}

async function toggleLog(): Promise<void> {
  if (log.value !== null) {
    log.value = null
    return
  }
  try {
    log.value = (await fetchLaunchLog(props.launch.adw_id)) || '(nothing printed yet)'
    logError.value = null
  } catch (err) {
    logError.value = messageOf(err)
  }
}
</script>

<template>
  <article class="launch-card" :class="launch.state">
    <div class="head">
      <span class="card-id">{{ launch.adw_id }}</span>
      <span class="state">{{ launch.state === 'refused' ? 'Refused' : 'Starting' }}</span>
    </div>
    <span class="card-adw">{{ launch.adw }}</span>
    <ShellCommand :commands="launch.commands" :shell="launch.shell" clamp />
    <template v-if="launch.state === 'refused'">
      <span class="dim">
        {{ launch.adw }} exited{{ launch.exit_code === null ? '' : ` with ${launch.exit_code}` }} before
        its Run began:
      </span>
      <pre class="tail">{{ launch.log_tail || '(it printed nothing)' }}</pre>
    </template>
    <span v-else class="dim">waiting for the Run to appear in the trace…</span>
    <div class="actions">
      <button type="button" @click="toggleLog">{{ log === null ? 'Show full log' : 'Hide log' }}</button>
      <button v-if="launch.state === 'refused'" type="button" :disabled="dismissing" @click="dismiss">
        Dismiss
      </button>
    </div>
    <div v-if="logError" class="error-text">{{ logError }}</div>
    <div v-if="dismissError" class="error-text">{{ dismissError }}</div>
    <pre v-if="log !== null" class="log">{{ log }}</pre>
  </article>
</template>

<style scoped>
.launch-card {
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 20px 22px;
  border: 1px solid var(--border-soft);
  border-radius: 16px;
  background: var(--surface);
  min-width: 0;
}

.launch-card.starting {
  border-color: rgba(108, 182, 255, 0.6);
  border-style: dashed;
}

.launch-card.refused {
  border-color: rgba(255, 111, 103, 0.6);
}

.head {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 10px;
}

.card-id {
  font-family: var(--mono);
  font-size: 18px;
  font-weight: 700;
  color: var(--purple);
}

.card-adw {
  font-family: var(--mono);
  color: var(--cyan);
}

.state {
  padding: 3px 13px;
  border-radius: 999px;
  border: 1px solid var(--border);
  white-space: nowrap;
}

.starting .state {
  color: var(--blue);
  border-color: rgba(108, 182, 255, 0.45);
}

.refused .state {
  color: var(--red);
  border-color: rgba(255, 111, 103, 0.45);
}

.log {
  max-height: 420px;
  overflow-y: auto;
}

.actions {
  display: flex;
  gap: 8px;
}

button {
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: transparent;
  color: var(--text);
  font: inherit;
  cursor: pointer;
}

button:disabled {
  opacity: 0.5;
  cursor: default;
}

.error-text {
  color: var(--red);
}
</style>
