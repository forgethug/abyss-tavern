<template>
  <div class="starlight-hall" :class="{ 'ceremony-awake': ceremonyMoment }">
    <div class="hall-atmosphere" aria-hidden="true">
      <i v-for="index in 11" :key="index" :class="`star star-${index}`" />
      <span class="light-haze haze-left" />
      <span class="light-haze haze-right" />
    </div>

    <header class="hall-hero">
      <div class="hall-crest" aria-hidden="true">
        <span class="crest-ring crest-ring-outer" />
        <span class="crest-ring crest-ring-inner" />
        <b>✦</b>
      </div>
      <div class="hall-heading">
        <span class="hall-eyebrow">
          {{ payload?.hall.eyebrow || '酒馆二层 · 长夜尽头的灯火' }}
        </span>
        <h2>{{ payload?.hall.name || '星灯礼堂' }}</h2>
        <p>
          {{ payload?.hall.description || '酒馆把值得纪念的日子写进灯火。今夜有宴时，礼堂会为赴约者留一张席位。' }}
        </p>
        <div class="hall-time-row">
          <span>{{ payload?.hall.local_date_label || '礼簿正在翻页' }}</span>
          <span>{{ payload?.hall.timezone_label || '酒馆时刻 · UTC+8' }}</span>
        </div>
      </div>
      <div class="hero-lantern" aria-hidden="true">
        <span class="lantern-crown" />
        <span class="lantern-glass"><i /></span>
        <span class="lantern-tail" />
      </div>
    </header>

    <nav class="hall-doors" role="tablist" aria-label="星灯礼堂活动">
      <button
        type="button"
        role="tab"
        class="hall-door birthday-door"
        :class="{ active: selectedWing === 'birthday' }"
        :aria-selected="selectedWing === 'birthday'"
        @click="selectedWing = 'birthday'"
      >
        <span class="door-icon" aria-hidden="true">✧</span>
        <span class="door-copy">
          <small>STARLIGHT OPEN HOUSE</small>
          <strong>星灯生日宴</strong>
          <em>寿星开桌，旅人自由串门</em>
        </span>
        <span class="door-status">{{ birthdayEvent?.status_label || '读取礼簿' }}</span>
      </button>
      <button
        type="button"
        role="tab"
        class="hall-door tournament-door"
        :class="{ active: selectedWing === 'tournament' }"
        :aria-selected="selectedWing === 'tournament'"
        @click="selectedWing = 'tournament'"
      >
        <span class="door-icon" aria-hidden="true">♠</span>
        <span class="door-copy">
          <small>TAVERN STORY EVENT</small>
          <strong>命运牌桌</strong>
          <em>报名、入桌，等待牌局揭晓</em>
        </span>
        <span class="door-status">赛事入口</span>
      </button>
      <button
        type="button"
        role="tab"
        class="hall-door reservation-door"
        aria-selected="false"
        aria-disabled="true"
        title="筹备中：目前不会接受预约，也不会收取租金"
        disabled
      >
        <span class="door-icon" aria-hidden="true">⌂</span>
        <span class="door-copy">
          <small>PRIVATE HALL LEASE</small>
          <strong>礼堂包场契</strong>
          <em>未来可支付租金，自定主题、来客与文字玩法</em>
        </span>
        <span class="door-status">暂未开放</span>
      </button>
    </nav>

    <section
      v-if="selectedWing === 'birthday'"
      class="birthday-wing"
      role="tabpanel"
      aria-live="polite"
    >
      <div v-if="loading && !payload" class="hall-state-card">
        <span class="state-orbit" aria-hidden="true">✦</span>
        <strong>侍者正在翻开星灯礼簿</strong>
        <p>请稍候，今夜的日期与席位正在核对。</p>
      </div>
      <div v-else-if="loadError || !birthdayEvent" class="hall-state-card error-state">
        <span aria-hidden="true">◇</span>
        <strong>礼簿的灯火暂时暗了</strong>
        <p>没有任何日期被改动。可以重新请侍者翻开礼簿。</p>
        <button type="button" class="ghost-button" :disabled="loading" @click="loadHall">
          重新翻开礼簿
        </button>
      </div>

      <template v-else>
        <article class="birthday-stage" :class="`status-${birthdayEvent.status}`">
          <div class="birthday-ribbon">{{ birthdayEvent.status_label }}</div>
          <div class="candle-row" aria-hidden="true">
            <span v-for="index in 3" :key="index" class="candle">
              <i class="flame" />
              <i class="wick" />
            </span>
          </div>
          <div class="birthday-stage-copy">
            <span class="event-eyebrow">{{ birthdayEvent.eyebrow }}</span>
            <h3>{{ birthdayEvent.title }}</h3>
            <p>{{ birthdayEvent.description }}</p>
          </div>
          <div class="event-rules">
            <span v-for="rule in birthdayEvent.rules" :key="rule">{{ rule }}</span>
          </div>
        </article>

        <article
          v-if="birthdayEvent.status === 'spectator'"
          class="hall-state-card"
        >
          <span class="state-orbit" aria-hidden="true">◇</span>
          <strong>今夜以旅人身份来庆生</strong>
          <p>你可以到这个区服已开放的生日桌串门、互动和留下祝福。个人生日登记、开宴与领取礼物仍在当前存活冒险者所在区操作。</p>
        </article>

        <article v-if="showBirthdayForm" class="birthday-ledger">
          <div class="ledger-heading">
            <span class="ledger-mark" aria-hidden="true">✦</span>
            <div>
              <small>THE STAR LANTERN LEDGER</small>
              <h4>{{ birthdayEvent.birthday ? '更正礼簿日期' : '在礼簿上留下你的日期' }}</h4>
              <p>只需要月与日。酒馆不询问年份；只有生日当晚由你主动开放桌子，其他玩家才会看见今夜有寿星在此。</p>
            </div>
          </div>
          <div class="date-fields">
            <label>
              <span>月份</span>
              <select v-model.number="birthdayMonth" :disabled="acting">
                <option v-for="month in 12" :key="month" :value="month">
                  {{ month }} 月
                </option>
              </select>
            </label>
            <span class="date-divider" aria-hidden="true">/</span>
            <label>
              <span>日期</span>
              <select v-model.number="birthdayDay" :disabled="acting">
                <option v-for="day in birthdayDays" :key="day" :value="day">
                  {{ day }} 日
                </option>
              </select>
            </label>
          </div>
          <div class="privacy-seal">
            <span aria-hidden="true">◇</span>
            <p>
              礼帖落印后，本年度日期会封存；下一自然年可以再次更正。
              2 月 29 日在非闰年会于 2 月 28 日开席。
            </p>
          </div>
          <div class="ledger-actions">
            <button
              type="button"
              class="ceremony-button"
              :disabled="acting"
              @click="saveBirthday"
            >
              {{ acting ? '礼簿落笔中…' : '写入星灯礼簿' }}
            </button>
            <button
              v-if="birthdayEvent.birthday"
              type="button"
              class="ghost-button"
              :disabled="acting"
              @click="birthdayFormOpen = false"
            >
              保留原日期
            </button>
          </div>
        </article>

        <article
          v-else-if="birthdayEvent.status === 'waiting'"
          class="birthday-countdown"
        >
          <div class="countdown-medallion">
            <span>距离开席</span>
            <strong>{{ birthdayEvent.days_until }}</strong>
            <small>天</small>
          </div>
          <div class="countdown-copy">
            <small>YOUR TABLE AWAITS</small>
            <h4>{{ birthdayEvent.observed_date_label }}，礼堂会为你亮灯</h4>
            <p>
              生辰已记作 {{ birthdayEvent.birthday?.label }}。在那一天回来，长桌、寿烛与一枚烛愿都会在这里等你。
            </p>
            <button
              v-if="birthdayEvent.birthday?.can_edit"
              type="button"
              class="ghost-button"
              @click="openBirthdayEditor"
            >
              更正礼簿日期
            </button>
          </div>
        </article>

        <article
          v-else-if="birthdayEvent.status === 'available'"
          class="wish-ceremony"
        >
          <div class="ceremony-invitation">
            <small>THE TABLE IS SET</small>
            <h4>{{ payload?.celebrant_name }}，今夜这张长桌为你留席</h4>
            <p>你的冒险者就是寿星在酒馆里的身影，只坐主位这一席。队友另列同行席，绝不会重复占名额。</p>
          </div>

          <section v-if="birthdayEvent.banquet" class="banquet-preview">
            <header class="banquet-heading">
              <div>
                <small>INVITATIONS DELIVERED</small>
                <h5>{{ birthdayEvent.banquet.source_label }}</h5>
              </div>
              <span>
                {{ birthdayEvent.banquet.guest_count }} 位同行者赴席
              </span>
            </header>
            <p class="invitation-copy">{{ birthdayEvent.banquet.invitation_copy }}</p>

            <div class="banquet-table-scene">
              <div class="seat-gallery" aria-label="今夜席位">
                <div class="seat-card celebrant-seat">
                  <span class="seat-medallion" aria-hidden="true">✦</span>
                  <small>{{ birthdayEvent.banquet.host.role_label }}</small>
                  <strong>{{ birthdayEvent.banquet.host.display_name }}</strong>
                  <em
                    v-if="birthdayEvent.banquet.host.adventurer_name
                      && birthdayEvent.banquet.host.adventurer_name !== birthdayEvent.banquet.host.player_name"
                  >玩家 · {{ birthdayEvent.banquet.host.player_name }}</em>
                </div>
                <div
                  v-for="(guest, index) in birthdayEvent.banquet.guests"
                  :key="`${guest.name}-${index}`"
                  class="seat-card guest-seat"
                >
                  <span class="seat-medallion" aria-hidden="true">◇</span>
                  <small>{{ guest.role_label }}</small>
                  <strong>{{ guest.name }}</strong>
                </div>
                <div
                  v-if="birthdayEvent.banquet.guests.length === 0"
                  class="seat-card tavern-seat"
                >
                  <span class="seat-medallion" aria-hidden="true">♨</span>
                  <small>酒馆陪席</small>
                  <strong>值夜侍者</strong>
                </div>
              </div>

              <section class="feast-theme-picker">
                <header>
                  <div>
                    <small>CHOOSE TONIGHT'S FEAST</small>
                    <strong>挑一席最像今晚的生日宴</strong>
                  </div>
                  <span>{{ birthdayEvent.banquet.feast_options.length }} 套菜单 · 只作文字纪念</span>
                </header>
                <div class="feast-theme-grid" role="radiogroup" aria-label="选择生日宴菜单">
                  <button
                    v-for="feast in birthdayEvent.banquet.feast_options"
                    :key="feast.key"
                    type="button"
                    role="radio"
                    class="feast-theme-card"
                    :class="{ selected: selectedFeastKey === feast.key }"
                    :aria-checked="selectedFeastKey === feast.key"
                    :disabled="acting"
                    @click="selectedFeastKey = feast.key"
                  >
                    <span class="feast-theme-sigil" aria-hidden="true">{{ feast.sigil }}</span>
                    <span>
                      <strong>{{ feast.label }}</strong>
                      <small>{{ feast.subtitle }}</small>
                    </span>
                    <em>{{ selectedFeastKey === feast.key ? '今夜上桌' : '翻看菜单' }}</em>
                  </button>
                </div>
                <p v-if="selectedFeastTheme" class="feast-theme-description">
                  <span aria-hidden="true">{{ selectedFeastTheme.sigil }}</span>
                  {{ selectedFeastTheme.description }}
                </p>
              </section>

              <div
                v-if="selectedFeastTheme"
                class="tabletop"
                :aria-label="`${selectedFeastTheme.label}菜单`"
              >
                <div class="table-runner" aria-hidden="true">
                  <i v-for="index in 5" :key="index">✦</i>
                </div>
                <div class="feast-grid">
                  <div
                    v-for="dish in selectedFeastTheme.dishes"
                    :key="dish.key"
                    class="feast-dish"
                  >
                    <span aria-hidden="true">{{ dish.sigil }}</span>
                    <div>
                      <small>{{ dish.course_label }}</small>
                      <strong>{{ dish.name }}</strong>
                      <p>{{ dish.description }}</p>
                    </div>
                  </div>
                </div>
              </div>
            </div>

            <footer class="host-promise">
              <span aria-hidden="true">♨</span>
              <p>{{ birthdayEvent.banquet.host_note }}</p>
            </footer>
          </section>

          <div class="wish-section-heading">
            <small>CHOOSE THE FINAL TOAST</small>
            <strong>把今年想听见的那句话交给寿烛</strong>
          </div>
          <div class="wish-grid" role="radiogroup" aria-label="选择烛愿">
            <button
              v-for="wish in birthdayEvent.wish_options"
              :key="wish.key"
              type="button"
              role="radio"
              class="wish-card"
              :class="{ selected: selectedWish === wish.key }"
              :aria-checked="selectedWish === wish.key"
              :disabled="acting"
              @click="selectedWish = wish.key"
            >
              <span class="wish-sigil" aria-hidden="true">{{ wish.sigil }}</span>
              <strong>{{ wish.label }}</strong>
              <p>{{ wish.verse }}</p>
            </button>
          </div>
          <label class="open-house-choice" :class="{ checked: openVisitorsOnAttend }">
            <input
              v-model="openVisitorsOnAttend"
              type="checkbox"
              :disabled="acting"
            >
            <span class="door-sign" aria-hidden="true">OPEN</span>
            <span>
              <strong>把门牌翻到“欢迎串门”</strong>
              <small>今夜其他玩家可以来坐一会儿、分点吃的、讲故事或留一句祝福。随时可以再收起门牌。</small>
            </span>
          </label>
          <button
            type="button"
            class="ceremony-button light-the-hall"
            :disabled="acting || !selectedWish || !selectedFeastTheme"
            @click="attendBirthday"
          >
            <span aria-hidden="true">✦</span>
            {{ acting ? '同席者正在落座，酒馆准备举杯…' : '请众人入席，点亮星灯' }}
          </button>
        </article>

        <template
          v-else-if="birthdayEvent.status === 'celebrated' && birthdayEvent.receipt"
        >
          <article class="banquet-afterglow">
            <header class="banquet-complete-heading">
              <span class="completion-sigil" aria-hidden="true">✦</span>
              <div>
                <small>THE LONG TABLE REMEMBERS</small>
                <h4>长桌已经开席，今夜有人与你同坐</h4>
                <p>
                  寿星主位只占一席，另有 {{ birthdayEvent.receipt.guest_count }} 位同行者赴约；
                  {{ birthdayEvent.receipt.feast_theme.label }}的 {{ birthdayEvent.receipt.feast.length }} 道菜已经端上桌。
                </p>
              </div>
            </header>

            <div class="afterglow-grid">
              <section class="afterglow-panel guest-roll">
                <div class="afterglow-title">
                  <small>GUEST ROLL</small>
                  <strong>今夜同席</strong>
                </div>
                <div class="guest-placecards">
                  <div class="placecard celebrant-placecard">
                    <span>✦</span>
                    <small>寿星主位</small>
                    <strong>{{ birthdayEvent.receipt.host.display_name }}</strong>
                    <em
                      v-if="birthdayEvent.receipt.host.adventurer_name
                        && birthdayEvent.receipt.host.adventurer_name !== birthdayEvent.receipt.host.player_name"
                    >玩家 · {{ birthdayEvent.receipt.host.player_name }}</em>
                  </div>
                  <div
                    v-for="(guest, index) in birthdayEvent.receipt.guests"
                    :key="`receipt-${guest.name}-${index}`"
                    class="placecard"
                  >
                    <span>◇</span>
                    <small>{{ guest.role_label }}</small>
                    <strong>{{ guest.name }}</strong>
                  </div>
                  <div
                    v-if="birthdayEvent.receipt.guests.length === 0"
                    class="placecard"
                  >
                    <span>♨</span>
                    <small>酒馆陪席</small>
                    <strong>值夜侍者</strong>
                  </div>
                </div>
              </section>

              <section class="afterglow-panel served-feast">
                <div class="afterglow-title">
                  <small>TONIGHT'S FEAST</small>
                  <strong>
                    {{ birthdayEvent.receipt.feast_theme.sigil }}
                    {{ birthdayEvent.receipt.feast_theme.label }}
                  </strong>
                  <em>{{ birthdayEvent.receipt.feast_theme.subtitle }}</em>
                  <p>{{ birthdayEvent.receipt.feast_theme.description }}</p>
                </div>
                <div class="served-dishes">
                  <div
                    v-for="dish in birthdayEvent.receipt.feast"
                    :key="`served-${dish.key}`"
                  >
                    <span>{{ dish.sigil }}</span>
                    <p><small>{{ dish.course_label }}</small>{{ dish.name }}</p>
                  </div>
                </div>
              </section>
            </div>

            <section class="tavern-blessing">
              <div class="host-medallion" aria-hidden="true">
                <span>♨</span>
              </div>
              <div class="blessing-copy">
                <small>THE TAVERN'S TOAST · 酒馆掌柜祝酒辞</small>
                <blockquote>“{{ birthdayEvent.receipt.tavern_blessing }}”</blockquote>
                <footer>— 深渊酒馆，谨以今夜炉火、长桌与满杯</footer>
              </div>
            </section>

            <section
              class="birthday-gift-card"
              :class="birthdayEvent.receipt.gift.status"
            >
              <div class="gift-cake" aria-hidden="true">
                <span>✦</span>
                <i class="cake-layer cake-top" />
                <i class="cake-layer cake-base" />
                <b>✧</b>
              </div>
              <div class="gift-copy">
                <small>THE TAVERN'S BIRTHDAY GIFT · 酒馆赠礼</small>
                <h5>{{ birthdayEvent.receipt.gift.name }}</h5>
                <p>{{ birthdayEvent.receipt.gift.description }}</p>
                <div class="gift-seals">
                  <span>{{ birthdayEvent.receipt.gift.rarity_label }}</span>
                  <span>{{ birthdayEvent.receipt.gift.immutable_label }}</span>
                  <span v-if="birthdayEvent.receipt.gift.destination_label">
                    已送入{{ birthdayEvent.receipt.gift.destination_label }}
                  </span>
                </div>
                <strong>{{ birthdayEvent.receipt.gift.status_label }}</strong>
              </div>
              <button
                v-if="birthdayEvent.receipt.gift.can_claim"
                type="button"
                class="ceremony-button gift-claim-button"
                :disabled="claimingGift"
                @click="claimBirthdayGift"
              >
                {{ claimingGift ? '掌柜正在递来礼盒…' : '郑重收下生日蛋糕' }}
              </button>
            </section>

            <section class="own-table-door">
              <span
                class="door-sign"
                :class="{ open: birthdayEvent.receipt.open_table.is_open }"
                aria-hidden="true"
              >{{ birthdayEvent.receipt.open_table.is_open ? 'OPEN' : 'QUIET' }}</span>
              <div>
                <small>YOUR TABLE, YOUR DOOR</small>
                <strong>
                  {{ birthdayEvent.receipt.open_table.is_open ? '门牌正朝向“欢迎串门”' : '这张桌子目前只留给你与同行者' }}
                </strong>
                <p>
                  已有 {{ birthdayEvent.receipt.open_table.visitor_count }} 段串门片刻留在桌边。
                  开放只影响文字互动，不公开你的出生年份，也不产生任何数值效果。
                </p>
              </div>
              <div class="own-table-actions">
                <button
                  v-if="birthdayEvent.receipt.open_table.token"
                  type="button"
                  class="ghost-button"
                  :disabled="visitsLoading"
                  @click="openOwnTableLedger"
                >翻看串门小记</button>
                <button
                  v-if="birthdayEvent.receipt.open_table.can_manage"
                  type="button"
                  class="ghost-button"
                  :disabled="togglingOpenTable"
                  @click="toggleOpenTable(!birthdayEvent.receipt.open_table.is_open)"
                >
                  {{ togglingOpenTable
                    ? '侍者正在翻门牌…'
                    : birthdayEvent.receipt.open_table.is_open ? '收起门牌' : '欢迎旅人串门' }}
                </button>
                <span v-else class="door-closed-note">生日夜结束后，门牌已封存</span>
              </div>
            </section>
          </article>

          <article class="ceremony-receipt">
            <div class="receipt-corners" aria-hidden="true">
              <i /><i /><i /><i />
            </div>
            <div class="receipt-seal" aria-hidden="true">✦</div>
            <small>STAR LANTERN RECORD · {{ birthdayEvent.receipt.year }}</small>
            <h4>{{ birthdayEvent.receipt.celebrant_name }} 的生日礼帖</h4>
            <p class="receipt-date">
              生辰 {{ birthdayEvent.receipt.birthday.label }} · 今岁已赴约
            </p>
            <blockquote>
              <span aria-hidden="true">{{ birthdayEvent.receipt.wish.sigil }}</span>
              <strong>{{ birthdayEvent.receipt.wish.label }}</strong>
              <p>{{ birthdayEvent.receipt.wish.verse }}</p>
            </blockquote>
            <footer>
              <span>礼帖落印于 {{ formatDateTime(birthdayEvent.receipt.attended_at) }}</span>
              <span>同席名单、宴席与祝词均已封存；蛋糕作为独立纪念赠礼，不附带能力变化。</span>
            </footer>
          </article>
        </template>

        <section class="shared-birthday-hall">
          <header class="shared-hall-heading">
            <div>
              <small>TONIGHT'S OPEN TABLES</small>
              <h4>今夜亮着的生日桌</h4>
              <p>同一天有几位寿星，就可以亮起几张桌。你不必入队，也不用带礼物；挑一种方式过去坐坐就好。</p>
            </div>
            <button
              type="button"
              class="ghost-button compact-button"
              :disabled="openTablesLoading"
              @click="loadOpenTables(openTablesPagination.page)"
            >
              {{ openTablesLoading ? '侍者正在巡桌…' : '看看今夜还有谁' }}
            </button>
          </header>

          <div v-if="openTablesLoading && openTables.length === 0" class="open-tables-state">
            <span aria-hidden="true">✦</span>
            <p>侍者正沿着长桌查看哪些门牌朝向“欢迎串门”。</p>
          </div>
          <div v-else-if="openTablesError" class="open-tables-state error-state">
            <span aria-hidden="true">◇</span>
            <p>今夜的开放门牌暂时看不清，可以稍后重新巡桌。</p>
          </div>
          <div v-else-if="openTables.length === 0" class="open-tables-state">
            <span aria-hidden="true">◌</span>
            <strong>今夜还没有门牌翻到“欢迎串门”</strong>
            <p>生日宴仍可以安静进行；是否把桌子开放给旅人，始终由寿星自己决定。</p>
          </div>
          <template v-else>
            <div class="open-table-grid">
              <article
                v-for="table in openTables"
                :key="table.token"
                class="open-table-card"
                :class="{ mine: table.is_mine, active: activeVisitTable?.token === table.token }"
              >
                <div class="table-card-lantern" aria-hidden="true">✦</div>
                <header>
                  <small>{{ table.is_mine ? 'YOUR OPEN TABLE' : 'WELCOME, TRAVELER' }}</small>
                  <h5>{{ table.host.display_name }} 的生日桌</h5>
                  <p
                    v-if="table.host.adventurer_name
                      && table.host.adventurer_name !== table.host.player_name"
                  >寿星玩家 · {{ table.host.player_name }}</p>
                </header>
                <div class="table-card-counts">
                  <span>{{ table.companion_count }} 位同行者</span>
                  <span>{{ table.visitor_count }} 段串门片刻</span>
                </div>
                <blockquote>
                  <span>{{ table.wish.sigil }}</span>
                  <p><strong>{{ table.wish.label }}</strong>{{ table.wish.verse }}</p>
                </blockquote>
                <div class="table-feast-theme">
                  <span aria-hidden="true">{{ table.feast_theme.sigil }}</span>
                  <p>
                    <strong>{{ table.feast_theme.label }}</strong>
                    <small>{{ table.feast_theme.subtitle }}</small>
                  </p>
                </div>
                <div class="table-dish-chips" aria-label="桌上菜肴">
                  <span v-for="dish in table.feast" :key="`${table.token}-${dish.key}`">
                    {{ dish.sigil }} {{ dish.name }}
                  </span>
                </div>
                <button
                  type="button"
                  class="ghost-button table-visit-button"
                  :disabled="visitsLoading"
                  @click="openVisitTable(table)"
                >
                  {{ table.is_mine ? '回到我的桌边' : '过去坐一会儿' }}
                </button>
              </article>
            </div>

            <nav
              v-if="openTablesPagination.pages > 1"
              class="hall-pagination"
              aria-label="生日桌分页"
            >
              <button
                type="button"
                class="ghost-button"
                :disabled="!openTablesPagination.has_prev || openTablesLoading"
                @click="loadOpenTables(openTablesPagination.page - 1)"
              >上一排长桌</button>
              <span>{{ openTablesPagination.page }} / {{ openTablesPagination.pages }}</span>
              <button
                type="button"
                class="ghost-button"
                :disabled="!openTablesPagination.has_next || openTablesLoading"
                @click="loadOpenTables(openTablesPagination.page + 1)"
              >下一排长桌</button>
            </nav>
          </template>

        </section>
      </template>
    </section>

    <section v-else class="tournament-wing" role="tabpanel">
      <div class="tournament-intro">
        <span class="table-emblem" aria-hidden="true">♠</span>
        <div>
          <small>THE EAST WING · FATE TABLE</small>
          <h3>穿过东侧拱门，牌局的喧声就在前方</h3>
          <p>这里沿用原有报名、抽签、签到与赛事记录；星灯礼堂不会改写任何牌桌规则。</p>
        </div>
      </div>
      <TavernActivityPanel />
    </section>

    <Teleport to="body">
      <Transition name="parlor-fade">
        <div
          v-if="activeVisitTable"
          class="visit-parlor-overlay s1-mobile-surface s1-mobile-overlay"
          @click.self="closeVisitTable"
        >
          <article
            ref="visitParlorDialog"
            class="visit-parlor s1-mobile-dialog"
            :class="{ 'owner-parlor': activeVisitTable.is_mine }"
            role="dialog"
            aria-modal="true"
            aria-labelledby="visit-parlor-title"
            tabindex="-1"
          >
            <div class="parlor-atmosphere" aria-hidden="true">
              <i v-for="index in 7" :key="`parlor-star-${index}`" />
            </div>

            <header class="visit-parlor-heading s1-mobile-dialog-header">
              <span class="parlor-heading-sigil" aria-hidden="true">
                {{ activeVisitTable.is_mine ? '✦' : '◇' }}
              </span>
              <div>
                <small>{{ activeVisitTable.is_mine ? 'THE HOST’S TABLESIDE JOURNAL' : 'PULL UP A CHAIR' }}</small>
                <div class="parlor-title-row">
                  <h5 id="visit-parlor-title">{{ activeVisitTable.host.display_name }} 的桌边</h5>
                  <span>{{ activeVisitTable.is_mine ? '寿星主位' : '来客席' }}</span>
                </div>
                <p v-if="activeVisitTable.is_mine">
                  串门小记已经在眼前翻开。你可以留在主位，慢慢看每位旅人怎样坐下、分食与祝愿。
                </p>
                <p v-else>
                  先挑一种今晚想做的事，再自由留一句话。坐多久都可以，离席也不必告别。
                </p>
              </div>
              <button
                type="button"
                class="parlor-close"
                aria-label="离开这张桌"
                @click="closeVisitTable"
              >×</button>
            </header>

            <div class="visit-parlor-body s1-mobile-dialog-body">
              <section class="parlor-table-summary">
                <div class="parlor-wish-copy">
                  <span aria-hidden="true">{{ activeVisitTable.wish.sigil }}</span>
                  <div>
                    <small>THE WISH AT THIS TABLE · {{ activeVisitTable.visitor_count }} 段串门片刻</small>
                    <strong>{{ activeVisitTable.wish.label }}</strong>
                    <p>{{ activeVisitTable.wish.verse }}</p>
                  </div>
                </div>
                <div class="parlor-feast-preview">
                  <small>
                    {{ activeVisitTable.feast_theme.sigil }}
                    {{ activeVisitTable.feast_theme.label }} · 随意分食
                  </small>
                  <div>
                    <span
                      v-for="dish in activeVisitTable.feast"
                      :key="`parlor-${activeVisitTable.token}-${dish.key}`"
                    >{{ dish.sigil }} {{ dish.name }}</span>
                  </div>
                </div>
              </section>

              <div
                class="visit-parlor-layout"
                :class="{ 'owner-view': activeVisitTable.is_mine }"
              >
                <section v-if="!activeVisitTable.is_mine" class="visit-composer">
                  <header class="visit-composer-heading">
                    <div>
                      <small>CHOOSE A TABLESIDE MOMENT</small>
                      <strong>今晚想怎么串门？</strong>
                      <p>有吃的、有闲聊，也有安静陪伴。所有选项都只会写成一段文字。</p>
                    </div>
                    <span>{{ visitActions.length }} 种</span>
                  </header>

                  <div class="visit-action-groups" role="tablist" aria-label="串门方式分类">
                    <button
                      v-for="group in visitActionGroups"
                      :key="group.key"
                      type="button"
                      role="tab"
                      :class="{ selected: selectedVisitActionGroup === group.key }"
                      :aria-selected="selectedVisitActionGroup === group.key"
                      :disabled="visiting"
                      @click="selectVisitActionGroup(group.key)"
                    >
                      <span aria-hidden="true">{{ group.sigil }}</span>
                      <strong>{{ group.label }}</strong>
                      <small>{{ group.description }}</small>
                    </button>
                  </div>

                  <div
                    class="visit-action-grid"
                    role="radiogroup"
                    :aria-label="selectedVisitActionGroupLabel"
                  >
                    <button
                      v-for="action in visibleVisitActions"
                      :key="action.key"
                      type="button"
                      role="radio"
                      class="visit-action-card"
                      :class="{ selected: selectedVisitAction === action.key }"
                      :aria-checked="selectedVisitAction === action.key"
                      :disabled="visiting"
                      @click="selectVisitAction(action)"
                    >
                      <span>{{ action.sigil }}</span>
                      <strong>{{ action.label }}</strong>
                      <small>{{ action.description }}</small>
                    </button>
                  </div>

                  <label class="visit-message-field">
                    <span>再说一句自己的话（可选）</span>
                    <textarea
                      v-model="visitMessage"
                      :maxlength="visitMessageMaxLength"
                      :disabled="visiting"
                      rows="3"
                      placeholder="例如：生日快乐。下次路过这里，我还来替你添一杯热饮。"
                    />
                    <small>{{ visitMessage.length }} / {{ visitMessageMaxLength }}</small>
                  </label>

                  <footer class="visit-composer-footer">
                    <p>若已有归属冒险者，小记会以其名字落笔；不消耗食物或道具，也不改变任何角色状态。</p>
                    <button
                      type="button"
                      class="ceremony-button"
                      :disabled="visiting || !selectedVisitAction"
                      @click="sendTableVisit"
                    >
                      {{ visitSubmitLabel }}
                    </button>
                  </footer>
                </section>

                <section class="visit-ledger" aria-live="polite">
                  <div class="visit-ledger-title">
                    <div>
                      <small>TABLESIDE MOMENTS</small>
                      <strong>{{ activeVisitTable.is_mine ? '今夜收到的串门小记' : '桌边刚刚发生的事' }}</strong>
                    </div>
                    <span>{{ visitsPagination.total }} 段</span>
                  </div>
                  <div v-if="visitsLoading" class="visit-empty">
                    <span aria-hidden="true">✦</span>
                    正在把桌边小记翻到第一页…
                  </div>
                  <div v-else-if="tableVisits.length === 0" class="visit-empty">
                    <span aria-hidden="true">◇</span>
                    还没有人留下文字。第一把椅子仍在等一位旅人拉开。
                  </div>
                  <div v-else class="visit-timeline">
                    <article
                      v-for="(visit, index) in tableVisits"
                      :key="`${visit.created_at}-${index}`"
                    >
                      <span class="visit-sigil" aria-hidden="true">{{ visit.action.sigil }}</span>
                      <div>
                        <div class="visit-moment-meta">
                          <small>{{ formatDateTime(visit.created_at) }}</small>
                          <span>
                            {{ visit.action.group_sigil || visit.action.sigil }}
                            {{ visit.action.label }}
                          </span>
                        </div>
                        <p>{{ visit.narrative }}</p>
                        <blockquote v-if="visit.message">“{{ visit.message }}”</blockquote>
                      </div>
                    </article>
                  </div>
                  <nav
                    v-if="visitsPagination.pages > 1"
                    class="hall-pagination compact-pagination"
                    aria-label="串门记录分页"
                  >
                    <button
                      type="button"
                      class="ghost-button"
                      :disabled="!visitsPagination.has_prev || visitsLoading"
                      @click="loadTableVisits(visitsPagination.page - 1)"
                    >较新的片刻</button>
                    <span>{{ visitsPagination.page }} / {{ visitsPagination.pages }}</span>
                    <button
                      type="button"
                      class="ghost-button"
                      :disabled="!visitsPagination.has_next || visitsLoading"
                      @click="loadTableVisits(visitsPagination.page + 1)"
                    >更早的片刻</button>
                  </nav>
                </section>
              </div>
            </div>
          </article>
        </div>
      </Transition>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from 'vue'
