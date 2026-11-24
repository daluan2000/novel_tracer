<script setup lang="ts">
import { onBeforeUnmount, ref } from 'vue'
import PanelDrawer from './PanelDrawer.vue'
defineProps<{ open: boolean; title: string }>()
const emit = defineEmits<{ close: [] }>()
const media = typeof window.matchMedia === 'function' ? window.matchMedia('(max-width: 900px)') : null
const compact = ref(media?.matches ?? false)
const update = () => { compact.value = media?.matches ?? false }
media?.addEventListener('change', update)
onBeforeUnmount(() => media?.removeEventListener('change', update))
</script>
<template>
  <PanelDrawer v-if="compact" :open="open" :title="title" kind="sheet" @close="emit('close')"><slot /></PanelDrawer>
  <aside v-else class="inspector-pane">
    <header class="pane-heading"><h2>{{ title }}</h2><button v-if="open" type="button" class="icon-button" :aria-label="`关闭${title}`" @click="emit('close')">×</button></header>
    <div class="inspector-content"><slot /></div>
  </aside>
</template>
