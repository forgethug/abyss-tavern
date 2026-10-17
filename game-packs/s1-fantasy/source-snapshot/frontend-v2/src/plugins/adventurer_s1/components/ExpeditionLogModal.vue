<template>
  <Teleport to="body">
  <div
    class="modal-overlay s1-mobile-surface s1-mobile-overlay"
    :class="{ 'is-fullscreen': isFullscreen }"
    @click.self="emit('close')"
  >
    <div
      class="modal-card s1-mobile-dialog"
      :class="[{ 'is-fullscreen': isFullscreen }, specialEventPresentation?.cardClass]"
      :style="specialEventStyle(specialEventType)"
    >
      <!-- 阅读进度 -->
      <div class="read-progress" :class="{ visible: readProgress > 2 }">
        <div class="read-progress-fill" :style="{ width: readProgress + '%' }" />
      </div>

      <header class="modal-head s1-mobile-dialog-header">
        <h3 class="modal-title">📜 探险日志<span class="modal-log-id"> #{{ logId }}</span></h3>
        <div class="head-tools">
          <div class="reader-toolbar" title="阅读设置">
            <button
              type="button"
              class="tool-btn reader-font-step"
              title="缩小字号"
              @click="changeFontSize(-1)"
            >
              A-
            </button>
            <span class="tool-label">{{ fontSizeLabel }}</span>
            <button
              type="button"
              class="tool-btn reader-font-step"
              title="放大字号"
              @click="changeFontSize(1)"
            >
              A+
            </button>
            <select
              class="theme-select"
              :value="theme"
              title="阅读背景"
              @change="onThemeChange"
            >
              <option v-for="[id, label] in themeOptions" :key="id" :value="id">
                {{ label }}
              </option>
            </select>
            <button
              type="button"
              class="tool-btn reader-settings-toggle"
              :class="{ active: readerSettingsOpen }"
              :aria-expanded="readerSettingsOpen"
              aria-controls="s1-reader-settings"
              title="更多阅读设置"
              @click="readerSettingsOpen = !readerSettingsOpen"
            >
              ⚙ <span class="reader-settings-label">阅读设置</span>
            </button>
            <button
              type="button"
              class="tool-btn"
              :title="isFullscreen ? '退出全屏' : '全屏阅读'"
              @click="toggleFullscreen"
            >
              {{ isFullscreen ? '⊡' : '⛶' }}
            </button>
          </div>
          <button type="button" class="modal-close" title="关闭" aria-label="关闭探险日志" @click="emit('close')">✕</button>
        </div>
      </header>

      <section v-if="readerSettingsOpen" id="s1-reader-settings" class="reader-settings-panel" aria-label="阅读设置">
        <div class="reader-mobile-basics">
          <div class="reader-mobile-font" role="group" aria-label="正文字号">
            <span>字号 {{ fontSizeLabel }}</span>
            <button type="button" class="tool-btn" aria-label="缩小字号" @click="changeFontSize(-1)">A−</button>
            <button type="button" class="tool-btn" aria-label="放大字号" @click="changeFontSize(1)">A+</button>
          </div>
          <label class="reader-setting-field">
            <span>阅读背景</span>
            <select :value="theme" @change="onThemeChange">
              <option v-for="[id, label] in themeOptions" :key="id" :value="id">{{ label }}</option>
            </select>
          </label>
        </div>
        <div class="reader-settings-grid">
          <label class="reader-setting-field">
            <span>字体</span>
            <select
              :value="prefs.fontFamily"
              @change="setReaderChoice('fontFamily', $event)"
            >
              <option v-for="[id, label] in fontOptions" :key="id" :value="id">{{ label }}</option>
            </select>
          </label>

          <div class="reader-setting-field">
            <span>字重</span>
            <div class="reader-choice-group">
              <button
                v-for="weight in readerWeightOptions"
                :key="weight"
                type="button"
                :class="{ active: prefs.fontWeight === weight }"
                @click="updatePrefs({ fontWeight: weight })"
              >{{ weight === 400 ? '标准' : weight === 500 ? '稍粗' : '加粗' }}</button>
            </div>
          </div>

          <label class="reader-setting-field reader-setting-range">
            <span>行高 <b>{{ Number(prefs.lineHeight).toFixed(2) }}</b></span>
            <input
              type="range"
              min="1.4"
              max="2.4"
              step="0.05"
              :value="prefs.lineHeight"
              @input="setReaderNumber('lineHeight', $event)"
            />
          </label>

          <label class="reader-setting-field reader-setting-range">
            <span>字间距 <b>{{ prefs.letterSpacing }}px</b></span>
            <input
              type="range"
              min="-0.5"
              max="2"
              step="0.25"
              :value="prefs.letterSpacing"
              @input="setReaderNumber('letterSpacing', $event)"
            />
          </label>

          <label class="reader-setting-field reader-setting-range">
            <span>段落间距 <b>{{ prefs.paragraphSpacing }}px</b></span>
            <input
              type="range"
              min="8"
              max="28"
              step="2"
              :value="prefs.paragraphSpacing"
              @input="setReaderNumber('paragraphSpacing', $event)"
            />
          </label>

          <div class="reader-setting-field">
            <span>阅读宽度</span>
            <div class="reader-choice-group">
              <button
                v-for="item in readerWidthOptions"
                :key="item.id"
                type="button"
                :class="{ active: prefs.readerWidth === item.id }"
                @click="updatePrefs({ readerWidth: item.id })"
              >{{ item.label }}</button>
            </div>
          </div>
        </div>

        <div class="reader-background-settings">
          <div class="reader-background-head">
            <div>
              <strong>本地阅读背景</strong>
              <small>仅保存在当前浏览器，不会上传；支持 JPG、PNG、WebP，最大 5 MB。</small>
            </div>
            <div class="reader-background-actions">
              <input
                ref="backgroundInputRef"
                class="visually-hidden-input"
                type="file"
                accept="image/jpeg,image/png,image/webp"
                @change="onBackgroundSelected"
              />
              <button type="button" :disabled="backgroundLoading" @click="backgroundInputRef?.click()">
                {{ backgroundUrl ? '更换图片' : '选择图片' }}
              </button>
              <button
                v-if="backgroundUrl"
                type="button"
                class="reader-clear-btn"
                :disabled="backgroundLoading"
                @click="removeReaderBackground"
              >清除</button>
            </div>
          </div>

          <div v-if="backgroundUrl" class="reader-settings-grid reader-background-controls">
            <label class="reader-setting-field reader-setting-range">
              <span>图片强度 <b>{{ Math.round(prefs.backgroundImageOpacity * 100) }}%</b></span>
              <input type="range" min="0.15" max="1" step="0.05" :value="prefs.backgroundImageOpacity" @input="setReaderNumber('backgroundImageOpacity', $event)" />
            </label>
            <label class="reader-setting-field reader-setting-range">
              <span>正文底板 <b>{{ Math.round(prefs.backgroundPanelOpacity * 100) }}%</b></span>
              <input type="range" min="0.5" max="1" step="0.05" :value="prefs.backgroundPanelOpacity" @input="setReaderNumber('backgroundPanelOpacity', $event)" />
            </label>
            <label class="reader-setting-field reader-setting-range">
              <span>底板模糊 <b>{{ prefs.backgroundBlur }}px</b></span>
              <input type="range" min="0" max="12" step="1" :value="prefs.backgroundBlur" @input="setReaderNumber('backgroundBlur', $event)" />
            </label>
            <label class="reader-setting-field">
              <span>图片填充</span>
              <select :value="prefs.backgroundFit" @change="setReaderChoice('backgroundFit', $event)">
                <option value="cover">铺满并裁切</option>
                <option value="contain">完整显示</option>
              </select>
            </label>
          </div>
        </div>

        <div class="reader-settings-foot">
          <button type="button" class="reader-reset-btn" @click="resetPrefs">恢复默认排版</button>
        </div>
      </section>


      <div v-if="loading" class="state state-loading">加载中…</div>
      <template v-else-if="log">
        <div class="log-meta-bar">
          <span v-if="log.party_name" class="meta-badge">🏰 {{ log.party_name }}</span>
          <span v-if="log.status === 'in_progress' || log.status === 'completed'" class="meta-badge">🗺️ {{ log.temporary_location_name || log.hidden_dungeon || log.target_dungeon }}</span>
          <span
            v-if="specialEventPresentation"
            class="meta-badge special-event-badge"
          >
            {{ specialEventPresentation.icon }} {{ specialEventPresentation.label }}
          </span>
          <span class="meta-status-wrap">
            <span class="meta-badge" :class="'st-' + log.status">{{ logStatusLabel(log.status) }}</span>
          </span>
          <span v-if="log.created_at" class="meta-time">{{ formatDateTime(log.created_at) }}</span>
        </div>

        <div class="log-name-highlight-bar">
          <button
            type="button"
            class="tool-btn"
            :class="{ active: isHighlighted }"
            title="给本次冒险者名字分配不同颜色"
            :disabled="!highlightableMemberNames.length"
            @click="autoHighlightNames"
          >
            🎨 名字高亮
          </button>
          <button
            v-if="isHighlighted"
            type="button"
            class="tool-btn"
            title="清除名字颜色"
            @click="clearHighlightNames"
          >
            清除
          </button>
        </div>

        <div v-if="isAdmin" class="log-admin-actions">
          <button
            v-if="log.status === 'in_progress'"
            type="button"
            class="btn-secondary btn-sm"
            :disabled="logActionLoading"
            @click="doStepExpeditionLog(log.id)"
          >
            逐步推送
          </button>
          <button
            v-if="log.status === 'in_progress'"
            type="button"
            class="btn-danger btn-sm"
            :disabled="logActionLoading"
            @click="doForceCompleteExpeditionLog(log.id)"
          >
            直接结算
          </button>
          <button
            v-if="log.status === 'in_progress'"
            type="button"
            class="btn-warning btn-sm"
            :disabled="logActionLoading || settlementRepairLoading"
            @click="toggleSettlementConflictRepair(log.id)"
          >
            {{ settlementRepairOpen ? '收起结算冲突恢复' : '结算冲突恢复' }}
          </button>
          <button
            v-if="log.status === 'failed'"
            type="button"
            class="btn-warning btn-sm"
            :disabled="logActionLoading"
            @click="doRegenerateExpeditionLog(log.id)"
          >
            重新生成
          </button>
          <button
            v-if="log.status === 'generating' && generatingRetryAvailable"
            type="button"
            class="btn-warning btn-sm"
            :disabled="logActionLoading"
            @click="doRetryGeneratingExpeditionLog(log.id)"
          >
            重试生成
          </button>
          <button
            type="button"
            class="btn-secondary btn-sm"
            @click="objectiveOutlineOpen = !objectiveOutlineOpen"
          >
            {{ objectiveOutlineOpen ? '收起客观局势草案' : '查看客观局势草案 AI' }}
          </button>
          <button
            type="button"
            class="btn-secondary btn-sm"
            :disabled="observerReviewLoading"
            @click="toggleObserverReview(log.id)"
          >
            {{ observerReviewOpen ? '收起 AI 剧情审核' : '查看 AI 剧情审核' }}
          </button>
          <button
            type="button"
            class="btn-secondary btn-sm"
            @click="worldIntelOpen = !worldIntelOpen"
          >
            {{ worldIntelOpen ? '收起世界情报注入' : '查看世界情报注入' }}
          </button>
          <button
            v-if="isAdmin && observerReview && observerReview.status !== 'completed'"
            type="button"
            class="btn-warning btn-sm"
            :disabled="observerRetryLoading"
            @click="doRetryObserverReview(log.id)"
          >
            {{ observerRetryLoading ? '重试中...' : '重试剧情审核' }}
          </button>
          <button
            type="button"
            class="btn-secondary btn-sm"
            @click="toggleEditMode"
          >
            {{ editMode ? '取消编辑' : '✏️ 编辑文本' }}
          </button>
        </div>

        <section
          v-if="isAdmin && generationFailureDetail"
          class="observer-review-panel generation-failure-panel"
        >
          <div class="observer-review-head">
            <strong>主探险生成诊断</strong>
            <span v-if="generationFailureCategory">
              {{ generationFailureCategory }}
              <template v-if="generationFailureCode"> · {{ generationFailureCode }}</template>
            </span>
          </div>
          <p v-if="generationFailureIsLength" class="observer-review-state">
            本次至少一次输出触及长度上限；下方会显示实际路由模型、响应模型、请求上限与完成 token，
            可据此换模型或调整 ADVENTURER_S1_EXPEDITION_AI_MAX_OUTPUT_TOKENS。
          </p>
          <pre class="observer-review-output">{{ generationFailureDetail }}</pre>
          <details v-if="generationRetryTraceText" class="world-intel-details">
            <summary>查看完整生成重试轨迹</summary>
            <pre class="observer-review-output">{{ generationRetryTraceText }}</pre>
          </details>
        </section>

        <section
          v-if="isAdmin && log.status === 'in_progress' && settlementRepairOpen"
          class="observer-review-panel settlement-repair-panel"
        >
          <div class="observer-review-head">
            <strong>结算冲突恢复</strong>
            <span>只处理冻结稳定装备 ID 与当前哈希完全一致的旧日志</span>
            <button
              type="button"
              class="btn-secondary btn-sm"
              :disabled="settlementRepairLoading || logActionLoading"
              @click="loadSettlementConflictRepairPreview(log.id)"
            >
              {{ settlementRepairLoading ? '核验中...' : '重新核验' }}
            </button>
          </div>
          <p class="observer-review-state settlement-repair-note">
            恢复不会重新调用 AI，也不会直接补金币或物品；它只移除能由稳定 ID
            唯一证明的重复消耗，或经管理员确认的稳定装备修改后同名重复新发放，
            然后在这条原日志上重放完整结算。
          </p>
          <p v-if="settlementRepairLoading" class="observer-review-state">
            正在读取冻结快照并核验当前装备状态…
          </p>
          <template v-else-if="settlementRepairPreview">
            <p
              v-if="settlementRepairPreview.last_replay_error"
              class="observer-review-error"
            >
              上次重放仍失败：{{ settlementRepairPreview.last_replay_error }}
            </p>
            <div
              v-if="settlementRepairPreview.candidates.length"
              class="settlement-repair-list"
            >
              <article
                v-for="candidate in settlementRepairPreview.candidates"
                :key="candidate.equipment_id"
                class="settlement-repair-card"
              >
                <div class="settlement-repair-card-head">
                  <strong>
                    {{ candidate.adventurer_name || `冒险者 #${candidate.adventurer_id}` }}
                  </strong>
                  <span>装备 #{{ candidate.equipment_id }}</span>
                </div>
                <div class="settlement-repair-change">
                  <span>修改：{{ candidate.source_name }} → {{ candidate.updated_name }}</span>
                  <span v-if="candidate.repair_kind === 'remove_conflicting_consumption'">
                    移除重复消耗：{{ candidate.consumed_name }} ×{{ candidate.quantity }}
                  </span>
                  <span v-else>
                    管理员确认移除同名重复新发放：{{ candidate.updated_name }} ×{{ candidate.quantity }}；
                    精确同名特质依赖将改绑到装备 #{{ candidate.equipment_id }}
                  </span>
                </div>
              </article>
            </div>
            <p
              v-else-if="settlementRepairPreview.mode === 'none' && !settlementRepairPreview.blockers.length"
              class="observer-review-state"
            >
              当前日志没有检测到可确认恢复的稳定装备结算冲突。
            </p>
            <ul
              v-if="settlementRepairPreview.blockers.length"
              class="settlement-repair-blockers"
            >
              <li
                v-for="blocker in settlementRepairPreview.blockers"
                :key="`${blocker.code}-${blocker.message}`"
              >
                {{ blocker.message }}（{{ blocker.code }}）
              </li>
            </ul>
            <div
              v-if="settlementRepairPreview.execution_available && settlementRepairPreview.preview_token"
              class="observer-review-actions settlement-repair-actions"
            >
              <button
                type="button"
                class="btn-primary btn-sm"
                :disabled="logActionLoading"
                @click="doExecuteSettlementConflictRepair(log.id)"
              >
                {{ logActionLoading
                  ? '处理中...'
                  : settlementRepairPreview.mode === 'retry'
                    ? '重新重放结算'
                    : '修复并重放结算' }}
              </button>
              <button
                v-if="settlementRepairPreview.can_restore"
                type="button"
                class="btn-danger btn-sm"
                :disabled="logActionLoading"
                @click="doRestoreSettlementConflictRepair(log.id)"
              >
                回滚日志补丁
              </button>
            </div>
          </template>
        </section>

        <!-- 编辑文本面板 -->
        <section v-if="isAdmin && editMode" class="edit-text-panel">
          <div class="edit-text-head">
            <strong>✏️ 编辑日志文本</strong>
            <button
              type="button"
              class="btn-primary btn-sm"
              :disabled="editSaving"
              @click="saveEditText"
            >
              {{ editSaving ? '保存中…' : '保存' }}
            </button>
          </div>
          <div v-for="item in editItems" :key="item.path" class="edit-text-item">
            <label class="edit-text-label">{{ item.label }}</label>
            <textarea
              v-model="item.value"
              class="edit-textarea"
              rows="4"
            />
          </div>
          <div v-if="editItems.length === 0" class="empty">无可编辑文本字段</div>
        </section>

        <section v-if="isAdmin && objectiveOutlineOpen" class="observer-review-panel objective-outline-panel">
          <div class="observer-review-head">
            <strong>客观局势草案 AI（剧情生成前）</strong>
          </div>
          <pre v-if="objectiveOutlineText" class="observer-review-output">{{ objectiveOutlineText }}</pre>
          <div v-if="objectiveOutlineText && objectiveOutlineValidationWarnings.length" class="observer-review-state">
            <p>草案已采用，但后端处理了以下非致命兼容问题：</p>
            <ul>
              <li v-for="(warning, index) in objectiveOutlineValidationWarnings" :key="index">
                {{ formatObjectiveOutlineWarning(warning) }}
              </li>
            </ul>
          </div>
          <div v-else-if="objectiveOutlineRejectedReason" class="observer-review-state objective-outline-rejected">
            <p>客观局势草案已被后端过滤，主剧情已继续生成。</p>
            <p>原因：{{ objectiveOutlineRejectedReason }}</p>
            <details v-if="objectiveOutlineRawExcerpt" class="world-intel-details">
              <summary>查看原始返回摘录</summary>
              <pre class="observer-review-output">{{ objectiveOutlineRawExcerpt }}</pre>
            </details>
          </div>
          <p v-else class="observer-review-state">
            该日志暂无客观局势草案记录（特殊机遇副本会跳过；2026-07-03 前的历史日志也未落库）。
          </p>
        </section>

        <section v-if="isAdmin && observerReviewOpen" class="observer-review-panel">
          <div v-if="observerReviewLoading" class="observer-review-state">正在读取审核结果…</div>
          <template v-else-if="observerReview">
            <div class="observer-review-head">
              <strong>独立剧情审核 AI</strong>
              <span :class="'review-status-' + observerReview.status">
                模型合同：{{ observerReviewStatusLabel(observerReview.status) }}
                <span v-if="observerReview.retry_count"> (重试{{ observerReview.retry_count }})</span>
              </span>
              <span :class="'review-consumption-' + (observerReview.consumption_status || 'unknown')">
                最终消费：{{ observerReviewConsumptionStatusLabel(observerReview.consumption_status) }}
              </span>
              <span>请求模型：{{ observerReview.model_used }}</span>
              <span v-if="observerReview.effective_model_used">
                最近确认路由模型：{{ observerReview.effective_model_used }}
              </span>
              <span v-if="observerReview.correlation_id">
                调用链：{{ observerReview.correlation_id }}
              </span>
              <span v-if="observerReview.updated_at">{{ formatDateTime(observerReview.updated_at) }}</span>
            </div>
            <p v-if="observerReview.error_message" class="observer-review-error">
              {{ observerReview.error_message }}
            </p>
            <p v-if="observerReview.consumption_status === 'failed'" class="observer-review-error">
              最终消费失败：{{ observerReview.consumption_failure_stage || 'downstream_consumption' }}
              <template v-if="observerReview.consumption_error_code">
                · {{ observerReview.consumption_error_code }}
              </template>
            </p>
            <div v-if="observerReviewIgnoredFields.length" class="observer-review-state">
              已忽略不允许覆盖的字段：{{ observerReviewIgnoredFields.join(', ') }}
            </div>
            <details v-if="observerReviewPreview.length" class="observer-review-preview" open>
              <summary>覆盖预览（{{ observerReviewPreview.length }} 项）</summary>
              <div v-if="observerMemberPreview.length" class="observer-member-preview-list">
                <div
                  v-for="member in observerMemberPreview"
                  :key="member.key"
                  class="observer-member-card"
                >
                  <div class="observer-member-head">
                    <strong>{{ member.name }}</strong>
                    <span v-if="member.id != null">#{{ member.id }}</span>
                    <span class="preview-count">{{ member.changes.length }} 项覆盖</span>
                  </div>
                  <div class="observer-change-list">
                    <div
                      v-for="change in member.changes"
                      :key="change.field"
                      class="observer-change-row"
                    >
                      <span class="observer-change-label">{{ change.label }}</span>
                      <span class="observer-change-before">{{ change.before }}</span>
                      <span class="observer-change-arrow">→</span>
                      <span class="observer-change-after">{{ change.after }}</span>
                    </div>
                  </div>
                </div>
              </div>
              <div v-if="observerGlobalPreview.length" class="observer-global-preview">
                <div
                  v-for="row in observerGlobalPreview"
                  :key="row.field"
                  class="observer-preview-row"
                >
                  <div class="observer-preview-row-head">
                    <strong>{{ observerFieldLabel(row.field) }}</strong>
                    <span :class="row.changed ? 'preview-changed' : 'preview-same'">
                      {{ row.changed ? '将被覆盖' : '无变化' }}
                    </span>
                  </div>
                  <div class="observer-preview-grid">
                    <div>
                      <em>当前</em>
                      <pre>{{ formatPreviewValue(row.before) }}</pre>
                    </div>
                    <div>
                      <em>覆盖后</em>
                      <pre>{{ formatPreviewValue(row.after) }}</pre>
                    </div>
                  </div>
                </div>
              </div>
            </details>
            <div v-if="observerReviewPreview.length" class="observer-review-actions">
              <button
                type="button"
                class="btn-primary btn-sm"
                :disabled="observerApplyLoading || !observerReviewCanApply"
                @click="doApproveObserverReview(observerReview.id)"
              >
                {{ observerApplyLoading ? '应用中...' : '应用覆盖' }}
              </button>
            </div>
            <pre v-if="observerReviewOutput" class="observer-review-output">{{ observerReviewOutput }}</pre>
            <p v-else class="observer-review-state">审核任务尚未产出内容。</p>
          </template>
          <p v-else class="observer-review-state">该日志暂无审核记录。</p>
        </section>

        <section v-if="isAdmin && worldIntelOpen" class="observer-review-panel world-intel-panel">
          <div class="observer-review-head">
            <strong>世界情报注入记录</strong>
            <span class="observer-review-state">只展示当时写入 full_log 的管理员调试快照；不作为玩家正文。</span>
          </div>
          <template v-if="worldIntelRecords.length">
            <article v-for="record in worldIntelRecords" :key="record.key" class="world-intel-record">
              <header class="world-intel-record-head">
                <strong>{{ record.label }}</strong>
                <span>{{ record.items.length }} 条情报</span>
              </header>
              <div class="world-intel-meta">
                <span>副本：{{ record.snapshot.target_dungeon || log.target_dungeon }}</span>
                <span>锚点：{{ joinOrNone(record.snapshot.context_keywords) }}</span>
              </div>
              <div v-if="record.items.length" class="world-intel-items">
                <div v-for="item in record.items" :key="`${record.key}-${item.id || item.title}`" class="world-intel-item">
                  <div class="world-intel-item-title">
                    <strong>#{{ item.id || '-' }} {{ item.title || '未命名情报' }}</strong>
                    <span>匹配 {{ item.match_score ?? '-' }}</span>
                  </div>
                  <div class="world-intel-match-grid">
                    <div><em>主地城</em><span>{{ item.matched_base_dungeon ? '命中' : '未命中' }}</span></div>
                    <div><em>关键词</em><span>{{ joinOrNone(item.matched_trigger_keywords) }}</span></div>
                    <div><em>scope</em><span>{{ joinOrNone(item.matched_scope_keys) }}</span></div>
                    <div><em>上下文锚点</em><span>{{ joinOrNone(item.matched_context_terms) }}</span></div>
                  </div>
                  <details v-if="item.matched_text_tokens?.length" class="world-intel-details compact">
                    <summary>查看粗略重叠碎片</summary>
                    <p class="world-intel-token-note">这是内部二字滑窗相似度碎片，不等同于语义关键词。</p>
                    <span>{{ joinOrNone(item.matched_text_tokens) }}</span>
                  </details>
                </div>
              </div>
              <p v-else class="observer-review-state">本次未注入世界情报。</p>
              <details class="world-intel-details">
                <summary>查看实际传入的情报块</summary>
                <pre class="observer-review-output">{{ record.snapshot.prompt_block || '无' }}</pre>
              </details>
              <details class="world-intel-details">
                <summary>查看匹配上下文</summary>
                <pre class="observer-review-output">{{ record.snapshot.context_text || '无' }}</pre>
              </details>
            </article>
          </template>
          <p v-else class="observer-review-state">
            该日志没有保存实际世界情报注入记录。旧日志或本次未启用情报注入时会出现这种情况。
          </p>
        </section>

        <div
          ref="readerRef"
          class="reader-scroll s1-mobile-dialog-body"
          :class="['reader-theme-' + theme, { 'has-custom-background': backgroundUrl }]"
          :style="readerBackgroundStyle"
          tabindex="0"
          role="region"
          aria-label="探险日志正文"
          @scroll.passive="onReaderScroll"
        >
          <article
            class="reader-prose"
            :style="proseStyle"
          >
            <div
              v-if="log.status === 'in_progress' || log.status === 'generating'"
              class="log-progress-hint"
            >
              <span class="progress-dot" />
              {{ log.status === 'generating' ? departureText : '探险进行中，事件将分批揭晓' }}
              <span v-if="events.length && log.status === 'in_progress'" class="progress-count">
                · 已揭晓 {{ events.length }} 条
              </span>
            </div>

            <p v-if="log.result_summary" class="log-summary">{{ log.result_summary }}</p>

            <div
              v-if="commentHintVisible && hasCommentableTargets"
              class="comment-discovery-hint"
              role="status"
            >
              <span aria-hidden="true">💬</span>
              <span>鼠标移入或轻点段落、成员状态，即可发表评论</span>
              <button type="button" aria-label="关闭段评提示" @click="dismissCommentHint">×</button>
            </div>

            <div v-if="events.length" class="event-list">
              <div
                v-for="(ev, i) in events"
                :key="i"
                class="event-item"
                :class="[
                  specialEventClass(i),
                  { 'is-comment-active': isEmptyCommentRevealed('event', i) },
                ]"
                :tabindex="isCommentable('event', i) && !commentCount('event', i) ? 0 : undefined"
                @click="revealEmptyComment('event', i)"
                @keydown.enter.prevent="revealEmptyComment('event', i)"
              >
                <span class="event-idx">{{ i + 1 }}</span>
                <p class="event-text">
                  <template v-for="(seg, si) in splitTextByNames(eventText(ev))" :key="si">
                    <span v-if="seg.color" :style="{ color: seg.color, fontWeight: 600 }">{{ seg.text }}</span>
                    <template v-else>{{ seg.text }}</template>
                  </template>
                  <button
                    v-if="isCommentable('event', i)"
                    type="button"
                    class="inline-comment-bubble"
                    :class="{ 'is-empty': !commentCount('event', i) }"
                    :aria-label="commentCount('event', i) ? `查看 ${commentCount('event', i)} 条评论` : '评论这一段'"
                    @click.stop="openComments('event', i)"
                  >
                    <span aria-hidden="true">💬</span>
                    <span v-if="commentCount('event', i)">{{ commentCount('event', i) }}</span>
                    <span v-else>评论</span>
                  </button>
                </p>
              </div>
            </div>
            <div v-else class="state subtle">暂无详细探险过程</div>

            <ExpeditionMemberUpdates
              v-if="log.status === 'completed'"
              :member-updates="memberUpdates"
              :comment-counts="log.comment_counts?.member_update"
              @open="emit('open', $event)"
              @comment="openComments('member_update', $event)"
            />

            <section v-if="log.status === 'completed' && likeInfo" class="log-like-panel">
              <div class="log-like-head">
                <h4 class="log-like-title">给队友点赞</h4>
                <span v-if="likeInfo.liked_adventurer_name" class="log-like-done">
                  已点赞 {{ likeInfo.liked_adventurer_name }}
                </span>
              </div>
              <div v-if="likeInfo.can_like" class="log-like-candidates">
                <button
                  v-for="candidate in likeInfo.candidates"
                  :key="candidate.id"
                  type="button"
                  class="like-candidate-btn"
                  :disabled="likeLoading"
                  @click="doLikeExpeditionLog(candidate.id)"
                >
                  👍 {{ candidate.name }}
                  <span class="like-count">{{ candidate.praise_count }}</span>
                </button>
              </div>
              <p v-else class="log-like-reason">
                {{ likeInfo.reason || '当前不能点赞' }}
              </p>
            </section>

            <div v-if="log.status === 'completed' && log.ai_feedback" class="log-ai-feedback">
              <span>这次 AI 写得怎么样？</span>
              <AiFeedbackControl
                :target="log.ai_feedback"
                @updated="updateAiFeedback"
              />
            </div>

            <div v-if="log.status === 'completed' && relationChanges.length" class="relation-changes">
              <h4 class="rc-title">羁绊变化</h4>
              <p class="rc-hint">这里记录两人共同关系的变化，不分别表示谁对谁的单向好感。</p>
              <div v-for="(rc, i) in relationChanges" :key="i" class="rc-item">
                <span class="rc-icon">{{ rc.direction === 'up' ? '💞' : '💔' }}</span>
                <span class="rc-text">
                  {{ rc.adventurer_a_name }} 与 {{ rc.adventurer_b_name }} 的共同关系{{ rc.direction === 'up' ? '更亲近了' : '更疏远了' }}
                </span>
              </div>
            </div>
          </article>
        </div>
      </template>
      <div v-else class="state state-loading">日志不存在或加载失败</div>
    </div>
  </div>
  </Teleport>
  <Teleport to="body">
    <div
      v-if="commentPanelOpen"
      class="comment-overlay s1-mobile-surface s1-mobile-overlay"
      role="dialog"
      aria-modal="true"
      aria-labelledby="expedition-comment-title"
      @click.self="closeComments"
    >
      <section class="comment-sheet s1-mobile-dialog">
        <header class="comment-sheet-head s1-mobile-dialog-header">
          <div>
            <h3 id="expedition-comment-title">评论</h3>
            <p>{{ commentTargetLabel }}</p>
          </div>
          <button type="button" class="comment-close" aria-label="关闭评论" @click="closeComments">×</button>
        </header>

        <div class="comment-list s1-mobile-dialog-body">
          <p v-if="commentLoading" class="comment-state">评论加载中…</p>
          <template v-else-if="comments.length">
            <article v-for="comment in comments" :key="comment.id" class="comment-item">
              <header>
                <strong>{{ comment.nickname }}</strong>
                <span v-if="comment.active_title" class="comment-title">{{ comment.active_title }}</span>
                <time v-if="comment.created_at">{{ formatDateTime(comment.created_at) }}</time>
              </header>
              <p>{{ comment.content }}</p>
            </article>
            <button
              v-if="commentHasNext"
              type="button"
              class="comment-more"
              :disabled="commentLoadingMore"
              @click="loadMoreComments"
            >
              {{ commentLoadingMore ? '加载中…' : '加载更多' }}
            </button>
          </template>
          <p v-else class="comment-state">还没有评论，来留下第一句吧。</p>
        </div>

        <form class="comment-compose" @submit.prevent="submitComment">
          <textarea
            v-model="commentDraft"
            maxlength="300"
            rows="3"
            placeholder="写下你的评论…"
            aria-label="评论内容"
          />
          <div class="comment-compose-foot">
            <span>{{ commentDraft.length }}/300</span>
            <button type="submit" :disabled="commentSubmitting || !commentDraft.trim()">
              {{ commentSubmitting ? '发送中…' : '发送' }}
            </button>
          </div>
        </form>
      </section>
    </div>
  </Teleport>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { useNameColors } from '../composables/useNameColors'
