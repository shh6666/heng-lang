#!/usr/bin/env python3
"""生成衡语的 macOS 应用图标（.icns）。

不依赖 Pillow：手写 PNG 编码（zlib + CRC），再做圆角与简单图形。
设计：深靛色圆角方块 + 中央「衡」字的几何化图形（天平衡梁）+ 底部一道横线。

用法：python3 tools/make_icon.py [输出目录]
"""

from __future__ import annotations

import struct
import subprocess
import sys
import zlib
from pathlib import Path

# macOS 图标需要的尺寸（iconset）
SIZES = [16, 32, 64, 128, 256, 512, 1024]


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def write_png(path: Path, w: int, h: int, pixels: bytearray) -> None:
    """pixels 是 RGBA 逐行、无过滤字节的原始数据。"""
    raw = bytearray()
    stride = w * 4
    for y in range(h):
        raw.append(0)                      # filter type 0
        raw += pixels[y * stride:(y + 1) * stride]
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)   # 8bit RGBA
    out = (b"\x89PNG\r\n\x1a\n"
           + _chunk(b"IHDR", ihdr)
           + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
           + _chunk(b"IEND", b""))
    path.write_bytes(out)


def render(size: int) -> bytearray:
    """画一张 size×size 的 RGBA 图。

    用超采样抗锯齿：内部按 4 倍绘制再平均。
    """
    ss = 4
    n = size * ss
    buf = bytearray(n * n * 4)

    # 配色
    bg_top = (28, 32, 68)        # 深靛
    bg_bot = (14, 16, 36)
    fg = (245, 248, 255)         # 近白
    accent = (96, 200, 255)      # 天蓝

    radius = n * 0.22            # 圆角半径
    cx = cy = n / 2.0

    def inside_round_rect(x: float, y: float) -> bool:
        # 圆角矩形：四角用圆判定
        rx = min(max(x, radius), n - radius)
        ry = min(max(y, radius), n - radius)
        return (x - rx) ** 2 + (y - ry) ** 2 <= radius * radius

    # 图形参数（按 n 归一化）
    def f(v: float) -> float:
        return v * n

    beam_y = f(0.36)
    beam_h = f(0.055)
    beam_x0, beam_x1 = f(0.20), f(0.80)

    post_x = f(0.475)
    post_w = f(0.05)
    post_y0, post_y1 = f(0.34), f(0.70)

    base_y = f(0.70)
    base_h = f(0.055)

    # 左右托盘（圆点在梁两端下方）
    pan_r = f(0.075)
    pan1 = (f(0.28), f(0.50))
    pan2 = (f(0.72), f(0.50))

    for y in range(n):
        for x in range(n):
            px, py = x + 0.5, y + 0.5
            if not inside_round_rect(px, py):
                continue
            # 背景竖向渐变
            t = py / n
            r = int(bg_top[0] * (1 - t) + bg_bot[0] * t)
            g = int(bg_top[1] * (1 - t) + bg_bot[1] * t)
            b = int(bg_top[2] * (1 - t) + bg_bot[2] * t)
            col = [r, g, b, 255]

            on = False
            # 横梁
            if beam_x0 <= px <= beam_x1 and beam_y <= py <= beam_y + beam_h:
                on = True
            # 立柱
            if post_x <= px <= post_x + post_w and post_y0 <= py <= post_y1:
                on = True
            # 底座
            if f(0.30) <= px <= f(0.70) and base_y <= py <= base_y + base_h:
                on = True
            # 两个托盘（圆）
            for (pcx, pcy) in (pan1, pan2):
                if (px - pcx) ** 2 + (py - pcy) ** 2 <= pan_r * pan_r:
                    on = True
            # 吊绳
            for pcx in (pan1[0], pan2[0]):
                if abs(px - pcx) <= f(0.008) and beam_y + beam_h <= py <= pan1[1] - pan_r * 0.8:
                    on = True

            if on:
                # 托盘与横梁用强调色，其余近白
                is_pan = any((px - pcx) ** 2 + (py - pcy) ** 2 <= pan_r * pan_r
                             for pcx, pcy in (pan1, pan2))
                if is_pan:
                    col = [accent[0], accent[1], accent[2], 255]
                else:
                    col = [fg[0], fg[1], fg[2], 255]

            i = (y * n + x) * 4
            buf[i:i + 4] = bytes(col)

    # 超采样缩放
    out = bytearray(size * size * 4)
    k = ss * ss
    for y in range(size):
        for x in range(size):
            ar = ag = ab = aa = 0
            for dy in range(ss):
                for dx in range(ss):
                    i = ((y * ss + dy) * n + (x * ss + dx)) * 4
                    ar += buf[i]; ag += buf[i + 1]; ab += buf[i + 2]; aa += buf[i + 3]
            o = (y * size + x) * 4
            out[o] = ar // k
            out[o + 1] = ag // k
            out[o + 2] = ab // k
            out[o + 3] = aa // k
    return out


def main() -> int:
    out_root = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("build/icon")
    iconset = out_root / "Heng.iconset"
    iconset.mkdir(parents=True, exist_ok=True)

    print("生成各尺寸 PNG…")
    for s in SIZES:
        png = iconset / f"icon_{s}x{s}.png"
        write_png(png, s, s, render(s))
        # macOS 还需要 @2x 命名
        if s <= 512:
            png2 = iconset / f"icon_{s}x{s}@2x.png"
            write_png(png2, s * 2, s * 2, render(s * 2))
        print(f"  {s}x{s}")

    icns = out_root / "Heng.icns"
    r = subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(icns)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print("iconutil 失败：", r.stderr[:300])
        return 1
    print(f"✓ {icns}（{icns.stat().st_size // 1024} KB）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
