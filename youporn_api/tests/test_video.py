from ..youporn_api import Client, Pornstar
from typing import Generator
client = Client()
video = client.get_video("https://www.youporn.com/watch/15852222/instruction-a-la-branlette-joi-fr-by-kalyssy/")


def test_everything():
    assert isinstance(video.title, str)
    assert isinstance(video.m3u8_base_url, str)
    assert isinstance(video.rating, str)
    assert isinstance(video.pornstars, Generator)
    assert isinstance(video.thumbnail, str)
    assert isinstance(video.categories, list)
    assert isinstance(video.views, str)
    assert isinstance(video.publish_date, str)
    assert isinstance(video.author, Pornstar)
    assert isinstance(video.length, str)
    assert video.download(quality="worst", downloader="threaded") is True
