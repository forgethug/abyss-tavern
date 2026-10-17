<template>
  <div class="modal-mask s1-mobile-surface s1-mobile-overlay" @mousedown.self="$emit('close')">
    <div
      class="modal-box modal-lg s1-mobile-dialog"
      :class="{ 'modal-xl': usesWideDetailLayout }"
      @mousedown.stop
    >
      <div class="detail-header s1-mobile-dialog-header">
        <span class="detail-icon">{{ building.template.icon }}</span>
        <div class="detail-heading-copy">
          <span class="detail-kicker">
            {{ isAssessmentPreview ? 'SEALED VERDICT · 封印评议' : 'OUTPOST FACILITY · 驻地设施' }}
          </span>
          <h4 class="detail-name">{{ displayBuildingName }}</h4>
          <div :class="['detail-status', 'badge-' + building.status, { 'badge-assessment': isAssessmentPreview }]">
            <template v-if="building.status === 'locked'">{{ lockedStatusLabel }}</template>
            <template v-else-if="displayStatus === 'building'">🔨 建造中</template>
            <template v-else-if="displayStatus === 'upgrading'">⬆️ 升级中</template>
            <template v-else-if="building.status === 'built' && isAssessmentPreview">✦ 静观中</template>
            <template v-else-if="building.status === 'built'">✅ 已建成</template>
          </div>
        </div>
        <button type="button" class="detail-close" aria-label="关闭建筑详情" @click="$emit('close')">×</button>
      </div>

      <p v-if="displayBuildingDescription" class="detail-desc">{{ displayBuildingDescription }}</p>

      <AmbientNpcEncounter v-if="!isAssessmentPreview" surface="building" variant="embedded" />

      <section v-if="building.status === 'built' && isAssessmentPreview" class="assessment-preview-notice">
        <div class="assessment-preview-seal" aria-hidden="true">✦</div>
        <div>
          <strong>世上先有其人，而后有其名</strong>
          <p>
            铭号将成为世界承认的事实：酒馆传颂，城邦记录，盟友敬称，敌人戒惧；往后的相逢与故事，也可能因这个名字改变。
          </p>
          <p v-if="assessmentMinLevel">
            Lv.{{ assessmentMinLevel }} 只是进入诸席视野的最低界线。终庭不受自荐，也不陈列等待继承的名位。
          </p>
          <p>
            终庭不出题，不颁任务，也不为任何人安排危险。它只看冒险者自己选择踏入的绝境，看谁能带回常阶无法解释的结果。
          </p>
          <p>
            留在二十级，或自行走进更危险的道路，都不是终庭替人作的决定。当功业已无可争辩，诸席才会为世人铸下应当如何称呼他的答案。
          </p>
          <small>诸席仍然静默。此处没有可供接取的任务，只有尚未写下的名字。</small>
        </div>
      </section>

      <div v-if="building.status !== 'built'" class="detail-stats">
        <div class="stat-item">
          <span class="stat-label">建造时间</span>
          <span class="stat-value">⏱️ {{ building.template.build_hours }}小时</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">帮忙位</span>
          <span class="stat-value">👥 {{ building.template.helper_slots }}位</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">每小时消耗</span>
          <span class="stat-value">💪 {{ building.template.stamina_cost_per_help }}精力</span>
        </div>
        <div class="stat-item">
          <span class="stat-label">每位帮手加速</span>
          <span class="stat-value">⏩ -{{ formatHelperSpeedupHours(building.template.speedup_minutes) }}小时/小时</span>
        </div>
      </div>

      <!-- ── 材料捐献（所有 locked 且有 required_materials 的建筑） ── -->
      <template v-if="building.status === 'locked' && Object.keys(building.template?.required_materials ?? {}).length > 0">
        <div class="donate-section">
          <h5 class="section-title">🪵 建造材料（需捐满才能开工）</h5>
          <div class="donate-list">
            <div
              v-for="(need, mat) in (building.template?.required_materials ?? {})"
              :key="mat"
              class="donate-row"
            >
              <div class="donate-row-head">
                <span class="donate-mat-icon">{{ matIcon(String(mat)) }}</span>
                <span class="donate-mat-name">{{ matLabel(String(mat)) }}</span>
                <span class="donate-mat-progress">
                  {{ building.donations_summary?.[mat] ?? 0 }} / {{ need }}
                  <span v-if="(building.my_donations?.[mat] ?? 0) > 0" class="donate-mine">
                    (我已捐 {{ building.my_donations?.[mat] }})
                  </span>
                </span>
              </div>
              <div v-if="(building.donations_summary?.[mat] ?? 0) < Number(need)" class="donate-form">
                <span v-if="myMaterialBalance(String(mat)) > 0" class="donate-have">
                  我有 {{ myMaterialBalance(String(mat)) }}
                </span>
                <span v-else class="donate-empty">无库存</span>
                <input
                  v-model.number="donateAmounts[mat]"
                  type="number"
                  min="1"
                  :max="Math.min(myMaterialBalance(String(mat)), Number(need) - (building.donations_summary?.[mat] ?? 0))"
                  class="donate-input"
                  placeholder="数量"
                  :disabled="myMaterialBalance(String(mat)) <= 0"
                />
                <button
                  type="button"
                  class="btn-primary btn-sm"
                  :disabled="donating || myMaterialBalance(String(mat)) <= 0"
                  @click="submitDonate(String(mat))"
                >
                  {{ donating ? '捐…' : '捐' }}
                </button>
              </div>
              <div v-else class="donate-full">✅ 已捐满</div>
            </div>
          </div>
        </div>
      </template>

      <template v-if="building.status === 'building'">
        <div class="progress-section">
          <div class="progress-header">
            <span>建造进度</span>
            <span>{{ liveProgress }}%</span>
          </div>
          <div class="progress-bar-lg">
            <div class="progress-fill-lg" :style="{ width: liveProgress + '%' }"></div>
          </div>
          <div class="remaining-text">剩余：{{ displayRemaining }}</div>
        </div>

        <div class="helpers-section">
          <h5 class="section-title">在位冒险者（{{ constructionBuilding.helpers?.length ?? 0 }}/{{ building.template.helper_slots }}）</h5>
          <div v-if="constructionBuilding.helpers && constructionBuilding.helpers.length > 0" class="helpers-list">
            <div v-for="h in constructionBuilding.helpers" :key="h.adventurer_id" class="helper-item">
              <div class="helper-info">
                <span class="helper-name">{{ h.adventurer_name }}</span>
                <span class="helper-stamina">💪 {{ h.stamina ?? '?' }}</span>
              </div>
              <button
                v-if="canRecall(h)"
                type="button"
                class="btn-recall"
                :disabled="recallingId === h.adventurer_id"
                @click="recallAdventurer(h.adventurer_id)"
              >
                {{ recallingId === h.adventurer_id ? '召回中…' : '召回' }}
              </button>
            </div>
          </div>
          <div v-else class="empty-helpers">暂无冒险者在位建造</div>
        </div>
      </template>

      <template v-if="displayStatus === 'upgrading'">
        <div class="progress-section">
          <div class="progress-header">
            <span>升级进度 · Lv.{{ buildingInstanceLevel }} → Lv.{{ buildingInstanceLevel + 1 }}</span>
            <span>{{ liveProgress }}%</span>
          </div>
          <div class="progress-bar-lg">
            <div class="progress-fill-lg" :style="{ width: liveProgress + '%' }"></div>
          </div>
          <div class="remaining-text">剩余：{{ displayRemaining }}</div>
        </div>

        <div class="helpers-section">
          <h5 class="section-title">在位冒险者（{{ constructionBuilding.helpers?.length ?? 0 }}/{{ building.template.helper_slots }}）</h5>
          <div v-if="constructionBuilding.helpers && constructionBuilding.helpers.length > 0" class="helpers-list">
            <div v-for="h in constructionBuilding.helpers" :key="h.adventurer_id" class="helper-item">
              <div class="helper-info">
                <span class="helper-name">{{ h.adventurer_name }}</span>
                <span class="helper-stamina">💪 {{ h.stamina ?? '?' }}</span>
              </div>
              <button
                v-if="canRecall(h)"
                type="button"
                class="btn-recall"
                :disabled="recallingId === h.adventurer_id"
                @click="recallAdventurer(h.adventurer_id)"
              >
                {{ recallingId === h.adventurer_id ? '召回中…' : '召回' }}
              </button>
            </div>
          </div>
          <div v-else class="empty-helpers">暂无冒险者在位升级</div>
        </div>
      </template>

      <!-- ── 已建成建筑：升级（酒馆 Lv2 后显示） ── -->
      <div
        v-if="buildingDetail.status === 'built' && showBuildingUpgradeSection"
        class="building-upgrade-section"
      >
        <h5 class="section-title">⬆️ 建筑升级</h5>
        <div class="building-upgrade-card">
          <div class="building-upgrade-level">
            当前 Lv.{{ buildingInstanceLevel }}
            <span v-if="buildingInstanceLevel < buildingMaxLevel">→ Lv.{{ buildingInstanceLevel + 1 }}</span>
          </div>
          <!-- 下一等级效果预览：后端按当前等级返回；Lv2 旧配置继续回退根级 description。 -->
          <p v-if="buildingUpgradeDescription" class="building-upgrade-hint">
            {{ buildingUpgradeDescription }}
          </p>
          <p v-if="!buildingUpgradeUnlocked" class="building-upgrade-lock">
            🔒 需管理员解锁酒馆 Lv.2（全服存活冒险者达 {{ tavernUpgradeRequirement }} 名）
          </p>
          <template v-else-if="buildingUpgradeUnlocked">
            <p v-if="upgradeSectionLoading" class="building-upgrade-hint">加载升级信息…</p>
            <div v-else-if="upgradeMaterialEntries.length" class="donate-list building-upgrade-donate">
              <div
                v-for="[mat, need] in upgradeMaterialEntries"
                :key="mat"
                class="donate-row"
              >
                <div class="donate-row-head">
                  <span class="donate-mat-icon">{{ matIcon(String(mat)) }}</span>
                  <span class="donate-mat-name">{{ matLabel(String(mat)) }}</span>
                  <span class="donate-mat-progress">
                    {{ buildingDetail.upgrade_donations_summary?.[mat] ?? 0 }} / {{ need }}
                    <span v-if="(buildingDetail.my_upgrade_donations?.[mat] ?? 0) > 0" class="donate-mine">
                      （我 {{ buildingDetail.my_upgrade_donations?.[mat] }}）
                    </span>
                  </span>
                </div>
                <div
                  v-if="(buildingDetail.upgrade_donations_summary?.[mat] ?? 0) < Number(need)"
                  class="donate-form"
                >
                  <span v-if="myMaterialBalance(String(mat)) > 0" class="donate-have">
                    持有 {{ myMaterialBalance(String(mat)) }}
                  </span>
                  <span v-else class="donate-empty">无库存</span>
                  <input
                    v-model.number="upgradeDonateAmounts[mat]"
                    type="number"
                    min="1"
                    :max="Math.min(
                      myMaterialBalance(String(mat)),
                      Number(need) - (buildingDetail.upgrade_donations_summary?.[mat] ?? 0),
                    )"
                    class="donate-input"
                    placeholder="数量"
                  />
                  <button
                    type="button"
                    class="btn-primary btn-sm"
                    :disabled="donatingUpgrade || !upgradeDonateAmounts[mat] || upgradeDonateAmounts[mat]! <= 0"
                    @click="submitUpgradeDonate(String(mat))"
                  >
                    {{ donatingUpgrade ? '…' : '捐献' }}
                  </button>
                </div>
                <div v-else class="donate-full">✅ 已捐满</div>
              </div>
            </div>
            <button
              v-if="canStartBuildingUpgrade"
              type="button"
              class="advs1-submit building-upgrade-btn"
              :disabled="upgradingBuilding"
              @click="onUpgradeBuilding"
            >
              {{ upgradingBuilding ? '启动中…' : `🔨 开始升级至 Lv.${buildingInstanceLevel + 1}` }}
            </button>
            <p v-else class="building-upgrade-hint">材料捐满后可开始升级；开工后设施暂时不可用，在场冒险者会被送回宿舍</p>
          </template>
          <!-- 未解锁：只读预览按钮（不可点） -->
          <button
            v-else-if="!buildingUpgradeUnlocked"
            type="button"
            class="advs1-submit building-upgrade-btn"
            disabled
          >
            🔒 升级至 Lv.{{ buildingInstanceLevel + 1 }}（需酒馆 Lv.2）
          </button>
        </div>
      </div>

      <!-- ── 已建成建筑：任务栏 ── -->
      <TaskBoardPanel
        v-if="building.status === 'built' && building.template?.category === 'task'"
        :building="building"
        @refreshed="$emit('refreshed')"
      />

      <!-- ── 已建成建筑：隐秘拍卖行 ── -->
      <SecretAuctionPanel
        v-if="building.status === 'built' && handlerKey === 'secret_auction'"
      />

      <!-- ── 已建成建筑：商店（草药店 / 军需店） ── -->
      <ShopPanel
        v-if="building.status === 'built' && isShopBuilding"
        :building="building"
        @refreshed="$emit('refreshed')"
      />

      <!-- ── 已建成建筑：收购商行 ── -->
      <BuybackPanel
        v-if="building.status === 'built' && isBuybackBuilding"
        :building="building"
        @refreshed="$emit('refreshed')"
      />

      <!-- ── 已建成建筑：深渊赌坊 ── -->
      <GamblingPanel
        v-if="building.status === 'built' && isGamblingBuilding"
        :building="building"
        @refreshed="$emit('refreshed')"
      />

      <!-- ── 已建成建筑：深渊茶舍 ── -->
      <TeaHousePanel
        v-if="building.status === 'built' && isTeaHouseBuilding"
      />

      <!-- ── 已建成建筑：深渊温泉 ── -->
      <HotSpringPanel
        v-if="building.status === 'built' && isHotSpringBuilding"
      />

      <!-- ── 已建成建筑：保险局 ── -->
      <InsurancePanel
        v-if="building.status === 'built' && isInsuranceBuilding"
      />

      <!-- ── 已建成建筑：幸运大转盘 ── -->
      <LuckyWheelPanel
        v-if="building.status === 'built' && isLuckyWheelBuilding"
        :building="building"
      />

      <HiddenDungeonExchangePanel
        v-if="building.status === 'built' && isHiddenDungeonExchangeBuilding"
        :building="building"
        @refreshed="$emit('refreshed')"
      />

      <!-- ── 已建成建筑：英灵碑林 ── -->
      <TombstonePanel
        v-if="building.status === 'built' && isMemorialBuilding"
        :building-id="building.id"
      />

      <!-- ── 已建成建筑：鲜花店只解锁墓地内送花 ── -->
      <div v-if="building.status === 'built' && isFlowerShopBuilding" class="flower-shop-guidance">
        <strong>💐 鲜花店已营业</strong>
        <span>请前往英灵碑林，选择对应墓碑与鲜花价位后送出。鲜花没有数值效果，并会在 24 小时后枯萎。</span>
      </div>


      <!-- ── 已建成建筑：百艺阁 ── -->
      <section
        v-if="building.status === 'built' && isSkillSchoolBuilding && skillSchoolOccupantsView.length > 0"
        class="skill-school-occupants-section"
      >
        <h5 class="section-title">📖 阁中学艺（{{ skillSchoolOccupantsView.length }}）</h5>
        <div class="skill-school-occupant-list">
          <div
            v-for="learner in skillSchoolOccupantsView"
            :key="learner.id"
            class="skill-school-occupant"
            :class="{ mine: learner.isMine }"
          >
            <div class="skill-school-occupant-main">
              <span class="skill-school-occupant-name">{{ learner.name }}</span>
              <span v-if="learner.isMine" class="skill-school-occupant-mine">我的冒险者</span>
              <span class="skill-school-occupant-mode">{{ learner.modeLabel }}</span>
              <span class="skill-school-occupant-lesson">{{ learner.lessonLabel }}</span>
            </div>
            <span
              class="skill-school-occupant-timer"
              :class="{ due: learner.remainingSeconds === 0 }"
              aria-live="off"
            >
              {{ formatSkillSchoolRemaining(learner.remainingSeconds) }}
            </span>
          </div>
        </div>
      </section>

      <SkillSchoolPanel
        v-if="building.status === 'built' && isSkillSchoolBuilding && !myActiveUsage"
        :building="building"
        @refreshed="handleSkillSchoolRefreshed"
      />

      <!-- ── 已建成建筑：使用流程（采集/打工/修炼等） ── -->
      <template v-if="building.status === 'built' && usageConfig && !isShopBuilding && !isBuybackBuilding && !isGamblingBuilding && !isLuckyWheelBuilding && !isHiddenDungeonExchangeBuilding && !isMemorialBuilding && !isFlowerShopBuilding && !isTeaHouseBuilding && !isHotSpringBuilding && !isInsuranceBuilding && handlerKey !== 'secret_auction' && building.template?.category !== 'task'">
        <div class="usage-info-section">
          <h5 class="section-title">使用说明</h5>
          <div class="usage-stats" :class="{ 'usage-stats-stacked': usageStatCount === 3 }">
            <span v-if="usageConfig.gold_cost">💰 金币消耗：{{ usageConfig.gold_cost }}</span>
            <span v-if="usageConfig.usage_stamina_cost">💪 精力消耗：{{ usageConfig.usage_stamina_cost }}</span>
            <span v-if="usageInputMaterials">🔥 材料消耗：{{ usageInputMaterials }}</span>
            <span v-if="usageConfig.usage_duration_minutes_per_hp">
              ⏱️ 治疗时长：缺失 HP × {{ usageConfig.usage_duration_minutes_per_hp }} 分钟
            </span>
            <span v-else-if="usageConfig.usage_duration_minutes">⏱️ 使用时长：{{ usageConfig.usage_duration_minutes }}分钟</span>
            <span v-if="usageConfig.cooldown_hours">⏳ CD：{{ usageConfig.cooldown_hours }}小时</span>
            <span v-if="usageConfig.daily_limit">📅 每日：{{ buildingDetail.today_usage_count ?? 0 }}/{{ usageConfig.daily_limit }}次</span>
            <span v-if="usageConfig.required_stat && usageConfig.required_stat_min">
              📊 {{ statLabel(usageConfig.required_stat) }}需求：≥{{ usageConfig.required_stat_min }}
            </span>
          </div>
          <div v-if="cooldownRemainingSeconds > 0" class="usage-cooldown-notice">
            ⏳ {{ buildingDetail.my_adventurer?.name || '你的冒险者' }}还需等待
            <strong>{{ cooldownRemainingText }}</strong> 才能再次使用
          </div>
          <div v-if="trainingQuotaLine" class="usage-desc training-quota-hint">{{ trainingQuotaLine }}</div>
          <div v-if="usageConfig.description || rewardPreview" class="usage-summary">
            <span v-if="usageConfig.description" class="usage-summary-desc">{{ usageConfig.description }}</span>
            <span v-if="rewardPreview" class="reward-preview">🎁 预计获得：{{ rewardPreview }}</span>
          </div>
          <div v-if="usageConfig.rest_like_recovery" class="usage-desc recovery-hint">
            💤 特别提示：占用期间按初级宿舍速度恢复精力和血量，但不恢复特质。
          </div>
          <!-- 线索追查处：追查池概览（不依赖冒险者是否空闲，打开即可见） -->
          <div v-if="handlerKey === 'clue_bureau'" class="clue-bureau-overview">
            <div v-if="clueBureauPoolCount === 0" class="clue-empty">
              <span class="clue-empty-icon">🗺️</span>
              <span>追查池为空，请联系管理员补充可追查 Boss。</span>
            </div>
            <template v-else>
              <div class="clue-stats">
                <span class="clue-stat-pill">
                  <span class="clue-stat-icon">🗂️</span>
                  追查池
                  <strong>{{ clueBureauPoolCount }}<span class="clue-stat-cap">/{{ clueBureauPoolCap }}</span></strong>
                </span>
                <span
                  v-if="clueBureauGrantEligibleCount > 0"
                  class="clue-stat-pill clue-stat-pill--active"
                >
                  <span class="clue-stat-icon">🔎</span>
                  可掉落线索 <strong>{{ clueBureauGrantEligibleCount }}</strong> 名
                </span>
              </div>
              <ul v-if="clueBureauTargets.length > 0" class="clue-target-list">
                <li
                  v-for="t in clueBureauTargets"
                  :key="`${t.index}-${t.label}`"
                  class="clue-target"
                  :class="{ 'clue-target--eligible': t.clue_eligible !== false }"
                >
                  <span class="clue-target-mark">{{ t.clue_eligible !== false ? '🔍' : '👻' }}</span>
                  <div class="clue-target-info">
                    <span class="clue-target-name">{{ t.label }}</span>
                  </div>
                  <span
                    class="clue-target-badge"
                    :class="t.clue_eligible !== false ? 'clue-target-badge--hot' : 'clue-target-badge--cold'"
                  >
                    {{ t.clue_eligible !== false ? '可追查' : '已现身' }}
                  </span>
                </li>
              </ul>
              <p v-else class="clue-all-cleared">
                <span class="clue-empty-icon">🏆</span>
                追查池内 Boss 均已全服讨伐，仍可出门追查，但较难带回新线索。
              </p>
            </template>
          </div>
          <div v-if="isItemCraftHandler" class="risk-warning">
            <div class="risk-title">⚠️ 风险提示</div>
            <ul v-if="handlerKey === 'blacksmith'" class="risk-list">
              <li>锻造<strong>存在失败可能</strong></li>
              <li>成功则两件投入物均消耗并产出新装备</li>
              <li>失败时每件物品可能保留或损毁，取决于锻造过程中的实际情况</li>
              <li>锻造失败时，<strong>退还一半锻造金币</strong>（无论物品是否被消耗）</li>
              <li>锻造过程需消耗炉火炭与淬火液，<strong>精力不退还</strong></li>
            </ul>
            <ul v-else-if="handlerKey === 'tailor_workshop'" class="risk-list">
              <li>裁坊只做<strong>防具与饰品</strong>，使用针线、裁片、编织、层叠与纹饰等精细衣装工艺</li>
              <li>第一件是优先保留的主体，第二件是必须实际参与成品的辅材或副件</li>
              <li>裁制<strong>存在失败可能</strong>；成功消耗两件投入并产出一件衣装成品</li>
              <li>失败时每件投入可能保留或损毁，并<strong>退还一半裁制金币</strong></li>
              <li>裁制作业已耗用工坊辅料，<strong>精力不退还</strong></li>
            </ul>
            <ul v-else-if="handlerKey === 'alchemy_workshop'" class="risk-list">
              <li>炼金工房专炼<strong>药水、材料、工具</strong>，不产出武器防具（装备请去铁匠铺）</li>
              <li>炼金合成<strong>存在失败可能</strong></li>
              <li>成功则全部投入物各消耗 1 个并产出炼金产物（默认存入仓库）；同类物品可重复投入，总量 1~5 个</li>
              <li>投入<strong>明显不合理</strong>或与炼金无关时，材料可能<strong>全部损毁</strong></li>
              <li>普通失败时部分材料可能保留；炼金失败均<strong>退还一半金币</strong></li>
              <li>炼金过程消耗试剂与精力，<strong>精力不退还</strong></li>
            </ul>
            <ul v-else-if="handlerKey === 'ember_kitchen'" class="risk-list">
              <li>每次投入 <strong>1~5 份食材</strong>，并从灶坊自带炊具中选择一种做法</li>
              <li>只有每份材料都能按该做法确认安全入口时，才会做成可分次使用的<strong>消耗品</strong></li>
              <li>使用次数会按食材数量、形态与可分食程度决定，<strong>不会超过投入总份数</strong></li>
              <li>任一材料不可食、有害或无法确认可食时，只会得到当场丢弃的<strong>黑暗料理</strong>，不会留下物品</li>
              <li>经掌勺人完成并核准的结果会消耗全部食材、{{ usageConfig?.gold_cost ?? 0 }} 金币与 {{ usageConfig?.usage_stamina_cost ?? 0 }} 精力；若灶坊未能完成作业，食材原样保留、费用与精力退还且不记冷却</li>
            </ul>
            <ul v-else class="risk-list">
              <li>构装工坊需要选择 <strong>2~5 件物品</strong>，单件物品无法构装</li>
              <li>物品<strong>点击顺序</strong>会作为槽位顺序，并与排列方式一起影响结果</li>
              <li>成功则全部投入物各消耗 1 个并产出新物品（默认存入仓库）</li>
              <li>失败时部分或全部物品可能损毁；构装失败<strong>不返还金币</strong></li>
              <li>构装消耗高额金币与精力，<strong>无论成败均不返还</strong>；若工坊未能完成本次作业，则费用与投入原样退还</li>
            </ul>
          </div>
          <div v-if="handlerKey === 'chapel'" class="risk-warning">
            <div class="risk-title">⚠️ 风险提示</div>
            <ul class="risk-list">
              <li>修女力量有限，<strong>净化并非每次都能成功</strong></li>
              <li>祈祷需燃耗珍贵的圣油与圣徽碎片，<strong>无论成败金币和精力不退还</strong></li>
            </ul>
          </div>
        </div>

        <!-- 深渊拾荒队：出门队伍（所有玩家），点进来才看到谁出发了、出门多久、还有多久 -->
        <div v-if="handlerKey === 'scavenge' && scavengeOccupantsView.length > 0" class="scavenge-party-section">
          <h5 class="section-title">🧭 出门队伍（{{ scavengeOccupantsView.length }}）</h5>
          <div class="scavenge-party-list">
            <div
              v-for="m in scavengeOccupantsView"
              :key="m.id"
              :class="['scavenge-party-item', { mine: m.isMine }]"
            >
              <span class="sp-name">{{ m.name }}<span v-if="m.isMine" class="sp-mine-tag">（我的）</span></span>
              <span class="sp-hours">出门 {{ m.hours }}h</span>
              <span class="sp-remain">{{ formatScavengeRemaining(m.remainingSec) }}</span>
            </div>
          </div>
        </div>

        <!-- 使用中：仅当「我的冒险者」正在此建筑时才占满面板；多床位时他人占用不挡派遣 -->
        <div v-if="myActiveUsage" class="usage-active-section">
          <h5 class="section-title">使用中</h5>
          <div class="usage-active-card">
            <span class="active-adv-name">{{ myActiveUsage.adventurer_name }}</span>
            <span v-if="activeSkillSchoolName" class="active-skill-name">📜 {{ activeSkillSchoolName }}</span>
            <span class="active-stamina">💪 {{ myActiveUsage.stamina ?? '?' }}</span>
          </div>
          <p v-if="handlerKey === 'blacksmith' && activeBuilding.my_craft_inputs?.length" class="forge-active-materials">
            本次投入：{{ activeBuilding.my_craft_inputs.map(item => item.name + ' ×' + item.quantity).join(' + ') }}
          </p>
          <div class="usage-timer">
            <p v-if="isItemCraftHandler" class="usage-queue-hint">已开始制作，不能取消；只有排队候场时可以取消并取回全部预留资源。</p>
            <div class="progress-bar-lg">
              <div class="progress-fill-lg usage-progress" :style="{ width: usageProgressPercent + '%' }"></div>
            </div>
            <div class="remaining-text">
              <template v-if="usageRemainingSeconds > 0">剩余：{{ formatUsageRemaining }}</template>
              <template v-else-if="aiResultPending">{{ itemCraftPendingLabel }}</template>
              <template v-else>✅ 使用完成</template>
            </div>
          </div>

          <div v-if="handlerKey === 'skill_school'" class="skill-school-retry-assurance">
            <strong>🪶 技术异常补课保障</strong>
            <span>
              若这堂课因系统技术异常没能授成，原练法与入席时的技能资料会原样保留；
              结算释放后，本页会直接显示补课入口，免重新候场、免再等一堂课。
              导师明确判定无法传授的练法不属于技术补课。
            </span>
          </div>

          <!-- 医院/疗养所/手术室：租用期间可随时退出（不退费）；到期后疗养所也须能手动退房 -->
          <button
            v-if="handlerKey === 'hospital' || handlerKey === 'clinic' || handlerKey === 'sanatorium'"
            type="button"
            class="btn-recall btn-quit-sanatorium"
            :disabled="recallingUsage"
            @click="quitTreatmentBuilding"
          >
            {{ recallingUsage ? '退出中…' : (handlerKey === 'hospital' || handlerKey === 'clinic' ? '🚪 现在出院（不退费）' : '🚪 退出疗养（不退费）') }}
          </button>

          <!-- AI 异步结果等待中：禁止重复提交 -->
          <div v-if="aiResultPending" class="ai-pending-hint">
            {{ itemCraftPendingLabel }}
          </div>

          <!-- 铁匠铺：选择两个物品，由锻造流程判断结果 -->
          <!-- 非铁匠铺建筑：计时器到 0 后服务器自动结算，前端不显示手动按钮 -->
          <div v-else-if="usageRemainingSeconds <= 0" class="auto-settling-hint">
            {{ settlingHintText }}
          </div>
        </div>

        <!-- 候场态：资源与选择已冻结，空位释放后由服务端严格按 FIFO 自动入席 -->
        <div v-else-if="myUsageQueue" class="usage-queue-section">
          <h5 class="section-title">门外候场</h5>
          <div class="usage-queue-card">
            <div class="usage-queue-seal">⌛</div>
            <div class="usage-queue-copy">
              <strong>{{ detailMyAdventurer?.name }}正在等候空位</strong>
              <span>
                当前第 {{ myUsageQueue.position ?? '?' }} 位
                · 共 {{ myUsageQueue.waiting_count }} 人候场
              </span>
            </div>
          </div>
          <p class="usage-queue-hint">
            金币、精力与本次选择已经封存；轮到后自动入席并从那一刻开始计时。候场期间不能执行其他行为，取消会退回全部预留资源。
            <span v-if="isItemCraftHandler">进入制作倒计时后不能取消。</span>
          </p>
          <button
            type="button"
            class="btn-recall usage-queue-cancel"
            :disabled="cancellingUsageQueue"
            @click="cancelBuildingUsageQueue"
          >
            {{ cancellingUsageQueue ? '取消中…' : '离开队列并取回预留资源' }}
          </button>
        </div>

        <!-- 空闲态：百艺阁在 SkillSchoolPanel 选技能，此处不重复展示 -->
        <div v-else-if="handlerKey !== 'skill_school'" class="usage-select-section">
          <div v-if="adventurersLoading" class="empty-helpers">加载中…</div>
          <div v-else-if="!usageCandidate" class="empty-helpers">
            {{ handlerKey === 'pet_evolution'
              ? '没有携带可嬗变伙伴的空闲冒险者'
              : (store.adventurers.length === 0 ? '你还没有冒险者，请先创建' : '没有空闲的冒险者（需要空闲或休息状态）') }}
          </div>
          <template v-else>
            <!-- 嬗变祭坛：选择冒险者与具体 companion 实例 -->
            <div v-if="handlerKey === 'pet_evolution'" class="pet-evolution-section">
              <div class="section-title">🧬 选择要嬗变或孵化的伙伴</div>
              <label v-if="petEligibleAdventurers.length > 1" class="pet-adventurer-select">
                <span>冒险者</span>
                <select v-model.number="selectedPetAdventurerId" class="pet-select">
                  <option v-for="a in petEligibleAdventurers" :key="a.id" :value="a.id">{{ a.name }}</option>
                </select>
              </label>
              <div class="pet-list">
                <label
                  v-for="pet in evolvableCompanions"
                  :key="pet.id"
                  :class="['pet-option', { selected: selectedCompanionId === pet.id, 'pet-disabled': pet.is_player_locked || isPetCompanionBlockedByAux(pet.id) }]"
                >
                  <input
                    v-model.number="selectedCompanionId"
                    type="radio"
                    name="pet-companion"
                    :value="pet.id"
                    :disabled="pet.is_player_locked || isPetCompanionBlockedByAux(pet.id)"
                  />
                  <InventorySelectionItemSummary
                    :name="pet.name"
                    :rarity="pet.rarity"
                    :quantity="pet.quantity"
                    :meta="[itemTypeLabel(pet.type), `${pet.evolution_stage ?? 0}阶`, ...(pet.is_player_locked ? ['已保护'] : [])]"
                    :description="pet.desc"
                  />
                </label>
                <div v-if="evolvableCompanions.length === 0" class="empty-helpers">没有可嬗变的伙伴</div>
              </div>
              <div class="section-title" style="margin-top: 8px;">辅助素材（可不选）</div>
              <InventoryListToolbar
                v-if="petMaterialItems.length"
                v-model:rarity="petMaterialRarityFilter"
                v-model:sort="petMaterialSort"
                :items="petMaterialItems"
                aria-label="按稀有度筛选嬗变辅助素材"
              />
              <div class="pet-list pet-material-list">
                <label
                  :class="['pet-option', { selected: !selectedAuxiliaryMaterialKey }]"
                >
                  <input v-model="selectedAuxiliaryMaterialKey" type="radio" name="pet-aux-material" value="" />
                  <span class="pet-option-main">不使用辅助素材</span>
                </label>
                <label
                  v-for="item in visiblePetMaterialItems"
                  :key="petMaterialKey(item)"
                  :class="['pet-option', { selected: selectedAuxiliaryMaterialKey === petMaterialKey(item), 'pet-disabled': isPetAuxBlockedByCompanion(item) }]"
                >
                  <input
                    v-model="selectedAuxiliaryMaterialKey"
                    type="radio"
                    name="pet-aux-material"
                    :value="petMaterialKey(item)"
                    :disabled="isPetAuxBlockedByCompanion(item)"
                  />
                  <InventorySelectionItemSummary
                    :name="item.name"
                    :rarity="item.rarity"
                    :quantity="item.quantity"
                    :meta="[itemTypeLabel(item.type), item.from === 'carry' ? '随身' : '仓库', ...(item.is_player_locked ? ['已保护'] : [])]"
                    :description="item.desc"
                  />
                </label>
                <div v-if="petMaterialsLoading" class="empty-helpers">加载中…</div>
                <div v-else-if="petMaterialItems.length === 0" class="empty-helpers">没有可用素材</div>
                <div v-else-if="visiblePetMaterialItems.length === 0" class="empty-helpers">当前稀有度下没有可用素材</div>
              </div>
              <p class="pet-evolution-hint">
                本次费用 {{ petEvolutionGoldCost.toLocaleString() }} 金币。第 4 阶起逐阶翻倍；失败退还一半金币，伙伴不消耗，但辅助素材一经派遣即消耗且不返还。<br>
                伙伴类型的伙伴嬗变成功率更高；其他类型大概率失败。
              </p>
              <div v-if="selectedCompanionId && !isCompanionType" class="risk-warning">
                <div class="risk-title">⚠️ 风险提示</div>
                <ul class="risk-list">
                  <li>当前选择的伙伴<strong>不是伙伴类型</strong>，祭坛守护者大概率<strong>无法引导嬗变</strong></li>
                  <li>失败将<strong>扣除一半金币</strong>且不会退还辅助素材</li>
                </ul>
              </div>
            </div>
            <!-- 治疗所/小圣堂：选择要处理的特质 -->
            <div v-if="handlerKey === 'healing_house' || handlerKey === 'chapel'" class="trait-select-section">
              <div class="section-title" style="margin-bottom: 8px;">{{ handlerKey === 'chapel' ? '🙏 选择要净化的特质' : '🩺 选择要治疗的特质' }}</div>
              <div v-if="healableTraits.length === 0" class="empty-helpers">{{ handlerKey === 'chapel' ? '该冒险者没有可净化的负面特质' : '该冒险者没有可治疗的负面特质' }}</div>
              <div v-else class="trait-list">
                <label
                  v-for="t in healableTraits"
                  :key="t.name"
                  :class="['trait-option', { selected: selectedTrait === t.name }]"
                >
                  <input v-model="selectedTrait" type="radio" name="heal-target" :value="t.name" />
                  <span class="trait-name">{{ t.name }}</span>
                  <span v-if="t.desc" class="trait-desc">{{ t.desc }}</span>
                </label>
              </div>
            </div>
            <!-- 治疗所 Lv2：可选携带 1 件治疗物品辅助 -->
            <div
              v-if="handlerKey === 'healing_house' && healingItemTreatmentUnlocked"
              class="trait-select-section"
            >
              <div class="section-title" style="margin-bottom: 8px;">🧪 携带治疗物品（可选）</div>
              <div class="forge-hint">带 1 件物品，治疗师会判断它能否辅助疗伤：有用则消耗，无用原样归还。</div>
              <button
                v-if="!showHealingItemPicker"
                type="button"
                class="advs1-submit usage-complete-btn"
                :disabled="healingItemsLoading"
                @click="openHealingItemPicker"
              >
                {{ healingItemsLoading ? '加载物品中…' : (healingItemRef ? `已选「${healingItemRef.name}」· 点击更换` : '选择治疗物品') }}
              </button>
              <div v-else-if="!healingItems.length" class="empty-helpers">随身和仓库都没有可携带的物品</div>
              <template v-else>
                <InventoryListToolbar
                  v-model:rarity="healingItemRarityFilter"
                  v-model:sort="healingItemSort"
                  :items="healingItems"
                  aria-label="按稀有度筛选治疗辅助物品"
                />
                <div class="trait-list">
                  <label :class="['trait-option', { selected: !healingItemRef }]">
                    <input type="radio" name="heal-item" :checked="!healingItemRef" @change="healingItemRef = null" />
                    <span class="trait-name">不携带</span>
                  </label>
                  <label
                    v-for="it in visibleHealingItems"
                    :key="it.from + '-' + it.id"
                    :class="['trait-option', 'inventory-item-option', { selected: isHealingItemSelected(it), 'forge-disabled': it.is_player_locked }]"
                  >
                    <input type="radio" name="heal-item" :checked="isHealingItemSelected(it)" :disabled="it.is_player_locked" @change="selectHealingItem(it)" />
                    <InventorySelectionItemSummary
                      :name="it.name"
                      :rarity="it.rarity"
                      :quantity="it.quantity"
                      :meta="[it.from === 'carry' ? '随身' : '仓库', ...(it.is_player_locked ? ['已保护'] : [])]"
                      :description="it.desc"
                    />
                  </label>
                  <div v-if="visibleHealingItems.length === 0" class="empty-helpers">当前稀有度下没有可携带的物品</div>
                </div>
              </template>
            </div>
            <div v-if="isItemCraftHandler" class="forge-panel">
              <button
                v-if="!showForgePanel"
                type="button"
                class="advs1-submit usage-complete-btn"
                :disabled="forgeLoading"
                @click="openForgePanel"
              >
                {{ forgeLoading ? '加载物品中…' : itemCraftOpenPanelLabel }}
              </button>
              <template v-else>
                <h5 class="section-title">{{ itemCraftPanelTitle }}</h5>
                <div class="forge-hint">{{ itemCraftPanelHint }}</div>
                <template v-if="forgeItems.length">
                  <div class="forge-search-row">
                    <input
                      v-model="forgeSearchQuery"
                      type="search"
                      class="advs1-input forge-search-input"
                      placeholder="搜索物品名称或描述…"
                      aria-label="搜索可投入物品"
                      autocomplete="off"
                      @keydown.esc="forgeSearchQuery = ''"
                    />
                    <button
                      v-if="forgeSearchActive"
                      type="button"
                      class="forge-search-clear"
                      @click="forgeSearchQuery = ''"
                    >清空</button>
                  </div>
                  <h5 class="section-title forge-subtitle">随身物品</h5>
                  <div v-if="visibleForgeCarryItems.length" class="forge-list">
                    <div
                      v-for="it in visibleForgeCarryItems"
                      :key="'start-fi-carry-' + it.id"
                      :class="forgeItemClasses(it)"
                    >
                      <div class="forge-qty-stepper">
                        <button
                          type="button"
                          class="forge-qty-btn"
                          :aria-label="`减少 ${it.name} 的投入数量`"
                          :disabled="getForgeItemQty(it) <= 0"
                          @click="adjustForgeItemQty(it, -1)"
                        >−</button>
                        <span class="forge-qty-value">{{ getForgeItemQty(it) }}</span>
                        <button
                          type="button"
                          class="forge-qty-btn"
                          :aria-label="`增加 ${it.name} 的投入数量`"
                          :disabled="!canIncreaseForgeItemQty(it)"
                          @click="adjustForgeItemQty(it, 1)"
                        >+</button>
                      </div>
                      <InventorySelectionItemSummary
                        :name="it.name"
                        :rarity="it.rarity"
                        :quantity="it.quantity"
                        :state="it.is_player_locked ? '已保护' : (handlerKey === 'item_assembly_workshop' ? forgeItemStateSummary(it) : '')"
                        :description="it.desc"
                      />
                    </div>
                  </div>
                  <div v-else-if="!forgeCarryItems.length" class="empty-helpers">随身没有可用于{{ itemCraftActionLabel }}的物品</div>
                  <div v-else class="empty-helpers">随身没有匹配“{{ forgeSearchQuery.trim() }}”的物品</div>

                  <h5 class="section-title forge-subtitle">仓库物品</h5>
                  <InventoryListToolbar
                    v-if="forgeWarehouseItems.length"
                    v-model:rarity="forgeWarehouseRarityFilter"
                    v-model:sort="forgeWarehouseSort"
                    :items="searchFilteredForgeWarehouseItems"
                    :aria-label="`按稀有度筛选可用于${itemCraftActionLabel}的仓库物品`"
                  />
                  <div v-if="visibleForgeWarehouseItems.length" class="forge-list">
                    <div
                      v-for="it in visibleForgeWarehouseItems"
                      :key="'start-fi-warehouse-' + it.id"
                      :class="forgeItemClasses(it)"
                    >
                      <div class="forge-qty-stepper">
                        <button
                          type="button"
                          class="forge-qty-btn"
                          :aria-label="`减少 ${it.name} 的投入数量`"
                          :disabled="getForgeItemQty(it) <= 0"
                          @click="adjustForgeItemQty(it, -1)"
                        >−</button>
                        <span class="forge-qty-value">{{ getForgeItemQty(it) }}</span>
                        <button
                          type="button"
                          class="forge-qty-btn"
                          :aria-label="`增加 ${it.name} 的投入数量`"
                          :disabled="!canIncreaseForgeItemQty(it)"
                          @click="adjustForgeItemQty(it, 1)"
                        >+</button>
                      </div>
                      <InventorySelectionItemSummary
                        :name="it.name"
                        :rarity="it.rarity"
                        :quantity="it.quantity"
                        :state="it.is_player_locked ? '已保护' : (handlerKey === 'item_assembly_workshop' ? forgeItemStateSummary(it) : '')"
                        :description="it.desc"
                      />
                    </div>
                  </div>
                  <div v-else-if="!forgeWarehouseItems.length" class="empty-helpers">仓库没有可用于{{ itemCraftActionLabel }}的物品</div>
                  <div v-else-if="forgeSearchActive && !searchFilteredForgeWarehouseItems.length" class="empty-helpers">
                    仓库没有匹配“{{ forgeSearchQuery.trim() }}”的物品
                  </div>
                  <div v-else class="empty-helpers">当前稀有度下没有可用于{{ itemCraftActionLabel }}的仓库物品</div>
                </template>
                <div v-else class="empty-helpers">没有可用于{{ itemCraftActionLabel }}的物品</div>
                <div class="forge-selection-status">
                  已选择 {{ forgeSelectionCount }}/{{ craftMaxSelection }}
                  {{ handlerKey === 'ember_kitchen' ? '份' : '件' }}
                </div>
                <div v-if="handlerKey === 'blacksmith' && expandedForgeSelectedRefs.length" class="forge-selection-status">
                  主物品：{{ expandedForgeSelectedRefs[0]?.name }}
                  <span v-if="expandedForgeSelectedRefs[1]"> · 锻造材料：{{ expandedForgeSelectedRefs[1].name }}</span>
                </div>
                <div v-if="handlerKey === 'tailor_workshop' && expandedForgeSelectedRefs.length" class="forge-selection-status">
                  衣装主体：{{ expandedForgeSelectedRefs[0]?.name }}
                  <span v-if="expandedForgeSelectedRefs[1]"> · 辅材或副件：{{ expandedForgeSelectedRefs[1].name }}</span>
                </div>
                <div v-if="handlerKey === 'blacksmith'" class="forge-intent-section">
                  <h5 class="section-title forge-subtitle">选择锻造取向</h5>
                  <p class="forge-hint">取向只决定铁匠优先尝试的方向；最终仍由两件投入和实际工艺决定，不能保证成功或升档。</p>
                  <div class="forge-intent-grid">
                    <label
                      v-for="intent in BLACKSMITH_FORGE_INTENTS"
                      :key="intent.code"
                      :class="['forge-intent-option', { selected: selectedForgeIntent === intent.code }]"
                    >
                      <input
                        v-model="selectedForgeIntent"
                        type="radio"
                        name="blacksmith-forge-intent"
                        :value="intent.code"
                      />
                      <span class="forge-intent-copy">
                        <strong>{{ intent.name }}</strong>
                        <span>{{ intent.description }}</span>
                      </span>
                    </label>
                  </div>
                </div>
                <div v-if="handlerKey === 'tailor_workshop'" class="forge-intent-section">
                  <h5 class="section-title forge-subtitle">选择裁制方案</h5>
                  <p class="forge-hint">方案只决定裁缝师优先尝试的方向；最终仍由两件投入和精细衣装工艺决定，不能保证成功或升档。</p>
                  <div class="forge-intent-grid">
                    <label
                      v-for="operation in TAILOR_WORKSHOP_OPERATIONS"
                      :key="operation.code"
                      :class="['forge-intent-option', { selected: selectedTailorOperation === operation.code }]"
                    >
                      <input
                        v-model="selectedTailorOperation"
                        type="radio"
                        name="tailor-workshop-operation"
                        :value="operation.code"
                      />
                      <span class="forge-intent-copy">
                        <strong>{{ operation.name }}</strong>
                        <span>{{ operation.description }}</span>
                      </span>
                    </label>
                  </div>
                </div>
                <div v-if="handlerKey === 'item_assembly_workshop'" class="assembly-layout-section">
                  <label class="assembly-label">
                    <span>排列方式</span>
                    <select v-model="assemblyLayoutMode" class="advs1-input assembly-select">
                      <option value="vertical_line">从上到下一条直线</option>
                      <option value="cross">上下左右围绕中心</option>
                      <option value="inside_out">从里到外</option>
                      <option value="left_to_right">从左到右</option>
                      <option value="ring">环形闭环</option>
                    </select>
                  </label>
                  <label class="assembly-label">
                    <span>期望打造方向（可选，最多 {{ ASSEMBLY_DIRECTION_MAX_LENGTH }} 字）</span>
                    <input
                      v-model="assemblyDesiredDirection"
                      class="advs1-input"
                      type="text"
                      :maxlength="ASSEMBLY_DIRECTION_MAX_LENGTH"
                      placeholder="描述希望偏向的用途或形态，超出素材时构装师会收束为可实现版本"
                    />
                  </label>
                  <div v-if="expandedForgeSelectedRefs.length" class="assembly-slots-preview">
                    <p class="forge-hint">每份材料独立占一个槽位，可前移或后移；同一材料也能分开放置。</p>
                    <div
                      v-for="(ref, idx) in expandedForgeSelectedRefs"
                      :key="forgeItemKey(ref) + '-' + idx"
                      class="assembly-slot"
                    >
                      <span class="assembly-slot-index">{{ idx + 1 }}</span>
                      <span class="assembly-slot-pos">{{ assemblySlotPosition(idx) }}</span>
                      <span class="assembly-slot-item">
                        <span class="assembly-slot-name">{{ ref.name }}</span>
                        <span v-if="forgeRefStateSummary(ref)" class="assembly-slot-state">
                          {{ forgeRefStateSummary(ref) }}
                        </span>
                      </span>
                      <div class="assembly-slot-actions">
                        <button type="button" class="forge-qty-btn" :disabled="idx === 0" :aria-label="`将第 ${idx + 1} 槽的 ${ref.name} 前移`" @click="moveAssemblySlot(idx, -1)">↑</button>
                        <button type="button" class="forge-qty-btn" :disabled="idx === expandedForgeSelectedRefs.length - 1" :aria-label="`将第 ${idx + 1} 槽的 ${ref.name} 后移`" @click="moveAssemblySlot(idx, 1)">↓</button>
                      </div>
                    </div>
                  </div>
                </div>
                <div v-if="handlerKey === 'ember_kitchen'" class="cookware-section">
                  <h5 class="section-title forge-subtitle">选择灶坊炊具</h5>
                  <p class="forge-hint">炊具由建筑提供，不会被消耗；它只决定做法，不能让不可食材料变得可食。</p>
                  <div class="cookware-grid">
                    <label
                      v-for="cookware in EMBER_KITCHEN_COOKWARE"
                      :key="cookware.code"
                      :class="['cookware-option', { selected: selectedCookwareCode === cookware.code }]"
                    >
                      <input
                        v-model="selectedCookwareCode"
                        type="radio"
                        name="ember-kitchen-cookware"
                        :value="cookware.code"
                      />
                      <span class="cookware-icon">{{ cookware.icon }}</span>
                      <span class="cookware-copy">
                        <strong>{{ cookware.name }}</strong>
                        <span>{{ cookware.description }}</span>
                      </span>
                    </label>
                  </div>
                </div>
                <button
                  type="button"
                  class="advs1-submit usage-dispatch-btn-block"
                  :disabled="dispatchingUsage || !canUseBuilding(usageCandidate).ok || !craftSelectionValid"
                  @click="dispatchUsage"
                >
                  {{ dispatchButtonLabel }}
                </button>
              </template>
            </div>
            <!-- 深渊食堂：选餐 -->
            <div v-if="handlerKey === 'dining_hall' && usageCandidate" class="dining-section">
              <div class="section-title">🍲 点一份餐</div>
              <p v-if="usageCandidate.expedition_buff?.label" class="dining-hint subtle">
                当前已有加成「{{ usageCandidate.expedition_buff.label }}」，再次用餐将覆盖。
              </p>
              <p v-if="buildingOccupants.length > 0" class="dining-hint subtle">
                当前 {{ buildingOccupants.length }}/{{ buildingUsageSlots }} 个用餐位使用中
              </p>
              <div v-if="diningMenuLoading" class="empty-helpers">加载菜单中…</div>
              <div v-else-if="diningMenuItems.length" class="dining-menu-list">
                <label
                  v-for="item in diningMenuItems"
                  :key="item.code"
                  class="dining-menu-item"
                  :class="{ selected: selectedFoodCode === item.code }"
                >
                  <input
                    v-model="selectedFoodCode"
                    type="radio"
                    name="dining-food"
                    :value="item.code"
                  />
                  <span class="dining-item-main">
                    <span class="dining-item-title">{{ item.icon }} {{ item.name }} · 💰 {{ item.gold_cost }}</span>
                    <span class="dining-item-meta">{{ item.buff_label }}</span>
                    <span class="dining-item-desc">{{ item.description }}</span>
                  </span>
                </label>
              </div>
              <div v-else class="empty-helpers">暂无可用餐品</div>
              <p v-if="selectedDiningItem" class="dining-hint">
                用餐约 {{ usageConfig?.usage_duration_minutes ?? 10 }} 分钟 · 冷却 {{ usageConfig?.cooldown_hours ?? 4 }} 小时 · 扣费 💰 {{ selectedDiningItem.gold_cost }}
              </p>
            </div>
            <p
              v-if="isLowConsumptionBuilding && usageCandidate"
              class="hospital-hint low-consumption-hint"
            >
              🪙 低消费临时金 {{ (usageCandidate.low_consumption_gold ?? 0).toFixed(2) }}
              / {{ (usageCandidate.low_consumption_gold_cap ?? 100).toFixed(0) }}；
              可与非负金币合并支付，本建筑会先扣金币、再用临时金补足。
            </p>
            <!-- 医院：自定义治疗金额 -->
            <div v-if="handlerKey === 'hospital' && usageCandidate" class="hospital-hint-section">
              <div class="section-title">⚕️ 住院治疗</div>
              <p class="hospital-hint">
                当前 ❤️ {{ liveHp(usageCandidate) }}/{{ usageCandidate.max_hp }}，
                缺失 {{ hospitalMissingHp(usageCandidate) }} 点 · 单价 {{ usageConfig?.gold_cost_per_hp ?? 2 }} 金币/点
              </p>
              <div class="hospital-cost-row">
                <label class="hospital-cost-label" for="hospital-treatment-gold">治疗花费（金币）</label>
                <input
                  id="hospital-treatment-gold"
                  v-model.number="hospitalTreatmentGold"
                  type="number"
                  class="donate-input hospital-cost-input"
                  :min="usageConfig?.gold_cost_per_hp ?? 2"
                  :max="hospitalMaxAffordableGold(usageCandidate)"
                  step="1"
                />
                <button
                  type="button"
                  class="btn-primary btn-sm"
                  @click="hospitalTreatmentGold = hospitalMaxAffordableGold(usageCandidate)"
                >
                  满血
                </button>
              </div>
              <p class="hospital-hint">
                实际扣费 💰 {{ hospitalActualCost(usageCandidate) }}，
                恢复 ❤️ {{ hospitalHealHp(usageCandidate) }} 点，
                时长 {{ hospitalSelectedDuration(usageCandidate) }} 分钟
              </p>
              <p v-if="buildingOccupants.length > 0" class="hospital-hint subtle">
                当前 {{ buildingOccupants.length }}/{{ buildingUsageSlots }} 张病床使用中
              </p>
              <p class="hospital-hint subtle">本建筑共 {{ buildingUsageSlots }} 张病床，可同时收治多名冒险者。</p>
            </div>
            <!-- 疗养所：按小时租用床位 -->
            <div v-if="handlerKey === 'sanatorium' && usageCandidate" class="hospital-hint-section">
              <div class="section-title">🌿 疗养入住</div>
              <p class="hospital-hint">
                加速负面特质自然恢复：宿舍 6 小时 1 周期，疗养所仅需 3 小时。
                单价 {{ usageConfig?.gold_cost_per_hour ?? 10 }} 金币/小时
              </p>
              <div class="hospital-cost-row">
                <label class="hospital-cost-label" for="sanatorium-rental-hours">租用时长（小时）</label>
                <input
                  id="sanatorium-rental-hours"
                  v-model.number="sanatoriumRentalHours"
                  type="number"
                  class="donate-input hospital-cost-input"
                  :min="1"
                  :max="sanatoriumMaxAffordableHours(usageCandidate)"
                  step="1"
                />
                <button
                  type="button"
                  class="btn-primary btn-sm"
                  @click="sanatoriumRentalHours = sanatoriumMaxAffordableHours(usageCandidate)"
                >
                  最长
                </button>
              </div>
              <p class="hospital-hint">
                实际扣费 💰 {{ sanatoriumActualCost(usageCandidate) }}，
                时长 {{ sanatoriumSelectedDuration(usageCandidate) }} 分钟
              </p>
              <p v-if="buildingOccupants.length > 0" class="hospital-hint subtle">
                当前 {{ buildingOccupants.length }}/{{ buildingUsageSlots }} 张床位使用中
              </p>
              <p class="hospital-hint subtle">本建筑共 {{ buildingUsageSlots }} 张床位，可同时入住多名冒险者。</p>
            </div>
            <!-- 深渊手术室：按分钟付费回血 -->
            <div v-if="handlerKey === 'clinic' && usageCandidate" class="hospital-hint-section">
              <div class="section-title">🔪 入院治疗</div>
              <p class="hospital-hint">
                当前 ❤️ {{ liveHp(usageCandidate) }}/{{ usageCandidate.max_hp }}，
                每分钟回复 {{ clinicHpPerMinuteDisplay(usageCandidate) }} 点（最大生命 {{ usageConfig?.hp_percent_per_minute ?? 1 }}%）·
                单价 {{ usageConfig?.gold_cost_per_minute ?? 10 }} 金币/分钟
              </p>
              <div class="hospital-cost-row">
                <label class="hospital-cost-label" for="clinic-rental-minutes">驻留时长（分钟）</label>
                <input
                  id="clinic-rental-minutes"
                  v-model.number="clinicRentalMinutes"
                  type="number"
                  class="donate-input hospital-cost-input"
                  :min="1"
                  :max="clinicMaxAffordableMinutes(usageCandidate)"
                  step="1"
                />
                <button
                  type="button"
                  class="btn-primary btn-sm"
                  @click="clinicRentalMinutes = clinicMaxAffordableMinutes(usageCandidate)"
                >
                  满血
                </button>
              </div>
              <p class="hospital-hint">
                实际扣费 💰 {{ clinicActualCost(usageCandidate) }}，
                恢复 ❤️ {{ clinicHealPreview(usageCandidate) }} 点，
                时长 {{ clinicSelectedDuration(usageCandidate) }} 分钟
              </p>
              <p v-if="buildingOccupants.length > 0" class="hospital-hint subtle">
                当前 {{ buildingOccupants.length }}/{{ buildingUsageSlots }} 张监护床位使用中
              </p>
              <p class="hospital-hint subtle">本建筑共 {{ buildingUsageSlots }} 张监护床位，可同时收治多名冒险者。</p>
            </div>
            <!-- 线索追查处：随机 Boss + 随机线索 -->
            <div v-if="handlerKey === 'clue_bureau' && usageCandidate" class="hospital-hint-section clue-search-card">
              <div class="section-title">🔍 线索追查</div>
              <p class="hospital-hint">
                出门追查隐藏 Boss 线索：每小时消耗
                <strong>{{ usageConfig?.stamina_cost_per_hour ?? 20 }}</strong> 精力，
                有机会随机获得一条（入随身装备）。
                四条线索可由小队成员分别携带，凑齐后前往对应副本出发即可触发隐藏 Boss。
              </p>
              <p class="hospital-hint subtle">
                讨伐成功结算时消耗对应线索并向持有者兑现金币：每条 300 金；
                若同一 Boss 有多条重复线索，合计上限 1200 金，按本次消耗的线索条数平分。
              </p>
              <div class="clue-search-controls">
                <label class="hospital-cost-label" for="clue-bureau-search-hours">出门时长（小时）</label>
                <input
                  id="clue-bureau-search-hours"
                  v-model.number="clueBureauSearchHours"
                  type="number"
                  class="donate-input hospital-cost-input"
                  :min="1"
                  :max="clueBureauMaxAffordableHours(usageCandidate)"
                  step="1"
                />
                <button
                  type="button"
                  class="btn-primary btn-sm"
                  @click="clueBureauSearchHours = clueBureauMaxAffordableHours(usageCandidate)"
                >
                  最长
                </button>
              </div>
              <div class="clue-search-summary">
                <span class="clue-search-chip">💪 消耗 {{ clueBureauStaminaCost(usageCandidate) }} 精力</span>
                <span class="clue-search-chip">⏱️ 出门 {{ clueBureauEffectiveHours(usageCandidate) }} 小时</span>
              </div>
              <p v-if="buildingUsageSlots > 0" class="hospital-hint subtle">
                当前 {{ buildingOccupants.length }}/{{ buildingUsageSlots }} 个出发位使用中
              </p>
            </div>
            <!-- 深渊拾荒队：选出门时长，归来随机带回不同稀有度战利品 -->
            <div v-if="handlerKey === 'scavenge' && usageCandidate" class="hospital-hint-section">
              <div class="section-title">🔦 出门搜寻</div>
              <p class="hospital-hint">
                选择出门时长，归来时随机带回一批不同稀有度的战利品（出门越久收获越多），甚至小概率可获得特殊机缘副本的门票。
                单价 {{ usageConfig?.gold_cost_per_hour ?? 20 }} 金币/小时 · 精力 {{ usageConfig?.usage_stamina_cost ?? 0 }}
              </p>
              <div class="hospital-cost-row">
                <label class="hospital-cost-label" for="scavenge-search-hours">出门时长（小时）</label>
                <input
                  id="scavenge-search-hours"
                  v-model.number="scavengeSearchHours"
                  type="number"
                  class="donate-input hospital-cost-input"
                  :min="1"
                  :max="scavengeMaxAffordableHours(usageCandidate)"
                  step="1"
                />
                <button
                  type="button"
                  class="btn-primary btn-sm"
                  @click="scavengeSearchHours = scavengeMaxAffordableHours(usageCandidate)"
                >
                  最长
                </button>
              </div>
              <p class="hospital-hint">
                实际扣费 💰 {{ scavengeActualCost(usageCandidate) }} · 出门 {{ scavengeEffectiveHours(usageCandidate) }} 小时后归来
              </p>
              <p v-if="buildingUsageSlots > 0" class="hospital-hint subtle">
                当前 {{ buildingOccupants.length }}/{{ buildingUsageSlots }} 个出发位使用中
              </p>
            </div>
            <button
              v-if="!isItemCraftHandler"
              type="button"
              class="advs1-submit usage-dispatch-btn-block"
              :disabled="dispatchingUsage || !canUseBuilding(usageCandidate).ok"
              @click="dispatchUsage"
            >
              {{ dispatchButtonLabel }}
            </button>
            <div class="dispatch-status">
              {{ usageCandidate.name }} · ❤️ {{ liveHp(usageCandidate) }}/{{ usageCandidate.max_hp }}
              · 💪 {{ liveStamina(usageCandidate) }}/{{ usageCandidate.max_stamina }}
              <span v-if="!canUseBuilding(usageCandidate).ok" class="dispatch-reason">
                — {{ canUseBuilding(usageCandidate).reason }}
              </span>
            </div>
          </template>
        </div>
      </template>

      <!-- ── 贡献者排行弹窗（共享组件，与宿舍区统一样式） ── -->
      <ContributorsModal
        v-if="showContributors && building.id && !isAssessmentPreview"
        :building-id="building.id"
        :show-naming-crown="isDormitoryBuilding"
        @close="showContributors = false"
      />

      <div class="detail-actions s1-mobile-dialog-footer">
        <S1MoreActions v-if="showMoreActions" :key="building.id">
          <button
            v-if="building.status === 'locked' && isAdmin && building.id"
            type="button"
            class="btn-toggle-off"
            :disabled="toggling"
            @click="toggleDisable"
          >
            {{ toggling ? '处理中…' : '🚫 隐藏建筑' }}
          </button>
          <button
            v-if="canFillMaterials"
            type="button"
            class="btn-gm"
            :disabled="fillingMaterials"
            @click="fillMaterials"
          >
            {{ fillingMaterials ? '补材料中…' : '🪵 GM 补满材料' }}
          </button>
          <button
            v-if="building.status === 'locked' && isAdmin"
            type="button"
            class="btn-gm"
            :disabled="completing"
            @click="completeBuild"
          >
            {{ completing ? '建成中…' : '⏩ GM 一键建成' }}
          </button>
          <button
            v-if="building.status === 'building' && isAdmin"
            type="button"
            class="btn-gm"
            :disabled="completing"
            @click="completeBuild"
          >
            {{ completing ? '建成中…' : '⏩ GM 一键建成' }}
          </button>
          <button
            v-if="building.status === 'building' && isAdmin"
            type="button"
            class="btn-reject"
            :disabled="resetting"
            @click="resetBuild"
          >
            {{ resetting ? '取消中…' : '🔄 取消建造' }}
          </button>
          <button
            v-if="displayStatus === 'upgrading' && isAdmin"
            type="button"
            class="btn-gm"
            :disabled="completing"
            @click="completeBuild"
          >
            {{ completing ? '完成中…' : '⏩ GM 一键升级完成' }}
          </button>
          <button
            v-if="building.status === 'built' && isAdmin && !isAssessmentPreview"
            type="button"
            :class="building.disabled ? 'btn-toggle-on' : 'btn-toggle-off'"
            :disabled="toggling"
            @click="toggleDisable"
          >
            {{ toggling ? '处理中…' : (building.disabled ? '✅ 启用建筑' : '🚫 禁用建筑') }}
          </button>
          <button
            v-if="building.id && !isAssessmentPreview"
            type="button"
            class="btn-contributors"
            @click="showContributors = true"
          >
            📊 贡献者
          </button>
        </S1MoreActions>
        <button
          v-if="building.status === 'locked' && isAdmin && building.id"
          type="button"
          class="advs1-submit"
          :disabled="starting"
          @click="startBuild"
        >
          {{ starting ? '启动中…' : (hasRequiredMaterials ? '📣 开放玩家捐献' : '🔨 开始建造') }}
        </button>
        <button
          v-if="building.status === 'building'"
          type="button"
          class="advs1-submit"
          :disabled="sendingHelp"
          @click="sendHelp"
        >
          {{ sendingHelp ? '派遣中…' : '🧑‍🤝‍🧑 派遣帮忙' }}
        </button>
        <button
          v-if="displayStatus === 'upgrading'"
          type="button"
          class="advs1-submit"
          :disabled="sendingHelp"
          @click="sendHelp"
        >
          {{ sendingHelp ? '派遣中…' : '🧑‍🤝‍🧑 派遣帮忙' }}
        </button>
        <button type="button" class="btn-cancel" @click="$emit('close')">关闭</button>
      </div>
    </div>

  </div>
