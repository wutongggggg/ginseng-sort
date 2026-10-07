#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
人参素材按标签编号归类 全量管线
用法:
  python pipeline.py manifest            # 阶段A: 扫描所有媒体, 按时间排序生成 manifest.json
  python pipeline.py classify [--limit N] [--workers W]   # 阶段B: qwen 识别每项(断点续跑), 缓存 results.json
  python pipeline.py group [--dry-run]   # 阶段C: 归组到 日期_编号 文件夹 + 生成核对报告
"""
import os, sys, json, re, io, zlib, hashlib, base64, subprocess, csv, shutil, argparse
import concurrent.futures
from datetime import datetime, timezone, timedelta
from PIL import Image
import numpy as np
import rawpy

TOOL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCHEMA = {
    # 下面三个只是占位默认值，实际使用请在 config.json 里指定（见 config.example.json）
    "photo_dirs": ["/path/to/DCIM/100MSDCF", "/path/to/DCIM/101MSDCF"],
    "video_dir":  "/path/to/DCIM/CLIP",
    "out_root":   "/path/to/已分类",
    "cache_dir":  os.path.join(TOOL_DIR, "cache"),
    "model":      "qwen3.6:35b",
    "ollama_url": "http://127.0.0.1:11434/api/generate",
    "target_w":   1280,
    "workers":    4,
    "cluster_gap_sec": 60,          # 同一根标签事件聚类的最大间隔(秒)
    "photo_exts": [".arw", ".jpg", ".jpeg", ".png"],
    "video_exts": [".mp4", ".mov", ".mts"],
    # 跨平台：优先用 PATH 上的 ffmpeg/ffprobe（Windows 上会自动带 .exe）
    "ffmpeg":     shutil.which("ffmpeg") or "ffmpeg",
    "ffprobe":    shutil.which("ffprobe") or "ffprobe",
    "move":       False,                 # apply 默认: False=复制, True=移动(删除源)
    "use_cv2":    False,                 # refine 用 OpenCV 红边轮廓裁切标签卡(备选, 默认用白色掩码)
}
CONFIG = dict(SCHEMA)

def load_config(path=None):
    """从 config.json 覆盖默认配置(浅合并). 返回 CONFIG."""
    global CONFIG
    cfg = dict(SCHEMA)
    if path is None:
        path = os.environ.get("GSORT_CONFIG") or os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "..", "config.json")
    if path and os.path.exists(path):
        with open(path) as fp:
            cfg.update(json.load(fp))
    CONFIG = cfg
    # 保持旧名称兼容内部逻辑
    globals()["CACHE_DIR"] = CONFIG["cache_dir"]
    globals()["OUT_ROOT"] = CONFIG["out_root"]
    globals()["MODEL"] = CONFIG["model"]
    globals()["OLLAMA"] = CONFIG["ollama_url"]
    globals()["TARGET_W"] = CONFIG["target_w"]
    globals()["WORKERS"] = CONFIG["workers"]
    return CONFIG

CACHE_DIR  = CONFIG["cache_dir"]
OUT_ROOT   = CONFIG["out_root"]
OLLAMA     = CONFIG["ollama_url"]
MODEL      = CONFIG["model"]
TARGET_W   = CONFIG["target_w"]
WORKERS    = CONFIG["workers"]
FFMPEG     = CONFIG["ffmpeg"]
FFPROBE    = CONFIG["ffprobe"]
VLOG       = sys.stderr

def log(*a):
    print(*a, file=VLOG, flush=True)

# ---------- 阶段A: 元数据 / 时间 ----------
def photo_capture_time(path):
    ext = os.path.splitext(path)[1].lower()
    dt = None
    try:
        if ext == ".arw":
            with rawpy.imread(path) as raw:
                t = raw.extract_thumb()
                if t.format == rawpy.ThumbFormat.JPEG:
                    im = Image.open(io.BytesIO(t.data))
                else:
                    im = Image.fromarray(t.data)
        else:
            im = Image.open(path)
        exif = im.getexif()
        from PIL.ExifTags import TAGS
        for k, v in exif.items():
            if TAGS.get(k) == "DateTimeOriginal":
                dt = v; break
        if not dt:
            for k, v in exif.items():
                if TAGS.get(k) == "DateTime":
                    dt = v; break
    except Exception as e:
        log("   photo time ERR", os.path.basename(path), e)
    if dt:
        try:
            return datetime.strptime(dt.strip(), "%Y:%m:%d %H:%M:%S")
        except Exception:
            return None
    return None

def video_capture_time(path):
    try:
        out = subprocess.run([FFPROBE, "-v", "error",
                              "-show_entries", "format_tags=creation_time",
                              "-of", "default=noprint_wrappers=1:nokey=1", path],
                             capture_output=True, text=True, timeout=60).stdout.strip()
        if "T" in out or out.endswith("Z"):
            s = out.rstrip("Z").replace("T", " ")
            dt = datetime.strptime(s[:19], "%Y-%m-%d %H:%M:%S")
            if out.endswith("Z"):  # UTC -> 北京时间(+8)
                dt = dt.replace(tzinfo=timezone.utc).astimezone(timezone(timedelta(hours=8))).replace(tzinfo=None)
            return dt
    except Exception as e:
        log("   video time ERR", os.path.basename(path), e)
    return None

def build_manifest():
    items = []
    for d in CONFIG["photo_dirs"]:
        if not os.path.isdir(d): log("missing", d); continue
        for f in os.listdir(d):
            if f.lower().endswith(tuple(CONFIG["photo_exts"])):
                p = os.path.join(d, f)
                items.append((p, "photo", photo_capture_time(p)))
    vid = CONFIG["video_dir"]
    if vid and os.path.isdir(vid):
        for f in sorted(os.listdir(vid)):
            if f.lower().endswith(tuple(CONFIG["video_exts"])):
                p = os.path.join(vid, f)
                items.append((p, "video", video_capture_time(p)))
    known, unknown = [i for i in items if i[2]], [i for i in items if not i[2]]
    known.sort(key=lambda x: x[2])
    log(f"扫描完成: 有时间的 {len(known)}, 无时间的 {len(unknown)}")
    for p, kind, t in unknown:
        log("  无时间戳(将按文件名排序尾部处理):", kind, os.path.basename(p))
    mani = [{"path": p, "kind": kind,
             "time": t.strftime("%Y-%m-%d %H:%M:%S") if t else None,
             "name": os.path.basename(p)} for p, kind, t in known]
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, "manifest.json"), "w") as fp:
        json.dump(mani, fp, ensure_ascii=False, indent=1)
    log(f"manifest 写入 {len(mani)} 项")

# ---------- 阶段B: qwen 识别 ----------
PROMPT = (
    "看这张拍摄人参的素材图，回答：1) ginseng_body: 画面里有没有一根【挂着的棕黄色人参本体】(整根参根+须)? "
    "2) 若没有独立人参本体、主体是一张单独的手写标签卡(白卡红边)，则 type=label，并读第一行日期(MMDD如0903 或 YYYYMMDD如20260904)和第二行编号(可能画圈，只要数字)；"
    "若有人参本体或不是独立标签卡，type=ginseng 且 date/number=null。"
    '严格只输出JSON: {"ginseng_body": bool, "type":"label"|"ginseng", "date":"字符串或null", "number":"纯数字或null"}'
)

def video_frame_preview(path):
    d = subprocess.run([FFPROBE, "-v", "error", "-show_entries",
                        "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", path],
                       capture_output=True, text=True, timeout=60).stdout.strip()
    dur = float(d) if d and d.replace(".", "", 1).isdigit() else 2.0
    samples = [min(0.3, dur * 0.2), dur * 0.5]
    prep = []
    for ss in samples:
        out = os.path.join(CACHE_DIR, "_frame_" + str(abs(hash(path + str(ss))) % 10**8) + ".jpg")
        subprocess.run([FFMPEG, "-v", "error", "-ss", f"{ss:.2f}",
                        "-i", path, "-frames:v", "1", "-y", out], timeout=60, check=False)
        if os.path.exists(out):
            try:
                im = Image.open(out).convert("RGB")
                w,h = im.size
                if w > TARGET_W: im = im.resize((TARGET_W, int(h*TARGET_W/w)), Image.LANCZOS)
                buf = io.BytesIO(); im.save(buf, "JPEG", quality=90)
                prep.append(buf.getvalue())
            except Exception:
                pass
            finally:
                try: os.remove(out)
                except Exception: pass
    return prep

def prepare_photo(path):
    ext = os.path.splitext(path)[1].lower()
    if ext == ".arw":
        with rawpy.imread(path) as raw:
            t = raw.extract_thumb()
            if t.format == rawpy.ThumbFormat.JPEG:
                im = Image.open(io.BytesIO(t.data)).convert("RGB")
            else:
                im = Image.fromarray(t.data)
    else:
        im = Image.open(path).convert("RGB")
    w, h = im.size
    if w > TARGET_W: im = im.resize((TARGET_W, int(h*TARGET_W/w)), Image.LANCZOS)
    buf = io.BytesIO(); im.save(buf, "JPEG", quality=90)
    return [buf.getvalue()]

def qwen_call(img_bytes, prompt=None):
    b64 = base64.b64encode(img_bytes).decode("ascii")
    body = {"model": MODEL, "stream": False, "think": False, "format": "json",
            "prompt": prompt or PROMPT, "images": [b64]}
    req = urllib.request.Request(OLLAMA, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=200) as r:
        d = json.load(r)
    try:
        return json.loads(d.get("response") or "{}")
    except Exception:
        return {"raw": d.get("response", "")}

# ---------- 标签卡裁切重读(提高编号准确率) ----------
READ_CROP_PROMPT = (
    "下面是从摄影图中裁剪出的一张手写标签卡。读出第一行日期(MMDD如0903 或 YYYYMMDD如20260904)"
    "和第二行编号(可能画圈，只要数字，若两位数请完整读出，如 12、23)。"
    "若看不清或不是标签卡，date 和 number 都填 null。"
    '严格只输出JSON：{"date":"字符串或null","number":"纯数字或null"}'
)

def _white_mask(rgb):
    r, g, b = rgb[..., 0].astype(int), rgb[..., 1].astype(int), rgb[..., 2].astype(int)
    return (r > 150) & (g > 150) & (b > 120)

def _largest_blob_bbox(mask):
    import collections
    H, W = mask.shape
    visited = np.zeros((H, W), dtype=bool)
    best = None
    for (sy, sx) in np.argwhere(mask):
        if visited[sy, sx]:
            continue
        q = collections.deque([(sy, sx)]); visited[sy, sx] = True
        area = 0; ys = []; xs = []
        while q:
            y, x = q.popleft(); area += 1; ys.append(y); xs.append(x)
            for dy, dx in ((1,0),(-1,0),(0,1),(0,-1)):
                ny, nx = y+dy, x+dx
                if 0 <= ny < H and 0 <= nx < W and mask[ny,nx] and not visited[ny,nx]:
                    visited[ny,nx] = True; q.append((ny,nx))
        if best is None or area > best[0]:
            best = (area, min(ys), max(ys), min(xs), max(xs))
    return best

def _load_image_rgb(path):
    """加载图片为 RGB PIL 图 (ARW 用 rawpy 内置预览, 其它用 PIL)."""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".arw":
        with rawpy.imread(path) as raw:
            t = raw.extract_thumb()
            return (Image.open(io.BytesIO(t.data)) if t.format == rawpy.ThumbFormat.JPEG
                    else Image.fromarray(t.data)).convert("RGB")
    return Image.open(path).convert("RGB")

def card_crop(path, target_w=1100):
    """检测白色卡片区域(白色掩码+最大连通域), 裁切放大后返回 PIL 图; 失败返回 None."""
    try:
        im = _load_image_rgb(path)
    except Exception:
        return None
    W, H = im.size
    det_w = 640
    small = im.resize((det_w, int(H * det_w / W)))
    rgb = np.asarray(small)
    bb = _largest_blob_bbox(_white_mask(rgb))
    if bb is None:
        return None
    _, y0, y1, x0, x1 = bb
    scale = W / det_w
    X0, X1 = int(x0*scale), int(x1*scale)
    Y0, Y1 = int(y0*scale), int(y1*scale)
    mw, mh = int((X1-X0)*0.30), int((Y1-Y0)*0.30)
    X0 = max(0, X0-mw); Y0 = max(0, Y0-mh); X1 = min(W, X1+mw); Y1 = min(H, Y1+mh)
    crop = im.crop((X0, Y0, X1, Y1))
    cw, ch = crop.size
    if max(cw, ch) > target_w:
        s = target_w / max(cw, ch)
        crop = crop.resize((int(cw*s), int(ch*s)), Image.LANCZOS)
    return crop

def card_crop_cv2(path, target_w=1100):
    """备选: 用 OpenCV 检测标签卡红色边框轮廓定位卡片, 裁切放大. 失败返回 None.
    对"干净、正拍的红边标签卡"更稳; 依赖 opencv-python-headless."""
    try:
        import cv2
        im = _load_image_rgb(path)
    except Exception:
        return None
    W, H = im.size
    arr = np.array(im)[:, :, ::-1]          # RGB->BGR
    small = cv2.resize(arr, (1000, int(H * 1000 / W)))
    b, g, r = small[..., 0], small[..., 1], small[..., 2]
    red = ((r > 110) & (r > g * 1.5) & (r > b * 1.5)).astype(np.uint8) * 255
    cnts, _ = cv2.findContours(red, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    best = None
    for c in cnts:
        x, y, ww, hh = cv2.boundingRect(c); area = cv2.contourArea(c)
        if ww > 40 and hh > 30 and area > 800 and (best is None or area > best[0]):
            best = (area, x, y, ww, hh)
    if best is None:
        return None
    _, x, y, ww, hh = best
    sc = W / 1000
    X0, Y0 = int(x*sc), int(y*sc); X1, Y1 = int((x+ww)*sc), int((y+hh)*sc)
    mw, mh = int(ww*sc*0.25), int(hh*sc*0.25)
    X0 = max(0, X0-mw); Y0 = max(0, Y0-mh); X1 = min(W, X1+mw); Y1 = min(H, Y1+mh)
    crop = im.crop((X0, Y0, X1, Y1)); cw, ch = crop.size
    if max(cw, ch) > target_w:
        s = target_w / max(cw, ch)
        crop = crop.resize((int(cw*s), int(ch*s)), Image.LANCZOS)
    return crop

def refine_labels():
    """对识别为 label 的项, 裁切卡片放大后重读 date/number, 更新 results.json.
    安全规则: 仅当裁切读回合法数字才覆盖, 否则保留原读数, 避免误伤."""
    with open(os.path.join(CACHE_DIR, "manifest.json")) as fp:
        mani = json.load(fp)
    res_path = os.path.join(CACHE_DIR, "results.json")
    with open(res_path) as fp:
        results = json.load(fp)
    use_cv2 = CONFIG.get("use_cv2", False)
    crop_fn = card_crop_cv2 if use_cv2 else card_crop
    label_paths = [it["path"] for it in mani if results.get(it["path"], {}).get("type") == "label"]
    log(f"重读标签 {len(label_paths)} 个 (裁切方式: {'OpenCV' if use_cv2 else '白色掩码'})")
    def one(path):
        try:
            crop = crop_fn(path)
            if crop is None:
                return {"ok": False, "reason": "no-card"}
            buf = io.BytesIO(); crop.save(buf, "JPEG", quality=92)
            return {"ok": True, "read": qwen_call(buf.getvalue(), READ_CROP_PROMPT)}
        except Exception as e:
            return {"ok": False, "reason": str(e)}
    done = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=WORKERS) as ex:
        fut = {ex.submit(one, p): p for p in label_paths}
        for f in concurrent.futures.as_completed(fut):
            p = fut[f]; r = f.result()
            if r.get("ok"):
                rd = r["read"]
                # 安全规则: 裁切读回合法数字才覆盖, 否则保留原值
                num = parse_num(rd.get("number"))
                if num is not None:
                    results[p]["number"] = rd.get("number")
                    results[p]["date"] = rd.get("date")
                results[p]["crop_read"] = True
            done += 1
            if done % 15 == 0:
                log(f"  {done}/{len(label_paths)}")
                with open(res_path, "w") as fp:
                    json.dump(results, fp, ensure_ascii=False)
    with open(res_path, "w") as fp:
        json.dump(results, fp, ensure_ascii=False)
    log("标签重读完成")

def classify_item(item):
    path, kind = item["path"], item["kind"]
    try:
        if kind == "photo":
            imgs = prepare_photo(path)
        else:
            imgs = video_frame_preview(path)
        if not imgs:
            return {"type": "error", "reason": "no frames"}
        results = [qwen_call(im) for im in imgs]
        # 每帧缩并: ginseng_body=true 或 type=ginseng -> ginseng; 只有 label且无人参本体才算标签
        fins = []
        for r in results:
            t = r.get("type"); gb = r.get("ginseng_body")
            if t == "label" and not gb:
                fins.append({"type": "label", "date": r.get("date"), "number": r.get("number")})
            else:
                fins.append({"type": "ginseng", "date": None, "number": None})
        labels = [f for f in fins if f["type"] == "label"]
        gins   = [f for f in fins if f["type"] == "ginseng"]
        # 只有"标签帧严格多于人参帧"才当作标签(视频同质, 多数表决; 人参优先防误判)
        if len(labels) > len(gins):
            lab = labels[0]
            return {"type": "label", "date": lab.get("date"), "number": lab.get("number"),
                    "_frames": fins}
        return {"type": "ginseng", "date": None, "number": None, "_frames": fins}
    except urllib.error.HTTPError as e:
        return {"type": "error", "reason": f"http{e.code}"}
    except Exception as e:
        return {"type": "error", "reason": str(e)}

def classify(limit=None, workers=WORKERS):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, "manifest.json")) as fp:
        mani = json.load(fp)
    if limit: mani = mani[:limit]
    res_path = os.path.join(CACHE_DIR, "results.json")
    prog_path = os.path.join(CACHE_DIR, "progress.json")
    results = {}
    if os.path.exists(res_path):
        with open(res_path) as fp:
            results = json.load(fp)
    todo = [it for it in mani if it["path"] not in results]
    total_todo = len(todo)
    log(f"待识别 {total_todo} / 已缓存 {len(results)}")
    import time
    t0 = time.time()
    done = 0
    def write_progress():
        pct = (len(results) / len(mani)) * 100
        rate = done / max(time.time() - t0, 0.001)          # this-run items/sec
        eta = (total_todo - done) / max(rate, 0.0001)
        p = {"done_run": done, "todo": total_todo, "done_all": len(results),
             "total_all": len(mani), "pct": round(pct, 1), "rate": round(rate, 2),
             "eta_sec": round(eta), "elapsed_sec": round(time.time() - t0)}
        with open(prog_path, "w") as fp:
            json.dump(p, fp)
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
        fut = {ex.submit(classify_item, it): it for it in todo}
        for f in concurrent.futures.as_completed(fut):
            it = fut[f]
            try:
                results[it["path"]] = f.result()
            except Exception as e:
                results[it["path"]] = {"type": "error", "reason": str(e)}
            done += 1
            if done % 10 == 0:
                write_progress()
                log(f"[{done}/{total_todo}] 已完成 {len(results)}/{len(mani)}  ({len(results)/len(mani)*100:.0f}%)")
                with open(res_path, "w") as fp:
                    json.dump(results, fp, ensure_ascii=False)
    write_progress()
    with open(res_path, "w") as fp:
        json.dump(results, fp, ensure_ascii=False)
    err = [p for p, r in results.items() if r.get("type") == "error"]
    log(f"识别完成 {len(results)} 项, 错误 {len(err)}")

def progress():
    """渲染实时进度条: 读取 progress.json / results.json."""
    import time
    with open(os.path.join(CACHE_DIR, "manifest.json")) as fp:
        total = len(json.load(fp))
    res = {}
    rp = os.path.join(CACHE_DIR, "results.json")
    if os.path.exists(rp):
        with open(rp) as fp:
            res = json.load(fp)
    done = len(res)
    pp = os.path.join(CACHE_DIR, "progress.json")
    info = {}
    if os.path.exists(pp):
        with open(pp) as fp:
            info = json.load(fp)
    pct = done / total * 100
    bars = 30
    filled = int(bars * pct / 100)
    bar = "█" * filled + "░" * (bars - filled)
    eta = info.get("eta_sec")
    rate = info.get("rate")
    if eta:
        em, es = divmod(int(eta), 60)
        eta_s = f"{em}分{es:02d}秒"
    else:
        eta_s = "?"
    r_s = f"{rate:.1f}/s" if rate else "?"
    print(f"[{bar}] {pct:.1f}%  ({done}/{total})   速度 {r_s}   预计剩余 {eta_s}")

# ---------- 阶段C: 归组 ----------
def canonical_date(label_date, fallback_year):
    s = re.sub(r"\D", "", str(label_date or ""))
    if len(s) == 8:  return s
    if len(s) == 4:  return f"{fallback_year}{s}"
    return None

def parse_num(s):
    """从标签读出的 number 字段提取编号: 取最后一段合理的小整数.
    模型有时会把日期也放进 number(如 '20260903 2'), 取最后一段数字且落在合理范围."""
    groups = re.findall(r"\d+", str(s or ""))
    for g in reversed(groups):
        n = int(g)
        if 1 <= n <= 5000:
            return n
    return None

def group(dry_run=True):
    from collections import Counter
    import json
    def gap_sec(t1, t2):
        try:
            d1 = datetime.strptime(t1, "%Y-%m-%d %H:%M:%S")
            d2 = datetime.strptime(t2, "%Y-%m-%d %H:%M:%S")
            return abs((d2 - d1).total_seconds())
        except Exception:
            return 1e9
    with open(os.path.join(CACHE_DIR, "manifest.json")) as fp:
        mani = json.load(fp)
    with open(os.path.join(CACHE_DIR, "results.json")) as fp:
        results = json.load(fp)
    items = [it for it in mani if results.get(it["path"], {}).get("type") in ("label", "ginseng")]
    items.sort(key=lambda it: it["time"] or it["name"])
    # 1) 标签事件
    evs = []
    for it in items:
        r = results[it["path"]]
        if r.get("type") == "label":
            evs.append({"path": it["path"], "time": it["time"], "fdate": it["time"][:10],
                        "kind": it["kind"], "name": it["name"], "num": parse_num(r.get("number")),
                        "date": r.get("date"), "rnum": r.get("number")})
    evs.sort(key=lambda e: e["time"])
    # 2) 按时间聚类同一根的多个标签事件(60s 内; 同根标签几秒内, 相邻根相隔数分钟)
    clusters = []
    assignments = {}
    for e in evs:
        if clusters and gap_sec(clusters[-1]["time"], e["time"]) <= CONFIG["cluster_gap_sec"]:
            clusters[-1]["evs"].append(e); clusters[-1]["time"] = e["time"]
        else:
            clusters.append({"time": e["time"], "fdate": e["fdate"], "evs": [e]})
    # 3) 每簇取多数编号 (消掉孤例误读); 并把标签项归到所属根文件夹
    for c in clusters:
        valid = [e["num"] for e in c["evs"] if e["num"] is not None]
        if valid:
            c["num"] = Counter(valid).most_common(1)[0][0]; c["unread"] = False
        else:
            c["num"] = None; c["unread"] = True
        for e in c["evs"]:
            if c["num"] is not None:
                assignments[e["path"]] = f"{c['fdate']}_{c['num']:03d}"
            else:
                assignments[e["path"]] = f"跳号待定_{c['fdate']}_标签未读出"
    # 4) 逐项归类
    before_first = []; gaps = []
    def which_folder(it_time, it_path, it_name):
        prev = None; nxt = None
        for c in clusters:
            if c["time"] <= it_time:
                prev = c
            elif nxt is None:
                nxt = c
        if prev is None:
            before_first.append(it_name); return
        if prev["unread"]:
            assignments[it_path] = f"跳号待定_{prev['fdate']}_标签未读出"; return
        if (nxt is not None and (not nxt["unread"]) and nxt["num"] is not None
                and nxt["num"] > prev["num"] + 1):
            assignments[it_path] = f"跳号待定_{prev['fdate']}_{prev['num']}到{nxt['num']}"; return
        assignments[it_path] = f"{prev['fdate']}_{prev['num']:03d}"
    for it in items:
        if results[it["path"]].get("type") == "ginseng":
            which_folder(it["time"], it["path"], it["name"])
    # 跳号记录(相邻簇)
    for i in range(len(clusters) - 1):
        a, b = clusters[i], clusters[i + 1]
        if a.get("num") and b.get("num") and a["fdate"] == b["fdate"] and b["num"] > a["num"] + 1:
            gaps.append({"from": a["num"], "to": b["num"], "fdate": a["fdate"],
                         "note": f"跳号 {a['num']}->{b['num']}, 中间疑似还有{b['num']-a['num']-1}根无标签, 已放入待定文件夹"})
    # 5) 输出
    folder_map = {}
    for p, folder in assignments.items():
        folder_map.setdefault(folder, []).append(os.path.basename(p))
    log(f"\n=== 归组结果(干跑) === 总项 {len(items)} | 标签簇 {len(clusters)} | 真正跳号 {len(gaps)}")
    log(f"生成文件夹 {len(folder_map)} 个")
    for folder in sorted(folder_map):
        log(f"  {folder}: {len(folder_map[folder])} 个文件")
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(os.path.join(CACHE_DIR, "group_report.json"), "w") as fp:
        json.dump({"assignments": {k: v for k, v in assignments.items()},
                   "clusters": [{"num": c.get("num"), "unread": c["unread"], "fdate": c["fdate"],
                                 "items": [(e["name"], e["num"]) for e in c["evs"]]} for c in clusters],
                   "gaps": gaps, "before_first": before_first},
                  fp, ensure_ascii=False, indent=1)
    with open(os.path.join(CACHE_DIR, "核对报告.csv"), "w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["类别", "文件名", "所在文件夹", "拍摄日期", "标签读出日期", "标签读出编号", "说明"])
        for it in items:
            p, r = it["path"], results[p]
            folder = assignments.get(p, "未匹配")
            note = ""
            if r.get("type") == "label":
                note = str(r.get("date")) + " / " + str(r.get("number"))
            w.writerow([r.get("type"), it["name"], folder, it["time"][:10],
                        r.get("date"), r.get("number"), note])
        for g in gaps:
            w.writerow(["跳号", "-", "-", g["fdate"], "-", f"{g['from']}->{g['to']}", g["note"]])
        for b in before_first:
            w.writerow(["首个标签前", b, "未匹配", "-", "-", "-", "编号出现前的人参, 需人工"])
    log("报告写入", os.path.join(CACHE_DIR, "核对报告.csv"))

def apply(mode="move"):
    """根据 group_report.json 把文件移入/复制到 OUT_ROOT/<文件夹>/."""
    g = json.load(open(os.path.join(CACHE_DIR, "group_report.json")))
    assignments = g["assignments"]
    import shutil
    moved = copied = 0
    notfound = []
    for src, folder in assignments.items():
        if not os.path.exists(src):
            notfound.append(src); continue
        dest_dir = os.path.join(OUT_ROOT, folder)
        os.makedirs(dest_dir, exist_ok=True)
        dest = os.path.join(dest_dir, os.path.basename(src))
        if mode == "move":
            shutil.move(src, dest); moved += 1
        else:
            shutil.copy2(src, dest); copied += 1
    log(f"apply {mode}: 移动 {moved}, 复制 {copied}, 未找到 {len(notfound)}")
    if notfound:
        log("未找到(可能已移动):")
        for s in notfound[:20]: log("  ", s)

def init_config(path="config.json"):
    """生成默认 config.json 模板."""
    with open(path, "w") as fp:
        json.dump(SCHEMA, fp, ensure_ascii=False, indent=2)
    log("已生成配置模板:", os.path.abspath(path), "请编辑后运行 --config 指定")

def check_prereqs():
    """检查 ffmpeg / ollama / 本地识别所需的库."""
    import importlib.util
    ok = True
    for name in ("rawpy", "PIL", "numpy"):
        if importlib.util.find_spec(name) is None:
            ok = False; log(f"  ✗ 缺 Python 库: {name}")
    if importlib.util.find_spec("cv2") is None:
        log("  · opencv 未装(仅 --cv2 备选裁切需要, 默认不需要)")
    if not os.path.exists(FFMPEG): ok = False; log(f"  ✗ 缺 ffmpeg: {FFMPEG}")
    if not os.path.exists(FFPROBE): ok = False; log(f"  ✗ 缺 ffprobe: {FFPROBE}")
    try:
        import urllib.request
        urllib.request.urlopen(OLLAMA.replace("generate", "tags"), timeout=4)
    except Exception as e:
        ok = False; log(f"  ✗ ollama 未响应({OLLAMA}): {e}")
    log("预检:", "通过 ✓" if ok else "存在缺失 ✗")
    return ok

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="人参素材按标签编号归类(可复用工具)")
    ap.add_argument("stage", choices=["manifest", "classify", "group", "apply", "progress",
                                      "refine", "all", "init-config", "check"])
    ap.add_argument("--config", default=None, help="配置文件路径(JSON)")
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--workers", type=int, default=None)
    ap.add_argument("--mode", choices=["move", "copy"], default=None)
    ap.add_argument("--cv2", action="store_true", help="refine 用 OpenCV 红边裁切(备选)")
    a = ap.parse_args()
    load_config(a.config)
    if a.cv2:
        CONFIG["use_cv2"] = True
    import urllib.request, urllib.error
    if a.stage == "init-config": init_config()
    elif a.stage == "check": check_prereqs()
    elif a.stage == "progress": progress()
    else:
        w = a.workers if a.workers else WORKERS
        if a.stage == "manifest": build_manifest()
        elif a.stage == "classify": classify(a.limit, w)
        elif a.stage == "group": group(dry_run=True)
        elif a.stage == "refine": refine_labels()
        elif a.stage == "apply": apply(a.mode if a.mode else ("move" if CONFIG["move"] else "copy"))
        elif a.stage == "all":
            build_manifest(); classify(None, w)
            group(dry_run=True)
            apply(a.mode if a.mode else ("move" if CONFIG["move"] else "copy"))
            log("全流程完成")
