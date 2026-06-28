#!/usr/bin/env python3
"""Generate recall-sqlite explainer video — for non-developer audience.

Zero terminal commands, zero code, pure visual storytelling.
Output: 30-40 second MP4 video.
"""

import numpy as np
from PIL import Image, ImageDraw, ImageFont
import os, json, subprocess, tempfile, shutil

# ─── Config ─────────────────────────────────────────────────────
W, H = 1080, 720           # 16:9
FPS = 30
BG = "#0d1117"
ACCENT = "#58a6ff"
GREEN = "#3fb950"
YELLOW = "#d29922"
GRAY = "#8b949e"
WHITE = "#e6edf3"
DARK_CARD = "#161b22"
BORDER = "#30363d"
OUTPUT = "D:/Workspace/03_Dev_Projects/recall/demo/recall-demo-video.mp4"
TMP = tempfile.mkdtemp(prefix="recall_vid_")

# ─── Font helpers ───────────────────────────────────────────────
def get_font(size, bold=False):
    """Try system fonts that support Chinese, fallback to PIL default."""
    candidates = [
        "C:/Windows/Fonts/msjh.ttc",     # Microsoft JhengHei (Traditional Chinese)
        "C:/Windows/Fonts/msyh.ttc",     # Microsoft YaHei (Simplified Chinese)
        "C:/Windows/Fonts/msjhbd.ttc",   # Microsoft JhengHei Bold
        "C:/Windows/Fonts/seguiemj.ttf", # Segoe UI Emoji
        "C:/Windows/Fonts/segoeui.ttf",  # Segoe UI
        "C:/Windows/Fonts/arial.ttf",
        "C:/Windows/Fonts/msgothic.ttc", # MS Gothic (Japanese/Chinese fallback)
        None,
    ]
    for c in candidates:
        try:
            return ImageFont.truetype(c, size, encoding='unic')
        except (OSError, AttributeError, TypeError):
            try:
                return ImageFont.truetype(c, size)
            except (OSError, AttributeError):
                continue
    return ImageFont.load_default()

FONT_LG = get_font(52)
FONT_MD = get_font(36)
FONT_SM = get_font(26)
FONT_XS = get_font(18)
FONT_MONO = get_font(28)

def make_frame(bg=BG):
    """Create a blank frame."""
    img = Image.new("RGB", (W, H), bg)
    draw = ImageDraw.Draw(img)
    return img, draw

def text_bbox(text, font):
    """Get text bounding box. Handles Pillow 8+ and 10+ APIs."""
    try:
        bbox = draw.textbbox((0, 0), text, font=font)
        return (bbox[2] - bbox[0], bbox[3] - bbox[1])
    except (NameError, AttributeError):
        return font.getbbox(text)[2:]

def center_text(draw, text, y, font=FONT_LG, fill=WHITE):
    b = text_bbox(text, font)
    x = (W - b[0]) // 2
    draw.text((x, y), text, fill=fill, font=font)
    return (x, y, b[0], b[1])

def draw_card(draw, x, y, w, h, bg=DARK_CARD):
    draw.rounded_rectangle([x, y, x+w, y+h], radius=12, fill=bg, outline=BORDER)

# ─── Scene generators ───────────────────────────────────────────

