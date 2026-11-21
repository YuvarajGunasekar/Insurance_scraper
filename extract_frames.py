import argparse
import cv2
import pathlib

parser = argparse.ArgumentParser(description="Extract a frame every three seconds from a video.")
parser.add_argument("video_path", type=pathlib.Path, help="Path to the input video.")
args = parser.parse_args()
if not args.video_path.is_file():
    parser.error(f"Video file does not exist: {args.video_path}")

video_path = str(args.video_path)
out_dir = pathlib.Path("screenshots/video_frames")
out_dir.mkdir(parents=True, exist_ok=True)

cap = cv2.VideoCapture(video_path)
fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
duration = total_frames / fps
print(f"Video duration: {duration:.1f}s, total frames: {total_frames}, fps: {fps}")

# Extract frames every 3 seconds
interval = int(fps * 3)
count = 0
frame_idx = 0

while cap.isOpened():
    ret, frame = cap.read()
    if not ret:
        break
    if frame_idx % interval == 0:
        sec = int(frame_idx / fps)
        out_path = out_dir / f"frame_{sec:03d}s.jpg"
        # Resize frame slightly to keep file size reasonable if needed
        cv2.imwrite(str(out_path), frame)
        count += 1
    frame_idx += 1

cap.release()
print(f"Extracted {count} frames to {out_dir}")
