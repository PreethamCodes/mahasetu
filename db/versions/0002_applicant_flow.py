"""Applicant sessions, one-time codes, department progress, message outbox and result detail."""
from pathlib import Path

from alembic import op

revision = "mahasetu_0002"
down_revision = "mahasetu_0001"
branch_labels = None
depends_on = None


def upgrade():
    sql = Path(__file__).with_suffix(".sql").read_text(encoding="utf-8")
    op.get_bind().exec_driver_sql(sql)


def downgrade():
    raise RuntimeError("Restore from a reviewed backup; this revision adds applicant-flow data.")
