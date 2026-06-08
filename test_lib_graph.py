#!/usr/bin/env python3
"""Tests for lib.graph — shared Microsoft Graph client (auth header, URL, JSON)."""

import json
import unittest
from unittest.mock import patch, MagicMock

from lib import graph_get, graph_post, GRAPH_BASE


def _mock_resp(body: bytes):
    resp = MagicMock()
    resp.read.return_value = body
    ctx = MagicMock()
    ctx.__enter__.return_value = resp
    return ctx


class TestGraphGet(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_builds_url_and_auth_header(self, mock_open):
        mock_open.return_value = _mock_resp(json.dumps({"value": []}).encode())
        graph_get("/me/messages", "tok123")
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, GRAPH_BASE + "/me/messages")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok123")
        self.assertEqual(req.get_method(), "GET")

    @patch("urllib.request.urlopen")
    def test_encodes_params_onto_query_string(self, mock_open):
        mock_open.return_value = _mock_resp(json.dumps({"value": [1]}).encode())
        graph_get("/me/messages", "tok", {"$top": "5", "$select": "id"})
        req = mock_open.call_args[0][0]
        self.assertIn("%24top=5", req.full_url)
        self.assertIn("%24select=id", req.full_url)

    @patch("urllib.request.urlopen")
    def test_parses_json_response(self, mock_open):
        mock_open.return_value = _mock_resp(json.dumps({"value": [{"id": "a"}]}).encode())
        result = graph_get("/me/messages", "tok")
        self.assertEqual(result["value"][0]["id"], "a")

    @patch("urllib.request.urlopen")
    def test_empty_body_returns_empty_dict(self, mock_open):
        mock_open.return_value = _mock_resp(b"")
        self.assertEqual(graph_get("/x", "tok"), {})


class TestGraphPost(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_posts_json_with_headers(self, mock_open):
        mock_open.return_value = _mock_resp(b"")
        graph_post("/me/sendMail", "tok", {"message": "hi"})
        req = mock_open.call_args[0][0]
        self.assertEqual(req.full_url, GRAPH_BASE + "/me/sendMail")
        self.assertEqual(req.get_header("Authorization"), "Bearer tok")
        self.assertEqual(req.get_header("Content-type"), "application/json")
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(json.loads(req.data), {"message": "hi"})

    @patch("urllib.request.urlopen")
    def test_empty_body_returns_empty_dict(self, mock_open):
        mock_open.return_value = _mock_resp(b"")
        self.assertEqual(graph_post("/x", "tok", {}), {})

    @patch("urllib.request.urlopen")
    def test_parses_json_response(self, mock_open):
        mock_open.return_value = _mock_resp(json.dumps({"id": "sent"}).encode())
        self.assertEqual(graph_post("/x", "tok", {})["id"], "sent")


if __name__ == "__main__":
    unittest.main(verbosity=2)
