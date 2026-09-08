from __future__ import annotations

import os
import re
import copy
import json
import asyncio
import logging
import argparse

from base_api.modules.logger import configure_app_logging

from base_api.modules.static_functions import str_to_bool

from dataclasses import dataclass
from curl_cffi import AsyncSession
from selectolax.lexbor import LexborHTMLParser
from typing import AsyncGenerator, ClassVar, Literal
from base_api.modules.type_hints import DownloadReport
from base_api.modules.config import IteratorConfig
from base_api import (
    BaseCore,
    BaseMedia,
    DownloadConfigHLS,
    DownloadConfigRAW,
    ErrorAction,
    ErrorMode,
    Helper,
    MediaLoadError,
    MediaLoadErrors,
    RetryPolicy,
    ScrapeErrorContext,
    ScrapeResult,
    media_field,
    make_iterator_config,
    is_resource_gone,
    default_on_error,
    scrape_stream,
)
from base_api.modules.errors import (
    DownloadCancelled,
    BotProtectionDetected,
    HTTPStatusError,
    InvalidProxy,
    NetworkRequestError,
    ResourceGone,
    UnknownError,
)

from youporn_api.modules.consts import (extractor_html, region_locked_pattern, headers, build_master_playlist,
                                        pick_best_mp4)
from youporn_api.modules.errors import (VideoUnavailable, NetworkError, ProxyError, BotDetection, UnknownNetworkError,
                                        RegionBlocked, DownloadFailed)


logger = logging.getLogger(name="YouPorn API")
logger.addHandler(logging.NullHandler())


_contains_resource_gone = is_resource_gone
on_error = default_on_error


async def get_html_content(core: BaseCore, url: str) -> str:
    try:
        return await core.fetch_text(url)

    except HTTPStatusError as e:
        logger.exception("Request failed for %s: %s", url, e)
        if e.status_code == 404:
            raise VideoUnavailable(f"Video is not available: {url}") from e
        raise NetworkError(f"Request failed for {url}: {e}") from e

    except NetworkRequestError as e:
        logger.exception("Request failed for %s: %s", url, e)
        raise NetworkError(f"Request failed for {url}: {e}") from e

    except InvalidProxy as e:
        logger.exception("Request failed for %s: %s", url, e)
        raise ProxyError(f"Request failed for {url}: {e}") from e

    except BotProtectionDetected as e:
        logger.exception("Request failed for %s: %s", url, e)
        raise BotDetection(f"Request failed for {url}: {e}") from e

    except UnknownError as e:
        logger.exception("Request failed for %s: %s", url, e)
        raise UnknownNetworkError(f"Request failed for {url}: {e}") from e

    except Exception:
        logger.exception("Failed to fetch or decode response for %s", url)
        raise


@dataclass(slots=True, kw_only=True)
class Channel(BaseMedia):
    url: str
    core: BaseCore
    name: str | None = media_field("html")
    channel_rank: str | None = media_field("html")
    total_videos_count: str | None = media_field("html")
    channel_view_count: str | None = media_field("html")
    channel_subscribers_count: str | None = media_field("html")
    description: str | None = media_field("html")

    loader_methods: ClassVar[dict[str, str]] = {"html": "_load_html"}

    async def _load_html(self) -> dict[str, object]:
        logger.info(f"Loading Channel HTML from {self.url}")
        html_content = await get_html_content(core=self.core, url=self.url)
        logger.debug(f"Received HTML Content for: {self.url}")
        return await asyncio.to_thread(self._extract_data, html_content)

    @staticmethod
    def _extract_data(html_content: str) -> dict:
        parser = LexborHTMLParser(html_content)
        name = parser.css_first("h1.name-title").text().replace("Subscribe", "").strip()
        channel_info_box = parser.css_first("div.main-stats-bar")
        channel_rank = channel_info_box.css_first("p.info-stat-data").text(strip=True)
        total_videos_count = channel_info_box.css("p.info-stat-data")[3].text(strip=True)
        channel_view_count = channel_info_box.css("p.info-stat-data")[1].text(strip=True)
        channel_subscribers_count = channel_info_box.css("p.info-stat-data")[2].text()
        description = parser.css_first("div.profile-bio.channel-description").text(strip=True)

        return {
            "name": name,
            "channel_rank": channel_rank,
            "total_videos_count": total_videos_count,
            "channel_view_count": channel_view_count,
            "channel_subscribers_count": channel_subscribers_count,
            "description": description
        }

    def videos(
        self,
        pages: int = 2,
        iterator_config: IteratorConfig | None = None,
    ) -> AsyncGenerator[ScrapeResult[Video], None]:
        url = self.url
        page_urls = [f"{url}?page={page}" for page in range(1, pages + 1)]
        logger.info(f"Requesting channel videos from urls: {page_urls}")
        if iterator_config is None:
            iterator_config = make_iterator_config()

        return scrape_stream(
            core=self.core,
            constructor=Video,
            target_page_urls=page_urls,
            item_extractor=extractor_html,
            iterator_config=iterator_config,
        )


