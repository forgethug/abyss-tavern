<template>
  <div class="world-outline-admin">
    <div class="world-admin-actions">
      <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="refreshAll">刷新</button>
      <button v-if="!readonly" type="button" class="btn-primary btn-sm" :disabled="maintaining" @click="submitMaintain">
        {{ maintaining ? '提交中…' : '提交大纲维护' }}
      </button>
    </div>

    <p class="admin-hint">
      世界情报库按上帝视角维护地牢生态、地区状态、势力实体与跨副本联动；条目有总量、scope 和关键词上限，优先维护/归档，不无限新建。DM 接力便签由异步大纲维护按副本合并后注入后续探险。
    </p>

    <nav class="workspace-tabs" aria-label="世界情报管理分区">
      <button
        v-for="workspace in WORLD_WORKSPACES"
        :key="workspace.id"
        type="button"
        :class="['workspace-tab', { active: activeWorkspace === workspace.id }]"
        @click="activeWorkspace = workspace.id"
      >
        <span class="workspace-icon" aria-hidden="true">{{ workspace.icon }}</span>
        <span><strong>{{ workspace.label }}</strong><small>{{ workspace.description }}</small></span>
        <span v-if="workspace.count !== undefined" class="workspace-count">{{ workspace.count }}</span>
      </button>
    </nav>

    <div class="workspace-heading">
      <div>
        <span class="workspace-kicker">WORLD INTELLIGENCE</span>
        <h6>{{ currentWorkspace.label }}</h6>
        <p>{{ currentWorkspace.detail }}</p>
      </div>
    </div>

    <nav v-if="activeWorkspace === 'pipeline'" class="pipeline-tabs" aria-label="维护队列类型">
      <button type="button" :class="['pipeline-tab', { active: activePipelineView === 'handoffs' }]" @click="activePipelineView = 'handoffs'">
        <span>📝 DM 接力便签</span><strong>{{ handoffTotal }}</strong>
      </button>
      <button type="button" :class="['pipeline-tab', { active: activePipelineView === 'candidates' }]" @click="activePipelineView = 'candidates'">
        <span>📥 世界事实候选</span><strong>{{ total }}</strong>
      </button>
    </nav>

    <section v-show="activeWorkspace === 'intel'" class="section-block">
      <h6>世界情报库</h6>
      <div class="toolbar compact-toolbar">
        <input v-model="intelKeyword" class="filter-input" placeholder="标题/摘要关键词" @keydown.enter="loadIntel(1)">
        <select v-model="intelTypeFilter" class="filter-select" @change="loadIntel(1)">
          <option value="">全部类型</option>
          <option v-for="type in intelTypes" :key="type" :value="type">{{ type }}</option>
        </select>
        <select v-model="intelStatusFilter" class="filter-select" @change="loadIntel(1)">
          <option value="">全部状态</option>
          <option v-for="status in intelStatuses" :key="status" :value="status">{{ status }}</option>
        </select>
        <button type="button" class="btn-secondary btn-sm" :disabled="intelLoading" @click="loadIntel(1)">刷新情报</button>
        <button v-if="!readonly" type="button" class="btn-primary btn-sm" @click="startCreateIntel">新建情报</button>
      </div>
      <div class="intel-meter">当前 {{ intelTotal }} / {{ intelMaxTotal || '?' }} 条。scope 最多 8 个，关键词最多 10 个；近义实体请编辑旧条目。</div>

      <section v-if="!readonly" class="intel-generator">
        <div class="generator-heading">
          <div>
            <strong>AI 起草世界情报</strong>
            <small>输入一条概述，生成可编辑草稿；不会自动保存。</small>
          </div>
          <button type="button" class="btn-secondary btn-sm" @click="generatorOpen = !generatorOpen">
            {{ generatorOpen ? '收起' : '展开' }}
          </button>
        </div>
        <div v-if="generatorOpen" class="generator-body">
          <label>内容概述
            <textarea
              v-model="generatorBrief"
              rows="4"
              maxlength="4000"
              placeholder="描述要生成的势力、NPC、地点或事件；需要关联已有势力时请写出其准确名称。"
            ></textarea>
          </label>
          <div class="form-grid">
            <label>期望类型
              <select v-model="generatorType">
                <option value="">由 AI 判断</option>
                <option v-for="type in intelTypes" :key="`gen-${type}`" :value="type">{{ type }}</option>
              </select>
            </label>
            <div class="dungeon-picker-field">
              <span>初始出没/影响地牢</span>
              <div class="dungeon-picker-actions">
                <button
                  type="button"
                  class="dungeon-option dungeon-option-all"
                  :class="{ active: generatorAllDungeons }"
                  :aria-pressed="generatorAllDungeons"
                  @click="toggleGeneratorAllDungeons"
                >全部地牢</button>
                <small v-if="!generatorAllDungeons">已选 {{ generatorDungeons.length }} / 8</small>
                <small v-else>包含后续新增地牢</small>
              </div>
              <div v-if="generatorDungeonsLoading" class="dungeon-picker-empty">正在加载地牢列表…</div>
              <div v-else-if="generatorDungeonOptions.length" class="dungeon-option-grid">
                <button
                  v-for="dungeon in generatorDungeonOptions"
                  :key="`gen-dungeon-${dungeon}`"
                  type="button"
                  class="dungeon-option"
                  :class="{ active: generatorDungeons.includes(dungeon) }"
                  :aria-pressed="generatorDungeons.includes(dungeon)"
                  :disabled="generatorAllDungeons"
                  @click="toggleGeneratorDungeon(dungeon)"
                >{{ dungeon }}</button>
              </div>
              <div v-else class="dungeon-picker-empty">地牢列表加载失败，请收起后重新展开。</div>
            </div>
          </div>
          <p class="admin-hint generator-hint">
            多选范围最多 8 个；选择“全部地牢”后，该情报可在任意当前或后续新增地牢被召回，但不代表每次探险都会出场。
          </p>
          <div class="form-actions">
            <button type="button" class="btn-primary btn-sm" :disabled="generatingIntel || !generatorBrief.trim()" @click="generateIntelDraft">
              {{ generatingIntel ? '生成中…' : '生成草稿' }}
            </button>
            <button type="button" class="btn-secondary btn-sm" :disabled="generatingIntel" @click="clearGenerator">清空</button>
          </div>
        </div>
      </section>

      <form v-if="editingIntel && !readonly" class="intel-form" @submit.prevent="saveIntel">
        <div class="form-grid">
          <label>标题<input v-model="intelForm.title" required></label>
          <label>类型
            <select v-model="intelForm.thread_type">
              <option v-for="type in intelTypes" :key="type" :value="type">{{ type }}</option>
            </select>
          </label>
          <label>状态
            <select v-model="intelForm.status">
              <option v-for="status in intelStatuses" :key="status" :value="status">{{ status }}</option>
            </select>
          </label>
          <label>权重<input v-model.number="intelForm.weight" type="number" min="1" max="100"></label>
        </div>
        <label>主地城（精确名称）<input v-model="intelForm.base_dungeon" placeholder="用于后续同地城队伍稳定召回"></label>
        <label>scope_keys（逗号分隔，最多 8 个）<input v-model="intelForm.scope_keys_text" placeholder="副本名, 地区名, 势力名"></label>
        <label>trigger_keywords（逗号分隔，最多 10 个）<input v-model="intelForm.trigger_keywords_text" placeholder="势力名, 实体名, 抽象机制词"></label>
        <label>摘要<textarea v-model="intelForm.summary" required rows="4"></textarea></label>
        <label>后续提示<textarea v-model="intelForm.next_reveal_hint" rows="2"></textarea></label>
        <label>连续性边界<textarea v-model="intelForm.power_profile" rows="2"></textarea></label>
        <div class="form-actions">
          <button type="submit" class="btn-primary btn-sm" :disabled="savingIntel">{{ savingIntel ? '保存中…' : '保存情报' }}</button>
          <button type="button" class="btn-secondary btn-sm" @click="cancelEditIntel">取消</button>
        </div>
      </form>

      <div v-if="intelLoading" class="empty">加载情报中…</div>
      <div v-else-if="intelRows.length === 0" class="empty">暂无情报</div>
      <div v-else class="intel-list">
        <article v-for="row in intelRows" :key="row.id" class="intel-card">
          <header>
            <strong>#{{ row.id }} {{ row.title }}</strong>
            <div class="header-actions">
              <span class="status-tag">{{ row.thread_type }}</span>
              <span :class="['status-tag', row.status]">{{ row.status }}</span>
              <span v-if="row.campaign_role" class="status-tag">共同战役 · {{ row.campaign_role }}</span>
              <span class="muted">权重 {{ row.weight }}</span>
              <button v-if="!readonly && !row.campaign_role" type="button" class="btn-secondary btn-xs" @click="startEditIntel(row)">编辑</button>
              <button v-if="!readonly && !row.campaign_role" type="button" class="btn-danger btn-xs" :disabled="deletingIntelId === row.id" @click="deleteIntel(row)">删除</button>
              <button v-if="!readonly && row.campaign_role" type="button" class="btn-secondary btn-xs" @click="activeWorkspace = 'campaigns'">转到共同战役</button>
            </div>
          </header>
          <p>{{ row.summary }}</p>
          <p v-if="row.base_dungeon" class="muted">主地城：{{ row.base_dungeon }}</p>
          <div class="tag-row"><span v-for="tag in row.scope_keys" :key="`s-${row.id}-${tag}`">scope: {{ tag }}</span></div>
          <div class="tag-row"><span v-for="tag in row.trigger_keywords" :key="`k-${row.id}-${tag}`">key: {{ tag }}</span></div>
          <p v-if="row.next_reveal_hint" class="muted">后续：{{ row.next_reveal_hint }}</p>
          <p v-if="row.power_profile" class="muted">连续性：{{ row.power_profile }}</p>
        </article>
      </div>
      <div v-if="intelTotal > intelPerPage" class="pager-row">
        <button type="button" class="btn-secondary btn-sm" :disabled="intelPage <= 1 || intelLoading" @click="loadIntel(intelPage - 1)">上一页</button>
        <span>第 {{ intelPage }} 页 / 共 {{ intelTotal }} 条</span>
        <button type="button" class="btn-secondary btn-sm" :disabled="intelPage * intelPerPage >= intelTotal || intelLoading" @click="loadIntel(intelPage + 1)">下一页</button>
      </div>
    </section>

    <section v-show="activeWorkspace === 'campaigns'" class="section-block campaign-section">
      <div class="campaign-heading">
        <div>
          <h6>共同战役主轴</h6>
          <p class="admin-hint">把一个共同世界事件拆成酒馆总目标、势力前线和副本前线。这里仅创建并编组现有世界线，不替代各副本自身目标，也不复用剧情链的阶段键。</p>
        </div>
        <button type="button" class="btn-secondary btn-sm" :disabled="campaignLoading || campaignMetaLoading" @click="loadCampaignWorkspace">
          {{ campaignLoading || campaignMetaLoading ? '刷新中…' : '刷新战役' }}
        </button>
      </div>

      <form v-if="!readonly" class="campaign-form" @submit.prevent="previewCampaign">
        <div class="form-actions">
          <button v-for="preset in campaignPresets" :key="preset.key" type="button" class="btn-secondary btn-sm"
            :disabled="campaignPresetLoading || campaignExecuting" @click="loadCampaignPreset(preset.key)">
            载入{{ preset.title }}
          </button>
        </div>
        <label class="campaign-checkbox"><input v-model="campaignForm.isolated_world" type="checkbox" @change="changeCampaignWorldMode">独立活动世界：全新临时副本</label>
        <p v-if="campaignForm.isolated_world" class="admin-hint">活动公开后即可在议事厅报名。填写全新地图名、背景与难度，旧副本履历、地点、势力和剧情记忆不带入；角色当前能力与装备保留，活动内新记录可持续继承。地图名创建后固定，活动结束后保留记录并关闭入口。</p>
        <label v-if="campaignForm.isolated_world">同时行动小队上限
          <input v-model.number="campaignForm.max_active_teams" type="number" min="1" :max="campaignMaxActiveTeams" required>
        </label>
        <p v-if="campaignForm.isolated_world" class="admin-hint">不同现场可同时行动，每个现场由一队占用至战报发布。超过一队时须配置阶段路线；转段会等待本阶段仍在行动的小队。</p>
        <label class="campaign-checkbox"><input v-model="campaignForm.dispatch_enabled" type="checkbox" :disabled="campaignForm.isolated_world">通过战役议事厅推进</label>
        <p v-if="!campaignForm.isolated_world" class="admin-hint">启用后先等待队伍在地图中发现，再发布小队行动。多队可报名，每轮只由实际触发的队伍推进，成败沿现有世界线演进。</p>
        <label v-if="campaignForm.dispatch_enabled" class="campaign-checkbox"><input v-model="campaignForm.staged" type="checkbox">按阶段推进</label>
        <p v-if="campaignForm.dispatch_enabled && campaignForm.staged" class="admin-hint">以下总纲与前线内容是后台完整设定。请在「阶段路线」中另写各阶段的公开名称与本地揭示内容，探险不会直接读取这份完整路线。</p>
        <div class="form-grid">
          <label>稳定战役键
            <input v-model.trim="campaignForm.campaign_key" required maxlength="64" pattern="[A-Za-z0-9][A-Za-z0-9._-]{2,63}" placeholder="例如 ancient_weapon_race">
          </label>
          <label>战役标题
            <input v-model.trim="campaignForm.title" required placeholder="例如：终焉兵器争夺战">
          </label>
        </div>
        <label>共同世界事实
          <textarea v-model.trim="campaignForm.summary" required rows="3" placeholder="只写已经由管理员确立、所有成员共同继承的起始事实。"></textarea>
        </label>
        <label>深渊酒馆目标
          <textarea v-model.trim="campaignForm.agenda" required rows="2" placeholder="酒馆为何调查、争夺、封印或利用这件事。"></textarea>
        </label>
        <label>力量与连续性边界
          <textarea v-model.trim="campaignForm.power_profile" required rows="2" placeholder="明确上限、已知能力、不能被剧情随意突破的边界。"></textarea>
        </label>

        <fieldset v-if="!campaignForm.isolated_world" class="campaign-fieldset">
          <legend>关联副本</legend>
          <p class="admin-hint">后续每条前线只能从这里选择精确副本作用域。</p>
          <div v-if="campaignMetaLoading" class="dungeon-picker-empty">正在加载副本与势力档案…</div>
          <div v-else-if="campaignDungeonOptions.length" class="dungeon-option-grid campaign-option-grid">
            <button
              v-for="dungeon in campaignDungeonOptions"
              :key="'campaign-dungeon-' + dungeon"
              type="button"
              class="dungeon-option"
              :class="{ active: campaignForm.related_dungeons.includes(dungeon) }"
              :aria-pressed="campaignForm.related_dungeons.includes(dungeon)"
              @click="toggleCampaignRelatedDungeon(dungeon)"
            >{{ dungeon }}</button>
          </div>
          <div v-else class="dungeon-picker-empty">未取得可用副本，请刷新元数据后再创建。</div>
        </fieldset>

        <fieldset v-if="!campaignForm.isolated_world" class="campaign-fieldset">
          <div class="campaign-fieldset-heading">
            <strong class="campaign-legend">势力前线（{{ campaignForm.dispatch_enabled ? '可选' : '至少 1 条' }}）</strong>
            <button type="button" class="btn-secondary btn-sm" @click="addCampaignFactionFront">添加势力前线</button>
          </div>
          <article v-for="(front, index) in campaignForm.faction_fronts" :key="'faction-front-' + index" class="campaign-front-card">
            <header>
              <strong>势力前线 {{ index + 1 }}</strong>
              <button
                type="button"
                class="btn-danger btn-xs"
                :disabled="!campaignForm.dispatch_enabled && campaignForm.faction_fronts.length <= 1"
                @click="removeCampaignFactionFront(index)"
              >移除</button>
            </header>
            <div class="form-grid">
              <label>权威势力档案
                <select v-model.number="front.profile_id" required>
                  <option :value="null">请选择势力档案</option>
                  <option v-for="profile in campaignFactionProfiles" :key="'campaign-profile-' + profile.id" :value="profile.id">
                    {{ campaignFactionProfileLabel(profile) }}
                  </option>
                </select>
              </label>
              <label>前线标题
                <input v-model.trim="front.title" required placeholder="该势力在战役中的独立前线名">
              </label>
            </div>
            <label>当前事实
              <textarea v-model.trim="front.summary" required rows="2" placeholder="该势力已经掌握或采取的行动。"></textarea>
            </label>
            <label>势力目标
              <textarea v-model.trim="front.agenda" required rows="2" placeholder="它想从共同事件中得到什么，以及下一步会如何介入。"></textarea>
            </label>
            <div class="campaign-scope-picker">
              <strong>精确副本作用域</strong>
              <div v-if="campaignForm.related_dungeons.length" class="dungeon-option-grid campaign-option-grid">
                <button
                  v-for="dungeon in campaignForm.related_dungeons"
                  :key="'faction-scope-' + index + '-' + dungeon"
                  type="button"
                  class="dungeon-option"
                  :class="{ active: front.scope_dungeons.includes(dungeon) }"
                  :aria-pressed="front.scope_dungeons.includes(dungeon)"
                  @click="toggleCampaignFactionDungeon(front, dungeon)"
                >{{ dungeon }}</button>
              </div>
              <small v-else class="muted">请先选择关联副本。</small>
            </div>
            <label class="campaign-checkbox">
              <input v-model="front.include_tavern" type="checkbox">
              同时登记深渊酒馆作用域，使这条势力线具备现有酒馆势力互动资格
            </label>
          </article>
        </fieldset>

        <fieldset class="campaign-fieldset">
          <div class="campaign-fieldset-heading">
            <strong class="campaign-legend">副本前线（至少 1 条）</strong>
            <button type="button" class="btn-secondary btn-sm" @click="addCampaignDungeonFront">添加副本前线</button>
          </div>
          <article v-for="(front, index) in campaignForm.dungeon_fronts" :key="'dungeon-front-' + index" class="campaign-front-card">
            <header>
              <strong>副本前线 {{ index + 1 }}</strong>
              <button
                type="button"
                class="btn-danger btn-xs"
                :disabled="campaignForm.dungeon_fronts.length <= 1"
                @click="removeCampaignDungeonFront(index)"
              >移除</button>
            </header>
            <div class="form-grid">
              <label>目标副本
                <input v-if="campaignForm.isolated_world" v-model.trim="front.dungeon" required maxlength="80" placeholder="全新的活动地图名">
                <select v-else v-model="front.dungeon" required>
                  <option value="">请选择已关联副本</option>
                  <option v-for="dungeon in campaignForm.related_dungeons" :key="'front-dungeon-' + index + '-' + dungeon" :value="dungeon">
                    {{ dungeon }}
                  </option>
                </select>
              </label>
              <label>前线标题
                <input v-model.trim="front.title" required placeholder="共同事件在这个副本中的本地表现">
              </label>
            </div>
            <template v-if="campaignForm.isolated_world">
              <label>所属城区（可选）<input v-model.trim="front.district" maxlength="80" placeholder="同一城区可包含多个独立现场"></label>
              <label>活动副本难度
                <select v-model.number="front.min_level" required>
                  <option v-for="tier in campaignActivityTiers" :key="tier.min_level" :value="tier.min_level">Lv.{{ tier.min_level }}-{{ tier.max_level }}</option>
                </select>
              </label>
              <label>新地点背景与模因设定
                <textarea v-model.trim="front.setting" required maxlength="4000" rows="3" placeholder="描述这个新地方的环境、独特规则和生态；后续在本活动内继续演变。"></textarea>
              </label>
            </template>
            <label>本地事实
              <textarea v-model.trim="front.summary" required rows="2" placeholder="这个地点已经确立的起始事实。"></textarea>
            </label>
            <label>本地推进目标
              <textarea v-model.trim="front.agenda" required rows="2" placeholder="后续探险可调查、阻止、争夺或改变的具体方向。"></textarea>
            </label>
          </article>
        </fieldset>

        <CampaignStagePlanEditor v-if="campaignForm.dispatch_enabled && campaignForm.staged"
          v-model="campaignForm.stage_plan" :maps="campaignForm.dungeon_fronts.map(front => front.dungeon).filter(Boolean)" />
        <div class="form-actions">
          <button type="submit" class="btn-primary btn-sm" :disabled="campaignPreviewing || campaignExecuting">
            {{ campaignPreviewing ? '生成预览中…' : '生成创建预览' }}
          </button>
          <button type="button" class="btn-secondary btn-sm" :disabled="campaignPreviewing || campaignExecuting" @click="resetCampaignForm">重置草稿</button>
        </div>
      </form>

      <section v-if="campaignPreview" class="campaign-preview-card">
        <div class="campaign-heading">
          <div>
            <strong>待确认预览</strong>
            <p class="admin-hint">预览令牌绑定当前输入；任意修改都会令它失效，必须重新预览。</p>
          </div>
          <button v-if="campaignPreviewToken" type="button" class="btn-primary btn-sm" :disabled="campaignExecuting" @click="executeCampaign">
            {{ campaignExecuting ? '正在创建…' : '确认按预览创建' }}
          </button>
          <span v-else class="status-tag pending">当前预览存在冲突，不能执行</span>
        </div>
        <pre class="json-preview campaign-preview-json">{{ formatJson(campaignPreview) }}</pre>
      </section>

      <div class="campaign-list-heading">
        <h6>已启动战役</h6>
        <span class="muted">共 {{ campaignTotal }} 条</span>
      </div>
      <div v-if="campaignLoading" class="empty">正在加载共同战役…</div>
      <div v-else-if="campaignRows.length === 0" class="empty">尚未启动共同战役。</div>
      <div v-else class="campaign-list">
        <article v-for="row in campaignRows" :key="row.campaign_key" class="campaign-card">
          <header>
            <div>
              <strong>{{ row.title }}</strong>
              <span class="muted">{{ row.campaign_key }}</span>
            </div>
            <div class="header-actions">
              <span v-if="row.status" :class="['status-tag', row.status]">{{ row.status }}</span>
              <span v-if="row.revision" class="status-tag">v{{ row.revision }}</span>
            </div>
          </header>
          <p v-if="row.summary">{{ row.summary }}</p>
          <CampaignStageAdminPanel v-if="row.dispatch && row.spine?.id" :root-id="row.spine.id" :readonly="readonly" />
          <div class="campaign-fact">
            <strong>议事厅</strong>
            <span v-if="row.dispatch">{{ row.dispatch.ended ? '事件已结束' : row.dispatch.stage_status === 'ready' ? (row.dispatch.stage_advance_mode === 'auto' ? '阶段已完成，等待自动推进' : '阶段已完成，等待核验') : row.dispatch.active_log_id ? '等待队伍战报与世界维护' : row.dispatch.discovered ? '已公开，按当前地图开放报名' : '尚未发现' }}</span>
            <button v-else-if="!readonly" type="button" class="btn-secondary btn-sm" :disabled="campaignExecuting" @click="enableCampaignDispatch(row)">启用议事厅推进</button>
            <span v-else>未启用</span>
          </div>
          <div v-if="row.dispatch?.maintenance_status === 'failed'" class="campaign-fact">
            <strong>发布失败</strong><span>{{ row.dispatch.maintenance_error }}</span>
            <button v-if="!readonly" type="button" class="btn-secondary btn-sm" :disabled="campaignExecuting" @click="retryCampaignPublication(row)">重试世界维护</button>
          </div>
          <div v-if="row.campaign_agenda || row.agenda" class="campaign-fact">
            <strong>酒馆目标</strong><span>{{ row.campaign_agenda || row.agenda }}</span>
          </div>
          <div v-if="row.power_profile" class="campaign-fact">
            <strong>力量边界</strong><span>{{ row.power_profile }}</span>
          </div>
          <div class="tag-row">
            <span v-for="dungeon in row.related_dungeons || []" :key="'campaign-related-' + row.campaign_key + '-' + dungeon">{{ dungeon }}</span>
          </div>

          <div class="campaign-member-list">
            <article v-for="member in campaignMembers(row)" :key="member.id" class="campaign-member-card">
              <header>
                <div>
                  <strong>#{{ member.id }} {{ member.title }}</strong>
                  <span class="status-tag">{{ campaignMemberRole(member) }}</span>
                </div>
                <div class="header-actions">
                  <span v-if="member.status" :class="['status-tag', member.status]">{{ member.status }}</span>
                  <span v-if="member.revision" class="status-tag">v{{ member.revision }}</span>
                </div>
              </header>
              <p v-if="member.summary">{{ member.summary }}</p>
              <p v-if="member.base_dungeon" class="muted">主副本：{{ member.base_dungeon }}</p>
              <div class="tag-row">
                <span v-for="scope in member.scope_keys || []" :key="'campaign-member-scope-' + member.id + '-' + scope">scope: {{ scope }}</span>
              </div>
              <div v-if="editingCampaignAgendaThreadId === member.id" class="campaign-agenda-editor">
                <textarea v-model="campaignAgendaDraft" rows="3" maxlength="4000"></textarea>
                <div class="form-actions">
                  <button type="button" class="btn-primary btn-sm" :disabled="campaignAgendaSaving" @click="saveCampaignAgenda(member)">
                    {{ campaignAgendaSaving ? '保存中…' : '按当前 revision 保存' }}
                  </button>
                  <button type="button" class="btn-secondary btn-sm" :disabled="campaignAgendaSaving" @click="cancelCampaignAgendaEdit">取消</button>
                </div>
              </div>
              <div v-else class="campaign-fact">
                <strong>当前议程</strong>
                <span>{{ campaignMemberAgenda(member) || '未设置' }}</span>
                <button v-if="!readonly" type="button" class="btn-secondary btn-xs" @click="beginCampaignAgendaEdit(member)">修改议程</button>
              </div>
            </article>
          </div>
        </article>
      </div>
    </section>

    <section v-show="activeWorkspace === 'actors'" class="section-block">
      <h6>长期世界角色</h6>
      <p class="admin-hint">由已结算剧情自动形成和维护。势力态度采用多维描述，不使用好感或敌意数值。</p>
      <div class="toolbar compact-toolbar">
        <input v-model="actorKeyword" class="filter-input" placeholder="角色专名" @keydown.enter="loadActors(1)">
        <select v-model="actorStatusFilter" class="filter-select" @change="loadActors(1)">
          <option value="">全部状态</option>
          <option v-for="status in actorStatuses" :key="status" :value="status">{{ status }}</option>
        </select>
        <button type="button" class="btn-secondary btn-sm" :disabled="actorLoading" @click="loadActors(1)">刷新角色</button>
      </div>
      <div v-if="actorLoading" class="empty">加载长期角色中…</div>
      <div v-else-if="actorRows.length === 0" class="empty">暂无长期世界角色；角色会在后续已结算剧情中自动形成。</div>
      <div v-else class="actor-list">
        <article v-for="actor in actorRows" :key="actor.id" class="actor-card">
          <header>
            <div>
              <strong>#{{ actor.id }} {{ actor.canonical_name }}</strong>
              <span :class="['status-tag', actor.status]">{{ actor.status }}</span>
              <span class="status-tag">{{ actor.actor_type }}</span>
            </div>
            <div class="header-actions">
              <span class="muted">重要度 {{ actor.importance }}</span>
              <button type="button" class="btn-secondary btn-xs" :disabled="actorDetailLoadingId === actor.id" @click="toggleActorDetail(actor)">
                {{ expandedActorId === actor.id ? '收起详情' : actorDetailLoadingId === actor.id ? '加载中…' : '查看详情' }}
              </button>
            </div>
          </header>
          <p>{{ actor.identity_summary }}</p>
          <div class="actor-summary-grid">
            <div v-if="actor.power_boundary"><strong>力量边界</strong><span>{{ actor.power_boundary }}</span></div>
            <div v-if="actor.long_term_goal"><strong>长期目标</strong><span>{{ actor.long_term_goal }}</span></div>
            <div v-if="actor.current_agenda"><strong>当前行动</strong><span>{{ actor.current_agenda }}</span></div>
            <div v-if="actor.current_state"><strong>当前处境</strong><span>{{ actor.current_state }}</span></div>
          </div>
          <div class="tag-row"><span v-for="tag in actor.scope_keys" :key="`actor-s-${actor.id}-${tag}`">scope: {{ tag }}</span></div>

          <div v-if="expandedActorId === actor.id && actorDetails[actor.id]" class="actor-detail">
            <div v-if="actorDetails[actor.id].stable_principles" class="detail-block">
              <strong>稳定原则</strong><span>{{ actorDetails[actor.id].stable_principles }}</span>
            </div>
            <div class="detail-block">
              <strong>对各势力的复杂态度</strong>
              <div v-if="!actorDetails[actor.id].faction_relations.length" class="empty compact-empty">暂无已形成的势力关系</div>
              <article v-for="relation in actorDetails[actor.id].faction_relations" :key="relation.id" class="relation-card">
                <header><strong>{{ relation.faction }}</strong><span class="muted">{{ relation.faction_key }}</span></header>
                <dl class="relation-grid">
                  <template v-for="field in relationFields" :key="field.key">
                    <template v-if="relation[field.key]">
                      <dt>{{ field.label }}</dt><dd>{{ relation[field.key] }}</dd>
                    </template>
                  </template>
                </dl>
              </article>
            </div>
            <div class="detail-block">
              <strong>关联世界线</strong>
              <div v-if="!actorDetails[actor.id].world_story_threads.length" class="empty compact-empty">暂无关联世界线</div>
              <div v-else class="thread-link-list">
                <span v-for="thread in actorDetails[actor.id].world_story_threads" :key="thread.id">
                  #{{ thread.id }} {{ thread.title || '未命名' }} · {{ thread.involvement }}<template v-if="thread.narrative_role"> · {{ thread.narrative_role }}</template>
                </span>
              </div>
            </div>
            <div class="detail-block">
              <strong>最近已结算交集</strong>
              <div v-if="!actorDetails[actor.id].recent_interactions.length" class="empty compact-empty">暂无交集记录</div>
              <article v-for="interaction in actorDetails[actor.id].recent_interactions" :key="interaction.id" class="interaction-row">
                <div class="meta-row"><span>Log #{{ interaction.expedition_log_id || '-' }}</span><span>{{ formatTime(interaction.created_at) }}</span></div>
                <p>{{ interaction.interaction_summary }}</p>
                <small v-if="interaction.actor_change_summary" class="muted">角色变化：{{ interaction.actor_change_summary }}</small>
                <small v-if="interaction.relation_change_summary" class="muted">关系变化：{{ interaction.relation_change_summary }}</small>
              </article>
            </div>
          </div>
        </article>
      </div>
      <div v-if="actorTotal > actorPerPage" class="pager-row">
        <button type="button" class="btn-secondary btn-sm" :disabled="actorPage <= 1 || actorLoading" @click="loadActors(actorPage - 1)">上一页</button>
        <span>第 {{ actorPage }} 页 / 共 {{ actorTotal }} 名</span>
        <button type="button" class="btn-secondary btn-sm" :disabled="actorPage * actorPerPage >= actorTotal || actorLoading" @click="loadActors(actorPage + 1)">下一页</button>
      </div>
    </section>

    <section v-show="activeWorkspace === 'preview'" class="section-block">
      <h6>情报注入预览</h6>
      <div class="toolbar compact-toolbar">
        <input v-model="previewDungeon" class="filter-input" placeholder="输入副本名" @keydown.enter="previewIntel">
        <input v-model="previewContext" class="filter-input wide-input" placeholder="可选：委托/实体/势力/关键物/特殊事件关键词" @keydown.enter="previewIntel">
        <button type="button" class="btn-secondary btn-sm" :disabled="previewLoading" @click="previewIntel">预览注入</button>
        <button type="button" class="btn-secondary btn-sm" :disabled="!previewHasResult" @click="previewDetailOpen = !previewDetailOpen">
          {{ previewDetailOpen ? '收起匹配详情' : '查看匹配详情' }}
        </button>
      </div>
      <div v-if="previewDetailOpen" class="match-detail-panel">
        <div class="detail-block">
          <strong>传入锚点</strong>
          <div class="tag-row detail-tags">
            <span v-for="tag in previewContextKeywords" :key="`ctx-${tag}`">{{ tag }}</span>
            <span v-if="!previewContextKeywords.length">无</span>
          </div>
        </div>
        <div class="detail-block">
          <strong>传入上下文</strong>
          <pre class="json-preview match-context">{{ previewContextText || '无' }}</pre>
        </div>
      </div>
      <div v-if="previewRows.length" class="intel-list">
        <article v-for="row in previewRows" :key="`preview-${row.id}`" class="intel-card preview-card">
          <header>
            <strong>{{ row.title }}</strong>
            <span class="muted">{{ row.thread_type }} / {{ row.status }} / 权重 {{ row.weight }} / 匹配 {{ row.match_score ?? '-' }}</span>
          </header>
          <p>{{ row.summary }}</p>
          <div v-if="previewDetailOpen" class="match-reason-grid">
            <div><strong>标题命中</strong><span>{{ row.matched_title ? '是' : '否' }}</span></div>
            <div><strong>主地城命中</strong><span>{{ row.matched_base_dungeon ? '是' : '否' }}</span></div>
            <div><strong>scope 命中</strong><span>{{ joinOrNone(row.matched_scope_keys) }}</span></div>
            <div><strong>关键词命中</strong><span>{{ joinOrNone(row.matched_trigger_keywords) }}</span></div>
            <div><strong>上下文锚点</strong><span>{{ joinOrNone(row.matched_context_terms) }}</span></div>
            <div><strong>正文 token</strong><span>{{ joinOrNone(row.matched_text_tokens) }}</span></div>
          </div>
        </article>
      </div>
      <pre v-if="previewBlock" class="json-preview">{{ previewBlock }}</pre>
    </section>

    <section v-show="activeWorkspace === 'pipeline' && activePipelineView === 'handoffs'" class="section-block">
      <h6>当前 DM 接力便签</h6>
      <div v-if="handoffLoading" class="empty">加载接力便签中…</div>
      <div v-else-if="handoffStates.length === 0" class="empty">暂无已维护便签</div>
      <div v-else class="handoff-grid">
        <article v-for="row in handoffStates" :key="row.id" class="handoff-card">
          <header>
            <strong>{{ row.target_dungeon }}</strong>
            <div class="header-actions">
              <span class="muted">{{ formatTime(row.updated_at) }}</span>
              <button type="button" class="btn-danger btn-xs" :disabled="deletingHandoffId === row.id" @click="deleteHandoff(row)">
                {{ deletingHandoffId === row.id ? '删除中…' : '删除便签' }}
              </button>
            </div>
          </header>
          <p>{{ row.note }}</p>
          <small class="muted">来源候选：{{ (row.source_candidate_ids || []).join(', ') || '无' }}</small>
        </article>
      </div>
      <div v-if="handoffTotal > handoffPerPage" class="pager-row">
        <button type="button" class="btn-secondary btn-sm" :disabled="handoffPage <= 1 || handoffLoading" @click="loadHandoffStates(handoffPage - 1)">上一页</button>
        <span>第 {{ handoffPage }} 页 / 共 {{ handoffTotal }} 条</span>
        <button type="button" class="btn-secondary btn-sm" :disabled="handoffPage * handoffPerPage >= handoffTotal || handoffLoading" @click="loadHandoffStates(handoffPage + 1)">下一页</button>
      </div>
    </section>

    <section v-show="activeWorkspace === 'pipeline' && activePipelineView === 'candidates'" class="section-block">
      <h6>世界事实 / 便签候选</h6>
      <div class="toolbar compact-toolbar">
        <input v-model="dungeonFilter" class="filter-input" placeholder="按副本名筛选候选" @keydown.enter="loadCandidates(1)">
        <select v-model="statusFilter" class="filter-select" @change="loadCandidates(1)">
          <option value="">全部状态</option>
          <option value="pending">pending</option>
          <option value="processed">processed</option>
          <option value="failed">failed</option>
        </select>
        <button type="button" class="btn-secondary btn-sm" :disabled="loading" @click="loadCandidates(1)">刷新候选</button>
      </div>
      <div v-if="loading" class="empty">加载中…</div>
      <div v-else-if="candidates.length === 0" class="empty">暂无候选</div>
      <div v-else class="candidate-list">
        <article v-for="row in candidates" :key="row.id" class="candidate-card">
          <header>
            <strong>#{{ row.id }} {{ row.target_dungeon }}</strong>
            <div class="header-actions">
              <span :class="['status-tag', row.status]">{{ row.status }}</span>
              <button type="button" class="btn-danger btn-xs" :disabled="deletingCandidateId === row.id" @click="deleteCandidate(row)">
                {{ deletingCandidateId === row.id ? '删除中…' : '删除' }}
              </button>
            </div>
          </header>
          <div class="meta-row">
            <span>Log #{{ row.expedition_log_id }}</span>
            <span>{{ formatTime(row.created_at) }}</span>
            <span v-if="row.processed_at">处理于 {{ formatTime(row.processed_at) }}</span>
          </div>
          <p v-if="row.dm_handoff_note_candidate" class="note-text">{{ row.dm_handoff_note_candidate }}</p>
          <pre v-if="row.world_fact_updates?.length" class="json-preview">{{ formatJson(row.world_fact_updates) }}</pre>
          <p v-if="row.outline_error" class="error-text">{{ row.outline_error }}</p>
        </article>
      </div>
      <div v-if="total > perPage" class="pager-row">
        <button type="button" class="btn-secondary btn-sm" :disabled="page <= 1 || loading" @click="loadCandidates(page - 1)">上一页</button>
        <span>第 {{ page }} 页 / 共 {{ total }} 条</span>
        <button type="button" class="btn-secondary btn-sm" :disabled="page * perPage >= total || loading" @click="loadCandidates(page + 1)">下一页</button>
      </div>
    </section>
  </div>
