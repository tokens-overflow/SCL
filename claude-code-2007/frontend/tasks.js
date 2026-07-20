import { api } from './api.js';
import { ChatController, avatarHtml, escapeHtml } from './chat.js';
import { SchedulerController } from './scheduler.js';

const $ = selector => document.querySelector(selector);
const $$ = selector => [...document.querySelectorAll(selector)];

class AppController {
  constructor() {
    this.config = { projects: [], models: [] };
    this.profile = { name: '我', avatar: '🙂' };
    this.tasks = [];
    this.friends = [];
    this.currentTaskId = null;
    this.currentView = 'chat';

    this.chat = new ChatController({
      api,
      elements: {
        title: $('#chat-title'),
        meta: $('#chat-meta'),
        messages: $('#messages'),
        input: $('#prompt'),
        sendButton: $('#send-button'),
        stopButton: $('#stop-button'),
      },
      onTaskStatus: (id, status) => this.setTaskStatus(id, status),
      onTaskUpdated: task => this.upsertTask(task),
      onError: error => this.showError(error),
    });

    this.scheduler = new SchedulerController({
      api,
      elements: {
        list: $('#schedule-list'),
        title: $('#schedule-title'),
        project: $('#schedule-project'),
        model: $('#schedule-model'),
        permission: $('#schedule-permission'),
        prompt: $('#schedule-prompt'),
        type: $('#schedule-type'),
        interval: $('#schedule-interval'),
        time: $('#schedule-time'),
        once: $('#schedule-once'),
        createButton: $('#schedule-create'),
      },
      getConfig: () => this.config,
      onTaskCreated: task => {
        this.upsertTask(task);
        this.openTask(task.id);
      },
      onError: error => this.showError(error),
    });

    this.#bind();
  }

  async init() {
    try {
      [this.config, this.profile, this.tasks, this.friends] = await Promise.all([
        api.getConfig(),
        api.getProfile(),
        api.getTasks(),
        api.getFriends(),
      ]);
      this.chat.setProfile(this.profile);
      this.#renderProfile();
      this.#fillTaskForm();
      this.scheduler.fillSelectors();
      this.renderTasks();
      this.renderFriends();
      await this.scheduler.load();
      await this.loadMoments();
      if (this.tasks[0]) this.openTask(this.tasks[0].id);
      else this.#emptyChat();
    } catch (error) {
      this.showError(error);
    }
  }