import { useReaderSettings } from '../composables/useReaderSettings'
import {
  READER_FONT_STACKS,
  READER_WIDTHS,
} from '../composables/useReaderSettings'
import { useReaderBackground } from '../composables/useReaderBackground'
import { useSocket } from '@/composables/useSocket'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import {
  getSpecialEventPresentation,
  isSpecialEventPresentationStatus,
  logStatusLabel,
  getExpeditionDepartureText,
  specialEventStyle,
} from '../utils'
import { formatDateTime } from '@/utils/time'
import ExpeditionMemberUpdates from './ExpeditionMemberUpdates.vue'
import AiFeedbackControl from './AiFeedbackControl.vue'
import type {
  ReaderBackgroundFit,
  ReaderFontFamily,
  ReaderTheme,
} from '../composables/useReaderSettings'
import type { CSSProperties } from 'vue'
import type {
  ExpeditionComment,
  ExpeditionCommentTargetType,
  ExpeditionObserverReview,
  ExpeditionSettlementConflictRepairPreview,
  S1ObjectiveOutline,
  AiFeedbackTarget,
} from '../stores/adventurerS1'

const props = withDefaults(defineProps<{ logId: number; isAdmin?: boolean }>(), {
  isAdmin: false,
})
const emit = defineEmits<{
  close: []
  'action-done': [newLogId?: number]
  open: [payload: { id: number; name?: string }]
}>()

