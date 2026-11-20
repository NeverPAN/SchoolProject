"""add experiment name to reconstruction jobs

Revision ID: 7b4d8f1ce921
Revises: e22a01f0fd1e
"""
from alembic import op
import sqlalchemy as sa
revision = "7b4d8f1ce921"
down_revision = "e22a01f0fd1e"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("reconstruction_jobs", sa.Column("experiment_name", sa.String(length=120), nullable=True))

def downgrade() -> None:
    op.drop_column("reconstruction_jobs", "experiment_name")
