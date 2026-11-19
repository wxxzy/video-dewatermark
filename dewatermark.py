#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
video-dewatermark —— 视频水印批量去除工具

核心能力
  1. 自动定位：在指定角落区域内，用多帧时间统计 + 空间高通，自动找出静止的半透明水印框；
  2. 两种修复引擎：
       delogo  —— 调用 ffmpeg delogo 滤镜，从边框像素插值填充（快，默认）
       inpaint —— OpenCV 掩膜修复（对水印笔画精细取样，画面细节保留更好）
  3. 批量处理：可传多个文件 / 整个目录，支持递归、并发、跳过已存在；
  4. 支持的输入：mp4 / mov / mkv / avi / flv / webm / m4v / ts / wmv / mpg

典型用法
  # 单文件，自动定位右下角水印
  python dewatermark.py input.mp4

  # 批量：整个目录（含子目录），输出到 out/
  python dewatermark.py "D:/素材" -o out --recursive

  # 手动指定水印框（x,y,w,h），适合自动定位不准时
  python dewatermark.py input.mp4 --box 578,1226,126,36

  # 先只看自动定位结果，不处理
  python dewatermark.py "D:/素材" --dry-run --preview
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Tuple

# 管道/重定向时统一用 UTF-8，控制台里保留系统编码（避免中文乱码）
try:
    for _stream in (sys.stdout, sys.stderr):
        if not _stream.isatty():
            _stream.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

try:
    import cv2  # noqa: F401
    import numpy  # noqa: F401
    HAVE_CV = True
except Exception:
    HAVE_CV = False

# 以 pythonw 运行时（图形界面），子进程 ffmpeg 若不屏蔽会弹出黑窗口
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".flv", ".webm", ".m4v", ".ts", ".wmv", ".mpg", ".mpeg", ".3gp"}

# ffmpeg 可复制的音频编码（mp4/mov 容器下安全）
COPYABLE_AUDIO = {"aac", "mp3", "alac", "ac3", "eac3", "mp2"}


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
SHOW_PROGRESS = True


def log(msg: str = "") -> None:
    print(msg, flush=True)


def human_size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}" if unit != "B" else f"{int(n)}B"
        n /= 1024
    return f"{n:.1f}TB"


def human_time(s: float) -> str:
    s = int(max(0, s))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"


FFMPEG_CANDIDATES = [
    "ffmpeg",
    r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
    r"C:\ffmpeg\bin\ffmpeg.exe",
]


def _find_ffmpeg() -> Tuple[str, str]:
    """返回 (ffmpeg, ffprobe) 路径。优先用完整构建（含 ffprobe）。"""
    env = os.environ.get("FFMPEG_PATH")
    cands: List[str] = []
    if env:
        cands.append(env)

    # winget 安装的 Gyan.FFmpeg（完整构建，优先）
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        winget = Path(local) / "Microsoft" / "WinGet" / "Packages"
        if winget.is_dir():
            for p in sorted(winget.glob("Gyan.FFmpeg*/**/bin/ffmpeg.exe"), reverse=True):
                cands.append(str(p))
    cands.extend(FFMPEG_CANDIDATES)
    # imageio-ffmpeg 自带的静态版（无 ffprobe，兜底）
    for p in Path(sys.prefix).glob("Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg*.exe"):
        cands.append(str(p))

    for c in cands:
        exe = shutil.which(c) if not os.path.isabs(c) else (c if os.path.isfile(c) else None)
        if not exe:
            continue
        probe = Path(exe).with_name("ffprobe.exe" if exe.lower().endswith(".exe") else "ffprobe")
        if probe.is_file():
            return exe, str(probe)
        if shutil.which("ffprobe"):
            return exe, shutil.which("ffprobe")
        # 没有 ffprobe 也能跑（只是拿不到精确时长）
        return exe, ""

    raise SystemExit(
        "未找到 ffmpeg。请安装后重试（winget install Gyan.FFmpeg），"
        "或设置环境变量 FFMPEG_PATH 指向 ffmpeg.exe。"
    )


FFMPEG, FFPROBE = _find_ffmpeg()


@dataclass
class VideoInfo:
    path: str
    width: int
    height: int
    fps: float
    duration: float
    audio_codec: Optional[str] = None
    has_audio: bool = False
    video_codec: str = ""
    bitrate: int = 0


