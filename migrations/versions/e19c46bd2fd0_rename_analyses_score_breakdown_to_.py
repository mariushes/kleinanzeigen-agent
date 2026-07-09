"""rename analyses.score_breakdown to verdict_axes

Revision ID: e19c46bd2fd0
Revises: 50f9532be7ea
Create Date: 2026-07-09 10:55:57.105115

"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e19c46bd2fd0'
down_revision: Union[str, Sequence[str], None] = '50f9532be7ea'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Rename in place (not drop+add) so existing verdicts keep their axis data."""
    op.alter_column('analyses', 'score_breakdown', new_column_name='verdict_axes')


def downgrade() -> None:
    op.alter_column('analyses', 'verdict_axes', new_column_name='score_breakdown')
