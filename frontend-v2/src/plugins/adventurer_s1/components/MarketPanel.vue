<template>
  <div class="market-panel">
    <!-- 工具栏：搜索 + 类型切换 + 稀有度筛选 + 我的挂单 + 上架 -->
    <div class="market-toolbar">
      <div class="search-row">
        <input
          v-model="searchQuery"
          type="search"
          class="market-search-input"
          placeholder="搜索物品名、描述、卖方冒险者或想要备注…"
          maxlength="50"
          @input="debouncedSearch"
        >
        <button
          v-if="activeSearch"
          type="button"
          class="search-clear-btn"
          title="清除搜索"
          @click="clearSearch"
        >✕</button>
      </div>
      <div class="toolbar-main">
        <div class="filter-chips">
          <div class="filter-group" role="group" aria-label="交易类型">
            <span class="filter-label">类型</span>
            <button
              v-for="f in TYPE_FILTERS"
              :key="f.value"
              type="button"
              class="chip"
              :class="{ active: typeFilter === f.value }"
              :aria-pressed="typeFilter === f.value"
              @click="setTypeFilter(f.value)"
            >{{ f.label }}</button>
          </div>
          <div class="filter-group" role="group" aria-label="物品品质">
            <span class="filter-label">品质</span>
            <button
              v-for="f in RARITY_FILTERS"
              :key="f.value"
              type="button"
              class="chip"
              :class="{ active: rarityFilter === f.value }"
              :aria-pressed="rarityFilter === f.value"
              :style="f.value ? { '--chip-color': rarityColor(f.value) } : {}"
              @click="setRarityFilter(f.value)"
            >{{ f.label }}</button>
          </div>
        </div>
        <div class="toolbar-actions">
          <button
            type="button"
            class="market-action-button market-action-button--secondary offer-alert-button"
            @click="showBuyOrders = true"
          >
            <span class="market-action-icon" aria-hidden="true">🪙</span>
            <span>求购市场</span>
            <span v-if="store.marketBuyOrderPendingOfferCount > 0" class="offer-alert-badge">
              {{ store.marketBuyOrderPendingOfferCount > 99 ? '99+' : store.marketBuyOrderPendingOfferCount }}
            </span>
          </button>
          <button type="button" class="market-action-button market-action-button--secondary" @click="openMyOffers">
            <span class="market-action-icon" aria-hidden="true">📋</span>
            <span>我的出价</span>
          </button>
          <button type="button" class="market-action-button market-action-button--secondary offer-alert-button" @click="openMyListings">
            <span class="market-action-icon" aria-hidden="true">📦</span>
            <span>我的挂单</span>
            <span v-if="store.marketPendingOfferCount > 0" class="offer-alert-badge">
              {{ store.marketPendingOfferCount > 99 ? '99+' : store.marketPendingOfferCount }}
            </span>
          </button>
          <button
            type="button"
            class="market-action-button market-action-button--primary"
            :disabled="!canTrade"
            @click="openListDialog"
          >
            <span class="market-action-icon market-action-icon--plus" aria-hidden="true">+</span>
            <span>上架物品</span>
          </button>
        </div>
      </div>
    </div>

    <p v-if="isExploring" class="trade-block-hint">
      你的冒险者正在探险中，暂无法上架、购买、出价或下架。
    </p>

    <p class="fee-hint">
      <strong>以物易物</strong>：卖家挂单可备注想要什么，买家出价后卖家选定成交，双方各付手续费；
      <strong>卖钱</strong>：标价出售，直接购买，成交时收 20% 手续费。
      <span v-for="r in RARITIES" :key="r" class="fee-item" :style="{ color: rarityColor(r) }">
        {{ rarityLabel(r) }} {{ feeOf(r) }}💰
      </span>
    </p>

    <!-- 格子信息条 -->
    <div v-if="slotInfo" class="slot-bar">
      <span class="slot-text">
        📦 挂单格子：<strong>{{ slotInfo.active_count }}</strong> / {{ slotInfo.max_slots }}
        <template v-if="slotInfo.extra_slots > 0">
          （基础 {{ slotInfo.base_slots }} + 扩展 {{ slotInfo.extra_slots }}）
        </template>
        <template v-else>
          （基础 {{ slotInfo.base_slots }}）
        </template>
        <span v-if="slotInfo.extra_slots_expires_at" class="slot-expire">
          · 扩展到期：{{ formatDateTime(slotInfo.extra_slots_expires_at) }}
        </span>
      </span>
      <button
        type="button"
        class="btn-expand"
        :disabled="busy || !canTrade"
        @click="doPurchaseSlots"
      >⚡ 扩容 +2格（50💰/24h）</button>
    </div>

    <!-- 在售列表 -->
    <div v-if="store.marketLoading && store.marketListings.length === 0" class="empty">加载中…</div>
    <div v-else-if="store.marketListings.length === 0" class="empty">
      {{ activeSearch ? `未找到与「${activeSearch}」相关的在售物品` : '暂无在售物品' }}
    </div>
    <div v-else class="listing-grid">
      <div v-for="l in store.marketListings" :key="l.id" class="listing-card" tabindex="0">
        <div class="listing-head">
          <strong class="listing-name" :title="marketDisplayName(l)" :style="{ color: rarityColor(l.rarity) }">{{ marketDisplayName(l) }}</strong>
          <span v-if="l.clue_label" class="rarity-pill clue-index-pill">{{ l.clue_label }}</span>
          <span class="rarity-pill" :style="{ color: rarityColor(l.rarity), borderColor: rarityColor(l.rarity) }">
            {{ rarityLabel(l.rarity) }}
          </span>
          <span v-if="l.listing_type === 'sell'" class="type-pill sell">💰 卖钱</span>
          <span v-else class="type-pill barter">↔ 以物易物</span>
          <span v-if="isMine(l) && (l.offers_count ?? 0) > 0" class="listing-offer-alert">
            🔔 {{ l.offers_count }} 个新出价
          </span>
          <span v-if="(l.quantity ?? 1) > 1" class="uses">×{{ l.quantity }}</span>
          <span v-if="l.uses_left != null" class="uses">剩余 {{ l.uses_left }} 次</span>
        </div>
        <p v-if="l.want_note" class="listing-want">💭 想要：{{ l.want_note }}</p>
        <ItemDescription class="listing-description" :text="l.desc" />
        <div class="listing-foot">
          <span class="seller">{{ l.seller_name || '?' }}</span>
          <span v-if="l.listing_type === 'sell' && l.price" class="price-tag">
            {{ l.price }} 💰
            <span v-if="(l.quantity ?? 1) > 1" class="unit-price">（×{{ l.quantity }}，单价约 {{ sellUnitPrice(l) }}）</span>
          </span>
          <span class="time">{{ l.created_at ? formatDateTime(l.created_at) : '' }}</span>
          <div class="listing-actions">
            <button
              v-if="isMine(l)"
              type="button"
              class="mini-btn"
              :disabled="busy || !canTrade"
              @click="doCancel(l.id)"
            >下架</button>
            <button
              v-if="isMine(l) && l.listing_type === 'barter'"
              type="button"
              class="mini-btn primary"
              :disabled="busy"
              @click="openOffersView(l)"
            >查看出价<span v-if="(l.offers_count ?? 0) > 0"> · {{ l.offers_count }}</span></button>
            <template v-if="!isMine(l) && l.listing_type === 'sell'">
              <button
                type="button"
                class="mini-btn primary"
                :disabled="busy || !canTrade"
                @click="openBuyDialog(l)"
              >购买</button>
            </template>
            <button
              v-if="!isMine(l) && l.listing_type === 'barter'"
              type="button"
              class="mini-btn primary"
              :disabled="busy || !canTrade"
              @click="openOfferDialog(l)"
            >出价</button>
          </div>
        </div>
      </div>
    </div>
    <Pagination v-if="store.marketPagination" :pagination="store.marketPagination" @go="goPage" />

    <!-- 上架弹窗：选自己仓库的物品 + 模式选择 -->
    <div v-if="showListDialog" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showListDialog = false">
      <div class="modal-card s1-mobile-dialog">
        <header class="modal-head s1-mobile-dialog-header">
          <h3 class="modal-title">上架物品</h3>
          <button type="button" class="modal-close" @click="showListDialog = false">✕</button>
        </header>
        <div class="modal-body s1-mobile-dialog-body">
          <p class="muted small">
            从「{{ myAdv?.name }}」的收藏室选择要挂上市场的物品（草药店类物品不可上架）。
            <span v-if="!warehouseLoading" class="warehouse-cap">
              · 收藏室 {{ myWarehouse.length }} / {{ warehouseCapacityLimit }} 格
            </span>
          </p>

          <!-- 模式切换 -->
          <div class="mode-toggle">
            <button
              type="button"
              class="mode-btn"
              :class="{ active: listMode === 'barter' }"
              @click="listMode = 'barter'"
            >↔ 以物易物</button>
            <button
              type="button"
              class="mode-btn"
              :class="{ active: listMode === 'sell' }"
              @click="listMode = 'sell'"
            >💰 卖钱</button>
          </div>

          <!-- 以物易物：想要什么备注 -->
          <div v-if="listMode === 'barter'" class="want-note-section">
            <label class="price-label">想要什么（可选，让出价者知道你的需求）</label>
            <input
              v-model="wantNote"
              type="text"
              class="price-input"
              maxlength="200"
              placeholder="例如：想要一把弓或法杖类武器"
            />
          </div>

          <!-- 卖钱模式价格设置 -->
          <div v-if="listMode === 'sell'" class="sell-price-section">
            <label class="price-label">
              售价（金币）
              <span v-if="sellPriceRange" class="price-range-hint">
                范围：{{ sellPriceRange.min }} ~ {{ sellPriceRange.max }}
              </span>
            </label>
            <input
              v-model.number="sellPrice"
              type="number"
              class="price-input"
              :min="sellPriceRange?.min ?? 0"
              :max="sellPriceRange?.max ?? 99999"
              step="1"
              placeholder="输入售价"
            />
            <p v-if="sellPriceRange" class="muted small">
              手续费 20%：你实得 {{ estimatedIncome }} 金币
            </p>
          </div>

          <div v-if="warehouseLoading" class="empty">加载中…</div>
          <div v-else-if="sellableItems.length === 0" class="empty">收藏室没有可上架的物品</div>
          <template v-else>
            <InventoryListToolbar
              v-model:rarity="listingRarityFilter"
              v-model:sort="listingSort"
              :items="sellableItems"
              aria-label="按稀有度筛选可上架物品"
            />
            <div v-if="visibleSellableItems.length === 0" class="empty">当前稀有度下没有可上架物品</div>
          </template>
          <div v-if="!warehouseLoading && sellableItems.length > 0 && visibleSellableItems.length" class="pick-list">
            <div
              v-for="w in visibleSellableItems"
              :key="'sell-' + w.id"
              class="pick-item"
            >
              <div class="pick-item-info">
                <strong :style="{ color: rarityColor(w.rarity) }">{{ warehouseDisplayName(w) }}</strong>
                <span v-if="w.clue_label" class="rarity-pill clue-index-pill">{{ w.clue_label }}</span>
                <span class="rarity-pill" :style="{ color: rarityColor(w.rarity), borderColor: rarityColor(w.rarity) }">
                  {{ rarityLabel(w.rarity) }}
                </span>
                <span v-if="listMode === 'barter'" class="fee-tag">预扣 {{ feeOf(w.rarity) }}💰</span>
                <span v-else-if="sellRangeForItem(w)" class="fee-tag">
                  可售 {{ sellRangeForItem(w)!.min }}~{{ sellRangeForItem(w)!.max }}💰
                </span>
                <span v-if="listMode === 'barter' && (w.quantity ?? 1) > 1" class="fee-tag warn">
                  可按件交换，不必整堆
                </span>
                <span v-if="(w.quantity ?? 1) > 1" class="uses">×{{ w.quantity }}</span>
                <span v-if="w.uses_left != null" class="uses">剩余 {{ w.uses_left }} 次</span>
                <p v-if="w.desc" class="pick-item-desc">{{ w.desc }}</p>
              </div>
              <div class="pick-item-actions">
                <label v-if="(w.quantity ?? 1) > 1" class="list-qty-label">
                  数量
                  <input
                    type="number" class="list-qty-input" :min="1" :max="w.quantity ?? 1"
                    :value="listQtyFor(w.id, w.quantity ?? 1)" :disabled="busy"
                    @input="onListQtyInput(w.id, w.quantity ?? 1, $event)"
                  />
                </label>
                <button type="button" class="pick-list-btn" :disabled="busy" @click="doList(w)">上架</button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 我的挂单弹窗 -->
    <div v-if="showMyListings" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showMyListings = false">
      <div class="modal-card s1-mobile-dialog">
        <header class="modal-head s1-mobile-dialog-header">
          <h3 class="modal-title">📦 我的挂单</h3>
          <button type="button" class="modal-close" @click="showMyListings = false">✕</button>
        </header>
        <div class="modal-body s1-mobile-dialog-body">
          <h4 class="sub-title">在售（{{ store.myMarketActive.length }}）</h4>
          <div v-if="store.myMarketActive.length === 0" class="muted small">没有在售的挂单</div>
          <div v-for="l in store.myMarketActive" :key="l.id" class="my-listing-row">
            <strong :style="{ color: rarityColor(l.rarity) }">{{ marketDisplayName(l) }}</strong>
            <span v-if="l.clue_label" class="rarity-pill clue-index-pill">{{ l.clue_label }}</span>
            <span class="rarity-pill" :style="{ color: rarityColor(l.rarity), borderColor: rarityColor(l.rarity) }">
              {{ rarityLabel(l.rarity) }}
            </span>
            <span v-if="l.listing_type === 'sell' && l.price" class="price-tag">{{ l.price }}💰</span>
            <button type="button" class="mini-btn" :disabled="busy || !canTrade" @click="doCancel(l.id)">下架</button>
          </div>

          <h4 class="sub-title">最近记录</h4>
          <div v-if="store.myMarketHistory.length === 0" class="muted small">暂无成交或取消记录</div>
          <div v-for="l in store.myMarketHistory" :key="l.id" class="my-listing-row history">
            <strong :style="{ color: rarityColor(l.rarity) }">{{ marketDisplayName(l) }}</strong>
            <span v-if="l.clue_label" class="rarity-pill clue-index-pill">{{ l.clue_label }}</span>
            <span class="rarity-pill" :style="{ color: rarityColor(l.rarity), borderColor: rarityColor(l.rarity) }">
              {{ rarityLabel(l.rarity) }}
            </span>
            <span v-if="l.listing_type === 'sell' && l.status === 'completed'" class="trade-note">
              💰 售得 {{ l.price }} 金币（手续费 {{ l.seller_fee_paid ?? '?' }}）
            </span>
            <span v-else-if="l.status === 'completed'" class="trade-note">
              ↔ 换得「{{ marketTradedDisplayName(l) }}」（{{ l.buyer_name || '?' }}）
            </span>
            <span v-else class="trade-note muted">已取消</span>
          </div>
        </div>
      </div>
    </div>

    <!-- 购买弹窗：堆叠>1 时可选择数量 -->
    <div v-if="buyTarget" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="buyTarget = null">
      <div class="modal-card s1-mobile-dialog">
        <header class="modal-head s1-mobile-dialog-header">
          <h3 class="modal-title">购买「{{ marketDisplayName(buyTarget) }}」</h3>
          <button type="button" class="modal-close" @click="buyTarget = null">✕</button>
        </header>
        <div class="modal-body s1-mobile-dialog-body">
          <p class="muted small">
            卖家 {{ buyTarget.seller_name || '?' }} ·
            <span :style="{ color: rarityColor(buyTarget.rarity) }">{{ rarityLabel(buyTarget.rarity) }}</span>
            · 库存 {{ buyTarget.quantity ?? 1 }} 件
            <template v-if="(buyTarget.quantity ?? 1) > 1">
              · 单价约 {{ sellUnitPrice(buyTarget) }} 💰
            </template>
          </p>
          <label v-if="(buyTarget.quantity ?? 1) > 1" class="buy-qty-row">
            购买数量
            <button
              type="button"
              class="qty-step-btn"
              :disabled="busy || buyQty <= 1"
              aria-label="减少购买数量"
              @click="setBuyQty(buyQty - 1, buyTarget.quantity ?? 1)"
            >−</button>
            <input
              type="number"
              class="list-qty-input"
              :min="1"
              :max="buyTarget.quantity ?? 1"
              step="1"
              inputmode="numeric"
              :value="buyQty"
              :disabled="busy"
              @input="onBuyQtyInput(buyTarget.quantity ?? 1, $event)"
              @blur="normalizeBuyQtyInput(buyTarget.quantity ?? 1, $event)"
            />
            <button
              type="button"
              class="qty-step-btn"
              :disabled="busy || buyQty >= (buyTarget.quantity ?? 1)"
              aria-label="增加购买数量"
              @click="setBuyQty(buyQty + 1, buyTarget.quantity ?? 1)"
            >+</button>
          </label>
          <p class="buy-total">
            应付 <strong>{{ buyTotalPrice }}</strong> 💰
          </p>
          <div class="buy-actions">
            <button type="button" class="mini-btn primary" :disabled="busy || !canTrade" @click="doBuyConfirm">
              确认购买
            </button>
            <button type="button" class="mini-btn" :disabled="busy" @click="buyTarget = null">取消</button>
          </div>
        </div>
      </div>
    </div>

    <!-- 出价弹窗：买家选自己的同稀有度物品出价 -->
    <div v-if="offerTarget" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="offerTarget = null">
      <div class="modal-card s1-mobile-dialog">
        <header class="modal-head s1-mobile-dialog-header">
          <h3 class="modal-title">出价「{{ marketDisplayName(offerTarget) }}」</h3>
          <button type="button" class="modal-close" @click="offerTarget = null">✕</button>
        </header>
        <div class="modal-body s1-mobile-dialog-body">
          <p v-if="offerTarget.want_note" class="want-note-display">💭 卖家想要：{{ offerTarget.want_note }}</p>
          <p class="muted small">
            卖家 {{ offerTarget.seller_name || '?' }} ·
            <span :style="{ color: rarityColor(offerTarget.rarity) }">{{ rarityLabel(offerTarget.rarity) }}</span>
            · 库存 {{ offerTarget.quantity ?? 1 }} 件
          </p>
          <p class="muted small">
            选择同稀有度物品出价；换出与换入数量须一致。卖家选定后成交，你需支付 {{ feeOf(offerTarget.rarity) }} 金币手续费。
          </p>
          <label v-if="(offerTarget.quantity ?? 1) > 1" class="buy-qty-row">
            交换数量
            <input
              type="number"
              class="list-qty-input"
              :min="1"
              :max="offerTarget.quantity ?? 1"
              :value="offerTradeQty"
              :disabled="busy"
              @input="onOfferTradeQtyInput(offerTarget.quantity ?? 1, $event)"
            />
          </label>
          <div v-if="warehouseLoading" class="empty">加载中…</div>
          <div v-else-if="offerCandidates.length === 0" class="empty">
            收藏室没有可出价的同稀有度物品
          </div>
          <template v-else>
            <InventoryListToolbar
              v-model:rarity="offerRarityFilter"
              v-model:sort="offerSort"
              :items="offerCandidates"
              aria-label="按稀有度筛选可出价物品"
            />
            <div v-if="visibleOfferCandidates.length === 0" class="empty">当前稀有度下没有可出价物品</div>
          </template>
          <div v-if="!warehouseLoading && offerCandidates.length > 0 && visibleOfferCandidates.length" class="pick-list">
            <div
              v-for="w in visibleOfferCandidates"
              :key="'offer-' + w.id"
              class="pick-item"
            >
              <div class="pick-item-info">
                <strong :style="{ color: rarityColor(w.rarity) }">{{ warehouseDisplayName(w) }}</strong>
                <span v-if="w.clue_label" class="rarity-pill clue-index-pill">{{ w.clue_label }}</span>
                <span class="rarity-pill" :style="{ color: rarityColor(w.rarity), borderColor: rarityColor(w.rarity) }">
                  {{ rarityLabel(w.rarity) }}
                </span>
                <span v-if="(w.quantity ?? 1) > 1" class="uses">×{{ w.quantity }}</span>
                <span v-if="w.uses_left != null" class="uses">剩余 {{ w.uses_left }} 次</span>
                <p v-if="w.desc" class="pick-item-desc">{{ w.desc }}</p>
              </div>
              <button
                type="button"
                class="pick-list-btn"
                :disabled="busy || (w.quantity ?? 1) < offerTradeQtyOf(offerTarget)"
                @click="doSubmitOffer(w)"
              >
                出价 ×{{ offerTradeQtyOf(offerTarget) }}
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 出价管理弹窗：卖家查看某挂单的所有出价 -->
    <div v-if="showOffersView" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showOffersView = false">
      <div class="modal-card s1-mobile-dialog">
        <header class="modal-head s1-mobile-dialog-header">
          <h3 class="modal-title">📦 出价管理</h3>
          <button type="button" class="modal-close" @click="showOffersView = false">✕</button>
        </header>
        <div class="modal-body s1-mobile-dialog-body">
          <div v-if="store.listingOffersTarget" class="offers-listing-info">
            <strong :style="{ color: rarityColor(store.listingOffersTarget.rarity) }">
              {{ marketDisplayName(store.listingOffersTarget) }}
            </strong>
            <span v-if="store.listingOffersTarget.clue_label" class="rarity-pill clue-index-pill">
              {{ store.listingOffersTarget.clue_label }}
            </span>
            <span class="rarity-pill" :style="{ color: rarityColor(store.listingOffersTarget.rarity), borderColor: rarityColor(store.listingOffersTarget.rarity) }">
              {{ rarityLabel(store.listingOffersTarget.rarity) }}
            </span>
            <span v-if="(store.listingOffersTarget.quantity ?? 1) > 1" class="uses">
              库存 ×{{ store.listingOffersTarget.quantity }}
            </span>
            <span v-if="store.listingOffersTarget.want_note" class="want-note-inline">
              💭 {{ store.listingOffersTarget.want_note }}
            </span>
            <button type="button" class="mini-btn" :disabled="busy || !canTrade" @click="openEditNote">编辑备注</button>
          </div>

          <!-- 编辑备注 -->
          <div v-if="editingNote" class="edit-note-section">
            <input
              v-model="editNoteText"
              type="text"
              class="price-input"
              maxlength="200"
              placeholder="修改想要什么备注"
            />
            <div class="edit-note-actions">
              <button type="button" class="mini-btn primary" :disabled="busy || !canTrade" @click="doSaveNote">保存</button>
              <button type="button" class="mini-btn" @click="editingNote = false">取消</button>
            </div>
          </div>

          <h4 class="sub-title">出价列表（{{ store.listingOffers.length }}）</h4>
          <div v-if="store.listingOffers.length === 0" class="muted small">暂无出价</div>
          <div v-for="o in store.listingOffers" :key="o.id" class="offer-row">
            <div class="offer-info">
              <strong :style="{ color: rarityColor(o.offered_rarity) }">{{ marketOfferDisplayName(o) }}</strong>
              <span v-if="o.offered_clue_label" class="rarity-pill clue-index-pill">{{ o.offered_clue_label }}</span>
              <span class="rarity-pill" :style="{ color: rarityColor(o.offered_rarity), borderColor: rarityColor(o.offered_rarity) }">
                {{ rarityLabel(o.offered_rarity) }}
              </span>
              <span v-if="(o.offered_quantity ?? 1) > 1 || (o.listing_quantity ?? 1) > 1" class="uses">
                换 ×{{ o.listing_quantity ?? o.offered_quantity ?? 1 }}
              </span>
              <span v-if="o.offered_uses_left != null" class="uses">剩余 {{ o.offered_uses_left }} 次</span>
              <span v-if="o.offered_desc" class="offer-desc">{{ o.offered_desc }}</span>
            </div>
            <div class="offer-meta">
              <span class="offer-buyer">{{ o.buyer_name || '?' }}</span>
              <span class="offer-time">{{ o.created_at ? formatDateTime(o.created_at) : '' }}</span>
              <span :class="['offer-status', o.status]">{{ offerStatusLabel(o.status) }}</span>
              <button
                v-if="o.status === 'pending'"
                type="button"
                class="mini-btn primary"
                :disabled="busy || !canTrade"
                @click="doAcceptOffer(o)"
              >选定成交</button>
              <button v-if="o.status === 'pending'" type="button" class="mini-btn" :disabled="busy" @click="doRejectOffer(o)">不要这个</button>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 我的出价弹窗：买家查看自己的出价记录 -->
    <div v-if="showMyOffers" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="showMyOffers = false">
      <div class="modal-card s1-mobile-dialog">
        <header class="modal-head s1-mobile-dialog-header">
          <h3 class="modal-title">📋 我的出价</h3>
          <button type="button" class="modal-close" @click="showMyOffers = false">✕</button>
        </header>
        <div class="modal-body s1-mobile-dialog-body">
          <div v-if="store.myOffers.length === 0" class="muted small">暂无出价记录</div>
          <div v-for="o in store.myOffers" :key="o.id" class="my-offer-row">
            <div class="offer-info">
              <span class="offer-label">出价：</span>
              <strong :style="{ color: rarityColor(o.offered_rarity) }">{{ marketOfferDisplayName(o) }}</strong>
              <span v-if="o.offered_clue_label" class="rarity-pill clue-index-pill">{{ o.offered_clue_label }}</span>
              <span class="rarity-pill" :style="{ color: rarityColor(o.offered_rarity), borderColor: rarityColor(o.offered_rarity) }">
                {{ rarityLabel(o.offered_rarity) }}
              </span>
            </div>
            <div v-if="o.listing" class="offer-listing-info">
              <span class="offer-label">目标：</span>
              <strong :style="{ color: rarityColor(o.listing.rarity) }">{{ marketDisplayName(o.listing) }}</strong>
              <span v-if="o.listing.clue_label" class="rarity-pill clue-index-pill">{{ o.listing.clue_label }}</span>
              <span class="rarity-pill" :style="{ color: rarityColor(o.listing.rarity), borderColor: rarityColor(o.listing.rarity) }">
                {{ rarityLabel(o.listing.rarity) }}
              </span>
              <span class="muted small">（{{ o.listing.seller_name || '?' }}）</span>
            </div>
            <div class="offer-meta">
              <span :class="['offer-status', o.status]">{{ offerStatusLabel(o.status) }}</span>
              <span class="offer-time">{{ o.created_at ? formatDateTime(o.created_at) : '' }}</span>
              <button
                v-if="o.status === 'pending'"
                type="button"
                class="mini-btn"
                :disabled="busy || !canTrade"
                @click="doCancelOffer(o)"
              >取消出价</button>
            </div>
          </div>
        </div>
      </div>
    </div>
    <MarketBuyOrdersPanel v-if="showBuyOrders" @close="showBuyOrders = false" />
  </div>
