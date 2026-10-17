import api from '@/composables/useApi'
import { isAxiosError } from 'axios'

export type TeamLootMode = 'auction' | 'roll'
export interface TeamLootLot {
  id: number
  expedition_log_id: number
  party_name: string | null
  target_dungeon: string
  name: string
  item: { type: string; desc: string; rarity: string; quantity: number }
  status: 'voting' | 'bidding' | 'rolling' | 'claim_pending' | 'settled' | 'unclaimed' | 'cancelled'
  mode: TeamLootMode | null
  deadline_at: string | null
  members: { name: string; codename: string; is_me: boolean; vote: TeamLootMode | null; has_bid: boolean }[]
  my_members: { adventurer_id: number; name: string; codename: string; vote: TeamLootMode | null; bid_amount: number; held_amount: number; can_bid: boolean; bid_block_reason: string | null }[]
  roll_rounds: { number: number; highest: number; tied: boolean; results: { name: string; value: number }[] }[]
  winner_name: string | null
  winning_amount: number
  auction_fee_rate: number
  can_claim: boolean
  delivery_hint: string | null
  receipt: { paid_amount: number; gross_amount?: number; fee_amount?: number; auction_fee_rate?: number; shares: { name: string; amount: number }[]; claimed_at: string } | null
}
export interface TeamLootPage {
  items: TeamLootLot[]
  page: number
  per_page: number
  total: number
  pages: number
  has_history: boolean
}
interface Envelope<T> { code: number; message: string; data: T }
export interface TeamLootSummary {
  voting: number
  bidding: number
  rolling: number
  claim_pending: number
}
const playerRequest = { s1RealmContext: 'membership' } as const

export async function fetchTeamLootSummary(): Promise<TeamLootSummary> {
  try {
    const { data } = await api.get<Envelope<TeamLootSummary>>('/adventurer-s1/team-loot/summary', playerRequest)
    if (data.code !== 200) throw new Error(data.message)
    return data.data
  } catch (error) { throw playerError(error) }
}

export async function fetchTeamLoot(page = 1, history = false): Promise<TeamLootPage> {
  try {
    const { data } = await api.get<Envelope<TeamLootPage>>('/adventurer-s1/team-loot', {
      ...playerRequest, params: { page, per_page: 20, history: history ? '1' : '0' },
    })
    if (data.code !== 200) throw new Error(data.message)
    return data.data
  } catch (error) { throw playerError(error) }
}

export async function voteTeamLoot(id: number, mode: TeamLootMode, adventurerId: number) {
  return mutate(id, 'vote', { mode, adventurer_id: adventurerId })
}
export async function bidTeamLoot(id: number, amount: string, adventurerId: number) {
  return mutate(id, 'bid', { amount, adventurer_id: adventurerId })
}
export async function claimTeamLoot(id: number) { return mutate(id, 'claim', {}) }

async function mutate(id: number, action: string, body: object): Promise<TeamLootLot> {
  try {
    const { data } = await api.post<Envelope<TeamLootLot>>(`/adventurer-s1/team-loot/${id}/${action}`, body, playerRequest)
    if (data.code !== 200) throw new Error(data.message)
    return data.data
  } catch (error) { throw playerError(error) }
}

function playerError(error: unknown): Error {
  if (isAxiosError<Envelope<unknown>>(error) && error.response?.data.message)
    return new Error(error.response.data.message)
  return error instanceof Error ? error : new Error('暂时无法完成分配操作，请稍后重试')
}
