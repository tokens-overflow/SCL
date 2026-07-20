export class ApiError extends Error {
  constructor(message, status = 0, payload = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.payload = payload;
  }
}

async function request(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
  });
  let payload = null;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!response.ok || (payload && payload.error)) {
    throw new ApiError(payload?.error || `请求失败 (${response.status})`, response.status, payload);
  }
  return payload;
}

const post = (path, body = {}) => request(path, {
  method: 'POST',
  body: JSON.stringify(body),
});

export const api = {
  getConfig: () => request('/api/config'),
  getSlashCommands: () => request('/api/slashcommands'),
  getTasks: () => request('/api/tasks'),
  createTask: data => post('/api/tasks', data),
  sendMessage: (id, text) => post(`/api/tasks/${id}/message`, { text }),
  interruptTask: id => post(`/api/tasks/${id}/interrupt`),
  pinTask: (id, pinned) => post(`/api/tasks/${id}/pin`, { pinned }),
  deleteTask: id => post(`/api/tasks/${id}/delete`),
  addTaskDir: (id, path) => post(`/api/tasks/${id}/adddir`, { path }),
  removeTaskDir: (id, path) => post(`/api/tasks/${id}/rmdir`, { path }),

  getFriends: () => request('/api/friends'),
  createFriend: data => post('/api/friends', data),
  deleteFriend: id => post(`/api/friends/${id}/delete`),

  getProfile: () => request('/api/profile'),
  saveProfile: data => post('/api/profile', data),

  getMoments: () => request('/api/moments'),
  createMoment: text => post('/api/moments', { text }),
  likeMoment: id => post(`/api/moments/${id}/like`),
  deleteMoment: id => post(`/api/moments/${id}/delete`),

  getSkills: () => request('/api/skills'),
  getSkill: dir => request(`/api/skills/${encodeURIComponent(dir)}`),
  createSkill: data => post('/api/skills', data),
  saveSkill: (dir, data) => post(`/api/skills/${encodeURIComponent(dir)}/save`, data),
  deleteSkill: dir => post(`/api/skills/${encodeURIComponent(dir)}/delete`),

  getCapabilities: () => request('/api/capabilities'),
  getClaudeMd: project => request(`/api/claudemd?project=${encodeURIComponent(project)}`),
  saveClaudeMd: data => post('/api/claudemd', data),

  getSchedules: () => request('/api/schedules'),
  createSchedule: data => post('/api/schedules', data),
  toggleSchedule: (id, enabled) => post(`/api/schedules/${id}/toggle`, { enabled }),
  runSchedule: id => post(`/api/schedules/${id}/run`),
  deleteSchedule: id => post(`/api/schedules/${id}/delete`),
};

export class TaskEventStream {
  constructor(taskId, { onEvent, onReady, onError }) {
    this.taskId = taskId;
    this.onEvent = onEvent;
    this.onReady = onReady;
    this.onError = onError;
    this.source = null;
  }

  open() {
    this.close();
    this.source = new EventSource(`/api/tasks/${this.taskId}/events`);
    this.source.onmessage = event => {
      try {
        this.onEvent?.(JSON.parse(event.data));
      } catch (error) {
        console.warn('忽略无法解析的 SSE 事件', error);
      }
    };
    this.source.addEventListener('ready', () => this.onReady?.());
    this.source.onerror = error => this.onError?.(error);
  }

  close() {
    if (this.source) {
      this.source.close();
      this.source = null;
    }
  }
}