</template>

<script setup lang="ts">
import ItemDescription from './ItemDescription.vue'
import { ref, computed, onMounted, onUnmounted } from 'vue'
import { useAdventurerS1Store, isPlayableAdventurer, type MarketListing, type MarketOffer } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useSocket } from '@/composables/useSocket'
import api from '@/composables/useApi'
import Pagination from '@/components/Pagination.vue'
import InventoryListToolbar from './InventoryListToolbar.vue'
import MarketBuyOrdersPanel from './MarketBuyOrdersPanel.vue'
import { formatDateTime } from '@/utils/time'
import { rarityColor, rarityLabel, isExploringStatus, hiddenBossClueItemDisplayName } from '../utils'
import { useInventoryListControls } from '../composables/useInventoryListControls'

interface WarehouseEntry {
  id: number
  name: string
  clue_label?: string | null
  type: string
  desc?: string
  rarity: string
  quantity?: number | null
  uses_left?: number | null
  evolution_stage?: number
  created_at?: string | null
  is_player_locked?: boolean
}

type ClueDisplayItem = {
  name?: string
  item_name?: string
  clue_label?: string | null
  traded_item_name?: string | null
  traded_clue_label?: string | null
  offered_item_name?: string
  offered_clue_label?: string | null
  item_type?: string
  type?: string
  evolution_stage?: number
}

