<template>
  <Teleport to="body">
  <div class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="emit('close')">
    <div class="modal-card appeal-modal s1-mobile-dialog">
      <header class="modal-head s1-mobile-dialog-header">
        <h3 class="modal-title">数据异常申诉</h3>
        <button type="button" class="modal-close" @click="emit('close')">✕</button>
      </header>

      <div class="modal-tabs">
        <button
          type="button"
          class="tab-btn"
          :class="{ active: activeTab === 'submit' }"
          @click="activeTab = 'submit'"
        >新建申诉</button>
        <button
          type="button"
          class="tab-btn"
          :class="{ active: activeTab === 'history' }"
          @click="switchToHistory"
        >
          申诉记录
          <span v-if="pendingCount > 0" class="tab-badge">{{ pendingCount }}</span>
          <span v-if="revertOfferPendingCount > 0" class="tab-badge revert-badge">{{ revertOfferPendingCount }}</span>
          <span v-if="revertRequestPendingConfirmCount > 0" class="tab-badge revert-badge">{{ revertRequestPendingConfirmCount }}</span>
        </button>
      </div>

      <div ref="appealBody" class="modal-body s1-mobile-dialog-body">
        <div
          v-if="appealQuota"
          class="quota-banner"
          :class="{ exhausted: selectedAppealQuota != null && selectedAppealQuota.remaining <= 0 }"
        >
          <div class="quota-pool-row" :class="{ active: usesFullTextQuota(appealCategory) }">
            <span>全文申诉（其他问题 / 稀有度复核）</span>
            <span class="quota-main">
              剩余 <strong>{{ appealQuota.other.unlimited ? '不限' : appealQuota.other.remaining }}</strong> 次
              <span class="quota-used muted">（已用 {{ appealQuota.other.used_24h }} / {{ appealQuota.other.unlimited ? '不限' : appealQuota.other.limit }}）</span>
            </span>
          </div>
          <div
            v-if="appealQuota.other.quota_mode === 'daily_first_balance_pool'"
            class="quota-breakdown muted"
          >
            <span>今日余 {{ appealQuota.other.today_remaining }}</span>
            <span>余额池 {{ appealQuota.other.balance_remaining }} / {{ appealQuota.other.balance_cap }}</span>
            <span>明日预计余额 {{ appealQuota.other.next_balance }}</span>
          </div>
          <div
            class="quota-pool-row"
            :class="{ active: appealCategory != null && !usesFullTextQuota(appealCategory) }"
          >
            <span>其余申诉</span>
            <span class="quota-main">
              剩余 <strong>{{ appealQuota.standard.unlimited ? '不限' : appealQuota.standard.remaining }}</strong> 次
              <span class="quota-used muted">（已用 {{ appealQuota.standard.used_24h }} / {{ appealQuota.standard.unlimited ? '不限' : appealQuota.standard.limit }}）</span>
            </span>
          </div>
          <p v-if="!appealQuota.standard.unlimited" class="quota-refresh muted">全文申诉优先使用今日次数；未用基础次数次日存入余额池，今日用完后再使用余额；其余申诉北京时间 0 点刷新</p>
        </div>

        <!-- 新建申诉 -->
        <template v-if="activeTab === 'submit'">
          <p v-if="appealCategory" class="hint">
            <template v-if="appealCategory === 'other'">
              请指出探险中已经发生、但记录遗漏或处理有误的内容；如涉及已有条目，可在下方勾选。陪审团会对照探险全文与实际记录纠正差错。
            </template>
            <template v-else-if="appealCategory === 'data_fix'">
              请勾选需要整理或纠错的条目，说明哪处记录有误。整理会保留原有效果与限制；涉及探险获得、损失或能力变化，请选择“其他问题”关联剧情。
            </template>
            <template v-else>
              请勾选需要处理的条目，并描述问题。提交后会核查申诉内容，确认无误后才会修改数据。
            </template>
          </p>
          <p v-else class="hint">请选择申诉类型。</p>

          <div class="category-section">
            <h4>申诉类型</h4>
            <div class="category-options">
              <label class="category-option" :class="{ active: appealCategory === 'data_fix' }">
                <input type="radio" value="data_fix" v-model="appealCategory">
                <span class="category-icon">📊</span>
                <span class="category-label">数据修正</span>
                <span class="category-desc">已有记录纠错与特质整理</span>
              </label>
              <label class="category-option" :class="{ active: appealCategory === 'other' }">
                <input type="radio" value="other" v-model="appealCategory">
                <span class="category-icon">⚔️</span>
                <span class="category-label">其他问题</span>
                <span class="category-desc">探险漏记、误扣与结算差错</span>
              </label>
              <label class="category-option" :class="{ active: appealCategory === 'rarity_review' }">
                <input type="radio" value="rarity_review" v-model="appealCategory">
                <span class="category-icon">🔎</span>
                <span class="category-label">稀有度复核</span>
                <span class="category-desc">副本掉落原物，最多 5 件</span>
              </label>
              <button
                type="button"
                class="category-option category-action"
                @click="emit('open-relief')"
              >
                <span class="category-icon">🍺</span>
                <span class="category-label">酒馆补贴申请</span>
                <span class="category-desc">更容易通过，补贴更高</span>
              </button>
              <button
                type="button"
                class="category-option category-action"
                @click="emit('open-projects')"
              >
                <span class="category-icon">📜</span>
                <span class="category-label">申诉专项项目</span>
                <span class="category-desc">装备自检 / 嬗变漏退 / 商行误售</span>
              </button>
            </div>
          </div>

          <template v-if="appealCategory">
            <div v-if="loading" class="state">加载可申诉条目…</div>
            <div v-else-if="loadError" class="state error">{{ loadError }}</div>

            <template v-else-if="targets">
            <div v-if="needsEvidence && latestExpedition && latestExpeditionFullText" class="evidence-section">
              <div class="evidence-header">
                <h4>上次探险全文证据</h4>
              </div>
              <p class="evidence-meta muted small">
                {{ latestExpedition.target_dungeon }}
                <span v-if="latestExpedition.party_name"> · {{ latestExpedition.party_name }}</span>
                <span v-if="latestExpedition.created_at"> · {{ formatTime(latestExpedition.created_at) }}</span>
              </p>
              <p class="evidence-summary muted small">
                系统会默认读取上次已完成探险全文，不再需要手动选择段落。
              </p>
            </div>
            <div v-else-if="needsEvidence && latestExpedition?.events?.length" class="evidence-section">
              <div class="evidence-header">
                <h4>{{ appealCategory === 'rarity_review' ? '选择上次探险段落（必选）' : '关联探险段落（可选）' }}</h4>
                <button
                  type="button"
                  class="evidence-toggle"
                  @click="evidenceExpanded = !evidenceExpanded"
                >
                  {{ evidenceExpanded ? '收起' : '展开选择' }}
                </button>
              </div>
              <p class="evidence-meta muted small">
                {{ latestExpedition.target_dungeon }}
                <span v-if="latestExpedition.party_name"> · {{ latestExpedition.party_name }}</span>
                <span v-if="latestExpedition.created_at"> · {{ formatTime(latestExpedition.created_at) }}</span>
              </p>
              <p v-if="!evidenceExpanded" class="evidence-summary muted small">
                {{ evidenceSelectionSummary }}
              </p>
              <div v-show="evidenceExpanded" class="evidence-options">
                <label v-if="appealCategory !== 'rarity_review'" class="evidence-row evidence-none">
                  <input type="radio" :value="null" v-model="selectedEventIndex">
                  <span>不关联探险段落</span>
                </label>
                <label
                  v-for="ev in latestExpedition.events"
                  :key="'ev-' + ev.index"
                  class="evidence-row"
                >
                  <input type="radio" :value="ev.index" v-model="selectedEventIndex">
                  <span class="evidence-index">第 {{ ev.index + 1 }} 段</span>
                  <span class="evidence-text">{{ ev.preview || ev.text }}</span>
                </label>
              </div>
            </div>
            <div v-else-if="appealCategory === 'rarity_review'" class="evidence-section">
              <p class="evidence-summary muted small warn">
                暂无上次已完成探险，不能提交副本掉落稀有度复核。
              </p>
            </div>
            <div v-if="totalSelectable" class="target-toolbar">
              <label class="target-search">
                <span>查找条目</span>
                <input v-model="targetSearch" type="search" placeholder="搜索名称或描述" aria-label="搜索可申诉条目的名称或描述">
              </label>
              <div class="target-toolbar-actions">
                <span class="muted small">已选 {{ checkedKeys.length }} 项</span>
                <button type="button" class="evidence-toggle" @click="setTargetSectionsCollapsed(false)">全部展开</button>
                <button type="button" class="evidence-toggle" @click="setTargetSectionsCollapsed(true)">全部收起</button>
                <button type="button" class="evidence-toggle" @click="jumpToAppealSection('appeal-submit-section')">填写与提交</button>
              </div>
              <nav class="target-navigation" aria-label="申诉条目分组跳转">
                <button v-for="sec in visibleSections" :key="sec.key" type="button" class="evidence-toggle" @click="jumpToTargetSection(sec.key)">
                  {{ sec.label }}（{{ sec.items.length }}）
                </button>
              </nav>
            </div>
            <p v-if="targetSearch.trim() && totalSelectable && !visibleSections.length" class="hint muted small">没有匹配的条目，可更换关键词；已勾选的条目仍会保留。</p>
            <div class="target-section" v-for="sec in visibleSections" :key="sec.key" :id="'appeal-target-' + sec.key">
              <h4 class="target-section-heading">
                <span>{{ sec.label }}（{{ sec.items.length }}）</span>
                <button type="button" class="evidence-toggle" :aria-expanded="!collapsedTargetSections[sec.key]" :aria-controls="'appeal-target-content-' + sec.key" @click="collapsedTargetSections[sec.key] = !collapsedTargetSections[sec.key]">
                  {{ collapsedTargetSections[sec.key] ? '展开' : '收起' }}
                </button>
              </h4>
              <div v-show="!collapsedTargetSections[sec.key]" :id="'appeal-target-content-' + sec.key">
              <label
                v-for="item in sec.items"
                :key="sec.type + '-' + item.id"
                class="target-row"
              >
                <input
                  type="checkbox"
                  :value="sec.type + ':' + item.id"
                  v-model="checkedKeys"
                  :disabled="isTargetDisabled(sec.type, item.id)"
                >
                <span class="target-name">{{ item.name }}</span>
                <span v-if="item.quantity > 1" class="target-quantity">×{{ item.quantity }}</span>
                <span v-if="item.rarity" class="target-rarity">{{ item.rarity }}</span>
                <span v-if="itemDescription(item)" class="target-desc">
                  {{ itemDescription(item) }}
                </span>
              </label>
              </div>
            </div>

            <p v-if="totalSelectable === 0 && appealCategory === 'other'" class="hint muted small">
              当前没有可选条目，可直接填写问题描述提交。
            </p>
            <div
              v-else-if="totalSelectable === 0"
              class="state"
            >暂无可申诉的条目</div>

            <div id="appeal-submit-section"></div>
            <p v-if="appealCategory === 'rarity_review'" class="rarity-review-note muted small">
              {{ latestExpeditionFullText
                ? '系统会忽略玩家描述，默认读取上次探险全文，并根据物品名称/描述/当前稀有度重新鉴定。'
                : '系统会忽略玩家描述，仅根据所选上次探险段落和物品名称/描述/当前稀有度重新鉴定。' }}可选物品包括未交易/未锻造/未消耗/未被申诉改动的副本掉落物（任意稀有度），以及副本内强化后稀有度结算异常的随身装备。堆叠物品按整堆复核，数量不会改变。
            </p>

            <div v-else class="note-block">
              <label>问题描述 <span class="req">*</span></label>
              <textarea
                v-model="playerNote"
                rows="4"
                maxlength="200"
                placeholder="例如：装备名后面混进了一大段描述文字…"
              />
              <small class="muted">{{ playerNote.length }}/200</small>
            </div>

            <div v-if="submitError" class="submit-error">{{ submitError }}</div>
            <div v-else-if="quotaExhausted" class="submit-error">
              今日此类申诉次数已用完，请明日 0 点后再试。
            </div>

            <div class="actions">
              <button type="button" class="btn-secondary" @click="emit('close')">取消</button>
              <button
                type="button"
                class="btn-primary"
                :disabled="submitDisabled"
                @click="submit"
              >
                {{ submitting ? '提交中…' : '提交申诉' }}
              </button>
            </div>
            </template>
          </template>
        </template>

        <!-- 申诉记录 -->
        <template v-else>
          <div v-if="historyLoading && !historyLoaded" class="state">加载申诉记录…</div>
          <div v-else-if="historyError && !historyLoaded" class="state error">{{ historyError }}</div>
          <div v-else-if="appeals.length === 0" class="state">暂无申诉记录</div>
          <ul v-else class="history-list">
            <li v-for="a in appeals" :key="a.id" class="history-item">
              <div class="history-top">
                <span class="history-id">#{{ a.id }}</span>
                <span v-if="a.appeal_category === 'tavern_relief'" class="relief-history-tag">
                  酒馆补贴申请
                </span>
                <span class="history-status" :class="historyStatusClass(a)">{{ statusLabel(a) }}</span>
                <span class="history-time">{{ formatTime(a.created_at) }}</span>
              </div>
              <div v-if="a.target_labels?.length" class="history-targets">
                涉及：{{ a.target_labels.join('、') }}
              </div>
              <div v-if="a.expedition_evidence" class="history-evidence muted small">
                关联探险：{{ a.expedition_evidence.target_dungeon || '未知' }}
                <template v-if="a.expedition_evidence.event_text">
                  第 {{ (a.expedition_evidence.event_index ?? 0) + 1 }} 段
                </template>
                <template v-else>全文</template>
              </div>
              <p class="history-note">{{ a.player_note }}</p>
              <div
                v-if="a.first_review_reason || a.second_review_reason"
                class="review-reasons"
              >
                <p v-if="a.first_review_reason" class="review-reason">
                  <strong>陪审团 1 号回复：</strong>{{ a.first_review_reason }}
                </p>
                <div v-if="a.first_review_reason && a.ai_feedback?.first_review" class="review-feedback">
                  <AiFeedbackControl
                    :target="a.ai_feedback.first_review"
                    @updated="a.ai_feedback.first_review = $event"
                  />
                </div>
                <p v-if="a.second_review_reason" class="review-reason">
                  <strong>陪审团 2 号回复：</strong>{{ a.second_review_reason }}
                </p>
                <div v-if="a.second_review_reason && a.ai_feedback?.second_review" class="review-feedback">
                  <AiFeedbackControl
                    :target="a.ai_feedback.second_review"
                    @updated="a.ai_feedback.second_review = $event"
                  />
                </div>
              </div>
              <p
                v-if="a.status !== 'rejected'
                  && a.status !== 'cancelled'
                  && a.player_final_reason
                  && a.player_final_reason !== a.first_review_reason
                  && a.player_final_reason !== a.second_review_reason"
                class="history-reply"
              >
                {{ a.player_final_reason }}
              </p>
              <div v-if="a.request_checklist?.length" class="history-checklist">
                <p class="checklist-title muted small">逐条处理结果</p>
                <ul class="checklist-items">
                  <li
                    v-for="(row, idx) in a.request_checklist"
                    :key="idx"
                    :class="'checklist-' + effectiveChecklistStatus(a, row.status)"
                  >
                    <span class="checklist-label">{{ checklistStatusLabel(effectiveChecklistStatus(a, row.status)) }}：{{ row.label }}</span>
                    <span v-if="checklistOutcomeLabel(row.outcome)" class="checklist-detail muted small">（{{ checklistOutcomeLabel(row.outcome) }}）</span>
                    <span v-if="row.detail" class="checklist-detail muted small">（{{ row.detail }}）</span>
                  </li>
                </ul>
              </div>
              <p
                v-if="a.status === 'rejected' && a.reject_reason && isWithdrawalOutcome(a)"
                class="history-withdrawn muted small"
              >
                撤回说明：{{ a.reject_reason }}
              </p>
              <p
                v-else-if="a.status === 'rejected' && a.reject_reason"
                class="history-reject"
              >
                {{ a.reject_reason_label || '驳回原因' }}：{{ a.reject_reason }}
              </p>
              <p
                v-if="a.status === 'cancelled' && isPlayerCancelled(a)"
                class="history-cancelled muted small"
              >
                {{ a.outcome_hint || '玩家在核查完成前取消了本次申诉，未产生数据变更。' }}
              </p>
              <p
                v-else-if="a.status === 'cancelled' && a.reject_reason"
                class="history-withdrawn muted small"
              >
                撤回说明：{{ a.reject_reason }}
              </p>
              <p v-if="a.processing_outcome" class="muted small">{{ a.outcome_hint }}</p>
              <div v-if="a.can_continue" class="history-actions-inline">
                <button type="button" class="btn-primary btn-xs"
                  :disabled="continuingId === a.id" @click="continueAppeal(a.id)">
                  {{ continuingId === a.id ? '正在提交…' : '继续处理原申诉' }}
                </button>
              </div>
              <p v-if="a.status === 'processing'" class="processing-hint muted small">
              核查通常需 1～3 分钟；若长时间停在「排队」，可点「取消申诉」后重新提交。
            </p>
            <div v-if="a.status === 'processing'" class="history-actions-inline">
                <button
                  type="button"
                  class="btn-danger btn-xs"
                  :disabled="cancellingId === a.id"
                  @click="cancelAppeal(a.id)"
                >取消申诉</button>
              </div>
              <p
                v-if="a.appeal_category === 'tavern_relief' && a.status === 'approved' && a.applied_at"
                class="history-done muted small"
              >
                已于 {{ formatTime(a.applied_at) }} 完成发放
                <template v-if="a.tavern_relief_result">
                  · {{ a.tavern_relief_result.recipient_count }} 人
                  · 共 {{ a.tavern_relief_result.grant_total }} 金币
                  <template v-if="(a.tavern_relief_result.applicant_bonus_grant || 0) > 0">
                    （个人 +{{ a.tavern_relief_result.applicant_bonus_grant }}）
                  </template>
                </template>
              </p>
              <p
                v-else-if="a.status === 'approved' && a.applied_at && a.core_request_satisfied === false"
                class="history-partial muted small"
              >
                已于 {{ formatTime(a.applied_at) }} 处理：仅做了部分数据整理，您申诉的核心诉求未完全满足
              </p>
              <p
                v-else-if="a.status === 'approved' && a.applied_at"
                class="history-done muted small"
              >
                已于 {{ formatTime(a.applied_at) }} 完成修复
              </p>
              <p v-if="a.player_review_status === 'pending'" class="ai-pending muted small">
                正在进行独立复核，结果将自动显示…
              </p>
              <div v-if="a.player_review_result" class="ai-review-result-display">
                <span :class="['review-decision', a.player_review_result.decision]">
                  {{ a.player_review_result.decision === 'rejected' ? '复核驳回'
                   : a.player_review_result.decision === 'error' ? '复核异常'
                   : '复核意见' }}
                </span>
                <span class="review-message">{{ a.player_review_result.message }}</span>
              </div>
              <div v-if="a.revert_offer_pending" class="revert-offer-box">
                <p class="revert-offer-hint">
                  经核查，本次申诉修复可能存在误删。若您同意，系统将根据变更记录尽力还原到修复前；若数据无误可拒绝。
                </p>
                <p v-if="a.revert_offer_note" class="revert-offer-admin-note muted small">
                  管理员说明：{{ a.revert_offer_note }}
                </p>
                <div class="history-actions-inline">
                  <button
                    type="button"
                    class="btn-danger btn-xs"
                    :disabled="revertOfferLoadingId === a.id"
                    @click="acceptRevertOffer(a.id)"
                  >同意撤销修复</button>
                  <button
                    type="button"
                    class="btn-secondary btn-xs"
                    :disabled="revertOfferLoadingId === a.id"
                    @click="declineRevertOffer(a.id)"
                  >拒绝</button>
                </div>
              </div>
              <p v-else-if="a.revert_offer_status === 'declined'" class="history-reject muted small">
                您已拒绝撤销本次申诉修复
              </p>
              <p v-if="a.player_revert_request_status === 'processing'" class="ai-pending muted small">
                撤回申请正在核查，结果将自动显示…
              </p>
              <div v-if="a.player_revert_request_pending_confirm" class="revert-offer-box revert-request-box">
                <p class="revert-offer-hint">
                  撤回申请已通过。确认后将根据变更记录尽力还原到修改前；若数据其实无误可放弃。
                </p>
                <p v-if="a.player_revert_request_ai_summary" class="revert-offer-admin-note muted small">
                  核查说明：{{ a.player_revert_request_ai_summary }}
                </p>
                <div class="history-actions-inline">
                  <button
                    type="button"
                    class="btn-danger btn-xs"
                    :disabled="revertRequestLoadingId === a.id"
                    @click="confirmRevertRequest(a.id)"
                  >确认撤回修复</button>
                  <button
                    type="button"
                    class="btn-secondary btn-xs"
                    :disabled="revertRequestLoadingId === a.id"
                    @click="declineRevertRequest(a.id)"
                  >放弃撤回</button>
                </div>
              </div>
              <p
                v-if="a.player_revert_request_status === 'rejected' && a.player_revert_request_ai_summary"
                class="history-reject muted small"
              >
                撤回申请未通过：{{ a.player_revert_request_ai_summary }}
              </p>
              <p v-else-if="a.player_revert_request_status === 'declined'" class="history-reject muted small">
                您已放弃撤回此次申诉修复
              </p>
              <template v-if="a.can_request_player_revert && revertRequestDraftId !== a.id">
                <p
                  v-if="a.status === 'rejected' && a.player_revert_request_status !== 'rejected'"
                  class="history-partial muted small"
                >
                  申诉虽已驳回，但部分修改已经生效；若认为这些改动不合理，可申请撤回。
                </p>
                <p
                  v-else-if="a.player_revert_request_status === 'rejected'"
                  class="history-partial muted small"
                >
                  可补充说明后重新申请撤回。
                </p>
                <div class="history-actions-inline">
                  <button
                    type="button"
                    class="btn-secondary btn-xs"
                    @click="openRevertRequestDraft(a.id)"
                  >{{
                    a.player_revert_request_status === 'rejected'
                      ? '重新申请撤回'
                      : (a.status === 'rejected' ? '申请撤回已生效修改' : '申请撤回此次修复')
                  }}</button>
                </div>
              </template>
              <div v-else-if="revertRequestDraftId === a.id" class="revert-request-form">
                <label class="small">撤回理由（将消耗 1 次申诉额度）</label>
                <textarea
                  v-model="revertRequestDraftNote"
                  rows="3"
                  maxlength="200"
                  :placeholder="a.status === 'rejected'
                    ? '说明为何认为已经生效的修改应撤销，例如误删物品、合并不合理等'
                    : '说明为何认为此次修复应撤销，例如误删物品、合并不合理等'"
                />
                <div class="history-actions-inline">
                  <button
                    type="button"
                    class="btn-danger btn-xs"
                    :disabled="revertRequestLoadingId === a.id || !revertRequestDraftNote.trim() || standardQuotaExhausted"
                    @click="submitRevertRequest(a.id)"
                  >提交撤回申请</button>
                  <button
                    type="button"
                    class="btn-secondary btn-xs"
                    :disabled="revertRequestLoadingId === a.id"
                    @click="cancelRevertRequestDraft"
                  >取消</button>
                </div>
              </div>
              <p v-else-if="a.status === 'reverted'" class="history-done muted small">
                已于 {{ formatTime(a.reverted_at) }} 撤销修复并还原数据
              </p>
            </li>
          </ul>
          <p v-if="historyError && historyLoaded" class="error small" role="status">{{ historyError }}</p>
          <div class="actions history-actions">
            <button type="button" class="btn-secondary btn-sm" :disabled="historyLoading" @click="loadHistory">
              刷新
            </button>
          </div>
        </template>
      </div>
    </div>
  </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, nextTick, onMounted, onUnmounted, watch } from 'vue'
