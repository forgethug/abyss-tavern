export interface TaskTemplateForBoard {
  id?: string
  can_accept?: boolean
  reject_reason?: string | null
  is_ended?: boolean
  max_completions?: number
  completion_remaining?: number
}

/** 任务栏列表展示：每日已完成任务保留到日界，其余永久完成/结束任务隐藏。 */
export function filterVisibleTaskTemplates<T extends TaskTemplateForBoard>(tasks: T[]): T[] {
  return tasks.filter(task => {
    if (task.reject_reason === '你已完成过该任务') return false
    if (task.is_ended) return false
    if (task.max_completions != null && (task.completion_remaining ?? 0) <= 0) return false
    return true
  })
}

/** 当前可接取任务 ID（有进行中任务时为空） */
export function listAcceptableTaskIds(
  tasks: TaskTemplateForBoard[],
  options?: { hasActiveTask?: boolean },
): string[] {
  if (options?.hasActiveTask) return []
  return filterVisibleTaskTemplates(tasks)
    .filter(task => task.can_accept !== false && task.id)
    .map(task => task.id!)
    .sort()
}

/** 当前可接取任务数（有进行中任务时为 0） */
export function countAcceptableTaskTemplates(
  tasks: TaskTemplateForBoard[],
  options?: { hasActiveTask?: boolean },
): number {
  return listAcceptableTaskIds(tasks, options).length
}
