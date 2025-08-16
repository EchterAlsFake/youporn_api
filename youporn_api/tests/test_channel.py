from ..youporn_api import Client, Channel

client = Client()
channel = client.get_channel("https://www.youporn.com/channel/mia-khalifa/")

def test_everything():
    assert isinstance(channel.name, str)
    assert isinstance(channel.description, str)
    assert isinstance(channel.channel_rank, str)
    assert isinstance(channel.channel_subscribers_count, str)
    assert isinstance(channel.channel_view_count, str)
    assert isinstance(channel.total_videos_count, str)

    for idx, video in enumerate(channel.videos()):
        if idx >= 1:
            break

        assert video.download(quality="worst", downloader="threaded") is True