<template>
  <div class="skills-page">
    <!-- ===== 顶部 Header ===== -->
    <div class="header">
      <h2>Skills 库</h2>
      <div class="header-actions">
        <button class="btn-ghost" @click="openAllowlist">治理配置</button>
        <button class="btn-primary" @click="openNew">+ 新建 Skill</button>
        <label class="btn-secondary zip-import-label" title="导入来自 skillsmp 的 .zip 文件">
          📦 导入 ZIP
          <input ref="zipInputRef" type="file" accept=".zip" class="hidden-file-input" @change="onZipChange" />
        </label>
      </div>
    </div>

    <!-- ===== Body：左侧列表 + 右侧主面板 ===== -->
    <div class="body">
      <!-- 左侧：技能列表 -->
      <div class="list-panel" :class="{ collapsed: listCollapsed }">
        <div v-if="loading" class="muted p16">加载中…</div>
        <div v-else-if="skills.length === 0" class="muted p16">
          还没有 Skill。用 Creator 创建第一个吧！
        </div>
        <template v-else>
          <div v-if="!listCollapsed" class="list-header">技能列表（{{ skills.length }}）</div>
          <div
            v-for="sk in skills"
            :key="sk.name"
            class="skill-item"
            :class="{ active: selected?.name === sk.name }"
            @click="select(sk.name)"
            :title="sk.display_name || sk.name"
          >
            <div class="sk-icon">{{ iconForSkill(sk) }}</div>
            <div v-if="!listCollapsed" class="sk-content">
              <div class="sk-name">{{ sk.display_name || sk.name }}</div>
              <div class="sk-meta muted">v{{ sk.version || '0.1.0' }} · {{ sk.scope }} · {{ sk.status }}</div>
              <div class="sk-desc muted">{{ sk.description || '暂无描述' }}</div>
            </div>
          </div>
        </template>
        <button class="list-collapse-btn" @click="listCollapsed = !listCollapsed" :title="listCollapsed ? '展开技能列表' : '折叠技能列表'">
          <span class="list-collapse-icon" :class="{ flipped: listCollapsed }">◀</span>
        </button>
      </div>

      <!-- 右侧：主面板 -->
      <div class="main-panel">
        <div v-if="!selected && !editing" class="muted empty-hint">← 选择一个 Skill 查看详情或测试</div>

        <!-- ===== 编辑态 ===== -->
        <template v-if="editing">
          <div class="detail-header">
            <input v-model="editName" placeholder="skill-name" style="max-width:220px;" />
            <button class="btn-primary" @click="save" :disabled="saving">{{ saving ? '保存中…' : '保存' }}</button>
            <button class="btn-ghost" @click="cancelEdit">取消</button>
          </div>
          <div v-if="editError" class="error px16">{{ editError }}</div>
          <textarea v-model="editContent" class="skill-editor" spellcheck="false" />
        </template>

        <!-- ===== 查看态：上下分割布局 ===== -->
        <template v-else-if="selected">
          <!-- 技能标题栏 -->
          <div class="detail-header">
            <div class="header-left">
              <span class="detail-title">{{ selected.display_name || selected.name }}</span>
              <div class="header-badges">
                <span class="gov-badge">{{ selected.scope }}</span>
                <span class="gov-badge">v{{ selected.version || '0.1.0' }}</span>
                <span class="gov-badge" :class="`status-${selected.status}`">{{ selected.status }}</span>
                <span class="gov-badge" :class="{ blocked: !selected.can_execute }">
                  {{ selected.can_execute ? '可执行' : '不可执行' }}
                </span>
              </div>
            </div>
            <div class="header-actions-inline">
              <button class="btn-ghost" @click="startEdit" :disabled="!selected.editable">编辑</button>
              <button class="btn-danger" @click="confirmDelete" :disabled="!selected.editable">删除</button>
            </div>
          </div>

          <!-- 上下分割容器（始终为垂直布局，不受 sidebar 影响 -->
          <div class="split-container">
            <!-- ===== 上区：技能配置 ===== -->
            <div class="config-area" :style="{ height: chatPanelCollapsed ? '100%' : `${splitRatio * 100}%` }">
              <!-- 操作按钮栏（三组分隔） -->
              <div class="action-bar">
                <div class="action-group">
                  <button class="btn-ghost btn-sm" @click="applyStatus('request_review')" :disabled="!selected.editable">提交审批</button>
                  <button class="btn-ghost btn-sm" @click="applyStatus('approve')" :disabled="!selected.editable">批准</button>
                  <button class="btn-ghost btn-sm" @click="applyStatus('reject')" :disabled="!selected.editable">驳回</button>
                </div>
                <div class="action-divider"></div>
                <div class="action-group">
                  <button class="btn-ghost btn-sm" @click="applyStatus('quarantine')" :disabled="!selected.editable">隔离</button>
                  <button class="btn-ghost btn-sm" @click="applyStatus(selected.status === 'disabled' ? 'enable' : 'disable')" :disabled="!selected.editable">
                    {{ selected.status === 'disabled' ? '启用' : '禁用' }}
                  </button>
                  <label class="btn-ghost btn-sm zip-import-label" :class="{ disabled: !selected.editable }">
                    ⬆ 升级 ZIP
                    <input type="file" accept=".zip" class="hidden-file-input" :disabled="!selected.editable" @change="onUpgradeZipChange" />
                  </label>
                  <select v-model="rollbackVersion" class="folder-select" :disabled="!selected.editable || !versions.length">
                    <option value="">回滚版本</option>
                    <option v-for="ver in versions" :key="`${ver.version}-${ver.timestamp}`" :value="ver.version">
                      {{ ver.version }}
                    </option>
                  </select>
                  <button class="btn-ghost btn-sm" @click="doRollback" :disabled="!selected.editable || !rollbackVersion">回滚</button>
                </div>
                <div class="action-divider"></div>
                <div class="action-group">
                  <button class="btn-ghost btn-sm btn-test-entry" @click="expandChatPanel" :disabled="!selected.can_execute">
                    🧪 测试对话
                  </button>
                </div>
              </div>

              <!-- 治理摘要卡 -->
              <div class="governance-grid">
                <div class="governance-card">
                  <div class="governance-title">治理摘要</div>
                  <div class="muted">来源：{{ selected.source?.type || 'directory' }}</div>
                  <div class="muted">安装方式：{{ selected.install_type || 'local' }}</div>
                  <div class="muted">生效作用域：{{ selected.resolved_scope || selected.scope }}</div>
                  <div class="muted">可见作用域：{{ (selected.available_scopes || []).join(', ') || '-' }}</div>
                </div>
                <div class="governance-card">
                  <div class="governance-title">版本历史</div>
                  <div v-if="versions.length" class="governance-list">
                    <div v-for="ve in lastFiveVersions" :key="`${ve.version}-${ve.timestamp}`">
                      v{{ ve.version }} · {{ ve.source_type || 'unknown' }}
                    </div>
                  </div>
                  <div v-else class="muted">暂无版本历史</div>
                </div>
                <div class="governance-card">
                  <div class="governance-title">最近事件</div>
                  <div v-if="events.length" class="governance-list">
                    <div v-for="event in events.slice(0, 5)" :key="event.id">
                      {{ event.type }} · {{ new Date(event.timestamp).toLocaleString() }}
                    </div>
                  </div>
                  <div v-else class="muted">暂无事件</div>
                </div>
              </div>

              <!-- SKILL.md 原文（可折叠） -->
              <CollapsiblePanel v-model:open="showSkillMd" title="SKILL.md" description="技能定义文件">
                <pre class="skill-preview">{{ selected.content }}</pre>
              </CollapsiblePanel>

              <!-- 资产文件 -->
              <CollapsiblePanel v-model:open="showAssets" title="资产文件" :badge="totalAssetCount || ''">
                <div v-for="folder in assetFolders" :key="folder" class="asset-group">
                  <div class="asset-group-title">{{ folder }}</div>
                  <div v-if="assets[folder] && assets[folder].length" class="asset-list">
                    <div v-for="fname in assets[folder]" :key="fname" class="asset-row">
                      <span class="asset-name">{{ fname }}</span>
                      <button class="btn-icon-edit" :disabled="!selected.editable" @click="openAssetEditor(folder, fname)" title="编辑">✎</button>
                      <button class="btn-icon-danger" :disabled="!selected.editable" @click="removeAsset(folder, fname)" title="删除">✕</button>
                    </div>
                  </div>
                  <div v-else class="muted asset-empty">暂无文件</div>
                </div>
                <div class="upload-row">
                  <select v-model="uploadFolder" class="folder-select" aria-label="上传目录">
                    <option v-for="f in assetFolders" :key="f" :value="f">{{ f }}</option>
                  </select>
                  <label class="file-input-label">
                    <input ref="fileInputRef" type="file" class="file-input" aria-label="选择要上传的文件" @change="onFileChange" />
                  </label>
                  <button class="btn-primary btn-sm" :disabled="!uploadFile || uploading || !selected.editable" @click="doUpload">
                    {{ uploading ? '上传中…' : '上传' }}
                  </button>
                </div>
                <div v-if="uploadError" class="error px16">{{ uploadError }}</div>
                <div v-if="assetError" class="error px16">{{ assetError }}</div>
              </CollapsiblePanel>
            </div>

            <!-- ===== 可拖拽分割线 ===== -->
            <div
              v-if="!chatPanelCollapsed"
              class="resize-handle"
              @mousedown="startDrag"
            >
              <div class="resize-line"></div>
            </div>

            <!-- ===== 下区：测试对话面板 ===== -->
            <div
              v-if="!chatPanelCollapsed"
              class="chat-panel"
              :style="{ height: `${(1 - splitRatio) * 100}%` }"
            >
              <!-- 左侧：对话主区（toolbar + messages + input） -->
              <div class="chat-main">
              <!-- 对话面板顶部工具栏 -->
              <div class="chat-toolbar" :class="{ 'refine-mode': chatMode === 'refine' }">
                <span class="chat-toolbar-title">
                  {{ chatMode === 'refine' ? '🔧 修改模式' : '技能对话测试' }}
                </span>
                <div class="chat-toolbar-actions">
                  <button class="btn-text" @click="clearChat" :disabled="streaming || chatMessages.length === 0">清空对话</button>
                  <button class="btn-text" @click="resetChat" :disabled="streaming">重置会话</button>
                  <button class="btn-text" @click="exportChat" :disabled="chatMessages.length === 0">导出对话</button>
                </div>
                <div class="chat-toolbar-right">
                  <button
                    v-if="chatMode === 'refine'"
                    class="btn-ghost btn-sm"
                    @click="switchToSandboxMode"
                    :disabled="streaming"
                    title="返回测试模式"
                  >◀ 返回测试</button>
                  <button
                    v-else
                    class="btn-ghost btn-sm"
                    @click="switchToRefineMode"
                    :disabled="streaming || chatMessages.length === 0"
                    title="根据测试反馈修改技能并保存新版本"
                  >🔧 修改技能</button>
                  <button
                    class="btn-ghost btn-sm"
                    :class="{ active: showThoughts }"
                    @click="showThoughts = !showThoughts"
                    :disabled="thoughts.length === 0 && !showThoughts"
                    title="显示/隐藏执行过程"
                  >🔍 执行过程{{ thoughts.length ? ` (${thoughts.length})` : '' }}</button>
                  <button
                    v-if="currentPlanPreview || currentSOP"
                    class="btn-ghost btn-sm"
                    :class="{ active: showPlanPanel }"
                    @click="showPlanPanel = !showPlanPanel"
                  >📋 方案{{ currentPlanPreview ? ' (待确认)' : '' }}</button>
                  <button class="btn-ghost btn-sm" @click="chatFileInputEl.click()" :disabled="streaming || uploading" title="上传文件">📎 上传</button>
                  <button class="btn-ghost btn-sm" @click="chatPanelCollapsed = true" title="折叠面板">⬇ 折叠</button>
                </div>
              </div>

              <!-- 不可执行提示 -->
              <div v-if="!selected.can_execute" class="warning-banner">
                ⚠️ 当前技能状态为 <strong>{{ selected.status }}</strong>，不允许在沙盒执行。
                <template v-if="selected.status === 'disabled'">请先启用技能后再测试。</template>
                <template v-else-if="selected.status === 'quarantine'">该技能已被隔离，请联系管理员处理。</template>
                <template v-else-if="selected.status === 'rejected'">该技能已被驳回，请修改后重新提交审批。</template>
              </div>

              <!-- 对话历史区 -->
              <div
                class="chat-messages"
                ref="chatMessagesEl"
                @dragover.prevent="onDragOver"
                @dragleave="onDragLeave"
                @drop.prevent="onDrop"
              >
                <!-- 拖拽浮层 -->
                <div v-if="isDragging" class="drag-overlay">
                  <div class="drag-overlay-content">释放文件上传</div>
                </div>

                <!-- 空状态 -->
                <div v-if="chatMessages.length === 0 && !streaming" class="empty-chat muted">
                  <div class="empty-chat-icon">💬</div>
                  <p>向 <strong>{{ selected.name }}</strong> 发送一条消息，开始测试它的行为。</p>
                  <p class="hint">💡 技能会根据你的指令生成计划并调用 scripts 下的脚本。你可以通过下方上传文件供脚本读取。</p>
                </div>

                <template v-for="(msg, i) in chatMessages" :key="i">
                  <!-- 模式切换系统消息 -->
                  <div v-if="msg.role === 'system' && msg.isModeSwitch" class="action-card isModeSwitch">
                    <span class="action-icon">🔧</span>
                    <span class="action-msg">{{ msg.content }}</span>
                  </div>
                  <!-- 脚本执行结果卡片 -->
                  <div v-else-if="msg.role === 'system'" class="action-card" :class="msg.success ? 'ok' : 'fail'">
                    <span class="action-icon">{{ msg.success ? '✅' : '❌' }}</span>
                    <span class="action-label">{{ actionLabel(msg.action) }}</span>
                    <span class="action-name">{{ msg.name }}</span>
                    <span class="action-msg">{{ msg.message }}</span>
                    <span v-if="msg.path" class="action-path">{{ msg.path }}</span>
                    <pre v-if="msg.stdout" class="action-output">{{ msg.stdout }}</pre>
                    <pre v-if="msg.stderr" class="action-stderr">{{ msg.stderr }}</pre>
                    <div v-if="msg.output_files && msg.output_files.length" class="action-files">
                      <span class="action-files-label">📥 生成文件：</span>
                      <a
                        v-for="f in msg.output_files"
                        :key="f.url"
                        :href="f.url"
                        :download="fileBasename(f)"
                        class="action-file-link"
                      >📄 {{ fileBasename(f) }}</a>
                    </div>
                  </div>

                  <!-- 普通对话气泡 -->
                  <div v-else class="message" :class="msg.role" @mouseenter="hoveredMsgIdx = i" @mouseleave="hoveredMsgIdx = -1">
                    <span class="msg-role-label">{{ msg.role === 'user' ? '用户' : 'Skill 输出' }}</span>
                    <div class="bubble">
                      <!-- P4: 文件附件消息卡片 -->
                      <div v-if="msg.files && msg.files.length" class="msg-file-attachments">
                        <div v-for="(f, fi) in msg.files" :key="fi" class="msg-file-card">
                          <span class="msg-file-icon">📄</span>
                          <span class="msg-file-name">{{ f.filename || f.path }}</span>
                        </div>
                      </div>
                      <ChatBubble :content="msg.content" :files="undefined" :streaming="false" />
                      <InlineTaskList
                        v-if="msg.taskChecklist"
                        :tasks="msg.taskChecklist.tasks"
                        :completed-indices="msg.taskChecklist.completedIndices"
                        :executing-index="msg.taskChecklist.executingIndex"
                      />
                    </div>
                    <!-- P4: 消息操作按钮 -->
                    <div v-if="hoveredMsgIdx === i && !streaming" class="msg-actions">
                      <button class="msg-action-btn" @click="copyMessage(msg)" title="复制文本">📋</button>
                      <button v-if="msg.role === 'assistant'" class="msg-action-btn" @click="regenerateMessage(i)" title="重新生成">🔄</button>
                      <button class="msg-action-btn" :class="{ bookmarked: msg.bookmarked }" @click="bookmarkMessage(msg)" title="收藏">⭐</button>
                    </div>
                  </div>
                </template>

                <!-- 状态条 -->
                <div v-if="currentStatus" class="status-bar" :class="`phase-${currentStatus.phase}`" role="status">
                  <span class="status-spinner" aria-hidden="true"></span>
                  <span class="status-message">{{ currentStatus.message }}</span>
                </div>

                <!-- 跳过步骤提示 -->
                <div v-if="skippedSteps.length && !streaming" class="skipped-bar">
                  <span class="skipped-icon">⏭️</span>
                  <span class="skipped-label">已跳过 {{ skippedSteps.length }} 个步骤：</span>
                  <span v-for="(s, idx) in skippedSteps" :key="idx" class="skipped-chip">
                    {{ s.step }}（{{ s.reason }}）
                  </span>
                </div>

                <!-- 流式输出中 -->
                <div v-if="streaming" class="message assistant">
                  <span class="msg-role-label">Skill 输出</span>
                  <div class="bubble">
                    <ChatBubble :content="streamBuffer" :streaming="true" />
                  </div>
                </div>
              </div>

              <!-- 输入区 -->
              <div class="chat-input-area">
                <!-- 本轮生成文件 -->
                <div v-if="chatRoundFiles.length" class="round-files-bar">
                  <span class="round-files-label">📥 本次生成的文件</span>
                  <a
                    v-for="f in chatRoundFiles"
                    :key="f.url"
                    :href="f.url"
                    :download="fileBasename(f)"
                    class="round-file-link"
                  >📄 {{ fileBasename(f) }}</a>
                </div>

                <!-- 已上传文件 chips -->
                <div v-if="uploadedFiles.length" class="upload-chips">
                  <span v-for="(f, idx) in uploadedFiles" :key="f.path" class="upload-chip">
                    <span class="chip-icon">📄</span>
                    <span class="chip-name">{{ f.filename }}</span>
                    <span v-if="f.size" class="chip-size">{{ formatFileSize(f.size) }}</span>
                    <button class="chip-remove" :disabled="streaming" @click="removeUploadedFile(idx)" title="移除">✕</button>
                  </span>
                </div>

                <!-- 错误提示 -->
                <div v-if="chatError" class="error px16">{{ chatError }}</div>

                <!-- 输入行 -->
                <div class="chat-input-row">
                  <textarea
                    v-model="chatInput"
                    rows="4"
                    :placeholder="chatMode === 'refine'
                      ? '描述你想如何修改这个技能…（Ctrl+Enter 发送 / Enter 换行）'
                      : '向已加载的 Skill 发送测试消息…（Ctrl+Enter 发送 / Enter 换行）'"
                    @keydown.ctrl.enter.prevent="sendChat"
                    @keydown.escape="chatInput = ''"
                    :disabled="streaming || (chatMode === 'sandbox' && !selected.can_execute)"
                  />
                  <div class="chat-actions">
                    <input ref="chatFileInputEl" type="file" multiple style="display:none;" @change="onChatFileSelected" />
                    <button
                      v-if="streaming"
                      class="btn-danger btn-sm"
                      @click="stopGeneration"
                      title="停止生成"
                    >⏹ 停止</button>
                    <button
                      v-else
                      class="btn-primary"
                      @click="sendChat"
                      :disabled="!chatInput.trim() || !selected.can_execute"
                    >发送</button>
                    <span v-if="!selected.can_execute" class="send-disabled-hint">
                      技能状态为 {{ selected.status }}，不可执行
                    </span>
                  </div>
                </div>
              </div>
              </div>

              <!-- 右侧：抽屉面板（执行过程/方案/SOP） -->
              <template v-if="showThoughts || showPlanPanel">
                <div class="chat-sidebar">
                  <!-- 执行过程 -->
                  <transition name="panel-slide">
                    <div v-if="showThoughts" class="thinking-sidebar">
                      <div class="thinking-sidebar-header">
                        <span>执行过程</span>
                        <button class="btn-ghost btn-sm" @click="showThoughts = false">✕</button>
                      </div>
                      <ThinkingPanel :thoughts="thoughts" />
                    </div>
                  </transition>

                  <!-- 方案/SOP -->
                  <transition name="panel-slide">
                    <div v-if="showPlanPanel" class="thinking-sidebar plan-sidebar">
                      <div class="thinking-sidebar-header">
                        <div class="plan-tabs">
                          <button class="plan-tab" :class="{ active: planTab === 'plan' }" @click="planTab = 'plan'">任务方案</button>
                          <button class="plan-tab" :class="{ active: planTab === 'sop' }" @click="planTab = 'sop'">SOP</button>
                        </div>
                        <button class="btn-ghost btn-sm" @click="showPlanPanel = false">✕</button>
                      </div>
                      <TaskPlanPanel
                        v-if="planTab === 'plan'"
                        :plan="currentPlanPreview"
                        :executing-index="executingIndex"
                        :completed-indices="completedIndices"
                        :confirming="confirming"
                        @confirm="confirmCurrentPlan"
                        @cancel="cancelCurrentPlan"
                      />
                      <SOPPanel
                        v-if="planTab === 'sop'"
                        :sop="currentSOP"
                        @export="exportSOP"
                      />
                    </div>
                  </transition>
                </div>
              </template>
            </div>
          </div>
        </template>
      </div>
    </div>

    <!-- ===== Modal: 资产编辑 ===== -->
    <div v-if="assetEditor" class="overlay" @click.self="closeAssetEditor">
      <div class="dialog dialog-editor">
        <div class="dialog-title">{{ assetEditor.folder }}/{{ assetEditor.filename }}</div>
        <div v-if="assetEditor.loading" class="muted p16">加载中…</div>
        <div v-else-if="assetEditor.binary" class="muted p16">该文件为二进制，无法编辑。</div>
        <textarea v-else v-model="assetEditor.content" class="skill-editor asset-edit-textarea" spellcheck="false" />
        <div v-if="assetEditor.error" class="error px16">{{ assetEditor.error }}</div>
        <div class="dialog-actions">
          <template v-if="!assetEditor.binary">
            <button class="btn-primary" :disabled="assetEditor.saving" @click="saveAssetEdit">
              {{ assetEditor.saving ? '保存中…' : '保存' }}
            </button>
          </template>
          <button class="btn-ghost" @click="closeAssetEditor">{{ assetEditor.binary ? '关闭' : '取消' }}</button>
        </div>
      </div>
    </div>

    <!-- ===== Modal: ZIP 导入 ===== -->
    <div v-if="zipImport" class="overlay" @click.self="cancelZipImport">
      <div class="dialog">
        <p style="margin-bottom:8px;font-weight:600;">📦 导入 Skill ZIP</p>
        <p class="muted" style="font-size:13px;margin-bottom:16px;">文件：{{ zipImport.filename }}</p>
        <template v-if="zipImport.conflict">
          <p style="margin-bottom:16px;">技能 <strong>{{ zipImport.conflictName }}</strong> 已存在，是否覆盖？</p>
          <div class="dialog-actions">
            <button class="btn-danger" :disabled="zipImport.loading" @click="doImportZip(true)">{{ zipImport.loading ? '导入中…' : '覆盖导入' }}</button>
            <button class="btn-ghost" @click="cancelZipImport">取消</button>
          </div>
        </template>
        <template v-else>
          <div v-if="zipImport.error" class="error" style="margin-bottom:12px;">{{ zipImport.error }}</div>
          <div class="dialog-actions">
            <button class="btn-primary" :disabled="zipImport.loading" @click="doImportZip(false)">{{ zipImport.loading ? '导入中…' : '导入' }}</button>
            <button class="btn-ghost" @click="cancelZipImport">取消</button>
          </div>
        </template>
      </div>
    </div>

    <!-- ===== Modal: 删除确认 ===== -->
    <div v-if="deleteTarget" class="overlay" @click.self="deleteTarget = null">
      <div class="dialog">
        <p>确认删除 <strong>{{ deleteTarget }}</strong>？此操作不可撤销。</p>
        <div class="dialog-actions">
          <button class="btn-danger" @click="doDelete">删除</button>
          <button class="btn-ghost" @click="deleteTarget = null">取消</button>
        </div>
      </div>
    </div>

    <!-- ===== Modal: 白名单编辑 ===== -->
    <div v-if="allowlistEditor" class="overlay" @click.self="closeAllowlist">
      <div class="dialog dialog-editor">
        <div class="dialog-title">白名单 / 治理配置 (JSON)</div>
        <textarea v-model="allowlistText" class="skill-editor asset-edit-textarea" spellcheck="false" />
        <div v-if="allowlistError" class="error px16">{{ allowlistError }}</div>
        <div class="dialog-actions">
          <button class="btn-primary" @click="saveAllowlistEditor">保存</button>
          <button class="btn-ghost" @click="closeAllowlist">取消</button>
        </div>
      </div>
    </div>

    <!-- ===== Modal: 清空/重置确认 ===== -->
    <div v-if="confirmAction" class="overlay" @click.self="confirmAction = null">
      <div class="dialog">
        <p>{{ confirmAction.message }}</p>
        <div class="dialog-actions">
          <button class="btn-danger" @click="confirmAction.onConfirm">确认</button>
          <button class="btn-ghost" @click="confirmAction = null">取消</button>
        </div>
      </div>
    </div>

    <!-- ===== Toast ===== -->
    <Teleport to="body">
      <transition name="toast-fade">
        <div v-if="toast.show" class="toast-container">
          <div class="toast" :class="toast.type">{{ toast.message }}</div>
        </div>
      </transition>
    </Teleport>
  </div>
