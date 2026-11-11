"""Regression for the real DOM object returned by AgentHistoryList.model_actions."""

import json
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
os.environ["ANONYMIZED_TELEMETRY"] = "false"
os.environ["BROWSER_USE_CLOUD_SYNC"] = "false"
os.environ["BROWSER_USE_CONFIG_DIR"] = str(ROOT / ".local/browser-use")

from browser_use.dom.views import DOMInteractedElement, DOMRect, NodeType
from personal_web_agent.runner import _json_default


class HistorySerializationTests(unittest.TestCase):
    def test_native_interacted_element_is_json_serializable(self):
        element = DOMInteractedElement(
            node_id=1, backend_node_id=2, frame_id=None, node_type=NodeType.ELEMENT_NODE,
            node_value="", node_name="A", attributes={"href": "tutorial/index.html"},
            bounds=DOMRect(x=1, y=2, width=100, height=20), x_path="/a", element_hash=3,
            ax_name="Tutorial",
        )
        data = json.loads(json.dumps({"actions": [{"click": {"index": 42}, "interacted_element": element}]}, default=_json_default))
        saved = data["actions"][0]["interacted_element"]
        self.assertEqual(saved["ax_name"], "Tutorial")
        self.assertEqual(saved["attributes"]["href"], "tutorial/index.html")
        self.assertEqual(saved["bounds"]["width"], 100)


if __name__ == "__main__":
    unittest.main()