@dataclass(slots=True, kw_only=True)
class Collection(BaseMedia):
    url: str
    core: BaseCore
    name: str | None = media_field("html")
    rating: str | None = media_field("html")
    total_videos_count: str | None = media_field("html")
    view_count: str | None = media_field("html")
    last_updated: str | None = media_field("html")

    loader_methods: ClassVar[dict[str, str]] = {"html": "_load_html"}

    async def _load_html(self) -> dict[str, object]:
        logger.info(f"Loading Collection HTML from {self.url}")
        html_content = await get_html_content(core=self.core, url=self.url)
        data = await asyncio.to_thread(self._extract_data, html_content)
        logger.debug("Finished extracting attributes for Collection")
        return data

    @staticmethod
    def _extract_data(html_content: str) -> dict:
        parser = LexborHTMLParser(html_content)

        name = parser.css_first("div.top-section").css_first("h4").text().replace("Collection:", "").strip()
        rating = parser.css_first("div.featureCollectionRating").text(strip=True)
        total_videos_count = parser.css_first("p.collection-videos-count").text(strip=True)
        view_count = parser.css_first("div.top-section").css("li")[1].css_first("p").text(strip=True)
        last_updated = parser.css_first("li.lastUpdated > p").text(strip=True)
        return {
            "name": name,
            "rating": rating,
            "total_videos_count": total_videos_count,
            "view_count": view_count,
            "last_updated": last_updated
        }

    def videos(
        self,
        pages: int = 2,
        iterator_config: IteratorConfig | None = None,
    ) -> AsyncGenerator[ScrapeResult[Video], None]:

        url = self.url
        page_urls = [f"{url}?page={page}" for page in range(1, pages + 1)]
        logger.info(f"Requesting collection videos from urls: {page_urls}")
        if iterator_config is None:
            iterator_config = make_iterator_config()

        return scrape_stream(
            core=self.core,
            constructor=Video,
            target_page_urls=page_urls,
            item_extractor=extractor_html,
            iterator_config=iterator_config,
        )

@dataclass(slots=True, kw_only=True)
class Pornstar(BaseMedia):
    url: str
    core: BaseCore
    name: str | None = media_field("html")
    profile_info: dict | None = media_field("html")

    loader_methods: ClassVar[dict[str, str]] = {"html": "_load_html"}

    async def _load_html(self) -> dict[str, object]:
        logger.info(f"Loading Pornstar HTML from {self.url}")
        html_content = await get_html_content(core=self.core, url=self.url)
        return await asyncio.to_thread(self._extract_data, html_content)

    def _extract_data(self, html_content: str) -> dict:
        parser = LexborHTMLParser(html_content)
        name = parser.css_first("h1.name-title").text(strip=True)
        dictionary = {}

        if not "/amateur/" in self.url:
            profile_info = parser.css_first("ul.profile-info")
            li_tags = profile_info.css("li.info-stat")

            for tag in li_tags:
                stuff = tag.css("p")
                key = stuff[0].text(strip=True)
                item = stuff[1].text(strip=True)
                dictionary.update({key: item})

        return {
            "name": name,
            "profile_info": dictionary
        }

    def videos(
        self,
        pages: int = 2,
        iterator_config: IteratorConfig | None = None,
    ) -> AsyncGenerator[ScrapeResult[Video], None]:
        page_urls = [f"{self.url}?page={page}" for page in range(1, pages + 1)]
        logger.info(f"Requesting pornstar videos from urls: {page_urls}")
        if iterator_config is None:
            iterator_config = make_iterator_config()

        return scrape_stream(
            core=self.core,
            constructor=Video,
            target_page_urls=page_urls,
            item_extractor=extractor_html,
            iterator_config=iterator_config,
        )