function companionStageDisplayName(
  name: string,
  itemType: string | undefined,
  stage: number | null | undefined,
): string {
  if (itemType !== 'companion') return name
  return typeof stage === 'number'
    ? `${name}（嬗变${Math.max(0, stage)}阶）`
    : `${name}（历史阶级未记录）`
}

function marketDisplayName(item: ClueDisplayItem | null | undefined): string {
  if (!item) return ''
  const name = hiddenBossClueItemDisplayName({
    name: item.item_name ?? item.name,
    clue_label: item.clue_label,
  })
  return companionStageDisplayName(name, item.item_type ?? item.type, item.evolution_stage)
}

function marketTradedDisplayName(item: ClueDisplayItem | null | undefined): string {
  if (!item?.traded_item_name) return ''
  return hiddenBossClueItemDisplayName({
    name: item.traded_item_name,
    clue_label: item.traded_clue_label,
  })
}

function marketOfferDisplayName(offer: MarketOffer | null | undefined): string {
  if (!offer) return ''
  const name = hiddenBossClueItemDisplayName({
    name: offer.offered_item_name,
    clue_label: offer.offered_clue_label,
  })
  return companionStageDisplayName(
    name, offer.offered_item_type, offer.offered_evolution_stage,
  )
}

function warehouseDisplayName(item: WarehouseEntry | null | undefined): string {
  if (!item) return ''
  return companionStageDisplayName(
    hiddenBossClueItemDisplayName(item), item.type, item.evolution_stage,
  )
}