</template>

<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue'
import api from '@/composables/useApi'
import CampaignStagePlanEditor from './CampaignStagePlanEditor.vue'
import CampaignStageAdminPanel from './CampaignStageAdminPanel.vue'
import type { CampaignChallenge, CampaignStagePlan } from './campaignStageTypes'
import { useGlobalMessage } from '@/composables/useGlobalMessage'

const props = withDefaults(defineProps<{
  readonly?: boolean
}>(), {
  readonly: false,
})
const readonly = computed(() => props.readonly)

interface CandidateRow {
  id: number
  expedition_log_id: number
  target_dungeon: string
  world_fact_updates: unknown[]
  dm_handoff_note_candidate: string
  status: string
  outline_error?: string
  processed_at?: string
  created_at?: string
}

interface HandoffRow {
  id: number
  target_dungeon: string
  note: string
  source_candidate_ids: number[]
  updated_at?: string
}

interface IntelRow {
  id: number
  title: string
  thread_type: string
  status: string
  base_dungeon: string
  scope_keys: string[]
  trigger_keywords: string[]
  summary: string
  next_reveal_hint: string
  power_tier: string
  power_profile: string
  weight: number
  revision?: number
  campaign_key?: string
  campaign_root_id?: number | null
  campaign_role?: string
  campaign_agenda?: string
  match_score?: number
  matched_title?: boolean
  matched_base_dungeon?: boolean
  matched_scope_keys?: string[]
  matched_trigger_keywords?: string[]
  matched_context_terms?: string[]
  matched_text_tokens?: string[]
}

