<template>
  <div class="admin-appeal-panel">
    <div class="auto-mode-bar">
      <strong>双 AI 自动审核</strong>
      <span class="auto-mode-hint muted small">普通修复自动执行；新增物品、补发金币等高权限操作必须通过独立复核。陪审团 2 号可补充修复（同等权限，可一键还原）。</span>
      <div class="auto-mode-actions">
        <button
          type="button"
          class="btn-danger btn-sm"
          :disabled="crossCharacterLoading"
          @click="previewCrossCharacterAppeals"
        >扫描跨角色申诉</button>
        <button
          type="button"
          class="btn-secondary btn-sm"
          :disabled="revertOfferBulkLoading"
          @click="previewRevertOffers"
        >预览误删还原确认</button>
        <button
          type="button"
          class="btn-secondary btn-sm"
          :disabled="misWithdrawLoading"
          @click="previewMisWithdrawals"
        >预览误标撤回</button>
        <button
          type="button"
          class="btn-secondary btn-sm"
          :disabled="stuckRevertLoading"
          @click="previewStuckRevertRequests"
        >预览卡住修复撤回</button>
        <button
          type="button"
          class="btn-secondary btn-sm"
          :disabled="rarityRefundLoading"
          @click="previewRarityRefunds"
        >预览稀有度误杀返还</button>
      </div>
    </div>
    <div v-if="crossCharacterPreview" class="revert-offer-bar cross-character-bar">
      <div class="revert-offer-head">
        <div>
          <strong>新角色申诉旧角色探险</strong>
          <p class="muted small">
            仅按结构化 ID 判断：申诉角色不在关联探险成员表中。已落库内容按审计台账倒序撤销；尚未落库的工单直接取消并阻止 AI 继续执行。
          </p>
        </div>
        <div class="revert-offer-actions">
          <label class="small muted">展示上限</label>
          <input v-model.number="crossCharacterLimit" type="number" min="1" max="200" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="crossCharacterLoading" @click="previewCrossCharacterAppeals">刷新预览</button>
          <button
            type="button"
            class="btn-danger btn-sm"
            :disabled="crossCharacterLoading || !crossCharacterItems.length"
            @click="executeCrossCharacterAppealReverts"
          >一键撤销本次预览（{{ crossCharacterItems.length }}）</button>
        </div>
      </div>
      <div class="revert-offer-summary">
        <span>候选 <b>{{ crossCharacterPreview.summary?.total_candidates ?? 0 }}</b> 条</span>
        <span>待取消 <b>{{ crossCharacterPreview.summary?.pending_to_cancel ?? 0 }}</b> 条</span>
        <span>已落库待撤销 <b>{{ crossCharacterPreview.summary?.applied_to_revert ?? 0 }}</b> 条</span>
      </div>
      <div v-if="crossCharacterItems.length" class="revert-offer-table-wrap">
        <table class="appeal-table revert-offer-table">
          <thead>
            <tr>
              <th>申诉</th>
              <th>当前申诉角色</th>
              <th>旧探险成员</th>
              <th>处理</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in crossCharacterItems" :key="item.id">
              <td>
                <button type="button" class="btn-link-like" @click="openDetail(item.id)">#{{ item.id }}</button>
                <div class="muted small">探险 #{{ item.expedition_log_id }} · {{ statusLabel(item.status) }}</div>
                <div class="muted small">{{ item.nickname || item.username || '未知玩家' }}</div>
              </td>
              <td>
                <div>{{ item.adventurer_name }} #{{ item.adventurer_id }}</div>
                <div class="muted small">{{ item.adventurer_status || '未知状态' }}</div>
              </td>
              <td class="muted small">
                <div v-for="member in item.expedition_members || []" :key="member.adventurer_id">
                  {{ member.name }} #{{ member.adventurer_id }}（{{ member.status || '未知状态' }}）
                </div>
                <div v-if="!(item.expedition_members || []).length">成员索引缺失</div>
              </td>
              <td>
                <span v-if="item.action === 'revert'" class="warn-tag">撤销 {{ item.active_audit_count }} 项落库变更</span>
                <span v-else class="muted small">取消未落库工单</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div v-if="rarityRefundPreview" class="revert-offer-bar rarity-refund-bar">
      <div class="revert-offer-head">
        <div>
          <strong>稀有度复核误杀返还</strong>
          <p class="muted small">
            自动扫描 AI 已给出稀有度修改 patch、但被旧护栏误判为失败的历史工单；执行后关闭旧单、返还 1 次申诉次数并发站内信。
          </p>
        </div>
        <div class="revert-offer-actions">
          <label class="small muted">展示上限</label>
          <input v-model.number="rarityRefundLimit" type="number" min="1" max="200" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="rarityRefundLoading" @click="previewRarityRefunds">刷新预览</button>
          <button
            type="button"
            class="btn-danger btn-sm"
            :disabled="rarityRefundLoading || !rarityRefundableCount"
            @click="executeRarityRefunds"
          >返还本次预览（{{ rarityRefundableCount }}）</button>
        </div>
      </div>
      <div class="revert-offer-summary">
        <span>候选 <b>{{ rarityRefundPreview.summary?.total_candidates ?? 0 }}</b> 条</span>
        <span>可返还 <b>{{ rarityRefundableCount }}</b> 条</span>
      </div>
      <div v-if="rarityRefundItems.length" class="revert-offer-table-wrap">
        <table class="appeal-table revert-offer-table">
          <thead>
            <tr>
              <th>申诉</th>
              <th>玩家/冒险者</th>
              <th>拟修正稀有度</th>
              <th>AI 摘要</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in rarityRefundItems" :key="item.id">
              <td>
                <button type="button" class="btn-link-like" @click="openDetail(item.id)">#{{ item.id }}</button>
                <div class="muted small">{{ formatTime(item.created_at) }}</div>
              </td>
              <td>
                <div>{{ item.nickname || item.username || '未知' }}</div>
                <div class="muted small">{{ item.adventurer_name }} #{{ item.adventurer_id }}</div>
              </td>
              <td class="muted small">
                <div v-for="u in item.rarity_updates || []" :key="u.type + '-' + u.id">
                  {{ u.name }}：{{ u.from }} → {{ u.to }}
                </div>
              </td>
              <td class="muted small">{{ item.ai_summary || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div v-if="revertOfferPreview" class="revert-offer-bar">
      <div class="revert-offer-head">
        <div>
          <strong>误删还原确认（个人页）</strong>
          <p class="muted small">
            仅列出命中误删特征的工单（删物误删 / 特质·技能删除未补合并等）；推送后在玩家个人页展示，不发邮件。
          </p>
        </div>
        <div class="revert-offer-actions">
          <label class="small muted">展示上限</label>
          <input v-model.number="revertOfferLimit" type="number" min="1" max="200" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="revertOfferBulkLoading" @click="previewRevertOffers">刷新预览</button>
          <button
            type="button"
            class="btn-danger btn-sm"
            :disabled="revertOfferBulkLoading || !revertOfferSendableCount"
            @click="executeRevertOffers"
          >推送本次预览（{{ revertOfferSendableCount }}）</button>
        </div>
      </div>
      <div class="revert-offer-summary">
        <span>候选 <b>{{ revertOfferPreview.summary?.total_candidates ?? 0 }}</b> 条</span>
        <span>可推送 <b>{{ revertOfferSendableCount }}</b> 条</span>
        <span class="muted small">筛选：误删特征命中</span>
      </div>
      <div v-if="revertOfferItems.length" class="revert-offer-table-wrap">
        <table class="appeal-table revert-offer-table">
          <thead>
            <tr>
              <th>申诉</th>
              <th>玩家/冒险者</th>
              <th>删除条目</th>
              <th>命中原因</th>
              <th>状态</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in revertOfferItems" :key="item.id">
              <td>
                <div>#{{ item.id }}</div>
                <div class="muted small">{{ item.player_note || '—' }}</div>
              </td>
              <td>
                <div>{{ item.nickname || item.username || '未知' }}</div>
                <div class="muted small">{{ item.adventurer_name }} #{{ item.adventurer_id }}</div>
              </td>
              <td>
                <div>delete ×{{ item.delete_count || 0 }}</div>
                <div class="muted small">物 {{ item.item_delete_count || 0 }} · 特质 {{ item.trait_delete_count || 0 }} · 技能 {{ item.skill_delete_count || 0 }}</div>
                <div v-if="item.deleted_item_names?.length" class="muted small">
                  {{ item.deleted_item_names.join('、') }}
                </div>
              </td>
              <td>
                <span v-if="item.mistake_signal_label">{{ item.mistake_signal_label }}</span>
                <span v-else class="muted small">—</span>
              </td>
              <td>
                <span v-if="item.can_send" class="st st-approved">可推送</span>
                <span v-else class="muted small">{{ item.skip_reason || '不可推送' }}</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div v-if="misWithdrawPreview" class="revert-offer-bar mis-withdraw-bar">
      <div class="revert-offer-head">
        <div>
          <strong>误标撤回修正</strong>
          <p class="muted small">
            玩家在<strong>普通申诉</strong>描述里写「申请撤回」、二审 allow、空 patch，却被标成 rejected 的历史工单。
            （不含「申请撤回此次修复」流程；那个请看下方「卡住修复撤回」。）
          </p>
        </div>
        <div class="revert-offer-actions">
          <label class="small muted">展示上限</label>
          <input v-model.number="misWithdrawLimit" type="number" min="1" max="200" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="misWithdrawLoading" @click="previewMisWithdrawals">刷新预览</button>
          <button
            type="button"
            class="btn-danger btn-sm"
            :disabled="misWithdrawLoading || !misWithdrawFixableCount"
            @click="executeMisWithdrawals"
          >修正本次预览（{{ misWithdrawFixableCount }}）</button>
        </div>
      </div>
      <div class="revert-offer-summary">
        <span>候选 <b>{{ misWithdrawPreview.summary?.total_candidates ?? 0 }}</b> 条</span>
        <span>可修正 <b>{{ misWithdrawFixableCount }}</b> 条</span>
      </div>
      <div v-if="misWithdrawItems.length" class="revert-offer-table-wrap">
        <table class="appeal-table revert-offer-table">
          <thead>
            <tr>
              <th>申诉</th>
              <th>玩家/冒险者</th>
              <th>当前驳回原因</th>
              <th>修正后说明</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in misWithdrawItems" :key="item.id">
              <td>
                <div>#{{ item.id }}</div>
                <div class="muted small">{{ item.player_note || '—' }}</div>
              </td>
              <td>
                <div>{{ item.nickname || item.username || '未知' }}</div>
                <div class="muted small">{{ item.adventurer_name }} #{{ item.adventurer_id }}</div>
              </td>
              <td class="muted small">{{ item.reject_reason || '—' }}</td>
              <td class="muted small">{{ item.new_status_reason || '—' }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div v-if="stuckRevertPreview" class="revert-offer-bar stuck-revert-bar">
      <div class="revert-offer-head">
        <div>
          <strong>卡住修复撤回</strong>
          <p class="muted small">
            玩家对已处理申诉点了「申请撤回此次修复」，但 AI 审核卡在 processing（常见于额度 stub 被误送进普通申诉 AI）。
          </p>
        </div>
        <div class="revert-offer-actions">
          <label class="small muted">展示上限</label>
          <input v-model.number="stuckRevertLimit" type="number" min="1" max="200" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="stuckRevertLoading" @click="previewStuckRevertRequests">刷新预览</button>
          <button
            type="button"
            class="btn-danger btn-sm"
            :disabled="stuckRevertLoading || !stuckRevertRetryableCount"
            @click="retryStuckRevertRequests"
          >重试本次预览（{{ stuckRevertRetryableCount }}）</button>
        </div>
      </div>
      <div class="revert-offer-summary">
        <span>卡住 <b>{{ stuckRevertPreview.summary?.total_candidates ?? 0 }}</b> 条</span>
        <span>可重试 <b>{{ stuckRevertRetryableCount }}</b> 条</span>
      </div>
      <div v-if="stuckRevertItems.length" class="revert-offer-table-wrap">
        <table class="appeal-table revert-offer-table">
          <thead>
            <tr>
              <th>原申诉</th>
              <th>玩家/冒险者</th>
              <th>撤回理由</th>
              <th>额度 stub</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in stuckRevertItems" :key="item.id">
              <td>
                <div>#{{ item.id }} · {{ statusLabel(item.status) }}</div>
                <div class="muted small">{{ item.player_note || '—' }}</div>
              </td>
              <td>
                <div>{{ item.nickname || item.username || '未知' }}</div>
                <div class="muted small">{{ item.adventurer_name }} #{{ item.adventurer_id }}</div>
              </td>
              <td class="muted small">{{ item.player_revert_request_note || '—' }}</td>
              <td class="muted small">
                <template v-if="item.stub_id">
                  #{{ item.stub_id }} · {{ statusLabel(item.stub_status || '') }}
                  <span v-if="item.stub_misprocessed" class="warn-tag">误走普通 AI</span>
                </template>
                <template v-else>—</template>
              </td>
              <td>
                <button
                  type="button"
                  class="btn-secondary btn-xs"
                  :disabled="stuckRevertLoading || !item.can_retry"
                  @click="retryOneStuckRevert(item.id)"
                >重试 AI</button>
                <button
                  type="button"
                  class="btn-danger btn-xs"
                  :disabled="stuckRevertLoading || !item.can_reset"
                  @click="resetOneStuckRevert(item.id)"
                >重置</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div class="bug-reclaim-bar">
      <div class="bug-reclaim-head">
        <div>
          <strong>转职书漏扣回收</strong>
          <p class="muted small">完整扫描全部历史结算，只按结构化职业变化、出发快照与图鉴使用条件精确核对；展示上限仅限制返回行数。普通地图、技能卷轴等图鉴未标记为转职凭证的卷轴，以及与本次副本不匹配的邀请函会直接过滤并计数；只有同次存在多份符合条件的邀请函才列入待核查。确认项能追回现物就回收；找不到现物时史诗扣 1500 金币、传说扣 10000 金币。</p>
        </div>
        <div class="bug-reclaim-actions">
          <label class="small muted">每类展示上限</label>
          <input v-model.number="classScrollReclaimLimit" type="number" min="1" max="50" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="classScrollReclaimLoading" @click="previewClassScrollReclaims">
            {{ classScrollReclaimLoading ? '检查中…' : '预览转职书回收' }}
          </button>
          <button
            type="button"
            class="btn-danger btn-sm"
            :disabled="classScrollReclaimLoading || !classScrollExecutableItems.length || !classScrollReclaimPreview?.snapshot_token || classScrollReclaimPreview?.summary?.scan_complete !== true"
            @click="executeClassScrollReclaims"
          >执行可回收项</button>
        </div>
      </div>
      <div v-if="classScrollReclaimPreview" class="bug-reclaim-summary">
        <span>结构化候选 <b>{{ classScrollReclaimPreview.summary?.signals || 0 }}</b> 项</span>
        <span>确认漏扣 <b>{{ classScrollReclaimPreview.summary?.matched || 0 }}</b> 项</span>
        <span v-if="classScrollReclaimPreview.summary?.review_required">待核查 <b>{{ classScrollReclaimPreview.summary.review_required }}</b> 项</span>
        <span>可执行 <b>{{ classScrollReclaimPreview.summary?.executable || 0 }}</b> 项</span>
        <span>无现物扣款 <b>{{ classScrollReclaimPreview.summary?.missing_item_candidates || 0 }}</b> 项 / <b>{{ classScrollReclaimPreview.summary?.missing_item_charge_gold || 0 }}</b> 金币</span>
        <span>退还成交买家 <b>{{ classScrollReclaimPreview.summary?.buyer_refund_gold || 0 }}</b> 金币</span>
        <span>扣卖家 <b>{{ classScrollReclaimPreview.summary?.seller_gold_clawback || 0 }}</b> 金币</span>
        <span v-if="classScrollReclaimPreview.summary?.catalog_rejected" class="muted small">图鉴未标记为转职凭证，已过滤 {{ classScrollReclaimPreview.summary.catalog_rejected }} 项</span>
        <span v-if="classScrollReclaimPreview.summary?.dungeon_rejected" class="muted small">副本条件不符已过滤 {{ classScrollReclaimPreview.summary.dungeon_rejected }} 项</span>
        <span v-if="classScrollReclaimPreview.summary?.ambiguous_rejected" class="muted small">多凭证歧义 {{ classScrollReclaimPreview.summary.ambiguous_rejected }} 次</span>
        <span v-if="classScrollReclaimPreview.summary?.scan_complete === true" class="muted small">已完成全历史扫描，共核对 {{ classScrollReclaimPreview.summary?.logs_scanned || 0 }} 份结算</span>
        <span v-else class="muted small">当前已核对 {{ classScrollReclaimPreview.summary?.logs_scanned || 0 }} 份结算</span>
        <span v-if="classScrollReclaimPreview.summary?.scan_complete !== true" class="rejected-text small">
          扫描未完成，本次结果不可执行
        </span>
        <span v-if="!classScrollReclaimPreview.snapshot_token" class="rejected-text small">共享预览缓存不可用，本次只能查看，不能执行</span>
      </div>
      <div v-if="classScrollReclaimItems.length" class="bug-reclaim-table-wrap">
        <table class="appeal-table bug-reclaim-table">
          <thead>
            <tr>
              <th>探险/冒险者</th>
              <th>职业变化</th>
              <th>确认应扣凭证</th>
              <th>回收路线</th>
              <th>资金处理</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in classScrollReclaimItems" :key="item.issue_key">
              <td>
                <div>探险 #{{ item.expedition_log_id }}</div>
                <div class="muted small">{{ item.adventurer_name || '未知冒险者' }} #{{ item.adventurer_id }}</div>
                <div class="muted small">{{ item.target_dungeon || '副本未知' }}</div>
              </td>
              <td>{{ item.old_class || '无职业' }} → {{ item.new_class }}</td>
              <td>
                <strong>{{ item.item_name }}</strong>
                <div class="muted small">{{ classScrollRarityLabel(item.item_rarity) }}</div>
                <div v-if="item.item_description" class="muted small">{{ item.item_description }}</div>
              </td>
              <td>
                <span :class="item.executable ? 'reclaim-status-tag rs-executed' : 'reclaim-status-tag rs-failed'">
                  {{ item.executable ? '可执行' : '跳过' }}
                </span>
                <div class="muted small">{{ item.route?.message }}</div>
              </td>
              <td>
                <div v-if="item.route?.missing_item_charge_gold">无现物扣原转职者 {{ item.route.missing_item_charge_gold }}</div>
                <div v-if="item.route?.buyer_refund_gold">退还成交买家 {{ item.route.buyer_refund_gold }}</div>
                <div v-if="item.route?.seller_gold_clawback">扣卖家 {{ item.route.seller_gold_clawback }}</div>
                <span v-if="!item.route?.missing_item_charge_gold && !item.route?.buyer_refund_gold && !item.route?.seller_gold_clawback" class="muted small">无金币变动</span>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <div v-if="classScrollReviewItems.length" class="bug-reclaim-table-wrap">
        <div class="muted small">多凭证歧义待核查（仅展示证据，不会进入“执行可回收项”）</div>
        <table class="appeal-table bug-reclaim-table">
          <thead>
            <tr>
              <th>探险/冒险者</th>
              <th>职业变化</th>
              <th>出发快照卷轴</th>
              <th>未自动认定原因</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in classScrollReviewItems" :key="item.issue_key">
              <td>
                <div>探险 #{{ item.expedition_log_id }}</div>
                <div class="muted small">{{ item.adventurer_name || '未知冒险者' }} #{{ item.adventurer_id }}</div>
                <div class="muted small">实际副本：{{ item.target_dungeon || '未知' }}</div>
              </td>
              <td>{{ item.old_class || '无职业' }} → {{ item.new_class }}</td>
              <td>
                <strong>{{ item.item_name }}</strong>
                <div class="muted small">{{ classScrollRarityLabel(item.item_rarity) }}</div>
                <div v-if="item.catalog_target_dungeon" class="muted small">图鉴限定：{{ item.catalog_target_dungeon }}</div>
              </td>
              <td>
                <span class="reclaim-status-tag rs-failed">待核查</span>
                <div class="muted small">{{ item.review_reason }}</div>
              </td>
            </tr>
          </tbody>
        </table>
        <div v-if="classScrollReclaimPreview?.summary?.review_truncated" class="muted small">
          待核查项超过展示上限；提高展示上限后可查看更多，但不会改变扫描范围。
        </div>
      </div>
    </div>
    <div class="bug-reclaim-bar">
      <div class="bug-reclaim-head">
        <div>
          <strong>异常收益回收</strong>
          <p class="muted small">扫描申诉 create_item 与同链路改量；模糊同名链路仅 1 次申诉的不进预览。纯改量且无 create 时首次数量归一仍合法。</p>
        </div>
        <div class="bug-reclaim-actions">
          <label class="small muted">展示上限</label>
          <input v-model.number="bugReclaimLimit" type="number" min="1" max="200" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="bugReclaimLoading" @click="previewBugReclaims">
            {{ bugReclaimLoading ? '扫描中…' : '预览回收' }}
          </button>
          <button
            type="button"
            class="btn-secondary btn-sm"
            :disabled="itemCatalogLoading"
            @click="openItemChangeCatalog"
          >申诉物品总览</button>
          <button
            type="button"
            class="btn-danger btn-sm"
            :disabled="bugReclaimLoading || bugReclaimPreview?.summary?.scan?.complete === false || !bugReclaimItems.length"
            @click="executeBugReclaims"
          >执行本次预览</button>
        </div>
      </div>
      <div v-if="bugReclaimPreview" class="bug-reclaim-summary">
        <template v-if="bugReclaimPreview.summary?.scan?.complete === false">
          <span>
            正在{{ bugReclaimPreview.summary.scan.mode === 'full_baseline' ? '建立全量基线' : '增量重算' }}：
            <b>{{ bugReclaimPreview.summary.scan.processed_adventurers || 0 }}</b>
            / {{ bugReclaimPreview.summary.scan.total_adventurers || 0 }} 名源冒险者
          </span>
          <span class="muted small">
            当前水位 audit #{{ bugReclaimPreview.summary.scan.target_audit_log_id || 0 }}；完成前不会生成可执行快照
          </span>
          <span v-if="bugReclaimPreview.summary.scan.busy" class="muted small">
            另一请求正在处理当前批次，正在等待其写回
          </span>
        </template>
        <template v-else>
          <span>应回收 <b>{{ bugReclaimPreview.summary?.total_candidates ?? 0 }}</b> 项</span>
          <span>本页展示 <b>{{ bugReclaimPreview.summary?.shown_count ?? bugReclaimItems.length }}</b> 项</span>
          <span v-if="bugReclaimPreview.summary?.raw_cluster_count" class="muted small">
            （持久化待处理候选 {{ bugReclaimPreview.summary.raw_cluster_count }}）
          </span>
          <span>异常数量 <b>{{ bugReclaimPreview.summary?.illegal_quantity || 0 }}</b></span>
          <span v-if="bugReclaimPreview.summary?.text_correction_count" class="muted small">
            文字修正 {{ bugReclaimPreview.summary.text_correction_count }} 项
          </span>
          <span>可扣现物 <b>{{ bugReclaimPreview.summary?.reclaim_item_quantity || 0 }}</b></span>
          <span>预计扣金币 <b>{{ bugReclaimPreview.summary?.gold_to_deduct || 0 }}</b></span>
          <span v-if="bugReclaimPreview.summary?.scan" class="muted small">
            {{ bugReclaimPreview.summary.scan.mode === 'full_baseline'
              ? '全量基线已完成'
              : bugReclaimPreview.summary.scan.mode === 'incremental_noop'
                ? '无新增 audit，直接读取待处理候选'
                : `增量重算已完成（末批精确回读 ${bugReclaimPreview.summary.scan.audit_rows || 0} 条历史）` }}
            · 水位 {{ bugReclaimPreview.summary.scan.cursor_before || 0 }} → {{ bugReclaimPreview.summary.scan.cursor_after || 0 }}
          </span>
        </template>
      </div>
      <div v-if="bugReclaimItems.length" class="bug-reclaim-table-wrap">
        <table class="appeal-table bug-reclaim-table">
          <thead>
            <tr>
              <th>玩家/冒险者</th>
              <th>来源</th>
              <th>物品详情</th>
              <th>数量</th>
              <th>回收</th>
              <th>路线</th>
            </tr>
          </thead>
          <tbody>
            <template v-for="item in bugReclaimItems" :key="item.issue_key">
            <tr>
              <td>
                <div>{{ item.nickname || item.username || '未知玩家' }}</div>
                <div class="muted small">{{ item.adventurer_name }} #{{ item.adventurer_id }}</div>
              </td>
              <td>
                <div>{{ bugIssueLabel(item.issue_type) }}</div>
                <div class="muted small">
                  <template v-if="timelineMeta(item).seed_appeal_id">种子申诉 #{{ timelineMeta(item).seed_appeal_id }}</template>
                  <template v-if="timelineMeta(item).fork_appeal_id"> · 归位 #{{ timelineMeta(item).fork_appeal_id }}</template>
                  <template v-if="item.target_id"> · {{ item.target_type }} #{{ item.target_id }}</template>
                </div>
                <div v-if="flowMeta(item).label" class="muted small">
                  流向：{{ flowMeta(item).label }}
                  <template v-if="flowMeta(item).chain_role">
                    · {{ chainRoleLabel(flowMeta(item).chain_role) }}
                  </template>
                </div>
                <div v-if="marketProv(item)" class="muted small market-seller-hint">
                  购入自 {{ marketProv(item)?.seller_name || `#${marketProv(item)?.seller_adventurer_id}` }}
                </div>
                <button
                  type="button"
                  class="btn-secondary btn-sm bug-detail-btn"
                  @click="openBugReclaimDetail(item)"
                >
                  查看详情
                </button>
                <div v-if="relatedAppealIds(item).length" class="appeal-link-row">
                  <button
                    v-for="aid in relatedAppealIds(item)"
                    :key="'br-appeal-' + item.issue_key + '-' + aid"
                    type="button"
                    class="btn-secondary btn-xs appeal-detail-link-btn"
                    :disabled="detailLoading"
                    @click="openDetail(aid)"
                  >
                    申诉 #{{ aid }}
                  </button>
                </div>
              </td>
              <td class="item-detail-cell">
                <div class="item-detail-grid">
                  <div class="detail-row">
                    <span class="detail-label">原名</span>
                    <span>{{ qa(item).original_name || item.item_name || '—' }}</span>
                  </div>
                  <div v-if="qa(item).original_desc" class="detail-row">
                    <span class="detail-label">原描述</span>
                    <span class="detail-text">{{ qa(item).original_desc }}</span>
                  </div>
                  <div class="detail-row">
                    <span class="detail-label">现名</span>
                    <span>{{ qa(item).current_name || item.item_name || '—' }}</span>
                  </div>
                  <div v-if="qa(item).current_desc" class="detail-row">
                    <span class="detail-label">现描述</span>
                    <span class="detail-text">{{ qa(item).current_desc }}</span>
                    <div v-if="qa(item).display_desc_source" class="muted small">来源：{{ qa(item).display_desc_source }}</div>
                  </div>
                  <div v-else-if="qa(item).desc_cleaned" class="detail-row">
                    <span class="detail-label">现描述</span>
                    <span class="muted small">申诉已清理（数量已写入 quantity 字段）</span>
                  </div>
                  <div v-else class="detail-row">
                    <span class="detail-label">现描述</span>
                    <span class="muted small">无现存堆且审计未记录 after 描述</span>
                  </div>
                  <div class="detail-row">
                    <span class="detail-label">物品来源</span>
                    <span>
                      {{ qa(item).primary_source_label || '未知' }}
                      <span v-if="qa(item).primary_source_description" class="muted small"> · {{ qa(item).primary_source_description }}</span>
                    </span>
                  </div>
                  <div v-if="qa(item).primary_source_trace?.skipped?.length" class="detail-row">
                    <span class="detail-label">来源回溯</span>
                    <span class="muted small source-trace-note">
                      已跳过 {{ qa(item).primary_source_trace.skipped.length }} 条中转流水
                      <template v-for="(s, si) in qa(item).primary_source_trace.skipped" :key="s.item_log_id || si">
                        （{{ s.description }}）
                      </template>
                      → 有效来源：{{ qa(item).primary_source_trace.effective?.description || qa(item).primary_source_description }}
                    </span>
                  </div>
                  <div v-if="qa(item).source_summary" class="detail-row">
                    <span class="detail-label">来源汇总</span>
                    <span class="muted small">{{ qa(item).source_summary }}</span>
                  </div>
                  <div v-if="(qa(item).appeal_grants || []).length" class="detail-row">
                    <span class="detail-label">申诉补发</span>
                    <span class="muted small">
                      <template v-for="(g, gi) in qa(item).appeal_grants || []" :key="g.audit_log_id || gi">
                        #{{ g.appeal_id }} {{ g.action || '新增' }} +{{ g.quantity }}<template v-if="g.is_illegal">（非法）</template><template v-if="Number(gi) < (qa(item).appeal_grants || []).length - 1">；</template>
                      </template>
                      <button type="button" class="linkish-btn" @click="openBugReclaimDetail(item)">展开明细</button>
                    </span>
                  </div>
                  <div class="detail-row muted small">
                    <span class="detail-label">分类</span>
                    <span>{{ item.item_rarity }} · {{ item.item_type || '未分类' }}</span>
                  </div>
                </div>
                <details v-if="(qa(item).stacks || []).length > 1" class="stack-detail">
                  <summary>{{ qa(item).stacks.length }} 堆明细</summary>
                  <ul class="route-list">
                    <li v-for="stack in qa(item).stacks || []" :key="`${stack.source}-${stack.id}`">
                      <span class="chg-action">{{ stack.source }} #{{ stack.id }}</span>
                      <span>×{{ stack.quantity }}</span>
                      <span>{{ stack.name }}</span>
                      <span class="muted small">{{ stack.source_label || '未知来源' }}</span>
                    </li>
                  </ul>
                </details>
              </td>
              <td>
                <div v-if="item.quantity_adjusted" class="illegal-qty-adjusted">
                  系统 +{{ item.computed_illegal_quantity }} → 认定 +{{ item.illegal_quantity }}
                </div>
                <div v-else>申诉非法 +{{ item.illegal_quantity }}</div>
                <div v-if="qa(item).origin_field_quantity != null" class="muted small">
                  申诉前字段量 {{ qa(item).origin_field_quantity }}
                </div>
                <div class="muted small">
                  现状 {{ qa(item).current_stack_count || 0 }} 堆 / 共 {{ qa(item).current_total_quantity ?? item.current_quantity ?? 0 }}
                </div>
                <div class="qty-adjust-row">
                  <input
                    v-model.number="bugReclaimQtyDraft[item.issue_key]"
                    type="number"
                    min="0"
                    class="qty-adjust-input"
                    :placeholder="`系统 +${bugReclaimComputedQty(item)}`"
                  />
                  <button
                    type="button"
                    class="btn-secondary btn-sm"
                    :disabled="bugReclaimLoading || bugReclaimAdjustingKey === item.issue_key"
                    @click="adjustBugReclaimQuantity(item)"
                  >
                    认定数量
                  </button>
                </div>
              </td>
              <td>
                <template v-if="item.reclaim_mode === 'text_correction'">
                  <div class="text-corr-plan">文字修正（不扣物）</div>
                  <div class="muted small">→ {{ item.text_correction?.corrected_name }}</div>
                  <div v-if="item.text_correction?.corrected_quantity != null" class="muted small">
                    数量→{{ item.text_correction.corrected_quantity }}
                  </div>
                </template>
                <template v-else>
                  <div>扣物 {{ item.reclaim_item_quantity || 0 }}</div>
                  <div>扣金币 {{ item.gold_to_deduct || 0 }}</div>
                  <div v-if="item.reclaim_narrative?.summary" class="muted small reclaim-reason">
                    {{ item.reclaim_narrative.summary }}
                  </div>
                </template>
                <button
                  type="button"
                  class="btn-secondary btn-sm dismiss-fp-btn"
                  :disabled="bugReclaimLoading || bugReclaimDismissingKey === item.issue_key"
                  @click="dismissBugReclaimFalsePositive(item)"
                >
                  标记误报
                </button>
              </td>
              <td>
                <details class="route-detail">
                  <summary>{{ (item.route || []).length }} 段流向</summary>
                  <div class="lineage-block">
                    <ul class="route-list">
                      <li v-for="(r, idx) in item.route || []" :key="'r' + idx">
                        <span class="chg-action">{{ routeKindLabel(r.kind) }}</span>
                        <span>×{{ r.quantity }}</span>
                        <span v-if="r.gold">扣 {{ r.gold }} 金</span>
                        <span class="muted small">{{ r.confidence }} · {{ r.message }}</span>
                      </li>
                    </ul>
                  </div>
                </details>
              </td>
            </tr>
            </template>
          </tbody>
        </table>
      </div>
      <details class="bug-reclaim-history">
        <summary @click="fetchBugReclaimHistory()">逐条查看回收历史</summary>
        <div v-if="bugReclaimHistoryLoading" class="muted small">历史加载中…</div>
        <div v-else-if="bugReclaimHistory.length === 0" class="muted small">暂无历史或尚未加载</div>
        <template v-else>
          <div class="bug-reclaim-history-table-wrap">
            <table class="appeal-table bug-reclaim-history-table">
              <thead>
                <tr>
                  <th>明细/批次</th>
                  <th>冒险者/玩家</th>
                  <th>物品与处理</th>
                  <th>状态</th>
                  <th>时间</th>
                  <th>操作</th>
                </tr>
              </thead>
              <tbody>
                <tr v-for="item in bugReclaimHistory" :key="item.id">
                  <td>#{{ item.id }} / 批次 #{{ item.batch_id }}</td>
                  <td>
                    <div>{{ item.adventurer_name || '未知冒险者' }} #{{ item.adventurer_id }}</div>
                    <div class="muted small">{{ item.nickname || item.username || '未知玩家' }}</div>
                  </td>
                  <td>
                    <div>{{ item.item_name }}</div>
                    <div class="muted small">
                      异常 {{ item.illegal_quantity }} · 扣物 {{ item.reclaimed_item_quantity }} · 扣金 {{ item.gold_deducted }}
                    </div>
                  </td>
                  <td>
                    <span :class="item.is_reverted ? 'reverted-tag' : ''">
                      {{ bugReclaimHistoryStatusLabel(item) }}
                    </span>
                    <div v-if="!item.can_revert && !item.is_reverted" class="muted small">
                      {{ item.revert_unavailable_reason }}
                    </div>
                  </td>
                  <td>{{ formatTime(item.reverted_at || item.executed_at || item.created_at) }}</td>
                  <td>
                    <button
                      type="button"
                      class="btn-secondary btn-xs"
                      :disabled="bugReclaimHistoryDetailLoading"
                      @click="openBugReclaimHistoryDetail(item.id)"
                    >
                      查看
                    </button>
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div class="bug-reclaim-history-pager">
            <button
              type="button"
              class="btn-secondary btn-xs"
              :disabled="bugReclaimHistoryPage <= 1 || bugReclaimHistoryLoading"
              @click="fetchBugReclaimHistory(bugReclaimHistoryPage - 1)"
            >上一页</button>
            <span>第 {{ bugReclaimHistoryPage }} / {{ bugReclaimHistoryTotalPages }} 页 · 共 {{ bugReclaimHistoryTotal }} 条</span>
            <button
              type="button"
              class="btn-secondary btn-xs"
              :disabled="bugReclaimHistoryPage >= bugReclaimHistoryTotalPages || bugReclaimHistoryLoading"
              @click="fetchBugReclaimHistory(bugReclaimHistoryPage + 1)"
            >下一页</button>
          </div>
        </template>
      </details>
    </div>
    <div class="bug-reclaim-bar qty-shortfall-bar">
      <div class="bug-reclaim-head">
        <div>
          <strong>异常数量漏给</strong>
          <p class="muted small">扫描申诉已修改物品 name/custom_desc 但 quantity 未随同落库的记录；优先按原始 patch 自动推算补发量，管理员可修改数量后统一应用。</p>
        </div>
        <div class="bug-reclaim-actions">
          <label class="small muted">展示上限</label>
          <input v-model.number="qtyShortfallLimit" type="number" min="1" max="200" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="qtyShortfallLoading" @click="previewQtyShortfalls">预览漏给</button>
          <button
            type="button"
            class="btn-primary btn-sm"
            :disabled="qtyShortfallLoading || !qtyShortfallItems.length"
            @click="executeQtyShortfalls"
          >应用补发</button>
        </div>
      </div>
      <div v-if="qtyShortfallPreview" class="bug-reclaim-summary">
        <span>候选 <b>{{ qtyShortfallPreview.summary?.total_candidates ?? 0 }}</b> 项</span>
        <span>本页展示 <b>{{ qtyShortfallPreview.summary?.shown_count ?? qtyShortfallItems.length }}</b> 项</span>
        <span>系统补发量 <b>{{ qtyShortfallPreview.summary?.computed_shortfall_quantity || 0 }}</b></span>
        <span>认定补发量 <b>{{ qtyShortfallPreview.summary?.shortfall_quantity || 0 }}</b></span>
        <span v-if="qtyShortfallPreview.summary?.needs_admin_quantity" class="muted small">
          待人工填量 {{ qtyShortfallPreview.summary.needs_admin_quantity }} 项
        </span>
        <span v-if="qtyShortfallPreview.summary?.scan?.audit_rows" class="muted small">
          已扫 {{ qtyShortfallPreview.summary.scan.audit_rows }} 条 audit
        </span>
      </div>
      <div v-if="qtyShortfallItems.length" class="bug-reclaim-table-wrap">
        <table class="appeal-table bug-reclaim-table qty-shortfall-table">
          <thead>
            <tr>
              <th>玩家/冒险者</th>
              <th>来源</th>
              <th>物品</th>
              <th>数量</th>
              <th>证据</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in qtyShortfallItems" :key="item.issue_key">
              <td>
                <div>{{ item.nickname || item.username || '未知玩家' }}</div>
                <div class="muted small">{{ item.adventurer_name }} #{{ item.adventurer_id }}</div>
              </td>
              <td>
                <div>{{ qtyShortfallIssueLabel(item.issue_type) }}</div>
                <div class="muted small">申诉 #{{ item.appeal_id }} · audit #{{ item.audit_log_id }}</div>
                <div v-if="item.quantity_source" class="muted small">数量来源：{{ item.quantity_source }}</div>
                <div v-else class="warning small">原 patch 未写 quantity，请手填</div>
              </td>
              <td>
                <div><strong>{{ item.item_name }}</strong></div>
                <div class="muted small">{{ item.item_rarity }} · {{ item.item_type || '未分类' }}</div>
                <div v-if="item.target_id" class="muted small">{{ item.target_type }} #{{ item.target_id }}</div>
                <div v-if="item.before?.name && item.after?.name && item.before.name !== item.after.name" class="muted small">
                  名称：{{ item.before.name }} → {{ item.after.name }}
                </div>
                <div v-if="item.after?.custom_desc" class="muted small detail-text">描述：{{ truncateText(item.after.custom_desc, 90) }}</div>
              </td>
              <td>
                <div>实际 ×{{ item.actual_quantity || 0 }} / 应为 ×{{ item.intended_quantity || '?' }}</div>
                <div v-if="item.quantity_adjusted" class="illegal-qty-adjusted">
                  系统 +{{ item.computed_shortfall_quantity }} → 认定 +{{ item.shortfall_quantity }}
                </div>
                <div v-else>补发 +{{ item.shortfall_quantity || 0 }}</div>
                <div class="qty-adjust-row">
                  <input
                    v-model.number="qtyShortfallQtyDraft[item.issue_key]"
                    type="number"
                    min="0"
                    class="qty-adjust-input"
                    :placeholder="`系统 +${qtyShortfallComputedQty(item)}`"
                  />
                  <button
                    type="button"
                    class="btn-secondary btn-sm"
                    :disabled="qtyShortfallLoading || qtyShortfallAdjustingKey === item.issue_key"
                    @click="adjustQtyShortfallQuantity(item)"
                  >认定数量</button>
                </div>
              </td>
              <td>
                <details class="route-detail">
                  <summary>查看 patch / audit</summary>
                  <div class="lineage-block">
                    <div class="muted small">原因：{{ item.reason }}</div>
                    <pre class="json-mini">{{ JSON.stringify({ before: item.before, after: item.after, patch_item: item.patch_item }, null, 2) }}</pre>
                  </div>
                </details>
              </td>
              <td>
                <button
                  v-if="item.appeal_id"
                  type="button"
                  class="btn-secondary btn-xs appeal-detail-link-btn"
                  :disabled="detailLoading"
                  @click="openDetail(item.appeal_id)"
                >
                  查看申诉
                </button>
                <button
                  type="button"
                  class="btn-secondary btn-sm dismiss-fp-btn"
                  :disabled="qtyShortfallLoading || qtyShortfallDismissingKey === item.issue_key"
                  @click="dismissQtyShortfallFalsePositive(item)"
                >标记误报</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
      <details class="bug-reclaim-history">
        <summary @click="fetchQtyShortfallHistory">查看补发历史</summary>
        <div v-if="qtyShortfallHistory.length === 0" class="muted small">暂无历史或尚未加载</div>
        <ul v-else class="change-list">
          <li v-for="b in qtyShortfallHistory" :key="b.id">
            <span class="chg-id">批次 #{{ b.id }}</span>
            <span>{{ qtyShortfallBatchLabel(b.status) }}</span>
            <span>{{ formatTime(b.created_at) }}</span>
            <span v-if="b.status === 'adjusted'">
              {{ b.summary?.item_name || '数量认定' }} · 系统 +{{ b.summary?.computed_shortfall_quantity ?? 0 }} → +{{ b.summary?.shortfall_quantity ?? 0 }}
            </span>
            <span v-else-if="b.status === 'dismissed'">{{ b.summary?.item_name || b.note || '误报' }}</span>
            <span v-else>补发 {{ b.summary?.granted_quantity ?? b.summary?.shortfall_quantity ?? 0 }}</span>
          </li>
        </ul>
      </details>
    </div>
    <div class="bug-reclaim-bar catalog-desc-pollution-bar">
      <div class="bug-reclaim-head">
        <div>
          <strong>图鉴描述污染还原</strong>
          <p class="muted small">
            图鉴物 custom_desc 留空时会回退图鉴描述；申诉 AI 若误补自拟文案或误清空进化 custom_desc，可在此预览并批量还原。
          </p>
        </div>
        <div class="bug-reclaim-actions">
          <label class="small muted">展示上限</label>
          <input v-model.number="catalogDescPollutionLimit" type="number" min="1" max="500" class="quota-num" />
          <button type="button" class="btn-secondary btn-sm" :disabled="catalogDescPollutionLoading" @click="previewCatalogDescPollution">预览污染</button>
          <button
            type="button"
            class="btn-primary btn-sm"
            :disabled="catalogDescPollutionLoading || !catalogDescPollutionItems.length"
            @click="executeCatalogDescPollution"
          >确认还原</button>
          <button
            type="button"
            class="btn-secondary btn-sm"
            :disabled="catalogDescPollutionLoading || !catalogDescPollutionPreview?.summary?.has_backup"
            @click="restoreCatalogDescPollution"
          >回滚上次</button>
        </div>
      </div>
      <div v-if="catalogDescPollutionPreview" class="bug-reclaim-summary">
        <span>候选 <b>{{ catalogDescPollutionPreview.summary?.total_candidates ?? 0 }}</b> 项</span>
        <span>本页 <b>{{ catalogDescPollutionPreview.summary?.shown_count ?? catalogDescPollutionItems.length }}</b> 项</span>
        <span>误补图鉴 <b>{{ catalogDescPollutionPreview.summary?.catalog_fill_count ?? 0 }}</b></span>
        <span>误清空 <b>{{ catalogDescPollutionPreview.summary?.cleared_custom_count ?? 0 }}</b></span>
        <span v-if="catalogDescPollutionPreview.summary?.scan?.audit_rows" class="muted small">
          已扫 {{ catalogDescPollutionPreview.summary.scan.audit_rows }} 条 audit
        </span>
      </div>
      <div v-if="catalogDescPollutionItems.length" class="bug-reclaim-table-wrap">
        <table class="appeal-table bug-reclaim-table catalog-desc-pollution-table">
          <thead>
            <tr>
              <th>玩家/冒险者</th>
              <th>来源</th>
              <th>物品</th>
              <th>描述对比</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="item in catalogDescPollutionItems" :key="item.issue_key">
              <td>
                <div>{{ item.nickname || item.username || '未知玩家' }}</div>
                <div class="muted small">{{ item.adventurer_name }} #{{ item.adventurer_id }}</div>
              </td>
              <td>
                <div>{{ catalogDescPollutionIssueLabel(item.issue_type) }}</div>
                <div class="muted small">申诉 #{{ item.appeal_id }} · audit #{{ item.audit_log_id }}</div>
                <div class="muted small">{{ item.reason }}</div>
              </td>
              <td>
                <div><strong>{{ item.item_name }}</strong></div>
                <div class="muted small">{{ item.target_type }} #{{ item.target_id }}</div>
              </td>
              <td>
                <div v-if="item.catalog_desc" class="muted small detail-text">图鉴：{{ truncateText(item.catalog_desc, 90) }}</div>
                <div v-if="item.current_custom_desc" class="warning small detail-text">当前：{{ truncateText(item.current_custom_desc, 90) }}</div>
                <div v-else-if="item.issue_type === 'catalog_fill'" class="ok small">还原后回退图鉴</div>
                <div v-if="item.repair_custom_desc" class="ok small detail-text">还原为：{{ truncateText(item.repair_custom_desc, 90) }}</div>
                <div v-else-if="item.issue_type === 'catalog_fill'" class="ok small">清空 custom_desc</div>
              </td>
              <td>
                <button
                  v-if="item.appeal_id"
                  type="button"
                  class="btn-secondary btn-xs appeal-detail-link-btn"
                  :disabled="detailLoading"
                  @click="openDetail(item.appeal_id)"
                >
                  查看申诉
                </button>
                <button
                  type="button"
                  class="btn-secondary btn-sm dismiss-fp-btn"
                  :disabled="catalogDescPollutionLoading || catalogDescPollutionDismissingKey === item.issue_key"
                  @click="dismissCatalogDescPollution(item)"
                >标记误报</button>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </div>
    <div class="quota-bar">
      <div class="daily-limit-row">
        <div>
          <strong>全文申诉每日次数</strong>
          <p class="quota-hint muted small">“其他问题”和“稀有度复核”共用；每天先使用当日次数，未用基础次数次日存入余额池。</p>
        </div>
        <div class="daily-limit-actions">
          <input
            v-model.number="dailyAppealLimits.other"
            type="number"
            min="1"
            max="100"
            class="quota-num"
            aria-label="全文申诉每日次数"
            :disabled="dailyAppealLimitLoading || dailyAppealLimitSaving !== null || balanceCapSaving"
          />
          <button
            type="button"
            class="btn-primary btn-sm"
            :disabled="!dailyAppealLimitLoaded || dailyAppealLimitLoading || dailyAppealLimitSaving !== null || balanceCapSaving"
            @click="saveDailyAppealLimit('other')"
          >{{ dailyAppealLimitSaving === 'other' ? '保存中…' : '保存' }}</button>
        </div>
      </div>
      <div class="daily-limit-row">
        <div>
          <strong>全文申诉余额池上限</strong>
          <p class="quota-hint muted small">默认 5，可设为 0 关闭积累；余额池用完后不会借用明日次数。</p>
        </div>
        <div class="daily-limit-actions">
          <input
            v-model.number="fullTextBalanceCap"
            type="number"
            min="0"
            max="100"
            class="quota-num"
            aria-label="全文申诉余额池上限"
            :disabled="dailyAppealLimitLoading || dailyAppealLimitSaving !== null || balanceCapSaving"
          />
          <button
            type="button"
            class="btn-primary btn-sm"
            :disabled="!dailyAppealLimitLoaded || dailyAppealLimitLoading || dailyAppealLimitSaving !== null || balanceCapSaving"
            @click="saveFullTextBalanceCap"
          >{{ balanceCapSaving ? '保存中…' : '保存' }}</button>
        </div>
      </div>
      <div class="daily-limit-row">
        <div>
          <strong>其余申诉每日次数</strong>
          <p class="quota-hint muted small">数据修正、酒馆补贴申请和撤回申请共用此额度池。</p>
        </div>
        <div class="daily-limit-actions">
          <input
            v-model.number="dailyAppealLimits.standard"
            type="number"
            min="1"
            max="100"
            class="quota-num"
            aria-label="其余申诉每日次数"
            :disabled="dailyAppealLimitLoading || dailyAppealLimitSaving !== null || balanceCapSaving"
          />
          <button
            type="button"
            class="btn-primary btn-sm"
            :disabled="!dailyAppealLimitLoaded || dailyAppealLimitLoading || dailyAppealLimitSaving !== null || balanceCapSaving"
            @click="saveDailyAppealLimit('standard')"
          >{{ dailyAppealLimitSaving === 'standard' ? '保存中…' : '保存' }}</button>
        </div>
      </div>
      <div class="quota-input-row">
        <strong>申诉次数管理</strong>
        <input
          v-model="quotaQuery"
          type="text"
          class="quota-input"
          placeholder="用户ID 或 用户名"
          @keyup.enter="fetchQuota()"
        />
        <button type="button" class="btn-secondary btn-sm" :disabled="quotaLoading" @click="fetchQuota()">查询</button>
        <span class="quota-spacer"></span>
        <button type="button" class="btn-danger btn-sm" :disabled="quotaActionLoading" @click="doResetAllQuota('other')">全服重置全文申诉</button>
        <button type="button" class="btn-danger btn-sm" :disabled="quotaActionLoading" @click="doResetAllQuota('standard')">全服重置其余申诉</button>
      </div>
      <div v-if="quota" class="quota-result">
        <div class="quota-summary">
          <span class="quota-name">{{ quota.nickname || quota.username }}</span>
          <span class="muted small">UID {{ quota.user_id }}</span>
          <span class="muted small">
            历史共 {{ quota.total_appeals }} 次
            <template v-if="quota.active_appeals"> · 进行中 {{ quota.active_appeals }}</template>
          </span>
        </div>
        <div class="quota-pool-admin-grid">
          <section
            v-for="pool in quotaPoolOptions"
            :key="pool.key"
            class="quota-pool-admin"
          >
            <strong>{{ pool.label }}</strong>
            <div class="quota-summary">
              <span class="quota-stat">今日已用 <b>{{ quota.quota_pools[pool.key].used_24h }}</b> / 上限 <b>{{ quota.quota_pools[pool.key].unlimited ? '不限' : quota.quota_pools[pool.key].limit }}</b></span>
              <span class="quota-stat">剩余 <b :class="quota.quota_pools[pool.key].remaining > 0 ? 'ok' : 'warn'">{{ quota.quota_pools[pool.key].unlimited ? '不限' : quota.quota_pools[pool.key].remaining }}</b></span>
              <span v-if="quota.quota_pools[pool.key].bonus > 0" class="quota-stat">临时额度 +{{ quota.quota_pools[pool.key].bonus }}</span>
              <template v-if="quota.quota_pools[pool.key].quota_mode === 'daily_first_balance_pool'">
                <span class="quota-stat">今日余 <b>{{ quota.quota_pools[pool.key].today_remaining }}</b></span>
                <span class="quota-stat">余额池 <b>{{ quota.quota_pools[pool.key].balance_remaining }}</b> / {{ quota.quota_pools[pool.key].balance_cap }}</span>
                <span class="quota-stat">明日预计余额 <b>{{ quota.quota_pools[pool.key].next_balance }}</b></span>
              </template>
            </div>
            <div class="quota-actions">
              <button type="button" class="btn-secondary btn-sm" :disabled="quotaActionLoading" @click="doResetQuota(pool.key)">清零已用</button>
              <input v-model.number="quotaAddCount[pool.key]" type="number" min="1" max="100" class="quota-num" />
              <button type="button" class="btn-primary btn-sm" :disabled="quotaActionLoading" @click="doAddQuota(pool.key)">增加次数</button>
            </div>
            <p class="quota-hint muted small">
              <template v-if="quota.quota_pools[pool.key].reset_at">上次清零：{{ formatTime(quota.quota_pools[pool.key].reset_at) }} · </template>
              <template v-if="quota.quota_pools[pool.key].quota_mode === 'daily_first_balance_pool'">今日次数用完后再使用余额池；</template>清零与临时额度均在北京时间 0 点自动失效
            </p>
          </section>
        </div>
      </div>
    </div>
    <div class="filter-bar">
      <select v-model="statusFilter" @change="fetchList(1)">
        <option value="">全部状态</option>
        <option value="processing">处理中</option>
        <option value="awaiting_review">待审核</option>
        <option value="approved">已通过</option>
        <option value="rejected">已驳回</option>
        <option value="cancelled">已取消</option>
        <option value="reverted">已还原</option>
      </select>
      <input
        v-model="searchQuery"
        type="search"
        class="appeal-search-input"
        placeholder="申诉ID / 玩家 / 冒险者 / 描述"
        @keydown.enter.prevent="applySearch"
      />
      <button type="button" class="btn-primary btn-sm" @click="applySearch">搜索</button>
      <button
        v-if="activeSearch"
        type="button"
        class="btn-secondary btn-sm"
        @click="clearSearch"
      >清除</button>
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="fetchList(page)">刷新</button>
    </div>
    <p v-if="activeSearch" class="search-hint muted small">
      当前搜索：<strong>{{ activeSearch }}</strong>
      <span v-if="pagination"> · 共 {{ pagination.total }} 条</span>
    </p>

    <div v-if="loading && !listLoaded" class="empty">加载中…</div>
    <div v-else-if="listError && !listLoaded" class="empty">{{ listError }}</div>
    <div v-else-if="items.length === 0" class="empty">
      {{ activeSearch ? `未找到与「${activeSearch}」匹配的申诉` : '暂无申诉' }}
    </div>
    <table v-else class="appeal-table primary-appeal-table">
      <thead>
        <tr>
          <th>ID</th>
          <th>玩家</th>
          <th>冒险者</th>
          <th>状态</th>
          <th>AI</th>
          <th>时间</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-for="a in items" :key="a.id">
          <td data-label="申诉 ID">{{ a.id }}</td>
          <td data-label="玩家">{{ a.nickname || a.username }}</td>
          <td data-label="冒险者">{{ a.adventurer_name }}</td>
          <td data-label="状态"><span :class="'st st-' + a.status">{{ appealStatusLabel(a) }}</span></td>
          <td data-label="AI">{{ aiLabel(a.ai_status) }}</td>
          <td data-label="时间">{{ formatTime(a.created_at) }}</td>
          <td class="ops-cell" data-label="操作">
            <button
              type="button"
              class="btn-secondary btn-xs"
              :disabled="detailLoading"
              @click="openDetail(a.id)"
            >查看</button>
            <button
              type="button"
              class="btn-secondary btn-xs"
              @click="quickQuota(a.user_id)"
            >额度</button>
            <button
              v-if="a.status === 'processing'
                || a.status === 'awaiting_review'
                || a.can_retry_ai"
              type="button"
              class="btn-secondary btn-xs"
              :disabled="rowActionId === a.id"
              @click="doRetryAi(a.id)"
            >重试 AI</button>
            <button
              v-if="a.status === 'processing' || a.status === 'awaiting_review'"
              type="button"
              class="btn-danger btn-xs"
              :disabled="rowActionId === a.id"
              @click="doCancel(a.id)"
            >取消</button>
          </td>
        </tr>
      </tbody>
    </table>

    <p v-if="listError && listLoaded" class="muted small" role="status">{{ listError }}</p>
    <Pagination v-if="pagination" :pagination="pagination" @go="fetchList" />

    <Teleport to="body">
      <div v-if="detail" class="modal-overlay appeal-detail-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeDetail">
        <div class="modal-content modal-lg detail-modal s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h5>申诉 #{{ detail.id }} — {{ detail.adventurer_name }}</h5>
            <button class="modal-close" @click="closeDetail">✕</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <p><strong>玩家：</strong>{{ detail.nickname || detail.username }}</p>
            <p><strong>描述：</strong>{{ detail.player_note }}</p>
            <p v-if="detail.target_labels?.length">
              <strong>申诉条目：</strong>{{ detail.target_labels.join('、') }}
            </p>
            <div v-if="detail.status === 'cancelled' && detail.outcome_hint" class="cancel-outcome-banner">
              <p><strong>{{ detail.status_label || appealStatusLabel(detail) }}</strong></p>
              <p class="muted small">{{ detail.outcome_hint }}</p>
            </div>
            <div v-if="detail.expedition_evidence" class="evidence-block">
              <p><strong>关联探险证据：</strong></p>
              <p class="muted small">
                {{ detail.expedition_evidence.target_dungeon || '未知' }}
                <span v-if="detail.expedition_evidence.party_name"> · {{ detail.expedition_evidence.party_name }}</span>
                <template v-if="detail.expedition_evidence.event_text">
                  · 第 {{ (detail.expedition_evidence.event_index ?? 0) + 1 }} 段
                </template>
                <template v-else> · 全文</template>
              </p>
              <blockquote v-if="detail.expedition_evidence.event_text" class="evidence-quote">
                {{ detail.expedition_evidence.event_text }}
              </blockquote>
              <details v-if="detail.expedition_evidence.member_update" class="member-update-raw">
                <summary>该探险对你的结算摘要（AI 参考）</summary>
                <pre>{{ JSON.stringify(detail.expedition_evidence.member_update, null, 2) }}</pre>
              </details>
            </div>
            <p v-if="detail.status === 'processing'" class="processing-hint">
              <strong>{{ detail.status_label || appealStatusLabel(detail) }}</strong>：
              {{ detail.outcome_hint || `AI ${aiLabel(detail.ai_status)}，完成后将自动处理。` }}
            </p>
            <p v-if="detail.ai_summary"><strong>AI 摘要：</strong>{{ detail.ai_summary }}</p>
            <details v-if="detail.ai_raw_response" class="patch-raw">
              <summary>一审完整输出（不省略）</summary>
              <pre>{{ JSON.stringify(parseJsonText(detail.ai_raw_response), null, 2) }}</pre>
            </details>
            <div
              v-if="!isCancelledBeforeAi(detail)"
              class="review-ai-block"
            >
              <p>
                <strong>二次复核 AI：</strong>
                {{ reviewAiLabel(detail.review_ai_status) }}
              </p>
              <p v-if="detail.review_ai_summary" class="muted small">{{ detail.review_ai_summary }}</p>
              <details v-if="detail.review_ai_raw_response" class="patch-raw">
                <summary>二次复核完整输出（不省略）</summary>
                <pre>{{ JSON.stringify(parseJsonText(detail.review_ai_raw_response), null, 2) }}</pre>
              </details>
            </div>
            <p v-else-if="detail.status === 'cancelled'" class="muted small cancelled-ai-skip">
              申诉在 AI 分析完成前已取消，未进入二次复核。
            </p>
            <p v-if="detail.reject_reason">
              <strong>{{ detail.reject_reason_label || '最终未通过原因' }}：</strong>{{ detail.reject_reason }}
            </p>
            <details v-if="detail.player_review_requests?.length" class="patch-raw">
              <summary>玩家独立复核记录（{{ detail.player_review_requests.length }}）</summary>
              <pre>{{ JSON.stringify(detail.player_review_requests, null, 2) }}</pre>
            </details>

            <div
              class="appeal-execution-banner"
              :class="'execution-' + (detail.execution_state || 'not_applied')"
            >
              <strong>{{ appealExecutionTitle(detail) }}</strong>
              <span>{{ appealExecutionDescription(detail) }}</span>
            </div>

            <h6>AI 拟议变更预览</h6>
            <div v-if="!detail.preview?.changes?.length" class="muted small">
              {{ isCancelledBeforeAi(detail)
                ? '本次申诉已取消，AI 未产出任何变更'
                : '无有效变更（AI 未产出 patch 或校验失败）' }}
            </div>
            <ul v-else class="change-list preview-change-list">
              <li v-for="(c, i) in detail.preview.changes" :key="i" class="preview-change-item">
                <div class="preview-change-head">
                  <span class="chg-action">{{ changeActionLabel(c.action) }}</span>
                  <span class="chg-type">{{ typeLabel(c.type) }}</span>
                  <span v-if="c.id" class="chg-id">#{{ c.id }}</span>
                  <strong v-if="previewChangeTitle(c)" class="preview-change-title">{{ previewChangeTitle(c) }}</strong>
                </div>
                <div v-if="c.action === 'update'" class="preview-change-body">
                  <code>{{ auditFieldLabel(c.field) }}</code>:
                  <span class="before">{{ fmtPreviewValue(c.before) }}</span>
                  →
                  <span class="after">{{ fmtPreviewValue(c.after) }}</span>
                </div>
                <div v-else-if="c.action === 'delete'" class="preview-change-body">
                  <dl v-if="c.record" class="audit-record-dl compact">
                    <template v-for="(val, key) in auditRecordFields(c.record)" :key="key">
                      <dt>{{ auditFieldLabel(String(key)) }}</dt>
                      <dd>{{ fmtPreviewValue(val) }}</dd>
                    </template>
                  </dl>
                </div>
                <div v-else class="preview-change-body">
                  <dl v-if="previewGrantFields(c).length" class="audit-record-dl compact">
                    <template v-for="row in previewGrantFields(c)" :key="row.key">
                      <dt>{{ auditFieldLabel(row.key) }}</dt>
                      <dd>
                        <div v-if="isLongAuditText(row.val)" class="audit-text-block after">{{ row.val }}</div>
                        <span v-else>{{ fmtPreviewValue(row.val) }}</span>
                      </dd>
                    </template>
                  </dl>
                  <span v-else class="after">{{ formatPreviewChange(c) }}</span>
                </div>
              </li>
            </ul>

            <details v-if="detail.proposed_patch" class="patch-raw">
              <summary>原始 Patch JSON</summary>
              <pre>{{ JSON.stringify(detail.proposed_patch, null, 2) }}</pre>
            </details>

            <details v-if="detail.review_supplement_patch" class="patch-raw">
              <summary>陪审团 2 号补充 Patch</summary>
              <pre>{{ JSON.stringify(detail.review_supplement_patch, null, 2) }}</pre>
            </details>

            <details v-if="detail.audit_logs?.length" class="patch-raw audit-log-section" open>
              <summary>
                落库变更台账（{{ detail.audit_logs.length }}）
                <span v-if="detail.status === 'reverted'" class="reverted-tag">· 已还原</span>
              </summary>
              <div class="audit-log-list">
                <article
                  v-for="log in detail.audit_logs"
                  :key="log.id"
                  class="audit-log-card"
                  :class="'audit-op-' + log.op_type"
                >
                  <header class="audit-log-head">
                    <span class="audit-badge" :class="'badge-' + log.op_type">{{ auditOpLabel(log.op_type) }}</span>
                    <span class="audit-type">{{ typeLabel(log.target_type) }}</span>
                    <span v-if="log.target_id" class="chg-id">#{{ log.target_id }}</span>
                    <strong v-if="log.target_name" class="audit-target-name">{{ log.target_name }}</strong>
                    <span v-if="log.reverted" class="reverted-tag">↩ 已还原</span>
                  </header>

                  <div v-if="log.op_type === 'delete' && log.deleted_record" class="audit-log-body audit-delete-body">
                    <p class="audit-delete-hint">已删除以下整条记录：</p>
                    <dl class="audit-record-dl">
                      <template v-for="(val, key) in auditRecordFields(log.deleted_record)" :key="key">
                        <dt>{{ auditFieldLabel(String(key)) }}</dt>
                        <dd>
                          <div v-if="isLongAuditText(val)" class="audit-text-block">{{ val }}</div>
                          <span v-else>{{ fmtAuditValue(val, String(key)) }}</span>
                        </dd>
                      </template>
                    </dl>
                  </div>

                  <div v-else-if="log.created_record" class="audit-log-body audit-create-body">
                    <p class="audit-create-hint">新增了以下内容：</p>
                    <dl class="audit-record-dl">
                      <template v-for="(val, key) in auditRecordFields(log.created_record)" :key="key">
                        <dt>{{ auditFieldLabel(String(key)) }}</dt>
                        <dd>
                          <div v-if="isLongAuditText(val)" class="audit-text-block after">{{ val }}</div>
                          <span v-else>{{ fmtAuditValue(val, String(key)) }}</span>
                        </dd>
                      </template>
                    </dl>
                  </div>

                  <div v-else-if="log.changes?.length" class="audit-log-body">
                    <ul class="audit-diff-list">
                      <li v-for="(c, ci) in log.changes" :key="ci">
                        <span class="audit-field-name">{{ auditFieldLabel(c.field) }}</span>
                        <template v-if="c.before !== null && c.before !== undefined && c.before !== ''">
                          <div v-if="isLongAuditText(c.before)" class="audit-diff-col">
                            <span class="diff-label before-label">改前</span>
                            <div class="audit-text-block before">{{ c.before }}</div>
                          </div>
                          <span v-else class="before inline-val">{{ fmtAuditValue(c.before, c.field) }}</span>
                        </template>
                        <span v-if="c.before !== null && c.before !== undefined && c.before !== '' && c.after !== null && c.after !== undefined && c.after !== ''" class="diff-arrow">→</span>
                        <template v-if="c.after !== null && c.after !== undefined && c.after !== ''">
                          <div v-if="isLongAuditText(c.after)" class="audit-diff-col">
                            <span class="diff-label after-label">改后</span>
                            <div class="audit-text-block after">{{ c.after }}</div>
                          </div>
                          <span v-else class="after inline-val">{{ fmtAuditValue(c.after, c.field) }}</span>
                        </template>
                      </li>
                    </ul>
                  </div>

                  <p v-else-if="log.target_name" class="audit-log-body muted small">（无字段级 diff 明细）</p>

                  <footer v-if="log.note || log.revert_result" class="audit-log-foot muted small">
                    <span v-if="log.note">{{ log.note }}</span>
                    <span v-if="log.revert_result"> · {{ log.revert_result }}</span>
                  </footer>
                </article>
              </div>
            </details>

            <div v-if="detail.status === 'processing'" class="review-actions">
              <button
                type="button"
                class="btn-secondary btn-sm"
                :disabled="actionLoading"
                @click="doRetryAi(detail.id)"
              >重试 AI</button>
              <button
                type="button"
                class="btn-danger btn-sm"
                :disabled="actionLoading"
                @click="doCancel(detail.id)"
              >取消申诉</button>
            </div>
            <div v-else-if="detail.status === 'awaiting_review'" class="review-actions">
              <button
                type="button"
                class="btn-primary btn-sm"
                :disabled="actionLoading"
                @click="doApprove(detail.id)"
              >通过并执行</button>
              <input
                v-model="rejectReason"
                type="text"
                class="reject-input"
                placeholder="驳回原因（可选）"
                :disabled="actionLoading"
              />
              <button
                type="button"
                class="btn-danger btn-sm"
                :disabled="actionLoading"
                @click="doReject(detail.id)"
              >驳回</button>
              <button
                type="button"
                class="btn-secondary btn-sm"
                :disabled="actionLoading"
                @click="doRetryAi(detail.id)"
              >重试 AI</button>
            </div>
            <div v-else class="review-actions">
              <span class="muted small">状态：{{ appealStatusLabel(detail) }}</span>
              <button
                v-if="detail.can_retry_ai"
                type="button"
                class="btn-secondary btn-sm"
                :disabled="actionLoading"
                @click="doRetryAi(detail.id)"
              >从失败阶段重试 AI</button>
              <span
                v-if="detail.revert_offer_status === 'pending'"
                class="muted small revert-offer-pending"
              >· 已推送至玩家个人页，待回应</span>
              <button
                v-if="detail.can_send_revert_offer"
                type="button"
                class="btn-secondary btn-sm"
                :disabled="actionLoading"
                @click="doSendRevertOffer(detail.id)"
              >发送还原确认</button>
              <button
                v-if="detail.can_revert"
                type="button"
                class="btn-danger btn-sm"
                :disabled="actionLoading"
                @click="doRevert(detail.id)"
              >一键还原</button>
              <button
                v-if="detail.can_fix_misclassified_withdrawal"
                type="button"
                class="btn-secondary btn-sm"
                :disabled="actionLoading"
                @click="doFixMisclassifiedWithdrawal(detail.id)"
              >修正为已撤回</button>
              <button
                v-if="detail.can_refund_rarity_review_guard_failure"
                type="button"
                class="btn-secondary btn-sm"
                :disabled="actionLoading"
                @click="doRefundRarityReview(detail.id)"
              >关闭旧稀有度单并返还次数</button>
              <button
                v-if="detail.can_retry_player_revert_request"
                type="button"
                class="btn-secondary btn-sm"
                :disabled="actionLoading"
                @click="doRetryRevertRequest(detail.id)"
              >重试修复撤回 AI</button>
              <button
                v-if="detail.can_reset_player_revert_request"
                type="button"
                class="btn-danger btn-sm"
                :disabled="actionLoading"
                @click="doResetRevertRequest(detail.id)"
              >重置修复撤回</button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div v-if="bugReclaimDetail" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeBugReclaimDetail">
        <div class="modal-content modal-lg bug-reclaim-detail-modal s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h5>{{ bugReclaimDetail.item_name }} — {{ bugReclaimDetail.adventurer_name }} #{{ bugReclaimDetail.adventurer_id }}</h5>
            <button class="modal-close" @click="closeBugReclaimDetail">✕</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <div class="brd-summary">
              <p><strong>玩家：</strong>{{ bugReclaimDetail.nickname || bugReclaimDetail.username || '未知' }}</p>
              <p><strong>判定类型：</strong>{{ bugIssueLabel(bugReclaimDetail.issue_type) }}</p>
              <p>
                <strong>非法增量：</strong>
                <template v-if="bugReclaimDetail.quantity_adjusted">
                  系统 +{{ bugReclaimDetail.computed_illegal_quantity }} → 认定 +{{ bugReclaimDetail.illegal_quantity }}
                </template>
                <template v-else>+{{ bugReclaimDetail.illegal_quantity }}</template>
                <template v-if="bugReclaimDetail.reclaim_mode !== 'text_correction'">
                  · <strong>扣物</strong> {{ bugReclaimDetail.reclaim_item_quantity || 0 }}
                  · <strong>扣金币</strong> {{ bugReclaimDetail.gold_to_deduct || 0 }}
                </template>
              </p>
              <div
                v-if="marketProv(bugReclaimDetail)"
                class="brd-market-prov"
              >
                <p><strong>市场购入来源</strong></p>
                <p>
                  卖家：
                  {{ marketProv(bugReclaimDetail)?.seller_nickname
                    || marketProv(bugReclaimDetail)?.seller_username
                    || marketProv(bugReclaimDetail)?.seller_name
                    || '未知' }}
                  · 冒险者 #{{ marketProv(bugReclaimDetail)?.seller_adventurer_id }}
                </p>
                <p class="muted small">
                  挂单 #{{ marketProv(bugReclaimDetail)?.listing_id }}
                  · 本次购入 ×{{ marketProv(bugReclaimDetail)?.purchase_quantity }}
                  · 实付 {{ marketProv(bugReclaimDetail)?.buyer_paid }} 金
                  · 卖家到账 {{ marketProv(bugReclaimDetail)?.seller_income }} 金
                  <template v-if="marketProv(bugReclaimDetail)?.purchased_at">
                    · {{ formatTime(marketProv(bugReclaimDetail)?.purchased_at) }}
                  </template>
                </p>
                <p v-if="marketProv(bugReclaimDetail)?.source_trace?.skipped?.length" class="muted small source-trace-note">
                  来源回溯：已跳过
                  <template v-for="(s, si) in marketProv(bugReclaimDetail)?.source_trace?.skipped || []" :key="s.item_log_id || si">
                    「{{ s.description }}」<template v-if="Number(si) < (marketProv(bugReclaimDetail)?.source_trace?.skipped?.length || 0) - 1"> → </template>
                  </template>
                  → 有效「{{ marketProv(bugReclaimDetail)?.source_trace?.effective?.description }}」
                </p>
              </div>
              <div class="qty-adjust-row brd-qty-adjust">
                <label class="small muted">认定非法数量（系统 +{{ bugReclaimComputedQty(bugReclaimDetail) }}，可上下调）</label>
                <input
                  v-model.number="bugReclaimQtyDraft[bugReclaimDetail.issue_key]"
                  type="number"
                  min="0"
                  class="qty-adjust-input"
                  :disabled="bugReclaimDetail.reclaim_mode === 'text_correction'"
                />
                <button
                  type="button"
                  class="btn-primary btn-sm"
                  :disabled="bugReclaimLoading || bugReclaimAdjustingKey === bugReclaimDetail.issue_key || bugReclaimDetail.reclaim_mode === 'text_correction'"
                  @click="adjustBugReclaimQuantity(bugReclaimDetail)"
                >
                  保存认定
                </button>
              </div>

              <div class="brd-text-correct">
                <h6>文字修正（替代扣物）</h6>
                <p class="muted small">适用于名称/描述写错数量、单件装备误改 quantity 等；执行时只改目标堆文字（可选改 quantity），不扣物扣金。</p>
                <div class="text-correct-form">
                  <label class="small muted">修正名称</label>
                  <input
                    v-model="textDraftFor(bugReclaimDetail).name"
                    type="text"
                    class="text-correct-name"
                    maxlength="100"
                  />
                  <label class="small muted">修正描述</label>
                  <textarea
                    v-model="textDraftFor(bugReclaimDetail).desc"
                    class="text-correct-desc"
                    rows="3"
                  />
                  <label class="small muted">修正数量（可选，留空则不改 quantity）</label>
                  <input
                    v-model.number="textDraftFor(bugReclaimDetail).quantity"
                    type="number"
                    min="1"
                    class="qty-adjust-input"
                    placeholder="不改"
                  />
                </div>
                <div class="text-correct-actions">
                  <button
                    type="button"
                    class="btn-primary btn-sm"
                    :disabled="bugReclaimLoading || bugReclaimTextAdjustingKey === bugReclaimDetail.issue_key"
                    @click="adjustBugReclaimText(bugReclaimDetail)"
                  >
                    保存文字修正
                  </button>
                  <button
                    v-if="bugReclaimDetail.reclaim_mode === 'text_correction'"
                    type="button"
                    class="btn-secondary btn-sm"
                    :disabled="bugReclaimLoading || bugReclaimTextAdjustingKey === bugReclaimDetail.issue_key"
                    @click="revertBugReclaimText(bugReclaimDetail)"
                  >
                    取消文字修正
                  </button>
                </div>
              </div>
              <p v-if="timelineAppealGroups(bugReclaimDetail).length" class="brd-structure">
                <strong>结构：</strong>
                共 <b>{{ timelineAppealCount(bugReclaimDetail) }}</b> 次玩家申诉
                （每次申诉 = 玩家单独提交并审核通过的一单），
                其中 <b>{{ timelineIllegalAppealCount(bugReclaimDetail) }}</b> 次判非法，
                合计 <b>{{ timelineSteps(bugReclaimDetail).length }}</b> 条审计操作。
              </p>
              <p v-if="bugReclaimDetail.reclaim_narrative?.summary" class="brd-reclaim-reason">
                <strong>回收说明：</strong>{{ bugReclaimDetail.reclaim_narrative.summary }}
              </p>
              <p v-if="flowMeta(bugReclaimDetail).detail" class="brd-verdict">
                <strong>流向判定：</strong>{{ flowMeta(bugReclaimDetail).label }}
                <template v-if="flowMeta(bugReclaimDetail).chain_role">
                  · {{ chainRoleLabel(flowMeta(bugReclaimDetail).chain_role) }}
                </template>
                — {{ flowMeta(bugReclaimDetail).detail }}
              </p>
              <p v-if="timelineMeta(bugReclaimDetail).verdict_text" class="brd-verdict">
                {{ timelineMeta(bugReclaimDetail).verdict_text }}
              </p>
            </div>

            <h6>按申诉分组</h6>
            <p class="brd-appeal-hint muted small">
              下面每一张卡片 = 玩家的一次独立申诉。不同申诉编号是多次单独提交，不是同一次申诉里的两步。
            </p>
            <p v-if="!timelineAppealGroups(bugReclaimDetail).length" class="muted small">
              暂无申诉链数据，请重新点「预览回收」刷新。
            </p>
            <div v-else class="brd-appeal-groups">
              <section
                v-for="grp in timelineAppealGroups(bugReclaimDetail)"
                :key="'grp' + grp.appeal_id"
                class="brd-appeal-group"
                :class="{ 'brd-appeal-illegal': grp.is_illegal, 'brd-appeal-fork': grp.is_fork && !grp.is_illegal }"
              >
                <div class="brd-appeal-group-head">
                  <span class="brd-appeal-index">第 {{ grp.appeal_index }}/{{ grp.appeal_count }} 次申诉</span>
                  <strong class="brd-appeal-id">#{{ grp.appeal_id }}</strong>
                  <span class="brd-step-badge">{{ grp.status_label }}</span>
                  <button
                    type="button"
                    class="btn-secondary btn-xs appeal-detail-link-btn"
                    :disabled="detailLoading"
                    @click="openDetail(grp.appeal_id)"
                  >
                    查看申诉
                  </button>
                </div>
                <p class="brd-player-summary">{{ grp.player_summary }}</p>
                <p class="brd-system-verdict" :class="{ 'brd-system-warn': grp.is_illegal }">{{ grp.system_verdict }}</p>

                <details class="brd-group-details" open>
                  <summary>{{ grp.steps?.length || 0 }} 条审计明细</summary>
                  <div
                    v-for="(step, si) in grp.steps || []"
                    :key="'gs' + si"
                    class="brd-inner-step"
                    :class="{ 'brd-step-illegal': step.is_illegal }"
                  >
                    <div class="brd-step-head">
                      <span class="brd-step-action">{{ step.action_label || auditOpLabel(step.op_type) }}</span>
                      <span v-if="step.is_illegal" class="tl-illegal-tag">非法 +{{ step.illegal_quantity }}</span>
                    </div>
                    <p class="brd-step-summary">{{ step.summary_text || formatStepFallback(step) }}</p>
                    <div v-if="step.op_type === 'update'" class="brd-step-fields">
                      <div v-if="step.target_type && step.target_id != null" class="muted small">
                        目标：{{ step.target_type }} #{{ step.target_id }}
                      </div>
                      <div v-if="step.name_before != null || step.name_after != null">
                        名称：{{ step.name_before ?? '—' }} → {{ step.name_after ?? '—' }}
                      </div>
                      <div v-if="step.qty_unchanged_note" class="brd-qty-unchanged">{{ step.qty_unchanged_note }}</div>
                      <div v-else-if="step.qty_before != null || step.qty_after != null">
                        数量：{{ step.qty_before ?? '—' }} → {{ step.qty_after ?? '—' }}
                      </div>
                      <div v-if="step.desc_before || step.desc_after" class="brd-desc-block">
                        <div v-if="step.desc_before"><span class="detail-label">改前描述</span> {{ step.desc_before }}</div>
                        <div v-if="step.desc_after"><span class="detail-label">改后描述</span> {{ step.desc_after }}</div>
                      </div>
                    </div>
                    <div v-else-if="step.op_type === 'create_item'" class="brd-step-fields">
                      <div>新建「{{ step.name_after || bugReclaimDetail.item_name }}」×{{ step.qty_after ?? 1 }}</div>
                    </div>
                    <p v-if="step.note" class="muted small brd-step-note">{{ step.note }}</p>
                  </div>
                </details>
              </section>
            </div>

            <details v-if="timelineSteps(bugReclaimDetail).length" class="brd-flat-steps">
              <summary>展开逐步时间线（{{ timelineSteps(bugReclaimDetail).length }} 步）</summary>
              <ol class="brd-step-list">
              <li
                v-for="(step, si) in timelineSteps(bugReclaimDetail)"
                :key="'brd' + si"
                class="brd-step"
                :class="{ 'brd-step-illegal': step.is_illegal, 'brd-step-seed': !step.is_illegal && (step.kind === 'seed_fork' || step.kind === 'seed_duplicate') }"
              >
                <div class="brd-step-head">
                  <span class="brd-step-appeal">申诉 #{{ step.appeal_id }}</span>
                  <span class="brd-step-badge">{{ timelineKindLabel(step) }}</span>
                  <span class="brd-step-action">{{ step.action_label || auditOpLabel(step.op_type) }}</span>
                  <span v-if="step.is_illegal" class="tl-illegal-tag">非法 +{{ step.illegal_quantity }}</span>
                  <span v-if="step.illegal_cumulative" class="muted small">累计非法 +{{ step.illegal_cumulative }}</span>
                </div>
                <p class="brd-step-summary">{{ step.summary_text || formatStepFallback(step) }}</p>
                <div v-if="step.op_type === 'update' && (step.name_before != null || step.qty_before != null)" class="brd-step-fields">
                  <div v-if="step.target_type && step.target_id != null" class="muted small">
                    目标：{{ step.target_type }} #{{ step.target_id }}
                  </div>
                  <div v-if="step.name_before != null || step.name_after != null">
                    名称：{{ step.name_before ?? '—' }} → {{ step.name_after ?? '—' }}
                  </div>
                  <div v-if="step.qty_before != null || step.qty_after != null">
                    数量：{{ step.qty_before ?? '—' }} → {{ step.qty_after ?? '—' }}
                  </div>
                  <div v-if="step.desc_before || step.desc_after" class="brd-desc-block">
                    <div v-if="step.desc_before"><span class="detail-label">改前描述</span> {{ step.desc_before }}</div>
                    <div v-if="step.desc_after"><span class="detail-label">改后描述</span> {{ step.desc_after }}</div>
                  </div>
                </div>
                <div v-else-if="step.op_type === 'create_item'" class="brd-step-fields">
                  <div>新建「{{ step.name_after || bugReclaimDetail.item_name }}」×{{ step.qty_after ?? 1 }}</div>
                  <div v-if="step.desc_after" class="brd-desc-block">
                    <span class="detail-label">描述</span> {{ step.desc_after }}
                  </div>
                </div>
                <p v-if="step.note" class="muted small brd-step-note">{{ step.note }}</p>
                <p v-if="step.reason" class="muted small">{{ lineageReasonLabel(step.reason) }}</p>
              </li>
            </ol>
            </details>

            <h6 v-if="(qa(bugReclaimDetail).appeal_grants || []).length">补发汇总</h6>
            <ul v-if="(qa(bugReclaimDetail).appeal_grants || []).length" class="brd-grant-list">
              <li v-for="(g, gi) in qa(bugReclaimDetail).appeal_grants || []" :key="g.audit_log_id || gi">
                <strong>申诉 #{{ g.appeal_id }}</strong>
                · audit #{{ g.audit_log_id }}
                · {{ g.summary || g.action }}
                <span v-if="g.is_illegal" class="tl-illegal-tag">非法 +{{ g.illegal_quantity || g.quantity }}</span>
                <span v-else class="muted small">（种子/合法）</span>
                <div v-if="g.note" class="muted small">{{ g.note }}</div>
              </li>
            </ul>

            <div class="brd-modal-actions">
              <button
                type="button"
                class="btn-secondary btn-sm"
                :disabled="bugReclaimLoading || bugReclaimDismissingKey === bugReclaimDetail.issue_key"
                @click="dismissBugReclaimFalsePositive(bugReclaimDetail); closeBugReclaimDetail()"
              >
                标记误报
              </button>
              <button type="button" class="btn-secondary btn-sm" @click="closeBugReclaimDetail">关闭</button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div
        v-if="bugReclaimHistoryDetail"
        class="modal-overlay bug-reclaim-history-overlay s1-mobile-surface s1-mobile-overlay"
        @click.self="closeBugReclaimHistoryDetail"
      >
        <div class="modal-content modal-lg bug-reclaim-history-modal s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h5>回收明细 #{{ bugReclaimHistoryDetail.id }} · 批次 #{{ bugReclaimHistoryDetail.batch_id }}</h5>
            <button class="modal-close" @click="closeBugReclaimHistoryDetail">✕</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <div class="history-detail-summary">
              <p><strong>玩家/冒险者：</strong>{{ bugReclaimHistoryDetail.nickname || bugReclaimHistoryDetail.username || '未知玩家' }} · {{ bugReclaimHistoryDetail.adventurer_name || '未知冒险者' }} #{{ bugReclaimHistoryDetail.adventurer_id }}</p>
              <p><strong>对象：</strong>{{ bugReclaimHistoryDetail.item_name }} · {{ bugIssueLabel(bugReclaimHistoryDetail.issue_type) }}</p>
              <p><strong>执行：</strong>异常 {{ bugReclaimHistoryDetail.illegal_quantity }} · 扣物 {{ bugReclaimHistoryDetail.reclaimed_item_quantity }} · 扣金币 {{ bugReclaimHistoryDetail.gold_deducted }}</p>
              <p><strong>状态：</strong>{{ bugReclaimHistoryStatusLabel(bugReclaimHistoryDetail) }}</p>
              <p v-if="bugReclaimHistoryDetail.batch_note"><strong>批次备注：</strong>{{ bugReclaimHistoryDetail.batch_note }}</p>
              <p v-if="bugReclaimHistoryDetail.revert_unavailable_reason" class="muted">
                {{ bugReclaimHistoryDetail.revert_unavailable_reason }}
              </p>
            </div>

            <details class="history-json-block" open>
              <summary>执行结果与恢复快照</summary>
              <pre>{{ JSON.stringify(bugReclaimHistoryDetail.result || {}, null, 2) }}</pre>
            </details>
            <details class="history-json-block">
              <summary>回收前 / 回收后</summary>
              <pre>{{ JSON.stringify({ before: bugReclaimHistoryDetail.before, after: bugReclaimHistoryDetail.after }, null, 2) }}</pre>
            </details>
            <details class="history-json-block">
              <summary>流向明细（{{ bugReclaimHistoryDetail.route?.length || 0 }} 段）</summary>
              <pre>{{ JSON.stringify(bugReclaimHistoryDetail.route || [], null, 2) }}</pre>
            </details>
            <details v-if="bugReclaimHistoryDetail.is_reverted" class="history-json-block" open>
              <summary>回溯结果</summary>
              <pre>{{ JSON.stringify(bugReclaimHistoryDetail.revert_result || {}, null, 2) }}</pre>
            </details>

            <div class="brd-modal-actions">
              <button
                v-if="bugReclaimHistoryDetail.can_revert"
                type="button"
                class="btn-danger btn-sm"
                :disabled="bugReclaimHistoryReverting"
                @click="revertBugReclaimHistoryItem"
              >{{ bugReclaimHistoryReverting ? '回溯中…' : '回溯这一条' }}</button>
              <button type="button" class="btn-secondary btn-sm" @click="closeBugReclaimHistoryDetail">关闭</button>
            </div>
          </div>
        </div>
      </div>
    </Teleport>

    <Teleport to="body">
      <div v-if="itemCatalogOpen" class="modal-overlay item-catalog-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeItemChangeCatalog">
        <div class="modal-content item-catalog-modal s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h5>申诉物品变更总览</h5>
            <button class="modal-close" @click="closeItemChangeCatalog">✕</button>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <div class="item-catalog-toolbar">
              <label class="small muted">排序</label>
              <select v-model="itemCatalogSort" class="catalog-sort-select" @change="fetchItemChangeCatalog">
                <option value="name">物品名</option>
                <option value="appeals">申诉次数</option>
                <option value="recent">最近变更</option>
                <option value="reclaim">回收状态</option>
              </select>
              <label class="small muted">展示</label>
              <input v-model.number="itemCatalogLimit" type="number" min="50" max="2000" class="quota-num" />
              <button type="button" class="btn-secondary btn-sm" :disabled="itemCatalogLoading" @click="fetchItemChangeCatalog">
                刷新
              </button>
              <span v-if="itemCatalogSummary" class="muted small catalog-summary">
                共 <b>{{ itemCatalogSummary.total_tracks }}</b> 条
                · 新增类 {{ itemCatalogSummary.create_tracks }}
                · 改量类 {{ itemCatalogSummary.qty_tracks }}
                · 待回收 {{ itemCatalogSummary.preview_eligible }}
                · 已执行 {{ itemCatalogSummary.reclaim_executed }}
                <template v-if="itemCatalogSummary.skipped_single_appeal">
                  · 已隐藏单次申诉 {{ itemCatalogSummary.skipped_single_appeal }}
                </template>
                <template v-if="itemCatalogSummary.shown_count < itemCatalogSummary.total_tracks">
                  （展示 {{ itemCatalogSummary.shown_count }}）
                </template>
              </span>
            </div>
            <div v-if="itemCatalogLoading" class="muted small">加载中…</div>
            <div v-else-if="!itemCatalogItems.length" class="muted small">暂无新增/改量记录</div>
            <div v-else class="item-catalog-table-wrap">
              <table class="appeal-table item-catalog-table">
                <thead>
                  <tr>
                    <th>物品</th>
                    <th>玩家</th>
                    <th>来源</th>
                    <th>变更</th>
                    <th>回收状态</th>
                    <th>申诉明细</th>
                  </tr>
                </thead>
                <tbody>
                  <tr v-for="row in itemCatalogItems" :key="row.track_key">
                    <td class="catalog-col-item">
                      <div><strong>{{ row.item_name }}</strong></div>
                      <div v-if="row.name_variants?.length > 1" class="muted small name-variants">
                        同名变体：{{ row.name_variants.join(' · ') }}
                      </div>
                      <div class="muted small">{{ row.item_type }} · {{ row.item_rarity }}</div>
                      <div v-if="row.target_id" class="muted small">{{ row.target_type }} #{{ row.target_id }}</div>
                      <div v-if="row.current_quantity != null" class="muted small">现存 ×{{ row.current_quantity }}</div>
                    </td>
                    <td class="catalog-col-player">
                      <div>{{ row.nickname || row.username || '未知' }}</div>
                      <div class="muted small">{{ row.adventurer_name }} #{{ row.adventurer_id }}</div>
                    </td>
                    <td class="catalog-col-source">
                      <div>{{ row.item_source_label || '—' }}</div>
                      <div v-if="row.item_source_description" class="muted small source-line">{{ row.item_source_description }}</div>
                    </td>
                    <td class="catalog-col-change">
                      <div>{{ row.change_summary }}</div>
                      <div class="muted small">{{ row.appeal_count }} 次申诉 · {{ row.event_count }} 步</div>
                    </td>
                    <td class="catalog-col-reclaim">
                      <span class="reclaim-status-tag" :class="'rs-' + row.reclaim_status">{{ row.reclaim_status_label }}</span>
                    </td>
                    <td class="catalog-col-events">
                      <details class="catalog-events">
                        <summary>{{ row.event_count }} 步</summary>
                        <ul class="catalog-event-list">
                          <li v-for="ev in row.events || []" :key="ev.audit_log_id">
                            <div>
                              <strong>#{{ ev.appeal_id }}</strong> · {{ ev.summary }}
                              <span class="muted small">{{ formatTime(ev.audit_at) }}</span>
                            </div>
                            <div v-if="ev.player_note" class="muted small">玩家：{{ ev.player_note }}</div>
                            <div v-if="ev.ai_summary" class="muted small">AI：{{ ev.ai_summary }}</div>
                          </li>
                        </ul>
                      </details>
                    </td>
                  </tr>
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, computed, onMounted, onUnmounted, watch } from 'vue'
import Pagination from '@/components/Pagination.vue'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatDateTime } from '@/utils/time'
import { useSocket } from '@/composables/useSocket'
import { useS1AppealRefresh } from '../composables/useS1AppealRefresh'

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const socket = useSocket()

type AppealQuotaPool = 'other' | 'standard'

interface AdminAppealQuotaPoolState {
  base_limit: number
  bonus: number
  limit: number
  unlimited?: boolean
  used_24h: number
  remaining: number
  reset_at: string | null
  quota_mode?: 'daily_first_balance_pool'
  today_remaining?: number
  balance_remaining?: number
  balance_cap?: number
  next_balance?: number
}

interface AdminAppealQuota {
  user_id: number
  username: string
  nickname?: string | null
  quota_pools: Record<AppealQuotaPool, AdminAppealQuotaPoolState>
  total_appeals: number
  active_appeals: number
}

const quotaPoolOptions: Array<{ key: AppealQuotaPool; label: string }> = [
  { key: 'other', label: '全文申诉（其他问题 / 稀有度复核）' },
  { key: 'standard', label: '其余申诉' },
]

const { refreshing: loading, refresh: refreshList } = useS1AppealRefresh(fetchListSnapshot)
const listLoaded = ref(false)
const listError = ref('')
const items = ref<any[]>([])
const pagination = ref<any>(null)
const page = ref(1)
const statusFilter = ref('awaiting_review')
const searchQuery = ref('')
const activeSearch = ref('')
const detail = ref<any>(null)
const detailLoading = ref(false)
const actionLoading = ref(false)
const rowActionId = ref<number | null>(null)
const rejectReason = ref('')

const quotaQuery = ref('')
const quota = ref<AdminAppealQuota | null>(null)
const quotaLoading = ref(false)
const quotaActionLoading = ref(false)
const quotaAddCount = ref<Record<AppealQuotaPool, number>>({ other: 1, standard: 1 })
const dailyAppealLimits = ref<Record<AppealQuotaPool, number>>({ other: 5, standard: 5 })
const fullTextBalanceCap = ref(5)
const dailyAppealLimitLoading = ref(false)
const dailyAppealLimitSaving = ref<AppealQuotaPool | null>(null)
const balanceCapSaving = ref(false)
const dailyAppealLimitLoaded = ref(false)

const crossCharacterLimit = ref(100)
const crossCharacterLoading = ref(false)
const crossCharacterPreview = ref<any>(null)
const crossCharacterItems = computed(() => crossCharacterPreview.value?.items || [])

const classScrollReclaimLimit = ref(20)
const classScrollReclaimLoading = ref(false)
const classScrollReclaimPreview = ref<any>(null)
const classScrollReclaimItems = ref<any[]>([])
const classScrollReviewItems = ref<any[]>([])
const classScrollExecutableItems = computed(() => (
  classScrollReclaimItems.value.filter((item) => item.executable)
))

function classScrollRarityLabel(rarity?: string) {
  return ({ epic: '史诗', legendary: '传说' } as Record<string, string>)[rarity || ''] || rarity || '未知稀有度'
}

const bugReclaimLimit = ref(50)
const bugReclaimLoading = ref(false)
const bugReclaimDismissingKey = ref<string | null>(null)
const bugReclaimAdjustingKey = ref<string | null>(null)
const bugReclaimTextAdjustingKey = ref<string | null>(null)
const bugReclaimQtyDraft = ref<Record<string, number>>({})
const bugReclaimTextDraft = ref<Record<string, { name: string; desc: string; quantity: number | null }>>({})
const bugReclaimPreview = ref<any>(null)
const bugReclaimItems = ref<any[]>([])
let bugReclaimPreviewRunId = 0
const bugReclaimHistory = ref<any[]>([])
const bugReclaimHistoryPage = ref(1)
const bugReclaimHistoryTotal = ref(0)
const bugReclaimHistoryTotalPages = ref(1)
const bugReclaimHistoryLoading = ref(false)
const bugReclaimHistoryDetail = ref<any>(null)
const bugReclaimHistoryDetailLoading = ref(false)
const bugReclaimHistoryReverting = ref(false)
const bugReclaimDetail = ref<any>(null)

const qtyShortfallLimit = ref(50)
const qtyShortfallLoading = ref(false)
const qtyShortfallDismissingKey = ref<string | null>(null)
const qtyShortfallAdjustingKey = ref<string | null>(null)
const qtyShortfallQtyDraft = ref<Record<string, number>>({})
const qtyShortfallPreview = ref<any>(null)
const qtyShortfallItems = ref<any[]>([])
const qtyShortfallHistory = ref<any[]>([])
const catalogDescPollutionLimit = ref(100)
const catalogDescPollutionLoading = ref(false)
const catalogDescPollutionDismissingKey = ref<string | null>(null)
const catalogDescPollutionPreview = ref<any>(null)
const catalogDescPollutionItems = ref<any[]>([])

const itemCatalogOpen = ref(false)
const itemCatalogLoading = ref(false)
const itemCatalogItems = ref<any[]>([])
const itemCatalogSummary = ref<any>(null)
const itemCatalogLimit = ref(500)
const itemCatalogSort = ref('name')

const revertOfferLimit = ref(50)
const revertOfferBulkLoading = ref(false)
const revertOfferPreview = ref<any>(null)
const revertOfferItems = computed(() => revertOfferPreview.value?.items || [])
const revertOfferSendableCount = computed(
  () => revertOfferItems.value.filter((i: { can_send?: boolean }) => i.can_send).length,
)

const misWithdrawLimit = ref(50)
const misWithdrawLoading = ref(false)
const misWithdrawPreview = ref<any>(null)
const misWithdrawItems = computed(() => misWithdrawPreview.value?.items || [])
const misWithdrawFixableCount = computed(
  () => misWithdrawItems.value.filter((i: { can_fix?: boolean }) => i.can_fix).length,
)

const stuckRevertLimit = ref(50)
const stuckRevertLoading = ref(false)
const stuckRevertPreview = ref<any>(null)
const stuckRevertItems = computed(() => stuckRevertPreview.value?.items || [])
const stuckRevertRetryableCount = computed(
  () => stuckRevertItems.value.filter((i: { can_retry?: boolean }) => i.can_retry).length,
)

const rarityRefundLimit = ref(50)
const rarityRefundLoading = ref(false)
const rarityRefundPreview = ref<any>(null)
const rarityRefundItems = computed(() => rarityRefundPreview.value?.items || [])
const rarityRefundableCount = computed(
  () => rarityRefundItems.value.filter((i: { can_refund?: boolean }) => i.can_refund).length,
)

function appealStatusLabel(row: { status?: string; status_label?: string } | string) {
  if (typeof row === 'object' && row?.status_label) return row.status_label
  return statusLabel(typeof row === 'object' ? (row.status || '') : row)
}

function appealExecutionTitle(row: { execution_state?: string }) {
  return ({
    not_applied: '未落库',
    partially_applied: '部分变更已落库',
    applied: '变更已落库',
    reverted: '落库变更已撤销',
  } as Record<string, string>)[row.execution_state || 'not_applied'] || '落库状态未知'
}

function appealExecutionDescription(row: {
  execution_state?: string
  applied_change_count?: number
}) {
  const count = Number(row.applied_change_count || 0)
  if (row.execution_state === 'applied') {
    return `审计台账确认已有 ${count} 条变更落库；下方仍是 AI 拟议方案，实际结果以落库变更台账为准。`
  }
  if (row.execution_state === 'partially_applied') {
    return `审计台账确认已有 ${count} 条变更落库，但拟议方案并未全部执行；请以落库变更台账为准。`
  }
  if (row.execution_state === 'reverted') {
    return '历史审计记录中的变更均已撤销；下方内容仅保留为原始拟议方案。'
  }
  return '没有审计台账记录，说明下方内容只是 AI 拟议方案，没有任何数据真正落库。'
}

function isCancelledBeforeAi(detail: {
  status?: string
  cancellation_kind?: string
  ai_status?: string
  ai_raw_response?: unknown
}) {
  if ((detail.status || '') !== 'cancelled') return false
  if (detail.cancellation_kind === 'player' || detail.cancellation_kind === 'admin') return true
  return (
    (detail.ai_status || '') in { pending: 1, processing: 1, cancelled: 1 }
    && !detail.ai_raw_response
  )
}

function statusLabel(s: string) {
  const m: Record<string, string> = {
    processing: 'AI 处理中',
    awaiting_review: '待审核',
    approved: '已通过',
    rejected: '已驳回',
    cancelled: '已结束',
    reverted: '已还原',
  }
  return m[s] || s
}

function aiLabel(s: string) {
  const m: Record<string, string> = {
    pending: '排队',
    processing: '分析中',
    done: '完成',
    failed: '失败',
    cancelled: '已取消',
  }
  return m[s] || s
}

function reviewAiLabel(s: string) {
  const m: Record<string, string> = {
    pending: '等待 DS 结果',
    processing: '复核中',
    done: '已完成',
    failed: '失败，未生成结论',
    cancelled: '未执行（已取消）',
  }
  return m[s] || s || '未开始'
}

function parseJsonText(value: unknown): unknown {
  if (typeof value !== 'string') return value
  try {
    return JSON.parse(value)
  } catch {
    return value
  }
}

function typeLabel(t: string) {
  const m: Record<string, string> = {
    trait: '特质', equipment: '装备', warehouse: '仓库', skill: '技能',
    item: '物品', currency: '金币',
  }
  return m[t] || t
}

function changeActionLabel(action: string) {
  return ({ update: '修改', delete: '删除', create: '新增', add: '补发' } as Record<string, string>)[action] || action
}

function auditOpLabel(op: string) {
  return ({
    update: '修改', delete: '删除', create_item: '补发物品', create_trait: '补发特质',
    create_skill: '补发技能', currency: '补发金币', team_pool: '团队池均分',
    tavern_subsidy_team: '酒馆补贴（团队）', tavern_subsidy_self: '酒馆补贴（个人）',
    reclaim_remove: '追回移除', reclaim_item: '追回补发', reclaim_compensation: '追回折价扣款',
  } as Record<string, string>)[op] || op
}

const AUDIT_FIELD_LABELS: Record<string, string> = {
  name: '名称',
  custom_desc: '描述',
  description: '描述',
  description_ai: 'AI 描述',
  type: '类型',
  rarity: '稀有度',
  quantity: '数量',
  level: '等级',
  amount: '金额',
  currency_code: '货币',
  balance_after: '余额',
  item_name: '物品',
  removed_qty: '移除数量',
  shortage_qty: '缺口数量',
  total_amount: '总额',
  per_member: '人均',
  mode: '分配方式',
  reason: '原因',
  destination: '落位',
  requested_destination: '请求落位',
  uses_left: '剩余次数',
  evolution_stage: '进化阶段',
  was_new: '是否新条目',
  sourced_from_catalog: '来自图鉴',
  recovery_policy: '恢复策略',
  recovery_stage: '恢复阶段',
  recovery_required: '恢复所需',
  recovery_hours: '恢复时长(h)',
  code: '货币',
}

function destinationLabel(dest: string) {
  return ({ equipment: '装备栏', warehouse: '仓库' } as Record<string, string>)[dest] || dest
}

function previewChangeTitle(c: { action?: string; record?: { name?: string }; after?: unknown; type?: string }) {
  if (c.action === 'delete') return String(c.record?.name || '')
  if (c.after && typeof c.after === 'object' && c.after !== null) {
    const name = (c.after as Record<string, unknown>).name
    if (name) return String(name)
  }
  return ''
}

function previewGrantFields(c: { action?: string; type?: string; after?: unknown }) {
  if (!c.after || typeof c.after !== 'object') return [] as Array<{ key: string; val: unknown }>
  const p = c.after as Record<string, unknown>
  const rows: Array<{ key: string; val: unknown }> = []
  const add = (key: string, val: unknown) => {
    if (val !== undefined && val !== null && val !== '') rows.push({ key, val })
  }
  if (c.type === 'item') {
    add('name', p.name)
    add('destination', destinationLabel(String(p.destination || '')))
    add('quantity', p.quantity ?? 1)
    add('type', p.type)
    add('rarity', p.rarity)
    add('description', p.description)
    add('reason', p.reason)
    add('uses_left', p.uses_left)
  } else if (c.type === 'trait' || c.type === 'skill') {
    add('name', p.name)
    add('description', p.description)
    add('reason', p.reason)
  } else if (c.type === 'currency') {
    add('amount', `+${p.amount} ${p.code || p.currency_code || 'gold'}`)
    add('reason', p.reason)
  } else if (c.type === 'team_pool' || c.type === 'tavern_subsidy') {
    add('total_amount', p.total_amount ?? p.amount)
    add('per_member', p.per_member)
    add('mode', p.mode)
    add('reason', p.reason)
  } else if (c.action === 'reclaim') {
    add('name', p.name)
    add('quantity', p.quantity)
    add('reason', p.reason)
  }
  return rows
}

function formatPreviewChange(c: { action?: string; type?: string; after?: unknown }) {
  const rows = previewGrantFields(c)
  if (rows.length) {
    return rows.map((r) => `${auditFieldLabel(r.key)}：${fmtPreviewValue(r.val)}`).join(' · ')
  }
  return fmtPreviewValue(c.after)
}

function fmtPreviewValue(v: unknown) {
  if (v === null || v === undefined || v === '') return '(空)'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (typeof v === 'object') {
    try {
      const s = JSON.stringify(v)
      return s.length > 120 ? s.slice(0, 120) + '…' : s
    } catch {
      return '(复杂对象)'
    }
  }
  const s = String(v)
  return s.length > 120 ? s.slice(0, 120) + '…' : s
}

function auditFieldLabel(field: string) {
  return AUDIT_FIELD_LABELS[field] || field
}

function isLongAuditText(v: unknown) {
  return typeof v === 'string' && v.length > 80
}

function fmtAuditValue(v: unknown, field = '') {
  if (v === null || v === undefined || v === '') return '(空)'
  if (typeof v === 'boolean') return v ? '是' : '否'
  if (field === 'destination' || field === 'requested_destination') {
    return destinationLabel(String(v))
  }
  if (typeof v === 'object') {
    try {
      const s = JSON.stringify(v)
      return s.length > 120 ? s.slice(0, 120) + '…' : s
    } catch {
      return '(复杂对象)'
    }
  }
  const s = String(v)
  return s.length > 120 ? s.slice(0, 120) + '…' : s
}

function auditRecordFields(record: Record<string, unknown> | null | undefined) {
  if (!record) return {}
  const order = [
    'name', 'destination', 'requested_destination', 'type', 'rarity', 'quantity', 'uses_left',
    'custom_desc', 'description', 'description_ai', 'level', 'reason', 'was_new', 'sourced_from_catalog',
    'recovery_policy', 'recovery_stage', 'recovery_required', 'recovery_hours',
  ]
  const out: Record<string, unknown> = {}
  for (const key of order) {
    if (record[key] !== undefined && record[key] !== null && record[key] !== '') {
      out[key] = record[key]
    }
  }
  for (const [key, val] of Object.entries(record)) {
    if (!(key in out) && val !== undefined && val !== null && val !== '') {
      out[key] = val
    }
  }
  return out
}

function qa(item: any) {
  return item?.quantity_analysis || {}
}

function bugIssueLabel(issue: string) {
  return ({
    appeal_create_grant: '申诉新补（×>1）',
    appeal_qty_increase: '申诉改量累计',
    identity_reappeal: '身份归位后又申诉',
    duplicate_lineage_reappeal: '补发重复后又申诉',
    mixed_lineage: '两种链路叠加',
    quantity_cluster: '数量异常簇（累计）',
    create_item_cluster: '脏物保留又补发（簇）',
    mixed_cluster: '抬量 + 补发（同物簇）',
  } as Record<string, string>)[issue] || issue
}

function lineageReasonLabel(reason: string) {
  return ({
    appeal_create_grant: '申诉新补 quantity>1',
    appeal_qty_increase: '申诉增加 quantity',
    identity_reappeal_qty: '归位后又改 quantity',
    identity_reappeal_create: '归位后又补发',
    duplicate_lineage_qty: '补发链路后又改 quantity',
    duplicate_lineage_create: '补发链路后又补发',
  } as Record<string, string>)[reason] || reason
}

function timelineMeta(item: any) {
  return item?.appeal_timeline || {}
}

function flowMeta(item: any) {
  return item?.post_appeal_flow || {}
}

function chainRoleLabel(role: string) {
  return ({
    downstream_buyer: '下游买方（非源头）',
    upstream_seller: '源头卖方',
    source_reappeal: '源头再申诉',
  } as Record<string, string>)[role] || role
}

function timelineSteps(item: any) {
  const steps = item?.appeal_timeline?.steps || []
  let cumulative = 0
  return steps.map((step: any) => {
    if (step.is_illegal) cumulative += Number(step.illegal_quantity || 0)
    return { ...step, illegal_cumulative: cumulative }
  })
}

function timelineAppealGroups(item: any) {
  const groups = item?.appeal_timeline?.appeal_groups
  if (groups?.length) return groups
  const steps = timelineSteps(item)
  if (!steps.length) return []
  const byId = new Map<number, any>()
  for (const step of steps) {
    const aid = Number(step.appeal_id)
    if (!byId.has(aid)) {
      byId.set(aid, {
        appeal_id: aid,
        steps: [],
        is_illegal: false,
        illegal_quantity: 0,
        is_fork: step.kind === 'seed_fork',
        is_seed: step.kind === 'seed_duplicate',
      })
    }
    const g = byId.get(aid)!
    g.steps.push(step)
    if (step.is_illegal) {
      g.is_illegal = true
      g.illegal_quantity += Number(step.illegal_quantity || 0)
    }
  }
  const sorted = [...byId.values()].sort((a, b) => a.appeal_id - b.appeal_id)
  const total = sorted.length
  return sorted.map((g, i) => ({
    ...g,
    appeal_index: i + 1,
    appeal_count: total,
    status_label: g.is_illegal ? `判非法 +${g.illegal_quantity}` : g.is_fork ? '身份归位（合法）' : '合法',
    player_summary: g.steps.map((s: any) => s.summary_text).filter(Boolean).join('；') || '见明细',
    system_verdict: g.is_illegal ? '系统判定非法（请重新预览以获取完整说明）' : '系统未判非法',
  }))
}

function timelineAppealCount(item: any) {
  return timelineMeta(item).appeal_count || timelineAppealGroups(item).length
}

function timelineIllegalAppealCount(item: any) {
  return timelineMeta(item).illegal_appeal_count
    ?? timelineAppealGroups(item).filter((g: { is_illegal: boolean }) => g.is_illegal).length
}

function relatedAppealIds(item: any): number[] {
  const ids = new Set<number>()
  const add = (v: unknown) => {
    const n = Number(v)
    if (Number.isFinite(n) && n > 0) ids.add(n)
  }
  add(item?.appeal_id)
  add(timelineMeta(item).seed_appeal_id)
  add(timelineMeta(item).fork_appeal_id)
  for (const g of timelineAppealGroups(item)) add(g.appeal_id)
  for (const id of flowMeta(item).appeal_ids || []) add(id)
  for (const g of qa(item).appeal_grants || []) add(g.appeal_id)
  for (const ev of item?.lineage || []) add(ev.appeal_id)
  return [...ids].sort((a, b) => a - b)
}

function timelineKindLabel(step: any) {
  if (step.is_illegal) {
    if (step.op_type === 'create_item') return '非法补发'
    if (step.op_type === 'update') return '非法改量'
    return '非法跟进'
  }
  return ({
    seed_fork: '身份归位',
    seed_duplicate: '重复补发',
    context: '同物变更',
    illegal_followup: '非法跟进',
  } as Record<string, string>)[step.kind] || step.kind
}

function formatStepFallback(step: any) {
  if (step.op_type === 'create_item') {
    return `新增「${step.name_after || '物品'}」×${step.qty_after ?? 1}`
  }
  return `${auditOpLabel(step.op_type)} @ #${step.appeal_id}`
}

function openBugReclaimDetail(item: any) {
  bugReclaimDetail.value = item
  if (bugReclaimQtyDraft.value[item.issue_key] == null) {
    bugReclaimQtyDraft.value[item.issue_key] = Number(item.illegal_quantity || 0)
  }
  ensureTextDraft(item)
}

function defaultTextDraft(item: any) {
  const q = qa(item)
  const tc = item.text_correction
  return {
    name: tc?.corrected_name ?? q.original_name ?? q.current_name ?? item.item_name ?? '',
    desc: tc?.corrected_desc ?? q.original_desc ?? q.current_desc ?? '',
    quantity: tc?.corrected_quantity ?? q.origin_field_quantity ?? null,
  }
}

function ensureTextDraft(item: any) {
  if (!bugReclaimTextDraft.value[item.issue_key]) {
    bugReclaimTextDraft.value[item.issue_key] = defaultTextDraft(item)
  }
}

function textDraftFor(item: any) {
  ensureTextDraft(item)
  return bugReclaimTextDraft.value[item.issue_key]
}

function syncBugReclaimTextDraft(items: any[]) {
  const next = { ...bugReclaimTextDraft.value }
  for (const item of items) {
    if (!next[item.issue_key]) {
      next[item.issue_key] = defaultTextDraft(item)
    }
  }
  bugReclaimTextDraft.value = next
}

function closeBugReclaimDetail() {
  bugReclaimDetail.value = null
}

function routeKindLabel(kind: string) {
  return ({
    current_item: '现存扣物',
    market_sell: '市场卖钱',
    market_barter: '市场换物',
    market_purchase_undo: '市场购入撤销',
    text_correction: '文字修正',
    building: '建筑消耗',
    buyback_sell: '收购出售',
    unknown_consumed: '去向不明',
  } as Record<string, string>)[kind] || kind
}

function marketProv(item: any) {
  return item?.market_provenance || qa(item).market_provenance || null
}

function formatTime(iso: string | null) {
  if (!iso) return '-'
  return formatDateTime(iso)
}

function truncateText(v: unknown, max = 60) {
  if (v === null || v === undefined) return ''
  const s = String(v)
  return s.length > max ? s.slice(0, max) + '…' : s
}

async function loadDailyAppealLimit() {
  dailyAppealLimitLoading.value = true
  dailyAppealLimitLoaded.value = false
  try {
    const res = await store.fetchDormitorySettings('selected')
    if (res?.code !== 200) {
      message?.error(res?.message || '全服申诉次数加载失败')
      return
    }
    const settings = res.data?.settings || []
    const keys: Record<AppealQuotaPool, string> = {
      other: 'data_appeal_other_daily_limit',
      standard: 'data_appeal_daily_limit',
    }
    for (const pool of quotaPoolOptions) {
      const item = settings.find(
        (setting: { key?: string }) => setting.key === keys[pool.key],
      )
      const value = Number(item?.value)
      if (!Number.isInteger(value) || value < 1 || value > 100) {
        message?.error(`${pool.label}次数配置无效，请检查后端设置`)
        return
      }
      dailyAppealLimits.value[pool.key] = value
    }
    const balanceCapItem = settings.find(
      (setting: { key?: string }) => setting.key === 'data_appeal_other_balance_cap',
    )
    const balanceCap = Number(balanceCapItem?.value)
    if (!Number.isInteger(balanceCap) || balanceCap < 0 || balanceCap > 100) {
      message?.error('全文申诉余额池上限配置无效，请检查后端设置')
      return
    }
    fullTextBalanceCap.value = balanceCap
    dailyAppealLimitLoaded.value = true
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '全服申诉次数加载失败')
  } finally {
    dailyAppealLimitLoading.value = false
  }
}