import { useAdventurerS1Store, type AiFeedbackTarget } from '../stores/adventurerS1'
import AiFeedbackControl from './AiFeedbackControl.vue'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatDateTime } from '@/utils/time'
import { useSocket } from '@/composables/useSocket'
import { useUserStore } from '@/stores/user'
import { useS1AppealRefresh } from '../composables/useS1AppealRefresh'

interface AppealQuotaState {
  remaining: number
  limit: number
  unlimited?: boolean
  used_24h: number
  quota_mode?: 'daily_first_balance_pool'
  today_remaining?: number
  balance_remaining?: number
  balance_cap?: number
  next_balance?: number
}

interface AppealQuota {
  other: AppealQuotaState
  standard: AppealQuotaState
}

type AppealCategory = 'data_fix' | 'other' | 'rarity_review'

interface DataAppealRecord {
  id: number
  ai_feedback?: {
    first_review?: AiFeedbackTarget
    second_review?: AiFeedbackTarget
  }
  player_note: string
  appeal_category?: string
  target_labels?: string[]
  expedition_evidence?: {
    expedition_log_id?: number
    event_index?: number
    auto_fallback?: boolean
    target_dungeon?: string
    party_name?: string
    event_text?: string
  } | null
  ai_status?: string
  player_review_status?: string
  player_review_result?: {
    decision?: string
    message?: string
    created_at?: string | null
  } | null
  status: string
  status_label?: string
  processing_outcome?: 'technical_failure' | 'execution_blocked' | null
  can_continue?: boolean
  cancellation_kind?: string
  outcome_hint?: string
  reject_reason_label?: string
  created_at?: string | null
  reviewed_at?: string | null
  reject_reason?: string | null
  first_review_reason?: string | null
  second_review_reason?: string | null
  player_final_reason?: string | null
  applied_at?: string | null
  core_request_satisfied?: boolean | null
  tavern_relief_result?: {
    decision?: 'approve' | 'reject'
    target_per_person?: number
    max_target_per_person?: number
    payout_per_person?: number
    recipient_count?: number
    team_target_total?: number
    team_grant_total?: number
    applicant_bonus_target?: number
    max_applicant_bonus_target?: number
    applicant_bonus_grant?: number
    grant_total?: number
    already_covered?: boolean
    message?: string
  } | null
  request_checklist?: Array<{
    label: string
    status?: string
    category?: string
    is_core?: boolean
    outcome?: string
    detail?: string
  }>
  revert_offer_status?: string
  revert_offer_pending?: boolean
  revert_offer_note?: string | null
  player_revert_request_status?: string
  player_revert_request_pending_confirm?: boolean
  player_revert_request_ai_summary?: string | null
  can_request_player_revert?: boolean
  reverted_at?: string | null
}

