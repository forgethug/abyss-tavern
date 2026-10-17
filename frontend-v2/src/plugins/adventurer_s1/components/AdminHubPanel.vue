<template>
  <div v-if="secondaryAuthStatusReady" class="admin-hub">
    <header class="admin-hero">
      <div class="admin-hero-mark" aria-hidden="true">🛡️</div>
      <div class="admin-hero-copy">
        <p class="admin-eyebrow">S1 CONTROL DESK</p>
        <h5>酒馆管理台</h5>
        <p>{{ activeAdminTab.description }}</p>
      </div>
      <div class="admin-current-location" aria-live="polite">
        <span>当前分区</span>
        <strong>{{ activeAdminTab.icon }} {{ activeAdminTab.label }}</strong>
      </div>
    </header>

    <AdminAiKillSwitch />

    <nav class="admin-subtabs" aria-label="管理分区">
      <button
        v-for="tab in availableAdminTabs"
        :key="tab.id"
        type="button"
        :class="['subtab-btn', { active: activeSubTab === tab.id }]"
        @click="switchSubTab(tab.id)"
      >
        <span class="subtab-icon" aria-hidden="true">{{ tab.icon }}</span>
        <span class="subtab-copy">
          <strong>{{ tab.label }}</strong>
          <small>{{ tab.shortDescription }}</small>
        </span>
        <span class="subtab-arrow" aria-hidden="true">›</span>
      </button>
    </nav>

    <!-- 运营 -->
    <div v-if="activeSubTab === 'ops'" class="subtab-panel">
      <nav class="section-subtabs" aria-label="运营工具">
        <button
          v-for="sec in OPS_SECTIONS"
          :key="sec.id"
          type="button"
          :class="['section-btn', { active: opsSection === sec.id }]"
          @click="opsSection = sec.id"
        >{{ sec.icon }} {{ sec.label }}</button>
      </nav>

      <div v-if="opsSection === 'notice'" class="section-card">
        <h6 class="section-title">📢 酒馆通知</h6>
        <textarea
          v-model="noticeDraft"
          class="admin-notice-input"
          rows="4"
          maxlength="2000"
          :disabled="!noticeLoaded"
          placeholder="输入通知内容，发布后所有玩家顶部可见，需点击「我知道了」才会关闭；再次修改后未确认的玩家会重新看到。"
        />
        <div class="admin-actions" style="margin-top: 8px;">
          <button type="button" class="btn-primary btn-sm" :disabled="savingNotice || !noticeLoaded" @click="saveNotice">发布通知</button>
          <button type="button" class="btn-secondary btn-sm" :disabled="savingNotice || !noticeLoaded" @click="clearNotice">清空通知</button>
          <button v-if="!noticeLoaded" type="button" class="btn-secondary btn-sm" :disabled="noticeLoading" @click="loadNoticeDraft">{{ noticeLoading ? '正在读取…' : '重新读取通知' }}</button>
        </div>
        <p class="admin-hint">当前版本：{{ store.noticeVersion || '无' }}；玩家确认后不再显示，直到你修改内容。</p>
      </div>

      <div v-if="opsSection === 'support-vote'" class="section-card">
        <h6 class="section-title">🗳️ 投票管理</h6>
        <div class="vote-admin-form">
          <label class="form-row">
            <span>投票标题</span>
            <input v-model="supportVoteDraft.title" class="admin-text-input" maxlength="100" placeholder="例如：下一阶段酒馆优先建设方向" />
          </label>
          <label class="form-row">
            <span>投票内容</span>
            <textarea v-model="supportVoteDraft.content" class="admin-notice-input" rows="3" maxlength="1000" placeholder="写给玩家看的投票说明" />
          </label>
          <label class="form-row">
            <span>截止日期</span>
            <input v-model="supportVoteDraft.deadline" class="admin-text-input" type="datetime-local" />
          </label>
          <div class="form-row">
            <span>投票选项</span>
            <div class="vote-option-editor">
              <div v-for="(opt, idx) in supportVoteDraft.options" :key="idx" class="vote-option-row">
                <input v-model="opt.label" class="admin-text-input" maxlength="100" :placeholder="`选项 ${idx + 1}`" />
                <input v-model="opt.description" class="admin-text-input" maxlength="300" placeholder="选项说明（可选）" />
                <button type="button" class="btn-secondary btn-sm" :disabled="supportVoteDraft.options.length <= 1" @click="removeSupportVoteOption(idx)">删除</button>
              </div>
              <button type="button" class="btn-secondary btn-sm" @click="addSupportVoteOption">+ 添加选项</button>
            </div>
          </div>
          <div class="admin-actions">
            <button type="button" class="btn-primary btn-sm" :disabled="savingSupportVote" @click="saveSupportVote">发布新投票</button>
            <button type="button" class="btn-secondary btn-sm" :disabled="savingSupportVote" @click="loadSupportVoteStats(true)">刷新统计</button>
            <button type="button" class="btn-warning btn-sm" :disabled="savingSupportVote" @click="clearSupportVote">清空投票</button>
          </div>
          <p class="admin-hint">发布新投票会清空旧投票记录，玩家按钮数字只显示当前投票选项数。</p>
        </div>

        <div v-if="supportVoteStats" class="vote-admin-summary">
          <div class="config-grid">
            <div class="config-item">
              <label>当前标题</label>
              <span>{{ supportVoteStats.current_vote.title || '无' }}</span>
            </div>
            <div class="config-item">
              <label>截止日期</label>
              <span>{{ formatSupportVoteDate(supportVoteStats.current_vote.deadline) }}</span>
            </div>
            <div class="config-item">
              <label>状态</label>
              <span>{{ supportVoteStats.current_vote.is_active ? '进行中' : (supportVoteStats.current_vote.is_expired ? '已截止' : '未发布') }}</span>
            </div>
            <div class="config-item">
              <label>投票人数</label>
              <span>{{ supportVoteStats.vote_count ?? 0 }}</span>
            </div>
          </div>
          <div class="vote-count-list">
            <div v-for="item in supportVoteStats.option_counts" :key="item.option_key" class="vote-count-row">
              <span>{{ item.label }}</span>
              <strong>{{ item.count }}</strong>
            </div>
            <p v-if="supportVoteStats.option_counts.length === 0" class="admin-hint">暂无投票选项。</p>
          </div>
          <div class="recent-vote-list">
            <div class="recent-vote-heading">
              <h6 class="section-title">最近投票</h6>
              <span>共显示最近 {{ supportVoteStats.recent_votes.length }} 条</span>
            </div>
            <div v-for="vote in pagedRecentVotes" :key="vote.id" class="recent-vote-row">
              <span>{{ vote.nickname || `用户#${vote.user_id}` }}</span>
              <span>{{ supportVoteOptionLabel(vote.option_key) }}</span>
              <span>{{ formatSupportVoteDate(vote.created_at) }}</span>
            </div>
            <p v-if="supportVoteStats.recent_votes.length === 0" class="admin-hint">暂无玩家投票。</p>
            <div v-if="recentVoteTotalPages > 1" class="recent-vote-pager">
              <button type="button" class="btn-secondary btn-sm" :disabled="recentVotePage <= 1" @click="recentVotePage--">上一页</button>
              <span>第 {{ recentVotePage }} / {{ recentVoteTotalPages }} 页</span>
              <button type="button" class="btn-secondary btn-sm" :disabled="recentVotePage >= recentVoteTotalPages" @click="recentVotePage++">下一页</button>
            </div>
          </div>
        </div>
      </div>

      <div v-if="opsSection === 'tavern'" class="section-card">
        <h6 class="section-title">🏰 酒馆运营</h6>
        <div class="admin-setting-block">
          <h6 class="admin-setting-heading">升级进度</h6>
          <label class="admin-toggle-row">
            <input
              type="checkbox"
              :checked="tavernUpgradeUiEnabled"
              :disabled="savingTavernUpgradeUi || !tavernUpgradeLoaded || tavernUpgradeLoading"
              @change="onTavernUpgradeUiToggle"
            />
            <span>显示酒馆升级进度面板</span>
          </label>
          <p class="admin-hint">
            关闭后全服玩家看不到升级进度。Lv.2 门槛为 72 名存活冒险者，达标后由管理员在面板中解锁。
          </p>
          <button v-if="!tavernUpgradeLoaded" type="button" class="btn-secondary btn-sm" :disabled="tavernUpgradeLoading" @click="loadTavernUpgrade">{{ tavernUpgradeLoading ? '正在读取…' : '重新读取升级状态' }}</button>
        </div>

      </div>

      <div v-if="opsSection === 'level-cap'" class="section-card">
        <h6 class="section-title">⭐ 等级上限维护</h6>
        <p class="admin-hint">
          当前经验字段是“当前等级内经验”，不是总经验。此操作只把当前等级上限及以上冒险者的当前等级经验清为 0，不改变等级，避免未来开放上限时连跳。
        </p>
        <div class="admin-actions" style="margin-top: 8px;">
          <button type="button" class="btn-warning btn-sm" :disabled="levelCapResetLoading" @click="resetLevelCapExp">
            清零封顶溢出经验
          </button>
          <button type="button" class="btn-primary btn-sm" :disabled="levelCapOpen15Loading" @click="openLevelCap15">
            一键开放到 15 级
          </button>
        </div>
        <p v-if="levelCapResetPreview" class="admin-hint">
          最近预览：当前上限 Lv.{{ levelCapResetPreview.max_level }}，受影响 {{ levelCapResetPreview.affected_count }} 名，合计清理 {{ levelCapResetPreview.total_exp_to_clear }} 经验，单人最高 {{ levelCapResetPreview.max_exp }}。
        </p>
      </div>

      <div v-if="opsSection === 'ai-feedback'" class="section-card">
        <AdminAiFeedbackPanel />
      </div>

      <div v-if="opsSection === 'export'" class="section-card">
        <h6 class="section-title">📦 数据导出</h6>
        <p class="admin-hint">导出当前管理区服的玩家业务数据：角色、仓库、动态、探险故事、聊天、建筑与宿舍等。探险正文按「编号-地点-日期-小队成员」生成 Markdown；管理员后台修改、审计与 AI 运行数据不会包含在压缩包中。此功能无需二级验证。</p>
        <div class="admin-actions">
          <button type="button" class="btn-primary btn-sm" :disabled="exportLoading" @click="exportAllPlayerData">
            {{ exportLoading ? '正在生成文件…' : '📦 生成全部数据归档' }}
          </button>
          <button type="button" class="btn-secondary btn-sm" :disabled="exportFilesLoading" @click="loadExportArchives">
            {{ exportFilesLoading ? '正在读取…' : '刷新归档列表' }}
          </button>
        </div>
        <p class="admin-hint">文件会保存到服务器的 <code>backend/backups</code> 目录，不再通过网页传输大 ZIP。请在服务器上手动下载。</p>
        <div v-if="exportFiles.length" class="export-archive-list">
          <div v-for="archive in exportFiles" :key="archive.filename" class="export-archive-row">
            <div>
              <strong>{{ archive.filename }}</strong>
              <span>{{ archive.size_mb }} MB · {{ archive.created_at }}</span>
            </div>
            <code>{{ archive.relative_path }}</code>
          </div>
        </div>
        <p v-else class="admin-hint">暂无已生成的归档。</p>
      </div>
    </div>

    <!-- 探险 -->
    <div v-if="activeSubTab === 'expedition'" class="subtab-panel">
      <nav class="section-subtabs" aria-label="探险工具">
        <button
          v-for="sec in EXPEDITION_SECTIONS"
          :key="sec.id"
          type="button"
          :class="['section-btn', { active: expeditionSection === sec.id }]"
          @click="expeditionSection = sec.id"
        >{{ sec.icon }} {{ sec.label }}</button>
      </nav>

      <div v-if="expeditionSection === 'party'" class="section-card">
        <h6 class="section-title">👥 匹配与小队</h6>
        <p class="admin-hint">仅显示<strong>匹配中</strong>与<strong>集结中（未进本）</strong>的玩家；已进本探险的不在此列出。</p>
        <AdminPartyMatchPanel />
      </div>

      <div v-if="expeditionSection === 'dispatch'" class="section-card">
        <h6 class="section-title">⚔️ 探险调度</h6>
        <div class="admin-actions">
          <button type="button" class="btn-primary btn-sm" :disabled="actionLoading" @click="doFormParties">🏰 手动匹配</button>
          <button type="button" class="btn-warning btn-sm" :disabled="actionLoading || dispatchPaused" @click="doDispatch">🚀 手动发车</button>
        </div>
        <p class="admin-hint">手动匹配：按当前发车配额从匹配队列成团。手动发车：扫已到出发时间的 forming 队发起探险。</p>
        <label class="admin-toggle-row" style="margin-top: 8px;">
          <input
            type="checkbox"
            :checked="dispatchPaused"
            :disabled="savingDispatchPaused"
            @change="onDispatchPausedToggle"
          />
          <span>暂停探险出发（集结照常）</span>
        </label>
        <p class="admin-hint">
          开启后：自动/手动发车静默跳过，玩家侧组队、投票、倒计时照常。关闭后积压的到期队伍会在下一轮 tick 正常发出。
        </p>
      </div>

      <div v-if="expeditionSection === 'config'" class="section-card">
        <AdminRuntimeSettingsPanel />
      </div>

      <div v-if="expeditionSection === 'trait'" class="section-card">
        <h6 class="section-title">✂️ AI 描述压缩</h6>
        <AdminTraitCompressPanel />
      </div>

      <div v-if="expeditionSection === 'fate-repair'" class="section-card">
        <AdminFateSettlementRepairPanel />
      </div>

    </div>

    <!-- 收益监控 -->
    <div v-if="activeSubTab === 'gain-monitor'" class="subtab-panel">
      <div class="section-card">
        <AdminGainAnomalyPanel />
      </div>
    </div>

    <!-- 线索追查 -->
    <div v-if="activeSubTab === 'clue-bureau'" class="subtab-panel">
      <div class="section-card">
        <h6 class="section-title">🔍 线索追查池</h6>
        <AdminClueBureauPanel ref="clueBureauRef" />
      </div>
    </div>

    <!-- 审核 -->
    <div v-if="activeSubTab === 'audit'" class="subtab-panel">
      <nav class="section-subtabs" aria-label="审核工具">
        <button
          v-for="sec in AUDIT_SECTIONS"
          :key="sec.id"
          type="button"
          :class="['section-btn', { active: auditSection === sec.id }]"
          @click="auditSection = sec.id"
        >{{ sec.icon }} {{ sec.label }}</button>
      </nav>

      <div v-if="auditSection === 'appeal'" class="section-card">
        <h6 class="section-title">🔧 数据异常申诉</h6>
        <AdminReadonlyDataAppealPanel v-if="isReadonly" />
        <AdminDataAppealPanel v-else />
      </div>

      <div v-if="auditSection === 'item'" class="section-card">
        <h6 class="section-title">🔍 物品持有者查询</h6>
        <AdminItemHolderSearchPanel />
      </div>
    </div>

    <!-- 数据内容 -->
    <div v-if="activeSubTab === 'content'" class="subtab-panel">
      <nav class="section-subtabs" aria-label="数据内容工具">
        <button
          v-for="sec in availableContentSections"
          :key="sec.id"
          type="button"
          :class="['section-btn', { active: contentSection === sec.id }]"
          @click="contentSection = sec.id"
        >{{ sec.icon }} {{ sec.label }}</button>
      </nav>

      <div v-if="contentSection === 'story'" class="section-card">
        <h6 class="section-title">🕸️ 隐藏剧情线</h6>
        <p class="admin-hint">玩家不可见，仅供探险 AI 延续长期伏笔。支持全服搜索、统计与增删改。</p>
        <AdminReadonlyStoryThreadPanel v-if="isReadonly" />
        <AdminStoryThreadPanel v-else />
      </div>

      <div v-if="contentSection === 'outline'" class="section-card">
        <h6 class="section-title">🌐 世界情报库</h6>
        <p class="admin-hint">维护公共世界情报、预览探险 AI 输出的世界事实候选，并查看按副本维护的 DM 接力便签。</p>
        <AdminWorldOutlinePanel :readonly="isReadonly" />
      </div>

      <div v-if="contentSection === 'adventurer'" class="section-card">
        <h6 class="section-title">🧙 冒险者数据</h6>
        <AdminAdventurerPanel />
      </div>

      <div v-if="contentSection === 'title'" class="section-card">
        <AdminTitlePanel />
      </div>

      <div v-if="contentSection === 'auction'" class="section-card">
        <AdminSecretAuctionPanel :readonly="isReadonly" />
      </div>
    </div>

    <!-- 建筑图鉴 -->
    <div v-if="activeSubTab === 'assets'" class="subtab-panel">
      <nav class="section-subtabs" aria-label="建筑图鉴工具">
        <button
          v-for="sec in availableAssetsSections"
          :key="sec.id"
          type="button"
          :class="['section-btn', { active: assetsSection === sec.id }]"
          @click="assetsSection = sec.id"
        >{{ sec.icon }} {{ sec.label }}</button>
      </nav>

      <div v-if="assetsSection === 'links'" class="section-card">
        <h6 class="section-title">📋 建筑与图鉴</h6>
        <p class="admin-hint">以下管理入口会跳转到「建筑」分区内的专用面板。</p>
        <div class="asset-link-grid">
          <button v-if="!isReadonly" type="button" class="asset-link-card" @click="emit('open-asset', 'reservation')">
            <span class="asset-link-icon">📋</span>
            <span class="asset-link-label">预约管理</span>
            <span class="asset-link-desc">建筑使用预约队列</span>
          </button>
          <button v-if="!isReadonly" type="button" class="asset-link-card" @click="emit('open-asset', 'building')">
            <span class="asset-link-icon">🏗️</span>
            <span class="asset-link-label">建筑管理</span>
            <span class="asset-link-desc">模板与实例</span>
          </button>
          <button v-if="!isReadonly" type="button" class="asset-link-card" @click="emit('open-asset', 'wheel')">
            <span class="asset-link-icon">🎡</span>
            <span class="asset-link-label">转盘管理</span>
            <span class="asset-link-desc">奖池与轮换</span>
          </button>
          <button type="button" class="asset-link-card" @click="emit('open-asset', 'catalog')">
            <span class="asset-link-icon">📚</span>
            <span class="asset-link-label">图鉴管理</span>
            <span class="asset-link-desc">物品图鉴条目</span>
          </button>
        </div>
      </div>

    </div>
  </div>
  <div v-else class="admin-auth-loading">正在确认管理权限…</div>
