# Auto-Scapture
"Purely for vibes :) "
Still in testing phase, but it is to the point where I was able to automate my technical documentation. 

## Installation Guide
**Option A – Standalone .exe (no Python needed):** download `Auto-Scapture.exe` and double-click it. Your `settings.json` (save folder, capture mode, presets, hotkeys, etc.) is saved next to the .exe.

**Option B – Run from source:** install Python 3.9+ and the dependencies:
```
pip install -r requirements.txt
python autocapture.py
```

**Building the .exe yourself:**
```
.\build_exe.ps1
```
The executable is written to `dist\Auto-Scapture.exe`.

Because this application uses global hot keys, it may require Adminstrator privileges in order to run.

## I. Introduction
The main goal for this project was to rapidly prototype and implement a real-time desktop application to automate repetitive documentation tasks. 

You are free to use this system for simple manual screen snipping. However, the system must handle massive slide decks and messy lab configurations, so it includes several advanced automation and batch-processing components. 

## II. Capture System
The core architecture of the application includes the following:
* **Capture Modes:** Features Manual (Hotkey), Auto-Capture (Fixed Slide Count), and Smart Auto (Detects a target "End" slide to stop automatically). Press **Esc** at any time to abort Auto/Smart capture, even while the slideshow has focus.
* **Smart Slide Handling:** Auto/Smart modes can skip duplicate slides (when a key press didn't change the screen) and Auto mode can stop by itself when the deck ends.
* **Multi-Monitor Area Selection:** Select a capture area on any monitor (or across monitors); "Show" briefly outlines the area and "Capture Now" takes a one-off shot.
* **Invisible to Itself:** Auto-Scapture's window and capture pop-ups are hidden from its own screenshots (toggle in Preferences), so a pinned window never ends up in your slides.
* **Review & Redact Studio:** A thumbnail queue with drag-and-drop reordering, double-click to redact, copy-to-clipboard, and redaction boxes / text labels (Ctrl+Z to undo).
* **Lossless PDF Compiler:** Compiles your session into a single PDF with every screenshot embedded pixel-for-pixel at full resolution (no JPEG blur), optionally on A4/Letter pages with crisp text page numbers. 200-slide decks export in well under a second.
* **Duplicate Flags:** Captures that are nearly identical to the one before (e.g. taken before a slide finished changing) are flagged in the pop-up, the session panel and Review, with one-click "Select Flagged", "Unflag" and "Scan Again". Exports warn you before including them.
* **Markdown Export:** Writes a step-by-step `.md` document (one heading + image per step) ready for wikis, GitHub or Obsidian.
* **Drag & Drop:** Drop images onto the window to add them to the session, or files onto the Lab Renamer tab.
* **Area Presets:** Save and recall specific bounding box coordinates on your screen.

## III. Video Capture
A built-in media player tab for pulling documentation screenshots out of recordings:
* **Scrub & Step:** Play/pause, a seek bar you can click anywhere on, +/- 5 s jumps, playback speed, and exact frame-by-frame stepping (Left/Right arrows).
* **Audio Loudness Chart:** A waveform of the video's audio under the seek bar shows loud and quiet parts at a glance; click it to jump there. Uses Qt's FFmpeg backend with hardware video decoding where your device supports it; works with MP4, MOV (incl. iPhone HEVC), MKV, WEBM and AVI.
* **Native-Resolution Frame Capture:** "Capture Frame" (C) saves the decoded video frame itself at the video's native resolution, with no player controls or screen scaling in the shot. Frames join the current session, so Review, duplicate flags, PDF and Markdown export all work as usual.
* **YouTube Timestamps:** Add titled timestamps (M) at the current position, jump to / rename / move / delete them, and export a ready-to-paste YouTube chapters `.txt` (or copy it). YouTube's rules (first stamp at 0:00, at least 3 stamps, 10 s minimum) are checked for you, and timestamps are remembered per video.

Keyboard: Space = play/pause, Left/Right = previous/next frame, Shift+Left/Right = -/+ 5 s, C = capture frame, M = add timestamp.

## IV. Lab Renamer
To expand the functionality for lab environments, a secondary module was implemented:
* **Bulk Sequential Renamer:** Load `.txt`, `.cfg`, or `.csv` files, preview their contents, and rename them sequentially in bulk.
* **Context Naming:** Tick "Context Naming" and the app peeks inside files to find "Title" or "Experiment" headers and renames the file accordingly.
* **Safe Renaming:** Batch renames never silently overwrite files, handle overlapping names (e.g. shifting `a1..a3` to `a2..a4`), and report any failures.
* **Format Conversion:** Manually edit or automatically batch-convert file extensions.
* **File Merger:** Combine multiple text or config files into one large master document, complete with generated visual dividers.

## V. Notes & Reflection
* The application is built entirely in Python using `PySide6` (Qt) for the GUI (migrated from `Tkinter`), `keyboard` for global hotkeys, and `Pillow` for image processing.
* The program compiles with PyInstaller (`build_exe.ps1`) into a single standalone `.exe` for daily use.