async function saveFullTextBalanceCap() {
  const cap = Number(fullTextBalanceCap.value)
  if (!Number.isInteger(cap) || cap < 0 || cap > 100) {
    message?.error('全文申诉余额池上限必须是 0 到 100 的整数')
    return
  }
  balanceCapSaving.value = true
  try {
    const res = await store.updateDormitorySetting(
      'data_appeal_other_balance_cap',
      cap,
      'selected',
    )
    if (res?.code === 200) {
      fullTextBalanceCap.value = cap
      dailyAppealLimitLoaded.value = true
      message?.success(`全文申诉余额池上限已改为 ${cap}`)
      if (quota.value) await fetchQuota()
    } else {
      message?.error(res?.message || '保存失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '保存失败')
  } finally {
    balanceCapSaving.value = false
  }
}

async function saveDailyAppealLimit(quotaPool: AppealQuotaPool) {
  const limit = Number(dailyAppealLimits.value[quotaPool])
  if (!Number.isInteger(limit) || limit < 1 || limit > 100) {
    message?.error(`${quotaPoolLabel(quotaPool)}每日次数必须是 1 到 100 的整数`)
    return
  }
  dailyAppealLimitSaving.value = quotaPool
  try {
    const settingKey = quotaPool === 'other'
      ? 'data_appeal_other_daily_limit'
      : 'data_appeal_daily_limit'
    const res = await store.updateDormitorySetting(settingKey, limit, 'selected')
    if (res?.code === 200) {
      dailyAppealLimits.value[quotaPool] = limit
      dailyAppealLimitLoaded.value = true
      message?.success(`${quotaPoolLabel(quotaPool)}每日次数已改为 ${limit}`)
      if (quota.value) await fetchQuota()
    } else {
      message?.error(res?.message || '保存失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '保存失败')
  } finally {
    dailyAppealLimitSaving.value = null
  }
}

async function fetchQuota() {
  const key = quotaQuery.value.trim()
  if (!key) {
    message?.error('请输入用户ID或用户名')
    return
  }
  quotaLoading.value = true
  try {
    const isId = /^\d+$/.test(key)
    const res = await store.adminGetAppealQuota(isId ? { user_id: key } : { username: key })
    if (res?.code === 200) {
      const data = res.data || {}
      const legacyPool = {
        base_limit: Number(data.base_limit || data.limit || 0),
        bonus: Number(data.bonus || 0),
        limit: Number(data.limit || 0),
        used_24h: Number(data.used_24h || 0),
        remaining: Number(data.remaining || 0),
        reset_at: data.reset_at || null,
      }
      quota.value = data.quota_pools
        ? data as AdminAppealQuota
        : {
            ...data,
            quota_pools: {
              other: { ...legacyPool },
              standard: legacyPool,
            },
          } as AdminAppealQuota
    } else {
      quota.value = null
      message?.error(res?.message || '查询失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '查询失败')
  } finally {
    quotaLoading.value = false
  }
}

function quickQuota(userId: number) {
  quotaQuery.value = String(userId)
  fetchQuota()
}

function quotaPoolLabel(quotaPool: AppealQuotaPool) {
  return quotaPoolOptions.find(pool => pool.key === quotaPool)?.label || quotaPool
}

async function doResetQuota(quotaPool: AppealQuotaPool) {
  if (!quota.value) return
  if (!confirm(`确定清零玩家「${quota.value.nickname || quota.value.username}」今日已用的${quotaPoolLabel(quotaPool)}次数？`)) return
  quotaActionLoading.value = true
  try {
    const res = await store.adminResetAppealQuota(quota.value.user_id, quotaPool)
    if (res?.code === 200) {
      message?.success(`已清零${quotaPoolLabel(quotaPool)}已用次数`)
      await fetchQuota()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    quotaActionLoading.value = false
  }
}

async function doAddQuota(quotaPool: AppealQuotaPool) {
  if (!quota.value) return
  const n = Number(quotaAddCount.value[quotaPool])
  if (!Number.isInteger(n) || n <= 0) {
    message?.error('请输入正整数次数')
    return
  }
  quotaActionLoading.value = true
  try {
    const res = await store.adminAddAppealQuota(quota.value.user_id, n, quotaPool)
    if (res?.code === 200) {
      message?.success(`${quotaPoolLabel(quotaPool)}已增加 ${n} 次额度`)
      await fetchQuota()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    quotaActionLoading.value = false
  }
}

async function doResetAllQuota(quotaPool: AppealQuotaPool) {
  if (!confirm(`确定全服重置${quotaPoolLabel(quotaPool)}？将清零所有存活冒险者对应玩家今日已用次数。`)) return
  quotaActionLoading.value = true
  try {
    const res = await store.adminResetAllAppealQuota(quotaPool)
    if (res?.code === 200) {
      message?.success(`已全服重置 ${res.data?.reset_count ?? 0} 名玩家的${quotaPoolLabel(quotaPool)}次数`)
      if (quota.value) await fetchQuota()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    quotaActionLoading.value = false
  }
}

function bugReclaimComputedQty(item: any) {
  return Number(item?.computed_illegal_quantity ?? item?.illegal_quantity ?? 0)
}

function syncBugReclaimQtyDraft(items: any[]) {
  const next: Record<string, number> = { ...bugReclaimQtyDraft.value }
  for (const item of items) {
    if (next[item.issue_key] == null) {
      next[item.issue_key] = Number(item.illegal_quantity || 0)
    }
  }
  bugReclaimQtyDraft.value = next
}

function applyBugReclaimItemUpdate(updated: any) {
  if (!updated?.issue_key) return
  const idx = bugReclaimItems.value.findIndex((i) => i.issue_key === updated.issue_key)
  if (idx >= 0) {
    bugReclaimItems.value[idx] = { ...bugReclaimItems.value[idx], ...updated }
  }
  if (bugReclaimDetail.value?.issue_key === updated.issue_key) {
    bugReclaimDetail.value = { ...bugReclaimDetail.value, ...updated }
  }
  bugReclaimQtyDraft.value[updated.issue_key] = Number(updated.illegal_quantity || 0)
  recomputeBugReclaimSummary()
}

function bugReclaimBatchLabel(status: string) {
  return ({
    executed: '已执行',
    dismissed: '误报',
    adjusted: '数量认定',
    preview: '预览',
    failed: '失败',
  } as Record<string, string>)[status] || status
}

function bugReclaimHistoryStatusLabel(item: any) {
  if (item?.is_reverted) return '已回溯'
  return bugReclaimBatchLabel(String(item?.status || ''))
}

function closeBugReclaimHistoryDetail() {
  if (bugReclaimHistoryReverting.value) return
  bugReclaimHistoryDetail.value = null
}

async function openBugReclaimHistoryDetail(itemId: number) {
  bugReclaimHistoryDetailLoading.value = true
  try {
    const res = await store.adminGetDataAppealBugReclaim(itemId)
    if (res?.code === 200) {
      bugReclaimHistoryDetail.value = res.data
    } else {
      message?.error(res?.message || '回收明细加载失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '回收明细加载失败')
  } finally {
    bugReclaimHistoryDetailLoading.value = false
  }
}

async function revertBugReclaimHistoryItem() {
  const item = bugReclaimHistoryDetail.value
  if (!item?.id || !item.can_revert) return
  if (!confirm(`确认只回溯回收明细 #${item.id}？系统会按该条冻结快照恢复物品与金币；不会回溯同批次其他条目。`)) return
  bugReclaimHistoryReverting.value = true
  try {
    const res = await store.adminRevertDataAppealBugReclaim(item.id, '管理员逐条回溯异常收益回收')
    if (res?.code === 200) {
      bugReclaimHistoryDetail.value = res.data
      message?.success(`回收明细 #${item.id} 已回溯`)
      await fetchBugReclaimHistory(bugReclaimHistoryPage.value)
    } else {
      message?.error(res?.message || '逐条回溯失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '逐条回溯失败')
  } finally {
    bugReclaimHistoryReverting.value = false
  }
}

function recomputeBugReclaimSummary() {
  const items = bugReclaimItems.value
  bugReclaimPreview.value = {
    ...(bugReclaimPreview.value || {}),
    summary: {
      count: items.length,
      illegal_quantity: items.reduce((s, i) => s + Number(i.illegal_quantity || 0), 0),
      reclaim_item_quantity: items.reduce((s, i) => s + Number(i.reclaim_item_quantity || 0), 0),
      gold_to_deduct: Math.round(items.reduce((s, i) => s + Number(i.gold_to_deduct || 0), 0) * 100) / 100,
      buyer_refund_gold: Math.round(items.reduce((s, i) => s + Number(i.buyer_refund_gold || 0), 0) * 100) / 100,
      seller_clawback_gold: Math.round(items.reduce((s, i) => s + Number(i.seller_clawback_gold || 0), 0) * 100) / 100,
      market_undo_count: items.filter((i) => i.reclaim_mode === 'market_purchase_undo').length,
      text_correction_count: items.filter((i) => i.reclaim_mode === 'text_correction').length,
    },
  }
}

async function adjustBugReclaimText(item: any) {
  ensureTextDraft(item)
  const draft = textDraftFor(item)
  const name = String(draft.name || '').trim()
  if (!name) {
    message?.error('修正名称不能为空')
    return
  }
  const label = item.item_name || item.issue_key
  const qtyHint = draft.quantity != null && Number.isFinite(Number(draft.quantity))
    ? `\n数量→${draft.quantity}`
    : '\n数量：不修改'
  if (!window.confirm(`保存「${label}」文字修正？\n\n名称→${name}${qtyHint}\n\n执行时不扣物扣金。`)) return

  bugReclaimTextAdjustingKey.value = item.issue_key
  try {
    const qty = draft.quantity
    const res = await store.adminAdjustDataAppealBugReclaimText({
      issue_key: item.issue_key,
      corrected_name: name,
      corrected_desc: draft.desc,
      corrected_quantity: qty != null && Number.isFinite(Number(qty)) ? Number(qty) : null,
      note: '管理员文字修正',
      item,
    })
    if (res?.code === 200 && res.data?.item) {
      applyBugReclaimItemUpdate(res.data.item)
      bugReclaimTextDraft.value[item.issue_key] = defaultTextDraft(res.data.item)
      message?.success(res.message || '已保存文字修正')
      await fetchBugReclaimHistory()
    } else {
      message?.error(res?.message || '保存失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '保存失败')
  } finally {
    bugReclaimTextAdjustingKey.value = null
  }
}

async function revertBugReclaimText(item: any) {
  if (!window.confirm('取消文字修正，恢复为扣物/扣金方案？')) return
  bugReclaimTextAdjustingKey.value = item.issue_key
  try {
    const res = await store.adminAdjustDataAppealBugReclaimText({
      issue_key: item.issue_key,
      corrected_name: '',
      item,
      revert: true,
    })
    if (res?.code === 200 && res.data?.item) {
      applyBugReclaimItemUpdate(res.data.item)
      bugReclaimTextDraft.value[item.issue_key] = defaultTextDraft(res.data.item)
      message?.success('已取消文字修正')
      await fetchBugReclaimHistory()
    } else {
      message?.error(res?.message || '取消失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '取消失败')
  } finally {
    bugReclaimTextAdjustingKey.value = null
  }
}

async function adjustBugReclaimQuantity(item: any) {
  const computed = bugReclaimComputedQty(item)
  const qty = Number(bugReclaimQtyDraft.value[item.issue_key])
  if (!Number.isFinite(qty) || qty < 0) {
    message?.error('请输入有效的非法数量')
    return
  }
  const label = item.item_name || item.issue_key
  const hint = qty === computed
    ? `恢复「${label}」为系统计算非法量 +${computed}？`
    : qty > computed
      ? `认定「${label}」非法量为 +${qty}（系统仅累计 +${computed}，上调）？\n\n扣物优先扣现存，不足部分按异常缺货价扣金币。`
      : `认定「${label}」非法量为 +${qty}（系统累计 +${computed}）？\n\n扣物/扣金币将按认定值重算。`
  if (!window.confirm(hint)) return

  bugReclaimAdjustingKey.value = item.issue_key
  try {
    const res = await store.adminAdjustDataAppealBugReclaimQuantity({
      issue_key: item.issue_key,
      illegal_quantity: qty,
      note: qty === computed ? '' : (
        qty > computed
          ? `管理员认定非法 +${qty}（系统 +${computed}，单件/多次改量漏算上调）`
          : `管理员认定非法 +${qty}（系统 +${computed}）`
      ),
      item,
    })
    if (res?.code === 200 && res.data?.item) {
      applyBugReclaimItemUpdate(res.data.item)
      message?.success(res.message || (res.data.reverted ? '已恢复系统计算值' : '已认定非法数量'))
      await fetchBugReclaimHistory()
    } else {
      message?.error(res?.message || '认定失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '认定失败')
  } finally {
    bugReclaimAdjustingKey.value = null
  }
}

async function dismissBugReclaimFalsePositive(item: any) {
  const label = item.item_name || item.issue_key
  if (!window.confirm(`标记「${label}」为误报？\n\n不会扣物/扣金币，且后续预览不再出现。`)) return
  bugReclaimDismissingKey.value = item.issue_key
  try {
    const res = await store.adminDismissDataAppealBugReclaim({
      issue_key: item.issue_key,
      note: '管理员标记误报',
      item,
    })
    if (res?.code === 200) {
      bugReclaimItems.value = bugReclaimItems.value.filter((i) => i.issue_key !== item.issue_key)
      recomputeBugReclaimSummary()
      message?.success('已标记误报')
      await fetchBugReclaimHistory()
    } else {
      message?.error(res?.message || '标记失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '标记失败')
  } finally {
    bugReclaimDismissingKey.value = null
  }
}

async function previewClassScrollReclaims() {
  const limit = Number(classScrollReclaimLimit.value || 20)
  if (!Number.isInteger(limit) || limit <= 0 || limit > 50) {
    message?.error('展示上限必须是 1-50 的整数')
    return
  }
  classScrollReclaimLoading.value = true
  try {
    const res = await store.adminPreviewClassScrollReclaims(limit)
    if (res?.code === 200) {
      classScrollReclaimPreview.value = res.data
      classScrollReclaimItems.value = res.data?.items || []
      classScrollReviewItems.value = res.data?.review_items || []
      if (res.data?.summary?.scan_complete !== true) {
        message?.error('全历史扫描未完成，本次结果不可执行')
      } else {
        const summary = res.data?.summary || {}
        const filtered = Number(summary.catalog_rejected || 0) + Number(summary.dungeon_rejected || 0)
        message?.success(`全历史扫描完成：结构化候选 ${summary.signals || 0} 项，已过滤 ${filtered} 项，确认漏扣 ${summary.matched || 0} 项，待核查 ${summary.review_required || 0} 项`)
      }
    } else {
      message?.error(res?.message || '转职书回收预览失败')
    }
  } catch (e: unknown) {
    const err = e as { code?: string; message?: string; response?: { data?: { message?: string } } }
    if (err.code === 'ECONNABORTED' || String(err.message || '').includes('timeout')) {
      message?.error('全历史记录扫描超时，请稍后重试')
    } else {
      message?.error(err.response?.data?.message || '转职书回收预览失败')
    }
  } finally {
    classScrollReclaimLoading.value = false
  }
}

async function executeClassScrollReclaims() {
  const count = classScrollExecutableItems.value.length
  if (!count) return
  const missingCharge = Number(classScrollReclaimPreview.value?.summary?.missing_item_charge_gold || 0)
  const refund = Number(classScrollReclaimPreview.value?.summary?.buyer_refund_gold || 0)
  const clawback = Number(classScrollReclaimPreview.value?.summary?.seller_gold_clawback || 0)
  if (!window.confirm(
    `确认执行 ${count} 项转职书追回？\n\n能找到现物的将直接回收；找不到现物的将向原转职者扣除 ${missingCharge} 金币。市场成交撤销时退还买家实付 ${refund} 金币并扣回卖家实际到账 ${clawback} 金币；扣款余额不足时允许扣成负数。`,
  )) return
  classScrollReclaimLoading.value = true
  try {
    const res = await store.adminExecuteClassScrollReclaims(
      Number(classScrollReclaimLimit.value || 20),
      String(classScrollReclaimPreview.value?.snapshot_token || ''),
    )
    if (res?.code === 200) {
      message?.success(`已回收 ${res.data?.summary?.executed || 0} 项，跳过 ${res.data?.summary?.skipped || 0} 项`)
      classScrollReclaimPreview.value = null
      classScrollReclaimItems.value = []
      classScrollReviewItems.value = []
    } else {
      message?.error(res?.message || '转职书回收失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '转职书回收失败')
  } finally {
    classScrollReclaimLoading.value = false
  }
}

async function previewBugReclaims() {
  const limit = Number(bugReclaimLimit.value || 50)
  if (!Number.isInteger(limit) || limit <= 0 || limit > 200) {
    message?.error('扫描上限必须是 1-200 的整数')
    return
  }
  const runId = ++bugReclaimPreviewRunId
  bugReclaimLoading.value = true
  try {
    while (runId === bugReclaimPreviewRunId) {
      const res = await store.adminPreviewDataAppealBugReclaims(limit)
      if (res?.code !== 200) {
        message?.error(res?.message || '预览失败')
        return
      }
      bugReclaimPreview.value = res.data
      bugReclaimItems.value = res.data?.items || []
      syncBugReclaimQtyDraft(bugReclaimItems.value)
      syncBugReclaimTextDraft(bugReclaimItems.value)
      const scan = res.data?.summary?.scan
      if (scan?.complete !== false) {
        message?.success(`待处理 ${res.data?.summary?.total_candidates ?? bugReclaimItems.value.length} 项，本页 ${bugReclaimItems.value.length} 项`)
        return
      }
      const retryAfterMs = Math.max(100, Math.min(Number(scan?.retry_after_ms || 250), 2000))
      await new Promise<void>((resolve) => window.setTimeout(resolve, retryAfterMs))
    }
  } catch (e: unknown) {
    const err = e as { code?: string; message?: string; response?: { data?: { message?: string } } }
    if (err.code === 'ECONNABORTED' || String(err.message || '').includes('timeout')) {
      message?.error('当前扫描批次超时，进度已保存在数据库；请重新点击预览继续')
    } else {
      message?.error(err.response?.data?.message || '预览失败')
    }
  } finally {
    if (runId === bugReclaimPreviewRunId) {
      bugReclaimLoading.value = false
    }
  }
}

async function executeBugReclaims() {
  if (bugReclaimPreview.value?.summary?.scan?.complete === false) {
    message?.error('预览扫描尚未完成，不能执行回收')
    return
  }
  const count = bugReclaimItems.value.length
  const gold = bugReclaimPreview.value?.summary?.gold_to_deduct || 0
  const textCorr = bugReclaimPreview.value?.summary?.text_correction_count || 0
  if (!count) return
  let confirmMsg = `确认执行本次异常收益回收？将处理 ${count} 项`
  if (textCorr > 0) {
    confirmMsg += `（含 ${textCorr} 项文字修正，不扣物扣金）`
  }
  confirmMsg += `，常规扣金币 ${gold}，金币不足会扣成负数。`
  if (!confirm(confirmMsg)) return
  bugReclaimLoading.value = true
  try {
    const res = await store.adminExecuteDataAppealBugReclaims(
      Number(bugReclaimLimit.value || 50),
      '管理员执行异常收益回收',
      String(bugReclaimPreview.value?.snapshot_token || ''),
    )
    if (res?.code === 200) {
      message?.success(`已执行回收：${res.data?.summary?.count || 0} 项`)
      bugReclaimPreview.value = null
      bugReclaimItems.value = []
      await fetchBugReclaimHistory()
    } else {
      message?.error(res?.message || '执行失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '执行失败')
  } finally {
    bugReclaimLoading.value = false
  }
}

async function fetchBugReclaimHistory(page = bugReclaimHistoryPage.value) {
  if (bugReclaimHistoryLoading.value) return
  bugReclaimHistoryLoading.value = true
  try {
    const res = await store.adminListDataAppealBugReclaims({ page, per_page: 10 })
    if (res?.code === 200) {
      bugReclaimHistory.value = res.data?.items || []
      bugReclaimHistoryPage.value = Number(res.data?.page || page)
      bugReclaimHistoryTotal.value = Number(res.data?.total || 0)
      bugReclaimHistoryTotalPages.value = Number(res.data?.total_pages || 1)
    }
  } catch {
    // 历史只是辅助信息，失败不打断主流程。
  } finally {
    bugReclaimHistoryLoading.value = false
  }
}

function qtyShortfallIssueLabel(issue: string) {
  return ({
    identity_update_quantity_missing: '改名/描述漏写数量',
    create_item_quantity_shortfall: '新增物品数量漏给',
    quantity_shortfall: '数量漏给',
  } as Record<string, string>)[issue] || issue
}

function qtyShortfallBatchLabel(status: string) {
  return ({
    executed: '已补发',
    dismissed: '误报',
    adjusted: '数量认定',
    preview: '预览',
    failed: '失败',
  } as Record<string, string>)[status] || status
}

function qtyShortfallComputedQty(item: any) {
  return Number(item?.computed_shortfall_quantity ?? item?.shortfall_quantity ?? 0)
}

function syncQtyShortfallQtyDraft(items: any[]) {
  const next: Record<string, number> = { ...qtyShortfallQtyDraft.value }
  for (const item of items) {
    if (next[item.issue_key] == null) {
      next[item.issue_key] = Number(item.shortfall_quantity || 0)
    }
  }
  qtyShortfallQtyDraft.value = next
}

function recomputeQtyShortfallSummary() {
  const items = qtyShortfallItems.value
  qtyShortfallPreview.value = {
    ...(qtyShortfallPreview.value || {}),
    summary: {
      ...(qtyShortfallPreview.value?.summary || {}),
      shown_count: items.length,
      shortfall_quantity: items.reduce((s, i) => s + Number(i.shortfall_quantity || 0), 0),
      computed_shortfall_quantity: items.reduce((s, i) => s + Number(i.computed_shortfall_quantity || 0), 0),
      needs_admin_quantity: items.filter((i) => i.needs_admin_quantity).length,
    },
  }
}

function applyQtyShortfallItemUpdate(updated: any) {
  if (!updated?.issue_key) return
  const idx = qtyShortfallItems.value.findIndex((i) => i.issue_key === updated.issue_key)
  if (idx >= 0) {
    qtyShortfallItems.value[idx] = { ...qtyShortfallItems.value[idx], ...updated }
  }
  qtyShortfallQtyDraft.value[updated.issue_key] = Number(updated.shortfall_quantity || 0)
  recomputeQtyShortfallSummary()
}

async function adjustQtyShortfallQuantity(item: any) {
  const computed = qtyShortfallComputedQty(item)
  const qty = Number(qtyShortfallQtyDraft.value[item.issue_key])
  if (!Number.isFinite(qty) || qty < 0) {
    message?.error('请输入有效的补发数量')
    return
  }
  const label = item.item_name || item.issue_key
  const hint = qty === computed
    ? `恢复「${label}」为系统计算补发量 +${computed}？`
    : `认定「${label}」补发量为 +${qty}（系统 +${computed}）？`
  if (!window.confirm(hint)) return

  qtyShortfallAdjustingKey.value = item.issue_key
  try {
    const res = await store.adminAdjustDataAppealQtyShortfallQuantity({
      issue_key: item.issue_key,
      shortfall_quantity: qty,
      note: qty === computed ? '' : `管理员认定补发 +${qty}（系统 +${computed}）`,
      item,
    })
    if (res?.code === 200 && res.data?.item) {
      applyQtyShortfallItemUpdate(res.data.item)
      message?.success(res.message || (res.data.reverted ? '已恢复系统计算值' : '已认定补发数量'))
      await fetchQtyShortfallHistory()
    } else {
      message?.error(res?.message || '认定失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '认定失败')
  } finally {
    qtyShortfallAdjustingKey.value = null
  }
}

async function dismissQtyShortfallFalsePositive(item: any) {
  const label = item.item_name || item.issue_key
  if (!window.confirm(`标记「${label}」为误报？\n\n不会补发，且后续预览不再出现。`)) return
  qtyShortfallDismissingKey.value = item.issue_key
  try {
    const res = await store.adminDismissDataAppealQtyShortfall({
      issue_key: item.issue_key,
      note: '管理员标记误报',
      item,
    })
    if (res?.code === 200) {
      qtyShortfallItems.value = qtyShortfallItems.value.filter((i) => i.issue_key !== item.issue_key)
      recomputeQtyShortfallSummary()
      message?.success('已标记误报')
      await fetchQtyShortfallHistory()
    } else {
      message?.error(res?.message || '标记失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '标记失败')
  } finally {
    qtyShortfallDismissingKey.value = null
  }
}

async function previewQtyShortfalls() {
  const limit = Number(qtyShortfallLimit.value || 50)
  if (!Number.isInteger(limit) || limit <= 0 || limit > 200) {
    message?.error('扫描上限必须是 1-200 的整数')
    return
  }
  qtyShortfallLoading.value = true
  try {
    const res = await store.adminPreviewDataAppealQtyShortfalls(limit)
    if (res?.code === 200) {
      qtyShortfallPreview.value = res.data
      qtyShortfallItems.value = res.data?.items || []
      syncQtyShortfallQtyDraft(qtyShortfallItems.value)
      message?.success(`发现漏给候选 ${res.data?.summary?.total_candidates ?? qtyShortfallItems.value.length} 项，本页 ${qtyShortfallItems.value.length} 项`)
    } else {
      message?.error(res?.message || '预览失败')
    }
  } catch (e: unknown) {
    const err = e as { code?: string; message?: string; response?: { data?: { message?: string } } }
    if (err.code === 'ECONNABORTED' || String(err.message || '').includes('timeout')) {
      message?.error('预览扫描超时（正式服约需 10–20 秒），请重试或缩小展示上限')
    } else {
      message?.error(err.response?.data?.message || '预览失败')
    }
  } finally {
    qtyShortfallLoading.value = false
  }
}

async function executeQtyShortfalls() {
  const count = qtyShortfallItems.value.length
  const qty = qtyShortfallPreview.value?.summary?.shortfall_quantity || 0
  const manual = qtyShortfallPreview.value?.summary?.needs_admin_quantity || 0
  if (!count) return
  let confirmMsg = `确认应用异常数量漏给补发？将检查 ${count} 项，认定补发总量 ${qty}。`
  if (manual > 0) confirmMsg += `\n\n仍有 ${manual} 项补发量为 0 或待人工认定，执行时会跳过这些项。`
  if (!confirm(confirmMsg)) return
  qtyShortfallLoading.value = true
  try {
    const res = await store.adminExecuteDataAppealQtyShortfalls(
      Number(qtyShortfallLimit.value || 50),
      '管理员执行异常数量漏给补发',
      String(qtyShortfallPreview.value?.snapshot_token || ''),
    )
    if (res?.code === 200) {
      message?.success(`已执行补发：${res.data?.summary?.count || 0} 项，数量 ${res.data?.summary?.granted_quantity || 0}`)
      qtyShortfallPreview.value = null
      qtyShortfallItems.value = []
      await fetchQtyShortfallHistory()
    } else {
      message?.error(res?.message || '执行失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '执行失败')
  } finally {
    qtyShortfallLoading.value = false
  }
}

async function fetchQtyShortfallHistory() {
  try {
    const res = await store.adminListDataAppealQtyShortfalls({ page: 1, per_page: 10 })
    if (res?.code === 200) qtyShortfallHistory.value = res.data?.items || []
  } catch {
    // 历史只是辅助信息，失败不打断主流程。
  }
}

function catalogDescPollutionIssueLabel(issue: string) {
  if (issue === 'catalog_fill') return '误补图鉴物描述'
  if (issue === 'cleared_custom') return '误清空定制描述'
  return issue || '未知'
}

async function previewCatalogDescPollution() {
  const limit = Number(catalogDescPollutionLimit.value || 100)
  if (!Number.isFinite(limit) || limit < 1) {
    message?.warning('展示上限无效')
    return
  }
  catalogDescPollutionLoading.value = true
  try {
    const res = await store.adminPreviewCatalogDescPollution(limit)
    if (res?.code === 200) {
      catalogDescPollutionPreview.value = res.data
      catalogDescPollutionItems.value = res.data?.items || []
      message?.success(`发现污染候选 ${res.data?.summary?.total_candidates ?? catalogDescPollutionItems.value.length} 项`)
    } else {
      message?.error(res?.message || '预览失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '预览失败')
  } finally {
    catalogDescPollutionLoading.value = false
  }
}

async function executeCatalogDescPollution() {
  const count = catalogDescPollutionItems.value.length
  if (!count) return
  if (!window.confirm(`确认还原本页 ${count} 条 custom_desc？执行前会自动备份，可用「回滚上次」撤销。`)) return
  catalogDescPollutionLoading.value = true
  try {
    const res = await store.adminExecuteCatalogDescPollution(
      Number(catalogDescPollutionLimit.value || 100),
      '管理员执行图鉴描述污染还原',
      String(catalogDescPollutionPreview.value?.snapshot_token || ''),
    )
    if (res?.code === 200) {
      message?.success(res.message || `已还原 ${res.data?.repaired ?? 0} 条`)
      catalogDescPollutionPreview.value = null
      catalogDescPollutionItems.value = []
    } else {
      message?.error(res?.message || '还原失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '还原失败')
  } finally {
    catalogDescPollutionLoading.value = false
  }
}

async function restoreCatalogDescPollution() {
  if (!window.confirm('确认回滚上一次图鉴描述污染还原？')) return
  catalogDescPollutionLoading.value = true
  try {
    const res = await store.adminRestoreCatalogDescPollution()
    if (res?.code === 200) {
      message?.success(res.message || `已回滚 ${res.data?.restored ?? 0} 条`)
    } else {
      message?.error(res?.message || '回滚失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '回滚失败')
  } finally {
    catalogDescPollutionLoading.value = false
  }
}

async function dismissCatalogDescPollution(item: { issue_key: string; item_name?: string }) {
  catalogDescPollutionDismissingKey.value = item.issue_key
  try {
    const res = await store.adminDismissCatalogDescPollution({
      issue_key: item.issue_key,
      note: item.item_name ? `误报：${item.item_name}` : '',
    })
    if (res?.code === 200) {
      catalogDescPollutionItems.value = catalogDescPollutionItems.value.filter((i) => i.issue_key !== item.issue_key)
      if (catalogDescPollutionPreview.value?.summary) {
        catalogDescPollutionPreview.value.summary.total_candidates = Math.max(
          0,
          Number(catalogDescPollutionPreview.value.summary.total_candidates || 0) - 1,
        )
        catalogDescPollutionPreview.value.summary.shown_count = catalogDescPollutionItems.value.length
      }
      message?.success('已标记误报')
    } else {
      message?.error(res?.message || '标记失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '标记失败')
  } finally {
    catalogDescPollutionDismissingKey.value = null
  }
}

async function fetchItemChangeCatalog() {
  itemCatalogLoading.value = true
  try {
    const res = await store.adminListAppealItemChangeCatalog({
      limit: itemCatalogLimit.value,
      sort: itemCatalogSort.value,
    })
    if (res?.code === 200) {
      itemCatalogItems.value = res.data?.items || []
      itemCatalogSummary.value = res.data?.summary || null
    } else {
      message?.error(res?.message || '加载申诉物品总览失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '加载申诉物品总览失败')
  } finally {
    itemCatalogLoading.value = false
  }
}

function openItemChangeCatalog() {
  itemCatalogOpen.value = true
  void fetchItemChangeCatalog()
}

function closeItemChangeCatalog() {
  itemCatalogOpen.value = false
}

function applySearch() {
  activeSearch.value = searchQuery.value.trim()
  void fetchList(1)
}

function clearSearch() {
  searchQuery.value = ''
  activeSearch.value = ''
  void fetchList(1)
}

function fetchList(p = 1) {
  page.value = p
  return refreshList()
}

async function fetchListSnapshot(isActive: () => boolean) {
  const params = {
    status: statusFilter.value || undefined,
    search: activeSearch.value || undefined,
    page: page.value,
    per_page: 20,
  }
  const isCurrent = () => isActive()
    && params.page === page.value
    && params.status === (statusFilter.value || undefined)
    && params.search === (activeSearch.value || undefined)
  try {
    const res = await store.adminListDataAppeals(params)
    if (!isCurrent()) return
    if (res?.code === 200) {
      items.value = res.data?.items || []
      listLoaded.value = true
      listError.value = ''
      pagination.value = {
        current_page: res.data?.page || 1,
        pages: Math.max(1, Math.ceil((res.data?.total || 0) / (res.data?.per_page || 20))),
        total: res.data?.total || 0,
      }
      maybeStartPolling()
    } else {
      listError.value = res?.message || '加载申诉列表失败'
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    if (isCurrent()) listError.value = err.response?.data?.message || '加载申诉列表失败'
  }
}

async function openDetail(id: number) {
  detailLoading.value = true
  try {
    const res = await store.adminGetDataAppeal(id)
    if (res?.code === 200) {
      detail.value = res.data
    } else {
      message?.error(res?.message || '加载申诉详情失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '加载申诉详情失败')
  } finally {
    detailLoading.value = false
  }
}

function closeDetail() {
  detail.value = null
}

async function doCancel(id: number) {
  if (!confirm('确定取消该申诉？取消后玩家可重新提交。')) return
  rowActionId.value = id
  actionLoading.value = true
  try {
    const res = await store.adminCancelDataAppeal(id)
    if (res?.code === 200) {
      message?.success('申诉已取消')
      if (detail.value?.id === id) closeDetail()
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    rowActionId.value = null
    actionLoading.value = false
  }
}

async function doRefundRarityReview(id: number) {
  if (!confirm(
    '确认关闭这条历史稀有度复核失败单，并给玩家返还 1 次全文申诉次数？\n'
    + '玩家需要重新提交稀有度复核。',
  )) return
  actionLoading.value = true
  try {
    const res = await store.adminRefundRarityReviewGuardFailure(id)
    if (res?.code === 200) {
      const remaining = res.data?.quota?.remaining
      message?.success(
        remaining == null
          ? '已关闭旧单并返还 1 次全文申诉次数'
          : `已关闭旧单并返还 1 次全文申诉次数，玩家剩余 ${remaining} 次`,
      )
      await openDetail(id)
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    actionLoading.value = false
  }
}

async function previewCrossCharacterAppeals() {
  const limit = Number(crossCharacterLimit.value || 100)
  if (!Number.isInteger(limit) || limit < 1 || limit > 200) {
    message?.error('展示上限必须是 1-200 的整数')
    return
  }
  crossCharacterLoading.value = true
  try {
    const res = await store.adminPreviewCrossCharacterDataAppeals(limit)
    if (res?.code === 200) {
      crossCharacterPreview.value = res.data
      const total = Number(res.data?.summary?.total_candidates || 0)
      message?.success(total ? `发现 ${total} 条跨角色探险申诉` : '未发现跨角色探险申诉')
    } else {
      message?.error(res?.message || '跨角色申诉扫描失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '跨角色申诉扫描失败')
  } finally {
    crossCharacterLoading.value = false
  }
}

async function executeCrossCharacterAppealReverts() {
  const appealIds = crossCharacterItems.value.map((item: { id: number }) => item.id)
  if (!appealIds.length) return
  const appliedCount = Number(crossCharacterPreview.value?.summary?.applied_to_revert || 0)
  const pendingCount = Number(crossCharacterPreview.value?.summary?.pending_to_cancel || 0)
  const confirmed = window.confirm(
    `确认处理本次预览的 ${appealIds.length} 条跨角色申诉？\n\n`
    + `将按审计台账撤销 ${appliedCount} 条已落库申诉，并取消 ${pendingCount} 条未落库工单。`
  )
  if (!confirmed) return

  crossCharacterLoading.value = true
  try {
    const res = await store.adminExecuteCrossCharacterDataAppealReverts(appealIds)
    if (res?.code === 200) {
      const reverted = res.data?.reverted?.length || 0
      const cancelled = res.data?.cancelled?.length || 0
      const errors = res.data?.errors?.length || 0
      if (errors) {
        message?.error(`已撤销 ${reverted} 条、取消 ${cancelled} 条；另有 ${errors} 条因资产已变化等原因未能撤销`)
      } else {
        message?.success(`已撤销 ${reverted} 条、取消 ${cancelled} 条跨角色申诉`)
      }
      await previewCrossCharacterAppeals()
      await fetchList(page.value)
    } else {
      message?.error(res?.message || '跨角色申诉撤销失败')
    }
  } catch (e: unknown) {
    const err = e as { response?: { data?: { message?: string } } }
    message?.error(err.response?.data?.message || '跨角色申诉撤销失败')
  } finally {
    crossCharacterLoading.value = false
  }
}

async function previewRarityRefunds() {
  rarityRefundLoading.value = true
  try {
    const res = await store.adminPreviewRarityReviewGuardFailures(rarityRefundLimit.value)
    if (res?.code === 200) {
      rarityRefundPreview.value = res.data
    } else {
      message?.error(res?.message || '预览失败')
    }
  } finally {
    rarityRefundLoading.value = false
  }
}

async function executeRarityRefunds() {
  const ids = rarityRefundItems.value
    .filter((i: { can_refund?: boolean }) => i.can_refund)
    .map((i: { id: number }) => i.id)
  if (!ids.length) {
    message?.error('当前预览没有可返还条目')
    return
  }
  if (!confirm(
    `确认关闭 ${ids.length} 条历史稀有度复核失败单，并分别返还 1 次全文申诉次数？\n`
    + '系统会给玩家发送次数返还站内信。',
  )) return
  rarityRefundLoading.value = true
  try {
    const res = await store.adminBulkRefundRarityReviewGuardFailures({
      appeal_ids: ids,
      notify_player: true,
    })
    if (res?.code === 200) {
      const refunded = res.data?.refunded?.length ?? 0
      const skipped = res.data?.skipped?.length ?? 0
      const errors = res.data?.errors?.length ?? 0
      message?.success(`已返还 ${refunded} 条，跳过 ${skipped} 条，失败 ${errors} 条`)
      await previewRarityRefunds()
      fetchList(page.value)
      if (detail.value?.id && ids.includes(detail.value.id)) {
        await openDetail(detail.value.id)
      }
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    rarityRefundLoading.value = false
  }
}

async function doRetryAi(id: number) {
  if (!confirm('重新提交 AI 分析？可复用的前序审核成品会保留，并回到「AI 处理中」。')) return
  rowActionId.value = id
  actionLoading.value = true
  try {
    const res = await store.adminRetryDataAppealAi(id)
    if (res?.code === 200) {
      message?.success('已重新提交 AI 分析')
      if (detail.value?.id === id) {
        await openDetail(id)
      }
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    rowActionId.value = null
    actionLoading.value = false
  }
}

async function doApprove(id: number) {
  if (!confirm('确认通过该申诉？将执行 AI 提出的修复方案。')) return
  actionLoading.value = true
  try {
    const res = await store.adminApproveDataAppeal(id)
    if (res?.code === 200) {
      message?.success('申诉已通过并执行修复')
      closeDetail()
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } catch (error: any) {
    message?.error(error?.response?.data?.message || error?.message || '操作失败')
  } finally {
    actionLoading.value = false
  }
}

async function doReject(id: number) {
  if (!confirm('确认驳回该申诉？')) return
  actionLoading.value = true
  try {
    const res = await store.adminRejectDataAppeal(id, rejectReason.value || undefined)
    if (res?.code === 200) {
      message?.success('申诉已驳回')
      rejectReason.value = ''
      closeDetail()
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    actionLoading.value = false
  }
}

async function doRevert(id: number) {
  if (!confirm('确认还原该申诉的数据修复？将按台账倒序撤销改动、扣回补发、重建被删条目（尽力还原）。')) return
  actionLoading.value = true
  try {
    const res = await store.adminRevertDataAppeal(id)
    if (res?.code === 200) {
      message?.success('已还原本次申诉的数据修复')
      if (detail.value?.id === id) {
        await openDetail(id)
      }
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    actionLoading.value = false
  }
}

async function previewMisWithdrawals() {
  misWithdrawLoading.value = true
  try {
    const res = await store.adminPreviewMisclassifiedWithdrawals(misWithdrawLimit.value)
    if (res?.code === 200) {
      misWithdrawPreview.value = res.data
    } else {
      message?.error(res?.message || '预览失败')
    }
  } finally {
    misWithdrawLoading.value = false
  }
}

async function executeMisWithdrawals() {
  const ids = misWithdrawItems.value
    .filter((i: { can_fix?: boolean }) => i.can_fix)
    .map((i: { id: number }) => i.id)
  if (!ids.length) {
    message?.error('当前预览没有可修正条目')
    return
  }
  if (!confirm(
    `确认将 ${ids.length} 条误标 rejected 的撤回申诉修正为 cancelled？\n`
    + '将向玩家发送状态修正邮件。',
  )) return
  misWithdrawLoading.value = true
  try {
    const res = await store.adminExecuteMisclassifiedWithdrawals({
      appeal_ids: ids,
      notify_player: true,
    })
    if (res?.code === 200) {
      const fixed = res.data?.fixed?.length ?? 0
      const skipped = res.data?.skipped?.length ?? 0
      const errors = res.data?.errors?.length ?? 0
      message?.success(`已修正 ${fixed} 条，跳过 ${skipped} 条，失败 ${errors} 条`)
      await previewMisWithdrawals()
      fetchList(page.value)
      if (detail.value?.id && ids.includes(detail.value.id)) {
        await openDetail(detail.value.id)
      }
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    misWithdrawLoading.value = false
  }
}

async function doFixMisclassifiedWithdrawal(id: number) {
  if (!confirm('确认将该申诉从「已驳回」修正为「已撤回」？将向玩家发送状态修正邮件。')) return
  actionLoading.value = true
  try {
    const res = await store.adminFixMisclassifiedWithdrawal(id, true)
    if (res?.code === 200) {
      message?.success('已修正为已撤回')
      await openDetail(id)
      fetchList(page.value)
      if (misWithdrawPreview.value) {
        await previewMisWithdrawals()
      }
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    actionLoading.value = false
  }
}

async function previewStuckRevertRequests() {
  stuckRevertLoading.value = true
  try {
    const res = await store.adminPreviewStuckRevertRequests(stuckRevertLimit.value)
    if (res?.code === 200) {
      stuckRevertPreview.value = res.data
    } else {
      message?.error(res?.message || '预览失败')
    }
  } finally {
    stuckRevertLoading.value = false
  }
}

async function retryStuckRevertRequests() {
  const ids = stuckRevertItems.value
    .filter((i: { can_retry?: boolean }) => i.can_retry)
    .map((i: { id: number }) => i.id)
  if (!ids.length) {
    message?.error('当前预览没有可重试条目')
    return
  }
  if (!confirm(`确认重试 ${ids.length} 条卡住的修复撤回 AI？`)) return
  stuckRevertLoading.value = true
  try {
    const res = await store.adminRetryStuckRevertRequests(ids)
    if (res?.code === 200) {
      const n = res.data?.retried?.length ?? 0
      message?.success(`已重试 ${n} 条`)
      await previewStuckRevertRequests()
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    stuckRevertLoading.value = false
  }
}

async function retryOneStuckRevert(id: number) {
  stuckRevertLoading.value = true
  try {
    const res = await store.adminRetryRevertRequest(id)
    if (res?.code === 200) {
      message?.success('已重新提交修复撤回 AI')
      await previewStuckRevertRequests()
      if (detail.value?.id === id) await openDetail(id)
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    stuckRevertLoading.value = false
  }
}

async function resetOneStuckRevert(id: number) {
  if (!confirm('确认重置？玩家将可重新申请撤回修复，已消耗的额度 stub 会作废。')) return
  stuckRevertLoading.value = true
  try {
    const res = await store.adminResetRevertRequest(id)
    if (res?.code === 200) {
      message?.success('已重置')
      await previewStuckRevertRequests()
      if (detail.value?.id === id) await openDetail(id)
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    stuckRevertLoading.value = false
  }
}

async function doRetryRevertRequest(id: number) {
  actionLoading.value = true
  try {
    const res = await store.adminRetryRevertRequest(id)
    if (res?.code === 200) {
      message?.success('已重新提交修复撤回 AI')
      await openDetail(id)
      fetchList(page.value)
      if (stuckRevertPreview.value) await previewStuckRevertRequests()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    actionLoading.value = false
  }
}

async function doResetRevertRequest(id: number) {
  if (!confirm('确认重置修复撤回？玩家将可重新申请。')) return
  actionLoading.value = true
  try {
    const res = await store.adminResetRevertRequest(id)
    if (res?.code === 200) {
      message?.success('已重置')
      await openDetail(id)
      fetchList(page.value)
      if (stuckRevertPreview.value) await previewStuckRevertRequests()
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    actionLoading.value = false
  }
}

async function previewRevertOffers() {
  revertOfferBulkLoading.value = true
  try {
    const res = await store.adminPreviewDataAppealRevertOffers({
      limit: revertOfferLimit.value,
      only_likely_mistake: true,
    })
    if (res?.code === 200) {
      revertOfferPreview.value = res.data
    } else {
      message?.error(res?.message || '预览失败')
    }
  } finally {
    revertOfferBulkLoading.value = false
  }
}

async function executeRevertOffers() {
  const ids = revertOfferItems.value
    .filter((i: { can_send?: boolean }) => i.can_send)
    .map((i: { id: number }) => i.id)
  if (!ids.length) {
    message?.error('当前预览没有可推送条目')
    return
  }
  if (!confirm(
    `确认向 ${ids.length} 名玩家个人页推送「同意则还原」确认？\n`
    + '（仅「我的冒险者 → 数据申诉」可见，不发邮件）',
  )) return
  revertOfferBulkLoading.value = true
  try {
    const res = await store.adminBulkSendDataAppealRevertOffers({
      appeal_ids: ids,
      only_likely_mistake: true,
    })
    if (res?.code === 200) {
      const sent = res.data?.sent?.length ?? 0
      const skipped = res.data?.skipped?.length ?? 0
      const errors = res.data?.errors?.length ?? 0
      message?.success(`已推送 ${sent} 条到个人页，跳过 ${skipped} 条，失败 ${errors} 条`)
      await previewRevertOffers()
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    revertOfferBulkLoading.value = false
  }
}

async function doSendRevertOffer(id: number) {
  const note = window.prompt(
    '可选：向玩家附带的说明（留空则使用默认文案）',
    '',
  )
  if (note === null) return
  actionLoading.value = true
  try {
    const res = await store.adminSendDataAppealRevertOffer(id, note.trim() || undefined)
    if (res?.code === 200) {
      message?.success('已向玩家个人页推送还原确认')
      if (detail.value?.id === id) {
        await openDetail(id)
      }
      fetchList(page.value)
    } else {
      message?.error(res?.message || '操作失败')
    }
  } finally {
    actionLoading.value = false
  }
}

let pollTimer: ReturnType<typeof setInterval> | null = null

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer)
    pollTimer = null
  }
}

function maybeStartPolling() {
  const watchingProcessing = !statusFilter.value || statusFilter.value === 'processing'
  const hasProcessingRow = items.value.some((a: { status: string }) => a.status === 'processing')
  if (!watchingProcessing || !hasProcessingRow) {
    stopPolling()
  } else if (!pollTimer) {
    pollTimer = setInterval(() => {
      if (!loading.value) fetchList(page.value)
    }, 5000)
  }
}

function onDataAppealChanged() {
  fetchList(page.value)
}

watch([statusFilter, items], maybeStartPolling, { deep: true })

onMounted(() => {
  socket.connect()
  socket.on('data_appeal_changed', onDataAppealChanged)
  loadDailyAppealLimit()
  fetchList(1)
})

onUnmounted(() => {
  bugReclaimPreviewRunId += 1
  stopPolling()
  socket.off('data_appeal_changed', onDataAppealChanged)
})
</script>

<style scoped lang="scss">
.admin-appeal-panel {
  min-width: 0;
  max-width: 100%;
}

.auto-mode-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 16px;
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
}

.auto-mode-toggle {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  cursor: pointer;
  font-size: 14px;
}

.auto-mode-hint {
  flex: 1;
  min-width: 200px;
}

.auto-mode-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
}

.revert-offer-pending {
  color: #f0a020;
}

.revert-offer-bar {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
}

.revert-offer-head {
  display: flex;
  flex-wrap: wrap;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 8px;
}

.revert-offer-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.stuck-revert-bar {
  margin-top: 12px;
}

.warn-tag {
  color: var(--warning-color, #c9a227);
  font-size: 12px;
}

.cancel-outcome-banner {
  margin: 0 0 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface-alt, rgba(128, 128, 128, 0.08));
}

.appeal-execution-banner {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 12px 0;
  padding: 10px 12px;
  border: 1px solid rgba(220, 53, 69, 0.5);
  border-radius: 8px;
  background: rgba(220, 53, 69, 0.1);
  color: var(--color-text);
  font-size: 13px;
}

.appeal-execution-banner.execution-applied {
  border-color: rgba(25, 135, 84, 0.5);
  background: rgba(25, 135, 84, 0.1);
}

.appeal-execution-banner.execution-partially_applied,
.appeal-execution-banner.execution-reverted {
  border-color: rgba(255, 193, 7, 0.55);
  background: rgba(255, 193, 7, 0.1);
}

.cancelled-ai-skip {
  margin: 8px 0;
}

.revert-offer-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-bottom: 8px;
  font-size: 13px;
}

.revert-offer-table-wrap {
  max-width: 100%;
  overflow-x: auto;
}

.filter-bar {
  display: flex;
  gap: 8px;
  margin-bottom: 12px;
  align-items: center;
  flex-wrap: wrap;
}

.appeal-search-input {
  flex: 1 1 220px;
  min-width: 180px;
  max-width: 360px;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg);
  color: var(--color-text);
}

.search-hint {
  margin: -4px 0 12px;
}

.quota-bar {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
}

.quota-input-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.daily-limit-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  margin-bottom: 10px;
  padding-bottom: 10px;
  border-bottom: 1px dashed var(--color-border);

  .quota-hint { margin-top: 4px; }
}

.daily-limit-actions {
  display: flex;
  flex: 0 0 auto;
  align-items: center;
  gap: 8px;
}

@media (max-width: 640px) {
  .daily-limit-row {
    align-items: stretch;
    flex-direction: column;
  }

  .daily-limit-actions {
    justify-content: flex-end;
  }
}

.quota-input {
  min-width: 160px;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg);
  color: var(--color-text);
}

.quota-num {
  width: 72px;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg);
  color: var(--color-text);
}

.quota-result {
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-border);
}

.quota-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 14px;
  font-size: 13px;
}

.quota-pool-admin-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
  margin-top: 10px;
}

.quota-pool-admin {
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
}

.quota-name { font-weight: 600; }
.quota-stat b { color: var(--color-text); }
.quota-stat .ok { color: #18a058; }
.quota-stat .warn { color: #d03050; }

.quota-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
}

.quota-hint { margin: 8px 0 0; }
.quota-spacer { flex: 1; min-width: 8px; }

@media (max-width: 760px) {
  .quota-pool-admin-grid {
    grid-template-columns: 1fr;
  }
}

.bug-reclaim-bar {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
}

.bug-reclaim-head {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;

  p { margin: 4px 0 0; }
}

.bug-reclaim-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  min-width: 260px;
}

.bug-reclaim-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px 16px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed var(--color-border);
  font-size: 13px;
}

.bug-reclaim-table-wrap {
  max-width: 100%;
  overflow-x: auto;
  margin-top: 10px;
}

.bug-reclaim-table {
  min-width: 1180px;
}

.qty-shortfall-bar {
  border-color: color-mix(in srgb, var(--color-primary, #2080f0) 35%, var(--color-border));
}

.qty-shortfall-table {
  min-width: 1120px;
}

.warning {
  color: var(--warning-color, #b8860b);
}

.json-mini {
  max-width: 360px;
  max-height: 220px;
  overflow: auto;
  margin: 0.4rem 0 0;
  padding: 0.45rem;
  border-radius: 6px;
  background: color-mix(in srgb, var(--color-text, #222) 6%, transparent);
  font-size: 0.75rem;
  white-space: pre-wrap;
}

.dismiss-fp-btn {
  margin-top: 6px;
}

.illegal-qty-adjusted {
  color: var(--warning-color, #b8860b);
  font-size: 0.85rem;
}

.qty-adjust-row {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.35rem;
  margin-top: 0.35rem;
}

.qty-adjust-input {
  width: 4.5rem;
  padding: 0.15rem 0.35rem;
  font-size: 0.85rem;
}

.brd-qty-adjust {
  margin: 0.5rem 0 0.75rem;
  flex-direction: column;
  align-items: flex-start;
}

.brd-market-prov {
  margin: 0.5rem 0 0.75rem;
  padding: 0.65rem 0.75rem;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: color-mix(in srgb, var(--color-primary, #2080f0) 6%, transparent);
}

.brd-market-plan {
  margin-top: 0.35rem;
  color: var(--warning-color, #b8860b);
}

.market-undo-plan {
  color: var(--warning-color, #b8860b);
  font-size: 0.85rem;
  margin-bottom: 0.15rem;
}

.source-trace-note {
  display: block;
  margin-top: 0.2rem;
  line-height: 1.45;
  color: var(--warning-color, #b8860b);
}

.text-corr-plan {
  color: var(--color-primary, #2080f0);
  font-size: 0.85rem;
  margin-bottom: 0.15rem;
}

.brd-text-correct {
  margin: 0.75rem 0;
  padding: 0.65rem 0.75rem;
  border: 1px dashed var(--color-border);
  border-radius: 6px;
}

.text-correct-form {
  display: flex;
  flex-direction: column;
  gap: 0.35rem;
  margin: 0.5rem 0;
}

.text-correct-name {
  width: 100%;
  max-width: 420px;
  padding: 0.25rem 0.4rem;
  font-size: 0.85rem;
}

.text-correct-desc {
  width: 100%;
  max-width: 520px;
  padding: 0.35rem 0.45rem;
  font-size: 0.85rem;
  resize: vertical;
}

.text-correct-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 0.35rem;
}

.item-catalog-modal {
  display: flex;
  flex-direction: column;
  overflow: hidden;
}

.item-catalog-modal .modal-body {
  flex: 1;
  min-height: 0;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  padding: 12px 16px 16px;
}

.item-catalog-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 0.5rem;
  margin-bottom: 0.75rem;
  flex-shrink: 0;
}

.catalog-sort-select {
  padding: 0.2rem 0.35rem;
  font-size: 0.85rem;
}

.catalog-summary {
  flex: 1 1 100%;
}

.item-catalog-table-wrap {
  max-width: 100%;
  flex: 1;
  min-height: 0;
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.item-catalog-table {
  width: 100%;
  table-layout: fixed;
}

.item-catalog-table th,
.item-catalog-table td {
  vertical-align: top;
  padding: 0.55rem 0.65rem;
  word-break: break-word;
}

.item-catalog-table .catalog-col-item { width: 14%; }
.item-catalog-table .catalog-col-player { width: 10%; }
.item-catalog-table .catalog-col-source { width: 24%; }
.item-catalog-table .catalog-col-change { width: 11%; }
.item-catalog-table .catalog-col-reclaim { width: 10%; }
.item-catalog-table .catalog-col-events { width: 31%; }

.item-catalog-table .source-line {
  max-width: none;
  white-space: pre-wrap;
}

.name-variants {
  margin-top: 0.15rem;
  line-height: 1.35;
}

.catalog-events summary {
  cursor: pointer;
  user-select: none;
}

.catalog-event-list {
  margin: 0.35rem 0 0;
  padding-left: 1.1rem;
  font-size: 0.85rem;
}

.catalog-event-list li + li {
  margin-top: 0.45rem;
}

.reclaim-status-tag {
  display: inline-block;
  padding: 0.1rem 0.4rem;
  border-radius: 4px;
  font-size: 0.8rem;
  white-space: nowrap;
}

.rs-preview_eligible {
  background: color-mix(in srgb, var(--warning-color, #b8860b) 18%, transparent);
  color: var(--warning-color, #b8860b);
}

.rs-detected_filtered {
  background: color-mix(in srgb, #888 15%, transparent);
  color: #888;
}

.rs-executed, .rs-executed_text {
  background: color-mix(in srgb, var(--color-success, #18a058) 15%, transparent);
  color: var(--color-success, #18a058);
}

.rs-dismissed {
  background: color-mix(in srgb, #888 12%, transparent);
  color: #666;
}

.rs-adjusted_qty, .rs-adjusted_text {
  background: color-mix(in srgb, var(--color-primary, #2080f0) 12%, transparent);
  color: var(--color-primary, #2080f0);
}

.rs-none {
  background: transparent;
  color: var(--color-text-muted, #999);
}

.bug-detail-btn {
  margin-top: 6px;
}

.appeal-link-row {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: 6px;
}

.appeal-detail-link-btn {
  white-space: nowrap;
}

.linkish-btn {
  margin-left: 6px;
  padding: 0;
  border: none;
  background: none;
  color: var(--color-primary, #2080f0);
  cursor: pointer;
  font-size: inherit;
  text-decoration: underline;
}

.bug-reclaim-detail-modal {
  max-width: 720px;
  max-height: 90vh;
}

.brd-summary {
  margin-bottom: 12px;
  padding: 10px 12px;
  border-radius: 8px;
  background: var(--color-surface-muted, rgba(0, 0, 0, 0.03));
  font-size: 13px;

  p { margin: 0 0 6px; }
}

.brd-verdict {
  margin-top: 8px !important;
  line-height: 1.5;
  color: var(--color-text-muted, #666);
}

.brd-reclaim-reason {
  margin-top: 8px !important;
  line-height: 1.55;
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(220, 53, 69, 0.06);
  border-left: 3px solid rgba(220, 53, 69, 0.45);
}

.reclaim-reason {
  margin-top: 4px;
  line-height: 1.45;
  max-width: 280px;
}

.brd-structure {
  line-height: 1.5;
}

.brd-appeal-hint {
  margin: 0 0 10px;
  line-height: 1.45;
}

.brd-appeal-groups {
  display: flex;
  flex-direction: column;
  gap: 12px;
  margin-bottom: 16px;
}

.brd-appeal-group {
  padding: 12px 14px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-surface);
}

.brd-appeal-fork {
  border-color: rgba(24, 160, 88, 0.4);
  background: rgba(24, 160, 88, 0.05);
}

.brd-appeal-illegal {
  border-color: rgba(208, 48, 80, 0.45);
  background: rgba(208, 48, 80, 0.06);
}

.brd-appeal-group-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  margin-bottom: 8px;
}

.brd-appeal-index {
  font-size: 12px;
  color: var(--color-text-muted, #888);
}

.brd-appeal-id {
  font-size: 15px;
}

.brd-player-summary {
  margin: 0 0 6px;
  font-size: 13px;
  font-weight: 500;
  line-height: 1.45;
}

.brd-system-verdict {
  margin: 0 0 8px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-muted, #777);
}

.brd-system-warn {
  color: #b03050;
}

.brd-group-details {
  margin-top: 6px;
  font-size: 12px;

  summary {
    cursor: pointer;
    color: var(--color-text-muted, #888);
    margin-bottom: 6px;
  }
}

.brd-inner-step {
  margin: 8px 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--color-surface-muted, rgba(0, 0, 0, 0.03));
}

.brd-qty-unchanged {
  color: #18a058;
  font-weight: 500;
}

.brd-flat-steps {
  margin-bottom: 12px;
  font-size: 13px;
}

.brd-step-list {
  margin: 0 0 16px;
  padding-left: 1.2em;
}

.brd-step {
  margin-bottom: 12px;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  list-style: decimal;
  font-size: 13px;
}

.brd-step-seed {
  border-color: rgba(24, 160, 88, 0.35);
  background: rgba(24, 160, 88, 0.05);
}

.brd-step-illegal {
  border-color: rgba(208, 48, 80, 0.35);
  background: rgba(208, 48, 80, 0.06);
}

.brd-step-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.brd-step-appeal {
  font-weight: 600;
}

.brd-step-badge {
  padding: 1px 8px;
  border-radius: 4px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  font-size: 12px;
}

.brd-step-action {
  font-size: 12px;
  color: var(--color-text-muted, #888);
}

.brd-step-summary {
  margin: 0 0 6px;
  font-weight: 500;
  line-height: 1.45;
}

.brd-step-fields {
  margin: 6px 0;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--color-surface);
  font-size: 12px;
  line-height: 1.5;
}

.brd-desc-block {
  margin-top: 6px;
  word-break: break-word;
  white-space: pre-wrap;
}

.brd-step-note {
  margin: 4px 0 0;
}

.brd-grant-list {
  margin: 0 0 16px;
  padding-left: 1.2em;
  font-size: 13px;

  li { margin-bottom: 8px; }
}

.brd-modal-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--color-border);
}

.item-detail-cell {
  min-width: 320px;
  max-width: 420px;
}

.item-detail-grid {
  display: grid;
  gap: 6px;
}

.detail-row {
  display: grid;
  grid-template-columns: 64px 1fr;
  gap: 8px;
  align-items: start;
  font-size: 12px;
  line-height: 1.45;
}

.detail-label {
  color: var(--color-text-muted, #888);
  flex-shrink: 0;
}

.detail-text {
  word-break: break-word;
  white-space: pre-wrap;
}

.stack-detail {
  margin-top: 8px;
}

.route-list {
  margin: 6px 0 0;
  padding-left: 0;
  list-style: none;

  li {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-bottom: 4px;
  }
}

.lineage-block {
  margin-top: 8px;
}

.route-detail {
  margin: 0;
}

.appeal-timeline-row td {
  padding: 0 8px 10px !important;
  background: var(--color-surface-muted, rgba(0, 0, 0, 0.02));
  border-top: none !important;
}

.appeal-timeline-wrap {
  margin: 4px 0 2px;
}

.appeal-timeline-head {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 8px 12px;
  margin-bottom: 6px;
  font-size: 12px;
}

.appeal-timeline-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 12px;

  th, td {
    border: 1px solid var(--color-border);
    padding: 5px 7px;
    text-align: left;
    vertical-align: top;
  }

  th {
    background: var(--color-surface);
    font-weight: 600;
  }

  tr.tl-seed td {
    background: rgba(24, 160, 88, 0.06);
  }

  tr.tl-illegal td {
    background: rgba(208, 48, 80, 0.1);
  }
}

.tl-desc-cell {
  max-width: 280px;
  word-break: break-word;
  white-space: pre-wrap;
  line-height: 1.4;
}

.tl-illegal-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 4px;
  background: rgba(208, 48, 80, 0.15);
  color: #d03050;
  font-weight: 600;
}

.tl-note {
  margin-top: 2px;
  line-height: 1.35;
}

.bug-reclaim-history {
  margin-top: 10px;
}

.rs-reverted {
  background: rgba(138, 109, 59, 0.12);
  color: #8a6d3b;
}

.bug-reclaim-history-table-wrap {
  margin-top: 10px;
  overflow-x: auto;
  border: 1px solid var(--color-border, #d8d8d8);
  border-radius: 8px;
  background: var(--color-surface, #fff);
}

.bug-reclaim-history-table {
  min-width: 920px;
}

.bug-reclaim-history-pager {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: center;
  gap: 10px;
  margin-top: 10px;
  color: var(--color-text-muted, #666);
  font-size: 12px;
}

.bug-reclaim-history-overlay {
  position: fixed;
  inset: 0;
  z-index: 2200;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.58);
}

.bug-reclaim-history-modal {
  width: min(820px, calc(100vw - 32px));
  max-width: 820px;
  max-height: 90vh;
  overflow: hidden;
  border: 1px solid var(--color-border, #d8d8d8);
  border-radius: 12px;
  background: var(--color-surface, #fff);
  color: var(--color-text, #222);
  box-shadow: 0 20px 60px rgba(0, 0, 0, 0.35);
}

.bug-reclaim-history-modal .modal-body {
  max-height: calc(90vh - 64px);
  overflow-y: auto;
}

.history-detail-summary {
  padding: 10px 12px;
  border: 1px solid var(--color-border, #d8d8d8);
  border-radius: 8px;
  background: var(--color-surface-muted, #f6f7f9);

  p {
    margin: 0 0 6px;
    overflow-wrap: anywhere;
  }

  p:last-child {
    margin-bottom: 0;
  }
}

.history-json-block {
  margin-top: 12px;
  border: 1px solid var(--color-border, #d8d8d8);
  border-radius: 8px;
  background: var(--color-surface, #fff);

  summary {
    padding: 9px 11px;
    cursor: pointer;
    font-weight: 600;
  }

  pre {
    max-height: 320px;
    margin: 0;
    padding: 10px 12px;
    overflow: auto;
    border-top: 1px solid var(--color-border, #d8d8d8);
    background: var(--color-surface-muted, #f6f7f9);
    color: var(--color-text, #222);
    font-size: 12px;
    line-height: 1.5;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
}

.appeal-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;

  th, td {
    border: 1px solid var(--color-border);
    padding: 6px 8px;
    text-align: left;
    overflow-wrap: anywhere;
  }
}

.ops-cell {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}

.st-awaiting_review { color: #f0a020; }
.st-approved { color: #18a058; }
.st-rejected { color: #d03050; }
.st-processing { color: #2080f0; }
.st-reverted { color: #8a6d3b; }
.reverted-tag { color: #8a6d3b; font-weight: 600; }

.empty { padding: 24px; text-align: center; color: var(--color-text-secondary); }

.evidence-block {
  margin: 12px 0;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.evidence-quote {
  margin: 8px 0 0;
  padding: 8px 10px;
  border-left: 3px solid var(--color-border);
  color: var(--color-text);
  font-size: 13px;
  line-height: 1.5;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}

.member-update-raw pre {
  font-size: 11px;
  max-height: 160px;
  overflow: auto;
  background: var(--color-surface);
  padding: 8px;
  border-radius: 4px;
}

.change-list {
  margin: 0;
  padding-left: 18px;
  font-size: 13px;
  line-height: 1.8;

  .before { color: #d03050; word-break: break-all; }
  .after { color: #18a058; word-break: break-all; }
  .chg-action { font-weight: 600; margin-right: 4px; }
}

.audit-log-section {
  margin-top: 12px;
}

.audit-log-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-top: 10px;
}

.audit-log-card {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  overflow: hidden;

  &.audit-op-delete {
    border-left: 3px solid #d03050;
  }

  &.audit-op-update {
    border-left: 3px solid #2080f0;
  }

  &.audit-op-create_item,
  &.audit-op-create_trait,
  &.audit-op-create_skill,
  &.audit-op-currency {
    border-left: 3px solid #18a058;
  }
}

.audit-log-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  padding: 8px 10px;
  background: var(--color-bg);
  border-bottom: 1px solid var(--color-border);
  font-size: 13px;
}

.audit-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 600;
  background: var(--color-border);
  color: var(--color-text);

  &.badge-delete { background: rgba(208, 48, 80, 0.15); color: #d03050; }
  &.badge-update { background: rgba(32, 128, 240, 0.15); color: #2080f0; }
  &.badge-create_item,
  &.badge-create_trait,
  &.badge-create_skill,
  &.badge-currency { background: rgba(24, 160, 88, 0.15); color: #18a058; }
}

.audit-target-name {
  font-size: 13px;
}

.audit-log-body {
  padding: 10px 12px;
  font-size: 13px;
}

.audit-delete-hint {
  margin: 0 0 8px;
  color: #d03050;
  font-weight: 600;
}

.audit-create-hint {
  margin: 0 0 8px;
  color: #18a058;
  font-weight: 600;
}

.preview-change-list {
  list-style: none;
  padding-left: 0;
}

.preview-change-item {
  margin-bottom: 10px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
}

.preview-change-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.preview-change-title {
  font-size: 13px;
}

.preview-change-body {
  font-size: 13px;
  padding-left: 4px;
}

.audit-record-dl.compact {
  margin-top: 4px;
}

.audit-record-dl {
  margin: 0;
  display: grid;
  grid-template-columns: 72px 1fr;
  gap: 6px 10px;

  dt {
    margin: 0;
    color: var(--color-text-muted, #888);
    font-size: 12px;
  }

  dd {
    margin: 0;
    word-break: break-word;
  }
}

.audit-diff-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;

  li {
    display: flex;
    flex-wrap: wrap;
    align-items: flex-start;
    gap: 6px;
  }
}

.audit-field-name {
  flex: 0 0 100%;
  font-weight: 600;
  font-size: 12px;
  color: var(--color-text-muted, #888);
}

.audit-diff-col {
  flex: 1 1 100%;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.diff-label {
  font-size: 11px;
  font-weight: 600;

  &.before-label { color: #d03050; }
  &.after-label { color: #18a058; }
}

.diff-arrow {
  color: var(--color-text-muted, #888);
  font-weight: 600;
}

.inline-val.before { color: #d03050; word-break: break-all; }
.inline-val.after { color: #18a058; word-break: break-all; }

.audit-text-block {
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 12px;
  line-height: 1.55;
  padding: 8px;
  border-radius: 6px;
  max-height: 200px;
  overflow: auto;

  &.before {
    background: rgba(208, 48, 80, 0.08);
    border: 1px solid rgba(208, 48, 80, 0.2);
    color: #d03050;
  }

  &.after {
    background: rgba(24, 160, 88, 0.08);
    border: 1px solid rgba(24, 160, 88, 0.2);
    color: #18a058;
  }
}

.audit-log-foot {
  padding: 6px 12px 10px;
  border-top: 1px dashed var(--color-border);
}

.patch-raw pre {
  font-size: 11px;
  max-height: 200px;
  overflow: auto;
  background: var(--color-bg);
  padding: 8px;
  border-radius: 4px;
}

.review-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 16px;
  align-items: center;
}

.reject-input {
  flex: 1;
  min-width: 160px;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg);
  color: var(--color-text);
}

.detail-modal .modal-body p { margin: 0 0 8px; font-size: 13px; }

.processing-hint {
  padding: 8px 10px;
  border-radius: 6px;
  background: rgba(32, 128, 240, 0.12);
  color: var(--color-text);
  font-size: 13px;
}

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

.appeal-detail-overlay {
  z-index: 1200;
}

.modal-content {
  box-sizing: border-box;
  min-width: 0;
  width: 100%;
  max-width: 640px;
  max-height: 88vh;
  overflow-y: auto;
  background: var(--color-surface, var(--color-bg));
  border: 1px solid var(--color-border);
  border-radius: 12px;
}

.modal-lg {
  max-width: 720px;
}

.modal-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 16px 20px;
  border-bottom: 1px solid var(--color-border);

  h5 {
    min-width: 0;
    margin: 0;
    font-size: 16px;
    overflow-wrap: anywhere;
  }
}

.modal-close {
  background: none;
  border: none;
  font-size: 20px;
  cursor: pointer;
  color: var(--color-text-secondary, #888);
}

.modal-body {
  min-width: 0;
  padding: 16px 20px 20px;
}

.modal-overlay.item-catalog-overlay {
  padding: 6px;
  align-items: stretch;
}

.modal-content.item-catalog-modal {
  width: calc(100vw - 12px);
  max-width: calc(100vw - 12px);
  height: calc(100vh - 12px);
  max-height: calc(100vh - 12px);
  overflow: hidden;
}

@media (max-width: 720px) {
  .auto-mode-bar,
  .revert-offer-head,
  .bug-reclaim-head {
    align-items: stretch;
    flex-direction: column;
  }

  .auto-mode-actions,
  .revert-offer-actions,
  .bug-reclaim-actions {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    width: 100%;
    min-width: 0;
  }

  .filter-bar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .appeal-search-input {
    grid-column: 1 / -1;
    box-sizing: border-box;
    width: 100%;
    min-width: 0;
    max-width: none;
  }

  .primary-appeal-table,
  .primary-appeal-table tbody,
  .primary-appeal-table tr,
  .primary-appeal-table td {
    display: block;
    width: 100%;
  }

  .primary-appeal-table {
    border-collapse: separate;
  }

  .primary-appeal-table thead { display: none; }

  .primary-appeal-table tbody {
    display: grid;
    gap: 10px;
  }

  .primary-appeal-table tr {
    box-sizing: border-box;
    overflow: hidden;
    border: 1px solid var(--color-border);
    border-radius: 10px;
    background: var(--color-bg);
  }

  .primary-appeal-table td {
    box-sizing: border-box;
    display: grid;
    grid-template-columns: 76px minmax(0, 1fr);
    gap: 10px;
    border: 0;
    border-bottom: 1px solid var(--color-border);
    padding: 8px 10px;
  }

  .primary-appeal-table td::before {
    content: attr(data-label);
    color: var(--color-text-secondary);
    font-size: 11px;
    font-weight: 600;
  }

  .primary-appeal-table td:last-child { border-bottom: 0; }

  .primary-appeal-table .ops-cell {
    display: grid;
    grid-template-columns: 76px repeat(2, minmax(0, 1fr));
  }

  .primary-appeal-table .ops-cell::before { grid-row: 1 / span 2; }

  .primary-appeal-table .ops-cell button {
    min-width: 0;
    min-height: 36px;
    white-space: normal;
  }

  .revert-offer-table-wrap,
  .bug-reclaim-table-wrap,
  .item-catalog-table-wrap,
  .appeal-timeline-wrap {
    width: 100%;
    max-width: 100%;
    overscroll-behavior-inline: contain;
  }

  .modal-overlay,
  .modal-overlay.item-catalog-overlay {
    align-items: stretch;
    padding: 0;
  }

  .modal-content,
  .modal-lg,
  .modal-content.item-catalog-modal {
    width: 100%;
    max-width: none;
    height: auto;
    max-height: 100dvh;
    border-radius: 0;
  }

  .modal-header { padding: 12px 14px; }
  .modal-body { padding: 12px 14px 18px; }

  .detail-modal .modal-body,
  .detail-modal .modal-body > *,
  .evidence-block,
  .review-ai-block,
  .audit-log-card,
  .audit-log-body,
  .preview-change-item {
    box-sizing: border-box;
    min-width: 0;
    max-width: 100%;
  }

  .detail-modal p,
  .audit-log-card,
  .preview-change-body,
  .audit-text-block {
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .member-update-raw pre,
  .patch-raw pre {
    box-sizing: border-box;
    max-width: 100%;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    word-break: break-word;
  }

  .audit-record-dl {
    grid-template-columns: 68px minmax(0, 1fr);
    gap: 6px 8px;
  }

  .audit-record-dl dd,
  .audit-diff-list li > * { min-width: 0; }

  .review-actions {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
  }

  .review-actions > *,
  .reject-input {
    box-sizing: border-box;
    width: 100%;
    min-width: 0;
    min-height: 40px;
  }
}
</style>
