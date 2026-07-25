/* ============================================================
 * Q-CC 前端 · main.js
 * 职责：启动初始化：加载配置/斜杠/头像/资料/好友/任务并打开首个会话。必须最后加载。
 * 说明：本文件是原 app.js 的一段(按功能拆分，逻辑未改)。
 *       经典 <script> 顶层声明共享同一全局作用域，靠 index.html
 *       的引入顺序保证依赖可见——请勿单独调整加载顺序。
 * ============================================================ */
/* ================= 启动 ================= */
(async () => {
  await loadConfig();
  await loadSlash();
  await loadAvatars();
  await loadProfile();
  await loadFriends();
  await loadTasks();
  if (TASKS[0]) openTask(TASKS[0].id);
})();