const store = useAdventurerS1Store()
const socket = useSocket()
const message = useGlobalMessage()
const loading = ref(true)
const logActionLoading = ref(false)
const settlementRepairOpen = ref(false)
const settlementRepairLoading = ref(false)
const settlementRepairPreview = ref<ExpeditionSettlementConflictRepairPreview | null>(null)
const observerReviewLoading = ref(false)
const observerApplyLoading = ref(false)
const observerRetryLoading = ref(false)
const likeLoading = ref(false)
const commentPanelOpen = ref(false)
const commentLoading = ref(false)
const commentLoadingMore = ref(false)
const commentSubmitting = ref(false)
const commentTargetType = ref<ExpeditionCommentTargetType>('event')
const commentTargetIndex = ref(0)
const commentDraft = ref('')
const comments = ref<ExpeditionComment[]>([])
const commentPage = ref(1)
const commentHasNext = ref(false)
const commentRequestId = ref(0)
const revealedEmptyCommentTarget = ref<string | null>(null)
const commentHintVisible = ref(false)
const COMMENT_DISCOVERY_HINT_KEY = 'adventurer-s1:expedition-comment-hint:v1'
const observerReviewOpen = ref(false)
const objectiveOutlineOpen = ref(false)
const worldIntelOpen = ref(false)
const observerReview = ref<ExpeditionObserverReview | null>(null)
const isFullscreen = ref(false)
const readProgress = ref(0)
let readProgressFrame: number | null = null
let readerUnmounted = false
const readerRef = ref<HTMLElement | null>(null)
const backgroundInputRef = ref<HTMLInputElement | null>(null)
const readerSettingsOpen = ref(false)

function updateAiFeedback(target: AiFeedbackTarget) {
  if (store.currentLog?.id === props.logId) {
    store.currentLog.ai_feedback = target
  }
}

const {
  fontSize,
  theme,
  lineHeight,
  fontSizeLabel,
  changeFontSize,
  setTheme,
  prefs,
  updatePrefs,
  resetPrefs,
  fontOptions,
  themeOptions,
} = useReaderSettings()

const {
  backgroundUrl,
  backgroundLoading,
  loadBackground,
  importBackground,
  clearBackground,
  disposeBackground,
} = useReaderBackground()

const readerWeightOptions = [400, 500, 600] as const
const readerWidthOptions = [
  { id: 'narrow' as const, label: '窄' },
  { id: 'standard' as const, label: '标准' },
  { id: 'wide' as const, label: '宽' },
]

const proseStyle = computed<CSSProperties>(() => ({
  fontSize: `${fontSize.value}px`,
  lineHeight: lineHeight.value,
  fontFamily: READER_FONT_STACKS[prefs.value.fontFamily],
  fontWeight: prefs.value.fontWeight,
  letterSpacing: `${prefs.value.letterSpacing}px`,
  maxWidth: `${READER_WIDTHS[prefs.value.readerWidth]}px`,
  '--reader-paragraph-spacing': `${prefs.value.paragraphSpacing}px`,
  '--reader-panel-opacity': `${Math.round(prefs.value.backgroundPanelOpacity * 100)}%`,
  '--reader-panel-blur': `${prefs.value.backgroundBlur}px`,
} as CSSProperties))

const readerBackgroundStyle = computed<CSSProperties>(() => {
  if (!backgroundUrl.value) return {}
  const cover = Math.round((1 - prefs.value.backgroundImageOpacity) * 100)
  return {
    backgroundImage: `linear-gradient(color-mix(in srgb, var(--reader-theme-background) ${cover}%, transparent), color-mix(in srgb, var(--reader-theme-background) ${cover}%, transparent)), url("${backgroundUrl.value}")`,
    backgroundSize: prefs.value.backgroundFit,
    backgroundPosition: 'center',
    backgroundRepeat: 'no-repeat',
    backgroundAttachment: 'local',
  }
})

const log = computed(() => {
  const cur = store.currentLog
  return cur?.id === props.logId ? cur : null
})
const isAdmin = computed(() => props.isAdmin)
const observerReviewOutput = computed(() => {
  if (observerReview.value?.result) return JSON.stringify(observerReview.value.result, null, 2)
  return observerReview.value?.raw_response || ''
})

const OBJECTIVE_OUTLINE_LABELS: Record<string, string> = {
  objective_situation: '客观局势',
  core_conflict: '核心矛盾',
  threat_source: '威胁来源',
  entry_path: '进入路径',
  event_chain: '事件链',
  turning_points: '客观转折',
  success_conditions: '成功条件',
  failure_modes: '失败模式',
  exit_condition: '收束条件',
  reward_sources: '收益来源',
  risk_tags: '风险结构',
}

function formatObjectiveOutline(outline: S1ObjectiveOutline | null | undefined): string {
  if (!outline || typeof outline !== 'object') return ''
  const lines: string[] = []
  for (const [key, label] of Object.entries(OBJECTIVE_OUTLINE_LABELS)) {
    const value = outline[key as keyof S1ObjectiveOutline]
    if (value == null || value === '') continue
    if (key === 'event_chain' && Array.isArray(value)) {
      const parts = value.map((item) => {
        if (item && typeof item === 'object' && 'event' in item) {
          const stage = String((item as { stage?: string }).stage || '').trim()
          const event = String((item as { event?: string }).event || '').trim()
          return stage ? `${stage}: ${event}` : event
        }
        return String(item).trim()
      }).filter(Boolean)
      if (parts.length) lines.push(`${label}：${parts.join('；')}`)
      continue
    }
    if (Array.isArray(value)) {
      const text = value.map((item) => String(item).trim()).filter(Boolean).join('；')
      if (text) lines.push(`${label}：${text}`)
      continue
    }
    const text = String(value).trim()
    if (text) lines.push(`${label}：${text}`)
  }
  return lines.join('\n\n')
}

