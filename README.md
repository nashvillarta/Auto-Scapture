# Auto-Scapture
"Purely for vibes :) "
Still in testing phase, but it is to the point where I was able to automate my technical documentation. 

## Installation Guide
**Option A – Standalone .exe (no Python needed):** download `Auto-Scapture.exe` and double-click it. Your `settings.json` is saved next to the .exe.

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
* **Capture Modes:** Features Manual (Hotkey), Auto-Capture (Fixed Slide Count), and Smart Auto (Detects a target "End" slide to stop automatically).
* **Review & Redact Studio:** An interface to reorder the session queue, delete mistakes, and draw redaction boxes or add text labels to images.
* **PDF Compiler:** Automatically compiles your active session of images into a single, clean PDF document.
* **Area Presets:** Save and recall specific bounding box coordinates on your screen.

## III. Lab Renamer
To expand the functionality for lab environments, a secondary module was implemented:
* **Bulk Sequential Renamer:** Load `.txt`, `.cfg`, or `.csv` files, preview their contents, and rename them sequentially in bulk.
* **Context Naming:** The app peeks inside files to find "Title" or "Experiment" headers and renames the file accordingly.
* **Format Conversion:** Manually edit or automatically batch-convert file extensions.
* **File Merger:** Combine multiple text or config files into one large master document, complete with generated visual dividers.

## IV. Notes & Reflection
* The application is built entirely in Python using `PySide6` (Qt) for the GUI (migrated from `Tkinter`), `keyboard` for global hotkeys, and `Pillow` for image processing.
* The program compiles with PyInstaller (`build_exe.ps1`) into a single standalone `.exe` for daily use.