</template>

<script setup lang="ts">
import { defineAsyncComponent, computed, onMounted, ref, watch } from 'vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import {
  getAdminSecondaryAuthScope,
  getAdminSecondaryAuthStatusReady,
} from '@/composables/useApi'
import { useAdventurerS1Store, type S1ExportArchive, type SupportVoteAdminStats } from '../stores/adventurerS1'
const AdminClueBureauPanel = defineAsyncComponent(() => import('./AdminClueBureauPanel.vue'))
const AdminGainAnomalyPanel = defineAsyncComponent(() => import('./AdminGainAnomalyPanel.vue'))
const AdminDataAppealPanel = defineAsyncComponent(() => import('./AdminDataAppealPanel.vue'))
const AdminReadonlyDataAppealPanel = defineAsyncComponent(() => import('./AdminReadonlyDataAppealPanel.vue'))
const AdminTraitCompressPanel = defineAsyncComponent(() => import('./AdminTraitCompressPanel.vue'))
const AdminPartyMatchPanel = defineAsyncComponent(() => import('./AdminPartyMatchPanel.vue'))
const AdminItemHolderSearchPanel = defineAsyncComponent(() => import('./AdminItemHolderSearchPanel.vue'))
const AdminStoryThreadPanel = defineAsyncComponent(() => import('./AdminStoryThreadPanel.vue'))
const AdminReadonlyStoryThreadPanel = defineAsyncComponent(() => import('./AdminReadonlyStoryThreadPanel.vue'))
const AdminWorldOutlinePanel = defineAsyncComponent(() => import('./AdminWorldOutlinePanel.vue'))
const AdminAdventurerPanel = defineAsyncComponent(() => import('./AdminAdventurerPanel.vue'))
const AdminTitlePanel = defineAsyncComponent(() => import('./AdminTitlePanel.vue'))
const AdminSecretAuctionPanel = defineAsyncComponent(() => import('./AdminSecretAuctionPanel.vue'))
const AdminAiKillSwitch = defineAsyncComponent(() => import('./AdminAiKillSwitch.vue'))
const AdminRuntimeSettingsPanel = defineAsyncComponent(() => import('./AdminRuntimeSettingsPanel.vue'))
const AdminFateSettlementRepairPanel = defineAsyncComponent(() => import('./AdminFateSettlementRepairPanel.vue'))
const AdminAiFeedbackPanel = defineAsyncComponent(() => import('./AdminAiFeedbackPanel.vue'))

