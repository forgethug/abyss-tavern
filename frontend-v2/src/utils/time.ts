function parseUTC(timestamp: number | string | Date): Date {
  if (timestamp instanceof Date) return timestamp
  const value = String(timestamp)
  return new Date(
    /[Zz]$|[+-]\d{2}:\d{2}$/.test(value) ? value : `${value}Z`,
  )
}

export function formatTime(timestamp: number | string | Date): string {
  return parseUTC(timestamp).toLocaleTimeString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDateTime(timestamp: number | string | Date): string {
  return parseUTC(timestamp).toLocaleString('zh-CN', {
    timeZone: 'Asia/Shanghai',
    hour12: false,
  })
}
