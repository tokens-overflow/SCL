import { TaskEventStream } from './api.js';

export const escapeHtml = value => String(value ?? '')
  .replaceAll('&', '&amp;')
  .replaceAll('<', '&lt;')
  .replaceAll('>', '&gt;')
  .replaceAll('"', '&quot;')
  .replaceAll("'", '&#039;');

export function avatarHtml(value) {
  const avatar = String(value || '🤖');
  return `<span class="avatar-text">${escapeHtml(avatar)}</span>`;
}

function inlineFormat(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+?)`/g, '<code>$1</code>');
}

function renderMarkdown(text) {
  const source = String(text || '');
  const codeBlocks = [];
  const replaced = source.replace(/```([\w+-]*)\n?([\s\S]*?)```/g, (_, language, code) => {
    const index = codeBlocks.length;
    codeBlocks.push({ language: language || 'text', code });
    return `\n@@CODE_BLOCK_${index}@@\n`;
  });
  const rows = replaced.split('\n');
  const html = [];
  let inList = false;
  const closeList = () => {
    if (inList) {
      html.push('</ul>');
      inList = false;
    }
  };
  for (const row of rows) {
    const marker = row.match(/^@@CODE_BLOCK_(\d+)@@$/);
    if (marker) {
      closeList();
      const block = codeBlocks[Number(marker[1])];
      html.push(
        `<div class="code-block"><div class="code-head"><span>${escapeHtml(block.language)}</span>` +
        `<button type="button" class="copy-code" data-code="${escapeHtml(block.code)}">复制</button></div>` +
        `<pre><code>${escapeHtml(block.code.replace(/\n$/, ''))}</code></pre></div>`,
      );
      continue;
    }
    const list = row.match(/^\s*[-*•]\s+(.*)$/);
    if (list) {
      if (!inList) {
        html.push('<ul>');
        inList = true;
      }
      html.push(`<li>${inlineFormat(list[1])}</li>`);
      continue;
    }
    closeList();
    if (/^#{1,4}\s+/.test(row)) {
      html.push(`<h4>${inlineFormat(row.replace(/^#{1,4}\s+/, ''))}</h4>`);
    } else if (row.trim()) {
      html.push(`<p>${inlineFormat(row)}</p>`);
    }
  }
  closeList();
  return html.join('');
}

function formatClock(timestampSeconds = Date.now() / 1000) {
  const date = new Date(timestampSeconds * 1000);
  return `${String(date.getHours()).padStart(2, '0')}:${String(date.getMinutes()).padStart(2, '0')}`;
}

export class ChatController {
  constructor({ api, elements, onTaskStatus, onTaskUpdated, onError }) {
    this.api = api;
    this.elements = elements;
    this.onTaskStatus = onTaskStatus;
    this.onTaskUpdated = onTaskUpdated;
    this.onError = onError;
    this.task = null;
    this.profile = { name: '我', avatar: '🙂' };
    this.stream = null;
    this.view = this.#newView();
    this.#bind();
  }