function tradeReceivedDisplayName(data: {
  received_item?: string
  received_clue_label?: string | null
} | null | undefined): string {
  if (!data?.received_item) return ''
  return hiddenBossClueItemDisplayName({
    name: data.received_item,
    clue_label: data.received_clue_label,
  })
}

const store = useAdventurerS1Store()
const message = useGlobalMessage()

const RARITIES = ['common', 'rare', 'epic', 'legendary']
const RARITY_FILTERS = [
  { value: '', label: '全部' },
  ...RARITIES.map(r => ({ value: r, label: rarityLabel(r) })),
]
const TYPE_FILTERS = [
  { value: '', label: '全部' },
  { value: 'barter', label: '↔ 以物易物' },
  { value: 'sell', label: '💰 卖钱' },
]

const busy = ref(false)
const rarityFilter = ref('')
const typeFilter = ref('')
const searchQuery = ref('')
const activeSearch = ref('')
let searchDebounceTimer: ReturnType<typeof setTimeout> | null = null
const showListDialog = ref(false)
const showMyListings = ref(false)
const warehouseLoading = ref(false)
const myWarehouse = ref<WarehouseEntry[]>([])
const warehouseCapacityLimit = ref(100)
const listMode = ref<'barter' | 'sell'>('barter')
const sellPrice = ref<number | null>(null)
const wantNote = ref('')
const offerTarget = ref<MarketListing | null>(null)
const buyTarget = ref<MarketListing | null>(null)
const buyQty = ref(1)
const offerTradeQty = ref(1)
const showOffersView = ref(false)
const showMyOffers = ref(false)
const showBuyOrders = ref(false)
const editingNote = ref(false)
const editNoteText = ref('')

// 玩家与冒险者 1:1 绑定：操作直接用绑定的存活冒险者，不弹选择列表
const myAdv = computed(() => store.adventurers.find(isPlayableAdventurer) ?? null)
const isExploring = computed(() => isExploringStatus(myAdv.value?.status))
const canTrade = computed(() => !!myAdv.value && !isExploring.value)
const slotInfo = computed(() => store.marketSlotInfo)

const sellableItems = computed(() => myWarehouse.value.filter(item => !item.is_player_locked))
const offerCandidates = computed(() => {
  if (!offerTarget.value) return []
  const rarity = offerTarget.value.rarity
  return myWarehouse.value.filter(w => !w.is_player_locked && w.rarity === rarity)
})
const {
  rarityFilter: listingRarityFilter,
  sort: listingSort,
  visibleItems: visibleSellableItems,
} = useInventoryListControls(sellableItems, warehouseDisplayName)
const {
  rarityFilter: offerRarityFilter,
  sort: offerSort,
  visibleItems: visibleOfferCandidates,
} = useInventoryListControls(offerCandidates, warehouseDisplayName)

// 卖钱模式：根据选中物品的稀有度显示价格范围
const sellPriceRange = computed(() => {
  // 在上架弹窗中，先选物品再定价格不太合理，所以显示通用范围
  // 用户点击物品时会校验具体稀有度
  return null as { min: number; max: number } | null
})

const estimatedIncome = computed(() => {
  if (!sellPrice.value || sellPrice.value <= 0) return 0
  return Math.round(sellPrice.value * (1 - store.marketSellFeeRate) * 100) / 100
})

// 上架数量（堆叠>1 取选择值，否则 1）
function listQtyOf(w: WarehouseEntry): number {
  return (w.quantity ?? 1) > 1 ? listQtyFor(w.id, w.quantity ?? 1) : 1
}

// 卖钱区间按上架数量整体放大（与后端 _validate_sell_price 一致）
function sellRangeForItem(w: WarehouseEntry): { min: number; max: number } | null {
  const r = store.marketSellPriceRange[w.rarity]
  if (!r) return null
  const q = listQtyOf(w)
  return { min: r.min * q, max: r.max * q }
}

function sellUnitPrice(l: MarketListing): number {
  const q = l.quantity ?? 1
  const price = l.price ?? 0
  if (q <= 1 || price <= 0) return price
  return Math.round((price / q) * 100) / 100
}

function calcBuyTotal(l: MarketListing, qty: number): number {
  const listingQty = l.quantity ?? 1
  const price = l.price ?? 0
  if (qty >= listingQty) return price
  return Math.round(sellUnitPrice(l) * qty * 100) / 100
}

const buyTotalPrice = computed(() => {
  if (!buyTarget.value) return 0
  return calcBuyTotal(buyTarget.value, buyQty.value)
})

