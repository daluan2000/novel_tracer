<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import { api } from '../api'
import type { NovelInfo, SectionPage } from '../types'

const props = defineProps<{ novel: NovelInfo }>()
const page = ref<SectionPage | null>(null)
const loading = ref(false)
const error = ref('')
const pageSize = 20
let loadGeneration = 0
const strategyNames: Record<string, string> = {
  detected_headings: '按章节标题划分',
  mixed_headings: '兼容多种标题格式',
  fallback_chunks: '按正文段落划分',
}
const detectorNames: Record<string, string> = {
  di_unit: '“第…章/回”标题', numeric: '数字编号标题', chinese_numeric: '中文数字标题',
  english: '英文标题', special: '特殊篇章标题', plain: '普通文本标题',
}

async function load(offset = 0) {
  const generation = ++loadGeneration
  const novelId = props.novel.novel_id
  loading.value = true
  error.value = ''
  try {
    const loaded = await api.sections(novelId, offset, pageSize)
    if (generation === loadGeneration) page.value = loaded
  } catch (cause) {
    if (generation === loadGeneration) error.value = cause instanceof Error ? cause.message : '章节加载失败。'
  } finally {
    if (generation === loadGeneration) loading.value = false
  }
}

watch(() => props.novel.novel_id, () => { page.value = null; void load() }, { immediate: true })
onBeforeUnmount(() => { loadGeneration++ })
</script>

<template>
  <section class="workspace-panel structure-panel">
    <details class="stats-details">
      <summary>章节识别详情 · {{ novel.section_count }} 个章节，{{ novel.chunk_count }} 个文本片段</summary>
      <div class="stats-grid">
        <div>
          <span>章节划分方式</span><strong>{{ strategyNames[novel.structure.strategy] || novel.structure.strategy }}</strong>
        </div>
        <div><span>识别置信度</span><strong>{{ Math.round(novel.structure.confidence * 100) }}%</strong></div>
        <div><span>识别到的标题</span><strong>{{ novel.structure.heading_count }}</strong></div>
        <div>
          <span>标题类型</span><strong>{{ novel.structure.detectors_used.map((name) => detectorNames[name] || name).join(' / ') || '未识别到明确标题' }}</strong>
        </div>
      </div>

      <div v-if="novel.structure.warnings.length" class="notice warning">
        <strong>结构提示</strong>
        <ul>
          <li v-for="warning in novel.structure.warnings" :key="warning">{{ warning }}</li>
        </ul>
      </div>

    </details>
    <div class="section-heading">
      <div>
        <p class="eyebrow">全书目录</p>
        <h2>章节目录</h2>
      </div>
      <span v-if="page">{{ page.total }} 个章节</span>
    </div>
    <p v-if="error" class="form-error" role="alert">{{ error }}</p>
    <div class="table-wrap" :aria-busy="loading">
      <table>
        <thead>
          <tr>
            <th>章节</th>
            <th>原文位置</th>
            <th>文本片段</th>
            <th>识别置信度</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="section in page?.items" :key="section.section_id">
            <td><strong>{{ section.title || '未命名段落' }}</strong><small>{{ section.section_id }}</small></td>
            <td>行 {{ section.start_line }}–{{ section.end_line }}</td>
            <td>{{ section.chunk_count }}</td>
            <td>{{ Math.round(section.confidence * 100) }}%</td>
          </tr>
        </tbody>
      </table>
    </div>
    <div v-if="page && page.total > page.limit" class="pagination">
      <button class="button secondary" :disabled="page.offset === 0 || loading"
        @click="load(Math.max(0, page.offset - page.limit))">上一页</button>
      <span>{{ page.offset + 1 }}–{{ Math.min(page.offset + page.limit, page.total) }} / {{ page.total }}</span>
      <button class="button secondary" :disabled="page.offset + page.limit >= page.total || loading"
        @click="load(page.offset + page.limit)">下一页</button>
    </div>
  </section>
</template>