export type AdminAssetPanel = 'reservation' | 'building' | 'wheel' | 'catalog'

type AdminSubTab = 'ops' | 'expedition' | 'gain-monitor' | 'clue-bureau' | 'audit' | 'content' | 'assets'
type OpsSection = 'notice' | 'support-vote' | 'tavern' | 'level-cap' | 'ai-feedback' | 'export'
type ExpeditionSection = 'party' | 'dispatch' | 'config' | 'trait' | 'fate-repair'
type AuditSection = 'appeal' | 'item'
type ContentSection = 'story' | 'outline' | 'adventurer' | 'title' | 'auction'
type AssetsSection = 'links'

interface SectionDef<T extends string> {
  id: T
  label: string
  icon: string
}

const ADMIN_TABS: {
  id: AdminSubTab
  label: string
  icon: string
  shortDescription: string
  description: string
}[] = [
  { id: 'ops', label: '运营', icon: '📢', shortDescription: '公告、投票与开关', description: '集中管理全服公告、玩家投票、酒馆接纳与等级上限。' },
  { id: 'expedition', label: '探险', icon: '⚔️', shortDescription: '组队、发车与运行', description: '查看匹配队列，控制探险调度并维护运行配置。' },
  { id: 'gain-monitor', label: '收益记录', icon: '📈', shortDescription: '历史与人工查询', description: '查看历史收益记录或人工回查；后台不再自动扫描。' },
  { id: 'clue-bureau', label: '线索追查', icon: '🔍', shortDescription: 'Boss 线索工作台', description: '维护线索追查池、Boss 范围及异常锁定状态。' },
  { id: 'audit', label: '审核', icon: '🔧', shortDescription: '申诉与物品查询', description: '审阅数据异常申诉，并按条件定位物品及其持有者。' },
  { id: 'content', label: '数据内容', icon: '🧙', shortDescription: '剧情、世界与角色', description: '维护隐藏剧情线、世界情报、冒险者数据及拍卖内容。' },
  { id: 'assets', label: '建筑图鉴', icon: '🏗️', shortDescription: '建筑与图鉴入口', description: '快速前往建筑、预约、转盘和物品图鉴等资产管理工具。' },
]

