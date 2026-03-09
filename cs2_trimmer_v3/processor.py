"""
CS2 Kill Clip Processor — V3 (final with stretch option)
"""
import os
import uuid
import shutil
import subprocess
import cv2
import numpy as np
import logging
import glob

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# ffmpeg / ffprobe path resolution
# ─────────────────────────────────────────────────────────────────────────────
def _find_binary(name: str) -> str:
    import shutil as _sh
    found = _sh.which(name)
    if found:
        return found
    exe = name + ".exe"
    candidates = [
        rf"C:\ffmpeg\bin\{exe}",
        rf"C:\Program Files\ffmpeg\bin\{exe}",
        rf"C:\Program Files (x86)\ffmpeg\bin\{exe}",
        os.path.expanduser(rf"~\ffmpeg\bin\{exe}"),
        os.path.expanduser(rf"~\scoop\shims\{exe}"),
        os.path.expanduser(rf"~\scoop\apps\ffmpeg\current\bin\{exe}"),
        rf"C:\ProgramData\chocolatey\bin\{exe}",
        rf"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\{exe}",
    ]
    winget_base = os.path.expanduser(r"~\AppData\Local\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_base):
        for pattern in [
            os.path.join(winget_base, "Gyan.FFmpeg*", "ffmpeg-*", "bin", exe),
            os.path.join(winget_base, "Gyan.FFmpeg*", "*", "bin", exe),
        ]:
            matches = sorted(glob.glob(pattern))
            if matches:
                candidates.insert(0, matches[-1])
    for c in candidates:
        if os.path.isfile(c):
            return c
    return name


FFMPEG_BIN  = _find_binary("ffmpeg")
FFPROBE_BIN = _find_binary("ffprobe")


def check_ffmpeg() -> tuple[bool, str]:
    try:
        r = subprocess.run([FFMPEG_BIN, "-version"],
                           capture_output=True, text=True, timeout=10)
        if r.returncode == 0:
            return True, r.stdout.split("\n")[0]
        return False, f"ffmpeg rc={r.returncode}"
    except FileNotFoundError:
        return False, (
            f"ffmpeg not found (tried: {FFMPEG_BIN}). "
            "Install from https://ffmpeg.org/download.html and add to PATH."
        )
    except Exception as e:
        return False, str(e)


# ─────────────────────────────────────────────────────────────────────────────
# Kill feed ROI constants
# ─────────────────────────────────────────────────────────────────────────────
KF_LEFT   = 0.83
KF_TOP    = 0.01
KF_RIGHT  = 0.995
KF_BOTTOM = 0.22

# Strict red mask (opaque border)
RED_LO_1 = np.array([0,   140,  60], dtype=np.uint8)
RED_HI_1 = np.array([10,  255, 245], dtype=np.uint8)
RED_LO_2 = np.array([170, 140,  60], dtype=np.uint8)
RED_HI_2 = np.array([180, 255, 245], dtype=np.uint8)

# Fade red mask (semi‑transparent, for lock extension)
FADE_LO_1 = np.array([0,   40, 40], dtype=np.uint8)
FADE_HI_1 = np.array([10, 255, 255], dtype=np.uint8)
FADE_LO_2 = np.array([170, 40, 40], dtype=np.uint8)
FADE_HI_2 = np.array([180, 255, 255], dtype=np.uint8)


# ─────────────────────────────────────────────────────────────────────────────
# Detection thresholds
# ─────────────────────────────────────────────────────────────────────────────
MIN_ROW_COVERAGE   = 0.25
MIN_ROW_RED_PX     = 8
MAX_BORDER_HEIGHT  = 40
MIN_BORDER_HEIGHT  = 14

FADE_ROW_COVERAGE  = 0.05
FADE_LOCK_EXTEND   = 2.0

ROI_BRIGHTNESS_MIN = 12.0
ROW_LOCK_SECONDS   = 8.0
ROW_LOCK_OVERLAP   = MAX_BORDER_HEIGHT   # 40 px
SAMPLE_INTERVAL_S  = 0.10

# Death‑flash sample area (bottom‑right corner)
DEATH_FLASH_BR_X      = 0.80
DEATH_FLASH_BR_Y      = 0.80
DEATH_FLASH_THRESHOLD = 0.10   # >10% red pixels → death flash


# ─────────────────────────────────────────────────────────────────────────────
# Color mask & border detection
# ─────────────────────────────────────────────────────────────────────────────
def _red_mask(roi_bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    return cv2.bitwise_or(
        cv2.inRange(hsv, RED_LO_1, RED_HI_1),
        cv2.inRange(hsv, RED_LO_2, RED_HI_2),
    )


def _fade_mask(roi_bgr: np.ndarray) -> np.ndarray:
    hsv = cv2.cvtColor(roi_bgr, cv2.COLOR_BGR2HSV)
    return cv2.bitwise_or(
        cv2.inRange(hsv, FADE_LO_1, FADE_HI_1),
        cv2.inRange(hsv, FADE_LO_2, FADE_HI_2),
    )


def _find_border_boxes(roi_bgr: np.ndarray) -> list[tuple[int, int]]:
    mask = _red_mask(roi_bgr)
    roi_w = roi_bgr.shape[1]
    min_px = max(MIN_ROW_RED_PX, roi_w * MIN_ROW_COVERAGE)

    row_counts = (mask.sum(axis=1) / 255).astype(float)
    edge_rows = np.where(row_counts >= min_px)[0].tolist()

    if len(edge_rows) < 2:
        return []

    # Cluster consecutive edge rows (border lines may be 1‑3 px thick)
    clusters: list[list[int]] = []
    for r in edge_rows:
        if clusters and r - clusters[-1][-1] <= 3:
            clusters[-1].append(r)
        else:
            clusters.append([r])

    mids = [int(np.mean(c)) for c in clusters]

    # Pair clusters into (top, bottom) of each kill box
    boxes: list[tuple[int, int]] = []
    used = set()
    for i, top in enumerate(mids):
        if i in used:
            continue
        for j in range(i + 1, len(mids)):
            if j in used:
                continue
            bot = mids[j]
            gap = bot - top
            if MIN_BORDER_HEIGHT <= gap <= MAX_BORDER_HEIGHT:
                boxes.append((top, bot))
                used.add(i)
                used.add(j)
                break
    return boxes


# ─────────────────────────────────────────────────────────────────────────────
# Frame quality gates
# ─────────────────────────────────────────────────────────────────────────────
def _roi_has_content(roi: np.ndarray) -> bool:
    return float(roi.mean()) >= ROI_BRIGHTNESS_MIN


def _is_death_flash(frame: np.ndarray) -> bool:
    h, w = frame.shape[:2]
    x1 = int(w * DEATH_FLASH_BR_X)
    y1 = int(h * DEATH_FLASH_BR_Y)
    patch = frame[y1:h, x1:w]
    mask = _red_mask(patch)
    red_fraction = (mask.sum() / 255) / max(1, patch.shape[0] * patch.shape[1])
    return red_fraction > DEATH_FLASH_THRESHOLD


def _has_fade_red_near_row(roi_bgr: np.ndarray, center_px: float) -> bool:
    mask = _fade_mask(roi_bgr)
    roi_h = roi_bgr.shape[0]
    roi_w = roi_bgr.shape[1]
    half = MAX_BORDER_HEIGHT // 2 + 4
    y1 = max(0, int(center_px) - half)
    y2 = min(roi_h, int(center_px) + half)
    if y2 <= y1:
        return False
    band = mask[y1:y2, :]
    row_px = (band.sum(axis=1) / 255).astype(float)
    min_px = max(2, roi_w * FADE_ROW_COVERAGE)
    return bool(row_px.max() >= min_px)


# ─────────────────────────────────────────────────────────────────────────────
# Main detection loop (with hard expiry)
# ─────────────────────────────────────────────────────────────────────────────
def extract_kill_timestamps(video_path: str, progress_callback=None) -> list[float]:
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"Cannot open: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = n_frames / fps if fps > 0 else 0.0
    step = max(1, int(fps * SAMPLE_INTERVAL_S))

    if progress_callback:
        progress_callback(
            f"Scanning {duration:.1f}s @ {fps:.0f}fps  "
            f"(sample every {SAMPLE_INTERVAL_S}s)"
        )

    kills: list[float] = []
    HARD_LOCK_MAX = ROW_LOCK_SECONDS + 4.0   # at most ~12s from first detection
    locked_rows: list[list] = []   # each element: [center_px, unlock_time, hard_expiry]
    skipped_dark = 0
    frame_idx = 0

    def _expire_locks(ts: float):
        locked_rows[:] = [lr for lr in locked_rows if ts < lr[1]]

    def _find_lock(center: float) -> int:
        for i, lr in enumerate(locked_rows):
            if abs(lr[0] - center) <= ROW_LOCK_OVERLAP:
                return i
        return -1

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % step == 0:
            h, w = frame.shape[:2]
            ts = frame_idx / fps
            _expire_locks(ts)

            x1 = int(w * KF_LEFT); x2 = int(w * KF_RIGHT)
            y1 = int(h * KF_TOP);  y2 = int(h * KF_BOTTOM)
            roi = frame[y1:y2, x1:x2]

            if not _roi_has_content(roi):
                skipped_dark += 1
                frame_idx += 1
                continue

            boxes = _find_border_boxes(roi)
            death_flash = _is_death_flash(frame)
            if death_flash and progress_callback:
                progress_callback(f"  [SKIP] t={ts:.2f}s  death‑flash detected")

            # --- Fade refresh (capped by hard expiry) ---
            for lr in locked_rows:
                if _has_fade_red_near_row(roi, lr[0]):
                    # Extend unlock time, but never past hard_expiry
                    lr[1] = min(lr[2], max(lr[1], ts + FADE_LOCK_EXTEND))

            # --- New kill detection (skip during death flash) ---
            if not death_flash:
                for top, bot in boxes:
                    center = (top + bot) / 2.0
                    lock_idx = _find_lock(center)
                    if lock_idx >= 0:
                        # Update existing lock's center (drift)
                        locked_rows[lock_idx][0] = center
                    else:
                        kills.append(ts)
                        hard = ts + HARD_LOCK_MAX
                        locked_rows.append([center, ts + ROW_LOCK_SECONDS, hard])
                        if progress_callback:
                            progress_callback(
                                f"  [KILL] t={ts:.2f}s  "
                                f"border=({top},{bot})  "
                                f"total={len(kills)}"
                            )

        frame_idx += 1

    cap.release()
    kills.sort()

    if progress_callback:
        if skipped_dark:
            progress_callback(f"  Skipped: {skipped_dark} dark‑ROI frames")
        progress_callback(f"Scan complete — {len(kills)} kill(s) found.")
    return kills


# ─────────────────────────────────────────────────────────────────────────────
# Segment building
# ─────────────────────────────────────────────────────────────────────────────
def build_segments(
    timestamps: list[float],
    n_before:   float,
    n_after:    float,
    duration:   float,
    full_span:  bool = False,
) -> list[tuple[float, float]]:
    if not timestamps:
        return []

    if full_span:
        return [(
            max(0.0, min(timestamps) - n_before),
            min(duration, max(timestamps) + n_after),
        )]

    ENGAGEMENT_GAP_S = 15.0
    sorted_ts = sorted(timestamps)
    groups: list[list[float]] = [[sorted_ts[0]]]
    for t in sorted_ts[1:]:
        if t - groups[-1][-1] <= ENGAGEMENT_GAP_S:
            groups[-1].append(t)
        else:
            groups.append([t])

    segs = [
        (
            max(0.0, min(g) - n_before),
            min(duration, max(g) + n_after),
        )
        for g in groups
    ]
    return segs


# ─────────────────────────────────────────────────────────────────────────────
# Video utilities
# ─────────────────────────────────────────────────────────────────────────────
def get_video_duration(video_path: str) -> float:
    try:
        r = subprocess.run(
            [FFPROBE_BIN, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", video_path],
            capture_output=True, text=True, timeout=30
        )
        return float(r.stdout.strip())
    except Exception:
        pass
    cap = cv2.VideoCapture(video_path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    frames = cap.get(cv2.CAP_PROP_FRAME_COUNT)
    cap.release()
    return frames / fps if fps else 0.0


def _ffmpeg(cmd: list[str], label: str, cb=None) -> bool:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except FileNotFoundError:
        msg = (
            f"ffmpeg not found at '{cmd[0]}'. "
            "Install ffmpeg and ensure it is in your PATH."
        )
        logger.error(msg)
        if cb:
            cb(f"  ERROR: {msg}")
        return False
    if r.returncode != 0:
        err = (r.stderr or "")[-500:].strip()
        logger.error(f"ffmpeg [{label}] rc={r.returncode}: {err}")
        if cb:
            cb(f"  ffmpeg error [{label}] rc={r.returncode}")
            for line in err.split("\n")[-3:]:
                if line.strip():
                    cb(f"    {line.strip()}")
        return False
    return True


# ─────────────────────────────────────────────────────────────────────────────
# Trim & stitch (with stretch option)
# ─────────────────────────────────────────────────────────────────────────────
def trim_video(
    input_path:  str,
    segments:    list[tuple[float, float]],
    output_path: str,
    stretch_to_fill: bool = False,  # New parameter
    progress_callback=None,
):
    tmp_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_files: list[str] = []

    # First, detect the video's aspect ratio to determine if stretching is needed
    stretch_filter = ""
    if stretch_to_fill:
        # Get video dimensions using ffprobe
        try:
            cmd = [
                FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height,sample_aspect_ratio,display_aspect_ratio",
                "-of", "default=noprint_wrappers=1:nokey=1", input_path
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            lines = r.stdout.strip().split('\n')
            if len(lines) >= 2:
                width = int(lines[0])
                height = int(lines[1])
                
                # Calculate source aspect ratio
                src_aspect = width / height
                target_aspect = 16/9  # Standard 16:9
                
                # If aspect ratio is close to 16:9, no stretching needed
                if abs(src_aspect - target_aspect) > 0.05:  # More than 5% difference
                    # Calculate scaling to fill 16:9 without black bars
                    if src_aspect > target_aspect:  # Wider than 16:9
                        # Scale to fit height, crop width
                        new_height = height
                        new_width = int(height * target_aspect)
                        stretch_filter = f"scale={new_width}:{new_height},crop={new_width}:{new_height}"
                    else:  # Taller than 16:9
                        # Scale to fit width, crop height
                        new_width = width
                        new_height = int(width / target_aspect)
                        stretch_filter = f"scale={new_width}:{new_height},crop={new_width}:{new_height}"
                    
                    if progress_callback:
                        progress_callback(f"  Stretching from {src_aspect:.2f}:1 to 16:9")
        except Exception as e:
            if progress_callback:
                progress_callback(f"  Warning: Could not detect aspect ratio, using default scaling")

    for i, (start, end) in enumerate(segments):
        tmp = os.path.join(tmp_dir, f"_kc_{uuid.uuid4().hex[:8]}.mp4")
        
        # Build ffmpeg command with optional stretch filter
        cmd = [
            FFMPEG_BIN, "-y",
            "-ss", f"{start:.3f}",
            "-i", input_path,
            "-t", f"{(end - start):.3f}",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
        ]
        
        # Add stretch filter if needed
        if stretch_filter:
            cmd.extend(["-vf", stretch_filter])
        
        cmd.append(tmp)
        
        ok = _ffmpeg(cmd, label=f"seg{i+1}", cb=progress_callback)

        if ok and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
            tmp_files.append(tmp)
            if progress_callback:
                progress_callback(
                    f"  Segment {i+1}/{len(segments)}: {start:.1f}s → {end:.1f}s" + 
                    (" (stretched)" if stretch_filter else "")
                )
        else:
            if progress_callback:
                progress_callback(f"  Segment {i+1} failed — skipping")

    if not tmp_files:
        raise RuntimeError(
            "All ffmpeg extractions failed. "
            f"ffmpeg binary: {FFMPEG_BIN}\n"
            "Run 'ffmpeg -version' to verify installation."
        )

    if len(tmp_files) == 1:
        shutil.move(tmp_files[0], output_path)
    else:
        list_path = os.path.join(tmp_dir, f"_kc_list_{uuid.uuid4().hex[:8]}.txt")
        with open(list_path, "w", encoding="utf-8") as f:
            for p in tmp_files:
                safe = os.path.abspath(p).replace("\\", "/")
                f.write(f"file '{safe}'\n")

        ok = _ffmpeg([
            FFMPEG_BIN, "-y",
            "-f", "concat", "-safe", "0",
            "-i", list_path,
            "-c", "copy",
            output_path,
        ], label="concat", cb=progress_callback)

        try:
            os.remove(list_path)
        except Exception:
            pass

        if not ok:
            raise RuntimeError("ffmpeg concat failed. See logs.")

    for p in tmp_files:
        try:
            os.remove(p)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Top‑level entry point
# ─────────────────────────────────────────────────────────────────────────────
def process_video(
    input_path:        str,
    output_dir:        str,
    n_before:          float,
    n_after:           float,
    full_span:         bool = False,
    stretch_to_fill:   bool = False,  # New
    progress_callback        = None,
) -> dict:
    basename = os.path.basename(input_path)
    mode = "full-span" if full_span else "per-kill"
    if stretch_to_fill:
        mode += " + stretch"

    if progress_callback:
        progress_callback(f"Processing: {basename}  [{mode} mode]")

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input not found: {input_path}")

    os.makedirs(output_dir, exist_ok=True)

    duration = get_video_duration(input_path)
    if progress_callback:
        progress_callback(f"  Duration: {duration:.1f}s")
    if duration == 0:
        raise ValueError(
            "Video duration is 0. Is ffprobe installed?\n"
            f"ffprobe path tried: {FFPROBE_BIN}"
        )

    timestamps = extract_kill_timestamps(input_path, progress_callback)

    if not timestamps:
        return {
            "input":       basename,
            "kills_found": 0,
            "output":      None,
            "message": (
                "No kills detected. "
                "Run: python debug_frames.py static/uploads/<your_clip> "
                "to inspect what the detector sees."
            ),
        }

    segments = build_segments(timestamps, n_before, n_after, duration, full_span)
    total_s  = sum(e - s for s, e in segments)

    if progress_callback:
        progress_callback(
            f"  {len(timestamps)} kill(s) -> {len(segments)} segment(s), "
            f"{total_s:.1f}s to export"
        )

    stem     = os.path.splitext(basename)[0]
    out_name = f"{stem}_kills_{uuid.uuid4().hex[:6]}.mp4"
    out_path = os.path.join(output_dir, out_name)

    trim_video(
        input_path, 
        segments, 
        out_path, 
        stretch_to_fill=stretch_to_fill,  # Pass it here
        progress_callback=progress_callback
    )

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(
            f"Output missing or empty: {out_path}\n"
            f"ffmpeg path used: {FFMPEG_BIN}"
        )

    total_s = sum(e - s for s, e in segments)
    if progress_callback:
        progress_callback(f"  Done: {out_name}  ({total_s:.1f}s)")

    return {
        "input":           basename,
        "kills_found":     len(timestamps),
        "segments":        len(segments),
        "output_duration": round(total_s, 1),
        "output":          out_name,
        "message":         f"{len(timestamps)} kill(s), {total_s:.1f}s total",
    }