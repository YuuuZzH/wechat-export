"""语音识别（ASR）模型文件的定位、服务与下载。

浏览器端用 transformers.js 跑 Whisper；模型文件（config/tokenizer + onnx 权重）
需要本地可访问：

  1) 优先用户目录 %LOCALAPPDATA%\\WeChatEXP\\models（可写，打包版也能下载）
  2) 其次程序内置的 src/web/static/models（打包时随 exe 一起分发，含三个模型的**小文件**）

onnx 权重（10~150MB）不适合塞进 exe，所以提供一键下载（默认 hf-mirror.com 国内镜像，
失败自动改用 HuggingFace 官方）。下载统一走 `http_download`：带浏览器 UA（反爬层会按 UA 拒绝，
issue #23）、支持断点续传、失败信息带 HTTP 状态码。
"""
import os

from .http_download import DownloadError, download_file  # noqa: F401  (导出便于测试/调用方)

# 每个文件的**真实字节数**（2026-10-08 用浏览器 UA 从 hf-mirror.com 实测）：
# 用于"还差多少 MB"的准确显示，以及下载后的大小校验。
_FILE_SIZES = {
    "Xenova/whisper-tiny": {
        "config.json": 2248,
        "generation_config.json": 3716,
        "preprocessor_config.json": 339,
        "tokenizer.json": 2480466,
        "tokenizer_config.json": 282683,
        "onnx/encoder_model_quantized.onnx": 10124910,
        "onnx/decoder_model_merged_quantized.onnx": 30727765,
    },
    "Xenova/whisper-base": {
        "config.json": 2248,
        "generation_config.json": 3776,
        "preprocessor_config.json": 339,
        "tokenizer.json": 2480466,
        "tokenizer_config.json": 282683,
        "onnx/encoder_model_quantized.onnx": 23200850,
        "onnx/decoder_model_merged_quantized.onnx": 53707539,
    },
    "Xenova/whisper-small": {
        "config.json": 2232,
        "generation_config.json": 3837,
        "preprocessor_config.json": 339,
        "tokenizer.json": 2480466,
        "tokenizer_config.json": 282683,
        "onnx/encoder_model_quantized.onnx": 92324809,
        "onnx/decoder_model_merged_quantized.onnx": 156780950,
    },
}

# 模型清单：相对 HuggingFace 仓库根的路径
ASR_MODELS = {
    "Xenova/whisper-tiny": {
        "label": "Whisper Tiny（最小最快，中文识别较粗）",
        "files": [
            "config.json",
            "generation_config.json",
            "preprocessor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "onnx/encoder_model_quantized.onnx",
            "onnx/decoder_model_merged_quantized.onnx",
        ],
        "sizeMb": 41.6,
        "recommended": False,
    },
    "Xenova/whisper-base": {
        "label": "Whisper Base（默认：体积与中文准确度均衡）",
        "files": [
            "config.json",
            "generation_config.json",
            "preprocessor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "onnx/encoder_model_quantized.onnx",
            "onnx/decoder_model_merged_quantized.onnx",
        ],
        "sizeMb": 76.0,
        "recommended": True,
    },
    "Xenova/whisper-small": {
        "label": "Whisper Small（中文更准，下载较大）",
        "files": [
            "config.json",
            "generation_config.json",
            "preprocessor_config.json",
            "tokenizer.json",
            "tokenizer_config.json",
            "onnx/encoder_model_quantized.onnx",
            "onnx/decoder_model_merged_quantized.onnx",
        ],
        "sizeMb": 240.2,
        "recommended": False,
    },
}
for _m, _spec in ASR_MODELS.items():
    _spec["sizeBytes"] = dict(_FILE_SIZES.get(_m, {}))

# 下载源：(显示名, base url)   —— 国内默认走 hf-mirror，HF 官方作为自动备选
MODEL_MIRRORS = [
    ("hf-mirror.com（国内镜像，推荐）", "https://hf-mirror.com"),
    ("HuggingFace 官方（需能访问外网）", "https://huggingface.co"),
]

