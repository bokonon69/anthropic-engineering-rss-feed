import asyncio
from datetime import datetime, timezone
from feedgen.feed import FeedGenerator
import html
from playwright.async_api import async_playwright
import re
import urllib.request
from urllib.parse import urljoin, urlparse
from dateutil import parser as date_parser

class AnthropicRSSGenerator:
    DATE_PATTERN = re.compile(
        r"\b(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:tember)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\s+\d{1,2},\s+\d{4}\b",
        re.IGNORECASE,
    )

    def __init__(self):
        self.base_url = "https://www.anthropic.com/engineering"

    def is_engineering_post_url(self, url):
        parsed = urlparse(url)
        return (
            parsed.netloc == "www.anthropic.com"
            and parsed.path.startswith("/engineering/")
            and parsed.path != "/engineering/"
        )

    def normalize_text(self, text):
        return re.sub(r"\s+", " ", text or "").strip()

    def parse_anchor_text(self, text):
        text = self.normalize_text(text)
        text = re.sub(r"^Featured\s+", "", text, flags=re.IGNORECASE)
        match = self.DATE_PATTERN.search(text)
        if not match:
            return None, None

        title = text[:match.start()].strip(" -–—")
        return title, match.group(0)

    def extract_meta_content(self, html_content, field_names):
        for field_name in field_names:
            patterns = [
                rf'<meta[^>]+(?:name|property)=["\']{re.escape(field_name)}["\'][^>]+content=["\']([^"\']+)["\']',
                rf'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:name|property)=["\']{re.escape(field_name)}["\']',
            ]
            for pattern in patterns:
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    return self.normalize_text(html.unescape(match.group(1)))
        return None

    def fetch_article_details_sync(self, url):
        request = urllib.request.Request(
            url,
            headers={
                "User-Agent": "Mozilla/5.0 (compatible; AnthropicEngineeringRSS/1.0)",
            },
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            html_content = response.read().decode("utf-8", "replace")

        date_match = re.search(
            r"Published\s*(?:<!--\s*-->)?\s*(" + self.DATE_PATTERN.pattern + r")",
            html_content,
            re.IGNORECASE,
        )
        date_text = date_match.group(1) if date_match else None
        description = self.extract_meta_content(
            html_content,
            ["description", "og:description", "twitter:description"],
        )
        return date_text, description

    async def fetch_article_details(self, url):
        try:
            return await asyncio.to_thread(self.fetch_article_details_sync, url)
        except Exception as e:
            print(f"Error fetching article details for {url}: {e}")
            return None, None

    def parse_date(self, date_text):
        """Parse date text and return a datetime object with timezone"""
        try:
            # Clean up the date text
            date_text = date_text.strip()
            
            # Try to parse the date
            parsed_date = date_parser.parse(date_text)
            
            # If no timezone info, assume UTC
            if parsed_date.tzinfo is None:
                parsed_date = parsed_date.replace(tzinfo=timezone.utc)
            
            return parsed_date
        except Exception as e:
            print(f"Error parsing date '{date_text}': {e}")
            # Return current date as fallback with UTC timezone
            return datetime.now(timezone.utc)

    async def fetch_posts(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(self.base_url, wait_until="domcontentloaded")
            await page.wait_for_selector("a[href*='/engineering/']")

            anchors = await page.locator("a[href*='/engineering/']").evaluate_all(
                """links => links.map(link => ({
                    href: link.href,
                    text: link.innerText || link.textContent || "",
                    title: (link.querySelector("h1, h2, h3")?.innerText || "").trim(),
                    date: (link.querySelector("[class*='date']")?.innerText || "").trim(),
                    summary: (link.querySelector("p")?.innerText || "").trim()
                }))"""
            )

            articles_data = []
            seen_urls = set()

            for anchor in anchors:
                try:
                    url = urljoin(self.base_url, anchor["href"])
                    if url in seen_urls or not self.is_engineering_post_url(url):
                        continue

                    title = self.normalize_text(anchor.get("title"))
                    date_text = self.normalize_text(anchor.get("date"))
                    description = self.normalize_text(anchor.get("summary"))

                    if not title or not date_text:
                        parsed_title, parsed_date_text = self.parse_anchor_text(anchor["text"])
                        title = title or parsed_title
                        date_text = date_text or parsed_date_text

                    if not title:
                        continue

                    if not date_text:
                        detail_date_text, detail_description = await self.fetch_article_details(url)
                        date_text = detail_date_text
                        description = description or detail_description

                    description = description or title

                    if not date_text:
                        print(f"Skipping article without publication date: {title}")
                        continue

                    parsed_date = self.parse_date(date_text)
                    seen_urls.add(url)
                    articles_data.append({
                        'title': title,
                        'url': url,
                        'date': parsed_date,
                        'date_text': date_text,
                        'description': description
                    })
                    
                    print(f"Found: {title} - {date_text}")
                    
                except Exception as e:
                    print(f"Error processing anchor: {e}")

            articles_data.sort(key=lambda x: x['date'], reverse=True)
            await browser.close()

            if not articles_data:
                raise RuntimeError("No engineering posts found. Anthropic may have changed the page structure.")

            return articles_data

    def create_feed(self):
        """Create a fresh feed instance"""
        feed = FeedGenerator()
        feed.title('Anthropic Engineering Blog')
        feed.link(href=self.base_url, rel='alternate')
        feed.description('Latest engineering posts from Anthropic')
        feed.language('en')
        
        # Add atom:link with rel="self" for better interoperability
        # This should be updated to match your actual GitHub Pages URL
        feed.link(href='https://raw.githubusercontent.com/bokonon69/anthropic-engineering-rss-feed/main/anthropic_engineering_rss.xml', rel='self')
        
        return feed

    def generate_rss(self, articles_data):
        # Create a fresh feed and add entries in sorted order
        feed = self.create_feed()
        
        # feedgen emits RSS items in reverse insertion order.
        for article_data in reversed(articles_data):
            entry = feed.add_entry()
            entry.title(article_data['title'])
            entry.link(href=article_data['url'])
            entry.pubDate(article_data['date'])
            entry.description(article_data['description'])
            
            # Add GUID for better interoperability (using the URL as GUID)
            entry.guid(article_data['url'], permalink=True)
            
        # Generate RSS feed content
        rss_content = feed.rss_str(pretty=True)
        return rss_content

async def main():
    generator = AnthropicRSSGenerator()
    articles_data = await generator.fetch_posts()
    rss_content = generator.generate_rss(articles_data)
    
    # Write to file
    with open('anthropic_engineering_rss.xml', 'wb') as f:
        f.write(rss_content)
    
    print("RSS feed generated successfully!")

if __name__ == "__main__":
    asyncio.run(main())
