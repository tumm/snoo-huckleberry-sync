"""Utility script to query and display SNOO baby IDs and Huckleberry child UIDs."""

import asyncio

# Import Windows gRPC SSL setup before importing packages that use gRPC
from .ssl_helper import make_aiohttp_connector, setup_grpc_ssl

setup_grpc_ssl()

import aiohttp  # noqa: E402
from python_snoo.snoo import Snoo  # noqa: E402

from . import config  # noqa: E402
from .huckleberry_sink import make_huckleberry_client  # noqa: E402
from .snoo_source import BABIES_URL  # noqa: E402


async def main() -> None:
    print("--- FETCHING SNOO BABIES ---")
    async with aiohttp.ClientSession(connector=make_aiohttp_connector()) as session:
        try:
            snoo = Snoo(config.SNOO_USERNAME, config.SNOO_PASSWORD, session)
            await snoo.authorize()
            hdrs = snoo.generate_snoo_auth_headers(snoo.tokens.aws_id)
            async with session.get(BABIES_URL, headers=hdrs) as r:
                r.raise_for_status()
                babies = await r.json()

            for b in babies:
                name = b.get("babyName") or b.get("name") or b.get("givenName") or "Unknown"
                print(f"  Name: {name:<15} | SNOO Baby ID: {b.get('_id')}")

            if snoo.reauth_task:
                snoo.reauth_task.cancel()
        except Exception as e:
            print(f"  Failed to fetch SNOO babies: {e}")

    print("\n--- FETCHING HUCKLEBERRY CHILDREN ---")
    async with aiohttp.ClientSession(connector=make_aiohttp_connector()) as session:
        try:
            hb = await make_huckleberry_client(
                session,
                config.HUCKLEBERRY_EMAIL,
                config.HUCKLEBERRY_PASSWORD,
                config.HUCKLEBERRY_TIMEZONE,
            )
            user = await hb.get_user()
            if user and user.childList:
                for c in user.childList:
                    print(f"  Nickname: {c.nickname:<11} | Huckleberry UID (cid): {c.cid}")
            else:
                print("  No children found in Huckleberry.")
            await hb.stop_all_listeners()
        except Exception as e:
            print(f"  Failed to fetch Huckleberry children: {e}")


if __name__ == "__main__":
    asyncio.run(main())