</template>

<script setup>
import { computed, ref, onMounted, onBeforeUnmount, nextTick, watch } from 'vue'
import {
  deleteAsset,
  deleteSkill,
  fetchAllowlist,
  fetchAssetContent,
  fetchSkill,
  fetchSkillAssets,
  fetchSkillEvents,
  fetchSkillVersions,
  fetchSkills,
  importSkillZip,
  rollbackSkillVersion,
  saveAllowlist,
  saveAssetContent,
  saveSkill,
  updateSkillStatus,
  upgradeSkillZip,
  uploadAsset,
} from '../composables/useSkills.js'
import { streamChat, confirmPlan, streamConfirmResponse } from '../composables/useChat.js'
import ChatBubble from '../components/ChatBubble.vue'
import InlineTaskList from '../components/InlineTaskList.vue'
import CollapsiblePanel from '../components/CollapsiblePanel.vue'
import ThinkingPanel from '../components/ThinkingPanel.vue'
import TaskPlanPanel from '../components/TaskPlanPanel.vue'
import SOPPanel from '../components/SOPPanel.vue'

// ================== 原始：技能列表 / 详情 / 编辑 ==================
const skills = ref([])
const selected = ref(null)
const listCollapsed = ref(false)
const loading = ref(true)
const chatMode = ref('sandbox')  // 'sandbox' | 'refine'
const editing = ref(false)
const editName = ref('')
const editContent = ref('')
const editError = ref('')
const saving = ref(false)
const deleteTarget = ref(null)

