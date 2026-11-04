"""站点注册表：新增站点时在这里加一行，并写一个 sites/xxx.py。"""

from __future__ import annotations

from .baidupan import BaiduPan
from .bilibili import Bilibili
from .cloud189 import Cloud189
from .fnnas import FnNasClub
from .kafan import Kafan
from .linuxdo import LinuxDo
from .nodeseek import NodeSeek
from .pan946 import Pan946
from .pojie import Pojie
from .right import EnShan
from .smzdm import Smzdm
from .tieba import Tieba
from .v2ex import V2ex
from .wnflb import WnflbForum

SITES = [
    BaiduPan,
    Bilibili,
    FnNasClub,
    Smzdm,
    Tieba,
    V2ex,
    NodeSeek,
    Cloud189,
    LinuxDo,
    WnflbForum,
    Pojie,
    Kafan,
    EnShan,
    Pan946,
]

BY_KEY = {site.key.lower(): site for site in SITES}