interface LatestExpeditionEvidence {
  log_id: number
  target_dungeon: string
  party_name?: string | null
  status: string
  created_at?: string | null
  full_text_appeal_enabled?: boolean
  events: Array<{ index: number; text: string; preview?: string }>
}

const props = withDefaults(defineProps<{
  initialTab?: 'submit' | 'history'
}>(), {
  initialTab: 'submit',
})

const emit = defineEmits<{
  close: []
  submitted: []
  'open-relief': []
  'open-projects': []
}>()
const store = useAdventurerS1Store()
const message = useGlobalMessage()
const socket = useSocket()
const userStore = useUserStore()

const activeTab = ref<'submit' | 'history'>(props.initialTab)
const loading = ref(false)
const loadError = ref('')
const targets = ref<any>(null)
const appealQuota = ref<AppealQuota | null>(null)
const checkedKeys = ref<string[]>([])
const appealBody = ref<HTMLElement | null>(null)
const targetSearch = ref('')
const collapsedTargetSections = ref<Record<string, boolean>>({})
const appealCategory = ref<AppealCategory | null>(null)
const selectedEventIndex = ref<number | null>(null)
const evidenceExpanded = ref(false)
const playerNote = ref('')
const submitting = ref(false)
const submitError = ref('')

const { refreshing: historyLoading, refresh: loadHistory } = useS1AppealRefresh(fetchHistorySnapshot)
const historyLoaded = ref(false)
const historyError = ref('')
const revertOfferLoadingId = ref<number | null>(null)
const revertRequestLoadingId = ref<number | null>(null)
const revertRequestDraftId = ref<number | null>(null)
const revertRequestDraftNote = ref('')
const appeals = ref<DataAppealRecord[]>([])
const cancellingId = ref<number | null>(null)