interface IntelFormState {
  id: number | null
  title: string
  thread_type: string
  status: string
  weight: number
  base_dungeon: string
  scope_keys_text: string
  trigger_keywords_text: string
  summary: string
  next_reveal_hint: string
  power_tier: string
  power_profile: string
}

interface CampaignFactionProfileMeta {
  id: number
  faction_name?: string
  canonical_name?: string
  name?: string
  title?: string
  revision?: number
}

interface CampaignFactionFrontForm {
  profile_id: number | null
  title: string
  summary: string
  agenda: string
  scope_dungeons: string[]
  include_tavern: boolean
}

interface CampaignDungeonFrontForm {
  challenge?: CampaignChallenge
  district: string
  dungeon: string
  title: string
  summary: string
  agenda: string
  min_level: number
  setting: string
}

interface CampaignFormState {
  max_active_teams: number
  isolated_world: boolean
  dispatch_enabled: boolean
  staged: boolean
  stage_plan: CampaignStagePlan
  campaign_key: string
  title: string
  summary: string
  agenda: string
  power_profile: string
  related_dungeons: string[]
  faction_fronts: CampaignFactionFrontForm[]
  dungeon_fronts: CampaignDungeonFrontForm[]
}

interface CampaignMemberRow {
  id: number
  title: string
  summary?: string
  status?: string
  revision?: number
  campaign_role?: string
  role?: string
  campaign_agenda?: string
  agenda?: string
  base_dungeon?: string
  scope_keys?: string[]
  faction_profile_id?: number | null
}

