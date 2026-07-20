import { escapeHtml } from './chat.js';

function formatDateTime(timestamp) {
  if (!timestamp) return '—';
  return new Date(timestamp * 1000).toLocaleString('zh-CN', { hour12: false });
}

export class SchedulerController {
  constructor({ api, elements, getConfig, onTaskCreated, onError }) {
    this.api = api;
    this.elements = elements;
    this.getConfig = getConfig;
    this.onTaskCreated = onTaskCreated;
    this.onError = onError;
    this.items = [];
    this.#bind();
  }

  #bind() {
    this.elements.createButton.addEventListener('click', () => this.create());
    this.elements.list.addEventListener('click', event => {
      const toggle = event.target.closest('[data-schedule-toggle]');
      if (toggle) return this.toggle(toggle.dataset.scheduleToggle, toggle.dataset.enabled !== 'true');
      const run = event.target.closest('[data-schedule-run]');
      if (run) return this.run(run.dataset.scheduleRun);
      const remove = event.target.closest('[data-schedule-delete]');
      if (remove) return this.delete(remove.dataset.scheduleDelete);
    });
  }

  async load() {
    try {
      this.items = await this.api.getSchedules();
      this.render();
    } catch (error) {
      this.onError?.(error);
    }
  }

  fillSelectors() {
    const config = this.getConfig();
    const project = this.elements.project;
    const model = this.elements.model;
    project.innerHTML = (config.projects || [])
      .filter(item => item.exists)
      .map(item => `<option value="${escapeHtml(item.name)}">${escapeHtml(item.name)} — ${escapeHtml(item.path)}</option>`)
      .join('');
    model.innerHTML = (config.models || [config.default_model])
      .map(item => `<option value="${escapeHtml(item)}"${item === config.default_model ? ' selected' : ''}>${escapeHtml(item)}</option>`)
      .join('');
  }

  async create() {
    const prompt = this.elements.prompt.value.trim();
    if (!prompt) {
      this.elements.prompt.focus();
      return;
    }
    const type = this.elements.type.value;
    const payload = {
      title: this.elements.title.value.trim(),
      project: this.elements.project.value,
      model: this.elements.model.value,
      permission_mode: this.elements.permission.value,
      prompt,
      sched_type: type,
      interval_min: Number(this.elements.interval.value || 60),
      at_time: this.elements.time.value || '09:00',
    };
    if (type === 'once') {
      const raw = this.elements.once.value;
      if (!raw) {
        this.elements.once.focus();
        return;
      }
      payload.at_datetime = new Date(raw).getTime() / 1000;
    }
    try {
      const created = await this.api.createSchedule(payload);
      this.items.unshift(created);
      this.elements.title.value = '';
      this.elements.prompt.value = '';
      this.render();
    } catch (error) {
      this.onError?.(error);
    }
  }

  async toggle(id, enabled) {
    try {
      const updated = await this.api.toggleSchedule(id, enabled);
      this.#replace(updated);
      this.render();
    } catch (error) {
      this.onError?.(error);
    }
  }

  async run(id) {
    try {
      const task = await this.api.runSchedule(id);
      this.onTaskCreated?.(task);
      await this.load();
    } catch (error) {
      this.onError?.(error);
    }
  }

  async delete(id) {
    if (!confirm('删除这个定时任务？')) return;
    try {
      await this.api.deleteSchedule(id);
      this.items = this.items.filter(item => item.id !== id);
      this.render();
    } catch (error) {
      this.onError?.(error);
    }
  }

  #replace(updated) {
    const index = this.items.findIndex(item => item.id === updated.id);
    if (index >= 0) this.items[index] = updated;
    else this.items.unshift(updated);
  }

  render() {
    if (!this.items.length) {
      this.elements.list.innerHTML = '<div class="empty-panel">还没有定时任务。</div>';
      return;
    }
    this.elements.list.innerHTML = this.items.map(item => `
      <article class="schedule-card">
        <header><strong>${escapeHtml(item.title || item.prompt.slice(0, 24))}</strong>
          <span class="status-pill ${item.enabled ? 'ok' : ''}">${item.enabled ? '启用' : '暂停'}</span></header>
        <p>${escapeHtml(item.prompt)}</p>
        <dl>
          <div><dt>类型</dt><dd>${escapeHtml(item.sched_type)}</dd></div>
          <div><dt>下次</dt><dd>${formatDateTime(item.next_run)}</dd></div>
          <div><dt>上次</dt><dd>${formatDateTime(item.last_run)}</dd></div>
          ${item.last_error ? `<div class="wide"><dt>错误</dt><dd class="danger">${escapeHtml(item.last_error)}</dd></div>` : ''}
        </dl>
        <footer>
          <button type="button" data-schedule-toggle="${item.id}" data-enabled="${item.enabled}">${item.enabled ? '暂停' : '启用'}</button>
          <button type="button" data-schedule-run="${item.id}">立即运行</button>
          <button type="button" class="danger-button" data-schedule-delete="${item.id}">删除</button>
        </footer>
      </article>`).join('');
  }
}
