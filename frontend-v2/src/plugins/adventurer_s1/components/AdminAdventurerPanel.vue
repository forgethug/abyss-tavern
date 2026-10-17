<template>
  <div class="admin-adv-panel">
    <button type="button" class="btn-secondary btn-xs" @click="toggleInactiveView">
      {{ showInactiveOnly ? '返回全部列表' : '筛选不活跃角色' }}
    </button>

    <div class="batch-actions">
      <button
        type="button"
        class="btn-secondary"
        :disabled="switchingAllToManual || saving"
        @click="switchAllPlayersToManual"
      >
        {{ switchingAllToManual ? '切换中…' : '全部玩家改为手动匹配' }}
      </button>
      <span class="batch-action-hint">作用于当前管理区服所有在册玩家，不受搜索、状态或分页限制。</span>
    </div>

    <!-- 搜索和过滤 -->
    <div class="filter-bar">
      <input
        v-model="search"
        type="text"
        placeholder="搜索冒险者名称…"
        @input="debouncedFetch"
      >
      <select v-model="statusFilter" @change="fetchList(1)">
        <option value="">全部状态</option>
        <option value="resting">休息中</option>
        <option value="in_party">组队中</option>
        <option value="exploring">探险中</option>
        <option value="activity">酒馆活动中</option>
        <option value="skill_school_queue">等候导师指点</option>
        <option value="building_queue">等候设施空位</option>
        <option value="building">建造中</option>
        <option value="dead">已死亡</option>
        <option value="archived">已封存</option>
      </select>
      <button type="button" class="btn-detect-alt" :disabled="detectingAlts" @click="detectAlts">
        {{ detectingAlts ? '检测中…' : '🔍 检测多号冒险者' }}
      </button>
      <span v-if="altDetectSummary" class="alt-summary">{{ altDetectSummary }}</span>
    </div>

    <p class="alt-detect-hint">扫描所有在册冒险者（非死亡/封存），按注册/登录 IP 关联分组，找出疑似同一人多号同时持有多个冒险者的情况。</p>

    <div v-if="sameAccountMulti.length > 0" class="same-account-panel">
      <div class="same-account-head">
        <span class="same-account-title">单账号多冒险者</span>
        <span class="same-account-count">{{ sameAccountMulti.length }} 个账号</span>
      </div>
      <div v-for="row in sameAccountMulti" :key="row.user_id" class="same-account-row">
        <span class="same-account-player">
          {{ row.nickname }}
          <span class="same-account-id">ID {{ row.user_id }} · {{ row.username }}</span>
        </span>
        <span class="same-account-advs">
          {{ row.active_adventurer_count }} 个在册冒险者：
          <strong v-for="(adv, idx) in row.adventurers" :key="adv.id">
            {{ Number(idx) > 0 ? '、' : '' }}{{ adv.name }}（{{ advStatusLabel(adv.status) }} Lv.{{ adv.level }}）
          </strong>
        </span>
      </div>
    </div>

    <div v-if="altGroups.length > 0" class="alt-filter-panel">
      <div class="alt-filter-row">
        <label class="alt-filter-label">多号关联组</label>
        <select v-model="selectedGroupId" class="alt-view-select">
          <option value="">全部关联组（{{ altGroups.length }}）</option>
          <option v-for="g in altGroups" :key="g.group_id" :value="String(g.group_id)">
            关联组 #{{ g.group_id }} · {{ g.link_summary }}
          </option>
        </select>
        <button type="button" class="btn-clear-alt" @click="clearAltDetect">清除结果</button>
      </div>
    </div>

    <div v-if="displayAltGroups.length > 0" class="alt-group-list">
      <section
        v-for="g in displayAltGroups"
        :key="g.group_id"
        class="alt-group-card"
      >
        <div class="alt-group-head">
          <span class="alt-group-title">关联组 #{{ g.group_id }}</span>
          <span class="alt-group-link">{{ g.link_summary }}</span>
          <span class="alt-group-count">{{ g.member_count }} 个账号 · {{ g.active_adventurer_count }} 个冒险者</span>
        </div>
        <div class="alt-group-members">
          <div v-for="m in g.members" :key="m.user_id" class="alt-member-row">
            <div class="alt-member-main">
              <span class="alt-member-player">
                {{ m.nickname }}
                <span v-if="m.is_frozen" class="alt-frozen-badge">已冻结</span>
                <span class="alt-member-id">ID {{ m.user_id }} · {{ m.username }}</span>
              </span>
              <span class="alt-member-adv">
                冒险者：
                <strong v-for="(adv, idx) in m.adventurers" :key="adv.id">
                  {{ Number(idx) > 0 ? '、' : '' }}{{ adv.name }}（{{ advStatusLabel(adv.status) }} Lv.{{ adv.level }}）
                </strong>
              </span>
              <span class="alt-member-meta">
                预约：{{ reservationStatusLabel(m.reservation_status) }}
                <template v-if="m.reservation_drawn"> · 已抽中</template>
              </span>
              <span class="alt-member-ip">注册 {{ m.register_ip || '-' }} · 登录 {{ m.last_login_ip || '-' }}</span>
            </div>
          </div>
        </div>
      </section>
    </div>

    <!-- 列表 -->
    <div v-if="loading" class="empty">加载中…</div>
    <div v-else-if="adventurers.length === 0" class="empty">暂无冒险者</div>
    <div v-else class="adv-table-wrap">
      <table class="adv-table">
        <thead>
          <tr>
            <th>ID</th>
            <th>名称</th>
            <th>等级</th>
            <th>状态</th>
            <th>HP</th>
            <th>属性</th>
            <th>金币</th>
            <th>当前贡献</th>
            <th>总贡献</th>
            <th>经验</th>
            <th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="a in adventurers" :key="a.id">
            <td data-label="ID">{{ a.id }}</td>
            <td data-label="名称">
              <span class="adv-name" @click="openEdit(a)">{{ a.name }}</span>
              <small v-if="a.character_class" class="adv-class">{{ a.character_class }}</small>
            </td>
            <td data-label="等级">Lv.{{ a.level }}</td>
            <td data-label="状态">
              <span :class="'status-tag st-' + a.status">{{ statusLabel(a.status, a.building_instance_status, a.building_handler_key) }}</span>
            </td>
            <td data-label="HP">{{ a.hp }}/{{ a.max_hp }}</td>
            <td class="attrs" data-label="属性">
              <span>💪{{ a.strength }}</span>
              <span>🏃{{ a.agility }}</span>
              <span>🧠{{ a.intelligence }}</span>
              <span>🍀{{ a.luck }}</span>
            </td>
            <td data-label="金币">{{ a.gold?.toFixed(2) }}</td>
            <td data-label="当前贡献">{{ a.contribution ?? 0 }}</td>
            <td data-label="总贡献">{{ a.contribution_total ?? 0 }}</td>
            <td data-label="经验">{{ a.exp }}</td>
            <td class="table-actions" data-label="操作">
              <button type="button" class="btn-secondary btn-xs" @click="openEdit(a)">编辑</button>
            </td>
          </tr>
        </tbody>
      </table>
    </div>

    <Pagination v-if="pagination" :pagination="pagination" @go="fetchList" />

    <!-- 编辑弹窗 -->
    <Teleport to="body">
      <div v-if="editTarget" class="modal-overlay s1-mobile-surface s1-mobile-overlay" @click.self="closeEdit">
        <div class="modal-content modal-lg s1-mobile-dialog">
          <div class="modal-header s1-mobile-dialog-header">
            <h5>编辑冒险者：{{ editForm.name || editTarget.name }} (ID: {{ editTarget.id }})</h5>
            <div class="modal-header-right">
              <label class="silent-toggle" title="开启后所有GM操作不显示在玩家最近动态中">
                <input v-model="gmSilentMode" type="checkbox"> 🫥 隐身模式
              </label>
              <button class="modal-close" @click="closeEdit">✕</button>
            </div>
          </div>
          <div class="modal-body s1-mobile-dialog-body">
            <div v-if="editorLoadingSections.length" class="editor-load-status" role="status">
              正在读取：{{ editorLoadingSections.map(section => EDITOR_SECTION_LABELS[section]).join('、') }}…
            </div>
            <div v-for="section in editorFailedSections" :key="section" class="editor-load-error" role="alert">
              <span>{{ EDITOR_SECTION_LABELS[section] }}加载失败：{{ editorLoadErrors[section] }}</span>
              <button type="button" class="btn-secondary btn-xs" @click="retryEditorSection(section)">
                重试{{ EDITOR_SECTION_LABELS[section] }}
              </button>
            </div>
            <template v-if="editorSectionReady('profile')">
            <div class="edit-grid">
              <div class="form-group">
                <label>名称</label>
                <input v-model="editForm.name" type="text" :maxlength="nameMaxLength">
                <small class="field-hint">仅中英文，最多 {{ nameMaxLength }} 字</small>
              </div>
              <div class="form-group">
                <label>性别</label>
                <select v-model="editForm.gender">
                  <option value="男">男</option>
                  <option value="女">女</option>
                  <option value="未知">未知</option>
                </select>
              </div>
              <div class="form-group">
                <label>职业</label>
                <input v-model="editForm.character_class" type="text" placeholder="无职业留空">
              </div>
              <div class="form-group">
                <label>等级</label>
                <input v-model.number="editForm.level" type="number" min="1">
              </div>
              <div class="form-group">
                <label>HP</label>
                <input v-model.number="editForm.hp" type="number" min="0">
              </div>
              <div class="form-group">
                <label>最大 HP</label>
                <input v-model.number="editForm.max_hp" type="number" min="1">
              </div>
              <div class="form-group">
                <label>力量</label>
                <input v-model.number="editForm.strength" type="number" min="0">
              </div>
              <div class="form-group">
                <label>敏捷</label>
                <input v-model.number="editForm.agility" type="number" min="0">
              </div>
              <div class="form-group">
                <label>智力</label>
                <input v-model.number="editForm.intelligence" type="number" min="0">
              </div>
              <div class="form-group">
                <label>幸运</label>
                <input v-model.number="editForm.luck" type="number" min="0">
              </div>
              <div class="form-group">
                <label>金币</label>
                <input v-model.number="editForm.gold" type="number" min="0" step="0.01">
              </div>
              <div class="form-group">
                <label>🏅 当前贡献</label>
                <input v-model.number="editForm.contribution" type="number" min="0" step="1">
              </div>
              <div class="form-group">
                <label>🏅 总贡献（累计）</label>
                <input v-model.number="editForm.contribution_total" type="number" min="0" step="1">
              </div>
              <div class="form-group">
                <label>经验</label>
                <input v-model.number="editForm.exp" type="number" min="0">
              </div>
              <div class="form-group">
                <label>精力</label>
                <input v-model.number="editForm.stamina" type="number" min="0" max="100">
              </div>
              <div class="form-group">
                <label>状态</label>
                <select v-model="editForm.status">
                  <option value="resting">休息中</option>
                  <option value="in_party">组队中</option>
                  <option value="exploring">探险中</option>
                  <option value="activity">酒馆活动中</option>
                  <option value="building">建造中</option>
                  <option value="dead">已死亡</option>
                </select>
              </div>
              <div class="form-group">
                <label>组队偏好</label>
                <select v-model="editForm.party_preference">
                  <option value="auto">自动</option>
                  <option value="manual">手动</option>
                  <option value="idle">空闲</option>
                  <option value="rest">休息</option>
                </select>
              </div>
              <div class="form-group">
                <label>性格</label>
                <input v-model="editForm.personality" type="text" placeholder="逗号分隔">
              </div>
              <div class="form-group full-width">
                <label>背景故事</label>
                <textarea v-model="editForm.history_story" rows="4" placeholder="冒险者背景故事"></textarea>
              </div>
            </div>

            <!-- 只读信息 -->
            <div class="readonly-info">
              <span>💰 金币: {{ editTarget.gold?.toFixed(2) }}</span>
              <span>⭐ 经验: {{ editTarget.exp }}</span>
              <span>🧑 所有者 ID: {{ editTarget.owner_id ?? '无' }}</span>
              <span>📅 创建: {{ editTarget.created_at }}</span>
            </div>

            <div v-if="editorSectionReady('fateReset')" class="sub-section fate-reset-admin">
              <div class="sub-header">
                <h6>🧭 命途重置</h6>
                <button
                  v-if="fateResetState?.journey && !fateResetState?.reset_request"
                  type="button"
                  class="btn-danger btn-xs"
                  :disabled="sendingFateReset"
                  @click="sendFateResetRequest"
                >{{ sendingFateReset ? '发送中…' : '发送重置确认' }}</button>
              </div>
              <div v-if="!fateResetState?.journey" class="sub-empty">该冒险者尚未开启命途</div>
              <template v-else>
                <div class="fate-reset-summary">
                  <span>当前路线：{{ fateResetState.journey.route_title || '尚未选择' }}</span>
                  <span>状态：{{ fateResetState.journey.status }}</span>
                  <span>关键经历：{{ fateResetState.journey.key_event_count ?? 0 }}</span>
                  <span>相关探险：{{ fateResetState.journey.related_expedition_count ?? 0 }}</span>
                </div>
                <div v-if="fateResetState.reset_request" class="fate-reset-pending">
                  已于 {{ fateResetState.reset_request.requested_at || '未知时间' }} 向玩家发送确认。
                  <span v-if="fateResetState.reset_request.reason">原因：{{ fateResetState.reset_request.reason }}</span>
                </div>
                <small class="field-hint">管理员不能直接清空命途。玩家接受后，系统才会清空命途进度与命途事件，并重新生成候选。</small>
              </template>
            </div>

            <!-- 特质管理 -->
            <div v-if="editorSectionReady('traits')" class="sub-section">
              <div class="sub-header">
                <h6>🎭 特质 ({{ traits.length }})</h6>
                <button type="button" class="btn-secondary btn-xs" @click="showAddTrait = !showAddTrait">
                  {{ showAddTrait ? '取消' : '+ 添加' }}
                </button>
              </div>
              <div v-if="showAddTrait" class="add-row">
                <input v-model="newTraitName" type="text" placeholder="特质名称" maxlength="100">
                <input v-model="newTraitDesc" type="text" placeholder="描述">
                <button type="button" class="btn-primary btn-xs" :disabled="!newTraitName.trim()" @click="addTrait">添加</button>
              </div>
              <div v-if="traits.length === 0" class="sub-empty">暂无特质</div>
              <div v-else class="trait-list">
                <div v-for="t in traits" :key="t.id ?? t.name" :class="['trait-item', { archived: t.is_archived }]">
                  <!-- 编辑模式 -->
                  <template v-if="editingTraitId === t.id">
                    <div class="edit-inline-form">
                      <div class="edit-inline-row">
                        <input v-model="editingTraitForm.name" type="text" placeholder="特质名称" maxlength="100" style="flex:1">
                        <input v-model="editingTraitForm.description" type="text" placeholder="描述" style="flex:2">
                      </div>
                    </div>
                    <div class="trait-actions">
                      <button type="button" class="btn-primary btn-xs" @click="saveEditTrait()">保存</button>
                      <button type="button" class="btn-secondary btn-xs" @click="cancelEditTrait">取消</button>
                    </div>
                  </template>
                  <!-- 只读模式 -->
                  <template v-else>
                    <div class="trait-info">
                      <span class="trait-name">{{ t.name }}</span>
                      <span class="trait-desc">{{ t.desc }}</span>
                      <span v-if="t.is_archived" class="archived-tag">已归档</span>
                    </div>
                    <div class="trait-actions">
                      <button type="button" class="btn-secondary btn-xs" @click="startEditTrait(t)">编辑</button>
                      <button
                        type="button"
                        class="btn-xs"
                        :class="t.is_archived ? 'btn-success' : 'btn-secondary'"
                        @click="toggleTraitArchive(t)"
                      >{{ t.is_archived ? '恢复' : '归档' }}</button>
                      <button type="button" class="btn-danger btn-xs" @click="deleteTrait(t)">删除</button>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <!-- 技能管理 -->
            <div v-if="editorSectionReady('skills')" class="sub-section">
              <div class="sub-header">
                <h6>⚔️ 技能 ({{ skills.length }})</h6>
                <button type="button" class="btn-secondary btn-xs" @click="showAddSkill = !showAddSkill">
                  {{ showAddSkill ? '取消' : '+ 添加' }}
                </button>
              </div>
              <div v-if="showAddSkill" class="add-row">
                <input v-model="newSkillName" type="text" placeholder="技能名称" maxlength="100">
                <input v-model="newSkillDesc" type="text" placeholder="描述">
                <button type="button" class="btn-primary btn-xs" :disabled="!newSkillName.trim()" @click="addSkill">添加</button>
              </div>
              <div v-if="skills.length === 0" class="sub-empty">暂无技能</div>
              <div v-else class="skill-list">
                <div v-for="s in skills" :key="s.id" :class="['skill-item', { unequipped: !s.equipped }]">
                  <!-- 编辑模式 -->
                  <template v-if="editingSkillId === s.id">
                    <div class="edit-inline-form">
                      <div class="edit-inline-row">
                        <input v-model="editingSkillForm.name" type="text" placeholder="技能名称" maxlength="100" style="flex:1">
                        <input v-model="editingSkillForm.description" type="text" placeholder="描述" style="flex:2">
                      </div>
                    </div>
                    <div class="skill-actions">
                      <button type="button" class="btn-primary btn-xs" @click="saveEditSkill()">保存</button>
                      <button type="button" class="btn-secondary btn-xs" @click="cancelEditSkill">取消</button>
                    </div>
                  </template>
                  <!-- 只读模式 -->
                  <template v-else>
                    <div class="skill-info">
                      <span class="skill-name">{{ s.name }}</span>
                      <span class="skill-desc">{{ s.description }}</span>
                      <span v-if="!s.equipped" class="unequipped-tag">未携带</span>
                    </div>
                    <div class="skill-actions">
                      <button type="button" class="btn-secondary btn-xs" @click="startEditSkill(s)">编辑</button>
                      <button
                        type="button"
                        class="btn-xs"
                        :class="s.equipped ? 'btn-secondary' : 'btn-success'"
                        @click="toggleSkillEquipped(s)"
                      >{{ s.equipped ? '卸下' : '携带' }}</button>
                      <button type="button" class="btn-danger btn-xs" @click="deleteSkill(s)">删除</button>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <!-- 装备（背包）管理 -->
            <div v-if="editorSectionReady('equipment')" class="sub-section">
              <div class="sub-header">
                <h6>🎒 随身装备 ({{ equipment.length }})</h6>
                <div class="sub-header-actions">
                <button type="button" class="btn-secondary btn-xs" @click="showAddEquipment = !showAddEquipment">
                  {{ showAddEquipment ? '取消' : '+ 添加' }}
                </button>
                <button type="button" class="btn-secondary btn-xs" @click="toggleCatalogBrowser('equipment')">
                  📚 图鉴
                </button>
                </div>
              </div>
              <!-- 图鉴浏览面板（装备） -->
              <div v-if="showCatalogBrowser === 'equipment'" class="catalog-browser">
                <div class="catalog-browser-filters">
                  <input v-model="catalogSearch" type="text" placeholder="搜索物品名…" @input="debouncedCatalogSearch">
                  <select v-model="catalogTypeFilter" @change="fetchCatalogBrowser">
                    <option value="">全部类型</option>
                    <option v-for="t in ITEM_TYPE_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
                  </select>
                  <select v-model="catalogRarityFilter" @change="fetchCatalogBrowser">
                    <option value="">全部稀有度</option>
                    <option value="common">普通</option>
                    <option value="rare">稀有</option>
                    <option value="epic">史诗</option>
                    <option value="legendary">传说</option>
                  </select>
                </div>
                <div v-if="catalogLoading" class="sub-empty">加载中…</div>
                <div v-else-if="catalogItems.length === 0" class="sub-empty">无匹配物品</div>
                <div v-else class="catalog-browser-list">
                  <div
                    v-for="c in catalogItems"
                    :key="c.id"
                    class="catalog-browser-item"
                    @click="addFromCatalog('equipment', c)"
                  >
                    <span class="cb-name">{{ c.name }}</span>
                    <span :class="'rarity-tag r-' + c.rarity">{{ rarityLabel(c.rarity) }}</span>
                    <span class="cb-type">{{ c.type }}</span>
                    <span v-if="c.description" class="cb-desc">{{ c.description }}</span>
                    <span v-if="c.world_unique" class="cb-unique">唯一</span>
                  </div>
                </div>
              </div>
              <div v-if="showAddEquipment" class="add-eq-row">
                <input
                  v-model="newEqName"
                  type="text"
                  placeholder="物品名称"
                  maxlength="100"
                  @input="onEqNameInput"
                >
                <select v-model="newEqRarity">
                  <option value="common">普通</option>
                  <option value="rare">稀有</option>
                  <option value="epic">史诗</option>
                  <option value="legendary">传说</option>
                </select>
                <input v-model="newEqType" type="text" placeholder="类型" maxlength="50" style="width:80px">
                <input v-model.number="newEqQuantity" type="number" placeholder="数量" min="1" style="width:70px">
                <input v-model.number="newEqUses" type="number" placeholder="使用次数" min="0" style="width:80px">
                <input
                  v-if="isCompanionType(newEqType)"
                  v-model.number="newEqEvolutionStage"
                  type="number"
                  placeholder="嬗变阶段"
                  min="0"
                  style="width:80px"
                >
                <input v-model="newEqDesc" type="text" placeholder="自定义描述（可选）" style="flex:2">
                <button type="button" class="btn-primary btn-xs" :disabled="!newEqName.trim()" @click="addEquipment">添加</button>
              </div>
              <!-- 图鉴搜索下拉 -->
              <div v-if="catalogResults.length > 0" class="catalog-dropdown">
                <div
                  v-for="c in catalogResults"
                  :key="c.id"
                  class="catalog-item"
                  @click="pickCatalogItem(c)"
                >
                  <span class="catalog-name">{{ c.name }}</span>
                  <span class="catalog-rarity">{{ rarityLabel(c.rarity) }}</span>
                  <span class="catalog-type">{{ c.type }}</span>
                </div>
              </div>
              <div v-if="equipment.length === 0" class="sub-empty">暂无装备</div>
              <div v-else class="eq-list">
                <div v-for="e in equipment" :key="e.id" class="eq-item">
                  <!-- 编辑模式 -->
                  <template v-if="editingEqId === e.id">
                    <div class="edit-inline-form">
                      <div class="edit-inline-row">
                        <input v-model="editingEqForm.name" type="text" placeholder="名称" maxlength="100">
                        <select v-model="editingEqForm.rarity">
                          <option value="common">普通</option>
                          <option value="rare">稀有</option>
                          <option value="epic">史诗</option>
                          <option value="legendary">传说</option>
                        </select>
                        <input v-model="editingEqForm.type" type="text" placeholder="类型" maxlength="50" style="width:80px">
                        <input v-model.number="editingEqForm.quantity" type="number" placeholder="数量" min="1" style="width:70px">
                        <input v-model.number="editingEqForm.uses_left" type="number" placeholder="使用次数" min="0" style="width:80px">
                        <input
                          v-if="isCompanionType(editingEqForm.type)"
                          v-model.number="editingEqForm.evolution_stage"
                          type="number"
                          placeholder="嬗变阶段"
                          min="0"
                          style="width:80px"
                        >
                      </div>
                      <div class="edit-inline-row">
                        <input v-model="editingEqForm.custom_desc" type="text" placeholder="自定义描述" style="flex:1">
                      </div>
                    </div>
                    <div class="eq-actions">
                      <button type="button" class="btn-primary btn-xs" @click="saveEditEquipment()">保存</button>
                      <button type="button" class="btn-secondary btn-xs" @click="cancelEditEquipment">取消</button>
                    </div>
                  </template>
                  <!-- 只读模式 -->
                  <template v-else>
                    <div class="eq-info">
                      <span class="eq-name">
                        {{ e.name }}
                      </span>
                      <span class="eq-meta">
                        <span :class="'rarity-tag r-' + e.rarity">{{ rarityLabel(e.rarity) }}</span>
                        <span v-if="e.type" class="eq-type">{{ e.type }}</span>
                        <span v-if="(e.quantity ?? 1) > 1" class="eq-uses">数量 {{ e.quantity }}</span>
                        <span v-if="e.uses_left != null" class="eq-uses">剩余 {{ e.uses_left }} 次</span>
                        <span v-if="isCompanionType(e.type)" class="eq-uses">{{ e.evolution_stage ?? 0 }} 阶</span>
                      </span>
                      <span v-if="e.custom_desc" class="eq-desc">{{ e.custom_desc }}</span>
                    </div>
                    <div class="eq-actions">
                      <button type="button" class="btn-secondary btn-xs" @click="startEditEquipment(e)">编辑</button>
                      <button type="button" class="btn-danger btn-xs" @click="deleteEquipment(e)">删除</button>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <!-- 仓库物品管理 -->
            <div v-if="editorSectionReady('warehouse')" class="sub-section">
              <div class="sub-header">
                <h6>📦 仓库物品 ({{ warehouse.length }})</h6>
                <div class="sub-header-actions">
                <button type="button" class="btn-secondary btn-xs" @click="showAddWarehouse = !showAddWarehouse">
                  {{ showAddWarehouse ? '取消' : '+ 添加' }}
                </button>
                <button type="button" class="btn-secondary btn-xs" @click="toggleCatalogBrowser('warehouse')">
                  📚 图鉴
                </button>
                </div>
              </div>
              <!-- 图鉴浏览面板（仓库） -->
              <div v-if="showCatalogBrowser === 'warehouse'" class="catalog-browser">
                <div class="catalog-browser-filters">
                  <input v-model="catalogSearch" type="text" placeholder="搜索物品名…" @input="debouncedCatalogSearch">
                  <select v-model="catalogTypeFilter" @change="fetchCatalogBrowser">
                    <option value="">全部类型</option>
                    <option v-for="t in ITEM_TYPE_OPTIONS" :key="t.value" :value="t.value">{{ t.label }}</option>
                  </select>
                  <select v-model="catalogRarityFilter" @change="fetchCatalogBrowser">
                    <option value="">全部稀有度</option>
                    <option value="common">普通</option>
                    <option value="rare">稀有</option>
                    <option value="epic">史诗</option>
                    <option value="legendary">传说</option>
                  </select>
                </div>
                <div v-if="catalogLoading" class="sub-empty">加载中…</div>
                <div v-else-if="catalogItems.length === 0" class="sub-empty">无匹配物品</div>
                <div v-else class="catalog-browser-list">
                  <div
                    v-for="c in catalogItems"
                    :key="c.id"
                    class="catalog-browser-item"
                    @click="addFromCatalog('warehouse', c)"
                  >
                    <span class="cb-name">{{ c.name }}</span>
                    <span :class="'rarity-tag r-' + c.rarity">{{ rarityLabel(c.rarity) }}</span>
                    <span class="cb-type">{{ c.type }}</span>
                    <span v-if="c.description" class="cb-desc">{{ c.description }}</span>
                    <span v-if="c.world_unique" class="cb-unique">唯一</span>
                  </div>
                </div>
              </div>
              <div v-if="showAddWarehouse" class="add-eq-row">
                <input
                  v-model="newWhName"
                  type="text"
                  placeholder="物品名称"
                  maxlength="100"
                  @input="onWhNameInput"
                >
                <select v-model="newWhRarity">
                  <option value="common">普通</option>
                  <option value="rare">稀有</option>
                  <option value="epic">史诗</option>
                  <option value="legendary">传说</option>
                </select>
                <input v-model="newWhType" type="text" placeholder="类型" maxlength="50" style="width:80px">
                <input v-model.number="newWhQuantity" type="number" placeholder="数量" min="1" style="width:70px">
                <input v-model.number="newWhUses" type="number" placeholder="使用次数" min="0" style="width:80px">
                <input v-model="newWhDesc" type="text" placeholder="自定义描述（可选）" style="flex:2">
                <button type="button" class="btn-primary btn-xs" :disabled="!newWhName.trim()" @click="addWarehouseItem">添加</button>
              </div>
              <!-- 图鉴搜索下拉（仓库） -->
              <div v-if="whCatalogResults.length > 0" class="catalog-dropdown">
                <div
                  v-for="c in whCatalogResults"
                  :key="c.id"
                  class="catalog-item"
                  @click="pickWhCatalogItem(c)"
                >
                  <span class="catalog-name">{{ c.name }}</span>
                  <span class="catalog-rarity">{{ rarityLabel(c.rarity) }}</span>
                  <span class="catalog-type">{{ c.type }}</span>
                </div>
              </div>
              <div v-if="warehouse.length === 0" class="sub-empty">仓库为空</div>
              <div v-else class="eq-list">
                <div v-for="w in warehouse" :key="w.id" class="eq-item">
                  <!-- 编辑模式 -->
                  <template v-if="editingWhId === w.id">
                    <div class="edit-inline-form">
                      <div class="edit-inline-row">
                        <input v-model="editingWhForm.name" type="text" placeholder="名称" maxlength="100">
                        <select v-model="editingWhForm.rarity">
                          <option value="common">普通</option>
                          <option value="rare">稀有</option>
                          <option value="epic">史诗</option>
                          <option value="legendary">传说</option>
                        </select>
                        <input v-model="editingWhForm.type" type="text" placeholder="类型" maxlength="50" style="width:80px">
                        <input v-model.number="editingWhForm.quantity" type="number" placeholder="数量" min="1" style="width:70px">
                        <input v-model.number="editingWhForm.uses_left" type="number" placeholder="使用次数" min="0" style="width:80px">
                      </div>
                      <div class="edit-inline-row">
                        <input v-model="editingWhForm.custom_desc" type="text" placeholder="自定义描述" style="flex:1">
                      </div>
                    </div>
                    <div class="eq-actions">
                      <button type="button" class="btn-primary btn-xs" @click="saveEditWarehouse()">保存</button>
                      <button type="button" class="btn-secondary btn-xs" @click="cancelEditWarehouse">取消</button>
                    </div>
                  </template>
                  <!-- 只读模式 -->
                  <template v-else>
                    <div class="eq-info">
                      <span class="eq-name">
                        {{ w.name }}
                      </span>
                      <span class="eq-meta">
                        <span :class="'rarity-tag r-' + w.rarity">{{ rarityLabel(w.rarity) }}</span>
                        <span v-if="w.type" class="eq-type">{{ w.type }}</span>
                        <span v-if="(w.quantity ?? 1) > 1" class="eq-uses">数量 {{ w.quantity }}</span>
                        <span v-if="w.uses_left != null" class="eq-uses">剩余 {{ w.uses_left }} 次</span>
                      </span>
                      <span v-if="w.custom_desc" class="eq-desc">{{ w.custom_desc }}</span>
                    </div>
                    <div class="eq-actions">
                      <button type="button" class="btn-secondary btn-xs" @click="startEditWarehouse(w)">编辑</button>
                      <button type="button" class="btn-danger btn-xs" @click="deleteWarehouseItem(w)">删除</button>
                    </div>
                  </template>
                </div>
              </div>
            </div>

            <!-- 隐藏剧情线（管理员只读，玩家不可见） -->
            <div v-if="editorSectionReady('storyThreads')" class="sub-section story-thread-section">
              <div class="sub-header">
                <h6>🕸️ 隐藏剧情线 ({{ storyThreads.length }})</h6>
                <span class="admin-only-hint">仅管理员可见</span>
              </div>
              <div v-if="storyThreads.length === 0" class="sub-empty">暂无隐藏剧情线</div>
              <div v-else class="story-thread-list">
                <div
                  v-for="st in storyThreads"
                  :key="st.id"
                  :class="['story-thread-item', 'st-thread-' + st.status]"
                >
                  <div class="story-thread-head">
                    <span class="story-thread-title">{{ st.title }}</span>
                    <span :class="['thread-status-tag', 'ts-' + st.status]">{{ storyThreadStatusLabel(st.status) }}</span>
                    <span class="story-thread-weight">权重 {{ st.weight }}</span>
                  </div>
                  <p class="story-thread-summary">{{ st.summary }}</p>
                  <div v-if="st.tags?.length" class="story-thread-tags">
                    <span v-for="tag in st.tags" :key="tag" class="thread-tag">{{ tag }}</span>
                  </div>
                  <div class="story-thread-meta">
                    <span v-if="st.source_log_id">来源日志 #{{ st.source_log_id }}</span>
                    <span v-if="st.last_touched_at">最近更新 {{ st.last_touched_at }}</span>
                  </div>
                </div>
              </div>
            </div>
            </template>
          </div>
          <div class="modal-footer s1-mobile-dialog-footer">
            <button
              v-if="editTarget && (
                (editTarget.status === 'building' && editTarget.building_instance_id)
                || editTarget.tea_house_seated
                || editTarget.hot_spring_seated
                || editTarget.tavern_seated
              )"
              type="button"
              class="btn-danger btn-xs"
              :disabled="evicting || !editorSectionReady('profile')"
              @click="confirmForceEvict"
            >
              {{ evicting ? '中断中…' : '🏠 强制返回宿舍' }}
            </button>
            <button
              v-if="editTarget && editTarget.status !== 'archived' && editTarget.status !== 'dead'"
              type="button"
              class="btn-danger archive-btn"
              :disabled="archiving || !editorSectionReady('profile')"
              @click="confirmArchive"
            >
              {{ archiving ? '封存中…' : '封存清退' }}
            </button>
            <button
              v-if="editTarget && editTarget.status === 'archived'"
              type="button"
              class="btn-success archive-btn"
              :disabled="unarchiving || !editorSectionReady('profile')"
              @click="confirmUnarchive"
            >
              {{ unarchiving ? '解封中…' : '解封回原宿舍' }}
            </button>
            <div class="modal-footer-spacer" />
            <button class="btn-secondary" @click="closeEdit">取消</button>
            <button class="btn-primary" :disabled="saving || !editorSectionReady('profile')" @click="saveEdit">
              {{ saving ? '保存中…' : '保存' }}
            </button>
          </div>
        </div>
      </div>
    </Teleport>
  </div>