interface CampaignRow {
  spine?: CampaignMemberRow
  dispatch?: { id: number; revision: number; discovered: boolean; ended: boolean; active_log_id: number | null; maintenance_status?: string; maintenance_error?: string; stage_status?: 'active' | 'ready' | 'ended' | null; stage_advance_mode?: 'auto' | 'manual' }
  campaign_key: string
  title: string
  summary?: string
  agenda?: string
  campaign_agenda?: string
  power_profile?: string
  status?: string
  revision?: number
  related_dungeons?: string[]
  members?: CampaignMemberRow[]
  threads?: CampaignMemberRow[]
  campaign_members?: CampaignMemberRow[]
}

interface WorldActorRow {
  id: number
  canonical_name: string
  aliases: string[]
  actor_type: string
  status: string
  identity_summary: string
  stable_principles: string
  power_tier: string
  power_boundary: string
  long_term_goal: string
  current_agenda: string
  current_state: string
  scope_keys: string[]
  trigger_keywords: string[]
  importance: number
}

type RelationTextKey = 'public_stance' | 'private_view' | 'interests' | 'concerns' | 'dependencies' | 'unresolved_matters' | 'behavior_boundary'

interface ActorFactionRelation extends Record<RelationTextKey, string> {
  id: number
  faction_key: string
  faction: string
}

interface ActorThreadLink {
  id: number
  title: string
  involvement: string
  narrative_role: string
  lease_required: boolean
}

interface ActorInteraction {
  id: number
  expedition_log_id: number | null
  interaction_summary: string
  actor_change_summary: string
  relation_change_summary: string
  created_at?: string
}

interface WorldActorDetail extends WorldActorRow {
  faction_relations: ActorFactionRelation[]
  world_story_threads: ActorThreadLink[]
  recent_interactions: ActorInteraction[]
}

type WorldWorkspaceId = 'intel' | 'campaigns' | 'actors' | 'preview' | 'pipeline'
type PipelineView = 'handoffs' | 'candidates'

const WORLD_INTEL_GENERATION_TIMEOUT_MS = 660_000

interface WorldWorkspace {
  id: WorldWorkspaceId
  label: string
  icon: string
  description: string
  detail: string
  count?: number
}

const message = useGlobalMessage()
const candidates = ref<CandidateRow[]>([])
const handoffStates = ref<HandoffRow[]>([])
const handoffPage = ref(1)
const handoffPerPage = ref(20)
const handoffTotal = ref(0)
const handoffLoading = ref(false)
const dungeonFilter = ref('')
const statusFilter = ref('')
const page = ref(1)
const perPage = ref(20)
const total = ref(0)
const loading = ref(false)
const maintaining = ref(false)
const deletingCandidateId = ref<number | null>(null)
const deletingHandoffId = ref<number | null>(null)
const intelRows = ref<IntelRow[]>([])
const intelTypes = ref<string[]>(['entity', 'event', 'faction', 'location', 'omen', 'artifact'])
const intelStatuses = ref<string[]>(['seeded', 'active', 'escalated', 'dormant', 'resolved'])
const intelKeyword = ref('')
const intelTypeFilter = ref('')
const intelStatusFilter = ref('')
const intelPage = ref(1)
const intelPerPage = ref(20)
const intelTotal = ref(0)
const intelMaxTotal = ref(0)
const intelLoading = ref(false)
const savingIntel = ref(false)
const deletingIntelId = ref<number | null>(null)
const editingIntel = ref(false)
const generatorOpen = ref(false)
const generatorBrief = ref('')
const generatorType = ref('')
const generatorDungeons = ref<string[]>([])
const generatorDungeonOptions = ref<string[]>([])
const generatorDungeonsLoading = ref(false)
const generatorAllDungeons = ref(false)
const generatingIntel = ref(false)
const campaignRows = ref<CampaignRow[]>([])
const campaignTotal = ref(0)
const campaignLoading = ref(false)
const campaignMetaLoading = ref(false)
const campaignPreviewing = ref(false)
const campaignExecuting = ref(false)
const campaignAgendaSaving = ref(false)
const campaignDungeonOptions = ref<string[]>([])
const campaignActivityTiers = ref<{ min_level: number; max_level: number }[]>([])
const campaignFactionProfiles = ref<CampaignFactionProfileMeta[]>([])
const campaignMaxFronts = ref(20)
const campaignMaxActiveTeams = ref(20)
const campaignPresets = ref<{ key: string; title: string }[]>([])
const campaignPresetLoading = ref(false)
const campaignMaxScopesPerFront = ref(8)
const campaignForm = ref<CampaignFormState>(emptyCampaignForm())
const campaignPreviewToken = ref('')
const campaignPreview = ref<unknown>(null)
const editingCampaignAgendaThreadId = ref<number | null>(null)
const campaignAgendaDraft = ref('')
const actorRows = ref<WorldActorRow[]>([])
const actorStatuses = ['rumored', 'active', 'withdrawn', 'missing', 'retired', 'dead']
const actorKeyword = ref('')
const actorStatusFilter = ref('')
const actorPage = ref(1)
const actorPerPage = ref(20)
const actorTotal = ref(0)
const actorLoading = ref(false)
const actorDetailLoadingId = ref<number | null>(null)
const expandedActorId = ref<number | null>(null)
const actorDetails = ref<Record<number, WorldActorDetail>>({})
const relationFields: { key: RelationTextKey; label: string }[] = [
  { key: 'public_stance', label: '公开立场' },
  { key: 'private_view', label: '真实看法' },
  { key: 'interests', label: '利益交集' },
  { key: 'concerns', label: '顾虑与戒备' },
  { key: 'dependencies', label: '依赖与需要' },
  { key: 'unresolved_matters', label: '未清事项' },
  { key: 'behavior_boundary', label: '行为边界' },
]
const previewDungeon = ref('')
const previewContext = ref('')
const previewLoading = ref(false)
const previewRows = ref<IntelRow[]>([])
const previewBlock = ref('')
const previewContextKeywords = ref<string[]>([])
const previewContextText = ref('')
const previewDetailOpen = ref(false)
const intelForm = ref<IntelFormState>(emptyIntelForm())
const activeWorkspace = ref<WorldWorkspaceId>('intel')
const activePipelineView = ref<PipelineView>('handoffs')
const WORLD_WORKSPACES = computed<WorldWorkspace[]>(() => {
  const workspaces: WorldWorkspace[] = [
    { id: 'intel', label: '世界情报', icon: '🌐', description: readonly.value ? '只读情报条目' : '维护情报条目', detail: '查看会被探险系统持续引用的世界实体、事件、势力、地点与征兆。', count: intelTotal.value },
  ]
  if (!readonly.value) workspaces.push({
    id: 'campaigns',
    label: '共同战役',
    icon: '⚔️',
    description: '启动并维护跨副本共同主轴',
    detail: '冻结共同事件、酒馆目标、势力目标和副本落点，再由各条现有世界线独立推进；创建必须先预览并凭令牌确认。',
    count: campaignTotal.value,
  })
  if (!readonly.value) workspaces.push(
    { id: 'actors', label: '长期角色', icon: '🎭', description: '角色与势力关系', detail: '查看由已结算剧情形成的长期角色，以及他们的目标、处境和势力关系。', count: actorTotal.value },
    { id: 'preview', label: '注入测试', icon: '🧪', description: '验证匹配结果', detail: '模拟副本与上下文输入，检查实际会注入探险 AI 的情报及匹配原因。' },
    { id: 'pipeline', label: '维护队列', icon: '📥', description: '候选与接力便签', detail: '处理剧情产生的世界事实候选，并检查按副本合并后的 DM 接力便签。', count: total.value },
  )
  return workspaces
})
const currentWorkspace = computed(() => WORLD_WORKSPACES.value.find(item => item.id === activeWorkspace.value) ?? WORLD_WORKSPACES.value[0])
const previewHasResult = computed(() => previewRows.value.length > 0 || !!previewContextText.value || previewContextKeywords.value.length > 0)