const OPS_SECTIONS: SectionDef<OpsSection>[] = [
  { id: 'notice', label: '酒馆通知', icon: '📢' },
  { id: 'support-vote', label: '投票管理', icon: '🗳️' },
  { id: 'tavern', label: '酒馆运营', icon: '🏰' },
  { id: 'level-cap', label: '等级上限', icon: '⭐' },
  { id: 'ai-feedback', label: 'AI 反馈', icon: '✨' },
  { id: 'export', label: '数据导出', icon: '📦' },
]

const EXPEDITION_SECTIONS: SectionDef<ExpeditionSection>[] = [
  { id: 'party', label: '匹配小队', icon: '👥' },
  { id: 'dispatch', label: '调度开关', icon: '⚔️' },
  { id: 'config', label: '运行与 AI', icon: '⚙️' },
  { id: 'trait', label: '描述压缩', icon: '✂️' },
  { id: 'fate-repair', label: '命途修复', icon: '🧯' },
]

const AUDIT_SECTIONS: SectionDef<AuditSection>[] = [
  { id: 'appeal', label: '数据申诉', icon: '🔧' },
  { id: 'item', label: '物品查询', icon: '🔍' },
]

const CONTENT_SECTIONS: SectionDef<ContentSection>[] = [
  { id: 'story', label: '隐藏剧情线', icon: '🕸️' },
  { id: 'outline', label: '世界情报', icon: '🌐' },
  { id: 'adventurer', label: '冒险者', icon: '🧙' },
  { id: 'title', label: '称号边框', icon: '🏷️' },
  { id: 'auction', label: '隐秘拍卖', icon: '🎭' },
]

const ASSETS_SECTIONS: SectionDef<AssetsSection>[] = [
  { id: 'links', label: '管理入口', icon: '📋' },
]

const emit = defineEmits<{
  'open-asset': [AdminAssetPanel]
}>()

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const secondaryAuthScope = getAdminSecondaryAuthScope()
const secondaryAuthStatusReady = getAdminSecondaryAuthStatusReady()
const isReadonly = computed(() => false)
const availableAdminTabs = computed(() => isReadonly.value
  ? ADMIN_TABS.filter(tab => tab.id === 'audit' || tab.id === 'content' || tab.id === 'assets')
  : ADMIN_TABS)
const availableContentSections = computed(() => isReadonly.value
  ? CONTENT_SECTIONS.filter(section => (
    section.id === 'story' || section.id === 'outline' || section.id === 'auction'
  ))
  : CONTENT_SECTIONS)
const availableAssetsSections = computed(() => isReadonly.value
  ? ASSETS_SECTIONS.filter(section => section.id === 'links')
  : ASSETS_SECTIONS)

