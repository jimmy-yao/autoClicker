"""模板匹配（找图）。

为什么不用 OpenCV
-----------------
``cv2.matchTemplate`` 是标准答案，但会往 exe 里塞进 40MB+ 的依赖，而这个项目一直
守着「只有 PySide6」的线。这里用一套零依赖的做法，核心是**借用 ``bytes.find()``**：

1. 从模板里挑一条**像素签名**（详见 ``pick_probes``）；
2. 在整帧裸字节上 ``find(签名)`` —— 这是 C 层的 memmem，十几 MB 只需几毫秒；
3. 对每个候选位置再抽 8x8 个采样点做**容差比对**，排除「像素对上了但整体形状不像」的假命中。

为什么能快
----------
签名很长，误命中率很低，绝大多数字节位置在第一步就被 C 代码否决掉了，
真正进入 Python 循环的只有极少数候选点。

两个探针，先锐利后退让
----------------------
单个「纯色横线」探针在**大面积同色背景**上会疯狂命中（整块桌布同一个颜色时，
每一行都能对上），几千个候选全是废的。所以默认先用一个更长的签名：
**内容最丰富的那一行整行像素**——它横跨文字和边框，字节多得多，几乎不会误命中。
只有当它一个候选都拿不到（那一行里有一个像素没渲染成一样）时，
才退回纯色探针：像素最稳，但选择性差。

限制
----
- 签名是**逐字节**比对的，不做模糊搜索：目标程序换主题 / 换语言 / 改 DPI 缩放后
  必须重新截模板。「容差」只作用于最后那步抽样比对，用来容忍文字抗锯齿一类的
  细微差异，救不回整体变色。这是刻意的取舍，换来了零依赖 + 每帧几毫秒。
- 只做**原尺寸**匹配，不做多尺度缩放：同一台机器、同一份界面没有缩放的余地。
"""

from __future__ import annotations

import base64
import binascii
import logging
import struct
import zlib
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - 只为类型提示，避免把 Win32 拖进来
    from ..win.screen import ScreenFrame

logger = logging.getLogger(__name__)

# 模板序列化格式：magic + 宽高 + zlib 压缩的 BGRA
MAGIC = b"ACT1"
BYTES_PER_PIXEL = 4

# 单通道色差容差。32 大约能容忍抗锯齿的文字边缘和轻微的渲染差异
DEFAULT_TOLERANCE = 32

# 抽样比对需要达到的通过比例。留 15% 的余量给文字/边缘的渲染差异
DEFAULT_MIN_SIMILARITY = 0.85

# 「纯色探针」至少要这么长才够特征（太短的纯色片段到处都是）
MIN_PROBE_PX = 6
# 「纯色探针」也不宜过长：越长越难要求「逐字节完全一致」
MAX_PROBE_PX = 48
# 「整行探针」的像素上限。越长越准，但一个像素渲染不同就整条废弃
MAX_ROW_PROBE_PX = 120

# 抽样网格：8x8 = 64 个点
SAMPLE_GRID = 8

# 每条探针最多验算多少个候选点。
# 这是备选位置的上限，不是搜索范围的上限；有界扫描（见 _scan）已经把
# 越界位置剔掉了，所以正常屏面上根本摸不到这个数
MAX_CANDIDATES = 5000


