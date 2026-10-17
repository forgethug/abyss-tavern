/** datetime-local 一律解释为香港时间，不依赖管理者浏览器或服务器时区。 */
export function hongKongInput(instant?: string): string {
  if (!instant) return ''
  const value = new Date(instant)
  if (!Number.isFinite(value.getTime())) return ''
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Asia/Hong_Kong', year: 'numeric', month: '2-digit', day: '2-digit',
    hour: '2-digit', minute: '2-digit', second: '2-digit', hourCycle: 'h23',
  }).formatToParts(value)
  const get = (type: string) => parts.find(part => part.type === type)?.value ?? ''
  return `${get('year')}-${get('month')}-${get('day')}T${get('hour')}:${get('minute')}:${get('second')}`
}

export function hongKongInstant(local: string): string {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$/.test(local)) throw new Error('请填写完整的香港时间')
  const value = new Date(`${local}+08:00`)
  if (!Number.isFinite(value.getTime())) throw new Error('时间无效')
  const result = value.toISOString()
  if (hongKongInput(result).slice(0, local.length) !== local) throw new Error('日期或时间无效')
  return result
}