const assets = ref({ assets: [], references: [], scripts: [] })
const assetFolders = ['assets', 'references', 'scripts']
const uploadFolder = ref('assets')
const uploadFile = ref(null)
const uploading = ref(false)
const uploadError = ref('')
const assetError = ref('')
const fileInputRef = ref(null)
const events = ref([])
const versions = ref([])
const rollbackVersion = ref('')
const allowlistEditor = ref(false)
const allowlistText = ref('')
const allowlistError = ref('')
const lastFiveVersions = computed(() => versions.value.slice(-5).reverse())
const totalAssetCount = computed(() => {
  let n = 0
  for (const f of assetFolders) n += (assets.value[f] || []).length
  return n
})

const assetEditor = ref(null)
const zipInputRef = ref(null)
const zipImport = ref(null)

// 配置区折叠状态
const showSkillMd = ref(true)
const showAssets = ref(true)

// ================== P0：上下分割布局 ==================
const splitRatio = ref(0.6) // 上区占比
const chatPanelCollapsed = ref(true) // 默认折叠，点击"测试对话"展开
const isDragging = ref(false)

function startDrag(e) {
  isDragging.value = true
  const startY = e.clientY
  const startRatio = splitRatio.value
  const container = e.target.closest('.split-container')
  if (!container) return
  const containerHeight = container.clientHeight

  function onMouseMove(ev) {
    const delta = ev.clientY - startY
    let newRatio = startRatio + delta / containerHeight
    newRatio = Math.max(0.2, Math.min(0.8, newRatio))
    splitRatio.value = newRatio
  }
  function onMouseUp() {
    isDragging.value = false
    document.removeEventListener('mousemove', onMouseMove)
    document.removeEventListener('mouseup', onMouseUp)
    document.body.style.cursor = ''
    document.body.style.userSelect = ''
  }
  document.addEventListener('mousemove', onMouseMove)
  document.addEventListener('mouseup', onMouseUp)
  document.body.style.cursor = 'row-resize'
  document.body.style.userSelect = 'none'
}

function expandChatPanel() {
  chatPanelCollapsed.value = false
  nextTick(() => {
    if (chatInput.value === undefined) return
    const textarea = document.querySelector('.chat-input-row textarea')
    if (textarea) textarea.focus()
  })
}

// ================== P1：沙盒对话核心 ==================
const chatMessages = ref([])
const chatInput = ref('')
const streaming = ref(false)
const streamBuffer = ref('')
const chatError = ref('')
const chatMessagesEl = ref(null)
const chatFileInputEl = ref(null)

const sessionId = ref(newSessionId())
const uploadedFiles = ref([])
const currentStatus = ref(null)
const skippedSteps = ref([])
const chatRoundFiles = ref([])
const abortController = ref(null)

