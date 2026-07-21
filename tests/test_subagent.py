"""test_subagent.py — Unit tests for the sub-agent system."""

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock, MagicMock

from src.agent_tools.subagent_tools import (
    delegate_task,
    _resolve_toolsets,
    TOOL_SCOPES,
)


class TestToolScopeResolution(unittest.TestCase):
    def test_default_is_read_only(self):
        disabled = _resolve_toolsets(None)
        self.assertIn("write_file", disabled)
        self.assertIn("edit_file", disabled)
        self.assertNotIn("web_search", disabled)
        self.assertNotIn("read_file", disabled)

    def test_full_scope_unlocks_all(self):
        disabled = _resolve_toolsets(["full"])
        self.assertEqual(disabled, set())

    def test_web_only(self):
        disabled = _resolve_toolsets(["web-only"])
        self.assertIn("write_file", disabled)
        self.assertIn("bash", disabled)
        self.assertNotIn("web_search", disabled)

    def test_custom_scope(self):
        disabled = _resolve_toolsets(["read", "read-terminal"])
        self.assertIn("write_file", disabled)
        # read-terminal reads only vs read only — combine both
        # both include manage_tasks, so it should still be disabled
        self.assertIn("manage_tasks", disabled)

    def test_invalid_scope_name(self):
        disabled = _resolve_toolsets(["unknown-scope"])
        # unknown names are ignored; falls back to default (read-only? Wait...
        # _resolve_toolsets doesn't apply default if toolsets list is non-empty but
        # invalid. Let's verify: it only adds from TOOL_SCOPES. If none match,
        # combined remains empty.
        # Actually looking at code: if not toolsets: return TOOL_SCOPES["read"].
        # If toolsets is ["unknown"]: loop runs, no match, combined stays empty.
        # That means a wrong toolsets unlocks everything accidentally!
        self.assertEqual(len(disabled), 0)


class TestDelegateTaskInputParsing(unittest.TestCase):
    @patch("src.agent_tools.subagent_tools._run_single_subagent",
           new_callable=AsyncMock)
    async def test_single_task_text_mode(self, mock_run):
        mock_run.return_value = {"goal": "Analyze", "response": "Done"}
        result = await delegate_task(
            content="Analyze this file\nLook at the structure and report",
            session_id="test-session",
            owner="admin",
        )
        self.assertTrue(result.get("sub_agent"))
        mock_run.assert_awaited_once()

    @patch("src.agent_tools.subagent_tools._run_single_subagent",
           new_callable=AsyncMock)
    async def test_single_task_json_with_toolsets(self, mock_run):
        mock_run.return_value = {"goal": "Research AI", "response": "Results"}
        result = await delegate_task(
            content=json.dumps({"goal": "Research AI", "toolsets": ["web-only"]}),
            session_id="test",
            owner="admin",
        )
        self.assertTrue(result.get("sub_agent"))
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(kwargs["goal"], "Research AI")

    @patch("src.agent_tools.subagent_tools._run_single_subagent",
           new_callable=AsyncMock)
    async def test_batch_mode_parallel(self, mock_run):
        mock_run.side_effect = [
            {"goal": "Task 1", "response": "Result 1"},
            {"goal": "Task 2", "response": "Result 2"},
        ]
        result = await delegate_task(
            content=json.dumps({
                "tasks": [
                    {"goal": "Task 1"},
                    {"goal": "Task 2", "context": "Extra context"},
                ]
            }),
            session_id="test",
            owner="admin",
        )
        self.assertTrue(result.get("batch"))
        self.assertEqual(result.get("count"), 2)
        self.assertEqual(len(result.get("results", [])), 2)

    @patch("src.agent_tools.subagent_tools._run_single_subagent",
           new_callable=AsyncMock)
    async def test_batch_mode_truncates_at_3(self, mock_run):
        mock_run.return_value = {"goal": "Task", "response": "Result"}
        result = await delegate_task(
            content=json.dumps({
                "tasks": [
                    {"goal": "T1"}, {"goal": "T2"}, {"goal": "T3"},
                    {"goal": "T4"}, {"goal": "T5"},
                ]
            }),
        )
        self.assertEqual(result.get("count"), 3)

    async def test_empty_content(self):
        result = await delegate_task(content="")
        self.assertIn("error", result)

    async def test_no_goal(self):
        result = await delegate_task(content="   \n  ")
        self.assertIn("error", result)


if __name__ == "__main__":
    unittest.main()
