"""Installation behavior with reviewed bounded choices and real staged writes."""

import json
import logging
import unittest
from unittest.mock import patch

import generate_profile as generator
import lib.direction as direction
import rss_poller
from lib.matching import matching_gaps
from lib.rss_selection import apply_choices, recommend
from tests import test_matching_setup as harness
from tests.test_rss_subscriptions import xml


class TestRssSetup(unittest.TestCase):
    def setUp(self):
        harness.TestMatchingSetup.setUp(self)
        data = json.loads(self.feeds.read_text())
        data["rss_selection_reviewed"] = False
        for feed in data["feeds"]:
            feed["enabled"] = False
        self.feeds.write_text(json.dumps(data))
        self.selected = ["fossjobs:designers", "wwr:design"]
        self.profession = "Accessibility designer"
        self.fetch = self.stack.enter_context(patch.object(rss_poller, "fetch_feed", return_value=(xml(), {})))

    seed_profile = harness.TestMatchingSetup.seed_profile

    def fake_model(self, model, prompt, key, timeout=90):
        if "Select RSS job feeds" in prompt:
            self.prompts.append(prompt)
            return json.dumps({"choices": [{"id": ident, "reason": "Matches the stated interests"} for ident in self.selected]})
        if "Generate Flash job-filter preferences" in prompt:
            self.prompts.append(prompt)
            sources = json.loads(prompt.split("SOURCES:\n", 1)[1].split("\n\nReturn ONLY JSON:", 1)[0])
            return json.dumps({"sources": [{"id": s["id"], "desired_roles": self.profession,
                                          "acceptable_roles": "Related roles consistent with current preferences"} for s in sources]})
        return harness.TestMatchingSetup.fake_model(self, model, prompt, key, timeout)

    def run_ui(self, answers, model=None):
        return harness.TestMatchingSetup.run_ui(self, answers, model, keep_rss=False)

    def install(self):
        return self.run_ui(["y", str(self.docs), "", "r", "r", "a"])

    def test_first_install_reviews_feeds_backfill_and_personalized_prompts_together(self):
        code, output, _ = self.install()
        self.assertEqual(code, 0)
        config = json.loads(self.feeds.read_text())
        self.assertTrue(config["rss_selection_reviewed"])
        enabled = [e for e in config["feeds"] if e["enabled"]]
        self.assertEqual({e["id"] for e in enabled}, {"fossjobs", "wwr"})
        self.assertIn("ADD Design", output)
        self.assertIn("available entries", output)
        self.assertIn("unknown dates", output)
        self.assertIn("Accessibility designer", output)
        for feed in enabled:
            self.assertEqual(feed["desired_roles"], self.profession)
            self.assertEqual(feed["subscriptions"][0]["backfill"]["mode"], "recent")
            self.assertEqual(feed["subscriptions"][0]["poll_interval_hours"], 1)
            self.assertNotIn("include_categories", feed)
        self.assertEqual(matching_gaps(self.root), [])

    def test_later_preserves_all_files_including_subscription_choices(self):
        before = generator.generation_snapshot()
        code, _, _ = self.run_ui(["y", str(self.docs), "", "r", "f", "l"])
        self.assertEqual(code, 1)
        self.assertEqual(generator.generation_snapshot(), before)

    def test_reinstall_keep_does_no_model_or_feed_requests(self):
        self.install()
        self.fetch.reset_mock()
        before = generator.generation_snapshot()
        code, _, model = self.run_ui([""])
        self.assertEqual(code, 0)
        self.assertEqual(generator.generation_snapshot(), before)
        model.assert_not_called()
        self.fetch.assert_not_called()

    def test_manual_selection_and_no_rss_are_valid(self):
        self.seed_profile()
        code, _, model = self.run_ui(["c", "", "m", "wwr:customer-support", "f", "a"])
        self.assertEqual(code, 0)
        self.assertFalse(any("Select RSS job feeds" in c.args[1] for c in model.call_args_list))
        enabled = [f for f in json.loads(self.feeds.read_text())["feeds"] if f["enabled"]]
        self.assertEqual([s["id"] for s in enabled[0]["subscriptions"]], ["customer-support"])
        self.assertEqual(enabled[0]["subscriptions"][0]["backfill"]["mode"], "future")
        self.fetch.reset_mock()
        code, _, _ = self.run_ui(["c", "", "d", "a"])
        self.assertEqual(code, 0)
        self.assertFalse(any(f["enabled"] for f in json.loads(self.feeds.read_text())["feeds"]))
        self.fetch.assert_not_called()

    def test_invalid_recommendation_corrected_once_or_manual_fallback(self):
        self.seed_profile()
        attempts = []
        def invalid_then_valid(*args, **kwargs):
            if "Select RSS job feeds" in args[1]:
                attempts.append(args[1])
                if len(attempts) == 1:
                    return "not JSON"
            return self.fake_model(*args, **kwargs)
        code, _, _ = self.run_ui(["c", "", "r", "r", "a"], invalid_then_valid)
        self.assertEqual(code, 0)
        self.assertEqual(len(attempts), 2)
        self.assertIn("failed validation: invalid JSON", attempts[1])
        def invalid(*args, **kwargs):
            return "not JSON" if "Select RSS job feeds" in args[1] else self.fake_model(*args, **kwargs)
        code, output, _ = self.run_ui(["c", "", "r", "m", "wwr:product", "a", "a"], invalid)
        self.assertEqual(code, 0)
        self.assertIn("invalid after two attempts", output)
        self.assertIn("No settings changed", output)
        self.assertIn("invalid JSON", (self.state / "setup.log").read_text())

    def test_failure_after_feed_proposal_publishes_nothing(self):
        self.seed_profile()
        before = generator.generation_snapshot()
        def broken_flash(*args, **kwargs):
            return "invalid" if "Generate Flash" in args[1] else self.fake_model(*args, **kwargs)
        with self.assertLogs(level=logging.ERROR):
            code, _, _ = self.run_ui(["c", "", "r", "r", "l"], broken_flash)
        self.assertEqual(code, 1)
        self.assertEqual(generator.generation_snapshot(), before)

    def test_failed_feed_preview_reports_problem_and_leaves_settings_untouched(self):
        self.seed_profile()
        before = generator.generation_snapshot()
        self.fetch.side_effect = OSError("feed unavailable")
        with self.assertLogs(level=logging.ERROR):
            code, output, _ = self.run_ui(["c", "", "r", "l"])
        self.assertEqual(code, 1)
        self.assertIn("feed unavailable", output)
        self.assertEqual(generator.generation_snapshot(), before)

    def test_professions_and_updated_direction_reach_recommendation_prompt(self):
        self.seed_profile()
        for profession, selected in (("Marketing specialist", ["fossjobs:marketing", "wwr:sales-and-marketing"]),
                                     ("Research scientist", ["fossjobs:researchers"]),
                                     ("Accessibility designer", ["fossjobs:designers"])):
            self.profession, self.selected = profession, selected
            for name in generator.PROFILE_FILES:
                (self.root / "source" / name).write_text(profession)
            before = self.feeds.read_bytes()
            direction.save_direction_pair("Prefer " + profession, profession)
            self.assertEqual(self.feeds.read_bytes(), before)
            code, _, _ = self.run_ui(["c", "", "r", "r", "a"])
            self.assertEqual(code, 0)
            prompt = [p for p in self.prompts if "Select RSS job feeds" in p][-1]
            self.assertIn("Prefer " + profession, prompt)
            self.assertIn("PROFILE:\n" + profession, prompt)

    def test_fsf_paid_only_or_volunteer_is_reviewed_explicitly(self):
        self.seed_profile()
        code, output, _ = self.run_ui(["c", "", "m", "fsf:all", "p", "r", "a"])
        self.assertEqual(code, 0)
        fsf = next(f for f in json.loads(self.feeds.read_text())["feeds"] if f["id"] == "fsf")
        self.assertEqual(fsf["subscriptions"][0]["exclude_title_prefixes"], ["Volunteer opportunity:"])
        self.assertIn("Excluded title prefixes", output)
        code, _, _ = self.run_ui(["c", "", "m", "fsf:all", "i", "r", "a"])
        self.assertEqual(code, 0)
        fsf = next(f for f in json.loads(self.feeds.read_text())["feeds"] if f["id"] == "fsf")
        self.assertEqual(fsf["subscriptions"][0]["exclude_title_prefixes"], [])

    def test_manual_choices_can_be_revised_before_apply(self):
        self.seed_profile()
        with patch.object(direction, "call_deepseek", side_effect=["Prefer customer support", "Customer support"]):
            code, _, _ = self.run_ui(["c", "", "m", "wwr:design", "r", "r",
                                     "Prefer customer support", "wwr:customer-support", "r", "a"])
        self.assertEqual(code, 0)
        feed = next(f for f in json.loads(self.feeds.read_text())["feeds"] if f["id"] == "wwr")
        self.assertEqual([s["id"] for s in feed["subscriptions"]], ["customer-support"])
        self.assertEqual(direction.load_current_direction_small(), "Customer support")

    def test_custom_and_disabled_choices_are_preserved(self):
        config = {"feeds": [
            {"id": "custom", "url": "https://example.org/feed", "enabled": True, "custom_field": "keep"},
            {"id": "wwr", "url": "https://weworkremotely.com/remote-jobs.rss", "enabled": False}],
            "rss_selection_reviewed": True}
        result = apply_choices(config, [{"id": "fossjobs:researchers", "reason": "Research"}])
        self.assertEqual(result["feeds"][0], config["feeds"][0])
        self.assertEqual(result["feeds"][1], config["feeds"][1])
        prompts = []
        def call(model, prompt, key):
            prompts.append(prompt)
            return '{"choices":[]}'
        recommend("Researcher", "Research", config, "fake", call)
        self.assertNotIn('"id": "wwr:', prompts[0])
        configured = {"feeds": [{"id": "wwr", "subscriptions": [{"id": "my-design-feed",
                      "url": "https://weworkremotely.com/categories/remote-design-jobs.rss",
                      "poll_interval_hours": 3}]}]}
        kept = apply_choices(configured, [{"id": "wwr:design", "reason": "Design"}])
        sub = kept["feeds"][0]["subscriptions"][0]
        self.assertEqual((sub["id"], sub["poll_interval_hours"]), ("my-design-feed", 3))

    def test_legacy_publisher_without_display_name_can_select_categories(self):
        self.seed_profile()
        self.feeds.write_text(json.dumps({"feeds": [
            {"id": "fossjobs", "url": "https://www.fossjobs.net/rss/all/"},
            {"id": "custom", "url": "https://weworkremotely.com/remote-jobs.rss",
             "desired_roles": "Design", "acceptable_roles": "Research"}]}))
        code, output, _ = self.run_ui(["c", "", "m", "fossjobs:researchers", "r", "a"])
        self.assertEqual(code, 0)
        self.assertIn("fossjobs / researchers", output)
        custom = next(f for f in json.loads(self.feeds.read_text())["feeds"] if f["id"] == "custom")
        self.assertNotIn("subscriptions", custom)
        self.assertEqual(custom["url"], "https://weworkremotely.com/remote-jobs.rss")

    def test_unknown_duplicate_ids_and_arbitrary_urls_are_rejected(self):
        for response in ({"choices": [{"id": "invented", "reason": "x"}]},
                         {"choices": [{"id": "wwr:design", "reason": "x"}]*2},
                         {"choices": [{"id": "wwr:design", "url": "https://invented.example", "reason": "x"}]},
                         {"choices": [{"id": "wwr:design", "reason": ""}]}):
            calls = []
            def model(*args):
                calls.append(args)
                return json.dumps(response)
            with self.subTest(response=response), self.assertRaises(ValueError):
                recommend("Designer", "", {"feeds": []}, "fake", model)
            self.assertEqual(len(calls), 2)


if __name__ == "__main__":
    unittest.main()
