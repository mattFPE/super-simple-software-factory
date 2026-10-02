<script setup lang="ts">
import { ref } from 'vue'
import type { LaunchPreview, Shell } from '../lib/types'

const props = defineProps<{
  commands: LaunchPreview['commands']
  /** The server's own platform's shell, shown first. */
  shell: Shell
  /** Scroll a long command inside a few lines, for a card. */
  clamp?: boolean
}>()

const LABELS: Record<Shell, string> = { powershell: 'PowerShell', posix: 'POSIX sh' }
const SHELLS: Shell[] = ['powershell', 'posix']

const shown = ref<Shell>(props.shell)
</script>

<template>
  <div class="shell-command">
    <div class="switch" role="tablist" aria-label="Shell to paste into">
      <button
        v-for="s in SHELLS"
        :key="s"
        type="button"
        role="tab"
        :aria-selected="shown === s"
        :class="{ on: shown === s }"
        @click="shown = s"
      >
        {{ LABELS[s] }}
      </button>
    </div>
    <pre v-if="commands[shown] !== null" class="command" :class="{ clamp }">{{ commands[shown] }}</pre>
    <span v-else class="note">
      PowerShell drops an argument that is exactly <code>--%</code>, so no PowerShell command runs this: paste the
      POSIX one into Git Bash or WSL.
    </span>
  </div>
</template>

<style scoped>
.shell-command {
  display: flex;
  flex-direction: column;
  gap: 6px;
  min-width: 0;
}

.switch {
  display: flex;
  gap: 4px;
}

.switch button {
  padding: 2px 10px;
  border: 1px solid var(--border-soft);
  border-radius: 999px;
  background: transparent;
  color: var(--faint);
  font: inherit;
  font-size: 14px;
  cursor: pointer;
}

.switch button.on {
  border-color: var(--border);
  color: var(--text);
}

.command {
  color: var(--text);
  white-space: pre-wrap;
}

.command.clamp {
  max-height: 7.5em;
  overflow-y: auto;
}

.note {
  color: var(--amber);
}
</style>
