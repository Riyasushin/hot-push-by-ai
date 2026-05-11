from ai_radar.fetchers.base import Fetcher, Item
from ai_radar.fetchers.qidian import QidianFetcher
from ai_radar.fetchers.rss import RSSFetcher
from ai_radar.fetchers.weread import WeReadFetcher

FETCHERS: dict[str, Fetcher] = {
    "rss": RSSFetcher(),
    "weread": WeReadFetcher(),
    "qidian": QidianFetcher(),
}

__all__ = ["FETCHERS", "Fetcher", "Item"]