@dataclass(kw_only=True, slots=True)
class User(BaseMedia):
    url: str
    core: BaseCore
    name: str | None = media_field("html")
    collection_urls: list[str] | None = media_field("html")

    loader_methods: ClassVar[dict[str, str]] = {"html": "_load_html"}

    async def _load_html(self) -> dict[str, object]:
        logger.info(f"Loading User HTML from {self.url}")
        html_content = await get_html_content(core=self.core, url=self.url)
        return await asyncio.to_thread(self._extract_data, html_content)

    @staticmethod
    def _extract_data(html_content: str) -> dict:
        parser = LexborHTMLParser(html_content)
        name = parser.css_first("h1.name-title").text(strip=True)

        container = parser.css_first("ul.playlists_list")
        _collections = container.css("li.playlists-container")
        urls = []

        for collection_container in _collections:
            urls.append(f'https://youporn.com{collection_container.css_first("a").attributes.get("href")}')

        return {
            "name": name,
            "collection_urls": urls
        }

    async def get_collections(self, load_html: bool = True) -> AsyncGenerator[Collection, None]:
        collection_urls = await self.get_field("collection_urls")
        logger.info(f"Getting collections for User: {self.name or self.url}")
        for collection_url in collection_urls:
            collection = Collection(url=collection_url, core=self.core)
            if load_html:
                await collection.load_sources("html")
            yield collection


