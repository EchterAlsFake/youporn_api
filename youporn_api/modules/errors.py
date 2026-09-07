from base_api.modules.errors import (
    NotFound,
    NetworkError,
    BotDetection,
    ProxyError,
    UnknownNetworkError,
    DownloadFailed,
    VideoUnavailable,
)


class RegionBlocked(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


class DataNotLoadedError(Exception):
    def __init__(self, msg):
        super().__init__(msg)
        self.msg = msg


__all__ = [
    "VideoUnavailable",
    "RegionBlocked",
    "NotFound",
    "NetworkError",
    "BotDetection",
    "ProxyError",
    "UnknownNetworkError",
    "DownloadFailed",
    "DataNotLoadedError",
]