function onBuyQtyInput(max: number, event: Event) {
  const input = event.target as HTMLInputElement
  // 用户替换现有数字时会先清空输入框；保留这个编辑中间态，避免立即
  // 回填 1 后把下一位拼成大数并钳制成最大库存。
  if (input.value === '') return
  let value = parseInt(input.value, 10)
  if (Number.isNaN(value)) value = 1
  setBuyQty(value, max)
  if (String(buyQty.value) !== input.value) input.value = String(buyQty.value)
}

function normalizeBuyQtyInput(max: number, event: Event) {
  const input = event.target as HTMLInputElement
  const value = parseInt(input.value, 10)
  setBuyQty(Number.isNaN(value) ? buyQty.value : value, max)
  input.value = String(buyQty.value)
}

function setBuyQty(value: number, max: number) {
  buyQty.value = Math.min(max, Math.max(1, Math.trunc(value)))
}

function openBuyDialog(l: MarketListing) {
  if (!ensureCanTrade()) return
  buyTarget.value = l
  buyQty.value = 1
}

function feeOf(rarity: string): number {
  return store.marketFees[rarity] ?? 0
}

function isMine(l: MarketListing): boolean {
  return store.adventurers.some(a => a.id === l.seller_adventurer_id)
}

function setTypeFilter(value: string) {
  typeFilter.value = value
  refreshList(1)
}

function setRarityFilter(value: string) {
  rarityFilter.value = value
  refreshList(1)
}

function marketListParams(page: number) {
  return [
    page,
    rarityFilter.value || undefined,
    typeFilter.value || undefined,
    activeSearch.value || undefined,
  ] as const
}

function goPage(page: number) {
  store.fetchMarketListings(...marketListParams(page))
}

async function refreshList(page?: number) {
  const targetPage = page ?? store.marketPagination?.current_page ?? 1
  await store.fetchMarketListings(...marketListParams(targetPage))
}

function debouncedSearch() {
  if (searchDebounceTimer) clearTimeout(searchDebounceTimer)
  searchDebounceTimer = setTimeout(() => {
    activeSearch.value = searchQuery.value.trim()
    refreshList(1)
  }, 300)
}

function clearSearch() {
  searchQuery.value = ''
  activeSearch.value = ''
  if (searchDebounceTimer) {
    clearTimeout(searchDebounceTimer)
    searchDebounceTimer = null
  }
  refreshList(1)
}

