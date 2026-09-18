"""프록시 풀.

지원하는 표기 (한 줄에 하나):
    host:port:user:pass
    user:pass@host:port
    http://user:pass@host:port
    host:port
빈 줄과 `#` 주석은 무시한다.

프록시가 여러 개면 요청마다 번갈아 쓴다(라운드로빈). 요청마다 IP 가 바뀌는
회전 프록시를 하나만 넣어도 되고, 고정 IP 프록시를 여러 개 넣어도 된다.
"""

import itertools
import os
import threading
from dataclasses import dataclass, field
from typing import Iterator, List, Optional, Sequence, Union

DEFAULT_SCHEME = "http"


def parse_proxy(raw: str) -> Optional[str]:
    """한 줄을 프록시 URL 로 정규화한다. 빈 줄/주석이면 None."""
    line = (raw or "").strip()
    if not line or line.startswith("#"):
        return None

    if "://" in line:
        return line

    if "@" in line:
        return f"{DEFAULT_SCHEME}://{line}"

    parts = line.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"{DEFAULT_SCHEME}://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        return f"{DEFAULT_SCHEME}://{line}"
    return None


def load_proxies(path: str) -> List[str]:
    """proxies.txt 를 읽어 프록시 URL 목록으로."""
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            proxy = parse_proxy(line)
            if proxy:
                out.append(proxy)
    return out


@dataclass
class ProxyPool:
    """프록시를 라운드로빈으로 돌려준다. 스레드 안전."""
    proxies: List[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _cycle: Optional[Iterator[str]] = field(default=None, repr=False)

    def __post_init__(self):
        self.proxies = [p for p in (parse_proxy(p) for p in self.proxies) if p]
        self._cycle = itertools.cycle(self.proxies) if self.proxies else None

    def __bool__(self) -> bool:
        return bool(self.proxies)

    def __len__(self) -> int:
        return len(self.proxies)

    def next(self) -> Optional[str]:
        """다음 프록시. 비어 있으면 None(직접 연결)."""
        if not self._cycle:
            return None
        with self._lock:
            return next(self._cycle)

    @classmethod
    def build(cls, source: Union[None, str, Sequence[str], "ProxyPool"]) -> "ProxyPool":
        """문자열 / 파일 경로 / 목록 / ProxyPool 중 아무거나 받아 풀을 만든다."""
        if source is None:
            return cls()
        if isinstance(source, ProxyPool):
            return source
        if isinstance(source, str):
            # 파일 경로면 읽고, 아니면 프록시 하나로 본다
            if os.path.exists(source):
                return cls(load_proxies(source))
            return cls([source])
        return cls(list(source))
