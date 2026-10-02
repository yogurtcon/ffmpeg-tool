# -*- coding: utf-8 -*-
"""
FFmpeg 测试素材生成工具 — Web 界面（Flask）。

运行（务必用「当前 python3」装依赖，避免 pip 与 python3 不是同一套解释器）：
  python3 -m pip install -r requirements.txt
  python3 app.py

浏览器打开 http://127.0.0.1:8765 或 http://localhost:8765

后台运行
不要日志、避免文件变大：
  nohup python3 app.py > /dev/null 2>&1 &
停止运行：
  pkill -f "python3 app.py"

说明：
- 生成结果暂存在项目目录下 generated/；约 30 分钟后自动删除（后台按修改时间扫描），请及时下载。
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

from flask import Flask, jsonify, render_template, request, send_file
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

import ffmpeg_builder as fb

BASE_DIR = Path(__file__).resolve().parent
SAMPLE_MEDIA_DIR = BASE_DIR / "samples"
app = Flask(__name__)
# 上传上限（超过时 Werkzeug 默认返回 HTML，需在 errorhandler 里改成 JSON 供前端解析）
app.config["MAX_CONTENT_LENGTH"] = 10 * 1024 * 1024 * 1024  # 10 GiB


@app.errorhandler(RequestEntityTooLarge)
def handle_request_entity_too_large(_e: RequestEntityTooLarge):
    max_mb = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return (
        jsonify(
            ok=False,
            log="",
            error=f"上传文件超过大小限制（当前上限约 {max_mb} MB），请压缩后重试。",
        ),
        413,
    )

# 服务器上生成的可下载文件保留时长（秒），超时按 mtime 删除，避免越积越多
OUTPUT_RETENTION_SEC = 30 * 60
_OUTPUT_MEDIA_SUFFIXES = frozenset({".mp4", ".mov", ".mp3", ".aac", ".m4a", ".flac", ".wav"})
_SAMPLE_MEDIA_SUFFIXES = frozenset({".mp3", ".aac", ".m4a", ".flac", ".wav", ".mp4", ".mov"})


def _list_sample_media() -> list[dict[str, object]]:
    """返回可在「无输入」状态选用的内置媒体，不暴露 samples/ 外的文件。"""
    if not SAMPLE_MEDIA_DIR.is_dir():
        return []

    grouped: dict[str, list[dict[str, str | float]]] = {}
    for path in sorted(SAMPLE_MEDIA_DIR.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _SAMPLE_MEDIA_SUFFIXES:
            continue
        relative_path = path.relative_to(SAMPLE_MEDIA_DIR)
        try:
            duration = fb.probe_duration_seconds(str(path))
        except Exception:  # noqa: BLE001
            duration = 0.0
        grouped.setdefault(str(relative_path.parent), []).append(
            {
                "filename": path.name,
                "path": relative_path.as_posix(),
                "duration": duration,
            }
        )
    return [
        {"category": category, "samples": grouped[category]}
        for category in sorted(grouped)
    ]


def _resolve_sample_media_path(relative_path: str) -> Path | None:
    """仅解析 samples/ 中允许格式的文件，防止表单参数造成路径穿越。"""
    if not relative_path or "\\" in relative_path:
        return None
    root = SAMPLE_MEDIA_DIR.resolve()
    path = (root / relative_path).resolve()
    try:
        path.relative_to(root)
    except ValueError:
        return None
    if not path.is_file() or path.suffix.lower() not in _SAMPLE_MEDIA_SUFFIXES:
        return None
    return path


def purge_stale_generated_files() -> None:
    """
    删除超过 OUTPUT_RETENTION_SEC 的生成音视频（按 mtime）：
    - 当前：generated/ 下各扩展名
    - 旧版：根目录下 test_* 命名（兼容曾写入项目根目录的文件）
    """
    now = time.time()
    base = BASE_DIR.resolve()
    gen = base / fb.GENERATED_DIRNAME
    if gen.is_dir():
        for p in gen.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() not in _OUTPUT_MEDIA_SUFFIXES:
                continue
            try:
                if now - p.stat().st_mtime > OUTPUT_RETENTION_SEC:
                    p.unlink(missing_ok=True)
            except OSError:
                pass
    for p in base.iterdir():
        if not p.is_file():
            continue
        if not p.name.startswith("test_"):
            continue
        if p.suffix.lower() not in _OUTPUT_MEDIA_SUFFIXES:
            continue
        try:
            if now - p.stat().st_mtime > OUTPUT_RETENTION_SEC:
                p.unlink(missing_ok=True)
        except OSError:
            pass


def _retention_cleanup_daemon() -> None:
    """周期性清理；守护线程，随进程退出而结束。"""
    while True:
        time.sleep(300)  # 每 5 分钟扫一次
        purge_stale_generated_files()


def start_retention_cleanup_background() -> None:
    purge_stale_generated_files()
    t = threading.Thread(target=_retention_cleanup_daemon, name="output-retention", daemon=True)
    t.start()


def _resolve_generated_download_path(name: str) -> Path | None:
    """
    仅允许下载 generated/ 下、扩展名在允许列表中的文件；防止路径穿越。
    返回解析后的绝对路径，非法名返回 None。
    """
    if not name or "/" in name or "\\" in name or ".." in name or name.startswith("."):
        return None
    root = (BASE_DIR / fb.GENERATED_DIRNAME).resolve()
    p = (BASE_DIR / fb.GENERATED_DIRNAME / name).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        return None
    if p.suffix.lower() not in _OUTPUT_MEDIA_SUFFIXES:
        return None
    return p


def _source_base_stem_for_request(source_mode: str, upload) -> str:
    """
    作品名首段：无输入为「无输入」；有上传为原文件主名（去后缀、安全化，保留中文）。
    """
    if source_mode != "local_file":
        return "无输入"
    if not upload or not upload.filename:
        return "无输入"
    # upload.filename 为 str，须用 Path(...).name 取 basename，不能写成 str(...).name
    stem = Path(str(upload.filename)).stem
    s = fb.sanitize_output_stem(stem)
    if not s or s.isspace():
        s = "upload"
    return s


@app.route("/")
def index() -> str:
    return render_template("index.html", sample_media=_list_sample_media())


@app.route("/api/generate", methods=["POST"])
def api_generate():
    source_mode = request.form.get("source_mode", "generator")
    # 兼容旧表单：本地上传统一为 local_file
    if source_mode in ("local_video", "local_audio"):
        source_mode = "local_file"
    target_kind = request.form.get("target_kind", "video")
    try:
        duration = float(request.form.get("duration", "0"))
    except ValueError:
        return jsonify(ok=False, log="", error="时长格式无效"), 400
    if duration <= 0:
        return jsonify(ok=False, log="", error="时长必须大于 0"), 400

    resolution = request.form.get("resolution", "1080p")
    container = request.form.get("container", ".mp4")
    orientation = request.form.get("orientation", "landscape")
    audio_format = request.form.get("audio_format", "MP3")
    try:
        input_repeat_count = int(request.form.get("input_repeat_count", "1"))
    except ValueError:
        return jsonify(ok=False, log="", error="重复次数格式无效"), 400
    if input_repeat_count < 1:
        return jsonify(ok=False, log="", error="重复次数必须大于等于 1"), 400

    upload = request.files.get("media_file")
    bundled_sample = request.form.get("bundled_sample", "")
    temp_path: str | None = None
    input_path: str | None = None
    log_lines: list[str] = []

    try:
        if source_mode == "local_file":
            if not upload or not upload.filename:
                return jsonify(ok=False, log="", error="请选择并上传本地媒体文件。"), 400
            raw_name = secure_filename(upload.filename)
            suffix = Path(raw_name).suffix or ".bin"
            fd, temp_path = tempfile.mkstemp(suffix=suffix)
            os.close(fd)
            upload.save(temp_path)
            input_path = temp_path
            log_lines.append("已保存上传文件到临时路径（处理完成后会删除）。")
            if input_repeat_count > 1:
                source_duration = fb.probe_duration_seconds(input_path)
                if source_duration <= 0:
                    return jsonify(ok=False, log="\n".join(log_lines), error="无法读取上传文件时长，不能按重复次数生成。"), 400
                repeated_duration = source_duration * input_repeat_count
                log_lines.append(
                    f"本地输入重复 {input_repeat_count} 次：源时长约 {source_duration:.3f} 秒，重复段约 {repeated_duration:.3f} 秒，目标时长 {duration:.3f} 秒。"
                )
                if duration > repeated_duration:
                    log_lines.append("目标时长超过重复段，超出部分将由工具生成内容补足。")
        else:
            input_repeat_count = 1

            # “无输入”且未选择内置素材时，默认使用单人.m4a，避免再生成默认音频。
            if not bundled_sample:
                bundled_sample = "单人声多人声/单人.m4a"

        source_stem = _source_base_stem_for_request(source_mode, upload)
        if bundled_sample:
            if source_mode != "generator":
                return jsonify(ok=False, log="", error="内置素材仅可在选择“无输入”时使用。"), 400
            sample_path = _resolve_sample_media_path(bundled_sample)
            if not sample_path:
                return jsonify(ok=False, log="", error="选择的内置素材不存在或格式不受支持。"), 400
            input_path = str(sample_path)
            source_mode = "local_file"
            source_stem = fb.sanitize_output_stem(sample_path.stem) or "内置素材"
            log_lines.append("使用内置素材：" + bundled_sample)

        br = fb.build_ffmpeg_command(
            source_mode=source_mode,
            target_kind=target_kind,
            input_path=input_path,
            target_duration=duration,
            source_base_stem=source_stem,
            resolution=resolution,
            container=container,
            orientation=orientation,
            audio_format=audio_format,
            input_repeat_count=input_repeat_count,
        )
        out_full = BASE_DIR / fb.GENERATED_DIRNAME / br.output_filename
        log_lines.append("输出目录：" + str(BASE_DIR))
        log_lines.append("输出文件：" + br.output_filename)
        log_lines.append("执行命令：" + subprocess.list2cmdline(br.argv))

        r = subprocess.run(
            br.argv,
            capture_output=True,
            text=True,
            timeout=3600,
        )
        if r.stdout:
            log_lines.append(r.stdout.strip())
        if r.stderr:
            log_lines.append(r.stderr.strip())

        if r.returncode == 0:
            purge_stale_generated_files()
            log_lines.append("生成成功。")
            log_lines.append(
                f"提示：该文件将在服务器上保留约 {OUTPUT_RETENTION_SEC // 60} 分钟，请及时下载；超时自动删除。"
            )
            dl = f"/api/download/{quote(br.output_filename, safe='')}"
            return jsonify(
                ok=True,
                log="\n".join(log_lines),
                output_filename=br.output_filename,
                download_url=dl,
            )
        log_lines.append(f"失败，退出码 {r.returncode}。")
        return jsonify(ok=False, log="\n".join(log_lines), error="FFmpeg 执行失败"), 200

    except Exception as ex:  # noqa: BLE001
        log_lines.append(f"异常：{ex!r}")
        return jsonify(ok=False, log="\n".join(log_lines), error=str(ex)), 200
    finally:
        if temp_path:
            try:
                Path(temp_path).unlink(missing_ok=True)
            except OSError:
                pass


@app.route("/api/download/<path:name>")
def api_download(name: str):
    path = _resolve_generated_download_path(name)
    if not path or not path.is_file():
        return "未找到文件", 404
    return send_file(path, as_attachment=True, download_name=name)


def main() -> None:
    start_retention_cleanup_background()
    # 0.0.0.0：局域网可用 192.168.x.x:8765；改代码后必须重启进程，否则会一直是旧的 127.0.0.1
    print("监听 0.0.0.0:8765 — 本机 http://127.0.0.1:8765 或 http://localhost:8765")
    print(f"生成文件保留 {OUTPUT_RETENTION_SEC // 60} 分钟后自动从服务器删除。")
    app.run(
        host="0.0.0.0",
        port=8765,
        debug=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