def probe(path: str) -> VideoInfo:
    info = VideoInfo(path=path, width=0, height=0, fps=25.0, duration=0.0)
    if not FFPROBE:
        width, height, fps, n = _probe_by_cv2(path)
        info.width, info.height, info.fps = width, height, fps
        info.duration = n / fps if fps else 0
        return info

    out = subprocess.run(
        [FFPROBE, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", path],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        creationflags=NO_WINDOW,
    ).stdout
    import json

    data = json.loads(out or "{}")
    fmt = data.get("format", {})
    info.duration = float(fmt.get("duration") or 0)
    info.bitrate = int(float(fmt.get("bit_rate") or 0))
    for st in data.get("streams", []):
        if st.get("codec_type") == "video" and not info.width:
            info.width = int(st.get("width") or 0)
            info.height = int(st.get("height") or 0)
            info.video_codec = st.get("codec_name") or ""
            fr = st.get("avg_frame_rate") or st.get("r_frame_rate") or "25/1"
            if not fr or fr in ("0/0",):
                fr = st.get("r_frame_rate") or "25/1"
            try:
                a, b = fr.split("/")
                info.fps = float(a) / float(b) if float(b) else 25.0
            except Exception:
                info.fps = 25.0
        elif st.get("codec_type") == "audio":
            info.has_audio = True
            info.audio_codec = st.get("codec_name")
    if info.duration <= 0 and info.fps > 0:
        info.duration = 0.0
    return info


def _probe_by_cv2(path: str) -> Tuple[int, int, float, int]:
    import cv2

    cap = cv2.VideoCapture(path)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    fps = cap.get(cv2.CAP_PROP_FPS) or 25.0
    n = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    cap.release()
    return w, h, fps, n


# --------------------------------------------------------------------------- #
# 水印自动定位
# --------------------------------------------------------------------------- #
Box = Tuple[int, int, int, int]  # x, y, w, h

CORNER_ROI = {
    # 角落搜索区域占全画面的比例 (w_frac, h_frac)
    "br": (0.50, 0.30),
    "bl": (0.50, 0.30),
    "tr": (0.50, 0.30),
    "tl": (0.50, 0.30),
    "bottom": (1.0, 0.18),
    "top": (1.0, 0.18),
}


def corner_roi(width: int, height: int, corner: str) -> Box:
    fw, fh = CORNER_ROI.get(corner, CORNER_ROI["br"])
    rw, rh = int(width * fw), int(height * fh)
    if corner == "br":
        return width - rw, height - rh, rw, rh
    if corner == "bl":
        return 0, height - rh, rw, rh
    if corner == "tr":
        return width - rw, 0, rw, rh
    if corner == "tl":
        return 0, 0, rw, rh
    if corner == "bottom":
        return 0, height - rh, width, rh
    return 0, 0, width, rh


def _sample_gray(path: str, count: int = 32, max_side: int = 0) -> Tuple[List["object"], int, int]:
    """均匀抽样若干帧灰度图。"""
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(path)
    if not cap.isOpened():
        raise RuntimeError(f"无法打开视频: {path}")
    total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
    w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    if total <= 0:
        idxs = list(range(count))
    else:
        idxs = sorted({int(x) for x in np.linspace(0, max(total - 1, 0), min(count, total))})

    frames: List[object] = []
    for i in idxs:
        cap.set(cv2.CAP_PROP_POS_FRAMES, i)
        ok, f = cap.read()
        if not ok:
            continue
        g = cv2.cvtColor(f, cv2.COLOR_BGR2GRAY)
        if max_side and max(g.shape) > max_side:
            s = max_side / max(g.shape)
            g = cv2.resize(g, (int(g.shape[1] * s), int(g.shape[0] * s)), interpolation=cv2.INTER_AREA)
        frames.append(g)
    cap.release()
    if not frames:
        raise RuntimeError(f"读取不到画面: {path}")
    return frames, frames[0].shape[1], frames[0].shape[0]


@dataclass
class Detection:
    box: Box
    mask: Optional["object"] = None      # 相对全图的水印掩膜（0/255）
    score: float = 0.0
    roi: Box = (0, 0, 0, 0)
    ok: bool = True
    confidence: float = 0.0
    note: str = ""


def _grow_run(has: "object", i0: int, i1: int, gap: int) -> Tuple[int, int]:
    """从 [i0,i1] 区间向两侧扩展，允许中断不超过 gap 个空位。返回新的 [s,e]。"""
    n = len(has)
    s, e = i0, i1
    miss = 0
    j = i1 + 1
    while j < n:
        if has[j]:
            e, miss = j, 0
        else:
            miss += 1
            if miss > gap:
                break
        j += 1
    miss = 0
    j = i0 - 1
    while j >= 0:
        if has[j]:
            s, miss = j, 0
        else:
            miss += 1
            if miss > gap:
                break
        j -= 1
    return s, e


def _cluster_components(seed: Dict[str, Any], comps: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    把与种子「同一行、高度相近、能量相当」的块聚成一行文字（图标 + 各字符）。
    能量下限很关键：它挡住同高度但弱得多的背景噪点被误并进来。
    """
    bh = max(seed["h"], 6)
    cluster = [seed]
    x0, y0 = seed["x"], seed["y"]
    x1, y1 = x0 + seed["w"], y0 + seed["h"]
    cy = (y0 + y1) / 2.0
    e_min = 0.45 * seed["energy"]

    rest = [c for c in comps if c is not seed]
    # 由近及远地并入，避免隔着一段background把远处噪点拉进来
    rest.sort(key=lambda c: abs(c["x"] + c["w"] / 2 - (x0 + x1) / 2))

    for c in rest:
        if c["energy"] < e_min:
            continue
        ch = max(c["h"], 1)
        if not (0.45 * bh <= ch <= 2.2 * bh):
            continue
        if abs((c["y"] + c["h"] / 2) - cy) > 0.7 * bh:
            continue
        gap = max(x0 - (c["x"] + c["w"]), c["x"] - x1, 0)
        if gap > 1.4 * bh:
            continue
        cluster.append(c)
        x0, y0 = min(x0, c["x"]), min(y0, c["y"])
        x1, y1 = max(x1, c["x"] + c["w"]), max(y1, c["y"] + c["h"])
        cy = (y0 + y1) / 2.0
    return cluster


def _col_peak(hp: "object", band0: int, band1: int, a: int, b: int) -> float:
    """区间 [a,b) 内每列最大值的 95 分位，代表这段区域的笔画强度。"""
    import numpy as np

    if b <= a:
        return 0.0
    seg = hp[band0:band1, a:b]
    if seg.size == 0:
        return 0.0
    return float(np.percentile(seg.max(axis=0), 95))


def _extend_horizontally(box: Box, hp: "object", thr: float, seed_energy: float, rw: int, rh: int) -> Box:
    """
    横向补全「对比度弱、但仍是笔画」的字符（典型场景：白字压在白衣服上）。
    扩展后做能量验证：新增区域的笔画强度不足原来一半就回退，避免顺着背景纹理/噪点一路长出去。
    """
    import numpy as np

    x, y, w, h = box
    hh = max(h, 6)
    band0 = max(0, y - int(0.15 * hh))
    band1 = min(rh, y + h + int(0.15 * hh))
    band = hp[band0:band1, :]
    if band.size == 0:
        return box

    colmax = band.max(axis=0)
    ok_col = colmax >= max(thr * 0.8, 0.55 * seed_energy)
    nx0, nx1 = _grow_run(ok_col, x, x + w - 1, gap=max(3, int(0.5 * hh)))

    max_ext = int(1.3 * w)
    nx0 = max(x - max_ext, nx0)
    nx1 = min(x + w - 1 + max_ext, nx1)
    nx0, nx1 = max(0, nx0), min(rw - 1, nx1)

    base = _col_peak(hp, band0, band1, x, x + w)
    if nx0 < x and _col_peak(hp, band0, band1, nx0, x) < 0.5 * base:
        nx0 = x
    if nx1 > x + w - 1 and _col_peak(hp, band0, band1, x + w, nx1 + 1) < 0.5 * base:
        nx1 = x + w - 1

    return nx0, y, nx1 - nx0 + 1, h


def _corner_distance(box: Box, corner: str, rw: int, rh: int) -> Tuple[float, float]:
    """框到「目标角落」两条边的距离（像素）。bottom/top 模式只看竖直方向。"""
    x, y, w, h = box
    dx = 0.0
    dy = 0.0
    if corner in ("br", "tr"):
        dx = rw - (x + w)
    elif corner in ("bl", "tl"):
        dx = x
    if corner in ("br", "bl", "bottom"):
        dy = rh - (y + h)
    elif corner in ("tr", "tl", "top"):
        dy = y
    return max(0.0, dx), max(0.0, dy)


def _box_score(box: Box, hp: "object", energy: float, corner: str, rw: int, rh: int) -> float:
    """
    水印可能性打分（0~1）：
      · 角落先验：水印通常贴着画面某个角，离边越远分越低（洛伦兹衰减）；
      · 对比强度：笔画的平均高频响应，越强越像叠加的文字；
      · 形态：一行文字应该是横向长条，高度占画面 1%~8%。
    """
    x, y, w, h = box
    if w <= 2 or h <= 2:
        return 0.0
    dx, dy = _corner_distance(box, corner, rw, rh)
    px = 1.0 / (1.0 + (dx / (0.10 * rw)) ** 2)
    py = 1.0 / (1.0 + (dy / (0.10 * rh)) ** 2)
    prox = px * py
    if corner in ("bottom", "top"):
        prox = py

    aspect = w / float(h)
    h_rel = h / float(rh)
    # 一行水印文字应当是横向长条；aspect 越小越可能是"人像/背景块"糊进来
    if aspect >= 1.8:
        shape = 1.0
    elif aspect >= 1.2:
        shape = 0.5
    else:
        shape = 0.25
    if not (0.03 <= h_rel <= 0.30):
        shape *= 0.4

    return prox * (0.35 + 0.65 * energy) * shape


def detect_watermark(
    path: str,
    corner: str = "br",
    samples: int = 32,
    min_contrast: float = 8.0,
    verbose: bool = False,
) -> Detection:
    """
    在 corner 指定的区域内定位水印，返回全图坐标下的外接框。

    思路：
      1) 抽 N 帧取时间中值 —— 移动的背景人物会被抹掉，静止叠加的水印留下；
      2) 对中值帧做高通，水印笔画是局部窄边，响应明显强于平滑背景；
      3) 用「多候选」代替「取最强块」：每个强响应块都当一次种子，聚成一行文字、
         再做带能量验证的横向补全，最后按「角落先验 + 对比强度 + 横向长条形态」打分取最优。
         这一步很关键 —— 背景里的亮边、桌沿往往比小号水印更"强"，只看强度必然选错。
    """
    if not HAVE_CV:
        return Detection((0, 0, 0, 0), ok=False, note="未安装 opencv-python，无法自动定位")
    import cv2
    import numpy as np

    frames, sw, sh = _sample_gray(path, count=samples)
    if len(frames) < 2:
        return Detection((0, 0, 0, 0), ok=False, note="可抽样帧太少")
    med = np.median(np.stack(frames).astype(np.float32), axis=0)

    try:
        info = probe(path)
        W, H = (info.width or sw), (info.height or sh)
    except Exception:
        W, H = sw, sh
    sx, sy = W / sw, H / sh

    rx, ry, rw, rh = corner_roi(sw, sh, corner)
    med_roi = med[ry:ry + rh, rx:rx + rw]

    sigma = max(2.5, min(6.0, sh / 320.0))
    hp = np.abs(med_roi - cv2.GaussianBlur(med_roi, (0, 0), sigma))
    if float(hp.max()) < min_contrast:
        return Detection((0, 0, 0, 0), ok=False, note="该区域没有明显的高对比结构")

    thr = max(min_contrast, float(np.percentile(hp, 97.0)))
    binary = (hp >= thr).astype(np.uint8)
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8))
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))

    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary, connectivity=8)
    if n <= 1:
        return Detection((0, 0, 0, 0), ok=False, note="未检测到文字笔画结构")

    roi_area = float(rw * rh)
    comps: List[Dict[str, Any]] = []
    for i in range(1, n):
        x, y, w, h, area = (int(v) for v in stats[i])
        if area < 5 or area > 0.30 * roi_area:
            continue
        if w > 0.95 * rw or h > 0.6 * rh:
            continue
        # 贯穿整个 ROI 的长直边（桌沿、墙角、画面分界线）不是文字
        if w > 0.6 * rw and h < 0.03 * rh:
            continue
        if h > 0.6 * rh and w < 0.03 * rw:
            continue
        sub = labels[y:y + h, x:x + w] == i
        if not sub.any():
            continue
        energy = float(hp[y:y + h, x:x + w][sub].mean())
        comps.append({"id": i, "x": x, "y": y, "w": w, "h": h, "area": area,
                      "energy": energy, "fill": area / float(w * h)})
    if not comps:
        return Detection((0, 0, 0, 0), ok=False, note="未找到合理的水印笔画块")

    thr_norm = max(thr * 2.5, 1e-6)
    best = None  # (score, box, cluster, energy)
    seeds = sorted(comps, key=lambda c: -c["area"] * c["energy"])[:14]
    for seed in seeds:
        cluster = _cluster_components(seed, comps)
        bx0 = min(c["x"] for c in cluster)
        by0 = min(c["y"] for c in cluster)
        bx1 = max(c["x"] + c["w"] for c in cluster)
        by1 = max(c["y"] + c["h"] for c in cluster)
        box = (bx0, by0, bx1 - bx0, by1 - by0)
        box = _extend_horizontally(box, hp, thr, seed["energy"], rw, rh)

        x, y, w, h = box
        sub = binary[y:y + h, x:x + w] > 0
        energy = float(hp[y:y + h, x:x + w][sub].mean()) if sub.any() else 0.0
        e_norm = min(1.0, energy / thr_norm)
        sc = _box_score(box, hp, e_norm, corner, rw, rh)
        if best is None or sc > best[0]:
            best = (sc, box, cluster, energy)
        if verbose:
            log(f"    候选 score={sc:.3f} box={box} E={energy:.1f}")

    score, box, cluster, energy = best
    x, y, w, h = box

    # 留一点内边距，覆盖半透明羽化边缘（漏掉 1~2px 会留下淡淡的水印轮廓）
    pad_x = max(2, int(round(0.03 * w)))
    pad_y = max(2, int(round(0.14 * h)))
    x = max(0, x - pad_x)
    y = max(0, y - pad_y)
    w = min(rw - x, w + pad_x * 2)
    h = min(rh - y, h + pad_y * 2)

    # 掩膜（ROI 内），供预览 / inpaint 使用
    m_roi = np.zeros((rh, rw), np.uint8)
    keep_ids = [c["id"] for c in cluster]
    if keep_ids:
        m_roi[np.isin(labels, keep_ids)] = 255
    inner = (hp[y:y + h, x:x + w] >= max(thr * 0.7, 0.4 * energy))
    m_roi[y:y + h, x:x + w] = np.maximum(m_roi[y:y + h, x:x + w], (inner * 255).astype(np.uint8))
    m_roi = cv2.dilate(m_roi, np.ones((3, 3), np.uint8), iterations=1)

    full = np.zeros((sh, sw), np.uint8)
    full[ry:ry + rh, rx:rx + rw] = m_roi
    mask = full if (sx == 1.0 and sy == 1.0) else cv2.resize(full, (W, H), interpolation=cv2.INTER_NEAREST)

    fbox = (
        int(round((rx + x) * sx)),
        int(round((ry + y) * sy)),
        int(round(w * sx)),
        int(round(h * sy)),
    )

    # 置信度：0.22 以上算可信；低于此值仍返回框，但标记为低置信，让界面提示人工确认
    confidence = min(1.0, score / 0.45)
    ok = score >= 0.22
    note = "" if ok else f"定位置信度偏低（{confidence:.0%}），建议人工确认"
    if verbose:
        log(f"    选定 box={fbox} score={score:.3f} 置信度={confidence:.0%} 候选块={len(cluster)}")

    bwd, bhd = fbox[2], fbox[3]
    if bwd < max(6, W * 0.01) or bhd < 4 or bwd > W * 0.8 or bhd > H * 0.45:
        return Detection(fbox, mask=mask, score=score, confidence=0.0,
                         ok=False, note=f"定位结果尺寸可疑 ({bwd}x{bhd})", roi=(rx, ry, rw, rh))

    return Detection(fbox, mask=mask, score=score, confidence=confidence,
                     ok=ok, note=note, roi=(rx, ry, rw, rh))


# --------------------------------------------------------------------------- #
# 处理引擎
# --------------------------------------------------------------------------- #
@dataclass
class Task:
    src: str
    dst: str
    info: VideoInfo
    boxes: List[Box] = field(default_factory=list)
    note: str = ""


def clamp_box(box: Box, width: int, height: int) -> Box:
    x, y, w, h = box
    x = max(1, min(x, width - 3))
    y = max(1, min(y, height - 3))
    w = max(1, min(w, width - 1 - x))
    h = max(1, min(h, height - 1 - y))
    return x, y, w, h


def build_delogo(boxes: Sequence[Box], width: int, height: int) -> str:
    parts = []
    for b in boxes:
        x, y, w, h = clamp_box(b, width, height)
        parts.append(f"delogo=x={x}:y={y}:w={w}:h={h}")
    return ",".join(parts)


def _encoder_args(crf: int, preset: str, extra: Optional[List[str]] = None) -> List[str]:
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset, "-pix_fmt", "yuv420p"] + (extra or [])


def _audio_args(info: VideoInfo, keep_audio: bool) -> List[str]:
    if not keep_audio or not info.has_audio:
        return ["-an"]
    if (info.audio_codec or "") in COPYABLE_AUDIO:
        return ["-c:a", "copy"]
    return ["-c:a", "aac", "-b:a", "192k"]


def run_ffmpeg_with_progress(args: List[str], duration: float, label: str,
                             on_progress: Optional[Callable[[float], None]] = None) -> None:
    """跑 ffmpeg 并解析 -progress 输出。给了 on_progress 就回调 0~1，否则打印到控制台。"""
    cmd = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y"] + args + ["-progress", "pipe:1", "-nostats"]
    proc = subprocess.Popen(
        cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
        creationflags=NO_WINDOW,
    )
    last = -1
    try:
        for line in proc.stdout:  # type: ignore[union-attr]
            line = line.strip()
            if line.startswith("out_time_us=") or line.startswith("out_time_ms="):
                try:
                    us = int(line.split("=", 1)[1])
                except ValueError:
                    continue
                if duration > 0:
                    frac = max(0.0, min(1.0, us / 1e6 / duration))
                    if on_progress is not None:
                        on_progress(frac)
                    elif SHOW_PROGRESS:
                        pct = int(frac * 100)
                        if pct >= last + 5:
                            last = pct
                            print(f"\r      {label} {pct:3d}% ", end="", flush=True)
            elif line == "progress=end":
                break
    finally:
        stderr = proc.stderr.read() if proc.stderr else ""  # type: ignore[union-attr]
        proc.wait()
    if last >= 0 and SHOW_PROGRESS and on_progress is None:
        print("\r" + " " * 40 + "\r", end="", flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 失败 (code={proc.returncode}):\n{stderr.strip()[:1200]}")


def process_delogo(task: Task, crf: int, preset: str, keep_audio: bool,
                   on_progress: Optional[Callable[[float], None]] = None) -> None:
    vf = build_delogo(task.boxes, task.info.width, task.info.height)
    args = [
        "-i", task.src,
        "-vf", vf,
        *_encoder_args(crf, preset, ["-movflags", "+faststart"] if task.dst.lower().endswith((".mp4", ".m4v", ".mov")) else []),
        *_audio_args(task.info, keep_audio),
    ]
    args.append(task.dst)
    run_ffmpeg_with_progress(args, task.info.duration, "去水印", on_progress)


def process_inpaint(task: Task, crf: int, preset: str, keep_audio: bool, algo: str = "telea",
                    radius: int = 4, on_progress: Optional[Callable[[float], None]] = None) -> None:
    """逐帧 OpenCV 掩膜修复，原始帧经管道直接送 ffmpeg 编码（只编码一次）。"""
    import cv2
    import numpy as np

    info = task.info
    W, H = info.width, info.height
    mask = np.zeros((H, W), np.uint8)
    for bx in task.boxes:
        x, y, w, h = clamp_box(bx, W, H)
        mask[y:y + h, x:x + w] = 255
    flag = cv2.INPAINT_NS if algo.lower() == "ns" else cv2.INPAINT_TELEA

    fps = info.fps or 25.0
    args = [FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
            "-f", "rawvideo", "-pix_fmt", "bgr24", "-s", f"{W}x{H}", "-r", f"{fps:.6f}",
            "-i", "pipe:0"]
    if keep_audio and info.has_audio:
        args += ["-i", task.src, "-map", "0:v:0", "-map", "1:a:0", *_audio_args(info, True)]
    else:
        args += ["-map", "0:v:0", "-an"]
    args += [*_encoder_args(crf, preset), "-movflags", "+faststart", task.dst]

    proc = subprocess.Popen(args, stdin=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW)
    cap = cv2.VideoCapture(task.src)
    total = max(1, int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0))
    idx = 0
    try:
        while True:
            ok, frame = cap.read()  # type: ignore[misc]
            if not ok:
                break
            if frame.shape[1] != W or frame.shape[0] != H:
                frame = cv2.resize(frame, (W, H), interpolation=cv2.INTER_AREA)
            fixed = cv2.inpaint(frame, mask, radius, flag)
            proc.stdin.write(fixed.tobytes())  # type: ignore[union-attr]
            idx += 1
            if on_progress is not None and idx % 4 == 0:
                on_progress(min(1.0, idx / total))
            if idx % 12 == 0 and SHOW_PROGRESS and on_progress is None:
                pct = int(idx / total * 100)
                print(f"\r      修复 {pct:3d}% ", end="", flush=True)
    finally:
        cap.release()
        if proc.stdin:
            proc.stdin.close()
        err = proc.stderr.read() if proc.stderr else b""
        proc.wait()
        if SHOW_PROGRESS:
            print("\r" + " " * 40 + "\r", end="", flush=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg 编码失败: {err.decode('utf-8', 'replace')[:800]}")


# --------------------------------------------------------------------------- #
# 批量框架
# --------------------------------------------------------------------------- #
def collect_inputs(inputs: Iterable[str], recursive: bool, include_exts: set) -> List[str]:
    files: List[str] = []
    for raw in inputs:
        p = Path(raw)
        if p.is_dir():
            it = p.rglob("*") if recursive else p.glob("*")
            for f in sorted(it):
                if f.is_file() and f.suffix.lower() in include_exts and not f.name.startswith("."):
                    files.append(str(f))
        elif p.is_file():
            if p.suffix.lower() in include_exts:
                files.append(str(p))
            else:
                log(f"  跳过（不是支持的视频格式）: {p}")
        else:
            log(f"  跳过（路径不存在）: {p}")
    # 去重保序
    seen, uniq = set(), []
    for f in files:
        key = os.path.normcase(os.path.abspath(f))
        if key not in seen:
            seen.add(key)
            uniq.append(f)
    return uniq


def make_output_path(src: str, outdir: Optional[str], suffix: str) -> str:
    stem = Path(src).stem
    name = f"{stem}{suffix}.mp4"
    if outdir:
        d = Path(outdir)
        d.mkdir(parents=True, exist_ok=True)
        return str(d / name)
    return str(Path(src).with_name(name))


def draw_preview(src: str, dst_png: str, det: Detection, boxes: Sequence[Box], frame_idx: int = 0) -> None:
    import cv2
    import numpy as np

    cap = cv2.VideoCapture(src)
    if frame_idx:
        cap.set(cv2.CAP_PROP_POS_FRAMES, frame_idx)
    ok, frame = cap.read()
    cap.release()
    if not ok:
        return
    # 把命中的水印像素染红，方便确认掩膜覆盖面
    if det.mask is not None and det.mask.shape[:2] == frame.shape[:2]:
        m = det.mask > 0
        frame[m] = (0.3 * frame[m].astype(float) + 0.7 * np.array([60, 60, 255])).astype("uint8")
    for x, y, w, h in boxes:
        cv2.rectangle(frame, (x, y), (x + w, y + h), (0, 255, 0), 2)
    # 用 imencode + Python 写文件，避开 OpenCV 在 Windows 中文路径下的编码问题
    Path(dst_png).parent.mkdir(parents=True, exist_ok=True)
    ok_enc, buf = cv2.imencode(".png", frame)
    if ok_enc:
        Path(dst_png).write_bytes(buf.tobytes())


def parse_box(text: str) -> Box:
    nums = [int(float(v)) for v in re.split(r"[,\sxX]+", text.strip()) if v.strip()]
    if len(nums) != 4:
        raise argparse.ArgumentTypeError("--box 格式应为 x,y,w,h")
    return nums[0], nums[1], nums[2], nums[3]


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="dewatermark",
        description="视频水印批量去除工具（自动定位右下角等区域的静止水印）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    ap.add_argument("inputs", nargs="*", default=None, help="视频文件或目录，可多个；缺省为当前目录")
    ap.add_argument("-o", "--output-dir", default=None, help="输出目录（默认与源文件同目录）")
    ap.add_argument("--suffix", default="_dewm", help="输出文件名后缀（默认 _dewm）")
    ap.add_argument("--mode", choices=["delogo", "inpaint"], default="delogo",
                    help="修复引擎：delogo（快，默认）/ inpaint（掩膜修复，细节更好但慢）")
    ap.add_argument("--corner", choices=list(CORNER_ROI.keys()), default="br", help="水印所在角落（默认 br 右下）")
    ap.add_argument("--box", action="append", type=parse_box, default=None,
                    help="手动指定水印框 x,y,w,h（可重复传多个框）；指定后不再自动定位")
    ap.add_argument("--no-auto", action="store_true", help="不做自动定位，用 --box 或默认比例框")
    ap.add_argument("--inpaint-algo", choices=["telea", "ns"], default="telea",
                    help="inpaint 引擎的算法：telea（默认，平滑）/ ns（保留结构边缘）")
    ap.add_argument("--pad", type=int, default=2, help="自动定位后向外扩的像素（默认 2）")
    ap.add_argument("--crf", type=int, default=18, help="x264 CRF，越小越清晰（默认 18）")
    ap.add_argument("--preset", default="medium", help="x264 preset（默认 medium）")
    ap.add_argument("--jobs", type=int, default=1, help="并发处理文件数（默认 1）")
    ap.add_argument("--recursive", action="store_true", help="目录输入时递归子目录")
    ap.add_argument("--overwrite", action="store_true", help="覆盖已存在的输出")
    ap.add_argument("--no-audio", action="store_true", help="不保留音轨")
    ap.add_argument("--dry-run", action="store_true", help="只定位并打印结果，不处理")
    ap.add_argument("--preview", action="store_true", help="在输出目录生成定位预览图（绿框=去水印范围）")
    ap.add_argument("--samples", type=int, default=32, help="自动定位时的抽样帧数（默认 32）")
    ap.add_argument("-q", "--quiet", action="store_true", help="少输出")
    args = ap.parse_args(argv)

    global SHOW_PROGRESS
    SHOW_PROGRESS = (args.jobs <= 1) and not args.quiet

    if args.mode == "inpaint" and not HAVE_CV:
        log("inpaint 引擎依赖 opencv-python，请先执行: pip install opencv-python numpy")
        return 2

    inputs = args.inputs or ["."]
    files = collect_inputs(inputs, args.recursive, VIDEO_EXTS)
    if not files:
        log("没有找到可处理的视频文件。")
        return 2

    log(f"ffmpeg : {FFMPEG}")
    log(f"待处理 : {len(files)} 个文件    引擎: {args.mode}")

    tasks: List[Task] = []
    for i, src in enumerate(files, 1):
        log(f"\n[{i}/{len(files)}] {os.path.basename(src)}")
        try:
            info = probe(src)
        except Exception as e:
            log(f"    探测失败，跳过: {e}")
            continue
        log(f"    分辨率 {info.width}x{info.height}  {info.fps:.2f}fps  {human_time(info.duration)}  "
            f"{human_size(os.path.getsize(src))}")
        if info.width <= 0 or info.height <= 0:
            log("    无法读取画面尺寸，跳过")
            continue

        det = Detection((0, 0, 0, 0), ok=False, note="手动指定")
        boxes: List[Box] = []
        if args.box:
            boxes = list(args.box)
        elif args.no_auto:
            # 兜底：按比例猜一个右下角框（宽 19%，高 3.2%）
            w = int(info.width * 0.19)
            h = max(12, int(info.height * 0.032))
            boxes = [(info.width - w - int(info.width * 0.02), info.height - h - int(info.height * 0.017), w, h)]
        else:
            if not HAVE_CV:
                log("    [警告] 缺少 opencv-python，自动定位不可用，已改用默认比例框。"
                    "建议执行: pip install opencv-python numpy")
            t0 = time.time()
            det = detect_watermark(src, corner=args.corner, samples=args.samples)
            if det.ok:
                x, y, w, h = det.box
                x -= args.pad
                y -= args.pad
                w += args.pad * 2
                h += args.pad * 2
                boxes = [clamp_box((x, y, w, h), info.width, info.height)]
                log(f"    自动定位: x={boxes[0][0]} y={boxes[0][1]} w={boxes[0][2]} h={boxes[0][3]}"
                    f"  ({time.time() - t0:.1f}s)")
            else:
                w = int(info.width * 0.19)
                h = max(12, int(info.height * 0.032))
                boxes = [clamp_box((info.width - w - int(info.width * 0.02),
                                    info.height - h - int(info.height * 0.017), w, h), info.width, info.height)]
                log(f"    自动定位失败（{det.note}），改用默认比例框: {boxes[0]}")

        dst = make_output_path(src, args.output_dir, args.suffix)
        if os.path.exists(dst) and not args.overwrite and not args.dry_run:
            log(f"    输出已存在，跳过: {os.path.basename(dst)}（加 --overwrite 覆盖）")
            continue

        if args.preview:
            png = str(Path(dst).with_suffix(".preview.png"))
            draw_preview(src, png, det, boxes)
            log(f"    预览图: {png}")

        tasks.append(Task(src=src, dst=dst, info=info, boxes=boxes)
                          )

    if args.dry_run:
        log(f"\n--dry-run：共 {len(tasks)} 个文件可处理，未写入任何文件。")
        return 0
    if not tasks:
        log("\n没有需要处理的文件。")
        return 0

    log(f"\n开始处理 {len(tasks)} 个文件 ...")
    ok_n, fail_n = 0, 0
    t_start = time.time()

    def worker(task: Task) -> Tuple[Task, Optional[str]]:
        try:
            if args.mode == "inpaint":
                process_inpaint(task, args.crf, args.preset, not args.no_audio, algo=args.inpaint_algo)
            else:
                process_delogo(task, args.crf, args.preset, not args.no_audio)
            return task, None
        except Exception as e:  # noqa: BLE001
            return task, str(e)

    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as ex:
            futures = [ex.submit(worker, t) for t in tasks]
            for fut in as_completed(futures):
                task, err = fut.result()
                if err:
                    fail_n += 1
                    log(f"  失败: {os.path.basename(task.src)}\n    {err}")
                else:
                    ok_n += 1
                    log(f"  完成: {os.path.basename(task.dst)}  {human_size(os.path.getsize(task.dst))}")
    else:
        for i, task in enumerate(tasks, 1):
            log(f"  [{i}/{len(tasks)}] {os.path.basename(task.src)}")
            t0 = time.time()
            task_, err = worker(task)
            if err:
                fail_n += 1
                log(f"      失败: {err}")
            else:
                ok_n += 1
                log(f"      完成 → {task.dst}  ({human_size(os.path.getsize(task.dst))}, {time.time() - t0:.1f}s)")

    log(f"\n全部结束：成功 {ok_n}，失败 {fail_n}，总耗时 {human_time(time.time() - t_start)}")
    return 0 if fail_n == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
