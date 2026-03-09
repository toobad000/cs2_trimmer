"""
CS2 Kill Clip Processor — OCR Version (FIXED)
Detects username in kill feed
"""
import os
import uuid
import shutil
import subprocess
import cv2
import numpy as np
import logging
import glob
import re
import pytesseract
from PIL import Image
import time

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


FFMPEG_BIN = _find_binary("ffmpeg")
FFPROBE_BIN = _find_binary("ffprobe")

# Set Tesseract path - YOU HAVE IT HERE
pytesseract.pytesseract.tesseract_cmd = r'D:\Apps\tesseract.exe'


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


def check_tesseract() -> tuple[bool, str]:
    """Check if Tesseract OCR is installed"""
    try:
        r = subprocess.run([pytesseract.pytesseract.tesseract_cmd, '--version'], 
                         capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            version_line = r.stdout.split('\n')[0] if r.stdout else "Unknown version"
            return True, version_line
        else:
            return False, f"Tesseract at {pytesseract.pytesseract.tesseract_cmd} returned error"
    except Exception as e:
        return False, f"Failed to run Tesseract: {str(e)}"


# ─────────────────────────────────────────────────────────────────────────────
# Kill feed ROI constants - TUNED for CS2
# ─────────────────────────────────────────────────────────────────────────────
# CS2 kill feed is in top-right corner
KF_LEFT = 0.70      # Start at 70% from left (captures full kill feed)
KF_TOP = 0.01       # Start at top
KF_RIGHT = 0.98     # Go to 98% (avoid very edge)
KF_BOTTOM = 0.20    # Go down to 20% (captures multiple kills)

# OCR settings - RELAXED
SAMPLE_INTERVAL_S = 0.2  # Sample every 0.2 seconds (5 fps) - faster
MIN_TEXT_CONFIDENCE = 30  # Much lower threshold (was 60)
MAX_KILL_FEED_ROWS = 6

# Lock settings
ROW_LOCK_SECONDS = 8.0
ROW_LOCK_OVERLAP = 40


# ─────────────────────────────────────────────────────────────────────────────
# Image preprocessing for CS2 kill feed - COMPLETELY REWRITTEN
# ─────────────────────────────────────────────────────────────────────────────
def preprocess_for_ocr(roi: np.ndarray) -> np.ndarray:
    """Enhanced preprocessing specifically for CS2 kill feed"""
    try:
        # Convert to grayscale
        if len(roi.shape) == 3:
            gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        else:
            gray = roi.copy()
        
        # CS2 has white text on dark background
        # Invert so text is black on white (better for Tesseract)
        inverted = cv2.bitwise_not(gray)
        
        # Increase contrast
        clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8,8))
        contrasted = clahe.apply(inverted)
        
        # Threshold to make text really clear
        _, thresh = cv2.threshold(contrasted, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        
        # Denoise but preserve text
        denoised = cv2.medianBlur(thresh, 3)
        
        return denoised
    except Exception as e:
        logger.debug(f"Preprocessing error: {e}")
        return roi


def split_into_rows(roi: np.ndarray) -> list[tuple[int, int, np.ndarray]]:
    """Split the ROI into individual kill feed rows"""
    h, w = roi.shape[:2]
    row_height = h // MAX_KILL_FEED_ROWS
    
    rows = []
    for i in range(MAX_KILL_FEED_ROWS):
        y1 = i * row_height
        y2 = (i + 1) * row_height
        if y2 > h:
            y2 = h
        if y2 - y1 < 15:  # Too small
            continue
        
        row_roi = roi[y1:y2, :]
        rows.append((y1, y2, row_roi))
    
    return rows


def extract_text_from_row(row_img: np.ndarray) -> tuple[str, float]:
    """Extract text from a single kill feed row"""
    try:
        # Preprocess
        processed = preprocess_for_ocr(row_img)
        
        # Convert to PIL
        pil_image = Image.fromarray(processed)
        
        # Tesseract config optimized for CS2 kill feed
        # --psm 7: Treat image as single text line
        # -c tessedit_char_whitelist: Only allow relevant characters
        custom_config = r'--oem 3 --psm 7 -c tessedit_char_whitelist=ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789[]()<>-→'
        
        # Get text
        text = pytesseract.image_to_string(pil_image, config=custom_config)
        text = text.strip()
        
        if text:
            # Get confidence (simplified)
            return text, 80.0  # Assume decent confidence if we got text
        
        return "", 0
    except Exception as e:
        logger.debug(f"OCR error: {e}")
        return "", 0


def contains_username(text: str, username: str) -> bool:
    """Check if the extracted text contains the target username"""
    if not text or not username:
        return False
    
    text_lower = text.lower()
    username_lower = username.lower()
    
    # Direct match
    if username_lower in text_lower:
        return True
    
    # Check for username at start of line (killer)
    patterns = [
        rf'^{username_lower}\s',  # username at start
        rf'^{username_lower}$',     # username alone
        rf'\s{username_lower}\s',   # username in middle
        rf'{username_lower}.*?→',   # username with arrow (killer)
        rf'→.*?{username_lower}',   # arrow then username (assist/victim)
    ]
    
    for pattern in patterns:
        if re.search(pattern, text_lower):
            return True
    
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Frame quality gates
# ─────────────────────────────────────────────────────────────────────────────
def _roi_has_content(roi: np.ndarray) -> bool:
    """Check if ROI has sufficient content"""
    if roi.size == 0:
        return False
    return float(roi.mean()) > 10.0  # Very low threshold


# ─────────────────────────────────────────────────────────────────────────────
# Main detection loop
# ─────────────────────────────────────────────────────────────────────────────
def extract_kill_timestamps(video_path: str, username: str, progress_callback=None) -> list[float]:
    """
    Scan the video using OCR to find frames where username appears in kill feed
    """
    cap = cv2.VideoCapture(video_path)
    if not cap.isOpened():
        raise IOError(f"Cannot open: {video_path}")

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    duration = n_frames / fps if fps > 0 else 0.0
    step = max(1, int(fps * SAMPLE_INTERVAL_S))

    if progress_callback:
        progress_callback(
            f"Scanning {duration:.1f}s @ {fps:.0f}fps for username: '{username}'"
        )

    kills: list[float] = []
    locked_rows: list[list] = []  # [row_y, unlock_time]
    frame_idx = 0
    ocr_count = 0
    
    # For progress logging
    last_progress = 0
    log_interval = max(1, n_frames // 20)  # Log every 5%

    def _expire_locks(ts: float):
        nonlocal locked_rows
        locked_rows = [lr for lr in locked_rows if ts < lr[1]]

    def _find_lock(row_y: int) -> int:
        for i, lr in enumerate(locked_rows):
            if abs(lr[0] - row_y) <= ROW_LOCK_OVERLAP:
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

            # Extract kill feed ROI
            x1 = int(w * KF_LEFT)
            x2 = int(w * KF_RIGHT)
            y1 = int(h * KF_TOP)
            y2 = int(h * KF_BOTTOM)
            roi = frame[y1:y2, x1:x2]

            # Skip if too dark
            if not _roi_has_content(roi):
                frame_idx += 1
                continue

            # Split ROI into rows
            rows = split_into_rows(roi)
            
            for row_y1, row_y2, row_img in rows:
                # Calculate absolute row position
                abs_row_y = y1 + row_y1
                
                # Perform OCR
                ocr_count += 1
                text, confidence = extract_text_from_row(row_img)
                
                if text and contains_username(text, username):
                    lock_idx = _find_lock(abs_row_y)
                    
                    if lock_idx >= 0:
                        # Update existing lock
                        locked_rows[lock_idx][0] = abs_row_y
                    else:
                        # New kill
                        kills.append(ts)
                        locked_rows.append([abs_row_y, ts + ROW_LOCK_SECONDS])
                        if progress_callback:
                            progress_callback(
                                f"  [KILL] t={ts:.2f}s  found '{text[:30]}...'  total={len(kills)}"
                            )

        # Progress logging
        if frame_idx % log_interval == 0 and progress_callback:
            pct = (frame_idx / n_frames) * 100
            if pct - last_progress >= 5:
                progress_callback(f"  Progress: {pct:.0f}%")
                last_progress = pct

        frame_idx += 1

    cap.release()
    kills.sort()

    if progress_callback:
        progress_callback(f"Scan complete — {len(kills)} kill(s) found for '{username}'.")
        if ocr_count > 0:
            progress_callback(f"  OCR attempts: {ocr_count}")

    return kills


# ─────────────────────────────────────────────────────────────────────────────
# Segment building (unchanged)
# ─────────────────────────────────────────────────────────────────────────────
def build_segments(
    timestamps: list[float],
    n_before: float,
    n_after: float,
    duration: float,
    full_span: bool = False,
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
# Video utilities (unchanged)
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


def trim_video(
    input_path: str,
    segments: list[tuple[float, float]],
    output_path: str,
    stretch_to_fill: bool = False,
    progress_callback=None,
):
    tmp_dir = os.path.dirname(os.path.abspath(output_path))
    os.makedirs(tmp_dir, exist_ok=True)
    tmp_files: list[str] = []

    stretch_filter = ""
    if stretch_to_fill:
        try:
            cmd = [
                FFPROBE_BIN, "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=width,height",
                "-of", "default=noprint_wrappers=1:nokey=1", input_path
            ]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
            lines = r.stdout.strip().split('\n')
            if len(lines) >= 2:
                width = int(lines[0])
                height = int(lines[1])
                
                src_aspect = width / height
                target_aspect = 16/9
                
                if abs(src_aspect - target_aspect) > 0.05:
                    if src_aspect > target_aspect:
                        new_height = height
                        new_width = int(height * target_aspect)
                        stretch_filter = f"scale={new_width}:{new_height},crop={new_width}:{new_height}"
                    else:
                        new_width = width
                        new_height = int(width / target_aspect)
                        stretch_filter = f"scale={new_width}:{new_height},crop={new_width}:{new_height}"
                    
                    if progress_callback:
                        progress_callback(f"  Stretching from {src_aspect:.2f}:1 to 16:9")
        except Exception as e:
            if progress_callback:
                progress_callback(f"  Warning: Could not detect aspect ratio")

    for i, (start, end) in enumerate(segments):
        tmp = os.path.join(tmp_dir, f"_kc_{uuid.uuid4().hex[:8]}.mp4")
        
        cmd = [
            FFMPEG_BIN, "-y",
            "-ss", f"{start:.3f}",
            "-i", input_path,
            "-t", f"{(end - start):.3f}",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "aac", "-b:a", "192k",
            "-avoid_negative_ts", "make_zero",
        ]
        
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
        raise RuntimeError("All ffmpeg extractions failed")

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

    for p in tmp_files:
        try:
            os.remove(p)
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Top‑level entry point
# ─────────────────────────────────────────────────────────────────────────────
def process_video(
    input_path: str,
    output_dir: str,
    n_before: float,
    n_after: float,
    username: str,
    full_span: bool = False,
    stretch_to_fill: bool = False,
    progress_callback=None,
) -> dict:
    basename = os.path.basename(input_path)
    mode = "full-span" if full_span else "per-kill"
    if stretch_to_fill:
        mode += " + stretch"

    if progress_callback:
        progress_callback(f"Processing: {basename}  [{mode} mode]  for user: {username}")

    if not os.path.exists(input_path):
        raise FileNotFoundError(f"Input not found: {input_path}")

    os.makedirs(output_dir, exist_ok=True)

    duration = get_video_duration(input_path)
    if progress_callback:
        progress_callback(f"  Duration: {duration:.1f}s")
    if duration == 0:
        raise ValueError("Video duration is 0")

    timestamps = extract_kill_timestamps(input_path, username, progress_callback)

    if not timestamps:
        return {
            "input": basename,
            "kills_found": 0,
            "output": None,
            "message": f"No kills detected for username '{username}'. Check spelling or try a different clip.",
        }

    segments = build_segments(timestamps, n_before, n_after, duration, full_span)
    total_s = sum(e - s for s, e in segments)

    if progress_callback:
        progress_callback(
            f"  {len(timestamps)} kill(s) -> {len(segments)} segment(s), "
            f"{total_s:.1f}s to export"
        )

    stem = os.path.splitext(basename)[0]
    out_name = f"{stem}_{username}_kills_{uuid.uuid4().hex[:6]}.mp4"
    out_path = os.path.join(output_dir, out_name)

    trim_video(
        input_path,
        segments,
        out_path,
        stretch_to_fill=stretch_to_fill,
        progress_callback=progress_callback
    )

    if not os.path.exists(out_path) or os.path.getsize(out_path) == 0:
        raise RuntimeError(f"Output missing or empty")

    total_s = sum(e - s for s, e in segments)
    if progress_callback:
        progress_callback(f"  Done: {out_name}  ({total_s:.1f}s)")

    return {
        "input": basename,
        "username": username,
        "kills_found": len(timestamps),
        "segments": len(segments),
        "output_duration": round(total_s, 1),
        "output": out_name,
        "message": f"{len(timestamps)} kill(s) for '{username}', {total_s:.1f}s total",
    }