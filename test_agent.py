#!/usr/bin/env python3
"""Tests for /briefme agent loop.

Run:  ./jobsmcp/bin/python3 test_agent.py
"""

import json
import unittest
from unittest.mock import patch, MagicMock

from agent import build_system_prompt, TOOLS, MAX_TURNS, run_agent


class TestSystemPrompt(unittest.TestCase):

    def test_prompt_includes_essential_structure(self):
        prompt = build_system_prompt()
        self.assertIn("decision-grade brief", prompt)
        self.assertIn("Brief structure", prompt)
        self.assertIn("Briefing discipline", prompt)
        self.assertIn("career-strategic profile", prompt)
        self.assertIn("web_search", prompt)
        self.assertIn("web_fetch", prompt)
        self.assertIn("read_file", prompt)


class TestToolDefinitions(unittest.TestCase):

    def test_all_tools_have_required_fields(self):
        for tool in TOOLS:
            self.assertEqual(tool["type"], "function")
            fn = tool["function"]
            self.assertIn("name", fn)
            self.assertIn("description", fn)
            self.assertIn("parameters", fn)
            self.assertIn("required", fn["parameters"])

    def test_read_file_tool_schema(self):
        read = [t for t in TOOLS if t["function"]["name"] == "read_file"][0]
        props = read["function"]["parameters"]["properties"]
        self.assertIn("path", props)
        self.assertEqual(props["path"]["type"], "string")

    def test_web_search_tool_schema(self):
        search = [t for t in TOOLS if t["function"]["name"] == "web_search"][0]
        props = search["function"]["parameters"]["properties"]
        self.assertIn("query", props)

    def test_web_fetch_tool_schema(self):
        fetch = [t for t in TOOLS if t["function"]["name"] == "web_fetch"][0]
        props = fetch["function"]["parameters"]["properties"]
        self.assertIn("url", props)


class TestAgentLoop(unittest.TestCase):

    def test_agent_returns_content_on_finish(self):
        """Agent stops when finish_reason is 'stop' and no tool calls."""
        mock_response = {
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": "# Brief\n\nThis is a test brief."
                },
                "finish_reason": "stop"
            }]
        }

        with patch("agent.urllib.request.urlopen") as mock_urlopen:
            mock_urlopen.return_value.__enter__.return_value.read.return_value = \
                json.dumps(mock_response).encode()

            result = run_agent("Test job description", "fake-key")
            self.assertIn("test brief", result)

    def test_agent_handles_tool_call_then_finish(self):
        """Agent calls a tool, gets result, then finishes."""
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "read_file", "arguments": '{"path": "/tmp/test"}'}
        }
        responses = [
            # Turn 1: model calls read_file
            json.dumps({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [tool_call]
                    },
                    "finish_reason": "tool_calls"
                }]
            }).encode(),
            # Turn 2: model finishes with brief
            json.dumps({
                "choices": [{
                    "message": {
                        "role": "assistant",
                        "content": "Brief based on profile"
                    },
                    "finish_reason": "stop"
                }]
            }).encode(),
        ]

        with patch("agent.urllib.request.urlopen") as mock_urlopen:
            mock_cm = MagicMock()
            mock_cm.read.side_effect = responses
            mock_urlopen.return_value.__enter__.return_value = mock_cm

            result = run_agent("Test job", "fake-key")
            self.assertIn("Brief", result)

    def test_max_turns_enforced(self):
        """Agent raises after MAX_TURNS."""
        tool_call = {
            "id": "call_1",
            "type": "function",
            "function": {"name": "web_search",
                         "arguments": '{"query": "test"}'}
        }
        response = json.dumps({
            "choices": [{
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [tool_call]
                },
                "finish_reason": "tool_calls"
            }]
        }).encode()

        with patch("agent.urllib.request.urlopen") as mock_urlopen:
            mock_cm = MagicMock()
            mock_cm.read.return_value = response
            mock_urlopen.return_value.__enter__.return_value = mock_cm

            with self.assertRaises(RuntimeError):
                run_agent("Test", "fake-key")


if __name__ == "__main__":
    unittest.main(verbosity=2)