// 确认弹窗
const confirmAction = ref(null)

// Toast
const toast = ref({ show: false, message: '', type: 'info' })
let toastTimer = null
function showToast(message, type = 'info', duration = 2000) {
  if (toastTimer) clearTimeout(toastTimer)
  toast.value = { show: true, message, type }
  toastTimer = setTimeout(() => { toast.value.show = false }, duration)
}

// Inline task checklist
const pendingChecklist = ref(null)

// P5: 执行过程面板
const thoughts = ref([])
const showThoughts = ref(false)

// P5: 规划模式
const showPlanPanel = ref(false)
const planTab = ref('plan')
const currentPlanPreview = ref(null)
const currentSOP = ref(null)
const confirming = ref(false)
const executingIndex = ref(-1)
const completedIndices = ref([])

// P4: 消息 hover 操作
const hoveredMsgIdx = ref(-1)

const ACTION_LABELS = {
  run_script: '运行脚本',
  init: '初始化目录',
  write: '写入 SKILL.md',
  write_file: '写入文件',
  validate: '校验格式',
  package: '打包 Skill',
  output_files: '生成文件',
  create_file: '创建文件',
  file_operation: '文件操作',
  creator_phase3_completed: 'Skill 创建完成',
}

function actionLabel(action) {
  return ACTION_LABELS[action] || action || '执行结果'
}
function iconForSkill(sk) {
  const map = {
    'animal-world-story-generator': '📖',
    'bio-science-illustrated-presentation': '🧬',
    'council': '🏛',
    'database-query-export': '📊',
    'huanzhu-data-pro': '💾',
    'mysql-htsql-query': '🔍',
    'mysql-htsql-simple': '🗃',
    'mysql-query-summary': '📈',
    'news-to-note': '📰',
    'pdf-translator': '📄',
  }
  return map[sk.name] || map[sk.scope] || '⚙️'
}
function fileBasename(f) {
  return f.name || (f.path || 'file').split('/').pop().split('\\').pop()
}
function newSessionId() {
  if (typeof crypto !== 'undefined' && crypto.randomUUID) return crypto.randomUUID()
  return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0
    return (c === 'x' ? r : (r & 0x3) | 0x8).toString(16)
  })
}
function formatFileSize(bytes) {
  if (!bytes) return ''
  if (bytes < 1024) return bytes + ' B'
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + ' KB'
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB'
}
async function scrollChatBottom() {
  await nextTick()
  if (chatMessagesEl.value) chatMessagesEl.value.scrollTop = chatMessagesEl.value.scrollHeight
}

// 清空对话（保留会话上下文）
function clearChat() {
  confirmAction.value = {
    message: '确认清空当前对话记录？会话上下文将保留。',
    onConfirm: () => {
      chatMessages.value = []
      chatRoundFiles.value = []
      skippedSteps.value = []
      confirmAction.value = null
      showToast('对话已清空')
    },
  }
}

// 重置会话（清空对话 + 新建 session + 清理后端）
function resetChat() {
  confirmAction.value = {
    message: '确认重置会话？将清除上下文记忆，重新发起对话。',
    onConfirm: async () => {
      confirmAction.value = null
      await cleanupSession()
      chatMessages.value = []
      chatInput.value = ''
      streamBuffer.value = ''
      chatError.value = ''
      currentStatus.value = null
      uploadedFiles.value = []
      chatRoundFiles.value = []
      skippedSteps.value = []
      pendingChecklist.value = null
      sessionId.value = newSessionId()
      showToast('会话已重置')
    },
  }
}

