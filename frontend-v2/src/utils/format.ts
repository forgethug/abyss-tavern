export function formatMoney(value: number): string {
  return Number(value || 0).toLocaleString('zh-CN', {
    minimumFractionDigits: 0,
    maximumFractionDigits: 2,
  })
}

export function renderTitle(title: string | null | undefined): string {
  return title ? `<span class="user-title">[${title}]</span>` : ''
}