</template>

<script setup lang="ts">
import { ref, watch, onMounted, onUnmounted, computed } from 'vue'
import { getS1AdminRealmTarget, type S1AdminRealmTarget } from '@/composables/useApi'
import { useAdventurerS1Store } from '../stores/adventurerS1'
import { useGlobalMessage } from '@/composables/useGlobalMessage'
import Pagination from '@/components/Pagination.vue'
import { ITEM_TYPE_OPTIONS } from '../utils'

const store = useAdventurerS1Store()
const message = useGlobalMessage()
const nameMaxLength = computed(() => store.options?.name_max_length ?? 7)

const loading = ref(false)
const search = ref('')
const statusFilter = ref('')
const adventurers = ref<any[]>([])
const pagination = ref<any>(null)
const showInactiveOnly = ref(false)
const archiving = ref(false)
const unarchiving = ref(false)
const evicting = ref(false)
const detectingAlts = ref(false)
const switchingAllToManual = ref(false)
const altGroups = ref<any[]>([])
const sameAccountMulti = ref<any[]>([])
const selectedGroupId = ref('')
let searchTimer: ReturnType<typeof setTimeout> | null = null

const altDetectSummary = computed(() => {
  if (altGroups.value.length === 0 && sameAccountMulti.value.length === 0) return ''
  const memberTotal = altGroups.value.reduce((sum, g) => sum + (g.member_count ?? 0), 0)
  const parts: string[] = []
  if (altGroups.value.length > 0) {
    parts.push(`${altGroups.value.length} 个关联组 · ${memberTotal} 个疑似多号账号`)
  }
  if (sameAccountMulti.value.length > 0) {
    parts.push(`${sameAccountMulti.value.length} 个单账号多冒险者`)
  }
  return parts.join('；')
})