// 导出对话
function exportChat() {
  if (!chatMessages.value.length) return
  const exportData = chatMessages.value.map((m) => {
    const base = { role: m.role, content: m.content || '' }
    if (m.files) base.files = m.files
    if (m.action) base.action = m.action
    if (m.success !== undefined) base.success = m.success
    return base
  })
  const blob = new Blob([JSON.stringify(exportData, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = `chat-${selected.value?.name || 'skill'}-${Date.now()}.json`
  a.click()
  URL.revokeObjectURL(url)
  showToast('对话已导出')
}

// 停止生成
function stopGeneration() {
  if (abortController.value) {
    abortController.value.abort()
    abortController.value = null
  }
}

// 文件上传
function onChatFileSelected(e) {
  const files = Array.from(e.target.files || [])
  e.target.value = ''
  if (!files.length || !selected.value) return
  uploadChatFiles(files)
}

async function uploadChatFiles(files) {
  uploading.value = true
  for (const file of files) {
    if (file.size > 10 * 1024 * 1024) {
      showToast(`文件 ${file.name} 超过 10MB 限制`, 'error')
      continue
    }
    const fd = new FormData()
    fd.append('file', file)
    fd.append('session_id', sessionId.value)
    try {
      const res = await fetch(
        `/api/skills/${encodeURIComponent(selected.value.name)}/sandbox-inputs`,
        { method: 'POST', body: fd }
      )
      if (!res.ok) {
        const err = await res.json().catch(() => ({ detail: '上传失败' }))
        showToast(err.detail || '上传失败', 'error')
      } else {
        const data = await res.json()
        uploadedFiles.value.push(data)
        showToast(`${file.name} 上传成功`)
      }
    } catch (e) {
      showToast(e.message || '上传失败', 'error')
    }
  }
  uploading.value = false
}

function removeUploadedFile(idx) {
  uploadedFiles.value.splice(idx, 1)
}

// 拖拽上传
const isDraggingFile = ref(false)
function onDragOver(e) {
  isDraggingFile.value = true
}
function onDragLeave() {
  isDraggingFile.value = false
}
function onDrop(e) {
  isDraggingFile.value = false
  const files = Array.from(e.dataTransfer?.files || [])
  if (!files.length || !selected.value) return
  uploadChatFiles(files)
}

// 发送消息
async function sendChat() {
  const text = chatInput.value.trim()
  if (!text) { showToast('请输入消息内容', 'error'); return }
  if (streaming.value) { showToast('正在生成中，请等待', 'error'); return }
  if (!selected.value) { showToast('请先选择一个技能', 'error'); return }
  if (chatMode.value === 'sandbox' && !selected.value.can_execute) {
    showToast(`技能状态为 ${selected.value.status}，不可执行`, 'error')
    return
  }

  chatError.value = ''
  const fileSnapshot = uploadedFiles.value.map((f) => ({ filename: f.filename, path: f.path }))

  chatMessages.value.push({
    role: 'user',
    content: text,
    files: fileSnapshot.length ? fileSnapshot : undefined,
  })
  chatInput.value = ''
  chatRoundFiles.value = []
  skippedSteps.value = []
  await scrollChatBottom()

  streaming.value = true
  streamBuffer.value = ''
  pendingChecklist.value = null
  thoughts.value = []
  currentPlanPreview.value = null
  currentSOP.value = null
  executingIndex.value = -1
  completedIndices.value = []

  const controller = new AbortController()
  abortController.value = controller

  try {
    let url, body

    if (chatMode.value === 'refine') {
      // 修改模式：发送到 refine 端点
      url = `/api/skills/${encodeURIComponent(selected.value.name)}/refine`
      body = {
        messages: chatMessages.value
          .filter((m) => m.role !== 'system')
          .map((m) => ({ role: m.role, content: m.content })),
        modification_request: text,
        sandbox_session_id: sessionId.value,
      }
    } else {
      // 测试模式：发送到 sandbox 端点
      url = `/api/chat/sandbox/${encodeURIComponent(selected.value.name)}`
      body = {
        messages: chatMessages.value
          .filter((m) => m.role !== 'system')
          .map((m) => ({ role: m.role, content: m.content })),
        execution_mode: 'execute',
        sandbox_session_id: sessionId.value,
      }
      if (fileSnapshot.length) body.input_files = fileSnapshot
    }

    for await (const chunk of streamChat(url, body, { signal: controller.signal })) {
      if (controller.signal.aborted) break

      if (typeof chunk === 'string') {
        streamBuffer.value += chunk
        await scrollChatBottom()
      } else if (chunk.type === 'status') {
        currentStatus.value = chunk.data
      } else if (chunk.type === 'refine_result') {
        // 修改完成：刷新技能数据，切回测试模式
        const result = chunk.data
        showToast(`技能已更新至 v${result.new_version}`, 'info')
        await select(selected.value.name)
        chatMode.value = 'sandbox'
        // 延迟重置会话，让用户看到修改结果
        setTimeout(() => {
          resetChat()
        }, 1500)
      } else if (chunk.type === 'action_result') {
        const r = chunk.data
        chatMessages.value.push({
          role: 'system',
          action: r.action,
          name: r.name,
          success: r.success,
          message: r.message,
          path: r.path,
          stdout: r.stdout || '',
          stderr: r.stderr || '',
          exit_code: r.exit_code,
          output_files: r.output_files || [],
        })
        if (r.output_files && r.output_files.length) {
          chatRoundFiles.value.push(...r.output_files)
        }
        await scrollChatBottom()
      } else if (chunk.type === 'step_skipped') {
        skippedSteps.value.push(chunk.data)
        thoughts.value.push({
          step: `step_skipped_${chunk.data.step}`,
          label: `跳过：${chunk.data.step}`,
          detail: chunk.data.reason,
          data: chunk.data,
          ts: chunk.data.ts,
        })
        if (!showThoughts.value) showThoughts.value = true
      } else if (chunk.type === 'sandbox_retry') {
        thoughts.value.push({
          step: 'sandbox_retry',
          label: `重试 (${chunk.data.attempt}/${chunk.data.max_retries})`,
          detail: chunk.data.corrected ? '已根据错误信息调整输入' : '重试执行',
          data: chunk.data,
          ts: chunk.data.ts,
        })
        if (!showThoughts.value) showThoughts.value = true
      } else if (chunk.type === 'thought') {
        thoughts.value.push(chunk.data)
        if (!showThoughts.value) showThoughts.value = true
      } else if (chunk.type === 'task_checklist') {
        pendingChecklist.value = chunk.data
      } else if (chunk.type === 'task_progress') {
        executingIndex.value = chunk.data.executing_index
        completedIndices.value = chunk.data.completed_indices
        // 更新行内任务清单
        if (pendingChecklist.value) {
          pendingChecklist.value = {
            ...pendingChecklist.value,
            executingIndex: chunk.data.executing_index,
            completedIndices: [...chunk.data.completed_indices],
          }
        }
      } else if (chunk.type === 'plan_preview') {
        currentPlanPreview.value = chunk.data
        showPlanPanel.value = true
        planTab.value = 'plan'
      } else if (chunk.type === 'sop_plan') {
        currentSOP.value = chunk.data
      }
    }

    if (streamBuffer.value.trim()) {
      const msg = { role: 'assistant', content: streamBuffer.value.trim() }
      if (pendingChecklist.value) {
        msg.taskChecklist = pendingChecklist.value
        pendingChecklist.value = null
      }
      chatMessages.value.push(msg)
    }
    streamBuffer.value = ''
  } catch (e) {
    if (e.name !== 'AbortError') {
      chatError.value = e.message || '发送失败'
      chatMessages.value.push({ role: 'assistant', content: `❌ ${e.message || '请求失败'}` })
    }
  } finally {
    streaming.value = false
    currentStatus.value = null
    abortController.value = null
    await scrollChatBottom()
  }
}

// 模式切换
function switchToRefineMode() {
  chatMode.value = 'refine'
  chatMessages.value.push({
    role: 'system',
    content: '已进入技能修改模式。请描述你希望如何修改这个技能，系统将根据你的测试反馈修改 SKILL.md 并保存为新版本。',
    isModeSwitch: true,
  })
  scrollChatBottom()
}

function switchToSandboxMode() {
  chatMode.value = 'sandbox'
}

// P4: 消息操作
function copyMessage(msg) {
  navigator.clipboard.writeText(msg.content || '').then(() => showToast('已复制'))
}
async function regenerateMessage(idx) {
  // 删除最后一条 assistant 消息及其后的 system 消息，重新发送
  const msg = chatMessages.value[idx]
  if (!msg || msg.role !== 'assistant') return
  // 找到这条 assistant 消息之前的最近一条 user 消息
  let userIdx = -1
  for (let i = idx - 1; i >= 0; i--) {
    if (chatMessages.value[i].role === 'user') { userIdx = i; break }
  }
  if (userIdx === -1) return
  // 删除从 userIdx+1 到末尾的所有消息
  chatMessages.value.splice(userIdx + 1)
  // 重新发送
  const userMsg = chatMessages.value[userIdx]
  chatInput.value = userMsg.content
  // 移除这条 user 消息（sendChat 会重新 push）
  chatMessages.value.splice(userIdx, 1)
  await sendChat()
}
function bookmarkMessage(msg) {
  msg.bookmarked = !msg.bookmarked
  showToast(msg.bookmarked ? '已收藏' : '已取消收藏')
}

// P5: 规划模式确认/取消
async function confirmCurrentPlan() {
  if (!currentPlanPreview.value || confirming.value || !selected.value) return
  const planId = currentPlanPreview.value.plan_id
  confirming.value = true
  streaming.value = true
  streamBuffer.value = ''
  thoughts.value = []
  chatRoundFiles.value = []
  executingIndex.value = -1
  completedIndices.value = []

  const controller = new AbortController()
  abortController.value = controller

  try {
    const response = await confirmPlan(selected.value.name, planId, 'confirm')
    const contentType = response.headers.get('content-type') || ''
    if (contentType.includes('text/event-stream')) {
      for await (const chunk of streamConfirmResponse(response)) {
        if (controller.signal.aborted) break
        if (typeof chunk === 'string') {
          streamBuffer.value += chunk
          await scrollChatBottom()
        } else if (chunk.type === 'status') {
          currentStatus.value = chunk.data
        } else if (chunk.type === 'thought') {
          thoughts.value.push(chunk.data)
          if (!showThoughts.value) showThoughts.value = true
        } else if (chunk.type === 'action_result') {
          const r = chunk.data
          chatMessages.value.push({
            role: 'system', action: r.action, name: r.name, success: r.success,
            message: r.message, path: r.path, stdout: r.stdout || '', stderr: r.stderr || '',
            exit_code: r.exit_code, output_files: r.output_files || [],
          })
          if (r.output_files && r.output_files.length) chatRoundFiles.value.push(...r.output_files)
          await scrollChatBottom()
        } else if (chunk.type === 'task_progress') {
          executingIndex.value = chunk.data.executing_index
          completedIndices.value = chunk.data.completed_indices
        }
      }
      if (streamBuffer.value.trim()) {
        chatMessages.value.push({ role: 'assistant', content: streamBuffer.value.trim() })
        streamBuffer.value = ''
      }
    } else {
      const data = await response.json()
      chatMessages.value.push({ role: 'assistant', content: data.message || '方案已执行完成。' })
    }
    currentPlanPreview.value = null
  } catch (e) {
    if (e.name !== 'AbortError') chatError.value = e.message
  } finally {
    confirming.value = false
    streaming.value = false
    currentStatus.value = null
    abortController.value = null
    executingIndex.value = -1
    completedIndices.value = []
    await scrollChatBottom()
  }
}
async function cancelCurrentPlan() {
  if (!currentPlanPreview.value || !selected.value) return
  try {
    await confirmPlan(selected.value.name, currentPlanPreview.value.plan_id, 'cancel')
    chatMessages.value.push({ role: 'assistant', content: '❌ 执行方案已取消。' })
    currentPlanPreview.value = null
  } catch (e) {
    chatError.value = e.message
  }
}
async function exportSOP(format) {
  if (!currentSOP.value) return
  const skillName = selected.value?.name || 'skill'
  if (format === 'json') {
    const blob = new Blob([JSON.stringify(currentSOP.value, null, 2)], { type: 'application/json' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = `sop-${skillName}.json`; a.click()
    URL.revokeObjectURL(url)
  } else {
    const sop = currentSOP.value
    let md = `# ${sop.title}\n\n**版本**：${sop.version}\n**复杂度**：${sop.complexity}\n\n## 步骤\n\n`
    for (const step of (sop.steps || [])) { md += `### ${step.order}. ${step.name}\n${step.description}\n\n` }
    if (sop.flowchart_mermaid) md += `\n## 流程图\n\n\`\`\`mermaid\n${sop.flowchart_mermaid}\n\`\`\`\n`
    const blob = new Blob([md], { type: 'text/markdown' })
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a'); a.href = url; a.download = `sop-${skillName}.md`; a.click()
    URL.revokeObjectURL(url)
  }
  showToast('SOP 已导出')
}

// 会话清理
async function cleanupSession() {
  if (!selected.value || !sessionId.value) return
  try {
    await fetch(
      `/api/skills/${encodeURIComponent(selected.value.name)}/sandbox-inputs/${encodeURIComponent(sessionId.value)}`,
      { method: 'DELETE' }
    )
  } catch { /* best-effort */ }
}

function _beforeUnloadHandler() {
  if (!selected.value || !sessionId.value) return
  const url = `/api/skills/${encodeURIComponent(selected.value.name)}/sandbox-inputs/${encodeURIComponent(sessionId.value)}`
  try {
    const xhr = new XMLHttpRequest()
    xhr.open('DELETE', url, false)
    xhr.send()
  } catch { /* best-effort */ }
}

// ================== 原有 CRUD 逻辑 ==================
async function load() {
  loading.value = true
  skills.value = await fetchSkills('manage', { includeHidden: true })
  loading.value = false
}
async function loadAssets(name) {
  try {
    assets.value = await fetchSkillAssets(name)
  } catch {
    assets.value = { assets: [], references: [], scripts: [] }
  }
}
async function select(name) {
  editing.value = false
  selected.value = await fetchSkill(name, 'manage')
  // 用 sandbox 模式的 can_execute 覆盖，因为测试对话走 sandbox 端点
  try {
    const sandboxSkill = await fetchSkill(name, 'sandbox')
    selected.value.can_execute = sandboxSkill.can_execute
  } catch { /* sandbox 模式下不可见则保持 manage 模式的结果 */ }
  uploadError.value = ''
  assetError.value = ''
  await loadAssets(name)
  await loadGovernance(name)
  // 切换技能时重置对话
  await cleanupSession()
  chatMessages.value = []
  chatInput.value = ''
  streamBuffer.value = ''
  chatError.value = ''
  currentStatus.value = null
  uploadedFiles.value = []
  chatRoundFiles.value = []
  skippedSteps.value = []
  pendingChecklist.value = null
  sessionId.value = newSessionId()
  showToast('已切换技能，会话已重置')
}
async function loadGovernance(name) {
  const [{ events: nextEvents }, versionInfo] = await Promise.all([
    fetchSkillEvents(name),
    fetchSkillVersions(name),
  ])
  events.value = nextEvents
  versions.value = versionInfo.versions || []
  rollbackVersion.value = ''
}
function openNew() {
  selected.value = null
  editing.value = true
  editName.value = ''
  editContent.value = `---\nname: my-skill\ndescription: Describe what this skill does and when to use it.\n---\n\n# My Skill\n\n`
  editError.value = ''
}
function startEdit() {
  if (!selected.value?.editable) return
  editName.value = selected.value.name
  editContent.value = selected.value.content
  editError.value = ''
  editing.value = true
}
function cancelEdit() {
  editing.value = false
  editError.value = ''
}
async function save() {
  const name = editName.value.trim()
  if (!name) { editError.value = 'Skill 名称不能为空'; return }
  saving.value = true
  editError.value = ''
  try {
    await saveSkill(name, editContent.value)
    await load()
    editing.value = false
    selected.value = await fetchSkill(name, 'manage')
    await loadAssets(name)
    await loadGovernance(name)
  } catch (e) {
    editError.value = e.message
  } finally {
    saving.value = false
  }
}
function confirmDelete() {
  if (!selected.value?.editable) return
  deleteTarget.value = selected.value.name
}
async function doDelete() {
  const name = deleteTarget.value
  deleteTarget.value = null
  await deleteSkill(name)
  selected.value = null
  assets.value = { assets: [], references: [], scripts: [] }
  uploadError.value = ''
  assetError.value = ''
  chatMessages.value = []
  await load()
}
function onFileChange(e) {
  uploadFile.value = e.target.files[0] || null
  uploadError.value = ''
}
async function doUpload() {
  if (!uploadFile.value || !selected.value) return
  uploading.value = true
  uploadError.value = ''
  try {
    await uploadAsset(selected.value.name, uploadFolder.value, uploadFile.value)
    uploadFile.value = null
    if (fileInputRef.value) fileInputRef.value.value = ''
    await loadAssets(selected.value.name)
  } catch (e) {
    uploadError.value = e.message
  } finally {
    uploading.value = false
  }
}
async function removeAsset(folder, filename) {
  if (!selected.value) return
  assetError.value = ''
  try {
    await deleteAsset(selected.value.name, folder, filename)
    await loadAssets(selected.value.name)
  } catch (e) {
    assetError.value = e.message
  }
}
async function openAssetEditor(folder, filename) {
  assetEditor.value = { folder, filename, content: '', loading: true, binary: false, saving: false, error: '' }
  try {
    const text = await fetchAssetContent(selected.value.name, folder, filename)
    assetEditor.value = { folder, filename, content: text, loading: false, binary: false, saving: false, error: '' }
  } catch (e) {
    if (e.status === 415) {
      assetEditor.value = { folder, filename, content: '', loading: false, binary: true, saving: false, error: '' }
    } else {
      assetEditor.value = null
      assetError.value = e.message
    }
  }
}
function closeAssetEditor() {
  assetEditor.value = null
}
async function saveAssetEdit() {
  if (!assetEditor.value || !selected.value) return
  assetEditor.value.saving = true
  assetEditor.value.error = ''
  try {
    await saveAssetContent(selected.value.name, assetEditor.value.folder, assetEditor.value.filename, assetEditor.value.content)
    assetEditor.value = null
    await loadAssets(selected.value.name)
  } catch (e) {
    assetEditor.value.error = e.message
  } finally {
    if (assetEditor.value) assetEditor.value.saving = false
  }
}
async function applyStatus(action) {
  if (!selected.value?.editable) return
  await updateSkillStatus(selected.value.name, action)
  await load()
  const refreshed = await fetchSkill(selected.value.name, 'manage')
  selected.value = refreshed
  await loadGovernance(refreshed.name)
}
async function onUpgradeZipChange(event) {
  const file = event.target.files[0]
  event.target.value = ''
  if (!file || !selected.value?.editable) return
  await upgradeSkillZip(selected.value.name, file)
  await load()
  const refreshed = await fetchSkill(selected.value.name, 'manage')
  selected.value = refreshed
  await loadGovernance(refreshed.name)
}
async function doRollback() {
  if (!selected.value?.editable || !rollbackVersion.value) return
  await rollbackSkillVersion(selected.value.name, rollbackVersion.value)
  await load()
  const refreshed = await fetchSkill(selected.value.name, 'manage')
  selected.value = refreshed
  await loadGovernance(refreshed.name)
}
async function openAllowlist() {
  const payload = await fetchAllowlist()
  allowlistText.value = JSON.stringify(payload, null, 2)
  allowlistError.value = ''
  allowlistEditor.value = true
}
function closeAllowlist() {
  allowlistEditor.value = false
}
async function saveAllowlistEditor() {
  try {
    await saveAllowlist(JSON.parse(allowlistText.value))
    allowlistEditor.value = false
    await load()
  } catch (e) {
    allowlistError.value = e.message
  }
}
function onZipChange(e) {
  const file = e.target.files[0]
  if (zipInputRef.value) zipInputRef.value.value = ''
  if (!file) return
  zipImport.value = { filename: file.name, file, loading: false, error: '', conflict: false, conflictName: '' }
}
function cancelZipImport() {
  zipImport.value = null
}
async function doImportZip(overwrite) {
  if (!zipImport.value) return
  zipImport.value.loading = true
  zipImport.value.error = ''
  zipImport.value.conflict = false
  try {
    const result = await importSkillZip(zipImport.value.file, overwrite)
    zipImport.value = null
    await load()
    selected.value = await fetchSkill(result.name, 'manage')
    await loadAssets(result.name)
    await loadGovernance(result.name)
    await cleanupSession()
    chatMessages.value = []
    sessionId.value = newSessionId()
  } catch (e) {
    if (e.status === 409) {
      zipImport.value.conflict = true
      zipImport.value.conflictName = e.skillName || ''
      zipImport.value.loading = false
    } else {
      zipImport.value.error = e.message
      zipImport.value.loading = false
    }
  }
}

onMounted(() => {
  load()
  window.addEventListener('beforeunload', _beforeUnloadHandler)
})
onBeforeUnmount(() => {
  cleanupSession()
  window.removeEventListener('beforeunload', _beforeUnloadHandler)
})
</script>

<style scoped>
/* ===== 全局页面 ===== */
.skills-page {
  display: flex;
  flex-direction: column;
  height: 100%;
  overflow: hidden;
  background: #1C2029;
  color: #E2E8F0;
}

/* ===== Header ===== */
.header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 14px 20px;
  border-bottom: 1px solid #2A2F3A;
  flex-shrink: 0;
  background: #1C2029;
}
.header h2 { font-size: 18px; font-weight: 600; margin: 0; }
.header-actions { display: flex; gap: 8px; align-items: center; }

/* ===== Body ===== */
.body {
  display: flex;
  flex: 1;
  overflow: hidden;
  min-height: 0;
}

/* ===== 左侧列表 ===== */
.list-panel {
  width: 240px;
  flex-shrink: 0;
  border-right: 1px solid #2A2F3A;
  overflow-y: auto;
  background: #1C2029;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.12) transparent;
  position: relative;
  transition: width 0.25s ease;
}
.list-panel.collapsed {
  width: 56px;
  overflow-x: hidden;
}
.list-header {
  padding: 10px 16px;
  font-size: 12px;
  color: #8892A4;
  border-bottom: 1px solid #2A2F3A;
  font-weight: 500;
  letter-spacing: 0.3px;
}
.skill-item {
  padding: 12px 16px;
  cursor: pointer;
  border-bottom: 1px solid #2A2F3A;
  transition: background 0.15s, padding 0.25s ease;
  display: flex;
  align-items: flex-start;
  gap: 10px;
}
.list-panel.collapsed .skill-item {
  padding: 12px 4px;
  justify-content: center;
  gap: 0;
}
.skill-item:hover { background: #252B38; }
.skill-item.active { background: #252B38; border-left: 3px solid #4A90E2; }
.sk-icon {
  font-size: 16px;
  flex-shrink: 0;
  display: flex;
  align-items: center;
  justify-content: center;
}
.sk-content { flex: 1; min-width: 0; }
.sk-name { font-weight: 500; margin-bottom: 3px; font-size: 14px; }
.sk-meta { font-size: 11px; margin-bottom: 3px; color: #8892A4; }
.sk-desc { font-size: 12px; color: #8892A4; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

.list-collapse-btn {
  position: absolute;
  right: -12px;
  top: 20px;
  width: 24px;
  height: 24px;
  border-radius: 50%;
  border: 1px solid #2A2F3A;
  background: #1C2029;
  color: #8892A4;
  font-size: 11px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10;
  transition: transform 0.15s, background 0.15s, color 0.15s;
}
.list-collapse-btn:hover {
  background: #4A90E2;
  color: #fff;
  border-color: #4A90E2;
}
.list-collapse-icon {
  display: inline-block;
  transition: transform 0.25s ease;
}
.list-collapse-icon.flipped { transform: rotate(180deg); }

/* ===== 右侧主面板 ===== */
.main-panel {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
}
.empty-hint {
  margin: auto;
  text-align: center;
  padding: 40px 20px;
  max-width: 400px;
  color: #8892A4;
}

/* ===== 详情头部 ===== */
.detail-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  padding: 10px 20px;
  border-bottom: 1px solid #2A2F3A;
  flex-shrink: 0;
  background: #1C2029;
}
.header-left { display: flex; flex-direction: column; gap: 4px; }
.detail-title { font-weight: 600; font-size: 15px; }
.header-badges { display: flex; flex-wrap: wrap; gap: 6px; }
.header-actions-inline { display: flex; gap: 8px; }

/* ===== Badges ===== */
.gov-badge {
  padding: 2px 8px;
  border-radius: 999px;
  background: #252B38;
  font-size: 11px;
  color: #C0C8D8;
}
.gov-badge.blocked { color: #E55; }
.status-approved { color: #4C8; }
.status-disabled, .status-rejected, .status-quarantined { color: #E55; }

/* ===== 上下分割容器 ===== */
.split-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-height: 0;
}

/* ===== 上区：技能配置 ===== */
.config-area {
  overflow-y: auto;
  flex-shrink: 0;
  background: #1C2029;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.12) transparent;
  transition: height 0.25s ease;
}

/* 操作按钮栏 */
.action-bar {
  display: flex;
  align-items: center;
  gap: 4px;
  padding: 8px 16px;
  border-bottom: 1px solid #2A2F3A;
  flex-wrap: wrap;
  background: #1E2330;
}
.action-group {
  display: flex;
  align-items: center;
  gap: 4px;
  flex-wrap: wrap;
}
.action-divider {
  width: 1px;
  height: 20px;
  background: #3A4050;
  margin: 0 6px;
}
.btn-test-entry {
  color: #4A90E2 !important;
  border-color: #4A90E2 !important;
}

/* 治理摘要 */
.governance-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 10px;
  padding: 12px 16px;
}
.governance-card {
  border: 1px solid #2A2F3A;
  border-radius: 8px;
  padding: 10px 12px;
  background: #1E2330;
}
.governance-title { font-weight: 600; margin-bottom: 6px; font-size: 13px; }
.governance-list { display: flex; flex-direction: column; gap: 3px; font-size: 12px; color: #8892A4; }

/* SKILL.md 预览 */
.skill-preview {
  margin: 0;
  padding: 12px 16px;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 12px;
  white-space: pre-wrap;
  word-break: break-word;
  background: #161A22;
  border-radius: 6px;
  max-height: 300px;
  overflow-y: auto;
  color: #C0C8D8;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.1) transparent;
}

/* 资产文件 */
.asset-group { margin-bottom: 10px; }
.asset-group-title {
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.06em;
  color: #8892A4;
  margin-bottom: 4px;
  font-family: monospace;
}
.asset-list { display: flex; flex-direction: column; gap: 2px; }
.asset-row {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 3px 8px;
  border-radius: 4px;
  background: #252B38;
}
.asset-name { flex: 1; font-size: 12px; font-family: monospace; word-break: break-all; color: #C0C8D8; }
.asset-empty { font-size: 12px; padding: 2px 0; color: #8892A4; }
.btn-icon-danger, .btn-icon-edit {
  background: none; border: none; cursor: pointer; padding: 2px 6px; font-size: 12px; line-height: 1; border-radius: 3px; flex-shrink: 0;
}
.btn-icon-danger { color: #E55; }
.btn-icon-danger:hover:not(:disabled) { background: rgba(229,85,85,0.15); }
.btn-icon-edit { color: #4A90E2; }
.btn-icon-edit:hover:not(:disabled) { background: rgba(74,144,226,0.15); }
.btn-icon-danger:disabled, .btn-icon-edit:disabled { opacity: 0.35; cursor: not-allowed; }
.upload-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  flex-wrap: wrap;
}
.folder-select {
  font-size: 12px;
  padding: 4px 8px;
  border-radius: 4px;
  border: 1px solid #2A2F3A;
  background: #1E2330;
  color: #C0C8D8;
}
.file-input { flex: 1; font-size: 12px; min-width: 0; }

/* 编辑态 */
.skill-editor {
  flex: 1;
  border: none;
  border-radius: 0;
  font-family: 'Fira Code', 'Cascadia Code', monospace;
  font-size: 13px;
  background: #161A22;
  color: #C0C8D8;
  padding: 16px 20px;
  resize: none;
  min-height: 0;
}

/* ===== 可拖拽分割线 ===== */
.resize-handle {
  height: 8px;
  flex-shrink: 0;
  cursor: row-resize;
  display: flex;
  align-items: center;
  justify-content: center;
  background: #1A1E28;
  transition: background 0.15s;
  position: relative;
  z-index: 2;
}
.resize-handle:hover {
  background: #2A3040;
}
.resize-line {
  width: 48px;
  height: 3px;
  border-radius: 2px;
  background: #3A4050;
  transition: background 0.15s;
}
.resize-handle:hover .resize-line {
  background: #4A90E2;
}

/* ===== 下区：测试对话面板 ===== */
.chat-panel {
  display: flex;
  flex-direction: row;
  overflow: hidden;
  min-height: 0;
  background: #181C25;
  flex-shrink: 0;
  transition: height 0.25s ease;
}

/* 聊天主区：左（垂直堆叠 toolbar + messages + input） */
.chat-main {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  min-width: 0;
  background: #181C25;
}

/* 抽屉面板容器：右（固定宽度，内部垂直堆叠 sidebar） */
.chat-sidebar {
  flex-shrink: 0;
  width: 320px;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  border-left: 1px solid #2A2F3A;
  background: #1A1E28;
}

/* 工具栏 */
.chat-toolbar {
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 6px 16px;
  border-bottom: 1px solid #2A2F3A;
  flex-shrink: 0;
  background: #1A1E28;
  transition: background 0.2s, border-color 0.2s;
}
.chat-toolbar.refine-mode {
  background: #1E1A14;
  border-bottom-color: #4A3D10;
}
.chat-toolbar-title { font-weight: 600; font-size: 13px; white-space: nowrap; }
.chat-toolbar.refine-mode .chat-toolbar-title { color: #E8A830; }
.chat-toolbar-actions { display: flex; gap: 4px; flex: 1; }
.chat-toolbar-right { display: flex; gap: 6px; }

/* 不可执行提示 */
.warning-banner {
  background: #2A2510;
  border-bottom: 1px solid #4A3D10;
  color: #D4A017;
  padding: 8px 16px;
  font-size: 13px;
  flex-shrink: 0;
}

/* 对话历史区 */
.chat-messages {
  flex: 1;
  overflow-y: auto;
  padding: 16px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  position: relative;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.1) transparent;
}

/* 拖拽浮层 */
.drag-overlay {
  position: absolute;
  inset: 0;
  background: rgba(74, 144, 226, 0.12);
  border: 2px dashed #4A90E2;
  border-radius: 8px;
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 10;
}
.drag-overlay-content {
  font-size: 16px;
  font-weight: 600;
  color: #4A90E2;
  padding: 16px 24px;
  background: rgba(24, 28, 37, 0.9);
  border-radius: 8px;
}

/* 空状态 */
.empty-chat {
  margin: auto;
  text-align: center;
  max-width: 400px;
  line-height: 1.8;
}
.empty-chat-icon { font-size: 40px; margin-bottom: 8px; opacity: 0.5; }
.empty-chat p { margin: 6px 0; }
.empty-chat .hint { font-size: 12px; color: #6B7280; }

/* 消息气泡 */
.message { display: flex; flex-direction: column; }
.message.user { align-items: flex-end; }
.message.assistant { align-items: flex-start; }
.msg-role-label {
  font-size: 10px;
  color: #6B7280;
  margin-bottom: 2px;
  padding: 0 4px;
}
.bubble {
  max-width: 75%;
  padding: 10px 14px;
  border-radius: 8px;
  background: #252B38;
  border: 1px solid #2A2F3A;
}
.message.user .bubble {
  background: #2A4A7F;
  border-color: #3A5A9F;
  color: #E8F0FF;
}

/* 脚本执行结果卡片 */
.action-card {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  gap: 4px 8px;
  padding: 8px 12px;
  border-radius: 8px;
  font-size: 12px;
  border: 1px solid transparent;
}
.action-card.ok { background: #0F2A1A; border-color: #1A4A2A; color: #6EE7A0; }
.action-card.fail { background: #2A0F0F; border-color: #4A1A1A; color: #FCA5A5; }
/* 模式切换系统消息 */
.action-card.isModeSwitch { background: #1E1A14; border-color: #4A3D10; color: #E8A830; }
.action-icon { font-size: 13px; }
.action-label { font-weight: 600; }
.action-name { font-family: monospace; background: rgba(255,255,255,0.07); padding: 1px 6px; border-radius: 4px; }
.action-msg { flex: 1 1 100%; margin-top: 2px; opacity: 0.85; }
.action-path { flex: 1 1 100%; font-family: monospace; font-size: 10px; opacity: 0.6; word-break: break-all; }
.action-output, .action-stderr {
  flex: 1 1 100%;
  margin: 4px 0 0;
  padding: 6px 8px;
  border-radius: 4px;
  font-family: 'Fira Code', monospace;
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-all;
  max-height: 150px;
  overflow-y: auto;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.08) transparent;
}
.action-output { background: rgba(0,0,0,0.2); color: inherit; }
.action-stderr { background: rgba(200,0,0,0.1); color: inherit; }
.action-files {
  flex: 1 1 100%;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  margin-top: 6px;
  padding-top: 6px;
  border-top: 1px dashed rgba(255,255,255,0.1);
}
.action-files-label { font-size: 11px; font-weight: 600; white-space: nowrap; }
.action-file-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(255,255,255,0.06);
  font-size: 11px;
  font-family: monospace;
  color: inherit;
  text-decoration: none;
  border: 1px solid rgba(255,255,255,0.1);
  transition: background 0.15s;
}
.action-file-link:hover { background: rgba(255,255,255,0.12); text-decoration: underline; }

/* 状态条 */
.status-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 12px;
  border: 1px solid #2A2F3A;
  background: #252B38;
  animation: status-fade-in 0.2s ease;
}
.status-bar.phase-analyzing { border-color: #1E40AF; background: #0F1A3A; color: #93C5FD; }
.status-bar.phase-loading, .status-bar.phase-loading_child, .status-bar.phase-loading_resources { border-color: #6B21A8; background: #1A0F2A; color: #D8B4FE; }
.status-bar.phase-planning { border-color: #92400E; background: #2A1F0F; color: #FDE68A; }
.status-bar.phase-executing { border-color: #166534; background: #0F2A1A; color: #86EFAC; }
.status-bar.phase-reading { border-color: #075985; background: #0F1A2A; color: #BAE6FD; }
.status-bar.phase-writing { border-color: #9A3412; background: #2A1A0F; color: #FDBA74; }
.status-bar.phase-creating { border-color: #9F1239; background: #2A0F1A; color: #FECDD3; }
.status-bar.phase-generating { border-color: #365314; background: #1A2A0F; color: #D9F99D; }
.status-spinner {
  display: inline-block;
  width: 12px;
  height: 12px;
  border: 2px solid currentColor;
  border-top-color: transparent;
  border-radius: 50%;
  animation: spin 0.8s linear infinite;
  flex-shrink: 0;
}
.status-message { flex: 1; }
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes status-fade-in { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: translateY(0); } }

/* 跳过步骤 */
.skipped-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  padding: 6px 12px;
  border-radius: 6px;
  font-size: 11px;
  border: 1px solid #1A4A2A;
  background: #0F2A1A;
  color: #86EFAC;
}
.skipped-icon { font-size: 13px; flex-shrink: 0; }
.skipped-label { font-weight: 600; white-space: nowrap; }
.skipped-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 1px 6px;
  border-radius: 3px;
  background: rgba(134,239,172,0.1);
  font-size: 10px;
  font-family: monospace;
}

/* 输入区 */
.chat-input-area {
  padding: 10px 16px 12px;
  border-top: 1px solid #2A2F3A;
  background: #1A1E28;
  flex-shrink: 0;
}
.round-files-bar {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 8px;
  padding: 6px 10px;
  margin-bottom: 8px;
  border-radius: 6px;
  background: #0F1A3A;
  border: 1px solid #1E40AF;
  color: #93C5FD;
  font-size: 12px;
}
.round-files-label { font-weight: 600; white-space: nowrap; flex-shrink: 0; }
.round-file-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px;
  border-radius: 4px;
  background: rgba(147,197,253,0.1);
  font-size: 11px;
  font-family: monospace;
  color: #93C5FD;
  text-decoration: none;
  border: 1px solid rgba(147,197,253,0.2);
  transition: background 0.15s;
}
.round-file-link:hover { background: rgba(147,197,253,0.2); text-decoration: underline; }

.upload-chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.upload-chip {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 2px 8px 2px 6px;
  border-radius: 12px;
  background: #252B38;
  border: 1px solid #2A2F3A;
  font-size: 11px;
  font-family: monospace;
  max-width: 240px;
  color: #C0C8D8;
}
.chip-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 140px; }
.chip-size { font-size: 10px; color: #6B7280; }
.chip-remove {
  background: none; border: none; cursor: pointer; padding: 0 2px; font-size: 11px; line-height: 1; color: #C0C8D8; opacity: 0.5; transition: opacity 0.15s;
}
.chip-remove:hover:not(:disabled) { opacity: 1; color: #E55; }
.chip-remove:disabled { cursor: not-allowed; opacity: 0.25; }

.chat-input-row {
  display: flex;
  gap: 8px;
  align-items: flex-end;
}
.chat-input-row textarea {
  flex: 1;
  min-height: 64px;
  max-height: 160px;
  resize: vertical;
  padding: 8px 12px;
  border-radius: 8px;
  border: 1px solid #2A2F3A;
  background: #161A22;
  color: #E2E8F0;
  font: inherit;
  font-size: 13px;
  line-height: 1.5;
  transition: border-color 0.15s;
  scrollbar-width: thin;
  scrollbar-color: rgba(255,255,255,0.08) transparent;
}
.chat-input-row textarea:focus {
  outline: none;
  border-color: #4A90E2;
}
.chat-input-row textarea:disabled {
  opacity: 0.4;
  cursor: not-allowed;
}
.chat-actions { display: flex; flex-direction: column; gap: 6px; align-items: flex-end; }
.send-disabled-hint {
  font-size: 11px;
  color: #D4A017;
  white-space: nowrap;
  margin-top: 2px;
}

/* ===== 按钮体系 ===== */
.btn-primary {
  background: #4A90E2;
  color: #fff;
  border: 1px solid #4A90E2;
  border-radius: 6px;
  padding: 8px 16px;
  font: inherit;
  font-weight: 500;
  cursor: pointer;
  transition: background 0.15s, opacity 0.15s;
  white-space: nowrap;
}
.btn-primary:hover:not(:disabled) { background: #3A7BC8; }
.btn-primary:disabled { opacity: 0.4; cursor: not-allowed; }
.btn-primary.btn-sm { padding: 5px 12px; font-size: 12px; }

.btn-ghost {
  background: #252B38;
  color: #C0C8D8;
  border: 1px solid #2A2F3A;
  border-radius: 6px;
  padding: 8px 14px;
  font: inherit;
  cursor: pointer;
  transition: background 0.15s, border-color 0.15s;
  white-space: nowrap;
}
.btn-ghost:hover:not(:disabled) { background: #2A3040; border-color: #4A90E2; }
.btn-ghost:disabled { opacity: 0.35; cursor: not-allowed; }
.btn-ghost.btn-sm { padding: 5px 10px; font-size: 12px; }

.btn-danger {
  background: transparent;
  color: #E55;
  border: 1px solid #E55;
  border-radius: 6px;
  padding: 8px 14px;
  font: inherit;
  cursor: pointer;
  transition: background 0.15s;
  white-space: nowrap;
}
.btn-danger:hover:not(:disabled) { background: rgba(229,85,85,0.12); }
.btn-danger:disabled { opacity: 0.35; cursor: not-allowed; }
.btn-danger.btn-sm { padding: 5px 10px; font-size: 12px; }

.btn-secondary {
  background: #252B38;
  color: #C0C8D8;
  border: 1px solid #2A2F3A;
  border-radius: 6px;
  padding: 8px 16px;
  font: inherit;
  cursor: pointer;
  display: inline-block;
  transition: background 0.15s;
}
.btn-secondary:hover { background: #2A3040; border-color: #4A90E2; }

.btn-text {
  background: none;
  border: none;
  color: #8892A4;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  padding: 4px 8px;
  border-radius: 4px;
  transition: color 0.15s, background 0.15s;
}
.btn-text:hover:not(:disabled) { color: #E2E8F0; background: rgba(255,255,255,0.06); }
.btn-text:disabled { opacity: 0.35; cursor: not-allowed; }

.zip-import-label { cursor: pointer; }
.zip-import-label.disabled { opacity: 0.35; pointer-events: none; }
.hidden-file-input { display: none; }

/* ===== Modal ===== */
.p16 { padding: 16px; }
.px16 { padding: 4px 16px; }
.muted { color: #8892A4; }
.error { color: #FCA5A5; }

.overlay {
  position: fixed;
  inset: 0;
  background: rgba(0,0,0,0.6);
  display: flex;
  align-items: center;
  justify-content: center;
  z-index: 100;
}
.dialog {
  background: #1E2330;
  border: 1px solid #2A2F3A;
  border-radius: 12px;
  padding: 20px 24px;
  max-width: 420px;
  width: 92%;
  box-shadow: 0 20px 60px rgba(0,0,0,0.4);
  color: #E2E8F0;
}
.dialog p { margin: 0 0 16px; line-height: 1.6; }
.dialog-actions { display: flex; gap: 10px; margin-top: 16px; justify-content: flex-end; }
.dialog-title { font-weight: 600; font-size: 14px; margin-bottom: 12px; font-family: monospace; word-break: break-all; }
.dialog-editor { width: 720px; max-width: 94vw; max-height: 86vh; display: flex; flex-direction: column; }
.asset-edit-textarea {
  flex: 1;
  min-height: 340px;
  resize: vertical;
  border: 1px solid #2A2F3A;
  border-radius: 6px;
  margin-bottom: 4px;
  padding: 12px;
  font-family: monospace;
  font-size: 13px;
  background: #161A22;
  color: #C0C8D8;
}

/* ===== Toast ===== */
.toast-container {
  position: fixed;
  bottom: 24px;
  left: 50%;
  transform: translateX(-50%);
  z-index: 200;
}
.toast {
  padding: 10px 20px;
  border-radius: 8px;
  font-size: 13px;
  font-weight: 500;
  box-shadow: 0 8px 24px rgba(0,0,0,0.3);
  white-space: nowrap;
}
.toast.info { background: #2A4A7F; color: #E8F0FF; }
.toast.error { background: #4A1A1A; color: #FCA5A5; }
.toast-fade-enter-active, .toast-fade-leave-active { transition: opacity 0.25s, transform 0.25s; }
.toast-fade-enter-from, .toast-fade-leave-to { opacity: 0; transform: translateX(-50%) translateY(12px); }

/* ===== 响应式 ===== */
@media (max-width: 900px) {
  .body { flex-direction: column; }
  .list-panel { width: 100%; max-height: 160px; border-right: none; border-bottom: 1px solid #2A2F3A; }
  .action-bar { flex-direction: column; align-items: flex-start; }
  .action-divider { width: 100%; height: 1px; margin: 4px 0; }
  .thinking-sidebar { width: 100%; border-left: none; border-top: 1px solid #2A2F3A; max-height: 260px; }
}

/* ===== P4: 消息操作按钮 ===== */
.msg-actions {
  display: flex;
  gap: 2px;
  margin-top: 2px;
  padding: 0 4px;
  opacity: 0;
  animation: msg-actions-in 0.15s ease forwards;
}
@keyframes msg-actions-in { to { opacity: 1; } }
.msg-action-btn {
  background: none;
  border: none;
  cursor: pointer;
  padding: 2px 4px;
  font-size: 12px;
  border-radius: 3px;
  opacity: 0.5;
  transition: opacity 0.15s, background 0.15s;
  color: #8892A4;
}
.msg-action-btn:hover { opacity: 1; background: rgba(255,255,255,0.08); }
.msg-action-btn.bookmarked { opacity: 1; color: #FBBF24; }

/* P4: 文件附件消息卡片 */
.msg-file-attachments {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 8px;
}
.msg-file-card {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 10px;
  border-radius: 6px;
  background: rgba(255,255,255,0.06);
  border: 1px solid rgba(255,255,255,0.1);
  font-size: 12px;
}
.msg-file-icon { font-size: 14px; }
.msg-file-name { font-family: monospace; word-break: break-all; }

/* ===== P5: 执行过程/方案面板（chat-sidebar 内部） ===== */
.thinking-sidebar {
  flex: 1;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: transparent;
}
.thinking-sidebar-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 8px 12px;
  font-size: 13px;
  font-weight: 600;
  border-bottom: 1px solid #2A2F3A;
  flex-shrink: 0;
  color: #E2E8F0;
}

/* P5: 方案/SOP 面板 */
.plan-tabs {
  display: flex;
  gap: 2px;
}
.plan-tab {
  padding: 4px 10px;
  border: none;
  background: transparent;
  color: #8892A4;
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  border-radius: 4px;
  transition: all 0.15s;
}
.plan-tab:hover { color: #E2E8F0; background: rgba(255,255,255,0.06); }
.plan-tab.active { color: #4A90E2; background: rgba(74,144,226,0.12); font-weight: 600; }

/* 面板滑入动画 */
.panel-slide-enter-active,
.panel-slide-leave-active {
  transition: width 0.25s ease, opacity 0.2s ease;
  overflow: hidden;
}
.panel-slide-enter-from,
.panel-slide-leave-to {
  width: 0;
  opacity: 0;
}
.panel-slide-enter-to,
.panel-slide-leave-from {
  width: 320px;
  opacity: 1;
}

</style>