  #bind() {
    $('#new-task-button').addEventListener('click', () => this.openTaskModal());
    $('#task-modal-cancel').addEventListener('click', () => this.closeTaskModal());
    $('#task-modal-create').addEventListener('click', () => this.createTask());
    $('#task-search').addEventListener('input', () => this.renderTasks());
    $('#task-list').addEventListener('click', event => this.#handleTaskListClick(event));
    $('#pinned-task-list').addEventListener('click', event => this.#handleTaskListClick(event));

    $$('[data-view]').forEach(button => button.addEventListener('click', () => this.showView(button.dataset.view)));

    $('#friend-list').addEventListener('click', event => this.#handleFriendClick(event));
    $('#friend-create').addEventListener('click', () => this.createFriend());
    $('#profile-save').addEventListener('click', () => this.saveProfile());

    $('#claudemd-project').addEventListener('change', () => this.loadClaudeMd());
    $('#claudemd-save').addEventListener('click', () => this.saveClaudeMd());

    $('#skill-new').addEventListener('click', () => this.newSkill());
    $('#skill-save').addEventListener('click', () => this.saveSkill());
    $('#skill-delete').addEventListener('click', () => this.deleteSkill());
    $('#skill-list').addEventListener('click', event => {
      const row = event.target.closest('[data-skill]');
      if (row) this.openSkill(row.dataset.skill);
    });

    $('#moment-create').addEventListener('click', () => this.createMoment());
    $('#moment-list').addEventListener('click', event => this.#handleMomentClick(event));

    $('#schedule-type').addEventListener('change', event => {
      const type = event.target.value;
      $('#schedule-interval-wrap').hidden = type !== 'interval';
      $('#schedule-time-wrap').hidden = type !== 'daily';
      $('#schedule-once-wrap').hidden = type !== 'once';
    });

    $('#legacy-link').addEventListener('click', () => window.open('/legacy', '_blank', 'noopener'));
  }

  showView(name) {
    this.currentView = name;
    $$('.view').forEach(view => view.classList.toggle('active', view.id === `view-${name}`));
    $$('[data-view]').forEach(button => button.classList.toggle('active', button.dataset.view === name));
    if (name === 'schedules') this.scheduler.load();
    if (name === 'skills') this.loadSkills();
    if (name === 'claudemd') this.loadClaudeMd();
    if (name === 'capabilities') this.loadCapabilities();
    if (name === 'moments') this.loadMoments();
  }

  #fillTaskForm() {
    const projectOptions = (this.config.projects || [])
      .map(project => `<option value="${escapeHtml(project.name)}"${project.exists ? '' : ' disabled'}>` +
        `${escapeHtml(project.name)} — ${escapeHtml(project.path)}${project.exists ? '' : '（缺失）'}</option>`)
      .join('');
    const modelOptions = (this.config.models || [this.config.default_model])
      .map(model => `<option value="${escapeHtml(model)}"${model === this.config.default_model ? ' selected' : ''}>${escapeHtml(model)}</option>`)
      .join('');
    $('#task-project').innerHTML = projectOptions;
    $('#task-model').innerHTML = modelOptions;
    $('#friend-project').innerHTML = `<option value="">默认项目</option>${projectOptions}`;
    $('#friend-model').innerHTML = `<option value="">默认模型</option>${modelOptions}`;
    $('#claudemd-project').innerHTML = projectOptions;
  }

  #renderProfile() {
    $('#profile-name-label').textContent = this.profile.name || '我';
    $('#profile-avatar-label').innerHTML = avatarHtml(this.profile.avatar);
    $('#profile-name').value = this.profile.name || '';
    $('#profile-avatar').value = this.profile.avatar || '';
  }

  async saveProfile() {
    try {
      this.profile = await api.saveProfile({
        name: $('#profile-name').value,
        avatar: $('#profile-avatar').value,
      });
      this.chat.setProfile(this.profile);
      this.#renderProfile();
      this.toast('资料已保存');
    } catch (error) {
      this.showError(error);
    }
  }

  openTaskModal({ title = '', prompt = '', project = '' } = {}) {
    $('#task-title').value = title;
    $('#task-prompt').value = prompt;
    if (project) $('#task-project').value = project;
    $('#task-modal').classList.add('show');
    $('#task-prompt').focus();
  }

  closeTaskModal() {
    $('#task-modal').classList.remove('show');
  }

  async createTask() {
    const prompt = $('#task-prompt').value.trim();
    if (!prompt) return $('#task-prompt').focus();
    try {
      const task = await api.createTask({
        title: $('#task-title').value.trim(),
        project: $('#task-project').value,
        model: $('#task-model').value,
        permission_mode: $('#task-permission').value,
        prompt,
      });
      this.closeTaskModal();
      $('#task-title').value = '';
      $('#task-prompt').value = '';
      this.upsertTask(task);
      this.openTask(task.id);
    } catch (error) {
      this.showError(error);
    }
  }

  upsertTask(task) {
    const index = this.tasks.findIndex(item => item.id === task.id);
    if (index >= 0) this.tasks[index] = { ...this.tasks[index], ...task };
    else this.tasks.unshift(task);
    this.renderTasks();
  }

  setTaskStatus(id, status) {
    const task = this.tasks.find(item => item.id === id);
    if (task) task.status = status;
    this.renderTasks();
  }

  renderTasks() {
    const query = $('#task-search').value.trim().toLowerCase();
    const visible = this.tasks.filter(task => !query || (task.title || '').toLowerCase().includes(query));
    $('#pinned-task-list').innerHTML = visible.filter(task => task.pinned).map(task => this.#taskHtml(task)).join('') || '<div class="compact-empty">暂无置顶</div>';
    $('#task-list').innerHTML = visible.filter(task => !task.pinned).map(task => this.#taskHtml(task)).join('') || '<div class="compact-empty">暂无聊天</div>';
  }

  #taskHtml(task) {
    return `<article class="task-row${task.id === this.currentTaskId ? ' active' : ''}" data-task="${task.id}">` +
      `<span class="task-avatar">${avatarHtml(task.agent_avatar || '🤖')}</span>` +
      `<span class="task-dot ${escapeHtml(task.status || 'idle')}"></span>` +
      `<span class="task-title">${escapeHtml(task.title || '未命名')}</span>` +
      `<button type="button" data-pin="${task.id}" title="${task.pinned ? '取消置顶' : '置顶'}">${task.pinned ? '📌' : '📍'}</button>` +
      `<button type="button" data-delete-task="${task.id}" class="icon-danger" title="删除">×</button></article>`;
  }

  async #handleTaskListClick(event) {
    const pin = event.target.closest('[data-pin]');
    if (pin) {
      event.stopPropagation();
      const task = this.tasks.find(item => item.id === pin.dataset.pin);
      if (!task) return;
      try {
        this.upsertTask(await api.pinTask(task.id, !task.pinned));
      } catch (error) {
        this.showError(error);
      }
      return;
    }
    const remove = event.target.closest('[data-delete-task]');
    if (remove) {
      event.stopPropagation();
      if (!confirm('删除这条聊天记录？')) return;
      try {
        await api.deleteTask(remove.dataset.deleteTask);
        this.tasks = this.tasks.filter(item => item.id !== remove.dataset.deleteTask);
        if (this.currentTaskId === remove.dataset.deleteTask) {
          this.currentTaskId = null;
          this.chat.close();
          this.#emptyChat();
        }
        this.renderTasks();
      } catch (error) {
        this.showError(error);
      }
      return;
    }
    const row = event.target.closest('[data-task]');
    if (row) this.openTask(row.dataset.task);
  }

  openTask(id) {
    const task = this.tasks.find(item => item.id === id);
    if (!task) return;
    this.currentTaskId = id;
    this.showView('chat');
    this.renderTasks();
    this.chat.open(task);
  }

  #emptyChat() {
    $('#chat-title').textContent = '欢迎使用';
    $('#chat-meta').textContent = '';
    $('#messages').innerHTML = '<div class="hero-empty"><div>🐧</div><h2>Claude Code 2007</h2><p>新建任务，或从右侧好友开始一段对话。</p></div>';
  }

