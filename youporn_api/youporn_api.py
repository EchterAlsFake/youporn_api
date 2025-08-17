import os
import json
import logging

from httpx import Response
from bs4 import BeautifulSoup
from functools import cached_property
from base_api.base import BaseCore, setup_logger
from base_api.modules.config import RuntimeConfig
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Generator, Optional, Literal, Dict, Tuple


try:
    from .modules.consts import *
    from .modules.errors import *

except (ModuleNotFoundError, ImportError):
    from modules.consts import *
    from modules.errors import *


def parse_bitrate_from_url(url: str) -> int | None:
    """
    Try to pull something like 4000K or 2000k out of the URL and convert to bps.
    """
    m = re.search(r'(\d+)\s*[kK](?![a-zA-Z])', url)
    if m:
        return int(m.group(1)) * 1000
    return None


def variant_key(item: Dict) -> Tuple[int, str]:
    """
    Sort key: default first (False>True trick with tuple), then by quality descending.
    We invert defaultQuality so True sorts before False.
    """
    return (0 if item.get("defaultQuality") else 1, -(int(item.get("quality", 0))))


def build_master_playlist(variants: List[Dict]) -> str:
    """
    Build an HLS master playlist string from a list of dicts like the one you provided.
    Each item should have: defaultQuality (bool), format (e.g., 'hls'), videoUrl, quality (e.g., '720').
    """
    # Keep only HLS entries with a URL
    items = [v for v in variants if v.get("format") == "hls" and v.get("videoUrl")]
    if not items:
        raise ValueError("No HLS variants found")

    # Sort: default first, then highest quality
    items.sort(key=variant_key)

    lines = ["#EXTM3U", "#EXT-X-VERSION:3"]

    for v in items:
        q = int(v.get("quality", 0))
        w, h = RES_BY_QUALITY.get(q, (0, 0))
        bw = parse_bitrate_from_url(v["videoUrl"]) or BPS_FALLBACK.get(q, 1_000_000)

        # You can add CODECS, FRAME-RATE, AUDIO, SUBTITLES if you know them.
        attrs = [
            f"BANDWIDTH={bw}",
            f"AVERAGE-BANDWIDTH={bw}",
        ]
        if w and h:
            attrs.append(f"RESOLUTION={w}x{h}")
        # Add NAME (for players that show labels)
        attrs.append(f'NAME="{q}p"')

        lines.append(f"#EXT-X-STREAM-INF:{','.join(attrs)}")
        lines.append(v["videoUrl"])

    return "\n".join(lines) + "\n"


class ErrorVideo:
    """Drop-in-ish stand-in that raises when accessed."""
    def __init__(self, url: str, err: Exception):
        self.url = url
        self._err = err

    def __getattr__(self, _):
        # Any attribute access surfaces the original error
        raise self._err


class Helper:
    def __init__(self, core: BaseCore):
        super(Helper).__init__()
        self.core = core

    def _get_video(self, url: str):
        return Video(url, core=self.core)

    def _make_video_safe(self, url: str):
        try:
            return Video(url, core=self.core)
        except Exception as e:
            return ErrorVideo(url, e)

    @staticmethod
    def _fetch_videos_from_html(html_content: str):
        video_urls = []
        soup = BeautifulSoup(html_content, "html.parser")
        try:
            main_container = soup.find("div", class_="full-row-thumbs")
            videos_container = main_container.find_all("div", class_="video-box pc js_video-box thumbnail-card js-pop")

        except AttributeError:
            main_container = soup.find("div", class_="three-thumbs-row")
            videos_container = main_container.find_all("div", class_="video-box pc js_video-box thumbnail-card js-pop")

        # Fetch content from HTML, if page = 0, to reduce one network request
        for video_object in videos_container:
            video_urls.append(f'https://youporn.com{video_object.find("a")["href"]}')

        return video_urls

    def iterator(self, pages: int = 0, max_workers=20):
        if pages == 0:
            pages = 100

        # create one pool and reuse it across pages (less overhead)
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            for page in range(pages):
                if page == 1:
                    continue

                if not self.url.endswith("/"):
                    self.url = self.url + "/"

                if page == 0:
                    html_content = self.core.fetch(self.url)

                else:
                    html_content = self.core.fetch(f"{self.url}?page={page}")

                if isinstance(html_content, Response):
                    break  # No more videos available
                video_urls = self._fetch_videos_from_html(html_content)

                futures = [executor.submit(self._make_video_safe, url) for url in video_urls]
                for fut in as_completed(futures):
                    yield fut.result()  # Either Video or ErrorVideo