</template>

<script setup lang="ts">
import { ref, computed, watch, onMounted, onUnmounted, reactive } from 'vue'
import { useAdventurerS1Store, type BuildingInstance, type AdventurerData, type BuildingHelper } from '../stores/adventurerS1'
import { useUserStore } from '@/stores/user'
import { useAdminSecondaryAccess } from '@/composables/useAdminSecondaryAccess'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import { useSocket } from '@/composables/useSocket'
import { useLiveTick } from '../composables/useLiveTick'
import { useBuildingConstructionProgress } from '../composables/useBuildingConstructionProgress'
import { itemTypeLabel, liveHpOf, liveStaminaOf, calcWorkStatOverflowBonus, calculatePetEvolutionGoldCost, computeEffectiveUsageSlots, formatBuildingRemaining, formatDurationSec, formatHelperSpeedupHours, formatPetEvolutionDispatchConfirm, formatUsageSlotsFullMessage, mergeBuildingConstructionTiming, resolvePetEvolutionEquipment, S1_ITEM_TYPE_COMPANION, buildingUsageStartMs, TRANSCENDENT_ASSESSMENT_PREVIEW_DESCRIPTION, TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY, TRANSCENDENT_ASSESSMENT_PREVIEW_NAME } from '../utils'
import TaskBoardPanel from './TaskBoardPanel.vue'
import AmbientNpcEncounter from './AmbientNpcEncounter.vue'
import ShopPanel from './ShopPanel.vue'
import BuybackPanel from './BuybackPanel.vue'
import GamblingPanel from './GamblingPanel.vue'
import LuckyWheelPanel from './LuckyWheelPanel.vue'
import HiddenDungeonExchangePanel from './HiddenDungeonExchangePanel.vue'
import TombstonePanel from './TombstonePanel.vue'
import SkillSchoolPanel from './SkillSchoolPanel.vue'
import TeaHousePanel from './TeaHousePanel.vue'
import HotSpringPanel from './HotSpringPanel.vue'
import InsurancePanel from './InsurancePanel.vue'
import SecretAuctionPanel from './SecretAuctionPanel.vue'
import ContributorsModal from './ContributorsModal.vue'
import S1MoreActions from './S1MoreActions.vue'
import InventoryListToolbar from './InventoryListToolbar.vue'
import InventorySelectionItemSummary from './InventorySelectionItemSummary.vue'
import { useInventoryListControls } from '../composables/useInventoryListControls'