const objectiveOutline = computed<S1ObjectiveOutline | null>(() => {
  const detail = log.value
  const fromTop = detail?.objective_outline
  if (fromTop && typeof fromTop === 'object') return fromTop
  const fromFullLog = detail?.full_log?.objective_outline
  if (fromFullLog && typeof fromFullLog === 'object') return fromFullLog
  return null
})

const objectiveOutlineText = computed(() => formatObjectiveOutline(objectiveOutline.value))
const objectiveOutlineRejectedReason = computed(() => {
  const detail = log.value as any
  return String(
    detail?.objective_outline_rejected_reason
    || detail?.full_log?.objective_outline_rejected_reason
    || '',
  ).trim()
})
const objectiveOutlineRawExcerpt = computed(() => {
  const detail = log.value as any
  return String(
    detail?.objective_outline_raw_excerpt
    || detail?.full_log?.objective_outline_raw_excerpt
    || '',
  ).trim()
})
interface ObjectiveOutlineValidationWarning {
  profile_index?: number
  field?: string
  issue?: string
  action?: string
}
const objectiveOutlineValidationWarnings = computed<ObjectiveOutlineValidationWarning[]>(() => {
  const detail = log.value as any
  const warnings = detail?.objective_outline_validation_warnings
    || detail?.full_log?.objective_outline_validation_warnings
  return Array.isArray(warnings) ? warnings : []
})
const formatObjectiveOutlineWarning = (warning: ObjectiveOutlineValidationWarning) => {
  if (warning.issue === 'numeric_string_coerced_to_integer') {
    return `字段 ${warning.field || '未知'} 的纯数字字符串已兼容转换为 JSON 整数`
  }
  if (warning.issue === 'json_repair_applied') {
    return '草案 JSON 结构已修复，并重新通过完整契约校验'
  }
  const profile = Number.isInteger(warning.profile_index) ? `画像 #${warning.profile_index}` : '能力画像容器'
  const field = warning.field ? `，字段 ${warning.field}` : ''
  const action = warning.action === 'dropped_profile' ? '，已丢弃该画像' : '，已降级为空数组'
  return `${profile}${field}${action}`
}
interface WorldIntelDebugItem {
  id?: number
  title?: string
  match_score?: number
  matched_base_dungeon?: boolean
  matched_trigger_keywords?: string[]
  matched_scope_keys?: string[]
  matched_context_terms?: string[]
  matched_text_tokens?: string[]
}

interface WorldIntelDebugSnapshot {
  target_dungeon?: string
  context_text?: string
  context_keywords?: string[]
  items?: WorldIntelDebugItem[]
  prompt_block?: string
}

function normalizeWorldIntelSnapshot(value: unknown): WorldIntelDebugSnapshot | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  return value as WorldIntelDebugSnapshot
}

const worldIntelRecords = computed(() => {
  const fullLog = (log.value?.full_log && typeof log.value.full_log === 'object') ? log.value.full_log as any : {}
  const defs = [
    { key: 'objective_world_intel_injection', label: '客观局势草案 AI' },
    { key: 'generation_world_intel_injection', label: '主剧情 AI' },
  ]
  return defs
    .map((def) => {
      const snapshot = normalizeWorldIntelSnapshot(fullLog[def.key])
      if (!snapshot) return null
      return {
        ...def,
        snapshot,
        items: Array.isArray(snapshot.items) ? snapshot.items : [],
      }
    })
    .filter((row): row is { key: string; label: string; snapshot: WorldIntelDebugSnapshot; items: WorldIntelDebugItem[] } => Boolean(row))
})

function joinOrNone(values?: unknown[]) {
  const texts = Array.isArray(values)
    ? values.map((value) => String(value ?? '').trim()).filter(Boolean)
    : []
  return texts.length ? texts.join('、') : '无'
}
const observerForbiddenOverrideFields = new Set([
  'events',
  'status',
  'current_event_index',
  'pending_pre_events',
  'expedition_log_id',
  'party_name',
  'target_dungeon',
])
const observerRawOverrideFields = computed<Record<string, unknown>>(() => {
  const result = observerReview.value?.result as any
  const fields = result?.override_fields
  return fields && typeof fields === 'object' && !Array.isArray(fields) ? fields : {}
})
const observerReviewIgnoredFields = computed(() => (
  Object.keys(observerRawOverrideFields.value).filter((field) => observerForbiddenOverrideFields.has(field))
))
const observerReviewPreview = computed(() => {
  if (Array.isArray(observerReview.value?.preview)) {
    return observerReview.value.preview
  }
  const fullLog = (log.value?.full_log && typeof log.value.full_log === 'object') ? log.value.full_log as any : {}
  return Object.entries(observerRawOverrideFields.value)
    .filter(([field]) => !observerForbiddenOverrideFields.has(field))
    .map(([field, after]) => {
      const before = fullLog[field]
      return {
        field,
        before,
        after,
        changed: JSON.stringify(before) !== JSON.stringify(after),
      }
    })
})
const observerGlobalPreview = computed(() => (
  observerReviewPreview.value.filter((row) => row.field !== 'member_updates')
))
const observerMemberPreview = computed(() => {
  const row = observerReviewPreview.value.find((item) => item.field === 'member_updates')
  if (!row || !Array.isArray(row.after)) return []
  const beforeList = Array.isArray(row.before) ? row.before : []
  return buildMemberPreview(beforeList, row.after)
})
const observerReviewCanApply = computed(() => (
  observerReview.value?.status === 'completed'
  && observerReviewPreview.value.some((row) => row.changed)
))

interface ObserverMemberChange {
  field: string
  label: string
  before: string
  after: string
}

interface ObserverMemberPreview {
  key: string
  id?: number | string
  name: string
  changes: ObserverMemberChange[]
}

const OBSERVER_MEMBER_FIELD_LABELS: Record<string, string> = {
  gold_change: '金币',
  score: '评分',
  hp_change: '生命变化',
  remaining_hp: '剩余生命',
  is_dead: '生死状态',
  death_reason: '死亡原因',
  new_class: '职业',
  new_traits: '新增/更新特质',
  removed_traits: '移除特质',
  new_skills: '新增技能',
  removed_skills: '移除技能',
  new_equipments: '获得装备/道具',
  consumed_equipments: '消耗装备/道具',
  used_equipments: '使用装备/道具（历史字段）',
  used_skills: '使用技能',
  history_story_append: '个人经历',
  strength_increase: '力量成长',
  agility_increase: '敏捷成长',
  intelligence_increase: '智力成长',
  luck_increase: '幸运成长',
  max_hp_increase: '生命上限',
  relationship_changes: '羁绊变化',
  story_threads: '剧情线',
}

const OBSERVER_MEMBER_FIELD_ORDER = [
  'gold_change',
  'score',
  'hp_change',
  'remaining_hp',
  'is_dead',
  'death_reason',
  'new_class',
  'new_equipments',
  'consumed_equipments',
  'used_equipments',
  'new_traits',
  'removed_traits',
  'new_skills',
  'removed_skills',
  'used_skills',
  'strength_increase',
  'agility_increase',
  'intelligence_increase',
  'luck_increase',
  'max_hp_increase',
  'history_story_append',
  'relationship_changes',
  'story_threads',
]

const OBSERVER_IGNORED_MEMBER_FIELDS = new Set(['id', 'name'])

function formatPreviewValue(value: unknown): string {
  if (typeof value === 'string') return value
  if (value === undefined) return '(未设置)'
  return JSON.stringify(value, null, 2)
}

function observerFieldLabel(field: string): string {
  const labels: Record<string, string> = {
    summary: '摘要',
    member_updates: '成员结算',
  }
  return labels[field] || field
}

function memberPreviewKey(member: any, index: number): string {
  if (member && typeof member === 'object') {
    if (member.id !== undefined && member.id !== null) return `id:${member.id}`
    if (member.name) return `name:${String(member.name)}`
  }
  return `idx:${index}`
}

function buildMemberPreview(beforeList: unknown[], afterList: unknown[]): ObserverMemberPreview[] {
  const beforeByKey = new Map<string, any>()
  beforeList.forEach((member, index) => {
    beforeByKey.set(memberPreviewKey(member, index), member)
  })

  const rows: ObserverMemberPreview[] = []
  afterList.forEach((after, index) => {
    if (!after || typeof after !== 'object') return
    const key = memberPreviewKey(after, index)
    const before = beforeByKey.get(key)
    const changes = buildMemberChanges(before, after)
    if (!changes.length) return
    rows.push({
      key,
      id: (after as any).id,
      name: String((after as any).name || (before as any)?.name || `成员 ${index + 1}`),
      changes,
    })
    beforeByKey.delete(key)
  })

  return rows
}

function buildMemberChanges(before: any, after: any): ObserverMemberChange[] {
  if (!before || typeof before !== 'object') {
    return [{
      field: '__member_added',
      label: '成员结算',
      before: '(无)',
      after: '新增整条结算',
    }]
  }
  const fields = new Set<string>([
    ...OBSERVER_MEMBER_FIELD_ORDER,
    ...Object.keys(before || {}),
    ...Object.keys(after || {}),
  ])
  return [...fields]
    .filter((field) => !OBSERVER_IGNORED_MEMBER_FIELDS.has(field))
    .filter((field) => stringifyComparable(before?.[field]) !== stringifyComparable(after?.[field]))
    .map((field) => ({
      field,
      label: OBSERVER_MEMBER_FIELD_LABELS[field] || field,
      before: formatMemberFieldValue(field, before?.[field]),
      after: formatMemberFieldValue(field, after?.[field]),
    }))
}

function stringifyComparable(value: unknown): string {
  return JSON.stringify(value ?? null)
}

function formatMemberFieldValue(field: string, value: unknown): string {
  if (value === undefined || value === null || value === '') return '(空)'
  if (field === 'is_dead') return value ? '阵亡' : '存活'
  if (field === 'gold_change') {
    const n = Number(value)
    return Number.isFinite(n) ? `${n > 0 ? '+' : ''}${n.toFixed(0)} 金币` : String(value)
  }
  if (field.endsWith('_increase') || field === 'hp_change' || field === 'remaining_hp' || field === 'score') {
    const n = Number(value)
    return Number.isFinite(n) ? `${n > 0 && field !== 'remaining_hp' && field !== 'score' ? '+' : ''}${n}` : String(value)
  }
  if (field === 'new_equipments') return formatEquipmentMap(value)
  if (field === 'consumed_equipments' || field === 'used_equipments') return formatNameList(value)
  if (field === 'new_traits' || field === 'removed_traits' || field === 'new_skills' || field === 'removed_skills' || field === 'used_skills') {
    return formatNameList(value)
  }
  if (typeof value === 'string') return compactText(value)
  return compactText(JSON.stringify(value, null, 2))
}

function compactText(value: string): string {
  const text = value.trim()
  if (!text) return '(空)'
  return text.length > 140 ? `${text.slice(0, 140)}...` : text
}

function stripPreviewName(raw: string): string {
  return raw.replace(/\s*[（(].*[）)]\s*$/, '').trim()
}

function formatNameList(source: unknown): string {
  const names = extractPreviewNames(source)
  return names.length ? names.join('、') : '(空)'
}

function extractPreviewNames(source: unknown): string[] {
  if (!source) return []
  if (Array.isArray(source)) {
    return source
      .map((item) => {
        if (typeof item === 'string') return stripPreviewName(item)
        if (item && typeof item === 'object' && typeof (item as { name?: string }).name === 'string') {
          const quantity = Number((item as { quantity?: unknown }).quantity)
          const suffix = Number.isFinite(quantity) && quantity > 1 ? ` ×${quantity}` : ''
          return `${stripPreviewName((item as { name: string }).name)}${suffix}`
        }
        return ''
      })
      .filter(Boolean)
  }
  if (typeof source === 'object') {
    return Object.keys(source as Record<string, unknown>).map(stripPreviewName).filter(Boolean)
  }
  return [String(source)]
}

function formatEquipmentMap(source: unknown): string {
  if (!source || typeof source !== 'object' || Array.isArray(source)) return formatNameList(source)
  const items = Object.entries(source as Record<string, any>).map(([rawName, data]) => {
    const quantity = Number(data?.quantity)
    const rarity = data?.rarity ? `/${data.rarity}` : ''
    const suffix = Number.isFinite(quantity) && quantity > 1 ? ` x${quantity}` : ''
    return `${stripPreviewName(rawName)}${suffix}${rarity}`
  })
  return items.length ? items.join('、') : '(空)'
}

function observerReviewStatusLabel(status: ExpeditionObserverReview['status']): string {
  return {
    pending: '等待中',
    processing: '审核中',
    completed: '通过',
    failed: '失败',
  }[status]
}

function observerReviewConsumptionStatusLabel(
  status: ExpeditionObserverReview['consumption_status'],
): string {
  const labels: Record<string, string> = {
    pending: '待定',
    succeeded: '通过',
    failed: '失败',
    not_applicable: '不适用',
  }
  return labels[status || ''] || '历史未知'
}

