"""Local headed-browser prerequisite check, without an Agent or model call."""

import asyncio
import json
import os
from pathlib import Path
import tempfile

ROOT = Path(__file__).resolve().parents[1]
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
os.environ["BROWSER_USE_DISABLE_EXTENSIONS"] = "1"
os.environ["BROWSER_USE_CONFIG_DIR"] = str(ROOT / ".local/browser-use")

from browser_use import Browser


async def main() -> None:
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    if not chrome.is_file():
        raise SystemExit("Expected local Chrome is missing; report blocker before changing browser setup")
    private = ROOT / ".local"
    private.mkdir(exist_ok=True)
    # 0.13.14 otherwise copies Chrome profiles to a separate system temp directory.
    # Its recognized temp prefix preserves this fresh, project-local directory.
    with tempfile.TemporaryDirectory(prefix="browser-use-user-data-dir-", dir=private) as profile:
        browser = Browser(
            executable_path=str(chrome),
            headless=False,
            use_cloud=False,
            user_data_dir=profile,
            enable_default_extensions=False,
            keep_alive=False,
        )
        assert Path(browser.browser_profile.user_data_dir).resolve() == Path(profile).resolve()
        try:
            async with asyncio.timeout(45):
                await browser.start()
                page = await browser.new_page("about:blank")
                await page.evaluate("() => { document.title = 'Personal Web Agent P0-A'; return document.title; }")
                title = await page.evaluate("() => document.title")
                assert "Personal Web Agent P0-A" in title, title
                print(json.dumps({
                    "result": "PASS", "headless": False, "use_cloud": False,
                    "url": "about:blank", "title": title,
                    "isolated_temporary_profile": True, "model_calls": 0,
                    "scope": "browser prerequisite only; not P0 Agent smoke or T01",
                }, indent=2))
        finally:
            await asyncio.wait_for(browser.kill(), timeout=15)


if __name__ == "__main__":
    asyncio.run(main())