// 拉绑定冒险者的收藏室（详情接口 owner 才返回 warehouse；不写 store.currentDetail 避免污染详情弹窗）
async function loadMyWarehouse() {
  if (!myAdv.value) return
  warehouseLoading.value = true
  try {
    const { data } = await api.get(`/adventurer-s1/${myAdv.value.id}`)
    if (data.code === 200) {
      myWarehouse.value = data.data?.warehouse ?? []
      warehouseCapacityLimit.value = data.data?.warehouse_capacity_limit ?? 100
    } else {
      message.error(data.message || '加载收藏室失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '加载收藏室失败')
  } finally {
    warehouseLoading.value = false
  }
}

function ensureCanTrade(): boolean {
  if (!myAdv.value) {
    message.error('你还没有冒险者，请先创建')
    return false
  }
  if (isExploring.value) {
    message.error('探险中无法进行交易')
    return false
  }
  return true
}

function openListDialog() {
  if (!ensureCanTrade()) return
  showListDialog.value = true
  listMode.value = 'barter'
  sellPrice.value = null
  wantNote.value = ''
  loadMyWarehouse()
}

function openOfferDialog(l: MarketListing) {
  if (!ensureCanTrade()) return
  offerTarget.value = l
  offerTradeQty.value = 1
  loadMyWarehouse()
}

function offerTradeQtyOf(l: MarketListing | null): number {
  if (!l) return 1
  return (l.quantity ?? 1) > 1 ? offerTradeQty.value : 1
}

function onOfferTradeQtyInput(max: number, event: Event) {
  const input = event.target as HTMLInputElement
  let value = parseInt(input.value, 10)
  if (Number.isNaN(value)) value = 1
  value = Math.min(max, Math.max(1, value))
  offerTradeQty.value = value
  if (String(value) !== input.value) input.value = String(value)
}

async function openMyListings() {
  showMyListings.value = true
  await store.fetchMyMarketListings()
}

async function openMyOffers() {
  showMyOffers.value = true
  await store.fetchMyOffers()
}

async function openOffersView(l: MarketListing) {
  showOffersView.value = true
  editingNote.value = false
  await store.fetchOffersForListing(l.id)
}

function openEditNote() {
  editNoteText.value = store.listingOffersTarget?.want_note || ''
  editingNote.value = true
}

async function doSaveNote() {
  if (!store.listingOffersTarget || busy.value || !ensureCanTrade()) return
  busy.value = true
  try {
    const res = await store.updateWantNote(store.listingOffersTarget.id, editNoteText.value)
    if (res.code === 200) {
      message.success('备注已更新')
      editingNote.value = false
      // 刷新出价列表
      await store.fetchOffersForListing(store.listingOffersTarget.id)
      await refreshList()
    } else {
      message.error(res.message || '更新失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '更新失败')
  } finally {
    busy.value = false
  }
}

async function doSubmitOffer(item: WarehouseEntry) {
  if (!myAdv.value || !offerTarget.value || busy.value || !ensureCanTrade()) return
  const qty = offerTradeQtyOf(offerTarget.value)
  if ((item.quantity ?? 1) < qty) {
    message.error(`「${warehouseDisplayName(item)}」数量不足（需要 ${qty} 件）`)
    return
  }
  busy.value = true
  try {
    const res = await store.submitOffer(
      offerTarget.value.id, myAdv.value.id, item.id,
      (offerTarget.value.quantity ?? 1) > 1 ? qty : undefined,
    )
    if (res.code === 201 || res.code === 200) {
      const qtyNote = qty > 1 ? ` ×${qty}` : ''
      message.success(`出价成功，「${warehouseDisplayName(item)}」${qtyNote} 已提交给卖家`)
      offerTarget.value = null
    } else {
      message.error(res.message || '出价失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '出价失败')
  } finally {
    busy.value = false
  }
}

async function doAcceptOffer(offer: MarketOffer) {
  if (!store.listingOffersTarget || busy.value || !ensureCanTrade()) return
  busy.value = true
  try {
    const res = await store.acceptOffer(store.listingOffersTarget.id, offer.id)
    if (res.code === 200) {
      const qty = res.data?.trade_quantity ?? offer.listing_quantity ?? offer.offered_quantity ?? 1
      const qtyNote = qty > 1 ? ` ×${qty}` : ''
      message.success(`成交成功，获得「${tradeReceivedDisplayName(res.data)}」${qtyNote}`)
      showOffersView.value = false
      await refreshList()
    } else {
      message.error(res.message || '成交失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '成交失败')
  } finally {
    busy.value = false
  }
}

async function doRejectOffer(offer: MarketOffer) {
  const listingId = store.listingOffersTarget?.id
  if (!listingId || busy.value) return
  busy.value = true
  try {
    const res = await store.rejectOffer(listingId, offer.id)
    if (res.code !== 200) {
      message.error(res.message || '拒绝报价失败')
      return
    }
    message.success('已拒绝，对方可以重新报价')
    await Promise.all([store.fetchOffersForListing(listingId), store.fetchMyMarketListings()])
  } catch (e: any) {
    message.error(e?.response?.data?.message || '处理失败，请刷新报价列表确认状态')
  } finally {
    busy.value = false
  }
}

async function doCancelOffer(offer: MarketOffer) {
  if (busy.value || !ensureCanTrade()) return
  busy.value = true
  try {
    const res = await store.cancelOffer(offer.id)
    if (res.code === 200) {
      message.success('出价已取消')
      await store.fetchMyOffers()
    } else {
      message.error(res.message || '取消失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消失败')
  } finally {
    busy.value = false
  }
}

function offerStatusLabel(status: string): string {
  const map: Record<string, string> = {
    pending: '⏳ 待选定',
    accepted: '✅ 已成交',
    rejected: '❌ 未选中',
    cancelled: '🚫 已取消',
  }
  return map[status] || status
}

// 挂单数量：默认 1，可改到堆叠上限
const listQty = ref<Record<number, number>>({})
function listQtyFor(id: number, max = 1) {
  const v = listQty.value[id]
  if (v == null) return 1
  return Math.min(max, Math.max(1, v))
}
function onListQtyInput(id: number, max: number, event: Event) {
  const input = event.target as HTMLInputElement
  let value = parseInt(input.value, 10)
  if (Number.isNaN(value)) value = 1
  value = Math.min(max, Math.max(1, value))
  listQty.value = { ...listQty.value, [id]: value }
  if (String(value) !== input.value) input.value = String(value)
}

async function doList(item: WarehouseEntry) {
  if (!myAdv.value || busy.value || !ensureCanTrade()) return

  // 卖钱模式校验
  if (listMode.value === 'sell') {
    if (!sellPrice.value || sellPrice.value <= 0) {
      message.error('请先设定售价')
      return
    }
    const range = sellRangeForItem(item)
    if (range) {
      const q = listQtyOf(item)
      const qtyNote = q > 1 ? `（${q} 件）` : ''
      if (sellPrice.value < range.min) {
        message.error(`售价不能低于 ${range.min} 金币${qtyNote}`)
        return
      }
      if (sellPrice.value > range.max) {
        message.error(`售价不能超过 ${range.max} 金币${qtyNote}`)
        return
      }
    }
  }

  const qty = (item.quantity ?? 1) > 1 ? listQtyFor(item.id, item.quantity ?? 1) : undefined

  busy.value = true
  try {
    const res = await store.createMarketListing(
      myAdv.value.id, item.id,
      listMode.value,
      listMode.value === 'sell' ? sellPrice.value ?? undefined : undefined,
      listMode.value === 'barter' ? wantNote.value || undefined : undefined,
      qty,
    )
    if (res.code === 201 || res.code === 200) {
      const modeLabel = listMode.value === 'sell' ? `（售价 ${sellPrice.value}💰）` : ''
      const qtyLabel = qty && qty > 1 ? ` ×${qty}` : ''
      message.success(`「${warehouseDisplayName(item)}」已上架${qtyLabel}${modeLabel}`)
      showListDialog.value = false
      await Promise.all([refreshList(), myAdv.value ? store.fetchMarketSlotInfo(myAdv.value.id) : Promise.resolve()])
    } else {
      message.error(res.message || '上架失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '上架失败')
  } finally {
    busy.value = false
  }
}

async function doCancel(listingId: number) {
  if (busy.value || !ensureCanTrade()) return
  busy.value = true
  try {
    const res = await store.cancelMarketListing(listingId)
    if (res.code === 200) {
      message.success('已下架，物品与预扣手续费已退回')
      const tasks: Promise<any>[] = [refreshList(), store.fetchMyMarketListings()]
      if (myAdv.value) tasks.push(store.fetchMarketSlotInfo(myAdv.value.id))
      await Promise.all(tasks)
    } else {
      message.error(res.message || '下架失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '下架失败')
  } finally {
    busy.value = false
  }
}

async function doBuyConfirm() {
  const l = buyTarget.value
  if (!l || !myAdv.value || busy.value || !ensureCanTrade()) return
  const qty = (l.quantity ?? 1) > 1 ? buyQty.value : 1
  const total = calcBuyTotal(l, qty)
  busy.value = true
  try {
    const res = await store.buyMarketListing(l.id, myAdv.value.id, qty)
    if (res.code === 200) {
      const qtyNote = qty > 1 ? ` ×${qty}` : ''
      message.success(`购买成功，获得「${marketDisplayName(l)}」${qtyNote}，花费 ${res.data?.price_paid ?? total} 金币`)
      buyTarget.value = null
      await refreshList()
    } else {
      message.error(res.message || '购买失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '购买失败')
  } finally {
    busy.value = false
  }
}

async function doPurchaseSlots() {
  if (!myAdv.value || busy.value || !ensureCanTrade()) return
  if (!window.confirm('确认花费 50 金币扩容市场 +2 格（24小时有效）？')) return
  busy.value = true
  try {
    const res = await store.purchaseMarketSlots(myAdv.value.id)
    if (res.code === 200) {
      message.success(`扩容成功！+${res.data?.extra_slots ?? 2} 格，24 小时有效`)
      await store.fetchMarketSlotInfo(myAdv.value.id)
    } else {
      message.error(res.message || '扩容失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '扩容失败')
  } finally {
    busy.value = false
  }
}

const socket = useSocket()

onMounted(async () => {
  // 保证绑定冒险者已加载（幂等，量小）
  if (store.adventurers.length === 0) {
    await store.fetchAdventurers()
  }
  const tasks: Promise<any>[] = [
    store.fetchMarketListings(1),
    store.fetchMarketBuyOrders(1),
  ]
  const adv = store.adventurers.find(isPlayableAdventurer)
  if (adv) tasks.push(store.fetchMarketSlotInfo(adv.id))
  await Promise.all(tasks)

  socket.on('adventurer_s1_market_changed', onMarketChanged)
})

onUnmounted(() => {
  if (searchDebounceTimer) clearTimeout(searchDebounceTimer)
  socket.off('adventurer_s1_market_changed', onMarketChanged)
})

function onMarketChanged() {
  refreshList()
  store.fetchMarketBuyOrders(1)
}
</script>

<style scoped lang="scss">
.market-panel {
  margin-top: 12px;
}

.market-toolbar {
  display: flex;
  flex-direction: column;
  gap: 8px;
  margin-bottom: 8px;
}

.search-row {
  display: flex;
  align-items: center;
  gap: 6px;
  width: 100%;
  max-width: 360px;
}

.market-search-input {
  flex: 1;
  min-width: 0;
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
  font-size: 13px;

  &::placeholder {
    color: var(--color-text-secondary);
  }

  &:focus {
    outline: none;
    border-color: var(--color-primary);
  }
}

.search-clear-btn {
  flex-shrink: 0;
  width: 28px;
  height: 28px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;
  font-size: 12px;
  line-height: 1;

  &:hover {
    color: var(--color-text);
    border-color: var(--color-text-secondary);
  }
}

.toolbar-main {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.filter-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.filter-group { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.filter-label { margin-right: 3px; color: var(--color-text-secondary); font-size: 11px; }

.chip {
  font-size: 12px;
  padding: 3px 12px;
  border: 1px solid var(--color-border);
  border-radius: 12px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;

  &.active {
    border-color: var(--chip-color, var(--color-primary));
    color: var(--chip-color, var(--color-primary));
    font-weight: 600;
  }
}

.toolbar-actions {
  display: flex;
  gap: 8px;
}

.market-action-button {
  appearance: none;
  -webkit-appearance: none;
  display: inline-flex;
  min-height: 36px;
  align-items: center;
  justify-content: center;
  gap: 7px;
  padding: 7px 13px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  color: var(--color-text);
  font: inherit;
  font-size: 12px;
  font-weight: 700;
  line-height: 1;
  white-space: nowrap;
  cursor: pointer;
  box-shadow: 0 3px 10px rgba(0, 0, 0, 0.1);
  transition: transform 0.16s ease, border-color 0.16s ease, background 0.16s ease, box-shadow 0.16s ease;

  &:hover:not(:disabled) {
    transform: translateY(-1px);
  }

  &:active:not(:disabled) {
    transform: translateY(0) scale(0.98);
  }

  &:focus-visible {
    outline: 2px solid var(--color-primary);
    outline-offset: 2px;
  }

  &:disabled {
    cursor: not-allowed;
    opacity: 0.48;
    box-shadow: none;
  }
}

.market-action-button--secondary {
  background: var(--color-bg);
  background: linear-gradient(180deg, color-mix(in srgb, var(--market-blue) 7%, var(--color-surface)), var(--color-bg));
  border-color: color-mix(in srgb, var(--market-blue) 24%, var(--color-border));

  &:hover:not(:disabled) {
    border-color: color-mix(in srgb, var(--market-blue) 55%, var(--color-border));
    background: color-mix(in srgb, var(--market-blue) 11%, var(--color-bg));
    box-shadow: 0 6px 14px color-mix(in srgb, var(--market-blue) 14%, transparent);
  }
}

.market-action-button--primary {
  border-color: var(--color-border);
  border-color: color-mix(in srgb, var(--market-blue) 38%, var(--color-border));
  background: var(--color-bg);
  background: linear-gradient(180deg, color-mix(in srgb, var(--market-blue) 10%, var(--color-surface)), var(--color-bg));
  color: var(--color-text);

  &:hover:not(:disabled) {
    border-color: color-mix(in srgb, var(--market-blue) 62%, var(--color-border));
    background: color-mix(in srgb, var(--market-blue) 14%, var(--color-bg));
    box-shadow: 0 6px 14px color-mix(in srgb, var(--market-blue) 16%, transparent);
  }
}

.market-action-icon {
  display: inline-grid;
  width: 20px;
  height: 20px;
  flex: 0 0 20px;
  place-items: center;
  border-radius: 6px;
  background: color-mix(in srgb, var(--market-blue) 10%, transparent);
  font-size: 13px;
  line-height: 1;
}

.market-action-icon--plus {
  background: color-mix(in srgb, var(--market-blue) 16%, transparent);
  color: var(--market-blue);
  font-size: 18px;
  font-weight: 600;
}

.offer-alert-button {
  position: relative;
}

.offer-alert-badge {
  display: inline-grid;
  min-width: 18px;
  height: 18px;
  place-items: center;
  padding: 0 5px;
  border: 1px solid rgba(255, 255, 255, 0.5);
  border-radius: 999px;
  background: #d94b5f;
  color: #fff;
  font-size: 10px;
  font-weight: 800;
  line-height: 1;
  box-shadow: 0 2px 8px rgba(217, 75, 95, 0.32);
}

.slot-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  margin-bottom: 8px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
}

.slot-text {
  .slot-expire {
    font-size: 11px;
    color: var(--color-text-secondary);
  }
}

.btn-expand {
  font-size: 12px;
  padding: 4px 12px;
  border: 1px solid #e6a817;
  border-radius: 8px;
  background: rgba(255, 193, 7, 0.1);
  color: #e6a817;
  cursor: pointer;
  white-space: nowrap;
  font-weight: 600;

  &:hover:not(:disabled) {
    background: rgba(255, 193, 7, 0.2);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.fee-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--color-text-secondary);

  .fee-item {
    margin-left: 8px;
    font-weight: 600;
  }
}

.trade-block-hint {
  margin: 0 0 12px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 13px;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-warning, #e6a817) 12%, var(--color-surface));
  border: 1px solid color-mix(in srgb, var(--color-warning, #e6a817) 35%, var(--color-border));
}

.fee-tag {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-text-secondary);
  white-space: nowrap;

  &.warn {
    color: var(--color-warning, #e6a817);
  }
}

.empty {
  padding: 24px 0;
  text-align: center;
  color: var(--color-text-secondary);
}

.listing-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  align-items: stretch;
  gap: 10px;
  margin-bottom: 12px;
}

.listing-card {
  padding: 10px 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.listing-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.listing-name {
  font-size: 14px;
}


.listing-foot {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 8px;

  .seller {
    font-size: 12px;
    color: var(--color-text);
  }

  .time {
    font-size: 11px;
    color: var(--color-text-secondary);
  }

  .mini-btn {
    margin-left: auto;
  }
}

.rarity-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 0 6px;
  border: 1px solid currentColor;
  border-radius: 10px;
}

.clue-index-pill {
  color: var(--color-accent, #58a6ff);
  border-color: var(--color-accent, #58a6ff);
  font-weight: 600;
}

.type-pill {
  font-size: 10px;
  font-weight: 600;
  padding: 0 6px;
  border-radius: 10px;

  &.sell {
    background: rgba(255, 193, 7, 0.15);
    color: #e6a817;
    border: 1px solid rgba(255, 193, 7, 0.3);
  }

  &.barter {
    background: rgba(108, 117, 125, 0.1);
    color: var(--color-text-secondary);
    border: 1px solid var(--color-border);
  }
}

.price-tag {
  font-size: 13px;
  font-weight: 700;
  color: #e6a817;
}

.uses {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.mini-btn {
  font-size: 11px;
  padding: 2px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-surface);
  color: var(--color-text);
  cursor: pointer;

  &.primary {
    border-color: var(--color-primary);
    color: var(--color-primary);
  }

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

/* ── 上架弹窗：模式切换 ── */
.mode-toggle {
  display: flex;
  gap: 8px;
  margin: 10px 0;
}

.mode-btn {
  flex: 1;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  cursor: pointer;
  transition: all 0.15s;

  &.active {
    border-color: var(--color-primary);
    color: var(--color-primary);
    background: var(--color-surface);
  }
}

.sell-price-section {
  margin: 10px 0;
  padding: 10px 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.price-label {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  font-weight: 600;
  color: var(--color-text);
  margin-bottom: 6px;
}

.price-range-hint {
  font-size: 11px;
  font-weight: 400;
  color: var(--color-text-secondary);
}

.price-input {
  width: 100%;
  padding: 6px 10px;
  font-size: 14px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  color: var(--color-text);

  &:focus {
    outline: none;
    border-color: var(--color-primary);
  }
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

.modal-card {
  width: 100%;
  max-width: 480px;
  max-height: 80vh;
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
  padding: 12px 16px;
  border-bottom: 1px solid var(--color-border);
}

.modal-title {
  margin: 0;
  font-size: 15px;
  color: var(--color-text);
}

.modal-close {
  border: none;
  background: transparent;
  font-size: 16px;
  color: var(--color-text-secondary);
  cursor: pointer;

  &:hover {
    color: var(--color-text);
  }
}

.modal-body {
  padding: 14px 16px;
  overflow-y: auto;
}

.muted {
  color: var(--color-text-secondary);

  &.small {
    font-size: 12px;
  }
}

.pick-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 10px;
}

.pick-item {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  color: var(--color-text);
  text-align: left;
}

.pick-item-info {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  flex: 1;
  min-width: 0;
}

.pick-item-actions {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-left: auto;
}

.pick-item-desc {
  flex-basis: 100%;
  min-width: 0;
  margin: 0;
  color: var(--color-text-secondary);
  font-size: 11px;
  line-height: 1.5;
  overflow-wrap: anywhere;
  white-space: normal;
}

.list-qty-label {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 11px;
  color: var(--color-text-secondary);
}

.list-qty-input {
  width: 48px;
  font-size: 12px;
  padding: 3px 4px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  color: var(--color-text);
  text-align: center;
}

.pick-list-btn {
  font-size: 12px;
  padding: 4px 14px;
  border: 1px solid var(--color-primary);
  border-radius: 8px;
  background: var(--color-primary);
  color: #fff;
  cursor: pointer;
  white-space: nowrap;

  &:hover:not(:disabled) {
    opacity: 0.9;
  }

  &:disabled {
    opacity: 0.5;
    cursor: not-allowed;
  }
}

.sub-title {
  margin: 12px 0 8px;
  font-size: 13px;
  color: var(--color-text);

  &:first-child {
    margin-top: 0;
  }
}

.my-listing-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 10px;
  margin-bottom: 6px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;

  .mini-btn {
    margin-left: auto;
  }

  &.history {
    opacity: 0.85;
  }
}

.trade-note {
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* ── 出价相关样式 ── */
.listing-want {
  margin: 6px 0 0;
  font-size: 12px;
  line-height: 1.5;
  color: #b8860b;
  font-style: italic;
}

.want-note-section {
  margin: 10px 0;
  padding: 10px 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.want-note-display {
  margin: 0 0 10px;
  padding: 8px 12px;
  background: rgba(184, 134, 11, 0.08);
  border: 1px solid rgba(184, 134, 11, 0.2);
  border-radius: 8px;
  font-size: 13px;
  color: #b8860b;
}

.want-note-inline {
  font-size: 12px;
  color: #b8860b;
  font-style: italic;
}

.offers-listing-info {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
  padding-bottom: 10px;
  border-bottom: 1px solid var(--color-border);

  .mini-btn {
    margin-left: auto;
  }
}

.edit-note-section {
  margin: 8px 0 12px;
  padding: 10px 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.edit-note-actions {
  display: flex;
  gap: 8px;
  margin-top: 8px;
}

.offer-row {
  padding: 10px 12px;
  margin-bottom: 6px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
}

.offer-info {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
}

.offer-desc {
  flex-basis: 100%;
  min-width: 0;
  font-size: 11px;
  line-height: 1.5;
  color: var(--color-text-secondary);
  overflow-wrap: anywhere;
  white-space: normal;
}

.offer-meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  font-size: 12px;

  .mini-btn {
    margin-left: auto;
  }
}

.offer-buyer {
  color: var(--color-text);
}

.offer-time {
  font-size: 11px;
  color: var(--color-text-secondary);
}

.offer-status {
  font-size: 11px;
  font-weight: 600;

  &.pending { color: #b8860b; }
  &.accepted { color: #2e7d32; }
  &.rejected { color: #c62828; }
  &.cancelled { color: var(--color-text-secondary); }
}

.my-offer-row {
  padding: 10px 12px;
  margin-bottom: 6px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;

  .offer-label {
    font-size: 12px;
    color: var(--color-text-secondary);
  }

  .offer-listing-info {
    margin-top: 4px;
    display: flex;
    align-items: center;
    gap: 6px;
    font-size: 13px;
  }
}

.buy-qty-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0 8px;
  font-size: 13px;
}

.qty-step-btn {
  width: 28px;
  height: 28px;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  color: var(--color-text);
  font-size: 16px;
  line-height: 1;
  cursor: pointer;

  &:hover:not(:disabled) {
    border-color: var(--color-primary);
    color: var(--color-primary);
  }

  &:disabled {
    opacity: 0.45;
    cursor: not-allowed;
  }
}

.buy-total {
  margin: 8px 0 12px;
  font-size: 14px;
}

.unit-price {
  font-size: 11px;
  opacity: 0.85;
}

.buy-actions {
  display: flex;
  gap: 8px;
}

/* ── 移动端适配 ── */
@media (max-width: 640px) {
  .modal-overlay {
    padding: 8px;
  }

  .modal-card {
    max-height: 88vh;
  }
}
/* 驻地个人市场：公开柜台、清晰账单与淡蓝交易反馈。 */
.market-panel { --market-blue: color-mix(in srgb, #5d87b3 76%, var(--color-primary)); }
.market-toolbar {
  position: relative;
  overflow: hidden;
  padding: 14px;
  border: 1px solid color-mix(in srgb, var(--market-blue) 24%, var(--color-border));
  border-radius: 14px;
  background: linear-gradient(120deg, color-mix(in srgb, var(--market-blue) 7%, var(--color-surface)), var(--color-surface));
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 4%, transparent), 0 8px 22px rgba(0, 0, 0, 0.08);
}

.listing-offer-alert {
  display: inline-flex;
  align-items: center;
  padding: 2px 8px;
  border: 1px solid color-mix(in srgb, #e45568 42%, var(--color-border));
  border-radius: 999px;
  background: #391a22;
  background: color-mix(in srgb, #e45568 13%, var(--color-bg));
  color: #f06a7c;
  font-size: 10px;
  font-weight: 700;
  box-shadow: 0 0 0 2px color-mix(in srgb, #e45568 5%, transparent);
}
.market-toolbar::before {
  content: 'TRADING COUNTER';
  position: absolute;
  right: 12px;
  top: 7px;
  color: color-mix(in srgb, var(--market-blue) 28%, transparent);
  font-size: 8px;
  font-weight: 800;
  letter-spacing: 0.15em;
}
.market-search-input {
  border-color: color-mix(in srgb, var(--market-blue) 20%, var(--color-border));
  background: var(--color-bg);
}
.market-search-input:focus { border-color: var(--market-blue); box-shadow: 0 0 0 3px color-mix(in srgb, var(--market-blue) 10%, transparent); }
.chip { transition: color 0.16s, border-color 0.16s, background 0.16s; }
.chip.active { border-color: var(--market-blue); background: color-mix(in srgb, var(--market-blue) 10%, var(--color-bg)); color: var(--market-blue); }
.fee-hint,
.slot-bar {
  border: 1px solid color-mix(in srgb, var(--market-blue) 16%, var(--color-border));
  border-radius: 10px;
  background: color-mix(in srgb, var(--market-blue) 4%, var(--color-bg));
}
.listing-grid { gap: 11px; }
.listing-card {
  position: relative;
  overflow: hidden;
  border-color: color-mix(in srgb, var(--market-blue) 16%, var(--color-border));
  border-radius: 12px;
  background: linear-gradient(110deg, color-mix(in srgb, var(--market-blue) 4%, var(--color-bg)), var(--color-bg));
  transition: transform 0.18s ease, border-color 0.18s ease, box-shadow 0.18s ease;
  display: flex;
  min-width: 0;
  flex-direction: column;
}
.listing-card::before {
  content: '';
  position: absolute;
  top: 0;
  left: 12px;
  width: 32px;
  height: 2px;
  background: var(--market-blue);
}
.listing-card:hover {
  transform: translateY(-2px);
  border-color: color-mix(in srgb, var(--market-blue) 48%, var(--color-border));
  box-shadow: 0 9px 22px rgba(46, 78, 108, 0.12);
}
.listing-head { gap: 4px 6px; align-content: flex-start; }
.listing-name {
  min-width: 0;
  max-width: 100%;
  white-space: normal;
  overflow-wrap: anywhere;
}
.listing-description { margin: 6px 0 8px; }
.listing-want {
  display: block;
  min-height: 0;
  overflow: visible;
  overflow-wrap: anywhere;
}
.listing-card:focus { outline: 2px solid color-mix(in srgb, var(--market-blue) 35%, transparent); outline-offset: 2px; }
.listing-foot {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  align-items: center;
  gap: 4px 8px;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px dashed color-mix(in srgb, var(--market-blue) 16%, var(--color-border));
}
.listing-foot .seller {
  grid-column: 1;
  grid-row: 1;
  overflow-wrap: anywhere;
}
.listing-foot .price-tag {
  grid-column: 2;
  grid-row: 1;
  text-align: right;
}
.listing-foot .unit-price { display: block; font-size: 10px; }
.listing-foot .time {
  grid-column: 1;
  grid-row: 2;
  min-width: 0;
  font-size: 10px;
}
.listing-actions {
  display: flex;
  min-width: 0;
  grid-column: 2;
  grid-row: 2;
  flex-wrap: wrap;
  justify-content: flex-end;
  gap: 4px;
}
.listing-foot .mini-btn {
  display: inline-flex;
  min-width: 56px;
  max-width: 100%;
  min-height: 30px;
  align-items: center;
  justify-content: center;
  margin-left: 0;
  padding: 4px 8px;
  white-space: normal;
}
.price-tag { color: var(--market-blue); }
.mini-btn.primary { background: var(--market-blue); border-color: var(--market-blue); color: #fff; }
.modal-card { border-color: color-mix(in srgb, var(--market-blue) 25%, var(--color-border)); }
.modal-head { background: color-mix(in srgb, var(--market-blue) 6%, var(--color-surface)); }

@media (max-width: 640px) {
  .market-toolbar { padding: 12px; }
  .market-toolbar::before { display: none; }
  .toolbar-main { align-items: stretch; }
  .toolbar-actions { width: 100%; }
  .toolbar-actions button { flex: 1; min-width: 0; min-height: 40px; padding-inline: 8px; }
  .listing-card:hover { transform: none; box-shadow: none; }
  .listing-card:active { transform: scale(0.99); }
  .listing-card { min-height: 0; }
  .listing-foot { align-items: stretch; }
  .listing-foot .mini-btn { width: auto; min-width: 84px; min-height: 36px; }
}
@media (prefers-reduced-motion: reduce) {
  .listing-card,
  .market-action-button { transition: none; }
}
@media (max-width: 640px) {
  .market-toolbar { padding: 10px; border-radius: 10px; background: var(--color-surface); box-shadow: none; }
  .search-row { max-width: none; }
  .toolbar-main { flex-direction: column; gap: 10px; }
  .filter-chips { align-items: stretch; flex-direction: column; gap: 4px; }
  .filter-group { gap: 3px; }
  .filter-label { width: 26px; margin-right: 2px; flex-shrink: 0; }
  .chip { flex: 0 1 auto; min-height: 28px; padding: 4px 7px; border-color: transparent; border-radius: 6px; background: transparent; font-size: 11px; text-align: center; }
  .chip.active { border-color: var(--chip-color, var(--market-blue)); color: var(--chip-color, var(--market-blue)); }
  .toolbar-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px; padding-top: 8px; border-top: 1px solid var(--color-border); }
  .toolbar-actions button { width: 100%; min-height: 34px; gap: 5px; padding: 6px 8px; border-radius: 7px; font-weight: 600; white-space: normal; box-shadow: none; }
  .toolbar-actions .market-action-button--secondary { background: var(--color-bg); border-color: transparent; color: var(--color-text-secondary); }
  .toolbar-actions .market-action-button--primary { background: var(--market-blue); color: #fff; border-color: transparent; }
  .market-action-icon { width: 16px; height: 16px; flex-basis: 16px; background: transparent; }
  .market-action-icon--plus { color: inherit; }
  .listing-grid { grid-template-columns: minmax(0, 1fr); }
  .listing-card { padding: 10px 12px; border-radius: 10px; background: var(--color-surface); }
  .listing-head { min-height: 0; gap: 5px; }
  .listing-name { white-space: normal; overflow-wrap: anywhere; }
  .listing-want { font-size: 12px; line-height: 1.55; }
  .listing-foot { align-items: center; gap: 4px 8px; margin-top: auto; }
  .listing-foot .mini-btn { flex: 0 1 auto; min-width: 56px; min-height: 30px; padding: 4px 9px; font-size: 12px; white-space: normal; }
  .slot-bar { align-items: center; gap: 6px; padding: 8px 10px; font-size: 11px; }
  .slot-bar .btn-expand { min-height: 30px; margin-left: auto; padding: 4px 8px; font-size: 11px; white-space: normal; }
  .modal-head { padding: 8px 12px; gap: 8px; }
  .modal-body { min-width: 0; padding: 14px; }
}
</style>