async function toggleObserverReview(logId: number) {
  observerReviewOpen.value = !observerReviewOpen.value
  if (!observerReviewOpen.value || observerReviewLoading.value) return
  observerReviewLoading.value = true
  try {
    const res = await store.adminGetExpeditionObserverReview(logId)
    if (res.code === 200) {
      observerReview.value = res.data?.review ?? null
    } else {
      message.error(res.message || '读取 AI 剧情审核失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '读取 AI 剧情审核失败')
  } finally {
    observerReviewLoading.value = false
  }
}

async function doApproveObserverReview(reviewId: number) {
  if (!observerReviewCanApply.value) return
  if (!confirm('确定应用独立剧情审核的覆盖建议？该操作只覆盖探险日志 full_log 字段，不会重新结算奖励。')) return
  observerApplyLoading.value = true
  try {
    const res = await store.adminApproveExpeditionObserverReview(reviewId)
    if (res.code === 200) {
      observerReview.value = res.data?.review ?? observerReview.value
      message.success(`已应用覆盖：${(res.data?.applied_fields || []).join(', ') || '无变化'}`)
    } else {
      message.error(res.message || '应用覆盖失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '应用覆盖失败')
  } finally {
    observerApplyLoading.value = false
  }
}

async function doRetryObserverReview(logId: number) {
  observerRetryLoading.value = true
  try {
    const res = await store.adminRetryExpeditionObserverReview(logId)
    if (res.code === 200) {
      message.success('已提交重试，审核结果将在稍后更新')
      // 刷新审核状态
      const reviewRes = await store.adminGetExpeditionObserverReview(logId)
      if (reviewRes.code === 200) {
        observerReview.value = reviewRes.data?.review ?? null
      }
    } else {
      message.error(res.message || '重试失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '重试失败')
  } finally {
    observerRetryLoading.value = false
  }
}

const events = computed<unknown[]>(() => {
  const fl: any = log.value?.full_log
  if (fl && Array.isArray(fl.events)) return fl.events
  return []
})

const specialEventType = computed<string | null>(() => {
  if (!isSpecialEventPresentationStatus(log.value?.status)) return null
  if (log.value?.special_event_theme) return log.value.special_event_theme
  const fl: any = log.value?.full_log
  if (!fl?.has_special_event) return null
  return fl.special_event_type
    || fl.special_event_risk_tier
    || (fl.is_extreme_crisis ? 'extreme_crisis' : 'special_event')
})

const specialEventPresentation = computed(() => {
  return getSpecialEventPresentation(specialEventType.value)
})

function specialEventClass(idx: number): string | null {
  if (!specialEventType.value || idx !== log.value?.special_event_event_index) return null
  return `se-${specialEventType.value}`
}

// 成员名字颜色：一键自动分配不同颜色，纯本地 localStorage
const { setColor, getColor, autoHighlightEnabled, setAutoHighlightEnabled } = useNameColors()

const HIGHLIGHT_COLORS = [
  '#dc2626', '#ea580c', '#d97706', '#16a34a',
  '#0d9488', '#0284c7', '#7c3aed', '#c026d3',
  '#e11d48', '#f59e0b', '#3b82f6', '#65a30d',
]

// 兼容新旧格式：新格式是字符串数组 ["name1", "name2"]，旧格式是对象数组 [{id, name}]
function normalizeMembers(members: unknown): string[] {
  if (!Array.isArray(members)) return []
  return [...new Set(
    members
      .map((member) => {
        if (typeof member === 'string') return member
        if (!member || typeof member !== 'object' || !('name' in member)) return ''
        return member.name
      })
      .map(name => String(name ?? '').trim())
      .filter(Boolean),
  )]
}

function getLogMemberNames(): string[] {
  const detail = log.value
  const fromTop = normalizeMembers(detail?.members)
  if (fromTop.length) return fromTop
  const fl = detail?.full_log
  const fromFullLogMembers = normalizeMembers(fl?.members)
  if (fromFullLogMembers.length) return fromFullLogMembers
  const names: string[] = []
  const pushName = (name: unknown) => {
    const text = String(name ?? '').trim()
    if (text && !names.includes(text)) names.push(text)
  }
  if (Array.isArray(fl?.member_updates)) {
    for (const update of fl.member_updates) {
      pushName(update?.name)
    }
  }
  if (names.length) return names
  const relationChanges = detail?.relationship_changes
  if (Array.isArray(relationChanges)) {
    for (const change of relationChanges) {
      pushName(change?.adventurer_a_name)
      pushName(change?.adventurer_b_name)
    }
  }
  return names
}

const highlightableMemberNames = computed(() => getLogMemberNames())

function applyHighlightColors(names: string[]) {
  const sorted = [...new Set(names)].sort()
  const usedColors = new Set(
    sorted
      .map(name => getColor(name))
      .filter((color): color is string => Boolean(color)),
  )
  let paletteIndex = 0
  for (const name of sorted) {
    if (getColor(name)) continue
    while (
      usedColors.has(HIGHLIGHT_COLORS[paletteIndex % HIGHLIGHT_COLORS.length])
      && usedColors.size < HIGHLIGHT_COLORS.length
    ) {
      paletteIndex += 1
    }
    const color = HIGHLIGHT_COLORS[paletteIndex % HIGHLIGHT_COLORS.length]
    setColor(name, color)
    usedColors.add(color)
    paletteIndex += 1
  }
}

const memberColorMap = computed<Map<string, string>>(() => {
  if (!autoHighlightEnabled.value) return new Map()
  const names = highlightableMemberNames.value
  if (!names.length) return new Map()
  const map = new Map<string, string>()
  for (const name of names) {
    const color = getColor(name)
    if (name && color) {
      map.set(name, color)
    }
  }
  return map
})

const isHighlighted = computed(() => autoHighlightEnabled.value)

function autoHighlightNames() {
  const names = highlightableMemberNames.value
  if (!names.length) {
    message.info('当前日志还没有可用于高亮的成员名字')
    return
  }
  setAutoHighlightEnabled(true)
  applyHighlightColors(names)
}

function clearHighlightNames() {
  setAutoHighlightEnabled(false)
  const names = highlightableMemberNames.value
  for (const name of names) {
    setColor(name, null)
  }
}

watch(highlightableMemberNames, (names) => {
  if (!autoHighlightEnabled.value || !names.length) return
  applyHighlightColors(names)
}, { immediate: true })

interface TextSegment {
  text: string
  color?: string
}

function splitTextByNames(text: string): TextSegment[] {
  const colorMap = memberColorMap.value
  if (!colorMap.size) return [{ text }]

  // 按名字长度降序排序，避免短名字先匹配
  const names = [...colorMap.keys()].sort((a, b) => b.length - a.length)
  const pattern = new RegExp(names.map(n => n.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|'), 'g')
  const segments: TextSegment[] = []
  let lastIdx = 0

  for (const match of text.matchAll(pattern)) {
    const idx = match.index!
    if (idx > lastIdx) {
      segments.push({ text: text.slice(lastIdx, idx) })
    }
    segments.push({ text: match[0], color: colorMap.get(match[0]) })
    lastIdx = idx + match[0].length
  }
  if (lastIdx < text.length) {
    segments.push({ text: text.slice(lastIdx) })
  }
  return segments.length ? segments : [{ text }]
}

const relationChanges = computed(() => log.value?.relationship_changes ?? [])
const likeInfo = computed(() => log.value?.like_info ?? null)

const memberUpdates = computed(() => {
  const fl = log.value?.full_log
  if (!fl || !Array.isArray(fl.member_updates)) return []
  return fl.member_updates
})

function commentCount(targetType: ExpeditionCommentTargetType, targetIndex: number): number {
  const count = Number(log.value?.comment_counts?.[targetType]?.[String(targetIndex)] ?? 0)
  return Number.isFinite(count) && count > 0 ? count : 0
}

function isCommentable(targetType: ExpeditionCommentTargetType, targetIndex: number): boolean {
  const counts = log.value?.comment_counts?.[targetType]
  return Boolean(counts && Object.prototype.hasOwnProperty.call(counts, String(targetIndex)))
}

const hasCommentableTargets = computed(() => {
  const counts = log.value?.comment_counts
  return Boolean(
    counts
    && (Object.keys(counts.event ?? {}).length || Object.keys(counts.member_update ?? {}).length),
  )
})

function commentTargetKey(targetType: ExpeditionCommentTargetType, targetIndex: number): string {
  return `${targetType}:${targetIndex}`
}

function isEmptyCommentRevealed(
  targetType: ExpeditionCommentTargetType,
  targetIndex: number,
): boolean {
  return revealedEmptyCommentTarget.value === commentTargetKey(targetType, targetIndex)
}

function revealEmptyComment(targetType: ExpeditionCommentTargetType, targetIndex: number) {
  if (!isCommentable(targetType, targetIndex) || commentCount(targetType, targetIndex)) return
  revealedEmptyCommentTarget.value = commentTargetKey(targetType, targetIndex)
}

function dismissCommentHint() {
  commentHintVisible.value = false
}

function showCommentHintOnce() {
  if (!hasCommentableTargets.value) return
  try {
    if (window.localStorage.getItem(COMMENT_DISCOVERY_HINT_KEY) === '1') return
    window.localStorage.setItem(COMMENT_DISCOVERY_HINT_KEY, '1')
  } catch {
    // 存储不可用时仍展示本次提示，不阻断日志阅读。
  }
  commentHintVisible.value = true
}

watch(hasCommentableTargets, (available) => {
  if (available) showCommentHintOnce()
}, { immediate: true })

const commentTargetLabel = computed(() => {
  if (commentTargetType.value === 'event') {
    return `第 ${commentTargetIndex.value + 1} 段`
  }
  const update = memberUpdates.value[commentTargetIndex.value]
  const name = String(update?.name || '').trim()
  return name ? `${name}的成员状态更新` : `第 ${commentTargetIndex.value + 1} 条成员状态更新`
})

async function fetchComments(page = 1, append = false, requestId = commentRequestId.value) {
  if (!log.value?.id) return
  const targetType = commentTargetType.value
  const targetIndex = commentTargetIndex.value
  const res = await store.fetchExpeditionComments(
    log.value.id,
    targetType,
    targetIndex,
    page,
  )
  if (res.code !== 200) {
    throw new Error(res.message || '读取评论失败')
  }
  if (requestId !== commentRequestId.value || !commentPanelOpen.value) return
  const incoming = Array.isArray(res.data?.items) ? res.data.items as ExpeditionComment[] : []
  comments.value = append ? [...comments.value, ...incoming] : incoming
  commentPage.value = Number(res.data?.page ?? page)
  commentHasNext.value = Boolean(res.data?.has_next)
  const current = log.value
  if (current?.comment_counts) {
    current.comment_counts[targetType][String(targetIndex)] = Number(
      res.data?.total ?? 0,
    )
  }
}

async function openComments(targetType: ExpeditionCommentTargetType, targetIndex: number) {
  const requestId = ++commentRequestId.value
  revealedEmptyCommentTarget.value = null
  commentHintVisible.value = false
  commentTargetType.value = targetType
  commentTargetIndex.value = targetIndex
  commentDraft.value = ''
  comments.value = []
  commentPage.value = 1
  commentHasNext.value = false
  commentPanelOpen.value = true
  commentLoading.value = true
  try {
    await fetchComments(1, false, requestId)
  } catch (e: any) {
    if (requestId === commentRequestId.value) {
      message.error(e?.response?.data?.message || e?.message || '读取评论失败')
    }
  } finally {
    if (requestId === commentRequestId.value) commentLoading.value = false
  }
}

function closeComments() {
  if (commentSubmitting.value) return
  commentRequestId.value += 1
  commentPanelOpen.value = false
}

async function loadMoreComments() {
  if (commentLoadingMore.value || !commentHasNext.value) return
  commentLoadingMore.value = true
  try {
    await fetchComments(commentPage.value + 1, true)
  } catch (e: any) {
    message.error(e?.response?.data?.message || e?.message || '读取评论失败')
  } finally {
    commentLoadingMore.value = false
  }
}

async function submitComment() {
  const content = commentDraft.value.trim()
  if (!content || !log.value?.id || commentSubmitting.value) return
  commentSubmitting.value = true
  try {
    const res = await store.postExpeditionComment(
      log.value.id,
      commentTargetType.value,
      commentTargetIndex.value,
      content,
    )
    if (res.code !== 200 && res.code !== 201) {
      message.error(res.message || '评论发表失败')
      return
    }
    commentDraft.value = ''
    await fetchComments()
  } catch (e: any) {
    message.error(e?.response?.data?.message || '评论发表失败')
  } finally {
    commentSubmitting.value = false
  }
}

const departureText = computed(() => getExpeditionDepartureText(log.value?.full_log))

const generationRetryTrace = computed<any[]>(() => {
  const detail = log.value as any
  const trace = detail?.generation_retry_trace
    || detail?.full_log?.generation_retry_trace
  return Array.isArray(trace) ? trace : []
})
const latestLengthFailure = computed(() => (
  [...generationRetryTrace.value].reverse().find(
    entry => entry?.reason_code === 'truncated_output',
  ) || null
))
const generationFailureDetail = computed(() => {
  const detail = log.value as any
  return String(
    detail?.generation_failure_detail
    || detail?.full_log?.generation_failure_detail
    || latestLengthFailure.value?.detail
    || '',
  ).trim()
})
const generationFailureCategory = computed(() => {
  const detail = log.value as any
  return String(
    detail?.generation_failure_category
    || detail?.full_log?.generation_failure_category
    || '',
  ).trim()
})
const generationFailureCode = computed(() => {
  const detail = log.value as any
  return String(
    detail?.generation_failure_code
    || detail?.full_log?.generation_failure_code
    || '',
  ).trim()
})
const generationFailureIsLength = computed(() => (
  latestLengthFailure.value !== null
  || generationFailureDetail.value.includes('输出被长度上限截断')
  || generationFailureDetail.value.includes('truncated_output')
))
const generationRetryTraceText = computed(() => {
  return generationRetryTrace.value.length
    ? JSON.stringify(generationRetryTrace.value, null, 2)
    : ''
})

const generatingRetryAvailable = computed(() => {
  const cur = log.value as { generating_retry_available?: boolean } | null
  return cur?.generating_retry_available === true
})

const POLL_INTERVAL_MS = 60_000
let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer !== null) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function syncPolling() {
  const status = log.value?.status
  const inFlight = status === 'in_progress' || status === 'generating'
  if (inFlight) {
    stopPolling()
    pollTimer = setInterval(async () => {
      await store.fetchLogDetail(props.logId, { revealFull: isAdmin.value })
      syncPolling()
    }, POLL_INTERVAL_MS)
  } else {
    stopPolling()
  }
}

function onExpeditionLogChanged(payload: any) {
  const pushedId = payload?.data?.id
  if (pushedId != null && pushedId !== props.logId) return
  store.fetchLogDetail(props.logId, { revealFull: isAdmin.value }).then(() => {
    syncPolling()
    if (payload?.action === 'completed') {
      emit('action-done')
    }
  })
}

function eventText(ev: any): string {
  if (typeof ev === 'string') return ev
  if (ev && typeof ev === 'object') return ev.content ?? ev.text ?? ev.description ?? ''
  return String(ev ?? '')
}

function toggleFullscreen() {
  isFullscreen.value = !isFullscreen.value
  scheduleReadProgressUpdate()
}

function onThemeChange(e: Event) {
  const v = (e.target as HTMLSelectElement).value as ReaderTheme
  setTheme(v)
}

function setReaderChoice(key: 'fontFamily' | 'backgroundFit', event: Event) {
  const value = (event.target as HTMLSelectElement).value
  if (key === 'fontFamily') updatePrefs({ fontFamily: value as ReaderFontFamily })
  else updatePrefs({ backgroundFit: value as ReaderBackgroundFit })
}

type ReaderNumberKey = 'lineHeight' | 'letterSpacing' | 'paragraphSpacing'
  | 'backgroundImageOpacity' | 'backgroundPanelOpacity' | 'backgroundBlur'

function setReaderNumber(key: ReaderNumberKey, event: Event) {
  const value = Number((event.target as HTMLInputElement).value)
  if (!Number.isFinite(value)) return
  updatePrefs({ [key]: value })
}

async function onBackgroundSelected(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  try {
    await importBackground(file)
    message.success('本地阅读背景已保存')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '背景图片导入失败')
  }
}

async function removeReaderBackground() {
  try {
    await clearBackground()
    message.success('本地阅读背景已清除')
  } catch (error) {
    message.error(error instanceof Error ? error.message : '背景图片清除失败')
  }
}

function onReaderScroll() {
  scheduleReadProgressUpdate()
}

function scheduleReadProgressUpdate() {
  if (readerUnmounted || readProgressFrame !== null) return

  readProgressFrame = requestAnimationFrame(() => {
    readProgressFrame = null
    updateReadProgress()
  })
}

function updateReadProgress() {
  const el = readerRef.value
  if (!el) {
    readProgress.value = 0
    return
  }
  const max = el.scrollHeight - el.clientHeight
  if (max <= 0) {
    readProgress.value = 0
    return
  }
  readProgress.value = Math.min(100, Math.round((el.scrollTop / max) * 100))
}

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape') {
    e.preventDefault()
    if (commentPanelOpen.value) {
      closeComments()
    } else if (isFullscreen.value) {
      isFullscreen.value = false
    } else {
      emit('close')
    }
    return
  }
  if (e.key === 'f' && !e.ctrlKey && !e.metaKey && !e.altKey) {
    const tag = (e.target as HTMLElement)?.tagName
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return
    toggleFullscreen()
    e.preventDefault()
    return
  }
  if (e.key === '=' || e.key === '+') {
    const tag = (e.target as HTMLElement)?.tagName
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return
    changeFontSize(1)
    e.preventDefault()
    return
  }
  if (e.key === '-') {
    const tag = (e.target as HTMLElement)?.tagName
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA') return
    changeFontSize(-1)
    e.preventDefault()
  }
}

async function doStepExpeditionLog(logId: number) {
  if (logActionLoading.value) return
  logActionLoading.value = true
  try {
    const res = await store.adminStepExpeditionLog(logId)
    if (res.code === 200) {
      message.success(res.data?.status === 'completed' ? '已推进并完成结算' : '已推进一批日志')
      await store.fetchLogDetail(logId, { revealFull: isAdmin.value })
      syncPolling()
      emit('action-done')
    } else {
      message.error(res.message || '推进失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '推进失败')
  } finally {
    logActionLoading.value = false
  }
}

async function doLikeExpeditionLog(toAdventurerId: number) {
  if (!log.value || likeLoading.value) return
  likeLoading.value = true
  try {
    const res = await store.likeExpeditionLog(log.value.id, toAdventurerId)
    if (res.code === 200) {
      message.success(res.data?.message || '点赞成功')
      await store.fetchAdventurers(true)
    } else {
      message.error(res.message || '点赞失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '点赞失败')
  } finally {
    likeLoading.value = false
  }
}

async function doForceCompleteExpeditionLog(logId: number) {
  if (logActionLoading.value) return
  if (!window.confirm('确定直接释放剩余日志并立即结算吗？')) return
  logActionLoading.value = true
  try {
    const res = await store.adminForceCompleteExpeditionLog(logId)
    if (res.code === 200) {
      message.success('已直接结算')
      await store.fetchLogDetail(logId, { revealFull: isAdmin.value })
      syncPolling()
      emit('action-done')
    } else {
      message.error(res.message || '结算失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '结算失败')
  } finally {
    logActionLoading.value = false
  }
}

async function loadSettlementConflictRepairPreview(logId: number) {
  if (settlementRepairLoading.value) return
  settlementRepairLoading.value = true
  try {
    const res = await store.adminPreviewExpeditionSettlementConflictRepair(logId)
    if (res.code === 200) {
      settlementRepairPreview.value = res.data as ExpeditionSettlementConflictRepairPreview
    } else {
      settlementRepairPreview.value = null
      message.error(res.message || '结算冲突恢复核验失败')
    }
  } catch (e: any) {
    settlementRepairPreview.value = null
    message.error(e?.response?.data?.message || '结算冲突恢复核验失败')
  } finally {
    settlementRepairLoading.value = false
  }
}

async function toggleSettlementConflictRepair(logId: number) {
  if (settlementRepairOpen.value) {
    settlementRepairOpen.value = false
    settlementRepairPreview.value = null
    return
  }
  settlementRepairOpen.value = true
  await loadSettlementConflictRepairPreview(logId)
}

async function doExecuteSettlementConflictRepair(logId: number) {
  const preview = settlementRepairPreview.value
  if (logActionLoading.value || !preview?.preview_token) return
  const semanticConfirmationCount = preview.candidates.filter(
    candidate => candidate.semantic_confirmation_required,
  ).length
  const consumptionCount = preview.candidates.length - semanticConfirmationCount
  const repairParts = [
    consumptionCount > 0 ? `移除 ${consumptionCount} 项重复消耗` : '',
    semanticConfirmationCount > 0
      ? `确认并移除 ${semanticConfirmationCount} 项稳定装备修改后的同名重复新发放`
      : '',
  ].filter(Boolean).join('，')
  const actionText = preview.mode === 'retry'
    ? '重新在原日志上重放完整结算'
    : `${repairParts}，并在原日志上重放完整结算`
  const semanticWarning = semanticConfirmationCount > 0
    ? '请确认剧情中的同名新发放确为稳定装备修改的重复表示；精确同名特质依赖将改绑到该稳定装备 ID。'
    : ''
  if (!window.confirm(`确定${actionText}吗？不会重新调用 AI。${semanticWarning}`)) return
  logActionLoading.value = true
  try {
    const res = await store.adminExecuteExpeditionSettlementConflictRepair(
      logId,
      preview.preview_token,
    )
    if (res.code !== 200) {
      message.error(res.message || '结算冲突恢复失败')
      return
    }
    await store.fetchLogDetail(logId, { revealFull: isAdmin.value })
    syncPolling()
    if (res.data?.status === 'completed') {
      message.success('已修复结算冲突并完成同日志结算')
      settlementRepairOpen.value = false
      settlementRepairPreview.value = null
      emit('action-done')
      return
    }
    message.error(
      `日志补丁已保存，但结算重放仍失败：${res.data?.replay_error || '未知错误'}`,
    )
    await loadSettlementConflictRepairPreview(logId)
  } catch (e: any) {
    message.error(e?.response?.data?.message || '结算冲突恢复失败')
  } finally {
    logActionLoading.value = false
  }
}

async function doRestoreSettlementConflictRepair(logId: number) {
  const preview = settlementRepairPreview.value
  if (logActionLoading.value || !preview?.preview_token || !preview.can_restore) return
  if (!window.confirm('确定回滚尚未结算成功的日志补丁吗？玩家资产不会被直接修改。')) return
  logActionLoading.value = true
  try {
    const res = await store.adminRestoreExpeditionSettlementConflictRepair(
      logId,
      preview.preview_token,
    )
    if (res.code !== 200) {
      message.error(res.message || '日志补丁回滚失败')
      return
    }
    message.success('已回滚结算冲突日志补丁')
    await store.fetchLogDetail(logId, { revealFull: isAdmin.value })
    syncPolling()
    await loadSettlementConflictRepairPreview(logId)
  } catch (e: any) {
    message.error(e?.response?.data?.message || '日志补丁回滚失败')
  } finally {
    logActionLoading.value = false
  }
}

async function doRegenerateExpeditionLog(logId: number) {
  if (logActionLoading.value) return
  if (!window.confirm('确定重新生成这条失败的探险日志吗？')) return
  logActionLoading.value = true
  try {
    const res = await store.adminRegenerateExpeditionLog(logId)
    if (res.code === 200) {
      message.success('已重新生成探险日志')
      const newLogId = res.data?.log_id
      emit('action-done', typeof newLogId === 'number' ? newLogId : undefined)
    } else {
      message.error(res.message || '重新生成失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '重新生成失败')
  } finally {
    logActionLoading.value = false
  }
}

async function doRetryGeneratingExpeditionLog(logId: number) {
  if (logActionLoading.value) return
  if (!window.confirm('该日志生成超时，确定由 GM 重试吗？（沿用原日志，不重复扣精力）')) return
  logActionLoading.value = true
  try {
    const res = await store.adminRetryGeneratingExpeditionLog(logId)
    if (res.code === 200) {
      message.success('已重试生成探险日志')
      await store.fetchLogDetail(logId, { revealFull: isAdmin.value })
      syncPolling()
      emit('action-done')
    } else {
      message.error(res.message || '重试生成失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '重试生成失败')
  } finally {
    logActionLoading.value = false
  }
}

// ── 编辑文本模式 ──
const editMode = ref(false)
const editSaving = ref(false)
interface EditItem { path: string; label: string; value: string }
const editItems = ref<EditItem[]>([])

function toggleEditMode() {
  if (editMode.value) {
    editMode.value = false
    editItems.value = []
    return
  }
  const fl: any = log.value?.full_log || {}
  const items: EditItem[] = []
  // result_summary
  if (log.value?.result_summary) {
    items.push({ path: '__result_summary__', label: '摘要 (result_summary)', value: log.value.result_summary })
  }
  // summary
  if (fl.summary) {
    items.push({ path: 'summary', label: '全文摘要 (full_log.summary)', value: fl.summary })
  }
  // events 文本
  const events = fl.events || []
  for (let i = 0; i < events.length; i++) {
    const evt = events[i]
    if (typeof evt === 'string') {
      items.push({ path: `events.${i}`, label: `事件 #${i}`, value: evt })
    } else if (evt && typeof evt === 'object') {
      for (const key of ['text', 'description', 'content']) {
        if (evt[key] && typeof evt[key] === 'string') {
          items.push({ path: `events.${i}.${key}`, label: `事件 #${i}.${key}`, value: evt[key] })
        }
      }
    }
  }
  // member_updates 叙事字段
  const updates = fl.member_updates || []
  for (let i = 0; i < updates.length; i++) {
    const upd = updates[i]
    if (!upd || typeof upd !== 'object') continue
    for (const key of ['death_reason', 'history_story_append']) {
      if (upd[key] && typeof upd[key] === 'string') {
        items.push({ path: `member_updates.${i}.${key}`, label: `队员变动 #${i}.${key}`, value: upd[key] })
      }
    }
  }
  editItems.value = items
  editMode.value = true
}

async function saveEditText() {
  if (editSaving.value) return
  editSaving.value = true
  try {
    const logId = log.value?.id
    if (!logId) return
    const payload: { result_summary?: string; full_log_patches: { path: string; value: string }[] } = { full_log_patches: [] }
    for (const item of editItems.value) {
      if (item.path === '__result_summary__') {
        payload.result_summary = item.value
      } else {
        payload.full_log_patches.push({ path: item.path, value: item.value })
      }
    }
    const res = await store.adminEditLogText(logId, payload)
    if (res.code === 200) {
      message.success('已保存')
      editMode.value = false
      editItems.value = []
      await store.fetchLogDetail(logId, { revealFull: isAdmin.value })
    } else {
      message.error(res.message || '保存失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '保存失败')
  } finally {
    editSaving.value = false
  }
}

onMounted(async () => {
  socket.on('expedition_log_changed', onExpeditionLogChanged)
  document.addEventListener('keydown', onKeydown)
  void loadBackground().catch(() => message.error('本地阅读背景加载失败，可在阅读设置中重新选择'))
  try {
    await store.fetchLogDetail(props.logId, { revealFull: isAdmin.value })
  } finally {
    loading.value = false
    syncPolling()
    scheduleReadProgressUpdate()
  }
})

onUnmounted(() => {
  readerUnmounted = true
  if (readProgressFrame !== null) {
    cancelAnimationFrame(readProgressFrame)
    readProgressFrame = null
  }
  socket.off('expedition_log_changed', onExpeditionLogChanged)
  document.removeEventListener('keydown', onKeydown)
  stopPolling()
  disposeBackground()
})
</script>

<style scoped lang="scss">
.modal-overlay {
  position: fixed;
  inset: 0;
  z-index: 1400;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 16px;
  background: rgba(0, 0, 0, 0.55);

  &.is-fullscreen {
    padding: 0;
    background: var(--color-bg);
  }
}

.modal-card.s1-mobile-dialog {
  width: 100%;
  max-width: 860px;
  max-height: 90vh;
  display: flex;
  flex-direction: column;
  font-weight: 400;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  overflow: hidden;
  position: relative;

  &.is-fullscreen {
    max-width: none;
    max-height: none;
    width: 100%;
    height: 100%;
    border-radius: 0;
    border: none;
  }
}

.read-progress {
  position: absolute;
  top: 0;
  left: 0;
  right: 0;
  height: 3px;
  background: transparent;
  z-index: 2;
  opacity: 0;
  transition: opacity 0.2s;

  &.visible {
    opacity: 1;
  }
}

.read-progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width 0.1s linear;
}

.modal-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.modal-title {
  margin: 0;
  font-size: 16px;
  color: var(--color-text);
  flex-shrink: 0;
}

.modal-log-id {
  font-size: 13px;
  font-weight: 500;
  color: var(--color-text-secondary);
  font-variant-numeric: tabular-nums;
}

.head-tools {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.reader-toolbar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 2px 6px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.tool-btn {
  border: none;
  background: transparent;
  padding: 4px 8px;
  font-size: 13px;
  color: var(--color-text);
  cursor: pointer;
  border-radius: 4px;
  line-height: 1.2;

  &:hover {
    background: var(--color-surface);
  }

  &.active {
    color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 12%, var(--color-surface));
  }
}

.tool-label {
  font-size: 11px;
  color: var(--color-text-secondary);
  min-width: 18px;
  text-align: center;
}

.theme-select {
  border: none;
  background: transparent;
  font-size: 12px;
  color: var(--color-text);
  padding: 4px 6px;
  cursor: pointer;
  max-width: 88px;

  &:focus {
    outline: none;
  }
}

.reader-settings-panel {
  flex-shrink: 0;
  max-height: 48vh;
  overflow-y: auto;
  padding: 14px 16px;
  color: var(--color-text);
  background: var(--color-surface);
  border-bottom: 1px solid var(--color-border);
}

.reader-settings-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 12px 16px;
}

.reader-setting-field {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 6px;
  color: var(--color-text-secondary);
  font-size: 12px;

  > span {
    display: flex;
    justify-content: space-between;
    gap: 8px;
  }

  b {
    color: var(--color-text);
    font-weight: 500;
  }

  select {
    width: 100%;
    min-height: 32px;
    padding: 4px 8px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 7px;
  }
}

.reader-setting-range input {
  width: 100%;
  accent-color: var(--color-primary);
}

.reader-choice-group {
  display: grid;
  grid-auto-flow: column;
  grid-auto-columns: minmax(0, 1fr);
  gap: 4px;

  button {
    min-height: 32px;
    padding: 4px 7px;
    color: var(--color-text-secondary);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 7px;
    cursor: pointer;
  }

  button.active {
    color: var(--color-primary);
    border-color: color-mix(in srgb, var(--color-primary) 60%, var(--color-border));
    background: color-mix(in srgb, var(--color-primary) 10%, var(--color-bg));
  }
}

.reader-background-settings {
  margin-top: 14px;
  padding-top: 14px;
  border-top: 1px solid var(--color-border);
}

.reader-background-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 14px;

  strong,
  small { display: block; }
  strong { margin-bottom: 3px; font-size: 13px; }
  small { color: var(--color-text-secondary); font-size: 11px; line-height: 1.45; }
}

.reader-background-actions {
  display: flex;
  flex-shrink: 0;
  gap: 6px;

  button {
    min-height: 32px;
    padding: 4px 10px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 7px;
    cursor: pointer;
  }

  button:disabled { cursor: wait; opacity: 0.55; }
  .reader-clear-btn { color: var(--color-danger); }
}

.visually-hidden-input {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip-path: inset(50%);
  white-space: nowrap;
}

.reader-background-controls { margin-top: 12px; }
.reader-settings-foot { display: flex; justify-content: flex-end; margin-top: 12px; }

.reader-reset-btn {
  padding: 4px 9px;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 7px;
  cursor: pointer;
}

.modal-close {
  border: none;
  background: transparent;
  font-size: 18px;
  color: var(--color-text-secondary);
  cursor: pointer;
  padding: 4px 6px;
  flex-shrink: 0;

  &:hover {
    color: var(--color-text);
  }
}

.state {
  padding: 24px 0;
  text-align: center;
  color: var(--color-text-secondary);

  &.subtle {
    padding: 12px 0;
    font-size: 13px;
  }

  &.state-loading {
    padding: 48px 16px;
  }
}

.log-meta-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  padding: 10px 16px;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.log-admin-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  padding: 8px 16px;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;
}

.observer-review-panel {
  max-height: 42vh;
  overflow: auto;
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  flex-shrink: 0;
}

.observer-review-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 12px;
  margin-bottom: 10px;
  font-size: 12px;
  color: var(--color-text-secondary);

  strong {
    font-size: 14px;
    color: var(--color-text);
  }
}

.review-status-completed { color: var(--color-success); }
.review-status-failed { color: var(--color-danger); }
.review-status-pending,
.review-status-processing { color: var(--color-warning); }
.review-consumption-succeeded { color: var(--color-success); }
.review-consumption-failed { color: var(--color-danger); }
.review-consumption-pending { color: var(--color-warning); }
.review-consumption-not_applicable,
.review-consumption-unknown { color: var(--color-text-secondary); }

.edit-text-panel {
  max-height: 50vh;
  overflow: auto;
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-border);
  background: var(--color-surface);
  color: var(--color-text);
  flex-shrink: 0;
}

.edit-text-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
  font-size: 14px;
}

.edit-text-item {
  margin-bottom: 12px;
}

.edit-text-label {
  display: block;
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 4px;
  font-family: monospace;
}

.edit-textarea {
  width: 100%;
  min-height: 60px;
  max-height: 200px;
  padding: 8px;
  font-size: 13px;
  line-height: 1.5;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
  resize: vertical;
  font-family: inherit;

  &:focus {
    outline: none;
    border-color: var(--color-primary);
  }
}

.observer-review-output {
  margin: 0;
  padding: 12px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
  font: 12px/1.6 ui-monospace, SFMono-Regular, Consolas, monospace;
}

.world-intel-panel {
  display: grid;
  gap: 12px;
}

.world-intel-record {
  display: grid;
  gap: 10px;
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.world-intel-record-head,
.world-intel-item-title,
.world-intel-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.world-intel-meta {
  justify-content: flex-start;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.world-intel-items {
  display: grid;
  gap: 8px;
}

.world-intel-item {
  display: grid;
  gap: 8px;
  padding: 8px;
  border: 1px dashed var(--color-border);
  border-radius: 8px;
}

.world-intel-match-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
  font-size: 12px;

  div {
    display: grid;
    gap: 2px;
  }

  em {
    color: var(--color-text-secondary);
    font-style: normal;
  }
}

.world-intel-details {
  font-size: 12px;

  &.compact {
    color: var(--color-text-secondary);
  }

  summary {
    cursor: pointer;
    color: var(--color-text);
    margin-bottom: 6px;
  }
}

.world-intel-token-note {
  margin: 0 0 4px;
  color: var(--color-text-secondary);
}

.objective-outline-rejected {
  display: grid;
  gap: 8px;
}

.observer-review-preview {
  margin: 10px 0;
  font-size: 12px;

  summary {
    cursor: pointer;
    color: var(--color-text);
  }
}

.observer-member-preview-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-top: 10px;
}

.observer-member-card {
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.observer-member-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  margin-bottom: 8px;
  color: var(--color-text);

  strong {
    font-size: 13px;
  }

  span {
    color: var(--color-text-secondary);
  }
}

.preview-count {
  padding: 1px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-warning) 14%, transparent);
  color: var(--color-warning) !important;
}

.observer-change-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.observer-change-row {
  display: grid;
  grid-template-columns: 104px minmax(0, 1fr) 14px minmax(0, 1fr);
  align-items: start;
  gap: 6px;
  padding: 6px 8px;
  border-radius: 6px;
  background: var(--color-surface);
}

.observer-change-label {
  font-weight: 600;
  color: var(--color-text);
}

.observer-change-before,
.observer-change-after {
  min-width: 0;
  overflow-wrap: anywhere;
}

.observer-change-before {
  color: var(--color-text-secondary);
}

.observer-change-after {
  color: var(--color-success);
}

.observer-change-arrow {
  color: var(--color-text-secondary);
  text-align: center;
}

.observer-global-preview {
  margin-top: 10px;
}

.observer-preview-row {
  margin-top: 8px;
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
}

.observer-preview-row-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.preview-changed {
  color: var(--color-warning);
}

.preview-same {
  color: var(--color-text-secondary);
}

.observer-preview-grid {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
  gap: 8px;
  margin-top: 8px;

  em {
    display: block;
    margin-bottom: 4px;
    color: var(--color-text-secondary);
    font-style: normal;
  }

  pre {
    max-height: 160px;
    margin: 0;
    padding: 8px;
    overflow: auto;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-surface);
    color: var(--color-text);
    font: 11px/1.5 ui-monospace, SFMono-Regular, Consolas, monospace;
  }
}

@media (max-width: 640px) {
  .observer-change-row,
  .observer-preview-grid {
    grid-template-columns: 1fr;
  }

  .observer-change-arrow {
    display: none;
  }
}

.observer-review-actions {
  display: flex;
  justify-content: flex-end;
  margin: 10px 0;
}

.observer-review-state,
.observer-review-error {
  margin: 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.observer-review-error {
  margin-bottom: 10px;
  color: var(--color-danger);
}

.log-name-highlight-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 6px 16px;
  border-bottom: 1px solid var(--color-border);
  flex-shrink: 0;

  .tool-btn.active {
    background: var(--color-primary);
    color: #fff;
  }

  .tool-btn:disabled {
    cursor: not-allowed;
    opacity: 0.45;
  }
}

.meta-badge {
  font-size: 12px;
  padding: 2px 10px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-bg);
  color: var(--color-text);

  &.st-completed {
    color: var(--color-success);
    border-color: var(--color-success);
  }

  &.st-failed {
    color: var(--color-danger);
    border-color: var(--color-danger);
  }
}

.settlement-repair-note {
  margin-bottom: 10px;
  line-height: 1.6;
}

.settlement-repair-list {
  display: grid;
  gap: 8px;
  margin-top: 10px;
}

.settlement-repair-card {
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
}

.settlement-repair-card-head,
.settlement-repair-change {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 6px 12px;
}

.settlement-repair-card-head {
  margin-bottom: 6px;
  font-size: 13px;
}

.settlement-repair-change {
  color: var(--color-text-secondary);
  font-size: 12px;
}

.settlement-repair-blockers {
  margin: 10px 0 0;
  padding-left: 20px;
  color: var(--color-danger);
  font-size: 12px;
}

.settlement-repair-actions {
  flex-wrap: wrap;
  gap: 8px;
}

.meta-status-wrap {
  display: inline-flex;
  align-items: center;
  gap: 4px;
}

.meta-time {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.reader-scroll {
  flex: 1 1 auto;
  min-width: 0;
  min-height: 0;
  overflow-x: hidden;
  overflow-y: auto;
  overscroll-behavior-y: contain;
  touch-action: pan-y pinch-zoom;
  -webkit-overflow-scrolling: touch;
}

.reader-prose {
  max-width: 720px;
  margin: 0 auto;
  padding: 20px 24px 32px;
  font-weight: 400;
}

.reader-scroll.has-custom-background .reader-prose {
  margin-top: 16px;
  margin-bottom: 16px;
  background: color-mix(in srgb, var(--reader-theme-background) var(--reader-panel-opacity), transparent);
  border: 1px solid color-mix(in srgb, currentColor 12%, transparent);
  border-radius: 12px;
  box-shadow: 0 8px 28px rgba(0, 0, 0, 0.12);
  backdrop-filter: blur(var(--reader-panel-blur));
  -webkit-backdrop-filter: blur(var(--reader-panel-blur));
}

/* 阅读主题：独立于站点主题，专用于正文区域 */
.reader-theme-default {
  --reader-theme-background: var(--color-bg);
  background: var(--color-bg);
  color: var(--color-text);
}

.reader-theme-paper {
  --reader-theme-background: #f4efe4;
  background: #f4efe4;
  color: #3c3428;

  .log-summary {
    background: rgba(255, 255, 255, 0.55);
    border: 1px solid #d8cfc0;
  }

  .log-progress-hint {
    background: rgba(255, 255, 255, 0.45);
    border-color: #d8cfc0;
    color: #5c5346;
  }

  .event-text,
  .rc-text {
    color: #3c3428;
  }

  .rc-item {
    background: rgba(255, 255, 255, 0.45);
    border-color: #d8cfc0;
  }
}

.reader-theme-dark {
  --reader-theme-background: #1c1c1e;
  background: #1c1c1e;
  color: #d4d4d8;

  .log-summary {
    background: #2a2a2e;
    border: 1px solid #3f3f46;
  }

  .log-progress-hint {
    background: #2a2a2e;
    border-color: #3f3f46;
    color: #a1a1aa;
  }

  .event-text,
  .rc-text {
    color: #d4d4d8;
  }

  .rc-item {
    background: #2a2a2e;
    border-color: #3f3f46;
  }

  .rc-title {
    color: #e4e4e7;
  }
}

.reader-theme-eye {
  --reader-theme-background: #e3edcd;
  background: #e3edcd;
  color: #2d4a2d;

  .log-summary {
    background: rgba(255, 255, 255, 0.5);
    border: 1px solid #b8cdb0;
  }

  .log-progress-hint {
    background: rgba(255, 255, 255, 0.45);
    border-color: #b8cdb0;
    color: #4a6b4a;
  }

  .event-text,
  .rc-text {
    color: #2d4a2d;
  }

  .rc-item {
    background: rgba(255, 255, 255, 0.45);
    border-color: #b8cdb0;
  }
}

.log-progress-hint {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 16px;
  padding: 8px 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px dashed var(--color-border);
  border-radius: 8px;
}

.progress-dot {
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--color-primary);
  animation: pulse 1.2s ease-in-out infinite;
}

.progress-count {
  color: inherit;
  opacity: 0.85;
}

@keyframes pulse {
  0%, 100% { opacity: 0.4; }
  50% { opacity: 1; }
}

.log-summary {
  margin: 0 0 var(--reader-paragraph-spacing, 20px);
  padding: 12px 14px;
  line-height: inherit;
  border-radius: 8px;
}

.event-list {
  display: flex;
  flex-direction: column;
  gap: var(--reader-paragraph-spacing, 16px);
}

.event-item {
  display: flex;
  gap: 12px;
  align-items: flex-start;

  &:focus-visible {
    outline: 1px solid color-mix(in srgb, var(--color-primary) 45%, transparent);
    outline-offset: 4px;
    border-radius: 6px;
  }
}

.event-idx {
  flex: none;
  width: 26px;
  height: 26px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
  font-weight: 700;
  color: #fff;
  background: var(--color-primary);
  border-radius: 50%;
  margin-top: 2px;
}

.event-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}

.inline-comment-bubble {
  display: inline-flex;
  align-items: center;
  gap: 2px;
  min-width: 24px;
  height: 21px;
  margin-left: 7px;
  padding: 0 6px;
  border: 0;
  border-radius: 999px;
  vertical-align: 1px;
  background: transparent;
  color: var(--color-text-secondary);
  font: inherit;
  font-size: 10px;
  line-height: 1;
  cursor: pointer;

  &:hover,
  &:focus-visible {
    color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  }
}

.inline-comment-bubble.is-empty {
  display: none;
  color: var(--color-primary);
}

.event-item.is-comment-active .inline-comment-bubble.is-empty,
.event-item:focus-within .inline-comment-bubble.is-empty {
  display: inline-flex;
}

@media (hover: hover) and (pointer: fine) {
  .event-item:hover .inline-comment-bubble.is-empty {
    display: inline-flex;
  }
}

.comment-discovery-hint {
  display: flex;
  align-items: center;
  gap: 7px;
  width: fit-content;
  margin: 10px 0 14px 38px;
  padding: 6px 8px 6px 10px;
  border: 1px solid color-mix(in srgb, var(--color-primary) 18%, var(--color-border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-primary) 6%, transparent);
  color: var(--color-text-secondary);
  font-size: 11px;

  button {
    display: grid;
    place-items: center;
    width: 20px;
    height: 20px;
    padding: 0;
    border: 0;
    border-radius: 50%;
    background: transparent;
    color: inherit;
    font-size: 16px;
    line-height: 1;
    cursor: pointer;
  }

  button:hover,
  button:focus-visible {
    background: color-mix(in srgb, var(--color-primary) 10%, transparent);
    color: var(--color-primary);
  }
}

.comment-overlay {
  position: fixed;
  inset: 0;
  z-index: 1500;
  display: flex;
  align-items: flex-end;
  justify-content: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.48);
}

.comment-sheet {
  width: min(620px, 100%);
  max-height: min(720px, 82vh);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--color-border);
  border-radius: 18px 18px 12px 12px;
  background: var(--color-surface);
  color: var(--color-text);
  box-shadow: 0 18px 60px rgba(0, 0, 0, 0.34);
}

.comment-sheet-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 16px 18px 12px;
  border-bottom: 1px solid var(--color-border);

  h3,
  p { margin: 0; }

  h3 { font-size: 17px; }

  p {
    margin-top: 3px;
    color: var(--color-text-secondary);
    font-size: 12px;
  }
}

.comment-close {
  width: 32px;
  height: 32px;
  border: 0;
  border-radius: 50%;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 24px;
  line-height: 1;
  cursor: pointer;
}

.comment-close:hover { background: var(--color-bg); }

.comment-list {
  flex: 1;
  min-height: 160px;
  overflow-y: auto;
  padding: 4px 18px 10px;
}

.comment-item {
  padding: 12px 0;
  border-bottom: 1px solid var(--color-border);

  header {
    display: flex;
    align-items: center;
    gap: 7px;
    color: var(--color-text-secondary);
    font-size: 11px;
  }

  strong {
    color: var(--color-text);
    font-size: 13px;
  }

  time { margin-left: auto; }

  p {
    margin: 7px 0 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-size: 14px;
    line-height: 1.65;
  }
}

.comment-title {
  padding: 1px 6px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  color: var(--color-primary);
}

.comment-state {
  margin: 0;
  padding: 42px 0;
  color: var(--color-text-secondary);
  text-align: center;
  font-size: 13px;
}

.comment-more {
  display: block;
  margin: 12px auto 2px;
  border: 0;
  background: transparent;
  color: var(--color-primary);
  cursor: pointer;
}

.comment-compose {
  padding: 12px 18px 16px;
  border-top: 1px solid var(--color-border);

  textarea {
    box-sizing: border-box;
    width: 100%;
    resize: vertical;
    max-height: 140px;
    padding: 10px 12px;
    border: 1px solid var(--color-border);
    border-radius: 10px;
    background: var(--color-bg);
    color: var(--color-text);
    font: inherit;
  }
}

.comment-compose-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 8px;
  color: var(--color-text-secondary);
  font-size: 11px;

  button {
    min-width: 72px;
    padding: 7px 16px;
    border: 0;
    border-radius: 999px;
    background: var(--color-primary);
    color: #fff;
    font-weight: 700;
    cursor: pointer;
  }

  button:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
}