const activeSubTab = ref<AdminSubTab>('ops')
const activeAdminTab = computed(() => ADMIN_TABS.find(tab => tab.id === activeSubTab.value) ?? ADMIN_TABS[0])
const opsSection = ref<OpsSection>('notice')
const expeditionSection = ref<ExpeditionSection>('party')
const auditSection = ref<AuditSection>('appeal')
const contentSection = ref<ContentSection>('story')
const assetsSection = ref<AssetsSection>('links')
const clueBureauRef = ref<InstanceType<typeof AdminClueBureauPanel> | null>(null)

function switchSubTab(tab: AdminSubTab) {
  if (isReadonly.value && !availableAdminTabs.value.some(item => item.id === tab)) return
  activeSubTab.value = tab
}

watch(isReadonly, readonly => {
  if (!readonly) return
  activeSubTab.value = 'content'
  if (!availableContentSections.value.some(section => section.id === contentSection.value)) {
    contentSection.value = 'story'
  }
}, { immediate: true })

const noticeDraft = ref('')
const noticeLoaded = ref(false)
const noticeLoading = ref(false)
const tavernUpgradeLoaded = ref(false)
const tavernUpgradeLoading = ref(false)
const panelMounted = ref(false)
const savingNotice = ref(false)
const savingSupportVote = ref(false)
const exportLoading = ref(false)
const exportFilesLoading = ref(false)
const exportFiles = ref<S1ExportArchive[]>([])
const supportVoteStats = ref<SupportVoteAdminStats | null>(null)
const recentVotePage = ref(1)
const RECENT_VOTE_PAGE_SIZE = 10
const recentVoteTotalPages = computed(() => Math.max(1, Math.ceil((supportVoteStats.value?.recent_votes.length ?? 0) / RECENT_VOTE_PAGE_SIZE)))
const pagedRecentVotes = computed(() => {
  const start = (recentVotePage.value - 1) * RECENT_VOTE_PAGE_SIZE
  return supportVoteStats.value?.recent_votes.slice(start, start + RECENT_VOTE_PAGE_SIZE) ?? []
})
const supportVoteDraft = ref({
  title: '',
  content: '',
  deadline: '',
  options: [{ label: '', description: '' }],
})
const actionLoading = ref(false)
const expeditionConfig = ref<Record<string, any> | null>(null)
const dispatchPaused = ref(false)
const savingDispatchPaused = ref(false)
const savingTavernUpgradeUi = ref(false)
const levelCapResetLoading = ref(false)
const levelCapOpen15Loading = ref(false)
const levelCapResetPreview = ref<{
  max_level: number
  affected_count: number
  total_exp_to_clear: number
  max_exp: number
} | null>(null)

const tavernUpgradeUiEnabled = computed(() => store.tavernUpgrade?.upgrade_ui_enabled === true)
async function saveNotice() {
  if (savingNotice.value || !noticeLoaded.value) return
  savingNotice.value = true
  try {
    const res = await store.adminSetNotice(noticeDraft.value.trim())
    if (res.code === 200) {
      message.success('通知已发布')
      noticeDraft.value = store.notice
    } else {
      message.error(res.message || '发布失败')
    }
  } finally {
    savingNotice.value = false
  }
}

async function clearNotice() {
  if (savingNotice.value || !noticeLoaded.value) return
  if (!confirm('确定清空酒馆通知？所有玩家将不再看到通知条。')) return
  savingNotice.value = true
  try {
    const res = await store.adminSetNotice('')
    if (res.code === 200) {
      message.success('通知已清空')
      noticeDraft.value = ''
    } else {
      message.error(res.message || '清空失败')
    }
  } finally {
    savingNotice.value = false
  }
}

async function exportAllPlayerData() {
  if (exportLoading.value) return
  exportLoading.value = true
  try {
    const res = await store.adminExportAllPlayerData()
    if (res.code !== 200) {
      message.error(res.message || '导出失败，请稍后重试')
      return
    }
    await loadExportArchives()
    message.success(`${res.data?.filename || '归档文件'} 已生成，请到服务器 backend/backups 目录下载`)
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '导出失败，请稍后重试')
  } finally {
    exportLoading.value = false
  }
}

async function loadExportArchives() {
  if (exportFilesLoading.value) return
  exportFilesLoading.value = true
  try {
    const res = await store.adminListExportArchives()
    if (res.code !== 200) {
      message.error(res.message || '归档列表读取失败')
      return
    }
    exportFiles.value = Array.isArray(res.data?.files) ? res.data.files : []
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '归档列表读取失败')
  } finally {
    exportFilesLoading.value = false
  }
}

async function fetchExpeditionConfig(force = false) {
  if (!force && store._expeditionConfig) {
    expeditionConfig.value = store._expeditionConfig
    return
  }
  try {
    const res = await store.adminGetExpeditionConfig('selected')
    if (res.code === 200) {
      expeditionConfig.value = res.data
      store._expeditionConfig = res.data
      dispatchPaused.value = res.data?.expedition_dispatch_paused === true
    }
  } catch { /* ignore */ }
}

async function onDispatchPausedToggle(e: Event) {
  const paused = (e.target as HTMLInputElement).checked
  const action = paused ? '暂停探险出发' : '恢复探险出发'
  if (!confirm(`确定${action}？${paused ? '调度层将静默跳过发车，玩家无感知。' : '积压的到期队伍会在下一轮 tick 正常发出。'}`)) {
    ;(e.target as HTMLInputElement).checked = !paused
    return
  }
  if (savingDispatchPaused.value) return
  savingDispatchPaused.value = true
  try {
    const res = await store.updateDormitorySetting(
      'expedition_dispatch_paused',
      paused,
      'selected',
    )
    if (res.code === 200) {
      dispatchPaused.value = paused
      if (expeditionConfig.value) {
        expeditionConfig.value = { ...expeditionConfig.value, expedition_dispatch_paused: paused }
      }
      message.success(paused ? '已静默暂停发车' : '已恢复发车')
    } else {
      message.error(res.message || '更新失败')
      ;(e.target as HTMLInputElement).checked = !paused
    }
  } catch (err: unknown) {
    const e2 = err as { response?: { data?: { message?: string } } }
    message.error(e2?.response?.data?.message || '更新失败')
    ;(e.target as HTMLInputElement).checked = !paused
  } finally {
    savingDispatchPaused.value = false
  }
}