function emptyIntelForm(): IntelFormState {
  return {
    id: null,
    title: '',
    thread_type: 'event',
    status: 'active',
    weight: 50,
    base_dungeon: '',
    scope_keys_text: '',
    trigger_keywords_text: '',
    summary: '',
    next_reveal_hint: '',
    power_tier: '',
    power_profile: '',
  }
}

function emptyCampaignFactionFront(): CampaignFactionFrontForm {
  return {
    profile_id: null,
    title: '',
    summary: '',
    agenda: '',
    scope_dungeons: [],
    include_tavern: false,
  }
}

function emptyCampaignDungeonFront(): CampaignDungeonFrontForm {
  return {
    district: '',
    dungeon: '',
    title: '',
    summary: '',
    agenda: '',
    min_level: 1,
    setting: '',
  }
}

function emptyCampaignForm(): CampaignFormState {
  return {
    max_active_teams: 1,
    isolated_world: false,
    dispatch_enabled: true,
    staged: false,
    stage_plan: { advance_mode: 'auto', revelation_enabled: true, continuity_enabled: true, stages: [] },
    campaign_key: '',
    title: '',
    summary: '',
    agenda: '',
    power_profile: '',
    related_dungeons: [],
    faction_fronts: [],
    dungeon_fronts: [emptyCampaignDungeonFront()],
  }
}

function formatTime(value?: string) {
  if (!value) return '-'
  return new Date(value).toLocaleString()
}

function formatJson(value: unknown) {
  return JSON.stringify(value, null, 2)
}

function joinOrNone(values?: string[]) {
  return values?.length ? values.join('、') : '无'
}

async function loadCandidates(nextPage = 1) {
  loading.value = true
  try {
    const params: Record<string, string | number> = { page: nextPage, per_page: perPage.value }
    if (dungeonFilter.value.trim()) params.target_dungeon = dungeonFilter.value.trim()
    if (statusFilter.value) params.status = statusFilter.value
    const { data } = await api.get('/adventurer-s1/admin/world-outline/candidates', { params })
    if (data.code === 200) {
      candidates.value = data.data?.items || []
      total.value = data.data?.total || 0
      page.value = data.data?.page || nextPage
      perPage.value = data.data?.per_page || perPage.value
    } else {
      message.error(data.message || '加载候选失败')
    }
  } finally {
    loading.value = false
  }
}

async function loadHandoffStates(nextPage = 1) {
  handoffLoading.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/world-outline/dm-handoff', {
      params: { page: nextPage, per_page: handoffPerPage.value },
    })
    if (data.code === 200) {
      handoffStates.value = data.data?.items || []
      handoffTotal.value = data.data?.total || 0
      handoffPage.value = data.data?.page || nextPage
      handoffPerPage.value = data.data?.per_page || handoffPerPage.value
    }
  } finally {
    handoffLoading.value = false
  }
}

async function loadIntel(nextPage = 1) {
  intelLoading.value = true
  try {
    const params: Record<string, string | number> = { page: nextPage, per_page: intelPerPage.value }
    if (intelKeyword.value.trim()) params.keyword = intelKeyword.value.trim()
    if (intelTypeFilter.value) params.thread_type = intelTypeFilter.value
    if (intelStatusFilter.value) params.status = intelStatusFilter.value
    const { data } = await api.get('/adventurer-s1/admin/world-outline/intel', { params })
    if (data.code === 200) {
      intelRows.value = data.data?.items || []
      intelTotal.value = data.data?.total || 0
      intelPage.value = data.data?.page || nextPage
      intelPerPage.value = data.data?.per_page || intelPerPage.value
      intelMaxTotal.value = data.data?.max_total || 0
      if (data.data?.thread_types?.length) intelTypes.value = data.data.thread_types
      if (data.data?.statuses?.length) intelStatuses.value = data.data.statuses
    } else {
      message.error(data.message || '加载情报库失败')
    }
  } finally {
    intelLoading.value = false
  }
}

async function loadActors(nextPage = 1) {
  actorLoading.value = true
  try {
    const params: Record<string, string | number> = { page: nextPage, per_page: actorPerPage.value }
    if (actorKeyword.value.trim()) params.keyword = actorKeyword.value.trim()
    if (actorStatusFilter.value) params.status = actorStatusFilter.value
    const { data } = await api.get('/adventurer-s1/admin/world-outline/actors', { params })
    if (data.code === 200) {
      actorRows.value = data.data?.items || []
      actorTotal.value = data.data?.total || 0
      actorPage.value = data.data?.page || nextPage
      actorPerPage.value = data.data?.per_page || actorPerPage.value
      expandedActorId.value = null
    } else {
      message.error(data.message || '加载长期世界角色失败')
    }
  } finally {
    actorLoading.value = false
  }
}

async function toggleActorDetail(actor: WorldActorRow) {
  if (expandedActorId.value === actor.id) {
    expandedActorId.value = null
    return
  }
  if (!actorDetails.value[actor.id]) {
    actorDetailLoadingId.value = actor.id
    try {
      const { data } = await api.get(`/adventurer-s1/admin/world-outline/actors/${actor.id}`)
      if (data.code !== 200) {
        message.error(data.message || '加载角色详情失败')
        return
      }
      actorDetails.value[actor.id] = data.data
    } finally {
      actorDetailLoadingId.value = null
    }
  }
  expandedActorId.value = actor.id
}

function normalizeCampaignStrings(value: unknown): string[] {
  if (!Array.isArray(value)) return []
  return [...new Set(value.map(item => String(item ?? '').trim()).filter(Boolean))]
}

function campaignFactionProfileLabel(profile: CampaignFactionProfileMeta) {
  const name = profile.faction_name || profile.canonical_name || profile.name || profile.title || `势力档案 #${profile.id}`
  return profile.revision ? `${name}（#${profile.id} / v${profile.revision}）` : `${name}（#${profile.id}）`
}

function campaignMembers(row: CampaignRow): CampaignMemberRow[] {
  return row.members || row.threads || row.campaign_members || []
}

function campaignMemberAgenda(member: CampaignMemberRow) {
  return member.campaign_agenda || member.agenda || ''
}

function campaignMemberRole(member: CampaignMemberRow) {
  return member.campaign_role || member.role || 'member'
}

async function loadCampaignMeta() {
  campaignMetaLoading.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/world-outline/campaigns/meta', {
      params: { page: 1, per_page: 100, faction_status: 'active' },
    })
    if (data.code !== 200) {
      message.error(data.message || '共同战役元数据加载失败')
      return
    }
    const meta = data.data || {}
    campaignActivityTiers.value = meta.activity_dungeon_tiers || []
    campaignPresets.value = meta.presets || []
    campaignMaxActiveTeams.value = Number(meta.limits?.max_active_teams) || 20
    let dungeons = normalizeCampaignStrings(
      meta.dungeons || meta.related_dungeons || meta.dungeon_options || meta.available_dungeons,
    )
    if (!dungeons.length) {
      const { data: dungeonData } = await api.get('/adventurer-s1/admin/items/dungeon-list')
      dungeons = normalizeCampaignStrings(dungeonData?.data)
    }
    campaignDungeonOptions.value = dungeons
    campaignMaxFronts.value = Math.max(2, Number(meta.limits?.max_fronts) || 20)
    campaignMaxScopesPerFront.value = Math.max(1, Number(meta.limits?.max_scope_keys_per_front) || 8)
    const rawProfiles = Array.isArray(meta.faction_profiles?.items)
      ? meta.faction_profiles.items
      : Array.isArray(meta.faction_profiles)
        ? meta.faction_profiles
        : Array.isArray(meta.profiles)
          ? meta.profiles
          : Array.isArray(meta.faction_profile_options)
            ? meta.faction_profile_options
            : []
    campaignFactionProfiles.value = rawProfiles
      .map((item: any) => ({
        id: Number(item.id ?? item.profile_id),
        faction_name: item.faction_name,
        canonical_name: item.canonical_name,
        name: item.name,
        title: item.title,
        revision: Number(item.revision || 0) || undefined,
      }))
      .filter((item: CampaignFactionProfileMeta) => Number.isInteger(item.id) && item.id > 0)
  } finally {
    campaignMetaLoading.value = false
  }
}

async function loadCampaigns() {
  campaignLoading.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/world-outline/campaigns', {
      params: { page: 1, per_page: 100 },
    })
    if (data.code !== 200) {
      message.error(data.message || '共同战役列表加载失败')
      return
    }
    const payload = data.data
    const rows = Array.isArray(payload)
      ? payload
      : payload?.items || payload?.campaigns || []
    campaignRows.value = (Array.isArray(rows) ? rows : []).map((row: any) => {
      const spine = row.spine || {}
      const fronts = Array.isArray(row.fronts) ? row.fronts : []
      const relatedDungeons = normalizeCampaignStrings(fronts.flatMap((front: any) => [
        front.base_dungeon,
        ...(Array.isArray(front.scope_keys) ? front.scope_keys : []),
      ])).filter(scope => scope !== '__all_dungeons__' && scope !== 'abyss_tavern')
      return {
        ...row,
        title: spine.title || row.title || row.campaign_key,
        summary: spine.summary || row.summary || '',
        campaign_agenda: spine.campaign_agenda || row.campaign_agenda || row.agenda || '',
        power_profile: spine.power_profile || row.power_profile || '',
        related_dungeons: relatedDungeons,
        members: [spine, ...fronts].filter(member => Number(member?.id) > 0),
      }
    })
    campaignTotal.value = Number(payload?.total ?? campaignRows.value.length) || 0
  } finally {
    campaignLoading.value = false
  }
}

async function loadCampaignWorkspace() {
  await Promise.all([loadCampaignMeta(), loadCampaigns()])
}

function toggleCampaignRelatedDungeon(dungeon: string) {
  const selected = campaignForm.value.related_dungeons
  campaignForm.value.related_dungeons = selected.includes(dungeon)
    ? selected.filter(item => item !== dungeon)
    : [...selected, dungeon]
}

function changeCampaignWorldMode() {
  if (campaignForm.value.isolated_world) campaignForm.value.dispatch_enabled = true
  campaignForm.value.dungeon_fronts = [emptyCampaignDungeonFront()]
  campaignForm.value.faction_fronts = []
  campaignForm.value.related_dungeons = []
  campaignForm.value.stage_plan.stages = []
}

function toggleCampaignFactionDungeon(front: CampaignFactionFrontForm, dungeon: string) {
  const maxDungeonScopes = Math.max(
    1,
    campaignMaxScopesPerFront.value - (front.include_tavern ? 1 : 0),
  )
  if (!front.scope_dungeons.includes(dungeon) && front.scope_dungeons.length >= maxDungeonScopes) {
    message.warning(
      '这条前线当前最多选择 ' + maxDungeonScopes
      + ' 个副本作用域；酒馆作用域也会占用一个名额',
    )
    return
  }
  front.scope_dungeons = front.scope_dungeons.includes(dungeon)
    ? front.scope_dungeons.filter(item => item !== dungeon)
    : [...front.scope_dungeons, dungeon]
}

