<template>
  <div class="token-usage-view">
    <div class="page-header">
      <div>
        <div class="page-eyebrow">S1 管理分析</div>
        <h4 class="page-title">AI Token 用量统计</h4>
        <p class="page-description">物理调用保留每次厂商尝试与费用；按类型视图另按业务会话展示逻辑链路结果。</p>
      </div>
      <div class="page-meta" aria-label="当前统计范围">
        <span class="scope-pill">{{ timeScopeLabel }}</span>
        <span class="scope-pill">{{ activeCallTypeLabel }}</span>
        <span v-if="lastUpdatedAt" class="updated-at">更新于 {{ formatRefreshTime(lastUpdatedAt) }}（UTC+8）</span>
      </div>
    </div>

    <!-- 顶部筛选栏 -->
    <div class="filter-bar">
      <div class="filter-group">
        <label>时间范围：</label>
        <select v-model="days" @change="handlePresetDaysChange">
          <option :value="1">最近 1 天</option>
          <option :value="3">最近 3 天</option>
          <option :value="7">最近 7 天</option>
          <option :value="14">最近 14 天</option>
          <option :value="30">最近 30 天</option>
        </select>
      </div>
      <div class="filter-group exact-start-filter">
        <label for="token-usage-start-at">精确起点（UTC+8）：</label>
        <input
          id="token-usage-start-at"
          v-model="startAtLocal"
          type="datetime-local"
          step="60"
          aria-describedby="token-usage-start-at-note"
          @change="handleExactStartChange"
        >
        <button
          v-if="startAtLocal"
          type="button"
          class="link-btn-sm"
          @click="clearExactStart"
        >清除</button>
        <span id="token-usage-start-at-note" class="filter-note">留空则使用最近天数</span>
      </div>
      <div class="filter-group">
        <label>调用类型：</label>
        <select
          v-model="callType"
          :disabled="activeTab === 'appeal-by-adventurer'"
          @change="handleGlobalFilterChange"
        >
          <option value="">全部</option>
          <option v-for="ct in callTypes" :key="ct" :value="ct">{{ callTypeLabel(ct) }}</option>
        </select>
        <span v-if="activeTab === 'appeal-by-adventurer'" class="filter-note">固定统计申诉一审与复核</span>
      </div>
      <button class="refresh-btn" @click="fetchActiveTab" :disabled="isActiveTabLoading">
        {{ isActiveTabLoading ? '刷新中…' : '刷新当前视图' }}
      </button>
    </div>

    <!-- Tab 栏 -->
    <div class="tab-bar" role="tablist" aria-label="Token 用量统计维度">
      <button
        v-for="tab in tabs"
        :key="tab.key"
        :class="['tab-btn', { active: activeTab === tab.key }]"
        role="tab"
        :aria-selected="activeTab === tab.key"
        @click="selectTab(tab.key)"
      >{{ tab.icon }} {{ tab.label }}</button>
    </div>

    <div v-if="loading && !overview" class="loading-text">加载中...</div>

    <!-- Tab 1: 总览 -->
    <div v-if="activeTab === 'overview'" class="tab-content">
      <template v-if="overview">
        <!-- 汇总卡片 -->
        <div class="stats-row">
          <div class="stat-card">
            <div class="stat-value">{{ overview.total_calls }}</div>
            <div class="stat-label">总调用次数</div>
          </div>
          <div class="stat-card">
            <div class="stat-value success">{{ formatPercent(overviewSuccessRate) }}</div>
            <div class="stat-label">物理调用成功率</div>
            <div class="stat-note">{{ overview.success_calls }} 次成功</div>
          </div>
          <div class="stat-card">
            <div class="stat-value danger">{{ overview.failed_calls }}</div>
            <div class="stat-label">失败</div>
          </div>
          <div class="stat-card">
            <div class="stat-value primary">{{ formatTokens(overview.total_tokens) }}</div>
            <div class="stat-label">总 Token</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{{ formatTokens(overviewAverageTokens) }}</div>
            <div class="stat-label">平均每次</div>
          </div>
          <div class="stat-card">
            <div class="stat-value cache">{{ (overview.cache_hit_rate * 100).toFixed(1) }}%</div>
            <div class="stat-label">缓存命中率</div>
          </div>
        </div>

        <!-- 详细数据 -->
        <div class="detail-grid">
          <div class="detail-card">
            <h5>📊 Token 明细</h5>
            <div class="detail-rows">
              <div class="detail-row">
                <span>输入 Token 总量</span>
                <span>{{ formatTokens(overview.prompt_tokens_total) }}</span>
              </div>
              <div class="detail-row">
                <span>├ 缓存命中</span>
                <span class="success">{{ formatTokens(overview.prompt_cache_hit_tokens) }}</span>
              </div>
              <div class="detail-row">
                <span>├ 缓存未命中</span>
                <span class="warning">{{ formatTokens(overview.prompt_cache_miss_tokens) }}</span>
              </div>
              <div class="detail-row">
                <span>输出 Token 总量</span>
                <span>{{ formatTokens(overview.completion_tokens) }}</span>
              </div>
              <div class="detail-row">
                <span>├ 最终内容（已拆分）</span>
                <span>{{ formatTokens(overview.completion_content_tokens) }}</span>
              </div>
              <div class="detail-row">
                <span>├ 推理 Token（厂商拆分）</span>
                <span>{{ formatTokens(overview.completion_reasoning_tokens) }}</span>
              </div>
              <div class="detail-row">
                <span>可见思考字符（已采集 {{ overview.visible_reasoning_known_calls }}/{{ overview.total_calls }} 次）</span>
                <span>{{ formatTokens(overview.visible_reasoning_chars) }}</span>
              </div>
              <div class="detail-row">
                <span>└ 历史/未拆分</span>
                <span class="warning">{{ formatTokens(overview.completion_unattributed_tokens) }}</span>
              </div>
            </div>
          </div>

          <div class="detail-card">
            <h5>📈 每日趋势</h5>
            <div class="daily-chart">
              <div v-for="d in overview.daily" :key="d.date" class="daily-row">
                <span class="daily-date">{{ shortDate(d.date) }}</span>
                <div class="daily-bar-track">
                  <div
                    class="daily-bar-fill"
                    :style="{ width: dailyBarWidth(d.total_tokens) + '%' }"
                    :title="`${d.total_tokens} tokens, ${d.calls} 次调用`"
                  ></div>
                </div>
                <span class="daily-value">{{ formatTokens(d.total_tokens) }}</span>
                <span class="daily-calls">{{ d.calls }}次</span>
              </div>
              <div v-if="!overview.daily?.length" class="empty-text">暂无数据</div>
            </div>
          </div>
        </div>
      </template>
    </div>

    <!-- Tab 2: 按类型 -->
    <div v-if="activeTab === 'by-type'" class="tab-content">
      <div v-if="byTypeLoading" class="loading-text">加载中...</div>
      <template v-else-if="sortedByType.length">
        <div class="table-toolbar">
          <span>共 {{ sortedByType.length }} 个类型 / 模型组合</span>
          <span class="table-hint">{{ tableSortSummary('byType') }}</span>
          <span class="table-hint">物理＝每次厂商调用；逻辑＝同调用类型跨模型按业务会话去重（同类型各模型行相同）</span>
        </div>
        <div class="data-table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th :aria-sort="ariaSort('byType', 'call_type_label')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'call_type_label', 'asc')">调用类型 <span>{{ sortIndicator('byType', 'call_type_label') }}</span></button></th>
                <th :aria-sort="ariaSort('byType', 'model')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'model', 'asc')">模型 <span>{{ sortIndicator('byType', 'model') }}</span></button></th>
                <th :aria-sort="ariaSort('byType', 'last_called_at')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'last_called_at')">最后调用 <span>{{ sortIndicator('byType', 'last_called_at') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'total_calls')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'total_calls')">物理调用 <span>{{ sortIndicator('byType', 'total_calls') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'success_rate')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'success_rate')">物理成功率 <span>{{ sortIndicator('byType', 'success_rate') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'logical_success_rate')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'logical_success_rate')">逻辑链路 <span>{{ sortIndicator('byType', 'logical_success_rate') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'prompt_tokens_total')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'prompt_tokens_total')">输入 Token <span>{{ sortIndicator('byType', 'prompt_tokens_total') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'prompt_cache_hit_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'prompt_cache_hit_tokens')">缓存命中 <span>{{ sortIndicator('byType', 'prompt_cache_hit_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'prompt_cache_miss_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'prompt_cache_miss_tokens')">缓存未命中 <span>{{ sortIndicator('byType', 'prompt_cache_miss_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'completion_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'completion_tokens')">输出总量 <span>{{ sortIndicator('byType', 'completion_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'completion_content_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'completion_content_tokens')">最终内容 <span>{{ sortIndicator('byType', 'completion_content_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'completion_reasoning_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'completion_reasoning_tokens')">推理 Token <span>{{ sortIndicator('byType', 'completion_reasoning_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'visible_reasoning_chars')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'visible_reasoning_chars')">可见思考字符 <span>{{ sortIndicator('byType', 'visible_reasoning_chars') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'completion_unattributed_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'completion_unattributed_tokens')">未拆分 <span>{{ sortIndicator('byType', 'completion_unattributed_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'total_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'total_tokens')">总 Token <span>{{ sortIndicator('byType', 'total_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'avg_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'avg_tokens')">平均/次 <span>{{ sortIndicator('byType', 'avg_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byType', 'cache_hit_rate')"><button class="sort-header-btn" @click="toggleTableSort('byType', 'cache_hit_rate')">命中率 <span>{{ sortIndicator('byType', 'cache_hit_rate') }}</span></button></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in sortedByType" :key="`${item.call_type}:${item.model || 'unknown'}`">
                <td><span class="type-badge">{{ item.call_type_label || callTypeLabel(item.call_type) }}</span></td>
                <td><code>{{ item.model || '未知模型' }}</code></td>
                <td class="dim">{{ formatUtc8RecordTime(item.last_called_at) }}</td>
                <td class="num">{{ item.total_calls }}</td>
                <td class="num">
                  <span :class="successRateClass(item)">{{ formatPercent(rowSuccessRate(item)) }}</span>
                  <span class="cell-note">{{ item.success_calls }} 成 / {{ item.failed_calls }} 败</span>
                </td>
                <td class="num" :title="logicalChainTitle(item)">
                  <span :class="logicalSuccessRateClass(item)">{{ formatPercent(rowLogicalSuccessRate(item)) }}</span>
                  <span class="cell-note">{{ item.logical_success_calls }} 成 / {{ item.logical_failed_calls }} 败</span>
                </td>
                <td class="num">{{ formatTokens(item.prompt_tokens_total) }}</td>
                <td class="num success">{{ formatTokens(item.prompt_cache_hit_tokens) }}</td>
                <td class="num warning">{{ formatTokens(item.prompt_cache_miss_tokens) }}</td>
                <td class="num">{{ formatTokens(item.completion_tokens) }}</td>
                <td class="num">{{ formatTokens(item.completion_content_tokens) }}</td>
                <td class="num">{{ formatTokens(item.completion_reasoning_tokens) }}</td>
                <td class="num">{{ formatTokens(item.visible_reasoning_chars) }}</td>
                <td class="num warning">{{ formatTokens(item.completion_unattributed_tokens) }}</td>
                <td class="num bold">{{ formatTokens(item.total_tokens) }}</td>
                <td class="num">{{ formatTokens(rowAverageTokens(item)) }}</td>
                <td class="num">{{ (item.cache_hit_rate * 100).toFixed(1) }}%</td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <div v-else class="empty-text">暂无数据</div>
    </div>

    <!-- Tab 3: 按小队 -->
    <div v-if="activeTab === 'by-party'" class="tab-content">
      <div v-if="byPartyLoading" class="loading-text">加载中...</div>
      <template v-else-if="sortedByParty.length">
        <div class="table-toolbar">
          <span>共 {{ sortedByParty.length }} 个小队归属</span>
          <span class="table-hint">{{ tableSortSummary('byParty') }}</span>
        </div>
        <div class="data-table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th :aria-sort="ariaSort('byParty', 'party_name')"><button class="sort-header-btn" @click="toggleTableSort('byParty', 'party_name', 'asc')">小队 <span>{{ sortIndicator('byParty', 'party_name') }}</span></button></th>
                <th>成员</th>
                <th class="num" :aria-sort="ariaSort('byParty', 'total_calls')"><button class="sort-header-btn" @click="toggleTableSort('byParty', 'total_calls')">调用次数 <span>{{ sortIndicator('byParty', 'total_calls') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byParty', 'visible_reasoning_chars')"><button class="sort-header-btn" @click="toggleTableSort('byParty', 'visible_reasoning_chars')">可见思考字符 <span>{{ sortIndicator('byParty', 'visible_reasoning_chars') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byParty', 'total_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byParty', 'total_tokens')">总 Token <span>{{ sortIndicator('byParty', 'total_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byParty', 'avg_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byParty', 'avg_tokens')">平均/次 <span>{{ sortIndicator('byParty', 'avg_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byParty', 'cache_hit_rate')"><button class="sort-header-btn" @click="toggleTableSort('byParty', 'cache_hit_rate')">缓存命中率 <span>{{ sortIndicator('byParty', 'cache_hit_rate') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byParty', 'success_rate')"><button class="sort-header-btn" @click="toggleTableSort('byParty', 'success_rate')">物理成功率 <span>{{ sortIndicator('byParty', 'success_rate') }}</span></button></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in sortedByParty" :key="item.party_id || 'none'">
                <td>{{ item.party_name }}</td>
                <td class="members-cell">{{ item.members || '-' }}</td>
                <td class="num">{{ item.total_calls }}</td>
                <td class="num">{{ formatTokens(item.visible_reasoning_chars) }}</td>
                <td class="num bold">{{ formatTokens(item.total_tokens) }}</td>
                <td class="num">{{ formatTokens(rowAverageTokens(item)) }}</td>
                <td class="num">{{ (item.cache_hit_rate * 100).toFixed(1) }}%</td>
                <td class="num">
                  <span :class="successRateClass(item)">{{ formatPercent(rowSuccessRate(item)) }}</span>
                  <span class="cell-note">{{ item.success_calls }} 成 / {{ item.failed_calls }} 败</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <div v-else class="empty-text">暂无数据</div>
    </div>

    <!-- Tab 4: 按探险 -->
    <div v-if="activeTab === 'by-expedition'" class="tab-content">
      <div v-if="byExpeditionLoading" class="loading-text">加载中...</div>
      <template v-else-if="sortedByExpedition.length">
        <div class="table-toolbar">
          <span>共 {{ sortedByExpedition.length }} 个探险归属</span>
          <span class="table-hint">{{ tableSortSummary('byExpedition') }}</span>
        </div>
        <div class="data-table-wrapper">
          <table class="data-table">
            <thead>
              <tr>
                <th :aria-sort="ariaSort('byExpedition', 'expedition_log_id')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'expedition_log_id', 'asc')">探险日志 ID <span>{{ sortIndicator('byExpedition', 'expedition_log_id') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'total_calls')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'total_calls')">调用次数 <span>{{ sortIndicator('byExpedition', 'total_calls') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'total_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'total_tokens')">总 Token <span>{{ sortIndicator('byExpedition', 'total_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'avg_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'avg_tokens')">平均/次 <span>{{ sortIndicator('byExpedition', 'avg_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'prompt_tokens_total')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'prompt_tokens_total')">输入 Token <span>{{ sortIndicator('byExpedition', 'prompt_tokens_total') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'completion_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'completion_tokens')">输出总量 <span>{{ sortIndicator('byExpedition', 'completion_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'completion_content_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'completion_content_tokens')">最终内容 <span>{{ sortIndicator('byExpedition', 'completion_content_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'completion_reasoning_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'completion_reasoning_tokens')">推理 Token <span>{{ sortIndicator('byExpedition', 'completion_reasoning_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'visible_reasoning_chars')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'visible_reasoning_chars')">可见思考字符 <span>{{ sortIndicator('byExpedition', 'visible_reasoning_chars') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'completion_unattributed_tokens')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'completion_unattributed_tokens')">未拆分 <span>{{ sortIndicator('byExpedition', 'completion_unattributed_tokens') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'cache_hit_rate')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'cache_hit_rate')">缓存命中率 <span>{{ sortIndicator('byExpedition', 'cache_hit_rate') }}</span></button></th>
                <th class="num" :aria-sort="ariaSort('byExpedition', 'success_rate')"><button class="sort-header-btn" @click="toggleTableSort('byExpedition', 'success_rate')">物理成功率 <span>{{ sortIndicator('byExpedition', 'success_rate') }}</span></button></th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="item in sortedByExpedition" :key="item.expedition_log_id || 'none'">
                <td>{{ item.expedition_log_id ?? '无关联' }}</td>
                <td class="num">{{ item.total_calls }}</td>
                <td class="num bold">{{ formatTokens(item.total_tokens) }}</td>
                <td class="num">{{ formatTokens(rowAverageTokens(item)) }}</td>
                <td class="num">{{ formatTokens(item.prompt_tokens_total) }}</td>
                <td class="num">{{ formatTokens(item.completion_tokens) }}</td>
                <td class="num">{{ formatTokens(item.completion_content_tokens) }}</td>
                <td class="num">{{ formatTokens(item.completion_reasoning_tokens) }}</td>
                <td class="num">{{ formatTokens(item.visible_reasoning_chars) }}</td>
                <td class="num warning">{{ formatTokens(item.completion_unattributed_tokens) }}</td>
                <td class="num">{{ (item.cache_hit_rate * 100).toFixed(1) }}%</td>
                <td class="num">
                  <span :class="successRateClass(item)">{{ formatPercent(rowSuccessRate(item)) }}</span>
                  <span class="cell-note">{{ item.success_calls }} 成 / {{ item.failed_calls }} 败</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
      </template>
      <div v-else class="empty-text">暂无数据</div>
    </div>

    <!-- Tab 5: 申诉按冒险者 -->
    <div v-if="activeTab === 'appeal-by-adventurer'" class="tab-content">
      <div class="appeal-filter-bar">
        <div class="filter-group">
          <label>排序：</label>
          <select v-model="appealSortBy" @change="fetchAppealByAdventurer">
            <option value="total_tokens">总 Token</option>
            <option value="avg_tokens">平均每次 Token</option>
            <option value="total_calls">调用次数</option>
          </select>
        </div>
        <div class="filter-group">
          <label>顺序：</label>
          <select v-model="appealSortOrder" @change="fetchAppealByAdventurer">
            <option value="desc">降序</option>
            <option value="asc">升序</option>
          </select>
        </div>
        <div class="filter-group">
          <label>指定日期：</label>
          <input v-model="appealDate" type="date" @change="fetchAppealByAdventurer" />
          <button v-if="appealDate" class="link-btn-sm" @click="clearAppealDate">清除</button>
        </div>
        <button
          v-if="appealStats?.link_stats?.pending_backfill"
          class="refresh-btn"
          @click="runAppealLinkBackfill"
          :disabled="appealBackfillLoading"
        >{{ appealBackfillLoading ? '回填中…' : `回填工单关联 (${appealStats.link_stats.pending_backfill})` }}</button>
      </div>

      <div v-if="appealStats?.link_stats?.pending_backfill" class="appeal-hint">
        历史 {{ appealStats.link_stats.pending_backfill }} 条记录未持久化 appeal_id；为避免实时推断拖慢页面，请点「回填工单关联」后再查看精确归属。
      </div>
      <div v-else-if="appealStats?.link_stats?.still_unlinked" class="appeal-hint warn">
        仍有 {{ appealStats.link_stats.still_unlinked }} 条无法匹配到任何申诉工单。
      </div>

      <div class="appeal-metric-note">
        「提交申诉」= 工单数（每日配额 5 次）；「AI 调用」= 每次调 API 计 1 次（一审 + 复核 + 失败重试会累加）。
      </div>

      <div v-if="appealLoading" class="loading-text">加载中...</div>
      <template v-else-if="appealStats">
        <div class="stats-row">
          <div class="stat-card">
            <div class="stat-value">{{ appealStats.summary.total_calls }}</div>
            <div class="stat-label">申诉 AI 调用</div>
          </div>
          <div class="stat-card">
            <div class="stat-value primary">{{ formatTokens(appealStats.summary.total_tokens) }}</div>
            <div class="stat-label">总 Token</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{{ formatTokens(appealStats.summary.visible_reasoning_chars) }}</div>
            <div class="stat-label">可见思考字符</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{{ formatTokens(appealStats.summary.avg_tokens_per_call) }}</div>
            <div class="stat-label">平均每次</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{{ appealStats.call_type_breakdown?.data_appeal_ai ?? 0 }}</div>
            <div class="stat-label">一审 AI 次</div>
          </div>
          <div class="stat-card">
            <div class="stat-value">{{ appealStats.call_type_breakdown?.data_appeal_review_ai ?? 0 }}</div>
            <div class="stat-label">复核 AI 次</div>
          </div>
        </div>

        <div class="detail-card" v-if="appealStats.summary.daily?.length">
          <h5>📈 每日申诉 Token 趋势</h5>
          <div class="daily-chart">
            <div v-for="d in appealStats.summary.daily" :key="d.date" class="daily-row">
              <span class="daily-date">{{ shortDate(d.date) }}</span>
              <div class="daily-bar-track">
                <div
                  class="daily-bar-fill appeal-bar"
                  :style="{ width: appealDailyBarWidth(d.total_tokens) + '%' }"
                  :title="`${d.total_tokens} tokens, ${d.calls} 次, 均 ${d.avg_tokens_per_call}`"
                ></div>
              </div>
              <span class="daily-value">{{ formatTokens(d.total_tokens) }}</span>
              <span class="daily-calls">{{ d.calls }}次</span>
              <span class="daily-avg">均{{ formatTokens(d.avg_tokens_per_call) }}</span>
            </div>
          </div>
        </div>

        <div class="data-table-wrapper" v-if="appealStats.adventurers?.length">
          <div class="table-toolbar table-toolbar-inline">
            <span>共 {{ appealStats.adventurers.length }} 名冒险者</span>
            <span class="table-hint">点击“AI 调用 / 总 Token / 平均每次”切换排序</span>
          </div>
          <table class="data-table">
            <thead>
              <tr>
                <th>#</th>
                <th>冒险者</th>
                <th class="num">提交申诉</th>
                <th class="num" :aria-sort="appealAriaSort('total_calls')"><button class="sort-header-btn" @click="toggleAppealSort('total_calls')">AI 调用 <span>{{ appealSortIndicator('total_calls') }}</span></button></th>
                <th class="num" :aria-sort="appealAriaSort('total_tokens')"><button class="sort-header-btn" @click="toggleAppealSort('total_tokens')">总 Token <span>{{ appealSortIndicator('total_tokens') }}</span></button></th>
                <th class="num">可见思考字符</th>
                <th class="num" :aria-sort="appealAriaSort('avg_tokens')"><button class="sort-header-btn" @click="toggleAppealSort('avg_tokens')">平均/次 <span>{{ appealSortIndicator('avg_tokens') }}</span></button></th>
                <th class="num">一审</th>
                <th class="num">复核</th>
                <th>每日明细</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="(item, idx) in appealStats.adventurers" :key="item.adventurer_id ?? 'unknown'">
                <td class="dim">{{ Number(idx) + 1 }}</td>
                <td>
                  <strong>{{ item.adventurer_name }}</strong>
                  <span class="dim" v-if="item.adventurer_id"> #{{ item.adventurer_id }}</span>
                </td>
                <td class="num">{{ item.submitted_appeals ?? 0 }}</td>
                <td class="num">{{ item.total_calls }}</td>
                <td class="num bold">{{ formatTokens(item.total_tokens) }}</td>
                <td class="num">{{ formatTokens(item.visible_reasoning_chars) }}</td>
                <td class="num">{{ formatTokens(item.avg_tokens_per_call) }}</td>
                <td class="num">
                  {{ item.by_call_type?.data_appeal_ai?.calls ?? 0 }}
                  <span class="dim">({{ formatTokens(item.by_call_type?.data_appeal_ai?.total_tokens ?? 0) }})</span>
                </td>
                <td class="num">
                  {{ item.by_call_type?.data_appeal_review_ai?.calls ?? 0 }}
                  <span class="dim">({{ formatTokens(item.by_call_type?.data_appeal_review_ai?.total_tokens ?? 0) }})</span>
                </td>
                <td class="daily-mini-cell">
                  <span
                    v-for="d in item.daily"
                    :key="d.date"
                    class="daily-mini-tag"
                    :title="`${d.date}: 提交${d.submitted_appeals ?? '?'}单, AI${d.calls}次, ${d.total_tokens} tokens`"
                  >{{ shortDate(d.date) }} {{ d.submitted_appeals ?? '?' }}单/{{ d.calls }}AI</span>
                  <span v-if="!item.daily?.length" class="dim">-</span>
                </td>
              </tr>
            </tbody>
          </table>
        </div>
        <div v-else class="empty-text">暂无申诉 AI 用量数据（新记录会关联冒险者 ID）</div>
      </template>
    </div>

    <!-- Tab 6: 物理调用记录 -->
    <div v-if="activeTab === 'records'" class="tab-content">
      <div class="records-filter">
        <div class="filter-group session-filter">
          <label>业务会话：</label>
          <input
            v-model.trim="recordFilters.correlation_id"
            type="text"
            placeholder="会话 ID，可点击表格填入"
            @change="applyRecordFilters"
          />
          <button
            v-if="recordFilters.correlation_id"
            type="button"
            class="clear-session-btn"
            @click="clearRecordCorrelation"
          >清除</button>
        </div>
        <div class="filter-group">
          <label>小队 ID：</label>
          <input v-model.number="recordFilters.party_id" type="number" placeholder="可选" @change="applyRecordFilters" />
        </div>
        <div class="filter-group">
          <label>探险日志 ID：</label>
          <input v-model.number="recordFilters.expedition_log_id" type="number" placeholder="可选" @change="applyRecordFilters" />
        </div>
        <div class="filter-group">
          <label>状态：</label>
          <select v-model="recordSuccessFilter" @change="applyRecordFilters">
            <option value="">全部</option>
            <option value="true">完全成功</option>
            <option value="false">未完全成功</option>
          </select>
        </div>
        <div class="filter-group">
          <label>每页：</label>
          <select v-model.number="recordsPerPage" @change="applyRecordFilters">
            <option :value="25">25 条</option>
            <option :value="50">50 条</option>
            <option :value="100">100 条</option>
          </select>
        </div>
      </div>

      <div v-if="recordsLoading" class="loading-text">加载中...</div>
      <template v-else>
        <p class="records-scope-note">
          一行代表一次物理 AI 调用；同一业务会话的生成、审核和线路重试会分成多行。
          点击会话 ID 可只查看该会话的完整调用链。
          部分成功、已降级与未采纳均归入“未完全成功”，成功率仍只统计完全成功。
        </p>
        <div class="table-toolbar">
          <span>共 {{ recordsTotal }} 条记录</span>
          <span class="table-hint">当前按 {{ recordSortLabel }}{{ recordsSortOrder === 'desc' ? '降序' : '升序' }}</span>
        </div>
        <div class="data-table-wrapper">
          <table class="data-table records-table">
            <thead>
              <tr>
                <th :aria-sort="recordAriaSort('id')"><button class="sort-header-btn" @click="toggleRecordSort('id', 'desc')">ID <span>{{ recordSortIndicator('id') }}</span></button></th>
                <th :aria-sort="recordAriaSort('correlation_id')"><button class="sort-header-btn" @click="toggleRecordSort('correlation_id', 'asc')">业务会话 <span>{{ recordSortIndicator('correlation_id') }}</span></button></th>
                <th>业务尝试</th>
                <th :aria-sort="recordAriaSort('is_success')"><button class="sort-header-btn" @click="toggleRecordSort('is_success')">状态 <span>{{ recordSortIndicator('is_success') }}</span></button></th>
                <th>失败诊断</th>
                <th :aria-sort="recordAriaSort('call_type')"><button class="sort-header-btn" @click="toggleRecordSort('call_type', 'asc')">调用类型 <span>{{ recordSortIndicator('call_type') }}</span></button></th>
                <th :aria-sort="recordAriaSort('model')"><button class="sort-header-btn" @click="toggleRecordSort('model', 'asc')">模型 <span>{{ recordSortIndicator('model') }}</span></button></th>
                <th>物理路由</th>
                <th :aria-sort="recordAriaSort('party_id')"><button class="sort-header-btn" @click="toggleRecordSort('party_id', 'asc')">小队 <span>{{ recordSortIndicator('party_id') }}</span></button></th>
                <th :aria-sort="recordAriaSort('expedition_log_id')"><button class="sort-header-btn" @click="toggleRecordSort('expedition_log_id', 'asc')">探险日志 <span>{{ recordSortIndicator('expedition_log_id') }}</span></button></th>
                <th>成员</th>
                <th class="num" :aria-sort="recordAriaSort('prompt_tokens_total')"><button class="sort-header-btn" @click="toggleRecordSort('prompt_tokens_total')">输入 <span>{{ recordSortIndicator('prompt_tokens_total') }}</span></button></th>
                <th class="num" :aria-sort="recordAriaSort('prompt_cache_hit_tokens')"><button class="sort-header-btn" @click="toggleRecordSort('prompt_cache_hit_tokens')">命中 <span>{{ recordSortIndicator('prompt_cache_hit_tokens') }}</span></button></th>
                <th class="num" :aria-sort="recordAriaSort('prompt_cache_miss_tokens')"><button class="sort-header-btn" @click="toggleRecordSort('prompt_cache_miss_tokens')">未命中 <span>{{ recordSortIndicator('prompt_cache_miss_tokens') }}</span></button></th>
                <th class="num" :aria-sort="recordAriaSort('completion_tokens')"><button class="sort-header-btn" @click="toggleRecordSort('completion_tokens')">输出总量 <span>{{ recordSortIndicator('completion_tokens') }}</span></button></th>
                <th class="num">最终内容</th>
                <th class="num" :aria-sort="recordAriaSort('completion_reasoning_tokens')"><button class="sort-header-btn" @click="toggleRecordSort('completion_reasoning_tokens')">推理 Token <span>{{ recordSortIndicator('completion_reasoning_tokens') }}</span></button></th>
                <th class="num" :aria-sort="recordAriaSort('visible_reasoning_chars')"><button class="sort-header-btn" @click="toggleRecordSort('visible_reasoning_chars')">可见思考字符 <span>{{ recordSortIndicator('visible_reasoning_chars') }}</span></button></th>
                <th class="num">未拆分</th>
                <th class="num" :aria-sort="recordAriaSort('total_tokens')"><button class="sort-header-btn" @click="toggleRecordSort('total_tokens')">总 Token <span>{{ recordSortIndicator('total_tokens') }}</span></button></th>
                <th :aria-sort="recordAriaSort('invoke_reason')"><button class="sort-header-btn" @click="toggleRecordSort('invoke_reason', 'asc')">入队原因 <span>{{ recordSortIndicator('invoke_reason') }}</span></button></th>
                <th :aria-sort="recordAriaSort('created_at')"><button class="sort-header-btn" @click="toggleRecordSort('created_at', 'desc')">时间（UTC+8） <span>{{ recordSortIndicator('created_at') }}</span></button></th>
                <th>Request ID</th>
              </tr>
            </thead>
            <tbody>
              <tr v-for="r in records" :key="r.id">
                <td class="dim">#{{ r.id }}</td>
                <td class="session-cell">
                  <button
                    v-if="r.correlation_id"
                    type="button"
                    class="session-link"
                    :title="`仅查看业务会话 ${r.correlation_id}`"
                    @click="focusRecordCorrelation(r.correlation_id)"
                  >{{ shortCorrelationId(r.correlation_id) }}</button>
                  <span v-else class="dim">-</span>
                </td>
                <td class="dim">{{ r.attempt_number ? `#${r.attempt_number}` : '-' }}</td>
                <td>
                  <span :class="recordOutcome(r).className" :title="recordOutcome(r).description">
                    {{ recordOutcome(r).label }}
                  </span>
                </td>
                <td class="diagnostic-cell">
                  <template v-if="!r.is_success">
                    <span v-if="recordDiagnosticLabel(r)" class="cell-note">{{ recordDiagnosticLabel(r) }}</span>
                    <code>{{ r.validation_error_code || r.failure_stage || 'legacy_unclassified' }}</code>
                    <span v-if="r.failure_stage" class="cell-note">stage={{ r.failure_stage }}</span>
                    <span v-if="r.finish_reason" class="cell-note">finish={{ r.finish_reason }}</span>
                    <span v-if="r.failover_reason" class="cell-note">failover={{ r.failover_reason }}</span>
                    <button v-if="r.has_failure_detail" type="button" class="session-link" @click="failureRecordId = r.id">查看全文</button>
                  </template>
                  <span v-else class="dim">-</span>
                </td>
                <td><span class="type-badge">{{ r.call_type_label || callTypeLabel(r.call_type) }}</span></td>
                <td><code>{{ r.model || '未知模型' }}</code></td>
                <td class="route-cell">
                  <code>{{ recordRouteLabel(r) }}</code>
                  <span v-if="r.transport_attempt" class="cell-note">transport #{{ r.transport_attempt }}</span>
                </td>
                <td>{{ r.party_name || '-' }}</td>
                <td>{{ r.expedition_log_id ? `#${r.expedition_log_id}` : '-' }}</td>
                <td class="members-cell">{{ r.members || '-' }}</td>
                <td class="num">{{ formatTokens(r.prompt_tokens_total) }}</td>
                <td class="num success">{{ formatTokens(r.prompt_cache_hit_tokens) }}</td>
                <td class="num warning">{{ formatTokens(r.prompt_cache_miss_tokens) }}</td>
                <td class="num">{{ formatTokens(r.completion_tokens) }}</td>
                <td class="num">{{ formatOptionalTokens(r.completion_content_tokens) }}</td>
                <td class="num">{{ formatOptionalTokens(r.completion_reasoning_tokens) }}</td>
                <td class="num">{{ formatOptionalTokens(r.visible_reasoning_chars) }}</td>
                <td class="num warning">{{ formatTokens(r.completion_unattributed_tokens) }}</td>
                <td class="num bold">{{ formatTokens(r.total_tokens) }}</td>
                <td class="dim invoke-reason-cell">
                  {{ r.invoke_reason_label || r.invoke_reason || '-' }}
                </td>
                <td class="dim">{{ formatUtc8RecordTime(r.created_at) }}</td>
                <td class="request-id-cell">
                  <code :title="r.provider_request_id || ''">{{ r.provider_request_id || '-' }}</code>
                </td>
              </tr>
            </tbody>
          </table>
        </div>

        <!-- 分页 -->
        <div class="pagination" v-if="recordsTotal > recordsPerPage">
          <button :disabled="recordsPage <= 1" @click="recordsPage--; fetchRecords()">‹ 上一页</button>
          <span>{{ recordsPage }} / {{ recordsPages }} (共 {{ recordsTotal }} 条)</span>
          <button :disabled="recordsPage >= recordsPages" @click="recordsPage++; fetchRecords()">下一页 ›</button>
        </div>
      </template>
    </div>
    <AiFailureDetailDialog
      v-if="failureRecordId !== null"
      :key="`${recordsRealm}-${failureRecordId}`"
      :record-id="failureRecordId"
      :realm="recordsRealm"
      @close="failureRecordId = null"
      @cleared="id => { const row = records.find(item => item.id === id); if (row) row.has_failure_detail = false }"
    />
  </div>
</template>

<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import api, { getS1AdminRealmTarget } from '@/composables/useApi'
import AiFailureDetailDialog from './AiFailureDetailDialog.vue'

type SortOrder = 'asc' | 'desc'
type ClientSortTable = 'byType' | 'byParty' | 'byExpedition'
type SortableRow = Record<string, any>

const TOKEN_USAGE_TIME_ZONE = 'Asia/Hong_Kong'
const TOKEN_USAGE_UTC_OFFSET = '+08:00'

interface TableSortState {
  key: string
  order: SortOrder
}

// ── 状态 ──
const loading = ref(false)
const days = ref(7)
const startAtLocal = ref('')
const callType = ref('')
const activeTab = ref('overview')
const lastUpdatedAt = ref<Date | null>(null)

const tabs = [
  { key: 'overview', icon: '📊', label: '总览' },
  { key: 'by-type', icon: '🏷️', label: '按类型' },
  { key: 'by-party', icon: '👥', label: '按小队' },
  { key: 'by-expedition', icon: '🗺️', label: '按探险' },
  { key: 'appeal-by-adventurer', icon: '📝', label: '申诉按冒险者' },
  { key: 'records', icon: '📋', label: '物理调用' },
]

const loadedTabs = ref<Set<string>>(new Set())

// ── 总览 ──
const overview = ref<any>(null)

// ── 按类型 ──
const byType = ref<any[]>([])
const byTypeLoading = ref(false)

// ── 按小队 ──
const byParty = ref<any[]>([])
const byPartyLoading = ref(false)

// ── 按探险 ──
const byExpedition = ref<any[]>([])
const byExpeditionLoading = ref(false)

const tableSorts = ref<Record<ClientSortTable, TableSortState>>({
  byType: { key: 'total_tokens', order: 'desc' },
  byParty: { key: 'total_tokens', order: 'desc' },
  byExpedition: { key: 'total_tokens', order: 'desc' },
})

// ── 申诉按冒险者 ──
const appealStats = ref<any>(null)
const appealLoading = ref(false)
const appealSortBy = ref('total_tokens')
const appealSortOrder = ref('desc')
const appealDate = ref('')
const appealBackfillLoading = ref(false)

// ── 物理调用记录 ──
const records = ref<any[]>([])
const recordsRealm = ref(getS1AdminRealmTarget())
const failureRecordId = ref<number | null>(null)
const recordsLoading = ref(false)
const recordsPage = ref(1)
const recordsPerPage = ref(50)
const recordsTotal = ref(0)
const recordsPages = ref(1)
const recordSuccessFilter = ref('')
const recordsSortBy = ref('created_at')
const recordsSortOrder = ref<SortOrder>('desc')
const recordFilters = ref<{
  correlation_id: string
  party_id: number | null
  expedition_log_id: number | null
}>({
  correlation_id: '',
  party_id: null,
  expedition_log_id: null,
})

// ── 调用类型标签（后端 S1_CALL_TYPE_LABELS 为权威来源） ──
const callTypeLabels = ref<Record<string, string>>({})

const callTypes = computed(() => {
  const types = new Set<string>(Object.keys(callTypeLabels.value))
  if (byType.value.length) byType.value.forEach((t: any) => types.add(t.call_type))
  return Array.from(types)
    .filter(Boolean)
    .sort((a, b) => callTypeLabel(a).localeCompare(callTypeLabel(b), 'zh-CN'))
})

const activeCallTypeLabel = computed(() => (
  activeTab.value === 'appeal-by-adventurer'
    ? '申诉 AI 专属'
    : callType.value ? callTypeLabel(callType.value) : '全部调用类型'
))

const timeScopeLabel = computed(() => {
  if (!startAtLocal.value) return `最近 ${days.value} 天`
  if (!utc8StartAtParam(startAtLocal.value)) return `最近 ${days.value} 天`
  return `${startAtLocal.value.slice(0, 16).replace('T', ' ')} 至今（UTC+8）`
})

const overviewSuccessRate = computed(() => rowSuccessRate(overview.value || {}))
const overviewAverageTokens = computed(() => rowAverageTokens(overview.value || {}))

const sortedByType = computed(() => sortRows(
  callType.value
    ? byType.value.filter(item => item.call_type === callType.value)
    : byType.value,
  tableSorts.value.byType,
))
const sortedByParty = computed(() => sortRows(byParty.value, tableSorts.value.byParty))
const sortedByExpedition = computed(() => sortRows(
  byExpedition.value,
  tableSorts.value.byExpedition,
))

const recordSortLabels: Record<string, string> = {
  id: 'ID',
  created_at: '时间（UTC+8）',
  call_type: '调用类型',
  model: '模型',
  party_id: '小队',
  expedition_log_id: '探险日志',
  correlation_id: '业务会话',
  prompt_tokens_total: '输入 Token',
  prompt_cache_hit_tokens: '缓存命中',
  prompt_cache_miss_tokens: '缓存未命中',
  completion_tokens: '输出总量',
  completion_reasoning_tokens: '推理 Token',
  visible_reasoning_chars: '可见思考字符',
  total_tokens: '总 Token',
  is_success: '状态',
  invoke_reason: '入队原因',
}

const recordSortLabel = computed(() => recordSortLabels[recordsSortBy.value] || '时间')

const isActiveTabLoading = computed(() => {
  if (activeTab.value === 'overview') return loading.value
  if (activeTab.value === 'by-type') return byTypeLoading.value
  if (activeTab.value === 'by-party') return byPartyLoading.value
  if (activeTab.value === 'by-expedition') return byExpeditionLoading.value
  if (activeTab.value === 'appeal-by-adventurer') return appealLoading.value
  if (activeTab.value === 'records') return recordsLoading.value
  return false
})

function callTypeLabel(ct: string): string {
  return callTypeLabels.value[ct] || ct
}

const sortKeyLabels: Record<string, string> = {
  call_type_label: '调用类型',
  model: '模型',
  party_name: '小队',
  expedition_log_id: '探险日志',
  total_calls: '调用次数',
  success_rate: '物理成功率',
  logical_success_rate: '逻辑链路成功率',
  last_called_at: '最后调用时间',
  prompt_tokens_total: '输入 Token',
  prompt_cache_hit_tokens: '缓存命中',
  prompt_cache_miss_tokens: '缓存未命中',
  completion_tokens: '输出总量',
  completion_content_tokens: '最终内容',
  completion_reasoning_tokens: '推理 Token',
  visible_reasoning_chars: '可见思考字符',
  completion_unattributed_tokens: '未拆分 Token',
  total_tokens: '总 Token',
  avg_tokens: '平均每次',
  cache_hit_rate: '缓存命中率',
}

function rowSuccessRate(row: SortableRow): number {
  const calls = Number(row.total_calls || 0)
  return calls > 0 ? Number(row.success_calls || 0) / calls : 0
}

function rowLogicalSuccessRate(row: SortableRow): number {
  const calls = Number(row.logical_total_calls || 0)
  return calls > 0 ? Number(row.logical_success_calls || 0) / calls : 0
}

function rowAverageTokens(row: SortableRow): number {
  const calls = Number(row.total_calls || 0)
  return calls > 0 ? Math.round(Number(row.total_tokens || 0) / calls) : 0
}

function successRateClass(row: SortableRow): string {
  return Number(row.failed_calls || 0) > 0 ? 'warning' : 'success'
}

function logicalSuccessRateClass(row: SortableRow): string {
  return Number(row.logical_failed_calls || 0) > 0 ? 'warning' : 'success'
}

function logicalChainTitle(row: SortableRow): string {
  const total = Number(row.logical_total_calls || 0)
  const correlated = Number(row.logical_correlated_calls || 0)
  const uncorrelated = Number(row.logical_uncorrelated_calls || 0)
  return [
    `同调用类型共 ${total} 条逻辑链路`,
    `${correlated} 条按业务会话 ID 聚合`,
    `${uncorrelated} 条无关联旧数据按物理记录保留`,
  ].join('；')
}

function sortableValue(row: SortableRow, key: string): string | number | boolean | null {
  if (key === 'success_rate') return rowSuccessRate(row)
  if (key === 'logical_success_rate') return rowLogicalSuccessRate(row)
  if (key === 'avg_tokens') return rowAverageTokens(row)
  if (key === 'call_type_label') return row.call_type_label || callTypeLabel(row.call_type)
  const value = row[key]
  return value == null ? null : value
}

function sortRows(rows: SortableRow[], state: TableSortState): SortableRow[] {
  const direction = state.order === 'asc' ? 1 : -1
  return rows
    .map((row, index) => ({ row, index }))
    .sort((left, right) => {
      const a = sortableValue(left.row, state.key)
      const b = sortableValue(right.row, state.key)
      if (a == null && b == null) return left.index - right.index
      if (a == null) return 1
      if (b == null) return -1
      let comparison = 0
      if (typeof a === 'number' && typeof b === 'number') {
        comparison = a - b
      } else if (typeof a === 'boolean' && typeof b === 'boolean') {
        comparison = Number(a) - Number(b)
      } else {
        comparison = String(a).localeCompare(String(b), 'zh-CN', { numeric: true })
      }
      return comparison === 0 ? left.index - right.index : comparison * direction
    })
    .map(item => item.row)
}

function toggleTableSort(
  table: ClientSortTable,
  key: string,
  initialOrder: SortOrder = 'desc',
) {
  const state = tableSorts.value[table]
  tableSorts.value[table] = {
    key,
    order: state.key === key ? (state.order === 'asc' ? 'desc' : 'asc') : initialOrder,
  }
}

function sortIndicator(table: ClientSortTable, key: string): string {
  const state = tableSorts.value[table]
  if (state.key !== key) return '↕'
  return state.order === 'asc' ? '↑' : '↓'
}

function ariaSort(table: ClientSortTable, key: string): 'none' | 'ascending' | 'descending' {
  const state = tableSorts.value[table]
  if (state.key !== key) return 'none'
  return state.order === 'asc' ? 'ascending' : 'descending'
}

function tableSortSummary(table: ClientSortTable): string {
  const state = tableSorts.value[table]
  return `按${sortKeyLabels[state.key] || state.key}${state.order === 'asc' ? '升序' : '降序'}`
}

function toggleAppealSort(key: string) {
  if (appealSortBy.value === key) {
    appealSortOrder.value = appealSortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    appealSortBy.value = key
    appealSortOrder.value = 'desc'
  }
  void fetchAppealByAdventurer()
}

function appealSortIndicator(key: string): string {
  if (appealSortBy.value !== key) return '↕'
  return appealSortOrder.value === 'asc' ? '↑' : '↓'
}

function appealAriaSort(key: string): 'none' | 'ascending' | 'descending' {
  if (appealSortBy.value !== key) return 'none'
  return appealSortOrder.value === 'asc' ? 'ascending' : 'descending'
}

function toggleRecordSort(key: string, initialOrder: SortOrder = 'desc') {
  if (recordsSortBy.value === key) {
    recordsSortOrder.value = recordsSortOrder.value === 'asc' ? 'desc' : 'asc'
  } else {
    recordsSortBy.value = key
    recordsSortOrder.value = initialOrder
  }
  recordsPage.value = 1
  void fetchRecords()
}

function recordSortIndicator(key: string): string {
  if (recordsSortBy.value !== key) return '↕'
  return recordsSortOrder.value === 'asc' ? '↑' : '↓'
}

function recordAriaSort(key: string): 'none' | 'ascending' | 'descending' {
  if (recordsSortBy.value !== key) return 'none'
  return recordsSortOrder.value === 'asc' ? 'ascending' : 'descending'
}

function applyRecordFilters() {
  recordsPage.value = 1
  void fetchRecords()
}

function focusRecordCorrelation(correlationId: unknown) {
  const normalized = String(correlationId || '').trim()
  if (!normalized) return
  recordFilters.value.correlation_id = normalized
  recordSuccessFilter.value = ''
  callType.value = ''
  recordsSortBy.value = 'created_at'
  recordsSortOrder.value = 'asc'
  applyRecordFilters()
}

function clearRecordCorrelation() {
  recordFilters.value.correlation_id = ''
  applyRecordFilters()
}

function shortCorrelationId(correlationId: unknown): string {
  const normalized = String(correlationId || '').trim()
  if (normalized.length <= 16) return normalized
  return `${normalized.slice(0, 8)}…${normalized.slice(-6)}`
}

function recordRouteLabel(record: SortableRow): string {
  const keySlot = String(record.api_key_slot || '').trim()
  const endpointSlot = String(record.api_endpoint_slot || '').trim()
  if (keySlot && endpointSlot && keySlot !== endpointSlot) {
    return `${keySlot}/${endpointSlot}`
  }
  return keySlot || endpointSlot || '-'
}

const worldOutlineConsumerOutcomes: Record<string, { label: string; className: string; description: string }> = {
  consumer_partial: { label: '部分成功', className: 'status-warning', description: '有效提案已落库，其他提案未获采纳。' },
  consumer_degraded: { label: '已降级', className: 'status-warning', description: '相关内容已保留到 DM 接力便签；有效世界线提案仍可落库。' },
  consumer_warning: { label: '已落库（有警告）', className: 'status-warning', description: '维护结果已落库，消费者记录了警告。' },
  consumer_rejected: { label: '未采纳', className: 'status-fail', description: '世界线提案未获采纳，具体原因见诊断。' },
}

const worldOutlineDiagnosticLabels: Record<string, string> = {
  candidate_scope_unresolved: '无法唯一绑定局部来源',
  candidate_change_unauthorized: '候选未授权这项世界线变化',
  independent_new_capacity_blocked: '独立新线暂未获容量准入',
  ecology_debt_degraded_to_handoff: '相关内容已转入 DM 接力便签',
  ecology_debt_recovered_to_handoff: '已从失败回执恢复到 DM 接力便签',
  world_story_consumer_partial: '部分世界线提案已落库',
  world_story_consumer_rejected: '世界线提案未获采纳',
  world_story_consumer_warning: '世界线落库时存在警告',
  consumer_transaction_failed: '消费事务失败，已回滚',
  world_outline_contract_invalid: '维护输出未通过结构校验',
}

function isWorldOutlineRecord(record: SortableRow): boolean {
  return record.call_type === 'world_outline_maintenance_ai'
    || record.call_type === 'world_outline_cycle_reseed_review_ai'
}

function recordOutcome(record: SortableRow) {
  if (record.is_success) return { label: '成功', className: 'status-ok', description: '结果已完整通过处理。' }
  const outcome = isWorldOutlineRecord(record)
    ? worldOutlineConsumerOutcomes[String(record.failure_stage || '')]
    : undefined
  return outcome || { label: '失败', className: 'status-fail', description: '调用或结果处理失败，具体原因见诊断。' }
}

function recordDiagnosticLabel(record: SortableRow): string {
  if (!record.validation_error_code && !record.failure_stage) return '该记录未保存具体原因'
  return isWorldOutlineRecord(record)
    ? worldOutlineDiagnosticLabels[String(record.validation_error_code || '')] || ''
    : ''
}

function markRefreshed() {
  lastUpdatedAt.value = new Date()
}

function timeRangeParams(): { days?: number; start_at?: string } {
  if (startAtLocal.value) {
    const startAt = utc8StartAtParam(startAtLocal.value)
    if (startAt) return { start_at: startAt }
  }
  return { days: days.value }
}

function utc8StartAtParam(value: string): string | null {
  const match = value.match(/^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})(?::(\d{2}))?$/)
  if (!match) return null
  const candidate = `${match[1]}:${match[2] || '00'}${TOKEN_USAGE_UTC_OFFSET}`
  return Number.isNaN(new Date(candidate).getTime()) ? null : candidate
}

async function fetchCallTypeLabels() {
  try {
    const { data } = await api.get('/adventurer-s1/admin/token-usage/call-types')
    if (data.code === 200 && Array.isArray(data.data)) {
      const map: Record<string, string> = {}
      for (const item of data.data) {
        if (item.call_type && item.label) map[item.call_type] = item.label
      }
      callTypeLabels.value = map
    }
  } catch {
    // 降级：至少保证常见类型可读
    callTypeLabels.value = {
      expedition_ai: '探险主剧情生成',
      expedition_objective_outline_ai: '探险客观局势大纲',
      expedition_resolution_ai: '探险高危主剧情因果底稿',
      expedition_death_resource_review_ai: '探险生命资源一致性复核',
      expedition_observer_review_ai: '探险剧情审核',
      healing_house_ai: '治疗所 AI',
      blacksmith_ai: '铁匠铺 AI',
      alchemy_workshop_ai: '炼金工房 AI',
      data_appeal_ai: '数据申诉 AI',
    }
  }
}

// ── 格式化 ──
function formatTokens(n: number): string {
  if (n == null) return '0'
  if (n >= 1_000_000) return (n / 1_000_000).toFixed(1) + 'M'
  if (n >= 1_000) return (n / 1_000).toFixed(1) + 'K'
  return n.toString()
}

function formatPercent(value: number): string {
  return `${(Number(value || 0) * 100).toFixed(1)}%`
}

function formatRefreshTime(value: Date): string {
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: TOKEN_USAGE_TIME_ZONE,
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(value)
}

function formatUtc8RecordTime(value: string | null | undefined): string {
  if (!value) return '-'
  const isoValue = value.includes('T') ? value : value.replace(' ', 'T')
  const hasTimezone = /(?:Z|[+-]\d{2}:\d{2})$/.test(isoValue)
  const parsed = new Date(hasTimezone ? isoValue : `${isoValue}Z`)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    timeZone: TOKEN_USAGE_TIME_ZONE,
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  }).format(parsed).replace(/\//g, '-')
}

function shortDate(dateStr: string): string {
  const parts = dateStr.split('-')
  return `${parts[1]}/${parts[2]}`
}

function dailyBarWidth(tokens: number): number {
  if (!overview.value?.daily?.length) return 0
  const maxTokens = Math.max(...overview.value.daily.map((d: any) => d.total_tokens), 1)
  return (tokens / maxTokens) * 100
}

function appealDailyBarWidth(tokens: number): number {
  if (!appealStats.value?.summary?.daily?.length) return 0
  const maxTokens = Math.max(...appealStats.value.summary.daily.map((d: any) => d.total_tokens), 1)
  return (tokens / maxTokens) * 100
}

function clearAppealDate() {
  appealDate.value = ''
  fetchAppealByAdventurer()
}

// ── 数据拉取 ──
async function fetchOverview() {
  loading.value = true
  try {
    const params: any = { ...timeRangeParams() }
    if (callType.value) params.call_type = callType.value
    const { data } = await api.get('/adventurer-s1/admin/token-usage', { params })
    if (data.code === 200) {
      overview.value = data.data
      markRefreshed()
    }
    loadedTabs.value.add('overview')
  } finally {
    loading.value = false
  }
}

async function fetchByType() {
  byTypeLoading.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/token-usage/by-type', {
      params: timeRangeParams(),
    })
    if (data.code === 200) {
      byType.value = data.data
      markRefreshed()
    }
    loadedTabs.value.add('by-type')
  } finally {
    byTypeLoading.value = false
  }
}

async function fetchByParty() {
  byPartyLoading.value = true
  try {
    const params: any = { ...timeRangeParams() }
    if (callType.value) params.call_type = callType.value
    const { data } = await api.get('/adventurer-s1/admin/token-usage/by-party', { params })
    if (data.code === 200) {
      byParty.value = data.data
      markRefreshed()
    }
    loadedTabs.value.add('by-party')
  } finally {
    byPartyLoading.value = false
  }
}

async function fetchByExpedition() {
  byExpeditionLoading.value = true
  try {
    const params: any = { ...timeRangeParams() }
    if (callType.value) params.call_type = callType.value
    const { data } = await api.get('/adventurer-s1/admin/token-usage/by-expedition', { params })
    if (data.code === 200) {
      byExpedition.value = data.data
      markRefreshed()
    }
    loadedTabs.value.add('by-expedition')
  } finally {
    byExpeditionLoading.value = false
  }
}

async function fetchAppealByAdventurer() {
  appealLoading.value = true
  try {
    const params: any = {
      ...timeRangeParams(),
      sort_by: appealSortBy.value,
      sort_order: appealSortOrder.value,
    }
    if (appealDate.value) params.date = appealDate.value
    const { data } = await api.get('/adventurer-s1/admin/token-usage/appeal-by-adventurer', { params })
    if (data.code === 200) {
      appealStats.value = data.data
      markRefreshed()
    }
    loadedTabs.value.add('appeal-by-adventurer')
  } finally {
    appealLoading.value = false
  }
}

async function runAppealLinkBackfill() {
  if (!confirm('为历史申诉 token 记录补写 appeal_id，并从工单复制 adventurer_id，是否继续？')) return
  appealBackfillLoading.value = true
  try {
    const { data } = await api.post('/adventurer-s1/admin/token-usage/appeal-link-backfill')
    if (data.code === 200) {
      await fetchAppealByAdventurer()
    }
  } finally {
    appealBackfillLoading.value = false
  }
}

async function fetchRecords() {
  const realm = getS1AdminRealmTarget()
  recordsLoading.value = true
  try {
    const params: any = {
      page: recordsPage.value,
      per_page: recordsPerPage.value,
      ...timeRangeParams(),
      sort_by: recordsSortBy.value,
      sort_order: recordsSortOrder.value,
    }
    if (callType.value) params.call_type = callType.value
    if (recordFilters.value.party_id) params.party_id = recordFilters.value.party_id
    if (recordFilters.value.expedition_log_id) params.expedition_log_id = recordFilters.value.expedition_log_id
    if (recordFilters.value.correlation_id) params.correlation_id = recordFilters.value.correlation_id
    if (recordSuccessFilter.value) params.is_success = recordSuccessFilter.value
    const { data } = await api.get('/adventurer-s1/admin/token-usage/records', { params, s1AdminRealmTarget: realm })
    if (realm !== getS1AdminRealmTarget()) return
    if (data.code === 200) {
      failureRecordId.value = null
      recordsRealm.value = realm
      records.value = data.data.records
      recordsTotal.value = data.data.total
      recordsPages.value = data.data.pages
      recordsPage.value = data.data.page
      recordsSortBy.value = data.data.sort_by || recordsSortBy.value
      recordsSortOrder.value = data.data.sort_order || recordsSortOrder.value
      markRefreshed()
    }
    loadedTabs.value.add('records')
  } finally {
    recordsLoading.value = false
  }
}

function fetchActiveTab() {
  if (activeTab.value === 'overview') return fetchOverview()
  if (activeTab.value === 'by-type') return fetchByType()
  if (activeTab.value === 'by-party') return fetchByParty()
  if (activeTab.value === 'by-expedition') return fetchByExpedition()
  if (activeTab.value === 'appeal-by-adventurer') return fetchAppealByAdventurer()
  if (activeTab.value === 'records') return fetchRecords()
}

function formatOptionalTokens(n: number | null | undefined): string {
  return n == null ? '—' : formatTokens(n)
}

function handleGlobalFilterChange() {
  loadedTabs.value = new Set()
  recordsPage.value = 1
  fetchActiveTab()
}

function handlePresetDaysChange() {
  startAtLocal.value = ''
  handleGlobalFilterChange()
}

function handleExactStartChange() {
  handleGlobalFilterChange()
}

function clearExactStart() {
  startAtLocal.value = ''
  handleGlobalFilterChange()
}

function selectTab(tabKey: string) {
  activeTab.value = tabKey
  if (!loadedTabs.value.has(tabKey)) {
    fetchActiveTab()
  }
}

onMounted(async () => {
  await fetchCallTypeLabels()
  fetchActiveTab()
})
</script>

<style scoped lang="scss">
$spacing-xs: 4px;
$spacing-sm: 8px;
$spacing-md: 16px;
$spacing-lg: 24px;
$font-xs: 12px;
$font-sm: 14px;

.token-usage-view {
  max-width: 1600px;
  margin: 0 auto;
  padding: $spacing-md;
}

.page-header {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: $spacing-lg;
  margin-bottom: $spacing-md;
}

.page-eyebrow {
  margin-bottom: 4px;
  color: var(--color-primary, #1890ff);
  font-size: $font-xs;
  font-weight: 700;
  letter-spacing: 0.12em;
}

.page-title {
  margin: 0;
  font-weight: 700;
  color: var(--color-text);
  font-size: 1.35rem;
}

.page-description {
  margin: 6px 0 0;
  color: var(--color-text-secondary);
  font-size: $font-sm;
}

.page-meta {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: $spacing-xs;
  flex-wrap: wrap;
}

.scope-pill {
  padding: 4px 10px;
  border: 1px solid color-mix(in srgb, var(--color-primary, #1890ff) 28%, var(--color-border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-primary, #1890ff) 8%, transparent);
  color: var(--color-text);
  font-size: $font-xs;
  white-space: nowrap;
}

.updated-at {
  color: var(--color-text-secondary);
  font-size: $font-xs;
  white-space: nowrap;
}

.filter-bar {
  display: flex;
  gap: $spacing-md;
  align-items: center;
  margin-bottom: $spacing-md;
  padding: $spacing-sm $spacing-md;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: var(--color-bg-secondary, var(--color-bg));
  flex-wrap: wrap;
}

.filter-group {
  display: flex;
  align-items: center;
  gap: $spacing-xs;

  label {
    font-size: $font-sm;
    color: var(--color-text-secondary);
    white-space: nowrap;
  }

  select, input {
    padding: 4px 8px;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-bg);
    color: var(--color-text);
    font-size: $font-sm;

    &:disabled {
      opacity: 0.6;
      cursor: not-allowed;
    }
  }

  input[type="number"] {
    width: 100px;
  }
}

.refresh-btn {
  margin-left: auto;
  padding: 6px 14px;
  border: 1px solid var(--color-primary, #1890ff);
  border-radius: 6px;
  background: var(--color-primary, #1890ff);
  color: #fff;
  cursor: pointer;
  font-size: $font-sm;
  font-weight: 600;
  &:hover:not(:disabled) { filter: brightness(1.06); }
  &:disabled { opacity: 0.5; cursor: not-allowed; }
}

.filter-note {
  color: var(--color-text-secondary);
  font-size: $font-xs;
  white-space: nowrap;
}

.tab-bar {
  display: flex;
  gap: 0;
  margin-bottom: $spacing-md;
  border-bottom: 2px solid var(--color-border);
  overflow-x: auto;
  scrollbar-width: thin;
}

.tab-btn {
  background: none;
  border: none;
  border-bottom: 2px solid transparent;
  margin-bottom: -2px;
  padding: $spacing-sm $spacing-md;
  font-size: $font-sm;
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.15s;
  white-space: nowrap;
  &:hover { color: var(--color-text); }
  &.active {
    color: var(--color-primary);
    border-bottom-color: var(--color-primary);
  }
}

.tab-content {
  margin-top: $spacing-md;
}

.stats-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: $spacing-md;
  margin-bottom: $spacing-lg;
}

.stat-card {
  min-width: 0;
  background: var(--color-bg-secondary, var(--color-bg));
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: $spacing-md;
  text-align: left;
}

.stat-value {
  font-size: 1.5rem;
  font-weight: 700;
  color: var(--color-text);
  &.success { color: var(--color-success, #52c41a); }
  &.danger { color: var(--color-danger, #ff4d4f); }
  &.primary { color: var(--color-primary, #1890ff); }
  &.cache { color: #722ed1; }
}

.stat-label {
  font-size: $font-xs;
  color: var(--color-text-secondary);
  margin-top: 4px;
}

.stat-note {
  margin-top: 2px;
  color: var(--color-text-secondary);
  font-size: $font-xs;
  opacity: 0.8;
}

.detail-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: $spacing-md;
}

@media (max-width: 900px) {
  .detail-grid { grid-template-columns: 1fr; }
}

.detail-card {
  background: var(--color-bg-secondary, var(--color-bg));
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: $spacing-md;

  h5 {
    margin: 0 0 $spacing-sm;
    font-size: $font-sm;
    color: var(--color-text);
  }
}

.detail-rows {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.detail-row {
  display: flex;
  justify-content: space-between;
  font-size: $font-sm;
  color: var(--color-text);
}

.daily-chart {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 300px;
  overflow-y: auto;
}

.daily-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: $font-xs;
}

.daily-date {
  width: 48px;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.daily-bar-track {
  flex: 1;
  height: 14px;
  background: var(--color-border);
  border-radius: 3px;
  overflow: hidden;
}

.daily-bar-fill {
  height: 100%;
  background: linear-gradient(90deg, var(--color-primary, #1890ff), #36cfc9);
  border-radius: 3px;
  transition: width 0.3s;
}

.daily-value {
  width: 60px;
  text-align: right;
  font-weight: 600;
  flex-shrink: 0;
}

.daily-calls {
  width: 36px;
  text-align: right;
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.data-table-wrapper {
  overflow: auto;
  max-height: min(70vh, 760px);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
}

.data-table {
  width: 100%;
  min-width: 860px;
  border-collapse: collapse;
  font-size: $font-sm;

  th, td {
    padding: 8px 12px;
    border-bottom: 1px solid var(--color-border);
    text-align: left;
  }

  th {
    font-weight: 600;
    color: var(--color-text-secondary);
    background: var(--color-bg-secondary, var(--color-bg));
    position: sticky;
    top: 0;
    z-index: 1;
    white-space: nowrap;
  }

  th.num, td.num {
    text-align: right;
  }

  tbody tr:hover {
    background: var(--color-bg-secondary, var(--color-bg));
  }
}

.records-table {
  min-width: 2080px;
}

.records-scope-note {
  margin: 0 0 $spacing-sm;
  color: var(--color-text-secondary);
  font-size: $font-xs;
}

.session-filter {
  input {
    min-width: 260px;
  }
}

.clear-session-btn,
.session-link {
  border: 0;
  background: transparent;
  color: var(--color-primary, #1890ff);
  cursor: pointer;
}

.clear-session-btn {
  padding: 2px 4px;
}

.session-link {
  max-width: 150px;
  padding: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-family: monospace;

  &:hover,
  &:focus-visible {
    text-decoration: underline;
  }
}

.route-cell {
  white-space: nowrap;
}

.request-id-cell {
  max-width: 220px;

  code {
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
}

.sort-header-btn {
  display: inline-flex;
  align-items: center;
  justify-content: inherit;
  gap: 4px;
  width: 100%;
  padding: 0;
  border: 0;
  background: transparent;
  color: inherit;
  font: inherit;
  font-weight: inherit;
  cursor: pointer;
  white-space: nowrap;

  span {
    min-width: 1em;
    color: var(--color-primary, #1890ff);
    font-size: $font-xs;
  }

  &:hover,
  &:focus-visible {
    color: var(--color-text);
  }
}

th.num .sort-header-btn {
  justify-content: flex-end;
}

.table-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: $spacing-sm;
  margin-bottom: $spacing-xs;
  color: var(--color-text-secondary);
  font-size: $font-xs;
}

.table-toolbar-inline {
  padding: 8px 12px 0;
}

.table-hint {
  text-align: right;
}

.cell-note {
  display: block;
  margin-top: 1px;
  color: var(--color-text-secondary);
  font-size: 10px;
}

.members-cell {
  max-width: 200px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-text-secondary);
  font-size: $font-xs;
}

.type-badge {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  background: var(--color-primary, #1890ff);
  color: #fff;
  font-size: $font-xs;
  white-space: nowrap;
}

.success { color: var(--color-success, #52c41a); }
.warning { color: #faad14; }
.danger { color: var(--color-danger, #ff4d4f); }
.bold { font-weight: 700; }
.dim { color: var(--color-text-secondary); }

.status-ok {
  color: var(--color-success, #52c41a);
  font-weight: 600;
}

.status-warning {
  color: var(--color-warning, #d48806);
  font-weight: 600;
}

.status-fail {
  color: var(--color-danger, #ff4d4f);
  font-weight: 600;
}

.records-filter {
  display: flex;
  gap: $spacing-md;
  margin-bottom: $spacing-md;
  padding: $spacing-sm $spacing-md;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg-secondary, var(--color-bg));
  flex-wrap: wrap;
}

.pagination {
  display: flex;
  justify-content: center;
  align-items: center;
  gap: $spacing-md;
  margin-top: $spacing-md;
  font-size: $font-sm;

  button {
    padding: 4px 12px;
    border: 1px solid var(--color-border);
    border-radius: 4px;
    background: var(--color-bg);
    color: var(--color-text);
    cursor: pointer;
    &:hover:not(:disabled) { background: var(--color-border); }
    &:disabled { opacity: 0.4; cursor: not-allowed; }
  }

  span { color: var(--color-text-secondary); }
}

.loading-text {
  text-align: center;
  color: var(--color-text-secondary);
  padding: $spacing-lg;
}

.empty-text {
  text-align: center;
  color: var(--color-text-secondary);
  padding: $spacing-lg;
}

.appeal-filter-bar {
  display: flex;
  gap: $spacing-md;
  align-items: center;
  margin-bottom: $spacing-md;
  flex-wrap: wrap;
}

.appeal-metric-note {
  margin-bottom: $spacing-md;
  font-size: $font-xs;
  color: var(--color-text-secondary);
}

.appeal-hint {
  margin-bottom: $spacing-md;
  padding: 8px 12px;
  border-radius: 6px;
  background: rgba(250, 173, 20, 0.12);
  border: 1px solid rgba(250, 173, 20, 0.35);
  color: var(--color-text-secondary);
  font-size: $font-sm;

  &.warn {
    background: rgba(255, 77, 79, 0.08);
    border-color: rgba(255, 77, 79, 0.35);
  }
}

.link-btn-sm {
  border: none;
  background: none;
  color: var(--color-primary, #1890ff);
  cursor: pointer;
  font-size: $font-xs;
  padding: 0 4px;
}

.daily-bar-fill.appeal-bar {
  background: linear-gradient(90deg, #722ed1, #eb2f96);
}

.daily-avg {
  width: 56px;
  text-align: right;
  color: var(--color-text-secondary);
  font-size: $font-xs;
  flex-shrink: 0;
}

.daily-mini-cell {
  max-width: 280px;
}

.daily-mini-tag {
  display: inline-block;
  margin: 2px 4px 2px 0;
  padding: 1px 6px;
  border-radius: 8px;
  background: var(--color-bg-secondary, var(--color-bg));
  border: 1px solid var(--color-border);
  font-size: $font-xs;
  white-space: nowrap;
}

.invoke-reason-cell {
  max-width: 120px;
  font-size: $font-xs;
}

@media (max-width: 720px) {
  .token-usage-view {
    padding: $spacing-sm;
  }

  .page-header {
    align-items: flex-start;
    flex-direction: column;
    gap: $spacing-sm;
  }

  .page-meta {
    justify-content: flex-start;
  }

  .filter-bar,
  .records-filter,
  .appeal-filter-bar {
    align-items: stretch;
    gap: $spacing-sm;
  }

  .filter-group {
    flex: 1 1 150px;
    justify-content: space-between;

    select,
    input {
      min-width: 0;
      max-width: 180px;
      flex: 1;
    }
  }

  .refresh-btn {
    width: 100%;
    margin-left: 0;
  }

  .stats-row {
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: $spacing-sm;
  }

  .stat-card {
    padding: $spacing-sm;
  }

  .stat-value {
    font-size: 1.2rem;
  }

  .table-toolbar {
    align-items: flex-start;
    flex-direction: column;
  }

  .table-hint {
    text-align: left;
  }

  .daily-avg {
    display: none;
  }
}
</style>
