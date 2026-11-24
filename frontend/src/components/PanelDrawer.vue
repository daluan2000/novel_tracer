<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
const props = withDefaults(defineProps<{ open: boolean; title: string; kind?: 'drawer' | 'modal' | 'sheet' }>(), { kind: 'drawer' })
const emit = defineEmits<{ close: [] }>()
const dialog = ref<HTMLDialogElement | null>(null)
watch(() => props.open, async () => {
  await nextTick()
  const element = dialog.value
  if (!element) return
  const open = props.open
  if (open && !element.open) {
    if (typeof element.showModal === 'function') element.showModal()
    else element.setAttribute('open', '')
  } else if (!open && element.open) {
    if (typeof element.close === 'function') element.close()
    else element.removeAttribute('open')
  }
}, { immediate: true })
function backdrop(event: MouseEvent) {
  if (event.target !== dialog.value) return
  const rect = dialog.value!.getBoundingClientRect()
  if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) emit('close')
}
onBeforeUnmount(() => { if (dialog.value?.open && typeof dialog.value.close === 'function') dialog.value.close() })
</script>
<template>
  <dialog ref="dialog" :class="['panel-dialog', kind]" :aria-label="title" @cancel.prevent="emit('close')" @click="backdrop">
    <header class="pane-heading"><h2>{{ title }}</h2><button type="button" class="icon-button" :aria-label="`关闭${title}`" autofocus @click="emit('close')">×</button></header>
    <div class="dialog-content"><slot v-if="open" /></div>
  </dialog>
</template>
