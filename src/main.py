from __future__ import annotations

import asyncio
from dataclasses import replace

import aiohttp

from .chart_service import DashboardChartService
from .config import COINS, OUTPUT_PATH
from .holder_service import HolderService
from .price_service import PriceService
from .renderer import DashboardRenderer
from .token_report import TokenReportService


async def async_main() -> None:
    async with aiohttp.ClientSession() as session:
        async def get_session() -> aiohttp.ClientSession:
            return session

        token_report_service = TokenReportService(get_session)
        values, holder_counts, chart_series = await asyncio.gather(
            PriceService().fetch_prices(),
            HolderService().fetch_counts([coin.ticker for coin in COINS]),
            DashboardChartService(token_report_service).fetch_series(
                [coin.ticker for coin in COINS]
            ),
        )
    values = [
        replace(
            value,
            holders=(holder_counts.get(value.ticker.upper()).count
                     if holder_counts.get(value.ticker.upper()) is not None
                     else value.holders),
            chart_points=(chart_series.get(value.ticker.upper()).points
                          if chart_series.get(value.ticker.upper()) is not None
                          else value.chart_points),
        )
        for value in values
    ]
    renderer = DashboardRenderer()
    output = renderer.render_to_file(values, OUTPUT_PATH)
    print(f"Dashboard saved to {output}")


def main() -> None:
    asyncio.run(async_main())
