<template>
  <div class="advs1 s1-mobile-surface">
    <header class="advs1-header">
      <div class="advs1-header-copy">
        <span class="advs1-kicker">ADVENTURER OUTPOST · SEASON I</span>
        <h2 class="advs1-title">
          <span class="advs1-sigil">⚔</span>
          深渊酒馆 <em>S1</em>
        </h2>
        <p class="advs1-subtitle">集结冒险者，建设共同驻地；从每一次远征中带回财富、伤痕与新的故事。</p>
      </div>
      <div class="advs1-beacon" aria-hidden="true"><span></span></div>
    </header>

    <TeamLootNotice
      :enabled="homeInitialReadFinished && !homeLoadPending && !homeLoadError && !isRealmVisitor && currentUserId != null"
      :on-logs="activeTab === 'logs'" :user-id="currentUserId"
      :realm="viewedRealmCode" @open="activeTab = 'logs'"
    />

    <div v-if="homeLoadError || homeRetryNotice" class="notice-banner" role="status" aria-live="polite">
      <p class="notice-banner-text">{{ homeLoadError || homeRetryNotice }}</p>
      <button v-if="homeLoadError" type="button" class="btn-primary btn-sm notice-banner-btn"
        :disabled="homeLoadPending" @click="initializeHome(true)">
        {{ homeLoadPending ? '正在读取…' : '重新读取' }}
      </button>
    </div>

    <div v-if="store.noticeShouldShow && store.notice" class="notice-banner">
      <div class="notice-banner-body">
        <strong>📢 酒馆通知</strong>
        <p class="notice-banner-text">{{ store.notice }}</p>
      </div>
      <button
        type="button"
        class="btn-primary btn-sm notice-banner-btn"
        :disabled="store.noticeAckLoading || isRealmVisitor"
        @click="confirmNotice"
      >{{ isRealmVisitor ? '访客只读' : '我知道了' }}</button>
    </div>

    <S1BetaNotice @acknowledge="focusActiveTab" />

    <div ref="tabAnchor" class="tab-anchor" aria-hidden="true"></div>
    <nav ref="tabNavigation" class="tabs" aria-label="深渊酒馆栏目">
      <button
        v-for="t in TABS"
        :key="t.id"
        type="button"
        :class="['tab-btn', { active: activeTab === t.id }]"
        :aria-current="activeTab === t.id ? 'page' : undefined"
        @click="switchTab(t.id)"
      >
        <span class="tab-btn-icon" aria-hidden="true">{{ t.icon }}</span>
        <span class="tab-btn-label">{{ t.label }}</span>
        <span
          v-if="t.id === 'building' && showBuildingTaskHint"
          class="tab-task-count"
        >{{ taskBoardHintBadgeCount }}</span>
      </button>
    </nav>

    <AmbientNpcEncounter :surface="ambientNpcSurface" />

    <!-- ───────── 酒馆：大堂座位 + 闲聊 ───────── -->
    <section v-show="activeTab === 'tavern'" class="tab-content">
      <TavernUpgradePanel v-if="showTavernUpgradeUi" />
      <div class="tavern-layout">
        <!-- 大堂：吧台 + 桌子 -->
        <div class="tavern-hall">
          <div class="tavern-hall-heading">
            <h3 class="col-title">酒馆大堂</h3>
          </div>
          <div v-if="store.tavernSeatsLoading && store.tavernSeats.length === 0" class="empty">加载中…</div>
          <template v-else>
            <div class="hall-bar">
              <div class="bar-counter">🍻 吧台</div>
              <div class="bar-stools">
                <div
                  v-for="seat in barSeats"
                  :key="seat.id"
                  :class="seatClass(seat)"
                  :title="seat.label"
                  @click="onSeatClick(seat)"
                >
                  <template v-if="seat.adventurer">
                    <div class="seat-name">{{ seat.adventurer.name }}</div>
                    <div v-if="seat.drink" class="seat-drink">{{ seat.drink.icon }} {{ seat.drink.name }}</div>
                    <div v-else class="seat-drink seat-drink-none">还没点饮品</div>
                    <div v-if="seat.food" class="seat-food">
                      {{ seat.food.icon }} {{ seat.food.name }} · {{ foodRemainingLabel(seat) }}
                    </div>
                    <p v-if="seat.drink?.text" class="seat-drink-text">{{ seat.drink.text }}</p>
                    <p v-if="seat.food" class="seat-drink-text">{{ foodProgressText(seat) }}</p>
                    <p v-if="seat.chatter" class="seat-chatter">{{ seat.chatter }}</p>
                    <div
                      v-for="req in (seat.incoming_treat_requests ?? [])"
                      :key="req.id"
                      class="treat-request"
                      @click.stop
                    >
                      <span>{{ req.payer_name || '有人' }} 请你喝 {{ req.drink?.icon }} {{ req.drink?.name }}</span>
                      <button type="button" :disabled="seatActionLoading" @click.stop.prevent="doRespondTreat(req.id, true)">接受</button>
                      <button type="button" :disabled="seatActionLoading" @click.stop.prevent="doRespondTreat(req.id, false)">拒绝</button>
                    </div>
                  </template>
                  <template v-else>
                    <div class="seat-empty-icon">🪑</div>
                    <div class="seat-empty-label">空位</div>
                  </template>
                </div>
              </div>
            </div>
            <div class="hall-tables">
              <div v-for="t in tavernTables" :key="t.key" class="hall-table">
                <div class="table-name">{{ t.label }}</div>
                <div class="table-seats">
                  <div
                    v-for="seat in t.seats"
                    :key="seat.id"
                    :class="seatClass(seat)"
                    :title="seat.label"
                    @click="onSeatClick(seat)"
                  >
                    <template v-if="seat.adventurer">
                      <div class="seat-name">{{ seat.adventurer.name }}</div>
                    <div v-if="seat.drink" class="seat-drink">{{ seat.drink.icon }} {{ seat.drink.name }}</div>
                    <div v-else class="seat-drink seat-drink-none">还没点饮品</div>
                    <div v-if="seat.food" class="seat-food">
                      {{ seat.food.icon }} {{ seat.food.name }} · {{ foodRemainingLabel(seat) }}
                    </div>
                    <p v-if="seat.drink?.text" class="seat-drink-text">{{ seat.drink.text }}</p>
                    <p v-if="seat.food" class="seat-drink-text">{{ foodProgressText(seat) }}</p>
                    <p v-if="seat.chatter" class="seat-chatter">{{ seat.chatter }}</p>
                      <div
                        v-for="req in (seat.incoming_treat_requests ?? [])"
                        :key="req.id"
                        class="treat-request"
                        @click.stop
                      >
                        <span>{{ req.payer_name || '有人' }} 请你喝 {{ req.drink?.icon }} {{ req.drink?.name }}</span>
                        <button type="button" :disabled="seatActionLoading" @click.stop.prevent="doRespondTreat(req.id, true)">接受</button>
                        <button type="button" :disabled="seatActionLoading" @click.stop.prevent="doRespondTreat(req.id, false)">拒绝</button>
                      </div>
                    </template>
                    <template v-else>
                      <div class="seat-empty-icon">🪑</div>
                      <div class="seat-empty-label">空位</div>
                    </template>
                  </div>
                </div>
              </div>
            </div>
            <p class="hall-hint">点击空位让你的冒险者入座；点自己的冒险者可以点酒 / 离座；点别人的冒险者可以请对方喝东西，对方接受后才扣请客人的金币。在座冒险者偶尔会冒出几句闲聊。</p>
          </template>
        </div>

        <!-- 聊天侧栏 -->
        <div class="chat-col tavern-chat">
          <h3 class="col-title">酒馆闲聊</h3>
        <div ref="chatContainer" class="chat-messages">
          <button v-if="store.chatHasNext" type="button" class="load-more" @click="loadMoreChat">加载更早消息</button>
          <div v-if="chatDisplay.length === 0" class="empty">还没有人发言，来说点什么吧</div>
          <div v-for="m in chatDisplay" :key="m.id" class="chat-msg">
            <div class="chat-msg-head">
              <span class="chat-nick">
                <span v-if="m.active_title" class="chat-title" v-html="renderTitle(m.active_title)"></span>
                {{ m.nickname }}
              </span>
              <small class="chat-time">{{ m.created_at ? formatTime(m.created_at) : '' }}</small>
            </div>
            <p class="chat-content">{{ m.content }}</p>
          </div>
        </div>
        <div class="chat-input-area">
          <textarea
            v-model="chatInput"
            class="chat-input"
            rows="2"
            :maxlength="CHAT_MAX"
            placeholder="说点什么吧…（最多 300 字，5秒冷却）"
          ></textarea>
          <div class="chat-input-foot">
            <small class="char-count">{{ chatInput.length }}/{{ CHAT_MAX }}</small>
            <button type="button" class="btn-primary btn-sm" :disabled="!chatInput.trim() || sending" @click="sendChat">
              {{ sending ? '发送中…' : '发送' }}
            </button>
          </div>
        </div>
        </div>
      </div>
    </section>

    <!-- ───────── 探险日志 ───────── -->
    <section v-show="activeTab === 'logs'" class="tab-content">
      <TeamLootPanel v-if="activeTab === 'logs'" ref="teamLootPanel" />
      <h3 class="col-title">探险日志</h3>
      <div class="log-toolbar">
        <input
          v-model="logSearch"
          type="text"
          class="log-search-input"
          placeholder="搜索日志…"
          @input="onLogSearchInput"
        />
        <select v-model="logStatusFilter" class="log-status-select" @change="onLogFilterChange">
          <option value="exclude_failed">隐藏失败</option>
          <option value="">全部状态（含失败）</option>
          <option value="completed">已完成</option>
          <option value="failed">失败</option>
          <option value="in_progress">进行中</option>
          <option value="generating">生成中</option>
        </select>
        <select v-model="logCategoryFilter" class="log-status-select" @change="onLogFilterChange">
          <option value="">全部日志</option>
          <option value="special">✨ 特殊事件</option>
          <option value="favorite">⭐ 我的收藏</option>
        </select>
      </div>
      <div v-if="store.logsLoading && store.logs.length === 0" class="empty">加载中…</div>
      <div v-else-if="store.logs.length === 0" class="empty">
        {{ logStatusFilter === 'exclude_failed' ? '暂无非失败日志，可从状态选项查看失败日志' : '暂无探险日志' }}
      </div>
      <div v-else class="log-list">
        <div
          v-for="log in store.logs"
          :key="log.id"
          class="log-card"
          :class="isSpecialEventPresentationStatus(log.status) ? specialEventCardClass(log.special_event_theme) : null"
          :style="isSpecialEventPresentationStatus(log.status) ? specialEventStyle(log.special_event_theme) : undefined"
          @click="openLog(log.id)"
        >
          <div class="log-card-head">
            <span class="log-where">
              <span v-if="log.party_name" class="log-party">🏰 {{ log.party_name }}</span>
              <span v-if="log.status === 'in_progress' || log.status === 'completed'">🗺️ {{ log.temporary_location_name || log.hidden_dungeon || log.target_dungeon }}</span>
            </span>
            <span class="log-status-wrap">
              <span class="log-status" :class="'st-' + log.status">{{ logStatusLabel(log.status) }}</span>
            </span>
          </div>
          <span
            v-if="isSpecialEventPresentationStatus(log.status) && getSpecialEventPresentation(log.special_event_theme)"
            class="log-special-event"
          >
            {{ getSpecialEventPresentation(log.special_event_theme)?.icon }}
            {{ getSpecialEventPresentation(log.special_event_theme)?.label }}
          </span>
          <p v-if="log.result_summary" class="log-summary">{{ log.result_summary }}</p>
          <div class="log-card-meta">
            <span>日志 #{{ log.id }}</span>
            <span v-if="log.created_at">{{ formatDateTime(log.created_at) }}</span>
            <button
              type="button"
              class="log-favorite-btn"
              :class="{ active: log.is_favorited }"
              :disabled="logFavoriteLoading === log.id"
              :aria-pressed="log.is_favorited"
              :title="log.is_favorited ? '取消收藏' : '收藏日志'"
              @click.stop="toggleLogFavorite(log)"
            >
              {{ logFavoriteLoading === log.id ? '…' : (log.is_favorited ? '★ 已收藏' : '☆ 收藏') }}
            </button>
          </div>
          <div v-if="isAdmin" class="log-admin-actions" @click.stop>
              <button
                v-if="log.status === 'in_progress'"
                type="button"
                class="btn-secondary btn-sm"
                :disabled="logActionLoading === log.id"
                @click.stop="doStepExpeditionLog(log.id)"
              >
                逐步推送
              </button>
              <button
                v-if="log.status === 'in_progress'"
                type="button"
                class="btn-danger btn-sm"
                :disabled="logActionLoading === log.id"
                @click.stop="doForceCompleteExpeditionLog(log.id)"
              >
                直接结算
              </button>
              <button
                v-if="log.status === 'failed'"
                type="button"
                class="btn-warning btn-sm"
                :disabled="logActionLoading === log.id"
                @click.stop="doRegenerateExpeditionLog(log.id)"
              >
                重新生成
              </button>
              <button
                v-if="canRetryGeneratingLog(log)"
                type="button"
                class="btn-warning btn-sm"
                :disabled="logActionLoading === log.id"
                @click.stop="doRetryGeneratingExpeditionLog(log.id)"
              >
                重试生成
              </button>
            </div>
        </div>
      </div>
      <Pagination v-if="store.logsPagination" :pagination="store.logsPagination" @go="goLogPage" />
    </section>

    <!-- ───────── 建筑：分区入口 ───────── -->
    <section v-show="activeTab === 'building' && !showDormitory && !showAdmin && !showBuildingAdmin && !showLuckyWheelAdmin && !showItemCatalogAdmin && !currentZone" class="tab-content">
      <TavernUpgradePanel v-if="showTavernUpgradeUi" />
      <div class="building-grid building-grid--zones">
        <div
          v-for="zone in BUILDING_ZONES"
          :key="zone.category"
          :class="['building-card', 'zone-card', { locked: isZoneEntryLocked(zone.category) && !isAdmin }]"
          @click="openZone(zone.category)"
        >
          <div class="building-icon zone-icon-wrap">
            {{ zone.icon }}
            <span
              v-if="zone.category === 'task' && showBuildingTaskHint"
              class="zone-task-count"
            >{{ taskBoardHintBadgeCount }}</span>
            <span
              v-if="zone.category === 'auction' && (store.secretAuctionOpenCount ?? 0) > 0"
              class="zone-task-count"
            >{{ store.secretAuctionOpenCount }}</span>
          </div>
          <div class="building-name">{{ zone.label }}</div>
          <div class="building-desc">{{ zoneSummary(zone.category) }}</div>
        </div>
        <div v-if="isAdmin" class="building-card admin-card" @click="showAdmin = true">
          <div class="building-icon">📋</div>
          <div class="building-name">预约管理</div>
          <div class="building-desc">审核与抽取冒险者</div>
        </div>
        <div v-if="isAdmin" class="building-card admin-card" @click="showBuildingAdmin = true">
          <div class="building-icon">🏗️</div>
          <div class="building-name">建筑管理</div>
          <div class="building-desc">管理建筑模板</div>
        </div>
        <div v-if="isAdmin" class="building-card admin-card" @click="showLuckyWheelAdmin = true">
          <div class="building-icon">🎡</div>
          <div class="building-name">转盘管理</div>
          <div class="building-desc">管理大转盘奖池</div>
        </div>
        <div v-if="isAdmin" class="building-card admin-card" @click="showItemCatalogAdmin = true">
          <div class="building-icon">📚</div>
          <div class="building-name">图鉴管理</div>
          <div class="building-desc">编辑物品图鉴</div>
        </div>
      </div>
    </section>

    <!-- ───────── 建筑：分区子视图（建筑分类） ───────── -->
    <section v-show="activeTab === 'building' && currentZone && currentZone !== 'dormitory' && currentZone !== 'market' && currentZone !== 'auction' && currentZone !== 'task'" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="currentZone = null">← 返回建筑</button>
        <h3 class="col-title" style="margin: 0;">
          {{ currentZoneInfo?.icon }} {{ currentZoneInfo?.label }}
        </h3>
      </div>
      <div v-if="store.buildingsLoading && currentZone && groupedBuildings[currentZone].length === 0" class="empty">加载中…</div>
      <div v-else-if="currentZone && currentZone !== 'task' && groupedBuildings[currentZone].length === 0" class="empty">
          该区暂无建筑{{ isAdmin ? '，可在「建筑管理」中创建' : '' }}
        </div>
      <div v-else-if="currentZone && currentZone !== 'task'" class="building-grid building-grid--facilities">
          <BuildingCard
            v-for="b in groupedBuildings[currentZone]"
            :key="b.id"
            :building="b"
            @click="openBuildingDetail(b)"
          />
        </div>
    </section>

    <!-- ───────── 个人交易市场（仅已建成后进入交易页） ───────── -->
    <section v-if="activeTab === 'building' && currentZone === 'market'" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="currentZone = null">← 返回建筑</button>
        <h3 class="col-title" style="margin: 0;">🏪 个人交易市场</h3>
      </div>
      <MarketPanel />
    </section>

    <!-- ───────── 隐秘拍卖行（仅已建成后进入） ───────── -->
    <section v-if="activeTab === 'building' && currentZone === 'auction'" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="currentZone = null">← 返回建筑</button>
        <h3 class="col-title" style="margin: 0;">🎭 隐秘拍卖行</h3>
      </div>
      <SecretAuctionPanel />
    </section>

    <!-- ───────── 建筑：任务栏 + 悬赏布告栏 ───────── -->
    <section v-if="activeTab === 'building' && currentZone === 'task'" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="currentZone = null">← 返回建筑</button>
        <h3 class="col-title" style="margin: 0;">📜 任务与世界事件</h3>
      </div>
      <div class="commission-subtabs">
        <button type="button" :class="{ active: taskZoneTab === 'task' }" @click="taskZoneTab = 'task'">📜 任务栏</button>
        <button type="button" :class="{ active: taskZoneTab === 'commission' }" @click="taskZoneTab = 'commission'">📋 悬赏布告栏</button>
        <button type="button" :class="{ active: taskZoneTab === 'campaign' }" @click="taskZoneTab = 'campaign'">🗺️ 战役议事厅</button>
      </div>
      <CampaignHallPanel v-if="taskZoneTab === 'campaign'" @accepted="onCampaignAccepted" @open-log="openLog" />
      <div v-else-if="store.buildingsLoading && !getTaskBoardBuilding()" class="empty">加载中…</div>
      <div v-else-if="!getTaskBoardBuilding()" class="empty">
        任务栏尚未建造{{ isAdmin ? '，可在「建筑管理」中创建' : '' }}
      </div>
      <template v-else>
        <BuildingCard v-if="getTaskBoardBuilding()?.status !== 'built'" :building="getTaskBoardBuilding()!" @click="openBuildingDetail(getTaskBoardBuilding()!)" />
        <TaskBoardPanel
          v-else-if="taskZoneTab === 'task'"
          :building="getTaskBoardBuilding()!"
          @refreshed="onTaskBoardRefreshed"
        />
        <CommissionBoardPanel v-else />
      </template>
    </section>

    <!-- ───────── 管理面板：预约 ───────── -->
    <section v-if="activeTab === 'building' && isAdmin && showAdmin" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="showAdmin = false">← 返回建筑</button>
        <h3 class="col-title" style="margin: 0;">📋 预约管理</h3>
      </div>
      <AdminReservationPanel />
    </section>

    <!-- ───────── 管理面板：建筑模板 ───────── -->
    <section v-if="activeTab === 'building' && isAdmin && showBuildingAdmin" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="showBuildingAdmin = false">← 返回建筑</button>
      </div>
      <AdminBuildingPanel />
    </section>

    <!-- ───────── 管理面板：幸运大转盘奖池 ───────── -->
    <section v-if="activeTab === 'building' && isAdmin && showLuckyWheelAdmin" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="showLuckyWheelAdmin = false">← 返回建筑</button>
      </div>
      <AdminLuckyWheelPanel />
    </section>

    <!-- ───────── 管理面板：物品图鉴 ───────── -->
    <section v-if="activeTab === 'building' && canViewAdmin && showItemCatalogAdmin" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="showItemCatalogAdmin = false">← 返回建筑</button>
      </div>
      <AdminItemCatalogPanel :readonly="false" />
    </section>

    <!-- ───────── 宿舍区 ───────── -->
    <section v-show="activeTab === 'building' && showDormitory" class="tab-content">
      <div class="dorm-header">
        <button type="button" class="btn-back" @click="showDormitory = false; swapMode = false; swapFirst = null; dormRoomId = null">← 返回建筑</button>
        <h3 class="col-title" style="margin: 0;">🏠 宿舍区</h3>
        <button
          type="button"
          :class="['btn-swap', { active: swapMode }]"
          @click="toggleSwapMode"
        >{{ swapMode ? '取消互换' : '🔄 互换床位' }}</button>
      </div>
      <div v-if="swapMode" class="swap-hint">
        <span>已选：{{ swapFirst?.name }}，点开目标房间后点击床位进行搬迁/互换</span>
        <span class="swap-hint-note">（空床位可直接搬，别人床位需对方同意）</span>
      </div>

      <!-- 互换请求通知面板 -->
      <div v-if="store.swapRequestsIncoming.length > 0 || store.swapRequestsOutgoing.length > 0" class="swap-request-panel">
        <div v-if="store.swapRequestsIncoming.length > 0" class="srp-section">
          <div class="srp-title">📩 收到的互换请求</div>
          <div v-for="req in store.swapRequestsIncoming" :key="'in-' + req.id" class="srp-card">
            <div class="srp-card-info">
              <span class="srp-adv-name">{{ req.requester_adv_name }}</span>
              <span class="srp-hint">想和你的冒险者</span>
              <span class="srp-adv-name">{{ req.target_adv_name }}</span>
              <span class="srp-hint">交换床位</span>
              <span class="srp-user">（{{ req.requester_nickname }}）</span>
            </div>
            <div class="srp-card-actions">
              <button type="button" class="btn-primary btn-sm" @click="doAcceptSwapRequest(req.id)">✅ 同意</button>
              <button type="button" class="btn-secondary btn-sm" @click="doRejectSwapRequest(req.id)">❌ 拒绝</button>
            </div>
          </div>
        </div>
        <div v-if="store.swapRequestsOutgoing.length > 0" class="srp-section">
          <div class="srp-title">📤 发出的互换请求</div>
          <div v-for="req in store.swapRequestsOutgoing" :key="'out-' + req.id" class="srp-card">
            <div class="srp-card-info">
              <span class="srp-adv-name">{{ req.requester_adv_name }}</span>
              <span class="srp-hint">→</span>
              <span class="srp-adv-name">{{ req.target_adv_name }}</span>
              <span class="srp-status">（等待对方回应…）</span>
            </div>
            <div class="srp-card-actions">
              <button type="button" class="btn-secondary btn-sm" @click="doCancelSwapRequest(req.id)">取消请求</button>
            </div>
          </div>
        </div>
      </div>

      <!-- 自建宿舍楼面板：全服共建 + 捐献 + 管理员设置 -->
      <div v-if="store.dormitoryMyStatus" class="dorm-build-panel">
        <div class="dbp-row">
          <div class="dbp-materials">
            <span class="dbp-label">我的材料</span>
            <span class="dbp-mat">🪵 {{ myMaterialBalance('wood') }}</span>
            <span class="dbp-mat">🪨 {{ myMaterialBalance('stone') }}</span>
          </div>
          <div class="dbp-concurrent">
            <span class="dbp-label">全服建造中</span>
            <span class="dbp-cnt">
              {{ store.dormitoryMyStatus.concurrent.current_building }}
              /
              {{ store.dormitoryMyStatus.concurrent.max_concurrent }}
            </span>
          </div>
          <div class="dbp-concurrent">
            <span class="dbp-label">玩家共建</span>
            <span class="dbp-cnt">
              {{ store.dormitoryMyStatus.total?.current ?? 0 }}
              /
              {{ store.dormitoryMyStatus.total?.max ?? 10 }}
            </span>
          </div>
          <div class="dbp-concurrent">
            <span class="dbp-label">普通宿舍</span>
            <span class="dbp-cnt">{{ store.dormitoryMyStatus.total?.room_count ?? store.dormitory.length }} 间</span>
          </div>
          <button
            type="button"
            class="btn-primary btn-sm"
            :disabled="applyingDormitory || dormitoryConcurrentFull || dormitoryTotalFull"
            :title="dormitoryTotalFull ? '玩家共建宿舍已达上限，需管理员扩容' : (dormitoryConcurrentFull ? '全服并发名额已满，请等其中一栋完工' : '')"
            @click="applyForDormitory"
          >
            {{ applyingDormitory ? '申请中…' : (dormitoryTotalFull ? '🚫 总数已满' : (dormitoryConcurrentFull ? '🚧 名额已满' : '🏗️ 申请新建宿舍')) }}
          </button>
          <button
            v-if="isAdmin"
            type="button"
            class="btn-secondary btn-sm"
            @click="toggleDormSettings"
          >⚙️ 上限设置</button>
        </div>
        <p class="dbp-hint">
          申请后系统等概率抽取 1 / 2 / 4 床房型，全服冒险者共同捐木 / 石建造，捐满自动开工。
        </p>

        <!-- 管理员设置 -->
        <div v-if="isAdmin && showDormSettings" class="dbp-admin-settings">
          <div class="dbp-setting-row">
            <label>全服并发上限：</label>
            <input
              v-model.number="dormMaxConcurrentDraft"
              type="number"
              min="1"
              max="100"
              class="dbp-settings-input"
            />
            <button
              type="button"
              class="btn-primary btn-sm"
              :disabled="savingDormSettings"
              @click="saveDormMaxConcurrent"
            >{{ savingDormSettings ? '保存中…' : '保存' }}</button>
            <span class="dbp-hint" style="margin-left: 8px;">含 locked（待建）+ building（在建）</span>
          </div>
          <div class="dbp-setting-row">
            <label>玩家共建上限：</label>
            <input
              v-model.number="dormMaxTotalDraft"
              type="number"
              min="1"
              max="200"
              class="dbp-settings-input"
            />
            <button
              type="button"
              class="btn-primary btn-sm"
              :disabled="savingDormSettings"
              @click="saveDormMaxTotal"
            >{{ savingDormSettings ? '保存中…' : '保存' }}</button>
            <span class="dbp-hint" style="margin-left: 8px;">不含种子房间升级占位；按普通宿舍统计</span>
          </div>
        </div>

        <div v-if="store.dormitoryMyStatus.in_progress_instances.length > 0" class="dbp-instances">
          <div
            v-for="inst in store.dormitoryMyStatus.in_progress_instances"
            :key="inst.id"
            class="dbp-card"
            :class="['st-' + inst.status, { 'is-mine': inst.is_mine }]"
          >
            <div class="dbp-card-head">
              <span class="dbp-card-name">🛏️ {{ inst.custom_name || `宿舍楼 #${inst.id}` }}</span>
              <span class="dbp-card-beds">{{ inst.template?.produced_beds }} 床位</span>
              <span v-if="inst.is_mine" class="dbp-tag-mine">我的</span>
              <span v-else class="dbp-tag-other">
                由 {{ inst.applicant_adv_name || inst.applicant_nickname || `用户#${inst.applicant_user_id}` }} 申请
              </span>
            </div>

            <template v-if="inst.status === 'locked'">
              <div class="dbp-materials-list">
                <div
                  v-for="(need, mat) in (inst.template?.required_materials ?? {})"
                  :key="mat"
                  class="dbp-mat-row"
                >
                  <div class="dbp-mat-row-head">
                    <span class="dbp-mat-icon">{{ matIcon(mat) }}</span>
                    <span class="dbp-mat-name">{{ matLabel(mat) }}</span>
                    <div class="dbp-mat-progress">
                      <div
                        class="dbp-mat-bar"
                        :style="{ width: matProgressPercent(inst, mat) + '%' }"
                      ></div>
                      <span class="dbp-mat-text">
                        {{ inst.donations_summary?.[mat] ?? 0 }} / {{ need }}
                        <span v-if="(inst.my_donations?.[mat] ?? 0) > 0" class="dbp-mat-mine">
                          (我已捐 {{ inst.my_donations?.[mat] }})
                        </span>
                      </span>
                    </div>
                  </div>
                  <div v-if="(inst.donations_summary?.[mat] ?? 0) < Number(need)" class="dbp-mat-form">
                    <span v-if="myMaterialBalance(String(mat)) > 0" class="dbp-mat-have">
                      我有 {{ myMaterialBalance(String(mat)) }}
                    </span>
                    <span v-else class="dbp-mat-empty">无库存</span>
                    <input
                      v-model.number="donateAmountBy[inst.id + ':' + mat]"
                      type="number"
                      min="1"
                      :max="Math.min(myMaterialBalance(String(mat)), Number(need) - (inst.donations_summary?.[mat] ?? 0))"
                      class="dbp-mat-amount"
                      placeholder="数量"
                      :disabled="myMaterialBalance(String(mat)) <= 0"
                    />
                    <button
                      type="button"
                      class="btn-primary btn-sm"
                      :disabled="donatingKey === (inst.id + ':' + mat) || myMaterialBalance(String(mat)) <= 0"
                      @click="submitDonate(inst.id, String(mat))"
                    >
                      {{ donatingKey === (inst.id + ':' + mat) ? '捐…' : '捐' }}
                    </button>
                  </div>
                </div>
              </div>
            </template>

            <template v-else-if="inst.status === 'building'">
              <div class="dbp-card-need">
                已开工 · 共需 {{ formatMaterials(inst.template?.required_materials) }}
              </div>
              <div class="dbp-progress-wrap">
                <div class="dbp-progress-bar">
                  <div class="dbp-progress-fill" :style="{ width: inst.progress_percent + '%' }"></div>
                </div>
                <span class="dbp-progress-text">{{ inst.progress_percent }}%</span>
              </div>
              <button
                type="button"
                class="btn-secondary btn-sm"
                @click="openBuildingDetail(inst)"
              >📋 派遣冒险者</button>
            </template>

            <button
              v-if="isAdmin && inst.status === 'locked'"
              type="button"
              class="btn-danger btn-sm dbp-card-cancel"
              :disabled="cancellingDormitoryId === inst.id"
              @click="confirmCancelDormitory(inst.id)"
            >{{ cancellingDormitoryId === inst.id ? '停止中…' : '❌ 停止建造' }}</button>
            <button
              v-if="isAdmin && inst.status === 'locked'"
              type="button"
              class="btn-gm btn-sm dbp-card-cancel"
              :disabled="gmCompletingId === inst.id"
              @click="gmCompleteDormitory(inst)"
            >{{ gmCompletingId === inst.id ? '建成中…' : '⏩ GM 一键建成' }}</button>
          </div>
        </div>
      </div>

      <div v-if="store.dormitoryPagination" class="dorm-list-toolbar">
        <span>共 {{ store.dormitoryPagination.total }} 间· 我的宿舍优先，其次显示关注冒险者所在宿舍</span>
        <span>第 {{ store.dormitoryPagination.current_page }} / {{ store.dormitoryPagination.pages }} 页</span>
      </div>
      <div v-if="store.dormitoryLoading && store.dormitory.length === 0" class="empty">加载中…</div>
      <div v-else-if="store.dormitory.length === 0" class="empty">暂无宿舍</div>
      <!-- 统一格式预览卡：床位布局/命名/升级/贡献者都在点开的房间详情弹窗里 -->
      <div v-else class="dorm-grid">
        <div
          v-for="room in store.dormitory"
          :key="room.id"
          :class="[
            'dorm-room',
            'dorm-room-lv-' + dormDisplayLevel(room),
            {
              'is-mine': room.is_mine,
              'has-followed': !room.is_mine && room.has_followed,
            },
          ]"
          role="button"
          tabindex="0"
          @click="openDormRoom(room.id)"
          @keydown.enter.prevent="openDormRoom(room.id)"
        >
          <div class="room-head">
            <span class="room-icon">{{ dormRoomIcon(room) }}</span>
            <span class="room-name">{{ room.name }}</span>
            <span class="room-cap">{{ roomOccupied(room) }}/{{ room.capacity }}</span>
          </div>
          <div class="room-badges">
            <span v-if="room.is_mine" class="room-relation-badge mine">我的宿舍</span>
            <span v-else-if="room.has_followed" class="room-relation-badge followed">已关注住客</span>
            <span class="level-badge">Lv.{{ dormDisplayLevel(room) }} {{ dormLevelInfo(dormDisplayLevel(room)).name }}</span>
            <span v-if="room.naming_user_id === currentUserId && !room.naming_used" class="naming-badge">✒️ 可命名</span>
            <span v-if="room.upgrade_naming_user_id === currentUserId && !room.upgrade_naming_used" class="naming-badge naming-badge-upgrade">✒️ 升级可命名</span>
          </div>
          <div class="room-pips">
            <span
              v-for="(bed, idx) in room.beds"
              :key="idx"
              class="bed-pip"
              :class="{ occupied: bed !== null }"
              :title="bed ? bed.adventurer_name : '空床位'"
            >🛏️</span>
          </div>
          <div class="room-residents">{{ roomResidents(room) }}</div>
        </div>
      </div>
      <div v-if="store.dormitoryPagination && store.dormitoryPagination.pages > 1" class="dorm-pagination">
        <Pagination :pagination="store.dormitoryPagination" @go="goDormitoryPage" />
      </div>
    </section>

    <!-- ───────── 我的冒险者 ───────── -->
    <section v-show="activeTab === 'my-adventurers'" class="tab-content">
      <div class="mine-section">
        <div
          v-if="pendingRevertOffers.length"
          class="revert-offer-page-banner"
        >
          <div class="revert-offer-page-text">
            <strong>⚠️ 有 {{ pendingRevertOffers.length }} 条申诉修复待您确认是否撤销</strong>
            <span class="muted small">（管理员邀请或撤回申请已通过核查，确认后将尽力还原）</span>
          </div>
          <button type="button" class="btn-danger btn-sm" @click="openDataAppealHistory">
            查看并处理
          </button>
        </div>
        <div
          v-for="offer in pendingPlaceholderRecallOffers"
          :key="offer.id"
          class="revert-offer-page-banner placeholder-recall-banner"
        >
          <div class="revert-offer-page-text">
            <strong>📜 占位入门技能回收确认</strong>
            <span class="muted small">
              管理员邀请移除
              {{ offer.placeholder_skills?.map((s: any) => `「${s.name}」`).join('、') || '占位技能' }}，
              换取 {{ offer.scroll_grant_count }} 个「{{ offer.scroll_name || '职业通用技能卷轴' }}」
            </span>
            <p v-if="offer.admin_note" class="muted small">说明：{{ offer.admin_note }}</p>
          </div>
          <div class="placeholder-recall-actions">
            <button
              type="button"
              class="btn-primary btn-sm"
              :disabled="placeholderRecallLoadingId === offer.id"
              @click="acceptPlaceholderRecallOffer(offer.id)"
            >同意回收</button>
            <button
              type="button"
              class="btn-secondary btn-sm"
              :disabled="placeholderRecallLoadingId === offer.id"
              @click="declinePlaceholderRecallOffer(offer.id)"
            >拒绝</button>
          </div>
        </div>
        <div class="mine-head">
          <h3 class="col-title">我的冒险者</h3>
          <button
            type="button"
            class="mine-tools-toggle btn-secondary"
            :aria-expanded="mineToolsExpanded"
            aria-controls="s1-character-tools"
            @click="mineToolsExpanded = !mineToolsExpanded"
          >管理与设置 <span aria-hidden="true">{{ mineToolsExpanded ? '⌃' : '⌄' }}</span></button>
          <div id="s1-character-tools" :class="['mine-head-actions', { 'is-mobile-collapsed': !mineToolsExpanded }]">
            <button
              v-if="store.squadFeatureEnabled && canOpenSquadModal && !showCreatePanel"
              type="button"
              class="btn-secondary btn-sm"
              @click="showSquadModal = true"
            >🛡️ 纹章小队</button>
            <button
              v-if="playableMyAdventurers.length > 0 && !showCreatePanel"
              type="button"
              class="btn-secondary btn-sm support-vote-btn"
              :disabled="store.supportVoteLoading"
              @click="openSupportVoteModal"
            >{{ supportVoteButtonText }}</button>
            <div
              class="mine-title-tools"
              role="group"
              aria-label="称号设置"
            >
              <span class="mine-title-tools-label">🎖️ 称号</span>
              <button
                v-if="playableMyAdventurers.length > 0 && !showCreatePanel"
                type="button"
                class="mine-title-tool"
                :disabled="titlePreviewLoading"
                @click="openTitlePreview"
              >{{ titlePreviewLoading ? '读取中…' : '预览' }}</button>
              <button
                type="button"
                class="mine-title-tool effects"
                :class="{ disabled: !titleEffectsEnabled }"
                :aria-pressed="!titleEffectsEnabled"
                title="只影响当前设备，称号文字与静态边框仍会保留"
                @click="toggleTitleEffects"
              >{{ titleEffectsEnabled ? '关闭动画' : '开启动画' }}<small>本机</small></button>
            </div>
            <button
              v-if="playableMyAdventurers.length > 0 && !showCreatePanel"
              type="button"
              class="btn-secondary btn-sm"
              @click="openTextCorrection"
            >✏️ 文本代号更正</button>
            <button
              v-if="playableMyAdventurers.length > 0 && !showCreatePanel"
              type="button"
              class="btn-secondary btn-sm"
              @click="showDataAppeal = true"
            >🔧 数据申诉</button>
            <button
              v-if="myAdventurers.length > 0 && !showCreatePanel"
              type="button"
              class="btn-secondary btn-sm"
              @click="showCompanionEvolutionLedger = true"
            >📖 嬗变账本</button>
            <button
              v-if="waitingNpcOrigin?.can_reset && !showCreatePanel"
              type="button"
              class="btn-secondary btn-sm"
              :disabled="waitingNpcOriginLoading || resettingWaitingNpcOrigin"
              :title="waitingNpcOrigin.blocked_reason || '只清除抽取前作为路人 NPC 继承的职业、物品、技能、特质与经历；正常初始项和其他来源不受影响'"
              @click="doResetWaitingNpcOrigin"
            >{{ resettingWaitingNpcOrigin ? '重置中…' : '🌱 另启新生' }}</button>
            <button
              v-if="myLoaded && canShowCreateEntry"
              type="button"
              class="btn-primary btn-sm"
              @click="showCreatePanel = !showCreatePanel"
            >{{ showCreatePanel ? '返回列表' : (archivedMyAdventurers.length > 0 ? '重新预约' : '+ 创建冒险者') }}</button>
          </div>
        </div>


        <div v-if="!myLoaded" class="empty">加载中…</div>
        <template v-else-if="playableMyAdventurers.length === 0 && !showCreatePanel">
          <ArchiveAppealPanel
            v-if="archivedMyAdventurers.length > 0"
            :adventurer="archivedMyAdventurers[0]"
            @view-detail="openDetail(archivedMyAdventurers[0])"
          />
          <div v-else class="empty-hint">
            <p>{{ isRealmVisitor ? '当前为跨区访客浏览，你的冒险者不会进入这个区服。' : '你还没有冒险者' }}</p>
            <button v-if="!isRealmVisitor" type="button" class="btn-primary" @click="showCreatePanel = true">创建冒险者</button>
          </div>
        </template>
        <CreateAdventurerPanel v-else-if="showCreatePanel && archivedMyAdventurers.length === 0" @reserved="onReserved" />
        <div v-else class="adv-grid">
          <AdventurerCard
            v-for="adv in playableMyAdventurers"
            :key="adv.id"
            :adv="adv"
            :current-user-id="currentUserId"
            @open="openDetail"
            @view-logs="viewAdvLogs"
          />
        </div>
      </div>

      <!-- 最近动态：金币/材料流水 + 建筑使用结果（治疗/锻造离线结算可回看） -->
      <div v-if="playableMyAdventurers.length > 0 && !showCreatePanel" class="mine-section activity-section">
        <div class="mine-head">
          <h3 class="col-title">📋 最近动态</h3>
          <div class="mine-head-actions">
            <select
              v-model="store.myActivityFilter"
              class="activity-filter-select"
              aria-label="动态筛选"
              :disabled="store.myActivityLoading"
              @change="store.fetchMyActivity(true)"
            >
              <option value="all">全部动态</option>
              <optgroup label="设施结果">
                <option value="building">全部设施</option>
                <option value="blacksmith">锻造记录</option>
                <option value="tailoring">裁制记录</option>
                <option value="alchemy">炼金记录</option>
                <option value="assembly">组装记录</option>
                <option value="cooking">烹饪记录</option>
                <option value="healing">治疗记录</option>
                <option value="training">修炼与技能</option>
                <option value="pet_evolution">伙伴嬗变</option>
              </optgroup>
              <optgroup label="流水与恢复">
                <option value="currency">货币收支</option>
                <option value="materials">材料变动</option>
                <option value="items">物品变动</option>
                <option value="trait_recovery">休息恢复</option>
              </optgroup>
            </select>
            <button
              type="button"
              class="btn-secondary btn-sm"
              :disabled="store.myActivityLoading"
              @click="store.fetchMyActivity(true)"
            >刷新</button>
          </div>
        </div>
        <div v-if="store.myActivityLoading && store.myActivity.length === 0" class="empty">加载中…</div>
        <div v-else-if="store.myActivity.length === 0" class="empty">
          {{ store.myActivityFilter === 'all' ? '还没有任何动态' : '当前分类还没有记录' }}
        </div>
        <ul v-else class="activity-list">
          <li v-for="e in store.myActivity" :key="e.kind + '-' + e.id" class="activity-item">
            <!-- 建筑使用：治疗/锻造带结果徽标 + 叙事；普通使用只显示完成 -->
            <template v-if="e.kind === 'building'">
              <div class="act-row">
                <span class="act-icon">{{ e.building_icon || '🏠' }}</span>
                <span class="act-title">{{ e.building_name }}</span>
                <span class="act-badge" :class="activityBadgeClass(e)">{{ activityBadgeLabel(e) }}</span>
                <template v-if="e.cost">
                  <span class="act-amount neg">
                    {{ (e.cost.change_amount ?? 0) >= 0 ? '+' : '' }}{{ e.cost.kind === 'currency' ? (e.cost.change_amount ?? 0).toFixed(2) : e.cost.change_amount }}
                  </span>
                  <span class="act-title">{{ e.cost.display_name }}</span>
                </template>
                <template v-if="e.reward">
                  <span class="act-amount" :class="(e.reward.change_amount ?? 0) >= 0 ? 'pos' : 'neg'">
                    {{ (e.reward.change_amount ?? 0) >= 0 ? '+' : '' }}{{ e.reward.kind === 'currency' ? (e.reward.change_amount ?? 0).toFixed(2) : e.reward.change_amount }}
                  </span>
                  <span class="act-title">{{ e.reward.display_name }}</span>
                </template>
                <template v-if="e.refund">
                  <span class="act-amount pos">
                    +{{ (e.refund.change_amount ?? 0).toFixed(2) }}
                  </span>
                  <span class="act-title">{{ e.refund.display_name }}</span>
                  <span v-if="e.refund.description" class="act-desc">{{ e.refund.description }}</span>
                </template>
                <button
                  v-if="e.undo_available"
                  type="button"
                  class="act-undo-btn"
                  :disabled="undoingActivityId === e.id"
                  :title="e.undo_expires_at ? `可在 ${formatDateTime(e.undo_expires_at)} 前撤回` : '撤回最近一次结果'"
                  @click="undoBuildingActivity(e)"
                >{{ undoingActivityId === e.id ? '撤回中…' : '后悔撤回' }}</button>
                <span class="act-time">{{ e.created_at ? formatDateTime(e.created_at) : '' }}</span>
              </div>
              <div
                v-if="e.result?.type === 'ember_kitchen' && !e.result.system_abort"
                class="act-detail"
              >
                <template v-if="e.result.is_edible">
                  出锅：<strong>{{ e.result.new_name }}</strong>
                  · 可食用 {{ e.result.uses_left ?? 1 }} 次
                  <span v-if="e.result.cookware_name"> · {{ e.result.cookware_name }}</span>
                </template>
                <template v-else>
                  黑暗料理无法入口，已当场丢弃 · 投入食材均已消耗
                </template>
              </div>
              <EvolutionActivityInputs v-if="e.result?.type === 'pet_evolution'" :result="e.result" />
              <div v-if="isTransformCraftResult(e.result?.type) && e.result?.success && e.result.new_name" class="act-detail">
                {{ e.result.old_name }} → <strong>{{ e.result.new_name }}</strong>
                <span v-if="e.result.type === 'item_assembly_workshop' && e.result.direction_fit === 'adjusted'"> · 构装师替你收了收方向</span>
              </div>
              <div
                v-else-if="e.result && isTransformCraftResult(e.result.type) && !e.result.success"
                class="act-detail"
              >
                <span v-if="e.result.type === 'item_assembly_workshop' && e.result.unreasonable_input">
                  全部投入槽位均在构装过程中损毁
                </span>
                <span v-else-if="e.result.unreasonable_input">不合理投入，材料全部损毁</span>
                <span v-if="e.result.type === 'item_assembly_workshop' && e.result.consumed_items?.length">
                  · 损毁：{{ e.result.consumed_items.map(formatAssemblyConsumedItem).join('、') }}
                </span>
                <span v-else-if="!e.result.unreasonable_input && e.result.consumed_item_names?.length">
                  消耗：{{ e.result.consumed_item_names.join('、') }}
                </span>
                <span v-else-if="!e.result.consumed">
                  物品均保留
                </span>
                <span v-if="e.result.gold_refunded">
                  · 退还 {{ e.result.gold_refunded }} 金币
                </span>
              </div>
              <div
                v-if="e.result?.type === 'healing' && e.result.success !== false && (e.result.healed_traits?.length || e.result.recovery_traits?.length || e.result.modified_traits?.length)"
                class="act-detail"
              >
                <span v-for="t in e.result.healed_traits ?? []" :key="'h-' + t" class="act-chip healed">✓ {{ t }}</span>
                <span
                  v-for="m in (e.result.recovery_traits ?? e.result.modified_traits ?? [])"
                  :key="'m-' + m.name"
                  class="act-chip modified"
                >
                  {{ m.name }} → {{ m.new_name }}
                </span>
              </div>
              <div v-if="e.result?.type === 'skill_training'" class="act-detail">
                <template v-if="e.result.success && e.result.skill">
                  <span v-if="e.result.operation === 'advance' && e.result.old_skill">
                    {{ e.result.old_skill.name }} → <strong>{{ e.result.skill.name }}</strong>
                  </span>
                  <span v-else>学会 <strong>{{ e.result.skill.name }}</strong></span>
                  <span v-if="e.result.direction_fit === 'adjusted'"> · 导师替你收了收路子</span>
                </template>
                <template v-else>
                  导师没能把这堂课授成
                  <span v-if="e.result.gold_refunded"> · 退还 {{ e.result.gold_refunded }} 金币</span>
                  <span v-if="e.result.stamina_refunded">、{{ e.result.stamina_refunded }} 精力</span>
                </template>
              </div>
              <p
                v-if="e.result?.type === 'skill_training' && !e.result.system_abort && e.result.reason"
                class="act-narrative"
              >师傅的话：{{ e.result.reason }}</p>
              <p
                v-if="e.result?.type === 'item_assembly_workshop' && !e.result.system_abort && e.result.reason"
                class="act-narrative"
              >构装师的话：{{ e.result.reason }}</p>
              <p v-if="e.result?.narrative" class="act-narrative">{{ e.result.narrative }}</p>
              <div v-if="e.ai_feedback" class="act-ai-feedback">
                <AiFeedbackControl
                  :target="e.ai_feedback"
                  @updated="updateActivityAiFeedback(e, $event)"
                />
              </div>
            </template>
            <!-- 物品变动 -->
            <template v-else-if="e.kind === 'item'">
              <div class="act-row">
                <span class="act-icon">{{ e.change_type === 'consume' ? '💔' : e.change_type === 'overflow' ? '📥' : '📦' }}</span>
                <span class="act-title" :style="{ color: rarityColor(e.item_rarity) }">{{ e.item_name }}</span>
                <span v-if="e.item_rarity && e.item_rarity !== 'common'" class="rarity-pill" :style="{ color: rarityColor(e.item_rarity), borderColor: rarityColor(e.item_rarity) }">{{ rarityLabel(e.item_rarity) }}</span>
                <span class="act-amount" :class="e.change_type === 'consume' ? 'neg' : 'pos'">
                  {{ e.change_type === 'consume' ? '-' : '+' }}{{ e.quantity ?? 1 }}
                </span>
                <button
                  v-if="appealItemActivityLabel(e) && e.expedition_log_id"
                  type="button"
                  class="act-source-link"
                  :aria-label="`${appealItemActivityLabel(e)}，打开探险日志 ${e.expedition_log_id}`"
                  @click="openAppealActivityLog(e)"
                >{{ appealItemActivityLabel(e) }} · 探险日志 #{{ e.expedition_log_id }}</button>
                <span v-else-if="appealItemActivityLabel(e)" class="act-source-tag">
                  {{ appealItemActivityLabel(e) }}
                </span>
                <span v-else class="act-desc">{{ e.description }}</span>
                <span class="act-time">{{ e.created_at ? formatDateTime(e.created_at) : '' }}</span>
              </div>
              <details
                v-if="appealItemActivityLabel(e) && e.description"
                class="act-description-details"
              >
                <summary>查看申诉处理详情</summary>
                <p>{{ e.description }}</p>
              </details>
            </template>
            <!-- 货币 / 材料流水 -->
            <template v-else>
              <div class="act-row">
                <span class="act-icon">{{ e.icon }}</span>
                <span class="act-amount" :class="(e.change_amount ?? 0) >= 0 ? 'pos' : 'neg'">
                  {{ (e.change_amount ?? 0) >= 0 ? '+' : '' }}{{ e.kind === 'currency' ? (e.change_amount ?? 0).toFixed(2) : e.change_amount }}
                </span>
                <span class="act-title">{{ e.display_name }}</span>
                <span v-if="e.item_name && e.item_rarity" class="act-desc" v-html="colorItemName(e.description || sourceTypeLabel(e.source_type), e.item_name, e.item_rarity)"></span>
                <span v-else class="act-desc">{{ e.description || sourceTypeLabel(e.source_type) }}</span>
                <span class="act-time">{{ e.created_at ? formatDateTime(e.created_at) : '' }}</span>
              </div>
            </template>
          </li>
        </ul>
        <button
          v-if="store.myActivityHasMore"
          type="button"
          class="btn-secondary btn-sm act-more"
          :disabled="store.myActivityLoading"
          @click="store.fetchMyActivity(false)"
        >{{ store.myActivityLoading ? '加载中…' : '加载更早的记录' }}</button>
      </div>
    </section>

    <!-- ───────── 冒险者：酒馆浏览 ───────── -->
    <section v-show="activeTab === 'adventurers'" class="tab-content">
      <div class="browse-section">
        <h3 class="col-title">酒馆冒险者</h3>
        <div class="filter-bar">
          <select v-model="browseFilter.status" @change="searchBrowse">
            <option value="">全部状态</option>
            <option value="idle">空闲</option>
            <option value="in_party">组队中</option>
            <option value="exploring">探险中</option>
            <option value="activity">酒馆活动中</option>
            <option value="resting">休息</option>
            <option value="dead">已阵亡</option>
          </select>
          <select v-model="browseFilter.sort_field" @change="searchBrowse">
            <option value="created_at">创建时间</option>
            <option value="level">等级</option>
            <option value="name">名字</option>
            <option value="strength">力量</option>
            <option value="agility">敏捷</option>
            <option value="intelligence">智力</option>
            <option value="luck">幸运</option>
            <option value="gold">金币</option>
          </select>
          <select v-model="browseFilter.sort_order" @change="searchBrowse">
            <option value="desc">降序</option>
            <option value="asc">升序</option>
          </select>
          <input
            v-model="browseFilter.search"
            class="search-input"
            placeholder="搜索冒险者…"
            @keyup.enter="searchBrowse"
          />
          <button type="button" class="btn-primary btn-sm" @click="searchBrowse">搜索</button>
        </div>

        <div v-if="store.browseLoading && store.browseAdventurers.length === 0" class="empty">加载中…</div>
        <div v-else-if="store.browseAdventurers.length === 0" class="empty">暂无冒险者</div>
        <div v-else class="adv-grid">
          <AdventurerCard
            v-for="adv in store.browseAdventurers"
            :key="adv.id"
            :adv="adv"
            :current-user-id="currentUserId"
            @open="openDetail"
            @toggle-follow="toggleFollow"
            @view-logs="viewAdvLogs"
          />
        </div>
        <Pagination v-if="store.browsePagination" :pagination="store.browsePagination" @go="goBrowsePage" />
      </div>
    </section>

    <!-- ───────── 小队 ───────── -->
    <section v-show="activeTab === 'party'" class="tab-content party-page pq-party-page">
      <div class="commission-subtabs party-board-subtabs pq-subtabs">
        <button
          type="button"
          :class="{ active: partyBoardTab === 'normal' }"
          @click="partyBoardTab = 'normal'"
        >🛡️ 普通小队</button>
        <button
          type="button"
          :class="{ active: partyBoardTab === 'special' }"
          @click="partyBoardTab = 'special'"
        >✨ 特殊机缘</button>
      </div>

      <div class="party-board-hero pq-notice-board">
        <div class="pq-board-text">
          <p class="party-kicker pq-kicker">ABYSS TAVERN BOARD</p>
          <h3>{{ partyBoardTab === 'normal' ? '普通副本招募板' : '特殊机缘集结板' }}</h3>
          <p>
            {{ partyBoardTab === 'normal'
              ? '先挑地图，再选择单人入队或开一张招募帖。公开小队会展示在大厅，邀请制小队只通过编号和邀请加入。'
              : '持票冒险者可组建特殊小队（门票须随身携带）；满员后投票或倒计时发车，适合提前协调阵容。' }}
          </p>
        </div>
        <div class="party-hero-stats pq-board-stats">
          <span v-if="partyBoardTab === 'normal'"><strong>{{ store.matchQueuedMemberCount }}</strong>人匹配中</span>
          <span v-if="partyBoardTab === 'normal'"><strong>{{ store.publicMatchTickets.length }}</strong>公开招募</span>
          <span v-else><strong>{{ displayedBrowseParties.length }}</strong>可浏览小队</span>
        </div>
      </div>

      <!-- 我的小队：普通副本优先走匹配 ticket，Party 卡片兜底展示成团后的真实状态 -->
      <div class="my-party-section pq-my-party">
        <div class="party-section-title-row">
          <h3 class="col-title pq-section-title">🛡️ 我的小队</h3>
          <button
            type="button"
            class="btn-secondary btn-sm party-refresh-btn"
            :disabled="partyRefreshing || store.matchTicketsLoading || store.myPartiesLoading || store.partiesLoading"
            @click="doRefreshPartyBoard"
          >{{ partyRefreshing ? '刷新中…' : '刷新' }}</button>
        </div>

        <!-- 普通副本：匹配 ticket -->
        <template v-if="partyBoardTab === 'normal'">
          <p v-if="store.campaignTaskNotice" class="party-vote-hint pq-hint" role="status">
            上次任务取消（{{ formatDateTime(store.campaignTaskNotice.cancelled_at) }}）：{{ store.campaignTaskNotice.message }}
          </p>
          <p v-if="campaignTaskCoolingDown" class="party-vote-hint match-error pq-error" role="status">
            活动任务接取冷却至 {{ formatDateTime(store.campaignTaskCooldownUntil!) }}。普通探险仍可参加。
          </p>
          <div v-if="store.matchTicketsLoading && store.matchTickets.length === 0 && displayedMyParties.length === 0" class="empty pq-empty"><span class="pq-empty-icon">⏳</span><span>加载中…</span></div>
          <div v-else-if="store.matchTickets.length === 0 && displayedMyParties.length === 0" class="empty hint pq-empty">
            <span class="pq-empty-icon">📋</span>
            <p class="pq-empty-text">你当前没有加入任何匹配小队。</p>
            <p class="pq-empty-sub">可在下方单人匹配、创建小队，或接受他人邀请。</p>
          </div>
          <div v-if="store.matchTickets.length > 0 || displayedMyParties.length > 0" class="party-list pq-party-grid">
            <div v-for="ticket in store.matchTickets" :key="'my-ticket-' + ticket.id" class="party-card party-card-mine match-ticket-card pq-party-scroll pq-scroll-mine">
              <div class="party-head pq-scroll-head">
                <span class="party-name pq-scroll-name">
                  {{ ticket.is_owner ? (ticket.queue_type === 'group' ? '我创建的小队' : '单人匹配') : '已加入的小队' }}{{ ticket.invite_code ? ` #${ticket.invite_code}` : '' }}
                </span>
                <span class="party-status pq-status-crest" :class="'st-' + ticket.status">{{ matchTicketStatusLabel(ticket.status) }}</span>
              </div>
              <div class="pq-dungeon-row">
                <span class="pq-dungeon-icon" aria-hidden="true">🗺️</span>
                <span class="pq-dungeon-name">{{ matchTicketDungeonName(ticket) }}</span>
                <span v-if="matchTicketDungeonLevelDesc(ticket)" class="pq-dungeon-level">推荐 {{ matchTicketDungeonLevelDesc(ticket) }}</span>
              </div>
              <div class="party-info pq-scroll-info pq-meta-info">
                <span>👥 {{ formatMatchTicketMembers(ticket) }}</span>
                <span v-if="ticket.campaign_task_title">🗺️ {{ ticket.campaign_task_title }}</span>
                <span v-if="ticket.campaign_departure_deadline">报名截止：{{ formatDateTime(ticket.campaign_departure_deadline) }} 前出发</span>
                <span>{{ ticket.access_mode === 'invite' ? '🔒 邀请制' : '🌐 公开' }}</span>
                <span v-if="ticket.queue_type === 'group'">{{ ticket.allow_solo_fill ? '✅ 匹配路人补位' : '🚫 不匹配路人' }}</span>
                <span v-if="matchTicketDisplayTime(ticket)">⏳ {{ formatDateTime(matchTicketDisplayTime(ticket)!) }}</span>
              </div>
              <label
                v-if="ticket.is_owner && ticket.queue_type === 'group' && (ticket.status === 'forming' || ticket.status === 'queued')"
                class="party-vote-hint pq-hint matchmaking-toggle-row"
              >
                <input
                  type="checkbox"
                  :checked="!!ticket.allow_solo_fill"
                  :disabled="matchmakingLoading"
                  @change="doSetMatchTicketSoloFill(ticket, $event)"
                />
                匹配路人补位
              </label>
              <p v-if="ticket.description" class="special-dungeon-flavor pq-flavor">{{ ticket.description }}</p>
              <button
                v-if="ticket.can_edit_description"
                type="button" class="btn-secondary btn-sm"
                :disabled="matchmakingLoading" @click="doEditMatchDescription(ticket)"
              >修改小队描述</button>
              <p v-if="ticket.match_error" class="party-vote-hint match-error pq-error">⚠ {{ ticket.match_error }}</p>
              <div
                v-if="ticket.has_fate_dungeon_risk && (ticket.fate_risk_confirmation_required || ticket.pending_fate_risk_member_names?.length)"
                class="party-actions pq-actions"
              >
                <small class="party-vote-hint match-error pq-error">
                  ⚠ {{ (ticket.fate_risk_member_names || []).join('、') }}的命途关联当前目标地牢，可能触发高于常规难度的事件；
                  触发后整场普通奖励与经验按 15 级标准结算。
                  <template v-if="ticket.pending_fate_risk_member_names?.length">
                    等待 {{ ticket.pending_fate_risk_member_names.join('、') }} 确认风险。
                  </template>
                </small>
                <button
                  v-if="ticket.fate_risk_confirmation_required"
                  type="button"
                  class="btn-warning btn-sm"
                  :disabled="matchmakingLoading"
                  @click="doConfirmMatchTicketFateRisk(ticket)"
                >确认风险</button>
              </div>
              <div class="party-members pq-crests">
                <span
                  v-for="m in ticket.members"
                  :key="ticket.id + '-' + m.adventurer_id"
                  class="member-badge pq-member-crest"
                  :class="memberTitleFrameClass(m)"
                  @click="openDetail(m)"
                >
                  {{ m.name }}<small> Lv.{{ m.level }}</small>
                </span>
              </div>
              <div v-if="ticket.status === 'queued'" class="party-actions pq-actions">
                <small class="party-vote-hint pq-hint">{{ formatQueueAhead(ticket) }}</small>
              </div>
              <div v-if="ticket.status === 'matched'" class="party-actions pq-actions">
                <small class="party-vote-hint pq-hint">{{ formatMatchedTicketHint(ticket) }}</small>
                <button
                  v-if="ownMatchTicketMember(ticket)"
                  type="button"
                  class="btn-secondary btn-sm"
                  :disabled="matchmakingLoading"
                  @click="doLeaveMatchedTicket(ticket)"
                >退出已成团小队</button>
              </div>
              <div v-if="ticket.status === 'forming' && ticket.is_owner" class="party-actions pq-actions">
                <button
                  type="button"
                  class="btn-primary btn-sm"
                  :disabled="matchmakingLoading || !!ticket.pending_fate_risk_member_names?.length"
                  @click="doStartMatchmaking(ticket.id)"
                >开始匹配</button>
                <button
                  v-if="ticket.queue_type === 'group' && ticket.access_mode === 'invite'"
                  type="button"
                  class="btn-secondary btn-sm"
                  :disabled="matchmakingLoading"
                  @click="doMakeMatchTicketPublic(ticket.id)"
                >🌐 设为公开</button>
                <small v-if="ticket.queue_type === 'group'" class="party-vote-hint pq-hint">小队邀请码：{{ ticket.invite_code || '生成中' }}</small>
              </div>
              <div v-if="ticket.status === 'queued' && ticket.is_owner && ticket.queue_type === 'group' && ticket.access_mode === 'invite'" class="party-actions pq-actions">
                <button
                  type="button"
                  class="btn-secondary btn-sm"
                  :disabled="matchmakingLoading"
                  @click="doMakeMatchTicketPublic(ticket.id)"
                >🌐 设为公开</button>
                <small class="party-vote-hint pq-hint">公开后会在招募板展示，匹配时允许路人单人补位</small>
              </div>
              <div v-if="ticket.status === 'forming' && ticket.is_owner && ticket.queue_type === 'group'" class="matchmaking-invite-panel pq-invite-panel">
                <div class="matchmaking-invite-row pq-invite-row">
                  <input v-model.trim="inviteSearch" type="text" placeholder="搜索可邀请冒险者" @keyup.enter="loadInviteCandidates(ticket.id)" />
                  <button type="button" class="btn-secondary btn-sm" :disabled="matchmakingLoading" @click="loadInviteCandidates(ticket.id)">搜索</button>
                </div>
                <div v-if="activeInviteTicketId === ticket.id" class="matchmaking-candidates pq-candidates">
                  <div v-if="store.matchInviteCandidates.length === 0" class="empty hint pq-empty pq-empty-inline"><span class="pq-empty-icon">🔇</span>暂无可邀请对象</div>
                  <button
                    v-for="c in store.matchInviteCandidates"
                    :key="'cand-' + c.adventurer_id"
                    type="button"
                    class="btn-secondary btn-sm pq-candidate"
                    :disabled="matchmakingLoading"
                    @click="doInviteCandidate(ticket.id, c.adventurer_id)"
                  >{{ c.is_followed ? '★ ' : '' }}{{ c.name }} Lv.{{ c.level }}</button>
                </div>
              </div>
              <div v-if="ticket.status === 'forming' || ticket.status === 'queued'" class="party-actions pq-actions">
                <button
                  v-if="ticket.is_owner"
                  type="button"
                  class="btn-secondary btn-sm"
                  :disabled="matchmakingLoading"
                  @click="ticket.status === 'queued' && ticket.queue_type === 'group' && !ticket.campaign_task_title ? doStopMatchmaking(ticket.id) : doCancelMatchmaking(ticket.id)"
                >{{ ticket.campaign_task_title ? '取消任务并解散' : (ticket.status === 'queued' && ticket.queue_type === 'group' ? '取消匹配' : (ticket.queue_type === 'group' ? '取消小队' : '取消匹配')) }}</button>
                <button
                  v-else
                  type="button"
                  class="btn-secondary btn-sm"
                  :disabled="matchmakingLoading"
                  @click="doLeaveMatchTicket(ticket)"
                >退出小队</button>
              </div>
            </div>
            <div v-for="p in displayedMyParties" :key="'mine-exp-' + p.id" class="party-card party-card-mine pq-party-scroll pq-scroll-mine">
              <div class="party-head pq-scroll-head">
                <span class="party-name pq-scroll-name">{{ p.name }}</span>
                <span class="party-status pq-status-crest" :class="'st-' + p.status">{{ partyStatusLabel(p.status) }}</span>
              </div>
              <div class="pq-dungeon-row">
                <span class="pq-dungeon-icon" aria-hidden="true">🗺️</span>
                <span class="pq-dungeon-name">{{ p.target_dungeon || '随机地图' }}</span>
                <span v-if="p.target_dungeon_level_desc" class="pq-dungeon-level">推荐 {{ p.target_dungeon_level_desc }}</span>
              </div>
              <div class="party-info pq-scroll-info pq-meta-info">
                <span>👥 {{ p.member_count }}/{{ p.target_size }}</span>
              </div>
              <div class="party-members pq-crests">
                <template v-for="m in p.members" :key="m.id">
                  <span
                    class="member-badge pq-member-crest"
                    :class="memberTitleFrameClass(m)"
                    @click="openDetail(m)"
                  >
                    {{ m.name }}<small> HP {{ m.hp }}/{{ m.max_hp }}</small>
                  </span>
                </template>
              </div>
            </div>
          </div>
        </template>

        <!-- 特殊机缘：AdventurerS1Party -->
        <template v-else>
        <div v-if="store.myPartiesLoading && store.myParties.length === 0" class="empty pq-empty"><span class="pq-empty-icon">⏳</span><span>加载中…</span></div>
        <div v-else-if="displayedMyParties.length === 0" class="empty hint pq-empty">
          <span class="pq-empty-icon">📜</span>
          <p class="pq-empty-text">你当前没有加入任何特殊机缘小队。</p>
          <p class="pq-empty-sub">持票后可在下方组建，或加入他人未满员的特殊小队。</p>
        </div>
        <div v-else class="party-list pq-party-grid">
          <div v-for="p in displayedMyParties" :key="'mine-' + p.id" class="party-card party-card-mine pq-party-scroll pq-scroll-mine pq-scroll-special">
            <div class="party-head pq-scroll-head">
              <span class="party-name pq-scroll-name">{{ p.name }}</span>
              <span class="party-status pq-status-crest" :class="'st-' + p.status">{{ partyStatusLabel(p.status) }}</span>
            </div>
            <div class="pq-dungeon-row">
              <span class="pq-dungeon-icon" aria-hidden="true">🗺️</span>
              <span class="pq-dungeon-name">{{ p.target_dungeon || '随机地图' }}</span>
              <span v-if="p.target_dungeon_level_desc" class="pq-dungeon-level">推荐 {{ p.target_dungeon_level_desc }}</span>
            </div>
            <div class="party-info pq-scroll-info pq-meta-info">
              <span>👥 {{ p.member_count }}/{{ p.target_size }}</span>
              <span v-if="p.departure_time" class="party-departure pq-departure">⏰ {{ formatDateTime(p.departure_time) }} 出发</span>
              <span
                v-if="isSpecialParty(p) && p.status === 'forming' && p.member_count >= p.target_size"
                class="party-vote-progress pq-vote-progress"
              >🗳️ 投票出发 {{ p.departure_vote_count ?? 0 }}/{{ p.member_count }}</span>
            </div>
            <div class="party-members pq-crests">
              <template v-for="m in p.members" :key="m.id">
                <span
                  class="member-badge pq-member-crest"
                  :class="[memberTitleFrameClass(m), { 'member-voted': hasMemberVotedDeparture(p, m.id) }]"
                  @click="openDetail(m)"
                >
                  {{ m.name }}<small> HP {{ m.hp }}/{{ m.max_hp }}</small>
                  <small v-if="hasMemberVotedDeparture(p, m.id)" class="vote-mark"> ✓</small>
                  <button
                    v-if="canLeave(p, m)"
                    type="button"
                    class="btn-leave-mini"
                    :disabled="leavingAdvId === m.id"
                    :title="'让 ' + m.name + ' 离队'"
                    @click.stop="doLeave(m, p)"
                  >×</button>
                  <button
                    v-if="isAdmin && (p.status === 'forming' || p.status === 'exploring')"
                    type="button"
                    class="btn-leave-mini btn-admin-remove"
                    :disabled="adminRemovingAdvId === m.id"
                    :title="'管理员移除 ' + m.name"
                    @click.stop="doAdminRemoveMember(p, m)"
                  >⊖</button>
                </span>
              </template>
            </div>
            <div v-if="isAdmin && (p.status === 'forming' || p.status === 'exploring')" class="party-actions party-admin-actions pq-actions pq-admin-actions">
              <button
                v-if="p.status === 'forming'"
                type="button"
                class="btn-warning btn-sm"
                :disabled="adminPartyLoading"
                @click.stop="doForceDispatch(p)"
              >⚡ 强制出发</button>
              <button
                type="button"
                class="btn-danger btn-sm"
                :disabled="adminPartyLoading"
                @click.stop="doForceDisband(p)"
              >💥 强制解散</button>
            </div>
            <div v-if="canVoteDeparture(p)" class="party-actions pq-actions">
              <button
                type="button"
                class="btn-primary btn-sm"
                :disabled="votingDepartureAdvId !== null || hasMyDepartureVote(p)"
                @click.stop="doVoteDeparture(p)"
              >
                {{ hasMyDepartureVote(p) ? '✓ 已投票出发' : '🗳️ 投票立即出发' }}
              </button>
              <small v-if="!hasMyDepartureVote(p)" class="party-vote-hint pq-hint">
                全体成员投赞成票后，下一调度周期立即出发；有人离队后需重新投票
              </small>
            </div>
          </div>
        </div>
        </template>
      </div>

      <!-- 普通副本：单人匹配 / 创建小队，成团后直接按配额进本 -->
      <div v-if="partyBoardTab === 'normal'" class="matchmaking-section pq-match-board">
        <h3 class="col-title pq-section-title">🎯 普通副本匹配</h3>
        <p class="special-dungeon-hint pq-board-hint">
          你当前的可操作冒险者会自动参与；可单人直接匹配，也可先创建小队等人加入。只有队长点击“开始匹配”后，小队才会进入匹配队列并按配额进本。主动选图时只能选不高于自身档位的副本（向下不限）；加入他人小队时最多可挑战高 1 档；选「随机地图」时仍按档位跨度自动匹配。
        </p>
        <details class="pq-queue-compass" aria-label="各地图候车人数">
          <summary class="pq-queue-compass-head">
            <div>
              <span class="pq-queue-compass-kicker">PARTY FORECAST</span>
              <h4>地图候车风向</h4>
            </div>
            <p>按冒险者最近一次有效报名统计；多选会计入每张候选地图。</p>
          </summary>
          <div class="pq-queue-compass-grid">
            <div
              class="pq-queue-compass-card pq-queue-compass-card--random"
              :class="{ 'is-quiet': store.matchRandomQueuedMemberCount === 0 }"
            >
              <span class="pq-queue-compass-icon" aria-hidden="true">🎲</span>
              <span class="pq-queue-compass-name">随机地图池<small>尚未锁定具体目的地</small></span>
              <strong>{{ store.matchRandomQueuedMemberCount }}<small>人</small></strong>
            </div>
            <div
              v-for="d in store.matchDungeonOptions"
              :key="'queue-compass-' + d.name"
              class="pq-queue-compass-card"
              :class="{ 'is-quiet': matchDungeonQueuedCount(d.name) === 0 }"
            >
              <span class="pq-queue-compass-icon" aria-hidden="true">🗺️</span>
              <span class="pq-queue-compass-name">{{ d.name }}<small>{{ d.level_desc || '普通副本' }}</small></span>
              <strong>{{ matchDungeonQueuedCount(d.name) }}<small>人</small></strong>
            </div>
          </div>
        </details>
        <div class="matchmaking-form pq-match-form pq-action-grid">
          <div class="pq-solo-card">
            <fieldset class="matchmaking-field pq-field pq-dungeon-pool">
              <legend>单人匹配地图（可多选）</legend>
              <div class="pq-dungeon-mode-options">
                <label>
                  <input v-model="matchDungeonMode" type="radio" value="random" />
                  <span>🎲 随机地图 <small>{{ store.matchRandomQueuedMemberCount }} 人候车</small></span>
                </label>
                <label>
                  <input v-model="matchDungeonMode" type="radio" value="custom" />
                  <span>🗺️ 自选地图池</span>
                </label>
              </div>
              <p class="pq-dungeon-pool-hint">
                {{ matchDungeonMode === 'random' ? '随机匹配所有可进入地图，并可计入随机地图每日任务' : (matchPreferredDungeons.length ? `已选 ${matchPreferredDungeons.length} 张；将匹配候选池有交集的玩家` : '请至少选择一张地图') }}
              </p>
              <div v-if="matchDungeonMode === 'custom'" class="pq-dungeon-pool-options">
                <label v-for="d in store.matchDungeonOptions" :key="d.name" class="pq-dungeon-pool-option">
                  <input v-model="matchPreferredDungeons" type="checkbox" :value="d.name" />
                  <span>{{ d.name }}<small>{{ d.level_desc || '普通副本' }} · {{ matchDungeonQueuedCount(d.name) }} 人可匹配</small></span>
                </label>
              </div>
              <button v-if="matchDungeonMode === 'custom' && matchPreferredDungeons.length" type="button" class="btn-ghost btn-sm" @click="matchPreferredDungeons = []">
                清空选择
              </button>
            </fieldset>
            <div class="party-actions matchmaking-solo-action pq-actions pq-solo-action">
              <button
                type="button"
                class="btn-primary btn-sm"
                :disabled="matchmakingLoading || playableMyAdventurers.length === 0 || (matchDungeonMode === 'custom' && matchPreferredDungeons.length === 0)"
                @click="doEnqueueSoloMatchmaking"
              >🎯 单人匹配</button>
            </div>
          </div>
          <div class="matchmaking-create-box pq-create-card">
            <label class="matchmaking-field pq-field">
              小队目标地图
              <select v-model="matchPartyPreferredDungeon">
                <option value="">随机地图</option>
                <option v-for="d in store.matchDungeonOptions" :key="d.name" :value="d.name">
                  {{ d.name }}{{ d.level_desc ? `（${d.level_desc}）` : '' }} · {{ matchDungeonQueuedCount(d.name) }} 人可匹配
                </option>
              </select>
            </label>
            <label class="matchmaking-field pq-field">
              小队描述
              <textarea v-model.trim="matchPartyDescription" rows="3" maxlength="500" placeholder="例如：想打稳一点，欢迎同档治疗/前排加入"></textarea>
            </label>
            <label class="matchmaking-field pq-field">
              小队类型
              <select v-model="matchAccessMode">
                <option value="public">公开小队：展示在招募板，其他人可申请加入</option>
                <option value="invite">邀请制小队：仅通过邀请码或邀请加入</option>
              </select>
            </label>
            <button
              type="button"
              class="btn-secondary btn-sm"
              :disabled="matchmakingLoading || playableMyAdventurers.length === 0"
              @click="doCreateMatchmakingParty"
            >🏰 创建小队</button>
          </div>
        </div>
        <div class="pq-market">
          <div class="matchmaking-invite-row pq-invite-row pq-code-row">
            <input v-model.trim="inviteTicketId" type="text" maxlength="6" inputmode="numeric" placeholder="输入 6 位小队邀请码" />
            <button
              type="button"
              class="btn-secondary btn-sm"
              :disabled="matchmakingLoading || !inviteTicketId"
              @click="doJoinInviteTicket"
            >加入邀请小队</button>
          </div>
          <div class="matchmaking-tickets pq-ticket-board">
            <div class="party-section-head pq-board-head">
              <h4>公开小队</h4>
              <span>{{ store.publicMatchTickets.length }} 个招募中</span>
            </div>
            <div v-if="store.publicMatchTickets.length === 0" class="empty hint pq-empty pq-empty-inline"><span class="pq-empty-icon">📭</span>暂无等待成员加入的公开小队</div>
            <div v-else class="party-list pq-pin-grid">
              <div v-for="ticket in store.publicMatchTickets" :key="'public-ticket-' + ticket.id" class="party-card match-ticket-card pq-pin-note">
                <div class="party-head pq-pin-head">
                  <span class="party-name pq-pin-name">公开小队 {{ ticket.invite_code ? '#' + ticket.invite_code : '' }}</span>
                  <span class="party-status pq-status-crest st-forming">等人中</span>
                </div>
                <div class="pq-dungeon-row">
                  <span class="pq-dungeon-icon" aria-hidden="true">🗺️</span>
                  <span class="pq-dungeon-name">{{ matchTicketDungeonName(ticket) }}</span>
                  <span v-if="matchTicketDungeonLevelDesc(ticket)" class="pq-dungeon-level">推荐 {{ matchTicketDungeonLevelDesc(ticket) }}</span>
                </div>
                <div class="party-info pq-pin-info pq-meta-info">
                  <span>👥 {{ formatMatchTicketMembers(ticket) }}</span>
                  <span v-if="ticket.created_at">⏳ {{ formatDateTime(ticket.created_at) }} 创建</span>
                </div>
                <p class="party-vote-hint pq-hint">队长开始匹配后才会进入匹配队列。</p>
                <p v-if="ticket.description" class="special-dungeon-flavor pq-flavor">{{ ticket.description }}</p>
                <div class="party-members pq-crests">
                  <span
                    v-for="m in ticket.members"
                    :key="ticket.id + '-' + m.adventurer_id"
                    class="member-badge pq-member-crest"
                    :class="memberTitleFrameClass(m)"
                    @click="openDetail(m)"
                  >
                    {{ m.name }}<small> Lv.{{ m.level }}</small>
                  </span>
                </div>
                <div class="party-actions pq-actions">
                  <button
                    type="button"
                    class="btn-secondary btn-sm"
                    :disabled="matchmakingLoading || playableMyAdventurers.length === 0"
                    @click="doJoinPublicMatchTicket(ticket)"
                  >加入小队</button>
                </div>
              </div>
            </div>
          </div>
          <div class="matchmaking-tickets pq-invites-board">
            <div class="party-section-head pq-board-head">
              <h4>我收到的邀请</h4>
              <span>{{ store.matchInvites.length }} 条待处理</span>
            </div>
            <div v-if="store.matchInvites.length === 0" class="empty hint pq-empty pq-empty-inline"><span class="pq-empty-icon">📭</span>暂无待处理邀请</div>
            <div v-else class="party-list pq-invite-grid">
              <div v-for="inv in store.matchInvites" :key="'invite-' + inv.id" class="party-card match-ticket-card pq-invite-letter">
                <div class="party-head pq-invite-head">
                  <span class="party-name pq-invite-name">小队邀请 {{ inv.ticket?.invite_code ? '#' + inv.ticket.invite_code : '' }}</span>
                  <span class="party-status pq-status-crest st-forming">待处理</span>
                </div>
                <div v-if="inv.ticket" class="pq-dungeon-row">
                  <span class="pq-dungeon-icon" aria-hidden="true">🗺️</span>
                  <span class="pq-dungeon-name">{{ matchTicketDungeonName(inv.ticket) }}</span>
                  <span v-if="matchTicketDungeonLevelDesc(inv.ticket)" class="pq-dungeon-level">推荐 {{ matchTicketDungeonLevelDesc(inv.ticket) }}</span>
                </div>
                <div class="party-info pq-invite-info pq-meta-info">
                  <span>👥 {{ inv.ticket ? formatMatchTicketMembers(inv.ticket) : '0 人' }}</span>
                  <span v-if="inv.created_at">⏳ {{ formatDateTime(inv.created_at) }}</span>
                </div>
                <p v-if="inv.ticket?.description" class="special-dungeon-flavor pq-flavor">{{ inv.ticket.description }}</p>
                <div class="party-actions pq-actions">
                  <button type="button" class="btn-primary btn-sm" :disabled="matchmakingLoading" @click="doAcceptMatchInvite(inv.id)">接受邀请</button>
                  <button type="button" class="btn-secondary btn-sm" :disabled="matchmakingLoading" @click="doDeclineMatchInvite(inv.id)">拒绝</button>
                </div>
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- 特殊机缘：持票冒险者可手动组建小队（仅特殊标签页） -->
      <div v-if="partyBoardTab === 'special'" class="special-dungeon-section pq-special-rift">
        <h3 class="col-title pq-section-title">✨ 特殊机缘副本</h3>
        <p class="special-dungeon-hint pq-board-hint">
          每队 4 人，集齐持票队员后开始出发倒计时。门票须放在随身栏，出发时扣除；存放在仓库中的门票无法使用。转职、顿悟与祛厄门票可由寻宝获得。
        </p>
        <div v-if="visibleSpecialDungeons.length === 0" class="empty hint pq-empty pq-empty-inline"><span class="pq-empty-icon">🌑</span>暂无可前往的特殊副本</div>
        <div v-else class="special-dungeon-list pq-rift-grid">
          <div v-for="d in visibleSpecialDungeons" :key="d.name" class="special-dungeon-card pq-rift-card">
            <div class="party-head pq-rift-head">
              <span class="party-name pq-rift-name">🗺️ {{ d.name }}</span>
              <span class="party-status pq-status-crest st-forming">{{ d.level_label }}</span>
            </div>
            <p class="special-dungeon-flavor pq-flavor">{{ d.flavor }}</p>
            <div class="party-info pq-rift-info">
              <span>👥 固定 {{ d.target_size ?? 4 }} 人</span>
              <span>🎟️ 需要门票：{{ d.ticket_item }}</span>
            </div>
            <div class="party-actions pq-actions">
              <button
                type="button"
                class="btn-primary btn-sm"
                :disabled="d.entry_enabled === false || creatingSpecialDungeon !== null || playableMyAdventurers.length === 0 || d.holds_ticket === false"
                @click.stop="doCreateSpecialParty(d)"
              >
                {{ d.entry_enabled === false ? '尚未开放' : d.holds_ticket === false ? '🎟️ 缺少门票' : '⚔️ 组建小队' }}
              </button>
            </div>
          </div>
        </div>
      </div>

      <h3 v-if="partyBoardTab === 'special'" class="col-title browse-party-title pq-section-title pq-browse-title">📋 浏览特殊机缘小队</h3>
      <div v-if="partyBoardTab === 'special'" class="filter-bar pq-filter-bar">
        <select v-model="partyStatus" @change="searchParties">
          <option value="active">活跃小队</option>
          <option value="forming">集结中</option>
          <option value="exploring">探险中</option>
          <option value="disbanded">已解散</option>
          <option value="all">全部</option>
        </select>
      </div>
      <div v-if="partyBoardTab === 'special' && store.partiesLoading && store.parties.length === 0" class="empty pq-empty"><span class="pq-empty-icon">⏳</span><span>加载中…</span></div>
      <div v-else-if="partyBoardTab === 'special' && displayedBrowseParties.length === 0" class="empty pq-empty">
        <span class="pq-empty-icon">🌌</span><span>暂无其他特殊机缘小队</span>
      </div>
      <div v-else-if="partyBoardTab === 'special'" class="party-list pq-party-grid">
        <div v-for="p in displayedBrowseParties" :key="p.id" class="party-card pq-party-scroll pq-scroll-browse">
          <div class="party-head pq-scroll-head">
            <span class="party-name pq-scroll-name">{{ p.name }}</span>
            <span class="party-status pq-status-crest" :class="'st-' + p.status">{{ partyStatusLabel(p.status) }}</span>
          </div>
          <div class="pq-dungeon-row">
            <span class="pq-dungeon-icon" aria-hidden="true">🗺️</span>
            <span class="pq-dungeon-name">{{ p.target_dungeon || '随机地图' }}</span>
            <span v-if="p.target_dungeon_level_desc" class="pq-dungeon-level">推荐 {{ p.target_dungeon_level_desc }}</span>
          </div>
          <div class="party-info pq-scroll-info pq-meta-info">
            <span>👥 {{ p.member_count }}/{{ p.target_size }}</span>
            <span v-if="p.departure_time" class="party-departure pq-departure">⏰ {{ formatDateTime(p.departure_time) }} 出发</span>
          </div>
          <div class="party-members pq-crests">
            <template v-for="m in p.members" :key="m.id">
              <span
                class="member-badge pq-member-crest"
                :class="memberTitleFrameClass(m)"
                @click="openDetail(m)"
              >
                {{ m.name }}<small> HP {{ m.hp }}/{{ m.max_hp }}</small>
                <button
                  v-if="isAdmin && (p.status === 'forming' || p.status === 'exploring')"
                  type="button"
                  class="btn-leave-mini btn-admin-remove"
                  :disabled="adminRemovingAdvId === m.id"
                  :title="'管理员移除 ' + m.name"
                  @click.stop="doAdminRemoveMember(p, m)"
                >⊖</button>
              </span>
            </template>
          </div>

          <div v-if="canShowJoinSection(p)" class="party-actions pq-actions">
            <button
              type="button"
              class="btn-secondary btn-sm"
              :disabled="joiningAdvId !== null"
              @click.stop="doJoinMyAdventurer(p)"
            >
              🚪 加入小队
            </button>
          </div>

          <div v-if="isAdmin && (p.status === 'forming' || p.status === 'exploring')" class="party-actions party-admin-actions pq-actions pq-admin-actions">
            <button
              v-if="p.status === 'forming'"
              type="button"
              class="btn-warning btn-sm"
              :disabled="adminPartyLoading"
              @click.stop="doForceDispatch(p)"
            >⚡ 强制出发</button>
            <button
              type="button"
              class="btn-danger btn-sm"
              :disabled="adminPartyLoading"
              @click.stop="doForceDisband(p)"
            >💥 强制解散</button>
          </div>
        </div>
      </div>
      <Pagination v-if="partyBoardTab === 'special' && store.partiesPagination" :pagination="store.partiesPagination" @go="goPartyPage" />
    </section>

    <!-- ───────── 星灯礼堂：生日纪念仪式与既有命运牌桌活动 ───────── -->
    <section v-if="activeTab === 'activity'" class="tab-content">
      <StarlightCeremonyHall />
    </section>

    <!-- ───────── 管理面板 ───────── -->
    <section v-if="activeTab === 'admin' && canViewAdmin" class="tab-content">
      <h3 class="col-title">🔧 管理工具</h3>
      <AdminHubPanel v-if="homeInitialReadFinished" @open-asset="onOpenAdminAsset" />
    </section>

    <DataAppealModal
      v-if="showDataAppeal"
      :initial-tab="dataAppealInitialTab"
      @close="onDataAppealClose"
      @submitted="onDataAppealSubmitted"
      @open-relief="openTavernReliefFromDataAppeal"
      @open-projects="openAppealProjectsFromDataAppeal"
    />
    <TavernReliefModal
      v-if="showTavernRelief"
      @close="showTavernRelief = false"
      @claimed="onTavernReliefClaimed"
    />
    <AppealProjectsModal
      v-if="showAppealProjects"
      @close="showAppealProjects = false"
      @changed="onAppealProjectsChanged"
    />
    <TitlePreviewModal
      v-if="showTitlePreview && store.currentDetail?.title_previews?.length"
      :previews="store.currentDetail.title_previews"
      @close="showTitlePreview = false"
    />
    <CodenameTextCorrectionModal
      v-if="textCorrectionAdvId != null"
      :adv-id="textCorrectionAdvId"
      @close="textCorrectionAdvId = null"
      @applied="onTextCorrectionApplied"
    />
    <CompanionEvolutionLedgerModal
      v-if="showCompanionEvolutionLedger"
      @close="showCompanionEvolutionLedger = false"
    />
    <div v-if="showSupportVoteModal" class="support-vote-mask s1-mobile-surface s1-mobile-overlay" @click.self="showSupportVoteModal = false">
      <div class="support-vote-modal s1-mobile-dialog" role="dialog" aria-modal="true" aria-labelledby="support-vote-title">
        <header class="support-vote-head s1-mobile-dialog-header">
          <div>
            <p class="support-vote-kicker">深渊酒馆 S1</p>
            <h4 id="support-vote-title">{{ store.supportVoteStatus?.title || '投票' }}</h4>
          </div>
          <button type="button" class="support-vote-close" @click="showSupportVoteModal = false">✕</button>
        </header>
        <div class="support-vote-body s1-mobile-dialog-body">
          <p class="support-vote-desc">
            {{ store.supportVoteStatus?.content || '有存活冒险者绑定的玩家可以参与当前酒馆投票，已投过也可以随时改。' }}
          </p>
          <p v-if="store.supportVoteStatus?.deadline" class="support-vote-deadline">
            截止：{{ formatDateTime(store.supportVoteStatus.deadline) }}
          </p>
          <div v-if="supportVoteOptions.length > 0" class="support-vote-options">
            <button
              v-for="opt in supportVoteOptions"
              :key="opt.key"
              type="button"
              :class="['support-vote-option', { selected: selectedSupportVoteOption === opt.key }]"
              @click="selectedSupportVoteOption = opt.key"
            >
              <strong>{{ opt.label }}</strong>
              <span>{{ opt.description }}</span>
            </button>
          </div>
          <div v-else class="support-vote-empty">
            <strong>{{ store.supportVoteStatus?.is_expired ? '投票已截止' : '当前没有可参与的投票' }}</strong>
          </div>
        </div>
        <footer class="support-vote-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-secondary btn-sm" @click="showSupportVoteModal = false">关闭</button>
          <button
            v-if="supportVoteOptions.length > 0"
            type="button"
            class="btn-primary btn-sm"
            :disabled="store.supportVoteLoading || !selectedSupportVoteOption"
            @click="doSupportVote"
          >{{ store.supportVoteLoading ? '提交中…' : '保存投票' }}</button>
        </footer>
      </div>
    </div>
    <AdventurerDetailModal
      v-if="detailAdvId != null"
      :adv-id="detailAdvId"
      :current-user-id="currentUserId"
      :elevated="detailStackedOnLog"
      @close="closeDetail"
      @changed="onDetailChanged"
      @view-logs="viewAdvLogs"
    />
    <AdventurerLogsModal
      v-if="logsAdv != null"
      :adv-id="logsAdv.id"
      :adv-name="logsAdv.name"
      :elevated="detailStackedOnLog"
      @close="logsAdv = null"
      @open-log="openLogFromAdventurerLogs"
    />
    <ExpeditionLogModal
      v-if="logModalId != null"
      :key="logModalId"
      :log-id="logModalId"
      :is-admin="isAdmin"
      @close="closeLog"
      @action-done="onLogActionDone"
      @open="openDetailFromLog"
    />
    <BuildingDetailModal
      v-if="detailBuilding != null"
      :building="detailBuilding"
      @close="detailBuilding = null"
      @refreshed="() => refreshBuildings(true)"
    />
    <DormRoomModal
      v-if="dormRoomId != null"
      :room-id="dormRoomId"
      :swap-mode="swapMode"
      :swap-first="swapFirst"
      :current-user-id="currentUserId"
      @close="dormRoomId = null"
      @bed-click="onBedClick"
    />

    <div
      v-if="undoTargetActivity"
      class="ai-result-mask s1-mobile-surface s1-mobile-overlay"
      @click.self="closeUndoItemModal"
    >
      <div class="ai-result-box tavern-modal s1-mobile-dialog">
        <h4 class="ai-result-title">{{ undoTargetActivity.undo_kind === 'tailor_workshop' ? '🪡' : '⚒️' }} 选择要恢复的物品</h4>
        <p class="tavern-modal-hint">
          撤回后会收回本次{{ undoTargetActivity.undo_kind === 'tailor_workshop' ? '裁制' : '锻造' }}成品，并恢复下面所选的一件投入物品；其他投入、金币和精力不会退还。
        </p>
        <div class="tavern-pick-list">
          <button
            v-for="option in undoTargetActivity.undo_item_options ?? []"
            :key="option.index"
            type="button"
            class="tavern-pick-item"
            :class="{ selected: selectedUndoItemIndex === option.index }"
            :disabled="undoingActivityId !== null"
            @click="selectedUndoItemIndex = option.index"
          >
            <span class="pick-main">{{ option.name }}</span>
            <small class="pick-sub">
              {{ option.type || '未分类' }} · {{ rarityLabel(option.rarity) }}
            </small>
          </button>
        </div>
        <div class="ai-result-actions tavern-modal-actions s1-mobile-dialog-footer">
          <button
            type="button"
            class="btn-secondary btn-sm"
            :disabled="undoingActivityId !== null"
            @click="closeUndoItemModal"
          >取消</button>
          <button
            type="button"
            class="btn-primary btn-sm"
            :disabled="selectedUndoItemIndex === null || undoingActivityId !== null"
            @click="submitCraftUndo"
          >{{ undoingActivityId !== null ? '撤回中…' : '确认撤回并恢复此物品' }}</button>
        </div>
      </div>
    </div>

    <!-- 酒馆点酒弹窗：酒单 + 离座 -->
    <!-- 入座没有弹窗：玩家只绑定一个冒险者，点空位直接用绑定的存活冒险者入座 -->
    <div v-if="orderTargetSeat" class="ai-result-mask s1-mobile-surface s1-mobile-overlay" @click.self="orderTargetSeat = null">
      <div class="ai-result-box tavern-modal s1-mobile-dialog">
        <h4 class="ai-result-title">🍽️ 给 {{ orderTargetSeat.adventurer?.name }} 点单</h4>
        <p class="tavern-modal-hint">
          <template v-if="orderAdvGold != null">冒险者钱包：💰{{ orderAdvGold.toFixed(2) }} 金币。</template>
          饮品与食物可以同时上桌；换餐会先按已吃时间结算旧食物，中途离座不退款。
        </p>
        <h5 class="tavern-menu-title">食物 · 缓慢恢复，吃完或离座时结算</h5>
        <div class="tavern-pick-list">
          <button
            v-for="food in store.foodMenu"
            :key="food.key"
            type="button"
            class="tavern-pick-item"
            :disabled="seatActionLoading || (orderAdvGold != null && orderAdvGold < food.price)"
            @click="doOrderFood(food)"
          >
            <span class="pick-main">{{ food.icon }} {{ food.name }} <strong class="drink-price">{{ food.price }} 金</strong></span>
            <small class="pick-sub">
              {{ food.duration_minutes }} 分钟 ·
              <template v-if="food.stamina_restore">体力 +{{ food.stamina_restore }}</template>
              <template v-if="food.stamina_restore && food.hp_restore">、</template>
              <template v-if="food.hp_restore">生命 +{{ food.hp_restore }}</template>
              · {{ food.desc }}
            </small>
          </button>
        </div>
        <h5 class="tavern-menu-title">饮品 · 纯展示，不影响数值</h5>
        <div class="tavern-pick-list">
          <button
            v-for="d in store.drinkMenu"
            :key="d.key"
            type="button"
            class="tavern-pick-item"
            :disabled="seatActionLoading || (orderAdvGold != null && orderAdvGold < d.price)"
            @click="doOrder(d)"
          >
            <span class="pick-main">{{ d.icon }} {{ d.name }} <strong class="drink-price">{{ d.price }} 金</strong></span>
            <small class="pick-sub">{{ d.desc }}</small>
          </button>
        </div>
        <div class="ai-result-actions tavern-modal-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-secondary btn-sm" :disabled="seatActionLoading" @click="doLeaveSeat">🚪 离座</button>
          <button type="button" class="btn-secondary btn-sm" @click="orderTargetSeat = null">取消</button>
        </div>
      </div>
    </div>

    <!-- 治疗所/铁匠铺 AI 结果弹窗 -->
    <div v-if="treatTargetSeat" class="ai-result-mask s1-mobile-surface s1-mobile-overlay" @click.self="treatTargetSeat = null">
      <div class="ai-result-box tavern-modal s1-mobile-dialog">
        <h4 class="ai-result-title">请 {{ treatTargetSeat.adventurer?.name }} 喝一杯</h4>
        <p class="tavern-modal-hint">
          <template v-if="treatPayerGold != null">你的冒险者钱包：{{ treatPayerGold.toFixed(2) }} 金币。</template>
          对方接受后才会扣钱，饮品会放到对方座位上。
        </p>
        <div class="tavern-pick-list">
          <button
            v-for="d in store.drinkMenu"
            :key="d.key"
            type="button"
            class="tavern-pick-item"
            :disabled="seatActionLoading || (treatPayerGold != null && treatPayerGold < d.price)"
            @click="doTreat(d)"
          >
            <span class="pick-main">{{ d.icon }} {{ d.name }} <strong class="drink-price">{{ d.price }} 金</strong></span>
            <small class="pick-sub">{{ d.desc }}</small>
          </button>
        </div>
        <div class="ai-result-actions tavern-modal-actions s1-mobile-dialog-footer">
          <button type="button" class="btn-secondary btn-sm" @click="treatTargetSeat = null">取消</button>
        </div>
      </div>
    </div>

    <div v-if="buildingAiResult" class="ai-result-mask s1-mobile-surface s1-mobile-overlay" @click.self="buildingAiResult = null">
      <div class="ai-result-box s1-mobile-dialog">
        <h4 class="ai-result-title">
          {{ buildingAiResult.type === 'tailor_workshop'
            ? '🪡 裁制结果'
            : (buildingAiResult.type === 'blacksmith'
            ? '⚒️ 锻造结果'
            : (buildingAiResult.type === 'alchemy_workshop'
              ? '⚗️ 炼金结果'
              : (buildingAiResult.type === 'item_assembly_workshop'
                ? '🧩 构装结果'
                : (buildingAiResult.type === 'ember_kitchen'
                  ? '🍲 烹饪结果'
                  : (buildingAiResult.type === 'pet_evolution'
                    ? '🧬 嬗变结果'
                    : (buildingAiResult.type === 'skill_training' ? '📜 一课所得' : '🩺 治疗结果')))))) }}
          <span v-if="buildingAiResult.adventurer_name" class="ai-result-adv">{{ buildingAiResult.adventurer_name }}</span>
        </h4>
        <template v-if="buildingAiResult.system_abort">
          <div class="ai-result-status fail">
            {{ buildingAiResult.type === 'skill_training'
              ? '❌ 导师没能把这堂课授成'
              : (buildingAiResult.type === 'ember_kitchen'
                ? '⚠️ 本次烹饪未能完成'
                : '⚠️ 本次作业未能完成') }}
          </div>
          <div
            v-if="buildingAiResult.gold_refunded || buildingAiResult.stamina_refunded"
            class="ai-result-status ok"
          >
            ↩️ 已退还<span v-if="buildingAiResult.gold_refunded"> {{ buildingAiResult.gold_refunded }} 金币</span><span v-if="buildingAiResult.gold_refunded && buildingAiResult.stamina_refunded">、</span><span v-if="buildingAiResult.stamina_refunded">{{ buildingAiResult.stamina_refunded }} 精力</span>
          </div>
          <div v-if="buildingAiResult.items_preserved" class="ai-result-status ok">
            📦 投入物品均已原样保留
          </div>
        </template>
        <template v-else-if="buildingAiResult.type === 'pet_evolution'">
          <div :class="['ai-result-status', buildingAiResult.success ? 'ok' : 'fail']">
            <template v-if="buildingAiResult.success">
              ✅ 嬗变成功：{{ buildingAiResult.old_name }} → {{ buildingAiResult.new_name }}
              <span v-if="buildingAiResult.evolution_stage != null">（第 {{ buildingAiResult.evolution_stage }} 阶）</span>
            </template>
            <template v-else>
              ❌ 嬗变或孵化失败
              <span v-if="buildingAiResult.gold_refunded">（已退还 {{ buildingAiResult.gold_refunded }} 金币）</span>
            </template>
          </div>
          <div v-if="buildingAiResult.pet_preserved" class="ai-result-status ok">
            🐾 所选伙伴安然无恙，没有被消耗
          </div>
          <div v-if="buildingAiResult.auxiliary_material_consumed" class="ai-result-status fail">
            🔥 辅助素材「{{ buildingAiResult.auxiliary_material_name || '未知素材' }}」已消耗，不予返还
          </div>
        </template>
        <template v-else-if="buildingAiResult.type === 'skill_training'">
          <div :class="['ai-result-status', buildingAiResult.success ? 'ok' : 'fail']">
            <template v-if="buildingAiResult.success && buildingAiResult.skill">
              <template v-if="buildingAiResult.operation === 'advance' && buildingAiResult.old_skill">
                ✅ 旧招磨成：{{ buildingAiResult.old_skill.name }} → {{ buildingAiResult.skill.name }}
              </template>
              <template v-else>
                ✅ 学会新本事：{{ buildingAiResult.skill.name }}
              </template>
            </template>
            <template v-else>
              ❌ 导师没能把这堂课授成
              <span v-if="buildingAiResult.gold_refunded || buildingAiResult.stamina_refunded">
                （已退还<span v-if="buildingAiResult.gold_refunded"> {{ buildingAiResult.gold_refunded }} 金币</span><span v-if="buildingAiResult.gold_refunded && buildingAiResult.stamina_refunded">、</span><span v-if="buildingAiResult.stamina_refunded">{{ buildingAiResult.stamina_refunded }} 精力</span>）
              </span>
            </template>
          </div>
          <div v-if="buildingAiResult.success && buildingAiResult.skill" class="ai-result-modified">
            <strong>{{ buildingAiResult.skill.name }}</strong>
            <div class="ai-result-modified-desc">{{ buildingAiResult.skill.description }}</div>
          </div>
          <div
            v-if="buildingAiResult.reason"
            class="ai-result-modified"
          >
            <strong>师傅的话</strong>
            <div class="ai-result-modified-desc">{{ buildingAiResult.reason }}</div>
          </div>
          <div v-if="buildingAiResult.success && buildingAiResult.direction_fit === 'adjusted'" class="ai-result-status">
            🧭 导师顺着你的想法，改成了更合身的练法
          </div>
        </template>
        <template v-else-if="buildingAiResult.type === 'ember_kitchen'">
          <div :class="['ai-result-status', buildingAiResult.is_edible ? 'ok' : 'fail']">
            <template v-if="buildingAiResult.is_edible">
              ✅ 出锅：{{ buildingAiResult.new_name }} · 可食用 {{ buildingAiResult.uses_left ?? 1 }} 次
            </template>
            <template v-else>
              🗑️ 黑暗料理无法入口，已由掌勺人当场丢弃
            </template>
          </div>
          <div v-if="buildingAiResult.is_edible && buildingAiResult.new_desc" class="ai-result-modified">
            <strong>{{ buildingAiResult.new_name }}</strong>
            <div class="ai-result-modified-desc">{{ buildingAiResult.new_desc }}</div>
          </div>
          <div v-if="buildingAiResult.cookware_name" class="ai-result-status">
            🍴 使用 {{ buildingAiResult.cookware_name }} 烹制
          </div>
          <div v-if="buildingAiResult.ingredient_names?.length" class="ai-result-modified-desc">
            投入：{{ buildingAiResult.ingredient_names.join('、') }}
          </div>
        </template>
        <template v-else-if="isTransformCraftResult(buildingAiResult.type)">
          <div :class="['ai-result-status', buildingAiResult.success ? 'ok' : 'fail']">
            <template v-if="buildingAiResult.success">
              ✅ {{ transformCraftResultLabel(buildingAiResult.type, true) }}：{{ buildingAiResult.old_name }} → {{ buildingAiResult.new_name }}
            </template>
            <template v-else>
              ❌ {{ transformCraftResultLabel(buildingAiResult.type, false) }}
              <template v-if="buildingAiResult.type === 'item_assembly_workshop' && buildingAiResult.unreasonable_input">，全部投入槽位均在构装过程中损毁</template>
              <template v-else-if="buildingAiResult.unreasonable_input">，不合理投入，材料全部损毁</template>
              <template v-if="buildingAiResult.type === 'item_assembly_workshop' && buildingAiResult.consumed_items?.length">；损毁：{{ buildingAiResult.consumed_items.map(formatAssemblyConsumedItem).join('、') }}</template>
              <template v-else-if="!buildingAiResult.unreasonable_input && buildingAiResult.consumed_item_names?.length">，{{ buildingAiResult.consumed_item_names.join('、') }}已损毁</template>
              <template v-else-if="buildingAiResult.consumed === false">，投入物品均完好取回</template>
              <span v-if="buildingAiResult.gold_refunded">（已退还 {{ buildingAiResult.gold_refunded }} 金币）</span>
            </template>
          </div>
          <div
            v-if="buildingAiResult.type === 'item_assembly_workshop' && buildingAiResult.success && buildingAiResult.direction_fit === 'adjusted'"
            class="ai-result-status"
          >🧭 构装师顺着你的想法，改成了材料真正撑得住的做法</div>
          <div
            v-if="buildingAiResult.type === 'item_assembly_workshop' && buildingAiResult.reason"
            class="ai-result-modified"
          >
            <strong>构装师的话</strong>
            <div class="ai-result-modified-desc">{{ buildingAiResult.reason }}</div>
          </div>
        </template>
        <template v-else>
          <div
            v-if="buildingAiResult.success === false"
            class="ai-result-status fail"
          >
            ❌ 治疗失败，特质未发生变化
            <span v-if="buildingAiResult.gold_refunded">（已退还 {{ buildingAiResult.gold_refunded }} 金币）</span>
          </div>
          <div v-if="buildingAiResult.healed_traits?.length" class="ai-result-status ok">
            ✅ 已治愈：{{ buildingAiResult.healed_traits.join('、') }}
          </div>
          <div
            v-for="m in (buildingAiResult.recovery_traits ?? buildingAiResult.modified_traits ?? [])"
            :key="m.name"
            class="ai-result-modified"
          >
            🛏️ {{ m.name }} → <strong>{{ m.new_name }}</strong>
            <div class="ai-result-modified-desc">{{ m.new_desc }}</div>
            <div v-if="m.stage" class="ai-result-modified-desc">
              回宿舍{{ m.policy === 'sleep' ? '睡眠' : '静养' }}后可自然恢复（还需 {{ m.remaining_cycles ?? (m.stage === 'medium' ? 2 : 1) }} 周期）
            </div>
          </div>
          <div
            v-if="buildingAiResult.carried_item_name"
            class="ai-result-status"
            :class="buildingAiResult.item_consumed ? 'fail' : 'ok'"
          >
            <template v-if="buildingAiResult.item_consumed">🧪 「{{ buildingAiResult.carried_item_name }}」已用于治疗，已消耗</template>
            <template v-else>🧪 「{{ buildingAiResult.carried_item_name }}」未派上用场，已原样归还</template>
          </div>
        </template>
        <p v-if="buildingAiResult.narrative" class="ai-result-narrative">{{ buildingAiResult.narrative }}</p>
        <div class="ai-result-actions s1-mobile-dialog-footer">
          <AiFeedbackControl
            v-if="buildingAiResult.ai_feedback"
            :target="buildingAiResult.ai_feedback"
            @updated="updateBuildingAiFeedback"
          />
          <button type="button" class="ai-result-close" @click="buildingAiResult = null">关闭</button>
        </div>
      </div>
    </div>

    <SquadModal
      v-if="showSquadModal"
      :adventurers="myAdventurers"
      @close="showSquadModal = false"
    />
  </div>
</template>

<script setup lang="ts">
import { tavernStorageKey } from '@/runtime/activeTavern'
import { ref, reactive, computed, watch, onMounted, onUnmounted, nextTick, defineAsyncComponent } from 'vue'
import { isS1BuildingVisible, isS1DungeonVisible } from '@/runtime/s1FeatureVisibility'
import { useAdventurerS1Store, isPlayableAdventurer, type AdventurerData, type DormBed, type DormRoom, type BuildingInstance, type BuildingCategory, type TavernSeat, type TavernDrinkMenuItem, type TavernFoodMenuItem, type BuildingUsageResult, type ActivityEntry, type AiFeedbackTarget } from '../stores/adventurerS1'
import { useUserStore } from '@/stores/user'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useSocket } from '@/composables/useSocket'
import { useLiveTick } from '../composables/useLiveTick'
import { useS1TitleEffects } from '../composables/useS1TitleEffects'
import { loadS1Home } from '../composables/s1HomeLoad'
import { renderTitle } from '@/utils/format'
import { formatDateTime, formatTime } from '@/utils/time'
import {
  logStatusLabel,
  getSpecialEventPresentation,
  isSpecialEventPresentationStatus,
  specialEventCardClass,
  specialEventStyle,
  partyStatusLabel,
  sourceTypeLabel,
  dormLevelInfo,
  dormDisplayLevel,
  dormRoomIcon,
  BUILDING_CODES_HIDDEN_FROM_ZONE_UI,
  TRANSCENDENT_ASSESSMENT_PREVIEW_CODE,
  TRANSCENDENT_ASSESSMENT_PREVIEW_DESCRIPTION,
  TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY,
  TRANSCENDENT_ASSESSMENT_PREVIEW_NAME,
  TRANSCENDENT_ASSESSMENT_PREVIEW_REQUIRED_LEVEL,
  isGeneratingStuckForGmRetry,
  rarityColor,
  rarityLabel,
} from '../utils'
import type { AdventurerTitle, ExpeditionLogSummary, MatchTicketData, PartyData } from '../stores/adventurerS1'
import Pagination from '@/components/Pagination.vue'
import AdventurerCard from '../components/AdventurerCard.vue'
import S1BetaNotice from '../components/S1BetaNotice.vue'
import AmbientNpcEncounter from '../components/AmbientNpcEncounter.vue'
const CreateAdventurerPanel = defineAsyncComponent(() => import('../components/CreateAdventurerPanel.vue'))
const ArchiveAppealPanel = defineAsyncComponent(() => import('../components/ArchiveAppealPanel.vue'))
const AdventurerDetailModal = defineAsyncComponent(() => import('../components/AdventurerDetailModal.vue'))
import '../styles/title-effects.scss'
const ExpeditionLogModal = defineAsyncComponent(() => import('../components/ExpeditionLogModal.vue'))
const AdventurerLogsModal = defineAsyncComponent(() => import('../components/AdventurerLogsModal.vue'))
const AdminReservationPanel = defineAsyncComponent(() => import('../components/AdminReservationPanel.vue'))
const AdminBuildingPanel = defineAsyncComponent(() => import('../components/AdminBuildingPanel.vue'))
const AdminLuckyWheelPanel = defineAsyncComponent(() => import('../components/AdminLuckyWheelPanel.vue'))
const AdminItemCatalogPanel = defineAsyncComponent(() => import('../components/AdminItemCatalogPanel.vue'))
import BuildingCard from '../components/BuildingCard.vue'
const BuildingDetailModal = defineAsyncComponent(() => import('../components/BuildingDetailModal.vue'))
const DormRoomModal = defineAsyncComponent(() => import('../components/DormRoomModal.vue'))
const MarketPanel = defineAsyncComponent(() => import('../components/MarketPanel.vue'))
const SecretAuctionPanel = defineAsyncComponent(() => import('../components/SecretAuctionPanel.vue'))
const TeamLootPanel = defineAsyncComponent(() => import('../components/TeamLootPanel.vue'))
import TeamLootNotice from '../components/TeamLootNotice.vue'
import EvolutionActivityInputs from '../components/EvolutionActivityInputs.vue'
const TaskBoardPanel = defineAsyncComponent(() => import('../components/TaskBoardPanel.vue'))
const CampaignHallPanel = defineAsyncComponent(() => import('../components/CampaignHallPanel.vue'))
const CommissionBoardPanel = defineAsyncComponent(() => import('../components/CommissionBoardPanel.vue'))
import type { AdminAssetPanel } from '../components/AdminHubPanel.vue'
const AdminHubPanel = defineAsyncComponent(() => import('../components/AdminHubPanel.vue'))
const DataAppealModal = defineAsyncComponent(() => import('../components/DataAppealModal.vue'))
const TavernReliefModal = defineAsyncComponent(() => import('../components/TavernReliefModal.vue'))
const AppealProjectsModal = defineAsyncComponent(() => import('../components/AppealProjectsModal.vue'))
const TitlePreviewModal = defineAsyncComponent(() => import('../components/TitlePreviewModal.vue'))
const CodenameTextCorrectionModal = defineAsyncComponent(() => import('../components/CodenameTextCorrectionModal.vue'))
const CompanionEvolutionLedgerModal = defineAsyncComponent(() => import('../components/CompanionEvolutionLedgerModal.vue'))
const SquadModal = defineAsyncComponent(() => import('../components/SquadModal.vue'))
import AiFeedbackControl from '../components/AiFeedbackControl.vue'
import { useAdminSecondaryAccess } from '@/composables/useAdminSecondaryAccess'
import {
  getAdminSecondaryAuthStatusReady,
  getAdminSecondarySocketReadGrant,
  getS1PlayerRealmView,
  syncAdminSecondaryAuthStatus,
  type S1PlayerRealmView,
} from '@/composables/useApi'
import TavernUpgradePanel from '../components/TavernUpgradePanel.vue'
const StarlightCeremonyHall = defineAsyncComponent(() => import('../components/StarlightCeremonyHall.vue'))

type TabId = 'tavern' | 'activity' | 'building' | 'my-adventurers' | 'adventurers' | 'party' | 'logs' | 'admin'
type MatchTicketDisplayData = Partial<Pick<
  MatchTicketData,
  | 'allow_solo_fill'
  | 'matched_party_dungeon'
  | 'matched_party_dungeon_level_desc'
  | 'member_count'
  | 'preferred_dungeon'
  | 'preferred_dungeon_level_desc'
  | 'preferred_dungeons'
  | 'queue_type'
>>
type AdventurerDetailTarget =
  | { id: number; adventurer_id?: number }
  | { id?: number; adventurer_id: number }

const ALL_TABS: { id: TabId; icon: string; label: string; adminOnly?: boolean }[] = [
  { id: 'tavern', icon: '🍺', label: '酒馆' },
  { id: 'activity', icon: '✦', label: '星灯礼堂' },
  { id: 'building', icon: '🏠', label: '建筑' },
  { id: 'my-adventurers', icon: '👤', label: '我的' },
  { id: 'adventurers', icon: '🧙', label: '冒险者' },
  { id: 'party', icon: '🏰', label: '小队' },
  { id: 'logs', icon: '📜', label: '探险日志' },
  { id: 'admin', icon: '🔧', label: '管理', adminOnly: true },
]
const store = useAdventurerS1Store()
const userStore = useUserStore()
const message = useGlobalMessage()
const requestedPlayerRealmView = getS1PlayerRealmView()
const delaySocketUntilRealmResolved = requestedPlayerRealmView != null

const {
  isAdminAccount: canViewAdmin,
  canUseFullAdminUi: isAdmin,
  scope: adminSecondaryScope,
} = useAdminSecondaryAccess()
const adminSecondaryAuthStatusReady = getAdminSecondaryAuthStatusReady()
const adminSecondarySocketReadGrant = getAdminSecondarySocketReadGrant()
const TABS = computed(() => ALL_TABS.filter(t => !t.adminOnly || canViewAdmin.value))

const TASK_HINT_STORAGE_KEY = tavernStorageKey('adventurer_s1_task_board_hint')

interface TaskBoardHintAck {
  /** 上次进入任务栏时已看到的可接任务 ID */
  seenIds: string[]
}

function getTaskHintStorageKey(): string {
  const uid = userStore.user?.id
  return uid != null ? `${TASK_HINT_STORAGE_KEY}_${uid}` : TASK_HINT_STORAGE_KEY
}

function loadTaskHintAck(): TaskBoardHintAck {
  try {
    const raw = localStorage.getItem(getTaskHintStorageKey())
    if (raw) {
      const parsed = JSON.parse(raw) as Partial<TaskBoardHintAck> & { count?: number }
      if (Array.isArray(parsed.seenIds)) {
        return {
          seenIds: parsed.seenIds.filter((id): id is string => typeof id === 'string'),
        }
      }
      // 旧版按数量记录：无法还原具体任务，视为未读以便新版本能提醒
      if (typeof parsed.count === 'number') {
        return { seenIds: [] }
      }
    }
  } catch {
    /* ignore */
  }
  return { seenIds: [] }
}

const taskHintAck = ref<TaskBoardHintAck>(loadTaskHintAck())

function saveTaskHintAck() {
  localStorage.setItem(getTaskHintStorageKey(), JSON.stringify(taskHintAck.value))
}

function hasUnseenTaskBoardHint(): boolean {
  return taskBoardUnseenIds().length > 0
}

function taskBoardUnseenIds(): string[] {
  const ids = store.taskAcceptableIds
  if (ids.length === 0) return []
  const seen = new Set(taskHintAck.value.seenIds)
  return ids.filter(id => !seen.has(id))
}

function acknowledgeTaskBoardHint() {
  taskHintAck.value = {
    seenIds: [...store.taskAcceptableIds],
  }
  saveTaskHintAck()
}

const showBuildingTaskHint = computed(() => {
  if (activeTab.value === 'building' && currentZone.value === 'task') return false
  return hasUnseenTaskBoardHint()
})

const taskBoardHintBadgeCount = computed(() => taskBoardUnseenIds().length)

const CHAT_MAX = 300

const STORAGE_KEY = 'adventurer_s1_active_tab'
function readSavedTab(): TabId | null {
  try {
    return localStorage.getItem(STORAGE_KEY) as TabId | null
  } catch {
    return null
  }
}
const savedTab = readSavedTab()
const tabNavigation = ref<HTMLElement | null>(null)
const tabAnchor = ref<HTMLElement | null>(null)
const mineToolsExpanded = ref(false)
// admin tab 在 user 加载前不在 TABS 中，需单独放行
const activeTab = ref<TabId>(savedTab && (savedTab === 'admin' || TABS.value.some(t => t.id === savedTab)) ? savedTab : 'adventurers')
const teamLootPanel = ref<InstanceType<typeof TeamLootPanel> | null>(null)
const showDormitory = ref(false)
const showAdmin = ref(false)
const showBuildingAdmin = ref(false)
const showLuckyWheelAdmin = ref(false)
const showItemCatalogAdmin = ref(false)
const ambientNpcSurface = computed(() => {
  if (activeTab.value === 'admin') return null
  if (
    activeTab.value === 'building'
    && (
      showAdmin.value
      || showBuildingAdmin.value
      || showLuckyWheelAdmin.value
      || showItemCatalogAdmin.value
    )
  ) return null
  return activeTab.value
})
const showSquadModal = ref(false)

watch(() => store.squadFeatureEnabled, enabled => {
  if (!enabled) showSquadModal.value = false
})

watch(isAdmin, fullAdmin => {
  if (fullAdmin) return
  showAdmin.value = false
  showBuildingAdmin.value = false
  showLuckyWheelAdmin.value = false
  showItemCatalogAdmin.value = false
})

const detailBuilding = ref<BuildingInstance | null>(null)
const dormRoomId = ref<number | null>(null)

// 异步设施结果弹窗（socket 推送，只弹自己冒险者的结果）
// 结构与最近动态里持久化的 BuildingUsageResult 相同，弹窗额外带冒险者名
interface BuildingAiResult extends BuildingUsageResult {
  adventurer_name?: string
}
const buildingAiResult = ref<BuildingAiResult | null>(null)
const undoingActivityId = ref<number | null>(null)
const undoTargetActivity = ref<ActivityEntry | null>(null)
const selectedUndoItemIndex = ref<number | null>(null)

function updateActivityAiFeedback(entry: ActivityEntry, target: AiFeedbackTarget) {
  entry.ai_feedback = target
}

function updateBuildingAiFeedback(target: AiFeedbackTarget) {
  if (buildingAiResult.value) buildingAiResult.value.ai_feedback = target
}

// ── 最近动态：建筑使用结果徽标 ──
// 治疗成功但没有特质变化时单独给「无需治疗」中性徽标，避免误导玩家以为治了什么；
// 治疗/锻造在结果持久化上线前的历史记录（legacy_no_result）显示「结果未记录」
function isTransformCraftResult(type: unknown): boolean {
  return type === 'blacksmith'
    || type === 'tailor_workshop'
    || type === 'alchemy_workshop'
    || type === 'item_assembly_workshop'
}

function transformCraftResultLabel(type: unknown, success: boolean): string {
  const action = type === 'tailor_workshop'
    ? '裁制'
    : type === 'alchemy_workshop'
      ? '炼金'
      : type === 'item_assembly_workshop'
        ? '构装'
        : '锻造'
  return `${action}${success ? '成功' : '失败'}`
}

function activityBadgeLabel(e: ActivityEntry): string {
  const r = e.result
  if (!r) return e.legacy_no_result ? '结果未记录' : '使用完成'
  if (r.undo) return '已撤回'
  if (r.type === 'healing') {
    if (!r.success) return '治疗失败'
    const recovery = r.recovery_traits ?? r.modified_traits ?? []
    const n = (r.healed_traits?.length ?? 0) + recovery.length
    return n > 0 ? '治疗成功' : '治疗失败'
  }
  if (r.type === 'blacksmith') return r.success ? '锻造成功' : '锻造失败'
  if (r.type === 'tailor_workshop') return r.success ? '裁制成功' : '裁制失败'
  if (r.type === 'alchemy_workshop') return r.success ? '炼金成功' : '炼金失败'
  if (r.type === 'item_assembly_workshop') return r.success ? '构装成功' : '构装失败'
  if (r.type === 'ember_kitchen') {
    if (r.system_abort) return '未能开灶'
    return r.is_edible ? '烹饪完成' : '已丢弃'
  }
  if (r.type === 'pet_evolution') return r.success ? '嬗变成功' : '嬗变失败'
  if (r.type === 'skill_training') {
    if (!r.success) return '未能授成'
    return r.operation === 'advance' ? '磨炼有成' : '学有所成'
  }
  return r.success ? '完成' : '失败'
}

function formatAssemblyConsumedItem(item: { slot_id: number; position: string; name: string }): string {
  return `槽位 ${item.slot_id}（${item.position}）「${item.name}」`
}

function appealItemActivityLabel(e: ActivityEntry): string {
  if (e.change_type !== 'gain' && e.change_type !== 'overflow') return ''
  if (e.source_type === 'data_appeal_ai') return '申诉补发'
  if (e.source_type === 'data_appeal_reclaim') return '申诉归还'
  return ''
}

function openAppealActivityLog(e: ActivityEntry) {
  const expeditionLogId = Number(e.expedition_log_id)
  if (!Number.isInteger(expeditionLogId) || expeditionLogId <= 0) return
  openLog(expeditionLogId)
}

/** 把描述文本中的「物品名」替换为带稀有度颜色的版本（v-html 用，已转义防 XSS） */
function colorItemName(desc: string, itemName: string, rarity: string): string {
  const esc = (s: string) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;')
  const color = rarityColor(rarity)
  const escapedName = esc(itemName)
  const colored = `<strong style="color:${color}">${escapedName}</strong>`
  // 替换「itemName」为着色版本
  return esc(desc).replace(`「${escapedName}」`, `「${colored}」`)
}

function activityBadgeClass(e: ActivityEntry): string {
  const r = e.result
  if (!r) return 'neutral'
  if (r.undo) return 'neutral'
  if (!r.success) return 'fail'
  if (r.type === 'healing') {
    const recovery = r.recovery_traits ?? r.modified_traits ?? []
    const n = (r.healed_traits?.length ?? 0) + recovery.length
    if (!r.success || n <= 0) return 'fail'
    return 'ok'
  }
  return 'ok'
}

function closeUndoItemModal() {
  if (undoingActivityId.value !== null) return
  undoTargetActivity.value = null
  selectedUndoItemIndex.value = null
}

async function submitCraftUndo() {
  const entry = undoTargetActivity.value
  const itemIndex = selectedUndoItemIndex.value
  if (!entry || itemIndex === null) return
  await performBuildingUndo(entry, itemIndex)
}

async function undoBuildingActivity(e: ActivityEntry) {
  if (!e.undo_available || undoingActivityId.value !== null) return
  if (
    (e.undo_kind === 'blacksmith' || e.undo_kind === 'tailor_workshop')
    && e.undo_item_options?.length
  ) {
    undoTargetActivity.value = e
    selectedUndoItemIndex.value = e.undo_item_options[0]?.index ?? null
    return
  }
  const target = e.undo_kind === 'blacksmith'
    ? '最近一次铁匠铺锻造'
    : e.undo_kind === 'tailor_workshop'
      ? '最近一次绮罗裁坊裁制'
      : e.undo_kind === 'pet_evolution'
      ? `最近一次伙伴嬗变（「${e.result?.new_name || '当前形态'}」恢复为「${e.result?.old_name || '原形态'}」）`
      : '最近一次百艺阁技能进阶'
  const restoredState = e.undo_kind === 'pet_evolution'
    ? (e.undo_restore_mode === 'legacy_known_fields'
        ? '按旧记录恢复可确认的名称、描述、稀有度和阶级；旧记录未保存的其他字段保持当前值'
        : '完整恢复伙伴嬗变前的状态（包括名称、描述、稀有度和阶级等）')
    : '恢复增强前的技能或所选制作投入物品'
  const confirmed = window.confirm(
    `确定撤回${target}吗？将${restoredState}；已消耗的金币、精力和材料不会退还。`,
  )
  if (!confirmed) return
  await performBuildingUndo(e)
}

async function performBuildingUndo(e: ActivityEntry, itemIndex?: number) {
  undoingActivityId.value = e.id
  try {
    const res = await store.undoBuildingActivity(e.id, itemIndex)
    if (res?.code !== 200) {
      message.error(res?.message || '撤回失败')
      return
    }
    const restoredItem = res?.data?.restored_item
    const restoredCompanion = res?.data?.restored_companion
    message.success(
      restoredItem
        ? `已撤回${e.undo_kind === 'tailor_workshop' ? '裁制' : '锻造'}并恢复「${restoredItem}」，其余成本不退还`
        : restoredCompanion
          ? `已撤回嬗变并恢复伙伴「${restoredCompanion}」，其余成本不退还`
          : '已撤回最近一次结果，消费成本不退还',
    )
    undoTargetActivity.value = null
    selectedUndoItemIndex.value = null
    await Promise.allSettled([
      store.fetchMyActivity(true),
      store.fetchAdventurers(true),
    ])
  } catch (err: any) {
    message.error(err?.response?.data?.message || err?.message || '撤回失败')
  } finally {
    undoingActivityId.value = null
  }
}

const myLoaded = ref(false)
const showCreatePanel = ref(false)
const showDataAppeal = ref(false)
const showTavernRelief = ref(false)
const showAppealProjects = ref(false)
const showTitlePreview = ref(false)
const showCompanionEvolutionLedger = ref(false)
const titlePreviewLoading = ref(false)
const showSupportVoteModal = ref(false)
const selectedSupportVoteOption = ref('')
const dataAppealInitialTab = ref<'submit' | 'history'>('submit')
const pendingRevertOffers = ref<Array<{
  id: number
  adventurer_name?: string
  revert_offer_note?: string | null
}>>([])
const pendingPlaceholderRecallOffers = ref<Array<{
  id: number
  adventurer_name?: string
  placeholder_skills?: Array<{ id?: number; name?: string }>
  scroll_grant_count?: number
  scroll_name?: string
  admin_note?: string | null
}>>([])
const placeholderRecallLoadingId = ref<number | null>(null)
interface WaitingNpcOriginState {
  has_origin: boolean
  can_reset: boolean
  status: string | null
  blocked_reason?: string | null
  inherited?: {
    character_class?: string | null
    equipment_count?: number
    skill_count?: number
    trait_count?: number
    has_history?: boolean
  }
}
const waitingNpcOrigin = ref<WaitingNpcOriginState | null>(null)
const waitingNpcOriginLoading = ref(false)
const resettingWaitingNpcOrigin = ref(false)
let waitingNpcOriginAdvId: number | null = null
const textCorrectionAdvId = ref<number | null>(null)
const detailAdvId = ref<number | null>(null)
/** 从探险日志内点成员名打开详情时叠在日志之上 */
const detailStackedOnLog = ref(false)
const logModalId = ref<number | null>(null)
// 名片「📜 探险」弹窗：不切换 tab，原地查看该冒险者的探险日志
const logsAdv = ref<{ id: number; name: string } | null>(null)
const logSearch = ref('')
const appliedLogSearch = ref('')
type LogStatusFilter = 'exclude_failed' | '' | 'completed' | 'failed' | 'in_progress' | 'generating'
const logStatusFilter = ref<LogStatusFilter>('exclude_failed')
const logCategoryFilter = ref<'' | 'special' | 'favorite'>('')
const logActionLoading = ref<number | null>(null)
const logFavoriteLoading = ref<number | null>(null)
let logSearchTimer: ReturnType<typeof setTimeout> | null = null
let logListRequestSeq = 0
let browseListRequestSeq = 0
let browseListRefreshedAt = 0
let browseInvalidationVersion = 0
let browseListRequestsInFlight = 0
let browseSocketRefreshTimer: ReturnType<typeof setTimeout> | null = null
let browseSocketRefreshQueued = false
const chatInput = ref('')
const sending = ref(false)
const chatContainer = ref<HTMLElement | null>(null)

// ── 酒馆大堂座位 ──
const orderTargetSeat = ref<TavernSeat | null>(null)
const treatTargetSeat = ref<TavernSeat | null>(null)
const seatActionLoading = ref(false)
let tavernPollTimer: ReturnType<typeof setInterval> | null = null
let fallbackPollTimer: ReturnType<typeof setInterval> | null = null
let taskBadgeDebounceTimer: ReturnType<typeof setTimeout> | null = null
const TASK_BADGE_DEBOUNCE_MS = 2000
const BROWSE_SOCKET_REFRESH_MIN_INTERVAL_MS = 8000
const TAVERN_POLL_INTERVAL_MS = 60_000
const TAVERN_POLL_JITTER_MS = 5000
const FALLBACK_POLL_INTERVAL_MS = 120_000

const barSeats = computed(() => store.tavernSeats.filter(s => s.zone === 'bar'))

// 同 table_key 的座位归并成一桌，按返回顺序（sort_order）排列
const tavernTables = computed(() => {
  const map = new Map<string, { key: string; label: string; seats: TavernSeat[] }>()
  for (const s of store.tavernSeats) {
    if (s.zone !== 'table' || !s.table_key) continue
    let g = map.get(s.table_key)
    if (!g) {
      g = { key: s.table_key, label: s.table_label || '桌子', seats: [] }
      map.set(s.table_key, g)
    }
    g.seats.push(s)
  }
  return [...map.values()]
})

const orderAdvGold = computed<number | null>(() => {
  const advId = orderTargetSeat.value?.adventurer?.id
  if (advId == null) return null
  const adv = store.adventurers.find(a => a.id === advId)
  return adv ? adv.gold : null
})

const treatPayerGold = computed<number | null>(() => {
  const adv = myAdventurers.value.find(isPlayableAdventurer)
  return adv ? adv.gold : null
})

function seatClass(seat: TavernSeat) {
  return [
    'seat',
    {
      'seat-vacant': !seat.adventurer,
      'seat-mine': seat.adventurer?.is_mine,
      'seat-clickable': true,
    },
  ]
}

function foodRemainingLabel(seat: TavernSeat): string {
  if (!seat.food) return ''
  const remainingMs = Date.parse(seat.food.expires_at) - (liveNow.value + store.tavernServerOffsetMs)
  if (remainingMs <= 0) return '等待结算'
  const minutes = Math.max(1, Math.ceil(remainingMs / 60_000))
  return `剩余 ${minutes} 分钟`
}

function foodProgressText(seat: TavernSeat): string {
  const food = seat.food
  if (!food) return ''
  const texts = food.progress_texts?.length ? food.progress_texts : [food.text]
  const startMs = Date.parse(food.started_at)
  const endMs = Date.parse(food.expires_at)
  const nowMs = liveNow.value + store.tavernServerOffsetMs
  const durationMs = Math.max(1, endMs - startMs)
  const progress = Math.max(0, Math.min(1, (nowMs - startMs) / durationMs))
  if (progress < 1 / 3) return texts[0] || ''
  if (progress < 3 / 4) return texts[1] || texts[0] || ''
  return texts[2] || texts[texts.length - 1] || ''
}

async function onSeatClick(seat: TavernSeat) {
  if (seat.adventurer) {
    if (seat.adventurer.is_mine) {
      if (!myLoaded.value) loadMine()
      await store.fetchDrinkMenu()
      await store.fetchFoodMenu()
      orderTargetSeat.value = seat
    } else {
      if (!myLoaded.value) await loadMine()
      await store.fetchDrinkMenu()
      treatTargetSeat.value = seat
    }
    return
  }
  // 空位：玩家只绑定一个冒险者，直接用绑定的存活冒险者入座，不出选择列表
  if (seatActionLoading.value) return
  seatActionLoading.value = true
  try {
    if (!myLoaded.value) await loadMine()
    const adv = myAdventurers.value.find(isPlayableAdventurer)
    if (!adv) {
      message.error('你还没有存活的冒险者')
      return
    }
    const res = await store.sitSeat(seat.id, adv.id)
    if (res.code === 200) {
      message.success(`${adv.name} 已在「${seat.label}」坐下`)
      await store.fetchTavernSeats(true, true)
    } else {
      message.error(res.message || '入座失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '入座失败')
  } finally {
    seatActionLoading.value = false
  }
}

async function doTreat(drink: TavernDrinkMenuItem) {
  if (seatActionLoading.value || !treatTargetSeat.value) return
  seatActionLoading.value = true
  try {
    const res = await store.requestTreat(treatTargetSeat.value.id, drink.key)
    if (res.code === 200) {
      message.success(`已邀请 ${treatTargetSeat.value.adventurer?.name || '对方'} 喝 ${drink.name}`)
      treatTargetSeat.value = null
      await store.fetchTavernSeats(true, true)
    } else {
      message.error(res.message || '请客失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '请客失败')
  } finally {
    seatActionLoading.value = false
  }
}

async function doRespondTreat(requestId: number, accept: boolean) {
  if (seatActionLoading.value) return
  seatActionLoading.value = true
  try {
    const res = await store.respondTreat(requestId, accept)
    if (res.code === 200) {
      message.success(accept ? '已接受这杯饮品' : '已拒绝请客')
      await store.fetchTavernSeats(true, true)
      if (accept) await loadMine()
    } else {
      message.error(res.message || '处理失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '处理失败')
  } finally {
    seatActionLoading.value = false
  }
}

async function doOrder(drink: TavernDrinkMenuItem) {
  if (seatActionLoading.value || !orderTargetSeat.value) return
  seatActionLoading.value = true
  try {
    const res = await store.orderDrink(orderTargetSeat.value.id, drink.key)
    if (res.code === 200) {
      message.success(`${drink.icon} 一杯${drink.name}已上桌`)
      // 同步冒险者钱包余额，点酒弹窗里的金币即时刷新
      const advId = orderTargetSeat.value.adventurer?.id
      const mine = advId != null ? store.adventurers.find(a => a.id === advId) : undefined
      if (mine && typeof res.data?.gold_balance === 'number') mine.gold = res.data.gold_balance
      orderTargetSeat.value = null
      await store.fetchTavernSeats(true, true)
    } else {
      message.error(res.message || '点酒失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '点酒失败')
  } finally {
    seatActionLoading.value = false
  }
}

async function doOrderFood(food: TavernFoodMenuItem) {
  if (seatActionLoading.value || !orderTargetSeat.value) return
  seatActionLoading.value = true
  try {
    const res = await store.orderFood(orderTargetSeat.value.id, food.key)
    if (res.code === 200) {
      message.success(`${food.icon} ${food.name}已上桌`)
      const advId = orderTargetSeat.value.adventurer?.id
      const mine = advId != null ? store.adventurers.find(a => a.id === advId) : undefined
      if (mine && typeof res.data?.gold_balance === 'number') mine.gold = res.data.gold_balance
      orderTargetSeat.value = null
      await store.fetchTavernSeats(true, true)
    } else {
      message.error(res.message || '点餐失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '点餐失败')
  } finally {
    seatActionLoading.value = false
  }
}

async function doLeaveSeat() {
  if (seatActionLoading.value || !orderTargetSeat.value) return
  seatActionLoading.value = true
  try {
    const res = await store.leaveSeat(orderTargetSeat.value.id)
    if (res.code === 200) {
      message.success('已离座')
      orderTargetSeat.value = null
      await store.fetchTavernSeats(true, true)
    } else {
      message.error(res.message || '离座失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '离座失败')
  } finally {
    seatActionLoading.value = false
  }
}

const browseFilter = reactive({ status: '', search: '', sort_field: 'created_at', sort_order: 'desc' })
const appliedBrowseFilter = reactive({ ...browseFilter })
const partyStatus = ref('active')

// 手动加入/离队相关: joiningAdvId / leavingAdvId 锁定按钮避免重复点击。
const joiningAdvId = ref<number | null>(null)
const leavingAdvId = ref<number | null>(null)
const votingDepartureAdvId = ref<number | null>(null)
const matchDungeonMode = ref<'random' | 'custom'>('random')
const matchPreferredDungeons = ref<string[]>([])
const matchPartyPreferredDungeon = ref('')
const matchPartyDescription = ref('')
const matchAccessMode = ref<'public' | 'invite'>('public')
const inviteTicketId = ref('')
const inviteSearch = ref('')
const activeInviteTicketId = ref<number | null>(null)
const matchmakingLoading = ref(false)
const partyRefreshing = ref(false)
const adminPartyLoading = ref(false)
const adminRemovingAdvId = ref<number | null>(null)
const expeditionConfig = ref<Record<string, number> | null>(null)

// 门票制特殊机缘副本（仅展示名称/门票/不限等级文案，后端掷骰概率不下发）。
interface SpecialDungeonInfo {
  name: string
  ticket_item: string
  level_label: string
  flavor: string
  target_size?: number
  holds_ticket?: boolean
  entry_enabled?: boolean
}
const specialDungeons = ref<SpecialDungeonInfo[]>([])
const visibleSpecialDungeons = computed(() => specialDungeons.value.filter(isS1DungeonVisible))
const creatingSpecialDungeon = ref<string | null>(null)
/** 小队页子标签：默认普通自动/手动组队；特殊机缘为门票制副本 */
const partyBoardTab = ref<'normal' | 'special'>('normal')

const specialDungeonNameSet = computed(
  () => new Set(specialDungeons.value.map(d => d.name)),
)

function isSpecialParty(p: PartyData): boolean {
  const dungeon = (p.target_dungeon || '').trim()
  return dungeon !== '' && specialDungeonNameSet.value.has(dungeon)
}

function matchTicketStatusLabel(status: string): string {
  if (status === 'forming') return '等人中'
  if (status === 'queued') return '匹配中'
  if (status === 'matched') return '已成团'
  if (status === 'cancelled') return '已取消'
  return status
}

function formatMatchTicketMembers(ticket: MatchTicketDisplayData): string {
  const count = ticket.member_count ?? 0
  if (ticket.queue_type === 'group' && count >= 4) {
    return ticket.allow_solo_fill
      ? `${count} 人（可进本，也可补路人至最多 6 人）`
      : `${count} 人（可直接排队进本）`
  }
  return `${count} 人（4 人起成团，最多 6 人）`
}

function memberTitleFrameClass(member: { active_title?: AdventurerTitle | null }): string {
  const frame = member.active_title?.frame_key
  return frame === 'fate' || frame === 'fate_companion' || frame === 'hidden_relic'
    ? `title-mini--${frame}`
    : ''
}

function matchTicketDungeonName(ticket?: MatchTicketDisplayData | null): string {
  if (ticket?.matched_party_dungeon) return ticket.matched_party_dungeon
  if (ticket?.preferred_dungeons?.length) return ticket.preferred_dungeons.join(' / ')
  return ticket?.preferred_dungeon || '随机地图'
}

function matchTicketDungeonLevelDesc(ticket?: MatchTicketDisplayData | null): string | null {
  return ticket?.matched_party_dungeon_level_desc || ticket?.preferred_dungeon_level_desc || null
}

function matchDungeonQueuedCount(dungeonName: string): number {
  return store.matchDungeonQueuedMemberCounts[dungeonName] ?? 0
}

function matchTicketDisplayTime(ticket: MatchTicketData): string | null {
  return ticket.status === 'queued' ? (ticket.queued_at || ticket.created_at) : ticket.created_at
}

function formatQueueAhead(ticket: MatchTicketData): string {
  const ahead = Math.max(0, ticket.queue_parties_ahead ?? 0)
  const prefix = ahead === 0 ? '前方暂无匹配报名' : `前方还有 ${ahead} 个匹配报名`
  if (ticket.queue_type === 'group' && ticket.member_count >= 4) {
    return ticket.allow_solo_fill
      ? `${prefix}；本队已达进本门槛，若有同地图/兼容单排会补位至 5-6 人。`
      : `${prefix}；本队不匹配路人，将按当前人数进本。`
  }
  return `${prefix}；正在等待人数/地图/档位匹配，排队中。`
}

function formatMatchedTicketHint(ticket: MatchTicketData): string {
  if (ticket.matched_party_departure_time) {
    return `已成团，正在等待发车调度（${formatDateTime(ticket.matched_party_departure_time)}）。`
  }
  return '已成团，正在排队等待发车调度。'
}

const displayedMyParties = computed(() => {
  const list = store.myParties
  if (partyBoardTab.value === 'special') {
    return list.filter(isSpecialParty)
  }
  // forming 普通队也必须兜底可见：匹配票据缓存/关联异常时，不能让玩家只看到
  // 冒险者“组队中”，却看不到实际占用他的队伍。
  return list.filter(p => !isSpecialParty(p) && (p.status === 'forming' || p.status === 'exploring'))
})

const displayedBrowseParties = computed(() => {
  if (partyBoardTab.value !== 'special') return []
  return store.parties.filter(isSpecialParty)
})

watch(partyBoardTab, tab => {
  if (tab === 'special') loadSpecialDungeons()
})

const showTavernUpgradeUi = computed(() => store.tavernUpgrade?.upgrade_ui_enabled === true)

const { now: liveNow } = useLiveTick()
const campaignTaskCoolingDown = computed(() => !!store.campaignTaskCooldownUntil
  && Date.parse(store.campaignTaskCooldownUntil) > liveNow.value + store.tavernServerOffsetMs)
const { titleEffectsEnabled, setTitleEffectsEnabled } = useS1TitleEffects()

type ZoneId = BuildingCategory

const BUILDING_ZONES: { category: ZoneId; label: string; icon: string }[] = [
  { category: 'dormitory', label: '宿舍区', icon: '🏠' },
  { category: 'market', label: '个人交易市场', icon: '🏪' },
  { category: 'auction', label: '隐秘拍卖行', icon: '🎭' },
  { category: 'medical', label: '治疗区', icon: '🏥' },
  { category: 'equipment', label: '装备区', icon: '⚒️' },
  { category: 'gathering', label: '采集区', icon: '🪓' },
  { category: 'training', label: '修炼区', icon: '🧘' },
  { category: 'memorial', label: '纪念区', icon: '🪦' },
  { category: 'leisure', label: '休闲区', icon: '🎲' },
  { category: 'work', label: '打工区', icon: '📋' },
  { category: 'task', label: '任务与议事厅', icon: '📜' },
]

const assessmentPreviewFallback: BuildingInstance = {
  id: -1,
  client_preview_only: true,
  template_id: -1,
  custom_name: null,
  status: 'built',
  upgrade_level: 0,
  build_started_at: null,
  build_complete_at: null,
  progress_percent: 100,
  remaining_seconds: 0,
  total_build_seconds: 0,
  helpers: [],
  template: {
    id: -1,
    code: TRANSCENDENT_ASSESSMENT_PREVIEW_CODE,
    name: TRANSCENDENT_ASSESSMENT_PREVIEW_NAME,
    icon: '✦',
    description: TRANSCENDENT_ASSESSMENT_PREVIEW_DESCRIPTION,
    building_type: 'unique',
    category: 'memorial',
    build_hours: 0,
    helper_slots: 0,
    stamina_cost_per_help: 0,
    speedup_minutes: 0,
    is_active: true,
    sort_order: -1,
    handler_key: TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY,
    settlement_config: {
      reward_type: 'none',
      reward_amount: 0,
      required_level: TRANSCENDENT_ASSESSMENT_PREVIEW_REQUIRED_LEVEL,
    },
  },
}

const groupedBuildings = computed<Record<BuildingCategory, BuildingInstance[]>>(() => {
  const groups: Record<BuildingCategory, BuildingInstance[]> = {
    general: [], dormitory: [], medical: [], equipment: [], leisure: [], gathering: [], training: [], market: [], auction: [], work: [], task: [], memorial: [],
  }
  // Lv2 解锁建筑的显隐统一由后端 lv2_locked_preview 预览占位控制：
  // 酒馆未到 Lv2 时后端返回只读预览（显示「🔒 酒馆 Lv.2 解锁」），升级后返回真实实例。
  const assessmentBuilding = store.buildings.find(
    b => b.template?.handler_key === TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY,
  ) ?? assessmentPreviewFallback
  const directoryBuildings = [
    assessmentBuilding,
    ...store.buildings.filter(
      b => b.template?.handler_key !== TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY,
    ),
  ]
  for (const b of directoryBuildings) {
    if (!isS1BuildingVisible(b)) continue
    const tpl = b.template as (BuildingInstance['template'] & { code?: string }) | undefined
    const code = tpl?.code ?? tpl?.handler_key
    if (code && BUILDING_CODES_HIDDEN_FROM_ZONE_UI.has(code)) continue
    // 兼容 seed 尚未重新运行的旧 general/assessment 分类；处理器才是该建筑的稳定身份。
    const cat = tpl?.handler_key === TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY
      ? 'memorial'
      : tpl?.category
    if (cat && groups[cat]) groups[cat].push(b)
  }
  return groups
})

const currentZone = ref<ZoneId | null>(null)
const currentZoneInfo = computed(() => BUILDING_ZONES.find(z => z.category === currentZone.value) || null)
// 任务区子 tab：任务栏（现有）/ 悬赏布告栏（委托）
const taskZoneTab = ref<'task' | 'commission' | 'campaign'>('task')

async function onCampaignAccepted() {
  partyBoardTab.value = 'normal'
  activeTab.value = 'party'
  await refreshMatchmakingState()
}

watch(currentZone, (zone) => {
  if (zone === 'task') {
    // TaskBoardPanel v-if mount 会自行 loadData()，无需重复 refreshTaskAcceptableCount
    acknowledgeTaskBoardHint()
  }
})

function onOpenAdminAsset(kind: AdminAssetPanel) {
  if (!isAdmin.value) return
  activeTab.value = 'building'
  currentZone.value = null
  showAdmin.value = kind === 'reservation'
  showBuildingAdmin.value = kind === 'building'
  showLuckyWheelAdmin.value = kind === 'wheel'
  showItemCatalogAdmin.value = kind === 'catalog'
}

// 任务栏：未建成前普通玩家不可进入分区
const ZONES_ENTRY_LOCKED_UNTIL_BUILT: ZoneId[] = ['task']

/** 个人交易市场唯一建筑（父级菜单点击用，等同治疗区里点「医院」） */
function getMarketBuilding(): BuildingInstance | null {
  const list = groupedBuildings.value['market']
  if (list.length > 0) return list[0]
  return store.buildings.find(b => {
    const tpl = b.template
    if (!tpl) return false
    return tpl.category === 'market'
      || tpl.code === 'personal_market'
      || tpl.name === '个人交易市场'
  }) ?? null
}

/** 隐秘拍卖行唯一建筑 */
function getAuctionBuilding(): BuildingInstance | null {
  const list = groupedBuildings.value['auction']
  if (list.length > 0) return list[0]
  return store.buildings.find(b => {
    const tpl = b.template
    if (!tpl) return false
    return tpl.category === 'auction'
      || tpl.code === 'secret_auction'
      || tpl.handler_key === 'secret_auction'
      || tpl.name === '隐秘拍卖行'
  }) ?? null
}

function openZone(id: ZoneId) {
  if (id === 'dormitory') {
    openDormitory()
    return
  }
  // 个人交易市场：等同治疗区里的「医院」——未建成直接弹捐献/建造详情，已建成才进交易页
  if (id === 'market') {
    void openMarketEntry()
    return
  }
  if (id === 'auction') {
    void openAuctionEntry()
    return
  }
  if (id === 'task') {
    void openTaskEntry()
    return
  }
  if (ZONES_ENTRY_LOCKED_UNTIL_BUILT.includes(id) && isZoneEntryLocked(id)) {
    if (isAdmin.value) {
      const building = getZoneBuilding(id)
      if (building) {
        detailBuilding.value = building
      }
    }
    return
  }
  currentZone.value = id
}

function isZoneEntryLocked(id: ZoneId): boolean {
  if (!ZONES_ENTRY_LOCKED_UNTIL_BUILT.includes(id)) return false
  const list = groupedBuildings.value[id] || []
  if (list.length === 0) return true
  // 任务栏零材料直连建造：building 阶段普通玩家也可进入帮忙
  if (id === 'task') {
    return list.every(b => b.status !== 'built' && b.status !== 'building')
  }
  return list.every(b => b.status !== 'built')
}

function getZoneBuilding(id: ZoneId) {
  const list = groupedBuildings.value[id] || []
  return list[0] || null
}

function getTaskBoardBuilding(): BuildingInstance | null {
  return store.buildings.find(b => b.template?.code === 'task_board' || b.template?.handler_key === 'task_board') ?? null
}

async function openTaskEntry() {
  if (store.buildings.some(b => b.template?.code === 'campaign_hall' && b.status === 'built')) {
    currentZone.value = 'task'
    if (getTaskBoardBuilding()?.status !== 'built') taskZoneTab.value = 'campaign'
    return
  }
  let building = getTaskBoardBuilding()
  if (!building) {
    await store.fetchBuildings()
    building = getTaskBoardBuilding()
  }
  if (!building) {
    message.warning('任务栏数据未加载，请刷新页面后重试' + (isAdmin.value ? '；若仍失败请在建筑管理中检查模板与实例' : ''))
    return
  }
  if (building.status === 'built') {
    currentZone.value = 'task'
  } else {
    // 与个人交易市场一致：未建成时打开建造详情，普通玩家也可派遣帮工
    openBuildingDetail(building)
  }
}

async function openMarketEntry() {
  let building = getMarketBuilding()
  if (!building) {
    await store.fetchBuildings()
    building = getMarketBuilding()
  }
  if (!building) {
    message.warning('个人交易市场数据未加载，请刷新页面后重试' + (isAdmin.value ? '；若仍失败请在建筑管理中检查模板与实例' : ''))
    return
  }
  if (building.status === 'built') {
    currentZone.value = 'market'
  } else {
    openBuildingDetail(building)
  }
}

async function openAuctionEntry() {
  let building = getAuctionBuilding()
  if (!building) {
    await store.fetchBuildings()
    building = getAuctionBuilding()
  }
  if (!building) {
    message.warning('隐秘拍卖行数据未加载，请刷新页面后重试' + (isAdmin.value ? '；若仍失败请在建筑管理中检查模板与实例' : ''))
    return
  }
  if (building.status === 'built') {
    currentZone.value = 'auction'
  } else {
    openBuildingDetail(building)
  }
}

function zoneSummary(id: ZoneId): string {
  if (id === 'dormitory') return '冒险者休息的住所'
  if (id === 'market') {
    const b = getMarketBuilding()
    if (!b) return '未建造'
    if (b.status === 'built') return '已开放'
    if (b.status === 'building') return '建造中'
    return '募集中'
  }
  if (id === 'auction') {
    const b = getAuctionBuilding()
    if (!b) return '未建造'
    if (b.status === 'building') return '建造中'
    if (b.status !== 'built') return '募集中'
    if (store.secretAuctionOpenCountLoading) return '已开放 · 加载中…'
    const n = store.secretAuctionOpenCount
    if (n == null) return '已开放'
    if (n === 0) return '已开放 · 暂无拍品'
    return `已开放 · ${n} 件竞拍中`
  }
  if (id === 'task') {
    const b = getTaskBoardBuilding()
    if (store.buildings.some(item => item.template?.code === 'campaign_hall' && item.status === 'built')) return '任务 · 委托 · 世界进展'
    if (!b) return '未建造'
    if (b.status === 'building') return '建造中'
    if (b.status !== 'built') return '需要建造'
    if (store.taskAcceptableCountLoading) return '已开放 · 加载中…'
    const n = store.taskAcceptableCount
    if (n == null) return '已开放'
    if (n === 0) return '已开放 · 暂无可接'
    return `已开放 · ${n} 个可接`
  }
  if (ZONES_ENTRY_LOCKED_UNTIL_BUILT.includes(id)) {
    const list = groupedBuildings.value[id] || []
    if (list.length === 0) return '未建造'
    const built = list.filter(b => b.status === 'built').length
    if (built === 0) return '需要建造'
    return '已开放'
  }
  const list = groupedBuildings.value[id]
  if (list.length === 0) return '暂无建筑'
  const built = list.filter(b => b.status === 'built').length
  return `${built}/${list.length} 已建成`
}

const swapMode = ref(false)
const swapFirst = ref<{ id: number; name: string } | null>(null)
const applyingDormitory = ref(false)
const cancellingDormitoryId = ref<number | null>(null)
const gmCompletingId = ref<number | null>(null)
const showDormSettings = ref(false)
const dormMaxConcurrentDraft = ref<number | null>(null)
const dormMaxTotalDraft = ref<number | null>(null)
const savingDormSettings = ref(false)
// 捐献表单状态：key = `${instanceId}:${material_name}`，仅记数量（冒险者后端自动取）
const donateAmountBy = reactive<Record<string, number | null>>({})
const donatingKey = ref<string | null>(null)

function myMaterialBalance(mat: string): number {
  return Number(store.materialBalances?.[mat] ?? store.dormitoryMyStatus?.my_materials?.[mat] ?? 0)
}

function roomOccupied(room: DormRoom): number {
  return room.beds.filter(b => b !== null).length
}

function roomResidents(room: DormRoom): string {
  const names = room.beds.filter((b): b is DormBed => b !== null).map(b => b.adventurer_name)
  return names.length > 0 ? names.join('、') : '空房间'
}

function toggleSwapMode() {
  if (swapMode.value) {
    // 取消互换模式
    swapMode.value = false
    swapFirst.value = null
    return
  }
  // 进入互换模式：自动选中自己的存活冒险者
  if (!myLoaded.value) loadMine()
  const myAdv = myAdventurers.value.find(isPlayableAdventurer)
  if (!myAdv) {
    message.error('你还没有存活的冒险者')
    return
  }
  swapFirst.value = { id: myAdv.id, name: myAdv.name }
  swapMode.value = true
}

function openDormitory() {
  showDormitory.value = true
  swapMode.value = false
  swapFirst.value = null
  store.fetchDormitory(true, 1)
  store.fetchDormitoryMyStatus(true)
  store.fetchMyMaterials()
  store.fetchSwapRequests()
}

async function applyForDormitory() {
  if (applyingDormitory.value) return
  applyingDormitory.value = true
  try {
    const res = await store.requestNewDormitory()
    if (res.code === 200 && res.data) {
      const tpl = res.data.template
      const beds = tpl?.produced_beds ?? '?'
      message.success(`抽到 ${beds} 床房型 · 共需 ${formatMaterials(tpl?.required_materials)}`)
      store.fetchDormitoryMyStatus(true)
    } else {
      message.error(res.message || '申请失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '申请失败')
  } finally {
    applyingDormitory.value = false
  }
}

async function submitDonate(instanceId: number, materialName: string) {
  const key = `${instanceId}:${materialName}`
  if (donatingKey.value === key) return
  const amount = donateAmountBy[key]
  if (!amount || amount <= 0) {
    message.error('请输入有效数量')
    return
  }
  donatingKey.value = key
  try {
    // 不传 adventurer_id，后端自动取 owner 名下首个非死冒险者
    const res = await store.donateToDormitory(instanceId, null, materialName, amount)
    if (res.code === 200) {
      const donated = res.data?.donated_amount ?? amount
      const triggered = res.data?.triggered_build_start
      message.success(
        triggered
          ? `捐献 ${donated} ${matLabel(materialName)},材料已凑齐自动开工!`
          : `捐献 ${donated} ${matLabel(materialName)}`
      )
      donateAmountBy[key] = null
      store.fetchDormitoryMyStatus(true)
      store.fetchMyMaterials()
      if (triggered) store.fetchBuildings(true)
    } else {
      message.error(res.message || '捐献失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '捐献失败')
  } finally {
    donatingKey.value = null
  }
}

async function confirmCancelDormitory(instanceId: number) {
  if (!window.confirm('确认停止该宿舍楼建造?已捐材料将按记录原路退还。')) return
  cancellingDormitoryId.value = instanceId
  try {
    const res = await store.cancelDormitory(instanceId)
    if (res.code === 200) {
      message.success(`已停止建造,退还 ${res.data?.refund_count ?? 0} 笔捐献`)
      store.fetchDormitoryMyStatus(true)
    } else {
      message.error(res.message || '停止失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '停止失败')
  } finally {
    cancellingDormitoryId.value = null
  }
}

async function gmCompleteDormitory(inst: any) {
  if (!window.confirm('确定要 GM 一键建成吗？将跳过材料和建造阶段直接完工。')) return
  gmCompletingId.value = inst.id
  try {
    const res = await store.completeBuilding(inst.id)
    if (res.code === 200) {
      message.success('已建成')
      store.fetchDormitoryMyStatus(true)
    } else {
      message.error(res.message || '建成失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '建成失败')
  } finally {
    gmCompletingId.value = null
  }
}

async function toggleDormSettings() {
  if (!showDormSettings.value) {
    const res = await store.fetchDormitorySettings('selected')
    if (res.code === 200) {
      const items = res.data?.settings ?? []
      const concurrent = items.find((i: any) => i.key === 'dormitory_max_concurrent')
      if (concurrent) dormMaxConcurrentDraft.value = Number(concurrent.value)
      const total = items.find((i: any) => i.key === 'dormitory_max_total')
      if (total) dormMaxTotalDraft.value = Number(total.value)
    }
  }
  showDormSettings.value = !showDormSettings.value
}

async function saveDormMaxConcurrent() {
  if (savingDormSettings.value) return
  const v = dormMaxConcurrentDraft.value
  if (!v || v < 1 || v > 100) {
    message.error('并发上限必须在 1 到 100 之间')
    return
  }
  savingDormSettings.value = true
  try {
    const res = await store.updateDormitorySetting(
      'dormitory_max_concurrent',
      v,
      'selected',
    )
    if (res.code === 200) {
      message.success('并发上限已更新')
      store.fetchDormitoryMyStatus(true)
    } else {
      message.error(res.message || '更新失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '更新失败')
  } finally {
    savingDormSettings.value = false
  }
}

async function saveDormMaxTotal() {
  if (savingDormSettings.value) return
  const v = dormMaxTotalDraft.value
  if (!v || v < 1 || v > 200) {
    message.error('总数上限必须在 1 到 200 之间')
    return
  }
  savingDormSettings.value = true
  try {
    const res = await store.updateDormitorySetting(
      'dormitory_max_total',
      v,
      'selected',
    )
    if (res.code === 200) {
      message.success('总数上限已更新')
      store.fetchDormitoryMyStatus(true)
    } else {
      message.error(res.message || '更新失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '更新失败')
  } finally {
    savingDormSettings.value = false
  }
}

const MAT_META: Record<string, { name: string; icon: string }> = {
  wood: { name: '木材', icon: '🪵' },
  stone: { name: '石料', icon: '🪨' },
}

function matIcon(mat: string): string {
  return MAT_META[mat]?.icon ?? '📦'
}

function matLabel(mat: string): string {
  return MAT_META[mat]?.name ?? mat
}

function matProgressPercent(inst: any, mat: string | number): number {
  const need = Number(inst.template?.required_materials?.[mat] ?? 0)
  if (need <= 0) return 0
  const got = Number(inst.donations_summary?.[mat] ?? 0)
  return Math.min(100, Math.round(got / need * 100))
}

function formatMaterials(mats?: Record<string, number> | null): string {
  if (!mats) return ''
  const labels: Record<string, string> = { wood: '🪵 木材', stone: '🪨 石料' }
  return Object.entries(mats)
    .map(([code, amt]) => `${labels[code] ?? code} ${amt}`)
    .join(' · ')
}

function onBedClick(bed: DormBed | null, roomId?: number, bedIndex?: number) {
  if (!swapMode.value) return
  if (!swapFirst.value) return // 自动选中模式下 swapFirst 始终有值
  const room = store.dormitory.find(item => item.id === roomId)


  // 空床位 → 直接搬迁
  if (!bed) {
    if (roomId === undefined || bedIndex === undefined) return
    doMoveToEmptyBed(swapFirst.value.id, roomId, bedIndex)
    return
  }

  // 点自己的床位 → 取消选中（退出互换模式）
  if (bed.adventurer_id === swapFirst.value.id) {
    swapMode.value = false
    swapFirst.value = null
    return
  }

  // 点别人的床位 → 发送互换请求
  doSendSwapRequest(swapFirst.value.id, bed.adventurer_id)
}

async function doMoveToEmptyBed(advId: number, roomId: number, bedIndex: number) {
  try {
    const res = await store.moveToEmptyBed(advId, roomId, bedIndex)
    if (res.code === 200) {
      message.success(res.data?.message || '搬迁成功')
      swapMode.value = false
      swapFirst.value = null
      store.fetchDormitory(true)
    } else {
      message.error(res.message || '搬迁失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '搬迁失败')
  }
}

async function doSendSwapRequest(advId: number, targetAdvId: number) {
  try {
    const res = await store.sendSwapRequest(advId, targetAdvId)
    if (res.code === 200) {
      message.success(res.data?.message || '已发送互换请求')
      swapMode.value = false
      swapFirst.value = null
      store.fetchSwapRequests()
    } else {
      message.error(res.message || '发送请求失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '发送请求失败')
  }
}

async function doAcceptSwapRequest(requestId: number) {
  try {
    const res = await store.acceptSwapRequest(requestId)
    if (res.code === 200) {
      message.success(res.data?.message || '互换成功')
      store.fetchDormitory(true)
      store.fetchSwapRequests()
    } else {
      message.error(res.message || '接受失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '接受失败')
  }
}

async function doRejectSwapRequest(requestId: number) {
  try {
    const res = await store.rejectSwapRequest(requestId)
    if (res.code === 200) {
      message.success('已拒绝互换请求')
      store.fetchSwapRequests()
    } else {
      message.error(res.message || '拒绝失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '拒绝失败')
  }
}

async function doCancelSwapRequest(requestId: number) {
  try {
    const res = await store.cancelSwapRequest(requestId)
    if (res.code === 200) {
      message.success('已取消互换请求')
      store.fetchSwapRequests()
    } else {
      message.error(res.message || '取消失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消失败')
  }
}

async function openBuildingDetail(b: BuildingInstance) {
  if (
    b.client_preview_only
    && b.template?.handler_key === TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY
  ) {
    detailBuilding.value = b
    return
  }
  if (b.lv2_locked_preview) {
    if (store.tavernUpgrade?.building_upgrade_unlocked) {
      await store.fetchBuildings(true)
      const tplId = b.template_id ?? b.template?.id
      const code = b.template?.code
      const refreshed = store.buildings.find((row) => {
        if (row.id && tplId != null && row.template_id === tplId) return true
        if (row.id && code && row.template?.code === code) return true
        return false
      })
      if (refreshed?.id) {
        detailBuilding.value = refreshed
        return
      }
    }
    message.info('该建筑需将酒馆升级至 Lv.2 后才能解锁建造')
    return
  }
  if (!b.id) {
    message.warning('建筑数据未就绪，请刷新页面后重试')
    return
  }
  detailBuilding.value = b
}

function openDormRoom(roomId: number) {
  dormRoomId.value = roomId
}

function isViewingTaskBoard(): boolean {
  return activeTab.value === 'building' && currentZone.value === 'task'
}

async function notifyTaskBoardIfNeeded(
  payload?: { reason?: string; task_name?: string },
) {
  if (!payload || !hasUnseenTaskBoardHint() || isViewingTaskBoard()) return
  const unseenCount = taskBoardUnseenIds().length
  const isNewEdition = payload?.reason === 'new_edition'
  const taskName = payload?.task_name?.trim()
  if (isNewEdition && taskName) {
    message.info(`📜 任务栏上新：${taskName}`)
  } else if (unseenCount > 0) {
    message.info(`📜 任务栏有 ${unseenCount} 个委托可接`)
  }
}

async function handleTaskBoardChanged(payload?: { reason?: string; task_name?: string }) {
  const b = getTaskBoardBuilding()
  if (b?.status !== 'built') {
    store.taskAcceptableCount = null
    store.taskAcceptableIds = []
    return
  }
  // 只在建筑页可见时处理；其余页面先标脏，进入建筑页再按需刷新。
  scheduleTaskBadgeRefresh(true)
  if (isViewingTaskBoard()) {
    acknowledgeTaskBoardHint()
  }
  await notifyTaskBoardIfNeeded(payload)
}

function scheduleTaskBadgeRefresh(force = false) {
  if (taskBadgeDebounceTimer) clearTimeout(taskBadgeDebounceTimer)
  taskBadgeDebounceTimer = setTimeout(() => {
    taskBadgeDebounceTimer = null
    void store.refreshTaskBadgesOnly(force)
  }, TASK_BADGE_DEBOUNCE_MS)
}

function refreshBuildingZoneSummaries(force = false) {
  const bTask = getTaskBoardBuilding()
  const bAuction = getAuctionBuilding()
  if (bTask?.status === 'built') {
    scheduleTaskBadgeRefresh(force)
  } else {
    store.taskAcceptableCount = null
    store.taskAcceptableIds = []
  }
  if (bAuction?.status === 'built') {
    void store.refreshSecretAuctionOpenCount(force)
  } else {
    store.secretAuctionOpenCount = null
  }
}

function clearUnavailableBuildingZoneSummaries() {
  if (getTaskBoardBuilding()?.status !== 'built') {
    store.taskAcceptableCount = null
    store.taskAcceptableIds = []
  }
  if (getAuctionBuilding()?.status !== 'built') {
    store.secretAuctionOpenCount = null
  }
}

function onTaskBoardRefreshed() {
  refreshBuildings(true)
}

function refreshBuildings(force = false) {
  store.fetchBuildings(force).then(() => {
    if (detailBuilding.value) {
      const updated = store.buildings.find(b => b.id === detailBuilding.value!.id)
      if (updated) detailBuilding.value = updated
    }
  })
}

const currentUserId = computed(() => userStore.user?.id ?? null)
let initialSnapshotReady = false
const homeLoadPending = ref(false)
// Defer the admin panel's mount reads until the initial request settles.
// Keep it accessible after a failed init, and mounted during manual retries.
const homeInitialReadFinished = ref(false)
const homeLoadError = ref('')
const homeRetryNotice = ref('')
let homeLoadController: AbortController | null = null
let homeDisposed = false
const squadSessionChangedBeforeMount = store.prepareSquadSession(currentUserId.value)

watch(currentUserId, (userId) => {
  const squadSessionChanged = store.prepareSquadSession(userId)
  if (squadSessionChanged) {
    homeLoadController?.abort()
    homeLoadController = null
    homeLoadPending.value = false
    homeInitialReadFinished.value = false
    initialSnapshotReady = false
    detailAdvId.value = null
    detailStackedOnLog.value = false
    showTitlePreview.value = false
    textCorrectionAdvId.value = null
  }
  taskHintAck.value = loadTaskHintAck()
  if (squadSessionChanged && userId != null) {
    void initializeHome(true)
  } else if (!squadSessionChanged) {
    refreshBuildingZoneSummaries(false)
  }
})

const myAdventurers = computed<AdventurerData[]>(() => store.adventurers)
const viewedRealmCode = computed<S1PlayerRealmView>(() => store.realmViewCode ?? 'region_1')
const isRealmVisitor = computed(() => store.realmVisitorMode)
const playableMyAdventurers = computed(() => myAdventurers.value.filter(isPlayableAdventurer))
const hasOwnedSquadBadge = computed(() => myAdventurers.value.some(adv => adv.squad_badge != null))
const canOpenSquadModal = computed(
  () => playableMyAdventurers.value.length > 0 || hasOwnedSquadBadge.value,
)
const matchDungeonOptionNames = computed(
  () => new Set(store.matchDungeonOptions.map(d => d.name)),
)

watch(matchDungeonMode, (mode) => {
  if (mode === 'random') matchPreferredDungeons.value = []
})

watch(
  () => [store.matchDungeonOptions, playableMyAdventurers.value[0]?.level] as const,
  () => {
    matchPreferredDungeons.value = matchPreferredDungeons.value.filter(name => matchDungeonOptionNames.value.has(name))
    const selected = matchPartyPreferredDungeon.value.trim()
    if (selected && !matchDungeonOptionNames.value.has(selected)) matchPartyPreferredDungeon.value = ''
  },
)
const archivedMyAdventurers = computed(() => myAdventurers.value.filter(a => a.status === 'archived'))
const canShowCreateEntry = computed(() =>
  !homeLoadPending.value && !homeLoadError.value && !isRealmVisitor.value
  && playableMyAdventurers.value.length === 0
  && archivedMyAdventurers.value.length === 0,
)
const supportVoteButtonText = computed(() => {
  if (store.supportVoteLoading) return '🗳️ 投票'
  const optionCount = store.supportVoteStatus?.options?.length ?? 0
  return optionCount > 0 ? `🗳️ 投票 (${optionCount})` : '🗳️ 投票'
})
const supportVoteOptions = computed(() => store.supportVoteStatus?.options ?? [])
const chatDisplay = computed(() => [...store.chatMessages].reverse())
const dormitoryConcurrentFull = computed(() => {
  const c = store.dormitoryMyStatus?.concurrent
  if (!c) return false
  return c.current_building >= c.max_concurrent
})
const dormitoryTotalFull = computed(() => {
  const t = store.dormitoryMyStatus?.total
  if (!t) return false
  return t.current >= t.max
})

function switchTab(tab: TabId) {
  activeTab.value = tab
  try {
    localStorage.setItem(STORAGE_KEY, tab)
  } catch {
    // 存储不可用不应阻断栏目切换及其数据加载。
  }
  if (tab !== 'building') {
    showDormitory.value = false
    showAdmin.value = false
    showBuildingAdmin.value = false
    showLuckyWheelAdmin.value = false
    showItemCatalogAdmin.value = false
    currentZone.value = null
    swapMode.value = false
    swapFirst.value = null
    dormRoomId.value = null
  }
  ensureFreshTabData(tab)
  if (tab === 'my-adventurers') void loadWaitingNpcOrigin(true)
  if (window.matchMedia('(max-width: 640px)').matches) {
    void nextTick(() => {
      // 导航本身会吸顶，用前置锚点定位其自然位置，避免切页后仍停在长列表底部。
      tabAnchor.value?.scrollIntoView({ block: 'start', behavior: 'instant' })
    })
  }
}

function focusActiveTab() {
  void nextTick(() => tabNavigation.value?.querySelector<HTMLButtonElement>('[aria-current="page"]')?.focus({ preventScroll: true }))
}

// 已加载过的 tab 集合：首次访问才请求，后续切换靠 WebSocket + 兜底轮询保持数据新鲜
const TAB_REFRESH_STALE_MS = 120_000
const _loadedTabs = new Set<TabId>()
const _dirtyTabs = new Set<TabId>()
const _tabRefreshedAt: Partial<Record<TabId, number>> = {}

function markTabFresh(tab: TabId) {
  _dirtyTabs.delete(tab)
  _tabRefreshedAt[tab] = Date.now()
}

function markTabsDirty(tabs: TabId[]) {
  for (const tab of tabs) {
    if (tab !== activeTab.value) _dirtyTabs.add(tab)
  }
}

function markTabDirty(tab: TabId) {
  _dirtyTabs.add(tab)
}

function isTabStale(tab: TabId): boolean {
  const at = _tabRefreshedAt[tab]
  return !at || Date.now() - at > TAB_REFRESH_STALE_MS
}

async function refreshTabData(
  tab: TabId,
  opts: { firstLoad: boolean; includeServerRefresh: boolean },
) {
  let refreshed = true
  if (tab === 'tavern') {
    const tasks: Promise<any>[] = []
    if (opts.includeServerRefresh) tasks.push(store.fetchTavernSeats(true, _dirtyTabs.has('tavern')))
    if (opts.firstLoad || opts.includeServerRefresh) {
      tasks.push(store.fetchChat(1).then((result) => {
        if (result?.code === 200) scrollChatToBottom()
        return result
      }))
    }
    const results = await Promise.allSettled(tasks)
    refreshed = results.every(result => result.status === 'fulfilled' && result.value?.code === 200)
  } else if (tab === 'logs') {
    if (opts.firstLoad) {
      const result = await fetchLogList({
        page: 1,
        search: appliedLogSearch.value || undefined,
        ...logStatusRequestParams(),
        category: logCategoryFilter.value || undefined,
        force: opts.includeServerRefresh,
      })
      refreshed = result.isCurrent && result.data?.code === 200
    } else {
      refreshed = await refreshCurrentLogs(opts.includeServerRefresh)
    }
    if (isAdmin.value) await fetchExpeditionConfig()
  } else if (tab === 'my-adventurers') {
    if (opts.includeServerRefresh) await refreshMyAdventurerData({ includeActivity: false, force: false })
  } else if (tab === 'adventurers') {
    if (opts.firstLoad) {
      const result = await searchBrowse()
      refreshed = result.isCurrent && result.data?.code === 200
    } else {
      refreshed = await refreshCurrentBrowse()
    }
  } else if (tab === 'party') {
    await refreshPartyTab(opts.includeServerRefresh)
  } else if (tab === 'building') {
    if (opts.includeServerRefresh) {
      await store.fetchBuildings(false)
    }
    refreshBuildingZoneSummaries(opts.includeServerRefresh)
  }
  return refreshed
}

function ensureTabData(tab: TabId) {
  // fetchInit 已加载: notice / adventurers / buildings / tavern_seats / drink_menu /
  // tavern_upgrade / activity / materials / task_counts / auction_counts
  // 只在该 tab 首次访问时补充 init 未覆盖的数据；后续切换不再主动请求
  if (_loadedTabs.has(tab)) return
  _loadedTabs.add(tab)

  if (tab === 'tavern') {
    store.fetchChat(1).then(() => scrollChatToBottom())
  } else if (tab === 'logs') {
    fetchLogList({
      page: 1,
      search: appliedLogSearch.value || undefined,
      ...logStatusRequestParams(),
      category: logCategoryFilter.value || undefined,
    })
      .then(() => { if (isAdmin.value) fetchExpeditionConfig() })
  } else if (tab === 'my-adventurers') {
    // 冒险者列表 / 动态 / 座位 均已在 init 中加载
  } else if (tab === 'adventurers') {
    searchBrowse()
  } else if (tab === 'party') {
    searchParties()
  } else if (tab === 'building') {
    // 建筑和升级进度已在 init 中加载；角标（task_count / auction_count）也已在 init 中返回
    // 子面板（market/auction/task）已改为 v-if，进入对应 zone 才 mount 拉数据
  }
}

void ensureTabData

function ensureFreshTabData(tab: TabId) {
  const firstLoad = !_loadedTabs.has(tab)
  const dirty = _dirtyTabs.has(tab)
  const stale = isTabStale(tab)
  if (!firstLoad && !dirty && !stale) return

  _loadedTabs.add(tab)
  void refreshTabData(tab, {
    firstLoad,
    includeServerRefresh: dirty || stale,
  }).then((refreshed) => {
    if (refreshed) markTabFresh(tab)
  })
}

async function confirmNotice() {
  const res = await store.ackNotice()
  if (res?.code === 200) {
    message.success('已确认通知')
  } else {
    message.error(res?.message || '确认失败，请刷新后重试')
    await store.fetchNotice()
  }
}

async function loadMine() {
  await store.fetchAdventurers()
  void store.fetchSupportVoteStatus()
  await loadWaitingNpcOrigin()
  myLoaded.value = true
}

async function loadWaitingNpcOrigin(force = false) {
  const adventurer = playableMyAdventurers.value[0]
  if (!adventurer) {
    waitingNpcOrigin.value = null
    waitingNpcOriginAdvId = null
    return
  }
  if (!force && waitingNpcOriginAdvId === adventurer.id && waitingNpcOrigin.value) return
  waitingNpcOriginLoading.value = true
  try {
    const result = await store.fetchWaitingNpcOrigin(adventurer.id)
    if (result.code === 200) {
      waitingNpcOrigin.value = result.data as WaitingNpcOriginState
      waitingNpcOriginAdvId = adventurer.id
    }
  } catch {
    // 个人页辅助入口读取失败不阻塞冒险者主列表。
  } finally {
    waitingNpcOriginLoading.value = false
  }
}

async function doResetWaitingNpcOrigin() {
  const adventurer = playableMyAdventurers.value[0]
  if (!adventurer || !waitingNpcOrigin.value?.can_reset || resettingWaitingNpcOrigin.value) return
  const confirmed = window.confirm(
    '确定让这个角色“另启新生”吗？\n\n这会一次性清除抽取前作为路人 NPC 继承的职业、物品、技能、特质与经历；正常初始项和之后其他来源的数据会保留。此操作不能重复。',
  )
  if (!confirmed) return
  resettingWaitingNpcOrigin.value = true
  try {
    const result = await store.resetWaitingNpcOrigin(adventurer.id)
    if (result.code !== 200) {
      message.error(result.message || '另启新生失败')
      return
    }
    message.success('已另启新生，抽取前路人经历与继承项已清除')
    await Promise.allSettled([
      store.fetchAdventurers(true),
      store.currentDetail?.id === adventurer.id
        ? store.fetchDetail(adventurer.id, { preserveDetail: true, force: true })
        : Promise.resolve(),
    ])
    await loadWaitingNpcOrigin(true)
  } catch (error: any) {
    message.error(error?.response?.data?.message || error?.message || '另启新生失败')
  } finally {
    resettingWaitingNpcOrigin.value = false
  }
}

async function openSupportVoteModal() {
  showSupportVoteModal.value = true
  try {
    await store.fetchSupportVoteStatus()
    selectedSupportVoteOption.value = store.supportVoteStatus?.my_option_key || supportVoteOptions.value[0]?.key || ''
  } catch (e: any) {
    selectedSupportVoteOption.value = ''
    message.error(e?.response?.data?.message || '投票信息加载失败')
  }
}

async function doSupportVote() {
  if (store.supportVoteLoading) return
  if (!selectedSupportVoteOption.value) {
    message.warning('请选择一个投票选项')
    return
  }
  try {
    const res = await store.castSupportVote(selectedSupportVoteOption.value)
    if (res?.code === 200) {
      message.success('投票已保存')
      showSupportVoteModal.value = false
      selectedSupportVoteOption.value = ''
    } else {
      message.error(res?.message || '投票失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '投票失败')
    await store.fetchSupportVoteStatus()
  }
}

function searchBrowse() {
  Object.assign(appliedBrowseFilter, browseFilter, { search: browseFilter.search.trim() })
  return fetchBrowseList({ page: 1, ...appliedBrowseFilter })
}
async function fetchBrowseList(params: Parameters<typeof store.fetchBrowseAdventurers>[0]) {
  const seq = ++browseListRequestSeq
  const coveredInvalidationVersion = browseInvalidationVersion
  browseListRequestsInFlight += 1
  try {
    const data = await store.fetchBrowseAdventurers(params)
    const isCurrent = seq === browseListRequestSeq
    if (isCurrent && data?.code === 200) {
      browseListRefreshedAt = Date.now()
      if (coveredInvalidationVersion === browseInvalidationVersion) {
        clearBrowseSocketRefreshTimer()
        browseSocketRefreshQueued = false
        markTabFresh('adventurers')
      }
    }
    return { data, isCurrent }
  } finally {
    browseListRequestsInFlight = Math.max(0, browseListRequestsInFlight - 1)
    if (
      browseListRequestsInFlight === 0
      && browseSocketRefreshQueued
      && initialSnapshotReady
    ) {
      browseSocketRefreshQueued = false
      armBrowseSocketRefresh()
    }
  }
}
async function refreshCurrentBrowse() {
  const page = store.browsePagination?.current_page ?? 1
  const result = await fetchBrowseList({ page, ...appliedBrowseFilter })
  if (!result.isCurrent) return false
  const lastPage = store.browsePagination?.pages ?? 1
  if (page > lastPage) {
    const clamped = await fetchBrowseList({ page: lastPage, ...appliedBrowseFilter })
    return clamped.isCurrent && clamped.data?.code === 200
  }
  return result.data?.code === 200
}

function scheduleBrowseSocketRefresh() {
  if (!initialSnapshotReady) return
  browseInvalidationVersion += 1
  markTabDirty('adventurers')
  armBrowseSocketRefresh()
}

function armBrowseSocketRefresh() {
  if (!initialSnapshotReady) return
  if (activeTab.value !== 'adventurers') {
    return
  }
  if (browseListRequestsInFlight > 0) {
    browseSocketRefreshQueued = true
    return
  }
  if (browseSocketRefreshTimer) return

  const elapsed = Date.now() - browseListRefreshedAt
  const delay = Math.max(0, BROWSE_SOCKET_REFRESH_MIN_INTERVAL_MS - elapsed)
  browseSocketRefreshTimer = setTimeout(runBrowseSocketRefresh, delay)
}

function clearBrowseSocketRefreshTimer() {
  if (!browseSocketRefreshTimer) return
  clearTimeout(browseSocketRefreshTimer)
  browseSocketRefreshTimer = null
}

function runBrowseSocketRefresh() {
  browseSocketRefreshTimer = null
  if (!initialSnapshotReady) return
  if (activeTab.value !== 'adventurers') {
    markTabDirty('adventurers')
    return
  }
  if (browseListRequestsInFlight > 0) {
    browseSocketRefreshQueued = true
    return
  }

  const remaining = BROWSE_SOCKET_REFRESH_MIN_INTERVAL_MS - (Date.now() - browseListRefreshedAt)
  if (remaining > 0) {
    browseSocketRefreshTimer = setTimeout(runBrowseSocketRefresh, remaining)
    return
  }

  void refreshCurrentBrowse().then((refreshed) => {
    if (!refreshed) markTabDirty('adventurers')
  }).catch(() => {
    markTabDirty('adventurers')
  })
}

function goBrowsePage(page: number) {
  void fetchBrowseList({ page, ...appliedBrowseFilter })
}

function searchParties() {
  return store.fetchPartiesBoard(partyStatus.value, true)
}
function goPartyPage(page: number) {
  store.fetchParties({ page, status: partyStatus.value, exclude_mine: true })
}

async function refreshPartyTab(force = false) {
  // 特殊机缘副本列表随小队页一起刷新（含当前冒险者持票状态）
  const sharedRefreshes = [
    loadSpecialDungeons(force),
    store.fetchMatchmakingBoard(force),
  ]
  // 首次加载或切 tab 用合并接口；翻页后刷新仍用独立接口保留页码
  if (store.partiesPagination?.current_page == null || store.partiesPagination.current_page <= 1) {
    sharedRefreshes.push(store.fetchPartiesBoard(partyStatus.value, force))
  } else {
    sharedRefreshes.push(
      store.fetchMyParties(partyStatus.value),
      store.fetchParties({
        page: store.partiesPagination.current_page,
        status: partyStatus.value,
        exclude_mine: true,
      }),
    )
  }
  return Promise.allSettled(sharedRefreshes)
}

async function doRefreshPartyBoard() {
  if (partyRefreshing.value) return
  partyRefreshing.value = true
  try {
    await Promise.allSettled([
      refreshPartyTab(true),
      store.fetchAdventurers(),
    ])
    message.success('小队情况已刷新')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '刷新小队情况失败')
  } finally {
    partyRefreshing.value = false
  }
}

// 我自己冒险者的 id 集合（含组队中/探险中），用于识别我的小队成员。
const myAdventurerIds = computed(() => new Set(store.adventurers.map(a => a.id)))

function ownMatchTicketMember(ticket: MatchTicketData) {
  const member = ticket.members.find(item => myAdventurerIds.value.has(item.adventurer_id))
  if (!member) return null
  return {
    adventurer_id: member.adventurer_id,
    name: member.name || '你的冒险者',
  }
}

// 普通匹配和特殊副本成团后都落在 forming Party；尚未出发时均允许自己的冒险者离队。
function canLeave(p: PartyData, m: { id: number }): boolean {
  return p.status === 'forming' && myAdventurerIds.value.has(m.id)
}

// 展示加入入口:小队还在集结、未满员、且我至少有一个冒险者。
function canShowJoinSection(p: PartyData): boolean {
  if (!isSpecialParty(p)) return false
  if (p.status !== 'forming') return false
  if (p.member_count >= p.target_size) return false
  return playableMyAdventurers.value.length > 0
}

function hasMemberVotedDeparture(p: PartyData, advId: number): boolean {
  return (p.departure_voted_adventurer_ids ?? []).includes(advId)
}

function myAdventurerInParty(p: PartyData): { id: number; name: string } | null {
  const mine = p.members.find(m => myAdventurerIds.value.has(m.id))
  return mine ? { id: mine.id, name: mine.name } : null
}

function canVoteDeparture(p: PartyData): boolean {
  if (!isSpecialParty(p)) return false
  if (p.status !== 'forming') return false
  if (p.member_count < p.target_size) return false
  return myAdventurerInParty(p) !== null
}

function hasMyDepartureVote(p: PartyData): boolean {
  const mine = myAdventurerInParty(p)
  if (!mine) return false
  return hasMemberVotedDeparture(p, mine.id)
}

async function doVoteDeparture(p: PartyData) {
  const mine = myAdventurerInParty(p)
  if (!mine) {
    message.error('你的冒险者不在该小队中')
    return
  }
  if (votingDepartureAdvId.value) return
  votingDepartureAdvId.value = mine.id
  try {
    const res = await store.votePartyDeparture(mine.id, p.id)
    if (res.code === 200) {
      const data = res.data || {}
      if (data.all_voted) {
        message.success('全体成员已投票，小队将在下一调度周期立即出发')
      } else {
        message.success(`${mine.name} 已投票（${data.vote_count}/${data.member_count}）`)
      }
      refreshPartyTab(true)
      store.fetchAdventurers()
    } else {
      message.error(res.message || '投票失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '投票失败')
  } finally {
    votingDepartureAdvId.value = null
  }
}

async function loadSpecialDungeons(force = false) {
  const adv = playableMyAdventurers.value[0]
  try {
    const res = await store.listSpecialDungeons(adv?.id, force)
    if (res.code === 200) {
      specialDungeons.value = Array.isArray(res.data) ? res.data : []
    }
  } catch {
    // 静默：特殊副本列表加载失败不阻塞小队页
  }
}

async function doCreateSpecialParty(d: SpecialDungeonInfo) {
  const adv = playableMyAdventurers.value[0]
  if (!adv) {
    message.error('你还没有冒险者')
    return
  }
  if (creatingSpecialDungeon.value) return
  creatingSpecialDungeon.value = d.name
  try {
    const res = await store.createSpecialParty(adv.id, d.name)
    if (res.code === 200) {
      message.success(`${adv.name} 已组建前往「${d.name}」的小队`)
      partyBoardTab.value = 'special'
      refreshPartyTab(true)
      loadSpecialDungeons()
      store.fetchAdventurers()
    } else {
      message.error(res.message || '组建失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '组建失败')
  } finally {
    creatingSpecialDungeon.value = null
  }
}

async function refreshMatchmakingState() {
  await Promise.allSettled([
    store.fetchMatchmakingBoard(true),
    store.fetchAdventurers(),
    store.fetchPartiesBoard(partyStatus.value, true),
  ])
}

async function doLeaveMatchTicket(ticket: MatchTicketData) {
  if (matchmakingLoading.value) return
  if (ticket.status === 'queued') {
    const rejoinHint = ticket.access_mode === 'public'
      ? '退出后，你将暂时无法重新加入该公开小队。'
      : ''
    const confirmed = window.confirm(
      `当前小队正在匹配中。退出会取消整队本次匹配，并让小队退回等人状态。${rejoinHint}\n\n确定退出小队吗？`,
    )
    if (!confirmed) return
  }
  matchmakingLoading.value = true
  try {
    const res = await store.leaveMatchTicket(ticket.id)
    if (res.code === 200) {
      message.success('已退出小队')
    } else {
      message.error(res.message || '退出小队失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '退出小队失败')
  } finally {
    await refreshMatchmakingState()
    matchmakingLoading.value = false
  }
}

async function doLeaveMatchedTicket(ticket: MatchTicketData) {
  if (matchmakingLoading.value) return
  const member = ownMatchTicketMember(ticket)
  if (!member) {
    message.error('未找到你在该小队中的冒险者，请刷新后重试')
    await refreshMatchmakingState()
    return
  }
  if (!window.confirm(`当前小队已成团但尚未出发。确定让「${member.name}」退出吗？`)) return

  matchmakingLoading.value = true
  try {
    const res = await store.leaveParty(member.adventurer_id)
    if (res.code === 200) {
      message.success(`${member.name} 已退出成团小队`)
    } else {
      message.error(res.message || '退出成团小队失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '退出成团小队失败')
  } finally {
    await refreshMatchmakingState()
    matchmakingLoading.value = false
  }
}

async function doEnqueueSoloMatchmaking() {
  if (matchmakingLoading.value) return
  if (playableMyAdventurers.value.length === 0) {
    message.error('你当前没有可匹配的冒险者')
    return
  }
  if (matchDungeonMode.value === 'custom' && matchPreferredDungeons.value.length === 0) {
    message.error('请至少选择一张匹配地图')
    return
  }
  matchmakingLoading.value = true
  try {
    const preferredDungeons = matchDungeonMode.value === 'custom' ? matchPreferredDungeons.value : []
    const res = await store.enqueueMatchmaking(preferredDungeons)
    if (res.code === 200) {
      message.success('已加入单人匹配队列')
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '报名匹配失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '报名匹配失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doCreateMatchmakingParty() {
  if (matchmakingLoading.value) return
  if (playableMyAdventurers.value.length === 0) {
    message.error('你当前没有可创建小队的冒险者')
    return
  }
  matchmakingLoading.value = true
  try {
    const res = await store.createMatchmakingParty({
      preferredDungeon: matchPartyPreferredDungeon.value.trim() || null,
      description: matchPartyDescription.value,
      accessMode: matchAccessMode.value,
      allowSoloFill: false,
    })
    if (res.code === 200) {
      message.success('已创建匹配小队')
      matchPartyDescription.value = ''
      matchAccessMode.value = 'public'
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '创建小队失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '创建小队失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doSetMatchTicketSoloFill(ticket: MatchTicketData, event: Event) {
  if (matchmakingLoading.value) return
  const input = event.target as HTMLInputElement | null
  const allowSoloFill = !!input?.checked
  matchmakingLoading.value = true
  try {
    const res = await store.setMatchTicketSoloFill(ticket.id, allowSoloFill)
    if (res.code === 200) {
      message.success(allowSoloFill ? '已允许匹配路人补位' : '已关闭匹配路人补位')
      await refreshMatchmakingState()
    } else {
      if (input) input.checked = !!ticket.allow_solo_fill
      message.error(res.message || '设置路人补位失败')
    }
  } catch (e: any) {
    if (input) input.checked = !!ticket.allow_solo_fill
    message.error(e?.response?.data?.message || '设置路人补位失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doJoinPublicMatchTicket(ticket: MatchTicketData) {
  if (matchmakingLoading.value) return
  let confirmedFateRisk = false
  if (ticket.has_fate_dungeon_risk) {
    const memberNames = (ticket.fate_risk_member_names || []).join('、') || '队内成员'
    const dungeonName = matchTicketDungeonName(ticket)
    const confirmed = window.confirm(
      `风险提示：${memberNames}正在进行与【${dungeonName}】关联的命途。`
      + '本次探险可能触发高于该地牢常规难度的命途事件；触发后整场普通奖励与经验按15级标准结算。是否仍要加入小队？',
    )
    if (!confirmed) return
    confirmedFateRisk = true
  }
  matchmakingLoading.value = true
  try {
    const res = await store.joinPublicMatchTicket(ticket.id, confirmedFateRisk)
    if (res.code === 200) {
      message.success('已加入公开小队')
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '加入小队失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加入小队失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doJoinInviteTicket() {
  const inviteCode = inviteTicketId.value.trim()
  if (!/^\d{6}$/.test(inviteCode)) {
    message.error('请输入 6 位数字小队邀请码')
    return
  }
  if (matchmakingLoading.value) return
  matchmakingLoading.value = true
  try {
    const res = await store.joinInviteMatchTicket(inviteCode)
    if (res.code === 200) {
      message.success('已加入邀请小队')
      inviteTicketId.value = ''
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '加入邀请小队失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加入邀请小队失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doStartMatchmaking(ticketId: number) {
  if (matchmakingLoading.value) return
  matchmakingLoading.value = true
  try {
    const res = await store.startMatchmakingTicket(ticketId)
    if (res.code === 200) {
      message.success('小队已开始匹配')
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '开始匹配失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '开始匹配失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doEditMatchDescription(ticket: MatchTicketData) {
  if (matchmakingLoading.value || !ticket.can_edit_description) return
  const description = window.prompt('修改小队描述（最多 500 字）', ticket.description || '')
  if (description === null) return
  if (description.trim().length > 500) {
    message.error('小队描述不能超过 500 字')
    return
  }
  matchmakingLoading.value = true
  try {
    const res = await store.updateMatchTicketDescription(ticket.id, description)
    if (res.code !== 200) {
      message.error(res.message || '修改描述失败')
      return
    }
    message.success('小队描述已更新')
    await refreshMatchmakingState()
  } catch (e: any) {
    message.error(e?.response?.data?.message || '处理失败，请刷新小队确认描述')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doStopMatchmaking(ticketId: number) {
  if (matchmakingLoading.value) return
  matchmakingLoading.value = true
  try {
    const res = await store.stopMatchmakingTicket(ticketId)
    if (res.code === 200) {
      message.success('已取消匹配，小队保留')
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '取消匹配失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消匹配失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doMakeMatchTicketPublic(ticketId: number) {
  if (matchmakingLoading.value) return
  matchmakingLoading.value = true
  try {
    const res = await store.makeMatchTicketPublic(ticketId)
    if (res.code === 200) {
      message.success('小队已设为公开，其他人可在招募板加入')
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '设为公开失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '设为公开失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function loadInviteCandidates(ticketId: number) {
  activeInviteTicketId.value = ticketId
  try {
    const res = await store.fetchMatchInviteCandidates(ticketId, inviteSearch.value)
    if (res.code !== 200) message.error(res.message || '加载邀请候选失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载邀请候选失败')
  }
}

async function doInviteCandidate(ticketId: number, adventurerId: number) {
  if (matchmakingLoading.value) return
  matchmakingLoading.value = true
  try {
    const res = await store.inviteMatchAdventurer(ticketId, adventurerId)
    if (res.code === 200) {
      message.success('已发送邀请')
      await loadInviteCandidates(ticketId)
    } else {
      message.error(res.message || '邀请失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '邀请失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doAcceptMatchInvite(inviteId: number) {
  if (matchmakingLoading.value) return
  matchmakingLoading.value = true
  try {
    const res = await store.acceptMatchInvite(inviteId)
    if (res.code === 200) {
      message.success('已加入小队')
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '接受邀请失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '接受邀请失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doDeclineMatchInvite(inviteId: number) {
  if (matchmakingLoading.value) return
  matchmakingLoading.value = true
  try {
    const res = await store.declineMatchInvite(inviteId)
    if (res.code === 200) {
      message.success('已拒绝邀请')
      await store.fetchMatchmakingBoard(true)
    } else {
      message.error(res.message || '拒绝邀请失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '拒绝邀请失败')
  } finally {
    matchmakingLoading.value = false
  }
}

async function doCancelMatchmaking(ticketId: number) {
  if (matchmakingLoading.value) return
  matchmakingLoading.value = true
  try {
    const res = await store.cancelMatchmaking(ticketId)
    if (res.code === 200) {
      message.success('已取消匹配')
    } else {
      message.error(res.message || '取消匹配失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消匹配失败')
  } finally {
    await refreshMatchmakingState()
    matchmakingLoading.value = false
  }
}

async function doJoinMyAdventurer(p: PartyData) {
  const adv = playableMyAdventurers.value[0]
  if (!adv) {
    message.error('你还没有冒险者')
    return
  }
  if (joiningAdvId.value) return
  joiningAdvId.value = adv.id
  try {
    const res = await store.joinParty(adv.id, p.id)
    if (res.code === 200) {
      message.success(`${adv.name} 已加入「${p.name}」`)
      // 刷新小队列表 + 我的冒险者列表,让按钮和状态立刻同步
      refreshPartyTab(true)
      store.fetchAdventurers()
    } else {
      message.error(res.message || '加入失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加入失败')
  } finally {
    joiningAdvId.value = null
  }
}

async function doLeave(m: { id: number; name: string }, p: PartyData) {
  if (leavingAdvId.value) return
  leavingAdvId.value = m.id
  try {
    const res = await store.leaveParty(m.id)
    if (res.code === 200) {
      message.success(`${m.name} 已离开「${p.name}」`)
      refreshPartyTab(true)
      store.fetchAdventurers()
    } else {
      message.error(res.message || '离队失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '离队失败')
  } finally {
    leavingAdvId.value = null
  }
}

async function doForceDispatch(p: PartyData) {
  if (adminPartyLoading.value) return
  if (!confirm(`确认强制出发「${p.name}」？将跳过等待时间和满员要求。`)) return
  adminPartyLoading.value = true
  try {
    const res = await store.forceDispatchParty(p.id, 'membership')
    if (res.code === 200) {
      message.success(`「${p.name}」已强制出发`)
      refreshPartyTab(true)
    } else {
      message.error(res.message || '强制出发失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '强制出发失败')
  } finally {
    adminPartyLoading.value = false
  }
}

async function doForceDisband(p: PartyData) {
  if (adminPartyLoading.value) return
  if (!confirm(`确认强制解散「${p.name}」？${p.status === 'exploring' ? '探险中的小队将被中断，日志标为失败。' : ''}`)) return
  adminPartyLoading.value = true
  try {
    const res = await store.forceDisbandParty(p.id, 'membership')
    if (res.code === 200) {
      message.success(`「${p.name}」已强制解散`)
      refreshPartyTab(true)
    } else {
      message.error(res.message || '强制解散失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '强制解散失败')
  } finally {
    adminPartyLoading.value = false
  }
}

async function doAdminRemoveMember(p: PartyData, m: { id: number; name: string }) {
  if (adminRemovingAdvId.value) return
  if (!confirm(`确认将「${m.name}」从「${p.name}」中移除？`)) return
  adminRemovingAdvId.value = m.id
  try {
    const res = await store.adminRemoveMember(p.id, m.id, 'membership')
    if (res.code === 200) {
      message.success(`已将「${m.name}」从「${p.name}」中移除`)
      refreshPartyTab(true)
      store.fetchAdventurers()
    } else {
      message.error(res.message || '移除失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '移除失败')
  } finally {
    adminRemovingAdvId.value = null
  }
}

// ── 管理面板：探险管理 ──────────────────────────────────────────────────
async function fetchExpeditionConfig(force = false) {
  // 优先复用 /logs 响应附带的配置（避免额外请求）
  if (!force && store._expeditionConfig) {
    expeditionConfig.value = store._expeditionConfig
    return
  }
  try {
    const res = await store.adminGetExpeditionConfig('membership')
    if (res.code === 200) {
      expeditionConfig.value = res.data
      store._expeditionConfig = res.data
    }
  } catch { /* ignore */ }
}

function logStatusRequestParams(): { status?: string; exclude_failed?: boolean } {
  if (logStatusFilter.value === 'exclude_failed') return { exclude_failed: true }
  return { status: logStatusFilter.value || undefined }
}

function goLogPage(page: number) {
  void fetchLogList({
    page,
    search: appliedLogSearch.value || undefined,
    ...logStatusRequestParams(),
    category: logCategoryFilter.value || undefined,
  })
}

function onLogSearchInput() {
  if (logSearchTimer) clearTimeout(logSearchTimer)
  logSearchTimer = setTimeout(() => {
    logSearchTimer = null
    const search = logSearch.value.trim()
    if (search === appliedLogSearch.value) return
    appliedLogSearch.value = search
    void fetchLogList({
      page: 1,
      search: appliedLogSearch.value || undefined,
      ...logStatusRequestParams(),
      category: logCategoryFilter.value || undefined,
    })
  }, 300)
}

function onLogFilterChange() {
  if (logSearchTimer) {
    clearTimeout(logSearchTimer)
    logSearchTimer = null
  }
  appliedLogSearch.value = logSearch.value.trim()
  void fetchLogList({
    page: 1,
    search: appliedLogSearch.value || undefined,
    ...logStatusRequestParams(),
    category: logCategoryFilter.value || undefined,
  })
}

async function fetchLogList(params: Parameters<typeof store.fetchLogs>[0]) {
  const seq = ++logListRequestSeq
  const data = await store.fetchLogs(params)
  return { data, isCurrent: seq === logListRequestSeq }
}

async function refreshCurrentLogs(force = true) {
  const page = store.logsPagination?.current_page ?? 1
  const result = await fetchLogList({
    page,
    search: appliedLogSearch.value || undefined,
    ...logStatusRequestParams(),
    category: logCategoryFilter.value || undefined,
    force,
  })
  if (!result.isCurrent) return false
  const lastPage = store.logsPagination?.pages ?? 1
  if (page > lastPage) {
    const clamped = await fetchLogList({
      page: lastPage,
      search: appliedLogSearch.value || undefined,
      ...logStatusRequestParams(),
      category: logCategoryFilter.value || undefined,
      force,
    })
    return clamped.isCurrent && clamped.data?.code === 200
  }
  return result.data?.code === 200
}

async function toggleLogFavorite(log: ExpeditionLogSummary) {
  if (logFavoriteLoading.value !== null) return
  logFavoriteLoading.value = log.id
  const nextValue = !log.is_favorited
  try {
    const res = await store.setExpeditionLogFavorite(log.id, nextValue)
    if (res.code !== 200) {
      message.error(res.message || '收藏操作失败')
      return
    }
    message.success(nextValue ? `已收藏日志 #${log.id}` : `已取消收藏日志 #${log.id}`)
    if (logCategoryFilter.value === 'favorite' && !nextValue) {
      await refreshCurrentLogs()
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '收藏操作失败')
  } finally {
    logFavoriteLoading.value = null
  }
}

async function doStepExpeditionLog(logId: number) {
  if (logActionLoading.value !== null) return
  logActionLoading.value = logId
  try {
    const res = await store.adminStepExpeditionLog(logId)
    if (res.code === 200) {
      message.success(res.data?.status === 'completed' ? '已推进并完成结算' : '已推进一批日志')
      await refreshCurrentLogs()
      if (logModalId.value === logId) await store.fetchLogDetail(logId)
    } else {
      message.error(res.message || '推进失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '推进失败')
  } finally {
    logActionLoading.value = null
  }
}

async function doForceCompleteExpeditionLog(logId: number) {
  if (logActionLoading.value !== null) return
  if (!window.confirm('确定直接释放剩余日志并立即结算吗？')) return
  logActionLoading.value = logId
  try {
    const res = await store.adminForceCompleteExpeditionLog(logId)
    if (res.code === 200) {
      message.success('已直接结算')
      await Promise.all([refreshCurrentLogs(), store.fetchAdventurers()])
      if (logModalId.value === logId) await store.fetchLogDetail(logId)
    } else {
      message.error(res.message || '结算失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '结算失败')
  } finally {
    logActionLoading.value = null
  }
}

function canRetryGeneratingLog(log: { status: string; created_at?: string | null }) {
  if (log.status !== 'generating') return false
  const minutes = expeditionConfig.value?.generating_gm_retry_minutes ?? 15
  return isGeneratingStuckForGmRetry(log.created_at ?? undefined, minutes)
}

async function doRegenerateExpeditionLog(logId: number) {
  if (logActionLoading.value !== null) return
  if (!window.confirm('确定重新生成这条失败的探险日志吗？')) return
  logActionLoading.value = logId
  try {
    const res = await store.adminRegenerateExpeditionLog(logId)
    if (res.code === 200) {
      message.success('已重新生成探险日志')
      await refreshCurrentLogs()
      const newLogId = res.data?.log_id
      if (typeof newLogId === 'number') logModalId.value = newLogId
    } else {
      message.error(res.message || '重新生成失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '重新生成失败')
  } finally {
    logActionLoading.value = null
  }
}

async function doRetryGeneratingExpeditionLog(logId: number) {
  if (logActionLoading.value !== null) return
  if (!window.confirm('该日志生成超时，确定由 GM 重试吗？（沿用原日志，不重复扣精力）')) return
  logActionLoading.value = logId
  try {
    const res = await store.adminRetryGeneratingExpeditionLog(logId)
    if (res.code === 200) {
      message.success('已重试生成探险日志')
      await refreshCurrentLogs()
    } else {
      message.error(res.message || '重试生成失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '重试生成失败')
  } finally {
    logActionLoading.value = null
  }
}

async function onLogActionDone(newLogId?: number) {
  await refreshCurrentLogs()
  store.fetchAdventurers()
  if (typeof newLogId === 'number') logModalId.value = newLogId
}

function viewAdvLogs(adv: AdventurerData | { id: number; name: string }) {
  logsAdv.value = { id: adv.id, name: adv.name }
}

function openDetail(adv: AdventurerDetailTarget) {
  const id = adv.id ?? adv.adventurer_id
  if (id != null) {
    detailStackedOnLog.value = false
    detailAdvId.value = id
  }
}

function openDetailFromLog(adv: AdventurerDetailTarget) {
  const id = adv.id ?? adv.adventurer_id
  if (id != null) {
    detailStackedOnLog.value = true
    detailAdvId.value = id
  }
}

function closeDetail() {
  detailAdvId.value = null
  detailStackedOnLog.value = false
  if (activeTab.value === 'my-adventurers' && _dirtyTabs.has('my-adventurers')) {
    ensureFreshTabData('my-adventurers')
  }
}

function openLog(id: number) {
  logModalId.value = id
}

function goDormitoryPage(page: number) {
  dormRoomId.value = null
  void store.fetchDormitory(true, page)
}

async function doConfirmMatchTicketFateRisk(ticket: MatchTicketData) {
  if (matchmakingLoading.value || !ticket.fate_risk_confirmation_required) return
  const dungeonName = matchTicketDungeonName(ticket)
  const riskNames = (ticket.fate_risk_member_names || []).join('、') || '队内成员'
  if (!window.confirm(
    `${riskNames}正在进行与【${dungeonName}】关联的命途，本次可能触发高于常规难度的事件；触发后整场普通奖励与经验按15级标准结算。确认承担风险吗？`,
  )) return
  matchmakingLoading.value = true
  try {
    const res = await store.confirmMatchTicketFateRisk(ticket.id)
    if (res.code === 200) {
      message.success('已确认命途风险')
      await refreshMatchmakingState()
    } else {
      message.error(res.message || '确认风险失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '确认风险失败')
  } finally {
    matchmakingLoading.value = false
  }
}

function openLogFromAdventurerLogs(id: number) {
  if (detailStackedOnLog.value) {
    logsAdv.value = null
    detailAdvId.value = null
    detailStackedOnLog.value = false
  }
  openLog(id)
}

function closeLog() {
  logModalId.value = null
  detailStackedOnLog.value = false
}

async function toggleFollow(adv: AdventurerData) {
  try {
    const res = adv.is_followed ? await store.unfollow(adv.id) : await store.follow(adv.id)
    if (res.code !== 200) message.error(res.message || '操作失败')
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  }
}

function onReserved() {
  showCreatePanel.value = false
}

function onDataAppealSubmitted() {
  showDataAppeal.value = false
  dataAppealInitialTab.value = 'submit'
  loadPendingRevertOffers()
}

function onDataAppealClose() {
  showDataAppeal.value = false
  dataAppealInitialTab.value = 'submit'
  loadPendingRevertOffers()
}

function openTavernReliefFromDataAppeal() {
  showDataAppeal.value = false
  dataAppealInitialTab.value = 'submit'
  showTavernRelief.value = true
}

function openAppealProjectsFromDataAppeal() {
  showDataAppeal.value = false
  dataAppealInitialTab.value = 'submit'
  showAppealProjects.value = true
}

function onTavernReliefClaimed() {
  void refreshMyAdventurerData({ includeActivity: false, force: true })
}

function onAppealProjectsChanged() {
  void refreshMyAdventurerData({ includeActivity: true, force: true })
}

function openDataAppealHistory() {
  dataAppealInitialTab.value = 'history'
  showDataAppeal.value = true
}

async function loadPendingRevertOffers() {
  try {
    const res = await store.fetchPendingDataAppealRevertOffers()
    if (res?.code === 200 && Array.isArray(res.data)) {
      pendingRevertOffers.value = res.data
    } else {
      pendingRevertOffers.value = []
    }
  } catch {
    pendingRevertOffers.value = []
  }
}

async function loadPendingPlaceholderRecallOffers() {
  try {
    const res = await store.fetchPendingPlaceholderSkillRecallOffers()
    if (res?.code === 200 && Array.isArray(res.data)) {
      pendingPlaceholderRecallOffers.value = res.data
    } else {
      pendingPlaceholderRecallOffers.value = []
    }
  } catch {
    pendingPlaceholderRecallOffers.value = []
  }
}

async function acceptPlaceholderRecallOffer(offerId: number) {
  if (placeholderRecallLoadingId.value != null) return
  if (!confirm('同意后将移除所列占位入门技能，并按数量补发「职业通用技能卷轴」到仓库。确定吗？')) return
  placeholderRecallLoadingId.value = offerId
  try {
    const res = await store.acceptPlaceholderSkillRecallOffer(offerId)
    if (res?.code === 200) {
      message.success(res.message || '已回收占位技能并补发卷轴')
      await Promise.all([
        loadPendingPlaceholderRecallOffers(),
        store.fetchAdventurers(true),
      ])
    } else {
      message.error(res?.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    placeholderRecallLoadingId.value = null
  }
}

async function declinePlaceholderRecallOffer(offerId: number) {
  if (placeholderRecallLoadingId.value != null) return
  placeholderRecallLoadingId.value = offerId
  try {
    const res = await store.declinePlaceholderSkillRecallOffer(offerId)
    if (res?.code === 200) {
      message.success('已拒绝本次回收邀请')
      await loadPendingPlaceholderRecallOffers()
    } else {
      message.error(res?.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    placeholderRecallLoadingId.value = null
  }
}

function openTextCorrection() {
  const adv = playableMyAdventurers.value[0]
  if (!adv) return
  textCorrectionAdvId.value = adv.id
}

async function openTitlePreview() {
  const adv = playableMyAdventurers.value[0]
  if (!adv || titlePreviewLoading.value) return
  titlePreviewLoading.value = true
  try {
    const result = await store.fetchDetail(adv.id, { preserveDetail: true, force: true })
    if (result?.stale_session) return
    if (result?.code !== 200) {
      message.error(result?.message || '称号预览读取失败')
      return
    }
    if (!store.currentDetail?.title_previews?.length) {
      message.error('当前没有可预览的称号')
      return
    }
    showTitlePreview.value = true
  } finally {
    titlePreviewLoading.value = false
  }
}

function toggleTitleEffects() {
  const enabled = !titleEffectsEnabled.value
  setTitleEffectsEnabled(enabled)
  message.success(enabled ? '本机称号动画已开启' : '本机称号动画已关闭')
}

function onTextCorrectionApplied() {
  textCorrectionAdvId.value = null
  store.fetchAdventurers()
}

function onDetailChanged() {
  // 关注/装备变更后，刷新当前 tab 以同步卡片状态
  if (activeTab.value === 'adventurers') refreshCurrentBrowse()
  else if (activeTab.value === 'my-adventurers') store.fetchAdventurers()
}

async function sendChat() {
  const content = chatInput.value.trim()
  if (!content || sending.value) return
  sending.value = true
  try {
    const res = await store.postChat(content)
    if (res.code === 200 || res.code === 201) {
      chatInput.value = ''
      await store.fetchChat(1)
      scrollChatToBottom()
    } else {
      message.error(res.message || '发送失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '发送失败')
  } finally {
    sending.value = false
  }
}

async function loadMoreChat() {
  await store.fetchChat(store.chatPage + 1)
}

function scrollChatToBottom() {
  nextTick(() => {
    if (chatContainer.value) chatContainer.value.scrollTop = chatContainer.value.scrollHeight
  })
}

const socket = useSocket()
let s1SocketPageRegistered = false

function registerS1SocketPage(connect = true) {
  if (!s1SocketPageRegistered) {
    socket.registerPage('adventurer_s1', false)
    registerS1SocketHandlers()
    s1SocketPageRegistered = true
  }
  if (connect) socket.connect()
}

function unregisterS1SocketPage() {
  if (!s1SocketPageRegistered) return
  socket.unregisterPage()
  s1SocketPageRegistered = false
}

const bufferedAdventurerEvents: any[] = []
const bufferedBuildingAiEvents: any[] = []

function payloadAdventurerIds(payload: any): number[] {
  if (!Array.isArray(payload?.ids)) return []
  return payload.ids.map((id: any) => Number(id)).filter(Number.isFinite)
}

function affectsMyAdventurer(payload: any): boolean {
  if (['title_catalog_changed', 'catalog_item_changed', 'party_preferences_changed'].includes(payload?.action)) {
    return store.adventurers.length > 0
  }
  const ids = payloadAdventurerIds(payload)
  if (ids.length === 0 || store.adventurers.length === 0) return false
  const myIds = new Set(store.adventurers.map(adv => adv.id))
  return ids.some(id => myIds.has(id))
}

function isBuildingSocketEvent(payload: any): boolean {
  const action = String(payload?.action || '')
  return action.startsWith('building_')
    || action.startsWith('dormitory_')
    || action.startsWith('dorm_room_')
    || payload?.building_instance_id != null
    || payload?.new_dorm_room_id != null
}

function affectsVisibleDormitory(payload: any): boolean {
  const action = String(payload?.action || '')
  if (
    action.startsWith('dorm_room_')
    || action === 'building_assigned'
    || payload?.new_dorm_room_id != null
    || payload?.dorm_upgrade_room_id != null
  ) {
    return true
  }

  const buildingId = Number(payload?.building_instance_id)
  if (
    Number.isFinite(buildingId)
    && store.dormitory.some(room => room.building_instance_id === buildingId)
  ) {
    return true
  }

  const changedIds = new Set(payloadAdventurerIds(payload))
  if (changedIds.size === 0) return false
  return store.dormitory.some(room => (
    room.beds.some(bed => bed != null && changedIds.has(bed.adventurer_id))
    || (room.upgrade_helpers ?? []).some(helper => changedIds.has(helper.adventurer_id))
  ))
}

function isPartySocketEvent(payload: any): boolean {
  const action = String(payload?.action || '')
  const reason = String(payload?.reason || '')
  return ['joined_party', 'left_party', 'status_changed'].includes(action)
    || ['manual_join', 'manual_leave', 'expedition_started', 'expedition_finished'].includes(reason)
}

function isTavernSocketEvent(payload: any): boolean {
  const action = String(payload?.action || '')
  return action.startsWith('tavern_') || isPartySocketEvent(payload)
}

function isSquadSocketEvent(payload: any): boolean {
  return String(payload?.action || '').startsWith('squad_')
}

function dirtyTabsForAdventurerEvent(payload: any): TabId[] {
  const tabs = new Set<TabId>(['adventurers'])
  if (affectsMyAdventurer(payload)) tabs.add('my-adventurers')
  if (isBuildingSocketEvent(payload)) tabs.add('building')
  if (isPartySocketEvent(payload)) tabs.add('party')
  if (isTavernSocketEvent(payload)) tabs.add('tavern')
  return [...tabs]
}

async function refreshMyAdventurerData(opts: { includeActivity?: boolean; force?: boolean } = {}) {
  const includeActivity = opts.includeActivity ?? true
  const force = opts.force ?? false
  const tasks: Promise<any>[] = [store.fetchAdventurers(force)]
  if (includeActivity) tasks.push(store.fetchMyActivity(true))
  await Promise.allSettled(tasks)
  if (activeTab.value === 'my-adventurers') await loadWaitingNpcOrigin(true)
}

function handleBuildingAiComplete(payload: any) {
  if (payload?.action !== 'building_ai_complete' || !payload?.data) return
  if (!initialSnapshotReady) {
    if (bufferedBuildingAiEvents.length < 20) bufferedBuildingAiEvents.push(payload)
    return
  }
  const advId = payloadAdventurerIds(payload)[0]
  const mine = advId != null ? store.adventurers.find(adv => adv.id === advId) : undefined
  if (mine) buildingAiResult.value = { ...payload.data, adventurer_name: mine.name }
}

const handleAdventurerChanged = socket.debouncedCallback((payload: any) => {
  if (!initialSnapshotReady) {
    if (bufferedAdventurerEvents.length < 100) bufferedAdventurerEvents.push(payload)
    return
  }

  if (isSquadSocketEvent(payload)) {
    markTabsDirty(['my-adventurers', 'adventurers'])
    const localEcho = store.consumeLocalSquadEcho(payload)
    if (localEcho === 'complete') {
      markTabFresh('my-adventurers')
    } else if (
      localEcho === 'pending'
      || showSquadModal.value
      || affectsMyAdventurer(payload)
    ) {
      void store.refreshSquadsAndAdventurers().then((refresh) => {
        if (refresh.ok) {
          markTabFresh('my-adventurers')
        } else {
          markTabDirty('my-adventurers')
        }
      }).catch(() => {
        markTabDirty('my-adventurers')
      })
    } else {
      store.markSquadsStale()
    }
    if (activeTab.value === 'adventurers') {
      scheduleBrowseSocketRefresh()
    }
    return
  }

  markTabsDirty(dirtyTabsForAdventurerEvent(payload))

  if (payload?.action === 'party_preferences_changed') {
    if (affectsMyAdventurer(payload)) {
      void store.refreshAdventurersAfterPreferenceChange().then((result) => {
        if (result?.code === 200) markTabFresh('my-adventurers')
        else markTabDirty('my-adventurers')
      }).catch(() => markTabDirty('my-adventurers'))
    }
    if (activeTab.value === 'adventurers') scheduleBrowseSocketRefresh()
    return
  }

  if (activeTab.value === 'my-adventurers') {
    if (affectsMyAdventurer(payload)) {
      const changedIds = payloadAdventurerIds(payload)
      if (detailAdvId.value != null && changedIds.includes(detailAdvId.value)) {
        _dirtyTabs.add('my-adventurers')
        return
      }
      void refreshMyAdventurerData({ includeActivity: false, force: false }).then(() => markTabFresh('my-adventurers'))
    }
  } else if (activeTab.value === 'adventurers') {
    scheduleBrowseSocketRefresh()
  } else if (activeTab.value === 'building') {
    if (isBuildingSocketEvent(payload)) {
      void store.fetchBuildings(true).then((result) => {
        if (result?.code !== 200 || !result?.data) {
          markTabDirty('building')
          return
        }
        if (payload?.action === 'building_completed') refreshBuildingZoneSummaries(true)
        else clearUnavailableBuildingZoneSummaries()
        markTabFresh('building')
      }).catch(() => {
        markTabDirty('building')
      })
      if (showDormitory.value && affectsVisibleDormitory(payload)) {
        void store.fetchDormitory(
          payload?.dorm_upgrade_room_id != null || payload?.new_dorm_room_id != null,
        ).catch(() => {
          markTabDirty('building')
        })
      }
    }
  } else if (activeTab.value === 'party') {
    if (isPartySocketEvent(payload)) {
      refreshPartyTab(true).then(() => markTabFresh('party'))
      if (affectsMyAdventurer(payload)) store.fetchAdventurers(true)
    }
  } else if (activeTab.value === 'tavern' && isTavernSocketEvent(payload)) {
    void store.fetchTavernSeats(true, true).then((result) => {
      if (result?.code === 200) markTabFresh('tavern')
      else markTabDirty('tavern')
    }).catch(() => markTabDirty('tavern'))
  }
})

async function refreshActiveTabAfterReconnect() {
  if (document.visibilityState !== 'visible') return
  let refreshed = true
  if (activeTab.value === 'my-adventurers') {
    await refreshMyAdventurerData({ includeActivity: false, force: true })
  } else if (activeTab.value === 'adventurers') {
    refreshed = await refreshCurrentBrowse()
  } else if (activeTab.value === 'party') {
    await refreshPartyTab(true)
  } else if (activeTab.value === 'building') {
    await store.fetchBuildings(true)
    refreshBuildingZoneSummaries(true)
  } else if (activeTab.value === 'tavern') {
    const results = await Promise.allSettled([store.fetchTavernSeats(true, true), store.fetchChat(1)])
    refreshed = results.every(result => result.status === 'fulfilled' && result.value?.code === 200)
  } else if (activeTab.value === 'logs') {
    refreshed = await refreshCurrentLogs(true)
  }
  if (refreshed) markTabFresh(activeTab.value)
}

function registerS1SocketHandlers() {
  socket.on('socket_connected', (state: any) => {
    if (state?.reconnected && !state?.recovered && initialSnapshotReady) {
      void refreshActiveTabAfterReconnect()
    }
  })

  socket.on('adventurer_changed', handleBuildingAiComplete)
  socket.on('adventurer_changed', (payload: any) => {
    if (payload?.action === 'building_completed' && payload?.dorm_naming_user_id === currentUserId.value) {
      message.success('🏆 你是这栋宿舍楼的最大贡献者，获得一次性命名权！去宿舍区给它起名吧')
    }
    if (payload?.action === 'building_completed' && payload?.dorm_upgrade_naming_user_id === currentUserId.value) {
      message.success('🏆 你是本次升级的最大贡献住客，获得一次性重新命名权！去宿舍区给它起名吧')
    }
  })
  socket.on('adventurer_changed', handleAdventurerChanged)

  socket.on('party_changed', socket.debouncedCallback((payload: any) => {
    if (!initialSnapshotReady) return
    const forceRefresh = String(payload?.action || '').startsWith('match_ticket')
    if (activeTab.value === 'party') {
      refreshPartyTab(forceRefresh).then(() => markTabFresh('party'))
    } else {
      markTabsDirty(['party'])
    }
  }, 2500))

  const refreshExpeditionLogs = socket.debouncedCallback(() => {
    if (!initialSnapshotReady) return
    if (activeTab.value === 'logs') {
      refreshCurrentLogs(true).then((refreshed) => {
        if (refreshed) markTabFresh('logs')
      })
    } else {
      markTabsDirty(['logs'])
    }
  })
  socket.on('expedition_log_changed', (payload: any) => {
    if (initialSnapshotReady && activeTab.value === 'logs' && payload?.data?.team_loot_registered) {
      void teamLootPanel.value?.refresh(1)
    }
    refreshExpeditionLogs()
  })

  socket.on('adventurer_chat_message', (data: any) => {
    if (data?.data && activeTab.value === 'tavern') {
      store.chatMessages.unshift(data.data)
      markTabFresh('tavern')
    } else if (data?.data) {
      markTabsDirty(['tavern'])
    }
  })

  socket.on('task_board_changed', (payload: any) => {
    if (!initialSnapshotReady) return
    if (activeTab.value !== 'building') {
      markTabsDirty(['building'])
      if (getTaskBoardBuilding()?.status === 'built') {
        void notifyTaskBoardIfNeeded(payload)
      }
      return
    }
    void handleTaskBoardChanged(payload).then(() => {
      markTabFresh('building')
    })
  })

  socket.on('data_appeal_changed', (payload: any) => {
    if (!initialSnapshotReady) return
    if (payload?.user_id && payload.user_id !== currentUserId.value) return
    void loadPendingRevertOffers()
  })

  socket.on('adventurer_s1_secret_auction_changed', () => {
    if (!initialSnapshotReady) return
    void store.refreshSecretAuctionOpenCount(true)
  })
}

async function initializeHome(force = squadSessionChangedBeforeMount) {
  if (homeDisposed || homeLoadPending.value) return
  const controller = new AbortController()
  homeLoadController = controller
  homeLoadPending.value = true
  homeLoadError.value = ''
  homeRetryNotice.value = '正在读取酒馆…'
  initialSnapshotReady = false
  try {
    await loadInitialHome(controller, force)
  } catch (error: any) {
    if (homeLoadController === controller && !controller.signal.aborted) {
      homeLoadError.value = error?.response?.data?.message || '酒馆暂时无法读取，请稍后重试'
    }
  } finally {
    if (homeLoadController === controller) {
      homeLoadPending.value = false
      homeRetryNotice.value = ''
      homeInitialReadFinished.value = true
    }
  }
}

async function loadInitialHome(controller: AbortController, force: boolean) {
  // F5 时先完成页面订阅和事件处理器注册，再请求初始快照，消除慢接口期间的事件丢失窗口。
  // 显式选择了浏览区服时，先由 init 确认是否为访客；访客绝不订阅归属区
  // 的实时事件，避免把一区推送混进二区画面（反向亦同）。
  if (!delaySocketUntilRealmResolved) {
    registerS1SocketPage()
  }
  store.prepareSquadSession(currentUserId.value)
  const userId = currentUserId.value
  const viewPreference = getS1PlayerRealmView()
  const isCurrent = () => !homeDisposed && homeLoadController === controller
    && currentUserId.value === userId && getS1PlayerRealmView() === viewPreference
  const fallback = [
    { key: 'notice', load: () => store.fetchNotice() },
    { key: 'adventurers', load: () => store.fetchAdventurers(true) },
    { key: 'buildings', load: () => store.fetchBuildings(true) },
    { key: 'seats', load: () => store.fetchTavernSeats(true, true) },
    { key: 'drinks', load: () => store.fetchDrinkMenu() },
    { key: 'upgrade', load: () => store.fetchTavernUpgradeProgress() },
    { key: 'activity', load: () => store.fetchMyActivity(true) },
    { key: 'materials', load: () => store.fetchMyMaterials() },
    { key: 'vote', load: () => store.fetchSupportVoteStatus() },
  ]
  const result = await loadS1Home({
    loadInit: (fresh) => store.fetchInit(fresh),
    force,
    signal: controller.signal,
    isCurrent,
    onRetry: () => { homeRetryNotice.value = '酒馆正忙，稍后继续读取…' },
    fallback,
  })
  if (!isCurrent() || result.kind === 'cancelled') return
  if (result.kind === 'busy') {
    homeLoadError.value = '酒馆正忙，暂时无法完成读取，请稍后重试'
    return
  }
  if (result.kind === 'unavailable') {
    homeLoadError.value = '酒馆读取超时或连接中断，请稍后重新读取'
    return
  }
  const initSucceeded = result.kind === 'ready'

  if (initSucceeded) {
    markTabFresh('tavern')
    markTabFresh('building')
    markTabFresh('my-adventurers')
    // Supplementary reads follow one another while the visible tab loads.
    void (async () => {
      const reads = [
        () => store.fetchSupportVoteStatus(),
        () => loadPendingRevertOffers(),
        () => loadPendingPlaceholderRecallOffers(),
      ]
      for (const read of reads) {
        if (!isCurrent() || controller.signal.aborted) return
        try { await read() } catch (error: any) {
          if (error?.response?.status === 503) return
        }
      }
    })()
  } else {
    const loaded = new Set(result.loaded)
    const tabSections: Partial<Record<TabId, string[]>> = {
      tavern: ['seats', 'drinks', 'upgrade'],
      building: ['buildings', 'materials'],
      'my-adventurers': ['adventurers', 'activity', 'materials'],
    }
    for (const [tab, sections] of Object.entries(tabSections)) {
      if (sections?.every(section => loaded.has(section))) markTabFresh(tab as TabId)
      else markTabDirty(tab as TabId)
    }
    if (loaded.has('buildings')) refreshBuildingZoneSummaries()
    if (loaded.size < fallback.length) homeLoadError.value = '部分酒馆数据尚未读取成功，可以稍后重新读取'
    void loadPendingRevertOffers()
  }

  if (delaySocketUntilRealmResolved && initSucceeded && !isRealmVisitor.value) {
    registerS1SocketPage()
  }
  if (isRealmVisitor.value) {
    bufferedBuildingAiEvents.splice(0)
    bufferedAdventurerEvents.splice(0)
  }

  // 补加载 init 没覆盖的数据（如 logs 分页、browse 分页等）
  if (initSucceeded || !_dirtyTabs.has(activeTab.value)) ensureFreshTabData(activeTab.value)
  myLoaded.value = initSucceeded || result.loaded.includes('adventurers')
  if (activeTab.value === 'my-adventurers') void loadWaitingNpcOrigin()
  initialSnapshotReady = true
  for (const payload of bufferedBuildingAiEvents.splice(0)) handleBuildingAiComplete(payload)
  const bufferedEvents = bufferedAdventurerEvents.splice(0)
  const bufferedSquadEvents = bufferedEvents.filter(isSquadSocketEvent)
  const bufferedNonSquadEvents = bufferedEvents.filter(payload => !isSquadSocketEvent(payload))
  if (bufferedSquadEvents.length > 0) {
    store.markSquadsStale()
    markTabsDirty(['my-adventurers', 'adventurers'])
    if (
      activeTab.value === 'my-adventurers'
      || bufferedSquadEvents.some(affectsMyAdventurer)
    ) {
      void store.refreshSquadsAndAdventurers().then((refresh) => {
        if (refresh.ok) markTabFresh('my-adventurers')
        else markTabDirty('my-adventurers')
      }).catch(() => {
        markTabDirty('my-adventurers')
      })
    }
  }
  if (activeTab.value === 'adventurers' && bufferedSquadEvents.length > 0) {
    scheduleBrowseSocketRefresh()
  }
  for (const payload of bufferedNonSquadEvents) {
    markTabsDirty(dirtyTabsForAdventurerEvent(payload))
  }
  const squadRefreshCoversActiveRoster = bufferedSquadEvents.length > 0
    && (activeTab.value === 'my-adventurers' || activeTab.value === 'adventurers')
  if (
    activeTab.value === 'my-adventurers'
    && bufferedSquadEvents.length === 0
    && bufferedNonSquadEvents.some(affectsMyAdventurer)
  ) {
    void refreshMyAdventurerData({ includeActivity: false, force: false })
  } else if (bufferedNonSquadEvents.length > 0 && !squadRefreshCoversActiveRoster) {
    const activeEvent = [...bufferedNonSquadEvents].reverse().find((payload) => {
      if (activeTab.value === 'building') return isBuildingSocketEvent(payload)
      if (activeTab.value === 'party') return isPartySocketEvent(payload)
      if (activeTab.value === 'tavern') return isTavernSocketEvent(payload)
      return true
    })
    if (activeEvent) handleAdventurerChanged(activeEvent)
  }
  // 任务栏角标已在 init 中返回，无需额外 fetchBuildings + refreshTaskBoardSummary
  // 子面板已改为 v-if，进入对应 zone 才 mount 拉数据

  // 酒馆座位轮询：刷新占座 / 点酒；闲聊文案按后端 5 分钟 tick 桶更新
  if (!tavernPollTimer) tavernPollTimer = setInterval(() => {
    if (initialSnapshotReady && activeTab.value === 'tavern' && document.visibilityState === 'visible') {
      void store.fetchTavernSeats(true).catch(() => markTabDirty('tavern'))
    }
  }, TAVERN_POLL_INTERVAL_MS + Math.floor(Math.random() * TAVERN_POLL_JITTER_MS))

  // 兜底轮询：目标区实时订阅不健康时，刷新当前可见的建筑或日志数据。
  if (!fallbackPollTimer) fallbackPollTimer = setInterval(() => {
    if (!initialSnapshotReady || document.visibilityState !== 'visible') return
    const realtimeHealthy = socket.isSocketHealthy()
    if (realtimeHealthy) return
    if (activeTab.value === 'building') {
      void store.fetchBuildings(false)
      refreshBuildingZoneSummaries(false)
      store.fetchDormitory(false)
    } else {
      void refreshActiveTabAfterReconnect().catch(() => markTabDirty(activeTab.value))
    }
  }, FALLBACK_POLL_INTERVAL_MS)
}

onMounted(() => initializeHome())

onUnmounted(() => {
  homeDisposed = true
  homeLoadController?.abort()
  initialSnapshotReady = false
  unregisterS1SocketPage()
  if (logSearchTimer) {
    clearTimeout(logSearchTimer)
    logSearchTimer = null
  }
  if (taskBadgeDebounceTimer) {
    clearTimeout(taskBadgeDebounceTimer)
    taskBadgeDebounceTimer = null
  }
  clearBrowseSocketRefreshTimer()
  browseSocketRefreshQueued = false
  if (tavernPollTimer) {
    clearInterval(tavernPollTimer)
    tavernPollTimer = null
  }
  if (fallbackPollTimer) {
    clearInterval(fallbackPollTimer)
    fallbackPollTimer = null
  }
})
</script>

<style scoped lang="scss">
.advs1 {
  width: 100%;
  min-width: 0;
  max-width: 1100px;
  margin: 0 auto;
  padding: 16px;
  box-sizing: border-box;
  font-weight: 400;
  overflow-x: clip;
}

.advs1-header {
  --outpost-brass: #5d87b3;
  position: relative;
  overflow: hidden;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  min-height: 132px;
  margin-bottom: 16px;
  padding: 22px 26px;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--outpost-brass) 26%, var(--color-border));
  border-radius: 18px;
  background:
    radial-gradient(circle at 86% 50%, rgba(93, 135, 179, 0.15), transparent 25%),
    linear-gradient(125deg, rgba(93, 135, 179, 0.09), var(--color-surface) 58%, var(--color-bg));
  background:
    radial-gradient(circle at 86% 50%, color-mix(in srgb, var(--outpost-brass) 15%, transparent), transparent 25%),
    linear-gradient(125deg, color-mix(in srgb, var(--outpost-brass) 9%, var(--color-surface)), var(--color-surface) 58%, var(--color-bg));
  box-shadow: 0 12px 30px rgba(0, 0, 0, 0.1);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 5%, transparent), 0 12px 30px rgba(0, 0, 0, 0.1);
}

/* 旧 WebView 会保留未知函数的自定义属性 token，必须按能力覆盖现代值。 */
@supports (color: color-mix(in srgb, #000 50%, #fff)) {
  .advs1-header {
    --outpost-brass: color-mix(in srgb, #5d87b3 74%, var(--color-primary));
  }
}

.advs1-header::before {
  content: '';
  position: absolute;
  inset: 0;
  opacity: 0.35;
  background: repeating-linear-gradient(105deg, transparent 0 28px, rgba(93, 135, 179, 0.04) 29px 30px);
  background: repeating-linear-gradient(105deg, transparent 0 28px, color-mix(in srgb, var(--outpost-brass) 4%, transparent) 29px 30px);
  pointer-events: none;
}

.advs1-header-copy { position: relative; z-index: 1; flex: 1; min-width: 0; }

.advs1-kicker {
  display: block;
  margin-bottom: 8px;
  color: var(--outpost-brass);
  font-size: 10px;
  font-weight: 800;
  letter-spacing: 0.18em;
}

.advs1-title {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
  margin: 0 0 7px;
  font-size: 25px;
  color: var(--color-text);
}

.advs1-title em {
  padding: 3px 7px;
  border: 1px solid var(--outpost-brass);
  border: 1px solid color-mix(in srgb, var(--outpost-brass) 45%, var(--color-border));
  border-radius: 5px;
  color: var(--outpost-brass);
  font-size: 11px;
  font-style: normal;
  letter-spacing: 0.08em;
}

.advs1-sigil {
  display: grid;
  width: 36px;
  height: 36px;
  place-items: center;
  border-radius: 10px;
  background: linear-gradient(145deg, #78a7d2, #29445f);
  background: linear-gradient(145deg, #78a7d2, color-mix(in srgb, var(--outpost-brass) 68%, #29445f));
  color: #fffaf0;
  box-shadow: 0 6px 18px rgba(93, 135, 179, 0.24);
  box-shadow: 0 6px 18px color-mix(in srgb, var(--outpost-brass) 24%, transparent);
}

.advs1-beacon {
  position: relative;
  z-index: 1;
  display: grid;
  width: 74px;
  height: 74px;
  flex: none;
  place-items: center;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--outpost-brass) 22%, var(--color-border));
  border-radius: 50%;
}

.advs1-beacon::before,
.advs1-beacon::after {
  content: '';
  position: absolute;
  border: 1px dashed rgba(93, 135, 179, 0.38);
  border: 1px dashed color-mix(in srgb, var(--outpost-brass) 38%, transparent);
  border-radius: 50%;
  animation: outpost-spin 18s linear infinite;
}
.advs1-beacon::before { inset: 8px; }
.advs1-beacon::after { inset: 17px; animation-direction: reverse; animation-duration: 12s; }
.advs1-beacon span {
  width: 10px;
  height: 22px;
  border-radius: 50% 50% 45% 45%;
  background: var(--outpost-brass);
  box-shadow: 0 0 18px rgba(93, 135, 179, 0.62);
  box-shadow: 0 0 18px color-mix(in srgb, var(--outpost-brass) 62%, transparent);
  animation: outpost-flame 2.4s ease-in-out infinite;
}

@keyframes outpost-spin { to { transform: rotate(360deg); } }
@keyframes outpost-flame { 50% { transform: translateY(-2px) scale(0.88, 1.08); opacity: 0.72; } }

.advs1-subtitle {
  margin: 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.notice-banner {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 12px;
  padding: 12px 14px;
  margin-bottom: 12px;
  background: color-mix(in srgb, var(--color-primary) 14%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-primary) 35%, var(--color-border));
  border-radius: 8px;
}
.notice-banner-body {
  flex: 1;
  min-width: 0;
  font-size: 13px;
  color: var(--color-text);
  line-height: 1.55;
}
.notice-banner-body strong {
  display: block;
  margin-bottom: 4px;
  font-size: 13px;
}
.notice-banner-text {
  margin: 0;
  white-space: pre-wrap;
  word-break: break-word;
}
.notice-banner-btn {
  flex-shrink: 0;
  align-self: center;
}

.admin-notice-input {
  width: 100%;
  padding: 8px 10px;
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  resize: vertical;
}

.notice-card {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 14px;
  margin-bottom: 12px;
  font-size: 13px;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.tabs {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(108px, 1fr));
  gap: 6px;
  margin-bottom: 16px;
  border-bottom: 1px solid var(--color-border);
  overflow-x: auto;
  scrollbar-width: none;

  &::-webkit-scrollbar {
    display: none;
  }
}

.tab-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 5px;
  flex: none;
  white-space: nowrap;
  padding: 8px 18px;
  font-size: 14px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: transparent;
  border: none;
  border-bottom: 2px solid transparent;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s;
  position: relative;

  &:hover {
    color: var(--color-text);
  }

  &.active {
    color: var(--color-primary);
    border-bottom-color: var(--color-primary);
  }
}

.tab-btn-label {
  display: inline-block;
}

.tab-task-count {
  position: absolute;
  top: 2px;
  right: 2px;
  min-width: 16px;
  height: 16px;
  padding: 0 4px;
  border-radius: 999px;
  background: var(--color-primary);
  color: #fff;
  font-size: 10px;
  font-weight: 700;
  line-height: 16px;
  text-align: center;
}

.col-title {
  margin: 0 0 10px;
  font-size: 15px;
  color: var(--color-text);
}

.empty {
  padding: 32px 0;
  text-align: center;
  color: var(--color-text-secondary);
}

.adv-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px;
}

.log-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.log-card {
  padding: 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 0.15s;

  &:hover {
    border-color: var(--color-primary);
  }
}

.log-card[class*='se-theme-'] {
  position: relative;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--se-accent) 58%, var(--color-border));
  background:
    linear-gradient(105deg, color-mix(in srgb, var(--se-accent) 10%, transparent), transparent 52%),
    var(--color-surface);
  box-shadow: inset 4px 0 0 var(--se-accent), 0 4px 14px color-mix(in srgb, var(--se-accent) 10%, transparent);
}

.log-special-event {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  margin-bottom: 6px;
  padding: 2px 8px;
  border: 1px solid color-mix(in srgb, var(--se-accent) 50%, var(--color-border));
  border-radius: 999px;
  background: color-mix(in srgb, var(--se-accent) 10%, var(--color-surface));
  color: var(--se-accent);
  font-size: 11px;
  font-weight: 700;
}

.log-card-head {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: start;
  gap: 8px;
  margin-bottom: 6px;
}

.log-where {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  font-size: 13px;
  color: var(--color-text);
}

.log-party {
  color: var(--color-text-secondary);
}

.log-status {
  flex: none;
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);

  &.st-completed {
    color: var(--color-success);
    border-color: var(--color-success);
  }

  &.st-failed {
    color: var(--color-danger);
    border-color: var(--color-danger);
  }

  &.st-generating,
  &.st-in_progress {
    color: var(--color-primary);
    border-color: var(--color-primary);
  }
}

.log-status-wrap {
  display: inline-flex;
  flex: none;
  align-items: center;
  gap: 4px;
}

.log-summary {
  margin: 0 0 6px;
  font-size: 12px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.log-card-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 5px 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.log-admin-actions {
  display: flex;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 6px;
  margin-top: 8px;
}
@media (max-width: 760px) { }
.log-toolbar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}

.log-search-input {
  flex: 1;
  padding: 6px 12px;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-text);
  outline: none;
  transition: border-color 0.15s;

  &:focus {
    border-color: var(--color-primary);
  }

  &::placeholder {
    color: var(--color-text-secondary);
  }
}

.log-status-select {
  padding: 6px 10px;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-text);
  outline: none;
  cursor: pointer;
}

/* 聊天 */
.chat-col {
  display: flex;
  flex-direction: column;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 12px;
}

.chat-messages {
  flex: 1;
  min-height: 280px;
  max-height: 460px;
  overflow-y: auto;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding-right: 4px;
}

.load-more {
  align-self: center;
  font-size: 12px;
  padding: 2px 12px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.chat-msg {
  background: var(--color-bg);
  border-radius: 8px;
  padding: 8px 10px;
}

.chat-msg-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 2px;
}

.chat-nick {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
}

.chat-title {
  margin-right: 4px;
}

.chat-time {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.chat-content {
  margin: 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--color-text);
  white-space: pre-wrap;
  word-break: break-word;
}

.chat-input-area {
  margin-top: 10px;
  border-top: 1px solid var(--color-border);
  padding-top: 10px;
}

.chat-input {
  width: 100%;
  padding: 8px 10px;
  font-size: 13px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  resize: vertical;
  outline: none;

  &:focus {
    border-color: var(--color-primary);
  }
}

.chat-input-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-top: 6px;
}

.char-count {
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* ── 酒馆大堂 ───────────────────────────────────────────── */
.tavern-layout {
  display: flex;
  gap: 14px;
  align-items: stretch;
}

.tavern-hall {
  flex: 1;
  min-width: 0;
  padding: 16px;
  overflow: hidden;
  background: var(--color-surface);
  background:
    radial-gradient(circle at 12% 0%, rgba(194, 130, 62, 0.13), transparent 34%),
    linear-gradient(155deg, color-mix(in srgb, #8b5a2b 8%, var(--color-surface)), var(--color-surface) 58%);
  border: 1px solid var(--color-border);
  border-radius: 14px;
  box-shadow: 0 10px 28px rgba(39, 24, 14, 0.08);
}

.tavern-hall-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 12px;

  .col-title {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 0;
  }

  .col-title::before {
    content: '◆';
    font-size: 11px;
    color: #b87938;
    text-shadow: 0 0 12px rgba(184, 121, 56, 0.45);
  }
}

.tavern-chat {
  width: 320px;
  flex-shrink: 0;
}

@media (max-width: 900px) {
  .tavern-layout {
    flex-direction: column;
  }

  .tavern-chat {
    width: 100%;
  }
}

.hall-bar {
  position: relative;
  padding: 13px;
  margin-bottom: 14px;
  background: var(--color-bg);
  background: linear-gradient(180deg, color-mix(in srgb, #9a6334 9%, var(--color-bg)), var(--color-bg));
  border: 1px solid #9b7048;
  border-color: color-mix(in srgb, #8b5a2b 44%, var(--color-border));
  border-radius: 12px;
  box-shadow: inset 0 1px rgba(255, 255, 255, 0.08);
}

.bar-counter {
  position: relative;
  padding: 9px 12px;
  font-size: 13px;
  font-weight: 700;
  letter-spacing: 0.12em;
  text-align: center;
  color: var(--color-text);
  background: #8b5a2b;
  background: linear-gradient(180deg, color-mix(in srgb, #a86e38 38%, var(--color-surface)), color-mix(in srgb, #70441f 25%, var(--color-surface)));
  border: 1px solid #8b5a2b;
  border-color: color-mix(in srgb, #8b5a2b 62%, var(--color-border));
  border-radius: 8px 8px 10px 10px;
  box-shadow: 0 4px 0 color-mix(in srgb, #5f3718 34%, var(--color-border)), 0 7px 14px rgba(58, 35, 19, 0.12);
}

.bar-stools {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 8px;
  margin-top: 10px;
}

@media (max-width: 1000px) {
  .bar-stools {
    grid-template-columns: repeat(3, 1fr);
  }
}

.hall-tables {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}

@media (max-width: 640px) {
  .hall-tables {
    grid-template-columns: 1fr;
  }
}

.hall-table {
  position: relative;
  min-width: 0;
  padding: 11px;
  background: var(--color-bg);
  background: linear-gradient(145deg, color-mix(in srgb, #8b5a2b 7%, var(--color-bg)), var(--color-bg));
  border: 1px solid #9b7048;
  border-color: color-mix(in srgb, #8b5a2b 34%, var(--color-border));
  border-radius: 12px;
  box-shadow: 0 5px 14px rgba(48, 30, 17, 0.06);
}

.table-name {
  width: fit-content;
  margin: -2px auto 10px;
  padding: 3px 11px;
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.05em;
  text-align: center;
  color: var(--color-text-secondary);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 999px;
}

.table-seats {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 8px;
}

.seat {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-height: 84px;
  padding: 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 9px;
  box-shadow: 0 2px 7px rgba(36, 24, 16, 0.04);
}

.seat-vacant {
  align-items: center;
  justify-content: center;
  color: var(--color-text-secondary);
  border-style: dashed;
  background: transparent;
  box-shadow: none;
}

.seat-clickable {
  cursor: pointer;
  transition: transform 0.15s ease, border-color 0.15s ease, background 0.15s ease, box-shadow 0.15s ease;

  &:hover {
    transform: translateY(-2px);
    border-color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 5%, var(--color-bg));
    box-shadow: 0 7px 16px rgba(36, 24, 16, 0.1);
  }
}

.seat-mine {
  border-color: color-mix(in srgb, var(--color-primary) 55%, var(--color-border));
  background: color-mix(in srgb, var(--color-primary) 6%, var(--color-bg));
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-primary) 22%, transparent), 0 5px 13px rgba(36, 24, 16, 0.07);
}

.seat-empty-icon {
  font-size: 20px;
  line-height: 1;
  opacity: 0.72;
  filter: saturate(0.78);
}

.seat-empty-label {
  font-size: 11px;
}

.seat-name {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.seat-drink {
  font-size: 12px;
  color: var(--color-text);
}

.seat-drink-none {
  color: var(--color-text-secondary);
}

.seat-food {
  font-size: 12px;
  color: var(--color-primary);
}

.tavern-menu-title {
  margin: 14px 0 8px;
  font-size: 13px;
  color: var(--color-text);
}

.seat-drink-text {
  margin: 0;
  font-size: 11px;
  font-style: italic;
  line-height: 1.4;
  color: var(--color-text-secondary);
  word-break: break-word;
}

.seat-chatter {
  margin: 4px 0 0;
  padding: 4px 6px;
  font-size: 11px;
  line-height: 1.45;
  color: var(--color-text);
  word-break: break-word;
  background: color-mix(in srgb, var(--color-primary) 8%, var(--color-bg));
  border-left: 2px solid color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
  border-radius: 0 6px 6px 0;
}

.treat-request {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
  margin-top: 4px;
  padding: 5px;
  font-size: 11px;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-primary) 10%, var(--color-bg));
  border: 1px solid color-mix(in srgb, var(--color-primary) 30%, var(--color-border));
  border-radius: 6px;

  span {
    flex: 1 1 100%;
  }

  button {
    min-height: 28px;
    padding: 4px 10px;
    font-size: 12px;
    color: var(--color-text);
    background: var(--color-surface);
    border: 1px solid var(--color-border);
    border-radius: 5px;
    cursor: pointer;
    touch-action: manipulation;
  }
}

.hall-hint {
  margin: 13px 2px 0;
  padding: 9px 11px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-text-secondary);
  background: var(--color-bg);
  background: color-mix(in srgb, #b87938 6%, var(--color-bg));
  border-left: 3px solid #b87938;
  border-radius: 0 8px 8px 0;
}

@media (max-width: 640px) {
  .tavern-hall {
    padding: 12px;
    border-radius: 12px;
  }

  .bar-stools {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }

  .table-seats {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}

/* 入座 / 点酒弹窗 */
.tavern-modal-hint {
  margin: 0 0 10px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.tavern-pick-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 320px;
  overflow-y: auto;
}

.tavern-pick-item {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: 8px 10px;
  text-align: left;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s;

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
  }

  &.selected {
    border-color: var(--color-primary);
    box-shadow: 0 0 0 1px var(--color-primary);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.pick-main {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
}

.pick-sub {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.drink-price {
  margin-left: 6px;
  color: var(--color-primary);
}

.tavern-modal-actions {
  display: flex;
  gap: 8px;
  justify-content: flex-end;
}

/* 冒险者标签 */
.browse-section {
  margin-top: 8px;
}

.filter-bar {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 12px;

  select,
  .search-input {
    padding: 6px 10px;
    font-size: 13px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 6px;
    outline: none;

    &:focus {
      border-color: var(--color-primary);
    }
  }

  .search-input {
    flex: 1;
    min-width: 160px;
  }
}

.btn-primary {
  color: #fff;
  background: var(--color-primary);
  border: none;
  border-radius: 6px;
  cursor: pointer;

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.btn-sm {
  padding: 6px 16px;
  font-size: 13px;
  font-weight: 600;
}

/* 建筑 */
.building-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(180px, 1fr));
  gap: 12px;
}

.building-grid--facilities {
  grid-template-columns: repeat(auto-fill, minmax(min(100%, 250px), 1fr));
  align-items: stretch;
}

// 同排的名称、说明、工位和费用共用行轨道；旧浏览器保留等高卡片回退。
@supports (grid-template-rows: subgrid) {
  @media (min-width: 641px) {
    .building-grid--facilities > :deep(.building-card) {
      grid-row: span 5;
      grid-template-rows: subgrid;
    }
  }
}

.building-grid--zones > .building-card {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 8px;
  padding: 20px 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.1s;

  &:hover:not(.disabled) {
    border-color: var(--color-primary);
    transform: translateY(-2px);
  }

  &.disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
}

.dorm-card {
  border-color: var(--color-success);

  &:hover {
    border-color: var(--color-success);
    box-shadow: 0 0 0 1px var(--color-success);
  }
}

.admin-card {
  border-style: dashed;
  opacity: 0.8;

  &:hover {
    opacity: 1;
    border-color: var(--color-primary);
  }
}

.section-card {
  background: var(--color-surface, var(--color-bg));
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 12px;
}

.section-title {
  font-size: 14px;
  font-weight: 600;
  margin: 0 0 10px;
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

.admin-hint-warn {
  color: var(--color-warning, #b8860b);
}

.admin-toggle-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
  cursor: pointer;
  user-select: none;
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

.locked {
  opacity: 0.5;
  cursor: not-allowed;

  &:hover {
    transform: none;
    border-color: var(--color-border);
  }
}

.building-icon {
  font-size: 32px;
  line-height: 1;
}

.building-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text);
}

.building-desc {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.zone-icon-wrap {
  position: relative;
  display: inline-block;
}

.zone-task-count {
  position: absolute;
  top: -6px;
  right: -10px;
  min-width: 18px;
  height: 18px;
  padding: 0 5px;
  border-radius: 999px;
  background: var(--color-primary);
  color: #fff;
  font-size: 11px;
  font-weight: 700;
  line-height: 18px;
  text-align: center;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.2);
}

/* 宿舍区 */
.dorm-header {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 12px;
  margin-bottom: 16px;
}

.btn-back {
  padding: 4px 12px;
  font-size: 13px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;

  &:hover {
    border-color: var(--color-primary);
  }
}

.commission-subtabs {
  display: flex;
  gap: 8px;
  margin-bottom: 14px;

  button {
    flex: 1;
    padding: 8px 10px;
    font-size: 13px;
    color: var(--color-text-secondary);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 8px;
    cursor: pointer;

    &:hover {
      border-color: var(--color-primary);
    }

    &.active {
      color: #fff;
      background: var(--color-primary);
      border-color: var(--color-primary);
    }
  }
}

.btn-swap {
  margin-left: auto;
  padding: 4px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
  transition: all 0.15s;

  &:hover {
    border-color: var(--color-primary);
    color: var(--color-text);
  }

  &.active {
    color: #fff;
    background: var(--color-primary);
    border-color: var(--color-primary);
  }
}

.swap-hint {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 12px;
  font-size: 13px;
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  border: 1px solid var(--color-primary);
  border-radius: 6px;
}

.swap-hint-note {
  font-size: 11px;
  color: var(--color-text-secondary);
}

/* ── 互换请求通知面板 ──────────────────────────────────────────────────── */
.swap-request-panel {
  margin-bottom: 16px;
}

.srp-section {
  margin-bottom: 8px;
}

.srp-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 6px;
}

.srp-card {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 4px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  font-size: 12px;
}

.srp-card-info {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}

.srp-adv-name {
  font-weight: 600;
  color: var(--color-primary);
}

.srp-hint {
  color: var(--color-text-secondary);
}

.srp-user {
  color: var(--color-text-secondary);
  font-size: 11px;
}

.srp-status {
  color: var(--color-text-secondary);
  font-size: 11px;
}

.srp-card-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

/* ── 自建宿舍楼面板 ──────────────────────────────────────────────────────── */
.dorm-build-panel {
  padding: 14px 16px;
  margin-bottom: 16px;
  background: color-mix(in srgb, var(--color-primary) 5%, var(--color-surface));
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.dbp-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 16px;
  margin-bottom: 8px;
}

.dbp-materials,
.dbp-concurrent {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 14px;
}

.dbp-label {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.dbp-mat {
  padding: 2px 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  font-weight: 600;
}

.dbp-cnt {
  padding: 2px 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 4px;
  font-weight: 600;
  color: var(--color-primary);
}

.dbp-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
}

.dbp-instances {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 10px;
}

.dbp-card {
  padding: 10px 12px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 6px;

  &.st-building {
    border-color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 8%, var(--color-surface));
  }
}

.dbp-card-head {
  display: flex;
  justify-content: space-between;
  margin-bottom: 4px;
  font-size: 13px;
  font-weight: 600;
}

.dbp-card-beds {
  font-size: 11px;
  padding: 1px 6px;
  background: var(--color-bg);
  border-radius: 3px;
}

.dbp-card-need {
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 4px;
}

.dbp-card-shortage {
  font-size: 12px;
  color: var(--color-danger);
  margin-bottom: 6px;
}

.dbp-progress-wrap {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}

.dbp-progress-bar {
  flex: 1;
  height: 8px;
  background: var(--color-bg);
  border-radius: 4px;
  overflow: hidden;
}

.dbp-progress-fill {
  height: 100%;
  background: var(--color-primary);
  transition: width 0.3s ease;
}

.dbp-progress-text {
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary);
  min-width: 32px;
  text-align: right;
}

.dbp-tag-mine,
.dbp-tag-other {
  font-size: 11px;
  padding: 2px 6px;
  border-radius: 4px;
  margin-left: 6px;
}

.dbp-tag-mine {
  background: var(--color-primary);
  color: #fff;
}

.dbp-tag-other {
  background: var(--color-bg);
  color: var(--color-text-secondary);
}

.dbp-card.is-mine {
  border-left: 3px solid var(--color-primary);
}

.dbp-admin-settings {
  display: flex;
  flex-direction: column;
  gap: 6px;
  padding: 8px 10px;
  background: var(--color-bg);
  border-radius: 6px;
  margin-bottom: 8px;
}

.dbp-setting-row {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dbp-settings-input {
  width: 80px;
  padding: 4px 6px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  font-size: 12px;
}

.dbp-materials-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin: 6px 0;
}

.dbp-mat-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 6px;
  background: var(--color-bg);
  border-radius: 4px;
}

.dbp-mat-row-head {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dbp-mat-icon {
  font-size: 14px;
}

.dbp-mat-name {
  font-size: 12px;
  min-width: 36px;
}

.dbp-mat-progress {
  flex: 1;
  position: relative;
  height: 14px;
  background: var(--color-bg);
  border-radius: 7px;
  overflow: hidden;
}

.dbp-mat-bar {
  height: 100%;
  background: linear-gradient(90deg, #6abf69, #4a9d4a);
  transition: width 0.3s ease;
}

.dbp-mat-text {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 10px;
  font-weight: 600;
  color: var(--color-text);
}

.dbp-mat-mine {
  margin-left: 4px;
  color: var(--color-primary);
}

.dbp-mat-form {
  display: flex;
  gap: 4px;
  align-items: center;
}

.dbp-mat-have {
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;
}

.dbp-mat-empty {
  font-size: 11px;
  color: var(--color-danger);
  white-space: nowrap;
}

.dbp-mat-amount {
  width: 70px;
  padding: 3px 6px;
  font-size: 11px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
}

.dbp-card-cancel {
  margin-top: 6px;
}

.log-favorite-btn {
  margin-left: auto;
  padding: 1px 8px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background: transparent;
  color: var(--color-text-secondary);
  font-size: 12px;
  cursor: pointer;

  &.active {
    border-color: #d6a318;
    color: #d6a318;
  }

  &:disabled {
    cursor: wait;
    opacity: 0.65;
  }
}

.dorm-list-toolbar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  flex-wrap: wrap;
  margin: 12px 0;
  padding: 9px 12px;
  border: 1px solid var(--color-border, #d8dee8);
  border-radius: 8px;
  background: #f4f7fb;
  background: color-mix(in srgb, var(--color-primary, #547aa5) 6%, var(--color-surface, #fff));
  color: var(--color-text-secondary, #5f6875);
  font-size: 12px;
}

.dorm-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
  gap: 12px;
}

/* 统一格式预览卡：头部/徽章/床位指示/住户 固定四行，1/2/4 床卡片同高对齐 */
.dorm-room {
  padding: 14px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  cursor: pointer;
  transition: border-color 0.15s, transform 0.15s;

  &:hover {
    border-color: var(--color-primary);
    transform: translateY(-1px);
  }
}

.room-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
}

.room-icon {
  font-size: 20px;
}

.room-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.room-cap {
  font-size: 12px;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--color-border);
  color: var(--color-text-secondary);
  flex-shrink: 0;
}

.room-badges {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 10px;
}

.room-relation-badge {
  padding: 1px 7px;
  border-radius: 999px;
  font-size: 11px;
  font-weight: 700;
}

.room-relation-badge.mine {
  color: #2e679f;
  background: #e6f1fb;
  background: color-mix(in srgb, var(--color-primary, #4f8bc9) 14%, var(--color-surface, #fff));
}

.room-relation-badge.followed {
  color: #7452a0;
  background: #f0e9f8;
  background: color-mix(in srgb, #9b7ac7 14%, var(--color-surface, #fff));
}

.dorm-pagination {
  display: flex;
  justify-content: center;
  margin-top: 18px;
}

.level-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  background: color-mix(in srgb, var(--color-primary) 12%, transparent);
  color: var(--color-primary);
  font-weight: 600;
}

.naming-badge {
  font-size: 11px;
  padding: 1px 8px;
  border-radius: 10px;
  font-weight: 600;
  color: var(--color-warning);
  background: color-mix(in srgb, var(--color-warning) 12%, transparent);
  border: 1px dashed var(--color-warning);
}

.naming-badge-upgrade {
  color: var(--color-primary);
  background: color-mix(in srgb, var(--color-primary) 12%, transparent);
  border-color: var(--color-primary);
}

.room-pips {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}

.bed-pip {
  width: 28px;
  height: 28px;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 14px;
  border-radius: 6px;
  border: 1px dashed var(--color-border);
  opacity: 0.35;

  &.occupied {
    opacity: 1;
    border: 1px solid color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
    background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  }
}

.room-residents {
  font-size: 12px;
  color: var(--color-text-secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* 冒险者标签 */
.mine-section {
  margin-bottom: 24px;

  .revert-offer-page-banner {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 12px;
    margin-bottom: 12px;
    padding: 12px 14px;
    border: 1px solid #f0a02066;
    border-radius: 10px;
    background: color-mix(in srgb, #f0a020 10%, var(--color-surface));
  }

  .revert-offer-page-text {
    display: flex;
    flex-direction: column;
    gap: 4px;
    font-size: 13px;
  }

  .placeholder-recall-banner {
    border-color: #2080f066;
    background: color-mix(in srgb, #2080f0 10%, var(--color-surface));
  }

  .placeholder-recall-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
  }
}

.mine-head {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;

  .mine-head-actions {
    display: flex;
    gap: 8px;
    align-items: center;
    flex-wrap: wrap;
  }

  .col-title {
    margin: 0;
  }
}

.mine-title-tools {
  display: inline-flex;
  max-width: 100%;
  min-height: 30px;
  align-items: stretch;
  overflow: hidden;
  border: 1px solid rgba(217, 185, 110, 0.52);
  border-radius: 8px;
  background: #171b21;
  background: color-mix(in srgb, #8b78c9 7%, var(--color-surface, #171b21));
}

.mine-title-tools-label {
  display: inline-flex;
  align-items: center;
  padding: 4px 8px;
  color: #e2c57e;
  font-size: 11px;
  font-weight: 700;
  white-space: nowrap;
}

.mine-title-tool {
  display: inline-flex;
  min-width: 0;
  align-items: center;
  gap: 4px;
  padding: 4px 8px;
  border: 0;
  border-left: 1px solid rgba(217, 185, 110, 0.28);
  border-radius: 0;
  background: #1a2026;
  background: color-mix(in srgb, #d9b96e 6%, var(--color-bg, #11161b));
  color: var(--color-text-secondary, #b8c2cc);
  font-size: 11px;
  cursor: pointer;
  white-space: nowrap;
}

.mine-title-tool:hover:not(:disabled) {
  background: #252329;
  background: color-mix(in srgb, #d9b96e 13%, var(--color-bg, #11161b));
  color: var(--color-text, #e6edf3);
}

.mine-title-tool:disabled {
  cursor: wait;
  opacity: 0.62;
}

.mine-title-tool.effects.disabled {
  background: #18232a;
  background: color-mix(in srgb, #4f9ed8 8%, var(--color-bg, #11161b));
  color: #8fb8d2;
}

.mine-title-tool small {
  padding: 1px 4px;
  border-radius: 4px;
  background: rgba(255, 255, 255, 0.07);
  font-size: 9px;
}

.support-vote-btn {
  border-color: rgba(160, 96, 240, 0.45);
}

.support-vote-mask {
  position: fixed;
  inset: 0;
  z-index: 1100;
  display: flex;
  align-items: center;
  justify-content: center;
  padding: 20px;
  background: rgba(0, 0, 0, 0.58);
  backdrop-filter: blur(4px);
}

.support-vote-modal {
  width: min(100%, 520px);
  max-height: min(88vh, 680px);
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border: 1px solid var(--color-border);
  border-radius: 16px;
  background: var(--color-surface);
  box-shadow: 0 24px 80px rgba(0, 0, 0, 0.32);
}

.support-vote-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 16px 18px;
  border-bottom: 1px solid var(--color-border);
  background: linear-gradient(135deg, rgba(160, 96, 240, 0.14), rgba(24, 160, 88, 0.08));

  h4 {
    margin: 0;
    color: var(--color-text);
  }
}

.support-vote-kicker {
  margin: 0 0 4px;
  font-size: 12px;
  font-weight: 800;
  color: var(--color-primary);
}

.support-vote-close {
  width: 32px;
  height: 32px;
  border: 1px solid var(--color-border);
  border-radius: 50%;
  background: var(--color-surface);
  color: var(--color-text-secondary);
  cursor: pointer;
}

.support-vote-body {
  padding: 18px;
  overflow-y: auto;
}

.support-vote-desc {
  margin: 0 0 14px;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.support-vote-deadline {
  margin: -6px 0 14px;
  font-size: 12px;
  color: var(--color-text-muted, var(--color-text-secondary));
}

.support-vote-options {
  display: grid;
  gap: 10px;
}

.support-vote-option {
  display: flex;
  flex-direction: column;
  gap: 6px;
  width: 100%;
  padding: 14px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-surface);
  color: var(--color-text);
  text-align: left;
  cursor: pointer;

  span {
    font-size: 13px;
    line-height: 1.55;
    color: var(--color-text-secondary);
  }

  &.selected {
    border-color: rgba(160, 96, 240, 0.65);
    background: color-mix(in srgb, #a060f0 10%, var(--color-surface));
  }
}

.support-vote-empty {
  display: flex;
  flex-direction: column;
  gap: 8px;
  padding: 18px;
  border: 1px dashed var(--color-border);
  border-radius: 12px;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-text) 4%, transparent);

  span {
    font-size: 13px;
    color: var(--color-text-secondary);
  }
}

.support-vote-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
  padding: 14px 18px 18px;
  border-top: 1px solid var(--color-border);
}

@media (max-width: 560px) {
  .support-vote-mask {
    align-items: flex-end;
    padding: 10px;
  }

  .support-vote-modal {
    max-height: 92vh;
    border-radius: 16px 16px 12px 12px;
  }

  .support-vote-actions {
    flex-direction: column-reverse;

    button {
      width: 100%;
    }
  }
}

/* ── 我的：最近动态时间线 ── */
.activity-list {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
}

.activity-item {
  padding: 10px 12px;
  border-bottom: 1px solid var(--color-border);

  &:last-child {
    border-bottom: none;
  }
}

.act-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.act-icon {
  flex-shrink: 0;
}

.act-title {
  font-weight: 600;
  color: var(--color-text);
}

.rarity-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 0 6px;
  border: 1px solid currentColor;
  border-radius: 10px;
}

.act-amount {
  font-weight: 700;
  font-variant-numeric: tabular-nums;

  &.pos {
    color: #3fb950;
  }

  &.neg {
    color: #dc3545;
  }
}

.act-desc {
  font-size: 0.85em;
  color: var(--color-text-secondary, var(--color-text));
  opacity: 0.85;
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.act-source-link,
.act-source-tag {
  flex: 0 1 auto;
  min-width: 0;
  max-width: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: var(--color-primary);
  font-size: 0.82em;
  font-weight: 600;
}

.act-source-link {
  padding: 2px 8px;
  border: 1px solid color-mix(in srgb, var(--color-primary) 38%, transparent);
  border-radius: 999px;
  background: color-mix(in srgb, var(--color-primary) 10%, transparent);
  cursor: pointer;
  font-family: inherit;
  text-align: left;

  &:hover {
    background: color-mix(in srgb, var(--color-primary) 17%, transparent);
  }

  &:focus-visible {
    outline: 2px solid var(--color-primary);
    outline-offset: 2px;
  }
}

.act-description-details {
  margin-top: 7px;
  color: var(--color-text-secondary, var(--color-text));
  font-size: 0.85em;

  summary {
    width: fit-content;
    color: var(--color-primary);
    cursor: pointer;
    user-select: none;
  }

  p {
    margin: 6px 0 0;
    line-height: 1.6;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
  }
}

.act-time {
  margin-left: auto;
  font-size: 0.78em;
  color: var(--color-text-secondary, var(--color-text));
  opacity: 0.7;
  flex-shrink: 0;
}

.act-badge {
  font-size: 0.78em;
  padding: 1px 8px;
  border-radius: 10px;
  border: 1px solid var(--color-border);
  color: var(--color-text);

  &.ok {
    color: #3fb950;
    border-color: rgba(63, 185, 80, 0.45);
    background: rgba(63, 185, 80, 0.12);
  }

  &.fail {
    color: #dc3545;
    border-color: rgba(220, 53, 69, 0.45);
    background: rgba(220, 53, 69, 0.12);
  }

  &.neutral {
    opacity: 0.75;
  }
}

.act-detail {
  margin-top: 6px;
  font-size: 0.85em;
  color: var(--color-text);
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
}

.act-chip {
  font-size: 0.92em;
  padding: 1px 8px;
  border-radius: 10px;

  &.healed {
    color: #3fb950;
    background: rgba(63, 185, 80, 0.12);
  }

  &.modified {
    color: #0d6efd;
    background: rgba(13, 110, 253, 0.1);
  }
}

.act-narrative {
  margin: 6px 0 0;
  font-size: 0.85em;
  line-height: 1.6;
  color: var(--color-text-secondary, var(--color-text));
  opacity: 0.9;
}

.act-ai-feedback {
  display: flex;
  justify-content: flex-end;
  margin-top: 7px;
}

.act-more {
  display: block;
  margin: 10px auto 0;
}

.empty-hint {
  padding: 32px 0;
  text-align: center;
  color: var(--color-text-secondary);

  p {
    margin: 0 0 12px;
    font-size: 14px;
  }
}

.archived-status-panel {
  padding: 28px 20px;
  text-align: center;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-surface, var(--color-bg));
}

.archived-icon {
  font-size: 36px;
  margin-bottom: 8px;
}

.archived-title {
  font-size: 16px;
  font-weight: 700;
  margin-bottom: 8px;
}

.archived-desc {
  margin: 0 0 14px;
  font-size: 13px;
  line-height: 1.6;
  color: var(--color-text-secondary);
}

.zone-lock-hint {
  margin: 16px 0 0;
  text-align: center;
  font-size: 13px;
  color: var(--color-text-secondary);
}
// ===== 小队招募板 · 蓝色主题（排版沿用 pq 体系，配色对齐旧版 primary 风格） =====
.party-page {
  position: relative;
}

.pq-party-page {
  --pq-parchment: color-mix(in srgb, var(--color-primary) 10%, var(--color-surface));
  --pq-parchment-dim: color-mix(in srgb, var(--color-primary) 18%, var(--color-border));
  --pq-gold: var(--color-primary);
  --pq-gold-bright: var(--color-primary);
  --pq-wood: color-mix(in srgb, var(--color-primary) 22%, var(--color-bg));
  --pq-wood-light: color-mix(in srgb, var(--color-primary) 12%, var(--color-surface));
  --pq-rift: color-mix(in srgb, var(--color-primary) 55%, #6366f1);
  --pq-rift-soft: color-mix(in srgb, var(--color-primary) 70%, #818cf8);
  --pq-ember: #fd7e14;
  // 间距 scale：统一所有 margin/padding/gap
  --pq-s1: 4px;
  --pq-s2: 8px;
  --pq-s3: 12px;
  --pq-s4: 16px;
  --pq-s5: 20px;
  --pq-s6: 28px;
  // 圆角归一
  --pq-r-card: 12px;
  --pq-r-board: 14px;
  --pq-r-hero: 16px;
  padding: var(--pq-s1) 2px var(--pq-s2);
}

// ── 浅色模式：沿用主题 primary，去掉暗色专用的文字阴影 ──
[data-theme="light"] .pq-party-page {
  .pq-notice-board {
    box-shadow: 0 12px 28px rgba(0, 0, 0, 0.08);

    .pq-board-text h3 {
      text-shadow: none;
    }
  }

  .pq-board-stats strong {
    text-shadow: none;
  }

  .pq-status-crest.st-disbanded {
    color: #4b5563;
  }

  .pq-scroll-info,
  .pq-pin-info,
  .pq-invite-info,
  .pq-rift-info,
  .pq-board-hint,
  .pq-flavor {
    color: var(--color-text-secondary);
  }

  .pq-board-stats span {
    color: var(--color-text-secondary);
  }

  .pq-solo-card {
    background: color-mix(in srgb, var(--color-primary) 6%, var(--color-bg));
  }

  .pq-create-card {
    background: color-mix(in srgb, var(--color-bg) 72%, transparent);
  }
}

// ── Subtabs · 胶囊切换 ──
.pq-subtabs {
  display: flex;
  justify-content: center;
  gap: var(--pq-s1);
  width: min(520px, 100%);
  margin: 0 auto var(--pq-s4);
  padding: 4px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 999px;

  button {
    position: relative;
    flex: 1;
    padding: var(--pq-s2) var(--pq-s3);
    font-size: 13px;
    font-weight: 600;
    letter-spacing: 0.02em;
    color: var(--color-text-secondary);
    background: transparent;
    border: 1px solid transparent;
    border-radius: 999px;
    cursor: pointer;
    transition: color 0.18s, background 0.18s, border-color 0.18s;

    &:hover {
      color: var(--color-primary);
      background: color-mix(in srgb, var(--color-primary) 8%, transparent);
    }

    &.active {
      color: var(--color-primary);
      background: color-mix(in srgb, var(--color-primary) 12%, var(--color-surface));
      border-color: color-mix(in srgb, var(--color-primary) 28%, transparent);
    }
  }
}

// ── 公告板 · 蓝色渐变顶栏 ──
.pq-notice-board {
  position: relative;
  display: flex;
  align-items: stretch;
  justify-content: space-between;
  gap: var(--pq-s4);
  margin-bottom: var(--pq-s5);
  padding: var(--pq-s6) var(--pq-s5);
  overflow: hidden;
  background:
    radial-gradient(circle at 12% 0%, color-mix(in srgb, var(--color-primary) 22%, transparent), transparent 34%),
    linear-gradient(135deg, color-mix(in srgb, var(--color-surface) 92%, transparent), var(--color-bg));
  border: 1px solid color-mix(in srgb, var(--color-primary) 24%, var(--color-border));
  border-radius: 18px;
  box-shadow: 0 18px 42px rgba(0, 0, 0, 0.12);

  &::after {
    content: '';
    position: absolute;
    right: -42px;
    bottom: -54px;
    width: 180px;
    height: 180px;
    background: radial-gradient(circle, color-mix(in srgb, var(--color-primary) 18%, transparent), transparent 66%);
    pointer-events: none;
  }

  .pq-board-text {
    position: relative;
    z-index: 1;
    flex: 1;
    min-width: 0;

    h3 {
      margin: 0 0 var(--pq-s2);
      font-size: 22px;
      letter-spacing: 0.01em;
      color: var(--color-text);
    }

    p {
      max-width: 760px;
      margin: 0;
      color: var(--color-text-secondary);
      font-size: 13px;
      line-height: 1.7;
    }
  }
}

.pq-kicker {
  margin: 0 0 6px !important;
  color: var(--pq-gold) !important;
  font-size: 11px !important;
  font-weight: 800;
  letter-spacing: 0.22em;
  text-transform: uppercase;
}

.pq-board-stats {
  position: relative;
  z-index: 1;
  display: flex;
  flex-wrap: wrap;
  gap: var(--pq-s2);
  min-width: 220px;

  span {
    flex: 1 1 120px;
    display: flex;
    flex-direction: row;
    align-items: baseline;
    gap: var(--pq-s2);
    min-height: auto;
    padding: var(--pq-s2) var(--pq-s3);
    color: var(--color-text-secondary);
    font-size: 12px;
    background: color-mix(in srgb, var(--color-bg) 72%, transparent);
    border: 1px solid color-mix(in srgb, var(--color-primary) 18%, var(--color-border));
    border-radius: 14px;
    backdrop-filter: blur(8px);
  }

  strong {
    margin-bottom: 0;
    color: var(--color-text);
    font-size: 24px;
    line-height: 1;
  }
}

// ── 标题 ──
.pq-section-title {
  letter-spacing: 0.01em;
}

// ── 我的小队 ──
.pq-my-party {
  position: relative;
  margin-bottom: var(--pq-s5);
  padding: var(--pq-s4);
  background: color-mix(in srgb, var(--color-surface) 82%, transparent);
  border: 1px solid var(--color-border);
  border-radius: 18px;
}

.party-section-title-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--pq-s2);
  margin-bottom: var(--pq-s3);

  .pq-section-title {
    margin-bottom: 0;
  }
}

.party-refresh-btn {
  flex: 0 0 auto;
  min-width: 64px;
}

.pq-party-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: var(--pq-s3);
}

.pq-grid-spaced {
  margin-top: 14px;
}

// ── 队伍卡片 ──
.pq-party-scroll {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: var(--pq-s3) var(--pq-s3) var(--pq-s2);
  overflow: hidden;
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--color-surface) 94%, #fff), var(--color-surface)),
    var(--color-surface);
  border: 1px solid color-mix(in srgb, var(--color-border) 82%, var(--color-primary));
  border-radius: 16px;
  box-shadow: 0 10px 26px rgba(0, 0, 0, 0.08);
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;

  &::before {
    content: '';
    position: absolute;
    inset: 0 0 auto;
    height: 3px;
    background: linear-gradient(90deg, var(--color-primary), transparent 70%);
    opacity: 0.65;
  }

  &::after {
    display: none;
  }

  &:hover {
    transform: translateY(-2px);
    border-color: color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
    box-shadow: 0 14px 34px rgba(0, 0, 0, 0.12);
  }
}

.pq-scroll-mine {
  border-color: color-mix(in srgb, var(--color-primary) 52%, var(--color-border));
  box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-primary) 22%, transparent), 0 12px 30px rgba(0, 0, 0, 0.1);
}

.pq-scroll-special {
  border-color: color-mix(in srgb, var(--pq-rift) 35%, var(--color-border));

  &::before {
    background: linear-gradient(90deg, var(--pq-rift), transparent 70%);
  }
}

.pq-scroll-browse {
  background:
    linear-gradient(180deg, color-mix(in srgb, var(--color-surface) 94%, #fff), var(--color-surface)),
    var(--color-surface);
}

.pq-scroll-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: var(--pq-s2);
  margin-bottom: var(--pq-s2);
}

.pq-scroll-name {
  min-width: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

// ── 副本名称 · 独立高对比行 ──
.pq-dungeon-row {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 6px 8px;
  margin-bottom: var(--pq-s2);
  padding: var(--pq-s2) var(--pq-s3);
  background: color-mix(in srgb, var(--color-primary) 16%, var(--color-bg));
  border: 1px solid color-mix(in srgb, var(--color-primary) 42%, var(--color-border));
  border-radius: 10px;
  box-shadow: inset 0 1px 0 color-mix(in srgb, #fff 6%, transparent);
}

.pq-dungeon-icon {
  flex: none;
  font-size: 14px;
  line-height: 1;
  opacity: 0.95;
}

.pq-dungeon-name {
  flex: 1 1 auto;
  min-width: 0;
  font-size: 15px;
  font-weight: 700;
  line-height: 1.35;
  color: var(--color-text);
  letter-spacing: 0.01em;
}

.pq-dungeon-level {
  flex: none;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-primary);
  white-space: nowrap;
}

.pq-scroll-info {
  display: flex;
  flex-wrap: wrap;
  gap: var(--pq-s2);
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: var(--pq-s2);

  > span {
    padding: var(--pq-s1) var(--pq-s2);
    background: color-mix(in srgb, var(--color-bg) 82%, transparent);
    border: 1px solid color-mix(in srgb, var(--color-border) 70%, transparent);
    border-radius: 999px;
  }

  &.pq-meta-info > span {
    color: var(--color-text);
    opacity: 0.9;
  }
}

// ── 状态徽章 · 图标 + 独立配色 ──
.pq-status-crest {
  flex: none;
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  font-weight: 600;
  padding: 4px 10px 4px 8px;
  border-radius: 999px;
  border: 1px solid color-mix(in srgb, var(--color-border) 80%, currentColor);
  color: var(--color-text-secondary);
  background: color-mix(in srgb, var(--color-bg) 80%, transparent);
  letter-spacing: 0.02em;

  &::before {
    font-size: 12px;
    line-height: 1;
  }

  &.st-forming {
    color: var(--color-primary);
    border-color: color-mix(in srgb, var(--color-primary) 55%, transparent);
    background: color-mix(in srgb, var(--color-primary) 12%, var(--color-bg));

    &::before { content: '🗡️'; }
  }

  &.st-queued {
    color: var(--pq-ember);
    border-color: var(--pq-ember);
    background: color-mix(in srgb, var(--pq-ember) 12%, var(--color-bg));

    &::before { content: '⏳'; }
  }

  &.st-matched {
    color: var(--pq-ember);
    border-color: var(--pq-ember);
    background: color-mix(in srgb, var(--pq-ember) 12%, var(--color-bg));

    &::before { content: '✅'; }
  }

  &.st-exploring {
    color: var(--pq-ember);
    border-color: var(--pq-ember);
    background: color-mix(in srgb, var(--pq-ember) 12%, var(--color-bg));

    &::before { content: '🗺️'; }
  }

  &.st-disbanded {
    color: #6b7280;
    border-color: color-mix(in srgb, #6b7280 50%, transparent);
    background: color-mix(in srgb, #6b7280 12%, var(--color-bg));

    &::before { content: '💀'; }
  }
}

// ── 冒险者徽章 ──
.pq-crests {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
}

.pq-member-crest {
  display: inline-flex;
  align-items: center;
  gap: var(--pq-s1);
  font-size: 12px;
  padding: 5px 8px;
  background: color-mix(in srgb, var(--color-bg) 86%, var(--color-primary));
  border: 1px solid color-mix(in srgb, var(--color-border) 78%, var(--color-primary));
  border-radius: 999px;
  cursor: pointer;
  color: var(--color-text);
  transition: border-color 0.15s, color 0.15s, transform 0.15s, background 0.15s;
  position: relative;
  overflow: visible;

  small {
    color: var(--color-text-secondary);
  }

  &:hover {
    border-color: var(--color-primary);
    color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 10%, var(--color-bg));
    transform: translateY(-1px);
  }

  &.member-voted {
    border-color: var(--color-success);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--color-success) 35%, transparent);
  }

  // 操作按钮浮出右上角，避免挤压 name
  .btn-leave-mini,
  .btn-admin-remove {
    position: absolute;
    top: -6px;
    right: -6px;
    z-index: 2;
    margin-left: 0;
  }
}
.pq-member-crest.title-mini--fate,
.pq-member-crest.title-mini--fate_companion{
  isolation: isolate;
  font-weight: 650;
}

.pq-member-crest.title-mini--fate::after {
  content: '';
  position: absolute;
  z-index: 1;
  pointer-events: none;
}

.pq-member-crest.title-mini--fate {
  border-color: color-mix(in srgb, #d9b96e 72%, #8b78c9);
  background:
    radial-gradient(circle at 82% 18%, rgba(217, 185, 110, 0.12), transparent 34%),
    linear-gradient(
      112deg,
      color-mix(in srgb, #8b78c9 8%, var(--color-bg)),
      color-mix(in srgb, #d9b96e 13%, var(--color-bg)) 48%,
      color-mix(in srgb, #8b78c9 11%, var(--color-bg))
    );
  background-size: 100% 100%, 210% 100%;
  box-shadow:
    inset 0 0 0 1px rgba(139, 120, 201, 0.12),
    0 0 8px rgba(217, 185, 110, 0.18);
  animation: pq-fate-crest-flow 6.4s ease-in-out infinite;

  &::before {
    content: '✦';
    flex: 0 0 auto;
    color: #e8c979;
    font-size: 10px;
    line-height: 1;
    text-shadow: 0 0 5px rgba(217, 185, 110, 0.72);
    animation: pq-fate-sigil 2.9s ease-in-out infinite;
  }

  &::after {
    inset: -3px -5px;
    border: 1px solid rgba(217, 185, 110, 0.74);
    border-right-color: rgba(139, 120, 201, 0.68);
    border-bottom-color: rgba(139, 120, 201, 0.42);
    border-radius: 54% 46% 52% 48% / 62% 46% 54% 38%;
    outline: 1px solid rgba(139, 120, 201, 0.16);
    outline-offset: 1px;
    box-shadow:
      -2px -1px 6px rgba(217, 185, 110, 0.2),
      3px 1px 7px rgba(139, 120, 201, 0.18);
    transform: rotate(-0.8deg);
    animation: pq-fate-mini-frame 4.6s ease-in-out infinite;
  }
}

.pq-member-crest.title-mini--fate_companion {
  border-color: color-mix(in srgb, #54d3b1 72%, #f0c25c);
  background:
    radial-gradient(circle at 15% 18%, rgba(84, 211, 177, 0.13), transparent 35%),
    linear-gradient(
      112deg,
      color-mix(in srgb, #2a947d 9%, var(--color-bg)),
      color-mix(in srgb, #f0c25c 12%, var(--color-bg)) 50%,
      color-mix(in srgb, #2a947d 10%, var(--color-bg))
    );
  box-shadow:
    inset 0 0 0 1px rgba(240, 194, 92, 0.12),
    0 0 8px rgba(84, 211, 177, 0.2);

  &::before {
    content: '✧';
    flex: 0 0 auto;
    color: #f0c25c;
    font-size: 11px;
    line-height: 1;
    text-shadow: 0 0 5px rgba(84, 211, 177, 0.8);
    animation: pq-fate-sigil 2.9s ease-in-out infinite;
  }
}

[data-theme="light"] .pq-member-crest.title-mini--fate {
  border-color: color-mix(in srgb, #9a6905 72%, #5d3a9a);
  box-shadow:
    inset 0 0 0 1px rgba(93, 58, 154, 0.1),
    0 0 7px rgba(151, 101, 0, 0.14);

  &::before {
    color: #7c5200;
    text-shadow: 0 0 3px rgba(151, 101, 0, 0.4);
  }

  &::after {
    border-color: rgba(154, 105, 5, 0.66);
    border-right-color: rgba(93, 58, 154, 0.58);
    border-bottom-color: rgba(93, 58, 154, 0.38);
    outline-color: rgba(93, 58, 154, 0.14);
    box-shadow:
      -2px -1px 4px rgba(151, 101, 0, 0.13),
      3px 1px 5px rgba(93, 58, 154, 0.12);
  }
}

.pq-member-crest.member-voted {
  border-color: var(--color-success);
}

@keyframes pq-fate-crest-flow {
  50% { background-position: 0 0, 100% 0; }
}

@keyframes pq-fate-sigil {
  50% { transform: rotate(45deg) scale(1.22); opacity: 0.78; }
}

@keyframes pq-fate-mini-frame {
  50% {
    border-top-color: rgba(139, 120, 201, 0.76);
    border-right-color: rgba(217, 185, 110, 0.82);
    outline-color: rgba(217, 185, 110, 0.24);
    filter: brightness(1.14);
  }
}

.vote-mark {
  color: var(--color-success);
  font-weight: 700;
}

// ── 描述 / 提示 / 出发 ──
.pq-flavor {
  box-sizing: border-box;
  min-width: 0;
  max-width: 100%;
  margin: 6px 0 8px;
  font-size: 12px;
  line-height: 1.55;
  color: var(--color-text);
  opacity: 0.85;
  font-style: italic;
  overflow-wrap: anywhere;
  word-break: break-word;
  border-left: 2px solid color-mix(in srgb, var(--color-primary) 35%, transparent);
  padding-left: 8px;
}

.pq-hint {
  display: block;
  margin-top: 4px;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.45;
}

.pq-error {
  color: var(--color-danger);
  font-weight: 600;
}

.pq-departure {
  color: var(--color-primary);
  font-weight: 600;
}

.pq-vote-progress {
  color: var(--color-text);
  font-size: 12px;
  font-weight: 600;
}

// ── 空状态 ──
.pq-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
  margin: 18px auto;
  padding: 22px 16px;
  max-width: 420px;
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 13px;
  background: color-mix(in srgb, var(--color-surface) 70%, transparent);
  border: 1px dashed color-mix(in srgb, var(--color-primary) 22%, var(--color-border));
  border-radius: 14px;

  .pq-empty-icon {
    font-size: 30px;
    line-height: 1;
    filter: grayscale(0.2) drop-shadow(0 2px 6px rgba(0, 0, 0, 0.3));
  }

  .pq-empty-text {
    margin: 4px 0 0;
    color: var(--color-text);
    font-size: 14px;
  }

  .pq-empty-sub {
    margin: 0;
    font-size: 12px;
    opacity: 0.85;
  }
}

.pq-empty-inline {
  flex-direction: row;
  justify-content: center;
  margin: 10px auto;
  padding: 12px;
  max-width: none;

  .pq-empty-icon {
    font-size: 18px;
  }
}

// ── 操作区 ──
.pq-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding-top: 10px;
  border-top: 1px dashed color-mix(in srgb, var(--color-border) 78%, transparent);
}

.pq-admin-actions {
  border-top: none;
  padding-top: 4px;
  margin-top: 4px;
}

// ── 匹配板 / 秘境卷轴 ──
.pq-match-board,
.pq-special-rift {
  position: relative;
  margin: 16px 0;
  padding: 16px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  border-radius: 18px;
}

.pq-board-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.6;
}

.pq-action-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  align-items: stretch;
  gap: var(--pq-s3);
  margin: var(--pq-s3) 0;
}

.pq-field {
  display: flex;
  flex-direction: column;
  gap: 6px;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 700;
  letter-spacing: 0.02em;

  select,
  textarea {
    width: 100%;
    padding: 9px 10px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 10px;
    outline: none;
    transition: border-color 0.15s, box-shadow 0.15s;

    &:focus {
      border-color: var(--color-primary);
      box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary) 14%, transparent);
    }
  }

  textarea {
    min-height: 88px;
    resize: vertical;
  }
}

.pq-solo-card {
  display: flex;
  flex-direction: column;
  gap: var(--pq-s2);
  padding: var(--pq-s3);
  background: color-mix(in srgb, var(--color-bg) 72%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-primary) 16%, var(--color-border));
  border-radius: var(--pq-r-card);
}

.pq-dungeon-pool {
  min-width: 0;
  margin: 0;
  padding: 0;
  border: 0;

  legend { padding: 0; }
}

.pq-dungeon-pool-hint {
  margin: 2px 0 8px;
  color: var(--color-text-secondary);
  font-size: 12px;
  font-weight: 500;
}

.pq-queue-compass {
  min-width: 0;
  margin: 12px 0 14px;
  padding: 14px;
  background:
    radial-gradient(circle at 92% 8%, color-mix(in srgb, var(--color-primary) 15%, transparent), transparent 42%),
    color-mix(in srgb, var(--color-bg) 86%, var(--color-surface, #17151d));
  border: 1px solid color-mix(in srgb, var(--color-primary) 25%, var(--color-border));
  border-radius: 14px;
  box-shadow: inset 0 1px 0 color-mix(in srgb, white 7%, transparent);
}

.pq-queue-compass-head {
  position: relative;
  display: flex;
  align-items: end;
  justify-content: space-between;
  gap: 12px;
  padding-right: 24px;
  cursor: pointer;
  list-style: none;

  &::-webkit-details-marker { display: none; }

  &::after {
    position: absolute;
    top: 50%;
    right: 2px;
    color: var(--color-text-secondary);
    content: '▾';
    transform: translateY(-50%) rotate(-90deg);
    transition: transform .18s ease;
  }

  &:focus-visible {
    outline: 2px solid color-mix(in srgb, var(--color-primary) 70%, transparent);
    outline-offset: 4px;
    border-radius: 6px;
  }

  h4 { margin: 1px 0 0; color: var(--color-text); font-size: 16px; }
  p {
    max-width: 520px;
    margin: 0;
    color: var(--color-text-secondary);
    font-size: 11px;
    line-height: 1.5;
    text-align: right;
  }
}

.pq-queue-compass[open] {
  .pq-queue-compass-head {
    margin-bottom: 10px;

    &::after { transform: translateY(-50%); }
  }
}

.pq-queue-compass-kicker {
  color: color-mix(in srgb, var(--color-primary) 78%, var(--color-text));
  font-size: 9px;
  font-weight: 800;
  letter-spacing: .16em;
}

.pq-queue-compass-grid {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px;
}

.pq-queue-compass-card {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 9px;
  min-width: 0;
  padding: 9px 11px;
  background: color-mix(in srgb, var(--color-surface, var(--color-bg)) 90%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-primary) 18%, var(--color-border));
  border-radius: 10px;

  > strong {
    color: var(--color-primary);
    font-size: 20px;
    font-variant-numeric: tabular-nums;
    line-height: 1;
    white-space: nowrap;

    small { margin-left: 2px; font-size: 10px; font-weight: 700; }
  }

  &.is-quiet { opacity: .66; }
}

.pq-queue-compass-card--random {
  background: linear-gradient(135deg, color-mix(in srgb, var(--color-primary) 12%, var(--color-bg)), var(--color-bg));
  border-style: dashed;
}

.pq-queue-compass-icon { font-size: 18px; }

.pq-queue-compass-name {
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--color-text);
  font-size: 13px;
  font-weight: 700;

  small {
    display: block;
    margin-top: 1px;
    color: var(--color-text-secondary);
    font-size: 10px;
    font-weight: 500;
  }
}

.pq-dungeon-mode-options {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 5px;

  label {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 7px 10px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 9px;
    cursor: pointer;
  }
}

.pq-dungeon-pool-options {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 7px;
  max-height: 230px;
  overflow-y: auto;
}

.pq-dungeon-pool-option {
  display: flex;
  align-items: flex-start;
  gap: 7px;
  min-width: 0;
  padding: 8px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 9px;
  cursor: pointer;

  input { flex: 0 0 auto; margin-top: 2px; }
  span, small { display: block; min-width: 0; overflow-wrap: anywhere; }
  small { margin-top: 2px; color: var(--color-text-secondary); font-size: 11px; }
}

@media (max-width: 640px) {
  .pq-queue-compass { padding: 12px; }
  .pq-queue-compass-head {
    align-items: flex-start;
    flex-direction: column;
    gap: 5px;

    p { text-align: left; }
  }
  .pq-queue-compass-grid { grid-template-columns: minmax(0, 1fr); }
  .pq-queue-compass-card { padding: 10px; }
  .pq-dungeon-pool-options { grid-template-columns: minmax(0, 1fr); }
}

.pq-create-card {
  display: flex;
  flex-direction: column;
  gap: var(--pq-s2);
  padding: var(--pq-s3);
  background: color-mix(in srgb, var(--color-bg) 72%, transparent);
  border: 1px dashed color-mix(in srgb, var(--color-primary) 28%, var(--color-border));
  border-radius: var(--pq-r-card);

  > .btn-secondary {
    align-self: flex-end;
  }
}

.pq-solo-action {
  margin-top: 0;
  padding-top: 0;
  border-top: none;
}

.pq-invite-panel {
  margin-top: 10px;
  padding: 10px;
  background: color-mix(in srgb, var(--color-bg) 78%, transparent);
  border: 1px dashed var(--color-border);
  border-radius: 12px;
}

.pq-invite-row {
  display: flex;
  gap: 8px;

  input {
    flex: 1;
    min-width: 0;
    padding: 8px 10px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 10px;
    outline: none;

    &:focus {
      border-color: var(--color-primary);
    }
  }
}

.pq-candidates {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}

.pq-candidate {
  font-size: 12px;
}

// ── 招募市场容器 ──
.pq-market {
  display: flex;
  flex-direction: column;
  gap: var(--pq-s3);
  margin-top: var(--pq-s3);
}

// ── 公开小队板 / 邀请板（左侧色带区分） ──
.pq-ticket-board {
  margin: 0;
  padding: var(--pq-s3);
  background: color-mix(in srgb, var(--color-surface) 80%, transparent);
  border: 1px solid color-mix(in srgb, var(--color-primary) 16%, var(--color-border));
  border-left: 3px solid var(--color-primary);
  border-radius: var(--pq-r-board);
}

.pq-invites-board {
  margin: 0;
  padding: var(--pq-s3);
  background: color-mix(in srgb, var(--color-surface) 80%, transparent);
  border: 1px solid color-mix(in srgb, var(--pq-rift) 22%, var(--color-border));
  border-left: 3px solid var(--pq-rift);
  border-radius: var(--pq-r-board);
}

.pq-board-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-bottom: 12px;

  h4 {
    margin: 0;
    color: var(--color-text);
    font-size: 15px;
    letter-spacing: 0.02em;
  }

  span {
    flex: none;
    color: var(--color-text-secondary);
    font-size: 12px;
  }
}

.pq-code-row {
  margin-bottom: 12px;
}

// ── 公开小队 · 便条贴板 ──
.pq-pin-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--pq-s3);
}

.pq-pin-note {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: var(--pq-s3);
  background: var(--color-surface);
  border: 1px solid color-mix(in srgb, var(--color-border) 82%, var(--color-primary));
  border-left: 3px solid var(--color-primary);
  border-radius: 10px;
  box-shadow: 0 6px 16px rgba(0, 0, 0, 0.08);
  transform: none;
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;

  &:hover {
    transform: translateY(-2px);
    border-color: color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.12);
  }
}

.pq-pin-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin: 4px 0 10px;
}

.pq-pin-name {
  min-width: 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pq-pin-info {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 10px;

  > span {
    padding: 4px 8px;
    background: color-mix(in srgb, var(--color-bg) 82%, transparent);
    border: 1px solid color-mix(in srgb, var(--color-border) 70%, transparent);
    border-radius: 999px;
  }

  &.pq-meta-info > span {
    color: var(--color-text);
    opacity: 0.9;
  }
}

// ── 邀请信 · 蜡封 ──
.pq-invite-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(260px, 1fr));
  gap: var(--pq-s3);
}

.pq-invite-letter {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 16px 14px 12px;
  background: var(--color-surface);
  border: 1px solid color-mix(in srgb, var(--pq-rift) 24%, var(--color-border));
  border-radius: 10px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.08);
  transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;

  &:hover {
    transform: translateY(-2px);
    border-color: color-mix(in srgb, var(--pq-rift) 45%, var(--color-border));
    box-shadow: 0 10px 26px rgba(0, 0, 0, 0.12);
  }
}

.pq-invite-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}

.pq-invite-name {
  min-width: 0;
  font-size: 14px;
  font-weight: 700;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pq-invite-info {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 10px;

  > span {
    padding: 4px 8px;
    background: color-mix(in srgb, var(--pq-rift) 10%, var(--color-bg));
    border: 1px solid color-mix(in srgb, var(--pq-rift) 22%, var(--color-border));
    border-radius: 999px;
  }

  &.pq-meta-info > span {
    color: var(--color-text);
    opacity: 0.9;
  }
}

// ── 秘境卷轴卡 ──
.pq-rift-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
  gap: 12px;
}

.pq-rift-card {
  position: relative;
  display: flex;
  flex-direction: column;
  min-width: 0;
  padding: 14px;
  background: linear-gradient(160deg, color-mix(in srgb, var(--color-primary) 8%, var(--color-surface)), var(--color-surface));
  border: 1px solid color-mix(in srgb, var(--color-primary) 22%, var(--color-border));
  border-radius: 12px;
  box-shadow: 0 6px 18px rgba(0, 0, 0, 0.08);
  transition: transform 0.16s ease, border-color 0.16s ease, box-shadow 0.16s ease;

  &::before {
    content: '';
    position: absolute;
    top: 0;
    left: 0;
    right: 0;
    height: 3px;
    background: linear-gradient(90deg, var(--color-primary), transparent 70%);
    opacity: 0.65;
  }

  &:hover {
    transform: translateY(-2px);
    border-color: color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
    box-shadow: 0 10px 24px rgba(0, 0, 0, 0.12);
  }
}

.pq-rift-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 10px;
}

.pq-rift-name {
  min-width: 0;
  font-size: 15px;
  font-weight: 700;
  color: var(--color-text);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.pq-rift-info {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  font-size: 12px;
  color: var(--color-text-secondary);
  margin-bottom: 10px;

  > span {
    padding: 4px 8px;
    background: color-mix(in srgb, var(--color-primary) 8%, var(--color-bg));
    border: 1px solid color-mix(in srgb, var(--color-primary) 18%, var(--color-border));
    border-radius: 999px;
  }
}

.pq-filter-bar {
  margin: 12px 0;

  select {
    padding: 8px 10px;
    color: var(--color-text);
    background: var(--color-bg);
    border: 1px solid var(--color-border);
    border-radius: 10px;
    outline: none;
    cursor: pointer;

    &:focus {
      border-color: var(--color-primary);
    }
  }
}

.pq-browse-title {
  margin-top: 12px;
}

// ── 响应式 ──
@media (max-width: 860px) {
  .pq-action-grid {
    grid-template-columns: 1fr;
  }

  .pq-notice-board {
    flex-direction: column;

    .pq-board-stats {
      width: 100%;
    }
  }
}

@media (max-width: 560px) {
  .pq-party-grid,
  .pq-pin-grid,
  .pq-invite-grid,
  .pq-rift-grid {
    grid-template-columns: 1fr;
  }

  .pq-board-stats span {
    flex: 1 1 100%;
  }
}

// ── 保留 · 小队成员操作按钮（模板仍引用） ──
.btn-leave-mini {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 16px;
  height: 16px;
  margin-left: 2px;
  padding: 0;
  font-size: 12px;
  line-height: 1;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 50%;
  cursor: pointer;
  transition: color 0.15s, border-color 0.15s, background 0.15s;

  &:hover:not(:disabled) {
    color: var(--color-danger);
    border-color: var(--color-danger);
    background: color-mix(in srgb, var(--color-danger) 8%, transparent);
  }

  &:disabled {
    opacity: 0.4;
    cursor: not-allowed;
  }
}

.btn-admin-remove {
  color: var(--color-text-secondary);
  border-color: var(--color-warning, #e6a23c);

  &:hover:not(:disabled) {
    color: var(--color-warning, #e6a23c);
    border-color: var(--color-warning, #e6a23c);
    background: color-mix(in srgb, var(--color-warning, #e6a23c) 8%, transparent);
  }
}

.btn-warning {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  color: #856404;
  background: #fff3cd;
  border: 1px solid #ffc107;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;

  &:hover:not(:disabled) {
    background: #ffe69c;
  }
  &:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
}

.btn-danger {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  background: #dc3545;
  border: 1px solid #dc3545;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;

  &:hover:not(:disabled) {
    background: #bb2d3b;
  }
  &:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
}

.btn-gm {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  color: #fff;
  background: #6f42c1;
  border: 1px solid #6f42c1;
  border-radius: 6px;
  cursor: pointer;
  transition: background 0.15s;

  &:hover:not(:disabled) {
    background: #5a32a3;
  }
  &:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
}

.btn-secondary {
  padding: 4px 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
  transition: border-color 0.15s, color 0.15s;

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
    color: var(--color-primary);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

/* 与 .btn-primary.btn-sm 对齐：避免 .btn-secondary 默认更小 padding 覆盖 btn-sm */
.btn-secondary.btn-sm {
  padding: 6px 16px;
  font-size: 13px;
}

.join-roster {
  margin-top: 8px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.roster-empty {
  font-size: 12px;
  color: var(--color-text-secondary);
  padding: 6px 8px;
  background: var(--color-bg);
  border-radius: 6px;
}

.roster-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 6px 10px;
  font-size: 12px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
  text-align: left;
  transition: border-color 0.15s, background 0.15s;

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
    background: color-mix(in srgb, var(--color-primary) 6%, var(--color-bg));
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.roster-name {
  font-weight: 600;
}

.roster-meta {
  color: var(--color-text-secondary);
}

/* ── 治疗所/铁匠铺 AI 结果弹窗 ── */
.ai-result-mask {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1100; /* 高于建筑详情弹窗(1000)，结果推送时可叠加展示 */
}
.ai-result-box {
  background: var(--color-bg, #1a1a2e);
  border: 1px solid var(--color-border, #333);
  color: var(--color-text, #ddd);
  border-radius: 12px;
  padding: 20px;
  max-width: 440px;
  width: 90%;
  max-height: 80vh;
  overflow-y: auto;
}
.ai-result-title {
  margin: 0 0 12px;
  font-size: 1.1em;
  display: flex;
  align-items: baseline;
  gap: 8px;
}
.ai-result-adv {
  font-size: 0.8em;
  font-weight: 400;
  color: var(--color-text-secondary, #888);
}
.ai-result-status {
  font-size: 0.9em;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--color-surface, var(--color-bg, #1a1a2e));
  border: 1px solid var(--color-border, #333);
  color: var(--color-text, #ddd);
  margin-bottom: 8px;
}
.ai-result-status.ok {
  color: var(--color-text, #ddd);
  background: color-mix(in srgb, var(--color-success, #3fb950) 10%, var(--color-surface, var(--color-bg, #1a1a2e)));
  border-color: color-mix(in srgb, var(--color-success, #3fb950) 45%, var(--color-border, #333));
  border-left: 4px solid var(--color-success, #3fb950);
}
.ai-result-status.fail {
  color: var(--color-text, #ddd);
  background: color-mix(in srgb, var(--color-danger, #f85149) 10%, var(--color-surface, var(--color-bg, #1a1a2e)));
  border-color: color-mix(in srgb, var(--color-danger, #f85149) 45%, var(--color-border, #333));
  border-left: 4px solid var(--color-danger, #f85149);
}
.ai-result-modified {
  font-size: 0.9em;
  padding: 8px 10px;
  border-radius: 6px;
  background: var(--color-surface, var(--color-bg, #1a1a2e));
  border: 1px solid var(--color-border, #333);
  color: var(--color-text, #ddd);
  margin-bottom: 8px;
}
.ai-result-modified-desc {
  margin-top: 4px;
  font-size: 0.85em;
  color: var(--color-text-secondary, #888);
  line-height: 1.5;
}
.ai-result-narrative {
  font-size: 0.88em;
  line-height: 1.6;
  color: var(--color-text, #ddd);
  background: var(--color-surface, var(--color-bg, #1a1a2e));
  border: 1px solid var(--color-border, #333);
  border-radius: 6px;
  padding: 10px;
  margin: 8px 0 0;
}
.ai-result-actions {
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 14px;
}
.ai-result-close {
  /* 跟随主题变量；--color-bg-secondary 未定义时旧的 #16213e 回退在亮色主题下是黑块 */
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  color: var(--color-text);
  padding: 7px 18px;
  border-radius: 6px;
  cursor: pointer;
}
.ai-result-close:hover {
  border-color: var(--color-primary, #0d6efd);
}

/* ── 移动端适配（统一 640px 断点） ── */
@media (max-width: 640px) {
  .advs1 {
    padding: 12px 16px 20px;
  }

  .advs1-title {
    font-size: 19px;
  }

  .notice-banner {
    flex-direction: column;
    gap: 10px;
    padding: 10px 12px;
  }

  .notice-banner-body {
    font-size: 12px;
  }

  .notice-banner-btn {
    align-self: stretch;
    width: 100%;
    min-height: 44px;
  }

  .admin-notice-input {
    font-size: 16px;
  }

  .tab-btn {
    padding: 8px 12px;
    font-size: 13px;
  }

  /* 吧台 3 列在窄屏太挤，降为 2 列（覆盖 1000px 断点的 3 列） */
  .bar-stools {
    grid-template-columns: repeat(2, 1fr);
  }

  /* 酒馆纵向堆叠后聊天区压缩高度，避免一屏全是聊天 */
  .chat-messages {
    min-height: 200px;
    max-height: 320px;
  }

  /* iOS Safari 表单控件字号 <16px 聚焦时会强制放大页面 */
  .chat-input,
  .log-search-input,
  .log-status-select,
  .filter-bar select,
  .filter-bar .search-input,
  .matchmaking-field select,
  .matchmaking-field textarea,
  .matchmaking-invite-row input {
    font-size: 16px;
  }

  .party-board-hero {
    flex-direction: column;
    gap: 12px;
    padding: 14px;

    h3 {
      font-size: 18px;
    }
  }

  .party-hero-stats {
    min-width: 0;

    span {
      min-height: 60px;
      padding: 10px;
    }

    strong {
      font-size: 20px;
    }
  }

  .my-party-section,
  .matchmaking-section,
  .special-dungeon-section,
  .matchmaking-tickets {
    padding: 12px;
    border-radius: 14px;
  }

  .party-list,
  .special-dungeon-list {
    grid-template-columns: 1fr;
  }

  .matchmaking-form,
  .matchmaking-create-box {
    grid-template-columns: 1fr;
  }

  .matchmaking-solo-action .btn-primary,
  .matchmaking-create-box .btn-secondary,
  .matchmaking-invite-row .btn-secondary {
    width: 100%;
  }

  .matchmaking-invite-row,
  .party-section-head {
    align-items: stretch;
    flex-direction: column;
  }

  .dbp-mat-amount,
  .dbp-settings-input {
    width: 84px;
    font-size: 16px;
  }

  /* 建筑分区入口：固定 2 列紧凑排布 */
  .building-grid {
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
  }

  .building-grid--zones > .building-card {
    padding: 14px 8px;
  }

  /* 筛选栏：下拉框均分一行，搜索框独占一行 */
  .filter-bar {
    select {
      flex: 1;
      min-width: 0;
    }

    .search-input {
      flex: 1 1 100%;
    }
  }

  .dorm-header {
    flex-wrap: wrap;
    row-gap: 8px;
  }

  .dbp-row {
    gap: 10px;
  }

  .section-card {
    padding: 12px;
  }

  .ai-result-mask {
    padding: 12px;
  }

  .ai-result-box {
    width: 100%;
    padding: 16px;
    max-height: 86vh;
  }
}

.act-undo-btn {
  padding: 2px 8px;
  border: 1px solid rgba(220, 53, 69, 0.5);
  border-radius: 10px;
  color: #dc3545;
  background: rgba(220, 53, 69, 0.08);
  font-size: 0.78em;
  cursor: pointer;

  &:hover:not(:disabled) {
    background: rgba(220, 53, 69, 0.16);
  }

  &:disabled {
    cursor: wait;
    opacity: 0.6;
  }
}

.dorm-room.is-mine {
  border-color: #4f8bc9;
  border-color: color-mix(in srgb, var(--color-primary, #4f8bc9) 68%, var(--color-border, #d8dee8));
  box-shadow: 0 5px 16px rgba(65, 122, 181, 0.12);
}

.dorm-room.has-followed {
  border-color: #9b7ac7;
  border-color: color-mix(in srgb, #9b7ac7 58%, var(--color-border, #d8dee8));
}

@media (max-width: 360px) {
  .advs1 {
    padding-inline: 12px;
  }
}

@media (max-width: 640px) {
  .advs1-header { min-height: 0; padding: 18px; flex-wrap: wrap; }
  .advs1-title { font-size: 21px; }
  .advs1-beacon { display: none; }
}

/* 小队长列表在触屏设备滚动时，动态背景与滤镜会让每个称号徽章持续重绘。 */
@media (max-width: 640px), (hover: none) and (pointer: coarse) {
  .pq-member-crest.title-mini--fate,
  .pq-member-crest.title-mini--fate_companion,
  .pq-member-crest.title-mini--fate::before,
  .pq-member-crest.title-mini--fate_companion::before,
  .pq-member-crest.title-mini--fate::after { animation: none; }
}

@media (prefers-reduced-motion: reduce) {
  .advs1-beacon::before,
  .advs1-beacon::after,
  .advs1-beacon span,
  .pq-member-crest.title-mini--fate,
  .pq-member-crest.title-mini--fate_companion,
  .pq-member-crest.title-mini--fate::before,
  .pq-member-crest.title-mini--fate_companion::before,
  .pq-member-crest.title-mini--fate::after { animation: none; }
}

/* 宿舍区：驻地生活区与等级辨识。 */
.dorm-build-panel {
  border: 1px solid color-mix(in srgb, #5d87b3 22%, var(--color-border));
  border-radius: 15px;
  background: linear-gradient(120deg, color-mix(in srgb, #5d87b3 6%, var(--color-surface)), var(--color-surface));
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent);
}
.dorm-room {
  --room-blue: color-mix(in srgb, #5d87b3 72%, var(--color-primary));
  position: relative;
  overflow: hidden;
  border-radius: 14px;
  border-color: color-mix(in srgb, var(--room-blue) 18%, var(--color-border));
  background: linear-gradient(145deg, color-mix(in srgb, var(--room-blue) 5%, var(--color-surface)), var(--color-surface));
  transition: transform 0.2s ease, border-color 0.2s ease, box-shadow 0.2s ease;
}
.dorm-room::before {
  content: '';
  position: absolute;
  top: 0;
  left: 14px;
  width: 34px;
  height: 2px;
  background: var(--room-blue);
}
.dorm-room:hover {
  border-color: color-mix(in srgb, var(--room-blue) 56%, var(--color-border));
  transform: translateY(-3px);
  box-shadow: 0 10px 24px rgba(0, 0, 0, 0.12);
}
.dorm-room-lv-1 .room-icon { opacity: 0.8; }
.dorm-room-lv-2 {
  border-color: color-mix(in srgb, #76a9d5 48%, var(--color-border));
  background:
    radial-gradient(circle at 100% 0%, color-mix(in srgb, #8bb9df 15%, transparent), transparent 38%),
    linear-gradient(145deg, color-mix(in srgb, #6e9fc9 10%, var(--color-surface)), var(--color-surface));
  box-shadow: inset 0 0 0 1px color-mix(in srgb, #8bb9df 10%, transparent);
}
.dorm-room-lv-2::before {
  width: calc(100% - 28px);
  background: linear-gradient(90deg, transparent, #8fc0e6, transparent);
  box-shadow: 0 0 10px color-mix(in srgb, #8fc0e6 42%, transparent);
}
.dorm-room-lv-2 .room-icon {
  transform: scale(1.12);
  filter: drop-shadow(0 4px 7px color-mix(in srgb, #78a9d2 30%, transparent));
}
.dorm-room-lv-2 .level-badge {
  border: 1px solid color-mix(in srgb, #83b2d8 48%, var(--color-border));
  background: color-mix(in srgb, #739fc5 14%, var(--color-bg));
  color: color-mix(in srgb, #6e9bc2 82%, var(--color-text));
}

@media (max-width: 640px) {
  .dorm-room:hover { transform: none; box-shadow: none; }
  .dorm-room:active { transform: scale(0.985); }
  .dbp-row { align-items: stretch; }
  .dbp-materials,
  .dbp-concurrent { min-width: calc(50% - 6px); }
}

/* S1 玩家界面控件基线：统一主题、焦点反馈与移动端触控尺寸。 */
.advs1 {
  :deep(input:not([type='checkbox']):not([type='radio']):not([type='range']):not([type='file'])),
  :deep(select),
  :deep(textarea) {
    appearance: none;
    -webkit-appearance: none;
    min-height: 36px;
    box-sizing: border-box;
    border: 1px solid var(--color-border);
    border-radius: 9px;
    background: var(--color-bg);
    background: linear-gradient(
      180deg,
      color-mix(in srgb, var(--color-surface) 54%, var(--color-bg)),
      var(--color-bg)
    );
    color: var(--color-text);
    font: inherit;
    line-height: 1.35;
    box-shadow: inset 0 1px 2px rgba(0, 0, 0, 0.06);
    transition: border-color 0.16s ease, box-shadow 0.16s ease, background 0.16s ease;
  }

  :deep(input:not([type='checkbox']):not([type='radio']):not([type='range']):not([type='file'])),
  :deep(select) {
    padding: 7px 10px;
  }

  :deep(textarea) {
    min-height: 76px;
    padding: 9px 11px;
    resize: vertical;
  }

  :deep(select) {
    padding-right: 30px;
    background-image:
      linear-gradient(45deg, transparent 50%, var(--color-text-secondary) 50%),
      linear-gradient(135deg, var(--color-text-secondary) 50%, transparent 50%),
      linear-gradient(180deg, color-mix(in srgb, var(--color-surface) 54%, var(--color-bg)), var(--color-bg));
    background-position:
      calc(100% - 15px) 50%,
      calc(100% - 10px) 50%,
      0 0;
    background-size: 5px 5px, 5px 5px, 100% 100%;
    background-repeat: no-repeat;
  }

  :deep(input::placeholder),
  :deep(textarea::placeholder) {
    color: var(--color-text-secondary);
    opacity: 0.72;
  }

  :deep(input:not([type='checkbox']):not([type='radio']):not([type='range']):not([type='file']):hover:not(:disabled)),
  :deep(select:hover:not(:disabled)),
  :deep(textarea:hover:not(:disabled)) {
    border-color: color-mix(in srgb, var(--color-primary) 42%, var(--color-border));
  }

  :deep(input:not([type='checkbox']):not([type='radio']):not([type='range']):not([type='file']):focus-visible),
  :deep(select:focus-visible),
  :deep(textarea:focus-visible) {
    outline: none;
    border-color: var(--color-primary);
    background-color: var(--color-bg);
    box-shadow: 0 0 0 3px color-mix(in srgb, var(--color-primary) 16%, transparent);
  }

  :deep(input:disabled),
  :deep(select:disabled),
  :deep(textarea:disabled) {
    cursor: not-allowed;
    opacity: 0.52;
  }

  :deep(.btn-primary),
  :deep(.btn-secondary),
  :deep(.btn-warning),
  :deep(.btn-danger),
  :deep(.advs1-submit) {
    appearance: none;
    -webkit-appearance: none;
    display: inline-flex;
    min-height: 36px;
    align-items: center;
    justify-content: center;
    gap: 6px;
    padding: 7px 13px;
    border: 1px solid var(--color-border);
    border-radius: 9px;
    font: inherit;
    font-size: 12px;
    font-weight: 700;
    line-height: 1.15;
    text-align: center;
    text-decoration: none;
    cursor: pointer;
    box-shadow: 0 3px 9px rgba(0, 0, 0, 0.1);
    transition: transform 0.16s ease, border-color 0.16s ease, background 0.16s ease, box-shadow 0.16s ease;
  }

  :deep(.btn-primary),
  :deep(.advs1-submit) {
    border-color: var(--color-primary);
    background: var(--color-primary);
    background: linear-gradient(135deg, color-mix(in srgb, var(--color-primary) 82%, #fff), var(--color-primary));
    color: #fff;
    text-shadow: 0 1px 1px rgba(0, 0, 0, 0.16);
  }

  :deep(.btn-secondary) {
    background: var(--color-bg);
    background: linear-gradient(180deg, color-mix(in srgb, var(--color-primary) 6%, var(--color-surface)), var(--color-bg));
    color: var(--color-text);
  }

  :deep(.btn-warning) {
    border-color: #c18a24;
    background: #c18a24;
    background: linear-gradient(135deg, #d7a33f, #ad7518);
    color: #fff;
  }

  :deep(.btn-danger) {
    border-color: var(--color-danger);
    background: var(--color-danger);
    background: linear-gradient(135deg, color-mix(in srgb, var(--color-danger) 82%, #fff), var(--color-danger));
    color: #fff;
  }

  :deep(.btn-primary:hover:not(:disabled)),
  :deep(.btn-secondary:hover:not(:disabled)),
  :deep(.btn-warning:hover:not(:disabled)),
  :deep(.btn-danger:hover:not(:disabled)),
  :deep(.advs1-submit:hover:not(:disabled)) {
    transform: translateY(-1px);
    box-shadow: 0 6px 14px color-mix(in srgb, var(--color-primary) 18%, transparent);
  }

  :deep(.btn-primary:active:not(:disabled)),
  :deep(.btn-secondary:active:not(:disabled)),
  :deep(.btn-warning:active:not(:disabled)),
  :deep(.btn-danger:active:not(:disabled)),
  :deep(.advs1-submit:active:not(:disabled)) {
    transform: translateY(0) scale(0.98);
  }

  :deep(.btn-primary:focus-visible),
  :deep(.btn-secondary:focus-visible),
  :deep(.btn-warning:focus-visible),
  :deep(.btn-danger:focus-visible),
  :deep(.advs1-submit:focus-visible) {
    outline: 2px solid var(--color-primary);
    outline-offset: 2px;
  }

  :deep(.btn-primary:disabled),
  :deep(.btn-secondary:disabled),
  :deep(.btn-warning:disabled),
  :deep(.btn-danger:disabled),
  :deep(.advs1-submit:disabled) {
    cursor: not-allowed;
    opacity: 0.48;
    box-shadow: none;
  }
}

@media (max-width: 640px) {
  .advs1 {
    :deep(input:not([type='checkbox']):not([type='radio']):not([type='range']):not([type='file'])),
    :deep(select) {
      min-height: 36px;
      padding-block: 6px;
      font-size: 16px;
      box-shadow: none;
    }

    :deep(.btn-primary),
    :deep(.btn-secondary),
    :deep(.btn-warning),
    :deep(.btn-danger),
    :deep(.advs1-submit) {
      min-height: 36px;
      padding: 6px 12px;
      border-radius: 8px;
      font-size: 13px;
      font-weight: 600;
      box-shadow: none;
    }

    :deep(.btn-secondary) {
      background: var(--color-surface);
      color: var(--color-text-secondary);
    }

    :deep(.btn-sm) {
      min-height: 32px;
      padding: 5px 10px;
      font-size: 12px;
    }
  }
}

@media (prefers-reduced-motion: reduce) {
  .advs1 {
    :deep(input),
    :deep(select),
    :deep(textarea),
    :deep(.btn-primary),
    :deep(.btn-secondary),
    :deep(.btn-warning),
    :deep(.btn-danger),
    :deep(.advs1-submit) {
      transition: none;
    }
  }
}

.advs1 .mine-tools-toggle { display: none; }
.tab-anchor { scroll-margin-top: 56px; }

.pq-party-grid > .pq-party-scroll > .pq-actions:last-child,
.pq-pin-note > .pq-actions,
.pq-invite-letter > .pq-actions,
.pq-rift-card > .pq-actions { margin-top: auto; }

.pq-scroll-name, .pq-pin-name, .pq-invite-name, .pq-rift-name {
  white-space: normal;
  overflow-wrap: anywhere;
}

.log-where, .room-head, .room-residents { min-width: 0; overflow-wrap: anywhere; }
.dorm-room { display: flex; flex-direction: column; min-width: 0; }
.room-pips { flex-wrap: wrap; }
.room-residents { margin-top: auto; white-space: normal; }

@media (min-width: 641px) {
  .building-grid--zones > .building-card {
    display: grid;
    grid-template-columns: 32px minmax(0, 1fr);
    align-content: start;
    align-items: center;
    gap: 4px 10px;
    text-align: left;
    padding: 14px;
  }
  .building-grid--zones .building-icon { grid-row: span 2; font-size: 28px; }
  .building-grid--zones .building-desc { grid-column: 2; }
}

@media (max-width: 640px) {
  .advs1 { padding: 8px 12px max(24px, env(safe-area-inset-bottom)); }
  .advs1-header { min-height: 0; padding: 12px 14px; gap: 10px; margin-bottom: 10px; border-radius: 12px; background: var(--color-surface); box-shadow: none; }
  .advs1-header::before { display: none; }
  .advs1-kicker, .advs1-subtitle { display: none; }
  .advs1-title { gap: 6px; margin: 0; font-size: 19px; }
  .advs1-sigil { width: 28px; height: 28px; border-radius: 8px; font-size: 16px; box-shadow: none; }

  .tabs {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 2px;
    overflow: visible;
    margin-bottom: 14px;
    padding: 4px;
    border: 1px solid var(--color-border);
    border-radius: 10px;
    background: var(--color-surface);
    box-shadow: 0 3px 8px rgba(0, 0, 0, .04);
  }
  .tab-btn {
    min-width: 0;
    min-height: 32px;
    padding: 6px 2px;
    border: 0;
    border-radius: 6px;
    gap: 4px;
    font-size: 12px;
    font-weight: 500;
    line-height: 1.4;
    white-space: normal;
  }
  .tab-btn:last-child:nth-child(7) { grid-column: span 2; }
  .tab-btn-icon { font-size: 12px; filter: saturate(.55); }
  .tab-btn.active { font-weight: 650; background: var(--color-bg); background: color-mix(in srgb, var(--color-primary) 9%, var(--color-bg)); }
  .tab-btn.active .tab-btn-icon { filter: none; }
  .tab-content { min-width: 0; }
  .col-title { padding-left: 8px; border-left: 2px solid var(--color-primary); font-size: 15px; font-weight: 650; }
  .adv-grid, .dorm-grid, .dbp-instances, .building-grid--facilities { grid-template-columns: minmax(0, 1fr); }
  .building-grid { gap: 8px; }
  .building-grid--zones { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .building-grid--zones .building-card { display: grid; grid-template-columns: 26px minmax(0, 1fr); align-content: start; align-items: start; gap: 4px 8px; min-width: 0; padding: 12px 10px; border-radius: 10px; text-align: left; }
  .building-grid--zones .building-icon { grid-row: span 2; font-size: 23px; line-height: 1.3; }
  .building-grid--zones .building-name { font-size: 13px; line-height: 1.4; }
  .building-grid--zones .building-desc { font-size: 11px; line-height: 1.5; }
  .dorm-header { align-items: center; gap: 8px; }
  .dorm-header .col-title { flex: 1; }
  .btn-back { min-height: 32px; padding: 5px 9px; font-size: 12px; border-radius: 7px; }

  .mine-head { flex-wrap: wrap; align-items: center; gap: 10px; }
  .mine-head .col-title { margin: 0; }
  .advs1 .mine-tools-toggle { display: inline-flex; min-height: 32px; margin-left: auto; padding: 4px 7px; gap: 6px; align-items: center; border-color: transparent; background: transparent; font-size: 12px; box-shadow: none; }
  .mine-head-actions { display: flex; min-width: 0; flex-wrap: wrap; gap: 6px; margin-left: auto; }
  #s1-character-tools { width: 100%; padding: 8px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-bg); box-sizing: border-box; }
  .mine-head-actions > button { flex: 0 1 auto; white-space: normal; }
  .mine-head-actions.is-mobile-collapsed { display: none; }
  .mine-title-tools { width: 100%; justify-content: space-between; }
  .mine-title-tool { flex: 1; justify-content: center; min-height: 30px; font-size: 11px; }
  .activity-filter-select { flex: 1; width: 100%; min-width: 0; }
  .activity-list { padding: 0; }
  .act-narrative { font-size: 14px; line-height: 1.75; overflow-wrap: anywhere; }

  .log-toolbar, .filter-bar { display: flex; flex-wrap: wrap; gap: 8px; }
  .log-search-input, .filter-bar .search-input { flex: 1 1 100%; width: 100%; min-width: 0; }
  .log-status-select, .filter-bar select { flex: 1 1 calc(50% - 8px); width: 0; min-width: 0; }
  .log-card { padding: 12px; border-radius: 10px; }
  .log-card-head { grid-template-columns: minmax(0, 1fr); gap: 6px; }
  .log-status-wrap { justify-self: start; }
  .log-where { min-width: 0; flex-wrap: wrap; overflow-wrap: anywhere; }
  .log-summary { margin-block: 8px; font-size: 13px; line-height: 1.65; }
  .log-card-meta { flex-wrap: wrap; gap: 8px; }
  .log-favorite-btn { margin-left: auto; }

  .tavern-layout { gap: 12px; }
  .tavern-hall, .tavern-chat, .hall-table, .bar-stools > div, .table-seats > div { min-width: 0; }
  .bar-stools, .table-seats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
  .seat-name { font-size: 13px; overflow-wrap: anywhere; }
  .hall-hint { font-size: 12px; line-height: 1.65; }
  .chat-content { font-size: 14px; line-height: 1.7; }
  .treat-request { flex-wrap: wrap; gap: 6px; }
  .treat-request > span { flex-basis: 100%; }
  .treat-request > button { flex: 1; }
  .srp-card, .revert-offer-page-banner { flex-wrap: wrap; gap: 10px; }
  .srp-card-actions, .placeholder-recall-actions { display: flex; width: 100%; gap: 8px; }
  .srp-card-actions > button, .placeholder-recall-actions > button { flex: 1; }
  .commission-subtabs { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 4px; padding: 3px; border: 1px solid var(--color-border); border-radius: 9px; background: var(--color-bg); }
  .commission-subtabs button { min-height: 32px; padding: 5px 8px; border: 0; border-radius: 6px; background: transparent; font-size: 12px; white-space: normal; }
  .commission-subtabs button.active { background: var(--color-surface); color: var(--color-primary); }
  .party-board-hero { min-width: 0; }
  .pq-party-grid, .pq-pin-grid, .pq-invite-grid, .pq-rift-grid { grid-template-columns: minmax(0, 1fr); }
  .party-actions { flex-wrap: wrap; gap: 8px; }
  .party-actions > button { flex: 1 1 120px; white-space: normal; }
  .ai-result-box { padding: 16px; }
  .tavern-pick-list { max-height: none; }
}

.pq-member-crest.title-mini--hidden_relic {
  isolation: isolate;
  font-weight: 650;
  border: 1px solid #a98959;
  border-radius: 5px 10px 5px 10px;
  background: linear-gradient(115deg, rgba(209, 175, 113, .10), rgba(185, 154, 232, .08)), var(--color-surface, #161b22);
  &::before { content: '⬖'; color: #d1af71; font-size: 11px; line-height: 1; }
}
[data-theme="light"] .pq-member-crest.title-mini--hidden_relic { border-color: #90611c; }
[data-theme="light"] .pq-member-crest.title-mini--hidden_relic::before { color: #7950ae; }

</style>

<style src="../styles/mobile.scss" lang="scss"></style>