function addCampaignFactionFront() {
  if (campaignForm.value.faction_fronts.length + campaignForm.value.dungeon_fronts.length >= campaignMaxFronts.value) {
    message.warning('一个共同战役最多 ' + campaignMaxFronts.value + ' 条前线')
    return
  }
  campaignForm.value.faction_fronts.push(emptyCampaignFactionFront())
}

function removeCampaignFactionFront(index: number) {
  if (!campaignForm.value.dispatch_enabled && campaignForm.value.faction_fronts.length <= 1) return
  campaignForm.value.faction_fronts.splice(index, 1)
}

function addCampaignDungeonFront() {
  if (campaignForm.value.faction_fronts.length + campaignForm.value.dungeon_fronts.length >= campaignMaxFronts.value) {
    message.warning('一个共同战役最多 ' + campaignMaxFronts.value + ' 条前线')
    return
  }
  campaignForm.value.dungeon_fronts.push(emptyCampaignDungeonFront())
}

function removeCampaignDungeonFront(index: number) {
  if (campaignForm.value.dungeon_fronts.length <= 1) return
  campaignForm.value.dungeon_fronts.splice(index, 1)
}

function resetCampaignForm() {
  campaignForm.value = emptyCampaignForm()
  campaignPreviewToken.value = ''
  campaignPreview.value = null
}

async function loadCampaignPreset(key: string) {
  if (campaignPresetLoading.value || campaignExecuting.value) return
  campaignPresetLoading.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/world-outline/campaigns/presets/' + encodeURIComponent(key))
    if (data.code !== 200) throw new Error(data.message || '战役方案读取失败')
    const preset = data.data
    const worlds = new Map<string, { district?: string; min_level: number; setting: string; challenge?: CampaignChallenge }>(
      preset.isolated_world.dungeons.map((entry: any) => [entry.name, entry]))
    campaignForm.value = {
      ...emptyCampaignForm(), isolated_world: true, dispatch_enabled: true, staged: true,
      max_active_teams: preset.isolated_world.max_active_teams,
      campaign_key: preset.campaign_key, title: preset.spine.title, summary: preset.spine.summary,
      agenda: preset.spine.campaign_agenda, power_profile: preset.spine.power_profile,
      stage_plan: preset.stage_plan,
      dungeon_fronts: preset.fronts.map((front: any) => {
        const world = worlds.get(front.base_dungeon)!
        return { dungeon: front.base_dungeon, title: front.title, summary: front.summary,
          agenda: front.campaign_agenda, district: world.district || '', min_level: world.min_level,
          setting: world.setting, challenge: world.challenge }
      }),
    }
    campaignPreview.value = null
    campaignPreviewToken.value = ''
    message.success('已载入百胃宴城方案，可编辑后生成创建预览')
  } catch (error: any) {
    message.error(error?.response?.data?.message || error.message || '战役方案读取失败')
  } finally {
    campaignPresetLoading.value = false
  }
}

function buildCampaignPayload(): { payload?: Record<string, unknown>; error?: string } {
  const form = campaignForm.value
  if (form.isolated_world && (!Number.isInteger(form.max_active_teams) || form.max_active_teams < 1
      || form.max_active_teams > Math.min(campaignMaxActiveTeams.value, form.dungeon_fronts.length))) {
    return { error: '同时行动小队数不能超过现场数与活动容量上限' }
  }
  if (form.isolated_world && form.max_active_teams > 1 && !form.staged) {
    return { error: '多队同时行动需要配置阶段路线' }
  }
  const campaignKey = form.campaign_key.trim()
  const title = form.title.trim()
  const summary = form.summary.trim()
  const agenda = form.agenda.trim()
  const powerProfile = form.power_profile.trim()
  const relatedDungeons = normalizeCampaignStrings(form.isolated_world
    ? form.dungeon_fronts.map(front => front.dungeon) : form.related_dungeons)
  if (!campaignKey || !title || !summary || !agenda || !powerProfile) {
    return { error: '请完整填写战役键、标题、共同事实、酒馆目标和力量边界' }
  }
  if (!relatedDungeons.length) return { error: '请至少选择一个关联副本' }
  if (!form.dispatch_enabled && !form.faction_fronts.length) return { error: '请至少配置一个势力前线' }
  if (!form.dungeon_fronts.length) return { error: '请至少配置一个副本前线' }

  const factionFronts = (form.isolated_world ? [] : form.faction_fronts).map((front, index) => {
    const profileId = Number(front.profile_id)
    const scopeDungeons = normalizeCampaignStrings(front.scope_dungeons)
    if (!Number.isInteger(profileId) || profileId <= 0 || !front.title.trim() || !front.summary.trim() || !front.agenda.trim()) {
      throw new Error(`请完整填写第 ${index + 1} 个势力前线`)
    }
    if (!scopeDungeons.length || scopeDungeons.some(dungeon => !relatedDungeons.includes(dungeon))) {
      throw new Error(`第 ${index + 1} 个势力前线必须选择至少一个已关联副本`)
    }
    if (scopeDungeons.length + (front.include_tavern ? 1 : 0) > campaignMaxScopesPerFront.value) {
      throw new Error(`第 ${index + 1} 个势力前线的副本与酒馆作用域超过上限`)
    }
    return {
      profile_id: profileId,
      title: front.title.trim(),
      summary: front.summary.trim(),
      agenda: front.agenda.trim(),
      scope_dungeons: scopeDungeons,
      include_tavern: !!front.include_tavern,
    }
  })
  if (new Set(factionFronts.map(front => front.profile_id)).size !== factionFronts.length) {
    return { error: '同一势力档案不能重复建立前线' }
  }

  const dungeonFronts = form.dungeon_fronts.map((front, index) => {
    const dungeon = front.dungeon.trim()
    if (!dungeon || !relatedDungeons.includes(dungeon) || !front.title.trim() || !front.summary.trim() || !front.agenda.trim()) {
      throw new Error(`请完整填写第 ${index + 1} 个副本前线，并选择已关联副本`)
    }
    return {
      dungeon,
      title: front.title.trim(),
      summary: front.summary.trim(),
      agenda: front.agenda.trim(),
    }
  })
  if (new Set(dungeonFronts.map(front => front.dungeon)).size !== dungeonFronts.length) {
    return { error: '同一副本不能重复建立副本前线' }
  }

  const activityDungeons = form.isolated_world ? form.dungeon_fronts.map((front, index) => {
    const tier = campaignActivityTiers.value.find(item => item.min_level === front.min_level)
    if (!tier || !front.setting.trim()) throw new Error(`请填写第 ${index + 1} 个新副本的背景与难度`)
    return { name: front.dungeon.trim(), ...tier, setting: front.setting.trim(),
      ...(front.challenge ? { challenge: front.challenge } : {}),
      ...(front.district.trim() ? { district: front.district.trim() } : {}) }
  }) : null

  return {
    payload: {
      campaign_key: campaignKey,
      dispatch_enabled: form.dispatch_enabled,
      ...(activityDungeons ? { isolated_world: { dungeons: activityDungeons, max_active_teams: form.max_active_teams } } : {}),
      ...(form.dispatch_enabled && form.staged ? { stage_plan: form.stage_plan } : {}),
      spine: {
        title,
        summary,
        campaign_agenda: agenda,
        power_profile: powerProfile,
      },
      fronts: [
        ...factionFronts.map(front => ({
          role: 'faction_front',
          title: front.title,
          summary: front.summary,
          campaign_agenda: front.agenda,
          base_dungeon: front.scope_dungeons[0],
          scope_keys: front.scope_dungeons,
          faction_profile_ids: [front.profile_id],
          tavern_interaction: front.include_tavern,
        })),
        ...dungeonFronts.map(front => ({
          role: 'dungeon_front',
          title: front.title,
          summary: front.summary,
          campaign_agenda: front.agenda,
          base_dungeon: front.dungeon,
          scope_keys: [front.dungeon],
          faction_profile_ids: [],
        })),
      ],
    },
  }
}

async function previewCampaign() {
  let built: { payload?: Record<string, unknown>; error?: string }
  try {
    built = buildCampaignPayload()
  } catch (error: any) {
    message.warning(error?.message || '共同战役表单不完整')
    return
  }
  if (!built.payload) {
    message.warning(built.error || '共同战役表单不完整')
    return
  }
  campaignPreviewing.value = true
  try {
    const { data } = await api.post('/adventurer-s1/admin/world-outline/campaigns/preview', built.payload)
    if (data.code !== 200 || !data.data) {
      message.error(data.message || '共同战役预览失败')
      return
    }
    const token = String(data.data.preview_token || '')
    campaignPreviewToken.value = token
    campaignPreview.value = data.data?.preview ?? data.data
    if (token) message.success('预览已生成；请核对所有成员后再确认创建')
    else message.warning('预览已生成，但存在阻止执行的冲突；请按预览提示修改后重试')
  } finally {
    campaignPreviewing.value = false
  }
}

async function executeCampaign() {
  if (!campaignPreviewToken.value) {
    message.warning('请先生成有效预览')
    return
  }
  if (!window.confirm(`确认按当前预览创建共同战役“${campaignForm.value.title.trim()}”？${campaignForm.value.isolated_world ? '新活动将立即公开，玩家通过议事厅进入全新的地图。' : '创建后各成员将进入现有世界线体系。'}`)) return
  campaignExecuting.value = true
  try {
    const { data } = await api.post('/adventurer-s1/admin/world-outline/campaigns', {
      preview_token: campaignPreviewToken.value,
    })
    if (data.code !== 200 && data.code !== 201) {
      message.error(data.message || '共同战役创建失败')
      return
    }
    message.success('共同战役已创建')
    resetCampaignForm()
    await loadCampaignWorkspace()
  } finally {
    campaignExecuting.value = false
  }
}

function beginCampaignAgendaEdit(member: CampaignMemberRow) {
  editingCampaignAgendaThreadId.value = member.id
  campaignAgendaDraft.value = campaignMemberAgenda(member)
}

function cancelCampaignAgendaEdit() {
  editingCampaignAgendaThreadId.value = null
  campaignAgendaDraft.value = ''
}

async function enableCampaignDispatch(row: CampaignRow) {
  const root = row.spine || campaignMembers(row).find(item => (item.campaign_role || item.role) === 'spine')
  if (!root?.id || campaignExecuting.value) return
  campaignExecuting.value = true
  try {
    const { data } = await api.post('/adventurer-s1/admin/world-outline/campaigns/' + root.id + '/dispatch')
    if (data.code !== 200) throw new Error(data.message || '启用失败')
    message.success('已启用议事厅推进，等待队伍发现')
    await loadCampaigns()
  } catch (error: any) {
    message.error(error?.response?.data?.message || error.message || '启用失败')
  } finally {
    campaignExecuting.value = false
  }
}

async function retryCampaignPublication(row: CampaignRow) {
  const root = row.spine || campaignMembers(row).find(item => (item.campaign_role || item.role) === 'spine')
  if (!root?.id || campaignExecuting.value) return
  campaignExecuting.value = true
  try {
    const { data } = await api.post('/adventurer-s1/admin/world-outline/campaigns/' + root.id + '/dispatch/retry')
    if (data.code !== 200) throw new Error(data.message || '重试提交失败')
    message.success('已重新排入世界维护队列')
    await loadCampaigns()
  } catch (error: any) {
    message.error(error?.response?.data?.message || error.message || '重试提交失败')
  } finally {
    campaignExecuting.value = false
  }
}

async function saveCampaignAgenda(member: CampaignMemberRow) {
  const revision = Number(member.revision)
  const agenda = campaignAgendaDraft.value.trim()
  if (!Number.isInteger(revision) || revision <= 0 || !agenda) {
    message.warning('议程不能为空，且成员必须带有有效 revision')
    return
  }
  campaignAgendaSaving.value = true
  try {
    const { data } = await api.patch(`/adventurer-s1/admin/world-outline/campaigns/threads/${member.id}/agenda`, {
      expected_revision: revision,
      campaign_agenda: agenda,
    })
    if (data.code !== 200) {
      message.error(data.message || '成员议程更新失败')
      return
    }
    message.success('成员议程已更新')
    cancelCampaignAgendaEdit()
    await loadCampaigns()
  } finally {
    campaignAgendaSaving.value = false
  }
}