/* 特殊事件只装饰阅读器边缘与首段提示，不改变正文阅读主题。 */
.modal-card[class*='se-theme-'] {
  border-color: color-mix(in srgb, var(--se-accent) 58%, var(--color-border));
  box-shadow: 0 18px 50px color-mix(in srgb, var(--se-accent) 16%, transparent), inset 0 3px 0 var(--se-accent);
}

.special-event-badge {
  color: var(--se-accent);
  border-color: color-mix(in srgb, var(--se-accent) 55%, var(--color-border));
  background: color-mix(in srgb, var(--se-accent) 9%, transparent);
  font-weight: 650;
}

.event-item[class*='se-'] .event-idx {
  background: var(--se-accent);
  box-shadow: 0 0 0 4px color-mix(in srgb, var(--se-accent) 16%, transparent);
}

.event-item[class*='se-'] .event-text {
  color: color-mix(in srgb, var(--se-accent) 78%, var(--color-text));
}

.log-ai-feedback {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 10px;
  color: var(--color-text-secondary);
  font-size: 12px;
}

.relation-changes {
  margin-top: 24px;
  padding-top: 16px;
  border-top: 1px solid var(--color-border);
}

.log-like-panel {
  margin-top: 24px;
  padding: 14px;
  border: 1px solid rgba(245, 158, 11, 0.28);
  border-radius: 12px;
  background: rgba(245, 158, 11, 0.08);
}

