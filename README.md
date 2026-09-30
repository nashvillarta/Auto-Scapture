# Auto-Scapture
"Purely for vibes :) "

A Windows desktop tool for automating technical documentation: capture slide decks and lab walkthroughs, pull frames out of recordings, manage everyday screenshots, and export everything as clean PDFs or Markdown.

Still in the testing phase, but it is to the point where I was able to automate my own technical documentation with it.

## Installation Guide

**Option A – Standalone .exe (no Python needed)**
Download `Auto-Scapture.exe` and double-click it. Your `settings.json` (save folder, capture mode, presets, hotkeys, layouts, etc.) is stored next to the .exe.

**Option B – Run from source**
Install Python 3.9+ and the dependencies:
```
pip install -r requirements.txt
python autocapture.py
```

**Building the .exe yourself**
```
.\build_exe.ps1
```
The executable is written to `dist\Auto-Scapture.exe`.

### Notes
* **Global hotkeys:** Because the application uses global hotkeys, it may require Administrator privileges to capture keys pressed inside other elevated windows.
* **Smart App Control:** The .exe is not yet code-signed, so Windows Smart App Control may block it. Running from source (Option B) works in the meantime, as Python and Qt are already signed by their publishers.
* **Disk space:** The single-file .exe unpacks itself to your temp folder at start-up (~150 MB), so keep some free space on your system drive.

## I. Introduction
The main goal for this project was to rapidly prototype and implement a real-time desktop application to automate repetitive documentation tasks.

You are free to use this system for simple manual screen snipping. However, the system must handle massive slide decks, long recordings and messy lab configurations, so it includes several advanced automation and batch-processing components. The application is organized into four tabs: **Auto-Scapture**, **Screenshots**, **Lab Renamer** and **Video Capture**.

