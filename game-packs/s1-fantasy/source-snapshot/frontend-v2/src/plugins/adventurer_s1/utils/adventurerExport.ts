import type { AdventurerData, ItemInstance } from '../stores/adventurerS1'
import { itemTypeLabel, personalityText, rarityLabel, statusLabel } from '../utils'

export function adventurerTextExport(
  adventurer: AdventurerData,
  skills: NonNullable<AdventurerData['skills']>,
  exportedAt = new Date(),
): string {
  const lines = [
    `${adventurer.name} · 冒险者资料`,
    `导出时间：${exportedAt.toLocaleString('zh-CN')}`,
    '以下为导出时的资料快照。',
    '',
    '【属性与状态】',
    `姓名：${adventurer.name}`,
    `等级：${adventurer.level}`,
    `职业：${adventurer.character_class || '未定'}`,
    `性别：${adventurer.gender}`,
    `性格：${personalityText(adventurer.personality) || '暂无'}`,
    `状态：${statusLabel(adventurer.status, adventurer.building_instance_status, adventurer.building_handler_key, adventurer.tavern_seated)}`,
    `生命：${adventurer.hp} / ${adventurer.max_hp}`,
    `精力：${adventurer.stamina} / ${adventurer.max_stamina}`,
    `力量：${adventurer.strength}　敏捷：${adventurer.agility}　智力：${adventurer.intelligence}　幸运：${adventurer.luck}`,
    `金币：${adventurer.gold}`,
    `低消金：${adventurer.low_consumption_gold ?? 0}`,
    `当前称号：${adventurer.active_title?.name || '未展示'}`,
    `仓库容量：${adventurer.warehouse?.length ?? 0} / ${adventurer.warehouse_capacity_limit ?? '—'}`,
    `最近表现评分：${adventurer.latest_score ?? '暂无'}`,
    `队友点赞：${adventurer.praise_count ?? 0}`,
    `精力恢复速度：${adventurer.stamina_recovery_per_minute ?? adventurer.dorm_stamina_recovery_per_minute ?? '—'} / 分钟`,
  ]
  if (adventurer.training_count != null) lines.push(`修炼次数：${adventurer.training_count} / ${adventurer.training_cap ?? '—'}`)
  if (adventurer.party_preference) lines.push(`组队偏好：${adventurer.party_preference === 'auto' ? '自动匹配' : '手动匹配'}`)
  const section = (title: string, entries: string[]) => {
    lines.push('', `【${title}】`, ...(entries.length ? entries : ['暂无']))
  }
  section('人物特质', adventurer.traits.map(trait =>
    `${trait.name}（${trait.equipped === false ? '未携带' : '已携带'}${trait.is_pure_positive ? '，可卸下' : '，当前不可卸下'}）\n${trait.desc || '暂无描述'}`))
  section('掌握技能', skills.map(skill =>
    `${skill.name}（${skill.equipped ? '已携带' : '未携带'}${skill.recovering ? '，恢复中' : ''}）\n${skill.description || '暂无描述'}`))
  const itemText = (item: ItemInstance) => [
    `${item.name} ×${item.quantity ?? 1}（${rarityLabel(item.rarity)}${item.type ? `，${itemTypeLabel(item.type)}` : ''}）`,
    item.desc || '暂无描述',
    ...(item.uses_left != null ? [`剩余使用次数：${item.uses_left}`] : []),
    ...(item.damage_note ? [`受损状况：${item.damage_note}`] : []),
    ...(item.recovering ? ['正在恢复'] : []),
  ].join('\n')
  section('携带物品', adventurer.equipment.map(itemText))
  section('仓库道具', (adventurer.warehouse ?? []).map(itemText))
  section('建筑材料', (adventurer.materials ?? []).map(material =>
    `${material.display_name || material.name} ×${material.quantity}`))
  return lines.join('\n\n') + '\n'
}

export function downloadAdventurerText(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob(['\uFEFF', text], { type: 'text/plain;charset=utf-8' }))
  const link = document.createElement('a')
  link.href = url
  link.download = `${name.replace(/[\\/:*?"<>|\x00-\x1f]/g, '_') || '冒险者'}-资料.txt`
  document.body.appendChild(link)
  link.click()
  link.remove()
  setTimeout(() => URL.revokeObjectURL(url), 0)
}
