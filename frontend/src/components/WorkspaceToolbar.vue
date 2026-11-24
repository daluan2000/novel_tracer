<script setup lang="ts">
defineProps<{ label?: string; count?: string | number }>()
</script>
<template>
  <div class="workspace-toolbar">
    <div class="toolbar-summary"><strong v-if="label">{{ label }}</strong><span v-if="count !== undefined" class="count-chip">{{ count }}</span><slot name="summary" /></div>
    <div class="toolbar-actions"><slot /></div>
  </div>
</template>
