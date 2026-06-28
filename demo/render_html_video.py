#!/usr/bin/env python3
"""Render HTML presentation to video using Playwright + ffmpeg."""
import subprocess, os, tempfile, shutil, time, json
from playwright.sync_api import sync_playwright

DEMO_DIR = "D:/Workspace/03_Dev_Projects/recall/demo"
HTML_FILE = os.path.join(DEMO_DIR, "demo_presentation.html")
TMP = tempfile.mkdtemp(prefix="recall_html_")
FPS = 30

# Scene durations (seconds) matching existing TTS
scenes = [
    ("s1", 7.0),   # title
    ("s2", 7.0),   # problem
    ("s3", 9.0),   # pain_points
    ("s4", 5.0),   # solution
    ("s5", 8.0),   # setup
    ("s6", 11.0),  # benefits
    ("s7", 9.0),   # how_it_works
    ("s8", 7.0),   # cta
]

# Convert HTML path to file:// URL
html_url = "file:///" + HTML_FILE.replace("\\", "/").replace("D:", "D:")

print(f"🎬 Rendering {len(scenes)} scenes from HTML...")
print(f"   HTML: {html_url}")
print(f"   Total: {sum(d for _,d in scenes):.1f}s = {int(sum(d for _,d in scenes)*FPS)} frames")

with sync_playwright() as p:
    browser = p.chromium.launch(headless=True)
    page = browser.new_page(viewport={"width": 1080, "height": 720})
    page.goto(html_url)
    time.sleep(1)  # let fonts load
    
    frame_idx = 0
    for slide_id, duration in scenes:
        n_frames = int(duration * FPS)
        print(f"  Scene {slide_id}: {duration}s = {n_frames} frames")
        
        # Show this slide
        page.evaluate(f"""
            document.querySelectorAll('.slide').forEach(s => s.classList.remove('active'));
            document.getElementById('{slide_id}').classList.add('active');
        """)
        time.sleep(0.3)  # let CSS transitions settle
        
        for f in range(n_frames):
            fname = os.path.join(TMP, f"frame_{frame_idx:06d}.png")
            page.screenshot(path=fname, full_page=False)
            frame_idx += 1
    
    browser.close()

print(f"\n  Total frames captured: {frame_idx}")

# Build video
video_path = os.path.join(DEMO_DIR, "recall-demo-video.mp4")
cmd = [
    "ffmpeg", "-y",
    "-framerate", str(FPS),
    "-pattern_type", "sequence",
    "-start_number", "0",
    "-i", os.path.join(TMP, "frame_%06d.png"),
    "-c:v", "libx264",
    "-preset", "medium",
    "-crf", "18",
    "-pix_fmt", "yuv420p",
    "-vf", "fps=30",
    video_path
]
print("  Encoding video...")
result = subprocess.run(cmd, capture_output=True, text=True)
if result.returncode != 0:
    print(f"  FFmpeg error: {result.stderr[:300]}")
else:
    size_mb = os.path.getsize(video_path) / (1024*1024)
    print(f"  ✅ Video: {video_path} ({size_mb:.1f} MB)")

# Mix with TTS
tts_full = os.path.join(DEMO_DIR, "tts_full.mp3")
narrated = os.path.join(DEMO_DIR, "recall-demo-video-narrated.mp4")
if os.path.exists(tts_full):
    cmd2 = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-i", tts_full,
        "-c:v", "copy",
        "-c:a", "aac",
        "-map", "0:v:0",
        "-map", "1:a:0",
        "-shortest",
        narrated
    ]
    result2 = subprocess.run(cmd2, capture_output=True, text=True)
    if result2.returncode == 0:
        sz = os.path.getsize(narrated)
        print(f"  ✅ Narrated: {narrated} ({sz/1024:.0f} KB)")
    else:
        print(f"  Audio mix error: {result2.stderr[:200]}")
else:
    print(f"  ⚠️ TTS not found at {tts_full}, video has no audio")

# Cleanup
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n✅ Complete!")
