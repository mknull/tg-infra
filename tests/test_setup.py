#!/usr/bin/env python3
"""CI setup simulation — verify the full setup pipeline without API calls."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_PROJECT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Fixture responses — synthetic, clearly fake, used only to verify plumbing
# ---------------------------------------------------------------------------

FIXTURE_EXTRACT = (
    "Candidate: Jane Doe, senior frontend engineer. "
    "Work: WidgetCorp (2020-2025), Acme Inc (2018-2020). "
    "Skills: TypeScript, React, CSS, Node.js, system design. "
    "Interests: accessibility, design systems, build tooling."
)

FIXTURE_SOURCE = """###FILE: interests.txt###
# LLMs: this file is read-only. Do not edit.

Jane Doe's core interest is building accessible, performant user interfaces at scale.

The deeper structure:
1. Design systems — at WidgetCorp she led the migration to a component library serving 12 product teams.
2. Web performance — reduced LCP by 40% at WidgetCorp through code splitting and image optimization.
3. Accessibility — led WCAG 2.1 AA compliance initiative across three product lines.

| Dimension | What the candidate cares about | Evidence |
|-----------|-------------------------------|----------|
| UI architecture | Scalable component systems | WidgetCorp library |
| Performance | Fast experiences | LCP reduction |
| Accessibility | Inclusive design | WCAG compliance |

My best synthesis: A frontend engineer who treats UI as infrastructure.

###FILE: skills.txt###
# LLMs: this file is read-only. Do not edit.

Jane Doe is a senior frontend engineer who delivers through systematic practices.

1. Frontend engineering
   | What they did | Evidence |
   |---------------|----------|
   | Led component library migration | WidgetCorp |

   Synthesis: Ships production UI at scale.

2. Design systems
   | What they did | Evidence |
   |---------------|----------|
   | Maintained internal UI kit | Acme Inc |

   Synthesis: Treats UI patterns as reusable infrastructure.

| Skill area | Strength | Rationale |
|------------|----------|-----------|
| React/TypeScript | Very strong | Primary tools across two roles |
| Design systems | Strong | Led migration at WidgetCorp |

Final synthesis: An engineer who brings systematic practices to UI development.

###FILE: tech_stack.txt###
# LLMs: this file is read-only. Do not edit.

Technical Stack (Document-Evidenced)

| Technology | Evidence | Notes |
|------------|----------|-------|
| TypeScript | WidgetCorp, Acme | Primary language |
| React | WidgetCorp, Acme | UI framework |

| Method | Evidence | Notes |
|--------|----------|-------|
| Design systems | WidgetCorp library | Component architecture |
| Web performance | LCP optimization | Core web vitals |

Document-backed conclusion: TypeScript and React dominate."""

FIXTURE_SOURCE_V2 = """###FILE: interests.txt###
# LLMs: this file is read-only. Do not edit.

Jane Doe's updated interests: backend infrastructure and distributed systems.

My best synthesis: A full-stack engineer expanding into backend.

###FILE: skills.txt###
# LLMs: this file is read-only. Do not edit.

Jane Doe is expanding into backend.

1. Backend engineering
   | What they did | Evidence |
   |---------------|----------|
   | Built API services | Side project |

   Synthesis: Growing backend capability.

Final synthesis: Full-stack trajectory.

###FILE: tech_stack.txt###
# LLMs: this file is read-only. Do not edit.

| Technology | Evidence | Notes |
|------------|----------|-------|
| TypeScript | WidgetCorp | Primary |