const props = defineProps<{
  building: BuildingInstance
}>()

const emit = defineEmits<{
  close: []
  refreshed: []
}>()

const store = useAdventurerS1Store()
const userStore = useUserStore()
const message = useGlobalMessage()

// 本地建筑详情副本（built 建筑需单独拉取 helpers）
const buildingDetail = ref<BuildingInstance>(props.building)

const activeBuilding = computed(() => (
  buildingDetail.value?.id != null ? buildingDetail.value : props.building
))

function applyBuildingConstructionPatch(patch: Partial<BuildingInstance> | null | undefined) {
  if (!patch?.id || patch.id !== buildingDetail.value.id) return
  const next = { ...buildingDetail.value, ...patch }
  buildingDetail.value = mergeBuildingConstructionTiming(next, patch)
}

watch(
  () => props.building,
  (b) => {
    if (!b?.id || buildingDetail.value.id !== b.id) return
    const detailedHelpers = new Map(
      (buildingDetail.value.helpers ?? [])
        .filter(h => h.building_assigned_at !== undefined || h.building_usage_extra !== undefined)
        .map(h => [h.adventurer_id, h]),
    )
    mergeBuildingConstructionTiming(buildingDetail.value, b)
    if (b.helpers) {
      // 列表接口只返回精简 helper；保留详情接口提供的使用起点与参数快照。
      buildingDetail.value.helpers = b.helpers.map((summary) => {
        const detailed = detailedHelpers.get(summary.adventurer_id)
        const incomingHasUsageDetail = (
          summary.building_assigned_at !== undefined
          || summary.building_usage_extra !== undefined
        )
        return detailed && !incomingHasUsageDetail
          ? { ...detailed, ...summary }
          : summary
      })
    }
  },
  { deep: true },
)

const { canUseFullAdminUi: isAdmin } = useAdminSecondaryAccess()
const starting = ref(false)
const resetting = ref(false)
const completing = ref(false)
const toggling = ref(false)
const fillingMaterials = ref(false)
const recallingId = ref<number | null>(null)
const donating = ref(false)
const donateAmounts = reactive<Record<string, number | null>>({})
const upgradeDonateAmounts = reactive<Record<string, number | null>>({})
const sendingHelp = ref(false)
const donatingUpgrade = ref(false)
const { now } = useLiveTick()

const showContributors = ref(false)
const showMoreActions = computed(() => (
  (!!props.building.id && !isAssessmentPreview.value)
  || canFillMaterials.value
  || (isAdmin.value && (
    props.building.status === 'locked'
    || props.building.status === 'building'
    || displayStatus.value === 'upgrading'
    || (props.building.status === 'built' && !isAssessmentPreview.value)
  ))
))
const upgradingBuilding = ref(false)
const upgradeSectionLoading = ref(true)

const buildingInstanceLevel = computed(() => buildingDetail.value.upgrade_level ?? 1)
const displayStatus = computed(() => buildingDetail.value.status ?? props.building.status)
const buildingUpgradeUnlocked = computed(() => !!store.tavernUpgrade?.building_upgrade_unlocked)
const buildingMaxLevel = computed(() => (
  buildingDetail.value.upgrade_max_level
  ?? buildingDetail.value.template?.upgrade_config?.max_level
  ?? store.tavernUpgrade?.building_max_level
  ?? 1
))
const tavernUpgradeRequirement = computed(() => store.tavernUpgrade?.next_level_requirement ?? 64)
const hasUpgradeConfig = computed(() => !!buildingDetail.value.template?.upgrade_config)
const buildingUpgradeDescription = computed(() => (
  buildingDetail.value.upgrade_description
  ?? buildingDetail.value.template?.upgrade_config?.description
  ?? ''
))
const showBuildingUpgradeSection = computed(() =>
  buildingDetail.value.template?.code !== 'tavern_hall'
  && buildingDetail.value.template?.category !== 'dormitory'
  && buildingInstanceLevel.value < buildingMaxLevel.value
  && (
    hasUpgradeConfig.value
    || (buildingUpgradeUnlocked.value && buildingDetail.value.status === 'built' && upgradeSectionLoading.value)
  )
)
const upgradeMaterialEntries = computed(() =>
  Object.entries(
    buildingDetail.value.upgrade_required_materials
    ?? buildingDetail.value.template?.upgrade_config?.cost
    ?? {},
  ),
)
const canStartBuildingUpgrade = computed(() => (
  buildingUpgradeUnlocked.value
  && buildingDetail.value.status === 'built'
  && buildingDetail.value.can_start_upgrade === true
))

async function refreshBuildingDetail() {
  const res = await store.fetchBuildingDetail(props.building.id)
  if (res.code === 200 && store.currentBuilding) {
    buildingDetail.value = store.currentBuilding
    mergeMyAdventurerFromDetail(buildingDetail.value)
    patchStoreAdventurerFromDetail(buildingDetail.value)
  }
  return res
}

/** 同步精力/状态：复用建筑详情 my_adventurer，避免额外拉全量 list */
async function syncAdventurerFromBuildingDetail() {
  return refreshBuildingDetail()
}

async function onUpgradeBuilding() {
  if (!props.building.id || upgradingBuilding.value || !canStartBuildingUpgrade.value) return
  const buildingName = buildingDetail.value.custom_name || buildingDetail.value.template?.name || '该设施'
  if (!confirm(
    `开始升级「${buildingName}」后，设施将暂时不可用，正在使用的冒险者会被送回宿舍。确认开始升级？`,
  )) {
    return
  }
  upgradingBuilding.value = true
  try {
    const res = await store.upgradeBuilding(props.building.id)
    if (res?.code === 200) {
      const evicted = res.data?.evicted_count ?? 0
      message.success(
        evicted > 0
          ? `升级已开始，${evicted} 名冒险者已送回宿舍，可继续派遣帮工加速`
          : '升级已开始，可继续派遣帮工加速',
      )
      await refreshBuildingDetail()
      emit('refreshed')
    } else {
      message.info(res?.message || '升级失败')
    }
  } finally {
    upgradingBuilding.value = false
  }
}

const isDormitoryBuilding = computed(() => props.building.template?.category === 'dormitory')
const hasRequiredMaterials = computed(() => {
  const req = props.building.template?.required_materials
  return !!req && Object.keys(req).length > 0
})
/** 仅建造/募集阶段需要拉材料余额 */
const needsMaterialsFetch = computed(() => {
  if (showBuildingUpgradeSection.value && buildingUpgradeUnlocked.value) return true
  if (!hasRequiredMaterials.value) return false
  const st = props.building.status
  return st === 'locked' || st === 'building' || st === 'upgrading'
})
const isWaitingForDonations = computed(() => (
  props.building.status === 'locked' && !!props.building.donation_open
))
const lockedStatusLabel = computed(() => (
  isWaitingForDonations.value ? '📣 募集中' : '🔒 未解锁'
))

const canFillMaterials = computed(() => {
  if (!isAdmin.value) return false
  if (props.building.status !== 'locked') return false
  if (props.building.template?.category !== 'dormitory') return false
  const req = props.building.template?.required_materials
  return !!req && Object.keys(req).length > 0
})

