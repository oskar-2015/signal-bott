# -*- coding: utf-8 -*-
import asyncio
import json
import re
from datetime import datetime
from playwright.async_api import async_playwright

SESSION_FILE = "qx_session.json"


async def main():
    captured_ssid = {"value": None}

    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir="./qx_profile",
            headless=False,
            args=["--disable-blink-features=AutomationControlled"]
        )
        page = await context.new_page()

        def handle_frame(payload):
            try:
                text = payload if isinstance(payload, str) else payload.decode("utf-8", errors="ignore")
                if "authorization" not in text or '"session"' not in text:
                    return
                match = re.search(r'"session"\s*:\s*"([^"]+)"', text)
                if match:
                    value = match.group(1)
                    if captured_ssid["value"] is None or len(value) > len(captured_ssid["value"]):
                        captured_ssid["value"] = value
                        print("ssid captured, length:", len(value))
            except Exception:
                pass

        def on_websocket(ws):
            ws.on("framesent", lambda p: handle_frame(p))
            ws.on("framereceived", lambda p: handle_frame(p))

        page.on("websocket", on_websocket)

        await page.goto("https://quotex.com/ar/sign-in")

        print("Log in manually in the browser window now.")
        print("Waiting 90 seconds...")

        await asyncio.sleep(90)

        cookies = await context.cookies()
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        cookies_str = "; ".join([f"{k}={v}" for k, v in cookie_dict.items()])

        if not captured_ssid["value"]:
            print("failed to capture ssid")
            await context.close()
            return

        session_data = {
            "cookies_str": cookies_str,
            "ssid": captured_ssid["value"],
            "saved_at": datetime.now().isoformat()
        }

        with open(SESSION_FILE, "w", encoding="utf-8") as f:
            json.dump(session_data, f, indent=2, ensure_ascii=False)

        print("session saved to", SESSION_FILE)
        await context.close()


if __name__ == "__main__":
    asyncio.run(main())