async function refreshAll() {
  if (activeWorkspace.value === 'intel') await loadIntel(intelPage.value)
  else if (activeWorkspace.value === 'campaigns') await loadCampaignWorkspace()
  else if (activeWorkspace.value === 'actors') await loadActors(actorPage.value)
  else if (activeWorkspace.value === 'pipeline' && activePipelineView.value === 'handoffs') await loadHandoffStates(handoffPage.value)
  else if (activeWorkspace.value === 'pipeline') await loadCandidates(page.value)
}

function splitList(text: string, max: number) {
  return text.split(/[，,]/).map(s => s.trim()).filter(Boolean).slice(0, max)
}

function startCreateIntel() {
  intelForm.value = emptyIntelForm()
  editingIntel.value = true
}

function startEditIntel(row: IntelRow) {
  if (row.campaign_role) {
    message.warning('共同战役成员只能在“共同战役”工作区修改议程')
    activeWorkspace.value = 'campaigns'
    return
  }
  intelForm.value = {
    id: row.id,
    title: row.title,
    thread_type: row.thread_type || 'event',
    status: row.status || 'active',
    weight: row.weight || 50,
    base_dungeon: row.base_dungeon || '',
    scope_keys_text: (row.scope_keys || []).join(', '),
    trigger_keywords_text: (row.trigger_keywords || []).join(', '),
    summary: row.summary || '',
    next_reveal_hint: row.next_reveal_hint || '',
    power_tier: row.power_tier || '',
    power_profile: row.power_profile || '',
  }
  editingIntel.value = true
}

function cancelEditIntel() {
  editingIntel.value = false
  intelForm.value = emptyIntelForm()
}

function clearGenerator() {
  generatorBrief.value = ''
  generatorType.value = ''
  generatorDungeons.value = []
  generatorAllDungeons.value = false
}

async function loadGeneratorDungeonOptions() {
  if (generatorDungeonOptions.value.length || generatorDungeonsLoading.value) return
  generatorDungeonsLoading.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/items/dungeon-list')
    generatorDungeonOptions.value = Array.isArray(data?.data)
      ? data.data.filter((item: unknown): item is string => typeof item === 'string' && item.trim().length > 0)
      : []
  } catch (error: any) {
    message.error(error?.response?.data?.message || '加载地牢选项失败')
  } finally {
    generatorDungeonsLoading.value = false
  }
}

function toggleGeneratorAllDungeons() {
  generatorAllDungeons.value = !generatorAllDungeons.value
  if (generatorAllDungeons.value) generatorDungeons.value = []
}

function toggleGeneratorDungeon(dungeon: string) {
  if (generatorAllDungeons.value) return
  const selected = generatorDungeons.value
  if (selected.includes(dungeon)) {
    generatorDungeons.value = selected.filter(item => item !== dungeon)
    return
  }
  if (selected.length >= 8) {
    message.warning('最多选择 8 个地牢；如需全局生效请选择“全部地牢”')
    return
  }
  generatorDungeons.value = [...selected, dungeon]
}

async function generateIntelDraft() {
  const brief = generatorBrief.value.trim()
  if (!brief) {
    message.warning('请输入世界情报概述')
    return
  }
  generatingIntel.value = true
  try {
    const { data } = await api.post('/adventurer-s1/admin/world-outline/intel/generate', {
      brief,
      preferred_type: generatorType.value,
      initial_dungeons: generatorDungeons.value,
      all_dungeons: generatorAllDungeons.value,
    }, {
      timeout: WORLD_INTEL_GENERATION_TIMEOUT_MS,
    })
    if (data.code !== 200 || !data.data) {
      message.error(data.message || '生成世界情报草稿失败')
      return
    }
    const draft = data.data as Partial<IntelRow>
    intelForm.value = {
      id: null,
      title: draft.title || '',
      thread_type: draft.thread_type || 'event',
      status: draft.status || 'seeded',
      weight: draft.weight || 60,
      base_dungeon: draft.base_dungeon || '',
      scope_keys_text: (draft.scope_keys || []).join(', '),
      trigger_keywords_text: (draft.trigger_keywords || []).join(', '),
      summary: draft.summary || '',
      next_reveal_hint: draft.next_reveal_hint || '',
      power_tier: draft.power_tier || '',
      power_profile: draft.power_profile || '',
    }
    editingIntel.value = true
    message.success('草稿已载入编辑区，请检查后保存')
  } catch (error: any) {
    const fallback = error?.code === 'ECONNABORTED'
      ? '世界情报生成超时，请稍后重试'
      : '生成世界情报草稿失败'
    message.error(error?.response?.data?.message || fallback)
  } finally {
    generatingIntel.value = false
  }
}

async function saveIntel() {
  savingIntel.value = true
  try {
    const payload = {
      title: intelForm.value.title.trim(),
      thread_type: intelForm.value.thread_type,
      status: intelForm.value.status,
      weight: intelForm.value.weight,
      base_dungeon: intelForm.value.base_dungeon.trim(),
      scope_keys: splitList(intelForm.value.scope_keys_text, 8),
      trigger_keywords: splitList(intelForm.value.trigger_keywords_text, 10),
      summary: intelForm.value.summary.trim(),
      next_reveal_hint: intelForm.value.next_reveal_hint.trim(),
      power_tier: intelForm.value.power_tier.trim(),
      power_profile: intelForm.value.power_profile.trim(),
    }
    const url = intelForm.value.id
      ? `/adventurer-s1/admin/world-outline/intel/${intelForm.value.id}`
      : '/adventurer-s1/admin/world-outline/intel'
    const { data } = intelForm.value.id ? await api.put(url, payload) : await api.post(url, payload)
    if (data.code === 200 || data.code === 201) {
      message.success('情报已保存')
      cancelEditIntel()
      await loadIntel(intelPage.value)
    } else {
      message.error(data.message || '保存情报失败')
    }
  } finally {
    savingIntel.value = false
  }
}

async function deleteIntel(row: IntelRow) {
  if (row.campaign_role) {
    message.warning('共同战役成员不能通过普通世界情报入口删除')
    activeWorkspace.value = 'campaigns'
    return
  }
  if (!window.confirm(`删除世界情报「${row.title}」？后续不会再注入该情报。`)) return
  deletingIntelId.value = row.id
  try {
    const { data } = await api.delete(`/adventurer-s1/admin/world-outline/intel/${row.id}`)
    if (data.code === 200) {
      message.success('情报已删除')
      await loadIntel(intelPage.value)
    } else {
      message.error(data.message || '删除情报失败')
    }
  } finally {
    deletingIntelId.value = null
  }
}

async function previewIntel() {
  if (!previewDungeon.value.trim() && !previewContext.value.trim()) {
    message.warning('请输入副本名或上下文关键词')
    return
  }
  previewLoading.value = true
  try {
    const { data } = await api.get('/adventurer-s1/admin/world-outline/intel/preview', {
      params: {
        target_dungeon: previewDungeon.value.trim(),
        context: previewContext.value.trim(),
      },
    })
    if (data.code === 200) {
      previewRows.value = data.data?.items || []
      previewBlock.value = data.data?.prompt_block || ''
      previewContextKeywords.value = data.data?.context_keywords || []
      previewContextText.value = data.data?.context_text || ''
      if (!previewRows.value.length) message.info('该副本当前不会注入世界情报')
    } else {
      message.error(data.message || '预览失败')
    }
  } finally {
    previewLoading.value = false
  }
}

async function submitMaintain() {
  maintaining.value = true
  try {
    const { data } = await api.post('/adventurer-s1/admin/world-outline/maintain', {})
    if (data.code === 200 && data.data?.submitted) {
      message.success('已提交异步大纲维护任务')
    } else {
      message.warning(data.message || '任务可能已在队列中')
    }
  } finally {
    maintaining.value = false
  }
}

async function deleteCandidate(row: CandidateRow) {
  if (!window.confirm(`删除候选 #${row.id}？已处理候选若已合并进接力便签，需要另行删除对应便签。`)) return
  deletingCandidateId.value = row.id
  try {
    const { data } = await api.delete(`/adventurer-s1/admin/world-outline/candidates/${row.id}`)
    if (data.code === 200) {
      message.success('候选已删除')
      await loadCandidates(page.value)
    } else {
      message.error(data.message || '删除候选失败')
    }
  } finally {
    deletingCandidateId.value = null
  }
}

async function deleteHandoff(row: HandoffRow) {
  if (!window.confirm(`删除「${row.target_dungeon}」当前 DM 接力便签？删除后该便签不会再注入后续探险。`)) return
  deletingHandoffId.value = row.id
  try {
    const { data } = await api.delete(`/adventurer-s1/admin/world-outline/dm-handoff/${row.id}`)
    if (data.code === 200) {
      message.success('接力便签已删除')
      await loadHandoffStates(handoffPage.value)
    } else {
      message.error(data.message || '删除接力便签失败')
    }
  } finally {
    deletingHandoffId.value = null
  }
}

const loadedWorkspaces = new Set<string>()

async function ensureWorkspaceLoaded() {
  const key = activeWorkspace.value === 'pipeline'
    ? `pipeline:${activePipelineView.value}`
    : activeWorkspace.value
  if (loadedWorkspaces.has(key) || activeWorkspace.value === 'preview') return
  loadedWorkspaces.add(key)
  try {
    await refreshAll()
  } catch {
    loadedWorkspaces.delete(key)
  }
}

watch(activeWorkspace, () => { void ensureWorkspaceLoaded() })
watch(activePipelineView, () => {
  if (activeWorkspace.value === 'pipeline') void ensureWorkspaceLoaded()
})
watch(generatorOpen, (open) => {
  if (open) void loadGeneratorDungeonOptions()
})
watch(campaignForm, () => {
  if (!campaignPreviewToken.value && campaignPreview.value === null) return
  campaignPreviewToken.value = ''
  campaignPreview.value = null
}, { deep: true })

onMounted(() => { void ensureWorkspaceLoaded() })
</script>