@dataclass(slots=True, kw_only=True)
class Video(BaseMedia):
    url: str
    core: BaseCore
    title: str | None = media_field("html")
    publish_date: str | None = media_field("html")
    length: str | None = media_field("html")
    rating: str | None = media_field("html")
    views: str | None = media_field("html")
    thumbnail: str | None = media_field("html")
    categories: list[str] | None = media_field("html")
    m3u8_base_url: str | None = media_field("html")
    author_link: str | None = media_field("html")
    pornstars_urls: list[str] | None = media_field("html")

    # Only when comming from the iterator, if they are None, it is how it is...
    uploader_id: str | None = None
    uploader_status: str | None = None
    uploader_type: str | None = None
    uploader_name: str | None = None
    video_id: str | None = None

    # You don't need this
    is_hls: bool | None = media_field("html")

    loader_methods: ClassVar[dict[str, str]] = {"html": "_load_html"}

    async def _load_html(self) -> dict[str, object]:

        logger.info(f"Loading Video HTML from {self.url}")
        html_content = await get_html_content(core=self.core, url=self.url)

        if region_locked_pattern.search(html_content):
            logger.warning(f"Video {self.url} is region blocked")
            raise RegionBlocked(f"The Video: {self.url} is not available in your region!")

        variants_url = await asyncio.to_thread(self._extract_variants_url, html_content)
        variants_json_str = await get_html_content(core=self.core, url=variants_url)
        variants = json.loads(variants_json_str)

        try:
            m3u8_base_url = build_master_playlist(variants)
            is_hls = True
            logger.debug(f"Video {self.url} is using HLS stream")

        except ValueError:
            logger.warning("Failed to build HLS playlist for %s; trying MP4 variants from %s", self.url, variants_url, exc_info=True)
            m3u8_base_url = pick_best_mp4(variants)
            is_hls = False
            logger.debug(f"Video {self.url} is using raw MP4 stream")

        data: dict = await asyncio.to_thread(self._extract_data, html_content)
        data["m3u8_base_url"] = m3u8_base_url
        data["is_hls"] = is_hls
        logger.debug(f"Finished extracting attributes for Video: {data['title']}")
        return data


    @staticmethod
    def _extract_variants_url(html_content: str) -> str:
        """Runs in a background thread to prevent regex from blocking the async loop."""
        media_definitions = re.search(r'mediaDefinition:\s*(.*?)\s*poster:', html_content,
                                      re.DOTALL | re.IGNORECASE).group(1)
        url = re.search(r'videoUrl":"(.*?)"', media_definitions).group(1).replace('\\', '')
        return url

    @staticmethod
    def _extract_data(html_content: str) -> dict:
        parser = LexborHTMLParser(html_content)
        title = parser.css_first("h1.videoTitle.tm_videoTitle").text(strip=True)
        length = re.search(r'"duration":"(.*?)"', html_content).group(1).replace("PT", "").replace("S", "").strip()
        rating = parser.css_first("span.tm_rating_percent").text(strip=True)
        views = parser.css_first("span.infoValue.tm_infoValue").text(strip=True)

        publish_date = parser.css_first("span.publishedDate").text(strip=True)
        author_link = f'https://youporn.com{parser.css_first("div.submitByLink > a").attributes.get("href")}'

        thumbnail = re.search(r"poster: '(.*?)'", html_content).group(1)
        categories_ = parser.css("a.button.bubble-button.categories-tags.tm_carousel_tag.js-pop")
        categories = []

        for category in categories_:
            categories.append(category.text(strip=True))

        pornstars_ = parser.css("a.metaDataPornstarLink.tm_pornstar_link")
        urls = []

        for pornstar_object in pornstars_:
            url = pornstar_object.attributes.get("href")
            urls.append(url)

        return {
            "title": title,
            "length": length,
            "rating": rating,
            "views": views,
            "publish_date": publish_date,
            "author_link": author_link,
            "thumbnail": thumbnail,
            "categories": categories,
            "pornstars_urls": urls
        }


    @property
    async def pornstars(self, html: bool = True) -> AsyncGenerator[Pornstar, None]:
        pornstars_urls = await self.get_field("pornstars_urls")
        logger.info(f"Getting pornstars for Video: {self.title}")
        for url in pornstars_urls:
            star = Pornstar(url=f"https://www.youporn.com{url}", core=self.core)
            if html:
                await star.load_sources("html")
            yield star

    async def download(self, configuration: DownloadConfigHLS, backup_configuration: DownloadConfigRAW | None = None
                       ) -> bool | DownloadReport:
        """
        :param configuration:
        :param backup_configuration:
        :return:
        """
        try:
            await self.load_fields("title", "m3u8_base_url", "is_hls")
            config = copy.deepcopy(configuration)
            config_backup = copy.deepcopy(backup_configuration)
            logger.info(f"Starting download for video: {self.title or self.url}")
            if not config.no_title:
                config.path = os.path.join(config.path, f"{self.title}.mp4")

                if config_backup:
                    config_backup.path = os.path.join(config_backup.path, f"{self.title}.mp4")

            config.m3u8_base_url = self.m3u8_base_url

            if not self.is_hls:
                assert isinstance(config_backup, DownloadConfigRAW), """
                The video you choose to download does not have an HLS stream. I tried falling back to raw video
                downloading over direct download links, but you did not provide a configuration for this case.

                Please supply a DownloadConfigRAW for the 'back_configuration' argument in this download function.
                Thanks :)
                """
                logger.info(f"Falling back to legacy download for video: {self.title or self.url}")
                return await self.core.legacy_download(configuration=config_backup, url=self.m3u8_base_url)

            return await self.core.download(configuration=config)
        except DownloadCancelled:
            raise
        except Exception as e:
            logger.exception("Download failed for %s: %s", self.url, e)
            raise DownloadFailed(f"Download failed for {self.url}: {e}") from e

    async def author(self, load_html: bool = True) -> Pornstar | Channel:
        link = await self.get_field("author_link")
        if not isinstance(link, str):
            raise ValueError(f"No author link found for {self.url}")
        logger.info(f"Fetching author for video {self.title or self.url}: {link}")
        if "channel" in link:
            channel = Channel(url=link, core=self.core)
            if load_html:
                await channel.load_sources("html")
            return channel

        else:
            pornstar = Pornstar(url=link, core=self.core)
            if load_html:
                await pornstar.load_sources("html")
            return pornstar