  renderFriends() {
    $('#friend-count').textContent = String(this.friends.length);
    $('#friend-list').innerHTML = this.friends.map(friend => `
      <article class="friend-row" data-friend="${friend.id}">
        <span>${avatarHtml(friend.avatar)}</span>
        <div><strong>${escapeHtml(friend.name)}</strong><small>[在线] ${escapeHtml(friend.sign || '')}</small></div>
        <button type="button" data-delete-friend="${friend.id}" title="删除">×</button>
      </article>`).join('') || '<div class="compact-empty">暂无好友</div>';
  }

  async #handleFriendClick(event) {
    const remove = event.target.closest('[data-delete-friend]');
    if (remove) {
      event.stopPropagation();
      if (!confirm('删除这个好友？')) return;
      try {
        await api.deleteFriend(remove.dataset.deleteFriend);
        this.friends = this.friends.filter(item => item.id !== remove.dataset.deleteFriend);
        this.renderFriends();
      } catch (error) {
        this.showError(error);
      }
      return;
    }
    const row = event.target.closest('[data-friend]');
    if (!row) return;
    const friend = this.friends.find(item => item.id === row.dataset.friend);
    if (!friend) return;
    const prompt = `${friend.persona || ''}\n\n请用上面的人设，跟我打个招呼、简短开场（别超过三句）。之后一直保持这个人设跟我聊。`;
    try {
      const task = await api.createTask({
        title: `💬 ${friend.name}`,
        project: friend.project || this.config.projects.find(item => item.exists)?.name || '',
        model: friend.model || this.config.default_model,
        permission_mode: this.config.default_permission_mode,
        prompt,
        agent_name: friend.name,
        agent_avatar: friend.avatar,
      });
      this.upsertTask(task);
      this.openTask(task.id);
    } catch (error) {
      this.showError(error);
    }
  }

  async createFriend() {
    const name = $('#friend-name').value.trim();
    if (!name) return $('#friend-name').focus();
    try {
      const friend = await api.createFriend({
        name,
        avatar: $('#friend-avatar').value.trim() || '🙂',
        sign: $('#friend-sign').value.trim(),
        persona: $('#friend-persona').value.trim(),
        project: $('#friend-project').value,
        model: $('#friend-model').value,
      });
      this.friends.push(friend);
      ['friend-name', 'friend-avatar', 'friend-sign', 'friend-persona'].forEach(id => { $(`#${id}`).value = ''; });
      this.renderFriends();
    } catch (error) {
      this.showError(error);
    }
  }

  async loadClaudeMd() {
    const project = $('#claudemd-project').value;
    if (!project) return;
    try {
      const value = await api.getClaudeMd(project);
      $('#claudemd-path').textContent = value.path || '';
      $('#claudemd-content').value = value.content || '';
    } catch (error) {
      this.showError(error);
    }
  }

  async saveClaudeMd() {
    try {
      const result = await api.saveClaudeMd({
        project: $('#claudemd-project').value,
        content: $('#claudemd-content').value,
      });
      $('#claudemd-path').textContent = result.path || '';
      this.toast('CLAUDE.md 已保存');
    } catch (error) {
      this.showError(error);
    }
  }

  async loadCapabilities() {
    try {
      const capabilities = await api.getCapabilities();
      $('#capabilities-json').textContent = JSON.stringify(capabilities, null, 2);
    } catch (error) {
      this.showError(error);
    }
  }

  async loadSkills() {
    try {
      const skills = await api.getSkills();
      $('#skill-list').innerHTML = skills.map(skill => `
        <button type="button" class="skill-row" data-skill="${escapeHtml(skill.dir)}">
          <strong>${escapeHtml(skill.name)}</strong><small>${escapeHtml(skill.description || '')}</small>
        </button>`).join('') || '<div class="empty-panel">没有发现 ~/.claude/skills。</div>';
    } catch (error) {
      this.showError(error);
    }
  }

  newSkill() {
    $('#skill-dir').value = '';
    $('#skill-name').value = '';
    $('#skill-description').value = '';
    $('#skill-body').value = '';
    $('#skill-name').disabled = false;
    $('#skill-delete').hidden = true;
  }

  async openSkill(dir) {
    try {
      const skill = await api.getSkill(dir);
      $('#skill-dir').value = skill.dir;
      $('#skill-name').value = skill.name;
      $('#skill-name').disabled = true;
      $('#skill-description').value = skill.description || '';
      $('#skill-body').value = skill.body || '';
      $('#skill-delete').hidden = false;
    } catch (error) {
      this.showError(error);
    }
  }

  async saveSkill() {
    const dir = $('#skill-dir').value;
    const payload = {
      name: $('#skill-name').value.trim(),
      description: $('#skill-description').value,
      body: $('#skill-body').value,
    };
    if (!payload.name && !dir) return $('#skill-name').focus();
    try {
      const skill = dir ? await api.saveSkill(dir, payload) : await api.createSkill(payload);
      await this.loadSkills();
      await this.openSkill(skill.dir);
      this.toast('Skill 已保存');
    } catch (error) {
      this.showError(error);
    }
  }

  async deleteSkill() {
    const dir = $('#skill-dir').value;
    if (!dir || !confirm(`删除 skill ${dir}？`)) return;
    try {
      await api.deleteSkill(dir);
      this.newSkill();
      await this.loadSkills();
    } catch (error) {
      this.showError(error);
    }
  }

  async loadMoments() {
    try {
      const moments = await api.getMoments();
      $('#moment-list').innerHTML = moments.map(moment => `
        <article class="moment-card">
          <header>${avatarHtml(moment.author_avatar)}<strong>${escapeHtml(moment.author_name)}</strong><time>${new Date(moment.ts * 1000).toLocaleString('zh-CN', { hour12: false })}</time></header>
          <p>${escapeHtml(moment.text)}</p>
          <footer><button type="button" data-like-moment="${moment.id}">👍 ${Number(moment.likes || 0)}</button>` +
          `${moment.mine ? `<button type="button" data-delete-moment="${moment.id}" class="danger-button">删除</button>` : ''}</footer>
        </article>`).join('') || '<div class="empty-panel">还没有动态。</div>';
    } catch (error) {
      this.showError(error);
    }
  }

  async createMoment() {
    const text = $('#moment-text').value.trim();
    if (!text) return;
    try {
      await api.createMoment(text);
      $('#moment-text').value = '';
      await this.loadMoments();
    } catch (error) {
      this.showError(error);
    }
  }

  async #handleMomentClick(event) {
    const like = event.target.closest('[data-like-moment]');
    const remove = event.target.closest('[data-delete-moment]');
    try {
      if (like) await api.likeMoment(like.dataset.likeMoment);
      if (remove && confirm('删除这条动态？')) await api.deleteMoment(remove.dataset.deleteMoment);
      if (like || remove) await this.loadMoments();
    } catch (error) {
      this.showError(error);
    }
  }

  showError(error) {
    console.error(error);
    const box = $('#error-toast');
    box.textContent = error?.message || String(error);
    box.classList.add('show');
    setTimeout(() => box.classList.remove('show'), 4200);
  }

  toast(message) {
    const box = $('#success-toast');
    box.textContent = message;
    box.classList.add('show');
    setTimeout(() => box.classList.remove('show'), 1800);
  }
}

const app = new AppController();
app.init();
