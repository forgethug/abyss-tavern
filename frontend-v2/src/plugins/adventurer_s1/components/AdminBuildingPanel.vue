<template>
  <section class="advs1-card">
    <button type="button" class="btn-toggle" @click="showLimitedShops = !showLimitedShops">🥮 限时商店管理</button>
    <AdminLimitedShopPanel v-if="showLimitedShops" />
    <div class="panel-header">
      <h3 class="col-title">🏗️ 建筑模板管理</h3>
      <div style="display: flex; gap: 8px; flex-wrap: wrap;">
        <button type="button" class="btn-toggle" :disabled="reconciling" @click="reconcileContribution">
          {{ reconciling ? '校准中…' : '🎯 一键校准贡献度' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="repairingCodenames" @click="repairCodenames">
          {{ repairingCodenames ? '修复中…' : '🔧 修复传记代号' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="restoringCodenames" @click="restoreCodenames">
          {{ restoringCodenames ? '还原中…' : '↩️ 还原传记' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="renamingDungeons" @click="renameDungeons">
          {{ renamingDungeons ? '迁移中…' : '🗺️ 迁移高阶副本名' }}
        </button>
        <button type="button" class="advs1-submit" @click="openCreate">+ 新建模板</button>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">🎲 赌坊金币对账校正</div>
      <div class="crb-hint">
        按每轮最终骰子结果、下注金额与金币流水重新计算实际净收益，只追缴超额派彩；已写入的追缴流水会参与下次对账，因此可重复预览且不会重复扣款。全额追缴允许金币变为负数。
      </div>
      <div class="crb-row">
        <input v-model.number="gamblingReconciliationAdvId" type="number" class="crb-input" placeholder="冒险者 ID（可选，留空=全量）" @input="gamblingReconciliationPreview = null" />
        <button type="button" class="btn-toggle" :disabled="gamblingReconciliationPreviewing" @click="doGamblingReconciliationPreview">
          {{ gamblingReconciliationPreviewing ? '扫描中…' : '🔍 预览赌坊账本' }}
        </button>
        <button
          type="button"
          class="btn-toggle"
          :disabled="gamblingReconciliationExecuting || !gamblingReconciliationPreview?.candidate_count || !gamblingReconciliationPreview?.safe_to_execute || !gamblingReconciliationPreview?.snapshot_token"
          @click="doGamblingReconciliationExecute"
        >
          {{ gamblingReconciliationExecuting ? '校正中…' : '💰 执行金币校正' }}
        </button>
      </div>
      <div v-if="gamblingReconciliationPreview" class="crb-info">
        <div class="crb-line">
          异常轮次 <b>{{ gamblingReconciliationPreview.candidate_count }}</b> 条 · 冒险者
          <b>{{ gamblingReconciliationPreview.adventurer_count }}</b> 名 · 待追缴
          <b>{{ gamblingReconciliationPreview.total_overpay }}</b> 金币
        </div>
        <div v-if="gamblingReconciliationPreview.unsafe_candidate_count" class="crb-hint crb-none">
          有 {{ gamblingReconciliationPreview.unsafe_candidate_count }} 条候选的标准下注流水不是恰好 1 条
          、扣款金额不等于下注金额，或最终轮次状态非法
          （缺失 {{ gamblingReconciliationPreview.missing_bet_rows }}、重复 {{ gamblingReconciliationPreview.duplicate_bet_rows }}、金额不符 {{ gamblingReconciliationPreview.bet_amount_mismatch_rows }}、状态非法 {{ gamblingReconciliationPreview.invalid_round_rows }}），为防误扣已禁止整批执行。可按冒险者 ID 缩小范围后重新核查。
        </div>
        <div v-else-if="gamblingReconciliationPreview.candidate_count && !gamblingReconciliationPreview.snapshot_token" class="crb-hint">
          当前共享缓存不可用，本次结果仅供预览；恢复缓存后重新预览才能执行。
        </div>
        <div v-if="gamblingReconciliationPreview.items?.length" class="crb-scroll-list">
          <div v-for="row in gamblingReconciliationPreview.items" :key="`${row.round_id}:${row.adventurer_id}`" class="crb-scroll-row">
            轮次 #{{ row.round_id }} · <b>{{ row.adventurer_name }}</b>（ID {{ row.adventurer_id }}）
            · 开奖{{ row.round_outcome === 'high' ? '大' : row.round_outcome === 'low' ? '小' : row.round_outcome }}
            / 押{{ row.choice === 'high' ? '大' : '小' }} {{ row.bet_amount }} 金
            · 应有净额 {{ row.expected_net }} · 实际净额 {{ row.actual_net }} · 追缴 <b>{{ row.overpay }}</b>
            · 流水 投注{{ row.n_bet }}笔/{{ row.bet_net }}金 · 派彩{{ row.n_win }}/退款{{ row.n_refund }}/已追缴{{ row.n_clawback }}
            <span v-if="!row.safe_to_execute" class="crb-none"> · 需人工核查</span>
          </div>
        </div>
        <div v-else class="crb-hint">当前没有需要校正的赌坊超额派彩。</div>
        <div v-if="gamblingReconciliationPreview.items_truncated" class="crb-hint">明细仅显示前 100 条，执行仍以本次完整服务端快照为准。</div>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">🎟️ 特殊副本未扣门票追缴</div>
      <div class="crb-hint">按探险成员、目标副本和发车时的门票消耗流水精确核对。转职 1500、顿悟 1000、祛厄 2000 金币/人次；允许扣成负数，已追缴人次不会重复扣款。</div>
      <div class="crb-row">
        <button type="button" class="btn-toggle" :disabled="ticketPenaltyPreviewing" @click="doTicketPenaltyPreview">
          {{ ticketPenaltyPreviewing ? '扫描中…' : '🔍 预览未扣门票' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="ticketPenaltyExecuting || !ticketPenaltyPreview?.candidate_count" @click="doTicketPenaltyExecute">
          {{ ticketPenaltyExecuting ? '追缴中…' : '💰 一键追缴金币' }}
        </button>
      </div>
      <div v-if="ticketPenaltyPreview" class="crb-info">
        <div class="crb-line">待追缴 <b>{{ ticketPenaltyPreview.candidate_count }}</b> 人次 · 合计 <b>{{ ticketPenaltyPreview.total_penalty_gold }}</b> 金币</div>
        <div v-if="ticketPenaltyPreview.items?.length" class="crb-scroll-list">
          <div v-for="row in ticketPenaltyPreview.items" :key="`${row.expedition_log_id}:${row.adventurer_id}`" class="crb-scroll-row">
            日志 #{{ row.expedition_log_id }} · <b>{{ row.adventurer_name }}</b>（ID {{ row.adventurer_id }}）
            · {{ row.dungeon_name }} · 未扣 {{ row.ticket_item }} · 追缴 {{ row.penalty_gold }} 金币
            · 当前 {{ row.balance_before }}
          </div>
        </div>
        <div v-else class="crb-hint">没有尚未追缴的未扣门票人次。</div>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">🧾 历史探险物品漏扣追回</div>
      <div class="crb-hint">
        扫描 2026-06-18 后 legacy consumed 把整组消耗品只扣 1 件的问题。只有出发快照、旧结算后的剩余量和当前唯一实例完全闭合，且之后没有同名物品变动时才会列入；来源混淆项只计入跳过，不会自动扣。
      </div>
      <div class="crb-row">
        <input v-model.number="consumptionRepairLogId" type="number" class="crb-input" placeholder="探险日志 ID（可选）" />
        <input v-model.number="consumptionRepairAdvId" type="number" class="crb-input" placeholder="冒险者 ID（可选）" />
        <button type="button" class="btn-toggle" :disabled="consumptionRepairPreviewing" @click="doConsumptionRepairPreview">
          {{ consumptionRepairPreviewing ? '扫描中…' : '🔍 预览精确漏扣' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="consumptionRepairExecuting || !consumptionRepairPreview?.candidate_count || !consumptionRepairPreview?.snapshot_token" @click="doConsumptionRepairExecute">
          {{ consumptionRepairExecuting ? '追回中…' : '✅ 执行追回' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="consumptionRepairRestoring" @click="doConsumptionRepairRestore">
          {{ consumptionRepairRestoring ? '撤销中…' : '↩️ 撤销追回' }}
        </button>
      </div>
      <div v-if="consumptionRepairPreview" class="crb-info">
        <div class="crb-line">
          扫描 {{ consumptionRepairPreview.scanned_logs ?? 0 }} 条 · 精确候选
          <b>{{ consumptionRepairPreview.candidate_count ?? 0 }}</b> 条 · 待追回
          <b>{{ consumptionRepairPreview.reclaim_quantity ?? 0 }}</b> 件
          · 跳过不确定 {{ consumptionRepairPreview.ambiguous_count ?? 0 }} 条
          <span v-if="consumptionRepairPreview.scan_truncated"> · 仅扫描最近 5000 条</span>
        </div>
        <div v-if="!consumptionRepairPreview.snapshot_token" class="crb-hint">
          当前共享缓存不可用，本次结果仅供预览；恢复缓存后重新预览才能执行。
        </div>
        <div v-if="consumptionRepairPreview.items?.length" class="crb-scroll-list">
          <div v-for="row in consumptionRepairPreview.items" :key="`${row.expedition_log_id}:${row.adventurer_id}:${row.item_name}`" class="crb-scroll-row">
            日志 #{{ row.expedition_log_id }} · <b>{{ row.adventurer_name }}</b>（ID {{ row.adventurer_id }}）
            · {{ row.item_name }} · 出发 {{ row.snapshot_quantity }} → 旧结算剩 {{ row.current_quantity }}
            · 追回 {{ row.reclaim_quantity }}
          </div>
        </div>
        <div v-else class="crb-hint">当前没有能安全、精确自动追回的历史漏扣。</div>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">🩺 冒险者职业修复</div>
      <div class="crb-hint">转职 / 进阶成功但职业为空时定向补职业。输 ID 预览传记结尾 → 核对 → 写入；修复前自动备份可还原。</div>
      <div class="crb-row">
        <input v-model.number="classRepairId" type="number" class="crb-input" placeholder="冒险者 ID" />
        <button type="button" class="btn-toggle" :disabled="classRepairPreviewing" @click="doClassRepairPreview">
          {{ classRepairPreviewing ? '查询中…' : '🔍 预览' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="classRepairRestoring" @click="doClassRepairRestore">
          {{ classRepairRestoring ? '还原中…' : '↩️ 还原职业修复' }}
        </button>
      </div>
      <div v-if="classRepairInfo" class="crb-info">
        <div class="crb-line">
          <b>{{ classRepairInfo.name }}</b>（ID {{ classRepairInfo.id }}）· Lv{{ classRepairInfo.level }} · {{ classRepairInfo.status }}
          · 当前职业：<span :class="classRepairInfo.current_class ? 'crb-has' : 'crb-none'">{{ classRepairInfo.current_class || '无职业' }}</span>
        </div>
        <div class="crb-story-label">传记结尾（据此判断该补什么职业）：</div>
        <pre class="crb-story">{{ classRepairInfo.history_story_tail || '（无传记）' }}</pre>
        <div class="crb-row">
          <input v-model="classRepairNewClass" type="text" class="crb-input" placeholder="新职业名（2~7 字）" maxlength="7" />
          <button type="button" class="btn-toggle" :disabled="classRepairing" @click="doClassRepair">
            {{ classRepairing ? '写入中…' : '✅ 写入职业' }}
          </button>
        </div>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">📜 转职漏技能 · 补发卷轴</div>
      <div class="crb-hint">
        扫描已完成探险里转职/进阶但该次未获得技能，或已消耗「职业通用技能卷轴」但该次未获得技能的冒险者；转职/进阶漏技能仍要求当前未持有同名卷轴、未通过申诉补过同类技能/卷轴，卷轴消耗失败则只按同一次副本是否已申诉补过来去重。补发图鉴卷轴到仓库。可先全量预览，或输 ID 查单人。
      </div>
      <div class="crb-row">
        <input v-model.number="classSkillScrollAdvId" type="number" class="crb-input" placeholder="冒险者 ID（可选，留空=全量）" />
        <button type="button" class="btn-toggle" :disabled="classSkillScrollPreviewing" @click="doClassSkillScrollPreview">
          {{ classSkillScrollPreviewing ? '扫描中…' : '🔍 预览' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="classSkillScrollRepairing || !classSkillScrollPreview" @click="doClassSkillScrollRepair">
          {{ classSkillScrollRepairing ? '补发中…' : '✅ 批量补发卷轴' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="classSkillScrollRestoring" @click="doClassSkillScrollRestore">
          {{ classSkillScrollRestoring ? '撤销中…' : '↩️ 撤销补发' }}
        </button>
      </div>
      <div v-if="classSkillScrollPreview" class="crb-info">
        <div class="crb-line">
          曾转职/进阶 {{ classSkillScrollPreview.class_change_adventurers ?? 0 }} 名 ·
          消耗过卷轴 {{ classSkillScrollPreview.scroll_consumed_adventurers ?? 0 }} 名
          <span v-if="classSkillScrollPreview.scroll_failed_adventurers !== undefined">
            （失败 {{ classSkillScrollPreview.scroll_failed_adventurers }} 名）
          </span> ·
          待补发 <b>{{ classSkillScrollPreview.candidates ?? classSkillScrollPreview.items?.length ?? 0 }}</b> 名
          <span v-if="classSkillScrollPreview.already_repaired_count">
            · 已补发 {{ classSkillScrollPreview.already_repaired_count }} 名
          </span>
        </div>
        <div v-if="classSkillScrollPreview.items?.length" class="crb-scroll-list">
          <div v-for="row in classSkillScrollPreview.items" :key="row.id" class="crb-scroll-row">
            <b>{{ row.name }}</b>（ID {{ row.id }}）· {{ row.current_class }}
            · Lv{{ row.level }} · {{ row.status }}
            <span v-if="row.last_class_change">
              · 最近 {{ row.last_class_change.old_class || '无' }} → {{ row.last_class_change.new_class }}
              <span v-if="row.last_class_change.log_id">（log #{{ row.last_class_change.log_id }}）</span>
            </span>
            <span v-if="row.recent_scroll_consumption">
              · 卷轴消耗 log #{{ row.recent_scroll_consumption.expedition_log_id }}
            </span>
            <span v-if="row.repair_reasons?.includes('scroll_consumed_without_skill')"> · 卷轴未学到技能</span>
          </div>
        </div>
        <div v-else class="crb-hint">当前没有符合条件的待补发冒险者。</div>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">🧩 探险技能漏发修复</div>
      <div class="crb-hint">
        对比已完成探险的 member_updates.new_skills 与当前技能表，仅补发结算已明确承诺但实际不存在的精确同名技能。支持按日志或冒险者筛选，重复执行不会重复发放。
      </div>
      <div class="crb-row">
        <input v-model.number="expeditionSkillLogId" type="number" class="crb-input" placeholder="探险日志 ID（可选）" />
        <input v-model.number="expeditionSkillAdvId" type="number" class="crb-input" placeholder="冒险者 ID（可选）" />
        <button type="button" class="btn-toggle" :disabled="expeditionSkillPreviewing" @click="doExpeditionSkillPreview">
          {{ expeditionSkillPreviewing ? '扫描中…' : '🔍 预览' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="expeditionSkillRepairing || !expeditionSkillPreview" @click="doExpeditionSkillRepair">
          {{ expeditionSkillRepairing ? '补发中…' : '✅ 补发漏发技能' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="expeditionSkillRestoring" @click="doExpeditionSkillRestore">
          {{ expeditionSkillRestoring ? '撤销中…' : '↩️ 撤销补发' }}
        </button>
      </div>
      <div v-if="expeditionSkillPreview" class="crb-info">
        <div class="crb-line">
          扫描日志 {{ expeditionSkillPreview.scanned_logs ?? 0 }} 条 · 待补发
          <b>{{ expeditionSkillPreview.candidate_count ?? 0 }}</b> 个技能
        </div>
        <div v-if="expeditionSkillPreview.items?.length" class="crb-scroll-list">
          <div v-for="row in expeditionSkillPreview.items" :key="`${row.adventurer_id}:${row.skill_name}`" class="crb-scroll-row">
            <b>{{ row.adventurer_name }}</b>（ID {{ row.adventurer_id }}）· {{ row.skill_name }}
            · 日志 #{{ row.expedition_log_ids?.join('、#') }}
          </div>
        </div>
        <div v-else class="crb-hint">当前没有结构化结算已承诺但未到账的技能。</div>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">📜 探险委托漏结算修复</div>
      <div class="crb-hint">
        按已完成探险日志中保存的结构化委托结果补写进度。只处理仍在 accepted、接取者归属一致且当次存活的委托；不会重置数据，也不会重复发奖。
      </div>
      <div class="crb-row">
        <input v-model.number="commissionSettlementLogId" type="number" class="crb-input" placeholder="探险日志 ID（必填）" />
        <button type="button" class="btn-toggle" :disabled="commissionSettlementPreviewing || !commissionSettlementLogId" @click="doCommissionSettlementPreview">
          {{ commissionSettlementPreviewing ? '查询中…' : '🔍 预览' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="commissionSettlementRepairing || !commissionSettlementPreview?.count" @click="doCommissionSettlementRepair">
          {{ commissionSettlementRepairing ? '补结算中…' : '✅ 补结算委托' }}
        </button>
      </div>
      <div v-if="commissionSettlementPreview" class="crb-info">
        <div class="crb-line">日志 #{{ commissionSettlementPreview.expedition_log_id }} · 待补结算 <b>{{ commissionSettlementPreview.count }}</b> 个委托</div>
        <div v-if="commissionSettlementPreview.candidates?.length" class="crb-scroll-list">
          <div v-for="row in commissionSettlementPreview.candidates" :key="row.commission_id" class="crb-scroll-row">
            <b>#{{ row.commission_id }} {{ row.title }}</b> · 冒险者 {{ row.adventurer_id }} · {{ row.progress_count }}/{{ row.required_count }}
          </div>
        </div>
        <div v-else class="crb-hint">该日志没有需要补结算的委托。</div>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">🔄 占位入门技能 · 回收换卷轴</div>
      <div class="crb-hint">
        早期通用技能卷轴兜底曾写入大量「xxx入门」敷衍技能。扫描技能名以「入门」结尾的占位技能（排除百艺阁旧卷传艺列表），向玩家推送确认：同意则移除这些技能，并按数量补发「职业通用技能卷轴」到仓库。
      </div>
      <div class="crb-row">
        <input v-model.number="placeholderRecallAdvId" type="number" class="crb-input" placeholder="冒险者 ID（可选，留空=全量）" />
        <button type="button" class="btn-toggle" :disabled="placeholderRecallPreviewing" @click="doPlaceholderRecallPreview">
          {{ placeholderRecallPreviewing ? '扫描中…' : '🔍 预览' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="placeholderRecallSending || !placeholderRecallPreview" @click="doPlaceholderRecallSend">
          {{ placeholderRecallSending ? '推送中…' : '✉️ 批量推送确认' }}
        </button>
      </div>
      <div v-if="placeholderRecallPreview" class="crb-info">
        <div class="crb-line">
          规则：{{ placeholderRecallPreview.suffix_rule }}
        </div>
        <div class="crb-line">
          持有占位技能 <b>{{ placeholderRecallPreview.total_adventurers ?? 0 }}</b> 名 ·
          可推送 <b>{{ placeholderRecallPreview.sendable_count ?? 0 }}</b> 名 ·
          待确认 <b>{{ placeholderRecallPreview.pending_count ?? 0 }}</b> 名
        </div>
        <div v-if="placeholderRecallPreview.skill_name_histogram?.length" class="crb-line">
          技能分布：
          <span v-for="(row, idx) in placeholderRecallPreview.skill_name_histogram" :key="row.name">
            {{ row.name }}×{{ row.count }}<span v-if="Number(idx) < placeholderRecallPreview.skill_name_histogram.length - 1">、</span>
          </span>
        </div>
        <div v-if="placeholderRecallPreview.items?.length" class="crb-scroll-list">
          <div v-for="row in placeholderRecallPreview.items" :key="row.adventurer_id" class="crb-scroll-row">
            <b>{{ row.adventurer_name }}</b>（ID {{ row.adventurer_id }}）· {{ row.character_class || '无职业' }}
            · 占位技能：{{ row.placeholder_skills?.map((s: any) => s.name).join('、') }}
            · 换 {{ row.scroll_grant_count }} 卷轴
            <span v-if="row.offer_status === 'pending'"> · 待确认中</span>
            <span v-else-if="row.can_send"> · 可推送</span>
            <span v-else> · {{ row.skip_reason }}</span>
          </div>
        </div>
        <div v-else class="crb-hint">当前没有符合条件的占位入门技能。</div>
      </div>
    </div>

    <div class="crb-box">
      <div class="crb-title">🧹 Lv2 锁定建筑修复</div>
      <div class="crb-hint">酒馆未升 Lv2 时，铁矿山 / 熔铁坊 / 深渊拾荒队 / 线索追查处等 Lv2 锁定建筑不该有实例。无主残留（无帮工、无捐献）可一键清理；有误捐记录的实例（含已开工 building 态）需先预览再退款锁定（退还材料 + 扣回贡献度 + 释放帮工并清除帮工时长 + 删除实例，恢复未解锁态）。</div>
      <div class="crb-row">
        <button type="button" class="btn-toggle" :disabled="orphanPreviewing" @click="doOrphanPreview">
          {{ orphanPreviewing ? '查询中…' : '🔍 预览无主残留' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="orphanPurging || !orphanInfo || orphanInfo.count === 0" @click="doOrphanPurge">
          {{ orphanPurging ? '清理中…' : '🧹 清理无主残留' }}
        </button>
        <button type="button" class="btn-toggle" :disabled="lv2DonationPreviewing" @click="doLv2DonationPreview">
          {{ lv2DonationPreviewing ? '查询中…' : '🔍 预览误捐实例' }}
        </button>
      </div>
      <div v-if="orphanInfo" class="crb-info">
        <div class="crb-line">无主残留 · 酒馆 Lv{{ orphanInfo.tavern_level }} · 可清理 <b>{{ orphanInfo.count }}</b> 个</div>
        <div v-if="orphanInfo.count > 0">
          <div v-for="o in orphanInfo.orphans" :key="o.id" class="crb-line">
            #{{ o.id }} · {{ o.name }}（{{ o.code }}）· {{ o.status }}
          </div>
        </div>
        <div v-else class="crb-line crb-none">无无主残留</div>
      </div>
      <div v-if="lv2DonationInfo" class="crb-info">
        <div class="crb-line">误捐实例 · 酒馆 Lv{{ lv2DonationInfo.tavern_level }} · 待退款 <b>{{ lv2DonationInfo.count }}</b> 个</div>
        <div v-if="lv2DonationInfo.count > 0">
          <div v-for="item in lv2DonationInfo.instances" :key="item.id" class="crb-line crb-row-inline">
            <span>#{{ item.id }} · {{ item.name }}（{{ item.code }}）· {{ item.status }} · 捐献 {{ item.donation_count }} 笔</span>
            <button
              type="button"
              class="btn-reject btn-sm"
              :disabled="lv2DonationRefundingId === item.id"
              @click="doLv2DonationRefund(item)"
            >
              {{ lv2DonationRefundingId === item.id ? '退款中…' : '💸 退款并锁定' }}
            </button>
          </div>
        </div>
        <div v-else class="crb-line crb-none">无误捐实例</div>
      </div>
    </div>

    <div v-if="store.buildingTemplatesLoading" class="advs1-state">加载中…</div>
    <div v-else-if="store.buildingTemplates.length === 0" class="advs1-state">暂无建筑模板</div>
    <div v-else class="template-list">
      <div v-for="tpl in store.buildingTemplates" :key="tpl.id" class="template-card">
        <div class="template-head">
          <span class="template-icon">{{ tpl.icon }}</span>
          <span class="template-name">{{ tpl.name }}</span>
          <span class="template-type">{{ tpl.building_type === 'unique' ? '唯一' : '可多个' }}</span>
          <span class="template-category">{{ categoryLabel(tpl.category) }}</span>
          <span v-if="tpl.handler_key" class="template-handler">{{ handlerName(tpl.handler_key) }}</span>
          <span :class="['template-status', tpl.is_active ? 'active' : 'inactive']">
            {{ tpl.is_active ? '启用' : '下架' }}
          </span>
        </div>
        <div class="template-desc">{{ tpl.description }}</div>
        <div class="template-meta">
          <span>⏱️ {{ tpl.build_hours }}h</span>
          <span>👥 {{ tpl.helper_slots }}位</span>
          <span>💪 {{ tpl.stamina_cost_per_help }}精力/h</span>
          <span>⏩ -1h/h</span>
          <span>🔢 {{ tpl.sort_order }}</span>
        </div>
        <div class="template-actions">
          <button type="button" class="btn-edit" @click="openEdit(tpl)">编辑</button>
          <button type="button" class="btn-toggle" @click="toggleActive(tpl)">
            {{ tpl.is_active ? '下架' : '启用' }}
          </button>
          <button v-if="tpl.building_type === 'multiple'" type="button" class="btn-add" @click="createInstance(tpl)">
            + 新建实例
          </button>
          <button type="button" class="btn-reject" @click="confirmDelete(tpl)">删除</button>
        </div>
      </div>
    </div>

    <!-- 建筑实例管理（含已禁用） -->
    <div class="instance-section">
      <div class="panel-header">
        <h3 class="col-title">🏠 建筑实例管理</h3>
        <button type="button" class="btn-toggle" @click="loadInstances">
          {{ instancesLoading ? '加载中…' : '刷新列表' }}
        </button>
      </div>
      <div v-if="instancesLoading" class="advs1-state">加载中…</div>
      <div v-else-if="allInstances.length === 0" class="advs1-state">暂无建筑实例</div>
      <div v-else class="instance-list">
        <div
          v-for="inst in allInstances"
          :key="inst.id"
          :class="['instance-card', { 'instance-disabled': inst.disabled }]"
        >
          <div class="instance-head">
            <span>{{ inst.template?.icon }}</span>
            <span class="instance-name">{{ inst.custom_name || inst.template?.name || `实例 #${inst.id}` }}</span>
            <span :class="['instance-status', 'status-' + inst.status]">{{ statusLabel(inst) }}</span>
            <span v-if="inst.disabled" class="instance-disabled-tag">🚫 已禁用</span>
          </div>
          <div class="instance-actions">
            <button
              type="button"
              :class="inst.disabled ? 'btn-add' : 'btn-reject'"
              :disabled="togglingId === inst.id"
              @click="doToggleDisable(inst)"
            >
              {{ togglingId === inst.id ? '处理中…' : (inst.disabled ? '✅ 启用' : '🚫 禁用') }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- 表单弹窗 -->
    <div v-if="showForm" class="modal-mask s1-mobile-surface s1-mobile-overlay" @click.self="showForm = false">
      <div class="modal-box modal-form s1-mobile-dialog">
        <h4 class="modal-title">{{ editingTemplate ? '编辑建筑模板' : '新建建筑模板' }}</h4>
        <div class="form-grid">
          <label class="form-label">
            名称
            <input v-model="form.name" class="advs1-input" maxlength="50" placeholder="建筑名称" />
          </label>
          <label class="form-label">
            图标
            <input v-model="form.icon" class="advs1-input" maxlength="10" placeholder="emoji 图标" />
          </label>
          <label class="form-label full-width">
            描述
            <textarea v-model="form.description" class="advs1-input" rows="2" placeholder="建筑描述"></textarea>
          </label>
          <label class="form-label">
            建筑类型
            <select v-model="form.building_type" class="advs1-input">
              <option value="unique">唯一建筑</option>
              <option value="multiple">可多个</option>
            </select>
          </label>
          <label class="form-label">
            分类
            <select v-model="form.category" class="advs1-input">
              <option value="general">通用</option>
              <option value="dormitory">宿舍区</option>
              <option value="medical">治疗区</option>
              <option value="equipment">装备区</option>
              <option value="gathering">采集区</option>
              <option value="training">修炼区</option>
              <option value="memorial">纪念区</option>
              <option value="leisure">休闲区</option>
              <option value="work">打工区</option>
              <option value="task">任务区</option>
            </select>
          </label>
          <label class="form-label">
            处理器
            <select v-model="form.handler_key" class="advs1-input">
              <option value="">无（用分类默认/结算配置）</option>
              <option v-for="h in availableHandlers" :key="h.key" :value="h.key">
                {{ h.name }} — {{ h.description }}
              </option>
            </select>
          </label>
          <label class="form-label">
            建造时间（小时）
            <input v-model.number="form.build_hours" type="number" min="1" class="advs1-input" />
          </label>
          <label class="form-label">
            帮忙位数
            <input v-model.number="form.helper_slots" type="number" min="1" class="advs1-input" />
          </label>
          <label class="form-label">
            每小时精力消耗
            <input v-model.number="form.stamina_cost_per_help" type="number" min="1" class="advs1-input" />
          </label>
          <label class="form-label">
            排序权重
            <input v-model.number="form.sort_order" type="number" class="advs1-input" />
          </label>
        </div>
        <div class="modal-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-cancel" @click="showForm = false">取消</button>
          <button type="button" class="advs1-submit" :disabled="submitting" @click="submitForm">
            {{ submitting ? '提交中…' : (editingTemplate ? '保存' : '创建') }}
          </button>
        </div>
      </div>
    </div>

    <!-- 删除确认 -->
    <div v-if="deleteTarget" class="modal-mask s1-mobile-surface s1-mobile-overlay" @click.self="deleteTarget = null">
      <div class="modal-box s1-mobile-dialog">
        <h4 class="modal-title">确认删除</h4>
        <p>确定要删除建筑模板「{{ deleteTarget.icon }} {{ deleteTarget.name }}」吗？</p>
        <p class="warn-text">此操作将同时删除对应的建筑实例和所有帮忙记录，且不可恢复。</p>
        <div class="modal-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-cancel" @click="deleteTarget = null">取消</button>
          <button type="button" class="btn-reject" :disabled="deleting" @click="doDelete">
            {{ deleting ? '删除中…' : '确认删除' }}
          </button>
        </div>
      </div>
    </div>
  </section>
</template>

<script setup lang="ts">
import AdminLimitedShopPanel from './AdminLimitedShopPanel.vue'
import { ref, reactive, onMounted } from 'vue'
import { useAdventurerS1Store, type BuildingTemplate, type BuildingTemplateFormData, type BuildingInstance } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import api from '@/composables/useApi'

const store = useAdventurerS1Store()
const showLimitedShops = ref(false)
const message = useGlobalMessage()

const showForm = ref(false)
const editingTemplate = ref<BuildingTemplate | null>(null)
const submitting = ref(false)
const deleteTarget = ref<BuildingTemplate | null>(null)
const allInstances = ref<BuildingInstance[]>([])
const instancesLoading = ref(false)
const togglingId = ref<number | null>(null)
const deleting = ref(false)

const availableHandlers = ref<Array<{key: string, name: string, description: string}>>([])

const defaultForm = (): BuildingTemplateFormData => ({
  name: '',
  icon: '🏗️',
  description: '',
  building_type: 'unique',
  category: 'general',
  build_hours: 4,
  helper_slots: 2,
  stamina_cost_per_help: 20,
  speedup_minutes: 15,
  sort_order: 0,
  handler_key: '',
})

const form = reactive<BuildingTemplateFormData>(defaultForm())

const reconciling = ref(false)
async function reconcileContribution() {
  if (reconciling.value) return
  if (!confirm('一键校准：把每名冒险者的贡献度精确对账到正确值——累计=建筑贡献榜应得，可消耗=应得−净消费（接委托扣、取消退还加）。该补补、该扣扣，可重复执行。确定吗？')) return
  reconciling.value = true
  try {
    const res = await store.adminReconcileContribution()
    if (res.code === 200) {
      const n = res.data?.contribution_net_change ?? 0
      const delta = n > 0 ? `净补 ${n}` : n < 0 ? `净扣 ${-n}` : '无需调整'
      message.success(`校准完成：${res.data?.adventurers_affected ?? 0} 名冒险者，可消耗贡献度${delta}`)
    } else {
      message.error(res.message || '校准失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '校准失败')
  } finally {
    reconciling.value = false
  }
}

const repairingCodenames = ref(false)
async function repairCodenames() {
  if (repairingCodenames.value) return
  // 先 dry-run 预览，把将修复 / 无法还原的数量展示给管理员再二次确认
  let preview: any
  try {
    const res = await store.adminPreviewRepairCodenames()
    if (res.code !== 200) { message.error(res.message || '预览失败'); return }
    preview = res.data
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败'); return
  }
  const willFix = preview?.will_fix ?? 0
  const residual = preview?.residual_after ?? 0
  if (willFix === 0) {
    message.info(residual > 0
      ? `没有可精确还原的代号；另有 ${residual} 名因对应探险日志已清理无法还原`
      : '传记中没有需要修复的残留代号')
    return
  }
  const residualNote = residual > 0
    ? `（另有 ${residual} 名因对应日志已清理无法还原，保持原样）`
    : ''
  if (!confirm(
    `将精确还原 ${willFix} 名冒险者传记里残留的探险代号（【M1】/冒险者甲…）为当次队友真名${residualNote}。\n\n`
    + '修复前会自动备份原文，出问题可点「↩️ 还原传记」一键撤销。确定吗？'
  )) return
  repairingCodenames.value = true
  try {
    const res = await store.adminRepairCodenames()
    if (res.code === 200) {
      const r = res.data?.residual_after ?? 0
      message.success(
        `修复完成：还原 ${res.data?.fixed ?? 0} 名，已备份原文`
        + (r > 0 ? `；仍残留 ${r} 名（日志已清理）` : '')
      )
    } else {
      message.error(res.message || '修复失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '修复失败')
  } finally {
    repairingCodenames.value = false
  }
}

const restoringCodenames = ref(false)
async function restoreCodenames() {
  if (restoringCodenames.value) return
  if (!confirm('从最近一次修复备份还原受影响冒险者的传记原文（撤销上次修复）。确定吗？')) return
  restoringCodenames.value = true
  try {
    const res = await store.adminRestoreCodenames()
    if (res.code === 200) {
      message.success(`已还原 ${res.data?.restored ?? 0} 名冒险者的传记`)
    } else {
      message.error(res.message || '还原失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '还原失败')
  } finally {
    restoringCodenames.value = false
  }
}

const renamingDungeons = ref(false)
async function renameDungeons() {
  if (renamingDungeons.value) return
  let preview: any
  try {
    const res = await store.adminPreviewDungeonRename()
    if (res.code !== 200) { message.error(res.message || '预览失败'); return }
    preview = res.data
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败'); return
  }
  const mappings = Object.entries(preview?.mappings || {})
    .map(([oldName, newName]) => `「${oldName}」→「${newName}」`)
    .join('、')
  const rows = preview?.total_rows ?? 0
  const replacements = preview?.total_replacements ?? 0
  const conflictText = preview?.has_conflicts
    ? `\n\n注意：检测到 ${preview.conflicts?.length || 0} 个唯一键冲突，冲突行会跳过，不会强行覆盖。`
    : ''
  if (replacements === 0) {
    message.info(`无需迁移：${mappings || '当前映射'} 没有命中旧数据`)
    return
  }
  if (!confirm(
    `将执行 S1 高阶副本名迁移：${mappings}。\n\n`
    + `预览命中 ${rows} 行，共 ${replacements} 处精确旧名。\n`
    + '只替换副本字段和结构化 JSON 里的精确旧名，不改历史剧情正文。'
    + conflictText
    + '\n\n确认执行吗？'
  )) return
  renamingDungeons.value = true
  try {
    const res = await store.adminExecuteDungeonRename()
    if (res.code === 200) {
      message.success(`迁移完成：替换 ${res.data?.total_replacements ?? 0} 处`)
    } else {
      message.error(res.message || '迁移失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '迁移失败')
  } finally {
    renamingDungeons.value = false
  }
}

// ── 赌坊金币对账校正 ──
const gamblingReconciliationAdvId = ref<number | null>(null)
const gamblingReconciliationPreview = ref<any>(null)
const gamblingReconciliationPreviewing = ref(false)
const gamblingReconciliationExecuting = ref(false)

async function doGamblingReconciliationPreview() {
  if (gamblingReconciliationPreviewing.value) return
  gamblingReconciliationPreviewing.value = true
  try {
    const res = await store.adminGamblingGoldReconciliationPreview(
      gamblingReconciliationAdvId.value ?? undefined,
    )
    if (res.code === 200) gamblingReconciliationPreview.value = res.data
    else message.error(res.message || '赌坊账本扫描失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '赌坊账本扫描失败')
  } finally {
    gamblingReconciliationPreviewing.value = false
  }
}

async function doGamblingReconciliationExecute() {
  const preview = gamblingReconciliationPreview.value
  if (
    !preview?.candidate_count
    || !preview?.safe_to_execute
    || !preview?.snapshot_token
    || gamblingReconciliationExecuting.value
  ) return
  if (!confirm(
    `将按本次完整快照校正 ${preview.candidate_count} 个赌坊轮次，`
    + `从 ${preview.adventurer_count} 名冒险者追回 ${preview.total_overpay} 金币。\n\n`
    + '这是全额追缴，余额不足时会扣成负数；执行前会重新锁定轮次并复核账本，发生变化则整批取消。确定执行吗？',
  )) return
  gamblingReconciliationExecuting.value = true
  try {
    const res = await store.adminGamblingGoldReconciliationExecute(String(preview.snapshot_token))
    if (res.code === 200) {
      message.success(
        `校正完成：${res.data?.executed_count ?? 0} 个轮次，`
        + `追回 ${res.data?.total_recovered ?? 0} 金币`,
      )
      await doGamblingReconciliationPreview()
    } else message.error(res.message || '赌坊金币校正失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '赌坊金币校正失败')
  } finally {
    gamblingReconciliationExecuting.value = false
  }
}

// ── 特殊副本未扣门票金币追缴 ──
const ticketPenaltyPreview = ref<any>(null)
const ticketPenaltyPreviewing = ref(false)
const ticketPenaltyExecuting = ref(false)

async function doTicketPenaltyPreview() {
  if (ticketPenaltyPreviewing.value) return
  ticketPenaltyPreviewing.value = true
  try {
    const res = await store.adminSpecialDungeonTicketPenaltiesPreview()
    if (res.code === 200) ticketPenaltyPreview.value = res.data
    else message.error(res.message || '扫描失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '扫描失败')
  } finally {
    ticketPenaltyPreviewing.value = false
  }
}

async function doTicketPenaltyExecute() {
  const preview = ticketPenaltyPreview.value
  if (!preview?.candidate_count || ticketPenaltyExecuting.value) return
  if (!confirm(
    `将对 ${preview.candidate_count} 个未扣门票人次追缴 ${preview.total_penalty_gold} 金币。\n\n`
    + '余额不足时会扣成负数；台账按探险日志与冒险者唯一去重。确定执行吗？'
  )) return
  ticketPenaltyExecuting.value = true
  try {
    const res = await store.adminSpecialDungeonTicketPenaltiesExecute(
      String(preview.snapshot_token || ''),
    )
    if (res.code === 200) {
      message.success(`已追缴 ${res.data?.executed_count ?? 0} 人次，共 ${res.data?.total_penalty_gold ?? 0} 金币`)
      await doTicketPenaltyPreview()
    } else message.error(res.message || '追缴失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '追缴失败')
  } finally {
    ticketPenaltyExecuting.value = false
  }
}

// ── 历史探险 legacy consumed 整组漏扣追回 ──
const consumptionRepairLogId = ref<number | null>(null)
const consumptionRepairAdvId = ref<number | null>(null)
const consumptionRepairPreview = ref<any>(null)
const consumptionRepairPreviewing = ref(false)
const consumptionRepairExecuting = ref(false)
const consumptionRepairRestoring = ref(false)

async function doConsumptionRepairPreview() {
  if (consumptionRepairPreviewing.value) return
  consumptionRepairPreviewing.value = true
  try {
    const res = await store.adminExpeditionConsumptionRepairPreview(
      consumptionRepairLogId.value || undefined,
      consumptionRepairAdvId.value || undefined,
    )
    if (res.code === 200) consumptionRepairPreview.value = res.data
    else message.error(res.message || '扫描失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '扫描失败')
  } finally {
    consumptionRepairPreviewing.value = false
  }
}

async function doConsumptionRepairExecute() {
  const preview = consumptionRepairPreview.value
  if (!preview?.candidate_count || consumptionRepairExecuting.value) return
  if (!confirm(
    `将按本次预览追回 ${preview.candidate_count} 条、共 ${preview.reclaim_quantity} 件历史漏扣物品。\n\n`
    + `另有 ${preview.ambiguous_count || 0} 条来源或状态不确定的记录不会处理。操作有台账，可用“撤销追回”补回。确定执行吗？`,
  )) return
  consumptionRepairExecuting.value = true
  try {
    const res = await store.adminExpeditionConsumptionRepairExecute(String(preview.snapshot_token || ''))
    if (res.code === 200) {
      message.success(`已追回 ${res.data?.repaired_count ?? 0} 条，共 ${res.data?.reclaimed_quantity ?? 0} 件`)
      await doConsumptionRepairPreview()
    } else message.error(res.message || '追回失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '追回失败')
  } finally {
    consumptionRepairExecuting.value = false
  }
}

async function doConsumptionRepairRestore() {
  if (consumptionRepairRestoring.value) return
  if (!confirm('将撤销所有尚未撤销的历史漏扣追回，并按台账把物品补回随身栏。确定吗？')) return
  consumptionRepairRestoring.value = true
  try {
    const res = await store.adminExpeditionConsumptionRepairRestore()
    if (res.code === 200) {
      message.success(`已撤销 ${res.data?.restored_count ?? 0} 条，共补回 ${res.data?.restored_quantity ?? 0} 件`)
      await doConsumptionRepairPreview()
    } else message.error(res.message || '撤销失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '撤销失败')
  } finally {
    consumptionRepairRestoring.value = false
  }
}

// ── 冒险者职业修复（转职/进阶成功但 character_class 为空的定向订正）──
const classRepairId = ref<number | null>(null)
const classRepairNewClass = ref('')
const classRepairInfo = ref<any>(null)
const classRepairPreviewing = ref(false)
const classRepairing = ref(false)
const classRepairRestoring = ref(false)

async function doClassRepairPreview() {
  if (!classRepairId.value) { message.error('请输入冒险者 ID'); return }
  if (classRepairPreviewing.value) return
  classRepairPreviewing.value = true
  try {
    const res = await store.adminClassRepairPreview(classRepairId.value)
    if (res.code === 200) {
      classRepairInfo.value = res.data
      classRepairNewClass.value = res.data?.current_class || ''
    } else {
      message.error(res.message || '预览失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    classRepairPreviewing.value = false
  }
}

async function doClassRepair() {
  if (!classRepairInfo.value || classRepairing.value) return
  const nc = classRepairNewClass.value.trim()
  if (!nc) { message.error('请输入新职业名'); return }
  const info = classRepairInfo.value
  if (!confirm(
    `将把【${info.name}】(ID ${info.id}) 的职业从「${info.current_class || '无'}」改为「${nc}」。\n\n`
    + '修复前会自动备份原值，可点「↩️ 还原职业修复」撤销。确定吗？'
  )) return
  classRepairing.value = true
  try {
    const res = await store.adminClassRepair(info.id, nc)
    if (res.code === 200) {
      if (res.data?.changed === false) {
        message.info(res.data?.message || '当前职业已是目标值')
      } else {
        message.success(`已写入：${res.data?.name} 职业「${res.data?.old_class || '无'}」→「${res.data?.new_class}」`)
      }
      await doClassRepairPreview()
    } else {
      message.error(res.message || '写入失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '写入失败')
  } finally {
    classRepairing.value = false
  }
}

async function doClassRepairRestore() {
  if (classRepairRestoring.value) return
  if (!confirm('把所有被职业修复改过的冒险者职业回退到修复前原值，并清空备份。确定吗？')) return
  classRepairRestoring.value = true
  try {
    const res = await store.adminClassRepairRestore()
    if (res.code === 200) {
      message.success(`已还原 ${res.data?.restored ?? 0} 个冒险者的职业`)
      classRepairInfo.value = null
    } else {
      message.error(res.message || '还原失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '还原失败')
  } finally {
    classRepairRestoring.value = false
  }
}

// ── 转职漏技能 · 补发职业通用技能卷轴 ──
const classSkillScrollAdvId = ref<number | null>(null)
const classSkillScrollPreview = ref<any>(null)
const classSkillScrollPreviewing = ref(false)
const classSkillScrollRepairing = ref(false)
const classSkillScrollRestoring = ref(false)

async function doClassSkillScrollPreview() {
  if (classSkillScrollPreviewing.value) return
  classSkillScrollPreviewing.value = true
  try {
    const advId = classSkillScrollAdvId.value || undefined
    const res = await store.adminClassSkillScrollPreview(advId)
    if (res.code === 200) {
      classSkillScrollPreview.value = res.data
    } else {
      message.error(res.message || '预览失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    classSkillScrollPreviewing.value = false
  }
}

async function doClassSkillScrollRepair() {
  if (classSkillScrollRepairing.value || !classSkillScrollPreview.value) return
  const items = classSkillScrollPreview.value.items || []
  const count = items.length
  if (count === 0) {
    message.info('没有待补发的冒险者')
    return
  }
  const advIds = classSkillScrollAdvId.value
    ? items.map((r: any) => r.id)
    : undefined
  if (!confirm(
    `将为 ${count} 名冒险者各补发 1 个「职业通用技能卷轴」到仓库。\n\n`
    + '补发前会自动备份，可点「↩️ 撤销补发」删除本次补发的卷轴。确定吗？'
  )) return
  classSkillScrollRepairing.value = true
  try {
    const res = await store.adminClassSkillScrollRepair(advIds)
    if (res.code === 200) {
      const granted = res.data?.granted ?? 0
      const errN = res.data?.errors?.length ?? 0
      message.success(`补发完成：成功 ${granted} 名${errN ? `，失败 ${errN} 名` : ''}`)
      await doClassSkillScrollPreview()
    } else {
      message.error(res.message || '补发失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '补发失败')
  } finally {
    classSkillScrollRepairing.value = false
  }
}

async function doClassSkillScrollRestore() {
  if (classSkillScrollRestoring.value) return
  if (!confirm('删除本服务上次补发写入仓库的「职业通用技能卷轴」，并清空备份。若玩家已消耗卷轴则无法找回。确定吗？')) return
  classSkillScrollRestoring.value = true
  try {
    const res = await store.adminClassSkillScrollRestore()
    if (res.code === 200) {
      message.success(`已撤销 ${res.data?.removed ?? 0} 件卷轴${res.data?.missing ? `（${res.data.missing} 件已不存在/已消耗）` : ''}`)
      classSkillScrollPreview.value = null
    } else {
      message.error(res.message || '撤销失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '撤销失败')
  } finally {
    classSkillScrollRestoring.value = false
  }
}

// ── 探险结算技能承诺 · 实际漏发修复 ──
const expeditionSkillLogId = ref<number | null>(null)
const expeditionSkillAdvId = ref<number | null>(null)
const expeditionSkillPreview = ref<any>(null)
const expeditionSkillPreviewing = ref(false)
const expeditionSkillRepairing = ref(false)
const expeditionSkillRestoring = ref(false)

async function doExpeditionSkillPreview() {
  if (expeditionSkillPreviewing.value) return
  expeditionSkillPreviewing.value = true
  try {
    const res = await store.adminExpeditionSkillRepairPreview(
      expeditionSkillLogId.value || undefined,
      expeditionSkillAdvId.value || undefined,
    )
    if (res.code === 200) expeditionSkillPreview.value = res.data
    else message.error(res.message || '预览失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    expeditionSkillPreviewing.value = false
  }
}

async function doExpeditionSkillRepair() {
  const count = expeditionSkillPreview.value?.candidate_count ?? 0
  if (expeditionSkillRepairing.value || count === 0) return
  if (!confirm(`将补发 ${count} 个结算已承诺但未到账的技能。操作会记录技能 ID 与来源日志，可撤销。确定吗？`)) return
  expeditionSkillRepairing.value = true
  try {
    const res = await store.adminExpeditionSkillRepair(
      expeditionSkillLogId.value || undefined,
      expeditionSkillAdvId.value || undefined,
    )
    if (res.code === 200) {
      message.success(res.message || `已补发 ${res.data?.created ?? 0} 个技能`)
      await doExpeditionSkillPreview()
    } else message.error(res.message || '补发失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '补发失败')
  } finally {
    expeditionSkillRepairing.value = false
  }
}

async function doExpeditionSkillRestore() {
  if (expeditionSkillRestoring.value) return
  if (!confirm('仅删除本修复工具创建且技能 ID、冒险者和名称仍完全匹配的记录。确定撤销吗？')) return
  expeditionSkillRestoring.value = true
  try {
    const res = await store.adminExpeditionSkillRepairRestore()
    if (res.code === 200) {
      message.success(res.message || `已撤销 ${res.data?.removed ?? 0} 个技能`)
      await doExpeditionSkillPreview()
    } else message.error(res.message || '撤销失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '撤销失败')
  } finally {
    expeditionSkillRestoring.value = false
  }
}

const commissionSettlementLogId = ref<number | null>(null)
const commissionSettlementPreview = ref<any>(null)
const commissionSettlementPreviewing = ref(false)
const commissionSettlementRepairing = ref(false)

async function doCommissionSettlementPreview() {
  if (!commissionSettlementLogId.value || commissionSettlementPreviewing.value) return
  commissionSettlementPreviewing.value = true
  try {
    const res = await store.adminCommissionSettlementRepairPreview(commissionSettlementLogId.value)
    if (res.code === 200) commissionSettlementPreview.value = res.data
    else message.error(res.message || '预览失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    commissionSettlementPreviewing.value = false
  }
}

async function doCommissionSettlementRepair() {
  const count = commissionSettlementPreview.value?.count ?? 0
  if (!commissionSettlementLogId.value || !count || commissionSettlementRepairing.value) return
  if (!confirm(`将按日志 #${commissionSettlementLogId.value} 补结算 ${count} 个委托。不会发奖，玩家仍需自行领取。确定吗？`)) return
  commissionSettlementRepairing.value = true
  try {
    const res = await store.adminCommissionSettlementRepair(commissionSettlementLogId.value)
    if (res.code === 200) {
      message.success(res.message || `已补结算 ${res.data?.fulfilled ?? 0} 个委托`)
      await doCommissionSettlementPreview()
    } else message.error(res.message || '补结算失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '补结算失败')
  } finally {
    commissionSettlementRepairing.value = false
  }
}

// ── 占位入门技能 · 回收换卷轴 ──
const placeholderRecallAdvId = ref<number | null>(null)
const placeholderRecallPreview = ref<any>(null)
const placeholderRecallPreviewing = ref(false)
const placeholderRecallSending = ref(false)

async function doPlaceholderRecallPreview() {
  if (placeholderRecallPreviewing.value) return
  placeholderRecallPreviewing.value = true
  try {
    const advId = placeholderRecallAdvId.value || undefined
    const res = await store.adminPlaceholderSkillRecallPreview(advId)
    if (res.code === 200) {
      placeholderRecallPreview.value = res.data
    } else {
      message.error(res.message || '预览失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    placeholderRecallPreviewing.value = false
  }
}

async function doPlaceholderRecallSend() {
  if (placeholderRecallSending.value || !placeholderRecallPreview.value) return
  const items = (placeholderRecallPreview.value.items || []).filter((r: any) => r.can_send)
  const count = items.length
  if (count === 0) {
    message.info('没有可推送的冒险者')
    return
  }
  const advIds = placeholderRecallAdvId.value
    ? items.map((r: any) => r.adventurer_id)
    : undefined
  if (!confirm(
    `将向 ${count} 名玩家推送回收确认：同意则移除其「xxx入门」占位技能，并按数量补发「职业通用技能卷轴」。\n\n`
    + '玩家可在「我的冒险者」页同意或拒绝。确定吗？'
  )) return
  placeholderRecallSending.value = true
  try {
    const res = await store.adminPlaceholderSkillRecallSendOffers(advIds)
    if (res.code === 200) {
      const sent = res.data?.sent?.length ?? 0
      const skipped = res.data?.skipped?.length ?? 0
      message.success(`推送完成：成功 ${sent} 名${skipped ? `，跳过 ${skipped} 名` : ''}`)
      await doPlaceholderRecallPreview()
    } else {
      message.error(res.message || '推送失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '推送失败')
  } finally {
    placeholderRecallSending.value = false
  }
}

// ── Lv2 锁定建筑无主残留清理（dump 还原 / 历史遗留致管理员视图误显示「募集中」）──
const orphanInfo = ref<any>(null)
const orphanPreviewing = ref(false)
const orphanPurging = ref(false)

async function doOrphanPreview() {
  if (orphanPreviewing.value) return
  orphanPreviewing.value = true
  try {
    const res = await store.adminPreviewOrphanLv2Instances()
    if (res.code === 200) {
      orphanInfo.value = res.data
    } else {
      message.error(res.message || '预览失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    orphanPreviewing.value = false
  }
}

async function doOrphanPurge() {
  if (orphanPurging.value || !orphanInfo.value || orphanInfo.value.count === 0) return
  if (!confirm(`将清理 ${orphanInfo.value.count} 个 Lv2 锁定建筑的无主残留实例（无帮工、无捐献）。确定吗？`)) return
  orphanPurging.value = true
  try {
    const res = await store.adminPurgeOrphanLv2Instances()
    if (res.code === 200) {
      message.success(`已清理 ${res.data?.purged ?? 0} 个残留实例`)
      await doOrphanPreview()
    } else {
      message.error(res.message || '清理失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '清理失败')
  } finally {
    orphanPurging.value = false
  }
}

const lv2DonationInfo = ref<any>(null)
const lv2DonationPreviewing = ref(false)
const lv2DonationRefundingId = ref<number | null>(null)

async function doLv2DonationPreview() {
  if (lv2DonationPreviewing.value) return
  lv2DonationPreviewing.value = true
  try {
    const res = await store.adminPreviewLv2LockedDonationInstances()
    if (res.code === 200) {
      lv2DonationInfo.value = res.data
    } else {
      message.error(res.message || '预览失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '预览失败')
  } finally {
    lv2DonationPreviewing.value = false
  }
}

async function doLv2DonationRefund(item: { id: number; name: string; code: string }) {
  if (lv2DonationRefundingId.value != null) return
  if (!confirm(`将退还「${item.name}」全部误捐材料、扣回贡献度并删除实例（building 态会一并释放帮工并清除帮工时长，恢复未解锁）。确定吗？`)) return
  lv2DonationRefundingId.value = item.id
  try {
    const res = await store.adminRefundLv2LockedBuildingDonations(item.id)
    if (res.code === 200) {
      message.success(res.data?.message || '已退款并锁定')
      await doLv2DonationPreview()
      await loadInstances()
    } else {
      message.error(res.message || '退款失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '退款失败')
  } finally {
    lv2DonationRefundingId.value = null
  }
}

function openCreate() {
  editingTemplate.value = null
  Object.assign(form, defaultForm())
  showForm.value = true
}

function openEdit(tpl: BuildingTemplate) {
  editingTemplate.value = tpl
  Object.assign(form, {
    name: tpl.name,
    icon: tpl.icon,
    description: tpl.description,
    building_type: tpl.building_type,
    category: tpl.category,
    build_hours: tpl.build_hours,
    helper_slots: tpl.helper_slots,
    stamina_cost_per_help: tpl.stamina_cost_per_help,
    speedup_minutes: tpl.speedup_minutes,
    sort_order: tpl.sort_order,
    handler_key: tpl.handler_key || '',
  })
  showForm.value = true
}

async function submitForm() {
  if (!form.name.trim()) {
    message.error('建筑名称不能为空')
    return
  }
  submitting.value = true
  try {
    let res
    if (editingTemplate.value) {
      res = await store.updateBuildingTemplate(editingTemplate.value.id, { ...form })
    } else {
      res = await store.createBuildingTemplate({ ...form })
    }
    if (res.code === 200 || res.code === 201) {
      message.success(editingTemplate.value ? '保存成功' : '创建成功')
      showForm.value = false
      store.fetchBuildingTemplates()
      store.fetchBuildings(true)
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    submitting.value = false
  }
}

async function toggleActive(tpl: BuildingTemplate) {
  try {
    const payload = { is_active: !tpl.is_active } satisfies Pick<BuildingTemplate, 'is_active'>
    const res = await store.updateBuildingTemplate(tpl.id, payload)
    if (res.code === 200) {
      message.success(tpl.is_active ? '已下架' : '已启用')
      store.fetchBuildingTemplates()
      store.fetchBuildings(true)
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  }
}

function handlerName(key: string): string {
  const h = availableHandlers.value.find(h => h.key === key)
  return h ? h.name : key
}

function categoryLabel(category: string): string {
  const labels: Record<string, string> = {
    general: '通用',
    dormitory: '宿舍区',
    medical: '治疗区',
    equipment: '装备区',
    gathering: '采集区',
    training: '修炼区',
    memorial: '纪念区',
    leisure: '休闲区',
    work: '打工区',
    market: '交易市场',
    auction: '拍卖行',
  }
  return labels[category] || category
}

async function loadInstances() {
  instancesLoading.value = true
  try {
    const res = await store.fetchAllBuildingInstances()
    if (res.code === 200) {
      allInstances.value = res.data ?? []
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载失败')
  } finally {
    instancesLoading.value = false
  }
}

async function doToggleDisable(inst: BuildingInstance) {
  const action = inst.disabled ? '启用' : '禁用'
  if (!confirm(`确定要${action}「${inst.custom_name || inst.template?.name || `实例 #${inst.id}`}」吗？`)) return
  togglingId.value = inst.id
  try {
    const res = await store.toggleBuildingDisabled(inst.id)
    if (res.code === 200) {
      message.success(`已${action}`)
      await loadInstances()
      store.fetchBuildings(true)
    } else {
      message.error(res.message || `${action}失败`)
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || `${action}失败`)
  } finally {
    togglingId.value = null
  }
}

function statusLabel(inst: BuildingInstance): string {
  if (inst.status === 'locked' && inst.donation_open) return '📣 募集中'
  return { locked: '🔒 未解锁', building: '🔨 建造中', built: '✅ 已建成' }[inst.status] || inst.status
}

async function loadHandlers() {
  try {
    const { data } = await api.get('/adventurer-s1/admin/building-handlers')
    if (data?.code === 200 && data.data) {
      availableHandlers.value = data.data
    }
  } catch {
    // 静默失败
  }
}

async function createInstance(tpl: BuildingTemplate) {
  try {
    const res = await store.createBuildingInstance(tpl.id)
    if (res.code === 200 || res.code === 201) {
      message.success('创建成功')
      store.fetchBuildings(true)
    } else {
      message.error(res.message || '创建失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '创建失败')
  }
}

function confirmDelete(tpl: BuildingTemplate) {
  deleteTarget.value = tpl
}

async function doDelete() {
  if (!deleteTarget.value) return
  deleting.value = true
  try {
    const res = await store.deleteBuildingTemplate(deleteTarget.value.id)
    if (res.code === 200) {
      message.success('删除成功')
      deleteTarget.value = null
      store.fetchBuildingTemplates()
      store.fetchBuildings(true)
    } else {
      message.error(res.message || '删除失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '删除失败')
  } finally {
    deleting.value = false
  }
}

onMounted(() => {
  store.fetchBuildingTemplates()
  loadInstances()
  loadHandlers()
})
</script>

<style scoped>
.panel-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-bottom: 16px;
}
.col-title {
  margin: 0;
}

.template-list {
  display: flex;
  flex-direction: column;
  gap: 12px;
}
.template-card {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 14px;
}
.template-head {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.template-icon {
  font-size: 1.4em;
}
.template-name {
  font-weight: 600;
  flex: 1;
}
.template-status {
  font-size: 0.8em;
  padding: 2px 8px;
  border-radius: 4px;
}
.template-status.active {
  background: rgba(25, 135, 84, 0.2);
  color: #198754;
}
.template-status.inactive {
  background: rgba(128, 128, 128, 0.2);
  color: #888;
}
.template-type {
  font-size: 0.75em;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(13, 110, 253, 0.2);
  color: #0d6efd;
}
.template-category {
  font-size: 0.75em;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(255, 193, 7, 0.2);
  color: #ffc107;
}
.template-handler {
  font-size: 0.75em;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(111, 66, 193, 0.2);
  color: #6f42c1;
}
.template-desc {
  font-size: 0.85em;
  color: var(--color-text-secondary);
  margin-bottom: 8px;
}
.template-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  font-size: 0.8em;
  color: var(--color-text-secondary);
  margin-bottom: 10px;
}
.template-actions {
  display: flex;
  gap: 8px;
}
.btn-edit, .btn-toggle, .btn-reject {
  padding: 4px 12px;
  border-radius: 6px;
  border: 1px solid var(--color-border);
  cursor: pointer;
  font-size: 0.85em;
}
.btn-edit {
  background: var(--color-primary);
  color: #fff;
  border-color: var(--color-primary);
}
.btn-toggle {
  background: var(--color-bg);
  color: var(--color-text);
}
.btn-add {
  background: rgba(25, 135, 84, 0.15);
  color: #198754;
  border-color: #198754;
}
.btn-reject {
  background: rgba(220, 53, 69, 0.15);
  color: #dc3545;
  border-color: #dc3545;
}

.modal-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 12px;
  z-index: 1000;
}
.modal-box {
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 12px;
  padding: 24px;
  max-width: 480px;
  width: 100%;
  max-height: 80vh;
  overflow-y: auto;
}
.modal-form .form-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  margin-bottom: 16px;
}
.form-label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 0.85em;
  color: var(--color-text-secondary);
}
.form-label.full-width {
  grid-column: 1 / -1;
}
.modal-title {
  margin: 0 0 16px;
  font-size: 1.2em;
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.warn-text {
  color: var(--color-danger);
  font-size: 0.85em;
}
.advs1-state {
  text-align: center;
  padding: 24px;
  color: var(--color-text-secondary);
}
.advs1-input {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 8px 10px;
  color: var(--color-text);
  font-size: 0.9em;
}
.advs1-submit {
  background: var(--color-primary);
  color: #fff;
  border: none;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}
.advs1-submit:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-cancel {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  color: var(--color-text);
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}

.instance-section {
  margin-top: 24px;
  padding-top: 20px;
  border-top: 1px solid var(--color-border);
}
.instance-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}
.instance-card {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 10px 14px;
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.instance-card.instance-disabled {
  opacity: 0.6;
  border-color: #dc3545;
}
.instance-head {
  display: flex;
  align-items: center;
  gap: 8px;
}
.instance-name {
  font-weight: 600;
}
.instance-status {
  font-size: 0.75em;
  padding: 2px 6px;
  border-radius: 4px;
}
.instance-status.status-locked {
  background: rgba(128, 128, 128, 0.2);
  color: #888;
}
.instance-status.status-building {
  background: rgba(255, 193, 7, 0.2);
  color: #ffc107;
}
.instance-status.status-built {
  background: rgba(25, 135, 84, 0.2);
  color: #198754;
}
.instance-disabled-tag {
  font-size: 0.75em;
  padding: 2px 6px;
  border-radius: 4px;
  background: rgba(220, 53, 69, 0.2);
  color: #dc3545;
}
.instance-actions {
  display: flex;
  gap: 8px;
}

/* ── 冒险者职业修复 ── */
.crb-box {
  margin: 12px 0;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}
.crb-title {
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 4px;
}
.crb-hint {
  font-size: 12px;
  color: var(--color-text-secondary);
}
.crb-row {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
  align-items: center;
  margin-top: 8px;
}
.crb-input {
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  color: var(--color-text);
  font-size: 16px;
}
.crb-info {
  margin-top: 10px;
}
.crb-line {
  color: var(--color-text);
  font-size: 14px;
}
.crb-row-inline {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  flex-wrap: wrap;
  margin-top: 4px;
}
.crb-row-inline .btn-sm {
  padding: 4px 10px;
  font-size: 12px;
  flex-shrink: 0;
}
.crb-has {
  color: var(--color-primary);
  font-weight: 600;
}
.crb-none {
  color: var(--color-danger);
  font-weight: 600;
}
.crb-story-label {
  margin-top: 8px;
  font-size: 12px;
  color: var(--color-text-secondary);
}
.crb-story {
  margin: 4px 0 0;
  padding: 8px;
  max-height: 200px;
  overflow: auto;
  white-space: pre-wrap;
  word-break: break-word;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  color: var(--color-text);
  font-size: 13px;
  line-height: 1.5;
}
.crb-scroll-list {
  margin-top: 8px;
  max-height: 240px;
  overflow: auto;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
}
.crb-scroll-row {
  padding: 6px 8px;
  font-size: 13px;
  color: var(--color-text);
  border-bottom: 1px solid var(--color-border);
}
.crb-scroll-row:last-child {
  border-bottom: none;
}

/* ── 移动端适配 ── */
@media (max-width: 640px) {
  .template-actions {
    flex-wrap: wrap;
  }

  .instance-card {
    flex-wrap: wrap;
    gap: 8px;
  }

  .modal-form .form-grid {
    grid-template-columns: 1fr;
  }

  .modal-box {
    padding: 16px;
    max-height: 90vh;
  }

  /* iOS Safari 表单控件字号 <16px 聚焦时会强制放大页面 */
  .advs1-input {
    font-size: 16px;
  }
}
</style>
