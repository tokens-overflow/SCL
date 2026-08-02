"""初始表结构

Revision ID: 0001
Revises:
Create Date: 2026-07-01

说明：这份迁移与 `app/state/models.py` 的 ORM 定义等价。
生产环境用 `alembic upgrade head` 建表，
Demo 与测试用 `Database.create_all()`——两者来源同一份模型，不会漂移。
"""
from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_tasks",
        sa.Column("task_id", sa.String(64), primary_key=True),
        sa.Column("user_id", sa.String(64), nullable=False, index=True),
        sa.Column("agent_id", sa.String(64), nullable=False, index=True),
        sa.Column("task_type", sa.String(64), nullable=False, server_default="generic"),
        sa.Column("original_input", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("risk_level", sa.String(16), nullable=False, server_default="LOW"),
        sa.Column("current_step", sa.String(128), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=False, server_default="", index=True),
        sa.Column("result_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_index("ix_task_status_updated", "agent_tasks", ["status", "updated_at"])

    op.create_table(
        "task_steps",
        sa.Column("step_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), sa.ForeignKey("agent_tasks.task_id", ondelete="CASCADE"), nullable=False, index=True),
        sa.Column("step_name", sa.String(128), nullable=False),
        sa.Column("step_type", sa.String(32), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("tool_name", sa.String(128), nullable=True),
        sa.Column("input_payload", sa.JSON(), nullable=True),
        sa.Column("output_payload", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("idempotency_key", sa.String(128), nullable=True, index=True),
        sa.Column("external_reference_id", sa.String(128), nullable=True),
        sa.Column("compensation_status", sa.String(32), nullable=False, server_default="NOT_REQUIRED"),
        sa.Column("critical", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("depends_on", sa.JSON(), nullable=True),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.UniqueConstraint("task_id", "step_name", name="uq_step_task_name"),
    )
    op.create_index("ix_step_status_retry", "task_steps", ["status", "next_retry_at"])

    op.create_table(
        "tool_executions",
        sa.Column("execution_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False, index=True),
        sa.Column("step_id", sa.String(64), nullable=False, index=True),
        sa.Column("tool_name", sa.String(128), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("arguments_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("arguments", sa.JSON(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("retryable", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("external_reference_id", sa.String(128), nullable=True),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=False, server_default=""),
        # 幂等的物理保证。应用层的「先查再写」挡不住并发，这条约束才是最后一道闸。
        sa.UniqueConstraint("idempotency_key", name="uq_execution_idempotency_key"),
    )
    op.create_index("ix_execution_step_status", "tool_executions", ["step_id", "status"])

    op.create_table(
        "approvals",
        sa.Column("approval_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False, index=True),
        sa.Column("step_id", sa.String(64), nullable=False, index=True),
        sa.Column("requested_action", sa.JSON(), nullable=False),
        sa.Column("requester", sa.String(64), nullable=False),
        sa.Column("approver_role", sa.String(64), nullable=False),
        sa.Column("approver_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(32), nullable=False, index=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("decision_comment", sa.Text(), nullable=True),
        sa.Column("risk_level", sa.String(16), nullable=False, server_default="MEDIUM"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_approval_status_expires", "approvals", ["status", "expires_at"])

    op.create_table(
        "audit_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=True, index=True),
        sa.Column("step_id", sa.String(64), nullable=True, index=True),
        sa.Column("event_type", sa.String(64), nullable=False, index=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False, server_default=""),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("trace_id", sa.String(64), nullable=False, server_default="", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_audit_task_created", "audit_events", ["task_id", "created_at"])

    op.create_table(
        "task_checkpoints",
        sa.Column("checkpoint_id", sa.String(64), primary_key=True),
        sa.Column("task_id", sa.String(64), nullable=False, index=True),
        sa.Column("step_name", sa.String(128), nullable=True),
        sa.Column("label", sa.String(64), nullable=False, server_default=""),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "token_mappings",
        sa.Column("token", sa.String(64), primary_key=True),
        sa.Column("entity_type", sa.String(32), nullable=False, index=True),
        sa.Column("original_value", sa.Text(), nullable=False),
        sa.Column("value_hash", sa.String(64), nullable=False, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("entity_type", "value_hash", name="uq_token_entity_value"),
    )

    # ---- 业务表（演示用）----
    op.create_table(
        "customers",
        sa.Column("customer_id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("tier", sa.String(16), nullable=False, server_default="STANDARD"),
        sa.Column("email", sa.String(128), nullable=False, server_default=""),
        sa.Column("phone", sa.String(32), nullable=False, server_default=""),
        sa.Column("department", sa.String(64), nullable=False, server_default="cs_north"),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE"),
        sa.Column("lifetime_value", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "customer_discounts",
        sa.Column("discount_id", sa.String(64), primary_key=True),
        sa.Column("customer_id", sa.String(32), nullable=False, index=True),
        sa.Column("discount_rate", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="ACTIVE", index=True),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.Text(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_discount_idempotency_key"),
    )
    op.create_index("ix_discount_customer_status", "customer_discounts", ["customer_id", "status"])

    op.create_table(
        "notifications",
        sa.Column("notification_id", sa.String(64), primary_key=True),
        sa.Column("customer_id", sa.String(32), nullable=False, index=True),
        sa.Column("channel", sa.String(16), nullable=False, server_default="sms"),
        sa.Column("template", sa.String(64), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="SENT"),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("idempotency_key", name="uq_notification_idempotency_key"),
    )


def downgrade() -> None:
    for table in (
        "notifications", "customer_discounts", "customers", "token_mappings",
        "task_checkpoints", "audit_events", "approvals", "tool_executions",
        "task_steps", "agent_tasks",
    ):
        op.drop_table(table)