const sections = computed(() => {
  const t = targets.value
  if (!t) return []
  const base = [
    { key: 'traits', type: 'trait', label: '特质', items: t.traits || [] },
    { key: 'equipment', type: 'equipment', label: '随身装备', items: t.equipment || [] },
    { key: 'warehouse', type: 'warehouse', label: '收藏室', items: t.warehouse || [] },
    { key: 'skills', type: 'skill', label: '技能', items: t.skills || [] },
  ]
  if (appealCategory.value === 'rarity_review') {
    return base
      .filter(s => s.type === 'equipment' || s.type === 'warehouse')
      .map(s => ({
        ...s,
        items: s.items.filter((item: any) => item.rarity_review_eligible === true),
      }))
      .filter(s => s.items.length > 0)
  }
  return base.filter(s => s.items.length > 0)
})

const totalSelectable = computed(() =>
  sections.value.reduce((n, s) => n + s.items.length, 0),
)

const visibleSections = computed(() => {
  const query = targetSearch.value.trim().toLocaleLowerCase()
  if (!query) return sections.value
  return sections.value.map(sec => ({
    ...sec,
    items: sec.items.filter((item: any) => (
      [item.name, itemDescription(item)].some(text => String(text ?? '').toLocaleLowerCase().includes(query))
    )),
  })).filter(sec => sec.items.length > 0)
})

