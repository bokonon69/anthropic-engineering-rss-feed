import argparse
import asyncio
from datetime import timezone
import json
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urljoin, urlparse
import xml.etree.ElementTree as ET

from dateutil import parser as date_parser
from feedgen.feed import FeedGenerator
from playwright.async_api import async_playwright


class AnthropicRSSGenerator:
    CARD_SELECTOR = "[data-listing-rows] article[data-listing-row]"

    def __init__(self):
        self.base_url = "https://www.anthropic.com/engineering"

    def article_key(self, url):
        """Identify the same article before and after the engineering URL migration."""
        parsed = urlparse(url)
        parts = parsed.path.strip("/").split("/")
        if (
            parsed.scheme != "https"
            or parsed.netloc != "www.anthropic.com"
            or len(parts) != 2
            or parts[0] not in ("engineering", "research")
            or not parts[1]
        ):
            raise ValueError(f"Unexpected article URL: {url}")
        return parts[1]

    def normalize_text(self, text):
        return re.sub(r"\s+", " ", text or "").strip()

    def parse_date(self, date_text):
        # Never invent a publication date when extraction fails.
        if not date_text or not date_text.strip():
            raise ValueError("Missing publication date")
        if not re.search(
            r"\b(?:\d{4}-\d{2}-\d{2}|[A-Za-z]+\s+\d{1,2},\s+\d{4}|\d{1,2}\s+[A-Za-z]+\s+\d{4})\b",
            date_text,
        ):
            raise ValueError(f"Incomplete publication date: {date_text}")
        parsed = date_parser.parse(date_text.strip())
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    async def extract_listing(self, page):
        # Cards can be hidden while the site's entrance animations initialize.
        await page.locator(self.CARD_SELECTOR).first.wait_for(state="attached")
        counts = await page.locator("[data-listing-state]").evaluate_all(
            "nodes => nodes.map(node => node.textContent.trim())"
        )
        totals = [int(text) for text in counts if re.fullmatch(r"\d+", text)]
        if len(totals) != 1 or totals[0] < 1:
            raise RuntimeError(f"Cannot establish Engineering article count: {counts}")
        expected_count = totals[0]

        for _ in range(100):
            count = await page.locator(self.CARD_SELECTOR).count()
            if count >= expected_count:
                break
            more = page.get_by_role("button", name="See more", exact=True)
            if await more.count() != 1:
                raise RuntimeError(f"Only {count}/{expected_count} article cards; no pagination button")
            await more.click(timeout=15000)
            await page.wait_for_function(
                "args => document.querySelectorAll(args.selector).length > args.count",
                arg={"selector": self.CARD_SELECTOR, "count": count},
                timeout=15000,
            )
        else:
            raise RuntimeError("Engineering pagination exceeded 100 pages")

        cards = await page.locator(self.CARD_SELECTOR).evaluate_all(
            """cards => cards.map(card => {
                const link = card.querySelector('h1 a[href], h2 a[href], h3 a[href]');
                const time = card.querySelector('time');
                return {
                    href: link?.getAttribute('href') || '',
                    title: link?.textContent || '',
                    date: time?.getAttribute('datetime') || time?.textContent || '',
                    summary: card.querySelector('p')?.textContent || ''
                };
            })"""
        )
        return self.parse_cards(cards, expected_count)

    def parse_cards(self, cards, expected_count):
        articles = {}
        for card in cards:
            url = urljoin(self.base_url, card["href"])
            key = self.article_key(url)
            title = self.normalize_text(card["title"])
            if not title:
                raise ValueError(f"Missing article title: {url}")
            article = {
                "title": title,
                "url": url,
                "date": self.parse_date(card["date"]),
                "description": self.normalize_text(card.get("summary")) or title,
            }
            if key in articles and articles[key] != article:
                raise ValueError(f"Conflicting duplicate article: {url}")
            articles[key] = article
        if len(articles) != expected_count:
            raise RuntimeError(f"Extracted {len(articles)}/{expected_count} Engineering articles")
        return sorted(articles.values(), key=lambda article: article["date"], reverse=True)

    async def fetch_posts(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            status = None
            try:
                response = await page.goto(self.base_url, wait_until="domcontentloaded")
                status = response.status if response else None
                print(f"Engineering response: HTTP {status}, URL {page.url}")
                if response is None or not response.ok:
                    raise RuntimeError(f"Engineering page returned HTTP {status}")
                articles = await self.extract_listing(page)
                for article in articles:
                    print(f"Found: {article['title']} - {article['date'].date()}")
                print(f"Validated complete listing: {len(articles)} articles")
                return articles
            except Exception as error:
                diagnostics = Path("diagnostics")
                diagnostics.mkdir(exist_ok=True)
                (diagnostics / "response.json").write_text(
                    json.dumps({"status": status, "url": page.url, "error": str(error)}, indent=2),
                    encoding="utf-8",
                )
                try:
                    (diagnostics / "engineering.html").write_text(await page.content(), encoding="utf-8")
                    await page.screenshot(path=str(diagnostics / "engineering.png"), timeout=5000)
                except Exception as diagnostic_error:
                    print(f"Could not capture all page diagnostics: {diagnostic_error}")
                raise
            finally:
                await browser.close()

    def load_existing_feed(self, path):
        path = Path(path)
        if not path.exists():
            return {}
        root = ET.parse(path).getroot()
        items = root.findall("./channel/item")
        if root.tag != "rss" or not items:
            raise ValueError("Existing feed is not a populated RSS document")
        articles = {}
        for item in items:
            url = item.findtext("link")
            key = self.article_key(url or "")
            guid = item.find("guid")
            if guid is None or not guid.text or key in articles:
                raise ValueError(f"Missing GUID or duplicate article in existing feed: {url}")
            articles[key] = {
                "title": item.findtext("title"),
                "url": url,
                "date": self.parse_date(item.findtext("pubDate")),
                "description": item.findtext("description") or item.findtext("title"),
                "guid": guid.text,
                "guid_permalink": guid.get("isPermaLink", "true").lower() == "true",
            }
        return articles

    def preserve_history(self, articles, existing):
        # Keep archived entries too, should Anthropic remove them from its listing.
        merged = {key: dict(article) for key, article in existing.items()}
        for article in articles:
            key = self.article_key(article["url"])
            previous = existing.get(key, {})
            merged[key] = {
                **article,
                "guid": previous.get("guid", article["url"]),
                "guid_permalink": previous.get("guid_permalink", True),
            }
        return sorted(merged.values(), key=lambda article: article["date"], reverse=True)

    def create_feed(self):
        feed = FeedGenerator()
        feed.title("Anthropic Engineering Blog")
        feed.link(href=self.base_url, rel="alternate")
        feed.description("Latest engineering posts from Anthropic")
        feed.language("en")
        feed.link(
            href="https://raw.githubusercontent.com/bokonon69/anthropic-engineering-rss-feed/main/anthropic_engineering_rss.xml",
            rel="self",
        )
        return feed

    def generate_rss(self, articles):
        if not articles:
            raise ValueError("Refusing to generate an empty feed")
        feed = self.create_feed()
        # feedgen emits RSS items in reverse insertion order.
        for article in reversed(articles):
            entry = feed.add_entry()
            entry.title(article["title"])
            entry.link(href=article["url"])
            entry.pubDate(article["date"])
            entry.description(article["description"])
            entry.guid(article.get("guid", article["url"]), permalink=article.get("guid_permalink", True))
        content = feed.rss_str(pretty=True)
        items = ET.fromstring(content).findall("./channel/item")
        guids = [item.findtext("guid") for item in items]
        if len(items) != len(articles) or len(set(guids)) != len(guids):
            raise ValueError("Generated feed has missing entries or duplicate GUIDs")
        return content

    def write_feed(self, path, content):
        # Validate before opening anything, then replace atomically.
        root = ET.fromstring(content)
        if root.tag != "rss" or not root.findall("./channel/item"):
            raise ValueError("Refusing to write an empty or invalid RSS document")
        path = Path(path)
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=path.parent, prefix=path.name + ".", delete=False) as output:
                temporary = Path(output.name)
                output.write(content)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="anthropic_engineering_rss.xml")
    args = parser.parse_args()
    generator = AnthropicRSSGenerator()
    existing = generator.load_existing_feed("anthropic_engineering_rss.xml")
    articles = generator.preserve_history(await generator.fetch_posts(), existing)
    generator.write_feed(args.output, generator.generate_rss(articles))
    print(f"RSS feed generated successfully: {len(articles)} entries, {len(existing)} existing GUIDs preserved")


if __name__ == "__main__":
    asyncio.run(main())
