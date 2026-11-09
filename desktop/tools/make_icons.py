"""生成桌面端应用图标（暖纸底 + 黑色方框 + 白色 ◈）。

设计令牌来自 docs/UI-OPS-REDESIGN.md 第 1 节：
    --ops-paper #f2f0ea / --ops-ink #17212b / --ops-accent #df542f

用法：
    .\\.venv\\Scripts\\python.exe desktop\\tools\\make_icons.py

产物：
    assets\\icon-1024.png   设计母版
    assets\\icon.png        512×512（BrowserWindow / Linux）
    assets\\icon.ico        16/24/32/48/64/128/256 多尺寸（electron-builder win.icon）
    desktop\\assets\\*       同一份拷贝，随 electron-builder files/extraResources 一起打包

几何全部手绘（不依赖字体），保证 16px 下仍然清晰。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

REPO = Path(__file__).resolve().parents[2]
ROOT_ASSETS = REPO / "assets"
DESKTOP_ASSETS = REPO / "desktop" / "assets"

PAPER = (242, 240, 234, 255)   # --ops-paper
INK = (23, 33, 43, 255)        # --ops-ink
WHITE = (255, 253, 248, 255)   # --ops-card

MASTER = 1024
ICO_SIZES = [(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)]


def _diamond(draw: ImageDraw.ImageDraw, cx: float, cy: float, r: float, fill) -> None:
    """以 (cx, cy) 为中心、半对角半径 r 的菱形（◈ 的骨架）。"""
    draw.polygon(
        [(cx, cy - r), (cx + r, cy), (cx, cy + r), (cx - r, cy)],
        fill=fill,
    )


def render(size: int = MASTER) -> Image.Image:
    """渲染一张 size×size 的图标（超采样绘制后交给调用方缩放）。"""
    img = Image.new("RGBA", (size, size), PAPER)
    draw = ImageDraw.Draw(img)

    # 黑色方框：四周留 8% 呼吸位，圆角 10%（直角太硬，圆角更像应用图标）
    pad = round(size * 0.08)
    box = (pad, pad, size - pad - 1, size - pad - 1)
    radius = round((size - 2 * pad) * 0.10)
    draw.rounded_rectangle(box, radius=radius, fill=INK)

    # 白色 ◈：外菱形实心 + 内菱形挖空（露出黑底）
    cx = cy = size / 2
    outer = size * 0.255
    inner = size * 0.098
    _diamond(draw, cx, cy, outer, WHITE)
    _diamond(draw, cx, cy, inner, INK)

    return img


def main() -> int:
    master = render(MASTER)
    ROOT_ASSETS.mkdir(parents=True, exist_ok=True)
    DESKTOP_ASSETS.mkdir(parents=True, exist_ok=True)

    master.save(ROOT_ASSETS / "icon-1024.png")
    for target in (ROOT_ASSETS, DESKTOP_ASSETS):
        master.resize((512, 512), Image.LANCZOS).save(target / "icon.png")
        master.save(target / "icon.ico", sizes=ICO_SIZES)
        # 任务栏 / 托盘常用 256 独立 PNG
        master.resize((256, 256), Image.LANCZOS).save(target / "icon-256.png")

    for path in sorted(ROOT_ASSETS.glob("icon*")) + sorted(DESKTOP_ASSETS.glob("icon*")):
        print(f"{path.relative_to(REPO)}  {path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