class Client:
    def __init__(self, core: BaseCore | None = None):
        if core is None:
            core = BaseCore()
        self.core = core
        self.core.initialize_session()
        assert isinstance(self.core.session, AsyncSession)
        self.core.session.headers.update(headers)

    async def get_video(self, url: str, load_html: bool = True) -> Video:
        video = Video(url=url, core=self.core)
        if load_html:
            await video.load_sources("html")
        return video

    async def get_pornstar(self, url: str, load_html: bool = True) -> Pornstar:
        pornstar = Pornstar(url=url, core=self.core)
        if load_html:
            await pornstar.load_sources("html")
        return pornstar

    async def get_channel(self, url: str, load_html: bool = True) -> Channel:
        channel = Channel(url=url, core=self.core)
        if load_html:
            await channel.load_sources("html")
        return channel

    async def get_collection(self, url: str, load_html: bool = True) -> Collection:
        collection = Collection(url=url, core=self.core)
        if load_html:
            await collection.load_sources("html")
        return collection

    def search_videos(self, query: str, pages: int = 0,
                      filter_relevance: Literal[
                          "views", "rating", "date", "duration"
                      ] | None = None,
                      filter_duration_minimum: Literal[
                          "10", "20", "30", "40", "50", "60"
                      ] | None = None,
                      filter_duration_maximum: Literal[
                          "10", "20", "30", "40", "50", "60"
                      ] | None = None,
                      filter_resolution: Literal[
                          "VR", "HD"
                      ] | None = None,
                      iterator_config: IteratorConfig | None = None,
                      ) -> AsyncGenerator[ScrapeResult[Video], None]:
        # Define basic filters
        query = query.replace(" ", "+")
        res = ""
        min_minutes = ""
        max_minutes = ""

        query = f"query={query}&"

        filter = "/search/?"

        if filter_relevance:
            filter = f"/search/{filter_relevance}/?"

        if filter_resolution:
            res = f"res={filter_resolution}&"

        if filter_duration_minimum:
            min_minutes = f"min_minutes={filter_duration_minimum}&"

        if filter_duration_maximum:
            max_minutes = f"max_minutes={filter_duration_maximum}&"

        page_urls = [
            f"https://www.youporn.com{filter}{query}{res}{min_minutes}{max_minutes}page={page}"
            for page in range(1, pages + 1)
        ]

        if iterator_config is None:
            iterator_config = make_iterator_config()

        return scrape_stream(
            core=self.core,
            constructor=Video,
            target_page_urls=page_urls,
            item_extractor=extractor_html,
            iterator_config=iterator_config,
        )


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="YouPorn API Command Line Interface")
    parser.add_argument("--download", metavar="URL", type=str, help="URL to download from")
    parser.add_argument("--quality", metavar="best|half|worst", type=str, default="best", help="The video quality (best, half, worst)")
    parser.add_argument("--file", metavar="FILE", type=str, help="(Optional) Specify a file with URLs (separated with new lines)")
    parser.add_argument("--output", metavar="DIR", type=str, required=True, help="The output path (with filename or directory)")
    parser.add_argument("--no-title", metavar="True,False", type=str, nargs="?", const="True", default="False",
                        help="Whether to apply video title automatically to output path or not")
    return parser


async def run_main(args_list: list[str] | None = None):
    parser = create_parser()
    args = parser.parse_args(args_list)
    no_title = str_to_bool(args.no_title) if isinstance(args.no_title, str) else bool(args.no_title)
    config = DownloadConfigHLS(quality=args.quality, path=args.output, no_title=no_title)
    raw_config = DownloadConfigRAW(quality=args.quality, path=args.output, no_title=no_title)

    urls: list[str] = []
    if args.download:
        urls.append(args.download)
    if args.file:
        with open(args.file, "r") as f:
            urls.extend([line.strip() for line in f if line.strip()])

    if not urls:
        parser.print_help()
        return

    client = Client()
    for url in urls:
        print(f"Fetching video information for: {url}")
        try:
            video = await client.get_video(url, load_html=True)
            title = getattr(video, "title", None) or url
            print(f"Starting download for: {title}")
            await video.download(configuration=config, backup_configuration=raw_config)
            print(f"Download complete: {title}")
        except Exception as e:
            logger.exception("CLI failed while processing %s", url)
            print(f"Error downloading {url}: {e}")


def main():
    configure_app_logging(level=logging.INFO)
    try:
        asyncio.run(run_main())
    except KeyboardInterrupt:
        print("\nOperation cancelled by user.")


if __name__ == "__main__":
    main()