function setTargetSectionsCollapsed(collapsed: boolean) {
  collapsedTargetSections.value = Object.fromEntries(sections.value.map(sec => [sec.key, collapsed]))
}

async function jumpToAppealSection(id: string) {
  await nextTick()
  appealBody.value?.querySelector<HTMLElement>(`#${id}`)?.scrollIntoView({ block: 'start', behavior: 'auto' })
}

function jumpToTargetSection(key: string) {
  collapsedTargetSections.value[key] = false
  void jumpToAppealSection(`appeal-target-${key}`)
}

const latestExpedition = computed<LatestExpeditionEvidence | null>(() =>
  targets.value?.latest_expedition || null,
)

const latestExpeditionFullText = computed(() =>
  latestExpedition.value?.full_text_appeal_enabled === true,
)

const evidenceSelectionSummary = computed(() => {
  if (selectedEventIndex.value === null) {
    return '当前：不关联探险段落'
  }
  const ev = latestExpedition.value?.events?.find(e => e.index === selectedEventIndex.value)
  const idx = selectedEventIndex.value + 1
  if (!ev) return `当前：已选第 ${idx} 段`
  const preview = truncate(ev.preview || ev.text, 72)
  return `当前：已选第 ${idx} 段 · ${preview}`
})

const pendingCount = computed(() =>
  appeals.value.filter(a => a.status === 'processing' || a.status === 'awaiting_review').length,
)

const revertOfferPendingCount = computed(() =>
  appeals.value.filter(a => a.revert_offer_pending).length,
)

const revertRequestPendingConfirmCount = computed(() =>
  appeals.value.filter(a => a.player_revert_request_pending_confirm).length,
)

function usesFullTextQuota(category: AppealCategory | null): boolean {
  return category === 'other' || category === 'rarity_review'
}

const selectedAppealQuota = computed<AppealQuotaState | null>(() => {
  if (!appealQuota.value || !appealCategory.value) return null
  return usesFullTextQuota(appealCategory.value)
    ? appealQuota.value.other
    : appealQuota.value.standard
})

const quotaExhausted = computed(() =>
  selectedAppealQuota.value != null && selectedAppealQuota.value.remaining <= 0,
)

const standardQuotaExhausted = computed(() =>
  appealQuota.value != null && appealQuota.value.standard.remaining <= 0,
)

const needsEvidence = computed(() =>
  appealCategory.value === 'other' || appealCategory.value === 'rarity_review',
)

const requiresSelectedTargets = computed(() => appealCategory.value !== 'other')

const submitDisabled = computed(() => {
  if (!appealCategory.value) return true
  if (submitting.value || quotaExhausted.value) return true
  if (
    requiresSelectedTargets.value
    && checkedKeys.value.length === 0
  ) return true
  if (appealCategory.value === 'rarity_review') {
    if (checkedKeys.value.length > 5) return true
    return latestExpeditionFullText.value ? !latestExpedition.value : selectedEventIndex.value === null
  }
  if (appealCategory.value === 'other' && latestExpeditionFullText.value && !latestExpedition.value) return true
  return !playerNote.value.trim()
})

const hasProcessingAppeal = computed(() =>
  appeals.value.some(a =>
    a.status === 'processing'
    || a.player_revert_request_status === 'processing',
  ),
)

let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function startPolling() {
  if (pollTimer || !hasProcessingAppeal.value) return
  pollTimer = setInterval(() => {
    if (!historyLoading.value) loadHistory()
  }, 5000)
}

function onDataAppealChanged(payload: { user_id?: number }) {
  if (payload?.user_id && payload.user_id !== userStore.user?.id) return
  loadHistory()
}

function truncate(s: string, max = 80) {
  if (!s) return ''
  return s.length > max ? s.slice(0, max) + '…' : s
}

function apiErrorMessage(error: unknown, fallback: string) {
  const err = error as { response?: { data?: { message?: string } } }
  return err.response?.data?.message || fallback
}

function itemDescription(item: any) {
  return item?.description || item?.desc || item?.custom_desc || ''
}

function formatTime(iso: string | null | undefined) {
  if (!iso) return '-'
  return formatDateTime(iso)
}

function isTargetDisabled(type: string, id: number) {
  if (appealCategory.value !== 'rarity_review') return false
  const key = `${type}:${id}`
  return checkedKeys.value.length >= 5 && !checkedKeys.value.includes(key)
}

function isPlayerCancelled(a: DataAppealRecord) {
  return a.status === 'cancelled' && (
    a.cancellation_kind === 'player'
    || a.cancellation_kind === 'admin'
    || a.reject_reason === '玩家已取消'
    || (a.reject_reason || '').startsWith('管理员')
  )
}

function isWithdrawalOutcome(a: DataAppealRecord) {
  if (a.processing_outcome) return false
  const text = `${a.reject_reason || ''}${a.player_final_reason || ''}${a.player_note || ''}`
  return /撤回|取消申诉|无需.*数据修改|无数据修改/.test(text)
}

function historyStatusClass(a: DataAppealRecord) {
  if (a.processing_outcome) return 'st-approved-partial'
  if (a.status === 'approved' && a.core_request_satisfied === false) {
    return 'st-approved-partial'
  }
  if (isPlayerCancelled(a)) {
    return 'st-cancelled-player'
  }
  if (a.status === 'cancelled' || (a.status === 'rejected' && isWithdrawalOutcome(a))) {
    return 'st-cancelled'
  }
  return 'st-' + a.status
}

function statusLabel(a: DataAppealRecord) {
  if (a.status_label) return a.status_label
  if (a.appeal_category === 'tavern_relief' && a.status === 'approved') {
    return '已发放'
  }
  if (a.status === 'processing') {
    if (a.ai_status === 'pending' || a.ai_status === 'processing') return '核查中'
    if (a.ai_status === 'failed') return '自动审核未通过'
    return '处理中'
  }
  if (a.status === 'approved' && a.core_request_satisfied === false) {
    return '已处理（核心未满足）'
  }
  if (isPlayerCancelled(a)) {
    return '已取消'
  }
  if (a.status === 'cancelled' || (a.status === 'rejected' && isWithdrawalOutcome(a))) {
    return '已撤回'
  }
  const map: Record<string, string> = {
    awaiting_review: '等待审核',
    approved: '已通过',
    rejected: '已驳回',
    cancelled: '已取消',
    reverted: '已撤销修复',
  }
  return map[a.status] || a.status
}

function checklistStatusLabel(status?: string) {
  const map: Record<string, string> = {
    done: '✓ 已处理',
    partial: '△ 部分',
    rejected: '✗ 未通过',
    pending: '· 待核对',
  }
  return map[status || 'pending'] || '· 待核对'
}

