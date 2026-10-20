#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
本地图片相似度检索工具
------------------------------------------------
用途：在某个文件夹（上千 ~ 上万张）里，找出与指定样图最相似的前 N 张。

原理（两阶段）：
  1) 感知哈希粗筛：pHash / dHash / aHash，O(1) 位运算比对，几万张毫秒级。
     对压缩、改尺寸、调色、加水印、轻微模糊都稳定。
  2) 局部特征复核：对粗筛出的候选，用 ORB 特征 + RANSAC 单应性估计，
     能识别裁剪、旋转、镜像、拼接等哈希不擅长的变化；
     同时叠加灰度归一化互相关（NCC）作为结构相似度。
     最终分数 = 加权综合，0~100。

用法示例：
  python find_similar.py --folder "D:\\照片库" --query "D:\\样图.jpg" --top 20
  python find_similar.py --folder "D:\\照片库" --query "D:\\样图.jpg" --top 20 --copy-to "D:\\命中结果"
"""
import argparse
import base64
import hashlib
import html
import io
import json
import os
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:
    import cv2
    HAS_CV2 = True
except Exception:
    HAS_CV2 = False

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

IMG_EXTS = {
    ".jpg", ".jpeg", ".jpe", ".png", ".bmp", ".gif", ".webp", ".tif", ".tiff",
    ".ppm", ".pgm", ".jfif", ".avif",
}

INDEX_VERSION = 5
SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / "cache"
OUTPUT_DIR = SCRIPT_DIR / "output"


# --------------------------------------------------------------------------
# 哈希计算
# --------------------------------------------------------------------------
_DCT = None


def dct_matrix(n: int) -> np.ndarray:
    """正交 DCT-II 变换矩阵，避免引入 scipy。"""
    k = np.arange(n, dtype=np.float64)
    m = np.cos(np.pi * (2 * k[None, :] + 1) * k[:, None] / (2.0 * n))
    m[0, :] *= 1.0 / np.sqrt(2.0)
    return m * np.sqrt(2.0 / n)


def _bits_to_int(bits: np.ndarray) -> int:
    v = 0
    for b in bits.astype(bool).ravel():
        v = (v << 1) | int(b)
    return v


def phash64(gray32: np.ndarray) -> int:
    global _DCT
    if _DCT is None:
        _DCT = dct_matrix(32)
    d = _DCT @ gray32 @ _DCT.T
    low = d[:8, :8].ravel()
    med = np.median(low[1:])          # 排除直流分量，判别力更强
    bits = low > med
    bits_full = np.zeros(64, dtype=bool)
    bits_full[:64] = bits[:64]
    return _bits_to_int(bits_full)


def dhash64(gray9x8: np.ndarray) -> int:
    return _bits_to_int(gray9x8[:, 1:] > gray9x8[:, :-1])


def ahash64(gray8: np.ndarray) -> int:
    return _bits_to_int(gray8 > gray8.mean())


def _open_image(path: Path):
    """统一打开：修正 EXIF 方向、取首帧、转 RGB。"""
    im = Image.open(path)
    im.load()
    im = ImageOps.exif_transpose(im)
    if im.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", im.size, (255, 255, 255))
        conv = im.convert("RGBA")
        bg.paste(conv, mask=conv.split()[-1])
        im = bg
    else:
        im = im.convert("RGB")
    return im


def compute_features(path: Path) -> dict:
    """单张图的特征：三种 64bit 哈希 + 尺寸 + 平均色。"""
    with _open_image(path) as im:
        w, h = im.size
        gray = im.convert("L")
        g32 = np.asarray(gray.resize((32, 32), Image.LANCZOS), dtype=np.float64)
        g98 = np.asarray(gray.resize((9, 8), Image.LANCZOS), dtype=np.float64)
        g8 = np.asarray(gray.resize((8, 8), Image.LANCZOS), dtype=np.float64)
        rgb_small = np.asarray(im.resize((8, 8), Image.LANCZOS), dtype=np.float64)
        # 中心裁剪哈希：让"被裁剪过的同一张图"也能匹配上
        cw, ch = im.size
        center = gray.crop((int(cw * 0.125), int(ch * 0.125),
                            int(cw * 0.875), int(ch * 0.875)))
        gc32 = np.asarray(center.resize((32, 32), Image.LANCZOS), dtype=np.float64)
    st = os.stat(path)
    return {
        "path": str(Path(path).resolve()),
        "size": st.st_size,
        "mtime": round(st.st_mtime, 3),
        "w": w,
        "h": h,
        "phash": format(phash64(g32), "016x"),
        "phash_c": format(phash64(gc32), "016x"),
        "dhash": format(dhash64(g98), "016x"),
        "ahash": format(ahash64(g8), "016x"),
        "mean": [round(float(x), 1) for x in rgb_small.reshape(-1, 3).mean(axis=0)],
    }


# --------------------------------------------------------------------------
# 索引
# --------------------------------------------------------------------------
def list_images(folder: Path):
    out = []
    skip_roots = {str(CACHE_DIR.resolve()).lower(), str(OUTPUT_DIR.resolve()).lower()}
    for root, dirs, files in os.walk(folder):
        if str(Path(root).resolve()).lower() in skip_roots:
            dirs[:] = []
            continue
        for name in files:
            if Path(name).suffix.lower() in IMG_EXTS:
                out.append(Path(os.path.abspath(os.path.join(root, name))))
    return out


def cache_file(folder: Path) -> Path:
    key = hashlib.sha1(str(folder.resolve()).lower().encode("utf-8")).hexdigest()[:16]
    return CACHE_DIR / f"index_{key}.json"


def build_index(folder: Path, workers: int, rebuild: bool) -> list:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cf = cache_file(folder)
    old = {}
    if cf.exists() and not rebuild:
        try:
            data = json.loads(cf.read_text(encoding="utf-8"))
            if data.get("version") == INDEX_VERSION:
                for it in data.get("items", []):
                    old[it["path"]] = it
        except Exception:
            old = {}

    files = list_images(folder)
    total = len(files)
    if total == 0:
        print(f"[!] 文件夹里没有找到图片：{folder}")
        return []

    print(f"[i] 文件夹：{folder}")
    print(f"[i] 找到图片 {total} 张，缓存命中 {sum(1 for f in files if str(f) in old)} 张")

    results = []
    todo = []
    for f in files:
        key = str(f)
        prev = old.get(key)
        try:
            st = f.stat()
        except OSError:
            continue
        if prev and prev.get("size") == st.st_size and abs(prev.get("mtime", 0) - st.st_mtime) < 1:
            results.append(prev)
        else:
            todo.append(f)

    if todo:
        print(f"[i] 需要计算特征 {len(todo)} 张，用 {workers} 线程…")
        done = 0
        fail = 0
        t0 = time.time()

        def work(p):
            try:
                return compute_features(p)
            except Exception:
                return None

        with ThreadPoolExecutor(max_workers=workers) as ex:
            for feats in ex.map(work, todo):
                done += 1
                if feats is None:
                    fail += 1
                else:
                    results.append(feats)
                if done % 20 == 0 or done == len(todo):
                    el = time.time() - t0
                    speed = done / el if el > 0 else 0
                    print(f"    进度 {done}/{len(todo)}  ({speed:.1f} 张/秒, 失败 {fail})", end="\r")
        print()
        if fail:
            print(f"[!] {fail} 张无法读取，已跳过")

        cf.write_text(
            json.dumps({"version": INDEX_VERSION, "folder": str(folder), "items": results},
                       ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"[i] 索引已缓存：{cf}")

    return results


# --------------------------------------------------------------------------
# 复核打分
# --------------------------------------------------------------------------
_ORB = None


def _orb():
    global _ORB
    if _ORB is None:
        _ORB = cv2.ORB_create(nfeatures=1200, fastThreshold=10)
    return _ORB


def _cv_gray(path: str, max_side: int = 640):
    try:
        buf = np.fromfile(path, dtype=np.uint8)     # 兼容中文路径
        img = cv2.imdecode(buf, cv2.IMREAD_GRAYSCALE)
    except Exception:
        return None
    if img is None:
        return None
    h, w = img.shape[:2]
    s = max_side / max(h, w)
    if s < 1:
        img = cv2.resize(img, (int(w * s), int(h * s)), interpolation=cv2.INTER_AREA)
    return img


def ncc_score(a_path: str, b_path: str) -> float:
    """灰度归一化互相关：对压缩/调色很敏感于结构，对亮度线性变化不敏感。"""
    try:
        a = _cv_gray(a_path, 256)
        b = _cv_gray(b_path, 256)
        if a is None or b is None:
            return 0.0
        a = cv2.resize(a, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float64).ravel()
        b = cv2.resize(b, (64, 64), interpolation=cv2.INTER_AREA).astype(np.float64).ravel()
        a -= a.mean()
        b -= b.mean()
        denom = np.sqrt((a * a).sum() * (b * b).sum())
        if denom < 1e-6:
            return 0.0
        return float(max(0.0, (a * b).sum() / denom))
    except Exception:
        return 0.0


def orb_score(q_path: str, c_path: str):
    """ORB + RANSAC 几何校验，对裁剪/旋转/镜像免疫。返回 0~1，失败返回 None。"""
    if not HAS_CV2:
        return None
    try:
        q = _cv_gray(q_path)
        c = _cv_gray(c_path)
        if q is None or c is None:
            return None
        orb = _orb()
        kp1, d1 = orb.detectAndCompute(q, None)
        kp2, d2 = orb.detectAndCompute(c, None)
        if d1 is None or d2 is None or len(kp1) < 10 or len(kp2) < 10:
            return None
        bf = cv2.BFMatcher(cv2.NORM_HAMMING, crossCheck=False)
        knn = bf.knnMatch(d1, d2, k=2)
        good = [m for pair in knn if len(pair) == 2
                for m, n in [pair] if m.distance < 0.78 * n.distance]
        if len(good) < 8:
            return 0.0
        src = np.float32([kp1[m.queryIdx].pt for m in good]).reshape(-1, 1, 2)
        dst = np.float32([kp2[m.trainIdx].pt for m in good]).reshape(-1, 1, 2)
        _, mask = cv2.findHomography(src, dst, cv2.RANSAC, 5.0, maxIters=2000)
        if mask is None:
            return 0.0
        inliers = int(mask.sum())
        if inliers < 8:          # 内点太少视为误配，避免重复纹理误判
            return 0.0
        base = max(1, min(len(kp1), len(kp2)))
        return float(min(1.0, inliers / base / 0.30))
    except Exception:
        return None


def clamp01(x: float) -> float:
    return 0.0 if x < 0 else (1.0 if x > 1 else x)


# --------------------------------------------------------------------------
# 查询
# --------------------------------------------------------------------------
def search(folder: Path, query: Path, top_n: int, workers: int, rebuild: bool,
           use_orb: bool, threshold: float):
    items = build_index(folder, workers, rebuild)
    if not items:
        return None, []

    q = compute_features(query)
    qph = int(q["phash"], 16)
    qphc = int(q["phash_c"], 16)
    qdh = int(q["dhash"], 16)
    qah = int(q["ahash"], 16)

    # ---------- 阶段一：哈希粗筛 ----------
    t0 = time.time()
    cand = []
    qpath_abs = os.path.normcase(str(query.resolve()))
    for it in items:
        if os.path.normcase(it["path"]) == qpath_abs:
            continue
        cph = int(it["phash"], 16)
        cphc = int(it.get("phash_c", it["phash"]), 16)
        # 全图 × 全图、全图 × 中心裁剪 四种组合取最小距离，容忍裁剪
        dp = min((cph ^ qph).bit_count(),
                 (cphc ^ qph).bit_count() + 2,
                 (cph ^ qphc).bit_count() + 2,
                 (cphc ^ qphc).bit_count() + 4)
        dd = (int(it["dhash"], 16) ^ qdh).bit_count()
        da = (int(it["ahash"], 16) ^ qah).bit_count()
        # 感知哈希随机图对的汉明距离约为 32，映射到 0 分起点
        s_ph = clamp01((1 - dp / 64.0 - 0.5) / 0.5)
        s_dh = clamp01((1 - dd / 64.0 - 0.5) / 0.5)
        s_ah = clamp01((1 - da / 64.0 - 0.5) / 0.5)
        rough = 0.60 * s_ph + 0.25 * s_dh + 0.15 * s_ah
        cand.append((rough, dp, dd, it))
    cand.sort(key=lambda x: x[0], reverse=True)
    k = max(120, min(600, top_n * 20))
    shortlist = cand[:k]
    t1 = time.time()

    # ---------- 阶段二：局部特征复核 ----------
    use_orb = use_orb and HAS_CV2
    qs = str(query)
    out = []
    for rough, dp, dd, it in shortlist:
        ncc = ncc_score(qs, it["path"])
        orb = orb_score(qs, it["path"]) if use_orb else None
        s_ph = clamp01((1 - dp / 64.0 - 0.5) / 0.5)
        s_dh = clamp01((1 - dd / 64.0 - 0.5) / 0.5)
        if orb is None:
            score = (0.58 * s_ph + 0.16 * s_dh + 0.26 * ncc) * 100.0
        else:
            # 几何校验（ORB+RANSAC）是"同一张图"最强的证据：
            # 哈希对裁剪/旋转不敏感地失效，此时由 orb 兜底抬分。
            base = 0.40 * s_ph + 0.13 * s_dh + 0.22 * ncc + 0.25 * orb
            geo = 0.85 * orb if orb >= 0.8 else 0.0
            score = max(base, geo) * 100.0
        score = clamp01(score / 100.0) * 100.0
        out.append({
            "score": round(score, 2),
            "phash_dist": dp,
            "dhash_dist": dd,
            "ncc": round(ncc, 4),
            "orb": None if orb is None else round(orb, 4),
            "file": Path(it["path"]).name,
            "path": it["path"],
            "w": it["w"],
            "h": it["h"],
            "size_kb": round(it["size"] / 1024),
            "rough": round(rough * 100, 2),
        })

    out.sort(key=lambda x: x["score"], reverse=True)
    if threshold > 0:
        out = [r for r in out if r["score"] >= threshold]
    out = out[:top_n]
    t2 = time.time()

    print(f"[i] 粗筛 {len(items)} 张耗时 {t1 - t0:.2f}s；复核 {len(shortlist)} 张耗时 {t2 - t1:.2f}s")
    return q, out


# --------------------------------------------------------------------------
# 输出
# --------------------------------------------------------------------------
def verdict(score: float) -> tuple:
    if score >= 88:
        return ("几乎确定是同一张", "#c0392b")
    if score >= 72:
        return ("很可能是同一张", "#d35400")
    if score >= 55:
        return ("内容相关，值得看一眼", "#b8860b")
    return ("相似度较低", "#7f8c8d")


def thumb_b64(path: str, width: int = 360) -> str:
    try:
        with _open_image(Path(path)) as im:
            im.thumbnail((width, width), Image.LANCZOS)
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=80)
            return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def write_report(query: Path, results: list, out_path: Path, folder: Path):
    q_b64 = thumb_b64(str(query), 640)
    cards = []
    for i, r in enumerate(results, 1):
        label, color = verdict(r["score"])
        img = thumb_b64(r["path"], 360)
        extra = f"ORB {int((r['orb'] or 0) * 100)}" if r["orb"] is not None else "ORB 未启用"
        cards.append(f"""
    <div class="card">
      <div class="rank">#{i}</div>
      <div class="thumb"><img src="{img}" alt=""></div>
      <div class="meta">
        <div class="score" style="color:{color}">{r['score']:.1f}<span>分</span></div>
        <div class="tag" style="background:{color}">{label}</div>
        <div class="name" title="{html.escape(r['path'])}">{html.escape(r['file'])}</div>
        <div class="kv">pHash 距离 {r['phash_dist']}/64 · NCC {r['ncc']:.2f} · {extra}</div>
        <div class="kv">{r['w']}×{r['h']} · {r['size_kb']} KB</div>
        <div class="path">{html.escape(r['path'])}</div>
      </div>
    </div>""")

    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<title>相似图检索结果</title>
<style>
  :root {{ --bg:#f7f8fa; --card:#fff; --line:#e5e7eb; --text:#1f2328; --muted:#6b7280; }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; padding:28px 32px 60px; background:var(--bg); color:var(--text);
         font-family:"Segoe UI","Microsoft YaHei",system-ui,sans-serif; }}
  h1 {{ font-size:20px; margin:0 0 4px; font-weight:600; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:22px; }}
  .query {{ display:flex; gap:18px; align-items:flex-start; background:var(--card);
            border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:26px; }}
  .query img {{ max-width:320px; max-height:240px; border-radius:8px; display:block; }}
  .query .info {{ font-size:13px; line-height:1.9; color:var(--muted); word-break:break-all; }}
  .query .info b {{ color:var(--text); font-weight:600; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fill,minmax(260px,1fr)); gap:16px; }}
  .card {{ background:var(--card); border:1px solid var(--line); border-radius:12px;
           overflow:hidden; position:relative; display:flex; flex-direction:column; }}
  .rank {{ position:absolute; top:10px; left:10px; background:rgba(0,0,0,.65); color:#fff;
           font-size:12px; padding:2px 8px; border-radius:20px; z-index:2; }}
  .thumb {{ height:190px; background:#eef1f4; display:flex; align-items:center; justify-content:center; }}
  .thumb img {{ max-width:100%; max-height:190px; object-fit:contain; display:block; }}
  .meta {{ padding:12px 14px 14px; border-top:1px solid var(--line); }}
  .score {{ font-size:26px; font-weight:700; line-height:1.1; }}
  .score span {{ font-size:12px; font-weight:400; color:var(--muted); margin-left:3px; }}
  .tag {{ display:inline-block; color:#fff; font-size:11px; padding:2px 8px;
          border-radius:4px; margin:6px 0 8px; }}
  .name {{ font-size:13px; font-weight:600; margin-bottom:5px;
           overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }}
  .kv {{ font-size:11.5px; color:var(--muted); line-height:1.7; }}
  .path {{ font-size:10.5px; color:#9ca3af; margin-top:6px; word-break:break-all; line-height:1.5; }}
</style></head><body>
  <h1>相似图检索结果</h1>
  <div class="sub">样图 · 在 {len(results)} 张结果中按综合相似度降序排列</div>
  <div class="query">
    <img src="{q_b64}" alt="query">
    <div class="info">
      <b>样图</b> {html.escape(query.name)}<br>
      <b>路径</b> {html.escape(str(query))}<br>
      <b>检索目录</b> {html.escape(str(folder))}<br>
      <b>生成时间</b> {time.strftime('%Y-%m-%d %H:%M:%S')}
    </div>
  </div>
  <div class="grid">{''.join(cards)}</div>
</body></html>"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(doc, encoding="utf-8")
    return out_path


def main():
    ap = argparse.ArgumentParser(description="在图片文件夹里查找与样图最相似的前 N 张")
    ap.add_argument("--folder", required=True, help="待检索的图片文件夹")
    ap.add_argument("--query", required=True, help="样图路径")
    ap.add_argument("--top", type=int, default=10, help="返回前 N 张，默认 10")
    ap.add_argument("--workers", type=int, default=min(8, (os.cpu_count() or 4)),
                    help="建索引线程数")
    ap.add_argument("--rebuild", action="store_true", help="忽略缓存，强制重建索引")
    ap.add_argument("--no-orb", action="store_true", help="关闭 ORB 复核，只用哈希（更快）")
    ap.add_argument("--threshold", type=float, default=0.0,
                    help="只保留分数不低于该值的结果，如 70")
    ap.add_argument("--copy-to", default="", help="把命中的前 N 张复制到该目录")
    ap.add_argument("--out", default="", help="HTML 报告输出路径")
    args = ap.parse_args()

    folder = Path(args.folder).expanduser()
    query = Path(args.query).expanduser()
    if not folder.is_dir():
        sys.exit(f"[x] 文件夹不存在：{folder}")
    if not query.is_file():
        sys.exit(f"[x] 样图不存在：{query}")

    t0 = time.time()
    q, results = search(folder, query, args.top, args.workers, args.rebuild,
                        not args.no_orb, args.threshold)
    if q is None:
        sys.exit("[x] 没有可用的索引")
    if not results:
        print("[!] 没有找到符合条件的结果（可降低 --threshold 或调大 --top）")
        return

    print()
    print(f"{'排名':<4}{'分数':<8}{'pHash':<7}{'NCC':<7}{'ORB':<7}文件")
    print("-" * 92)
    for i, r in enumerate(results, 1):
        orb = "-" if r["orb"] is None else f"{r['orb']:.2f}"
        print(f"{i:<6}{r['score']:<9.1f}{r['phash_dist']:<8}{r['ncc']:<8.2f}{orb:<8}{r['file']}")
    print("-" * 92)
    print(f"总耗时 {time.time() - t0:.1f}s")

    out = Path(args.out).expanduser() if args.out else (
        OUTPUT_DIR / f"report_{time.strftime('%Y%m%d_%H%M%S')}.html")
    write_report(query, results, out, folder)
    print(f"[√] HTML 报告：{out}")

    meta = out.with_suffix(".json")
    meta.write_text(json.dumps({"query": str(query), "folder": str(folder), "results": results},
                               ensure_ascii=False, indent=2), encoding="utf-8")

    if args.copy_to:
        dst = Path(args.copy_to).expanduser()
        dst.mkdir(parents=True, exist_ok=True)
        for i, r in enumerate(results, 1):
            src = Path(r["path"])
            target = dst / f"{i:02d}_{r['score']:.0f}_{src.name}"
            try:
                shutil.copy2(src, target)
            except Exception as e:
                print(f"[!] 复制失败 {src.name}: {e}")
        print(f"[√] 已复制 {len(results)} 张到：{dst}")


if __name__ == "__main__":
    main()
