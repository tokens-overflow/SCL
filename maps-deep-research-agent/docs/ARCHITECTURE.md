# 架构说明

## 1. 整体流程

```
用户主题 ──► Planner (DeepSeek JSON 模式) ──► TaskNode[]（DAG）
                                                │
                                                ▼
                                       TaskExecutor（拓扑调度）
                                       ├─ places  ───► GoogleMapsClient
                                       ├─ geocoding ─►       …
                                       ├─ directions
                                       └─ distance_matrix
                                                │
                                                ▼
                            每个 TaskNode 的 evidence + 流式 summary
                                                │
                       ┌────────────────────────┴───────────────────────┐
                       ▼                                                ▼
                Reporter (DeepSeek)                       Itinerary (DeepSeek JSON)
                       │                                                │
                       └──► report_markdown          map_overview + itinerary
```

## 2. 核心组件

### Planner（`services/planner.py`）

- 使用 DeepSeek 的 **JSON 模式**（`response_format={"type": "json_object"}`）保证可解析；
- LLM 必须把任务映射到 4 个工具之一：`places` / `geocoding` / `directions` / `distance_matrix`；
- 校验阶段：
  - 截断超过 `max_tasks` 的额外任务；
  - 去除自循环；
  - 用 DFS 上色算法检测并打破环依赖；
  - 把未知工具回退到 `places`。

### TaskExecutor（`services/executor.py`）

DAG 调度：

```
remaining_deps[id] = len(depends_on)
ready  = { id | remaining_deps[id] == 0 }
while pending > 0:
    nid = ready.get()              # 等待至少一个任务就绪
    asyncio.create_task(runner(nid))
    pending -= 1

runner(node):
    async with sem:                # 并发上限 = task_concurrency
        async with tool.run(...)   # 执行实际工具调用
    for child in children[node.id]:
        remaining_deps[child] -= 1
        if remaining_deps[child] == 0:
            await ready.put(child)
```

**依赖传值**：当下游任务缺少坐标时，自动从上游 `geocoding`/`places` 任务中
拷贝第一个地点的 `lat/lng` 当作 location bias —— 这是 chapter 14 完全没有的。

### Maps 工具层（`tools/maps/*`）

每个工具实现 `Tool` 抽象接口：

```python
class Tool(abc.ABC):
    name: str
    description: str
    async def run(self, args: dict) -> ToolResult: ...
```

`GoogleMapsClient` 提供五个底层方法（text_search / nearby / geocode / directions /
distance_matrix），所有调用都过 `MapsCache`：

- L1: 内存 LRU，按 `(tool, args)` 的 sha256 做键；
- L2: `diskcache`，可跨进程复用；
- TTL 由 `CACHE_TTL_SECONDS` 控制。

### DeepSeek 客户端（`llm/client.py`）

- 直接调用 OpenAI 兼容 endpoint，不经过 `hello_agents`；
- 提供三种模式：
  - `chat`：普通同步对话；
  - `chat_json`：强制 JSON 输出 + 解析容错；
  - `stream_chat`：流式输出，附带 token 统计；
- 使用 `tenacity` 做指数退避重试。

### 事件模型（`models.py`）

所有 SSE 事件都是 Pydantic 模型，序列化时带 `type` 鉴别器：

```
status | plan_ready | task_update | summary_chunk |
tool_call | tool_result | report | usage | error | done
```

前端的 `types/events.ts` 完全对齐这套联合类型，便于做穷举 switch。

## 3. 增强点对比（vs chapter 14）

| 维度          | chapter 14                                       | 本项目 |
| ------------- | ------------------------------------------------ | ------ |
| 任务模型      | flat list                                        | **DAG**，含依赖传值 |
| 工具         | `note` + 搜索 SDK                                  | **可插拔 Tool 接口** + 4 个 Maps 工具 |
| LLM 客户端    | `hello_agents`                                   | **直接 DeepSeek SDK**（JSON 模式 / 流式 / 用量统计） |
| 任务并发      | 多线程 + 锁                                       | **asyncio Semaphore + 拓扑队列** |
| 事件          | `dict` + 字符串 type                              | **Pydantic 类型事件 + SSE event 行** |
| 缓存          | 无                                                | **LRU + diskcache 双层** |
| 重试          | 无                                                | **指数退避（LLM 与 HTTP）** |
| 用量统计      | 无                                                | **token 与 Maps 调用 实时统计** |
| 前端          | Markdown                                          | **Markdown + 交互地图 + 行程时间线** |
| 配置          | os.environ                                        | **pydantic-settings + per-request overrides** |
| 死锁防护      | 无                                                | **拓扑队列含 force-unblock 兜底** |
| 输出          | 单一 Markdown                                     | **Markdown + JSON 行程 + map_overview** |

## 4. 失败模式与兜底

- Planner 输出非法 JSON → 回退为单一 `places` 任务；
- 工具调用异常 → `TaskNode.status = failed`，不影响其它任务；
- 行程构建失败 → 返回空数组，不阻塞主报告；
- 浏览器中断 SSE → 后端的 `asyncio.CancelledError` 路径会清理状态；
- 缓存损坏 → `MapsCache.get` 返回 None 自动回退到真实调用。