function checklistOutcomeLabel(outcome?: string) {
  const map: Record<string, string> = {
    already_satisfied: '当前数据已满足，无需重复写入',
    conflict: '最终执行条件冲突，未写入',
    stale_target: '本项未执行，请查看具体说明',
    partial_stale_target: '部分项目未执行，请查看具体说明',
    review_unavailable: '本轮未完成核对',
  }
  return map[outcome || ''] || ''
}

/** 终态 rejected 时，不把 AI 误标的 done 展示成打勾（后端已修，前端兜底）。 */
function effectiveChecklistStatus(appeal: DataAppealRecord, status?: string) {
  const s = status || 'pending'
  if (!appeal.processing_outcome && appeal.status === 'rejected' && (s === 'done' || s === 'partial')) {
    return 'rejected'
  }
  return s
}

function normalizeAppealQuota(raw: any): AppealQuota | null {
  if (!raw) return null
  if (raw.other && raw.standard) return raw as AppealQuota
  if (
    Number.isFinite(Number(raw.remaining))
    && Number.isFinite(Number(raw.limit))
    && Number.isFinite(Number(raw.used_24h))
  ) {
    const legacy = {
      remaining: Number(raw.remaining),
      limit: Number(raw.limit),
      used_24h: Number(raw.used_24h),
    }
    return { other: legacy, standard: { ...legacy } }
  }
  return null
}

async function loadTargets() {
  loading.value = true
  loadError.value = ''
  try {
    const res = await store.fetchDataAppealTargets()
    if (res?.code === 200) {
      targets.value = res.data
      appealQuota.value = normalizeAppealQuota(res.data?.quota)
      selectedEventIndex.value = null
    } else {
      loadError.value = res?.message || '加载失败'
    }
  } catch {
    loadError.value = '加载失败，请稍后重试'
  } finally {
    loading.value = false
  }
}

async function fetchHistorySnapshot(isActive: () => boolean) {
  try {
    const res = await store.fetchMyDataAppeals(30)
    if (!isActive()) return
    if (res?.code === 200) {
      appeals.value = res.data || []
      historyLoaded.value = true
      historyError.value = ''
      if (hasProcessingAppeal.value) startPolling()
      else stopPolling()
    } else {
      historyError.value = res?.message || '加载失败'
    }
  } catch {
    if (isActive()) historyError.value = '加载失败，请稍后重试'
  }
}

function switchToHistory() {
  activeTab.value = 'history'
  if (!historyLoaded.value && !historyLoading.value) {
    loadHistory()
  }
}

onMounted(() => {
  socket.connect()
  socket.on('data_appeal_changed', onDataAppealChanged)
  void loadHistory()
})

onUnmounted(() => {
  stopPolling()
  socket.off('data_appeal_changed', onDataAppealChanged)
})

watch(hasProcessingAppeal, (processing) => {
  if (processing) startPolling()
  else stopPolling()
})

watch(appealCategory, (category) => {
  checkedKeys.value = []
  targetSearch.value = ''
  collapsedTargetSections.value = {}
  submitError.value = ''
  if (category && !targets.value && !loading.value) {
    void loadTargets()
  }
  if (category === 'rarity_review') {
    playerNote.value = ''
    evidenceExpanded.value = true
  } else {
    selectedEventIndex.value = null
  }
})

const continuingId = ref<number | null>(null)

async function continueAppeal(id: number) {
  continuingId.value = id
  try {
    const res = await store.continueDataAppeal(id)
    if (res?.code === 200) {
      message?.success('已继续处理原申诉')
    } else {
      message?.error(res?.message || '暂时无法继续处理')
    }
  } catch (error: unknown) {
    message?.error(apiErrorMessage(error, '暂时无法继续处理'))
  } finally {
    continuingId.value = null
    await loadHistory()
  }
}

async function cancelAppeal(id: number) {
  if (!confirm('确定取消该申诉？取消后可重新提交。')) return
  cancellingId.value = id
  try {
    const res = await store.cancelDataAppeal(id)
    if (res?.code === 200) {
      message?.success('申诉已取消')
      await loadHistory()
    } else {
      message?.error(res?.message || '取消失败')
    }
  } catch (error: unknown) {
    message?.error(apiErrorMessage(error, '取消失败'))
  } finally {
    cancellingId.value = null
  }
}

async function acceptRevertOffer(id: number) {
  if (!confirm(
    '确认同意撤销本次申诉的数据修复？系统将根据变更记录尽力还原到修复前，此操作不可自动撤销。',
  )) return
  revertOfferLoadingId.value = id
  try {
    const res = await store.acceptDataAppealRevertOffer(id)
    if (res?.code === 200) {
      message?.success('已撤销修复并完成数据还原')
      await loadHistory()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } catch (error: unknown) {
    message?.error(apiErrorMessage(error, '操作失败'))
  } finally {
    revertOfferLoadingId.value = null
  }
}

async function declineRevertOffer(id: number) {
  if (!confirm('确认拒绝撤销邀请？本次申诉修复将保持不变。')) return
  revertOfferLoadingId.value = id
  try {
    const res = await store.declineDataAppealRevertOffer(id)
    if (res?.code === 200) {
      message?.success('已拒绝撤销邀请')
      await loadHistory()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } catch (error: unknown) {
    message?.error(apiErrorMessage(error, '操作失败'))
  } finally {
    revertOfferLoadingId.value = null
  }
}

function openRevertRequestDraft(id: number) {
  revertRequestDraftId.value = id
  revertRequestDraftNote.value = ''
}

function cancelRevertRequestDraft() {
  revertRequestDraftId.value = null
  revertRequestDraftNote.value = ''
}

async function submitRevertRequest(id: number) {
  if (revertRequestLoadingId.value !== null) return
  const note = revertRequestDraftNote.value.trim()
  if (!note) {
    message?.error('请填写撤回理由')
    return
  }
  if (!confirm('提交后将消耗 1 次申诉额度，并核查是否允许撤回。确定提交？')) return
  revertRequestLoadingId.value = id
  try {
    const res = await store.requestDataAppealRevert(id, note)
    if (res?.code === 200) {
      message?.success(res.message || '撤回申请已提交')
      cancelRevertRequestDraft()
      await Promise.all([loadHistory(), loadTargets()])
    } else {
      message?.error(res?.message || '提交失败')
    }
  } catch (error: unknown) {
    message?.error(apiErrorMessage(error, '提交失败'))
  } finally {
    revertRequestLoadingId.value = null
  }
}

async function confirmRevertRequest(id: number) {
  if (revertRequestLoadingId.value !== null) return
  if (!confirm(
    '确认撤回此次申诉的数据修复？系统将根据变更记录尽力还原到修复前，此操作不可自动撤销。',
  )) return
  revertRequestLoadingId.value = id
  try {
    const res = await store.confirmDataAppealRevertRequest(id)
    if (res?.code === 200) {
      message?.success('已撤回修复并完成数据还原')
      await loadHistory()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } catch (error: unknown) {
    message?.error(apiErrorMessage(error, '操作失败'))
  } finally {
    revertRequestLoadingId.value = null
  }
}

async function declineRevertRequest(id: number) {
  if (revertRequestLoadingId.value !== null) return
  if (!confirm('确认放弃撤回？本次申诉修复将保持不变。')) return
  revertRequestLoadingId.value = id
  try {
    const res = await store.declineDataAppealRevertRequest(id)
    if (res?.code === 200) {
      message?.success('已放弃撤回')
      await loadHistory()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } catch (error: unknown) {
    message?.error(apiErrorMessage(error, '操作失败'))
  } finally {
    revertRequestLoadingId.value = null
  }
}

async function submit() {
  if (submitting.value) return
  submitError.value = ''
  const category = appealCategory.value
  if (!category) return
  const selected = checkedKeys.value.map(key => {
    const [type, id] = key.split(':')
    return { type, id: Number(id) }
  })
  if (requiresSelectedTargets.value && !selected.length) {
    submitError.value = '请至少选择一条'
    return
  }
  submitting.value = true
  try {
    const payload: {
      targets: { type: string; id: number }[]
      player_note: string
      appeal_category: string
      expedition_evidence?: { expedition_log_id: number; event_index?: number }
    } = {
      targets: selected,
      player_note: category === 'rarity_review'
        ? '副本掉落稀有度复核'
        : playerNote.value.trim(),
      appeal_category: category,
    }
    if (
      needsEvidence.value
      && !latestExpeditionFullText.value
      && selectedEventIndex.value !== null
      && latestExpedition.value?.log_id != null
    ) {
      payload.expedition_evidence = {
        expedition_log_id: latestExpedition.value.log_id,
        event_index: selectedEventIndex.value,
      }
    }
    const res = await store.submitDataAppeal(payload)
    if (res?.code === 200 || res?.code === 201) {
      message?.success(res.message || res.data?.message || '申诉已提交')
      checkedKeys.value = []
      selectedEventIndex.value = null
      playerNote.value = ''
      await Promise.all([loadTargets(), loadHistory()])
      activeTab.value = 'history'
      emit('submitted')
    } else {
      submitError.value = res?.message || '提交失败'
    }
  } catch (e: unknown) {
    submitError.value = apiErrorMessage(e, '提交失败')
  } finally {
    submitting.value = false
  }
}
</script>

<style scoped lang="scss">
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(0, 0, 0, 0.55);
}

.modal-card {
  width: 100%;
  max-width: 520px;
  max-height: 88vh;
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  overflow: hidden;
}

.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-border);
}

.modal-title {
  margin: 0;
  font-size: 17px;
  color: var(--color-text);
}

.modal-close {
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 18px;
  cursor: pointer;
  padding: 4px 8px;
}

.modal-tabs {
  display: flex;
  gap: 0;
  border-bottom: 1px solid var(--color-border);
  padding: 0 12px;
}

.quota-banner {
  margin: 0 0 14px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);

  &.exhausted {
    border-color: #d03050;
    background: rgba(208, 48, 80, 0.08);
  }
}

