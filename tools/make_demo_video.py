"""Record the narrated ~3-minute Saarthi demo: Part 1 Fleet Agent (/fleet), Part 2 CEO Agent (/ceo).
Needs the app running on localhost:8080.  Usage: .venv\\Scripts\\python tools\\make_demo_video.py"""
import asyncio
import base64
import json
import shutil
import subprocess
from pathlib import Path

import edge_tts
from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "demo"
WORK = OUT / "_work"
FFMPEG = shutil.which("ffmpeg") or r"C:\Users\admin\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe\ffmpeg-9.0-full_build\bin\ffmpeg.exe"
FFPROBE = str(Path(FFMPEG).with_name("ffprobe.exe"))
VOICE = "en-IN-PrabhatNeural"
BASE = "http://localhost:8080"

PARTS = [
    ("fleet", "/fleet", 152.5, [
        (300, 4200, "Meet Saarthi AI, the agentic co-pilot for India's truck fleets."),
        (4500, 6000, "Fleet owners still run on phone calls, and every crisis eats their profit."),
        (10500, 6500, "The Fleet Agent gives them ten Gemini-powered agents, watching everything live."),
        (17000, 19000, "A new order arrives. The Dispatch Agent picks the best truck, and the Route Oracle predicts the road, "
                       "live weather, traffic, arrival time and cost, then briefs the driver in Hindi."),
        (36000, 19000, "A coolant leak on the highway. The Recovery Agent diagnoses it, calls the nearest mechanic and moves "
                       "the cargo to an empty truck nearby. The customer gets a new arrival time, not a penalty."),
        (55000, 16000, "An empty truck in Ahmedabad. The Freight Matcher ranks the load board and books a paying load back home."),
        (71000, 15000, "Delivery refused. Saarthi calls the consignee and the shipper, and picks storage and redelivery over "
                       "an expensive return."),
        (86000, 15000, "A shipper cancels after dispatch. Saarthi calculates the exposure, bills the fee, and finds new freight."),
        (101000, 15000, "Eight hours waiting at a warehouse. The Detention Agent calls the dock, escalates, and bills the waiting time."),
        (116000, 12000, "Papers expiring? The Compliance Agent renews and verifies them before a fine."),
        (128000, 10500, "And Saarthi Dash, our dashboard robot, stops a tired driver for chai and rest."),
        (139000, 12500, "The FASTag Auditor matches every toll against real NHAI toll plazas on the route, and catches a double charge."),
    ]),
    ("ceo", "/ceo", 61.5, [
        (300, 3200, "Now, the CEO Agent."),
        (3500, 10500, "Every morning, it briefs the owner on profit, the live fleet, and the one thing to do."),
        (14000, 16000, "The owner asks in Hindi: which truck is losing the most money? The CEO Agent queries ninety days of "
                       "trips and answers with a chart."),
        (30000, 9000, "Agents propose, the owner approves with one tap, and the driver is told instantly."),
        (39000, 10000, "One sentence, and the owner's message reaches the driver's dashboard. No phone calls while driving."),
        (49000, 6000, "Built on Gemini, Vertex AI, BigQuery, Cloud Run and Antigravity."),
        (55300, 5500, "Saarthi AI. Every truck connected. Every crisis handled."),
    ]),
]


def dur(path):
    r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
                       capture_output=True, text=True)
    return float(json.loads(r.stdout)["format"]["duration"])


async def tts(part, lines):
    clips = []
    for i, (start, slot, text) in enumerate(lines):
        p = WORK / f"{part}_n{i:02d}.mp3"
        for attempt in range(6):
            try:
                await edge_tts.Communicate(text, VOICE, rate="+4%").save(str(p))
                break
            except Exception as e:  # noqa: BLE001  (hotspot drops)
                print(f"  tts retry {attempt + 1}: {type(e).__name__}")
                await asyncio.sleep(4)
        else:
            raise RuntimeError("TTS failed - check internet")
        d = dur(p)
        speed = min(1.35, d / ((slot - 300) / 1000)) if d > (slot - 300) / 1000 else 1.0
        clips.append((start, p, speed))
        print(f"  {part} line {i}: {d:.1f}s / slot {slot / 1000:.1f}s speed {speed:.2f}")
    return clips


