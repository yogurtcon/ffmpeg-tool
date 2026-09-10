# -*- coding: utf-8 -*-
"""
FFmpeg 命令构建模块：与 UI 解耦，仅负责根据参数生成 ffmpeg/ffprobe 参数列表。

说明：
- 依赖本机已安装 ffmpeg / ffprobe，且可在 PATH 中调用。
- 音频统一重采样为 48kHz 立体声，便于 concat 与编码器兼容。
- AAC 使用内置 aac 编码器；若本机构建了 libfdk_aac，可自行改 builder（部分 FFmpeg 构建不含 fdk）。
- 无输入及需合成视频片段时，默认由 aevalsrc 生成音轨，画面为 showwaves 音波图（与「音频转视频」一致）。
"""

from __future__ import annotations

import shutil
import subprocess
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

# ---------------------------------------------------------------------------
# 常量：采样率、帧率（测试素材用固定值即可）
# ---------------------------------------------------------------------------
SAMPLE_RATE = 48_000
AUDIO_CHANNELS = 2
VIDEO_FPS = 30

# ---------------------------------------------------------------------------
# 默认 lavfi 音源（音高缓慢摆动）；画面由 showwaves 从该音轨生成
# ---------------------------------------------------------------------------
def _default_lavfi_audio(duration_sec: Optional[float] = None) -> str:
    """duration_sec 为 None 时不写 duration，由外层 -t 或与视频 -shortest 对齐。"""
    expr = "0.2*sin(2*PI*(440+100*sin(2*PI*0.25*t))*t)"
    if duration_sec is not None:
        return f"aevalsrc={expr}:sample_rate={SAMPLE_RATE}:duration={duration_sec}"
    return f"aevalsrc={expr}:sample_rate={SAMPLE_RATE}"


def _showwaves_filter_single_audio_input(w: int, h: int) -> str:
    """输入 0 为音轨时：asplit → 一路 showwaves 得 [v]，一路原音 [aout]。"""
    return (
        f"[0:a]asplit=2[aw][aout];"
        f"[aw]showwaves=s={w}x{h}:mode=line:rate={VIDEO_FPS}:colors=0xFFFFFF|0x3366FF[v]"
    )


# 分辨率标签 -> (宽, 高) 横屏基准；竖屏时对调
RESOLUTION_MAP = {
    "4k": (3840, 2160),
    "2k": (2560, 1440),
    "1080p": (1920, 1080),
    "720p": (1280, 720),
}


@dataclass
class BuildResult:
    """单次构建结果：ffmpeg 参数列表与建议输出文件名（不含目录）。"""

    argv: List[str]
    output_filename: str


def find_ffmpeg() -> str:
    """返回 ffmpeg 可执行文件路径；找不到则返回 'ffmpeg'（由调用方处理失败）。"""
    p = shutil.which("ffmpeg")
    return p if p else "ffmpeg"


def find_ffprobe() -> str:
    """返回 ffprobe 可执行文件路径。"""
    p = shutil.which("ffprobe")
    return p if p else "ffprobe"


def probe_duration_seconds(path: str) -> float:
    """
    用 ffprobe 读取容器时长（秒）。
    解析失败或 N/A 时返回 0.0（由上层按「整段生成/补足」策略处理）。
    """
    exe = find_ffprobe()
    cmd = [
        exe,
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        path,
    ]
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60,
        )
        if r.returncode != 0:
            return 0.0
        s = (r.stdout or "").strip()
        if not s or s.upper() == "N/A":
            return 0.0
        return float(s)
    except (ValueError, subprocess.TimeoutExpired, OSError):
        return 0.0


def _dims_for_video(resolution_key: str, orientation: str) -> Tuple[int, int]:
    """根据分辨率与横竖屏返回 (width, height)。"""
    w, h = RESOLUTION_MAP[resolution_key]
    if orientation == "portrait":
        return h, w
    return w, h


def _video_output_ext(container: str) -> str:
    """容器下拉值 '.mp4' / '.mov' -> 文件扩展名。"""
    c = container.strip().lower()
    if c == ".mov":
        return "mov"
    return "mp4"