const displayAltGroups = computed(() => {
  if (!selectedGroupId.value) return altGroups.value
  const gid = Number(selectedGroupId.value)
  return altGroups.value.filter((g) => g.group_id === gid)
})

function clearAltDetect() {
  altGroups.value = []
  sameAccountMulti.value = []
  selectedGroupId.value = ''
}

async function detectAlts() {
  detectingAlts.value = true
  try {
    const res = await store.detectAdventurerAlts(24)
    if (res.code === 200 && res.data) {
      altGroups.value = res.data.groups ?? []
      sameAccountMulti.value = res.data.same_account_multi ?? []
      selectedGroupId.value = ''
      const totalOwners = res.data.total_owners ?? 0
      const groupCount = res.data.group_count ?? 0
      const sameCount = sameAccountMulti.value.length
      if (groupCount === 0 && sameCount === 0) {
        message.success(`已扫描 ${totalOwners} 个在册冒险者账号，未发现多号或单号多冒险者`)
      } else {
        const parts: string[] = []
        if (groupCount > 0) {
          parts.push(`${groupCount} 个关联组（${res.data.flagged_count ?? 0} 个账号）`)
        }
        if (sameCount > 0) {
          parts.push(`${sameCount} 个单账号多冒险者`)
        }
        message.warning(`发现 ${parts.join('，')}`)
      }
    } else {
      message.error(res.message || '检测失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '检测失败')
  } finally {
    detectingAlts.value = false
  }
}

async function switchAllPlayersToManual() {
  if (switchingAllToManual.value || saving.value) return
  const realm = getS1AdminRealmTarget()
  const realmLabel = '当前酒馆'
  if (!window.confirm(
    `确定将${realmLabel}的全部自动匹配玩家切换为手动吗？\n\n`
    + '本操作覆盖所有在册玩家，不受当前列表筛选和分页限制。切换后停止后续自动报名；已有报名和正在进行的探险继续。玩家之后仍可自行开启自动匹配。',
  )) return
  switchingAllToManual.value = true
  try {
    const res = await store.adminSwitchAllPlayersToManual(realm)
    if (res.code !== 200 || !res.data) {
      message.error(res.message || '批量切换失败')
      return
    }
    const updatedCount = res.data.updated_count
    message.success(updatedCount > 0
      ? `已将${realmLabel}的 ${updatedCount} 名玩家冒险者切换为手动匹配`
      : `${realmLabel}没有需要切换的自动匹配玩家`)
    closeEdit()
    try {
      await fetchList(pagination.value?.page ?? 1)
    } catch {
      message.warning('切换已完成，列表刷新失败，请重新加载页面')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '批量切换失败')
  } finally {
    switchingAllToManual.value = false
  }
}

function advStatusLabel(status: string): string {
  const map: Record<string, string> = {
    resting: '休息中',
    in_party: '组队中',
    exploring: '探险中',
    activity: '酒馆活动中',
    skill_school_queue: '等候导师指点',
    building_queue: '等候设施空位',
    building: '建造中',
    dead: '已死亡',
    archived: '已封存',
  }
  return map[status] || status
}

function reservationStatusLabel(status: string | null | undefined): string {
  if (!status) return '无预约'
  const map: Record<string, string> = {
    pending: '等待中',
    drawn: '已抽中',
    rejected: '已拒绝',
    cancelled: '已取消',
  }
  return map[status] || status
}

const editTarget = ref<any>(null)
const fateResetState = ref<any>(null)
const sendingFateReset = ref(false)
const editForm = ref<Record<string, any>>({})
const editOriginal = ref<Record<string, any>>({})
const saving = ref(false)

// 特质/技能
const traits = ref<any[]>([])
const skills = ref<any[]>([])
const showAddTrait = ref(false)
const newTraitName = ref('')
const newTraitDesc = ref('')
const showAddSkill = ref(false)
const newSkillName = ref('')
const newSkillDesc = ref('')

// 装备/仓库
const equipment = ref<any[]>([])
const warehouse = ref<any[]>([])
const showAddEquipment = ref(false)
const newEqName = ref('')
const newEqRarity = ref('common')
const newEqType = ref('')
const newEqQuantity = ref(1)
const newEqUses = ref<number | null>(null)
const newEqEvolutionStage = ref(0)
const newEqDesc = ref('')
const gmSilentMode = ref(localStorage.getItem('s1_gm_silent') === 'true')
watch(gmSilentMode, (v) => localStorage.setItem('s1_gm_silent', String(v)))
const catalogResults = ref<any[]>([])
let eqCatalogTimer: ReturnType<typeof setTimeout> | null = null
const editingEqId = ref<number | null>(null)
const editingEqForm = ref<Record<string, any>>({})
const showAddWarehouse = ref(false)
const newWhName = ref('')
const newWhRarity = ref('common')
const newWhType = ref('')
const newWhQuantity = ref(1)
const newWhUses = ref<number | null>(null)
const newWhDesc = ref('')
const whCatalogResults = ref<any[]>([])
let whCatalogTimer: ReturnType<typeof setTimeout> | null = null
const editingWhId = ref<number | null>(null)
const editingWhForm = ref<Record<string, any>>({})

// 图鉴浏览面板（装备区 / 仓库区共用一套状态，同一时刻只开一个）
const showCatalogBrowser = ref<'equipment' | 'warehouse' | null>(null)
const catalogSearch = ref('')
const catalogTypeFilter = ref('')
const catalogRarityFilter = ref('')
const catalogItems = ref<any[]>([])
const catalogLoading = ref(false)
let catalogBrowserTimer: ReturnType<typeof setTimeout> | null = null

const editingSkillId = ref<number | null>(null)
const editingSkillForm = ref<Record<string, any>>({})
const editingTraitId = ref<number | null>(null)
const editingTraitForm = ref<Record<string, any>>({})

const storyThreads = ref<any[]>([])
const EDITOR_SECTION_LABELS = {
  profile: '基础资料',
  traits: '特质',
  skills: '技能',
  equipment: '随身装备',
  warehouse: '仓库物品',
  storyThreads: '隐藏剧情线',
  fateReset: '命途状态',
} as const
type EditorSection = keyof typeof EDITOR_SECTION_LABELS
type EditorLoadState = 'idle' | 'loading' | 'ready' | 'error'
const EDITOR_SECTIONS = Object.keys(EDITOR_SECTION_LABELS) as EditorSection[]
const editorLoadStates = ref<Partial<Record<EditorSection, EditorLoadState>>>({})
const editorLoadErrors = ref<Partial<Record<EditorSection, string>>>({})
const editorLoadingSections = computed(() => EDITOR_SECTIONS.filter(section => editorLoadStates.value[section] === 'loading'))
const editorFailedSections = computed(() => EDITOR_SECTIONS.filter(section => editorLoadStates.value[section] === 'error'))
const editorRealm = ref<S1AdminRealmTarget | null>(null)
let editorLoadGeneration = 0
// Keep one read in flight, including retries and rapid switches between editors.
let editorLoadTail: Promise<void> = Promise.resolve()
const editorLoaders: Record<EditorSection, (advId: number) => Promise<any>> = {
  profile: advId => store.adminGetAdventurer(advId),
  traits: advId => store.adminListTraits(advId),
  skills: advId => store.adminListSkills(advId),
  equipment: advId => store.adminListEquipment(advId),
  warehouse: advId => store.adminListWarehouse(advId),
  storyThreads: advId => store.adminListStoryThreads(advId),
  fateReset: advId => store.adminGetFateResetState(advId),
}

function editorSectionReady(section: EditorSection): boolean {
  return editorLoadStates.value[section] === 'ready'
}

function statusLabel(s: string, buildingInstanceStatus?: string | null, handlerKey?: string | null): string {
  if (s === 'building' && buildingInstanceStatus === 'built') {
    const handlerLabels: Record<string, string> = {
      healing_house: '治疗中', chapel: '祈祷中', blacksmith: '锻造中',
      tailor_workshop: '裁制中', alchemy_workshop: '炼金中', training: '修炼中',
      herbalist_shop: '购物中', armory_shop: '购物中', forge_supply_shop: '购物中', task_board: '执行任务',
      tavern_hall: '喝酒中', massage_parlor: '按摩中', foot_spa: '泡脚中',
      music_pavilion: '听曲中', sanatorium: '疗养中', dining_hall: '用餐中',
      skill_school: '研习中',
    }
    return handlerLabels[handlerKey ?? ''] ?? '工作中'
  }
  const map: Record<string, string> = {
    resting: '休息中', in_party: '组队中', exploring: '探险中', activity: '酒馆活动中',
    skill_school_queue: '等候导师指点',
    building_queue: '等候设施空位',
    building: '建造中', dead: '已死亡', idle: '空闲', archived: '已封存',
  }
  return map[s] || s
}

function storyThreadStatusLabel(status: string): string {
  const map: Record<string, string> = {
    active: '进行中',
    dormant: '休眠',
    resolved: '已收束',
  }
  return map[status] || status
}

async function fetchList(page = 1) {
  loading.value = true
  try {
    if (showInactiveOnly.value) {
      const res = await store.adminFetchInactiveAdventurers({ page, per_page: 20 })
      if (res.code === 200 && res.data) {
        adventurers.value = res.data.items ?? []
        pagination.value = { page: res.data.page, pages: res.data.pages, total: res.data.total }
      }
      return
    }
    const res = await store.adminFetchAdventurers({
      page,
      status: statusFilter.value || undefined,
      search: search.value || undefined,
    })
    if (res.code === 200 && res.data) {
      adventurers.value = res.data.items ?? []
      pagination.value = { page: res.data.page, pages: res.data.pages, total: res.data.total }
    }
  } finally {
    loading.value = false
  }
}

function debouncedFetch() {
  if (searchTimer) clearTimeout(searchTimer)
  searchTimer = setTimeout(() => fetchList(1), 300)
}

async function openEdit(adv: any) {
  closeEdit()
  editTarget.value = adv
  const realm = getS1AdminRealmTarget()
  const generation = editorLoadGeneration
  editorRealm.value = realm
  showAddTrait.value = false
  showAddSkill.value = false
  showAddEquipment.value = false
  showAddWarehouse.value = false
  let loaded = editorLoadTail
  for (const section of EDITOR_SECTIONS) {
    loaded = queueEditorSection(section, adv.id, generation, realm)
  }
  await loaded
}

function queueEditorSection(
  section: EditorSection,
  advId: number,
  generation: number,
  realm: S1AdminRealmTarget,
): Promise<void> {
  editorLoadStates.value[section] = 'loading'
  delete editorLoadErrors.value[section]
  const isCurrent = () => (
    generation === editorLoadGeneration
    && editTarget.value?.id === advId
    && editorRealm.value === realm
    && getS1AdminRealmTarget() === realm
  )
  const load = async () => {
    if (!isCurrent()) return
    try {
      const res = await editorLoaders[section](advId)
      if (!isCurrent()) return
      if (res?.code !== 200) throw new Error(res?.message || '读取失败')
      applyEditorSection(section, res.data, advId)
      editorLoadStates.value[section] = 'ready'
    } catch (error: unknown) {
      if (!isCurrent()) return
      const err = error as { response?: { data?: { message?: string } }; message?: string }
      editorLoadStates.value[section] = 'error'
      editorLoadErrors.value[section] = err?.response?.data?.message || err?.message || '读取失败，请重试'
    }
  }
  editorLoadTail = editorLoadTail.then(load, load)
  return editorLoadTail
}

function retryEditorSection(section: EditorSection): Promise<void> | undefined {
  if (!editTarget.value || !editorRealm.value || editorLoadStates.value[section] !== 'error') return
  return queueEditorSection(section, editTarget.value.id, editorLoadGeneration, editorRealm.value)
}

function applyEditorSection(section: EditorSection, data: any, advId: number) {
  if (section === 'profile') {
    if (!data || data.id !== advId) throw new Error('冒险者详情不完整或身份不匹配，请重试')
    const formData = {
      name: data.name,
      gender: data.gender,
      character_class: data.character_class ?? '',
      level: data.level,
      hp: data.hp,
      max_hp: data.max_hp,
      strength: data.strength,
      agility: data.agility,
      intelligence: data.intelligence,
      luck: data.luck,
      gold: data.gold,
      contribution: data.contribution ?? 0,
      contribution_total: data.contribution_total ?? 0,
      exp: data.exp,
      stamina: data.stamina,
      status: data.status,
      party_preference: data.party_preference,
      personality: Array.isArray(data.personality) ? data.personality.join(',') : (data.personality ?? ''),
      history_story: data.history_story ?? '',
    }
    editTarget.value = data
    editForm.value = { ...formData }
    editOriginal.value = { ...formData }
  } else if (section === 'fateReset') {
    fateResetState.value = data ?? null
  } else {
    const items = data ?? []
    if (!Array.isArray(items)) throw new Error('列表数据格式不完整，请重试')
    if (section === 'traits') traits.value = items
    if (section === 'skills') skills.value = items
    if (section === 'equipment') equipment.value = items
    if (section === 'warehouse') warehouse.value = items
    if (section === 'storyThreads') storyThreads.value = items
  }
}

function closeEdit() {
  editorLoadGeneration += 1
  editorRealm.value = null
  editorLoadStates.value = {}
  editorLoadErrors.value = {}
  editTarget.value = null
  editForm.value = {}
  editOriginal.value = {}
  traits.value = []
  skills.value = []
  equipment.value = []
  warehouse.value = []
  catalogResults.value = []
  whCatalogResults.value = []
  editingEqId.value = null
  editingEqForm.value = {}
  editingWhId.value = null
  editingWhForm.value = {}
  editingSkillId.value = null
  editingSkillForm.value = {}
  editingTraitId.value = null
  editingTraitForm.value = {}
  storyThreads.value = []
  fateResetState.value = null
  showCatalogBrowser.value = null
}

onUnmounted(closeEdit)

async function sendFateResetRequest() {
  if (!editTarget.value || sendingFateReset.value || !editorSectionReady('profile') || !editorSectionReady('fateReset')) return
  const reason = window.prompt(
    `向「${editTarget.value.name}」的绑定玩家发送命途重置确认？\n玩家接受后会清空命途进度与命途事件，并重新生成候选。\n\n请填写重置原因：`,
    '命途候选显示与最终选择不一致，需要重置后重新选择。',
  )
  if (reason === null) return
  sendingFateReset.value = true
  try {
    const res = await store.adminRequestFateReset(editTarget.value.id, reason.trim())
    if (res.code === 200) {
      fateResetState.value = {
        ...(fateResetState.value || {}),
        reset_request: res.data,
      }
      message.success('命途重置确认已发送给绑定玩家')
    } else {
      message.error(res.message || '发送命途重置确认失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '发送命途重置确认失败')
  } finally {
    sendingFateReset.value = false
  }
}

async function saveEdit() {
  if (!editTarget.value || saving.value || !editorSectionReady('profile')) return
  saving.value = true
  try {
    // 只发实际改动的字段，避免未变更字段触发副作用（如 stamina 时间戳重置）
    const payload: Record<string, any> = {}
    for (const key of Object.keys(editForm.value)) {
      if (editForm.value[key] !== editOriginal.value[key]) {
        payload[key] = editForm.value[key]
      }
    }
    if (Object.keys(payload).length === 0) {
      message.info('没有改动')
      saving.value = false
      return
    }
    if (payload.name !== undefined) {
      payload.name = String(payload.name).trim()
      if (!payload.name) {
        message.error('名称不能为空')
        saving.value = false
        return
      }
      if (payload.name.length > nameMaxLength.value) {
        message.error(`名称过长（最多 ${nameMaxLength.value} 个字）`)
        saving.value = false
        return
      }
      if (!/^[一-鿿a-zA-Z]+$/.test(payload.name)) {
        message.error('名称只能包含中文或英文字母')
        saving.value = false
        return
      }
    }
    if (gmSilentMode.value) payload.silent = true
    const res = await store.adminUpdateAdventurer(editTarget.value.id, payload)
    if (res.code === 200) {
      message.success(res.data?.message || '保存成功')
      closeEdit()
      fetchList(pagination.value?.page ?? 1)
    } else {
      message.error(res.message || '保存失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '保存失败')
  } finally {
    saving.value = false
  }
}

function toggleInactiveView() {
  showInactiveOnly.value = !showInactiveOnly.value
  fetchList(1)
}

async function confirmArchive() {
  if (!editTarget.value || archiving.value || !editorSectionReady('profile')) return
  const name = editTarget.value.name || `ID ${editTarget.value.id}`
  const reason = window.prompt(
    `确认封存清退「${name}」？\n档案盒数据会全部保留，但会释放床位并清出酒馆编制。\n\n可选：填写原因（留空亦可）`,
    '',
  )
  if (reason === null) return

  archiving.value = true
  try {
    const res = await store.adminArchiveAdventurer(editTarget.value.id, reason.trim() || undefined)
    if (res.code === 200) {
      message.success('封存清退成功，床位已释放')
      closeEdit()
      await fetchList(pagination.value?.page ?? 1)
    } else {
      message.error(res.message || '封存清退失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '封存清退失败')
  } finally {
    archiving.value = false
  }
}

async function confirmUnarchive() {
  if (!editTarget.value || unarchiving.value || !editorSectionReady('profile')) return
  const name = editTarget.value.name || `ID ${editTarget.value.id}`
  if (!window.confirm(`确认解封「${name}」并优先送回封存前的原宿舍床位？\n如果原床位不可用，将随机安排空床位；没有空床位则无法解封。`)) return

  unarchiving.value = true
  try {
    const res = await store.adminUnarchiveAdventurer(editTarget.value.id)
    if (res.code === 200) {
      message.success(res.data?.message || '已解封并回到原宿舍')
      closeEdit()
      await fetchList(pagination.value?.page ?? 1)
    } else {
      message.error(res.message || '解封失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '解封失败')
  } finally {
    unarchiving.value = false
  }
}

async function confirmForceEvict() {
  if (!editTarget.value || evicting.value || !editorSectionReady('profile')) return
  const name = editTarget.value.name || `ID ${editTarget.value.id}`
  if (!window.confirm(`确认强制将「${name}」从建筑中中断并送回宿舍？\n会正常结算建筑奖励。`)) return

  evicting.value = true
  try {
    const res = await store.adminForceEvictBuilding(editTarget.value.id)
    if (res.code === 200) {
      message.success('已强制返回宿舍')
      closeEdit()
      await fetchList(pagination.value?.page ?? 1)
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  } finally {
    evicting.value = false
  }
}

// ── 特质 CRUD ──────────────────────────────────────────────────────────
async function addTrait() {
  if (!editTarget.value || !newTraitName.value.trim()) return
  try {
    const res = await store.adminAddTrait(editTarget.value.id, newTraitName.value.trim(), newTraitDesc.value.trim())
    if (res.code === 200) {
      message.success(res.data?.message || '添加成功')
      newTraitName.value = ''
      newTraitDesc.value = ''
      showAddTrait.value = false
      const r = await store.adminListTraits(editTarget.value.id)
      if (r.code === 200) traits.value = r.data ?? []
    } else {
      message.error(res.message || '添加失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '添加失败')
  }
}

async function toggleTraitArchive(t: any) {
  if (!t.id) {
    message.error('特质缺少 id，无法归档（请刷新后重试）')
    return
  }
  try {
    const res = await store.adminUpdateTrait(t.id, { is_archived: !t.is_archived })
    if (res.code === 200) {
      message.success(res.data?.message || (t.is_archived ? '已恢复' : '已归档'))
      const r = await store.adminListTraits(editTarget.value.id)
      if (r.code === 200) traits.value = r.data ?? []
    } else {
      message.error(res.message || '操作失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '操作失败')
  }
}

async function deleteTrait(t: any) {
  if (!t.id) {
    message.error('特质缺少 id，无法删除（请刷新后重试）')
    return
  }
  if (!confirm(`确认删除特质【${t.name}】？`)) return
  try {
    const res = await store.adminDeleteTrait(t.id)
    if (res.code === 200) {
      message.success(res.data?.message || '删除成功')
      traits.value = traits.value.filter(x => x.id !== t.id)
    } else {
      message.error(res.message || '删除失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '删除失败')
  }
}

function startEditTrait(t: any) {
  if (!t.id) {
    message.error('特质缺少 id，无法编辑（请刷新后重试）')
    return
  }
  editingTraitId.value = t.id
  editingTraitForm.value = {
    name: t.name,
    description: t.desc || '',
  }
}

function cancelEditTrait() {
  editingTraitId.value = null
  editingTraitForm.value = {}
}

async function saveEditTrait() {
  if (!editingTraitId.value) return
  try {
    const res = await store.adminUpdateTrait(editingTraitId.value, editingTraitForm.value)
    if (res.code === 200) {
      message.success(res.data?.message || '更新成功')
      cancelEditTrait()
      const r = await store.adminListTraits(editTarget.value.id)
      if (r.code === 200) traits.value = r.data ?? []
    } else {
      message.error(res.message || '更新失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '更新失败')
  }
}

// ── 技能 CRUD ──────────────────────────────────────────────────────────
async function addSkill() {
  if (!editTarget.value || !newSkillName.value.trim()) return
  try {
    const res = await store.adminAddSkill(editTarget.value.id, newSkillName.value.trim(), newSkillDesc.value.trim())
    if (res.code === 200) {
      message.success(res.data?.message || '添加成功')
      newSkillName.value = ''
      newSkillDesc.value = ''
      showAddSkill.value = false
      const r = await store.adminListSkills(editTarget.value.id)
      if (r.code === 200) skills.value = r.data ?? []
    } else {
      message.error(res.message || '添加失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '添加失败')
  }
}

async function toggleSkillEquipped(s: any) {
  try {
    const res = await store.adminUpdateSkill(s.id, { equipped: !s.equipped })
    if (res.code === 200) {
      const r = await store.adminListSkills(editTarget.value.id)
      if (r.code === 200) skills.value = r.data ?? []
    }
  } catch { /* ignore */ }
}

async function deleteSkill(s: any) {
  if (!confirm(`确认删除技能【${s.name}】？`)) return
  try {
    const res = await store.adminDeleteSkill(s.id)
    if (res.code === 200) {
      message.success(res.data?.message || '删除成功')
      skills.value = skills.value.filter(x => x.id !== s.id)
    }
  } catch { /* ignore */ }
}

// ── 装备（背包）CRUD ────────────────────────────────────────────────────

function rarityLabel(r: string): string {
  const map: Record<string, string> = { common: '普通', rare: '稀有', epic: '史诗', legendary: '传说' }
  return map[r] || r
}

// ── 图鉴浏览面板 ──────────────────────────────────────────────────────────

function toggleCatalogBrowser(target: 'equipment' | 'warehouse') {
  if (showCatalogBrowser.value === target) {
    showCatalogBrowser.value = null
    return
  }
  showCatalogBrowser.value = target
  catalogSearch.value = ''
  catalogTypeFilter.value = ''
  catalogRarityFilter.value = ''
  fetchCatalogBrowser()
}

async function fetchCatalogBrowser() {
  catalogLoading.value = true
  try {
    const params: Record<string, string> = {}
    if (catalogSearch.value.trim()) params.search = catalogSearch.value.trim()
    if (catalogTypeFilter.value) params.type = catalogTypeFilter.value
    if (catalogRarityFilter.value) params.rarity = catalogRarityFilter.value
    const res = await store.adminSearchItemCatalog(params)
    if (res.code === 200) catalogItems.value = res.data ?? []
  } catch { catalogItems.value = [] }
  finally { catalogLoading.value = false }
}

function debouncedCatalogSearch() {
  if (catalogBrowserTimer) clearTimeout(catalogBrowserTimer)
  catalogBrowserTimer = setTimeout(fetchCatalogBrowser, 300)
}

async function addFromCatalog(target: 'equipment' | 'warehouse', item: any) {
  if (!editTarget.value) return
  const advId = editTarget.value.id
  try {
    const payload: Record<string, any> = {
      name: item.name,
      type: item.type || '',
      rarity: item.rarity || 'common',
      quantity: 1,
      custom_desc: item.description || undefined,
    }
    if (gmSilentMode.value) payload.silent = true
    const res = target === 'equipment'
      ? await store.adminAddEquipment(advId, payload)
      : await store.adminAddWarehouse(advId, payload)
    if (res.code === 200) {
      message.success(res.data?.message || '添加成功')
      if (target === 'equipment') {
        const r = await store.adminListEquipment(advId)
        if (r.code === 200) equipment.value = r.data ?? []
      } else {
        const r = await store.adminListWarehouse(advId)
        if (r.code === 200) warehouse.value = r.data ?? []
      }
    } else {
      message.error(res.message || '添加失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '添加失败')
  }
}

async function onEqNameInput() {
  if (eqCatalogTimer) clearTimeout(eqCatalogTimer)
  const q = newEqName.value.trim()
  if (q.length < 1) { catalogResults.value = []; return }
  eqCatalogTimer = setTimeout(async () => {
    try {
      const res = await store.adminSearchItemCatalog({ search: q })
      if (res.code === 200) catalogResults.value = res.data ?? []
    } catch { catalogResults.value = [] }
  }, 300)
}

function isCompanionType(type?: string | null) {
  return (type || '').trim().toLowerCase() === 'companion'
}

function pickCatalogItem(c: any) {
  newEqName.value = c.name
  newEqRarity.value = c.rarity || 'common'
  newEqType.value = c.type || ''
  if (isCompanionType(c.type)) {
    newEqEvolutionStage.value = 0
  }
  catalogResults.value = []
}

async function addEquipment() {
  if (!editTarget.value || !newEqName.value.trim()) return
  try {
    const payload: Record<string, any> = {
      name: newEqName.value.trim(),
      type: newEqType.value.trim(),
      rarity: newEqRarity.value,
      quantity: Math.max(1, Number(newEqQuantity.value || 1)),
      uses_left: newEqUses.value,
      custom_desc: newEqDesc.value.trim() || undefined,
    }
    if (isCompanionType(newEqType.value)) {
      payload.evolution_stage = Math.max(0, Number(newEqEvolutionStage.value || 0))
    }
    if (gmSilentMode.value) payload.silent = true
    const res = await store.adminAddEquipment(editTarget.value.id, payload)
    if (res.code === 200) {
      message.success(res.data?.message || '添加成功')
      newEqName.value = ''
      newEqType.value = ''
      newEqRarity.value = 'common'
      newEqQuantity.value = 1
      newEqUses.value = null
      newEqEvolutionStage.value = 0
      newEqDesc.value = ''
      showAddEquipment.value = false
      catalogResults.value = []
      const r = await store.adminListEquipment(editTarget.value.id)
      if (r.code === 200) equipment.value = r.data ?? []
    } else {
      message.error(res.message || '添加失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '添加失败')
  }
}

function startEditEquipment(e: any) {
  editingEqId.value = e.id
  editingEqForm.value = {
    name: e.name,
    type: e.type || '',
    rarity: e.rarity || 'common',
    quantity: e.quantity ?? 1,
    uses_left: e.uses_left,
    custom_desc: e.custom_desc || '',
    evolution_stage: e.evolution_stage ?? 0,
  }
}

function cancelEditEquipment() {
  editingEqId.value = null
  editingEqForm.value = {}
}

async function saveEditEquipment() {
  if (!editingEqId.value) return
  try {
    const payload: Record<string, any> = { ...editingEqForm.value }
    if (payload.uses_left !== null && payload.uses_left !== undefined) {
      payload.uses_left = Number(payload.uses_left)
    }
    payload.quantity = Math.max(1, Number(payload.quantity || 1))
    if (isCompanionType(payload.type)) {
      payload.evolution_stage = Math.max(0, Number(payload.evolution_stage ?? 0))
    } else {
      delete payload.evolution_stage
    }
    const res = await store.adminUpdateEquipment(editingEqId.value, payload)
    if (res.code === 200) {
      message.success(res.data?.message || '更新成功')
      cancelEditEquipment()
      const r = await store.adminListEquipment(editTarget.value.id)
      if (r.code === 200) equipment.value = r.data ?? []
    } else {
      message.error(res.message || '更新失败')
    }
  } catch (ex: any) {
    message.error(ex?.response?.data?.message || '更新失败')
  }
}

async function deleteEquipment(e: any) {
  if (!confirm(`确认删除装备【${e.name}】？`)) return
  try {
    const res = await store.adminDeleteEquipment(e.id)
    if (res.code === 200) {
      message.success(res.data?.message || '删除成功')
      equipment.value = equipment.value.filter(x => x.id !== e.id)
    }
  } catch { /* ignore */ }
}

// ── 仓库物品 CRUD ───────────────────────────────────────────────────────

async function onWhNameInput() {
  if (whCatalogTimer) clearTimeout(whCatalogTimer)
  const q = newWhName.value.trim()
  if (q.length < 1) { whCatalogResults.value = []; return }
  whCatalogTimer = setTimeout(async () => {
    try {
      const res = await store.adminSearchItemCatalog({ search: q })
      if (res.code === 200) whCatalogResults.value = res.data ?? []
    } catch { whCatalogResults.value = [] }
  }, 300)
}

function pickWhCatalogItem(c: any) {
  newWhName.value = c.name
  newWhRarity.value = c.rarity || 'common'
  newWhType.value = c.type || ''
  whCatalogResults.value = []
}

async function addWarehouseItem() {
  if (!editTarget.value || !newWhName.value.trim()) return
  try {
    const payload: Record<string, any> = {
      name: newWhName.value.trim(),
      type: newWhType.value.trim(),
      rarity: newWhRarity.value,
      quantity: Math.max(1, Number(newWhQuantity.value || 1)),
      uses_left: newWhUses.value,
      custom_desc: newWhDesc.value.trim() || undefined,
    }
    if (gmSilentMode.value) payload.silent = true
    const res = await store.adminAddWarehouse(editTarget.value.id, payload)
    if (res.code === 200) {
      message.success(res.data?.message || '添加成功')
      newWhName.value = ''
      newWhType.value = ''
      newWhRarity.value = 'common'
      newWhQuantity.value = 1
      newWhUses.value = null
      newWhDesc.value = ''
      showAddWarehouse.value = false
      whCatalogResults.value = []
      const r = await store.adminListWarehouse(editTarget.value.id)
      if (r.code === 200) warehouse.value = r.data ?? []
    } else {
      message.error(res.message || '添加失败')
    }
  } catch (e: any) {
    message.error(e?.response?.data?.message || '添加失败')
  }
}

async function deleteWarehouseItem(w: any) {
  if (!confirm(`确认删除仓库物品【${w.name}】？`)) return
  try {
    const res = await store.adminDeleteWarehouse(w.id)
    if (res.code === 200) {
      message.success(res.data?.message || '删除成功')
      warehouse.value = warehouse.value.filter(x => x.id !== w.id)
    }
  } catch { /* ignore */ }
}

function startEditWarehouse(w: any) {
  editingWhId.value = w.id
  editingWhForm.value = {
    name: w.name,
    type: w.type || '',
    rarity: w.rarity || 'common',
    quantity: w.quantity ?? 1,
    uses_left: w.uses_left,
    custom_desc: w.custom_desc || '',
  }
}

function cancelEditWarehouse() {
  editingWhId.value = null
  editingWhForm.value = {}
}

async function saveEditWarehouse() {
  if (!editingWhId.value) return
  try {
    const payload: Record<string, any> = { ...editingWhForm.value }
    if (payload.uses_left !== null && payload.uses_left !== undefined) {
      payload.uses_left = Number(payload.uses_left)
    }
    payload.quantity = Math.max(1, Number(payload.quantity || 1))
    const res = await store.adminUpdateWarehouse(editingWhId.value, payload)
    if (res.code === 200) {
      message.success(res.data?.message || '更新成功')
      cancelEditWarehouse()
      const r = await store.adminListWarehouse(editTarget.value.id)
      if (r.code === 200) warehouse.value = r.data ?? []
    } else {
      message.error(res.message || '更新失败')
    }
  } catch (ex: any) {
    message.error(ex?.response?.data?.message || '更新失败')
  }
}

// ── 技能编辑 ──────────────────────────────────────────────────────────

function startEditSkill(s: any) {
  editingSkillId.value = s.id
  editingSkillForm.value = {
    name: s.name,
    description: s.description || '',
  }
}

function cancelEditSkill() {
  editingSkillId.value = null
  editingSkillForm.value = {}
}

async function saveEditSkill() {
  if (!editingSkillId.value) return
  try {
    const res = await store.adminUpdateSkill(editingSkillId.value, editingSkillForm.value)
    if (res.code === 200) {
      message.success(res.data?.message || '更新成功')
      cancelEditSkill()
      const r = await store.adminListSkills(editTarget.value.id)
      if (r.code === 200) skills.value = r.data ?? []
    } else {
      message.error(res.message || '更新失败')
    }
  } catch (ex: any) {
    message.error(ex?.response?.data?.message || '更新失败')
  }
}

onMounted(() => {
  if (!store.options) void store.fetchCreationOptions()
  fetchList(1)
})
</script>

<style scoped>
.admin-adv-panel {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.batch-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}

.batch-action-hint {
  color: var(--color-text-secondary, #888);
  font-size: 12px;
}

.filter-bar {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.filter-bar input,
.filter-bar select {
  padding: 6px 10px;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.filter-bar input {
  flex: 1;
  min-width: 160px;
}

.empty {
  text-align: center;
  padding: 32px;
  color: var(--color-text-secondary, #888);
}

.adv-table-wrap {
  overflow-x: auto;
}

.adv-table {
  width: 100%;
  border-collapse: collapse;
  font-size: 13px;
}

.adv-table th,
.adv-table td {
  padding: 8px 10px;
  text-align: left;
  border-bottom: 1px solid var(--color-border);
  white-space: nowrap;
}

.adv-table th {
  font-weight: 600;
  font-size: 12px;
  color: var(--color-text-secondary, #888);
  position: sticky;
  top: 0;
  background: var(--color-bg);
}

.adv-name {
  color: var(--color-primary);
  cursor: pointer;
  font-weight: 600;
}

.adv-name:hover {
  text-decoration: underline;
}

.adv-class {
  margin-left: 4px;
  color: var(--color-text-secondary, #888);
  font-size: 11px;
}

.status-tag {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 10px;
  font-size: 11px;
  font-weight: 600;
}

.st-resting { background: #d4edda; color: #155724; }
.st-in_party { background: #cce5ff; color: #004085; }
.st-exploring { background: #fff3cd; color: #856404; }
.st-building { background: #e2d5f1; color: #4a235a; }
.st-dead { background: #f8d7da; color: #721c24; }
.st-idle { background: #d4edda; color: #155724; }
.st-archived { background: #e9ecef; color: #495057; }

.attrs {
  display: flex;
  gap: 6px;
  font-size: 12px;
}

.btn-xs {
  padding: 3px 8px;
  font-size: 11px;
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
}

.btn-secondary:hover:not(:disabled) {
  border-color: var(--color-primary);
  color: var(--color-primary);
}

.btn-primary {
  padding: 6px 16px;
  font-size: 13px;
  font-weight: 600;
  color: #fff;
  background: var(--color-primary);
  border: none;
  border-radius: 6px;
  cursor: pointer;
}

.btn-primary:disabled {
  opacity: 0.6;
  cursor: not-allowed;
}

/* 编辑弹窗 */
.modal-overlay {
  position: fixed;
  inset: 0;
  background: rgba(0, 0, 0, 0.5);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 1000;
}

.modal-content {
  background: var(--color-surface, var(--color-bg));
  border-radius: 12px;
  width: 90%;
  max-width: 640px;
  max-height: 85vh;
  overflow-y: auto;
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
}

.modal-header h5 {
  margin: 0;
  font-size: 16px;
}

.modal-close {
  background: none;
  border: none;
  font-size: 20px;
  cursor: pointer;
  color: var(--color-text-secondary, #888);
}

.modal-body {
  padding: 16px 20px;
}

.modal-footer {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 12px 20px;
  border-top: 1px solid var(--color-border);
}

.modal-footer-spacer {
  flex: 1;
}

.archive-btn {
  margin-right: auto;
}

.edit-grid {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 12px;
}

.editor-load-status,
.editor-load-error {
  margin-bottom: 12px;
  font-size: 13px;
  line-height: 1.5;
}

.editor-load-status {
  color: var(--color-text-secondary, #888);
}

.editor-load-error {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 10px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  color: var(--color-danger, #dc3545);
}

.form-group {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.form-group.full-width {
  grid-column: 1 / -1;
}

.form-group label {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text-secondary, #888);
}

.field-hint {
  font-size: 11px;
  color: var(--color-text-secondary, #888);
}

.form-group input,
.form-group select,
.form-group textarea {
  padding: 6px 10px;
  font-size: 13px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.form-group textarea {
  resize: vertical;
  font-family: inherit;
}

.readonly-info {
  display: flex;
  flex-wrap: wrap;
  gap: 12px;
  margin-top: 12px;
  padding-top: 12px;
  border-top: 1px dashed var(--color-border);
  font-size: 12px;
  color: var(--color-text-secondary, #888);
}

/* 特质/技能子区块 */
.sub-section {
  margin-top: 16px;
  padding-top: 12px;
  border-top: 1px solid var(--color-border);
}

.sub-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 8px;
}

.sub-header h6 {
  margin: 0;
  font-size: 14px;
  font-weight: 600;
}

.sub-empty {
  font-size: 12px;
  color: var(--color-text-secondary, #888);
  padding: 8px 0;
}

.add-row {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
}

.add-row input {
  flex: 1;
  padding: 5px 8px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.trait-list,
.skill-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.trait-item,
.skill-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  font-size: 12px;
}

.trait-item.archived {
  opacity: 0.6;
  border-style: dashed;
}

.skill-item.unequipped {
  opacity: 0.6;
  border-style: dashed;
}

.trait-info,
.skill-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.trait-name,
.skill-name {
  font-weight: 600;
  font-size: 13px;
}

.trait-desc,
.skill-desc {
  color: var(--color-text-secondary, #888);
  word-break: break-word;
}

.archived-tag,
.unequipped-tag {
  display: inline-block;
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
  background: #f0f0f0;
  color: #888;
  width: fit-content;
}

.trait-actions,
.skill-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

.btn-xs {
  padding: 3px 8px;
  font-size: 11px;
}

.btn-success {
  padding: 3px 8px;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  background: #28a745;
  border: 1px solid #28a745;
  border-radius: 6px;
  cursor: pointer;
}

.btn-success:hover:not(:disabled) {
  background: #218838;
}

.btn-danger {
  padding: 3px 8px;
  font-size: 11px;
  font-weight: 600;
  color: #fff;
  background: #dc3545;
  border: 1px solid #dc3545;
  border-radius: 6px;
  cursor: pointer;
}

.btn-danger:hover:not(:disabled) {
  background: #bb2d3b;
}

/* 装备/仓库子区块 */
.add-eq-row {
  display: flex;
  gap: 6px;
  margin-bottom: 8px;
  flex-wrap: wrap;
  align-items: center;
}

.add-eq-row input,
.add-eq-row select {
  padding: 5px 8px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.add-eq-row input {
  flex: 1;
  min-width: 100px;
}

.catalog-dropdown {
  border: 1px solid var(--color-border);
  border-radius: 6px;
  max-height: 150px;
  overflow-y: auto;
  margin-bottom: 8px;
  background: var(--color-surface, var(--color-bg));
}

.catalog-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.1s;
}

.catalog-item:hover {
  background: var(--color-bg);
}

.catalog-name {
  font-weight: 600;
}

.catalog-rarity {
  font-size: 10px;
  padding: 1px 6px;
  border-radius: 8px;
  background: #f0f0f0;
  color: #888;
}

.catalog-type {
  color: var(--color-text-secondary, #888);
  font-size: 11px;
}

/* 图鉴浏览面板 */
.sub-header-actions {
  display: flex;
  gap: 4px;
}

.catalog-browser {
  border: 1px solid var(--color-border);
  border-radius: 8px;
  margin-bottom: 10px;
  background: var(--color-surface, var(--color-bg));
  overflow: hidden;
}

.catalog-browser-filters {
  display: flex;
  gap: 6px;
  padding: 8px 10px;
  border-bottom: 1px solid var(--color-border);
  flex-wrap: wrap;
  align-items: center;
}

.catalog-browser-filters input {
  flex: 1;
  min-width: 120px;
  padding: 5px 8px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.catalog-browser-filters select {
  padding: 5px 8px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.catalog-browser-list {
  max-height: 250px;
  overflow-y: auto;
}

.catalog-browser-item {
  display: flex;
  gap: 8px;
  align-items: center;
  padding: 6px 10px;
  font-size: 12px;
  cursor: pointer;
  transition: background 0.1s;
  border-bottom: 1px solid var(--color-border, #eee);
}

.catalog-browser-item:last-child {
  border-bottom: none;
}

.catalog-browser-item:hover {
  background: var(--color-bg);
}

.cb-name {
  font-weight: 600;
  flex-shrink: 0;
}

.cb-type {
  color: var(--color-text-secondary, #888);
  font-size: 11px;
  flex-shrink: 0;
}

.cb-desc {
  color: var(--color-text-secondary, #888);
  font-size: 11px;
  flex: 1;
  min-width: 0;
  word-break: break-word;
}

.cb-unique {
  font-size: 10px;
  padding: 1px 5px;
  border-radius: 8px;
  background: #ff9800;
  color: #fff;
  flex-shrink: 0;
}

.eq-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.eq-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  padding: 8px 10px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 6px;
  font-size: 12px;
}

.eq-info {
  display: flex;
  flex-direction: column;
  gap: 2px;
  min-width: 0;
  flex: 1;
}

.eq-name {
  font-weight: 600;
  font-size: 13px;
}

.eq-meta {
  display: flex;
  gap: 6px;
  align-items: center;
  font-size: 11px;
}

.rarity-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 8px;
  font-size: 10px;
  font-weight: 600;
}

.r-common { background: #e9ecef; color: #6c757d; }
.r-uncommon { background: #d4edda; color: #155724; }
.r-rare { background: #cce5ff; color: #004085; }
.r-epic { background: #e2d5f1; color: #4a235a; }

.eq-type {
  color: var(--color-text-secondary, #888);
}

.eq-uses {
  color: var(--color-text-secondary, #888);
}

.eq-desc {
  color: var(--color-text-secondary, #888);
  word-break: break-word;
}

.eq-actions {
  display: flex;
  gap: 4px;
  flex-shrink: 0;
}

/* 内联编辑表单 */
.edit-inline-form {
  display: flex;
  flex-direction: column;
  gap: 6px;
  flex: 1;
  min-width: 0;
}

.edit-inline-row {
  display: flex;
  gap: 6px;
  align-items: center;
}

.edit-inline-row input[type="text"],
.edit-inline-row input[type="number"],
.edit-inline-row select {
  padding: 4px 8px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.edit-inline-row input[type="text"] {
  flex: 1;
  min-width: 60px;
}

.edit-inline-check {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  white-space: nowrap;
  cursor: pointer;
}

.story-thread-section {
  border-top-style: dashed;
}

.admin-only-hint {
  font-size: 11px;
  color: var(--color-text-secondary, #888);
}

.story-thread-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.story-thread-item {
  padding: 10px 12px;
  background: var(--color-bg);
  border: 1px solid var(--color-border);
  border-radius: 8px;
  font-size: 12px;
}

.story-thread-item.st-thread-dormant {
  opacity: 0.85;
  border-style: dashed;
}

.story-thread-item.st-thread-resolved {
  opacity: 0.7;
}

.story-thread-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.story-thread-title {
  font-weight: 700;
  font-size: 13px;
}

.thread-status-tag {
  display: inline-block;
  padding: 1px 8px;
  border-radius: 10px;
  font-size: 10px;
  font-weight: 600;
}

.ts-active { background: #cce5ff; color: #004085; }
.ts-dormant { background: #fff3cd; color: #856404; }
.ts-resolved { background: #e9ecef; color: #495057; }

.story-thread-weight {
  margin-left: auto;
  font-size: 11px;
  color: var(--color-text-secondary, #888);
}

.story-thread-summary {
  margin: 0 0 8px;
  line-height: 1.5;
  white-space: pre-wrap;
  word-break: break-word;
}

.story-thread-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
  margin-bottom: 6px;
}

.thread-tag {
  display: inline-block;
  padding: 1px 6px;
  border-radius: 8px;
  font-size: 10px;
  background: rgba(128, 128, 128, 0.15);
  color: var(--color-text-secondary, #888);
}

.story-thread-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  font-size: 11px;
  color: var(--color-text-secondary, #888);
}

.silent-toggle {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  font-size: 12px;
  color: var(--color-text-secondary);
  white-space: nowrap;
  cursor: pointer;
  user-select: none;
}

.silent-toggle input[type="checkbox"] {
  margin: 0;
}

.modal-header-right {
  display: flex;
  align-items: center;
  gap: 12px;
}

.btn-detect-alt {
  padding: 6px 14px;
  font-size: 12px;
  font-weight: 600;
  color: var(--color-text);
  background: color-mix(in srgb, var(--color-warning) 12%, var(--color-surface));
  border: 1px solid var(--color-warning);
  border-radius: 6px;
  cursor: pointer;
  white-space: nowrap;

  &:disabled {
    opacity: 0.6;
    cursor: not-allowed;
  }
}

.alt-summary {
  font-size: 12px;
  font-weight: 600;
  color: var(--color-warning);
}

.alt-detect-hint {
  margin: 0 0 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
  line-height: 1.5;
}

.same-account-panel {
  margin-bottom: 14px;
  border: 1px solid var(--color-danger);
  border-radius: 8px;
  overflow: hidden;
  background: color-mix(in srgb, var(--color-danger) 5%, var(--color-bg));
}

.same-account-head {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  border-bottom: 1px dashed var(--color-danger);
  background: color-mix(in srgb, var(--color-danger) 8%, var(--color-surface));
}

.same-account-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
}

.same-account-count {
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-danger);
}

.same-account-row {
  display: flex;
  flex-direction: column;
  gap: 4px;
  padding: 10px 12px;
  border-bottom: 1px solid var(--color-border);
  font-size: 12px;
}

.same-account-row:last-child {
  border-bottom: none;
}

.same-account-player {
  color: var(--color-text);
  font-weight: 600;
}

.same-account-id {
  margin-left: 6px;
  font-weight: 400;
  color: var(--color-text-secondary);
}

.same-account-advs {
  color: var(--color-text-secondary);
}

.alt-filter-panel {
  margin-bottom: 14px;
  padding: 12px;
  border: 1px solid var(--color-warning);
  border-radius: 8px;
  background: color-mix(in srgb, var(--color-warning) 6%, var(--color-bg));
}

.alt-filter-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 10px;
}

.alt-filter-label {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
  white-space: nowrap;
}

.alt-view-select {
  width: auto;
  min-width: 220px;
  flex: none;
  padding: 6px 10px;
  font-size: 12px;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  background: var(--color-bg);
  color: var(--color-text);
}

.btn-clear-alt {
  padding: 6px 12px;
  font-size: 12px;
  color: var(--color-text-secondary);
  background: transparent;
  border: 1px solid var(--color-border);
  border-radius: 6px;
  cursor: pointer;
}

.btn-clear-alt:hover {
  border-color: var(--color-text-secondary);
}

.alt-group-list {
  display: flex;
  flex-direction: column;
  gap: 14px;
  margin-bottom: 16px;
}

.alt-group-card {
  border: 1px solid var(--color-warning);
  border-radius: 10px;
  overflow: hidden;
  background: var(--color-bg);
}

.alt-group-head {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
  padding: 10px 12px;
  background: color-mix(in srgb, var(--color-warning) 10%, var(--color-surface));
  border-bottom: 1px dashed var(--color-warning);
}

.alt-group-title {
  font-size: 13px;
  font-weight: 700;
  color: var(--color-text);
}

.alt-group-link {
  font-size: 12px;
  color: var(--color-text-secondary);
}

.alt-group-count {
  margin-left: auto;
  font-size: 11px;
  font-weight: 600;
  color: var(--color-warning);
}

.alt-group-members {
  display: flex;
  flex-direction: column;
}

.alt-member-row {
  padding: 10px 12px;
  border-bottom: 1px solid var(--color-border);
}

.alt-member-row:last-child {
  border-bottom: none;
}

.alt-member-main {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 12px;
}

.alt-member-player {
  color: var(--color-text);
  font-weight: 600;
}

.alt-member-id {
  margin-left: 6px;
  font-weight: 400;
  color: var(--color-text-secondary);
}

.alt-frozen-badge {
  margin-left: 6px;
  padding: 1px 6px;
  border-radius: 8px;
  font-size: 10px;
  font-weight: 600;
  color: var(--color-danger);
  border: 1px solid var(--color-danger);
}

.alt-member-adv,
.alt-member-meta,
.alt-member-ip {
  color: var(--color-text-secondary);
}
.fate-reset-admin { border-color: color-mix(in srgb, var(--color-warning, #d29922) 42%, var(--color-border, #30363d)); }
.fate-reset-summary { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 8px; }
.fate-reset-summary span { padding: 4px 8px; border-radius: 7px; background: var(--color-bg, #0d1117); color: var(--color-text-secondary, #8b949e); font-size: 12px; }
.fate-reset-pending { display: grid; gap: 4px; margin-bottom: 8px; padding: 9px 10px; border-radius: 8px; background: color-mix(in srgb, var(--color-warning, #d29922) 12%, var(--color-bg, #0d1117)); color: var(--color-warning, #d29922); font-size: 12px; }

@media (max-width: 640px) {

  .filter-bar {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
  }

  .filter-bar > * {
    min-width: 0;
    width: 100%;
    min-height: 40px;
    box-sizing: border-box;
  }

  .filter-bar input,
  .filter-bar button,
  .filter-bar .alt-summary {
    grid-column: 1 / -1;
  }

  .adv-table-wrap {
    overflow: visible;
  }

  .adv-table,
  .adv-table tbody {
    display: block;
  }

  .adv-table thead {
    display: none;
  }

  .adv-table tr {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 10px;
    padding: 12px;
    border: 1px solid var(--color-border);
    border-radius: 10px;
    background: var(--color-surface, var(--color-bg));
  }

  .adv-table td {
    display: grid;
    gap: 4px;
    min-width: 0;
    padding: 0;
    border: 0;
    white-space: normal;
    overflow-wrap: anywhere;
  }

  .adv-table td::before {
    content: attr(data-label);
    font-size: 11px;
    font-weight: 600;
    color: var(--color-text-secondary, #888);
  }

  .adv-table .attrs,
  .adv-table .table-actions {
    grid-column: 1 / -1;
  }

  .adv-table .attrs {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 12px;
  }

  .adv-table .attrs::before {
    flex-basis: 100%;
  }

  .adv-table .table-actions {
    grid-template-columns: 1fr;
  }

  .adv-table .table-actions button {
    width: 100%;
    min-height: 40px;
  }

  .modal-overlay {
    align-items: flex-end;
    padding: 8px;
  }

  .modal-content,
  .modal-lg {
    width: 100%;
    max-width: none;
    max-height: calc(100dvh - 16px);
    border-radius: 16px 16px 10px 10px;
  }

  .modal-header {
    align-items: flex-start;
    padding: 12px;
  }

  .modal-body {
    padding: 12px;
  }

  .modal-footer {
    flex-wrap: wrap;
    padding: 10px 12px 12px;
  }

  .modal-footer-spacer {
    display: none;
  }

  .modal-footer button {
    flex: 1 1 calc(50% - 4px);
    min-height: 40px;
    margin: 0;
  }

  .edit-grid {
    grid-template-columns: minmax(0, 1fr);
    gap: 10px;
  }

  .form-group.full-width {
    grid-column: auto;
  }

  .add-row,
  .add-eq-row {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 8px;
  }

  .add-row input,
  .add-row select,
  .add-row button,
  .add-eq-row input,
  .add-eq-row select,
  .add-eq-row button {
    width: 100%;
    min-width: 0;
    min-height: 38px;
    box-sizing: border-box;
  }

  .trait-item,
  .skill-item,
  .equipment-item {
    align-items: flex-start;
    flex-wrap: wrap;
  }

  .trait-item button,
  .skill-item button,
  .equipment-item button {
    min-height: 36px;
  }

  .alt-filter-select {
    width: 100%;
    min-width: 0;
  }
}
</style>