import api from '@/composables/useApi'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { formatDateTime } from '@/utils/time'
import TavernActivityPanel from './TavernActivityPanel.vue'

type HallWing = 'birthday' | 'tournament'
type BirthdayStatus = 'needs_birthday' | 'waiting' | 'available' | 'celebrated' | 'spectator'

interface BirthdayWishDisplay {
  label: string
  verse: string
  sigil: string
}

interface BirthdayWishOption extends BirthdayWishDisplay {
  key: string
}

interface VisitActionGroup {
  key: string
  label: string
  description: string
  sigil: string
}

interface VisitAction {
  key: string
  label: string
  description: string
  sigil: string
  group?: VisitActionGroup
}

interface CeremonyIdentity {
  display_name: string
  player_name: string
  adventurer_name: string | null
}

interface CeremonyHost extends CeremonyIdentity {
  role_label: string
}

interface BanquetGuest {
  name: string
  role_label: string
}

interface FeastDishChip {
  key: string
  name: string
  sigil: string
}

interface ServedFeastDish extends FeastDishChip {
  course_label: string
}

interface FeastDish extends ServedFeastDish {
  description: string
}

interface FeastThemeSummary {
  label: string
  subtitle: string
  sigil: string
}

interface FeastThemeDisplay extends FeastThemeSummary {
  description: string
}

