#!/usr/bin/env python3
"""
Universal RSS bot:
- YouTube feeds (Atom)
- classic RSS/Atom feeds from websites
- Reddit feeds with anti-bot workarounds (UA, cookies, old.reddit fallback)

The bot is NOT tied to Telegram/Discord/etc.
Notifications are sent via pluggable channels:
1) Desktop notifications on PC
2) ntfy (recommended in self-hosted mode) for Android + PC

Usage:
    python Code.py --config config.json
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
import subprocess
import threading
import time
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import xml.etree.ElementTree as ET


DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class FeedItem:
    source_name: str
    source_url: str
    item_id: str
    title: str
    link: str
    published_ts: int | None


class SeenStore:
    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_items (
                item_id TEXT PRIMARY KEY,
                seen_at INTEGER NOT NULL
            )
            """
        )
        self.conn.commit()

    def is_seen(self, item_id: str) -> bool:
        row = self.conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ?", (item_id,)
        ).fetchone()
        return row is not None

    def mark_seen(self, item_id: str) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO seen_items(item_id, seen_at) VALUES(?, strftime('%s','now'))",
            (item_id,),
        )
        self.conn.commit()


class FeedStore:
    """Persistent feed registry so users can manage feeds without editing config.json."""

    def __init__(self, db_path: Path) -> None:
        self.conn = sqlite3.connect(db_path)
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS feeds (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('rss', 'youtube', 'reddit')),
                url TEXT,
                channel_id TEXT,
                subreddit TEXT,
                cookies TEXT,
                enabled INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        self.conn.commit()

    def add_feed(self, feed: dict[str, Any]) -> int:
        cur = self.conn.execute(
            """
            INSERT INTO feeds(name, type, url, channel_id, subreddit, cookies, enabled)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            """,
            (
                feed["name"],
                feed["type"],
                feed.get("url"),
                feed.get("channel_id"),
                feed.get("subreddit"),
                feed.get("cookies"),
                1 if feed.get("enabled", True) else 0,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def list_feeds(self, enabled_only: bool = True) -> list[dict[str, Any]]:
        if enabled_only:
            rows = self.conn.execute(
                """
                SELECT id, name, type, url, channel_id, subreddit, cookies, enabled
                FROM feeds
                WHERE enabled = 1
                ORDER BY id
                """
            ).fetchall()
        else:
            rows = self.conn.execute(
                """
                SELECT id, name, type, url, channel_id, subreddit, cookies, enabled
                FROM feeds
                ORDER BY id
                """
            ).fetchall()
        out: list[dict[str, Any]] = []
        for row in rows:
            out.append(
                {
                    "id": row[0],
                    "name": row[1],
                    "type": row[2],
                    "url": row[3],
                    "channel_id": row[4],
                    "subreddit": row[5],
                    "cookies": row[6],
                    "enabled": bool(row[7]),
                }
            )
        return out

    def set_enabled(self, feed_id: int, enabled: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE feeds SET enabled = ? WHERE id = ?",
            (1 if enabled else 0, feed_id),
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete_feed(self, feed_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
        self.conn.commit()
        return cur.rowcount > 0


class Notifier:
    def send(self, title: str, body: str, link: str) -> None:
        raise NotImplementedError


class DesktopNotifier(Notifier):
    """Linux/macOS desktop notification using notify-send (Linux) or osascript (macOS)."""

    def __init__(self, timeout_ms: int = 7000) -> None:
        self.timeout_ms = timeout_ms

    def send(self, title: str, body: str, link: str) -> None:
        message = f"{body}\n{link}" if link else body
        try:
            subprocess.run(
                ["notify-send", "-t", str(self.timeout_ms), title, message],
                check=False,
                capture_output=True,
                text=True,
            )
            return
        except FileNotFoundError:
            pass

        # macOS fallback
        script = (
            f'display notification "{message[:180]}" '
            f'with title "{title[:80]}"'
        )
        try:
            subprocess.run(["osascript", "-e", script], check=False)
        except FileNotFoundError:
            logging.warning("No desktop notifier found (notify-send/osascript).")


class NtfyNotifier(Notifier):
    """ntfy publisher (works with public or self-hosted ntfy server)."""

    def __init__(self, base_url: str, topic: str, token: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.topic = topic
        self.token = token

    def send(self, title: str, body: str, link: str) -> None:
        payload = body if not link else f"{body}\n{link}"
        url = f"{self.base_url}/{self.topic}"
        headers = {
            "Title": title[:120],
            "Priority": "default",
            "Tags": "newspaper",
            "User-Agent": DEFAULT_UA,
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"

        req = Request(url=url, data=payload.encode("utf-8"), headers=headers, method="POST")
        with urlopen(req, timeout=20):
            pass


class FeedFetcher:
    def __init__(self, user_agent: str = DEFAULT_UA, timeout: int = 20) -> None:
        self.user_agent = user_agent
        self.timeout = timeout

    def get(self, url: str, cookies: str | None = None) -> bytes:
        headers = {"User-Agent": self.user_agent, "Accept": "application/rss+xml, application/atom+xml, text/xml, */*"}
        if cookies:
            headers["Cookie"] = cookies
        request = Request(url=url, headers=headers)
        with urlopen(request, timeout=self.timeout) as resp:
            return resp.read()


class FeedParser:
    @staticmethod
    def parse(feed_name: str, feed_url: str, raw_xml: bytes) -> list[FeedItem]:
        root = ET.fromstring(raw_xml)
        tag = FeedParser._strip_ns(root.tag)

        if tag == "rss":
            return FeedParser._parse_rss(feed_name, feed_url, root)
        if tag == "feed":
            return FeedParser._parse_atom(feed_name, feed_url, root)
        raise ValueError(f"Unsupported feed root tag: {root.tag}")

    @staticmethod
    def _parse_rss(feed_name: str, feed_url: str, root: ET.Element) -> list[FeedItem]:
        channel = root.find("channel")
        if channel is None:
            return []

        out: list[FeedItem] = []
        for item in channel.findall("item"):
            title = (item.findtext("title") or "(no title)").strip()
            link = (item.findtext("link") or "").strip()
            guid = (item.findtext("guid") or link or title).strip()
            pub_date = item.findtext("pubDate")
            out.append(
                FeedItem(
                    source_name=feed_name,
                    source_url=feed_url,
                    item_id=f"{feed_url}::{guid}",
                    title=title,
                    link=link,
                    published_ts=FeedParser._parse_time(pub_date),
                )
            )
        return out

    @staticmethod
    def _parse_atom(feed_name: str, feed_url: str, root: ET.Element) -> list[FeedItem]:
        ns = ""
        if root.tag.startswith("{"):
            ns = root.tag.split("}")[0] + "}"

        out: list[FeedItem] = []
        for entry in root.findall(f"{ns}entry"):
            title = (entry.findtext(f"{ns}title") or "(no title)").strip()
            item_id = (entry.findtext(f"{ns}id") or title).strip()

            link = ""
            for link_el in entry.findall(f"{ns}link"):
                href = link_el.attrib.get("href", "")
                rel = link_el.attrib.get("rel", "alternate")
                if rel == "alternate" and href:
                    link = href
                    break
                if not link and href:
                    link = href

            published = entry.findtext(f"{ns}published") or entry.findtext(f"{ns}updated")
            out.append(
                FeedItem(
                    source_name=feed_name,
                    source_url=feed_url,
                    item_id=f"{feed_url}::{item_id}",
                    title=title,
                    link=link,
                    published_ts=FeedParser._parse_time(published),
                )
            )
        return out

    @staticmethod
    def _parse_time(value: str | None) -> int | None:
        if not value:
            return None
        value = value.strip()
        try:
            if "T" in value and (value.endswith("Z") or "+" in value or "-" in value[10:]):
                return int(
                    __import__("datetime")
                    .datetime.fromisoformat(value.replace("Z", "+00:00"))
                    .timestamp()
                )
            return int(parsedate_to_datetime(value).timestamp())
        except Exception:
            return None

    @staticmethod
    def _strip_ns(tag: str) -> str:
        return tag.split("}", 1)[1] if "}" in tag else tag


class RssBot:
    def __init__(
        self,
        feeds: list[dict[str, Any]],
        notifiers: list[Notifier],
        store: SeenStore,
        poll_interval_sec: int = 180,
    ) -> None:
        self.feeds = feeds
        self.notifiers = notifiers
        self.store = store
        self.poll_interval_sec = poll_interval_sec
        self.fetcher = FeedFetcher()

    def run_forever(self) -> None:
        logging.info("Starting RSS bot: %d feeds, interval=%ss", len(self.feeds), self.poll_interval_sec)
        while True:
            for feed in self.feeds:
                self._process_feed(feed)
            time.sleep(self.poll_interval_sec)

    def _process_feed(self, feed_cfg: dict[str, Any]) -> None:
        feed_type = feed_cfg.get("type", "rss")
        name = feed_cfg.get("name", "unknown")
        url = self._normalize_feed_url(feed_cfg)
        cookies = feed_cfg.get("cookies")

        try:
            raw = self.fetcher.get(url, cookies=cookies)
        except HTTPError as e:
            if feed_type == "reddit":
                fallback = self._reddit_fallback_url(feed_cfg)
                if fallback and fallback != url:
                    logging.warning("%s: HTTP %s on %s, trying fallback %s", name, e.code, url, fallback)
                    raw = self.fetcher.get(fallback, cookies=cookies)
                    url = fallback
                else:
                    logging.error("%s: HTTP error %s (%s)", name, e.code, url)
                    return
            else:
                logging.error("%s: HTTP error %s (%s)", name, e.code, url)
                return
        except URLError as e:
            logging.error("%s: network error %s (%s)", name, e.reason, url)
            return
        except Exception as e:
            logging.exception("%s: unknown fetch error (%s): %s", name, url, e)
            return

        try:
            items = FeedParser.parse(name, url, raw)
        except Exception as e:
            logging.error("%s: parse error (%s): %s", name, url, e)
            return

        new_items = [it for it in items if not self.store.is_seen(it.item_id)]
        new_items.sort(key=lambda x: (x.published_ts or 0, x.item_id))

        for item in new_items:
            title = f"[{item.source_name}] {item.title}"
            body = f"New item from {item.source_url}"
            for notifier in self.notifiers:
                try:
                    notifier.send(title=title, body=body, link=item.link)
                except Exception as e:
                    logging.error("Notifier error (%s): %s", notifier.__class__.__name__, e)
            self.store.mark_seen(item.item_id)
            logging.info("Notified: %s", item.title)

    def _normalize_feed_url(self, feed_cfg: dict[str, Any]) -> str:
        ftype = feed_cfg.get("type", "rss")
        if ftype == "youtube":
            channel_id = feed_cfg.get("channel_id")
            if not channel_id:
                raise ValueError("youtube feed requires channel_id")
            return f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

        if ftype == "reddit":
            subreddit = feed_cfg.get("subreddit")
            if subreddit:
                return f"https://www.reddit.com/r/{subreddit}/new/.rss"

        url = feed_cfg.get("url")
        if not url:
            raise ValueError("feed requires url")
        return url

    def _reddit_fallback_url(self, feed_cfg: dict[str, Any]) -> str | None:
        subreddit = feed_cfg.get("subreddit")
        if not subreddit:
            return None
        return f"https://old.reddit.com/r/{subreddit}/new/.rss"


def load_config(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if "feeds" not in data or not isinstance(data["feeds"], list):
        raise ValueError("config must contain 'feeds' list")
    return data


def build_notifiers(cfg: dict[str, Any]) -> list[Notifier]:
    out: list[Notifier] = []

    if cfg.get("desktop_notifications", True):
        out.append(DesktopNotifier(timeout_ms=int(cfg.get("desktop_timeout_ms", 7000))))

    ntfy_cfg = cfg.get("ntfy")
    if isinstance(ntfy_cfg, dict) and ntfy_cfg.get("enabled"):
        out.append(
            NtfyNotifier(
                base_url=ntfy_cfg.get("base_url", "https://ntfy.sh"),
                topic=ntfy_cfg["topic"],
                token=ntfy_cfg.get("token"),
            )
        )

    if not out:
        raise ValueError("No notifiers enabled")
    return out


def validate_feeds(feeds: Iterable[dict[str, Any]]) -> None:
    for i, feed in enumerate(feeds):
        ftype = feed.get("type", "rss")
        if ftype == "youtube" and not feed.get("channel_id"):
            raise ValueError(f"feeds[{i}]: youtube requires channel_id")
        if ftype == "reddit" and not (feed.get("subreddit") or feed.get("url")):
            raise ValueError(f"feeds[{i}]: reddit requires subreddit or url")
        if ftype == "rss" and not feed.get("url"):
            raise ValueError(f"feeds[{i}]: rss requires url")


def parse_feed_from_args(args: argparse.Namespace) -> dict[str, Any]:
    feed: dict[str, Any] = {
        "name": args.name,
        "type": args.feed_type,
        "url": args.url,
        "channel_id": args.channel_id,
        "subreddit": args.subreddit,
        "cookies": args.cookies,
        "enabled": True,
    }
    validate_feeds([feed])
    return feed


def start_health_server(host: str, port: int) -> ThreadingHTTPServer:
    class HealthHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            if self.path in ("/", "/health", "/healthz"):
                payload = b"ok"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            logging.debug("health-server: " + format, *args)

    server = ThreadingHTTPServer((host, port), HealthHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    logging.info("Health server started at http://%s:%s", host, port)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Universal RSS bot")
    parser.add_argument("--config", default="config.json", help="Path to JSON config")
    parser.add_argument("--db-path", default=None, help="Override db path (default from config or seen.db)")
    parser.add_argument("--once", action="store_true", help="Run one iteration and exit (for default/run command)")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run polling bot")
    run_parser.add_argument("--config", help="Path to JSON config (overrides global --config)")
    run_parser.add_argument("--db-path", help="Override db path (overrides global --db-path)")
    run_parser.add_argument("--once", action="store_true", help="Run one iteration and exit")
    run_parser.add_argument("--health-host", default="0.0.0.0", help="Health server bind host")
    run_parser.add_argument("--health-port", type=int, default=None, help="Health server port (disabled if omitted)")

    add_parser = subparsers.add_parser("add-feed", help="Add feed to database")
    add_parser.add_argument("--name", required=True, help="Feed display name")
    add_parser.add_argument("--feed-type", required=True, choices=["rss", "youtube", "reddit"])
    add_parser.add_argument("--url", help="Feed URL for rss or custom reddit feed URL")
    add_parser.add_argument("--channel-id", help="YouTube channel id (for feed-type=youtube)")
    add_parser.add_argument("--subreddit", help="Subreddit name (for feed-type=reddit)")
    add_parser.add_argument("--cookies", help="Optional Cookie header for anti-bot")

    list_parser = subparsers.add_parser("list-feeds", help="List stored feeds")
    list_parser.add_argument("--all", action="store_true", help="Show disabled feeds too")

    disable_parser = subparsers.add_parser("disable-feed", help="Disable feed by id")
    disable_parser.add_argument("--id", required=True, type=int)

    enable_parser = subparsers.add_parser("enable-feed", help="Enable feed by id")
    enable_parser.add_argument("--id", required=True, type=int)

    remove_parser = subparsers.add_parser("remove-feed", help="Delete feed by id")
    remove_parser.add_argument("--id", required=True, type=int)

    import_parser = subparsers.add_parser(
        "import-feeds-from-config",
        help="Import feeds from config.json into DB once",
    )
    import_parser.add_argument("--replace", action="store_true", help="Delete existing feeds before import")

    args = parser.parse_args()
    command = args.command or "run"

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    cfg_path = Path(args.config)
    cfg = load_config(cfg_path)
    db_path = Path(args.db_path or cfg.get("db_path", "seen.db"))
    feed_store = FeedStore(db_path)

    if command == "add-feed":
        feed = parse_feed_from_args(args)
        feed_id = feed_store.add_feed(feed)
        print(f"Added feed #{feed_id}: {feed['name']}")
        return

    if command == "list-feeds":
        feeds = feed_store.list_feeds(enabled_only=not args.all)
        if not feeds:
            print("No feeds found.")
            return
        for feed in feeds:
            print(
                f"#{feed['id']} | {'ENABLED' if feed['enabled'] else 'DISABLED'} | "
                f"{feed['type']} | {feed['name']} | url={feed.get('url') or '-'} | "
                f"channel_id={feed.get('channel_id') or '-'} | subreddit={feed.get('subreddit') or '-'}"
            )
        return

    if command == "disable-feed":
        if not feed_store.set_enabled(args.id, False):
            raise SystemExit(f"Feed with id={args.id} not found")
        print(f"Feed #{args.id} disabled")
        return

    if command == "enable-feed":
        if not feed_store.set_enabled(args.id, True):
            raise SystemExit(f"Feed with id={args.id} not found")
        print(f"Feed #{args.id} enabled")
        return

    if command == "remove-feed":
        if not feed_store.delete_feed(args.id):
            raise SystemExit(f"Feed with id={args.id} not found")
        print(f"Feed #{args.id} removed")
        return

    if command == "import-feeds-from-config":
        validate_feeds(cfg["feeds"])
        if args.replace:
            ids = [f["id"] for f in feed_store.list_feeds(enabled_only=False)]
            for feed_id in ids:
                feed_store.delete_feed(feed_id)
        for feed in cfg["feeds"]:
            feed_store.add_feed(feed)
        print(f"Imported {len(cfg['feeds'])} feeds from config")
        return

    validate_feeds(cfg["feeds"])
    interval = int(cfg.get("poll_interval_sec", 180))
    notifiers = build_notifiers(cfg)
    feeds_from_db = feed_store.list_feeds(enabled_only=True)
    active_feeds = feeds_from_db if feeds_from_db else cfg["feeds"]

    bot = RssBot(
        feeds=active_feeds,
        notifiers=notifiers,
        store=SeenStore(db_path),
        poll_interval_sec=interval,
    )

    health_port = getattr(args, "health_port", None)
    if health_port is not None:
        start_health_server(getattr(args, "health_host", "0.0.0.0"), int(health_port))

    run_once = bool(getattr(args, "once", False))
    if run_once:
        for feed in active_feeds:
            bot._process_feed(feed)
    else:
        bot.run_forever()


if __name__ == "__main__":
    main()