.log-like-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 10px;
}

.log-like-title {
  margin: 0;
  font-size: 15px;
}

.log-like-done {
  font-size: 12px;
  font-weight: 700;
  color: #b45309;
}

.log-like-candidates {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.like-candidate-btn {
  border: 1px solid rgba(245, 158, 11, 0.42);
  border-radius: 999px;
  padding: 6px 12px;
  background: var(--color-surface);
  color: var(--color-text);
  font-weight: 700;
  cursor: pointer;
}

.like-candidate-btn:disabled {
  opacity: 0.65;
  cursor: not-allowed;
}

.like-count {
  margin-left: 4px;
  color: #b45309;
}

.log-like-reason {
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 13px;
}

.rc-title {
  margin: 0 0 10px;
  font-size: 15px;
}
.rc-hint { margin: 0 0 10px; font-size: 12px; line-height: 1.6; opacity: 0.8; }

.rc-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 8px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.rc-icon {
  font-size: 14px;
}

.rc-text {
  font-size: inherit;
}

@media (max-width: 640px) {
  .comment-discovery-hint {
    width: auto;
    margin-left: 0;
    border-radius: 10px;
  }

  .comment-overlay { padding: 0; }

  .comment-sheet {
    max-height: 86vh;
    border-right: 0;
    border-bottom: 0;
    border-left: 0;
    border-radius: 18px 18px 0 0;
  }

  .modal-overlay:not(.is-fullscreen) {
    padding: 8px;
  }

  .modal-card:not(.is-fullscreen) {
    // 窄屏阅读器需要确定高度，让正文独立滚动，不依赖被锁定的页面。
    height: calc(100vh - 16px);
    height: calc(100dvh - max(8px, env(safe-area-inset-top)) - max(8px, env(safe-area-inset-bottom)));
    max-height: 94vh;
  }

  .reader-prose {
    padding: 16px 14px 28px;
  }

  .reader-settings-panel {
    max-height: 40vh;
    max-height: 40dvh;
    padding: 12px;
  }

  .reader-settings-grid {
    grid-template-columns: 1fr;
    gap: 10px;
  }

  .reader-background-head {
    align-items: stretch;
    flex-direction: column;
  }

  .reader-background-actions button { flex: 1; }

  .reader-toolbar {
    gap: 2px;
  }

  .theme-select {
    max-width: 72px;
    font-size: 11px;
  }

  .modal-title {
    font-size: 14px;
  }

  /* 超窄屏（<360px）头部放不下时允许工具栏换行右对齐，避免横向溢出 */
  .modal-head {
    flex-wrap: wrap;
    gap: 6px;
    padding: 10px 12px;
  }

  .head-tools {
    margin-left: auto;
  }
}
.reader-settings-label, .reader-mobile-basics { display: none; }
@media (max-width: 640px) {
  .modal-head { flex-wrap: nowrap; gap: 8px; padding: 8px 12px; }
  .modal-title { min-width: 0; flex-shrink: 1; font-size: 15px; overflow-wrap: anywhere; }
  .modal-log-id { display: block; margin-top: 2px; font-size: 12px; }
  .head-tools { flex: none; gap: 4px; margin-left: 0; }
  .reader-toolbar { gap: 4px; padding: 0; border: 0; background: transparent; }
  .reader-toolbar > .reader-font-step,
  .reader-toolbar > .tool-label,
  .reader-toolbar > .theme-select { display: none; }
  .reader-settings-toggle { display: inline-flex; align-items: center; gap: 4px; padding-inline: 6px; }
  .reader-settings-label { display: inline; font-size: 13px; }
  .reader-mobile-basics { display: grid; gap: 12px; margin-bottom: 12px; }
  .reader-mobile-font { display: flex; align-items: center; gap: 8px; }
  .reader-mobile-font > span { flex: 1; }
  .reader-mobile-font > button { border: 1px solid var(--color-border); }
  .reader-choice-group { flex-wrap: wrap; gap: 6px; }
  .reader-prose { overflow-wrap: anywhere; }
  .comment-sheet-head { gap: 8px; padding: 10px 12px; }
  .comment-compose { flex-shrink: 0; padding: 12px; }
  .comment-list { min-height: 0; }
}
</style>
