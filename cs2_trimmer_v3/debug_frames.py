"""
debug_frames.py — matches processor V3 (hard expiry + death flash)
"""
import sys, os, argparse
import cv2
import numpy as np

sys.path.insert(0, os.path.dirname(__file__))
import processor as P


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--out", default="debug_out", help="Output folder")
    args = ap.parse_args()

    os.makedirs(args.out, exist_ok=True)

    cap = cv2.VideoCapture(args.video)
    if not cap.isOpened():
        print(f"ERROR: Cannot open {args.video}")
        sys.exit(1)

    fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    n_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    step = max(1, int(fps * P.SAMPLE_INTERVAL_S))
    duration = n_frames / fps

    print(f"Video   : {args.video}")
    print(f"Duration: {duration:.1f}s @ {fps:.0f}fps  ({n_frames} frames)")
    print(f"Sampling: every {P.SAMPLE_INTERVAL_S}s = every {step} frames")
    print(f"Output  : {args.out}/")
    print(f"Legend  : GREEN=new kill  ORANGE=locked(suppressed)  RED=rejected")
    print()

    # Lock state: [center_px, unlock_time, hard_expiry]
    locked_rows: list[list] = []

    def _expire(ts):
        locked_rows[:] = [lr for lr in locked_rows if ts < lr[1]]

    def _find_lock(center):
        for i, lr in enumerate(locked_rows):
            if abs(lr[0] - center) <= P.ROW_LOCK_OVERLAP:
                return i
        return -1

    saved = 0
    kills = 0
    frame_idx = 0

    while True:
        ret, frame = cap.read()
        if not ret:
            break

        if frame_idx % step == 0:
            ts = frame_idx / fps
            h, w = frame.shape[:2]

            _expire(ts)

            x1 = int(w * P.KF_LEFT); x2 = int(w * P.KF_RIGHT)
            y1 = int(h * P.KF_TOP);  y2 = int(h * P.KF_BOTTOM)
            roi = frame[y1:y2, x1:x2]

            if not P._roi_has_content(roi):
                frame_idx += 1
                continue

            mask = P._red_mask(roi)
            roi_w = roi.shape[1]
            roi_h = roi.shape[0]
            min_px = max(P.MIN_ROW_RED_PX, roi_w * P.MIN_ROW_COVERAGE)
            row_px = (mask.sum(axis=1) / 255).astype(float)
            has_red = bool(row_px.max() >= min_px)

            if not has_red:
                frame_idx += 1
                continue

            boxes = P._find_border_boxes(roi)

            if not boxes:
                # Red present but no valid box
                vis_roi = roi.copy()
                vis_mask = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
                _save_debug(frame, vis_roi, vis_mask, row_px, min_px,
                            roi_w, roi_h, x1, y1, x2, y2, w, h, ts,
                            label="REJECTED", color=(0, 0, 255),
                            info="red found but no valid border box",
                            out=args.out, frame_idx=frame_idx)
                saved += 1
                print(f"  [REJECT ] t={ts:.2f}s  (red rows but no valid box)")
                frame_idx += 1
                continue

            death_flash = P._is_death_flash(frame)

            # Fade refresh (capped by hard expiry)
            for lr in locked_rows:
                if P._has_fade_red_near_row(roi, lr[0]):
                    lr[1] = min(lr[2], max(lr[1], ts + P.FADE_LOCK_EXTEND))

            # Classify boxes
            new_boxes = []
            existing_boxes = []
            if not death_flash:
                for top, bot in boxes:
                    center = (top + bot) / 2.0
                    lock_idx = _find_lock(center)
                    if lock_idx >= 0:
                        locked_rows[lock_idx][0] = center
                        existing_boxes.append((top, bot))
                    else:
                        new_boxes.append((top, bot))
                        hard = ts + (P.ROW_LOCK_SECONDS + 4.0)  # HARD_LOCK_MAX
                        locked_rows.append([center, ts + P.ROW_LOCK_SECONDS, hard])
            else:
                existing_boxes = list(boxes)

            is_new = len(new_boxes) > 0

            if death_flash:
                label = "DEATH FLASH"
                color = (0, 0, 220)
                info = f"bottom‑right red — suppressed boxes={boxes}"
                print(f"  [DEATH   ] t={ts:.2f}s  (death flash, kill suppressed)")
            elif is_new:
                kills += 1
                label = "NEW KILL"
                color = (0, 200, 0)
                info = f"boxes={new_boxes}"
                print(f"  [NEW KILL] t={ts:.2f}s  border={new_boxes}  total={kills}")
            else:
                label = "LOCKED"
                color = (0, 140, 255)
                info = f"suppressed={existing_boxes}"
                print(f"  [LOCKED  ] t={ts:.2f}s  {existing_boxes}")

            # Draw boxes on ROI
            vis_roi = roi.copy()
            for top, bot in new_boxes:
                cv2.rectangle(vis_roi, (0, top), (roi_w-1, bot), (0, 200, 0), 2)
                cv2.putText(vis_roi, f"NEW {top}-{bot}", (2, max(top+12, 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 200, 0), 1)
            for top, bot in existing_boxes:
                cv2.rectangle(vis_roi, (0, top), (roi_w-1, bot), (0, 140, 255), 1)
                cv2.putText(vis_roi, f"lock {top}-{bot}", (2, max(top+12, 10)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.38, (0, 140, 255), 1)

            vis_mask = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
            _save_debug(frame, vis_roi, vis_mask, row_px, min_px,
                        roi_w, roi_h, x1, y1, x2, y2, w, h, ts,
                        label=label, color=color, info=info,
                        out=args.out, frame_idx=frame_idx)
            saved += 1

        frame_idx += 1

    cap.release()
    print(f"\nDone. {saved} frames saved, {kills} unique kill(s) detected -> {args.out}/")


def _save_debug(frame, vis_roi, vis_mask, row_px, min_px,
                roi_w, roi_h, x1, y1, x2, y2, w, h, ts,
                label, color, info, out, frame_idx):

    vis_full = frame.copy()
    cv2.rectangle(vis_full, (x1, y1), (x2, y2), color, 2)
    cv2.putText(vis_full, f"t={ts:.2f}s [{label}]", (x1, max(y1-6, 10)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, color, 2)

    # Bar chart of red pixels per row
    chart_w = 200
    chart = np.zeros((roi_h, chart_w, 3), dtype=np.uint8)
    max_px = max(float(row_px.max()), 1.0)
    for r, px in enumerate(row_px):
        blen = int(px / max_px * (chart_w - 30))
        bcol = (0, 200, 0) if px >= min_px else (80, 80, 80)
        cv2.line(chart, (0, r), (blen, r), bcol, 1)
    tx = int(min_px / max_px * (chart_w - 30))
    cv2.line(chart, (tx, 0), (tx, roi_h), (0, 0, 255), 1)

    vis_roi_r = cv2.resize(vis_roi, (roi_w, roi_h))
    vis_mask_r = cv2.resize(vis_mask, (roi_w, roi_h))
    chart_r = cv2.resize(chart, (chart_w, roi_h))

    top_row = np.hstack([vis_roi_r, vis_mask_r, chart_r])
    scale = top_row.shape[1] / w
    small = cv2.resize(vis_full, (top_row.shape[1], int(h * scale)))
    output = np.vstack([top_row, small])

    status = f"t={ts:.2f}s  [{label}]  {info}"
    cv2.putText(output, status, (8, 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5, color, 1)

    prefix = label.replace(" ", "_")
    fname = os.path.join(out, f"{prefix}_f{frame_idx:07d}_t{ts:.3f}.jpg")
    cv2.imwrite(fname, output)


if __name__ == "__main__":
    main()