class Channel(Helper):
    def __init__(self, url: str, core: BaseCore):
        super(Channel, self).__init__(core)
        self.url = url
        self.core = core
        self.html_content = self.core.fetch(url)
        self.soup = BeautifulSoup(self.html_content, "html.parser")
        self.channel_info_box = self.soup.find("div", class_="channel-sideBar")


    @cached_property
    def name(self) -> str:
        return self.soup.find("h1", class_="title-text").text.replace("Subscribe", "").strip()

    @cached_property
    def channel_rank(self) -> str:
        return self.channel_info_box.find("div", class_="channel-rank").find("div", class_="rank").text.strip()

    @cached_property
    def total_videos_count(self) -> str:
        return self.channel_info_box.find("div", class_="channel-info").find("div", class_="info-metrics").text.strip()

    @cached_property
    def channel_view_count(self) -> str:
        return self.channel_info_box.find_all("div", class_="channel-info")[1].find("div", class_="info-metrics").text.strip()

    @cached_property
    def channel_subscribers_count(self) -> str:
        return self.channel_info_box.find_all("div", class_="channel-info")[2].find("div",
                                                                                    class_="info-metrics").text.strip()

    @cached_property
    def description(self) -> str:
        return self.soup.find("div", class_="channel-description").find("p").text.strip()

    def videos(self, pages: int = 0, max_workers=20):
        yield from self.iterator(pages=pages, max_workers=max_workers)


class Collection(Helper):
    def __init__(self, url: str, core: BaseCore):
        super(Collection, self).__init__(core)
        self.url = url
        self.core = core
        self.html_content = self.core.fetch(self.url)
        self.soup = BeautifulSoup(self.html_content, "html.parser")

    @cached_property
    def name(self) -> str:
        return self.soup.find("div", class_="top-section").find("h4").text.replace("Collection:", "").strip()

    @cached_property
    def rating(self) -> str:
        return self.soup.find("div", class_="featureCollectionRating").text.strip()

    @cached_property
    def total_videos_count(self) -> str:
        return self.soup.find("p", class_="collection-videos-count").text.strip()

    @cached_property
    def view_count(self) -> str:
        return self.soup.find("div", class_="top-section").find_all("li")[1].find("p").text.strip()

    @cached_property
    def last_updated(self) -> str:
        return self.soup.find("li", class_="lastUpdated").find("p").text.strip()

    def videos(self, pages: int = 0, max_workers=20):
        yield from self.iterator(pages=pages, max_workers=max_workers)


class Pornstar(Helper):
    def __init__(self, url: str, core: BaseCore):
        super(Pornstar, self).__init__(core)
        self.url = url
        self.core = core
        self.logger = setup_logger(name="YOUPORN API - [Pornstar]", level=logging.ERROR)
        self.html_content = self.core.fetch(self.url)
        self.soup = BeautifulSoup(self.html_content, "html.parser")

    @cached_property
    def name(self) -> str:
        return self.soup.find("h1", class_="name-title").text.strip()

    @cached_property
    def pornstar_profile_info(self) -> dict:
        profile_info = self.soup.find("ul", class_="profile-info")
        li_tags = profile_info.find_all("li", class_="info-stat")
        dictionary = {}

        for tag in li_tags:
            stuff = tag.find_all("p")
            key = stuff[0].text.strip()
            item = stuff[1].text.strip()
            dictionary.update({key: item})

        return dictionary

    def videos(self, pages: int = 0, max_workers=20):
        yield from self.iterator(pages=pages, max_workers=max_workers)


class User:
    def __init__(self, url: str, core: BaseCore):
        self.url = url
        self.core = core
        self.html_content = self.core.fetch(self.url)
        self.soup = BeautifulSoup(self.html_content, "html.parser")

    @cached_property
    def name(self) -> str:
        return self.soup.find("h1", class_="name-title").text.strip()

    @cached_property
    def collections(self) -> Generator[Collection, None, None]:
        container = self.soup.find("ul", class_="playlists_list")
        _collections = container.find_all("li", class_="playlists-container")

        for collection_container in _collections:
            yield Collection(f'https://youporn.com{collection_container.find("a").get("href")}', core=self.core)