DEFAULT_MODEL = "Xenova/whisper-base"
FALLBACK_MODEL = "Xenova/whisper-tiny"


def user_model_dir() -> str:
    """用户可写的模型目录（打包版也能往里下载）。"""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, "WeChatEXP", "models")


def bundled_model_dir() -> str:
    """随程序分发的模型目录（src/web/static/models 或 exe 解包目录）。"""
    import sys as _sys
    bundle = getattr(_sys, "_MEIPASS", None)
    if bundle:
        return os.path.join(bundle, "src", "web", "static", "models")
    here = os.path.dirname(os.path.abspath(__file__))          # engine/services
    src = os.path.dirname(os.path.dirname(here))               # src
    return os.path.join(src, "web", "static", "models")


def model_roots():
    """查找顺序：用户目录 → 内置目录。"""
    return [user_model_dir(), bundled_model_dir()]


def resolve_model_file_ex(model: str, relpath: str):
    """返回 (路径, 'user'|'bundled')；都没有返回 (None, None)。"""
    rel = relpath.replace("/", os.sep)
    for kind, root in (("user", user_model_dir()), ("bundled", bundled_model_dir())):
        p = os.path.join(root, model.replace("/", os.sep), rel)
        if os.path.isfile(p):
            return p, kind
    return None, None


def resolve_model_file(model: str, relpath: str):
    """按顺序返回第一个存在的模型文件路径；都不存在返回 None。"""
    return resolve_model_file_ex(model, relpath)[0]


def size_bytes(model: str, relpath: str) -> int:
    """该文件的已知字节数（未知返回 0）。"""
    return int(ASR_MODELS.get(model, {}).get("sizeBytes", {}).get(relpath, 0) or 0)


def model_complete(model: str = DEFAULT_MODEL) -> bool:
    """模型文件是否齐全（用户目录或内置目录任一命中即可）。"""
    spec = ASR_MODELS.get(model)
    if not spec:
        return False
    return all(resolve_model_file(model, rel) for rel in spec["files"])


def fallback_model_info(model: str = DEFAULT_MODEL) -> dict:
    """下载失败时建议改用的模型：优先挑一个**已经就绪**的最小模型。"""
    cands = [m for m in ASR_MODELS if m != model]
    cands.sort(key=lambda m: ASR_MODELS[m]["sizeMb"])
    ready = [m for m in cands if model_complete(m)]
    pick = (ready or cands or [model])[0]
    return {
        "model": pick,
        "label": ASR_MODELS.get(pick, {}).get("label", pick),
        "sizeMb": ASR_MODELS.get(pick, {}).get("sizeMb", 0),
        "ready": bool(ready),
        "reason": ("已就绪，可直接使用（无需下载）" if ready else
                   "仍需下载权重文件；也可稍后重试或换下载源"),
        "manualDir": user_model_dir(),
    }


def model_status(model: str = DEFAULT_MODEL) -> dict:
    """模型文件就绪情况（缺哪些、还差多少 MB、哪些是内置的、下载源）。"""
    spec = ASR_MODELS.get(model)
    if not spec:
        return {"error": "unknown_model", "model": model, "files": [], "complete": False}
    files = []
    for rel in spec["files"]:
        found, kind = resolve_model_file_ex(model, rel)
        size = os.path.getsize(found) if found else 0
        files.append({"name": rel, "exists": bool(found), "sizeMb": round(size / 1048576, 2),
                      "path": found or "", "bundled": kind == "bundled"})
    missing = [f["name"] for f in files if not f["exists"]]
    missing_bytes = sum(size_bytes(model, m) for m in missing)
    return {
        "model": model,
        "label": spec["label"],
        "files": files,
        "complete": not missing,
        "missing": missing,
        "missingBytes": missing_bytes,
        "missingMb": round(missing_bytes / 1048576, 2),
        "sizeMb": spec["sizeMb"],
        "userDir": user_model_dir(),
        "bundledDir": bundled_model_dir(),
        "mirrors": [{"name": n, "url": u} for n, u in MODEL_MIRRORS],
        "available": [{"model": m, "label": s["label"], "sizeMb": s["sizeMb"],
                       "recommended": bool(s.get("recommended")),
                       "complete": model_complete(m)}
                      for m, s in ASR_MODELS.items()],
        "fallback": fallback_model_info(model),
    }