def _audio_mux_args_for_format(audio_format: str) -> List[str]:
    """部分封装需要显式指定 muxer（如 m4a -> ipod）。"""
    af = audio_format.upper().strip()
    if af == "M4A":
        return ["-f", "ipod"]
    return []


def _audio_codec_and_format(audio_format: str) -> Tuple[str, List[str], str]:
    """
    音频格式下拉 -> (编码器名, 额外输出参数列表, 文件扩展名)。
    audio_format: MP3, AAC, FLAC, m4a, wav
    """
    af = audio_format.upper().strip()
    if af == "MP3":
        return "libmp3lame", ["-q:a", "4"], "mp3"
    if af == "AAC":
        return "aac", ["-b:a", "192k"], "aac"
    if af == "FLAC":
        return "flac", [], "flac"
    if af == "M4A":
        return "aac", ["-b:a", "192k"], "m4a"
    if af == "WAV":
        return "pcm_s16le", [], "wav"
    raise ValueError(f"不支持的音频格式: {audio_format}")


def _output_args_for_video_container(container: str) -> List[str]:
    """视频封装：mp4 / mov 的 mux 相关参数。"""
    c = container.strip().lower()
    if c == ".mp4":
        return ["-movflags", "+faststart"]
    return []


def _sanitize_filename_part(s: str) -> str:
    """去掉文件名不安全字符。"""
    for ch in '\\/:*?"<>|':
        s = s.replace(ch, "_")
    return s.replace(" ", "_")


GENERATED_DIRNAME = "generated"
"""生成文件所在子目录名（与 app 中下载/清理一致）。"""


def get_generated_output_dir() -> Path:
    d = Path(__file__).resolve().parent / GENERATED_DIRNAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def sanitize_output_stem(s: str, max_len: int = 100) -> str:
    """
    作品名首段：上传文件名为去后缀的主名，或「无输入」。
    允许中文；过长时截断。
    """
    t = _sanitize_filename_part(s.strip() or "无输入")
    if not t:
        t = "无输入"
    if len(t) > max_len:
        t = t[:max_len]
    return t


def _filename_timestamp() -> str:
    """文件名末尾时间：yyyyMMddHHmmss，例 20260422164609。"""
    return datetime.now().strftime("%Y%m%d%H%M%S")


def _format_duration_for_name(duration_sec: float) -> str:
    """如 10s、10.5s。"""
    if duration_sec < 0:
        duration_sec = 0.0
    s = f"{duration_sec:.2f}".rstrip("0").rstrip(".")
    return f"{s}s"


def _normalized_repeat_count(input_repeat_count: int) -> int:
    """本地上传输入循环次数，至少为 1。"""
    return max(1, int(input_repeat_count))


def _input_args_for_local_file(input_path: str, input_repeat_count: int) -> List[str]:
    """本地文件输入参数；重复时用 demuxer 循环，后续再按目标时长截断。"""
    repeat = _normalized_repeat_count(input_repeat_count)
    if repeat <= 1:
        return ["-i", input_path]
    return ["-stream_loop", str(repeat - 1), "-i", input_path]


def make_output_basename_video(
    source_stem: str,
    resolution_key: str,
    orientation: str,
    container: str,
    target_duration: float,
) -> str:
    base = sanitize_output_stem(source_stem)
    ori_label = "横屏" if orientation == "landscape" else "竖屏"
    ext = _video_output_ext(container)
    res = _sanitize_filename_part(resolution_key)
    mid = f"{res}_{ori_label}_{_format_duration_for_name(target_duration)}"
    return f"{base}_{mid}_{_filename_timestamp()}.{ext}"


def make_output_basename_audio(source_stem: str, audio_format: str, target_duration: float) -> str:
    base = sanitize_output_stem(source_stem)
    _, _, ext = _audio_codec_and_format(audio_format)
    mid = _format_duration_for_name(target_duration)
    return f"{base}_{mid}_{_filename_timestamp()}.{ext}"