Document-backed conclusion: TypeScript remains primary."""


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_module(filename, alias):
    spec = importlib.util.spec_from_file_location(alias, _PROJECT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _fake_deepseek(model, prompt, api_key, timeout=90):
    """Return fixture responses based on prompt content."""
    if "list every fact" in prompt.lower():
        return FIXTURE_EXTRACT
    if "###FILE:" in prompt.lower() or "career analyst" in prompt.lower():
        return FIXTURE_SOURCE
    return '{"decision": "skip", "reason": "unknown prompt"}'


class _BaseSetupTest(unittest.TestCase):
    """Shared setup for tests that need the generate-profile module and temp dir."""

    @classmethod
    def setUpClass(cls):
        cls.gen = _load_module("generate-profile.py", f"gen_{cls.__name__}")
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="setup_test_"))
        cls.docsdir = cls.tmpdir / "docs"
        cls.docsdir.mkdir()
        (cls.docsdir / "cv.txt").write_text(
            "Jane Doe. Senior frontend engineer at WidgetCorp 2020-2025.\n"
            "TypeScript, React, CSS, Node.js. Led design system migration.\n"
            "Reduced LCP by 40%. WCAG 2.1 AA compliance.\n"
        )
        cls.outdir = cls.tmpdir / "output"
        cls.outdir.mkdir(exist_ok=True)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def setUp(self):
        for f in ("interests.txt", "skills.txt", "tech_stack.txt"):
            (self.outdir / f).unlink(missing_ok=True)

    def _run_stage1(self, force=False):
        """Run Stage 1 with mocked API, return extracted document text."""
        docs_text = self.gen.extract_text(self.docsdir)
        with patch.object(self.gen, "call_deepseek",
                          side_effect=_fake_deepseek):
            self.gen.GEN_DIR = self.outdir
            self.gen.stage1_generate_source(
                docs_text, "Test User", "fake-key", force=force)
        return docs_text


# ---------------------------------------------------------------------------
# Setup pipeline tests
# ---------------------------------------------------------------------------

class TestSetupSimulation(_BaseSetupTest):
    """Simulate the full setup pipeline with mocked API calls."""

    def test_stage1_runs_without_error(self):
        """Stage 1 produces files without crashing."""
        self._run_stage1(force=True)
        for name in ("interests.txt", "skills.txt", "tech_stack.txt"):
            self.assertTrue((self.outdir / name).exists(),
                            f"{name} not generated")

    def test_source_files_have_correct_structure(self):
        """Generated source files have header, content, and expected keywords."""
        self._run_stage1(force=True)
        parts = []
        for name in ("interests.txt", "skills.txt", "tech_stack.txt"):
            path = self.outdir / name
            self.assertTrue(path.exists())
            content = path.read_text()
            self.assertIn("# LLMs: this file is read-only", content)
            self.assertGreater(len(content), 200)
            parts.append(content)
        context = "\n\n".join(parts).lower()
        self.assertIn("typescript", context)
        self.assertIn("frontend", context)

    def test_readonly_marker_created(self):
        """The ThisDirectoryIsReadOnly marker is created."""
        self._run_stage1(force=True)
        self.assertTrue((self.outdir / "ThisDirectoryIsReadOnly").exists())


class TestSetupIdempotency(_BaseSetupTest):
    """Running setup twice must not corrupt existing files."""

    @staticmethod
    def _idempotent_fake(model, prompt, api_key, timeout=90):
        if "list every fact" in prompt.lower():
            return "Candidate: engineer. Skills: Python, Java."
        return FIXTURE_SOURCE

    def test_second_run_without_force_preserves_originals(self):
        """Without --force, existing files are not overwritten."""
        with patch.object(self.gen, "call_deepseek",
                          side_effect=self._idempotent_fake):
            self.gen.GEN_DIR = self.outdir
            self.gen.stage1_generate_source(
                self.gen.extract_text(self.docsdir), "User", "key")
            first = (self.outdir / "interests.txt").read_text()
            # Second run — should skip all existing files
            self.gen.stage1_generate_source(
                self.gen.extract_text(self.docsdir), "User", "key")
            second = (self.outdir / "interests.txt").read_text()
            self.assertEqual(first, second)

    def test_force_overwrites_existing_files(self):
        """--force overwrites previously generated files."""
        with patch.object(self.gen, "call_deepseek",
                          side_effect=self._idempotent_fake):
            self.gen.GEN_DIR = self.outdir
            self.gen.stage1_generate_source(
                self.gen.extract_text(self.docsdir), "User", "key")
            first = (self.outdir / "interests.txt").read_text()
            # Force with different fixture
            with patch.object(self.gen, "call_deepseek",
                              return_value=FIXTURE_SOURCE_V2):
                self.gen.stage1_generate_source(
                    self.gen.extract_text(self.docsdir), "User", "key",
                    force=True)
            second = (self.outdir / "interests.txt").read_text()
            self.assertNotEqual(first, second)


# ---------------------------------------------------------------------------
# Auth pipeline tests
# ---------------------------------------------------------------------------

class TestAuthPipeline(unittest.TestCase):
    """Token management: load, save, expiry gating, refresh."""

    def setUp(self):
        import lib.auth as auth_module
        self.auth = auth_module
        self.token_file = _PROJECT / "state" / "outlook-token.json"
        self.original = None
        if self.token_file.exists():
            self.original = self.token_file.read_text()

    def tearDown(self):
        if self.original is not None:
            self.token_file.write_text(self.original)
        else:
            self.token_file.unlink(missing_ok=True)

    def _write_token(self, access="test-at", expires_in=3600):
        self.token_file.write_text(json.dumps({
            "access_token": access,
            "refresh_token": "test-rt",
            "expires_at": int((time.time() + expires_in) * 1000),
        }))

    def test_load_token_reads_valid_json(self):
        self._write_token("test-at")
        self.assertEqual(self.auth.load_token()["access_token"], "test-at")

    def test_save_token_atomic_write(self):
        self.auth.save_token({
            "access_token": "at-atomic",
            "refresh_token": "rt-atomic",
            "expires_at": 9999999999999,
        })
        self.assertEqual(json.loads(self.token_file.read_text())["access_token"],
                         "at-atomic")

    @patch("urllib.request.urlopen")
    def test_returns_valid_token_without_refresh(self, mock_urlopen):
        self._write_token("valid-token", 7200)
        token = self.auth.ensure_valid_token({"OUTLOOK_CLIENT_ID": "x"})
        self.assertEqual(token, "valid-token")
        mock_urlopen.assert_not_called()

    @patch("urllib.request.urlopen")
    def test_refreshes_when_near_expiry(self, mock_urlopen):
        self._write_token("stale", 0)  # already expired
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "access_token": "fresh-token",
            "refresh_token": "new-rt",
            "expires_in": 3600,
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        token = self.auth.ensure_valid_token({"OUTLOOK_CLIENT_ID": "x"})
        self.assertEqual(token, "fresh-token")

    @patch("urllib.request.urlopen")
    def test_raises_when_refresh_fails(self, mock_urlopen):
        self._write_token("stale", 0)
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({"error": "invalid_grant"}).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp
        with self.assertRaises(RuntimeError):
            self.auth.ensure_valid_token({"OUTLOOK_CLIENT_ID": "x"})

    def test_raises_without_client_id(self):
        self._write_token("stale", 0)
        with self.assertRaises(RuntimeError):
            self.auth.ensure_valid_token({})


# ---------------------------------------------------------------------------
# Health + config checks
# ---------------------------------------------------------------------------

class TestCIHealthCheck(unittest.TestCase):
    """The health tool must run and return a valid exit code (0 clean / 1 issues
    found). Whether the live state is actually clean is the audit's job over real
    data — not a unit test's — so this does NOT assert health is green; that
    would couple a code test to whatever the developer's machine happens to
    contain (audit checks runtime behavior; tests check code)."""

    def test_health_check_runs(self):
        status = os.system(f"{_PROJECT}/audit --health >/dev/null 2>&1")
        self.assertIn(os.waitstatus_to_exitcode(status), (0, 1))


class TestChannelsJSONStructure(unittest.TestCase):
    """Verify channels.json has all required fields for prompt assembly."""

    def test_all_channels_have_required_fields(self):
        with open(_PROJECT / "state" / "channels.json") as f:
            channels = json.loads(f.read())["channels"]
        required = {"username", "queue_prefix", "desired_roles",
                     "acceptable_roles"}
        for ch in channels:
            missing = required - set(ch.keys())
            self.assertEqual(set(), missing,
                             f"channel {ch.get('username', '?')} missing: {missing}")
            self.assertGreater(len(ch["desired_roles"]), 0)
            self.assertGreater(len(ch["acceptable_roles"]), 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