async def record(part, path):
    fdir = WORK / f"frames_{part}"
    if fdir.exists():
        shutil.rmtree(fdir)
    fdir.mkdir(parents=True)
    frames = []
    async with async_playwright() as pw:
        b = await pw.chromium.launch(channel="msedge", args=["--disable-gpu"])
        ctx = await b.new_context(viewport={"width": 1280, "height": 720}, device_scale_factor=1)
        page = await ctx.new_page()
        cdp = await ctx.new_cdp_session(page)

        def on_frame(params):
            p = fdir / f"f{len(frames):06d}.jpg"
            p.write_bytes(base64.b64decode(params["data"]))
            frames.append((params["metadata"]["timestamp"], p))
            asyncio.ensure_future(cdp.send("Page.screencastFrameAck", {"sessionId": params["sessionId"]}))

        cdp.on("Page.screencastFrame", on_frame)
        await page.goto(BASE + path, wait_until="load", timeout=60000)  # SSE stream never goes idle
        await cdp.send("Page.startScreencast", {"format": "jpeg", "quality": 88, "maxWidth": 1280, "maxHeight": 720})
        await page.wait_for_timeout(2500)
        await page.evaluate("runDemo()")
        await page.wait_for_function("window.DEMO_WALL0 !== undefined", timeout=20000)
        wall0 = await page.evaluate("window.DEMO_WALL0")
        await page.wait_for_function("window.DEMO_DONE === true", timeout=300000, polling=1000)
        await page.wait_for_timeout(800)
        await cdp.send("Page.stopScreencast")
        await b.close()
    lst = WORK / f"frames_{part}.txt"
    with open(lst, "w", encoding="utf-8") as fh:
        for i, (ts, p) in enumerate(frames):
            nxt = frames[i + 1][0] if i + 1 < len(frames) else ts + 0.5
            fh.write(f"file '{p.as_posix()}'\nduration {max(0.001, nxt - ts):.4f}\n")
        fh.write(f"file '{frames[-1][1].as_posix()}'\n")
    video = WORK / f"screen_{part}.mp4"
    subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-vf", "fps=30,format=yuv420p",
                    "-c:v", "libx264", "-preset", "veryfast", "-crf", "18", str(video)], check=True, capture_output=True)
    print(f"  {part}: {len(frames)} frames")
    return video, wall0 / 1000.0 - frames[0][0]


def mux(part, video, offset, clips, length):
    inputs, filters, labels = ["-i", str(video)], [], []
    for i, (start, p, speed) in enumerate(clips):
        inputs += ["-i", str(p)]
        delay = int((offset + start / 1000) * 1000)
        tempo = f"atempo={speed:.3f}," if speed > 1.0 else ""
        filters.append(f"[{i + 1}:a]{tempo}adelay={delay}|{delay},volume=1.6[a{i}]")
        labels.append(f"[a{i}]")
    filters.append(f"{''.join(labels)}amix=inputs={len(labels)}:normalize=0,apad[aout]")
    out = WORK / f"part_{part}.mp4"
    ss = max(0.0, offset - 0.1)
    subprocess.run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(filters), "-map", "0:v", "-map", "[aout]",
                    "-ss", f"{ss:.2f}", "-t", f"{length:.2f}", "-c:v", "libx264", "-preset", "medium", "-crf", "21",
                    "-pix_fmt", "yuv420p", "-r", "30", "-c:a", "aac", "-ar", "48000", "-ac", "2", "-b:a", "160k", str(out)],
                   check=True, capture_output=True)
    return out


async def main():
    OUT.mkdir(exist_ok=True)
    WORK.mkdir(parents=True, exist_ok=True)
    parts = []
    for part, path, length, lines in PARTS:
        print(f"[{part}] narration")
        clips = await tts(part, lines)
        print(f"[{part}] recording {path}")
        video, offset = await record(part, path)
        parts.append(mux(part, video, offset, clips, length))
    lst = WORK / "parts.txt"
    lst.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
    final = OUT / "Saarthi_AI_Demo_3min.mp4"
    subprocess.run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", "-movflags", "+faststart",
                    str(final)], check=True, capture_output=True)
    print(f"DONE {final} ({final.stat().st_size / 1e6:.1f} MB, {dur(final):.1f}s)")


if __name__ == "__main__":
    asyncio.run(main())