def build_ffmpeg_command(
    *,
    source_mode: str,
    target_kind: str,
    input_path: Optional[str],
    target_duration: float,
    source_base_stem: str = "无输入",
    # 视频专用
    resolution: str = "1080p",
    container: str = ".mp4",
    orientation: str = "landscape",
    # 音频专用
    audio_format: str = "MP3",
    # 本地上传专用
    input_repeat_count: int = 1,
) -> BuildResult:
    """
    根据 UI 状态构建 ffmpeg 命令。

    source_mode: 'generator' | 'local_file'；亦兼容旧值 'local_video' / 'local_audio'（视为本地上传）
    target_kind: 'video' | 'audio'
    input_path: 本地文件路径；generator 时为 None
    target_duration: 目标时长（秒），必须 > 0
    source_base_stem: 输出文件名首段，无输入时为「无输入」，有上传时为原文件名主名（无后缀）
    input_repeat_count: 本地上传输入循环次数；1 表示不循环
    """
    if target_duration <= 0:
        raise ValueError("目标时长必须大于 0")

    if source_mode in ("local_file", "local_video", "local_audio"):
        if not input_path:
            raise ValueError("本地上传需要 input_path")
        # 有视频轨 → 原「视频上传」流程；仅音频/无画 → 原「音频上传」流程
        source_mode = "local_video" if _has_video_stream_ffprobe(input_path) else "local_audio"

    out_dir = get_generated_output_dir()
    ffmpeg = find_ffmpeg()

    if target_kind == "audio":
        return _build_audio_output(
            ffmpeg=ffmpeg,
            out_dir=out_dir,
            source_mode=source_mode,
            input_path=input_path,
            target_duration=target_duration,
            audio_format=audio_format,
            source_stem=source_base_stem,
            input_repeat_count=input_repeat_count,
        )

    # target_kind == "video"
    return _build_video_output(
        ffmpeg=ffmpeg,
        out_dir=out_dir,
        source_mode=source_mode,
        input_path=input_path,
        target_duration=target_duration,
        resolution=resolution,
        container=container,
        orientation=orientation,
        source_stem=source_base_stem,
        input_repeat_count=input_repeat_count,
    )


