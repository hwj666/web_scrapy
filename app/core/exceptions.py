"""流水线专用异常。"""


class StopJob(Exception):
    """任务停止信号，在流水线中抛出以快速中止。"""


class JobAborted(Exception):
    """熔断中止：链路整体失效时提前终止，不再白跑剩下的视频。"""


class NewDomainError(Exception):
    """页面提示站点已换新域名：由上层切换 base 后重试当前请求。"""

    def __init__(self, host: str):
        self.host = host
        super().__init__(f"站点已切换到新域名: {host}")


class DomainChanged(Exception):
    """任务运行期域名已切换：正在下载的视频需要丢弃旧 m3u8 重新解析。"""