def _source_order(base_url: str = None):
    """下载源顺序：用户指定的排最前，其余依次兜底（去重）。"""
    urls = [u for _n, u in MODEL_MIRRORS]
    if base_url:
        chosen = base_url.rstrip("/")
        urls = [chosen] + [u for u in urls if u.rstrip("/") != chosen]
    return urls


def download_model(model: str = DEFAULT_MODEL, base_url: str = None,
                   progress_fn=None, dest_root: str = None, retries: int = 2) -> dict:
    """把缺失的模型文件下载到用户模型目录。

    行为：
      * **已就绪就跳过**（用户目录或内置目录里有就不重复下载 —— 内置目录已随 exe 带三个模型的
        小文件，所以默认模型只需下载 onnx 权重）；
      * **多源自动回退**：某个源 403/超时/出错时自动换下一个源（而不是让用户自己猜）；
      * 每个源默认重试 `retries` 次；全部失败时抛 IOError，信息里含状态码与已尝试的源。

    progress_fn(msg, pct) 用于回传进度；返回 {downloaded, skipped, bytes, dir, mirror, used}。
    """
    if progress_fn is None:
        progress_fn = lambda msg, pct=0: None
    spec = ASR_MODELS.get(model)
    if not spec:
        raise ValueError("未知模型: " + str(model))
    sources = _source_order(base_url)
    root = os.path.join(dest_root or user_model_dir(), model.replace("/", os.sep))
    os.makedirs(root, exist_ok=True)

    downloaded = skipped = 0
    total_bytes = 0
    used = {}
    errors = []
    files = spec["files"]
    for idx, rel in enumerate(files):
        target = os.path.join(root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(target), exist_ok=True)
        existing = resolve_model_file(model, rel)
        if existing and os.path.abspath(existing) != os.path.abspath(target):
            skipped += 1
            progress_fn("已就绪，跳过 %s" % rel, (idx + 1) / float(len(files)))
            continue
        if os.path.isfile(target) and os.path.getsize(target) > 0:
            skipped += 1
            progress_fn("已存在，跳过 %s" % rel, (idx + 1) / float(len(files)))
            continue

        progress_fn("下载 %s …" % rel, idx / float(len(files)))
        got = None
        last_err = None
        for src in sources:
            url = "%s/%s/resolve/main/%s" % (src.rstrip("/"), model, rel)
            for _attempt in range(max(1, int(retries))):
                try:
                    n = download_file(url, target, timeout=180)
                    got = (src, n)
                    break
                except DownloadError as e:
                    last_err = e
                except Exception as e:                          # 网络中断等
                    last_err = e
            if got:
                break
            progress_fn("源 %s 不可用（%s），自动改用下一个源…" % (src, last_err),
                        idx / float(len(files)))
        if not got:
            errors.append("%s: %s" % (rel, last_err))
            continue
        src, n = got
        used[src] = used.get(src, 0) + 1
        total_bytes += n
        downloaded += 1
        progress_fn("完成 %s（%s）" % (rel, src), (idx + 1) / float(len(files)))

    if errors:
        raise IOError("下载失败：%s（已尝试 %s；可在界面换下载源，或手动把模型文件放到 %s）"
                      % ("; ".join(errors[:3]), "、".join(sources), user_model_dir()))

    return {"downloaded": downloaded, "skipped": skipped, "bytes": total_bytes,
            "dir": root, "mirror": (list(used)[0] if used else sources[0]),
            "sources": sources, "used": used}