interface FeastThemeOption extends FeastThemeDisplay {
  key: string
  dishes: FeastDish[]
}

interface BirthdayBanquet {
  source_label: string
  invitation_copy: string
  host: CeremonyHost
  guests: BanquetGuest[]
  guest_count: number
  feast_options: FeastThemeOption[]
  host_note: string
}

type BirthdayGiftStatus = 'delivered' | 'claimable' | 'waiting_for_adventurer'

interface BirthdayGift {
  name: string
  description: string
  sigil: string
  rarity_label: string
  immutable_label: string
  status: BirthdayGiftStatus
  status_label: string
  destination_label: string | null
  can_claim: boolean
}

interface BirthdayReceipt {
  year: number
  celebrant_name: string
  host: CeremonyIdentity
  birthday: {
    label: string
  }
  wish: BirthdayWishDisplay
  guests: BanquetGuest[]
  guest_count: number
  feast_theme: FeastThemeDisplay
  feast: ServedFeastDish[]
  tavern_blessing: string
  gift: BirthdayGift
  open_table: {
    token: string | null
    is_open: boolean
    can_manage: boolean
    visitor_count: number
  }
  attended_at: string
}

interface BirthdayEvent {
  title: string
  eyebrow: string
  description: string
  rules: string[]
  status: BirthdayStatus
  status_label: string
  birthday: null | {
    month: number
    day: number
    label: string
    can_edit: boolean
  }
  observed_date_label: string | null
  days_until: number | null
  wish_options: BirthdayWishOption[]
  visit_actions: VisitAction[]
  visit_policy: {
    message_max_length: number
  }
  banquet: BirthdayBanquet | null
  receipt: BirthdayReceipt | null
}

interface OpenBirthdayTable {
  token: string
  host: CeremonyIdentity
  wish: BirthdayWishDisplay
  companion_count: number
  visitor_count: number
  feast_theme: FeastThemeSummary
  feast: FeastDishChip[]
  is_mine: boolean
}

interface CeremonyVisitAction {
  label: string
  sigil: string
  group_sigil: string
}

interface CeremonyVisit {
  action: CeremonyVisitAction
  narrative: string
  message: string | null
  created_at: string
}

interface PageNavigation {
  page: number
  pages: number
  has_next: boolean
  has_prev: boolean
}

interface VisitPagination extends PageNavigation {
  total: number
}

interface CeremonyHallPayload {
  hall: {
    name: string
    eyebrow: string
    description: string
    local_date_label: string
    timezone_label: string
  }
  celebrant_name: string
  birthday_event: BirthdayEvent
}

function emptyPageNavigation(): PageNavigation {
  return {
    page: 1,
    pages: 1,
    has_next: false,
    has_prev: false,
  }
}

function emptyVisitPagination(): VisitPagination {
  return {
    ...emptyPageNavigation(),
    total: 0,
  }
}

function createVisitRequestToken(): string {
  const bytes = new Uint8Array(16)
  crypto.getRandomValues(bytes)
  return Array.from(bytes, value => value.toString(16).padStart(2, '0')).join('')
}

const selectedWing = ref<HallWing>('birthday')
const payload = ref<CeremonyHallPayload | null>(null)
const loading = ref(false)
const loadError = ref(false)
const acting = ref(false)
const birthdayFormOpen = ref(false)
const birthdayMonth = ref(1)
const birthdayDay = ref(1)
const selectedWish = ref('')
const selectedFeastKey = ref('')
const openVisitorsOnAttend = ref(false)
const ceremonyMoment = ref(false)
const claimingGift = ref(false)
const togglingOpenTable = ref(false)
const openTables = ref<OpenBirthdayTable[]>([])
const openTablesLoading = ref(false)
const openTablesError = ref(false)
const openTablesPagination = ref<PageNavigation>(emptyPageNavigation())
const openTableVisitActions = ref<VisitAction[]>([])
const activeVisitTable = ref<OpenBirthdayTable | null>(null)
const tableVisits = ref<CeremonyVisit[]>([])
const visitsLoading = ref(false)
const visitsPagination = ref<VisitPagination>(emptyVisitPagination())
const selectedVisitAction = ref('')
const selectedVisitActionGroup = ref('')
const visitMessage = ref('')
const visiting = ref(false)
const visitParlorDialog = ref<HTMLElement | null>(null)
const message = useGlobalMessage()
const HALL_TIMEZONE_OFFSET_MS = 8 * 60 * 60 * 1000
const LEGACY_VISIT_ACTION_GROUP: VisitActionGroup = {
  key: 'moments',
  label: '桌边片刻',
  description: '挑一件此刻想做的事。',
  sigil: '◇',
}
let ceremonyTimer: ReturnType<typeof setTimeout> | null = null
let hallDayTimer: ReturnType<typeof setTimeout> | null = null
let bodyScrollLocked = false
let previousBodyOverflow = ''
let visitTableTrigger: HTMLElement | null = null

const birthdayEvent = computed(() => payload.value?.birthday_event ?? null)
const selectedFeastTheme = computed(() => {
  const options = birthdayEvent.value?.banquet?.feast_options ?? []
  return options.find(option => option.key === selectedFeastKey.value) ?? options[0] ?? null
})
const visitActions = computed(() => (
  openTableVisitActions.value.length > 0
    ? openTableVisitActions.value
    : birthdayEvent.value?.visit_actions ?? []
))
const visitActionGroups = computed<VisitActionGroup[]>(() => {
  const groups = new Map<string, VisitActionGroup>()
  visitActions.value.forEach((action) => {
    const group = visitActionGroupFor(action)
    if (!groups.has(group.key)) groups.set(group.key, group)
  })
  return Array.from(groups.values())
})
const visibleVisitActions = computed(() => {
  if (!selectedVisitActionGroup.value) return visitActions.value
  return visitActions.value.filter(
    action => visitActionGroupFor(action).key === selectedVisitActionGroup.value,
  )
})
const selectedVisitActionDetails = computed(() => (
  visitActions.value.find(action => action.key === selectedVisitAction.value) ?? null
))
const selectedVisitActionGroupLabel = computed(() => (
  visitActionGroups.value.find(group => group.key === selectedVisitActionGroup.value)?.label
  ?? '选择串门方式'
))
const visitSubmitLabel = computed(() => {
  if (visiting.value) return '侍者正在记下这一刻…'
  if (!selectedVisitActionDetails.value) return '先挑一种串门方式'
  return `把「${selectedVisitActionDetails.value.label}」留在桌边`
})
const visitMessageMaxLength = computed(() => (
  birthdayEvent.value?.visit_policy.message_max_length ?? 120
))
const birthdayDays = computed(() => {
  const count = new Date(2000, birthdayMonth.value, 0).getDate()
  return Array.from({ length: count }, (_, index) => index + 1)
})
const showBirthdayForm = computed(() => (
  birthdayEvent.value?.status === 'needs_birthday' || birthdayFormOpen.value
))

watch(birthdayMonth, () => {
  if (birthdayDay.value > birthdayDays.value.length) {
    birthdayDay.value = birthdayDays.value.length
  }
})

watch(activeVisitTable, async (table) => {
  if (!table) {
    unlockBodyScroll()
    return
  }
  lockBodyScroll()
  await nextTick()
  visitParlorDialog.value?.focus()
})

function visitActionGroupFor(action: VisitAction): VisitActionGroup {
  return action.group ?? LEGACY_VISIT_ACTION_GROUP
}

function syncVisitActionSelection(preferredKey = selectedVisitAction.value) {
  const actions = visitActions.value
  if (actions.length === 0) {
    selectedVisitAction.value = ''
    selectedVisitActionGroup.value = ''
    return
  }
  const action = actions.find(option => option.key === preferredKey) ?? actions[0]
  selectedVisitAction.value = action.key
  selectedVisitActionGroup.value = visitActionGroupFor(action).key
}