async function onTavernUpgradeUiToggle(e: Event) {
  const enabled = (e.target as HTMLInputElement).checked
  if (savingTavernUpgradeUi.value) return
  savingTavernUpgradeUi.value = true
  try {
    const res = await store.setTavernUpgradeUiEnabled(enabled, 'selected')
    if (res?.code === 200) {
      message.success(enabled ? '已显示酒馆升级面板' : '已隐藏酒馆升级面板')
    } else {
      message.error(res?.message || '设置失败')
      await store.adminFetchTavernUpgradeProgress()
    }
  } finally {
    savingTavernUpgradeUi.value = false
  }
}

function toDatetimeLocal(value: string | null | undefined): string {
  if (!value) return ''
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`
}

function formatSupportVoteDate(value: string | null | undefined): string {
  if (!value) return '无'
  const d = new Date(value)
  if (Number.isNaN(d.getTime())) return value
  return d.toLocaleString()
}

function fillSupportVoteDraft(stats: SupportVoteAdminStats) {
  const vote = stats.current_vote
  supportVoteDraft.value = {
    title: vote.title || '',
    content: vote.content || '',
    deadline: toDatetimeLocal(vote.deadline),
    options: vote.options.length > 0
      ? vote.options.map(opt => ({ label: opt.label, description: opt.description || '' }))
      : [{ label: '', description: '' }],
  }
}

async function loadSupportVoteStats(fillDraft = false) {
  try {
    const res = await store.adminFetchSupportVoteStats()
    if (res.code === 200) {
      supportVoteStats.value = res.data as SupportVoteAdminStats
      recentVotePage.value = 1
      if (fillDraft) fillSupportVoteDraft(supportVoteStats.value)
    } else {
      message.error(res.message || '投票统计加载失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '投票统计加载失败')
  }
}

function addSupportVoteOption() {
  if (supportVoteDraft.value.options.length >= 20) {
    message.warning('最多 20 个选项')
    return
  }
  supportVoteDraft.value.options.push({ label: '', description: '' })
}

function removeSupportVoteOption(idx: number) {
  if (supportVoteDraft.value.options.length <= 1) return
  supportVoteDraft.value.options.splice(idx, 1)
}

function supportVoteOptionLabel(optionKey: string): string {
  const item = supportVoteStats.value?.option_counts.find(opt => opt.option_key === optionKey)
  return item?.label || optionKey
}

async function saveSupportVote() {
  if (savingSupportVote.value) return
  savingSupportVote.value = true
  try {
    const res = await store.adminSetSupportVote({
      title: supportVoteDraft.value.title.trim(),
      content: supportVoteDraft.value.content.trim(),
      deadline: supportVoteDraft.value.deadline || null,
      options: supportVoteDraft.value.options
        .map(opt => ({ label: opt.label.trim(), description: opt.description.trim() }))
        .filter(opt => opt.label),
    })
    if (res.code === 200) {
      message.success('投票已发布')
      supportVoteStats.value = res.data as SupportVoteAdminStats
      recentVotePage.value = 1
      fillSupportVoteDraft(supportVoteStats.value)
    } else {
      message.error(res.message || '发布失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '发布失败')
  } finally {
    savingSupportVote.value = false
  }
}

async function clearSupportVote() {
  if (savingSupportVote.value) return
  if (!confirm('确定清空当前投票？现有投票记录也会被删除。')) return
  savingSupportVote.value = true
  try {
    const res = await store.adminSetSupportVote({ clear: true })
    if (res.code === 200) {
      message.success('投票已清空')
      supportVoteStats.value = res.data as SupportVoteAdminStats
      recentVotePage.value = 1
      fillSupportVoteDraft(supportVoteStats.value)
    } else {
      message.error(res.message || '清空失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '清空失败')
  } finally {
    savingSupportVote.value = false
  }
}

async function resetLevelCapExp() {
  if (levelCapResetLoading.value) return
  levelCapResetLoading.value = true
  try {
    const preview = await store.adminPreviewLevelCapExpReset()
    if (preview.code !== 200) {
      message.error(preview.message || '预览失败')
      return
    }
    levelCapResetPreview.value = preview.data
    const d = preview.data || {}
    const affected = Number(d.affected_count || 0)
    if (affected <= 0) {
      message.success(`当前上限 Lv.${d.max_level ?? '?'} 没有需要清零的封顶经验`)
      return
    }
    const ok = confirm(
      `确定清零当前等级上限 Lv.${d.max_level} 及以上冒险者的当前等级经验？\n` +
      `受影响 ${affected} 名，合计 ${d.total_exp_to_clear || 0} 经验，单人最高 ${d.max_exp || 0}。\n` +
      '此操作不会改变等级，只会把 exp 设为 0。'
    )
    if (!ok) return
    const res = await store.adminResetLevelCapExp()
    if (res.code === 200) {
      message.success(res.message || `已清零 ${res.data?.affected_count ?? 0} 名冒险者的封顶经验`)
      levelCapResetPreview.value = null
    } else {
      message.error(res.message || '清零失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '清零失败')
  } finally {
    levelCapResetLoading.value = false
  }
}

async function openLevelCap15() {
  if (levelCapOpen15Loading.value) return
  if (!confirm('确定将 S1 冒险者等级上限开放到 Lv.15？建议先清零封顶溢出经验。')) return
  levelCapOpen15Loading.value = true
  try {
    const res = await store.adminOpenLevelCap15()
    if (res.code === 200) {
      message.success(res.message || '已开放等级上限到 Lv.15')
      levelCapResetPreview.value = null
    } else {
      message.error(res.message || '开放失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '开放失败')
  } finally {
    levelCapOpen15Loading.value = false
  }
}

async function doFormParties() {
  if (actionLoading.value) return
  actionLoading.value = true
  try {
    const res = await store.adminFormParties('selected')
    if (res.code === 200) {
      const d = res.data
      message.success(`匹配完成：成团 ${d.matched ?? 0} 支，当前可用槽 ${d.slots ?? 0}`)
    } else {
      message.error(res.message || '组队失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '组队失败')
  } finally {
    actionLoading.value = false
  }
}

async function doDispatch() {
  if (actionLoading.value || dispatchPaused.value) return
  actionLoading.value = true
  try {
    const res = await store.adminDispatchDeparted('selected')
    if (res.code === 200) {
      const d = res.data
      if (d.ai_blocked) {
        message.warning(d.ai_blocked_reason || '当前 S1 AI 调用已关闭，待发队伍已保留')
      } else {
        message.success(`发车完成：成功 ${d.dispatched} 支，失败 ${d.failed} 支`)
      }
    } else {
      message.error(res.message || '发车失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message.error(err?.response?.data?.message || '发车失败')
  } finally {
    actionLoading.value = false
  }
}

onMounted(() => { panelMounted.value = true })

async function loadNoticeDraft() {
  if (noticeLoaded.value || noticeLoading.value) return
  noticeLoading.value = true
  try {
    const res = await store.adminFetchNotice()
    if (res?.code !== 200) throw new Error(res?.message || '通知读取失败')
    noticeDraft.value = store.notice
    noticeLoaded.value = true
  } catch {
    message.error('通知读取失败，请稍后重新读取')
  } finally {
    noticeLoading.value = false
  }
}

async function loadTavernUpgrade() {
  if (tavernUpgradeLoading.value) return
  tavernUpgradeLoading.value = true
  tavernUpgradeLoaded.value = false
  try {
    const res = await store.adminFetchTavernUpgradeProgress()
    if (res?.code !== 200) throw new Error(res?.message || '升级状态读取失败')
    tavernUpgradeLoaded.value = true
  } catch {
    message.error('升级状态读取失败，请稍后重新读取')
  } finally {
    tavernUpgradeLoading.value = false
  }
}

function loadVisibleAdminSection() {
  if (!panelMounted.value || !secondaryAuthStatusReady.value || isReadonly.value) return
  if (activeSubTab.value === 'ops') {
    if (opsSection.value === 'notice') void loadNoticeDraft()
    else if (opsSection.value === 'support-vote' && !supportVoteStats.value) void loadSupportVoteStats(true)
    else if (opsSection.value === 'tavern') {
      void loadTavernUpgrade()
    } else if (opsSection.value === 'export') void loadExportArchives()
  } else if (activeSubTab.value === 'expedition' && expeditionSection.value === 'dispatch') {
    // The runtime-settings child owns its own read; only dispatch needs this one.
    void fetchExpeditionConfig(true)
  }
}

watch(
  [panelMounted, secondaryAuthStatusReady, isReadonly, activeSubTab, opsSection, expeditionSection],
  loadVisibleAdminSection,
)

defineExpose({
  refreshClueBureau: () => clueBureauRef.value?.loadPreview(),
})
</script>

<style scoped lang="scss">
.admin-hub {
  display: grid;
  grid-template-columns: minmax(190px, 220px) minmax(0, 1fr);
  gap: 16px 18px;
  align-items: start;
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
}

.admin-auth-loading {
  padding: 28px;
  text-align: center;
  color: var(--color-text-secondary);
}

.export-archive-list {
  display: grid;
  gap: 8px;
  margin-top: 12px;
}

.export-archive-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.export-archive-row > div {
  display: grid;
  gap: 3px;
  min-width: 0;
}

.export-archive-row strong,
.export-archive-row code {
  overflow-wrap: anywhere;
}

.export-archive-row span {
  color: var(--color-text-secondary);
  font-size: 12px;
}

@media (max-width: 700px) {
  .export-archive-row {
    align-items: flex-start;
    flex-direction: column;
  }
}

.admin-hero {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  gap: 14px;
  min-width: 0;
  padding: 16px 18px;
  overflow: hidden;
  border: 1px solid var(--color-border);
  border-radius: 14px;
  background: linear-gradient(135deg, rgba(99, 102, 241, 0.13), transparent 48%), var(--color-surface, var(--color-bg));
  box-shadow: 0 8px 24px rgba(15, 23, 42, 0.06);
}

.admin-hero-mark {
  display: grid;
  flex: 0 0 46px;
  width: 46px;
  height: 46px;
  place-items: center;
  border: 1px solid var(--color-border);
  border-radius: 13px;
  background: color-mix(in srgb, var(--color-primary) 12%, var(--color-bg));
  font-size: 23px;
}

.admin-hero-copy {
  min-width: 0;

  h5 {
    margin: 1px 0 3px;
    color: var(--color-text);
    font-size: 19px;
    font-weight: 700;
  }

  > p:last-child {
    margin: 0;
    color: var(--color-text-secondary, #718096);
    font-size: 12px;
  }
}

.admin-eyebrow {
  margin: 0;
  color: var(--color-primary);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.18em;
}

.admin-current-location {
  display: flex;
  flex: 0 0 auto;
  flex-direction: column;
  gap: 2px;
  margin-left: auto;
  padding: 8px 12px;
  border-left: 2px solid var(--color-primary);
  color: var(--color-text-secondary, #718096);
  font-size: 10px;

  strong {
    color: var(--color-text);
    font-size: 13px;
  }
}

.admin-subtabs {
  display: flex;
  position: sticky;
  top: 12px;
  flex-direction: column;
  gap: 5px;
  min-width: 0;
  padding: 7px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-surface, var(--color-bg));
}

.subtab-btn {
  display: grid;
  grid-template-columns: 30px minmax(0, 1fr) auto;
  align-items: center;
  gap: 8px;
  width: 100%;
  border: 1px solid var(--color-border);
  background: var(--color-bg);
  color: var(--color-text);
  border-radius: 9px;
  padding: 9px 10px;
  font-size: 13px;
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, background 0.15s;

  &:hover {
    border-color: var(--color-primary);
  }

  &.active {
    background: color-mix(in srgb, var(--color-primary) 12%, var(--color-bg));
    border-color: var(--color-primary);
    color: var(--color-text);

    .subtab-arrow {
      color: var(--color-primary);
      transform: translateX(2px);
    }
  }
}

.subtab-icon {
  font-size: 18px;
  text-align: center;
}

.subtab-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 1px;

  strong { font-size: 13px; }

  small {
    overflow: hidden;
    color: var(--color-text-secondary, #888);
    font-size: 10px;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.subtab-arrow {
  color: var(--color-text-secondary, #888);
  font-size: 20px;
  transition: color 0.15s, transform 0.15s;
}

.subtab-panel {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 10px;
  max-width: 100%;
}

.section-subtabs {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  padding: 4px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.section-btn {
  border: none;
  background: transparent;
  color: var(--color-text-secondary, #888);
  border-radius: 6px;
  padding: 5px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.15s, color 0.15s;

  &:hover {
    background: var(--color-surface, var(--color-bg));
    color: var(--color-text);
  }

  &.active {
    background: var(--color-surface, var(--color-bg));
    color: var(--color-text);
    font-weight: 600;
    box-shadow: inset 0 0 0 1px var(--color-border);
  }
}

.section-card {
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
  background: var(--color-surface, var(--color-bg));
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 18px;
  box-shadow: 0 5px 18px rgba(15, 23, 42, 0.04);
  overflow-wrap: anywhere;
}

.section-card > :deep(*) {
  min-width: 0;
  max-width: 100%;
}

.section-card :deep(pre),
.section-card :deep(code),
.section-card :deep(dd),
.section-card :deep(td),
.section-card :deep(th) {
  overflow-wrap: anywhere;
  word-break: break-word;
}

.section-card :deep(pre) {
  max-width: 100%;
  white-space: pre-wrap;
}

.section-card :deep(input),
.section-card :deep(select),
.section-card :deep(textarea) {
  min-width: 0;
  max-width: 100%;
  box-sizing: border-box;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 10px;
}

.admin-notice-input {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
  font-size: 14px;
  resize: vertical;
}

.admin-text-input {
  width: 100%;
  box-sizing: border-box;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
  font-size: 14px;
}

.vote-admin-form,
.vote-admin-summary {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.vote-admin-summary {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--color-border);
}

.form-row {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 13px;
  color: var(--color-text-secondary, #888);
}

.vote-option-editor {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.vote-option-row {
  display: grid;
  grid-template-columns: minmax(150px, 1fr) minmax(180px, 1.4fr) auto;
  gap: 8px;
  align-items: center;
}

.vote-count-list,
.recent-vote-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.recent-vote-heading,
.recent-vote-pager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.recent-vote-heading {
  color: var(--color-text-secondary, #888);
  font-size: 11px;

  .section-title { margin-bottom: 0; }
}

.recent-vote-pager {
  justify-content: flex-end;
  padding-top: 2px;
  color: var(--color-text-secondary, #888);
  font-size: 12px;
}

.vote-count-row,
.recent-vote-row {
  display: grid;
  grid-template-columns: 1fr auto;
  gap: 10px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
}

.recent-vote-row {
  grid-template-columns: minmax(120px, 1fr) minmax(120px, 1fr) auto;
  font-size: 13px;
}

.admin-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.admin-hint {
  font-size: 12px;
  color: var(--color-text-secondary, #888);
  margin: 6px 0 0;
}

.admin-toggle-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  cursor: pointer;
  user-select: none;
}

.admin-setting-block {
  min-width: 0;
}

.admin-setting-block-separated {
  margin-top: 16px;
  padding-top: 16px;
  border-top: 1px solid var(--color-border);
}

.admin-setting-heading {
  margin: 0 0 8px;
  color: var(--color-text);
  font-size: 13px;
  font-weight: 700;
}

.admin-boundary-note,
.admin-setting-error {
  margin: 8px 0 0;
  padding: 8px 10px;
  border-radius: 7px;
  font-size: 12px;
  line-height: 1.5;
}

.admin-boundary-note {
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-warning, #f59e0b) 10%, var(--color-bg));
  border: 1px solid color-mix(in srgb, var(--color-warning, #f59e0b) 32%, var(--color-border));
}

.admin-setting-error {
  color: var(--color-danger);
  background: color-mix(in srgb, var(--color-danger) 8%, var(--color-bg));
  border: 1px solid color-mix(in srgb, var(--color-danger) 28%, var(--color-border));
}

.config-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(200px, 1fr));
  gap: 8px;
}

.config-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 6px 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  font-size: 13px;

  label {
    font-weight: 500;
    color: var(--color-text-secondary, #888);
  }
}

.link-btn {
  text-decoration: none;
  display: inline-flex;
  align-items: center;
}

.asset-link-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(160px, 1fr));
  gap: 10px;
  margin-top: 4px;
}

.asset-link-card {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 4px;
  padding: 12px 14px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
  cursor: pointer;
  text-decoration: none;
  text-align: left;
  transition: border-color 0.15s, box-shadow 0.15s;

  &:hover {
    border-color: var(--color-primary);
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
  }
}

.asset-link-icon {
  font-size: 20px;
  line-height: 1;
}

.asset-link-label {
  font-size: 14px;
  font-weight: 600;
}

.asset-link-desc {
  font-size: 11px;
  color: var(--color-text-secondary, #888);
}

@media (max-width: 720px) {
  .admin-hub {
    display: flex;
    flex-direction: column;
    gap: 10px;
  }

  .admin-hero {
    width: 100%;
    gap: 10px;
    padding: 12px;
    border-radius: 12px;
  }

  .admin-hero-mark {
    flex-basis: 38px;
    width: 38px;
    height: 38px;
    border-radius: 10px;
    font-size: 19px;
  }

  .admin-hero-copy {
    h5 { font-size: 16px; }

    > p:last-child {
      display: -webkit-box;
      overflow: hidden;
      -webkit-box-orient: vertical;
      -webkit-line-clamp: 2;
      line-height: 1.45;
    }
  }

  .admin-current-location { display: none; }

  .admin-subtabs {
    position: static;
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    width: 100%;
    gap: 6px;
    padding: 6px;
  }

  .subtab-btn {
    display: grid;
    grid-template-columns: auto minmax(0, 1fr);
    justify-content: center;
    gap: 5px;
    width: 100%;
    min-height: 44px;
    padding: 7px 6px;
    text-align: center;
  }

  .subtab-icon { font-size: 16px; }

  .subtab-copy {
    display: block;

    strong {
      display: block;
      overflow: hidden;
      font-size: 12px;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
  }

  .subtab-copy small,
  .subtab-arrow { display: none; }

  .section-subtabs {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 5px;
  }

  .section-btn {
    min-width: 0;
    min-height: 40px;
    padding: 7px 8px;
    line-height: 1.25;
  }

  .section-card {
    min-width: 0;
    padding: 10px;
  }

  .section-title { margin-bottom: 8px; }

  .admin-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));

    > button,
    > a {
      justify-content: center;
      min-width: 0;
      min-height: 40px;
      white-space: normal;
    }
  }

  .admin-toggle-row {
    align-items: flex-start;
    line-height: 1.45;

    input { margin-top: 3px; }
  }

  .config-grid { grid-template-columns: minmax(0, 1fr); }

  .config-item {
    gap: 10px;

    span {
      min-width: 0;
      overflow-wrap: anywhere;
      text-align: right;
    }
  }

  .asset-link-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }

  .asset-link-card {
    min-width: 0;
    padding: 11px;
  }

  .vote-option-row,
  .recent-vote-row {
    grid-template-columns: 1fr;
  }

  .vote-option-row .btn-secondary { min-height: 38px; }

  .recent-vote-row { gap: 4px; }

  .recent-vote-pager {
    display: grid;
    grid-template-columns: 1fr auto 1fr;

    button { min-height: 38px; }
  }
}

@media (max-width: 380px) {
  .section-subtabs { grid-template-columns: minmax(0, 1fr); }

  .admin-actions,
  .asset-link-grid { grid-template-columns: minmax(0, 1fr); }
}
</style>
