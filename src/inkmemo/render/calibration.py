from __future__ import annotations

import hashlib
import io

from PIL import Image, ImageDraw, ImageFont

from ..models import LayoutResult
from .layouts import RED, png_to_planes


def _font(size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", size)


def create_calibration() -> LayoutResult:
    image = Image.new("RGB", (800, 480), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle((12, 12, 787, 467), outline="black", width=3)
    draw.rectangle((20, 20, 120, 70), fill="black")
    draw.text((35, 28), "左上", font=_font(24), fill="white")
    draw.rectangle((680, 20, 780, 70), fill=RED)
    draw.text((695, 28), "右上", font=_font(24), fill="white")
    draw.rectangle((20, 410, 120, 460), fill=RED)
    draw.text((35, 418), "左下", font=_font(24), fill="white")
    draw.rectangle((680, 410, 780, 460), fill="black")
    draw.text((695, 418), "右下", font=_font(24), fill="white")

    title_font = _font(54)
    body_font = _font(24)
    title = "墨笺 InkMemo"
    title_width = draw.textlength(title, font=title_font)
    draw.text(((800 - title_width) / 2, 168), title, font=title_font, fill="black")
    draw.rectangle((250, 244, 550, 264), fill=RED)
    subtitle = "方向 / 红黑 / 中文校准图"
    subtitle_width = draw.textlength(subtitle, font=body_font)
    draw.text(((800 - subtitle_width) / 2, 286), subtitle, font=body_font, fill="black")

    draw.polygon(((400, 88), (374, 128), (426, 128)), fill=RED)
    draw.rectangle((390, 126, 410, 154), fill=RED)
    draw.text((438, 108), "上", font=_font(28), fill="black")
    draw.text((334, 346), "请确认：上方箭头朝上，左上黑、右上红、左下红、右下黑", font=_font(20), fill="black")

    output = io.BytesIO()
    image.save(output, format="PNG")
    png = output.getvalue()
    black, red = png_to_planes(png)
    return LayoutResult(
        layout="calibration",
        png=png,
        black=black,
        red=red,
        display_hash=hashlib.sha256(png).hexdigest(),
    )
