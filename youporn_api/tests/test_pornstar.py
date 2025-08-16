from ..youporn_api import Client, Pornstar

client = Client()
pornstar_amateur = client.get_pornstar("https://www.youporn.com/amateur/ph-kalyssy/")
pornstar_real = client.get_pornstar("https://www.youporn.com/pornstar/mia-khalifa/")

def test_amateur_pornstar():
    assert isinstance(pornstar_amateur.name, str)
    for idx, video in enumerate(pornstar_amateur.videos()):
        if idx >= 1:
            break

        assert video.download(quality="worst", downloader="threaded") is True

def test_real_pornstar():
    assert isinstance(pornstar_real.name, str)
    assert isinstance(pornstar_real.pornstar_profile_info, dict)

    for idx, video in enumerate(pornstar_amateur.videos()):
        if idx >= 1:
            break

        assert video.download(quality="worst", downloader="threaded") is True