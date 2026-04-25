# Auto-Scapture
"Purely for vibes :) "
Still in testing phase, but it is to the point where I was able to automate my technical documentation. 

## Installation Guide
Users must have Python installed on their machine and the following dependencies:
```
pip install keyboard Pillow
```
Within your Windows desktop, click Open with Python.

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
* The application is built entirely in Python using `Tkinter` for the GUI, `keyboard` for global hotkeys, and `Pillow` for image processing.
* Your program should compile without any errors using PyInstaller to create a standalone `.exe` for daily use.
