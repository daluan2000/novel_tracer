<script setup lang="ts">
defineProps<{ name: string }>()
const paths: Record<string, string> = {
  library: 'M4 4h5v16H4z M9 6h5v14H9z M16 4l4-1 3 16-4 1z',
  agent: 'M21 11.5a8.4 8.4 0 0 1-.9 3.8 8.5 8.5 0 0 1-7.6 4.7 8.4 8.4 0 0 1-3.8-.9L3 21l1.9-5.7a8.4 8.4 0 0 1-.9-3.8 8.5 8.5 0 0 1 4.7-7.6 8.4 8.4 0 0 1 3.8-.9h.5a8.5 8.5 0 0 1 8 8v.5Z M8 10h8 M8 14h5',
  structure: 'M8 4h13 M8 12h13 M8 20h13 M3 4h.01 M3 12h.01 M3 20h.01',
  search: 'M21 21l-5-5 M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0Z',
  graph: 'M9 5a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z M22 12a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z M11 20a3 3 0 1 1-6 0 3 3 0 0 1 6 0Z M9 6l7 4 M7 8l1 9 M11 19l6-5',
  settings: 'M12 8a4 4 0 1 1 0 8 4 4 0 0 1 0-8Z M12 2v3 M12 19v3 M2 12h3 M19 12h3 M5 5l2 2 M17 17l2 2 M5 19l2-2 M17 7l2-2',
  upload: 'M12 16V3 M7 8l5-5 5 5 M4 16v5h16v-5',
  arrow: 'M5 12h14 M14 7l5 5-5 5',
  book: 'M12 5C8 2 4 3 2 4v15c3-2 7-1 10 1 3-2 7-3 10-1V4c-2-1-6-2-10 1Z M12 5v15',
}
</script>

<template>
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.7"
    stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
    <path :d="paths[name] || paths.book" />
  </svg>
</template>