  #newView() {
    return {
      currentBubble: null,
      currentMessageId: null,
      streamBuffer: '',
      toolRows: new Map(),
      initialized: false,
    };
  }

  #bind() {
    this.elements.sendButton.addEventListener('click', () => this.send());
    this.elements.stopButton.addEventListener('click', () => this.stop());
    this.elements.input.addEventListener('keydown', event => {
      if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
        event.preventDefault();
        this.send();
      }
    });
    this.elements.messages.addEventListener('click', event => {
      const copy = event.target.closest('.copy-code');
      if (copy) {
        navigator.clipboard.writeText(copy.dataset.code || '');
        copy.textContent = '已复制';
        setTimeout(() => { copy.textContent = '复制'; }, 1200);
        return;
      }
      const header = event.target.closest('.tool-head, .thinking-head');
      if (header) header.parentElement.classList.toggle('open');
    });
  }

  setProfile(profile) {
    this.profile = profile || this.profile;
  }

  open(task) {
    this.close();
    this.task = task;
    this.view = this.#newView();
    this.elements.messages.innerHTML = '';
    this.elements.title.textContent = task.title || '聊天';
    this.elements.meta.textContent = task.model ? `模型 ${task.model}` : '';
    this.elements.stopButton.hidden = task.status !== 'running' && task.status !== 'stopping';
    this.stream = new TaskEventStream(task.id, {
      onEvent: event => this.#handleEvent(event),
      onError: () => {},
    });
    this.stream.open();
  }

  close() {
    this.stream?.close();
    this.stream = null;
  }

  async send() {
    const text = this.elements.input.value.trim();
    if (!this.task || !text) return;
    this.elements.sendButton.disabled = true;
    try {
      const updated = await this.api.sendMessage(this.task.id, text);
      this.task = updated;
      this.elements.input.value = '';
      this.onTaskStatus?.(this.task.id, 'running');
      this.elements.stopButton.hidden = false;
      this.onTaskUpdated?.(updated);
    } catch (error) {
      this.onError?.(error);
    } finally {
      this.elements.sendButton.disabled = false;
    }
  }

  async stop() {
    if (!this.task) return;
    try {
      const updated = await this.api.interruptTask(this.task.id);
      this.task = updated;
      this.onTaskUpdated?.(updated);
      this.onTaskStatus?.(updated.id, updated.status);
      this.#systemLine('⏹ 正在停止…');
    } catch (error) {
      this.onError?.(error);
    }
  }

  #handleEvent(event) {
    switch (event.type) {
      case 'x-user':
        this.#closeBubble();
        this.#userMessage(event.text, event.ts);
        break;
      case 'system':
        if (event.subtype === 'init') {
          this.elements.meta.textContent = `模型 ${event.model || ''} · 会话 ${(event.session_id || '').slice(0, 8)}`;
          if (!this.view.initialized) {
            this.#systemLine(`—— Claude Code 已上线（${event.model || ''}）——`);
            this.view.initialized = true;
          }
        }
        break;
      case 'stream_event':
        this.#streamEvent(event.event || {});
        break;
      case 'assistant':
        if (event.message) this.#assistantMessage(event.message);
        break;
      case 'user':
        this.#toolResults(event.message?.content || []);
        break;
      case 'result':
        this.#closeBubble();
        this.#resultLine(event);
        this.onTaskStatus?.(this.task?.id, event.is_error ? 'error' : 'idle');
        this.elements.stopButton.hidden = true;
        break;
      case 'x-sys':
        this.#systemLine(`📎 ${event.text || ''}`);
        break;
      case 'x-stderr':
        this.#systemLine(`⚠ ${String(event.text || '').split('\n').slice(-3).join(' / ').slice(0, 400)}`);
        break;
      case 'x-proc-exit':
        if (event.cancelled) {
          this.#systemLine('⏹ 已停止');
          this.onTaskStatus?.(this.task?.id, 'idle');
        } else if (event.code) {
          this.#systemLine(`进程退出，代码 ${event.code}`);
          this.onTaskStatus?.(this.task?.id, 'error');
        }
        this.elements.stopButton.hidden = true;
        break;
      default:
        break;
    }
  }

  #streamEvent(event) {
    if (event.type === 'message_start' && event.message?.id) {
      this.#ensureBubble(event.message.id);
      return;
    }
    if (event.type === 'content_block_delta' && event.delta?.type === 'text_delta') {
      const bubble = this.#ensureBubble(null);
      this.view.streamBuffer += event.delta.text || '';
      bubble.querySelector('.stream').innerHTML = `${renderMarkdown(this.view.streamBuffer)}<span class="caret"></span>`;
      this.#scrollBottom();
    }
  }

  #ensureBubble(messageId) {
    if (
      this.view.currentBubble &&
      (this.view.currentMessageId === null || messageId == null || this.view.currentMessageId === messageId)
    ) {
      if (messageId != null) this.view.currentMessageId = messageId;
      return this.view.currentBubble;
    }
    const bubble = document.createElement('article');
    bubble.className = 'message agent';
    const name = this.task?.agent_name || 'Claude';
    const avatar = this.task?.agent_avatar || '🤖';
    bubble.innerHTML = `<header>${avatarHtml(avatar)}<span>${escapeHtml(name)}</span><time>${formatClock()}</time></header>` +
      '<div class="message-body"><div class="blocks"></div><div class="stream"></div></div>';
    this.elements.messages.appendChild(bubble);
    this.view.currentBubble = bubble;
    this.view.currentMessageId = messageId || null;
    this.view.streamBuffer = '';
    this.#scrollBottom();
    return bubble;
  }

  #assistantMessage(message) {
    const bubble = this.#ensureBubble(message.id);
    const blocks = bubble.querySelector('.blocks');
    let html = '';
    for (const block of message.content || []) {
      if (block.type === 'text') html += renderMarkdown(block.text);
      if (block.type === 'thinking') {
        html += `<section class="thinking"><button class="thinking-head" type="button">💭 思考…</button>` +
          `<pre>${escapeHtml(block.thinking || '')}</pre></section>`;
      }
      if (block.type === 'tool_use') html += this.#toolRow(block);
    }
    blocks.insertAdjacentHTML('beforeend', html);
    blocks.querySelectorAll('.tool-row').forEach(row => this.view.toolRows.set(row.dataset.toolId, row));
    bubble.querySelector('.stream').innerHTML = '';
    this.view.streamBuffer = '';
    this.#scrollBottom();
  }

  #toolRow(block) {
    const input = block.input || {};
    const summary = input.command || input.file_path || input.pattern || input.url || input.description || input.prompt || '';
    return `<section class="tool-row" data-tool-id="${escapeHtml(block.id || '')}">` +
      `<button type="button" class="tool-head"><span>🔧 ${escapeHtml(block.name || 'Tool')}</span>` +
      `<small>${escapeHtml(String(summary).slice(0, 140))}</small><em>运行中…</em></button>` +
      '<pre class="tool-output"></pre></section>';
  }

  #toolResults(content) {
    for (const block of content) {
      if (block.type !== 'tool_result') continue;
      const row = this.view.toolRows.get(block.tool_use_id);
      if (!row) continue;
      const raw = Array.isArray(block.content)
        ? block.content.map(item => item.text || '').join('\n')
        : String(block.content || '');
      row.querySelector('.tool-output').textContent = raw.slice(0, 8000) || '(无输出)';
      row.querySelector('em').textContent = block.is_error ? '出错 ❌' : '完成 ✔';
    }
  }

  #userMessage(text, timestamp) {
    const message = document.createElement('article');
    message.className = 'message user';
    message.innerHTML = `<header>${avatarHtml(this.profile.avatar)}<span>${escapeHtml(this.profile.name)}</span>` +
      `<time>${formatClock(timestamp)}</time></header><div class="message-body">${renderMarkdown(text)}</div>`;
    this.elements.messages.appendChild(message);
    this.#scrollBottom();
  }

  #systemLine(text) {
    const line = document.createElement('div');
    line.className = 'system-line';
    line.textContent = text;
    this.elements.messages.appendChild(line);
    this.#scrollBottom();
  }

  #resultLine(event) {
    const line = document.createElement('div');
    line.className = `result-line${event.is_error ? ' error' : ''}`;
    const duration = event.duration_ms ? `${(event.duration_ms / 1000).toFixed(1)}s` : '?';
    const cost = event.total_cost_usd == null ? '' : ` · $${Number(event.total_cost_usd).toFixed(4)}`;
    line.textContent = `${event.is_error ? '❌ 出错' : '✔ 本轮完成'} · ${duration}${cost}`;
    this.elements.messages.appendChild(line);
    this.#scrollBottom();
  }

  #closeBubble() {
    this.view.currentBubble = null;
    this.view.currentMessageId = null;
    this.view.streamBuffer = '';
  }

  #scrollBottom() {
    this.elements.messages.scrollTop = this.elements.messages.scrollHeight;
  }
}
