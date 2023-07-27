# coding=utf-8
from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
import json

from stundenplan24_py import (
    IndiwareStundenplanerClient, Hosting
)
import aiohttp

from .plan_downloader import PlanDownloader
from .plan_processor import PlanProcessor
from .cache import Cache


class PlanCrawler:
    def __init__(self, plan_downloader: PlanDownloader, plan_processor: PlanProcessor):
        self.plan_downloader = plan_downloader
        self.plan_processor = plan_processor

    async def check_infinite(self, interval: int = 60):
        self.plan_processor.update_all()

        while True:
            downloaded_files = await self.plan_downloader.update_fetch()

            if downloaded_files:
                self.plan_processor.meta_extractor.invalidate_cache()

            for date, revision, downloaded_files_metadata in downloaded_files.items():
                self.plan_processor.update_plans(date, revision)

            if downloaded_files:
                self.plan_processor.update_meta()

            await asyncio.sleep(interval)


async def get_clients(session: aiohttp.ClientSession | None = None) -> dict[str, PlanCrawler]:
    # parse credentials
    with open("creds.json", "r", encoding="utf-8") as f:
        _creds = json.load(f)

    clients = {}

    for school_name, data in _creds.items():
        specifier = data['school_number'] if 'school_number' in data else school_name
        logger = logging.getLogger(specifier)
        cache = Cache(Path(f".cache/{specifier}").absolute())

        hosting = Hosting.deserialize(data["hosting"])
        client = IndiwareStundenplanerClient(hosting, session)

        plan_downloader = PlanDownloader(client, cache, logger=logger)
        plan_processor = PlanProcessor(cache, specifier, logger=logger)

        # create crawler
        p = PlanCrawler(plan_downloader, plan_processor)

        clients |= {school_name: p}

    return clients


async def main():
    argument_parser = argparse.ArgumentParser()

    argument_parser.add_argument("--only-download", action="store_true",
                                 help="Only download plans, do not parse them.")
    argument_parser.add_argument("--only-process", action="store_true",
                                 help="Do not download plans, only parse existing.")
    argument_parser.add_argument("-loglevel", "-l", default="INFO")

    args = argument_parser.parse_args()

    logging.basicConfig(level=args.loglevel, format="[%(asctime)s] [%(levelname)8s] %(name)s: %(message)s",
                        datefmt="%Y-%m-%d %H:%M:%S")

    clients = await get_clients()
    try:
        if args.only_process:
            for client in clients.values():
                client.plan_processor.update_all()
        elif not args.only_download:
            await asyncio.gather(
                *[client.check_infinite() for client in clients.values()]
            )
        else:
            await asyncio.gather(
                *[client.plan_downloader.check_infinite() for client in clients.values()]
            )
    finally:
        logging.debug("Closing clients...")
        await asyncio.gather(*(client.plan_downloader.client.close() for client in clients.values()),
                             return_exceptions=True)

if __name__ == "__main__":
    asyncio.run(main())