const constructionBuilding = computed(() => activeBuilding.value)
const {
  liveRemaining,
  liveProgress,
  isProgressPaused,
} = useBuildingConstructionProgress(constructionBuilding)

const displayRemaining = computed(() => {
  const text = formatBuildingRemaining(liveRemaining.value)
  if (isProgressPaused.value) return `${text}（无人帮忙，暂停）`
  return text
})

async function startBuild() {
  starting.value = true
  try {
    const res = await store.startBuilding(props.building.id)
    if (res.code === 200) {
      if (res.data?.waiting_for_donations) {
        const missing = Array.isArray(res.data.missing_materials)
          ? `：${res.data.missing_materials.join('、')}`
          : ''
        message.success(`已开放玩家捐献，材料凑齐后会自动开工${missing}`)
      } else {
        message.success('建造已开始')
      }
      emit('refreshed')
      emit('close')
    } else {
      message.error(res.message || '启动失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '启动失败')
  } finally {
    starting.value = false
  }
}

async function resetBuild() {
  if (!confirm('确定要取消建造吗？将清除建造进度并释放帮忙的冒险者。')) return
  resetting.value = true
  try {
    const res = await store.resetBuilding(props.building.id)
    if (res.code === 200) {
      message.success('已取消建造')
      emit('refreshed')
      emit('close')
    } else {
      message.error(res.message || '取消失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消失败')
  } finally {
    resetting.value = false
  }
}

async function completeBuild() {
  const isUpgrade = props.building.status === 'upgrading'
  const confirmMsg = isUpgrade
    ? '确定要 GM 一键完成升级吗？将立即跳过剩余升级时间并提升至下一等级。'
    : '确定要 GM 一键建成吗？将立即跳过建造阶段直接完工。'
  if (!confirm(confirmMsg)) return
  completing.value = true
  try {
    const res = await store.completeBuilding(props.building.id)
    if (res.code === 200) {
      message.success(isUpgrade ? '升级已完成' : '已建成')
      emit('refreshed')
      emit('close')
    } else {
      message.error(res.message || (isUpgrade ? '升级完成失败' : '建成失败'))
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || (isUpgrade ? '升级完成失败' : '建成失败'))
  } finally {
    completing.value = false
  }
}

async function fillMaterials() {
  fillingMaterials.value = true
  try {
    const res = await store.fillBuildingMaterials(props.building.id)
    if (res.code === 200) {
      const filled = res.data?.filled || {}
      const entries = Object.entries(filled)
      const msg = entries.length > 0
        ? `已给 ${res.data?.target_adventurer_name} 补 ${entries.map(([k, v]) => `${k}+${v}`).join('、')}`
        : '材料已满，无需补充'
      message.success(msg)
      emit('refreshed')
    } else {
      message.error(res.message || '补充失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '补充失败')
  } finally {
    fillingMaterials.value = false
  }
}

async function toggleDisable() {
  const action = props.building.disabled ? '启用' : '禁用'
  if (!confirm(`确定要${action}这个建筑吗？`)) return
  toggling.value = true
  try {
    const res = await store.toggleBuildingDisabled(props.building.id)
    if (res.code === 200) {
      message.success(`已${action}`)
      emit('refreshed')
      emit('close')
    } else {
      message.error(res.message || `${action}失败`)
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || `${action}失败`)
  } finally {
    toggling.value = false
  }
}

// 只有冒险者主人或管理员才显示召回按钮（后端 service 层也做了校验）
function canRecall(h: BuildingHelper): boolean {
  if (isAdmin.value) return true
  const myId = userStore.user?.id
  return myId != null && h.owner_id === myId
}

async function recallAdventurer(adventurerId: number) {
  recallingId.value = adventurerId
  try {
    const res = await store.recallFromBuilding(adventurerId)
    if (res.code === 200) {
      message.success('已召回')
      applyBuildingConstructionPatch(res.data?.building)
      emit('refreshed')
      await refreshBuildingDetail()
    } else {
      message.error(res.message || '召回失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '召回失败')
  } finally {
    recallingId.value = null
  }
}

// MAX_PER_PLAYER 默认 1：直接取首个空闲冒险者派遣帮忙，不再弹"选择冒险者"层
async function sendHelp() {
  if (sendingHelp.value) return
  const candidate = eligibleAdventurers.value[0]
  if (!candidate) {
    message.error(store.adventurers.length === 0 && !detailMyAdventurer.value
      ? '你还没有冒险者，请先创建'
      : '没有空闲的冒险者（需要空闲或休息状态）')
    return
  }
  const perCost = props.building.template.stamina_cost_per_help ?? 20
  if (liveStamina(candidate) < perCost) {
    message.error(`${candidate.name} 精力不足（每次需要 ${perCost}）`)
    return
  }
  if (!confirm(`确定派遣 ${candidate.name} 帮忙建造「${props.building.template.name}」吗？`)) return
  sendingHelp.value = true
  try {
    const res = await store.helpBuilding(props.building.id, candidate.id)
    if (res.code === 200) {
      message.success('派遣成功！建造时间已缩短')
      applyBuildingConstructionPatch(res.data?.building)
      emit('refreshed')
      await refreshBuildingDetail()
    } else {
      message.error(res.message || '派遣失败')
      void syncAdventurerFromBuildingDetail()
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '派遣失败')
    void syncAdventurerFromBuildingDetail()
  } finally {
    sendingHelp.value = false
  }
}

// ── 采集/打工/修炼等使用流程 ──

const adventurersLoading = ref(false)
const recallingUsage = ref(false)
const cancellingUsageQueue = ref(false)

// ── 治疗所 / 铁匠铺：AI 异步 handler ──
const handlerKey = computed(() => activeBuilding.value.template?.handler_key ?? null)

const ITEM_CRAFT_HANDLER_KEYS = new Set([
  'blacksmith',
  'tailor_workshop',
  'alchemy_workshop',
  'item_assembly_workshop',
  'ember_kitchen',
])
const isItemCraftHandler = computed(() => ITEM_CRAFT_HANDLER_KEYS.has(handlerKey.value ?? ''))
const craftMaxSelection = computed(() => (
  handlerKey.value === 'blacksmith' || handlerKey.value === 'tailor_workshop' ? 2 : 5
))
const craftMinSelection = computed(() => (
  handlerKey.value === 'alchemy_workshop' || handlerKey.value === 'ember_kitchen' ? 1 : 2
))
const craftSelectionValid = computed(() => {
  const n = forgeSelectionCount.value
  return n >= craftMinSelection.value && n <= craftMaxSelection.value
})
const itemCraftActionLabel = computed(() => {
  if (handlerKey.value === 'tailor_workshop') return '裁制'
  if (handlerKey.value === 'alchemy_workshop') return '炼金'
  if (handlerKey.value === 'item_assembly_workshop') return '构装'
  if (handlerKey.value === 'ember_kitchen') return '烹饪'
  return '锻造'
})
const itemCraftOpenPanelLabel = computed(() => (
  handlerKey.value === 'tailor_workshop'
    ? '🪡 选择裁制方案'
    : handlerKey.value === 'alchemy_workshop'
    ? '⚗️ 选择炼金材料'
    : (handlerKey.value === 'item_assembly_workshop'
        ? '🧩 选择构装物品'
        : (handlerKey.value === 'ember_kitchen' ? '🍳 选择食材与炊具' : '⚒️ 选择锻造方案'))
))
const itemCraftPanelTitle = computed(() => (
  handlerKey.value === 'tailor_workshop'
    ? '选择衣装主体与辅材（总量 2 件）'
    : handlerKey.value === 'alchemy_workshop'
    ? '选择炼金材料（总量 1~5 个）'
    : (handlerKey.value === 'item_assembly_workshop'
        ? '选择构装物品（总量 2~5 件）'
        : (handlerKey.value === 'ember_kitchen' ? '选择食材（总量 1~5 份）' : '选择锻造物品（总量 2 件）'))
))
const itemCraftPanelHint = computed(() => (
  handlerKey.value === 'tailor_workshop'
    ? '请依次投入衣装主体与辅材或副件，总计 2 件；同一堆叠也可选择 2 个。每个投入单位消耗 1，堆叠中的其余物品保留。成功产物仅限防具或饰品；后悔撤回时可任选一件投入恢复，另一件不返还。'
    : handlerKey.value === 'alchemy_workshop'
    ? '从随身和仓库中选择材料，同类物品可重复投入（如堆叠 5 个可选 1~5 个），不同物品合计不超过 5 个。炼金产物为药水/材料/工具；胡乱投入或与炼金无关的物品可能全部损毁。'
    : (handlerKey.value === 'item_assembly_workshop'
        ? '从随身和仓库中按顺序投入总计 2~5 件物品；同一堆叠可选择多个，每份可独立调整槽位。构装会同时考虑物品、顺序、槽位关系与备注。'
        : (handlerKey.value === 'ember_kitchen'
            ? '从随身和仓库中投入 1~5 份食材，再选择灶坊自带炊具。只有每份材料都能按该做法安全入口时才会得到餐食；否则黑暗料理当场丢弃。'
            : '请依次投入主物品和锻造材料，总计 2 件；同一堆叠也可选择 2 个。每个投入单位消耗 1，堆叠中的其余物品保留。后悔撤回只恢复第一件主物品，材料不返还。'))
))
const itemCraftPendingLabel = computed(() => {
  if (handlerKey.value === 'tailor_workshop') return '🪡 裁制中，裁缝师正在灯下细作…'
  if (handlerKey.value === 'alchemy_workshop') return '⚗️ 炼金中，请稍候…'
  if (handlerKey.value === 'item_assembly_workshop') return '🧩 构装中，请稍候…'
  if (handlerKey.value === 'ember_kitchen') return '🍲 炉火正旺，掌勺人正在收汁装盘…'
  if (handlerKey.value === 'blacksmith') return '⚒️ 锻造中，请稍候…'
  if (handlerKey.value === 'skill_school') return '📜 导师还在替这堂课收尾，请稍候…'
  return '🩺 治疗中…'
})

const SHOP_HANDLER_KEYS = new Set(['herbalist_shop', 'armory_shop', 'forge_supply_shop', 'limited_shop'])
const isShopBuilding = computed(() => SHOP_HANDLER_KEYS.has(handlerKey.value ?? ''))
const isBuybackBuilding = computed(() => handlerKey.value === 'buyback_shop')
const isGamblingBuilding = computed(() => handlerKey.value === 'gambling_hall')
const isLuckyWheelBuilding = computed(() => handlerKey.value === 'lucky_wheel')
const isHiddenDungeonExchangeBuilding = computed(() => handlerKey.value === 'hidden_dungeon_exchange')
const isMemorialBuilding = computed(() => handlerKey.value === 'fallen_memorial')
const isFlowerShopBuilding = computed(() => handlerKey.value === 'flower_shop')
const isSkillSchoolBuilding = computed(() => handlerKey.value === 'skill_school')
const isTeaHouseBuilding = computed(() => handlerKey.value === 'tea_house')
const isHotSpringBuilding = computed(() => handlerKey.value === 'hot_spring')
const isInsuranceBuilding = computed(() => handlerKey.value === 'insurance_shop')
const isAssessmentPreview = computed(() => handlerKey.value === TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY)
const displayBuildingName = computed(() => (
  isAssessmentPreview.value
    ? TRANSCENDENT_ASSESSMENT_PREVIEW_NAME
    : (activeBuilding.value.custom_name || activeBuilding.value.template.name)
))
const displayBuildingDescription = computed(() => (
  isAssessmentPreview.value
    ? TRANSCENDENT_ASSESSMENT_PREVIEW_DESCRIPTION
    : activeBuilding.value.template?.description
))
const assessmentMinLevel = computed(() => (
  activeBuilding.value.template?.settlement_config?.required_level ?? null
))
const WIDE_DETAIL_HANDLER_KEYS = new Set([
  'secret_auction',
  'gambling_hall',
  'tea_house',
  'hot_spring',
  'insurance_shop',
  'lucky_wheel',
  'hidden_dungeon_exchange',
  'fallen_memorial',
  'flower_shop',
  'skill_school',
  TRANSCENDENT_ASSESSMENT_PREVIEW_HANDLER_KEY,
])
const usesWideDetailLayout = computed(() => (
  props.building.status === 'built'
  && (
    props.building.template?.category === 'task'
    || isShopBuilding.value
    || isBuybackBuilding.value
    || WIDE_DETAIL_HANDLER_KEYS.has(handlerKey.value ?? '')
  )
))

const settlingHintText = computed(() => {
  if (handlerKey.value === 'skill_school' && isCustomSkillTraining.value) {
    return '📜 导师正替你理顺这堂课的最后几招…'
  }
  const map: Record<string, string> = {
    healing_house: '🩺 治疗进行中，请稍候…',
    chapel: '🙏 祈祷进行中，修女正在诵经…',
    hospital: '⚕️ 住院治疗中，请稍候…',
    sanatorium: usageRemainingSeconds.value <= 0
      ? '🌿 租用已到期，正在自动退房…（也可点上方按钮立即退出）'
      : '🌿 疗养休息中，请稍候…',
    dining_hall: '🍲 正在用餐，请稍候…',
    skill_school: '📜 正跟着导师照旧卷演练…',
    pet_evolution: '🧬 嬗变仪式进行中，请稍候…',
    armory_shop: '🛡️ 装备整备中，请稍候…',
    task_board: '📋 任务进行中，请稍候…',
  }
  return map[handlerKey.value ?? ''] ?? '⏳ 工作进行中，请稍候…'
})

type BuildingUsageConfig = NonNullable<BuildingInstance['template']['settlement_config']>

const usageConfig = computed<BuildingUsageConfig | null>(() => {
  if (isShopBuilding.value || isBuybackBuilding.value || isGamblingBuilding.value || isTeaHouseBuilding.value || isHotSpringBuilding.value || isInsuranceBuilding.value) return null
  const tplCfg = activeBuilding.value.template?.settlement_config
  if (handlerKey.value === 'clue_bureau') {
    return tplCfg || {
      stamina_cost_per_hour: 20,
      usage_stamina_cost: 0,
      usage_slots: 8,
      max_search_hours: 6,
    }
  }
  if (handlerKey.value === 'scavenge') {
    return tplCfg || {
      gold_cost_per_hour: 20,
      usage_stamina_cost: 40,
      usage_slots: 8,
      max_search_hours: 8,
    }
  }
  if (handlerKey.value === 'dining_hall') {
    const cfg = tplCfg
    return cfg || { usage_duration_minutes: 10, cooldown_hours: 4, usage_stamina_cost: 0 }
  }
  if (handlerKey.value === 'skill_school') {
    const cfg = tplCfg
    return cfg || { usage_duration_minutes: 60, usage_stamina_cost: 0, reward_type: 'none' }
  }
  if (handlerKey.value === 'sanatorium') {
    const cfg = tplCfg
    return cfg || {
      gold_cost_per_hour: 10,
      usage_duration_minutes: 60,
      usage_stamina_cost: 0,
      usage_slots: 4,
    }
  }
  if (handlerKey.value === 'clinic') {
    const cfg = tplCfg
    return cfg || {
      gold_cost_per_minute: 10,
      hp_percent_per_minute: 1,
      usage_stamina_cost: 0,
      usage_slots: 4,
    }
  }
  const cfg = tplCfg
  if (!cfg) return null
  if (cfg.reward_type === 'none') return null
  // 休闲/医疗建筑：有 gold_cost、gold_cost_per_hp、动态时长或 reward_type 即可
  if (cfg.usage_stamina_cost || cfg.gold_cost || cfg.gold_cost_per_hp || cfg.gold_cost_per_hour
    || cfg.stamina_cost_per_hour
    || cfg.usage_duration_minutes_per_hp || cfg.gold_cost_per_minute || cfg.reward_type) return cfg
  return null
})

const buildingOccupants = computed(() => buildingDetail.value.helpers ?? [])

const buildingUsageSlots = computed(() =>
  computeEffectiveUsageSlots(activeBuilding.value, usageConfig.value),
)

const usageQueueState = computed(() => buildingDetail.value.usage_queue ?? null)
// usage_queue 仅由后端对白名单异步设施投影，避免前端复制业务枚举。
const isBuildingUsageQueueEligible = computed(() => usageQueueState.value !== null)
const usageQueueWaitingCount = computed(() => usageQueueState.value?.waiting_count ?? 0)
const buildingAtUsageCapacity = computed(() => (
  buildingUsageSlots.value > 0
  && buildingOccupants.value.length >= buildingUsageSlots.value
))
const shouldJoinBuildingUsageQueue = computed(() => (
  isBuildingUsageQueueEligible.value
  && (buildingAtUsageCapacity.value || usageQueueWaitingCount.value > 0)
))

const myAdventurerIds = computed(() => new Set(store.adventurers.map(a => a.id)))

/** 仅当「我的冒险者」正在此建筑使用时返回；多床位时他人占用不影响派遣入口 */
const myActiveUsage = computed(() =>
  buildingOccupants.value.find(h => myAdventurerIds.value.has(h.adventurer_id)) ?? null,
)

const skillSchoolOccupantsView = computed(() => {
  if (handlerKey.value !== 'skill_school') return []

  return buildingOccupants.value
    .map((occupant) => {
      const extra = occupant.building_usage_extra as {
        usage_duration_minutes?: number
        skill_name?: string
        operation?: 'learn' | 'advance'
        target_skill_name?: string
      } | null | undefined
      const isCustom = extra?.operation === 'learn' || extra?.operation === 'advance'
      const startMs = buildingUsageStartMs(occupant, 'skill_school')
      const durationMinutes = effectiveUsageDurationMinutes(occupant)
      const endMs = startMs != null && durationMinutes > 0
        ? startMs + durationMinutes * 60 * 1000
        : null
      const remainingSeconds = endMs == null
        ? null
        : Math.max(0, Math.ceil((endMs - now.value) / 1000))
      const lessonLabel = extra?.operation === 'learn'
        ? '另学一门本事'
        : (extra?.operation === 'advance'
            ? `磨炼「${extra.target_skill_name || '所选旧招'}」`
            : `演练「${extra?.skill_name?.trim() || '阁中旧卷'}」`)

      return {
        id: occupant.adventurer_id,
        name: occupant.adventurer_name,
        isMine: myAdventurerIds.value.has(occupant.adventurer_id),
        modeLabel: isCustom ? '导师指点' : '旧卷传艺',
        lessonLabel,
        endMs,
        remainingSeconds,
      }
    })
    .sort((left, right) => (
      (left.endMs ?? Number.POSITIVE_INFINITY) - (right.endMs ?? Number.POSITIVE_INFINITY)
      || left.id - right.id
    ))
})

function formatSkillSchoolRemaining(seconds: number | null): string {
  if (seconds == null) return '收功时辰待核对'
  if (seconds <= 0) return '导师正在收尾'
  return `还需 ${formatDurationSec(seconds)}`
}

const activeSkillSchoolName = computed(() => {
  if (handlerKey.value !== 'skill_school' || !myActiveUsage.value) return ''
  const extra = myActiveUsage.value.building_usage_extra as {
    skill_name?: string
    operation?: 'learn' | 'advance'
    target_skill_name?: string
  } | null | undefined
  if (extra?.operation === 'learn') return '另学一门本事'
  if (extra?.operation === 'advance') return `磨炼「${extra.target_skill_name || '所选旧招'}」`
  return extra?.skill_name?.trim() || ''
})

const isCustomSkillTraining = computed(() => {
  if (handlerKey.value !== 'skill_school' || !myActiveUsage.value) return false
  const extra = myActiveUsage.value.building_usage_extra as { operation?: string } | null | undefined
  return extra?.operation === 'learn' || extra?.operation === 'advance'
})

const activeUsageStartMs = computed(() => (
  myActiveUsage.value
    ? buildingUsageStartMs(myActiveUsage.value, handlerKey.value)
    : null
))

const usageRemainingSeconds = computed(() => {
  if (!myActiveUsage.value || !usageConfig.value) return 0
  const startMs = activeUsageStartMs.value
  if (startMs == null) return 0
  const durationMin = effectiveUsageDurationMinutes(myActiveUsage.value)
  const durationMs = durationMin * 60 * 1000
  const endMs = startMs + durationMs
  return Math.max(0, Math.floor((endMs - now.value) / 1000))
})

// 到期结算完全交给服务器 scheduler；异步任务失败再由 recovery 静默补投或退款。
// 玩家端只展示等待态，不提交或重试结算。
const ASYNC_USAGE_HANDLER_KEYS = new Set([
  'healing_house',
  'blacksmith',
  'alchemy_workshop',
  'item_assembly_workshop',
  'ember_kitchen',
  'chapel',
  'pet_evolution',
  'skill_school',
])
const aiResultPending = computed(() => (
  usageRemainingSeconds.value <= 0
  && ASYNC_USAGE_HANDLER_KEYS.has(handlerKey.value ?? '')
))

async function handleSkillSchoolRefreshed() {
  await refreshBuildingDetail()
  emit('refreshed')
}


interface ForgeItem {
  id: number
  name: string
  quantity?: number | null
  type: string
  desc: string
  rarity: string
  from: 'carry' | 'warehouse'
  created_at?: string | null
  uses_left?: number | null
  evolution_stage?: number | null
  damage_stage?: string | null
  damage_note?: string | null
  is_player_locked?: boolean
}
interface ForgeItemRef {
  source: 'carry' | 'warehouse'
  id: number
  name: string
  quantity?: number
}
interface AssemblyLayoutSlot {
  index: number
  position: string
  item_ref: ForgeItemRef
}
interface EmberKitchenCookware {
  code: 'stew_pot' | 'frying_pan' | 'steamer' | 'baking_tray' | 'stone_mortar'
  icon: string
  name: string
  description: string
}
type BlacksmithForgeIntentCode =
  | 'free'
  | 'reinforce'
  | 'remedy_limit'
  | 'merge_trait'
  | 'reshape'
  | 'conservative'
  | 'breakthrough'
interface BlacksmithForgeIntent {
  code: BlacksmithForgeIntentCode
  name: string
  description: string
}
type TailorWorkshopOperationCode =
  | 'free'
  | 'reinforce'
  | 'refit'
  | 'layer'
  | 'embroider'
  | 'reshape'
  | 'masterwork'
interface TailorWorkshopOperation {
  code: TailorWorkshopOperationCode
  name: string
  description: string
}
const BLACKSMITH_FORGE_INTENTS: readonly BlacksmithForgeIntent[] = [
  { code: 'free', name: '自由创作', description: '由铁匠依两件投入自由寻找最自然的成品，不预设强化方向。' },
  { code: 'reinforce', name: '主体强化', description: '第一件作为主体，第二件用于加强它已经具备的核心用途。' },
  { code: 'remedy_limit', name: '补足短板', description: '保留第一件主体与核心用途，尝试用第二件缓解它已有的明确限制。' },
  { code: 'merge_trait', name: '特性融合', description: '保留第一件主体与核心用途，让第二件已有的关键特性成为成品的一部分。' },
  { code: 'reshape', name: '形态重塑', description: '允许改变第一件的形态或用法，追求两件投入共同支持的新结构。' },
  { code: 'conservative', name: '稳健锻造', description: '保留第一件主体与核心用途，优先追求稳定、实用的改进。' },
  { code: 'breakthrough', name: '追求突破', description: '材料确实支持时追求质变或升档；不支持时仍可能小改或失败。' },
]
const TAILOR_WORKSHOP_OPERATIONS: readonly TailorWorkshopOperation[] = [
  { code: 'free', name: '自由裁作', description: '让裁缝师依两件投入寻找最自然的衣装方向，不预设强化。' },
  { code: 'reinforce', name: '密缝加固', description: '保留第一件主体，在材料支持时加强已有结构、耐用性或防护。' },
  { code: 'refit', name: '合身改制', description: '调整主体的贴合、活动或穿戴方式，第二件必须实际参与。' },
  { code: 'layer', name: '复层织合', description: '让第二件成为主体的实际层次或连接结构。' },
  { code: 'embroider', name: '纹绣引性', description: '以纹饰或细部连接承接第二件已经具备的性质。' },
  { code: 'reshape', name: '拆裁重制', description: '改变主体的衣装形态或穿戴用途，但保留必要连续性。' },
  { code: 'masterwork', name: '极致巧作', description: '两件投入确实支持时追求明显质变；不足时仍会收束或失败。' },
]
const EMBER_KITCHEN_COOKWARE: readonly EmberKitchenCookware[] = [
  { code: 'stew_pot', icon: '🥘', name: '厚底炖锅', description: '慢炖汤羹，让食材充分熟化。' },
  { code: 'frying_pan', icon: '🍳', name: '长柄煎锅', description: '高温煎炒，适合薄切与小块食材。' },
  { code: 'steamer', icon: '♨️', name: '叠层蒸笼', description: '以蒸汽加热，尽量保留原味。' },
  { code: 'baking_tray', icon: '🔥', name: '黑铁烤盘', description: '以炉膛干热烘烤，形成焦香外壳。' },
  { code: 'stone_mortar', icon: '🪨', name: '石臼与研杵', description: '捣碎、研磨与冷拌，不能替代必要加热。' },
]
const showForgePanel = ref(false)
const forgeLoading = ref(false)
const forgeItems = ref<ForgeItem[]>([])
const forgeSelectedRefs = ref<ForgeItemRef[]>([])
const forgeSearchQuery = ref('')
const assemblyLayoutMode = ref<'vertical_line' | 'cross' | 'inside_out' | 'left_to_right' | 'ring'>('vertical_line')
const ASSEMBLY_DIRECTION_MAX_LENGTH = 30
const assemblyDesiredDirection = ref('')
const selectedCookwareCode = ref<EmberKitchenCookware['code']>('stew_pot')
const selectedForgeIntent = ref<BlacksmithForgeIntentCode>('free')
const selectedTailorOperation = ref<TailorWorkshopOperationCode>('free')

const forgeCarryItems = computed(() => forgeItems.value.filter(i => i.from === 'carry'))
const forgeWarehouseItems = computed(() => forgeItems.value.filter(i => i.from === 'warehouse'))
const normalizedForgeSearchQuery = computed(() => forgeSearchQuery.value.trim().toLocaleLowerCase())
const forgeSearchActive = computed(() => normalizedForgeSearchQuery.value.length > 0)
const visibleForgeCarryItems = computed(() => forgeCarryItems.value.filter(forgeItemMatchesSearch))
const searchFilteredForgeWarehouseItems = computed(() => (
  forgeWarehouseItems.value.filter(forgeItemMatchesSearch)
))
const {
  rarityFilter: forgeWarehouseRarityFilter,
  sort: forgeWarehouseSort,
  visibleItems: visibleForgeWarehouseItems,
} = useInventoryListControls(searchFilteredForgeWarehouseItems)
const forgeSelectionCount = computed(() => forgeSelectedRefs.value.reduce(
  (sum, ref) => sum + Math.max(1, Math.floor(Number(ref.quantity ?? 1))),
  0,
))

const expandedForgeSelectedRefs = computed<ForgeItemRef[]>(() => (
  forgeSelectedRefs.value.flatMap(ref => Array.from(
    { length: Math.max(1, Math.floor(Number(ref.quantity ?? 1))) },
    () => ({ ...ref, quantity: 1 }),
  ))
))

function forgeItemMatchesSearch(it: ForgeItem): boolean {
  const query = normalizedForgeSearchQuery.value
  if (!query) return true
  return [it.name, it.desc]
    .some(value => String(value ?? '').toLocaleLowerCase().includes(query))
}

function getForgeItemQty(it: ForgeItem): number {
  const key = forgeItemKey(it)
  return forgeSelectedRefs.value.reduce((total, ref) => (
    total + (forgeItemKey(ref) === key ? Math.max(1, Math.floor(Number(ref.quantity ?? 1))) : 0)
  ), 0)
}

function canIncreaseForgeItemQty(it: ForgeItem): boolean {
  if (it.is_player_locked) return false
  const maxStack = Math.max(1, Math.floor(Number(it.quantity ?? 1)))
  if (getForgeItemQty(it) >= maxStack) return false
  return forgeSelectionCount.value < craftMaxSelection.value
}

function adjustForgeItemQty(it: ForgeItem, delta: number) {
  if (it.is_player_locked && delta > 0) return
  const key = forgeItemKey(it)
  const current = getForgeItemQty(it)
  const maxStack = Math.max(1, Math.floor(Number(it.quantity ?? 1)))
  let next = current + delta
  if (next < 0) next = 0
  if (next > maxStack) next = maxStack
  const otherTotal = forgeSelectionCount.value - current
  const maxAllowed = craftMaxSelection.value - otherTotal
  if (next > maxAllowed) next = Math.max(0, maxAllowed)
  if (handlerKey.value === 'item_assembly_workshop') {
    const slots = [...expandedForgeSelectedRefs.value]
    if (next > current) {
      for (let count = current; count < next; count += 1) {
        slots.push({ source: it.from, id: it.id, name: it.name, quantity: 1 })
      }
    } else {
      let remaining = current - next
      for (let index = slots.length - 1; index >= 0 && remaining > 0; index -= 1) {
        if (forgeItemKey(slots[index]) === key) {
          slots.splice(index, 1)
          remaining -= 1
        }
      }
    }
    forgeSelectedRefs.value = slots
    return
  }
  if (next === 0) {
    forgeSelectedRefs.value = forgeSelectedRefs.value.filter(ref => forgeItemKey(ref) !== key)
  } else {
    const existingIndex = forgeSelectedRefs.value.findIndex(ref => forgeItemKey(ref) === key)
    if (existingIndex >= 0) {
      forgeSelectedRefs.value = forgeSelectedRefs.value.map((ref, index) => (
        index === existingIndex ? { ...ref, quantity: next } : ref
      ))
    } else {
      forgeSelectedRefs.value = [
        ...forgeSelectedRefs.value,
        { source: it.from, id: it.id, name: it.name, quantity: next },
      ]
    }
  }
}

function moveAssemblySlot(index: number, offset: -1 | 1) {
  const slots = [...expandedForgeSelectedRefs.value]
  const target = index + offset
  if (handlerKey.value !== 'item_assembly_workshop' || index < 0 || index >= slots.length || target < 0 || target >= slots.length) return
  const [moved] = slots.splice(index, 1)
  slots.splice(target, 0, moved)
  forgeSelectedRefs.value = slots
}

function buildCraftItemRefs(): ForgeItemRef[] {
  return forgeSelectedRefs.value.map(ref => ({
    ...ref,
    quantity: Math.max(1, Math.floor(Number(ref.quantity ?? 1))),
  }))
}

function assemblySlotPosition(index: number): string {
  const total = expandedForgeSelectedRefs.value.length
  if (assemblyLayoutMode.value === 'vertical_line') {
    return total === 5 ? ['顶部', '上段', '中段', '下段', '底部'][index] : `第 ${index + 1} 段`
  }
  if (assemblyLayoutMode.value === 'cross') return ['中心', '上方', '右方', '下方', '左方'][index] ?? `槽位 ${index + 1}`
  if (assemblyLayoutMode.value === 'inside_out') return ['核心', '内层一', '内层二', '外层一', '外层二'][index] ?? `槽位 ${index + 1}`
  if (assemblyLayoutMode.value === 'left_to_right') {
    return total === 5 ? ['最左', '左段', '中段', '右段', '最右'][index] : `第 ${index + 1} 段`
  }
  if (assemblyLayoutMode.value === 'ring') return ['起点', '右上', '右下', '左下', '左上'][index] ?? `环位 ${index + 1}`
  return `槽位 ${index + 1}`
}

function buildAssemblyLayoutSlots(): AssemblyLayoutSlot[] {
  return expandedForgeSelectedRefs.value.map((ref, index) => ({
    index: index + 1,
    position: assemblySlotPosition(index),
    item_ref: ref,
  }))
}

function forgeItemKey(it: { id: number; source?: ForgeItemRef['source']; from?: ForgeItem['from'] }): string {
  return `${it.source ?? it.from}:${it.id}`
}

function forgeItemStateSummary(it: ForgeItem): string {
  const parts: string[] = []
  if (it.uses_left != null) parts.push(`剩余 ${it.uses_left} 次`)
  if (it.type === 'companion' || (it.evolution_stage ?? 0) > 0) {
    parts.push(`第 ${it.evolution_stage ?? 0} 阶`)
  }
  if (it.damage_stage || it.damage_note) {
    const damage = it.damage_stage && it.damage_note
      ? `${it.damage_stage}（${it.damage_note}）`
      : (it.damage_stage || it.damage_note)
    parts.push(`损伤：${damage}`)
  }
  return parts.join(' · ')
}

function forgeRefStateSummary(ref: ForgeItemRef): string {
  const key = forgeItemKey(ref)
  const item = forgeItems.value.find(candidate => forgeItemKey(candidate) === key)
  return item ? forgeItemStateSummary(item) : ''
}

function isForgeItemSelected(it: ForgeItem): boolean {
  return getForgeItemQty(it) > 0
}

function isForgeItemDisabled(it: ForgeItem): boolean {
  return !!it.is_player_locked || (getForgeItemQty(it) === 0 && forgeSelectionCount.value >= craftMaxSelection.value)
}

function forgeItemClasses(it: ForgeItem) {
  return [
    'forge-item',
    {
      'forge-disabled': isForgeItemDisabled(it),
      'forge-selected': isForgeItemSelected(it),
    },
  ]
}

function buildForgeItemsFromDetail(detail: AdventurerData): ForgeItem[] {
  const items: ForgeItem[] = []
  for (const eq of detail.equipment ?? []) {
    items.push({
      id: eq.id, name: eq.name, quantity: eq.quantity, type: eq.type ?? '', desc: eq.desc ?? '',
      rarity: eq.rarity, from: 'carry', created_at: eq.created_at,
      uses_left: eq.uses_left, evolution_stage: eq.evolution_stage,
      damage_stage: eq.damage_stage, damage_note: eq.damage_note,
      is_player_locked: eq.is_player_locked,
    })
  }
  for (const w of detail.warehouse ?? []) {
    items.push({
      id: w.id, name: w.name, quantity: w.quantity, type: w.type ?? '', desc: w.desc ?? '',
      rarity: w.rarity, from: 'warehouse', created_at: w.created_at,
      uses_left: w.uses_left, evolution_stage: w.evolution_stage,
      damage_stage: w.damage_stage, damage_note: w.damage_note,
      is_player_locked: w.is_player_locked,
    })
  }
  return items
}

async function loadDetailForItemPicker(advId: number, force = false): Promise<AdventurerData | null> {
  if (!force && store.currentDetail?.id === advId) {
    return store.currentDetail as AdventurerData
  }
  const res = await store.fetchDetail(advId, { preserveDetail: true, force })
  if (res.code !== 200 || !res.data) {
    message.error(res.message || '加载物品信息失败')
    return null
  }
  return res.data as AdventurerData
}

async function openForgePanel() {
  const advId = myActiveUsage.value?.adventurer_id ?? usageCandidate.value?.id
  if (!advId || forgeLoading.value) return
  forgeLoading.value = true
  try {
    // 构装会冻结实例状态；每次打开都绕过详情节流，避免展示旧次数、阶位或损伤。
    const detail = await loadDetailForItemPicker(advId, true)
    if (!detail) return
    forgeItems.value = buildForgeItemsFromDetail(detail)
    forgeSelectedRefs.value = []
    forgeSearchQuery.value = ''
    assemblyLayoutMode.value = 'vertical_line'
    assemblyDesiredDirection.value = ''
    selectedCookwareCode.value = 'stew_pot'
    selectedForgeIntent.value = 'free'
    selectedTailorOperation.value = 'free'
    showForgePanel.value = true
  } finally {
    forgeLoading.value = false
  }
}

// ── 治疗所 Lv2：可选携带治疗物品 ──
const showHealingItemPicker = ref(false)
const healingItemsLoading = ref(false)
const healingItems = ref<ForgeItem[]>([])
const {
  rarityFilter: healingItemRarityFilter,
  sort: healingItemSort,
  visibleItems: visibleHealingItems,
} = useInventoryListControls(computed(() => healingItems.value))
const healingItemRef = ref<ForgeItemRef | null>(null)

// 治疗所是否已升至 Lv2 并解锁治疗物品辅助（与后端 handler_effects.item_treatment 对齐）
const healingItemTreatmentUnlocked = computed(() => {
  if (handlerKey.value !== 'healing_house') return false
  if (buildingInstanceLevel.value <= 1) return false
  const effects = buildingDetail.value.template?.upgrade_config?.handler_effects as Record<string, any> | undefined
  return !!effects?.item_treatment
})

function isHealingItemSelected(it: ForgeItem): boolean {
  return !!healingItemRef.value && healingItemRef.value.source === it.from && healingItemRef.value.id === it.id
}
function selectHealingItem(it: ForgeItem) {
  if (it.is_player_locked) return
  healingItemRef.value = { source: it.from, id: it.id, name: it.name }
}
async function openHealingItemPicker() {
  const advId = myActiveUsage.value?.adventurer_id ?? usageCandidate.value?.id
  if (!advId || healingItemsLoading.value) return
  healingItemsLoading.value = true
  try {
    const detail = await loadDetailForItemPicker(advId)
    if (!detail) return
    healingItems.value = buildForgeItemsFromDetail(detail)
    showHealingItemPicker.value = true
  } finally {
    healingItemsLoading.value = false
  }
}

// AI 结果落地后（worker 推送）：清掉等待态并刷新建筑详情，结果弹窗由主视图负责
const socket = useSocket()

const CONSTRUCTION_SOCKET_ACTIONS = new Set([
  'building_assigned',
  'building_recalled',
  'dormitory_started',
  'dormitory_donated',
])
const BUILDING_USAGE_QUEUE_SOCKET_ACTIONS = new Set([
  'building_usage_queue_joined',
  'building_usage_queue_promoted',
  'building_usage_queue_cancelled',
])

function onConstructionSocket(payload: any) {
  const action = payload?.action
  if (!action || !CONSTRUCTION_SOCKET_ACTIONS.has(action)) return
  const instId = payload?.building_instance_id
  if (instId != null && instId !== props.building.id) return
  if (
    constructionBuilding.value.status !== 'building'
    && constructionBuilding.value.status !== 'upgrading'
    && action !== 'dormitory_started'
  ) {
    return
  }
  void refreshBuildingDetail()
}

function onAdventurerChanged(payload: any) {
  if (BUILDING_USAGE_QUEUE_SOCKET_ACTIONS.has(payload?.action)) {
    const instId = payload?.building_instance_id
    if (instId == null || instId === props.building.id) {
      void refreshBuildingDetail()
    }
    return
  }
  const advId = Array.isArray(payload?.ids) ? payload.ids[0] : null
  const mineId = myActiveUsage.value?.adventurer_id
  if (advId == null || mineId == null || advId !== mineId) return

  if (payload?.action === 'building_ai_complete') {
    showForgePanel.value = false
  } else if (payload?.action === 'building_auto_complete') {
    // 采集等到点自动结算
  } else {
    return
  }

  store.fetchBuildingDetail(props.building.id).then((res) => {
    if (res.code === 200 && store.currentBuilding) {
      buildingDetail.value = store.currentBuilding
      syncClueBureauFromDetail(buildingDetail.value)
      mergeMyAdventurerFromDetail(buildingDetail.value)
      patchStoreAdventurerFromDetail(buildingDetail.value)
    }
  })
  store.fetchMyMaterials()
}

const usageProgressPercent = computed(() => {
  if (!usageConfig.value) return 0
  const total = effectiveUsageDurationMinutes(myActiveUsage.value) * 60
  if (total <= 0) return 100
  return Math.min(100, Math.round((1 - usageRemainingSeconds.value / total) * 100))
})

const formatUsageRemaining = computed(() => {
  const s = usageRemainingSeconds.value
  if (s <= 0) return '已完成'
  if (s < 60) return '即将完成'
  const m = Math.floor(s / 60)
  const sec = s % 60
  return `${m}分${sec > 0 ? sec + '秒' : ''}`
})

const rewardPreview = computed(() => {
  const cfg = usageConfig.value
  if (!cfg) return ''
  const overflowBonus = cfg.reward_type === 'gold'
    ? calcWorkStatOverflowBonus(cfg.required_stat, cfg.required_stat_min, usageCandidate.value)
    : 0
  if (cfg.reward_type === 'material') {
    return `${matLabel(cfg.reward_material_name ?? '') || '材料'} +${cfg.reward_amount ?? 0}`
  }
  if (cfg.reward_type === 'stat') {
    return `${statLabel(cfg.stat_name ?? '')} +${cfg.reward_amount ?? 0}`
  }
  if (cfg.reward_type === 'gold') {
    const base = cfg.reward_amount ?? 0
    const total = base + overflowBonus
    if (overflowBonus > 0) {
      return `💰 金币 +${total}（基础 ${base} + 属性溢出 ${overflowBonus}）`
    }
    return `💰 金币 +${base}`
  }
  if (cfg.reward_type === 'stamina') {
    return `💪 精力 +${cfg.reward_amount ?? 0}`
  }
  return ''
})

// 产业链建筑使用时消耗的输入材料（炭窑吃木材、熔铁坊吃铁矿石+木炭）
const usageInputMaterials = computed(() => {
  const mats = (usageConfig.value as Record<string, unknown> | null)?.input_materials
  if (!mats || typeof mats !== 'object') return ''
  return Object.entries(mats as Record<string, unknown>)
    .filter(([, v]) => Number(v) > 0)
    .map(([k, v]) => `${matLabel(k)} ${v}`)
    .join('、')
})

const usageStatCount = computed(() => {
  const cfg = usageConfig.value
  if (!cfg) return 0
  return [
    !!cfg.gold_cost,
    !!cfg.usage_stamina_cost,
    !!usageInputMaterials.value,
    !!(cfg.usage_duration_minutes_per_hp || cfg.usage_duration_minutes),
    !!cfg.cooldown_hours,
    !!cfg.daily_limit,
    !!(cfg.required_stat && cfg.required_stat_min),
  ].filter(Boolean).length
})

const cooldownAvailableAtMs = computed(() => {
  const parsed = Date.parse(buildingDetail.value.cooldown_available_at ?? '')
  return Number.isFinite(parsed) ? parsed : 0
})

const cooldownRemainingSeconds = computed(() => (
  cooldownAvailableAtMs.value > now.value
    ? Math.ceil((cooldownAvailableAtMs.value - now.value) / 1000)
    : 0
))

const cooldownRemainingText = computed(() => {
  const total = cooldownRemainingSeconds.value
  const hours = Math.floor(total / 3600)
  const minutes = Math.floor((total % 3600) / 60)
  const seconds = total % 60
  if (hours > 0) return `${hours}小时${minutes}分${seconds}秒`
  if (minutes > 0) return `${minutes}分${seconds}秒`
  return `${seconds}秒`
})

const detailMyAdventurer = ref<AdventurerData | null>(null)

const myUsageQueue = computed(() => {
  const queue = usageQueueState.value
  const adv = detailMyAdventurer.value
  if (
    !isBuildingUsageQueueEligible.value
    || !queue?.is_waiting
    || adv?.status !== 'building_queue'
  ) return null
  return queue
})

function mergeMyAdventurerFromDetail(detail: BuildingInstance | null | undefined) {
  const ma = detail?.my_adventurer
  detailMyAdventurer.value = ma ? { ...ma } as AdventurerData : null
}

/** 建筑详情 my_adventurer 含最新 stamina_updated_at，合并进 store 避免弹窗精力虚高 */
function patchStoreAdventurerFromDetail(detail: BuildingInstance | null | undefined) {
  const ma = detail?.my_adventurer
  if (!ma?.id) return
  const idx = store.adventurers.findIndex(a => a.id === ma.id)
  if (idx < 0) return
  const cur = store.adventurers[idx]
  store.adventurers[idx] = {
    ...cur,
    status: ma.status ?? cur.status,
    gold: ma.gold ?? cur.gold,
    low_consumption_gold: ma.low_consumption_gold ?? cur.low_consumption_gold,
    low_consumption_gold_cap: ma.low_consumption_gold_cap ?? cur.low_consumption_gold_cap,
    stamina: ma.stamina ?? cur.stamina,
    max_stamina: ma.max_stamina ?? cur.max_stamina,
    stamina_updated_at: ma.stamina_updated_at ?? cur.stamina_updated_at,
    hp: ma.hp ?? cur.hp,
    max_hp: ma.max_hp ?? cur.max_hp,
    hp_updated_at: ma.hp_updated_at ?? cur.hp_updated_at,
    training_count: ma.training_count ?? cur.training_count,
    training_cap: ma.training_cap ?? cur.training_cap,
  }
}

const eligibleAdventurers = computed(() => {
  const fromStore = store.adventurers.filter(a => a.status === 'idle' || a.status === 'resting')
  if (fromStore.length > 0) return fromStore
  const ma = detailMyAdventurer.value
  if (ma && (ma.status === 'idle' || ma.status === 'resting')) return [ma]
  return []
})

const petEligibleAdventurers = computed(() => eligibleAdventurers.value.filter(a =>
  (a.equipment ?? []).length > 0,
))
const selectedPetAdventurerId = ref<number | null>(null)
const selectedCompanionId = ref<number | null>(null)

watch(petEligibleAdventurers, (items) => {
  if (!items.some(a => a.id === selectedPetAdventurerId.value)) {
    selectedPetAdventurerId.value = items[0]?.id ?? null
  }
}, { immediate: true })

// 通用建筑取首个空闲冒险者；嬗变祭坛只从有可嬗变伙伴的冒险者中选择。
const usageCandidate = computed<AdventurerData | null>(() => {
  if (handlerKey.value !== 'pet_evolution') return eligibleAdventurers.value[0] ?? null
  return petEligibleAdventurers.value.find(a => a.id === selectedPetAdventurerId.value)
    ?? petEligibleAdventurers.value[0]
    ?? null
})

const petPickerDetail = ref<AdventurerData | null>(null)
const evolvableCompanions = computed(() => resolvePetEvolutionEquipment(
  petPickerDetail.value,
  usageCandidate.value,
))

const isCompanionType = computed(() => {
  const item = evolvableCompanions.value.find(i => i.id === selectedCompanionId.value)
  return item?.type === S1_ITEM_TYPE_COMPANION
})

watch(evolvableCompanions, (items) => {
  if (!items.some(item => item.id === selectedCompanionId.value && !item.is_player_locked)) {
    selectedCompanionId.value = items.find(item => !item.is_player_locked)?.id ?? null
  }
}, { immediate: true })

const petMaterialItems = ref<ForgeItem[]>([])
const {
  rarityFilter: petMaterialRarityFilter,
  sort: petMaterialSort,
  visibleItems: visiblePetMaterialItems,
} = useInventoryListControls(computed(() => petMaterialItems.value))
const petMaterialsLoading = ref(false)
const selectedAuxiliaryMaterialKey = ref('')

function petMaterialKey(item: ForgeItem): string {
  return `${item.from}:${item.id}`
}

const selectedAuxiliaryMaterial = computed(() =>
  petMaterialItems.value.find(item => petMaterialKey(item) === selectedAuxiliaryMaterialKey.value) ?? null,
)

/** 随身同一件物品不能同时作为嬗变对象与辅助素材（与后端校验一致）。 */
function isPetAuxBlockedByCompanion(item: ForgeItem): boolean {
  return !!item.is_player_locked || (item.from === 'carry' && item.id === selectedCompanionId.value)
}

function isPetCompanionBlockedByAux(petId: number): boolean {
  const aux = selectedAuxiliaryMaterial.value
  return !!aux && aux.from === 'carry' && aux.id === petId
}

watch(selectedCompanionId, (companionId) => {
  const aux = selectedAuxiliaryMaterial.value
  if (aux && aux.from === 'carry' && aux.id === companionId) {
    selectedAuxiliaryMaterialKey.value = ''
  }
})

async function loadPetMaterials(adventurerId: number | null) {
  petPickerDetail.value = null
  petMaterialItems.value = []
  selectedAuxiliaryMaterialKey.value = ''
  if (!adventurerId || handlerKey.value !== 'pet_evolution') return
  petMaterialsLoading.value = true
  try {
    const res = await store.fetchDetail(adventurerId, { preserveDetail: true })
    if (res.code !== 200 || !res.data || selectedPetAdventurerId.value !== adventurerId) return
    const detail = res.data as AdventurerData
    petPickerDetail.value = detail
    petMaterialItems.value = buildForgeItemsFromDetail(detail)
  } finally {
    petMaterialsLoading.value = false
  }
}

watch([selectedPetAdventurerId, handlerKey], ([adventurerId]) => {
  loadPetMaterials(adventurerId)
}, { immediate: true })

const selectedCompanion = computed(() =>
  evolvableCompanions.value.find(item => item.id === selectedCompanionId.value) ?? null,
)
const petEvolutionGoldCost = computed(() => {
  const base = usageConfig.value?.gold_cost ?? 0
  return calculatePetEvolutionGoldCost(
    base,
    selectedCompanion.value?.evolution_stage ?? 0,
  )
})

const isTrainingBuilding = computed(() => handlerKey.value === 'training')

const trainingQuotaAdv = computed((): AdventurerData | null => {
  if (!isTrainingBuilding.value) return null
  const activeId = myActiveUsage.value?.adventurer_id
  if (activeId != null) {
    return store.adventurers.find(a => a.id === activeId) ?? null
  }
  return usageCandidate.value
})

const trainingQuotaLine = computed(() => {
  const adv = trainingQuotaAdv.value
  if (!adv || adv.training_cap == null) return ''
  const used = adv.training_count ?? 0
  return `🧘 训练潜能：${used}/${adv.training_cap}（力量、敏捷、智力三处共用，升级解锁更多）`
})
const dispatchingUsage = ref(false)

// 治疗所：特质选择（列表接口只返回未归档特质，无需再过滤）
const selectedTrait = ref<string>('')

interface DiningMenuItem {
  code: string
  name: string
  icon: string
  description: string
  gold_cost: number
  buff_label: string
}
const diningMenuItems = ref<DiningMenuItem[]>([])
const diningMenuLoading = ref(false)
const selectedFoodCode = ref('')

const selectedDiningItem = computed(() =>
  diningMenuItems.value.find(i => i.code === selectedFoodCode.value) ?? null,
)

async function loadDiningMenu() {
  if (handlerKey.value !== 'dining_hall' || !props.building.id) return
  diningMenuLoading.value = true
  try {
    const res = await store.fetchDiningMenu(props.building.id)
    if (res.code === 200 && res.data?.items) {
      diningMenuItems.value = res.data.items as DiningMenuItem[]
      if (!selectedFoodCode.value && diningMenuItems.value.length) {
        selectedFoodCode.value = diningMenuItems.value[0].code
      }
    }
  } finally {
    diningMenuLoading.value = false
  }
}

watch(handlerKey, (key) => {
  if (key === 'dining_hall') loadDiningMenu()
}, { immediate: true })

const healableTraits = computed(() => {
  if (!usageCandidate.value) return []
  return usageCandidate.value.traits ?? []
})

const dispatchButtonLabel = computed(() => {
  if (dispatchingUsage.value) return '派遣中…'
  if (!usageCandidate.value) return '🚀 派遣'
  const check = canUseBuilding(usageCandidate.value)
  if (!check.ok) return `🚀 派遣（${check.reason ?? '条件不足'}）`
  if ((handlerKey.value === 'healing_house' || handlerKey.value === 'chapel') && !selectedTrait.value) return '🚀 派遣（请选择特质）'
  if (isItemCraftHandler.value && !craftSelectionValid.value) {
    return handlerKey.value === 'alchemy_workshop'
      ? '🚀 派遣（请选择 1~5 件物品）'
      : (handlerKey.value === 'item_assembly_workshop'
          ? '🚀 派遣（请选择 2~5 件物品）'
          : (handlerKey.value === 'ember_kitchen'
              ? '🚀 派遣（请选择 1~5 份食材）'
              : '🚀 派遣（请选择 2 件物品）'))
  }
  if (handlerKey.value === 'pet_evolution' && selectedCompanionId.value == null) {
    return '🚀 派遣（请选择伙伴）'
  }
  if (handlerKey.value === 'dining_hall' && !selectedFoodCode.value) {
    return '🚀 派遣（请选择餐品）'
  }
  if (shouldJoinBuildingUsageQueue.value) {
    return usageQueueWaitingCount.value > 0
      ? `🕰 加入候场（前方 ${usageQueueWaitingCount.value} 人）`
      : '🕰 加入候场队列'
  }
  return '🚀 派遣'
})

function liveStamina(a: AdventurerData): number {
  return liveStaminaOf(a, now.value)
}

function liveHp(a: AdventurerData): number {
  return liveHpOf(a, now.value)
}

const LOW_CONSUMPTION_BUILDING_KEYS = new Set(['hospital', 'sanatorium', 'clinic'])
const isLowConsumptionBuilding = computed(() => LOW_CONSUMPTION_BUILDING_KEYS.has(handlerKey.value ?? ''))

function lowConsumptionAvailableGold(a: AdventurerData): number {
  if (!LOW_CONSUMPTION_BUILDING_KEYS.has(handlerKey.value ?? '')) {
    return Number(a.gold ?? 0)
  }
  return Math.max(0, Number(a.gold ?? 0)) + Math.max(0, Number(a.low_consumption_gold ?? 0))
}

function hospitalTreatmentCost(a: AdventurerData): number {
  const perHp = usageConfig.value?.gold_cost_per_hp ?? 2
  const missing = hospitalMissingHp(a)
  return perHp * missing
}

function hospitalMissingHp(a: AdventurerData): number {
  return Math.max(0, (a.max_hp ?? 100) - liveHp(a))
}

const hospitalTreatmentGold = ref(0)

function hospitalMaxAffordableGold(a: AdventurerData): number {
  const fullCost = hospitalTreatmentCost(a)
  return Math.min(fullCost, Math.floor(lowConsumptionAvailableGold(a)))
}

function hospitalHealHp(a: AdventurerData): number {
  const perHp = usageConfig.value?.gold_cost_per_hp ?? 2
  if (perHp <= 0) return 0
  return Math.min(hospitalMissingHp(a), Math.floor(hospitalTreatmentGold.value / perHp))
}

function hospitalActualCost(a: AdventurerData): number {
  const perHp = usageConfig.value?.gold_cost_per_hp ?? 2
  return hospitalHealHp(a) * perHp
}

function hospitalSelectedDuration(a: AdventurerData): number {
  const perHp = usageConfig.value?.usage_duration_minutes_per_hp ?? 2
  const healHp = hospitalHealHp(a)
  return healHp > 0 ? Math.max(1, healHp * perHp) : 0
}

function syncHospitalTreatmentGold() {
  const adv = usageCandidate.value
  if (!adv || handlerKey.value !== 'hospital') return
  const maxAfford = hospitalMaxAffordableGold(adv)
  const minCost = usageConfig.value?.gold_cost_per_hp ?? 2
  if (maxAfford < minCost) {
    hospitalTreatmentGold.value = 0
    return
  }
  const perHp = usageConfig.value?.gold_cost_per_hp ?? 2
  const currentHeal = Math.min(hospitalMissingHp(adv), Math.floor(hospitalTreatmentGold.value / perHp))
  if (currentHeal < 1 || hospitalTreatmentGold.value > maxAfford) {
    hospitalTreatmentGold.value = maxAfford
  }
}

watch([
  usageCandidate,
  handlerKey,
  () => usageCandidate.value?.gold,
  () => usageCandidate.value?.low_consumption_gold,
], syncHospitalTreatmentGold, { immediate: true })

const sanatoriumRentalHours = ref(1)

function sanatoriumMaxAffordableHours(a: AdventurerData): number {
  const perHour = usageConfig.value?.gold_cost_per_hour ?? 10
  if (perHour <= 0) return 1
  const maxByGold = Math.floor(lowConsumptionAvailableGold(a) / perHour)
  return Math.max(1, Math.min(72, maxByGold))
}

function sanatoriumActualCost(a: AdventurerData): number {
  const perHour = usageConfig.value?.gold_cost_per_hour ?? 10
  const hours = Math.max(1, Math.min(sanatoriumRentalHours.value, sanatoriumMaxAffordableHours(a)))
  return hours * perHour
}

function sanatoriumSelectedDuration(a: AdventurerData): number {
  const hours = Math.max(1, Math.min(sanatoriumRentalHours.value, sanatoriumMaxAffordableHours(a)))
  return hours * 60
}

function syncSanatoriumRentalHours() {
  const adv = usageCandidate.value
  if (!adv || handlerKey.value !== 'sanatorium') return
  const maxHours = sanatoriumMaxAffordableHours(adv)
  if (sanatoriumRentalHours.value < 1 || sanatoriumRentalHours.value > maxHours) {
    sanatoriumRentalHours.value = Math.max(1, maxHours)
  }
}

watch([
  usageCandidate,
  handlerKey,
  () => usageCandidate.value?.gold,
  () => usageCandidate.value?.low_consumption_gold,
], syncSanatoriumRentalHours, { immediate: true })

// ── 深渊手术室：按分钟付费回血（每分钟 hp_percent_per_minute% 最大HP + gold_cost_per_minute 金币） ──
const clinicRentalMinutes = ref(1)

function clinicHpPercent(): number {
  return usageConfig.value?.hp_percent_per_minute ?? 1
}
function clinicHealForMinutes(a: AdventurerData, minutes: number): number {
  return Math.floor(minutes * (a.max_hp ?? 100) * clinicHpPercent() / 100)
}
function clinicHpPerMinuteDisplay(a: AdventurerData): string {
  const rate = (a.max_hp ?? 100) * clinicHpPercent() / 100
  if (rate <= 0) return '1'
  return Number.isInteger(rate) ? String(Math.round(rate)) : rate.toFixed(1)
}
function clinicMissingHp(a: AdventurerData): number {
  return Math.max(0, (a.max_hp ?? 100) - liveHp(a))
}
function clinicMinutesToFull(a: AdventurerData): number {
  const rate = (a.max_hp ?? 100) * clinicHpPercent() / 100
  if (rate <= 0) return 1
  return Math.max(1, Math.ceil(clinicMissingHp(a) / rate))
}
function clinicMaxAffordableMinutes(a: AdventurerData): number {
  const perMin = usageConfig.value?.gold_cost_per_minute ?? 10
  if (perMin <= 0) return 1
  const maxByGold = Math.floor(lowConsumptionAvailableGold(a) / perMin)
  return Math.max(1, Math.min(clinicMinutesToFull(a), maxByGold))
}
function clinicEffectiveMinutes(a: AdventurerData): number {
  const raw = Number(clinicRentalMinutes.value)
  const minutes = Number.isFinite(raw) ? Math.floor(raw) : 1
  return Math.max(1, Math.min(minutes, clinicMaxAffordableMinutes(a)))
}
function clinicActualCost(a: AdventurerData): number {
  const perMin = usageConfig.value?.gold_cost_per_minute ?? 10
  return clinicEffectiveMinutes(a) * perMin
}
function clinicHealPreview(a: AdventurerData): number {
  return Math.min(clinicMissingHp(a), clinicHealForMinutes(a, clinicEffectiveMinutes(a)))
}
function clinicSelectedDuration(a: AdventurerData): number {
  return clinicEffectiveMinutes(a)
}
function syncClinicRentalMinutes() {
  const adv = usageCandidate.value
  if (!adv || handlerKey.value !== 'clinic') return
  const maxMin = clinicMaxAffordableMinutes(adv)
  const raw = Number(clinicRentalMinutes.value)
  if (!Number.isFinite(raw) || raw < 1) {
    clinicRentalMinutes.value = 1
  } else if (raw > maxMin) {
    clinicRentalMinutes.value = maxMin
  }
}
watch([
  usageCandidate,
  handlerKey,
  () => usageCandidate.value?.gold,
  () => usageCandidate.value?.low_consumption_gold,
], syncClinicRentalMinutes, { immediate: true })
watch(handlerKey, (key) => {
  if (key === 'clinic') clinicRentalMinutes.value = 1
})

// ── 深渊拾荒队：出门搜寻档位（按小时计费，最多 max_search_hours）+ 出门队伍展示 ──
const scavengeSearchHours = ref(1)

const scavengeMaxHours = computed(() => Math.max(1, usageConfig.value?.max_search_hours ?? 8))

function scavengeMaxAffordableHours(a: AdventurerData): number {
  const perHour = usageConfig.value?.gold_cost_per_hour ?? 20
  const capped = scavengeMaxHours.value
  if (perHour <= 0) return capped
  const maxByGold = Math.floor((a.gold ?? 0) / perHour)
  return Math.max(1, Math.min(capped, maxByGold))
}

function scavengeEffectiveHours(a: AdventurerData): number {
  return Math.max(1, Math.min(scavengeSearchHours.value, scavengeMaxAffordableHours(a)))
}

function scavengeActualCost(a: AdventurerData): number {
  const perHour = usageConfig.value?.gold_cost_per_hour ?? 20
  return scavengeEffectiveHours(a) * perHour
}

function syncScavengeSearchHours() {
  const adv = usageCandidate.value
  if (!adv || handlerKey.value !== 'scavenge') return
  const maxHours = scavengeMaxAffordableHours(adv)
  if (scavengeSearchHours.value < 1 || scavengeSearchHours.value > maxHours) {
    scavengeSearchHours.value = Math.max(1, maxHours)
  }
}
watch([usageCandidate, handlerKey, () => usageCandidate.value?.gold], syncScavengeSearchHours, { immediate: true })

// ── 线索追查处 ──
interface ClueBureauTarget {
  index: number
  label: string
  clue_eligible?: boolean
}

const clueBureauTargets = ref<ClueBureauTarget[]>([])
const clueBureauPoolCount = ref(0)
const clueBureauPoolCap = ref(10)
const clueBureauGrantEligibleCount = ref(0)

function syncClueBureauFromDetail(detail: BuildingInstance | null | undefined) {
  const cb = detail?.clue_bureau
  if (!cb) return
  clueBureauTargets.value = Array.isArray(cb.targets) ? cb.targets : []
  clueBureauPoolCount.value = Number(cb.pool_count ?? clueBureauTargets.value.length)
  clueBureauPoolCap.value = Number(cb.pool_cap ?? 10)
  clueBureauGrantEligibleCount.value = Number(
    cb.grant_eligible_count
      ?? clueBureauTargets.value.filter(t => t.clue_eligible !== false).length,
  )
}
const clueBureauSearchHours = ref(1)

const clueBureauMaxHours = computed(() => Math.max(1, usageConfig.value?.max_search_hours ?? 6))

function clueBureauMaxAffordableHours(a: AdventurerData): number {
  const perHour = usageConfig.value?.stamina_cost_per_hour ?? 20
  const capped = clueBureauMaxHours.value
  if (perHour <= 0) return capped
  const maxByStamina = Math.floor(liveStamina(a) / perHour)
  return Math.max(1, Math.min(capped, maxByStamina))
}

function clueBureauEffectiveHours(a: AdventurerData): number {
  return Math.max(1, Math.min(clueBureauSearchHours.value, clueBureauMaxAffordableHours(a)))
}

function clueBureauStaminaCost(a: AdventurerData): number {
  const perHour = usageConfig.value?.stamina_cost_per_hour ?? 20
  return clueBureauEffectiveHours(a) * perHour
}

function syncClueBureauSearchHours() {
  const adv = usageCandidate.value
  if (!adv || handlerKey.value !== 'clue_bureau') return
  const maxHours = clueBureauMaxAffordableHours(adv)
  if (clueBureauSearchHours.value < 1 || clueBureauSearchHours.value > maxHours) {
    clueBureauSearchHours.value = Math.max(1, maxHours)
  }
}

watch([usageCandidate, handlerKey, () => usageCandidate.value?.gold, () => usageCandidate.value?.stamina], syncClueBureauSearchHours, { immediate: true })
watch(buildingDetail, (detail) => {
  syncClueBureauFromDetail(detail)
  mergeMyAdventurerFromDetail(detail)
}, { immediate: true, deep: true })

/** 出门队伍：所有正在该建筑出门搜寻的冒险者（含自己），供"看谁出发了"展示 */
const scavengeOccupantsView = computed(() => {
  if (handlerKey.value !== 'scavenge') return []
  return buildingOccupants.value.map(occ => {
    const extra = occ.building_usage_extra as { search_hours?: number } | null | undefined
    const durationMin = effectiveUsageDurationMinutes(occ)
    const assigned = occ.building_assigned_at ? new Date(occ.building_assigned_at).getTime() : 0
    const endMs = assigned + durationMin * 60 * 1000
    const remainingSec = assigned ? Math.max(0, Math.floor((endMs - now.value) / 1000)) : 0
    return {
      id: occ.adventurer_id,
      name: occ.adventurer_name,
      isMine: myAdventurerIds.value.has(occ.adventurer_id),
      hours: extra?.search_hours ?? 0,
      remainingSec,
    }
  })
})

function formatScavengeRemaining(sec: number): string {
  if (sec <= 0) return '即将归来'
  const h = Math.floor(sec / 3600)
  const m = Math.floor((sec % 3600) / 60)
  if (h > 0) return `剩 ${h}小时${m}分`
  if (m > 0) return `剩 ${m}分`
  return '剩 不到 1 分'
}

function hospitalTreatmentDuration(a: AdventurerData): number {
  const perHp = usageConfig.value?.usage_duration_minutes_per_hp ?? 2
  const missing = Math.max(0, (a.max_hp ?? 100) - liveHp(a))
  return missing > 0 ? Math.max(1, missing * perHp) : 0
}

function effectiveUsageDurationMinutes(user?: BuildingHelper | null, adv?: AdventurerData | null): number {
  const cfg = usageConfig.value
  if (!cfg) return 0
  const stored = user?.building_usage_extra as { usage_duration_minutes?: number } | null | undefined
  if (stored?.usage_duration_minutes != null) return stored.usage_duration_minutes
  if (cfg.usage_duration_minutes_per_hp && adv) {
    return hospitalTreatmentDuration(adv)
  }
  return cfg.usage_duration_minutes ?? 0
}

function statLabel(stat: string): string {
  const map: Record<string, string> = {
    strength: '力量', agility: '敏捷', intelligence: '智力', luck: '运气',
  }
  return map[stat] ?? stat
}

function materialIcon(name: string): string {
  const metaIcon = store.materialMeta?.[name]?.icon
  if (metaIcon) return metaIcon
  const map: Record<string, string> = { wood: '🪵', stone: '🪨' }
  return map[name] ?? '📦'
}

function matIcon(name: string): string {
  return materialIcon(name)
}

function matLabel(name: string): string {
  const metaName = store.materialMeta?.[name]?.name
  if (metaName) return metaName
  const map: Record<string, string> = { wood: '木材', stone: '石料' }
  return map[name] ?? name
}

function myMaterialBalance(mat: string): number {
  return Number(store.materialBalances?.[mat] ?? store.dormitoryMyStatus?.my_materials?.[mat] ?? 0)
}

async function submitUpgradeDonate(mat: string) {
  const amount = upgradeDonateAmounts[mat]
  if (!amount || amount <= 0 || !props.building.id) return
  donatingUpgrade.value = true
  try {
    const res = await store.donateToBuildingUpgrade(props.building.id, mat, amount)
    if (res.code === 200) {
      const donated = res.data?.donated_amount ?? amount
      message.success(`捐献 ${donated} ${matLabel(mat)}`)
      upgradeDonateAmounts[mat] = null
      await Promise.all([store.fetchMyMaterials(), refreshBuildingDetail()])
      emit('refreshed')
    } else {
      message.error(res.message || '捐献失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '捐献失败')
  } finally {
    donatingUpgrade.value = false
  }
}

async function submitDonate(mat: string) {
  const amount = donateAmounts[mat]
  if (!amount || amount <= 0) return
  donating.value = true
  try {
    const res = await store.donateToDormitory(props.building.id, null, mat, amount)
    if (res.code === 200) {
      const donated = res.data?.donated_amount ?? amount
      const triggered = res.data?.triggered_build_start
      message.success(
        triggered
          ? `捐献 ${donated} ${matLabel(mat)}，材料已凑齐自动开工！`
          : `捐献 ${donated} ${matLabel(mat)}`
      )
      donateAmounts[mat] = null
      store.fetchMyMaterials()
      emit('refreshed')
    } else {
      message.error(res.message || '捐献失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '捐献失败')
  } finally {
    donating.value = false
  }
}

function canUseBuilding(adv: AdventurerData): { ok: boolean; reason?: string } {
  const cfg = usageConfig.value
  if (!cfg) return { ok: false, reason: '配置错误' }
  if (cooldownRemainingSeconds.value > 0) {
    return { ok: false, reason: `冷却中，还需 ${cooldownRemainingText.value}` }
  }
  const slots = buildingUsageSlots.value
  if (
    slots > 0
    && buildingOccupants.value.length >= slots
    && !isBuildingUsageQueueEligible.value
  ) {
    return {
      ok: false,
      reason: formatUsageSlotsFullMessage(
        buildingOccupants.value.length,
        slots,
        handlerKey.value,
        props.building.template?.category,
      ),
    }
  }
  const stamina = liveStamina(adv)
  if (stamina < (cfg.usage_stamina_cost ?? 0)) return { ok: false, reason: '精力不足' }
  if (handlerKey.value === 'pet_evolution') {
    if ((adv.gold ?? 0) < petEvolutionGoldCost.value) {
      return { ok: false, reason: `金币不足（需要 ${petEvolutionGoldCost.value.toLocaleString()}）` }
    }
  } else if (cfg.gold_cost_per_hour && handlerKey.value !== 'clue_bureau') {
    const perHour = cfg.gold_cost_per_hour ?? 10
    const isScavenge = handlerKey.value === 'scavenge'
    const cost = isScavenge ? scavengeActualCost(adv) : sanatoriumActualCost(adv)
    if (cost < perHour) {
      const hourLabel = isScavenge ? '出门 1 小时' : '租用 1 小时'
      return { ok: false, reason: `金币不足（至少 ${perHour} 金币${hourLabel}）` }
    }
    if (lowConsumptionAvailableGold(adv) < cost) {
      return { ok: false, reason: `可用金额不足（需要 ${cost}）` }
    }
  } else if (handlerKey.value === 'clue_bureau') {
    const perHour = cfg.stamina_cost_per_hour ?? 20
    const cost = clueBureauStaminaCost(adv)
    if (liveStamina(adv) < perHour) {
      return { ok: false, reason: `精力不足（至少 ${perHour}，追查 1 小时）` }
    }
    if (liveStamina(adv) < cost) {
      return { ok: false, reason: `精力不足（需要 ${cost}，当前 ${liveStamina(adv)}）` }
    }
  } else if (cfg.gold_cost_per_minute) {
    const curHp = liveHp(adv)
    if (curHp >= (adv.max_hp ?? 100)) return { ok: false, reason: '生命值已满' }
    const perMin = cfg.gold_cost_per_minute ?? 10
    if (lowConsumptionAvailableGold(adv) < perMin) {
      return { ok: false, reason: `可用金额不足（至少 ${perMin}，驻留 1 分钟）` }
    }
  } else if (cfg.gold_cost_per_hp) {
    const curHp = liveHp(adv)
    if (curHp >= (adv.max_hp ?? 100)) return { ok: false, reason: '生命值已满' }
    const cost = handlerKey.value === 'hospital'
      ? hospitalActualCost(adv)
      : hospitalTreatmentCost(adv)
    const minCost = cfg.gold_cost_per_hp ?? 2
    if (handlerKey.value === 'hospital' && cost < minCost) {
      return { ok: false, reason: `治疗金额至少 ${minCost} 金币（恢复 1 点生命值）` }
    }
    if (lowConsumptionAvailableGold(adv) < cost) {
      return { ok: false, reason: `可用金额不足（需要 ${cost}）` }
    }
  } else if (cfg.gold_cost && (adv.gold ?? 0) < cfg.gold_cost) {
    return { ok: false, reason: '金币不足' }
  } else if (handlerKey.value === 'dining_hall') {
    const food = selectedDiningItem.value
    if (!food) return { ok: false, reason: '请选择餐品' }
    if ((adv.gold ?? 0) < food.gold_cost) {
      return { ok: false, reason: `金币不足（需要 ${food.gold_cost}）` }
    }
  }
  if (handlerKey.value === 'clue_bureau' && clueBureauPoolCount.value === 0) {
    return { ok: false, reason: '追查池为空，请联系管理员补充' }
  }
  if (cfg.required_stat && cfg.required_stat_min) {
    const val = (adv as any)[cfg.required_stat] ?? 0
    if (val < cfg.required_stat_min) return { ok: false, reason: `${statLabel(cfg.required_stat)}不足` }
  }
  if (handlerKey.value === 'training') {
    const cap = adv.training_cap
    const used = adv.training_count ?? 0
    if (cap != null && used >= cap) {
      return { ok: false, reason: `训练潜能已耗尽（${used}/${cap}）` }
    }
  }
  return { ok: true }
}

async function dispatchUsage() {
  const adv = usageCandidate.value
  if (!adv || dispatchingUsage.value) return
  const check = canUseBuilding(adv)
  if (!check.ok) {
    message.error(check.reason ?? '无法使用')
    return
  }
  // 治疗所/小圣堂：必须选择特质
  if ((handlerKey.value === 'healing_house' || handlerKey.value === 'chapel') && !selectedTrait.value) {
    message.error(handlerKey.value === 'chapel' ? '请先选择要净化的特质' : '请先选择要治疗的特质')
    return
  }
  if (isItemCraftHandler.value) {
      if (!craftSelectionValid.value) {
        message.error(
          handlerKey.value === 'alchemy_workshop'
            ? '请先选择 1~5 件炼金材料'
            : (handlerKey.value === 'tailor_workshop'
                ? '请先按顺序选择衣装主体与辅材，共 2 件'
                : handlerKey.value === 'item_assembly_workshop'
                ? '请先按顺序选择 2~5 件构装物品'
                : (handlerKey.value === 'ember_kitchen'
                    ? '请先选择 1~5 份食材'
                    : '请先选择 2 件锻造物品')),
        )
        return
      }
  }
  if (handlerKey.value === 'dining_hall' && !selectedFoodCode.value) {
    message.error('请先选择餐品')
    return
  }
  if (handlerKey.value === 'pet_evolution' && selectedCompanionId.value == null) {
    message.error('请先选择要嬗变或孵化的伙伴')
    return
  }
  if (handlerKey.value === 'pet_evolution') {
    const companion = selectedCompanion.value
    if (!companion) {
      message.error('所选伙伴已不在可嬗变列表中，请重新选择')
      return
    }
    if (companion.is_player_locked) {
      message.error('所选伙伴已受保护，请先解锁再进行嬗变')
      return
    }
    const confirmed = window.confirm(formatPetEvolutionDispatchConfirm({
      adventurerName: adv.name,
      companionName: companion.name,
      currentStage: companion.evolution_stage ?? 0,
      goldCost: petEvolutionGoldCost.value,
      currentGold: adv.gold ?? 0,
      auxiliaryMaterialName: selectedAuxiliaryMaterial.value?.name,
    }))
    if (!confirmed) return
  }
  if (!isItemCraftHandler.value) {
    showForgePanel.value = false
  }
  dispatchingUsage.value = true
  try {
    const extra = handlerKey.value === 'healing_house'
      ? {
          target_traits: [selectedTrait.value],
          ...(healingItemRef.value ? { item_ref: healingItemRef.value } : {}),
        }
      : handlerKey.value === 'chapel'
      ? { target_traits: [selectedTrait.value] }
      : isItemCraftHandler.value
        ? {
            item_refs: buildCraftItemRefs(),
            ...(handlerKey.value === 'blacksmith'
              ? { forge_intent: selectedForgeIntent.value }
              : {}),
            ...(handlerKey.value === 'tailor_workshop'
              ? { tailor_operation: selectedTailorOperation.value }
              : {}),
            ...(handlerKey.value === 'item_assembly_workshop'
              ? {
                  layout_mode: assemblyLayoutMode.value,
                  layout_slots: buildAssemblyLayoutSlots(),
                  desired_direction: assemblyDesiredDirection.value.trim(),
                }
              : {}),
            ...(handlerKey.value === 'ember_kitchen'
              ? { cookware_code: selectedCookwareCode.value }
              : {}),
          }
      : handlerKey.value === 'hospital'
          ? { treatment_gold: hospitalActualCost(adv) }
          : handlerKey.value === 'sanatorium'
            ? { rental_hours: Math.max(1, sanatoriumRentalHours.value) }
            : handlerKey.value === 'clinic'
            ? { rental_minutes: clinicEffectiveMinutes(adv) }
            : handlerKey.value === 'scavenge'
            ? { search_hours: scavengeEffectiveHours(adv) }
            : handlerKey.value === 'clue_bureau'
            ? { search_hours: clueBureauEffectiveHours(adv) }
            : handlerKey.value === 'dining_hall'
            ? { food_code: selectedFoodCode.value }
            : handlerKey.value === 'pet_evolution'
              ? {
                  companion_id: selectedCompanionId.value ?? undefined,
                  auxiliary_material_ref: selectedAuxiliaryMaterial.value
                    ? {
                        source: selectedAuxiliaryMaterial.value.from,
                        id: selectedAuxiliaryMaterial.value.id,
                        name: selectedAuxiliaryMaterial.value.name,
                      }
                    : undefined,
                }
            : undefined
    const requestExtra = {
      ...(extra ?? {}),
      ...(shouldJoinBuildingUsageQueue.value ? { join_queue: true } : {}),
    }
    const res = await store.startBuildingUsage(props.building.id, adv.id, requestExtra)
    if (res.code === 200) {
      const dispatchMsgs: Record<string, string> = {
        healing_house: '冒险者已前往治疗所，等待治疗…',
        chapel: '冒险者已前往小圣堂，等待祈祷…',
        hospital: '冒险者已前往医院，等待治疗…',
        sanatorium: '冒险者已入住疗养所，恢复期加速中…',
        clinic: '冒险者已进入手术室，治疗进行中…',
        scavenge: '冒险者已出门搜寻，归来时见分晓…',
        clue_bureau: '冒险者已出门追查线索，归来时见分晓…',
        dining_hall: '冒险者已开始用餐，请稍候…',
        blacksmith: '冒险者已前往铁匠铺',
        tailor_workshop: '冒险者已前往绮罗裁坊',
        alchemy_workshop: '冒险者已前往炼金工房',
        item_assembly_workshop: '冒险者已前往构装工坊',
        ember_kitchen: '冒险者已在余烬灶坊点起炉火，出锅后见分晓…',
        training: '冒险者开始修炼',
        pet_evolution: '冒险者已进入祭坛，等待嬗变仪式…',
      }
      if (res.data?.queued) {
        message.success(`候场登记完成，当前第 ${res.data.queue_position ?? '?'} 位；入席后才开始计时`)
      } else {
        message.success(dispatchMsgs[handlerKey.value ?? ''] ?? '已派遣')
      }
      selectedTrait.value = ''
      healingItemRef.value = null
      healingItems.value = []
      showHealingItemPicker.value = false
      forgeSelectedRefs.value = []
      forgeSearchQuery.value = ''
      assemblyLayoutMode.value = 'vertical_line'
      assemblyDesiredDirection.value = ''
      selectedCookwareCode.value = 'stew_pot'
      selectedForgeIntent.value = 'free'
      selectedTailorOperation.value = 'free'
      forgeItems.value = []
      showForgePanel.value = false
      emit('refreshed')
      await refreshBuildingDetail()
    } else {
      message.error(res.message || '派遣失败')
      void syncAdventurerFromBuildingDetail()
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '派遣失败')
    void syncAdventurerFromBuildingDetail()
  } finally {
    dispatchingUsage.value = false
  }
}

async function cancelBuildingUsageQueue() {
  const adv = detailMyAdventurer.value
  if (!adv || !myUsageQueue.value || cancellingUsageQueue.value) return
  if (!confirm('确定离开候场队列？预留金币、精力与已消耗的嬗变辅助素材会原样退回。')) return
  cancellingUsageQueue.value = true
  try {
    const res = await store.cancelBuildingUsageQueue(props.building.id, adv.id)
    if (res.code === 200) {
      const gold = Number(res.data?.gold_refunded ?? 0)
      const stamina = Number(res.data?.stamina_refunded ?? 0)
      const refundParts = [
        gold > 0 ? `金币 ${gold}` : '',
        stamina > 0 ? `精力 ${stamina}` : '',
        res.data?.auxiliary_material_refunded ? '嬗变辅助素材' : '',
      ].filter(Boolean)
      message.success(
        refundParts.length > 0
          ? `已离开候场，退回${refundParts.join('、')}`
          : '已离开候场',
      )
      emit('refreshed')
      await refreshBuildingDetail()
    } else {
      message.error(res.message || '取消候场失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '取消候场失败')
  } finally {
    cancellingUsageQueue.value = false
  }
}

async function quitTreatmentBuilding() {
  if (!myActiveUsage.value) return
  const isHospitalLike = handlerKey.value === 'hospital' || handlerKey.value === 'clinic'
  const prompt = isHospitalLike
    ? '确定现在出院？已完成的治疗量会结算，已缴费用不会退还。'
    : '确定退出疗养？已缴租金不会退还，冒险者将返回宿舍。'
  if (!confirm(prompt)) return
  recallingUsage.value = true
  try {
    const res = await store.recallFromBuilding(myActiveUsage.value.adventurer_id)
    if (res.code === 200) {
      message.success(isHospitalLike ? '已出院，已完成的治疗量已结算' : '已退出疗养，冒险者返回宿舍')
      emit('refreshed')
      await syncAdventurerFromBuildingDetail()
    } else {
      message.error(res.message || '退出失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '退出失败')
  } finally {
    recallingUsage.value = false
  }
}

onMounted(async () => {
  if (props.building.client_preview_only && isAssessmentPreview.value) {
    upgradeSectionLoading.value = false
    adventurersLoading.value = false
    return
  }
  socket.on('adventurer_changed', onAdventurerChanged)
  socket.on('adventurer_changed', onConstructionSocket)
  adventurersLoading.value = store.adventurers.length === 0
  try {
    await refreshBuildingDetail()
    const detail = buildingDetail.value
    const mayNeedUpgrade = detail.status === 'built'
      && detail.template?.code !== 'tavern_hall'
      && detail.template?.category !== 'dormitory'
    await Promise.all([
      needsMaterialsFetch.value || (mayNeedUpgrade && buildingUpgradeUnlocked.value)
        ? store.fetchMyMaterials()
        : Promise.resolve(),
      mayNeedUpgrade && !store.tavernUpgrade
        ? store.fetchTavernUpgradeProgress()
        : Promise.resolve(),
    ])
  } finally {
    upgradeSectionLoading.value = false
    adventurersLoading.value = false
  }
})

onUnmounted(() => {
  socket.off('adventurer_changed', onAdventurerChanged)
  socket.off('adventurer_changed', onConstructionSocket)
})
</script>

<style scoped>
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
  max-width: 500px;
  width: 100%;
  max-height: 85vh;
  overflow-y: auto;
}
.modal-title {
  margin: 0 0 14px;
  font-size: 1.1em;
  color: var(--color-text);
}
.modal-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
}
.modal-lg {
  max-width: 560px;
}

.modal-xl {
  max-width: min(1040px, calc(100vw - 32px));
}

@media (min-width: 900px) {
  .modal-xl .detail-stats {
    grid-template-columns: repeat(4, minmax(0, 1fr));
  }
}

.detail-header {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}
.detail-icon {
  font-size: 2.5em;
}
.detail-name {
  margin: 0;
  font-size: 1.3em;
}
.detail-status {
  font-size: 0.8em;
  padding: 2px 8px;
  border-radius: 4px;
  display: inline-block;
  margin-top: 4px;
}
.badge-locked {
  background: rgba(128, 128, 128, 0.2);
  color: #888;
}
.badge-building {
  background: rgba(255, 193, 7, 0.2);
  color: #ffc107;
}
.badge-built {
  background: rgba(25, 135, 84, 0.2);
  color: #198754;
}

.detail-desc {
  color: var(--color-text-secondary);
  font-size: 0.9em;
  margin-bottom: 16px;
  line-height: 1.5;
}

.detail-stats {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
  margin-bottom: 16px;
}
.stat-item {
  background: var(--color-bg);
  border-radius: 8px;
  padding: 10px;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.stat-label {
  font-size: 0.75em;
  color: var(--color-text-secondary);
}
.stat-value {
  font-size: 0.95em;
}

.progress-section {
  margin-bottom: 16px;
}
.progress-header {
  display: flex;
  justify-content: space-between;
  font-size: 0.85em;
  margin-bottom: 6px;
}
.progress-bar-lg {
  height: 10px;
  background: var(--color-border);
  border-radius: 5px;
  overflow: hidden;
  margin-bottom: 6px;
}
.progress-fill-lg {
  height: 100%;
  background: var(--color-primary);
  border-radius: 5px;
  transition: width 0.3s;
}
.remaining-text {
  font-size: 0.85em;
  color: var(--color-warning);
  text-align: center;
}

.helpers-section {
  margin-bottom: 16px;
}
.section-title {
  font-size: 0.9em;
  margin: 0 0 8px;
}
.helpers-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.helper-item {
  display: flex;
  justify-content: space-between;
  align-items: center;
  background: var(--color-bg);
  padding: 8px 10px;
  border-radius: 6px;
  font-size: 0.85em;
}
.helper-info {
  min-width: 0;
  flex-wrap: wrap;
  display: flex;
  align-items: center;
  gap: 10px;
}
.helper-name {
  font-weight: 500;
}
.helper-stamina {
  font-size: 0.85em;
  color: var(--color-text-secondary);
}
.btn-recall {
  background: rgba(220, 53, 69, 0.15);
  color: #dc3545;
  border: 1px solid #dc3545;
  padding: 4px 10px;
  border-radius: 4px;
  cursor: pointer;
  font-size: 0.8em;
}
.btn-recall:hover:not(:disabled) {
  background: rgba(220, 53, 69, 0.3);
}
.btn-recall:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-quit-sanatorium {
  display: block;
  width: 100%;
  margin-top: 10px;
  text-align: center;
}
.empty-helpers {
  text-align: center;
  color: var(--color-text-secondary);
  font-size: 0.85em;
  padding: 12px;
}

.detail-actions {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  justify-content: flex-end;
  flex-wrap: wrap;
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
.btn-reject {
  background: rgba(220, 53, 69, 0.15);
  color: #dc3545;
  border: 1px solid #dc3545;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}
.btn-gm {
  background: rgba(255, 193, 7, 0.15);
  color: #ffc107;
  border: 1px solid #ffc107;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}
.btn-gm:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
.btn-toggle-off {
  background: rgba(220, 53, 69, 0.15);
  color: #dc3545;
  border: 1px solid #dc3545;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}
.btn-toggle-on {
  background: rgba(25, 135, 84, 0.15);
  color: #198754;
  border: 1px solid #198754;
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}
.btn-toggle-off:disabled, .btn-toggle-on:disabled {
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

.usage-info-section {
  margin-bottom: 16px;
}

.flower-shop-guidance {
  display: flex;
  flex-direction: column;
  gap: 0.45rem;
  margin-bottom: 16px;
  padding: 14px 16px;
  border: 1px solid var(--color-border, #d8cfbf);
  border-radius: 10px;
  background: var(--color-surface, #f8f4ec);
  color: var(--color-text, #2f2922);
  line-height: 1.55;
}

.flower-shop-guidance span {
  color: var(--color-text-secondary, #746a5d);
  font-size: 0.9rem;
}

.building-upgrade-section {
  margin-bottom: 16px;
}

.building-upgrade-donate {
  margin: 12px 0;
}

.building-upgrade-card {
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 10px;
  padding: 12px 14px;
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.building-upgrade-level {
  font-size: 0.95em;
  font-weight: 600;
  color: var(--color-text);
}

.building-upgrade-lock,
.building-upgrade-hint {
  margin: 0;
  font-size: 0.85em;
  color: var(--color-text-secondary);
  line-height: 1.45;
}

.building-upgrade-lock {
  color: var(--color-warning);
}

.building-upgrade-btn {
  align-self: flex-start;
}
.usage-stats {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  font-size: 0.85em;
  margin-bottom: 10px;
}
.usage-stats > span {
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--color-primary) 12%, var(--color-border));
  border-radius: 8px;
  background: var(--color-bg);
  background: color-mix(in srgb, var(--color-bg) 94%, var(--color-primary));
  color: var(--color-text);
  line-height: 1.4;
}
.usage-stats-stacked {
  grid-template-columns: 1fr;
}
.usage-cooldown-notice {
  margin-bottom: 10px;
  padding: 9px 10px;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--color-warning) 40%, var(--color-border));
  border-radius: 8px;
  background: var(--color-bg);
  background: color-mix(in srgb, var(--color-bg) 92%, var(--color-warning));
  color: var(--color-text);
  font-size: 0.85em;
  line-height: 1.5;
}
.usage-cooldown-notice strong {
  color: var(--color-warning);
}
.usage-desc {
  font-size: 0.85em;
  color: var(--color-text-secondary);
  margin: 0;
  padding: 9px 10px;
  border-left: 3px solid var(--color-primary);
  border-left: 3px solid color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
  border-radius: 0 6px 6px 0;
  background: var(--color-bg);
  background: color-mix(in srgb, var(--color-bg) 96%, var(--color-primary));
  line-height: 1.5;
}
.usage-desc + .usage-desc {
  margin-top: 6px;
}
.usage-summary {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 9px 10px;
  border-left: 3px solid var(--color-primary);
  border-left: 3px solid color-mix(in srgb, var(--color-primary) 45%, var(--color-border));
  border-radius: 0 6px 6px 0;
  background: var(--color-bg);
  background: color-mix(in srgb, var(--color-bg) 96%, var(--color-primary));
  font-size: 0.85em;
  line-height: 1.5;
}
.usage-summary-desc {
  min-width: 0;
  color: var(--color-text-secondary);
}
.reward-preview {
  flex: 0 0 auto;
  color: #3fb950;
  font-weight: 600;
  white-space: nowrap;
}
.risk-warning {
  margin-top: 10px;
  padding: 10px 12px;
  background: rgba(255, 193, 7, 0.08);
  border: 1px dashed rgba(255, 193, 7, 0.4);
  border-radius: 8px;
  font-size: 0.85em;
}
.risk-title {
  font-weight: 600;
  color: var(--color-warning);
  margin-bottom: 6px;
}
.risk-list {
  margin: 0;
  padding-left: 18px;
  color: var(--color-text-secondary);
  line-height: 1.7;
}
.risk-list strong {
  color: var(--color-danger);
}

.usage-active-section {
  margin-bottom: 16px;
}
.usage-active-card {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--color-bg);
  padding: 10px 12px;
  border-radius: 8px;
  margin-bottom: 10px;
}
.active-adv-name {
  font-weight: 500;
}
.active-stamina {
  font-size: 0.85em;
  color: var(--color-text-secondary);
}
.usage-timer {
  margin-bottom: 10px;
}
.usage-progress {
  background: var(--color-success);
}
.usage-complete-btn {
  display: block;
  width: 100%;
  margin-top: 8px;
}
.skill-school-retry-assurance {
  display: flex;
  flex-direction: column;
  gap: 4px;
  margin: 10px 0;
  padding: 10px 12px;
  border: 1px solid rgba(46, 160, 67, 0.45);
  border-radius: 8px;
  background: rgba(46, 160, 67, 0.1);
  color: var(--color-text-secondary);
  font-size: 0.85em;
  line-height: 1.6;
}
.skill-school-retry-assurance strong {
  color: var(--color-success);
}

.ai-pending-hint {
  text-align: center;
  font-size: 0.85em;
  color: var(--color-warning);
  background: rgba(255, 193, 7, 0.08);
  border: 1px dashed rgba(255, 193, 7, 0.4);
  border-radius: 6px;
  padding: 10px;
}
.auto-settling-hint {
  text-align: center;
  font-size: 0.85em;
  color: var(--color-text-secondary);
  padding: 10px;
}

.forge-panel {
  margin-top: 4px;
}
.forge-panel .usage-dispatch-btn-block {
  margin-top: 12px;
}

.assessment-preview-notice {
  display: grid;
  grid-template-columns: auto minmax(0, 1fr);
  gap: 14px;
  margin-bottom: 16px;
  padding: 16px;
  border: 1px solid #9d86d8;
  border: 1px solid color-mix(in srgb, #9d86d8 48%, var(--color-border));
  border-radius: 12px;
  background:
    radial-gradient(circle at 0 0, rgba(157, 134, 216, 0.16), transparent 45%),
    var(--color-surface);
  background:
    radial-gradient(circle at 0 0, rgba(157, 134, 216, 0.16), transparent 45%),
    color-mix(in srgb, #35284e 12%, var(--color-surface));
  box-shadow: inset 0 0 22px rgba(76, 54, 112, 0.08);
}

.assessment-preview-seal {
  display: grid;
  width: 42px;
  height: 42px;
  place-items: center;
  border: 1px solid #9d86d8;
  border: 1px solid color-mix(in srgb, #bba8ed 62%, var(--color-border));
  border-radius: 50%;
  color: #c7b8ef;
  background: rgba(72, 50, 108, 0.16);
  font-size: 1.25rem;
}

.assessment-preview-notice strong {
  color: #bba8ed;
  color: color-mix(in srgb, #c7b8ef 84%, var(--color-text));
  letter-spacing: 0.12em;
}

.assessment-preview-notice p {
  margin: 0.45rem 0;
  color: var(--color-text);
  line-height: 1.65;
}

.assessment-preview-notice small {
  color: var(--color-text-secondary);
  line-height: 1.55;
}

.badge-assessment {
  color: #bba8ed;
  background: rgba(92, 68, 140, 0.16);
}
.forge-search-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin: 12px 0;
}
.forge-search-input {
  flex: 1;
  min-width: 0;
}
.forge-search-clear {
  flex: 0 0 auto;
  min-width: 60px;
  min-height: 36px;
  padding: 6px 12px;
  color: var(--color-text);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
}
.forge-search-clear:hover {
  color: var(--color-primary);
  border-color: var(--color-primary);
}
.forge-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-bottom: 12px;
  max-height: 220px;
  overflow-y: auto;
}
.forge-item {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  background: var(--color-bg);
  border: 1px solid transparent;
  border-radius: 6px;
  padding: 8px 10px;
  cursor: pointer;
  font-size: 0.85em;
}
.forge-item input {
  margin-top: 3px;
  flex-shrink: 0;
}
.forge-qty-stepper {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-shrink: 0;
  margin-top: 1px;
}
.forge-qty-btn {
  width: 24px;
  height: 24px;
  padding: 0;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-bg);
  color: var(--color-text);
  cursor: pointer;
  line-height: 1;
}
.forge-qty-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
.forge-qty-value {
  min-width: 18px;
  text-align: center;
  font-weight: 600;
}
.forge-item.forge-selected {
  border-color: var(--color-primary);
}
.forge-item.forge-disabled {
  opacity: 0.5;
  cursor: not-allowed;
}
@media (max-width: 640px) {
  .modal-box {
    padding: 18px;
    max-height: calc(100vh - 16px);
  }

  .detail-stats {
    grid-template-columns: 1fr;
  }

  .usage-stats {
    grid-template-columns: 1fr;
  }

  .usage-summary {
    align-items: flex-start;
    flex-direction: column;
    gap: 4px;
  }

  .reward-preview {
    white-space: normal;
  }

  .forge-search-input {
    font-size: 16px;
  }

  .forge-search-clear {
    min-height: 36px;
  }
}
.assembly-layout-section {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 8px;
  margin-top: 10px;
}
.assembly-label {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 0.85em;
  color: var(--color-text-secondary);
}
.assembly-select,
.assembly-note {
  width: 100%;
}
.assembly-note {
  min-height: 64px;
  resize: vertical;
}
.assembly-slots-preview {
  grid-column: 1 / -1;
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.assembly-slot {
  display: grid;
  grid-template-columns: 28px 72px minmax(0, 1fr) auto;
  gap: 6px;
  align-items: center;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  padding: 6px 8px;
  background: var(--color-bg);
  font-size: 0.82em;
}
.assembly-slot-index {
  color: var(--color-primary);
  font-weight: 700;
}
.assembly-slot-pos {
  color: var(--color-text-secondary);
}
.assembly-slot-name {
  color: var(--color-text);
  overflow-wrap: anywhere;
}
.assembly-slot-actions {
  display: flex;
  gap: 6px;
}
.assembly-slot-actions .forge-qty-btn {
  min-width: 44px;
  min-height: 44px;
}
.assembly-slot-item {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
}
.assembly-slot-state {
  color: var(--color-warning);
  font-size: 0.9em;
  line-height: 1.3;
}
.cookware-section {
  margin-top: 12px;
}
.forge-intent-section {
  margin-top: 14px;
}
.forge-intent-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(210px, 1fr));
  gap: 8px;
}
.forge-intent-option {
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 9px;
  align-items: start;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 10px;
  background: var(--color-bg);
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}
.forge-intent-option.selected {
  border-color: var(--color-primary);
  background: rgba(88, 166, 255, 0.1);
  background: color-mix(in srgb, var(--color-primary) 10%, var(--color-bg));
}
.forge-intent-option input {
  margin: 3px 0 0;
}
.forge-intent-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
  color: var(--color-text-secondary);
  font-size: 0.82em;
  line-height: 1.4;
}
.forge-intent-copy strong {
  color: var(--color-text);
  font-size: 1.05em;
}
.cookware-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  gap: 8px;
}
.cookware-option {
  display: grid;
  grid-template-columns: auto auto 1fr;
  gap: 8px;
  align-items: center;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  padding: 9px 10px;
  background: var(--color-bg);
  cursor: pointer;
  transition: border-color 0.15s ease, background-color 0.15s ease;
}
.cookware-option.selected {
  border-color: var(--color-primary);
  background: rgba(88, 166, 255, 0.1);
  background: color-mix(in srgb, var(--color-primary) 10%, var(--color-bg));
}
.cookware-option input {
  margin: 0;
}
.cookware-icon {
  font-size: 1.35em;
}
.cookware-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 2px;
  font-size: 0.82em;
  color: var(--color-text-secondary);
}
.cookware-copy strong {
  color: var(--color-text);
  font-size: 1.05em;
}

.usage-select-section {
  margin-bottom: 16px;
}
.usage-queue-section {
  margin-bottom: 16px;
  padding: 14px;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--detail-brass) 32%, var(--color-border));
  border-radius: 12px;
  background: linear-gradient(120deg, rgba(91, 132, 178, 0.09), var(--color-bg));
  background: linear-gradient(120deg, color-mix(in srgb, var(--detail-brass) 9%, var(--color-bg)), var(--color-bg));
}
.usage-queue-card {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 11px 12px;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--detail-brass) 20%, var(--color-border));
  border-radius: 10px;
  background:
    radial-gradient(circle at 94% 0%, rgba(91, 132, 178, 0.1), transparent 26%),
    var(--color-surface);
  background: color-mix(in srgb, var(--detail-brass) 5%, var(--color-surface));
}
.usage-queue-seal {
  display: grid;
  width: 38px;
  height: 38px;
  flex: none;
  place-items: center;
  border-radius: 50%;
  background: rgba(91, 132, 178, 0.18);
  background: color-mix(in srgb, var(--detail-brass) 18%, var(--color-bg));
  font-size: 20px;
}
.usage-queue-copy {
  display: flex;
  min-width: 0;
  flex-direction: column;
  gap: 3px;
  color: var(--color-text-secondary);
  font-size: 0.86em;
}
.usage-queue-copy strong { color: var(--color-text); }
.usage-queue-hint {
  margin: 10px 0;
  color: var(--color-text-secondary);
  font-size: 0.84em;
  line-height: 1.65;
}
.usage-queue-cancel { width: 100%; }
.usage-dispatch-btn-block {
  display: block;
  width: 100%;
  margin-bottom: 8px;
}
.dispatch-status {
  font-size: 0.85em;
  color: var(--color-text-secondary);
  text-align: center;
}
.trait-select-section {
  margin-bottom: 12px;
}
.trait-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 200px;
  overflow-y: auto;
}
.trait-option {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--color-border, #333);
  border-radius: 8px;
  cursor: pointer;
  transition: border-color 0.15s, background 0.15s;
}
.trait-option:hover {
  border-color: var(--color-primary, #0d6efd);
  background: rgba(13, 110, 253, 0.06);
}
.trait-option.selected {
  border-color: var(--color-primary, #0d6efd);
  background: rgba(13, 110, 253, 0.12);
}
.trait-option input[type="radio"] {
  margin: 0;
  accent-color: var(--color-primary, #0d6efd);
}
.trait-option.inventory-item-option {
  align-items: flex-start;
}
.trait-option.inventory-item-option input[type="radio"] {
  margin-top: 3px;
}
.trait-name {
  font-weight: 600;
  font-size: 0.9em;
  color: var(--color-text);
}
.trait-desc {
  font-size: 0.8em;
  color: var(--color-text-secondary);
  flex: 1;
  text-align: right;
}

.pet-evolution-section {
  margin-bottom: 12px;
}
.pet-adventurer-select {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
  font-size: 0.85em;
}
.pet-select {
  flex: 1;
  padding: 6px 8px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-surface);
  color: var(--color-text);
}
.pet-list {
  display: flex;
  flex-direction: column;
  gap: 4px;
  max-height: 200px;
  overflow-y: auto;
  padding: 2px;
}
.pet-material-list {
  overscroll-behavior-y: contain;
  scrollbar-gutter: stable;
}
.pet-list::-webkit-scrollbar {
  width: 4px;
}
.pet-list::-webkit-scrollbar-thumb {
  background: var(--color-border);
  border-radius: 2px;
}
.pet-list::-webkit-scrollbar-track {
  background: transparent;
}
.pet-option {
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
  flex-shrink: 0;
  font-size: 0.85em;
}
.pet-option > input {
  flex-shrink: 0;
  margin-top: 3px;
}
.pet-option.selected {
  border-color: var(--color-primary);
  background: rgba(13, 110, 253, 0.1);
}
.pet-option.pet-disabled {
  opacity: 0.55;
  cursor: not-allowed;
}
.pet-option-main {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px;
  line-height: 1.4;
}
.pet-option-main span {
  color: var(--color-text-secondary);
}
.pet-evolution-hint {
  margin: 8px 0 0;
  color: var(--color-text-secondary);
  font-size: 0.8em;
  line-height: 1.45;
}

.dispatch-reason {
  color: var(--color-danger);
}

/* ── 材料捐献 ── */
.donate-section {
  margin-bottom: 16px;
}
.donate-list {
  display: flex;
  flex-direction: column;
  gap: 10px;
}
.donate-row {
  background: var(--color-bg);
  border-radius: 8px;
  padding: 10px 12px;
}
.donate-row-head {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 6px;
}
.donate-mat-icon {
  font-size: 1.1em;
}
.donate-mat-name {
  font-weight: 600;
  font-size: 0.9em;
}
.donate-mat-progress {
  margin-left: auto;
  font-size: 0.85em;
  color: var(--color-text-secondary);
}
.donate-mine {
  color: var(--color-primary);
  font-size: 0.9em;
}
.donate-form {
  display: flex;
  align-items: center;
  gap: 8px;
}
.donate-have {
  font-size: 0.8em;
  color: var(--color-text-secondary);
}
.donate-empty {
  font-size: 0.8em;
  color: var(--color-danger);
}
.donate-input {
  width: 70px;
  padding: 4px 8px;
  border: 1px solid var(--color-border);
  border-radius: 4px;
  background: var(--color-surface);
  color: var(--color-text);
  font-size: 0.85em;
}
.donate-full {
  font-size: 0.85em;
  color: var(--color-success);
}

.hospital-cost-row {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 8px 0;
}

.hospital-cost-label {
  font-size: 0.9em;
  color: var(--color-text);
}

.hospital-cost-input {
  width: 96px;
}

.hospital-hint {
  font-size: 0.9em;
  color: var(--color-text);
  margin: 4px 0;
}

.hospital-hint.subtle {
  opacity: 0.75;
  font-size: 0.85em;
}

/* ── 线索追查处 ── */
.clue-bureau-overview {
  margin: 10px 0 4px;
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background:
    linear-gradient(135deg, rgba(124, 58, 237, 0.08), rgba(59, 130, 246, 0.05));
}
.clue-empty,
.clue-all-cleared {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 0.85em;
  color: var(--color-text-secondary);
  margin: 0;
}
.clue-empty-icon {
  font-size: 1.15em;
  flex-shrink: 0;
}
.clue-stats {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-bottom: 10px;
}
.clue-stat-pill {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 0.82em;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
}
.clue-stat-pill strong {
  font-weight: 700;
}
.clue-stat-cap {
  color: var(--color-text-secondary);
  font-weight: 400;
}
.clue-stat-pill--active {
  color: #fff;
  background: linear-gradient(135deg, #7c3aed, #6366f1);
  border-color: transparent;
}
.clue-stat-icon {
  font-size: 0.95em;
}
.clue-target-list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.clue-target {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 10px;
  border-radius: 8px;
  background: var(--color-surface);
  border: 1px solid var(--color-border);
  transition: border-color 0.15s ease, transform 0.15s ease;
}
.clue-target--eligible {
  border-color: rgba(124, 58, 237, 0.4);
}
.clue-target-mark {
  font-size: 1.05em;
  flex-shrink: 0;
}
.clue-target-info {
  display: flex;
  flex-direction: column;
  gap: 1px;
  min-width: 0;
  flex: 1;
}
.clue-target-name {
  font-size: 0.88em;
  font-weight: 600;
  color: var(--color-text);
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.clue-target-dungeon {
  font-size: 0.76em;
  color: var(--color-text-secondary);
}
.clue-target-badge {
  flex-shrink: 0;
  padding: 2px 9px;
  border-radius: 999px;
  font-size: 0.74em;
  font-weight: 600;
}
.clue-target-badge--hot {
  color: #fff;
  background: linear-gradient(135deg, #7c3aed, #6366f1);
}
.clue-target-badge--cold {
  color: var(--color-text-secondary);
  background: var(--color-bg);
  border: 1px solid var(--color-border);
}

.clue-search-card {
  padding: 12px;
  border: 1px solid var(--color-border);
  border-radius: 10px;
  background:
    linear-gradient(135deg, rgba(124, 58, 237, 0.06), rgba(59, 130, 246, 0.04));
}
.clue-search-controls {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  margin: 10px 0;
}
.clue-search-summary {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 4px;
}
.clue-search-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 0.82em;
  color: var(--color-text);
  background: var(--color-surface);
  border: 1px solid var(--color-border);
}

/* ── 深渊拾荒队：出门队伍列表 ── */
.scavenge-party-section {
  margin: 12px 0;
}
.scavenge-party-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
  margin-top: 8px;
}
.scavenge-party-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  font-size: 0.88em;
}
.scavenge-party-item.mine {
  border-color: var(--color-primary);
  background: rgba(13, 110, 253, 0.08);
}
.scavenge-party-item .sp-name {
  font-weight: 600;
  color: var(--color-text);
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.scavenge-party-item .sp-mine-tag {
  color: var(--color-primary);
  font-weight: 400;
  font-size: 0.85em;
}
.scavenge-party-item .sp-hours {
  color: var(--color-text-secondary);
  flex-shrink: 0;
}
.scavenge-party-item .sp-remain {
  color: var(--color-primary);
  flex-shrink: 0;
}

.forge-active-materials {
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 0.85rem;
  line-height: 1.6;
}

/* ── 百艺阁：在阁学员与本地倒计时 ── */
.skill-school-occupants-section {
  margin: 12px 0 16px;
}
.skill-school-occupant-list {
  display: flex;
  flex-direction: column;
  gap: 7px;
  margin-top: 8px;
}
.skill-school-occupant {
  display: grid;
  grid-template-columns: minmax(0, 1fr) auto;
  align-items: center;
  gap: 12px;
  padding: 9px 12px;
  border: 1px solid var(--color-border, #30363d);
  border-radius: 8px;
  background: var(--color-bg, #0d1117);
}
.skill-school-occupant.mine {
  border-color: var(--color-primary, #58a6ff);
  background: rgba(13, 110, 253, 0.08);
}
.skill-school-occupant-main {
  display: flex;
  align-items: center;
  gap: 7px;
  min-width: 0;
  flex-wrap: wrap;
}
.skill-school-occupant-name {
  min-width: 0;
  overflow: hidden;
  color: var(--color-text, #e6edf3);
  font-weight: 650;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.skill-school-occupant-mine,
.skill-school-occupant-mode {
  padding: 2px 7px;
  border-radius: 999px;
  font-size: 0.75em;
}
.skill-school-occupant-mine {
  color: var(--color-primary, #58a6ff);
  background: rgba(13, 110, 253, 0.12);
}
.skill-school-occupant-mode {
  color: var(--color-warning, #d29922);
  background: rgba(210, 153, 34, 0.12);
}
.skill-school-occupant-lesson {
  flex-basis: 100%;
  min-width: 0;
  overflow-wrap: anywhere;
  color: var(--color-text-secondary, #8b949e);
  font-size: 0.82em;
}
.skill-school-occupant-timer {
  color: var(--color-primary, #58a6ff);
  font-size: 0.86em;
  font-variant-numeric: tabular-nums;
  font-weight: 650;
  white-space: nowrap;
}
.skill-school-occupant-timer.due {
  color: var(--color-warning, #d29922);
}

@media (max-width: 640px) {
  .skill-school-occupant {
    grid-template-columns: 1fr;
    gap: 6px;
  }

  .skill-school-occupant-timer {
    justify-self: start;
  }
}

/* ── 贡献者按钮 ── */
.btn-contributors {
  background: rgba(13, 110, 253, 0.12);
  color: var(--color-primary);
  border: 1px solid var(--color-primary);
  padding: 8px 16px;
  border-radius: 6px;
  cursor: pointer;
}
.btn-contributors:hover:not(:disabled) {
  background: rgba(13, 110, 253, 0.25);
}
.btn-contributors:disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

/* ── 移动端适配 ── */
@media (max-width: 640px) {
  .modal-box {
    padding: 16px;
    max-height: 92vh;
  }

  .detail-stats {
    gap: 8px;
  }

  /* iOS Safari 表单控件字号 <16px 聚焦时会强制放大页面 */
  .donate-input {
    width: 84px;
    font-size: 16px;
  }
}

.dining-section {
  margin-bottom: 12px;
}

.dining-hint {
  margin: 6px 0;
  font-size: 13px;
  color: var(--color-text-secondary);
}

.dining-hint.subtle {
  opacity: 0.85;
}

.dining-menu-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
  max-height: 240px;
  overflow-y: auto;
  margin: 8px 0;
}

.dining-menu-item {
  display: flex;
  gap: 8px;
  align-items: flex-start;
  padding: 10px 12px;
  border: 1px solid var(--color-border);
  border-radius: 8px;
  background: var(--color-bg);
  cursor: pointer;
}

.dining-menu-item.selected {
  border-color: #d97706;
  background: rgba(217, 119, 6, 0.08);
}

.dining-item-main {
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.dining-item-title {
  font-weight: 600;
}

.dining-item-meta {
  font-size: 12px;
  color: #b45309;
}

.dining-item-desc {
  font-size: 12px;
  color: var(--color-text-secondary);
}

/* 驻地设施详情：保持功能密度，同时建立清晰的设施主视觉。 */
.modal-mask {
  --detail-brass: #5b84b2;
  background: rgba(12, 10, 8, 0.56);
  animation: detail-mask-in 0.18s ease-out both;
}

/*
 * 自定义属性会保留未知函数的原始 token；旧 WebView 因此不能靠连续声明
 * 自动回退。只在浏览器确认支持 color-mix() 后覆盖实色基线。
 */
@supports (color: color-mix(in srgb, #000 50%, #fff)) {
  .modal-mask {
    --detail-brass: color-mix(in srgb, #5b84b2 74%, var(--color-primary));
  }
}

.modal-box.modal-lg {
  position: relative;
  overflow-x: hidden;
  max-width: 760px;
  padding: 16px;
  border-color: var(--color-border);
  border-color: color-mix(in srgb, var(--detail-brass) 28%, var(--color-border));
  border-radius: 12px;
  background:
    radial-gradient(circle at 94% 0%, rgba(91, 132, 178, 0.1), transparent 26%),
    var(--color-surface);
  background:
    radial-gradient(circle at 94% 0%, color-mix(in srgb, var(--detail-brass) 10%, transparent), transparent 26%),
    var(--color-surface);
  background-color: #161b22;
  background-color: var(--color-surface, #161b22);
  box-shadow: 0 26px 70px rgba(0, 0, 0, 0.3);
  box-shadow: inset 0 1px color-mix(in srgb, var(--color-text) 5%, transparent), 0 26px 70px rgba(0, 0, 0, 0.3);
  animation: detail-panel-in 0.28s cubic-bezier(0.2, 0.8, 0.2, 1) both;
}

.modal-box.modal-lg::after {
  content: '';
  position: absolute;
  top: 22px;
  bottom: 22px;
  left: 0;
  width: 4px;
  border-radius: 0 4px 4px 0;
  background: linear-gradient(180deg, var(--detail-brass), transparent);
  background: linear-gradient(180deg, var(--detail-brass), color-mix(in srgb, var(--detail-brass) 28%, transparent));
}

.modal-box.modal-lg::before {
  content: '';
  position: absolute;
  top: 0;
  left: 24px;
  right: 24px;
  height: 2px;
  background: linear-gradient(90deg, transparent, var(--detail-brass), transparent);
  box-shadow: 0 0 12px rgba(91, 132, 178, 0.38);
  box-shadow: 0 0 12px color-mix(in srgb, var(--detail-brass) 38%, transparent);
}

.detail-header {
  position: relative;
  min-height: 0;
  margin: 0 0 12px;
  padding: 12px 52px 12px 12px;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--detail-brass) 20%, var(--color-border));
  border-radius: 14px;
  background:
    linear-gradient(115deg, rgba(91, 132, 178, 0.1), var(--color-bg));
  background:
    linear-gradient(115deg, color-mix(in srgb, var(--detail-brass) 10%, var(--color-bg)), var(--color-bg));
}

.detail-header::after {
  content: none;
}

.detail-heading-copy { min-width: 0; }
.detail-kicker {
  display: block;
  margin-bottom: 5px;
  color: var(--detail-brass);
  font-size: 9px;
  font-weight: 800;
  letter-spacing: 0.14em;
}
.detail-close {
  position: absolute;
  top: 12px;
  right: 12px;
  display: grid;
  width: 34px;
  height: 34px;
  place-items: center;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--detail-brass) 22%, var(--color-border));
  border-radius: 9px;
  background: var(--color-surface);
  background: color-mix(in srgb, var(--detail-brass) 5%, var(--color-surface));
  color: var(--color-text-secondary);
  font-size: 21px;
  line-height: 1;
  cursor: pointer;
  transition: transform 0.16s ease, border-color 0.16s ease, color 0.16s ease;
}
.detail-close:hover {
  transform: rotate(5deg);
  border-color: var(--detail-brass);
  color: var(--detail-brass);
}

.detail-icon {
  display: grid;
  width: 44px;
  height: 44px;
  flex: none;
  place-items: center;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--detail-brass) 34%, var(--color-border));
  border-radius: 14px;
  background: var(--color-surface);
  background: color-mix(in srgb, var(--detail-brass) 8%, var(--color-surface));
  filter: drop-shadow(0 7px 10px rgba(91, 132, 178, 0.2));
  filter: drop-shadow(0 7px 10px color-mix(in srgb, var(--detail-brass) 20%, transparent));
  transition: transform 0.22s ease;
}

.detail-header:hover .detail-icon { transform: translateY(-2px) rotate(-2deg); }
.detail-name { color: var(--color-text); letter-spacing: 0.02em; }
.detail-status { border: 1px solid currentColor; border-radius: 999px; }
.detail-desc {
  padding: 11px 13px;
  border-left: 3px solid var(--detail-brass);
  border-left: 3px solid color-mix(in srgb, var(--detail-brass) 56%, var(--color-border));
  border-radius: 0 8px 8px 0;
  background: var(--color-bg);
  background: color-mix(in srgb, var(--detail-brass) 5%, var(--color-bg));
}

.donate-section,
.progress-section,
.helpers-section,
.usage-info-section,
.building-upgrade-section {
  position: relative;
  padding: 14px;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--detail-brass) 15%, var(--color-border));
  border-radius: 12px;
  background: var(--color-bg);
  background: linear-gradient(110deg, color-mix(in srgb, var(--detail-brass) 4%, var(--color-bg)), var(--color-bg));
}

.progress-section::before,
.helpers-section::before,
.usage-info-section::before {
  content: '';
  position: absolute;
  top: -1px;
  left: 14px;
  width: 34px;
  height: 2px;
  background: var(--detail-brass);
}

.detail-stats { grid-template-columns: repeat(4, minmax(0, 1fr)); }
.stat-item {
  min-width: 0;
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--detail-brass) 14%, var(--color-border));
  background: var(--color-bg);
  background: color-mix(in srgb, var(--detail-brass) 4%, var(--color-bg));
}
.stat-value { color: var(--color-text); overflow-wrap: anywhere; }
.section-title {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--color-text);
}
.section-title::before {
  content: '';
  width: 16px;
  height: 2px;
  background: var(--detail-brass);
}
.helper-item,
.building-upgrade-card,
.dining-menu-item {
  border: 1px solid var(--color-border);
  border: 1px solid color-mix(in srgb, var(--detail-brass) 14%, var(--color-border));
  background: var(--color-bg);
  background: color-mix(in srgb, var(--detail-brass) 4%, var(--color-bg));
}
.progress-fill-lg {
  background: linear-gradient(90deg, var(--detail-brass), var(--color-primary));
  background: linear-gradient(90deg, var(--detail-brass), color-mix(in srgb, var(--detail-brass) 56%, var(--color-primary)));
  background-size: 180% 100%;
  animation: detail-progress-flow 3s linear infinite;
}

.usage-dispatch-btn-block {
  position: relative;
  -webkit-appearance: none;
  appearance: none;
  min-height: 44px;
  margin-top: 14px;
  border: 1px solid #6f9bc5;
  border: 1px solid color-mix(in srgb, var(--detail-brass) 64%, var(--color-border));
  border-radius: 10px;
  background-color: #426f9b;
  background-image: linear-gradient(110deg, #426f9b, #6f9bc5);
  background:
    linear-gradient(110deg, color-mix(in srgb, var(--detail-brass) 86%, #29445f), #6f9bc5);
  color: #fffaf0;
  -webkit-text-fill-color: #fffaf0;
  font-size: 14px;
  font-weight: 800;
  letter-spacing: 0.05em;
  box-shadow: 0 7px 18px rgba(66, 111, 155, 0.24), inset 0 1px rgba(255, 255, 255, 0.2);
  box-shadow: 0 7px 18px color-mix(in srgb, var(--detail-brass) 24%, transparent), inset 0 1px rgba(255, 255, 255, 0.2);
  transition: transform 0.18s ease, box-shadow 0.18s ease, filter 0.18s ease;
}
.usage-dispatch-btn-block::before {
  content: '◆';
  margin-right: 8px;
  font-size: 9px;
}
.usage-dispatch-btn-block:hover:not(:disabled) {
  transform: translateY(-2px);
  filter: saturate(1.08) brightness(1.04);
  box-shadow: 0 10px 24px rgba(66, 111, 155, 0.32), inset 0 1px rgba(255, 255, 255, 0.24);
  box-shadow: 0 10px 24px color-mix(in srgb, var(--detail-brass) 32%, transparent), inset 0 1px rgba(255, 255, 255, 0.24);
}
.usage-dispatch-btn-block:active:not(:disabled) { transform: translateY(0) scale(0.99); }
.usage-dispatch-btn-block:disabled {
  border-color: var(--color-border);
  background-color: #e2e6ea;
  background-image: none;
  background: var(--color-bg);
  color: var(--color-text-secondary);
  -webkit-text-fill-color: var(--color-text-secondary);
  box-shadow: none;
}

.modal-box.modal-xl { max-width: min(1040px, calc(100vw - 32px)); }
.detail-name { overflow-wrap: anywhere; }
.helper-name, .active-adv-name { overflow-wrap: anywhere; }
.hospital-cost-row, .clue-search-controls {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 96px auto;
  align-items: center;
}
.hospital-cost-input { max-width: 100%; }
@media (max-width: 640px) {
  .hospital-cost-row, .clue-search-controls { grid-template-columns: minmax(0, 1fr) auto; }
  .hospital-cost-label { grid-column: 1 / -1; }
  .hospital-cost-input { width: 100%; }
}

@keyframes detail-mask-in { from { opacity: 0; } }
@keyframes detail-panel-in { from { opacity: 0; transform: translateY(14px) scale(0.985); } }
@keyframes detail-progress-flow { to { background-position: -180% 0; } }

@media (max-width: 640px) {
  .modal-mask { align-items: flex-end; padding: 0; }
  .modal-box.modal-lg {
    width: 100%;
    max-width: none;
    max-height: 94dvh;
    padding: 16px;
    border-right: 0;
    border-bottom: 0;
    border-left: 0;
    border-radius: 18px 18px 0 0;
    overscroll-behavior: contain;
  }
  .modal-box.modal-lg::after { display: none; }
  .modal-box.modal-lg::before { left: 38%; right: 38%; height: 4px; border-radius: 99px; }
  .detail-header { min-height: 0; margin: 0 0 12px; padding: 12px 48px 12px 12px; }
  .detail-header::after { display: none; }
  .detail-icon { width: 36px; height: 36px; font-size: 24px; }
  .detail-name { font-size: 1.12em; }
  .detail-kicker { letter-spacing: 0.08em; }
  .detail-close { top: 9px; right: 9px; min-height: 0; }
  .detail-stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .stat-item { padding: 9px; }
  .helper-item { align-items: flex-start; gap: 8px; }
  .helper-info { min-width: 0; flex-wrap: wrap; gap: 4px 8px; }
  .btn-recall { min-height: 38px; flex: none; }
  .donate-form { flex-wrap: wrap; }
  .usage-dispatch-btn-block { min-height: 36px; }
  .donate-section,
  .progress-section,
  .helpers-section,
  .usage-info-section,
  .building-upgrade-section { padding: 12px; }
}

@media (prefers-reduced-motion: reduce) {
  .modal-mask,
  .modal-box.modal-lg,
  .detail-icon,
  .progress-fill-lg { animation: none; transition: none; }
}

@media (max-width: 640px) {
  .modal-box.modal-lg { padding: 14px; }
  .detail-header { padding-left: 8px; padding-right: 52px; }
  .detail-heading-copy { min-width: 0; }
  .detail-kicker { font-size: 10px; overflow-wrap: anywhere; }
  .detail-close { width: 32px; height: 32px; top: 10px; right: 10px; }
  .forge-intent-grid, .cookware-grid { grid-template-columns: minmax(0, 1fr); }
  .forge-intent-option { grid-template-columns: auto minmax(0, 1fr); }
  .cookware-option { grid-template-columns: auto auto minmax(0, 1fr); }
  .assembly-layout-section { grid-template-columns: minmax(0, 1fr); }
  .assembly-slot { grid-template-columns: 24px minmax(0, 1fr); padding: 10px; font-size: 14px; }
  .assembly-slot-item { grid-column: 2; }
  .assembly-slot-actions { grid-column: 2; }
  .assembly-slot-name { overflow-wrap: anywhere; }
  .donate-row-head { flex-wrap: wrap; }
  .donate-form { gap: 8px; }
  .donate-have, .donate-empty { flex: 1 1 100%; }
  .modal-box > .detail-actions {
    position: static;
    margin-top: 16px;
  }
}
</style>
