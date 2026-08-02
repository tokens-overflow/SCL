# 数据库迁移

## 为什么 Demo 用 `create_all()`，生产必须用 Alembic

`create_all()` 只会「创建不存在的表」。它**不会**处理列的增删改——
于是第一次给 `task_steps` 加一个字段时，线上会出现
「代码里有这个列、数据库里没有」的错位，而且没有任何提示。

Alembic 的价值在于它把结构变更变成**有版本、可回滚、可审核**的脚本。

## 常用命令

```bash
# 生成迁移（自动对比 ORM 与当前库结构）
alembic revision --autogenerate -m "add xxx"

# 应用到最新
alembic upgrade head

# 回退一步
alembic downgrade -1

# 只生成 SQL 交给 DBA 审核（生产常用）
alembic upgrade head --sql
```

## 关于 SQLite

SQLite 不支持大部分 `ALTER TABLE`，所以 `env.py` 里开了
`render_as_batch=True`——Alembic 会用「建新表 → 拷数据 → 换名」的方式实现。
切到 PostgreSQL 后这个开关不影响正确性，可以保留。