class Video:
    def __init__(self, url: str, core: BaseCore):
        self.url = url
        self.core = core
        self.logger = setup_logger(name="YOUPORN API - [Video]", level=logging.ERROR)
        self.html_content = self.core.fetch(self.url)

        if isinstance(self.html_content, Response):
            raise VideoUnavailable(f"The Video: {self.url} is unavailable / not found.")

        if region_locked_pattern.search(self.html_content):
            raise RegionBlocked(f"The Video: {self.url} is not available in your region!")

        self.soup = BeautifulSoup(self.html_content, "html.parser")

    @cached_property
    def title(self) -> str:
        try:
            return self.soup.find("h1", class_="videoTitle tm_videoTitle").text.strip()

        except AttributeError:
            raise f"URL: {self.url} raised an error!"

    @cached_property
    def length(self) -> str:
        return re.search(r'"duration":"(.*?)"', self.html_content).group(1).replace("PT", "").replace("S", "").strip()

    @cached_property
    def rating(self) -> str:
        return self.soup.find("span", class_="tm_rating_percent").text.strip()

    @cached_property
    def views(self) -> str:
        return self.soup.find("span", class_="infoValue tm_infoValue").text.strip()

    @cached_property
    def publish_date(self) -> str:
        return self.soup.find("span", class_="publishedDate").text.strip()

    @cached_property
    def author(self) -> Pornstar | Channel:
        link = f'https://youporn.com{self.soup.find("div", class_="submitByLink").find("a").get("href")}'
        if "channel" in link:
            return Channel(link, core=self.core)

        else:
            return Pornstar(link, core=self.core)

    @cached_property
    def m3u8_base_url(self) -> str:
        media_definitions = re.search(r'mediaDefinition: (.*?) poster:', self.html_content, re.DOTALL | re.IGNORECASE).group(1)
        url = re.search(r'videoUrl":"(.*?)"', media_definitions).group(1).replace('\\', '')

        content = self.core.fetch(url)
        return build_master_playlist(json.loads(content))

    @cached_property
    def thumbnail(self) -> str:
        return re.search(r"poster: '(.*?)'", self.html_content).group(1)

    @cached_property
    def categories(self) -> List[str]:
        categories_ = self.soup.find_all("a", class_="button bubble-button categories-tags tm_carousel_tag js-pop")
        categories = []

        for category in categories_:
            categories.append(category.text)

        return categories

    @cached_property
    def pornstars(self) -> Generator[Pornstar, None, None]:
        pornstars_ = self.soup.find_all("a", class_="button bubble-button tm_carousel_tag")

        for pornstar_object in pornstars_:
            yield Pornstar(f'https://youporn.com{pornstar_object["href"]}', core=self.core)

    def download(self, downloader, quality, path="./", callback=None, no_title=False, remux: bool = False,
                 callback_remux=None) -> bool:
        """
        :param callback:
        :param downloader:
        :param quality:
        :param path:
        :param no_title:
        :param remux:
        :param callback_remux:
        :return:
        """
        if not no_title:
            path = os.path.join(path, f"{self.title}.mp4")

        self.core.download(video=self, quality=quality, path=path, callback=callback, downloader=downloader,
                           remux=remux, callback_remux=callback_remux)
        return True

    def get_segments(self, quality) -> list:
        """
        :param quality: (str, Quality) The video quality
        :return: (list) A list of segments (the .ts files)
        """
        segments = self.core.get_segments(quality=quality, m3u8_url_master=self.m3u8_base_url)
        return segments


class Client(Helper):
    def __init__(self, core: Optional[BaseCore] = None):
        super().__init__(core)
        self.core = core or BaseCore(config=RuntimeConfig())
        self.core.initialize_session(headers)

    def get_video(self, url: str) -> Video:
        return Video(url, core=self.core)

    def get_pornstar(self, url: str) -> Pornstar:
        return Pornstar(url, core=self.core)

    def get_channel(self, url: str) -> Channel:
        return Channel(url, core=self.core)

    def get_collection(self, url: str) -> Collection:
        return Collection(url, core=self.core)

    def search_videos(self, query: str, pages: int = 0, max_workers=20,
                      filter_relevance: Literal[
                          "views", "rating", "date", "duration"
                      ] = None,
                      filter_duration_minimum: Literal[
                          "10", "20", "30", "40", "50", "60"
                      ] = None,
                      filter_duration_maximum: Literal[
                          "10", "20", "30", "40", "50", "60"
                      ] = None,
                      filter_resolution: Literal[
                          "VR", "HD"
                      ] = None,
                      ):
        # Define basic filters
        res = ""
        min_minutes = ""
        max_minutes = ""
        query = f"query={query}&"
        filter = f"/?"

        if filter_relevance:
            filter = f"/{filter_relevance}/?"

        if filter_resolution:
            res = f"res={filter_resolution}&"

        if filter_duration_minimum:
            min_minutes = f"min_minutes={filter_duration_minimum}&"

        if filter_duration_maximum:
            max_minutes = f"max_minutes={filter_duration_maximum}&"

        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            if pages == 0:
                pages = 100

            for page in range(pages):
                if page == 1:
                    continue

                url = f"https://youporn.com{filter}{query}{res}{min_minutes}{max_minutes}&page={page}"
                html_content = self.core.fetch(url)

                if isinstance(html_content, Response):
                    break  # No more videos available

                video_urls = self._fetch_videos_from_html(html_content)

                futures = [executor.submit(self._make_video_safe, url) for url in video_urls]
                for fut in as_completed(futures):
                    yield fut.result()  # Either Video or ErrorVideo

# MOMOLAND BAAM 24/7!