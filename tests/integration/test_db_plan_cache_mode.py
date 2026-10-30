"""Guards the Phase 33 finding: connections must plan with real parameter values."""

from __future__ import annotations

import pytest
from packages.config.settings import get_settings
from packages.database.session import get_engine
from sqlalchemy import text


@pytest.mark.integration
async def test_engine_connections_force_custom_plans() -> None:
    engine = get_engine(get_settings().database)
    async with engine.connect() as conn:
        value = (await conn.execute(text("show plan_cache_mode"))).scalar_one()
    assert value == "force_custom_plan"