def _build_audio_output(
    ffmpeg: str,
    out_dir: Path,
    source_mode: str,
    input_path: Optional[str],
    target_duration: float,
    audio_format: str,
    source_stem: str = "无输入",
    input_repeat_count: int = 1,
) -> BuildResult:
    codec, extra_a, _ext = _audio_codec_and_format(audio_format)
    basename = make_output_basename_audio(source_stem, audio_format, target_duration)
    out_path = out_dir / basename

    mux = _audio_mux_args_for_format(audio_format)

    if source_mode == "generator":
        lavfi = _default_lavfi_audio(target_duration)
        argv = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            lavfi,
            "-ac",
            str(AUDIO_CHANNELS),
            "-c:a",
            codec,
            *extra_a,
            "-t",
            str(target_duration),
            *mux,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    if source_mode == "local_video":
        assert input_path
        return _build_audio_from_video(
            ffmpeg=ffmpeg,
            out_path=out_path,
            basename=basename,
            input_path=input_path,
            target_duration=target_duration,
            audio_format=audio_format,
            codec=codec,
            extra_a=extra_a,
            mux=mux,
            input_repeat_count=input_repeat_count,
        )

    # 本地音频文件
    assert input_path
    di = probe_duration_seconds(input_path)
    if di <= 0:
        di = 0.0
    repeat = _normalized_repeat_count(input_repeat_count)
    source_duration = di * repeat if di > 0 else 0.0
    local_input_args = _input_args_for_local_file(input_path, repeat)

    if target_duration <= source_duration and source_duration > 0:
        # 直接截断到目标时长
        argv = [
            ffmpeg,
            "-y",
            *local_input_args,
            "-t",
            str(target_duration),
            "-ac",
            str(AUDIO_CHANNELS),
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            codec,
            *extra_a,
            *mux,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    if di <= 0:
        # 无法探测时长：整段按 lavfi 生成等长（读文件失败时与「无输入」类似，但这里仍有路径）
        # 退化为：仅用 lavfi 生成 target_duration（避免对坏文件无限等待）
        lavfi = _default_lavfi_audio(target_duration)
        argv = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            lavfi,
            "-ac",
            str(AUDIO_CHANNELS),
            "-c:a",
            codec,
            *extra_a,
            *mux,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    pad = target_duration - source_duration
    # 段1：输入音频；段2：补足生成音频；concat 前统一格式
    # [0:a] atrim + aformat -> a0; [1:a] -> a1; concat
    fc = (
        f"[0:a]atrim=0:{source_duration},asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:"
        f"channel_layouts=stereo[a0];"
        f"[1:a]asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:"
        f"channel_layouts=stereo[a1];"
        f"[a0][a1]concat=n=2:v=0:a=1[aout]"
    )
    lavfi = _default_lavfi_audio(pad)
    argv = [
        ffmpeg,
        "-y",
        *local_input_args,
        "-f",
        "lavfi",
        "-i",
        lavfi,
        "-filter_complex",
        fc,
        "-map",
        "[aout]",
        "-c:a",
        codec,
        *extra_a,
        *mux,
        str(out_path),
    ]
    return BuildResult(argv=argv, output_filename=basename)


def _build_audio_from_video(
    ffmpeg: str,
    out_path: Path,
    basename: str,
    input_path: str,
    target_duration: float,
    audio_format: str,
    codec: str,
    extra_a: List[str],
    mux: List[str],
    input_repeat_count: int = 1,
) -> BuildResult:
    """
    从本地视频文件抽取/重编码音频；支持截断与「正弦波」补足超长部分。
    若文件无音频轨，则整段输出为 lavfi 正弦波（便于测试流水线不中断）。
    """
    di = probe_duration_seconds(input_path)
    if di <= 0:
        di = 0.0
    repeat = _normalized_repeat_count(input_repeat_count)
    source_duration = di * repeat if di > 0 else 0.0
    local_input_args = _input_args_for_local_file(input_path, repeat)
    has_a = _has_audio_stream_ffprobe(input_path)

    if not has_a:
        lavfi = _default_lavfi_audio(target_duration)
        argv = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            lavfi,
            "-ac",
            str(AUDIO_CHANNELS),
            "-c:a",
            codec,
            *extra_a,
            *mux,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    if target_duration <= source_duration and source_duration > 0:
        argv = [
            ffmpeg,
            "-y",
            *local_input_args,
            "-t",
            str(target_duration),
            "-map",
            "0:a",
            "-vn",
            "-ac",
            str(AUDIO_CHANNELS),
            "-ar",
            str(SAMPLE_RATE),
            "-c:a",
            codec,
            *extra_a,
            *mux,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    if di <= 0:
        lavfi = _default_lavfi_audio(target_duration)
        argv = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            lavfi,
            "-ac",
            str(AUDIO_CHANNELS),
            "-c:a",
            codec,
            *extra_a,
            *mux,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    pad = target_duration - source_duration
    fc = (
        f"[0:a]atrim=0:{source_duration},asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:"
        f"channel_layouts=stereo[a0];"
        f"[1:a]asetpts=PTS-STARTPTS,aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:"
        f"channel_layouts=stereo[a1];"
        f"[a0][a1]concat=n=2:v=0:a=1[aout]"
    )
    lavfi = _default_lavfi_audio(pad)
    argv = [
        ffmpeg,
        "-y",
        *local_input_args,
        "-f",
        "lavfi",
        "-i",
        lavfi,
        "-filter_complex",
        fc,
        "-map",
        "[aout]",
        "-c:a",
        codec,
        *extra_a,
        *mux,
        str(out_path),
    ]
    return BuildResult(argv=argv, output_filename=basename)


def _build_video_output(
    ffmpeg: str,
    out_dir: Path,
    source_mode: str,
    input_path: Optional[str],
    target_duration: float,
    resolution: str,
    container: str,
    orientation: str,
    source_stem: str = "无输入",
    input_repeat_count: int = 1,
) -> BuildResult:
    w, h = _dims_for_video(resolution, orientation)
    basename = make_output_basename_video(
        source_stem, resolution, orientation, container, target_duration
    )
    out_path = out_dir / basename
    mux_extra = _output_args_for_video_container(container)

    if source_mode == "generator":
        # 与「音频转视频」相同：默认音轨 + showwaves 画面
        argv = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            _default_lavfi_audio(None),
            "-filter_complex",
            _showwaves_filter_single_audio_input(w, h),
            "-map",
            "[v]",
            "-map",
            "[aout]",
            "-t",
            str(target_duration),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            *mux_extra,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    if source_mode == "local_audio":
        # 音频 -> 波形视频：先构造长度为目标时长的音频流，再 showwaves
        assert input_path
        return _build_audio_to_waves_video(
            ffmpeg=ffmpeg,
            out_path=out_path,
            basename=basename,
            input_path=input_path,
            target_duration=target_duration,
            w=w,
            h=h,
            mux_extra=mux_extra,
            input_repeat_count=input_repeat_count,
        )

    # local_video -> video
    assert input_path
    di = probe_duration_seconds(input_path)
    if di <= 0:
        di = 0.0

    if di <= 0:
        # 无法探测：退化为音波图视频（与无输入一致）
        argv = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            _default_lavfi_audio(None),
            "-filter_complex",
            _showwaves_filter_single_audio_input(w, h),
            "-map",
            "[v]",
            "-map",
            "[aout]",
            "-t",
            str(target_duration),
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            *mux_extra,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    return _build_local_video_to_video(
        ffmpeg=ffmpeg,
        out_path=out_path,
        basename=basename,
        input_path=input_path,
        di=di,
        target_duration=target_duration,
        w=w,
        h=h,
        mux_extra=mux_extra,
        input_repeat_count=input_repeat_count,
    )


def _has_audio_stream_ffprobe(path: str) -> bool:
    """粗略判断是否有音频轨（用于选 filter 图）。"""
    exe = find_ffprobe()
    cmd = [
        exe,
        "-v",
        "error",
        "-select_streams",
        "a",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        return bool((r.stdout or "").strip())
    except OSError:
        return False


def _has_video_stream_ffprobe(path: str) -> bool:
    """判断是否有可解码视频轨；纯音频/无画则为 False。"""
    exe = find_ffprobe()
    cmd = [
        exe,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_entries",
        "stream=index",
        "-of",
        "csv=p=0",
        path,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return False
        return bool((r.stdout or "").strip())
    except OSError:
        return False


def _build_local_video_to_video(
    ffmpeg: str,
    out_path: Path,
    basename: str,
    input_path: str,
    di: float,
    target_duration: float,
    w: int,
    h: int,
    mux_extra: List[str],
    input_repeat_count: int = 1,
) -> BuildResult:
    has_a = _has_audio_stream_ffprobe(input_path)
    repeat = _normalized_repeat_count(input_repeat_count)
    source_duration = di * repeat if di > 0 else 0.0
    local_input_args = _input_args_for_local_file(input_path, repeat)

    scale_chain = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1"
    )

    if target_duration <= source_duration:
        # 音视频都要截断：此前只对 a 做了 atrim，视频未 trim 会整片重编码，与目标时长不符
        td = str(target_duration)
        vtrim = f"{scale_chain},trim=start=0:duration={td},setpts=PTS-STARTPTS[v]"
        if has_a:
            fc = (
                f"[0:v]{vtrim};"
                f"[0:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
                f"atrim=0:{td},asetpts=PTS-STARTPTS[a]"
            )
            maps = ["-map", "[v]", "-map", "[a]"]
        else:
            fc = (
                f"[0:v]{vtrim};"
                f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=0:{td},"
                f"asetpts=PTS-STARTPTS[a]"
            )
            maps = ["-map", "[v]", "-map", "[a]"]
        argv = [
            ffmpeg,
            "-y",
            *local_input_args,
            "-filter_complex",
            fc,
            *maps,
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            *mux_extra,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    pad = target_duration - source_duration
    a_pad = _default_lavfi_audio(pad)

    if has_a:
        # 输入 0=文件；1=补足用 lavfi 音轨 → showwaves 得到第二段画面
        fc = (
            f"[0:v]{scale_chain},trim=start=0:duration={source_duration},setpts=PTS-STARTPTS[v0];"
            f"[0:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
            f"atrim=0:{source_duration},asetpts=PTS-STARTPTS[a0];"
            f"[1:a]asplit=2[sws][a1];"
            f"[sws]showwaves=s={w}x{h}:mode=line:rate={VIDEO_FPS}:colors=0xFFFFFF|0x3366FF[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[v];"
            f"[a0][a1]concat=n=2:v=0:a=1[a]"
        )
        argv = [
            ffmpeg,
            "-y",
            *local_input_args,
            "-f",
            "lavfi",
            "-i",
            a_pad,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            *mux_extra,
            str(out_path),
        ]
    else:
        fc = (
            f"[0:v]{scale_chain},trim=start=0:duration={source_duration},setpts=PTS-STARTPTS[v0];"
            f"anullsrc=r={SAMPLE_RATE}:cl=stereo,atrim=0:{source_duration},asetpts=PTS-STARTPTS[a0];"
            f"[1:a]asplit=2[sws][a1];"
            f"[sws]showwaves=s={w}x{h}:mode=line:rate={VIDEO_FPS}:colors=0xFFFFFF|0x3366FF[v1];"
            f"[v0][v1]concat=n=2:v=1:a=0[v];"
            f"[a0][a1]concat=n=2:v=0:a=1[a]"
        )
        argv = [
            ffmpeg,
            "-y",
            *local_input_args,
            "-f",
            "lavfi",
            "-i",
            a_pad,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[a]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            *mux_extra,
            str(out_path),
        ]
    return BuildResult(argv=argv, output_filename=basename)


def _build_audio_to_waves_video(
    ffmpeg: str,
    out_path: Path,
    basename: str,
    input_path: str,
    target_duration: float,
    w: int,
    h: int,
    mux_extra: List[str],
    input_repeat_count: int = 1,
) -> BuildResult:
    """
    本地音频 -> 波形视频。
    先通过 filter_complex 得到长度 target_duration 的立体声，再 showwaves 生成视频并映射该音频。
    """
    di = probe_duration_seconds(input_path)
    if di <= 0:
        di = 0.0
    repeat = _normalized_repeat_count(input_repeat_count)
    source_duration = di * repeat if di > 0 else 0.0
    local_input_args = _input_args_for_local_file(input_path, repeat)

    if di <= 0:
        argv = [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            _default_lavfi_audio(target_duration),
            "-filter_complex",
            _showwaves_filter_single_audio_input(w, h),
            "-map",
            "[v]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            *mux_extra,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    if target_duration <= source_duration:
        fc = (
            f"[0:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
            f"atrim=0:{target_duration},asetpts=PTS-STARTPTS[at];"
            f"[at]asplit=2[aw][aout];"
            f"[aw]showwaves=s={w}x{h}:mode=line:rate={VIDEO_FPS}:colors=0xFFFFFF|0x3366FF[v]"
        )
        argv = [
            ffmpeg,
            "-y",
            *local_input_args,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[aout]",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-b:a",
            "192k",
            *mux_extra,
            str(out_path),
        ]
        return BuildResult(argv=argv, output_filename=basename)

    pad = target_duration - source_duration
    fc = (
        f"[0:a]aformat=sample_rates={SAMPLE_RATE}:channel_layouts=stereo,"
        f"atrim=0:{source_duration},asetpts=PTS-STARTPTS[a0];"
        f"[1:a]aformat=sample_fmts=fltp:sample_rates={SAMPLE_RATE}:"
        f"channel_layouts=stereo,asetpts=PTS-STARTPTS[a1];"
        f"[a0][a1]concat=n=2:v=0:a=1[am];"
        f"[am]asplit=2[aw][aout];"
        f"[aw]showwaves=s={w}x{h}:mode=line:rate={VIDEO_FPS}:colors=0xFFFFFF|0x3366FF[v]"
    )
    lavfi_pad = _default_lavfi_audio(pad)
    argv = [
        ffmpeg,
        "-y",
        *local_input_args,
        "-f",
        "lavfi",
        "-i",
        lavfi_pad,
        "-filter_complex",
        fc,
        "-map",
        "[v]",
        "-map",
        "[aout]",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-b:a",
        "192k",
        *mux_extra,
        str(out_path),
    ]
    return BuildResult(argv=argv, output_filename=basename)
