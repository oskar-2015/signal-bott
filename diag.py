# -*- coding: utf-8 -*-
import asyncio
import json
import os
from pyquotex.stable_api import Quotex

SESSION_FILE = "qx_session.json"


def load_session():
    with open(SESSION_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("cookies_str"), data.get("ssid")


async def main():
    cookies_str, ssid = load_session()
    client = Quotex(email="x@x.com", password="x", lang="en")
    client.set_session(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36", cookies=cookies_str, ssid=ssid)
    check, reason = await client.connect()
    print("connect:", check, reason)

    assets = client.get_all_asset_name()
    print("TYPE:", type(assets))
    print("FIRST 3 ITEMS:", assets[:3])

    name = assets[0][0]
    print("checking asset:", name)
    result = await client.check_asset_open(name)
    print("check_asset_open RESULT:", result)
    print("RESULT TYPE:", type(result))


if __name__ == "__main__":
    asyncio.run(main())
