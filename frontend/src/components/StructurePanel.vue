<script setup lang="ts">
import { onMounted, ref, watch } from 'vue'
import { api } from '../api'
import type { NovelInfo, SectionPage } from '../types'

const props = defineProps<{ novel: NovelInfo }>()
const page = ref<SectionPage | null>(null)
const loading = ref(false)
const error = ref('')
const pageSize = 50

async function load(offset = 0) {
  loading.value = true
  error.value = ''
  try {
    page.value = await api.sections(props.novel.novel_id, offset, pageSize)
  } catch (cause) {
    error.value = cause instanceof Error ? cause.message : '章节加载失败。'
  } finally {
    loading.value = false
  }
}

onMounted(() => load())
watch(() => props.novel.novel_id, () => load())
</script>

<template>
  <section class="workspace-panel">
    <div class="stats-grid">
      <div><span>结构策略</span><strong>{{ novel.structure.strategy }}</strong></div>
      <div><span>识别置信度</span><strong>{{ Math.round(novel.structure.confidence * 100) }}%</strong></div>
      <div><span>标题命中</span><strong>{{ novel.structure.heading_count }}</strong></div>
      <div><span>识别器</span><strong>{{ novel.structure.detectors_used.join(' / ') || '安全降级' }}</strong></div>
    </div>

    <div v-if="novel.structure.warnings.length" class="notice warning">
      <strong>结构提示</strong>
      <ul><li v-for="warning in novel.structure.warnings" :key="warning">{{ warning }}</li></ul>
    </div>

    <div class="section-heading">
      <div><p class="eyebrow">SECTION MAP</p><h2>章节结构</h2></div>
      <span v-if="page">{{ page.total }} 个 Section</span>
    </div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <div class="table-wrap" :aria-busy="loading">
      <table>
        <thead><tr><th>章节</th><th>行号</th><th>Chunk</th><th>置信度</th></tr></thead>
        <tbody>
          <tr v-for="section in page?.items" :key="section.section_id">
            <td><strong>{{ section.title || '未命名段落' }}</strong><small>{{ section.section_id }}</small></td>
            <td>Ln {{ section.start_line }}–{{ section.end_line }}</td>
            <td>{{ section.chunk_count }}</td>
            <td>{{ Math.round(section.confidence * 100) }}%</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="page && page.total > page.limit" class="pagination">
      <button class="button secondary" :disabled="page.offset === 0 || loading" @click="load(Math.max(0, page.offset - page.limit))">上一页</button>
      <span>{{ page.offset + 1 }}–{{ Math.min(page.offset + page.limit, page.total) }} / {{ page.total }}</span>
      <button class="button secondary" :disabled="page.offset + page.limit >= page.total || loading" @click="load(page.offset + page.limit)">下一页</button>
    </div>
  </section>
</template>
