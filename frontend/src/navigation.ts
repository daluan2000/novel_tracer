export type PageId = 'library' | 'agent' | 'structure' | 'search' | 'graph' | 'settings'
export type GraphSection = 'explore' | 'entities' | 'build' | 'history'
export interface WorkspaceRoute { page: PageId; graphSection: GraphSection }
export const graphSections: Array<{ id: GraphSection; label: string; hint: string }> = [
  { id: 'explore', label: '关系探索', hint: '从人物与关系回到原文' },
  { id: 'entities', label: '实体整理', hint: '合并名称，核对身份' },
  { id: 'build', label: '构建任务', hint: '构建、进度与诊断' },
  { id: 'history', label: '修改历史', hint: '查看及恢复图谱版本' },
]
export function parseRoute(hash: string): WorkspaceRoute {
  const [page, section] = hash.replace(/^#\/?/, '').split('/')
  return {
    page: ['library', 'agent', 'structure', 'search', 'graph', 'settings'].includes(page || '') ? page as PageId : 'agent',
    graphSection: graphSections.some(item => item.id === section) ? section as GraphSection : 'explore',
  }
}
export function routeHash(route: WorkspaceRoute) {
  return route.page === 'graph' ? `#/graph/${route.graphSection}` : `#/${route.page}`
}
