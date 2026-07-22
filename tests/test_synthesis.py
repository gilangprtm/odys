"""test_synthesis.py — Unit tests for sub-agent result synthesis (FR-4.5)."""

import asyncio
import unittest
from unittest.mock import patch, AsyncMock

from src.agent_tools.subagent_tools import delegate_task, _synthesize_results


# Helper to run async tests with unittest
def patch_async_test(func):
    def wrapper(*args, **kwargs):
        return asyncio.run(func(*args, **kwargs))
    return wrapper


class TestSubagentSynthesis(unittest.TestCase):

    @patch_async_test
    async def test_synthesize_results_success_and_error(self):
        results = [
            {"goal": "Check database", "response": "Database is healthy.", "tool_calls": 2},
            {"goal": "Check cache", "error": "Connection timed out"},
        ]
        summary = await _synthesize_results("Check infrastructure", results)
        
        self.assertIn("## Synthesis for: Check infrastructure", summary)
        self.assertIn("✅ **Check database**", summary)
        self.assertIn("Database is healthy", summary)
        self.assertIn("⚠️ **Check cache** (error)", summary)
        self.assertIn("Connection timed out", summary)

    @patch("src.agent_tools.subagent_tools._run_single_subagent", new_callable=AsyncMock)
    @patch_async_test
    async def test_delegate_task_batch_synthesis(self, mock_run):
        mock_run.side_effect = [
            {"goal": "Check database", "response": "Database OK"},
            {"goal": "Check cache", "response": "Cache OK"},
        ]

        batch_input = {
            "tasks": [
                {"goal": "Check database", "context": "main-db"},
                {"goal": "Check cache", "context": "redis"},
            ]
        }

        output = await delegate_task(json_dumps(batch_input))
        
        self.assertTrue(output.get("sub_agent"))
        self.assertTrue(output.get("batch"))
        self.assertIn("synthesis", output)
        self.assertIn("## Synthesis for: Check database", output["synthesis"])
        self.assertIn("✅ **Check database**", output["synthesis"])
        self.assertIn("✅ **Check cache**", output["synthesis"])


# Simple json_dumps helper for mock test
def json_dumps(data):
    import json
    return json.dumps(data)


if __name__ == "__main__":
    unittest.main()
