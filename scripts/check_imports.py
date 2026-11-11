"""Offline P0-A import/interface check; never constructs or runs an Agent."""

import inspect
import json
import os
from pathlib import Path
import socket

ROOT = Path(__file__).resolve().parents[1]
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
os.environ["BROWSER_USE_CONFIG_DIR"] = str(ROOT / ".local/browser-use")


def deny_network(*args, **kwargs):
    raise RuntimeError("Network is disabled during the offline import check")


socket.socket.connect = deny_network
socket.socket.connect_ex = deny_network
socket.create_connection = deny_network

from importlib.metadata import metadata, version
from browser_use import Agent, Browser, ChatDeepSeek
from dotenv import load_dotenv
import personal_web_agent

llm = ChatDeepSeek(model="deepseek-flash")
assert llm.model == "deepseek-flash"
assert llm.provider == "deepseek"
assert "use_vision" in inspect.signature(Agent.__init__).parameters
assert callable(Browser.start)
assert callable(load_dotenv)
print(json.dumps({
    "result": "PASS",
    "model_calls": 0,
    "network": "blocked by socket guard",
    "browser_use": version("browser-use"),
    "requires_python": metadata("browser-use")["Requires-Python"],
    "deepseek_model_explicit": llm.model,
    "deepseek_model_library_default": inspect.signature(ChatDeepSeek).parameters["model"].default,
    "deepseek_base_url": str(llm.base_url),
    "agent_use_vision_parameter": True,
    "planned_use_vision": False,
}, indent=2))
