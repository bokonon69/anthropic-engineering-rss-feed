import tempfile
import unittest
from pathlib import Path
import xml.etree.ElementTree as ET

from playwright.async_api import async_playwright

from anthropic_rss import AnthropicRSSGenerator


def card(slug="example", title="Example", date="Oct 13, 2026"):
    return {"href": f"/research/{slug}", "title": title, "date": date}


class FeedTests(unittest.TestCase):
    def setUp(self):
        self.generator = AnthropicRSSGenerator()

    def test_migrated_url_has_same_identity(self):
        g = self.generator
        self.assertEqual(g.article_key("https://www.anthropic.com/engineering/example"),
                         g.article_key("https://www.anthropic.com/research/example"))

    def test_rejects_navigation_external_and_non_https_urls(self):
        for url in ("https://www.anthropic.com/research/team/engineering",
                    "https://example.com/research/example",
                    "http://www.anthropic.com/research/example"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.generator.article_key(url)

    def test_missing_or_invalid_date_fails_instead_of_inventing_today(self):
        for date in ("", None, "not a date", "Oct 13", "Oct 2026", "2026-10"):
            with self.subTest(date=date), self.assertRaises((ValueError, TypeError)):
                self.generator.parse_cards([card(date=date)], 1)

    def test_incomplete_listing_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "1/2"):
            self.generator.parse_cards([card()], 2)

    def test_identical_cards_are_deduplicated(self):
        self.assertEqual(len(self.generator.parse_cards([card(), card()], 1)), 1)

    def test_conflicting_duplicates_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "Conflicting duplicate"):
            self.generator.parse_cards([card(), card(title="Other title")], 1)

    def test_migration_preserves_guid_and_archived_entries(self):
        g = self.generator
        old = g.parse_cards([card(), card(slug="archived", date="Sep 1, 2026")], 2)
        old[0]["url"] = "https://www.anthropic.com/engineering/example"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            path.write_bytes(g.generate_rss(old))
            existing = g.load_existing_feed(path)
        current = g.parse_cards([card(), card(slug="new", date="Nov 1, 2026")], 2)
        merged = g.preserve_history(current, existing)
        items = ET.fromstring(g.generate_rss(merged)).findall("./channel/item")
        self.assertEqual(len(items), 3)
        self.assertEqual(items[0].findtext("link"), "https://www.anthropic.com/research/new")
        migrated = items[1]
        self.assertEqual(migrated.findtext("link"), "https://www.anthropic.com/research/example")
        self.assertEqual(migrated.findtext("guid"), "https://www.anthropic.com/engineering/example")
        self.assertEqual(migrated.findtext("pubDate"), "Tue, 13 Oct 2026 00:00:00 +0000")

    def test_duplicate_guids_are_rejected(self):
        articles = self.generator.parse_cards([card(), card(slug="other")], 2)
        for article in articles:
            article["guid"] = "duplicate"
        with self.assertRaisesRegex(ValueError, "duplicate GUIDs"):
            self.generator.generate_rss(articles)

    def test_invalid_output_leaves_existing_file_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            path.write_bytes(b"original feed")
            for content in (b"not XML", b"<rss><channel/></rss>"):
                with self.subTest(content=content), self.assertRaises((ET.ParseError, ValueError)):
                    self.generator.write_feed(path, content)
                self.assertEqual(path.read_bytes(), b"original feed")

    def test_corrupt_existing_feed_is_not_silently_discarded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "feed.xml"
            path.write_bytes(b"broken XML")
            with self.assertRaises(ET.ParseError):
                self.generator.load_existing_feed(path)


class ListingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.playwright = await async_playwright().start()
        self.browser = await self.playwright.chromium.launch()
        self.page = await self.browser.new_page()
        self.generator = AnthropicRSSGenerator()

    async def asyncTearDown(self):
        await self.browser.close()
        await self.playwright.stop()

    async def test_card_title_and_sibling_time_with_pagination(self):
        await self.page.set_content("""
            <p data-listing-state>2</p><span data-listing-state>Most recent</span>
            <a href='/research/team/alignment'>Other research</a>
            <div data-listing-rows>
              <article data-listing-row='one' style='visibility:hidden'>
                <header><time>Oct 13, 2026</time></header>
                <h3><a href='/research/one'> First article </a></h3>
              </article>
            </div>
            <button onclick="document.querySelector('[data-listing-rows]').insertAdjacentHTML(
              'beforeend', `<article data-listing-row='two'>
              <time datetime='2026-09-01'>September 1, 2026</time>
              <h3><a href='/research/two'>Second article</a></h3></article>`);
              this.remove()">See more</button>
        """)
        articles = await self.generator.extract_listing(self.page)
        self.assertEqual([a["title"] for a in articles], ["First article", "Second article"])
        self.assertEqual(articles[1]["date"].isoformat(), "2026-09-01T00:00:00+00:00")

    async def test_missing_pagination_fails(self):
        await self.page.set_content("""
            <p data-listing-state>2</p><div data-listing-rows>
              <article data-listing-row='one'><time>Oct 13, 2026</time>
                <h3><a href='/research/one'>First article</a></h3>
              </article>
            </div>
        """)
        with self.assertRaisesRegex(RuntimeError, "1/2"):
            await self.generator.extract_listing(self.page)

    async def test_missing_total_fails(self):
        await self.page.set_content("""
            <div data-listing-rows><article data-listing-row='one'>
            <time>Oct 13, 2026</time><h3><a href='/research/one'>First article</a></h3>
            </article></div>
        """)
        with self.assertRaisesRegex(RuntimeError, "Cannot establish"):
            await self.generator.extract_listing(self.page)


if __name__ == "__main__":
    unittest.main()