.quota-pool-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 6px;
  border-radius: 6px;
  color: var(--color-text-secondary);
  font-size: 12px;

  &.active {
    background: rgba(24, 160, 88, 0.1);
    color: var(--color-text);
  }
}

.quota-refresh {
  margin: 5px 6px 0;
  font-size: 11px;
}

.quota-breakdown {
  display: flex;
  flex-wrap: wrap;
  gap: 4px 12px;
  margin: 1px 6px 5px;
  font-size: 11px;
}

.quota-main {
  font-size: 13px;
  color: var(--color-text);

  strong {
    color: var(--color-primary, #18a058);
    font-size: 15px;
  }
}

.quota-banner.exhausted .quota-pool-row.active .quota-main strong {
  color: #d03050;
}

.quota-used {
  font-size: 12px;
}

@media (max-width: 520px) {
  .quota-pool-row {
    align-items: flex-start;
    flex-direction: column;
    gap: 2px;
  }
}

.tab-btn {
  position: relative;
  padding: 10px 14px;
  border: none;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 13px;
  font-weight: 600;
  cursor: pointer;
  border-bottom: 2px solid transparent;
  margin-bottom: -1px;

  &.active {
    color: var(--color-primary);
    border-bottom-color: var(--color-primary);
  }
}

.tab-badge {
  display: inline-block;
  min-width: 16px;
  padding: 0 5px;
  margin-left: 4px;
  font-size: 11px;
  line-height: 16px;
  text-align: center;
  border-radius: 8px;
  background: #f0a020;
  color: #fff;
  font-weight: 700;
}

.modal-body {
  padding: 16px 18px;
  overflow-y: auto;
}

.hint {
  font-size: 13px;
  color: var(--color-text-secondary);
  margin: 0 0 12px;
  line-height: 1.5;
}

.target-toolbar {
  position: sticky;
  top: 0;
  z-index: 2;
  display: grid;
  gap: 6px;
  padding: 10px;
  background: var(--color-surface, var(--color-bg));
  border: 1px solid var(--color-border);
  border-radius: 8px;
}
.target-search {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
}
.target-search input {
  flex: 1;
  min-width: 0;
  min-height: 44px;
  padding: 8px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
}
.target-toolbar-actions, .target-navigation, .target-section-heading {
  display: flex;
  align-items: center;
  gap: 8px;
}
.target-toolbar-actions, .target-navigation {
  overflow-x: auto;
}
.target-toolbar-actions > *, .target-navigation > * {
  flex-shrink: 0;
}
.target-toolbar .evidence-toggle, .target-section-heading .evidence-toggle {
  min-height: 44px;
  white-space: nowrap;
}
.target-section-heading {
  justify-content: space-between;
}
.target-section, #appeal-submit-section {
  scroll-margin-top: 180px;
}
.target-section h4 {
  margin: 12px 0 6px;
  font-size: 13px;
}

.target-row {
  display: flex;
  flex-wrap: wrap;
  align-items: flex-start;
  gap: 6px 8px;
  padding: 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  margin-bottom: 6px;
  cursor: pointer;
  background: var(--color-surface, var(--color-bg));

  input { margin-top: 3px; }
}

.target-name {
  font-weight: 600;
  font-size: 13px;
}

.target-quantity {
  color: var(--color-text-secondary);
  font-size: 12px;
  line-height: 20px;
}

.target-rarity {
  padding: 1px 6px;
  border-radius: 999px;
  background: rgba(32, 128, 240, 0.12);
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 18px;
}

.target-desc {
  flex: 1 1 100%;
  font-size: 12px;
  color: var(--color-text-secondary);
  padding-left: 22px;
}

.category-section {
  margin-bottom: 16px;

  h4 {
    margin: 0 0 8px;
    font-size: 14px;
    color: var(--color-text);
  }
}

.category-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.category-option {
  flex: 1 1 105px;
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 12px 8px;
  border: 2px solid var(--color-border);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
  text-align: center;

  input { display: none; }

  &:hover { border-color: var(--color-primary); }

  &.active {
    border-color: var(--color-primary);
    background: var(--color-primary-bg, rgba(var(--color-primary-rgb, 59,130,246), 0.08));
  }
}

.category-action {
  font: inherit;
  color: inherit;
  background: transparent;
}

.category-icon { font-size: 20px; }
.category-label { font-size: 13px; font-weight: 600; color: var(--color-text); }
.category-desc { font-size: 11px; color: var(--color-text-secondary); }

.evidence-section {
  margin-bottom: 16px;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);

  h4 {
    margin: 0;
    font-size: 14px;
    color: var(--color-text);
  }
}

.evidence-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 8px;
}

.evidence-toggle {
  flex-shrink: 0;
  padding: 4px 10px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface, var(--color-bg));
  color: var(--color-text-secondary);
  cursor: pointer;

  &:hover {
    color: var(--color-text);
    border-color: var(--color-primary, var(--color-border));
  }
}

.evidence-summary {
  margin: 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--color-surface, rgba(0, 0, 0, 0.03));
  line-height: 1.45;
}

.evidence-options {
  margin-top: 4px;
}