## II. Capture System (Auto-Scapture tab)
The core architecture of the application includes the following:
* **Capture Modes:** Manual (Hotkey), Auto-Capture (Fixed Slide Count), and Smart Auto (detects a target "End" slide to stop automatically). Press **Esc** at any time to abort Auto/Smart capture, even while the slideshow has focus.
* **Smart Slide Handling:** Auto/Smart modes can skip duplicate slides (when a key press didn't change the screen), and Auto mode can stop by itself once the deck ends.
* **Duplicate Flags:** Captures that are nearly identical to the one before (e.g. taken before a slide finished changing) are flagged in the capture pop-up, the session panel and Review. Exports warn you before including them.
* **Multi-Monitor Area Selection:** Select a capture area on any monitor (or across monitors). "Show" briefly outlines the area and "Capture Now" takes a one-off shot.
* **Area Presets:** Save and recall specific bounding box coordinates on your screen.
* **Layouts:** Switch between **Vertical** and **Horizontal** layouts, or go **Minimal** (Ctrl+M) for a compact controller you can pin over your slides. Each layout remembers its own window size and position.
* **Stays Out of the Shot:** While a capture is running, Auto-Scapture's own window and pop-ups are hidden from its screenshots, so a pinned window never ends up in your slides (screen-sharing apps such as Discord can still see it otherwise).
* **Review & Redact Studio:** A thumbnail queue with drag-and-drop reordering, redaction boxes and text labels (Ctrl+Z to undo), and copy-to-clipboard.
* **Lossless PDF Compiler:** Compiles the session into a single PDF with every screenshot embedded pixel-for-pixel at full resolution, optionally on A4/Letter pages with page numbers.
* **Markdown Export:** Writes a step-by-step `.md` document (one heading and image per step) for wikis, GitHub or Obsidian.

## III. Screenshots (Screenshots tab)
A replacement for Win+Shift+S / Snipping Tool, and a manager for the Screenshots folder that tends to fill up unnoticed:
* **Snip:** The screen freezes and dims. Drag to capture an area, or click a window to capture just that window. **F** captures the current screen, **A** all screens, **Esc** cancels. Full Screen and All Monitors buttons, plus an optional 3/5/10 second delay, are also available.
* **Screenshot Key:** An optional global key (Print Screen by default) that snips from anywhere and takes over from the Windows tool. Auto-Scapture can keep running in the system tray when closed, and can start with Windows.
* **Saved & Copied:** Every screenshot is saved to your Windows Screenshots folder (the same one Snipping Tool uses, even if it has been moved or synced to OneDrive) and copied to the clipboard.
* **Screenshots Gallery:** Browse the folder (newest first, updates live) with search, date filters and a preview. Open, copy, rename, redact, move, add to your capture session, or delete (always to the Recycle Bin).
* **Organize by Month:** One click files loose screenshots into `YYYY-MM` folders.

## IV. Video Capture (Video Capture tab)
A built-in media player for pulling documentation screenshots out of recordings:
* **Scrub & Step:** Play/pause, a clickable seek bar, ±5 second jumps, playback speed, and exact frame-by-frame stepping.
* **Audio Loudness Chart:** A waveform of the audio under the seek bar highlights loud and quiet sections; click it to jump there.
* **Native-Resolution Frame Capture:** "Capture Frame" saves the decoded video frame at the video's native resolution, with no player controls or screen scaling in the shot. Frames join the current session, so Review, duplicate flags, PDF and Markdown export all work as usual.
* **YouTube Timestamps:** Add titled timestamps at the current position, then jump to, rename, move or delete them. Export a ready-to-paste YouTube chapters `.txt` (or copy it); YouTube's chapter rules are checked for you, and timestamps are remembered per video.
* **Format Support:** MP4, MOV (including iPhone HEVC), MKV, WEBM and AVI, using Qt's FFmpeg backend with hardware decoding where available.

## V. Lab Renamer (Lab Renamer tab)
To expand the functionality for lab environments, a secondary module was implemented:
* **Bulk Sequential Renamer:** Load `.txt`, `.cfg` or `.csv` files, preview their contents, and rename them sequentially in bulk.
* **Context Naming:** Optionally peeks inside files for "Title" or "Experiment" headers and renames each file accordingly.
* **Safe Renaming:** Batch renames never silently overwrite files, handle overlapping names (e.g. shifting `a1..a3` to `a2..a4`), and report any failures.
* **Format Conversion:** Manually edit or batch-convert file extensions.
* **File Merger:** Combine multiple text or config files into one master document, complete with generated visual dividers.

## VI. Keyboard Shortcuts

| Where | Key | Action |
|---|---|---|
| Anywhere | Capture hotkey (default `Ctrl+Shift+A`) | Capture the selected area (Manual mode) |
| Anywhere | Screenshot key (default `Print Screen`, optional) | Snip |
| Anywhere | `Esc` | Abort Auto / Smart capture |
| Auto-Scapture | `Ctrl+M` | Toggle Minimal layout |
| Snip overlay | `F` / `A` / `Esc` | This screen / all screens / cancel |
| Redaction Studio | `Ctrl+Z` / `Ctrl+S` | Undo / save |
| Video Capture | `Space` | Play / pause |
| Video Capture | `←` / `→` | Previous / next frame |
| Video Capture | `Shift+←` / `Shift+→` | Back / forward 5 seconds |
| Video Capture | `C` / `M` | Capture frame / add timestamp |
| Lists & gallery | `Delete` / `F2` | Delete (Recycle Bin for screenshots) / rename |

## VII. Notes & Reflection
* The application is built entirely in Python using `PySide6` (Qt) for the GUI (migrated from `Tkinter`), `keyboard` for global hotkeys, and `Pillow` for image processing.
* The program compiles with PyInstaller (`build_exe.ps1`) into a single standalone `.exe` for daily use.
* Next on the list: code-signing the .exe so it runs cleanly with Smart App Control enabled.

## License
MIT – see [LICENSE](LICENSE).