def scene_title(n_frames):
    """Scene 0: Title / Hook."""
    frames = []
    for i in range(n_frames):
        img, draw = make_frame()
        # Top accent line
        draw.rectangle([200, 80, W-200, 86], fill=ACCENT)
        # Main title
        center_text(draw, "你的 AI 助理", W//2 - 80, FONT_LG, GRAY)
        center_text(draw, "有失憶症嗎？", W//2 - 20, get_font(64), WHITE)
        # Subtitle
        center_text(draw, "你跟它說過的，它真的記得嗎？", W//2 + 50, FONT_MD, GRAY)
        # Bottom line
        draw.rectangle([200, H-100, W-200, H-94], fill=ACCENT)
        progress = min(1.0, i / max(n_frames*0.6, 1))
        filled = int(progress * (W-400))
        draw.rectangle([200, H-80, 200+filled, H-74], fill=GREEN)
        frames.append(np.array(img))
    return frames

def scene_problem(n_frames):
    """Scene 1: The Problem — visual storytelling."""
    frames = []
    items = [
        ("💬", "你告訴 AI 你的偏好", "「用 Docker 部署，不要 Dockerfile」", 100),
        ("❌", "AI 忘記了", "「好的，我創建一個 Dockerfile...」", 280),
        ("😤", "你要再說一次", "「我說過了！docker-compose！」", 460),
    ]
    for i in range(n_frames):
        img, draw = make_frame()
        progress = min(1.0, (i+1) / (n_frames*0.7))
        draw.text((50, 30), "你每天在重複的事", fill=GRAY, font=FONT_SM)
        draw.rectangle([50, 70, int(50 + (W-100)*progress), 76], fill=ACCENT)
        
        for j, (icon, title, desc, y) in enumerate(items):
            p = max(0, min(1, (progress * 3 - j * 0.5)))
            if p <= 0:
                continue
            alpha = int(p * 255)
            # Icon circle
            draw.ellipse([120, y, 200, y+80], fill=DARK_CARD, outline=BORDER)
            draw.text((130, y+10), icon, fill=WHITE, font=get_font(40))
            # Title
            draw.text((230, y+5), title, fill=WHITE, font=get_font(32))
            # Description
            draw.text((230, y+45), desc, fill=GRAY, font=get_font(22))
            # Arrow
            if j < len(items) - 1 and p > 0.5:
                draw.text((W//2 - 15, y+95), "↓", fill=GREEN, font=get_font(30))
        frames.append(np.array(img))
    return frames

def scene_pain_points(n_frames):
    """Scene 2: Three pain points."""
    frames = []
    cards = [
        ("🔄", "換 AI 工具", "每次都要重新教", GRAY),
        ("🗑️", "開新對話", "一切歸零", GRAY),
        ("💰", "記憶還要花錢", "每個月燒 token 費用", GRAY),
    ]
    for i in range(n_frames):
        img, draw = make_frame()
        progress = min(1.0, (i+1) / (n_frames*0.7))
        
        draw.text((W//2-100, 30), "所有方案的共同問題", fill=GRAY, font=FONT_SM)
        draw.rectangle([W//2-100, 65, int(W//2-100 + 400*progress), 71], fill=ACCENT)
        
        if progress > 0.1:
            draw.text((W//2, 100), "把記憶塞進對話 → 塞不下", fill=WHITE, font=FONT_MD, anchor="mt")
        if progress > 0.2:
            draw.text((W//2, 150), "租 Vector DB → 要 infra + API key", fill=WHITE, font=FONT_MD, anchor="mt")
        if progress > 0.35:
            draw.text((W//2, 200), "用 LLM 做記憶 → 燒 token 費用", fill=WHITE, font=FONT_MD, anchor="mt")
        
        # Pain point cards
        for j, (icon, title, desc, color) in enumerate(cards):
            p = max(0, min(1, (progress * 2.5 - j * 0.5)))
            if p <= 0:
                continue
            cx = 180 + j * 380
            cy = 320
            draw_card(draw, cx, cy, 320, 200)
            draw.text((cx+130, cy+25), icon, fill=WHITE, font=get_font(48), anchor="mt")
            draw.text((cx+160, cy+90), title, fill=WHITE, font=get_font(30), anchor="mt")
            draw.text((cx+160, cy+135), desc, fill=GRAY, font=get_font(22), anchor="mt")
        frames.append(np.array(img))
    return frames

def scene_solution(n_frames):
    """Scene 3: Enter recall-sqlite."""
    frames = []
    for i in range(n_frames):
        img, draw = make_frame()
        progress = min(1.0, (i+1) / (n_frames*0.6))
        
        # Background glow effect
        glow_radius = int(progress * 300)
        draw.ellipse([W//2-glow_radius, H//2-glow_radius, 
                      W//2+glow_radius, H//2+glow_radius], 
                     fill="#1a2332", outline=None)
        
        if progress > 0.1:
            center_text(draw, "🧠 解決方案", H//2 - 100, FONT_SM, GRAY)
        if progress > 0.25:
            bbox = center_text(draw, "recall-sqlite", H//2 - 30, get_font(64), ACCENT)
        if progress > 0.45:
            center_text(draw, "給你的 AI 一個不會失憶的大腦", H//2 + 50, FONT_MD, WHITE)
            center_text(draw, "一條指令安裝 · 單一檔案 · 開源免費", H//2 + 110, FONT_SM, GRAY)
        
        # Pulsing dot
        pulse = int(8 * abs(np.sin(i * 0.08)))
        dot_color = GREEN if i % 2 == 0 else GREEN
        draw.ellipse([W//2-pulse, H-170-pulse, W//2+pulse, H-170+pulse], fill=dot_color)
        
        frames.append(np.array(img))
    return frames

def scene_benefits(n_frames):
    """Scene 4: Three key benefits anyone can understand."""
    frames = []
    benefits = [
        ("⚡", "瞬間回應", "80 毫秒，比眨眼還快 10 倍", GREEN),
        ("🔒", "完全離線", "資料不離開你的電腦，不需網路", ACCENT),
        ("💰", "零費用", "不用月費、不燒 token、沒有隱藏成本", YELLOW),
    ]
    for i in range(n_frames):
        img, draw = make_frame()
        progress = min(1.0, (i+1) / (n_frames*0.65))
        
        draw.text((W//2-80, 30), "為什麼不一樣", fill=GRAY, font=FONT_SM)
        draw.rectangle([W//2-80, 65, int(W//2-80 + 280*progress), 71], fill=ACCENT)
        
        for j, (icon, title, desc, color) in enumerate(benefits):
            p = max(0, min(1, (progress * 2.5 - j * 0.3)))
            if p <= 0:
                continue
            cx = 90 + j * 340
            cy = 160
            draw_card(draw, cx, cy, 300, 400)
            # Big icon
            draw.text((cx+150, cy+40), icon, fill=color, font=get_font(64), anchor="mt")
            # Title
            draw.text((cx+150, cy+130), title, fill=WHITE, font=get_font(34), anchor="mt")
            # Description
            desc_lines = [desc[i:i+18] for i in range(0, len(desc), 18)]
            for li, line in enumerate(desc_lines):
                draw.text((cx+150, cy+180+li*35), line, fill=GRAY, font=get_font(22), anchor="mt")
        
        if progress > 0.7:
            # vs alternatives bar
            draw_card(draw, 90, 580, 900, 120)
            draw.text((540, 600), "對比其他方案：需要 LLM API 呼叫、需要 Vector DB、需要網路", fill=GRAY, font=FONT_XS, anchor="mt")
            draw.text((540, 635), "recall-sqlite：只需要一個 SQLite 檔案", fill=GREEN, font=FONT_SM, anchor="mt")
        
        frames.append(np.array(img))
    return frames

def scene_how_it_works(n_frames):
    """Scene 5: Simple flow — tell → remember → recall."""
    frames = []
    steps = [
        ("🗣️", "你告訴 AI", "「用 Docker 部署」"),
        ("🧠", "AI 記住", "在 SQLite 中建立記憶"),
        ("🔄", "跨越對話", "換 AI 工具也記得"),
        ("⚡", "瞬間召回", "80ms 找到相關記憶"),
    ]
    for i in range(n_frames):
        img, draw = make_frame()
        progress = min(1.0, (i+1) / (n_frames*0.6))
        
        draw.text((W//2-80, 30), "運作方式", fill=GRAY, font=FONT_SM)
        draw.rectangle([W//2-80, 65, int(W//2-80 + 200*progress), 71], fill=ACCENT)
        
        # Flow arrows
        arrow_x = []
        for j, (icon, title, desc) in enumerate(steps):
            p = max(0, min(1, (progress * 3 - j * 0.3)))
            if p <= 0:
                continue
            cx = 100 + j * 260
            cy = 200
            # Card
            draw_card(draw, cx, cy, 220, 280)
            # Icon
            draw.text((cx+110, cy+35), icon, fill=WHITE, font=get_font(56), anchor="mt")
            # Title
            draw.text((cx+110, cy+120), title, fill=WHITE, font=get_font(30), anchor="mt")
            # Description
            draw.text((cx+110, cy+170), desc, fill=GRAY, font=get_font(22), anchor="mt")
            
            # Arrow between cards
            if j > 0 and p > 0.5 and arrow_x:
                prev_end = arrow_x[-1] + 110
                curr_start = cx - 10
                arr_mid = (prev_end + curr_start) // 2
                draw.text((arr_mid-10, cy+120), "→", fill=ACCENT, font=get_font(40))
            
            arrow_x.append(cx+110)
        
        if progress > 0.8:
            center_text(draw, "三個路徑同時檢索：向量相似度 + 關鍵字 + 全文搜尋", H-120, FONT_XS, GRAY)
            center_text(draw, "不用 LLM、不用 API Key、不用上雲端", H-85, FONT_XS, GRAY)
        
        frames.append(np.array(img))
    return frames

def scene_cta(n_frames):
    """Scene 6: Call to action."""
    frames = []
    for i in range(n_frames):
        img, draw = make_frame()
        progress = min(1.0, (i+1) / (n_frames*0.5))
        
        if progress > 0.1:
            center_text(draw, "開始使用", H//2 - 100, FONT_SM, GRAY)
        if progress > 0.2:
            center_text(draw, "pip install recall-sqlite", H//2 - 30, get_font(40), GREEN)
        if progress > 0.4:
            center_text(draw, "三行指令上路 · 零 infra · 零 token 帳單", H//2 + 30, FONT_SM, WHITE)
        if progress > 0.55:
            center_text(draw, "🧠 github.com/Jnocode/recall-memory", H//2 + 90, FONT_MD, ACCENT)
        if progress > 0.65:
            center_text(draw, "🏆 已納入 Hermes Agent 官方記憶供應商（PR #51205）", H//2 + 140, FONT_XS, GRAY)
        
        # Bottom badge
        if progress > 0.75:
            draw_card(draw, W//2-150, H-130, 300, 70)
            draw.text((W//2, H-110), "開源 · Apache 2.0 · 免費", fill=GRAY, font=FONT_XS, anchor="mt")
        
        frames.append(np.array(img))
    return frames

# ─── Video assembly ──────────────────────────────────────────────

def render_scene(scene_fn, duration_sec, name):
    """Render a scene to frames in tmp directory."""
    n = int(duration_sec * FPS)
    frames = scene_fn(n)
    paths = []
    for idx, frame in enumerate(frames):
        fname = os.path.join(TMP, f"{name}_{idx:04d}.png")
        Image.fromarray(frame).save(fname)
        paths.append(fname)
    return paths

def assemble_video(scene_specs, output_path):
    """Render scenes and compose into final MP4."""
    all_paths = []
    filter_complex = ""
    concat_inputs = ""
    stream_idx = 0
    
    for name, duration in scene_specs:
        paths = render_scene(globals()[f"scene_{name}"], duration, name)
        all_paths.extend(paths)
    
    # Use ffmpeg with image sequence for each scene
    print(f"  Generating {len(all_paths)} frames...")
    
    # Write concat file for ffmpeg
    # Group by scene for crossfade
    scene_list_path = os.path.join(TMP, "scenes.txt")
    with open(scene_list_path, "w") as f:
        for name, duration in scene_specs:
            pattern = os.path.join(TMP, f"{name}_%04d.png")
            f.write(f"file '{pattern}'\n")
            f.write(f"duration {duration}\n")
    
    cmd = [
        "ffmpeg", "-y",
        "-f", "image2",
        "-framerate", str(FPS),
        "-pattern_type", "sequence",
        "-start_number", "0",
        "-i", os.path.join(TMP, "%s_%04d.png".replace("%s", scene_specs[0][0])),
    ]
    
    # Actually, let's use a simpler approach - concat demuxer
    # First rename to sequential, then concat
    print("  Arranging frames...")
    seq_dir = os.path.join(TMP, "seq")
    os.makedirs(seq_dir, exist_ok=True)
    
    global_idx = 0
    for name, duration in scene_specs:
        n = int(duration * FPS)
        for j in range(n):
            src = os.path.join(TMP, f"{name}_{j:04d}.png")
            dst = os.path.join(seq_dir, f"frame_{global_idx:06d}.png")
            if os.path.exists(src):
                os.rename(src, dst)
            else:
                # Create blank frame
                img, _ = make_frame()
                img.save(dst)
            global_idx += 1
    
    print(f"  Total frames: {global_idx}")
    
    # Build video with ffmpeg
    cmd = [
        "ffmpeg", "-y",
        "-framerate", str(FPS),
        "-pattern_type", "sequence",
        "-start_number", "0",
        "-i", os.path.join(seq_dir, "frame_%06d.png"),
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-vf", "fps=30",
        output_path
    ]
    
    print("  Encoding video...")
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"  FFmpeg error: {result.stderr[:500]}")
        return False
    
    size_mb = os.path.getsize(output_path) / (1024*1024)
    print(f"  ✅ Video: {output_path} ({size_mb:.1f} MB)")
    return True

# ─── Main ────────────────────────────────────────────────────────

if __name__ == "__main__":
    scenes = [
        ("title", 7.0),
        ("problem", 7.5),
        ("pain_points", 9.5),
        ("solution", 5.0),
        ("benefits", 13.0),
        ("how_it_works", 10.5),
        ("cta", 8.5),
    ]
    
    total = sum(d for _, d in scenes)
    print(f"🎬 recall-sqlite explainer video")
    print(f"   Duration: {total:.1f}s @ {FPS}fps = {int(total*FPS)} frames")
    print(f"   Resolution: {W}x{H}")
    print(f"   Scenes: {len(scenes)}")
    print()
    
    success = assemble_video(scenes, OUTPUT)
    
    if success:
        print(f"\n✅ Complete!")
    else:
        print(f"\n❌ Failed")
    
    # Cleanup
    shutil.rmtree(TMP, ignore_errors=True)
