"""omni fleet provider specs

Universal Ingestion Matrix: a config-driven provider is an omni_fleet_sources row whose spec holds
its ProviderSpec JSON (endpoints, auth, rate limit, JSONPath mapping). Built-in adapters keep NULL.

Revision ID: 2ca48d2a6d3e
Revises: e2e4d037638c
Create Date: 2026-10-08 14:39:05.074658

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '2ca48d2a6d3e'
down_revision: Union[str, Sequence[str], None] = 'e2e4d037638c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('omni_fleet_sources', sa.Column('spec', sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), 'postgresql'), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('omni_fleet_sources', 'spec')