function selectVisitActionGroup(groupKey: string) {
  selectedVisitActionGroup.value = groupKey
  const current = selectedVisitActionDetails.value
  if (current && visitActionGroupFor(current).key === groupKey) return
  const firstAction = visitActions.value.find(
    action => visitActionGroupFor(action).key === groupKey,
  )
  selectedVisitAction.value = firstAction?.key ?? ''
}

function selectVisitAction(action: VisitAction) {
  selectedVisitAction.value = action.key
  selectedVisitActionGroup.value = visitActionGroupFor(action).key
}

function lockBodyScroll() {
  if (bodyScrollLocked) return
  previousBodyOverflow = document.body.style.overflow
  document.body.style.overflow = 'hidden'
  bodyScrollLocked = true
}

function unlockBodyScroll() {
  if (!bodyScrollLocked) return
  document.body.style.overflow = previousBodyOverflow
  bodyScrollLocked = false
}

function handleParlorKeydown(event: KeyboardEvent) {
  if (event.key === 'Escape' && activeVisitTable.value) closeVisitTable()
}

function syncBirthdayForm() {
  const birthday = birthdayEvent.value?.birthday
  if (!birthday) return
  birthdayMonth.value = birthday.month
  birthdayDay.value = birthday.day
}

function syncFeastSelection() {
  const options = birthdayEvent.value?.banquet?.feast_options ?? []
  if (options.length === 0) {
    selectedFeastKey.value = ''
    return
  }
  if (!options.some(option => option.key === selectedFeastKey.value)) {
    selectedFeastKey.value = options[0].key
  }
}

async function loadHall() {
  if (loading.value) return
  loading.value = true
  loadError.value = false
  try {
    const response = await api.get('/adventurer-s1/ceremony-hall')
    const body = response.data
    if (body.code !== 200) {
      loadError.value = true
      return
    }
    payload.value = body.data
    syncBirthdayForm()
    syncFeastSelection()
  } catch {
    loadError.value = true
  } finally {
    loading.value = false
  }
}

async function loadOpenTables(page = 1) {
  if (openTablesLoading.value) return
  openTablesLoading.value = true
  openTablesError.value = false
  try {
    const response = await api.get('/adventurer-s1/ceremony-hall/birthday/open-tables', {
      params: { page },
    })
    const body = response.data
    if (body.code !== 200) {
      openTablesError.value = true
      return
    }
    openTables.value = body.data.items
    openTablesPagination.value = body.data.pagination
    openTableVisitActions.value = body.data.visit_actions
    if (
      activeVisitTable.value
      && !openTables.value.some(table => table.token === activeVisitTable.value?.token)
    ) {
      closeVisitTable()
    }
  } catch {
    openTablesError.value = true
  } finally {
    openTablesLoading.value = false
  }
}

async function toggleOpenTable(visitorsOpen: boolean) {
  if (togglingOpenTable.value) return
  togglingOpenTable.value = true
  try {
    const response = await api.put('/adventurer-s1/ceremony-hall/birthday/open-table', {
      visitors_open: visitorsOpen,
    })
    const body = response.data
    if (body.code !== 200) {
      message.error(body.message || '礼堂门牌暂时没有翻动')
      return
    }
    payload.value = body.data
    await loadOpenTables(1)
    message.success(body.message || (visitorsOpen ? '生日桌已经开放' : '生日桌已经收起'))
  } catch (error: any) {
    message.error(error?.response?.data?.message || '礼堂门牌暂时没有翻动')
  } finally {
    togglingOpenTable.value = false
  }
}

async function openVisitTable(table: OpenBirthdayTable) {
  visitTableTrigger = document.activeElement instanceof HTMLElement
    ? document.activeElement
    : null
  activeVisitTable.value = table
  tableVisits.value = []
  visitsPagination.value = emptyVisitPagination()
  visitMessage.value = ''
  syncVisitActionSelection()
  await loadTableVisits(1)
}

async function openOwnTableLedger() {
  const receipt = birthdayEvent.value?.receipt
  const token = receipt?.open_table.token
  if (!receipt || !token) return
  await openVisitTable({
    token,
    host: receipt.host,
    wish: receipt.wish,
    companion_count: receipt.guest_count,
    visitor_count: receipt.open_table.visitor_count,
    feast: receipt.feast,
    feast_theme: receipt.feast_theme,
    is_mine: true,
  })
}

function closeVisitTable() {
  const trigger = visitTableTrigger
  activeVisitTable.value = null
  tableVisits.value = []
  visitsPagination.value = emptyVisitPagination()
  visitMessage.value = ''
  selectedVisitAction.value = ''
  selectedVisitActionGroup.value = ''
  visitTableTrigger = null
  void nextTick(() => {
    if (trigger?.isConnected) trigger.focus()
  })
}

async function loadTableVisits(page = 1) {
  const table = activeVisitTable.value
  if (!table || visitsLoading.value) return
  visitsLoading.value = true
  try {
    const token = encodeURIComponent(table.token)
    const response = await api.get(
      `/adventurer-s1/ceremony-hall/birthday/open-tables/${token}/visits`,
      { params: { page } },
    )
    const body = response.data
    if (body.code !== 200) {
      message.error(body.message || '桌边小记暂时无法翻开')
      return
    }
    activeVisitTable.value = body.data.table
    tableVisits.value = body.data.items
    visitsPagination.value = body.data.pagination
    openTableVisitActions.value = body.data.visit_actions
    syncVisitActionSelection()
  } catch (error: any) {
    message.error(error?.response?.data?.message || '桌边小记暂时无法翻开')
  } finally {
    visitsLoading.value = false
  }
}

async function sendTableVisit() {
  const table = activeVisitTable.value
  if (!table || table.is_mine || visiting.value || !selectedVisitAction.value) return
  visiting.value = true
  try {
    const token = encodeURIComponent(table.token)
    const response = await api.post(
      `/adventurer-s1/ceremony-hall/birthday/open-tables/${token}/visits`,
      {
        action_key: selectedVisitAction.value,
        message: visitMessage.value,
        request_token: createVisitRequestToken(),
      },
    )
    const body = response.data
    if (body.code !== 200) {
      message.error(body.message || '这一刻暂时没有写进礼簿')
      return
    }
    visitMessage.value = ''
    await loadTableVisits(1)
    await loadOpenTables(openTablesPagination.value.page)
    message.success(body.message || '这一小段串门时光已经留在桌边')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '这一刻暂时没有写进礼簿')
  } finally {
    visiting.value = false
  }
}

function openBirthdayEditor() {
  syncBirthdayForm()
  birthdayFormOpen.value = true
}

async function saveBirthday() {
  if (acting.value) return
  acting.value = true
  try {
    const response = await api.put('/adventurer-s1/ceremony-hall/birthday', {
      month: birthdayMonth.value,
      day: birthdayDay.value,
    })
    const body = response.data
    if (body.code !== 200) {
      message.error(body.message || '礼簿落笔失败')
      return
    }
    payload.value = body.data
    birthdayFormOpen.value = false
    selectedWish.value = ''
    syncBirthdayForm()
    syncFeastSelection()
    message.success(body.message || '生日已经写入星灯礼簿')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '礼簿落笔失败')
  } finally {
    acting.value = false
  }
}

async function attendBirthday() {
  const feast = selectedFeastTheme.value
  if (acting.value || !selectedWish.value || !feast) return
  acting.value = true
  try {
    const response = await api.post('/adventurer-s1/ceremony-hall/birthday/attend', {
      wish_key: selectedWish.value,
      feast_key: feast.key,
      visitors_open: openVisitorsOnAttend.value,
    })
    const body = response.data
    if (body.code !== 200) {
      message.error(body.message || '星灯暂时没有亮起')
      return
    }
    payload.value = body.data
    await loadOpenTables(1)
    ceremonyMoment.value = true
    if (ceremonyTimer) clearTimeout(ceremonyTimer)
    ceremonyTimer = setTimeout(() => {
      ceremonyMoment.value = false
    }, 3600)
    message.success(body.message || '今夜星灯已经为你点亮')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '星灯暂时没有亮起')
  } finally {
    acting.value = false
  }
}

async function claimBirthdayGift() {
  if (claimingGift.value) return
  claimingGift.value = true
  try {
    const response = await api.post('/adventurer-s1/ceremony-hall/birthday/gift/claim')
    const body = response.data
    if (body.code !== 200) {
      message.error(body.message || '蛋糕礼盒暂时没有递到手中')
      return
    }
    payload.value = body.data
    message.success(body.message || '掌柜把生日蛋糕郑重交到了你的手中')
  } catch (error: any) {
    message.error(error?.response?.data?.message || '蛋糕礼盒暂时没有递到手中')
  } finally {
    claimingGift.value = false
  }
}

function millisecondsUntilNextHallDay(): number {
  const now = Date.now()
  const hallTime = new Date(now + HALL_TIMEZONE_OFFSET_MS)
  const nextHallMidnight = Date.UTC(
    hallTime.getUTCFullYear(),
    hallTime.getUTCMonth(),
    hallTime.getUTCDate() + 1,
  ) - HALL_TIMEZONE_OFFSET_MS
  return Math.max(1000, nextHallMidnight - now + 1000)
}

function scheduleHallDayRefresh() {
  if (hallDayTimer) clearTimeout(hallDayTimer)
  hallDayTimer = setTimeout(async () => {
    await Promise.all([loadHall(), loadOpenTables(1)])
    scheduleHallDayRefresh()
  }, millisecondsUntilNextHallDay())
}

onMounted(async () => {
  window.addEventListener('keydown', handleParlorKeydown)
  await Promise.all([loadHall(), loadOpenTables(1)])
  scheduleHallDayRefresh()
})

onUnmounted(() => {
  window.removeEventListener('keydown', handleParlorKeydown)
  unlockBodyScroll()
  if (ceremonyTimer) clearTimeout(ceremonyTimer)
  if (hallDayTimer) clearTimeout(hallDayTimer)
})
</script>