.evidence-meta {
  margin: 0 0 10px;
}

.evidence-row {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 8px 0;
  border-top: 1px solid var(--color-border);
  cursor: pointer;
  font-size: 13px;
  color: var(--color-text);

  &:first-of-type {
    border-top: none;
  }

  input {
    margin-top: 3px;
    flex-shrink: 0;
  }
}

.evidence-none {
  padding-top: 0;
}

.evidence-index {
  flex-shrink: 0;
  font-weight: 600;
  min-width: 52px;
}

.evidence-text {
  flex: 1;
  color: var(--color-text-secondary);
  line-height: 1.45;
}

.note-block {
  margin-top: 16px;

  label { display: block; margin-bottom: 6px; font-size: 13px; }
  textarea {
    width: 100%;
    box-sizing: border-box;
    padding: 8px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: var(--color-bg);
    color: var(--color-text);
    resize: vertical;
  }
}

.rarity-review-note {
  margin: 14px 0 0;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  line-height: 1.5;
}

.history-list {
  list-style: none;
  margin: 0;
  padding: 0;
}

.history-item {
  padding: 12px;
  margin-bottom: 8px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.history-top {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.history-id {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.relief-history-tag {
  padding: 1px 7px;
  border-radius: 999px;
  background: rgba(208, 161, 71, 0.14);
  color: #b7791f;
  font-size: 11px;
  font-weight: 700;
}

.history-status {
  font-size: 12px;
  font-weight: 700;
  padding: 2px 8px;
  border-radius: 4px;

  &.st-processing { color: #2080f0; background: rgba(32, 128, 240, 0.12); }
  &.st-awaiting_review { color: #f0a020; background: rgba(240, 160, 32, 0.12); }
  &.st-approved { color: #18a058; background: rgba(24, 160, 88, 0.12); }
  &.st-approved-partial { color: #f0a020; background: rgba(240, 160, 32, 0.12); }
  &.st-rejected { color: #d03050; background: rgba(208, 48, 80, 0.12); }
  &.st-cancelled { color: #8a6d3b; background: rgba(138, 109, 59, 0.12); }
}

.history-time {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.history-targets {
  font-size: 12px;
  color: var(--color-text);
  margin-bottom: 4px;
}

.history-evidence {
  margin-bottom: 4px;
}

.history-note {
  margin: 0 0 6px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text);
  word-break: break-word;
}

.history-ai, .history-done, .history-partial {
  margin: 0 0 4px;
}

.history-reject {
  margin: 4px 0 0;
  font-size: 12px;
  color: #d03050;
}

.history-withdrawn {
  margin: 4px 0 0;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.history-checklist {
  margin: 6px 0 8px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
}

.checklist-title {
  margin: 0 0 6px;
}

.checklist-items {
  margin: 0;
  padding-left: 18px;
  list-style: none;

  li {
    margin: 0 0 4px;
    font-size: 12px;
    line-height: 1.5;
    color: var(--color-text);
    word-break: break-word;

    &::before {
      content: '';
      display: inline-block;
      width: 0;
    }
  }

  .checklist-done .checklist-label { color: #18a058; }
  .checklist-partial .checklist-label { color: #f0a020; }
  .checklist-rejected .checklist-label { color: #d03050; }
  .checklist-pending .checklist-label { color: var(--color-text-secondary); }
}

.checklist-detail {
  display: inline;
}

.history-reply {
  margin: 4px 0 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
}

.review-reasons {
  margin: 6px 0 0;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
}

.review-feedback {
  display: flex;
  justify-content: flex-end;
  margin: 6px 0;
}

.review-reason {
  margin: 0;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text);
  word-break: break-word;

  & + .review-reason {
    margin-top: 4px;
  }
}

.ai-pending {
  margin: 4px 0;
}

.ai-review-result-display {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px;
  margin: 6px 0;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  font-size: 12px;

  .review-decision {
    font-weight: 700;
    &.rejected { color: #d03050; }
    &.placeholder { color: #f0a020; }
    &.error { color: #d03050; }
  }
  .review-message {
    color: var(--color-text);
    line-height: 1.45;
  }
}

.muted { color: var(--color-text-secondary); }
.small { font-size: 12px; }

.req { color: #d03050; }

.actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 16px;
}

.history-actions {
  margin-top: 8px;
}

.history-actions-inline {
  margin-top: 8px;
}

.revert-offer-box {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px solid #f0a02055;
  border-radius: 8px;
  background: color-mix(in srgb, #f0a020 8%, var(--color-bg));
}

.revert-offer-hint {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text);
}

.revert-offer-admin-note {
  margin: 0 0 8px;
}

.revert-request-box {
  border-color: #2080f055;
  background: color-mix(in srgb, #2080f0 8%, var(--color-bg));
}

.revert-request-form {
  margin-top: 10px;
  padding: 10px 12px;
  border: 1px dashed var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);

  label {
    display: block;
    margin-bottom: 6px;
    color: var(--color-text-secondary);
  }

  textarea {
    width: 100%;
    box-sizing: border-box;
    margin-bottom: 8px;
    padding: 8px;
    border: 1px solid var(--color-border);
    border-radius: 6px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: 13px;
    resize: vertical;
  }
}

.tab-badge.revert-badge {
  background: #f0a020;
}

.st-reverted { color: #8a6d3b; }

.btn-xs {
  padding: 4px 10px;
  font-size: 12px;
}
.btn-danger {
  background: #d03050;
  color: #fff;
  border-color: transparent;
}

.submit-error, .state.error { color: #d03050; font-size: 13px; }
.state { text-align: center; padding: 24px; color: var(--color-text-secondary); }

.btn-primary, .btn-secondary {
  padding: 8px 16px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  cursor: pointer;
  font-size: 13px;
}
.btn-sm {
  padding: 6px 14px;
  font-size: 12px;
}
.btn-primary {
  background: var(--color-primary, #18a058);
  color: #fff;
  border-color: transparent;
}
.btn-primary:disabled { opacity: 0.5; cursor: not-allowed; }
.btn-secondary { background: var(--color-bg); color: var(--color-text); }
.btn-secondary:disabled { opacity: 0.5; cursor: not-allowed; }
@media (max-width: 640px) {
  .modal-head { padding: 8px 12px; gap: 8px; }
  .modal-tabs { padding: 0 8px; flex-shrink: 0; }
  .modal-tabs .tab-btn { flex: 1; min-width: 0; padding-inline: 6px; white-space: normal; }
  .modal-body { padding: 14px 12px; }
  .target-toolbar { position: static; gap: 8px; padding: 8px; }
  .target-search > span { flex-shrink: 0; }
  .target-search input { width: 0; min-height: 36px; }
  .target-toolbar-actions, .target-navigation { flex-wrap: wrap; gap: 6px; overflow: visible; }
  .target-toolbar-actions > span { flex-basis: 100%; }
  .target-toolbar .evidence-toggle { min-height: 32px; padding: 5px 8px; white-space: normal; }
  .target-section, #appeal-submit-section { scroll-margin-top: 12px; }
  .target-row, .evidence-row { min-height: 44px; }
  .target-desc { padding-left: 0; font-size: 14px; line-height: 1.7; }
  .category-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .category-option { min-width: 0; }
  .category-desc { font-size: 12px; line-height: 1.6; }
  .evidence-header { flex-wrap: wrap; }
  .evidence-summary, .hint { font-size: 14px; line-height: 1.7; }
}
</style>
