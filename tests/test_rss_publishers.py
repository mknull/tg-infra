"""Publisher quirks verified against sanitized public-feed structures."""

import unittest
from pathlib import Path

from lib.rss_catalog import load_catalog
from lib.rss_details import extract_detail
from rss_poller import parse_feed, plain_text, _selected

FIXTURES = Path(__file__).parent / "fixtures/publishers"


class TestPublishers(unittest.TestCase):
    def test_all_six_publisher_contracts_parse_using_their_catalogue_options(self):
        for pid, publisher in load_catalog().items():
            with self.subTest(publisher=pid):
                options = {k: publisher["defaults"].get(k, False) for k in
                           ("allow_empty", "repair_html_entities", "merge_variants")}
                items = parse_feed((FIXTURES / f"{pid}.xml").read_bytes(), publisher["options"][0]["url"], **options)
                self.assertEqual(len(items), 0 if pid == "tails" else 3 if pid == "fsf" else 1)
                if pid == "fossjobs":
                    self.assertEqual(items[0]["source_id"], items[0]["url"])
                    self.assertEqual(items[0]["posted_at"][:10], "2026-10-13")
                if pid == "wwr":
                    self.assertTrue(items[0]["title"].endswith("F&O"))
                    self.assertIn("Canada", items[0]["content"])
                    self.assertIn("United States", items[0]["content"])
                if pid == "nodesk":
                    self.assertIn("company’s researcher", items[0]["title"])

    def test_fsf_paid_filter_accepts_unfamiliar_titles_and_explicitly_excludes_information(self):
        items = parse_feed((FIXTURES / "fsf.xml").read_bytes(), "https://static.fsf.org/fsforg/rss/jobs.xml")
        options = {**load_catalog()["fsf"]["defaults"], "exclude_title_prefixes": ["Volunteer opportunity:"]}
        selected = [i for i in items if _selected(i, options)]
        self.assertEqual([i["title"] for i in selected], ["Systems Integration Administrator"])
        options["exclude_title_prefixes"] = []
        self.assertEqual(len([i for i in items if _selected(i, options)]), 2)

    def test_detail_pages_preserve_requirements_and_geography_without_navigation(self):
        for pid in ("fsf", "nodesk"):
            content = extract_detail((FIXTURES / f"{pid}-detail.html").read_bytes(), pid, "https://example.org/jobs/", plain_text)
            self.assertIn("Requirements", content)
            self.assertNotIn("unrelated", content)
            self.assertIn("United States" if pid == "fsf" else "Cyprus", content)
            with self.assertRaises(ValueError):
                extract_detail(b"<html>Please verify you are human</html>", pid, "https://example.org", plain_text)

    def test_wwr_variant_order_does_not_change_revision(self):
        import re
        raw = (FIXTURES / "wwr.xml").read_bytes()
        raw = raw.replace(b"Sun, 18 Oct 2026", b"Thu, 01 Oct 2026", 1)
        entries = re.findall(rb"<item>.*?</item>", raw, re.DOTALL)
        reverse = raw.replace(entries[0], b"__FIRST__").replace(entries[1], entries[0]).replace(b"__FIRST__", entries[1])
        a = parse_feed(raw, "https://weworkremotely.com/remote-jobs.rss", merge_variants=True)
        b = parse_feed(reverse, "https://weworkremotely.com/remote-jobs.rss", merge_variants=True)
        self.assertEqual(a[0]["content_hash"], b[0]["content_hash"])
        self.assertEqual(a[0]["posted_at"], b[0]["posted_at"])
        self.assertEqual(a[0]["posted_at"][:10], "2026-10-18")


if __name__ == "__main__":
    unittest.main()