<style scoped>
.starlight-hall {
  --hall-gold: #e6bd72;
  --hall-gold-bright: #ffe0a0;
  --hall-ink: #17111e;
  --hall-plum: #351d36;
  --hall-wine: #6c3038;
  position: relative;
  isolation: isolate;
  display: grid;
  gap: 18px;
  min-height: 520px;
  padding: clamp(14px, 2.2vw, 26px);
  overflow: hidden;
  color: #f0e4d0;
  border: 1px solid rgba(230, 189, 114, .24);
  border-radius: 22px;
  background:
    radial-gradient(circle at 15% 0%, rgba(132, 67, 69, .28), transparent 35%),
    radial-gradient(circle at 90% 16%, rgba(91, 70, 130, .24), transparent 32%),
    linear-gradient(152deg, #211522 0%, #15131d 48%, #1f161b 100%);
  box-shadow: 0 24px 70px rgba(5, 3, 9, .36), inset 0 1px rgba(255, 229, 177, .06);
}

.hall-atmosphere { position: absolute; inset: 0; z-index: -1; pointer-events: none; }
.star { position: absolute; width: 3px; height: 3px; border-radius: 50%; background: #ffe5ac;
  box-shadow: 0 0 11px rgba(255, 218, 150, .8); opacity: .5; }
.star-1 { left: 8%; top: 9%; } .star-2 { left: 21%; top: 24%; } .star-3 { left: 37%; top: 7%; }
.star-4 { left: 55%; top: 18%; } .star-5 { left: 72%; top: 8%; } .star-6 { left: 91%; top: 29%; }
.star-7 { left: 12%; top: 72%; } .star-8 { left: 44%; top: 84%; } .star-9 { left: 67%; top: 66%; }
.star-10 { left: 83%; top: 88%; } .star-11 { left: 96%; top: 57%; }
.light-haze { position: absolute; width: 280px; height: 280px; border-radius: 50%; filter: blur(22px); opacity: .1; }
.haze-left { left: -170px; top: 32%; background: #d28a62; }
.haze-right { right: -170px; top: 8%; background: #9c78d0; }

.hall-hero {
  position: relative;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: clamp(14px, 2.4vw, 28px);
  padding: clamp(22px, 3vw, 34px);
  overflow: hidden;
  border: 1px solid rgba(230, 189, 114, .32);
  border-radius: 18px;
  background:
    linear-gradient(90deg, rgba(67, 35, 49, .9), rgba(31, 25, 39, .84)),
    repeating-linear-gradient(120deg, transparent 0 22px, rgba(255,255,255,.02) 23px 24px);
  box-shadow: inset 0 0 60px rgba(236, 170, 90, .06);
}
.hall-hero::after { content: ''; position: absolute; inset: 8px; border: 1px solid rgba(255, 222, 161, .08);
  border-radius: 12px; pointer-events: none; }
.hall-crest { position: relative; display: grid; place-items: center; width: 82px; height: 82px; }
.hall-crest b { position: relative; z-index: 1; color: var(--hall-gold-bright); font-size: 29px;
  text-shadow: 0 0 20px rgba(255, 217, 144, .7); }
.crest-ring { position: absolute; border: 1px solid rgba(230, 189, 114, .44); border-radius: 50%; }
.crest-ring-outer { inset: 1px; box-shadow: inset 0 0 20px rgba(230, 189, 114, .08); }
.crest-ring-inner { inset: 14px; border-style: dashed; transform: rotate(18deg); }
.hall-heading { min-width: 0; }
.hall-eyebrow, .event-eyebrow, .ledger-heading small, .ceremony-invitation small,
.ceremony-receipt > small, .tournament-intro small {
  color: var(--hall-gold); font-size: 11px; font-style: normal; letter-spacing: .15em; text-transform: uppercase;
}
.hall-heading h2 { margin: 5px 0 7px; color: #fff1d6; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: clamp(29px, 4vw, 44px); font-weight: 500; letter-spacing: .08em; }
.hall-heading > p { max-width: 760px; margin: 0; color: #cdbda9; line-height: 1.75; }
.hall-time-row { display: flex; flex-wrap: wrap; gap: 8px 16px; margin-top: 13px; color: #aa9b91; font-size: 12px; }
.hall-time-row span + span::before { content: '◇'; margin-right: 16px; color: #8e7658; }
.hero-lantern { position: relative; width: 58px; height: 104px; opacity: .88; }
.lantern-crown, .lantern-tail { position: absolute; left: 50%; width: 30px; height: 8px; transform: translateX(-50%);
  border-radius: 8px; background: #8f6a3c; }
.lantern-crown { top: 3px; } .lantern-tail { bottom: 5px; width: 20px; }
.lantern-glass { position: absolute; inset: 14px 7px 17px; display: grid; place-items: center;
  border: 1px solid rgba(255, 210, 128, .58); border-radius: 45% 45% 34% 34%;
  background: radial-gradient(circle, rgba(255, 193, 90, .2), rgba(79, 36, 43, .45)); }
.lantern-glass i { width: 11px; height: 25px; border-radius: 60% 45% 60% 40%; background: #ffd583;
  box-shadow: 0 0 20px 7px rgba(244, 150, 62, .36); }

.hall-doors { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 12px; }
.hall-door { position: relative; display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center;
  gap: 14px; min-height: 88px; padding: 15px 17px; color: #d7c9b6; text-align: left; cursor: pointer;
  border: 1px solid rgba(255,255,255,.1); border-radius: 15px; background: rgba(11, 10, 16, .46);
  transition: border-color .18s ease, transform .18s ease, background .18s ease; }
.hall-door:hover:not(:disabled) { transform: translateY(-2px); border-color: rgba(230, 189, 114, .36); }
.hall-door.active { color: #fff0d5; border-color: rgba(230, 189, 114, .54);
  background: linear-gradient(135deg, rgba(103, 48, 58, .54), rgba(32, 27, 42, .72));
  box-shadow: inset 0 0 30px rgba(255, 188, 94, .05); }
.reservation-door:disabled { opacity: .76; border-style: dashed; background:
  radial-gradient(circle at 88% 18%, rgba(126, 101, 155, .15), transparent 34%),
  linear-gradient(135deg, rgba(34, 28, 43, .72), rgba(13, 12, 18, .62)); }
.reservation-door .door-icon { color: #c9acd9; border-color: rgba(197, 167, 217, .24);
  box-shadow: inset 0 0 18px rgba(181, 137, 214, .06); }
.reservation-door .door-status { color: #c6b2ce; border: 1px solid rgba(197, 167, 217, .12);
  background: rgba(181, 137, 214, .08); }
.door-icon { display: grid; place-items: center; width: 46px; height: 46px; color: var(--hall-gold-bright);
  border: 1px solid rgba(230, 189, 114, .28); border-radius: 50%; font-size: 21px; }
.door-copy { display: grid; gap: 2px; min-width: 0; }
.door-copy small { color: #967f69; font-size: 9px; letter-spacing: .14em; }
.door-copy strong { font-family: Georgia, 'Noto Serif SC', serif; font-size: 18px; font-weight: 500; }
.door-copy em { overflow: hidden; color: #9e9185; font-size: 12px; font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
.door-status { padding: 5px 9px; color: #e8c88d; border-radius: 999px; background: rgba(230, 189, 114, .1);
  font-size: 11px; white-space: nowrap; }

.birthday-wing, .tournament-wing { display: grid; gap: 16px; }
.hall-state-card { display: grid; justify-items: center; gap: 8px; padding: 54px 20px; text-align: center;
  border: 1px dashed rgba(230, 189, 114, .28); border-radius: 16px; background: rgba(12, 11, 16, .42); }
.hall-state-card > span { color: var(--hall-gold); font-size: 28px; }
.hall-state-card p { margin: 0; color: #a99c90; }
.state-orbit { animation: quiet-orbit 3s linear infinite; }

.birthday-stage { position: relative; display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 20px;
  padding: clamp(22px, 3vw, 34px); overflow: hidden; border: 1px solid rgba(230, 189, 114, .26);
  border-radius: 18px; background: linear-gradient(135deg, rgba(86, 42, 50, .7), rgba(28, 23, 35, .84)); }
.birthday-stage.status-available { border-color: rgba(255, 211, 129, .62);
  box-shadow: 0 0 45px rgba(232, 151, 66, .12), inset 0 0 50px rgba(246, 188, 95, .06); }
.birthday-ribbon { position: absolute; top: 18px; right: -34px; width: 150px; padding: 6px 0;
  color: #2c1b12; text-align: center; transform: rotate(35deg); background: var(--hall-gold); font-size: 11px; }
.candle-row { display: flex; align-items: flex-end; gap: 7px; min-width: 76px; padding: 20px 0 8px; }
.candle { position: relative; width: 16px; height: 55px; border-radius: 3px 3px 7px 7px;
  background: linear-gradient(90deg, #b87165, #f0b58a 44%, #9e5657); box-shadow: inset -3px 0 rgba(71, 28, 39, .22); }
.candle:nth-child(2) { height: 72px; }
.wick { position: absolute; left: 50%; top: -8px; width: 1px; height: 9px; background: #3a2925; }
.flame { position: absolute; z-index: 2; left: 50%; top: -25px; width: 11px; height: 22px;
  transform: translateX(-50%) rotate(5deg); border-radius: 60% 40% 62% 38%;
  background: linear-gradient(#fff6c7, #ffc85e 45%, #e2713e); box-shadow: 0 0 14px rgba(255, 175, 76, .75); }
.birthday-stage-copy h3 { margin: 5px 0 8px; color: #fff0d2; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: clamp(25px, 3vw, 34px); font-weight: 500; }
.birthday-stage-copy p { max-width: 780px; margin: 0; color: #c5b5a5; line-height: 1.7; }
.event-rules { grid-column: 1 / -1; display: flex; flex-wrap: wrap; gap: 8px; }
.event-rules span { padding: 6px 10px; color: #bcae9d; border: 1px solid rgba(255,255,255,.08);
  border-radius: 999px; background: rgba(255,255,255,.035); font-size: 12px; }

.birthday-ledger, .birthday-countdown, .wish-ceremony, .banquet-afterglow,
.ceremony-receipt, .tournament-intro {
  border: 1px solid rgba(230, 189, 114, .22); border-radius: 18px; background: rgba(12, 11, 16, .62);
}
.birthday-ledger { display: grid; gap: 20px; padding: clamp(22px, 3vw, 32px); }
.ledger-heading { display: flex; gap: 15px; align-items: flex-start; }
.ledger-mark { display: grid; place-items: center; flex: 0 0 42px; height: 42px; color: #3a2317;
  border-radius: 50%; background: var(--hall-gold); }
.ledger-heading h4, .countdown-copy h4, .ceremony-invitation h4, .ceremony-receipt h4,
.tournament-intro h3 { margin: 4px 0 6px; color: #f6e6c9; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: clamp(20px, 2.5vw, 27px); font-weight: 500; }
.ledger-heading p, .countdown-copy p, .ceremony-invitation p, .tournament-intro p { margin: 0; color: #aa9d90; line-height: 1.65; }
.date-fields { display: flex; align-items: end; gap: 14px; }
.date-fields label { display: grid; gap: 7px; min-width: 150px; color: #bdad9c; font-size: 13px; }
.date-fields select { min-height: 46px; padding: 9px 38px 9px 12px; color: #f2dfbf;
  border: 1px solid rgba(230, 189, 114, .28); border-radius: 10px; background: #19141e; }
.date-divider { padding-bottom: 12px; color: #786958; font-size: 22px; }
.privacy-seal { display: flex; gap: 10px; align-items: flex-start; padding: 12px 14px; color: #a89a8c;
  border-left: 2px solid rgba(230, 189, 114, .38); background: rgba(230, 189, 114, .045); }
.privacy-seal p { margin: 0; line-height: 1.55; font-size: 12px; }
.ledger-actions { display: flex; flex-wrap: wrap; gap: 10px; }

.ceremony-button, .ghost-button { min-height: 44px; padding: 10px 17px; cursor: pointer; border-radius: 10px;
  transition: transform .18s ease, filter .18s ease, border-color .18s ease; }
.ceremony-button { color: #2b1a11; border: 1px solid #f3cd82;
  background: linear-gradient(180deg, #f2cd85, #c99549); box-shadow: 0 7px 22px rgba(211, 140, 55, .17); }
.ghost-button { color: #dbc7a7; border: 1px solid rgba(230, 189, 114, .24); background: rgba(255,255,255,.045); }
.ceremony-button:hover:not(:disabled), .ghost-button:hover:not(:disabled) { transform: translateY(-1px); filter: brightness(1.07); }
button:disabled { cursor: not-allowed; opacity: .5; }
button:focus-visible, select:focus-visible, textarea:focus-visible, input:focus-visible {
  outline: 2px solid #f5cf82; outline-offset: 3px;
}

.birthday-countdown { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 28px;
  padding: clamp(22px, 3vw, 34px); }
.countdown-medallion { display: grid; place-items: center; align-content: center; width: 148px; height: 148px;
  border: 1px solid rgba(230, 189, 114, .5); border-radius: 50%;
  background: radial-gradient(circle, rgba(230, 189, 114, .14), rgba(65, 35, 50, .26) 62%, transparent 64%);
  box-shadow: inset 0 0 0 7px rgba(230, 189, 114, .035); }
.countdown-medallion span { color: #a89478; font-size: 11px; letter-spacing: .12em; }
.countdown-medallion strong { color: var(--hall-gold-bright); font-family: Georgia, serif; font-size: 48px; line-height: 1; }
.countdown-medallion small { color: #bca88a; }
.countdown-copy .ghost-button { margin-top: 14px; }

.wish-ceremony { display: grid; gap: 22px; padding: clamp(22px, 3vw, 34px); }
.banquet-preview { display: grid; gap: 15px; padding: clamp(16px, 2.5vw, 24px);
  border: 1px solid rgba(230, 189, 114, .22); border-radius: 16px;
  background: linear-gradient(145deg, rgba(91, 48, 52, .2), rgba(20, 17, 24, .72)); }
.banquet-heading { display: flex; align-items: end; justify-content: space-between; gap: 16px; }
.banquet-heading small, .wish-section-heading small, .banquet-complete-heading small,
.afterglow-title small, .blessing-copy > small { color: var(--hall-gold); font-size: 10px; letter-spacing: .14em; }
.banquet-heading h5 { margin: 4px 0 0; color: #f3dfbc; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: clamp(18px, 2.2vw, 23px); font-weight: 500; }
.banquet-heading > span { padding: 6px 10px; color: #e8c98d; border: 1px solid rgba(230, 189, 114, .2);
  border-radius: 999px; background: rgba(230, 189, 114, .06); font-size: 11px; white-space: nowrap; }
.invitation-copy { margin: 0; color: #aa9b8f; line-height: 1.65; font-size: 13px; }
.banquet-table-scene { display: grid; gap: 13px; }
.seat-gallery { display: grid; grid-template-columns: repeat(auto-fit, minmax(138px, 1fr)); gap: 9px; }
.seat-card { position: relative; display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center;
  gap: 1px 10px; min-height: 66px; padding: 11px 12px; overflow: hidden;
  border: 1px solid rgba(255,255,255,.08); border-radius: 12px; background: rgba(7, 7, 10, .32); }
.seat-card::after { content: ''; position: absolute; inset: auto 9px 0; height: 1px;
  background: linear-gradient(90deg, transparent, rgba(230,189,114,.28), transparent); }
.celebrant-seat { border-color: rgba(242, 198, 112, .42); background: rgba(117, 54, 61, .24); }
.seat-medallion { grid-row: 1 / 4; display: grid; place-items: center; width: 34px; height: 34px;
  color: #edcb86; border: 1px solid rgba(230,189,114,.25); border-radius: 50%; }
.seat-card small { align-self: end; color: #8f8277; font-size: 10px; }
.seat-card strong { align-self: start; overflow: hidden; color: #ead9bd; font-size: 13px; text-overflow: ellipsis; white-space: nowrap; }
.seat-card em, .placecard em { overflow: hidden; color: #8f7d70; font-size: 9px; font-style: normal;
  text-overflow: ellipsis; white-space: nowrap; }
.feast-theme-picker { display: grid; gap: 10px; padding: 14px;
  border: 1px solid rgba(230,189,114,.14); border-radius: 14px; background: rgba(10, 9, 13, .28); }
.feast-theme-picker > header { display: flex; align-items: end; justify-content: space-between; gap: 14px; }
.feast-theme-picker > header > div { display: grid; gap: 3px; }
.feast-theme-picker > header small { color: var(--hall-gold); font-size: 9px; letter-spacing: .13em; }
.feast-theme-picker > header strong { color: #ead8ba; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: 15px; font-weight: 500; }
.feast-theme-picker > header > span { color: #8e8177; font-size: 9px; white-space: nowrap; }
.feast-theme-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(158px, 1fr)); gap: 8px; }
.feast-theme-card { position: relative; display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 3px 9px;
  min-height: 88px; padding: 11px; overflow: hidden; color: #cbbb9f; text-align: left; cursor: pointer;
  border: 1px solid rgba(255,255,255,.07); border-radius: 11px; background: rgba(255,255,255,.025);
  transition: border-color .18s ease, background .18s ease, transform .18s ease; }
.feast-theme-card:hover:not(:disabled) { transform: translateY(-1px); border-color: rgba(230,189,114,.34); }
.feast-theme-card.selected { border-color: rgba(239,199,119,.58);
  background: linear-gradient(145deg, rgba(126,58,66,.36), rgba(74,53,82,.2));
  box-shadow: inset 0 0 28px rgba(244,177,78,.055); }
.feast-theme-card > span:nth-child(2) { display: grid; align-content: center; gap: 3px; min-width: 0; }
.feast-theme-card strong { overflow: hidden; color: #eddbbc; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.feast-theme-card small { color: #92847a; line-height: 1.4; font-size: 9px; }
.feast-theme-card em { grid-column: 2; align-self: end; color: #b99865; font-size: 8px; font-style: normal;
  letter-spacing: .08em; }
.feast-theme-sigil { grid-row: 1 / 3; display: grid; place-items: center; width: 34px; height: 34px;
  color: #f0ca84; border: 1px solid rgba(230,189,114,.25); border-radius: 50%; background: rgba(230,189,114,.045); }
.feast-theme-description { display: flex; align-items: flex-start; gap: 8px; margin: 0; padding: 9px 11px;
  color: #a99a8c; border-left: 2px solid rgba(230,189,114,.36); background: rgba(230,189,114,.035);
  line-height: 1.55; font-size: 10px; }
.feast-theme-description span { color: #e4bc72; }
.tabletop { position: relative; min-height: 190px; padding: 30px clamp(14px, 2.4vw, 25px) 20px; overflow: hidden;
  border: 1px solid rgba(226, 173, 102, .3); border-radius: 42px 42px 20px 20px;
  background:
    linear-gradient(90deg, rgba(46, 18, 16, .22), transparent 12% 88%, rgba(46, 18, 16, .22)),
    repeating-linear-gradient(92deg, #593429 0 44px, #654032 45px 47px, #4d2b25 48px 92px);
  box-shadow: inset 0 12px 30px rgba(255, 193, 109, .045), 0 16px 30px rgba(0,0,0,.18); }
.tabletop::before { content: ''; position: absolute; inset: 8px; border: 1px solid rgba(255,219,155,.1);
  border-radius: 34px 34px 15px 15px; pointer-events: none; }
.table-runner { position: absolute; left: 5%; right: 5%; top: 10px; display: flex; justify-content: space-around;
  height: 17px; color: rgba(255,221,157,.42); border-top: 1px solid rgba(230,189,114,.19);
  border-bottom: 1px solid rgba(230,189,114,.13); background: rgba(95, 31, 40, .32); font-size: 8px; line-height: 15px; }
.feast-grid { position: relative; z-index: 1; display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
.feast-dish { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 10px; padding: 12px;
  border: 1px solid rgba(255,231,186,.1); border-radius: 13px; background: rgba(20, 11, 12, .48);
  box-shadow: inset 0 1px rgba(255,241,213,.04); }
.feast-dish > span { display: grid; place-items: center; width: 37px; height: 37px; color: #f0cc86;
  border: 1px solid rgba(230,189,114,.24); border-radius: 50%; background: rgba(230,189,114,.06); }
.feast-dish small { display: block; color: #ad9070; font-size: 9px; letter-spacing: .08em; }
.feast-dish strong { display: block; margin-top: 2px; color: #f0ddbd; font-family: Georgia, 'Noto Serif SC', serif; font-size: 13px; }
.feast-dish p { margin: 4px 0 0; color: #9d8e82; line-height: 1.45; font-size: 11px; }
.host-promise { display: flex; align-items: center; gap: 10px; padding: 10px 13px; color: #b8a796;
  border-left: 2px solid rgba(230,189,114,.48); background: rgba(230,189,114,.04); }
.host-promise > span { color: var(--hall-gold); }
.host-promise p { margin: 0; line-height: 1.55; font-size: 12px; }
.wish-section-heading { display: grid; gap: 4px; padding-top: 2px; text-align: center; }
.wish-section-heading strong { color: #ead8ba; font-family: Georgia, 'Noto Serif SC', serif; font-size: 17px; font-weight: 500; }
.wish-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 11px; }
.wish-card { display: grid; grid-template-columns: auto 1fr; gap: 3px 12px; min-height: 118px; padding: 16px;
  color: #ddcfbb; text-align: left; cursor: pointer; border: 1px solid rgba(255,255,255,.09);
  border-radius: 13px; background: rgba(255,255,255,.035); }
.wish-card:hover:not(:disabled), .wish-card.selected { border-color: rgba(237, 194, 113, .54);
  background: linear-gradient(135deg, rgba(122, 59, 68, .38), rgba(64, 49, 79, .28)); }
.wish-card.selected { box-shadow: inset 0 0 28px rgba(244, 177, 78, .06); }
.wish-sigil { grid-row: 1 / 3; display: grid; place-items: center; width: 42px; height: 42px; color: #f4d28d;
  border: 1px solid rgba(230, 189, 114, .32); border-radius: 50%; font-size: 18px; }
.wish-card strong { align-self: end; color: #f0dfc0; }
.wish-card p { align-self: start; margin: 3px 0 0; color: #9f9286; line-height: 1.55; font-size: 12px; }
.open-house-choice { display: grid; grid-template-columns: auto auto minmax(0, 1fr); align-items: center; gap: 12px;
  padding: 15px 16px; cursor: pointer; border: 1px solid rgba(230,189,114,.18); border-radius: 13px;
  background: rgba(255,255,255,.025); transition: border-color .18s ease, background .18s ease; }
.open-house-choice.checked { border-color: rgba(239,198,117,.5);
  background: linear-gradient(120deg, rgba(119,56,64,.3), rgba(230,189,114,.045)); }
.open-house-choice input { width: 17px; height: 17px; accent-color: #d7a959; }
.open-house-choice > span:last-child { display: grid; gap: 3px; }
.open-house-choice strong { color: #ead8b9; font-family: Georgia, 'Noto Serif SC', serif; font-weight: 500; }
.open-house-choice small { color: #9c8e82; line-height: 1.55; }
.door-sign { display: grid; place-items: center; min-width: 54px; min-height: 30px; padding: 4px 8px;
  color: #3a2418; border-radius: 5px; background: linear-gradient(180deg, #e6c17b, #b57b3e);
  box-shadow: inset 0 0 0 1px rgba(255,244,215,.22), 0 4px 12px rgba(0,0,0,.18);
  font-family: Georgia, serif; font-size: 10px; letter-spacing: .12em; }
.light-the-hall { justify-self: center; min-width: min(100%, 300px); font-size: 15px; letter-spacing: .06em; }

.banquet-afterglow { display: grid; gap: 20px; padding: clamp(22px, 3.5vw, 38px);
  overflow: hidden; background:
    radial-gradient(circle at 50% 0%, rgba(221, 149, 69, .13), transparent 37%),
    linear-gradient(145deg, rgba(66, 34, 44, .87), rgba(20, 18, 25, .94)); }
.banquet-complete-heading { display: flex; align-items: center; gap: 15px; }
.completion-sigil { display: grid; place-items: center; flex: 0 0 54px; height: 54px; color: #40231d;
  border-radius: 50%; background: radial-gradient(circle, #ffe0a0, #c99549 73%);
  box-shadow: 0 0 26px rgba(231, 168, 77, .22); font-size: 20px; }
.banquet-complete-heading h4 { margin: 4px 0 5px; color: #f8e7c7; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: clamp(22px, 3vw, 30px); font-weight: 500; }
.banquet-complete-heading p { margin: 0; color: #aa9c90; line-height: 1.6; }
.afterglow-grid { display: grid; grid-template-columns: 1.08fr .92fr; gap: 12px; }
.afterglow-panel { display: grid; align-content: start; gap: 12px; padding: 17px;
  border: 1px solid rgba(230,189,114,.16); border-radius: 14px; background: rgba(7, 7, 10, .26); }
.afterglow-title { display: grid; gap: 2px; }
.afterglow-title strong { color: #ead7b8; font-family: Georgia, 'Noto Serif SC', serif; font-weight: 500; }
.afterglow-title em { color: #8f8177; font-size: 9px; font-style: normal; line-height: 1.45; }
.afterglow-title p { margin: 4px 0 0; color: #9b8d82; font-size: 10px; line-height: 1.55; }
.guest-placecards { display: grid; grid-template-columns: repeat(auto-fit, minmax(118px, 1fr)); gap: 8px; }
.placecard { display: grid; grid-template-columns: auto minmax(0,1fr); gap: 0 8px; align-items: center;
  min-height: 53px; padding: 9px; border: 1px solid rgba(255,255,255,.075); border-radius: 10px;
  background: linear-gradient(145deg, rgba(255,255,255,.035), rgba(255,255,255,.012)); }
.placecard > span { grid-row: 1 / 4; color: #d7ae6a; }
.placecard small { align-self: end; color: #877b72; font-size: 9px; }
.placecard strong { align-self: start; overflow: hidden; color: #dfceb4; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
.celebrant-placecard { border-color: rgba(230,189,114,.36); background: rgba(117, 54, 61, .18); }
.served-dishes { display: grid; gap: 7px; }
.served-dishes > div { display: grid; grid-template-columns: auto minmax(0,1fr); align-items: center; gap: 9px;
  padding: 7px 9px; border-bottom: 1px solid rgba(255,255,255,.055); }
.served-dishes > div > span { color: #e0b873; }
.served-dishes p { margin: 0; color: #d4c1a6; font-size: 12px; }
.served-dishes small { display: inline-block; min-width: 64px; margin-right: 7px; color: #826f60; font-size: 9px; }
.tavern-blessing { position: relative; display: grid; grid-template-columns: auto minmax(0,1fr); gap: 18px;
  padding: clamp(18px, 3vw, 28px); overflow: hidden; border: 1px solid rgba(243,202,125,.35);
  border-radius: 16px; background:
    radial-gradient(circle at 8% 50%, rgba(247, 178, 80, .15), transparent 30%),
    linear-gradient(115deg, rgba(93, 42, 47, .38), rgba(20, 17, 24, .74)); }
.tavern-blessing::after { content: '✦'; position: absolute; right: 18px; top: 4px; color: rgba(230,189,114,.09);
  font-size: 86px; pointer-events: none; }
.host-medallion { display: grid; place-items: center; width: 64px; height: 64px; color: #3a2118;
  border-radius: 50%; background: linear-gradient(145deg, #f2d293, #b97d3d);
  box-shadow: 0 0 30px rgba(237,168,73,.13); font-size: 22px; }
.blessing-copy { position: relative; z-index: 1; min-width: 0; }
.blessing-copy blockquote { margin: 8px 0 10px; color: #f0debf; border: 0;
  font-family: Georgia, 'Noto Serif SC', serif; font-size: clamp(16px, 2.2vw, 21px); line-height: 1.9; }
.blessing-copy footer { color: #9a8877; font-size: 11px; }
.birthday-gift-card { position: relative; display: grid; grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center; gap: clamp(15px, 2.4vw, 25px); padding: clamp(18px, 3vw, 28px); overflow: hidden;
  border: 1px solid rgba(242, 201, 121, .3); border-radius: 16px; background:
    radial-gradient(circle at 9% 42%, rgba(248, 188, 102, .15), transparent 28%),
    linear-gradient(120deg, rgba(75, 39, 48, .48), rgba(19, 17, 24, .78)); }
.birthday-gift-card::after { position: absolute; right: -23px; bottom: -55px; color: rgba(239, 199, 124, .055);
  content: '✦'; font-size: 150px; pointer-events: none; }
.birthday-gift-card.delivered { border-color: rgba(248, 211, 137, .46);
  box-shadow: inset 0 0 36px rgba(236, 174, 80, .045); }
.birthday-gift-card.waiting_for_adventurer { border-style: dashed; }
.gift-cake { position: relative; display: grid; place-items: center; width: 92px; height: 88px; }
.gift-cake > span { position: absolute; z-index: 3; top: -1px; color: #ffe2a2; text-shadow: 0 0 17px rgba(255, 190, 88, .88);
  font-size: 19px; animation: cake-flame 2.2s ease-in-out infinite; }
.cake-layer { position: absolute; left: 50%; display: block; transform: translateX(-50%);
  border: 1px solid rgba(255, 230, 183, .42); box-shadow: inset 0 5px rgba(255, 240, 209, .2), 0 8px 18px rgba(0,0,0,.17); }
.cake-top { top: 28px; z-index: 2; width: 58px; height: 25px; border-radius: 14px 14px 8px 8px;
  background: linear-gradient(180deg, #f2d4a5, #d68c75); }
.cake-base { top: 47px; width: 76px; height: 31px; border-radius: 10px 10px 15px 15px;
  background: linear-gradient(180deg, #e6b38b, #9f5362); }
.gift-cake > b { position: absolute; z-index: 3; top: 52px; color: #ffe0a0; font-size: 13px; font-weight: 400; }
.gift-copy { position: relative; z-index: 1; display: grid; gap: 6px; min-width: 0; }
.gift-copy > small { color: #d9ae68; font-size: 9px; letter-spacing: .14em; }
.gift-copy h5 { margin: 0; color: #f5dfbc; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: clamp(19px, 2.5vw, 26px); font-weight: 500; }
.gift-copy > p { max-width: 850px; margin: 0; color: #b5a395; line-height: 1.7; font-size: 11px; }
.gift-copy > strong { color: #e0bd7d; font-size: 11px; font-weight: 500; }
.gift-seals { display: flex; flex-wrap: wrap; gap: 6px; margin: 2px 0; }
.gift-seals span { padding: 4px 8px; color: #bca181; border: 1px solid rgba(231, 190, 112, .17);
  border-radius: 999px; background: rgba(230, 189, 114, .045); font-size: 9px; }
.gift-claim-button { position: relative; z-index: 1; min-width: 180px; white-space: nowrap; }
.own-table-door { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 15px;
  padding: 16px; border: 1px solid rgba(230,189,114,.18); border-radius: 14px; background: rgba(7,7,10,.28); }
.own-table-door .door-sign:not(.open) { color: #baaa93; background: linear-gradient(180deg, #554d48, #302a2b); }
.own-table-door > div { display: grid; gap: 3px; }
.own-table-door > .own-table-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 7px; }
.own-table-actions .ghost-button { min-height: 36px; padding: 7px 10px; white-space: nowrap; }
.own-table-door small { color: #b8935c; font-size: 9px; letter-spacing: .13em; }
.own-table-door strong { color: #ead7b8; font-family: Georgia, 'Noto Serif SC', serif; font-weight: 500; }
.own-table-door p { margin: 0; color: #95887d; line-height: 1.55; font-size: 11px; }
.door-closed-note { color: #847970; font-size: 11px; }

.ceremony-receipt { position: relative; display: grid; justify-items: center; padding: clamp(30px, 5vw, 52px);
  overflow: hidden; text-align: center; background:
    linear-gradient(rgba(41, 27, 37, .94), rgba(28, 23, 32, .96)),
    repeating-linear-gradient(45deg, transparent 0 10px, rgba(255,255,255,.02) 11px 12px); }
.receipt-corners i { position: absolute; width: 30px; height: 30px; border-color: rgba(230, 189, 114, .45); }
.receipt-corners i:nth-child(1) { left: 12px; top: 12px; border-left: 1px solid; border-top: 1px solid; }
.receipt-corners i:nth-child(2) { right: 12px; top: 12px; border-right: 1px solid; border-top: 1px solid; }
.receipt-corners i:nth-child(3) { left: 12px; bottom: 12px; border-left: 1px solid; border-bottom: 1px solid; }
.receipt-corners i:nth-child(4) { right: 12px; bottom: 12px; border-right: 1px solid; border-bottom: 1px solid; }
.receipt-seal { display: grid; place-items: center; width: 62px; height: 62px; margin-bottom: 15px; color: #351f23;
  border-radius: 50%; background: var(--hall-gold); box-shadow: 0 0 28px rgba(230, 177, 88, .18); font-size: 23px; }
.ceremony-receipt h4 { margin-top: 8px; font-size: clamp(24px, 3.5vw, 35px); }
.receipt-date { margin: 0; color: #aa9a88; }
.ceremony-receipt blockquote { width: min(100%, 660px); margin: 25px 0 20px; padding: 20px;
  border: 0; border-top: 1px solid rgba(230, 189, 114, .2); border-bottom: 1px solid rgba(230, 189, 114, .2); }
.ceremony-receipt blockquote > span { color: var(--hall-gold); font-size: 24px; }
.ceremony-receipt blockquote strong { display: block; margin-top: 7px; color: #f0dbb5; }
.ceremony-receipt blockquote p { margin: 7px 0 0; color: #c1b19d; line-height: 1.8; }
.ceremony-receipt footer { display: grid; gap: 5px; color: #887c72; font-size: 11px; }

.shared-birthday-hall { display: grid; gap: 17px; padding: clamp(20px, 3vw, 32px); overflow: hidden;
  border: 1px solid rgba(181,151,213,.22); border-radius: 18px; background:
    radial-gradient(circle at 92% 0%, rgba(117,84,157,.15), transparent 32%),
    radial-gradient(circle at 4% 100%, rgba(191,111,73,.12), transparent 30%),
    linear-gradient(145deg, rgba(18,16,25,.9), rgba(28,20,28,.88)); }
.shared-hall-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 18px; }
.shared-hall-heading small, .open-table-card header small, .visit-parlor-heading small,
.visit-ledger-title small { color: #c4a1df; font-size: 9px; letter-spacing: .15em; }
.shared-hall-heading h4 { margin: 4px 0 6px; color: #f1dfc5; font-family: Georgia, 'Noto Serif SC', serif;
  font-size: clamp(21px, 2.8vw, 29px); font-weight: 500; }
.shared-hall-heading p { max-width: 720px; margin: 0; color: #a39591; line-height: 1.65; }
.compact-button { min-height: 38px; padding: 7px 12px; white-space: nowrap; }
.open-tables-state { display: grid; justify-items: center; gap: 7px; padding: 31px 18px; color: #aa9d91;
  text-align: center; border: 1px dashed rgba(193,160,218,.2); border-radius: 14px; background: rgba(5,5,9,.24); }
.open-tables-state > span { color: #c7a3df; font-size: 23px; }
.open-tables-state p { margin: 0; line-height: 1.55; }
.open-table-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 11px; }
.open-table-card { position: relative; display: grid; align-content: start; gap: 12px; min-width: 0; padding: 17px;
  overflow: hidden; border: 1px solid rgba(255,255,255,.085); border-radius: 15px; background:
    linear-gradient(145deg, rgba(82,47,65,.27), rgba(13,12,18,.62));
  transition: transform .18s ease, border-color .18s ease, box-shadow .18s ease; }
.open-table-card:hover, .open-table-card.active { transform: translateY(-2px); border-color: rgba(209,173,225,.4);
  box-shadow: 0 14px 30px rgba(0,0,0,.15), inset 0 0 30px rgba(181,137,214,.035); }
.open-table-card.mine { border-color: rgba(230,189,114,.34); }
.table-card-lantern { position: absolute; right: 12px; top: 6px; color: rgba(231,198,139,.12); font-size: 52px; }
.open-table-card header { position: relative; z-index: 1; min-width: 0; }
.open-table-card h5, .visit-parlor-heading h5 { margin: 4px 0 3px; color: #f0ddbf;
  font-family: Georgia, 'Noto Serif SC', serif; font-size: 19px; font-weight: 500; }
.open-table-card header p { margin: 0; color: #897b73; font-size: 10px; }
.table-card-counts { display: flex; flex-wrap: wrap; gap: 6px; }
.table-card-counts span { padding: 4px 7px; color: #b5a397; border-radius: 999px;
  background: rgba(255,255,255,.045); font-size: 10px; }
.open-table-card > blockquote { display: grid; grid-template-columns: auto minmax(0,1fr); gap: 9px; margin: 0;
  padding: 10px 0; border: 0; border-top: 1px solid rgba(255,255,255,.055); border-bottom: 1px solid rgba(255,255,255,.055); }
.open-table-card > blockquote > span { color: #e7c17b; font-size: 20px; }
.open-table-card > blockquote p { display: grid; gap: 3px; margin: 0; color: #93877f; line-height: 1.45; font-size: 10px; }
.open-table-card > blockquote strong { color: #d8c5aa; font-size: 12px; }
.table-feast-theme { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 8px;
  padding: 8px 9px; border: 1px solid rgba(230,189,114,.1); border-radius: 9px;
  background: rgba(230,189,114,.025); }
.table-feast-theme > span { color: #e3b96f; font-size: 16px; }
.table-feast-theme p { display: grid; gap: 1px; min-width: 0; margin: 0; }
.table-feast-theme strong { color: #cdbb9f; font-size: 10px; }
.table-feast-theme small { overflow: hidden; color: #7f736c; font-size: 8px; text-overflow: ellipsis; white-space: nowrap; }
.table-dish-chips { display: flex; flex-wrap: wrap; gap: 5px; }
.table-dish-chips span { max-width: 100%; overflow: hidden; padding: 4px 7px; color: #9b8977;
  border: 1px solid rgba(230,189,114,.09); border-radius: 6px; font-size: 9px; text-overflow: ellipsis; white-space: nowrap; }
.table-visit-button { justify-self: stretch; margin-top: auto; }
.hall-pagination { display: flex; align-items: center; justify-content: center; gap: 11px; }
.hall-pagination .ghost-button { min-height: 36px; padding: 6px 11px; font-size: 11px; }
.hall-pagination > span { color: #887b73; font-size: 11px; }

.visit-parlor-overlay {
  --hall-gold: #e6bd72;
  --hall-gold-bright: #ffe0a0;
  position: fixed;
  inset: 0;
  z-index: 2600;
  display: grid;
  place-items: center;
  padding: clamp(10px, 2.4vw, 30px);
  overflow: hidden;
  background:
    radial-gradient(circle at 50% 16%, rgba(129, 75, 99, .3), transparent 38%),
    rgba(5, 4, 9, .84);
  backdrop-filter: blur(12px) saturate(.82);
}
.visit-parlor {
  position: relative;
  isolation: isolate;
  display: grid;
  grid-template-rows: auto minmax(0, 1fr);
  width: min(1120px, 100%);
  max-height: min(900px, calc(100dvh - clamp(20px, 4.8vw, 60px)));
  overflow: hidden;
  color: #eee0cb;
  outline: none;
  border: 1px solid rgba(239, 201, 130, .42);
  border-radius: 24px;
  background:
    radial-gradient(circle at 8% 8%, rgba(163, 81, 72, .2), transparent 30%),
    radial-gradient(circle at 92% 0%, rgba(120, 86, 160, .17), transparent 31%),
    linear-gradient(150deg, #2b1924 0%, #15131b 46%, #23171d 100%);
  box-shadow:
    0 35px 110px rgba(0, 0, 0, .72),
    0 0 0 1px rgba(255, 227, 172, .035) inset,
    0 0 70px rgba(204, 143, 80, .09) inset;
}
.visit-parlor::before {
  position: absolute;
  inset: 9px;
  z-index: -1;
  pointer-events: none;
  content: '';
  border: 1px solid rgba(230, 189, 114, .09);
  border-radius: 17px;
}
.parlor-atmosphere { position: absolute; inset: 0; z-index: -1; pointer-events: none; }
.parlor-atmosphere i { position: absolute; width: 3px; height: 3px; border-radius: 50%; background: #ffe3a2;
  box-shadow: 0 0 13px rgba(255, 214, 137, .8); opacity: .52; }
.parlor-atmosphere i:nth-child(1) { left: 8%; top: 20%; }
.parlor-atmosphere i:nth-child(2) { left: 24%; top: 7%; }
.parlor-atmosphere i:nth-child(3) { left: 43%; top: 17%; }
.parlor-atmosphere i:nth-child(4) { left: 64%; top: 6%; }
.parlor-atmosphere i:nth-child(5) { left: 86%; top: 23%; }
.parlor-atmosphere i:nth-child(6) { left: 72%; top: 82%; }
.parlor-atmosphere i:nth-child(7) { left: 14%; top: 76%; }
.visit-parlor-heading {
  position: relative;
  z-index: 2;
  display: grid;
  grid-template-columns: auto minmax(0, 1fr) auto;
  align-items: center;
  gap: 16px;
  padding: clamp(18px, 2.6vw, 28px) clamp(20px, 3.2vw, 36px);
  border-bottom: 1px solid rgba(230, 189, 114, .15);
  background: linear-gradient(180deg, rgba(59, 31, 40, .92), rgba(31, 22, 29, .86));
  box-shadow: 0 12px 34px rgba(0, 0, 0, .16);
}
.parlor-heading-sigil { display: grid; place-items: center; width: 52px; height: 52px; color: #33201f;
  border-radius: 50%; background: linear-gradient(145deg, #f2cf88, #bf7d47); box-shadow: 0 0 28px rgba(229, 171, 91, .2);
  font-size: 20px; }
.visit-parlor-heading > div { min-width: 0; }
.parlor-title-row { display: flex; align-items: center; flex-wrap: wrap; gap: 9px; }
.parlor-title-row > span { padding: 3px 8px; color: #d9b67a; border: 1px solid rgba(230, 189, 114, .18);
  border-radius: 999px; background: rgba(230, 189, 114, .055); font-size: 9px; letter-spacing: .08em; }
.visit-parlor-heading h5 { margin: 4px 0 3px; color: #f3dfbd;
  font-family: Georgia, 'Noto Serif SC', serif; font-size: clamp(21px, 2.8vw, 30px); font-weight: 500; }
.visit-parlor-heading p { max-width: 760px; margin: 2px 0 0; color: #aa9b8f; line-height: 1.55; font-size: 12px; }
.parlor-close { display: grid; place-items: center; width: 38px; height: 38px; color: #d9c5a8; cursor: pointer;
  border: 1px solid rgba(230,189,114,.2); border-radius: 50%; background: rgba(10,9,13,.34); font-size: 23px;
  transition: color .18s ease, border-color .18s ease, transform .18s ease, background .18s ease; }
.parlor-close:hover { color: #fff0cf; border-color: rgba(230,189,114,.48); background: rgba(117,61,55,.32); transform: rotate(8deg); }
.visit-parlor-body { display: grid; align-content: start; gap: 16px; min-height: 0; padding: clamp(16px, 2.6vw, 28px);
  overflow-y: auto; overscroll-behavior: contain; scrollbar-color: rgba(213, 169, 98, .35) transparent; }
.parlor-table-summary { display: grid; grid-template-columns: minmax(0, 1.1fr) minmax(260px, .9fr); gap: 14px;
  padding: 15px 17px; border: 1px solid rgba(230, 189, 114, .13); border-radius: 15px;
  background: linear-gradient(115deg, rgba(91, 48, 49, .22), rgba(8, 8, 12, .42)); }
.parlor-wish-copy { display: grid; grid-template-columns: auto minmax(0, 1fr); align-items: center; gap: 12px; min-width: 0; }
.parlor-wish-copy > span { color: #edc779; font-size: 25px; }
.parlor-wish-copy > div { display: grid; gap: 3px; min-width: 0; }
.parlor-wish-copy small, .parlor-feast-preview > small { color: #a88470; font-size: 8px; letter-spacing: .13em; }
.parlor-wish-copy strong { color: #ead6b6; font-size: 12px; }
.parlor-wish-copy p { margin: 0; color: #9e8f84; line-height: 1.5; font-size: 10px; }
.parlor-feast-preview { display: grid; align-content: center; gap: 7px; padding-left: 14px;
  border-left: 1px solid rgba(230, 189, 114, .11); }
.parlor-feast-preview > div { display: flex; flex-wrap: wrap; gap: 5px; }
.parlor-feast-preview span { padding: 4px 7px; color: #bda88c; border: 1px solid rgba(230, 189, 114, .1);
  border-radius: 999px; background: rgba(255, 255, 255, .025); font-size: 9px; }
.visit-parlor-layout { display: grid; grid-template-columns: minmax(0, 1.08fr) minmax(310px, .92fr); align-items: start; gap: 16px; }
.visit-parlor-layout.owner-view { grid-template-columns: minmax(0, 780px); justify-content: center; }
.visit-composer { display: grid; gap: 14px; min-width: 0; padding: clamp(15px, 2.2vw, 21px);
  border: 1px solid rgba(255,255,255,.075); border-radius: 16px;
  background: linear-gradient(150deg, rgba(25, 17, 23, .86), rgba(8, 7, 11, .63)); }
.visit-composer-heading { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; }
.visit-composer-heading > div { display: grid; gap: 3px; }
.visit-composer-heading small { color: #c5a1dc; font-size: 8px; letter-spacing: .14em; }
.visit-composer-heading strong { color: #ead8bd; font-family: Georgia, 'Noto Serif SC', serif; font-weight: 500; font-size: 18px; }
.visit-composer-heading p { margin: 0; color: #8e8178; line-height: 1.5; font-size: 10px; }
.visit-composer-heading > span { flex: 0 0 auto; padding: 4px 8px; color: #bda26f; border-radius: 999px;
  background: rgba(230, 189, 114, .07); font-size: 9px; }
.visit-action-groups { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 7px; }
.visit-action-groups button { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 1px 6px; min-width: 0; padding: 9px;
  color: #aa9b8b; text-align: left; cursor: pointer; border: 1px solid rgba(255,255,255,.07); border-radius: 10px;
  background: rgba(255,255,255,.025); transition: border-color .18s ease, background .18s ease, transform .18s ease; }
.visit-action-groups button:hover:not(:disabled), .visit-action-groups button.selected { border-color: rgba(209, 173, 225, .36);
  background: linear-gradient(145deg, rgba(104, 69, 124, .23), rgba(112, 59, 56, .17)); transform: translateY(-1px); }
.visit-action-groups button > span { grid-row: 1 / 3; align-self: center; color: #dfb973; font-size: 15px; }
.visit-action-groups strong { overflow: hidden; color: #d8c4a8; font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.visit-action-groups small { overflow: hidden; color: #756b66; font-size: 8px; text-overflow: ellipsis; white-space: nowrap; }
.visit-action-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
.visit-action-card { position: relative; display: grid; grid-template-columns: auto minmax(0,1fr); align-content: center;
  gap: 3px 9px; min-height: 75px; padding: 11px; overflow: hidden; color: #cdbda9; text-align: left; cursor: pointer;
  border: 1px solid rgba(255,255,255,.075); border-radius: 11px; background: rgba(255,255,255,.025);
  transition: border-color .18s ease, background .18s ease, transform .18s ease, box-shadow .18s ease; }
.visit-action-card::after { position: absolute; right: -14px; bottom: -23px; width: 58px; height: 58px; content: '';
  border: 1px solid rgba(230, 189, 114, .045); border-radius: 50%; }
.visit-action-card:hover:not(:disabled), .visit-action-card.selected { border-color: rgba(227,187,111,.44);
  background: linear-gradient(145deg, rgba(112,59,56,.31), rgba(75, 48, 78, .16)); transform: translateY(-1px);
  box-shadow: 0 9px 20px rgba(0, 0, 0, .13); }
.visit-action-card.selected::before { position: absolute; inset: 0 auto 0 0; width: 2px; content: '';
  background: linear-gradient(#f4d796, #a96947); }
.visit-action-card > span { grid-row: 1 / 3; align-self: center; color: #e5bc73; font-size: 20px; }
.visit-action-card strong { align-self: end; color: #e2cfb2; font-size: 11px; }
.visit-action-card small { align-self: start; color: #8b7e74; line-height: 1.4; font-size: 9px; }
.visit-message-field { display: grid; grid-template-columns: 1fr auto; gap: 7px; color: #b4a392; font-size: 11px; }
.visit-message-field textarea { grid-column: 1 / -1; width: 100%; min-height: 82px; padding: 11px; resize: vertical;
  color: #eddbc0; caret-color: #eac57f; border: 1px solid rgba(230,189,114,.18); border-radius: 10px;
  outline: none; background: rgba(11, 10, 15, .88); font: inherit; line-height: 1.55; }
.visit-message-field textarea:focus { border-color: rgba(230, 189, 114, .44); box-shadow: 0 0 0 3px rgba(230, 189, 114, .06); }
.visit-message-field > small { color: #7e726a; }
.visit-composer-footer { display: flex; align-items: center; justify-content: space-between; gap: 14px; }
.visit-composer-footer p { max-width: 310px; margin: 0; color: #766d67; line-height: 1.45; font-size: 9px; }
.visit-composer-footer .ceremony-button { min-height: 40px; padding: 9px 14px; white-space: nowrap; font-size: 11px; }
.visit-ledger { display: grid; align-content: start; gap: 11px; min-width: 0; padding: clamp(15px, 2.2vw, 21px);
  border: 1px solid rgba(255,255,255,.07); border-radius: 16px;
  background: linear-gradient(155deg, rgba(9, 8, 12, .68), rgba(30, 20, 24, .63)); }
.visit-ledger-title { display: flex; justify-content: space-between; align-items: end; gap: 12px; padding-bottom: 8px;
  border-bottom: 1px solid rgba(255, 255, 255, .055); }
.visit-ledger-title > div { display: grid; gap: 2px; }
.visit-ledger-title strong { color: #e1cdb0; font-family: Georgia, 'Noto Serif SC', serif; font-weight: 500; }
.visit-ledger-title > span { padding: 4px 8px; color: #9c8c7e; border-radius: 999px;
  background: rgba(255, 255, 255, .035); font-size: 9px; }
.visit-empty { display: grid; justify-items: center; gap: 8px; padding: 34px 10px; color: #8f837b; text-align: center; font-size: 11px; }
.visit-empty > span { color: #d8b673; font-size: 21px; }
.visit-timeline { display: grid; gap: 8px; }
.visit-timeline > article { position: relative; display: grid; grid-template-columns: auto minmax(0,1fr); gap: 10px;
  padding: 11px 12px; overflow: hidden; border: 1px solid rgba(255, 255, 255, .04); border-left-color: rgba(225,183,107,.35);
  border-radius: 0 10px 10px 0; background: linear-gradient(90deg, rgba(230,189,114,.055), transparent 78%); }
.visit-sigil { display: grid; place-items: center; width: 32px; height: 32px; color: #e0b873;
  border: 1px solid rgba(230,189,114,.17); border-radius: 50%; background: rgba(230, 189, 114, .025); }
.visit-timeline article > div { min-width: 0; }
.visit-moment-meta { display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 5px; }
.visit-moment-meta small { color: #776c66; font-size: 8px; }
.visit-moment-meta span { padding: 2px 6px; color: #9f8c75; border-radius: 999px; background: rgba(255, 255, 255, .035); font-size: 8px; }
.visit-timeline p { margin: 5px 0 0; color: #c8b7a0; line-height: 1.55; font-size: 11px; }
.visit-timeline blockquote { margin: 7px 0 0; padding: 8px 10px; color: #d7c3a5; border: 0;
  border-radius: 8px; background: rgba(99,52,61,.2); font-family: Georgia, 'Noto Serif SC', serif; line-height: 1.6; }
.compact-pagination { padding-top: 3px; }
.parlor-fade-enter-active, .parlor-fade-leave-active { transition: opacity .2s ease; }
.parlor-fade-enter-active .visit-parlor, .parlor-fade-leave-active .visit-parlor {
  transition: opacity .22s ease, transform .22s ease;
}
.parlor-fade-enter-from, .parlor-fade-leave-to { opacity: 0; }
.parlor-fade-enter-from .visit-parlor, .parlor-fade-leave-to .visit-parlor { opacity: 0; transform: translateY(14px) scale(.985); }

.tournament-intro { display: flex; align-items: center; gap: 18px; padding: 20px 22px; }
.table-emblem { display: grid; place-items: center; flex: 0 0 54px; height: 54px; color: #efcf92;
  border: 1px solid rgba(230, 189, 114, .28); border-radius: 50%; font-size: 24px; }
.tournament-intro h3 { font-size: 20px; }
.tournament-wing :deep(.activity-panel) { padding: 4px; }

.ceremony-awake .hall-hero, .ceremony-awake .banquet-afterglow,
.ceremony-awake .ceremony-receipt { animation: hall-awakens 1.3s ease both; }
.ceremony-awake .birthday-gift-card { animation: gift-arrives .9s .68s ease both; }
.ceremony-awake .star { animation: star-awakens 1.8s ease both; }
.ceremony-awake .lantern-glass i, .ceremony-awake .flame { filter: brightness(1.35); }
.ceremony-awake .placecard { animation: seat-arrives .7s ease both; }
.ceremony-awake .placecard:nth-child(2) { animation-delay: .12s; }
.ceremony-awake .placecard:nth-child(3) { animation-delay: .22s; }
.ceremony-awake .placecard:nth-child(4) { animation-delay: .32s; }
.ceremony-awake .placecard:nth-child(5) { animation-delay: .42s; }
.ceremony-awake .served-dishes > div { animation: dish-arrives .65s ease both; }
.ceremony-awake .served-dishes > div:nth-child(2) { animation-delay: .16s; }
.ceremony-awake .served-dishes > div:nth-child(3) { animation-delay: .27s; }
.ceremony-awake .served-dishes > div:nth-child(4) { animation-delay: .38s; }
.ceremony-awake .tavern-blessing { animation: toast-rises .9s .52s ease both; }

@keyframes quiet-orbit { to { transform: rotate(360deg); } }
@keyframes hall-awakens {
  0% { box-shadow: inset 0 0 60px rgba(236, 170, 90, .06); }
  45% { box-shadow: inset 0 0 100px rgba(255, 189, 94, .2), 0 0 45px rgba(231, 155, 65, .15); }
  100% { box-shadow: inset 0 0 60px rgba(236, 170, 90, .08); }
}
@keyframes star-awakens { 50% { opacity: 1; transform: scale(2.1); } }
@keyframes seat-arrives {
  from { opacity: 0; transform: translateY(12px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes dish-arrives {
  from { opacity: 0; transform: translateX(10px); }
  to { opacity: 1; transform: translateX(0); }
}
@keyframes toast-rises {
  from { opacity: 0; transform: translateY(18px); }
  to { opacity: 1; transform: translateY(0); }
}
@keyframes gift-arrives {
  from { opacity: 0; transform: translateY(14px) scale(.985); }
  to { opacity: 1; transform: translateY(0) scale(1); }
}
@keyframes cake-flame {
  0%, 100% { opacity: .78; transform: translateY(0) scale(.92); }
  50% { opacity: 1; transform: translateY(-2px) scale(1.08); }
}

@media (max-width: 980px) {
  .hall-doors { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .visit-parlor-layout { grid-template-columns: 1fr; }
  .visit-action-groups { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .visit-action-groups small { white-space: normal; }
}

@media (max-width: 760px) {
  .starlight-hall { padding: 11px; border-radius: 16px; }
  .hall-hero { grid-template-columns: auto minmax(0, 1fr); padding: 21px 17px; }
  .hall-crest { width: 58px; height: 58px; }
  .hero-lantern { display: none; }
  .hall-time-row span + span::before { display: none; }
  .hall-doors { grid-template-columns: 1fr; }
  .hall-door { min-height: 82px; }
  .birthday-stage { grid-template-columns: 1fr; gap: 10px; }
  .candle-row { min-height: 76px; padding-left: 6px; }
  .event-rules { grid-column: auto; }
  .birthday-countdown { grid-template-columns: 1fr; justify-items: center; text-align: center; }
  .countdown-copy .ghost-button { width: 100%; }
  .wish-grid { grid-template-columns: 1fr; }
  .feast-grid, .afterglow-grid { grid-template-columns: 1fr; }
  .feast-theme-picker > header { display: grid; }
  .feast-theme-picker > header > span { white-space: normal; }
  .banquet-heading { align-items: flex-start; }
  .tabletop { border-radius: 28px 28px 16px 16px; }
  .tavern-blessing { grid-template-columns: 1fr; }
  .host-medallion { width: 52px; height: 52px; }
  .birthday-gift-card { grid-template-columns: auto minmax(0, 1fr); }
  .birthday-gift-card > .gift-claim-button { grid-column: 1 / -1; justify-self: stretch; }
  .own-table-door { grid-template-columns: auto minmax(0, 1fr); }
  .own-table-door > .own-table-actions { grid-column: 1 / -1; justify-content: flex-start; }
  .shared-hall-heading { display: grid; }
  .shared-hall-heading .compact-button { justify-self: start; }
  .visit-parlor-overlay { padding: 8px; }
  .visit-parlor { max-height: calc(100dvh - 16px); border-radius: 18px; }
  .visit-parlor-heading { grid-template-columns: auto minmax(0, 1fr) auto; gap: 10px; padding: 15px; }
  .parlor-heading-sigil { width: 42px; height: 42px; font-size: 17px; }
  .visit-parlor-heading p { font-size: 10px; }
  .visit-parlor-body { gap: 12px; padding: 12px; }
  .parlor-table-summary { grid-template-columns: 1fr; }
  .parlor-feast-preview { padding: 10px 0 0; border-top: 1px solid rgba(230, 189, 114, .11); border-left: 0; }
  .visit-action-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .visit-composer-footer { align-items: stretch; flex-direction: column; }
  .visit-composer-footer p { max-width: none; }
  .visit-composer-footer .ceremony-button { width: 100%; white-space: normal; }
  .date-fields { align-items: stretch; }
  .date-fields label { min-width: 0; flex: 1 1 0; }
  .date-divider { align-self: end; }
  .ceremony-button, .ghost-button { min-height: 46px; }
  .ledger-actions > * { flex: 1 1 180px; }
}

@media (max-width: 470px) {
  .hall-hero { grid-template-columns: 1fr; }
  .hall-crest { display: none; }
  .hall-heading h2 { font-size: 30px; }
  .hall-door { grid-template-columns: auto minmax(0, 1fr); }
  .door-status { grid-column: 2; justify-self: start; }
  .date-fields { display: grid; grid-template-columns: 1fr; }
  .date-divider { display: none; }
  .banquet-heading { display: grid; }
  .banquet-heading > span { justify-self: start; }
  .seat-gallery, .guest-placecards { grid-template-columns: 1fr; }
  .feast-theme-grid { grid-template-columns: 1fr; }
  .feast-dish { grid-template-columns: 1fr; }
  .open-house-choice { grid-template-columns: auto minmax(0, 1fr); }
  .open-house-choice .door-sign { grid-column: 2; justify-self: start; }
  .open-house-choice > span:last-child { grid-column: 1 / -1; }
  .open-table-grid, .visit-action-grid { grid-template-columns: 1fr; }
  .visit-parlor-heading { grid-template-columns: minmax(0, 1fr) auto; }
  .parlor-heading-sigil { display: none; }
  .parlor-title-row { align-items: flex-start; flex-direction: column; gap: 3px; }
  .parlor-title-row > span { display: none; }
  .visit-action-groups { grid-template-columns: 1fr 1fr; }
  .visit-action-groups button { grid-template-columns: auto minmax(0, 1fr); }
  .visit-action-groups small { display: none; }
  .visit-composer-heading { display: grid; }
  .visit-composer-heading > span { justify-self: start; }
  .visit-composer, .visit-ledger { padding: 13px; }
  .hall-pagination { justify-content: space-between; }
  .banquet-complete-heading { align-items: flex-start; }
  .completion-sigil { flex-basis: 44px; height: 44px; }
  .birthday-gift-card { grid-template-columns: 1fr; justify-items: center; text-align: center; }
  .gift-copy { justify-items: center; }
  .gift-seals { justify-content: center; }
  .gift-claim-button { width: 100%; }
  .tournament-intro { align-items: flex-start; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after { scroll-behavior: auto !important; animation: none !important; transition: none !important; }
}
</style>