@dataclass(frozen=True)
class Template:
    """一小块屏幕像素。``data`` 是自顶向下、``stride = width * 4`` 的 BGRA。"""

    width: int
    height: int
    data: bytes

    def __post_init__(self) -> None:
        if self.width < 1 or self.height < 1:
            raise ValueError("模板尺寸必须大于 0")
        expected = self.width * self.height * BYTES_PER_PIXEL
        if len(self.data) != expected:
            raise ValueError(f"模板像素数量不对（{len(self.data)} != {expected}）")

    @property
    def stride(self) -> int:
        return self.width * BYTES_PER_PIXEL

    @property
    def summary(self) -> str:
        return f"{self.width} × {self.height}"

    def pixel(self, x: int, y: int) -> tuple[int, int, int]:
        """返回 ``(B, G, R)``。"""
        offset = y * self.stride + x * BYTES_PER_PIXEL
        return self.data[offset], self.data[offset + 1], self.data[offset + 2]

    # ------------------------------------------------------------ 序列化
    def encode(self) -> str:
        """压成可放进 JSON 的字符串。

        用 zlib 而不是 PNG：不引入任何编码器依赖，纯色块压缩比也不差，
        而且这一步完全不碰 Qt，核心层因此保持干净。
        """
        if self.width > 0xFFFF or self.height > 0xFFFF:  # pragma: no cover - 现实里到不了
            raise ValueError("模板尺寸超出存储格式上限")
        payload = (
            MAGIC
            + struct.pack("<HH", self.width, self.height)
            + zlib.compress(self.data, 9)
        )
        return base64.b64encode(payload).decode("ascii")

    @classmethod
    def decode(cls, text: str) -> "Template":
        """从 ``encode()`` 的结果还原。数据坏掉时抛 ``ValueError``。"""
        if not text:
            raise ValueError("模板为空")
        try:
            raw = base64.b64decode(text, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("模板数据不是合法的 base64") from exc

        if len(raw) < 8 or raw[:4] != MAGIC:
            raise ValueError("模板数据格式不认识（可能是旧版本或已损坏）")

        width, height = struct.unpack_from("<HH", raw, 4)
        if width < 1 or height < 1:
            raise ValueError("模板尺寸无效")

        try:
            data = zlib.decompress(raw[8:])
        except zlib.error as exc:
            raise ValueError("模板数据已损坏") from exc

        return cls(width=width, height=height, data=data)

    @classmethod
    def from_frame(cls, frame: "ScreenFrame") -> "Template":
        """把一整帧（通常是刚裁好的那一小块）当成模板。"""
        return cls(width=frame.width, height=frame.height, data=bytes(frame.data))


@dataclass(frozen=True)
class Match:
    """一次命中。``(x, y)`` 相对帧左上角。"""

    x: int
    y: int
    similarity: float


@dataclass(frozen=True)
class SearchResult:
    """搜索结果。

    ``best`` 是**最高分候选**，即使没达到阈值也保留——超时时把它打日志，
    用户就能看到「最接近的是 0.62」而不是一句干巴巴的「没找到」。

    ``candidates`` 是扫描时**签名命中的位置数**（多条探针累加），不是完整比对次数；
    它主要用来判断「屏幕上连颜色接近的地方都没有」还是「有候选但形状对不上」。
    """

    match: Match | None
    best: Match | None
    candidates: int

    @property
    def found(self) -> bool:
        return self.match is not None

    @property
    def best_similarity(self) -> float:
        return self.best.similarity if self.best else 0.0


@dataclass(frozen=True)
class Probe:
    """探针：模板里一段纯色横线，用它在帧里做字节级初筛。"""

    row: int
    offset: int
    signature: bytes

    @property
    def length(self) -> int:
        return len(self.signature) // BYTES_PER_PIXEL


def solid_run_length(template: Template) -> int:
    """模板里最长的一段纯色横线的像素数。

    这是「这个模板稳不稳」的指标：纯色段越长、越厚，界面轻微变化时越不容易失配。
    小到 ``MIN_PROBE_PX`` 以下时纯色签名就没法用了，只能靠整行签名硬碰。
    UI 用它给用户提示。
    """
    return _longest_run(template)[2]


def _longest_run(template: Template) -> tuple[int, int, int]:
    """返回 ``(行号, 起始列, 长度)``：模板中最长的一段同色横线。"""
    best_row = 0
    best_start = 0
    best_length = 0

    for y in range(template.height):
        base = y * template.stride
        run_start = 0
        run_pixel = template.data[base : base + BYTES_PER_PIXEL]
        for x in range(1, template.width + 1):
            if x == template.width:
                pixel = None
            else:
                offset = base + x * BYTES_PER_PIXEL
                pixel = template.data[offset : offset + BYTES_PER_PIXEL]
            if pixel != run_pixel:
                length = x - run_start
                if length > best_length:
                    best_length = length
                    best_row = y
                    best_start = run_start
                if pixel is not None:
                    run_start = x
                    run_pixel = pixel

    return best_row, best_start, best_length


def pick_probes(template: Template) -> list[Probe]:
    """挑出模板的签名，按「先锐利、后退让」排序。

    1. **内容最丰富的那一行**（横跨文字/边框）：签名里带着颜色变化，
       所以几乎不会在别处误命中。代价是要求这整整一行逐像素一致，
       界面稍微变一点就会整条落空。
    2. **最长的一段纯色横线**：纯色几乎不可能渲染出差异，所以最稳；
       但也因为太「普通」，在大面积同色背景上会产生一大堆候选。
       它同时是 1 号探针整条落空时的退路。

    模板本来就是纯色（没有文字也没有边框）时两者会重合，此时只返回一条。
    连一段够长的纯色都找不出来时（照片类模板），只返回 1 号签名。
    """
    probes = [_busy_probe(template)]
    solid = _solid_probe(template)
    if solid is None:
        logger.debug("模板里没有够长的纯色段，只用「整行签名」这一个探针")
    elif not any(solid.row == probe.row and solid.offset == probe.offset for probe in probes):
        probes.append(solid)
    return probes


def _busy_probe(template: Template) -> Probe:
    """内容最丰富的那一行的整行像素。"""
    row = _busiest_row(template)
    length = min(template.width, MAX_ROW_PROBE_PX)
    start = (template.width - length) // 2
    base = row * template.stride
    return Probe(
        row=row,
        offset=start,
        signature=template.data[base + start * 4 : base + (start + length) * 4],
    )


def _solid_probe(template: Template) -> Probe | None:
    """最长的一段纯色横线的中间部分。纯色段太短就返回 ``None``。

    太短的纯色片段（比如 1 个像素）几乎在整帧的每个位置都能命中，
    拿它当签名只会把候选配额烧光，不如不用。
    """
    row, start, run = _longest_run(template)
    if run < MIN_PROBE_PX:
        return None

    length = min(run, MAX_PROBE_PX)
    # 取中间一段：避开与文字/边框接壤的两端
    start += (run - length) // 2
    base = row * template.stride
    return Probe(
        row=row,
        offset=start,
        signature=template.data[base + start * 4 : base + (start + length) * 4],
    )


def _busiest_row(template: Template) -> int:
    """返回「相邻像素差异之和」最大的一行，即内容最丰富的一行。"""
    best_row = 0
    best_change = -1
    for y in range(template.height):
        base = y * template.stride
        change = 0
        previous = template.data[base : base + 3]
        for x in range(1, template.width):
            offset = base + x * 4
            current = template.data[offset : offset + 3]
            change += (
                abs(current[0] - previous[0])
                + abs(current[1] - previous[1])
                + abs(current[2] - previous[2])
            )
            previous = current
        if change > best_change:
            best_change = change
            best_row = y
    return best_row


def sample_points(template: Template, grid: int = SAMPLE_GRID) -> list[tuple[int, int]]:
    """模板内均匀分布的抽样点（含四角与中心）。"""
    cols = max(1, min(grid, template.width))
    rows = max(1, min(grid, template.height))
    xs = [_spread(index, cols, template.width) for index in range(cols)]
    ys = [_spread(index, rows, template.height) for index in range(rows)]
    return [(x, y) for y in ys for x in xs]


def _spread(index: int, count: int, size: int) -> int:
    if count <= 1:
        return size // 2
    return round(index * (size - 1) / (count - 1))


def similarity(
    frame: "ScreenFrame",
    template: Template,
    x: int,
    y: int,
    tolerance: int,
    samples: list[tuple[int, int]],
) -> float:
    """模板放在帧内 ``(x, y)`` 处时，抽样点落在容差内的比例。

    只比 BGR 三个通道：屏幕像素的 alpha 通道没有意义。
    """
    frame_data = frame.data
    frame_stride = frame.stride
    template_data = template.data
    template_stride = template.stride

    passed = 0
    for sx, sy in samples:
        toffset = sy * template_stride + sx * 4
        foffset = (y + sy) * frame_stride + (x + sx) * 4
        if (
            abs(template_data[toffset] - frame_data[foffset]) <= tolerance
            and abs(template_data[toffset + 1] - frame_data[foffset + 1]) <= tolerance
            and abs(template_data[toffset + 2] - frame_data[foffset + 2]) <= tolerance
        ):
            passed += 1
    return passed / len(samples)


def find_template(
    frame: "ScreenFrame",
    template: Template,
    *,
    tolerance: int = DEFAULT_TOLERANCE,
    min_similarity: float = DEFAULT_MIN_SIMILARITY,
) -> SearchResult:
    """在帧里找模板。

    多个候选同分时取**最靠上、再最靠左**的那个（``find`` 按偏移递增返回，
    而偏移是行主序，所以「第一个达到最高分的」天然就是这个规则）。

    依次尝试 ``pick_probes`` 给的签名，先命中的先用——所以顺序很关键，
    它也决定了「同分取最左上」这条规则实际落在哪个签名上。
    """
    if template.width > frame.width or template.height > frame.height:
        return SearchResult(match=None, best=None, candidates=0)

    samples = sample_points(template)
    best: Match | None = None
    candidates = 0
    truncated = False

    for probe in pick_probes(template):
        matched, probe_best, scanned, probe_truncated = _scan(
            frame, template, probe, samples, tolerance, min_similarity
        )
        candidates += scanned
        truncated = truncated or probe_truncated

        if probe_best is not None and (best is None or probe_best.similarity > best.similarity):
            best = probe_best
        if matched is not None:
            return SearchResult(match=matched, best=matched, candidates=candidates)

    if truncated:
        logger.warning(
            "候选位置超过 %s 个，已提前结束搜索（当前最高相似度 %.2f）",
            MAX_CANDIDATES,
            best.similarity if best else 0.0,
        )

    return SearchResult(match=None, best=best, candidates=candidates)


def _scan(
    frame: "ScreenFrame",
    template: Template,
    probe: Probe,
    samples: list[tuple[int, int]],
    tolerance: int,
    min_similarity: float,
) -> tuple[Match | None, Match | None, int, bool]:
    """用一条签名扫一遍帧。返回 ``(达标匹配, 最高分候选, 命中数, 是否被截断)``。"""
    data = frame.data
    stride = frame.stride
    max_x = frame.width - template.width
    max_y = frame.height - template.height
    signature = probe.signature
    span = len(signature)

    # 只有落在「模板左上角合法范围」里的签名起点才可能是真命中。
    # 把搜索区间裁到这一带很重要：整块同色的桌面上，签名在每一行都命中，
    # 不裁的话前几千个候选全落在这个范围之外，白白把配额烧光。
    begin = probe.row * stride
    end = min(len(data), (max_y + probe.row + 1) * stride)

    best: Match | None = None
    hits = 0
    truncated = False

    position = data.find(signature, begin, end)
    while position >= 0:
        hits += 1
        if hits > MAX_CANDIDATES:
            truncated = True
            break

        # 签名必须整体落在同一行内、且按 4 字节像素对齐，否则只是跨行的巧合
        row, offset = divmod(position, stride)
        if offset % 4 == 0 and offset + span <= stride:
            x = offset // 4 - probe.offset
            y = row - probe.row
            if 0 <= x <= max_x and 0 <= y <= max_y:
                score = similarity(frame, template, x, y, tolerance, samples)
                if best is None or score > best.similarity:
                    best = Match(x=x, y=y, similarity=score)
                    if score >= 1.0:
                        # 满分不可能被超越，而且 find 按行主序返回，
                        # 所以这里停下同时也就固定了「同分取最左上」的规则
                        break

        position = data.find(signature, position + 1, end)

    matched = best if best is not None and best.similarity >= min_similarity else None
    return matched, best, hits, truncated