<style scoped>
.world-outline-admin { display: flex; flex-direction: column; gap: 14px; }
.world-admin-actions { display: flex; flex-wrap: wrap; justify-content: flex-end; gap: 8px; }
.workspace-tabs { display: grid; grid-template-columns: repeat(auto-fit, minmax(138px, 1fr)); gap: 8px; }
.workspace-tab { display: grid; grid-template-columns: auto minmax(0, 1fr) auto; align-items: center; gap: 9px; min-width: 0; padding: 10px 11px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-bg); color: var(--color-text); text-align: left; cursor: pointer; transition: border-color 0.15s, background 0.15s, transform 0.15s; }
.workspace-tab:hover { border-color: var(--color-primary); transform: translateY(-1px); }
.workspace-tab.active { border-color: var(--color-primary); background: color-mix(in srgb, var(--color-primary) 10%, var(--color-bg)); box-shadow: inset 0 -2px 0 var(--color-primary); }
.workspace-tab > span:nth-child(2) { display: grid; min-width: 0; gap: 2px; }
.workspace-tab strong { font-size: 13px; }
.workspace-tab small { overflow: hidden; color: var(--color-text-muted); font-size: 10px; text-overflow: ellipsis; white-space: nowrap; }
.workspace-icon { font-size: 18px; }
.workspace-count { min-width: 22px; padding: 2px 6px; border-radius: 999px; background: var(--color-surface-hover); color: var(--color-text-muted); font-size: 10px; text-align: center; }
.workspace-heading { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; padding: 2px 2px 0; }
.workspace-heading h6 { margin: 2px 0 3px; font-size: 16px; }
.workspace-heading p { margin: 0; color: var(--color-text-muted); font-size: 12px; }
.workspace-kicker { color: var(--color-primary); font-size: 9px; font-weight: 800; letter-spacing: 0.14em; }
.pipeline-tabs { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; padding: 5px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-bg); }
.pipeline-tab { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; padding: 9px 12px; border: 1px solid transparent; border-radius: 8px; background: transparent; color: var(--color-text-muted); cursor: pointer; }
.pipeline-tab:hover { color: var(--color-text); background: var(--color-surface-hover); }
.pipeline-tab.active { border-color: var(--color-border); background: var(--color-surface); color: var(--color-text); box-shadow: 0 2px 7px rgba(15, 23, 42, 0.05); }
.pipeline-tab strong { min-width: 24px; padding: 2px 7px; border-radius: 999px; background: var(--color-surface-hover); font-size: 11px; text-align: center; }
.toolbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.compact-toolbar { margin-bottom: 8px; }
.filter-input, .filter-select { border: 1px solid var(--color-border); border-radius: 8px; padding: 6px 8px; background: var(--color-surface); color: var(--color-text); }
.filter-input { min-width: 180px; }
.wide-input { min-width: min(420px, 100%); flex: 1 1 320px; }
.admin-hint, .muted { color: var(--color-text-muted); }
.section-block { border: 1px solid var(--color-border); border-radius: 12px; padding: 12px; background: var(--color-surface); }
.handoff-grid, .candidate-list, .intel-list, .actor-list { display: grid; gap: 10px; }
.handoff-card, .candidate-card, .intel-card, .actor-card { border: 1px solid var(--color-border); border-radius: 10px; padding: 10px; background: var(--color-bg); }
.handoff-card header, .candidate-card header, .intel-card header, .actor-card > header, .relation-card header, .meta-row, .pager-row { display: flex; flex-wrap: wrap; gap: 10px; align-items: center; justify-content: space-between; }
.header-actions { display: inline-flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.btn-danger { border: 1px solid rgba(197, 48, 48, 0.45); border-radius: 8px; background: rgba(197, 48, 48, 0.08); color: #c53030; cursor: pointer; }
.btn-danger:disabled { opacity: 0.6; cursor: not-allowed; }
.btn-xs { padding: 2px 7px; font-size: 12px; }
.meta-row { justify-content: flex-start; color: var(--color-text-muted); font-size: 12px; }
.status-tag { border-radius: 999px; padding: 2px 8px; font-size: 12px; background: var(--color-surface-hover); }
.status-tag.pending { color: #b7791f; }
.status-tag.processed { color: #2f855a; }
.status-tag.failed { color: #c53030; }
.note-text { white-space: pre-wrap; }
.json-preview { max-height: 220px; overflow: auto; padding: 8px; border-radius: 8px; background: rgba(0, 0, 0, 0.08); font-size: 12px; white-space: pre-wrap; }
.error-text { color: #c53030; }
.empty { padding: 12px; color: var(--color-text-muted); }
.intel-meter { color: var(--color-text-muted); font-size: 12px; margin-bottom: 8px; }
.intel-generator { display: grid; gap: 9px; margin: 10px 0; padding: 10px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-bg); }
.generator-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
.generator-heading > div { display: grid; gap: 2px; min-width: 0; }
.generator-heading small { color: var(--color-text-muted); }
.generator-body { display: grid; gap: 8px; padding-top: 8px; border-top: 1px dashed var(--color-border); }
.generator-body label { display: grid; gap: 4px; color: var(--color-text-muted); font-size: 13px; }
.generator-body input, .generator-body select, .generator-body textarea { box-sizing: border-box; width: 100%; border: 1px solid var(--color-border); border-radius: 8px; padding: 6px 8px; background: var(--color-surface); color: var(--color-text); }
.generator-hint { margin: 0; font-size: 12px; }
.dungeon-picker-field { display: grid; gap: 6px; min-width: 0; color: var(--color-text-muted); font-size: 13px; }
.dungeon-picker-actions { display: flex; align-items: center; gap: 8px; min-width: 0; }
.dungeon-picker-actions small { color: var(--color-text-muted); }
.dungeon-option-grid { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 5px; max-height: 188px; overflow-y: auto; padding: 6px; border: 1px solid var(--color-border); border-radius: 8px; background: var(--color-surface); }
.dungeon-option { min-width: 0; padding: 5px 7px; border: 1px solid var(--color-border); border-radius: 7px; background: var(--color-bg); color: var(--color-text-muted); font-size: 12px; cursor: pointer; overflow-wrap: anywhere; }
.dungeon-option:hover:not(:disabled) { border-color: var(--color-primary); color: var(--color-text); }
.dungeon-option.active { border-color: var(--color-primary); background: color-mix(in srgb, var(--color-primary) 14%, var(--color-bg)); color: var(--color-primary); font-weight: 700; }
.dungeon-option:disabled { cursor: not-allowed; opacity: 0.45; }
.dungeon-option-all { flex: 0 0 auto; padding-inline: 12px; }
.dungeon-picker-empty { padding: 12px; border: 1px dashed var(--color-border); border-radius: 8px; color: var(--color-text-muted); text-align: center; }
.intel-form { display: grid; gap: 8px; margin: 10px 0; padding: 10px; border: 1px dashed var(--color-border); border-radius: 10px; }
.form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 8px; }
.intel-form label { display: grid; gap: 4px; font-size: 13px; color: var(--color-text-muted); }
.intel-form input, .intel-form select, .intel-form textarea { border: 1px solid var(--color-border); border-radius: 8px; padding: 6px 8px; background: var(--color-surface); color: var(--color-text); }
.form-actions { display: flex; gap: 8px; }
.campaign-section { display: grid; gap: 12px; }
.campaign-heading, .campaign-list-heading { display: flex; flex-wrap: wrap; align-items: flex-start; justify-content: space-between; gap: 10px; }
.campaign-heading h6, .campaign-list-heading h6 { margin: 0; }
.campaign-heading p { margin: 4px 0 0; max-width: 820px; }
.campaign-form { display: grid; gap: 10px; padding: 12px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-bg); }
.campaign-form > label, .campaign-front-card > label, .campaign-form .form-grid label { display: grid; gap: 4px; color: var(--color-text-muted); font-size: 13px; }
.campaign-form input, .campaign-form select, .campaign-form textarea, .campaign-agenda-editor textarea { box-sizing: border-box; width: 100%; border: 1px solid var(--color-border); border-radius: 8px; padding: 7px 8px; background: var(--color-surface); color: var(--color-text); }
.campaign-fieldset { display: grid; min-width: 0; gap: 9px; margin: 0; padding: 11px; border: 1px dashed var(--color-border); border-radius: 10px; }
.campaign-fieldset > legend { padding: 0 5px; font-weight: 700; }
.campaign-fieldset-heading { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.campaign-legend { font-weight: 700; }
.campaign-option-grid { max-height: 168px; }
.campaign-front-card, .campaign-card, .campaign-member-card, .campaign-preview-card { display: grid; gap: 8px; padding: 10px; border: 1px solid var(--color-border); border-radius: 10px; background: var(--color-surface); }
.campaign-front-card > header, .campaign-card > header, .campaign-member-card > header { display: flex; flex-wrap: wrap; align-items: center; justify-content: space-between; gap: 8px; }
.campaign-card > header > div, .campaign-member-card > header > div { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
.campaign-checkbox { display: flex !important; align-items: flex-start; }
.campaign-checkbox input { width: auto; margin-top: 2px; }
.campaign-scope-picker { display: grid; gap: 6px; }
.campaign-scope-picker > strong { color: var(--color-text-muted); font-size: 12px; }
.campaign-preview-card { border-color: color-mix(in srgb, var(--color-primary) 55%, var(--color-border)); background: color-mix(in srgb, var(--color-primary) 5%, var(--color-surface)); }
.campaign-preview-json { max-height: 360px; }
.campaign-list, .campaign-member-list { display: grid; gap: 10px; }
.campaign-member-list { margin-top: 4px; padding-top: 10px; border-top: 1px dashed var(--color-border); }
.campaign-member-card { background: var(--color-bg); }
.campaign-fact { display: grid; grid-template-columns: minmax(82px, auto) minmax(0, 1fr) auto; align-items: start; gap: 8px; }
.campaign-fact strong { color: var(--color-text-muted); font-size: 12px; }
.campaign-fact span { min-width: 0; white-space: pre-wrap; }
.campaign-agenda-editor { display: grid; gap: 7px; }
.tag-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
.tag-row span { border-radius: 999px; padding: 2px 7px; background: var(--color-surface-hover); color: var(--color-text-muted); font-size: 12px; }
.preview-card { gap: 6px; }
.match-detail-panel { display: grid; gap: 10px; margin: 8px 0 10px; padding: 10px; border: 1px dashed var(--color-border); border-radius: 10px; background: var(--color-bg); }
.detail-block { display: grid; gap: 6px; }
.detail-tags { margin-top: 0; }
.match-context { max-height: 140px; margin: 0; }
.match-reason-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 8px; margin-top: 8px; padding-top: 8px; border-top: 1px dashed var(--color-border); font-size: 12px; }
.match-reason-grid div { display: grid; gap: 3px; }
.match-reason-grid strong { color: var(--color-text-muted); font-weight: 600; }
.actor-card > header > div { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.actor-summary-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 8px; }
.actor-summary-grid div { display: grid; gap: 3px; padding: 8px; border-radius: 8px; background: var(--color-surface); }
.actor-summary-grid strong, .detail-block > strong { color: var(--color-text-muted); font-size: 12px; }
.actor-detail { display: grid; gap: 12px; margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--color-border); }
.relation-card { border: 1px solid var(--color-border); border-radius: 8px; padding: 9px; background: var(--color-surface); }
.relation-grid { display: grid; grid-template-columns: minmax(90px, 130px) 1fr; gap: 5px 10px; margin: 8px 0 0; }
.relation-grid dt { color: var(--color-text-muted); font-size: 12px; }
.relation-grid dd { margin: 0; white-space: pre-wrap; }
.thread-link-list { display: flex; flex-wrap: wrap; gap: 6px; }
.thread-link-list span { border-radius: 8px; padding: 5px 8px; background: var(--color-surface-hover); font-size: 12px; }
.interaction-row { padding: 8px 0; border-bottom: 1px dashed var(--color-border); }
.interaction-row:last-child { border-bottom: 0; }
.interaction-row p { margin: 5px 0; }
.interaction-row small { display: block; margin-top: 3px; }
.compact-empty { padding: 5px 0; }
@media (max-width: 640px) {
  .world-admin-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .world-admin-actions button { min-width: 0; min-height: 40px; }
  .workspace-tabs { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 7px; }
  .workspace-tab { grid-template-columns: auto minmax(0, 1fr) auto; min-width: 0; padding: 9px; }
  .workspace-tab small { display: none; }
  .workspace-icon { font-size: 16px; }
  .workspace-heading { align-items: flex-start; }
  .workspace-heading p { line-height: 1.5; }
  .pipeline-tabs { grid-template-columns: 1fr; }
  .section-block { padding: 10px; }
  .compact-toolbar { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .compact-toolbar > * { width: 100%; min-width: 0; min-height: 40px; box-sizing: border-box; }
  .compact-toolbar .wide-input,
  .compact-toolbar .filter-input:first-child { grid-column: 1 / -1; }
  .form-grid { grid-template-columns: minmax(0, 1fr); }
  .dungeon-option-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .form-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .form-actions button { min-height: 40px; }
  .campaign-heading { display: grid; grid-template-columns: minmax(0, 1fr); }
  .campaign-heading > button { width: 100%; min-height: 40px; }
  .campaign-fieldset-heading { align-items: stretch; }
  .campaign-fieldset-heading button { width: 100%; min-height: 40px; }
  .campaign-fact { grid-template-columns: minmax(0, 1fr); }
  .campaign-fact button { min-height: 38px; }
  .generator-heading { align-items: flex-start; }
  .handoff-card header,
  .candidate-card header,
  .intel-card header,
  .actor-card > header { align-items: flex-start; }
  .header-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); width: 100%; }
  .header-actions button { min-width: 0; min-height: 38px; }
  .actor-summary-grid,
  .match-reason-grid { grid-template-columns: minmax(0, 1fr); }
  .pager-row { display: grid; grid-template-columns: 1fr auto 1fr; }
  .pager-row button:last-child { justify-self: end; }
  .json-preview { max-width: 100%; overflow-wrap: anywhere; }
  .relation-grid { grid-template-columns: 1fr; }
  .relation-grid dd { margin-bottom: 5px; }
}
</style>
