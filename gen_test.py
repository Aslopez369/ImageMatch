#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成测试用图库：60 张互不相关的"照片" + 6 张同一张图的变形版本。"""
import random
import shutil
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

BASE = Path(__file__).resolve().parent
ALBUM = BASE / "test" / "album"
QUERY = BASE / "test" / "query"


def make_scene(seed: int, size=(1200, 900)) -> Image.Image:
    rnd = random.Random(seed)
    w, h = size
    img = Image.new("RGB", size)
    d = ImageDraw.Draw(img)
    # 背景渐变
    c1 = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
    c2 = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
    for y in range(h):
        t = y / h
        d.line([(0, y), (w, y)],
               fill=(int(c1[0] * (1 - t) + c2[0] * t),
                     int(c1[1] * (1 - t) + c2[1] * t),
                     int(c1[2] * (1 - t) + c2[2] * t)))
    # 随机形状
    for _ in range(rnd.randint(8, 16)):
        x0 = rnd.randint(-100, w)
        y0 = rnd.randint(-100, h)
        x1 = x0 + rnd.randint(60, 500)
        y1 = y0 + rnd.randint(60, 400)
        col = (rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        if rnd.random() < 0.5:
            d.ellipse([x0, y0, x1, y1], fill=col)
        else:
            d.rectangle([x0, y0, x1, y1], fill=col)
    for _ in range(rnd.randint(3, 7)):
        pts = [(rnd.randint(0, w), rnd.randint(0, h)) for _ in range(5)]
        d.line(pts, fill=(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255)),
               width=rnd.randint(3, 18))
    return img


def main():
    if BASE.joinpath("test").exists():
        shutil.rmtree(BASE / "test")
    ALBUM.mkdir(parents=True)
    QUERY.mkdir(parents=True)

    for i in range(60):
        make_scene(i).save(ALBUM / f"photo_{i:03d}.jpg", quality=92)

    # 目标图 = photo_007，做各种变形后放进库
    src = make_scene(7)
    src.save(ALBUM / "orig_007.jpg", quality=95)

    v = src.copy()
    v.resize((800, 600), Image.LANCZOS).save(ALBUM / "v_resized.jpg", quality=72)

    w, h = src.size
    src.crop((int(w * 0.15), int(h * 0.15), int(w * 0.85), int(h * 0.85))).save(
        ALBUM / "v_cropped.jpg", quality=88)

    src.rotate(12, expand=True, fillcolor=(255, 255, 255)).save(
        ALBUM / "v_rotated.jpg", quality=88)

    ImageEnhance.Brightness(src).enhance(1.25).save(ALBUM / "v_brighter.jpg", quality=88)

    wm = src.copy()
    dw = ImageDraw.Draw(wm)
    dw.text((40, h - 70), "WATERMARK 2026", fill=(255, 255, 255))
    wm.save(ALBUM / "v_watermark.jpg", quality=85)

    src.filter(ImageFilter.GaussianBlur(2.0)).save(ALBUM / "v_blur.jpg", quality=80)

    # 查询图：亮度变化 + 轻微压缩（不在库里）
    ImageEnhance.Color(src).enhance(1.3).save(QUERY / "query.jpg", quality=75)

    total = len(list(ALBUM.glob("*.jpg")))
    print(f"测试库已生成：{ALBUM}  共 {total} 张")
    print(f"查询图：{QUERY / 'query.jpg'}")


if __name__ == "__main__":
    main()
