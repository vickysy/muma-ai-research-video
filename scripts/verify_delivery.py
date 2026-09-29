#!/usr/bin/env python3
# coding: utf-8
"""Read-only checks for a narrated video delivery; writes only --report."""
import argparse
import json
import math
from pathlib import Path
import re
import shutil
import subprocess
import sys

STAMP = re.compile(r"^(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})\s+-->\s+(\d{2,}):([0-5]\d):([0-5]\d),(\d{3})$")

def seconds(values):
    h, m, s, ms = map(int, values)
    return h * 3600 + m * 60 + s + ms / 1000

def probe(path):
    if not path.is_file():
        raise ValueError(f"File not found: {path}")
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True, text=True, timeout=60,
    )
    if r.returncode:
        raise ValueError(f"ffprobe failed: {path}: {r.stderr[-1000:]}")
    return json.loads(r.stdout)

def check_srt(path, duration):
    text = path.read_text(encoding="utf-8-sig").strip()
    if not text:
        raise ValueError("SRT is empty")
    count, previous_end = 0, -1.0
    for block in re.split(r"\r?\n\s*\r?\n", text):
        lines = block.splitlines()
        if len(lines) < 3 or not lines[0].strip().isdigit():
            raise ValueError(f"Malformed SRT cue near: {block[:80]}")
        match = STAMP.fullmatch(lines[1].strip())
        if not match:
            raise ValueError(f"Malformed SRT timestamp: {lines[1]}")
        start, end = seconds(match.groups()[:4]), seconds(match.groups()[4:])
        if start < 0 or end <= start:
            raise ValueError(f"Invalid SRT interval: {start} --> {end}")
        if start < previous_end - 0.005:
            raise ValueError(f"SRT cues overlap or run backwards: {start} < {previous_end}")
        if end > duration + 0.05:
            raise ValueError(f"SRT ends outside video: {end} > {duration}")
        if not "\n".join(lines[2:]).strip():
            raise ValueError("SRT cue has no text")
        count += 1
        previous_end = end
    return {"file": str(path), "cues": count, "last_end_seconds": previous_end,
            "alignment_scope": "timestamps only; speech alignment requires review"}

def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--video", type=Path, required=True)
    ap.add_argument("--srt", type=Path)
    ap.add_argument("--portrait", type=Path)
    ap.add_argument("--landscape", type=Path)
    ap.add_argument("--full-decode", action="store_true")
    ap.add_argument("--report", type=Path, required=True)
    args = ap.parse_args()
    inputs = [p.resolve() for p in [args.video, args.srt, args.portrait, args.landscape] if p]
    if args.report.resolve() in inputs:
        ap.error("--report must not overwrite an input")
    result = {"errors": [], "warnings": [],
              "full_decode": "not_requested", "visual_review": "not_performed_by_script",
              "listening_review": "not_performed_by_script"}
    duration = None
    if not shutil.which("ffprobe"):
        result["errors"].append("ffprobe is not installed")
    else:
        try:
            data = probe(args.video)
            streams = data.get("streams", [])
            video = next((s for s in streams if s.get("codec_type") == "video"), None)
            audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
            if not video or not audio:
                raise ValueError("Narrated delivery must contain video and audio streams")
            duration = float(data["format"]["duration"])
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError("Invalid duration")
            result["video"] = {
                "file": str(args.video.resolve()), "duration_seconds": duration,
                "width": video["width"], "height": video["height"],
                "fps": video.get("avg_frame_rate"), "video_codec": video["codec_name"],
                "audio_codec": audio["codec_name"], "bytes": args.video.stat().st_size,
            }
            if video["codec_name"] != "h264" or audio["codec_name"] != "aac":
                result["warnings"].append("Codec differs from default H.264/AAC; verify target platform")
            if (video["width"], video["height"]) != (1920, 1080):
                result["warnings"].append("Dimensions differ from default 1920x1080; check current brief")
        except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
            result["errors"].append(str(exc))
        for key, path, rw, rh in [
            ("portrait", args.portrait, 3, 4),
            ("landscape", args.landscape, 4, 3),
        ]:
            if not path:
                continue
            try:
                data = probe(path)
                stream = next((s for s in data["streams"] if s.get("codec_type") == "video"), None)
                if not stream:
                    raise ValueError(f"No image stream in {path}")
                w, h = stream["width"], stream["height"]
                result[key] = {"file": str(path.resolve()), "width": w, "height": h,
                               "expected_ratio": f"{rw}:{rh}"}
                if w * rh != h * rw:
                    raise ValueError(f"{key} ratio mismatch: {w}x{h}, expected {rw}:{rh}")
            except (ValueError, KeyError, OSError, subprocess.SubprocessError) as exc:
                result["errors"].append(str(exc))
    if args.srt:
        if duration is None:
            result["errors"].append("SRT cannot be checked without valid video duration")
        else:
            try:
                result["subtitles"] = check_srt(args.srt, duration)
            except (ValueError, OSError) as exc:
                result["errors"].append(str(exc))
    if args.full_decode:
        if not shutil.which("ffmpeg"):
            result["errors"].append("ffmpeg is not installed")
            result["full_decode"] = "unavailable"
        elif duration is not None:
            try:
                decoded = subprocess.run(
                    ["ffmpeg", "-nostdin", "-v", "error", "-i", str(args.video),
                     "-map", "0:v:0", "-map", "0:a:0", "-f", "null", "-"],
                    capture_output=True, text=True, timeout=max(120, duration * 5),
                )
                if decoded.returncode or decoded.stderr.strip():
                    result["full_decode"] = "failed"
                    result["errors"].append("Full decode failed: " + decoded.stderr[-2000:])
                else:
                    result["full_decode"] = "passed"
            except (OSError, subprocess.SubprocessError) as exc:
                result["full_decode"] = "failed"
                result["errors"].append(str(exc))
        else:
            result["full_decode"] = "not_run_invalid_video"
    result["technical_checks_passed"] = not result["errors"]
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["technical_checks_passed"] else 1

if __name__ == "__main__":
    sys.exit(main())

