import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk, colorchooser
import keyboard
from PIL import ImageGrab, Image, ImageTk, ImageChops, ImageStat, ImageDraw, ImageFont
import os
import json
import ctypes 
import threading
import time
import random 
import re # Added for the smart renamer context peek

# --- Windows DPI Scaling Fix ---
try:
    ctypes.windll.user32.SetProcessDPIAware()
except AttributeError:
    pass 
# -------------------------------

class ScreenCaptureApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Auto-Scapture Test Build (1.6.2)") 
        self.root.geometry("480x1050") 
        
        self.counter = 1
        self.capture_region = None
        self.is_listening = False
        self.session_images = [] 
        self.reference_end_image = None 
        
        self.current_session_folder = None
        
        self.script_dir = os.path.dirname(os.path.abspath(__file__))
        self.settings_file = os.path.join(self.script_dir, "settings.json")
        
        self.auto_open_pdf = tk.BooleanVar(value=True) 
        self.is_pinned = tk.BooleanVar(value=False) 
        self.continuous_capture = tk.BooleanVar(value=False)
        
        self.settings_data = self.load_settings()
        self.presets = self.settings_data.get("presets", {})
        
        prefs = self.settings_data.get("preferences", {})
        self.auto_open_pdf.set(prefs.get("auto_open_pdf", True))
        self.continuous_capture.set(prefs.get("continuous_capture", False))
        
        self.saved_hotkeys = prefs.get("saved_hotkeys", ["ctrl+shift+a", "f5"])
        self.saved_filenames = prefs.get("saved_filenames", ["Module1_"])
        
        self.renamer_extensions = prefs.get("renamer_extensions", [".txt", ".cfg", ".csv", ".json", ".md", ".log"])

        # ==========================================
        # --- TABBED INTERFACE ---
        # ==========================================
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)

        self.cap_tab = tk.Frame(self.notebook)
        self.rename_tab = tk.Frame(self.notebook)

        self.notebook.add(self.cap_tab, text="📸 Auto-Scapture")
        self.notebook.add(self.rename_tab, text="📂 Lab Renamer")

        self.setup_capture_ui()
        self.setup_renamer_ui()

    # ==========================================
    # --- TAB 1: EXACT 1.1.1 CAPTURE UI ---
    # ==========================================
    def setup_capture_ui(self):
        top_frame = tk.Frame(self.cap_tab)
        top_frame.pack(fill="x", padx=10, pady=5)
        
        self.pin_btn = tk.Checkbutton(top_frame, text="📌 Pin Window", variable=self.is_pinned, command=self.toggle_pin)
        self.pin_btn.pack(side="left")
        
        self.settings_btn = tk.Button(top_frame, text="⚙️ Preferences", command=self.open_settings_window, bg="#e0e0e0")
        self.settings_btn.pack(side="right")
        
        title_label = tk.Label(self.cap_tab, text="Auto-Scapture Test Build (1.6.2)", font=("Helvetica", 12, "bold"))
        title_label.pack(pady=(0, 10))

        tk.Label(self.cap_tab, text="Master Save Directory:").pack(pady=(5, 0))
        folder_frame = tk.Frame(self.cap_tab)
        folder_frame.pack(fill="x", padx=20)
        
        self.folder_var = tk.StringVar()
        default_path = os.path.join(os.path.expanduser("~"), "Desktop", "AutoCaptures")
        self.folder_var.set(default_path)
        
        self.folder_entry = tk.Entry(folder_frame, textvariable=self.folder_var)
        self.folder_entry.pack(side="left", expand=True, fill="x")
        
        self.browse_btn = tk.Button(folder_frame, text="Browse", command=self.browse_folder)
        self.browse_btn.pack(side="right", padx=(5, 0))
        
        name_frame = tk.Frame(self.cap_tab)
        name_frame.pack(fill="x", padx=20, pady=(15, 5))
        tk.Label(name_frame, text="Base File Name:").pack(anchor="w")
        name_inner = tk.Frame(name_frame)
        name_inner.pack(fill="x")
        
        self.name_var = tk.StringVar(value=self.saved_filenames[0] if self.saved_filenames else "Module1_") 
        self.name_combo = ttk.Combobox(name_inner, textvariable=self.name_var)
        self.name_combo.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.name_combo.bind("<<ComboboxPost>>", self.scan_for_pdfs) 
        
        self.save_name_btn = tk.Button(name_inner, text="Save", command=self.save_filename)
        self.save_name_btn.pack(side="left")
        self.del_name_btn = tk.Button(name_inner, text="Delete", command=self.delete_filename)
        self.del_name_btn.pack(side="left", padx=(5, 0))

        hk_frame = tk.Frame(self.cap_tab)
        hk_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(hk_frame, text="Capture Hotkey:").pack(anchor="w")
        hk_inner = tk.Frame(hk_frame)
        hk_inner.pack(fill="x")
        
        self.hotkey_var = tk.StringVar(value=self.saved_hotkeys[0] if self.saved_hotkeys else "ctrl+shift+a") 
        self.hotkey_combo = ttk.Combobox(hk_inner, textvariable=self.hotkey_var)
        self.hotkey_combo['values'] = self.saved_hotkeys
        self.hotkey_combo.pack(side="left", fill="x", expand=True, padx=(0, 5))
        
        self.save_hk_btn = tk.Button(hk_inner, text="Save", command=self.save_hotkey)
        self.save_hk_btn.pack(side="left")
        self.del_hk_btn = tk.Button(hk_inner, text="Delete", command=self.delete_hotkey)
        self.del_hk_btn.pack(side="left", padx=(5, 0))

        preset_frame = tk.Frame(self.cap_tab)
        preset_frame.pack(fill="x", padx=20, pady=5)
        tk.Label(preset_frame, text="Saved Area Presets:").pack(anchor="w")
        preset_inner = tk.Frame(preset_frame)
        preset_inner.pack(fill="x")
        
        self.preset_var = tk.StringVar()
        self.preset_combo = ttk.Combobox(preset_inner, textvariable=self.preset_var, state="readonly")
        self.preset_combo['values'] = list(self.presets.keys())
        self.preset_combo.pack(side="left", fill="x", expand=True, padx=(0, 5))
        self.preset_combo.bind("<<ComboboxSelected>>", self.apply_preset)
        
        self.save_preset_btn = tk.Button(preset_inner, text="Save Area", command=self.save_preset)
        self.save_preset_btn.pack(side="left")
        self.del_preset_btn = tk.Button(preset_inner, text="Delete", command=self.delete_preset)
        self.del_preset_btn.pack(side="left", padx=(5, 0))
        
        self.select_btn = tk.Button(self.cap_tab, text="1. Select Screen Area", command=self.activate_snipping)
        self.select_btn.pack(pady=(15, 5))
        
        self.status_label = tk.Label(self.cap_tab, text="Area: Not Selected", fg="blue")
        self.status_label.pack()

        mode_frame = tk.LabelFrame(self.cap_tab, text="Capture Mode", padx=10, pady=5)
        mode_frame.pack(fill="x", padx=20, pady=(10, 5))

        self.capture_mode = tk.StringVar(value="Manual")
        tk.Radiobutton(mode_frame, text="Mode 1: Manual (Hotkey)", variable=self.capture_mode, value="Manual", command=self.update_mode_ui).pack(side="top", anchor="w")
        tk.Radiobutton(mode_frame, text="Mode 2: Auto-Capture (Fixed Slide Count)", variable=self.capture_mode, value="Auto", command=self.update_mode_ui).pack(side="top", anchor="w")
        tk.Radiobutton(mode_frame, text="Mode 3: Smart Auto (Detect Section Title/End)", variable=self.capture_mode, value="Smart", command=self.update_mode_ui).pack(side="top", anchor="w")

        self.dynamic_settings_frame = tk.Frame(mode_frame)
        self.dynamic_settings_frame.pack(fill="x", pady=5)

        self.auto_count_lbl = tk.Label(self.dynamic_settings_frame, text="Slides:")
        self.auto_count_var = tk.IntVar(value=10)
        self.auto_count_entry = tk.Entry(self.dynamic_settings_frame, textvariable=self.auto_count_var, width=4)

        prefs = self.settings_data.get("preferences", {})

        self.auto_delay_lbl = tk.Label(self.dynamic_settings_frame, text="Delay (s):")
        self.delay_frame = tk.Frame(self.dynamic_settings_frame)
        self.auto_delay_min_var = tk.DoubleVar(value=prefs.get("delay_min", 1.5))
        self.auto_delay_max_var = tk.DoubleVar(value=prefs.get("delay_max", 2.0))
        self.auto_delay_min_entry = tk.Entry(self.delay_frame, textvariable=self.auto_delay_min_var, width=4)
        self.auto_delay_min_entry.pack(side="left")
        tk.Label(self.delay_frame, text="-").pack(side="left")
        self.auto_delay_max_entry = tk.Entry(self.delay_frame, textvariable=self.auto_delay_max_var, width=4)
        self.auto_delay_max_entry.pack(side="left")

        self.auto_key_lbl = tk.Label(self.dynamic_settings_frame, text="Key:")
        self.auto_key_var = tk.StringVar(value="right")
        self.auto_key_combo = ttk.Combobox(self.dynamic_settings_frame, textvariable=self.auto_key_var, values=["right", "space", "enter", "down", "page down"], width=7, state="readonly")

        self.set_ref_btn = tk.Button(self.dynamic_settings_frame, text="Set Current Screen as Target 'End' Slide", command=self.capture_reference_image, bg="#ffeb99")
        self.ref_status_lbl = tk.Label(self.dynamic_settings_frame, text="Target Slide: NOT SET", fg="red")

        self.cont_cb_frame = tk.Frame(self.cap_tab)
        self.cont_cb_frame.pack(fill="x", padx=20)
        self.cont_cb = tk.Checkbutton(self.cont_cb_frame, text="Enable Continuous Capture (Hold key to spam)", 
                                      variable=self.continuous_capture, command=self.save_settings)
        
        self.start_btn = tk.Button(self.cap_tab, text="2. Start Capture Sequence", command=self.toggle_listening, state=tk.DISABLED)
        self.start_btn.pack(pady=10)
        
        tk.Frame(self.cap_tab, height=2, bd=1, relief="sunken").pack(fill="x", padx=20, pady=10)
        
        self.load_btn = tk.Button(self.cap_tab, text="📂 Load Past Images into Session", command=self.load_past_images, bg="#fff2cc")
        self.load_btn.pack(pady=(0, 5))

        self.review_btn = tk.Button(self.cap_tab, text="👁️ Review / Redact Current Session", command=self.open_review_window, bg="#cfe2f3")
        self.review_btn.pack(pady=(0, 5))

        self.clear_btn = tk.Button(self.cap_tab, text="🗑️ Clear Current Session", command=self.clear_session, bg="#ffcccc")
        self.clear_btn.pack(pady=(0, 5))
        
        self.pdf_btn = tk.Button(self.cap_tab, text="3. Export Session to PDF", command=self.export_to_pdf, bg="#d9ead3")
        self.pdf_btn.pack(pady=5)
        
        self.session_label = tk.Label(self.cap_tab, text="Images in current session: 0")
        self.session_label.pack()
        
        self.scan_for_pdfs()
        self.update_mode_ui()


    # ==========================================
    # --- TAB 2: LAB RENAMER UI ---
    # ==========================================

    def setup_renamer_ui(self):
        self.rename_files_list = []
        tk.Label(self.rename_tab, text="Bulk File Renamer", font=("Helvetica", 14, "bold")).pack(pady=15)
        tk.Button(self.rename_tab, text="📂 1. Select Files to Rename", command=self.load_rename_files, height=2, font=("Arial", 10)).pack(fill="x", padx=40, pady=5)
        
        self.rename_lb_main = tk.Listbox(self.rename_tab, height=15, font=("Consolas", 9), exportselection=False)
        self.rename_lb_main.pack(fill="both", expand=True, padx=20, pady=10)
        
        self.rename_count_lbl = tk.Label(self.rename_tab, text="Files Loaded: 0")
        self.rename_count_lbl.pack(pady=5)

        tk.Button(self.rename_tab, text="👁️ 2. Review, Convert & Rename", command=self.open_renamer_review_window, bg="#cfe2f3", height=2, font=("Arial", 10, "bold")).pack(fill="x", padx=40, pady=15)
        tk.Button(self.rename_tab, text="🗑️ Clear Loaded Files", command=self.clear_rename_list, bg="#ffcccc").pack(pady=10)

    def load_rename_files(self):
        fs = filedialog.askopenfilenames(title="Select Files", filetypes=[("Text Files", "*.txt"), ("Config Files", "*.cfg"), ("CSV Files", "*.csv"), ("All Files", "*.*")])
        if fs:
            self.rename_files_list = list(fs)
            self.refresh_renamer_main_list()

    def clear_rename_list(self):
        self.rename_files_list = []
        self.refresh_renamer_main_list()
        
    def refresh_renamer_main_list(self):
        self.rename_lb_main.delete(0, tk.END)
        for f in self.rename_files_list:
            self.rename_lb_main.insert(tk.END, os.path.basename(f))
        self.rename_count_lbl.config(text=f"Files Loaded: {len(self.rename_files_list)}")

    def open_renamer_review_window(self):
        if not self.rename_files_list:
            messagebox.showinfo("Empty", "Please load some files first!")
            return
            
        r_win = tk.Toplevel(self.root)
        r_win.title("Review, Convert, Merge & Rename")
        r_win.geometry("1000x700")
        r_win.transient(self.root)
        if self.is_pinned.get(): r_win.attributes('-topmost', True)
        r_win.grab_set()

        left_frame = tk.Frame(r_win, width=300)
        left_frame.pack(side="left", fill="y", padx=10, pady=10)
        right_frame = tk.Frame(r_win, bg="#1e1e1e", width=400)
        right_frame.pack(side="right", fill="both", expand=True, padx=10, pady=10)
        right_frame.pack_propagate(False)

        tk.Label(left_frame, text="Reorder Files:", font=("Arial", 10, "bold")).pack(pady=(0, 5))
        list_frame = tk.Frame(left_frame)
        list_frame.pack(fill="both", expand=True)
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        
        self.r_listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, selectmode=tk.EXTENDED, font=("Arial", 10), exportselection=False)
        self.r_listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=self.r_listbox.yview)

        for f in self.rename_files_list:
            self.r_listbox.insert(tk.END, os.path.basename(f))

        btn_frame = tk.Frame(left_frame)
        btn_frame.pack(fill="x", pady=10)
        tk.Button(btn_frame, text="⬆ Up", command=self.move_rename_up).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="⬇ Down", command=self.move_rename_down).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="❌ Del", command=self.remove_rename_item, fg="red").grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="⇈ Top", command=self.move_rename_top).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="⇊ Bot", command=self.move_rename_bottom).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="⇄ Rev", command=self.reverse_rename_list).grid(row=1, column=2, sticky="ew", padx=2, pady=2)
        btn_frame.columnconfigure(0, weight=1); btn_frame.columnconfigure(1, weight=1); btn_frame.columnconfigure(2, weight=1)

        self.preview_title = tk.Label(right_frame, text="Preview", bg="#1e1e1e", fg="white", font=("Arial", 10, "bold"))
        self.preview_title.pack(pady=5)

        single_frame = tk.LabelFrame(right_frame, text="Manual Edit Selected File", bg="#1e1e1e", fg="white", padx=10, pady=5)
        single_frame.pack(fill="x", padx=5, pady=(0, 10))
        self.single_name_var = tk.StringVar()
        tk.Entry(single_frame, textvariable=self.single_name_var).pack(side="left", fill="x", expand=True, padx=5)
        self.single_ext_var = tk.StringVar()
        self.single_ext_combo = ttk.Combobox(single_frame, textvariable=self.single_ext_var, values=self.renamer_extensions, width=7)
        self.single_ext_combo.pack(side="left", padx=5)
        tk.Button(single_frame, text="Rename File", command=self.rename_single_file, bg="#ffeb99").pack(side="left")
        
        self.preview_text = tk.Text(right_frame, bg="#2d2d2d", fg="#d4d4d4", wrap="word", state="disabled")
        self.preview_text.pack(fill="both", expand=True, padx=5, pady=5)
        self.preview_img_lbl = tk.Label(right_frame, bg="#1e1e1e")
        self.r_listbox.bind("<<ListboxSelect>>", self.update_rename_preview)

        rename_frame = tk.LabelFrame(right_frame, text="Batch Sequential Naming & Conversion", bg="#1e1e1e", fg="white", padx=10, pady=5)
        rename_frame.pack(fill="x", padx=5, pady=5)
        tk.Label(rename_frame, text="Base:", bg="#1e1e1e", fg="white").grid(row=0, column=0, sticky="w")
        self.rn_base_var = tk.StringVar(value="Lab1-SC_")
        tk.Entry(rename_frame, textvariable=self.rn_base_var, width=15).grid(row=0, column=1, sticky="ew", padx=2)
        tk.Label(rename_frame, text="Idx:", bg="#1e1e1e", fg="white").grid(row=0, column=2, sticky="w")
        self.rn_start_var = tk.IntVar(value=1)
        tk.Entry(rename_frame, textvariable=self.rn_start_var, width=4).grid(row=0, column=3, sticky="w", padx=2)
        tk.Label(rename_frame, text="Ext:", bg="#1e1e1e", fg="white").grid(row=0, column=4, sticky="w")
        self.rn_ext_var = tk.StringVar(value="Keep Original")
        self.rn_ext_combo = ttk.Combobox(rename_frame, textvariable=self.rn_ext_var, values=["Keep Original"] + self.renamer_extensions, width=12)
        self.rn_ext_combo.grid(row=0, column=5, sticky="w", padx=2)
        tk.Button(rename_frame, text="APPLY RENAME & CONVERT TO ALL", bg="#4CAF50", fg="white", font=("Arial", 10, "bold"), command=lambda: self.execute_batch_rename(r_win)).grid(row=1, column=0, columnspan=6, pady=10, sticky="ew")
        rename_frame.columnconfigure(1, weight=1)

        combine_frame = tk.LabelFrame(right_frame, text="File Merger", bg="#1e1e1e", fg="white", padx=10, pady=5)
        combine_frame.pack(fill="x", padx=5, pady=(0, 10))
        tk.Button(combine_frame, text="🔗 COMBINE ALL LISTED FILES INTO ONE", bg="#337ab7", fg="white", font=("Arial", 10, "bold"), command=self.combine_files).pack(fill="x", pady=5)

        if self.rename_files_list:
            self.r_listbox.selection_set(0)
            self.update_rename_preview()

    def update_rename_preview(self, event=None):
        selection = self.r_listbox.curselection()
        if not selection: return
        idx = selection[0]
        path = self.rename_files_list[idx]
        
        self.preview_title.config(text=f"Preview: {os.path.basename(path)}")
        base_n = os.path.splitext(os.path.basename(path))[0]
        ext = os.path.splitext(path)[1].lower()

        if hasattr(self, 'single_name_var'):
            self.single_name_var.set(base_n)
            self.single_ext_var.set(ext)
        
        self.preview_text.pack_forget()
        self.preview_img_lbl.pack_forget()
        
        if ext in [".txt", ".cfg", ".csv", ".log", ".md", ".xml", ".json", ".ini", ".data"]:
            self.preview_text.pack(fill="both", expand=True, padx=5, pady=5)
            self.preview_text.config(state="normal")
            self.preview_text.delete(1.0, tk.END)
            try:
                with open(path, "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read(2000) 
                    self.preview_text.insert(tk.END, content)
            except Exception as e:
                self.preview_text.insert(tk.END, f"Could not read file:\n{e}")
            self.preview_text.config(state="disabled")
            
        elif ext in [".png", ".jpg", ".jpeg", ".bmp"]:
            self.preview_img_lbl.pack(fill="both", expand=True, padx=5, pady=5)
            try:
                img = Image.open(path)
                img.thumbnail((450, 450)) 
                photo = ImageTk.PhotoImage(img)
                self.preview_img_lbl.config(image=photo, text="")
                self.preview_img_lbl.image = photo 
            except Exception:
                self.preview_img_lbl.config(image='', text="Image not readable", fg="red")
        else:
            self.preview_text.pack(fill="both", expand=True, padx=5, pady=5)
            self.preview_text.config(state="normal")
            self.preview_text.delete(1.0, tk.END)
            self.preview_text.insert(tk.END, f"File format ({ext}) preview not supported.\n\nFile Location:\n{path}")
            self.preview_text.config(state="disabled")

    def rename_single_file(self):
        selection = self.r_listbox.curselection()
        if not selection: 
            messagebox.showwarning("Warning", "No file selected in the list.")
            return
        idx = selection[0]
        old_path = self.rename_files_list[idx]
        dir_n = os.path.dirname(old_path)
        
        new_name = self.single_name_var.get().strip()
        new_ext = self.single_ext_var.get().strip()
        
        if not new_ext.startswith(".") and new_ext: new_ext = "." + new_ext
        if not new_name:
            messagebox.showerror("Error", "File name cannot be empty.")
            return
            
        new_path = os.path.join(dir_n, new_name + new_ext)
        if old_path != new_path:
            if os.path.exists(new_path) and not messagebox.askyesno("Overwrite?", f"'{os.path.basename(new_path)}' exists. Overwrite?"):
                return
            try:
                os.rename(old_path, new_path)
                self.rename_files_list[idx] = new_path
                self.r_listbox.delete(idx)
                self.r_listbox.insert(idx, os.path.basename(new_path))
                self.r_listbox.select_set(idx)
                self.refresh_renamer_main_list()
                
                if new_ext and new_ext not in self.renamer_extensions:
                    self.renamer_extensions.append(new_ext)
                    self.save_settings()
                    if hasattr(self, 'single_ext_combo'): self.single_ext_combo['values'] = self.renamer_extensions
                    if hasattr(self, 'rn_ext_combo'): self.rn_ext_combo['values'] = ["Keep Original"] + self.renamer_extensions
                        
                messagebox.showinfo("Success", f"File renamed to {os.path.basename(new_path)}")
            except Exception as e:
                messagebox.showerror("Error", f"Failed to rename:\n{e}")

    def _refresh_r_listbox(self):
        self.r_listbox.delete(0, tk.END)
        for f in self.rename_files_list:
            self.r_listbox.insert(tk.END, os.path.basename(f))

    def move_rename_up(self):
        selection = self.r_listbox.curselection()
        if len(selection) != 1: return
        idx = selection[0]
        if idx > 0:
            item = self.rename_files_list.pop(idx)
            self.rename_files_list.insert(idx - 1, item)
            self._refresh_r_listbox()
            self.r_listbox.select_set(idx - 1)
            self.update_rename_preview()

    def move_rename_down(self):
        selection = self.r_listbox.curselection()
        if len(selection) != 1: return
        idx = selection[0]
        if idx < len(self.rename_files_list) - 1:
            item = self.rename_files_list.pop(idx)
            self.rename_files_list.insert(idx + 1, item)
            self._refresh_r_listbox()
            self.r_listbox.select_set(idx + 1)
            self.update_rename_preview()

    def move_rename_top(self):
        selection = self.r_listbox.curselection()
        if len(selection) != 1: return
        idx = selection[0]
        if idx > 0:
            item = self.rename_files_list.pop(idx)
            self.rename_files_list.insert(0, item)
            self._refresh_r_listbox()
            self.r_listbox.select_set(0)
            self.update_rename_preview()

    def move_rename_bottom(self):
        selection = self.r_listbox.curselection()
        if len(selection) != 1: return
        idx = selection[0]
        if idx < len(self.rename_files_list) - 1:
            item = self.rename_files_list.pop(idx)
            self.rename_files_list.append(item)
            self._refresh_r_listbox()
            self.r_listbox.select_set(len(self.rename_files_list) - 1)
            self.update_rename_preview()

    def reverse_rename_list(self):
        self.rename_files_list.reverse()
        self._refresh_r_listbox()

    def remove_rename_item(self):
        selection = self.r_listbox.curselection()
        if not selection: return
        for idx in reversed(selection):
            del self.rename_files_list[idx]
            self.r_listbox.delete(idx)
        self.preview_text.config(state="normal")
        self.preview_text.delete(1.0, tk.END)
        self.preview_text.config(state="disabled")
        self.preview_img_lbl.config(image='')
        self.preview_title.config(text="Preview")

    def execute_batch_rename(self, window):
        base = self.rn_base_var.get()
        start_idx = self.rn_start_var.get()
        new_ext_val = self.rn_ext_var.get().strip()
        
        if new_ext_val != "Keep Original" and not new_ext_val.startswith("."):
            new_ext_val = "." + new_ext_val
        count = 0
        
        for i, old_path in enumerate(self.rename_files_list):
            dir_n = os.path.dirname(old_path)
            ext = os.path.splitext(old_path)[1]
            final_ext = ext if new_ext_val == "Keep Original" else new_ext_val
            new_name = f"{base}{start_idx + i}{final_ext}"
            
            if hasattr(self, 'rn_smart_var') and self.rn_smart_var.get() and final_ext.lower() in [".txt", ".cfg", ".log", ".md", ".csv", ".ini"]:
                try:
                    with open(old_path, 'r', encoding='utf-8', errors='ignore') as f:
                        for _ in range(5):
                            line = f.readline()
                            m = re.search(r"(?:Title|Experiment|Lab|Name):\s*(.*)", line, re.I)
                            if m: 
                                clean = "".join(x for x in m.group(1).strip() if x.isalnum() or x in " -_")
                                if clean: 
                                    new_name = f"{clean}{final_ext}"
                                    break
                except Exception as e:
                    print(f"Error reading file {old_path}: {e}")

            new_path = os.path.join(dir_n, new_name)
            try:
                os.rename(old_path, new_path)
                self.rename_files_list[i] = new_path 
                count += 1
            except Exception as e:
                print(f"Failed to rename {old_path}: {e}")
                
        if new_ext_val != "Keep Original" and new_ext_val not in self.renamer_extensions:
            self.renamer_extensions.append(new_ext_val)
            self.save_settings()
            
        messagebox.showinfo("Success", f"Successfully renamed {count} files.", parent=window)
        window.destroy()
        self.refresh_renamer_main_list()

    def combine_files(self):
        if not self.rename_files_list:
            messagebox.showinfo("Empty", "No files to combine.")
            return
        
        save_path = filedialog.asksaveasfilename(
            title="Save Combined File",
            defaultextension=".txt",
            filetypes=[("Text File", "*.txt"), ("Config File", "*.cfg"), ("Markdown", "*.md"), ("All Files", "*.*")],
            initialfile="Combined_Output.txt"
        )
        
        if not save_path: 
            return
        
        try:
            with open(save_path, 'w', encoding='utf-8') as outfile:
                for fp in self.rename_files_list:
                    filename = os.path.basename(fp)
                    outfile.write(f"{'='*50}\n--- START FILE: {filename} ---\n{'='*50}\n\n")
                    try:
                        with open(fp, 'r', encoding='utf-8', errors='ignore') as infile:
                            outfile.write(infile.read())
                    except Exception as e:
                        outfile.write(f"[Error reading file: {e}]")
                    outfile.write("\n\n")
            messagebox.showinfo("Success", f"Combined {len(self.rename_files_list)} files into:\n{save_path}")
        except Exception as e:
            messagebox.showerror("Error", f"Failed to combine files:\n{e}")

    def open_settings_window(self):
        settings_win = tk.Toplevel(self.root)
        settings_win.title("Auto-Scapture Preferences")
        settings_win.geometry("650x350") 
        settings_win.transient(self.root) 
        if self.is_pinned.get():
            settings_win.attributes('-topmost', True)
        settings_win.grab_set() 
        
        top_pref_frame = tk.Frame(settings_win)
        top_pref_frame.pack(fill="x", padx=10, pady=10)
        
        cb = tk.Checkbutton(top_pref_frame, text="Auto-open PDF after export", variable=self.auto_open_pdf, command=self.save_settings)
        cb.pack(side="left")
        
        tk.Button(top_pref_frame, text="📄 View Raw settings.json", command=self.view_settings_file).pack(side="right")
        
        tk.Frame(settings_win, height=2, bd=1, relief="sunken").pack(fill="x", padx=10, pady=5)
        tk.Label(settings_win, text="Manage Saved Settings", font=("Arial", 10, "bold")).pack()
        
        lists_frame = tk.Frame(settings_win)
        lists_frame.pack(fill="both", expand=True, padx=10, pady=5)
        
        # 1. Base Names
        f_names = tk.Frame(lists_frame)
        f_names.pack(side="left", fill="both", expand=True, padx=5)
        tk.Label(f_names, text="Base Names").pack()
        lb_names = tk.Listbox(f_names, height=10, exportselection=False)
        lb_names.pack(fill="both", expand=True)
        for item in self.saved_filenames: lb_names.insert(tk.END, item)
        
        def remove_pref_name():
            sel = lb_names.curselection()
            if sel:
                val = lb_names.get(sel[0])
                self.saved_filenames.remove(val)
                lb_names.delete(sel[0])
                self.save_settings()
                self.scan_for_pdfs()
                if self.name_var.get() == val: self.name_var.set("")
        tk.Button(f_names, text="Remove", command=remove_pref_name).pack(pady=5)

        # 2. Hotkeys
        f_hks = tk.Frame(lists_frame)
        f_hks.pack(side="left", fill="both", expand=True, padx=5)
        tk.Label(f_hks, text="Hotkeys").pack()
        lb_hks = tk.Listbox(f_hks, height=10, exportselection=False)
        lb_hks.pack(fill="both", expand=True)
        for item in self.saved_hotkeys: lb_hks.insert(tk.END, item)
        
        def remove_pref_hk():
            sel = lb_hks.curselection()
            if sel:
                val = lb_hks.get(sel[0])
                self.saved_hotkeys.remove(val)
                lb_hks.delete(sel[0])
                self.hotkey_combo['values'] = self.saved_hotkeys
                self.save_settings()
                if self.hotkey_var.get() == val: self.hotkey_var.set("")
        tk.Button(f_hks, text="Remove", command=remove_pref_hk).pack(pady=5)

        # 3. Area Presets
        f_presets = tk.Frame(lists_frame)
        f_presets.pack(side="left", fill="both", expand=True, padx=5)
        tk.Label(f_presets, text="Area Presets").pack()
        lb_presets = tk.Listbox(f_presets, height=10, exportselection=False)
        lb_presets.pack(fill="both", expand=True)
        for item in self.presets.keys(): lb_presets.insert(tk.END, item)
        
        def remove_pref_preset():
            sel = lb_presets.curselection()
            if sel:
                val = lb_presets.get(sel[0])
                del self.presets[val]
                lb_presets.delete(sel[0])
                self.preset_combo['values'] = list(self.presets.keys())
                self.save_settings()
                if self.preset_var.get() == val: 
                    self.preset_var.set("")
                    self.capture_region = None
                    self.status_label.config(text="Area: Not Selected", fg="blue")
                    self.start_btn.config(state=tk.DISABLED)
        tk.Button(f_presets, text="Remove", command=remove_pref_preset).pack(pady=5)
        
        # 4. File Extensions
        f_exts = tk.Frame(lists_frame)
        f_exts.pack(side="left", fill="both", expand=True, padx=5)
        tk.Label(f_exts, text="File Exts").pack()
        lb_exts = tk.Listbox(f_exts, height=10, exportselection=False)
        lb_exts.pack(fill="both", expand=True)
        for item in self.renamer_extensions: lb_exts.insert(tk.END, item)
        
        def remove_pref_ext():
            sel = lb_exts.curselection()
            if sel:
                val = lb_exts.get(sel[0])
                self.renamer_extensions.remove(val)
                lb_exts.delete(sel[0])
                self.save_settings()
        tk.Button(f_exts, text="Remove", command=remove_pref_ext).pack(pady=5)

    def view_settings_file(self):
        if not os.path.exists(self.settings_file): self.save_settings() 
        os.startfile(self.settings_file)

    def load_settings(self):
        default_data = {
            "presets": {}, 
            "preferences": {
                "auto_open_pdf": True, 
                "continuous_capture": False, 
                "saved_hotkeys": ["ctrl+shift+a"], 
                "saved_filenames": ["Module1_"],
                "delay_min": 1.5,
                "delay_max": 2.0,
                "renamer_extensions": [".txt", ".cfg", ".csv", ".json", ".md", ".log"]
            }
        }
        if os.path.exists(self.settings_file):
            try:
                with open(self.settings_file, "r") as f:
                    data = json.load(f)
                    if "preferences" not in data: data["preferences"] = default_data["preferences"]
                    if "renamer_extensions" not in data["preferences"]:
                        data["preferences"]["renamer_extensions"] = default_data["preferences"]["renamer_extensions"]
                    return data
            except Exception: return default_data
        return default_data

    def save_settings(self):
        try:
            d_min = float(self.auto_delay_min_var.get())
            d_max = float(self.auto_delay_max_var.get())
        except tk.TclError:
            d_min, d_max = 1.5, 2.0
            
        data = {
            "presets": self.presets, 
            "preferences": {
                "auto_open_pdf": self.auto_open_pdf.get(), 
                "continuous_capture": self.continuous_capture.get(), 
                "saved_hotkeys": self.saved_hotkeys, 
                "saved_filenames": self.saved_filenames,
                "delay_min": d_min,
                "delay_max": d_max,
                "renamer_extensions": self.renamer_extensions
            }
        }
        with open(self.settings_file, "w") as f: json.dump(data, f, indent=4)

    def update_mode_ui(self):
        for child in self.dynamic_settings_frame.winfo_children():
            child.grid_forget()
        self.cont_cb.pack_forget()

        mode = self.capture_mode.get()

        if mode == "Manual":
            self.cont_cb.pack(pady=(10, 0)) 
            self.start_btn.config(text="2. Start Listening")
            self.hotkey_combo.config(state="normal")
            self.save_hk_btn.config(state="normal")
            self.del_hk_btn.config(state="normal")

        elif mode == "Auto":
            self.auto_count_lbl.grid(row=0, column=0, padx=2, pady=5)
            self.auto_count_entry.grid(row=0, column=1, padx=2)
            self.auto_delay_lbl.grid(row=0, column=2, padx=(5, 2))
            self.delay_frame.grid(row=0, column=3, padx=2)
            self.auto_key_lbl.grid(row=0, column=4, padx=(5, 2))
            self.auto_key_combo.grid(row=0, column=5, padx=2)
            self.start_btn.config(text="2. Start Auto-Capture (3s Delay)")
            self.hotkey_combo.config(state="disabled")
            self.save_hk_btn.config(state="disabled")
            self.del_hk_btn.config(state="disabled")

        elif mode == "Smart":
            self.set_ref_btn.grid(row=0, column=0, columnspan=3, pady=5, padx=5)
            self.ref_status_lbl.grid(row=0, column=3, columnspan=3, pady=5)
            self.auto_delay_lbl.grid(row=1, column=0, padx=2, pady=2)
            self.delay_frame.grid(row=1, column=1, columnspan=2, padx=2)
            self.auto_key_lbl.grid(row=1, column=3, padx=(5, 2))
            self.auto_key_combo.grid(row=1, column=4, columnspan=2, padx=2)
            self.start_btn.config(text="2. Start Smart Capture (3s Delay)")
            self.hotkey_combo.config(state="disabled")
            self.save_hk_btn.config(state="disabled")
            self.del_hk_btn.config(state="disabled")

    def capture_reference_image(self):
        if not self.capture_region:
            messagebox.showwarning("No Area", "Please select a Screen Area first!")
            return
        
        self.root.attributes('-alpha', 0.0)
        self.root.update()
        time.sleep(0.2)
        
        self.reference_end_image = ImageGrab.grab(bbox=self.capture_region)
        self.ref_status_lbl.config(text="Target Slide: SAVED", fg="green")
        
        self.root.attributes('-alpha', 1.0)
        messagebox.showinfo("Target Set", "The current screen has been saved as the Target End Slide.\n\nNavigate to the START of your presentation before starting the macro.")

    def scan_for_pdfs(self, event=None):
        folder = self.folder_var.get()
        pdf_names = set(self.saved_filenames) 
        if os.path.exists(folder):
            for item in os.listdir(folder):
                path = os.path.join(folder, item)
                if os.path.isfile(path) and item.lower().endswith(".pdf"):
                    base_name = item[:-4] + "_"
                    pdf_names.add(base_name)
                elif os.path.isdir(path):
                    try:
                        for sub_item in os.listdir(path):
                            if sub_item.lower().endswith(".pdf"):
                                base_name = sub_item[:-4] + "_"
                                pdf_names.add(base_name)
                    except OSError:
                        pass

        combo_values = sorted(list(pdf_names))
        if not combo_values: combo_values = ["Module1_"]
        self.name_combo['values'] = combo_values

    def save_filename(self):
        current = self.name_var.get().strip()
        if current and current not in self.saved_filenames:
            self.saved_filenames.append(current)
            self.save_settings()
            self.scan_for_pdfs()
            messagebox.showinfo("Saved", f"Added '{current}' to your saved file names.")
            
    def delete_filename(self):
        current = self.name_var.get().strip()
        if current in self.saved_filenames:
            self.saved_filenames.remove(current)
            self.save_settings()
            self.name_var.set("")
            self.scan_for_pdfs()
            messagebox.showinfo("Deleted", f"Removed '{current}' from saved file names.")

    def save_hotkey(self):
        current = self.hotkey_var.get().lower().strip()
        if current and current not in self.saved_hotkeys:
            self.saved_hotkeys.append(current)
            self.hotkey_combo['values'] = self.saved_hotkeys
            self.save_settings()
            messagebox.showinfo("Saved", f"Added '{current}' to your saved hotkeys.")

    def delete_hotkey(self):
        current = self.hotkey_var.get().lower().strip()
        if current in self.saved_hotkeys:
            self.saved_hotkeys.remove(current)
            self.hotkey_combo['values'] = self.saved_hotkeys
            self.hotkey_var.set(self.saved_hotkeys[0] if self.saved_hotkeys else "")
            self.save_settings()
            messagebox.showinfo("Deleted", f"Removed '{current}' from saved hotkeys.")

    def save_preset(self):
        if not self.capture_region: return
        preset_name = simpledialog.askstring("Save Preset", "Enter a name for this area:")
        if preset_name:
            self.presets[preset_name] = self.capture_region
            self.save_settings()
            self.preset_combo['values'] = list(self.presets.keys())
            self.preset_combo.set(preset_name)

    def delete_preset(self):
        selected = self.preset_var.get()
        if selected in self.presets:
            del self.presets[selected] 
            self.save_settings()       
            self.preset_combo['values'] = list(self.presets.keys())
            self.preset_combo.set('')
            self.capture_region = None
            self.status_label.config(text="Area: Not Selected", fg="blue")
            self.start_btn.config(state=tk.DISABLED)

    def toggle_pin(self):
        self.root.attributes('-topmost', self.is_pinned.get())

    def apply_preset(self, event=None):
        selected = self.preset_var.get()
        if selected in self.presets:
            self.capture_region = tuple(self.presets[selected])
            self.status_label.config(text=f"Area Captured! (Preset: {selected})\n{self.capture_region}")
            self.start_btn.config(state=tk.NORMAL)

    def browse_folder(self):
        folder_selected = filedialog.askdirectory()
        if folder_selected:
            self.folder_var.set(folder_selected)
            self.scan_for_pdfs()

    def activate_snipping(self):
        self.overlay = tk.Toplevel(self.root)
        self.overlay.attributes('-alpha', 0.3)
        self.overlay.attributes('-fullscreen', True)
        self.overlay.config(cursor="cross")
        self.canvas = tk.Canvas(self.overlay, cursor="cross", bg="gray")
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self.on_press)
        self.canvas.bind("<B1-Motion>", self.on_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_release)
        
    def on_press(self, event):
        self.start_x, self.start_y = event.x, event.y
        self.rect = self.canvas.create_rectangle(self.start_x, self.start_y, self.start_x, self.start_y, outline='red', width=3, fill="black")
        
    def on_drag(self, event):
        self.canvas.coords(self.rect, self.start_x, self.start_y, event.x, event.y)
        
    def on_release(self, event):
        self.capture_region = (min(self.start_x, event.x), min(self.start_y, event.y), max(self.start_x, event.x), max(self.start_y, event.y))
        self.overlay.destroy()
        self.preset_combo.set('') 
        self.status_label.config(text=f"Area Captured!\n{self.capture_region}")
        self.start_btn.config(state=tk.NORMAL)
        self.reference_end_image = None
        self.ref_status_lbl.config(text="Target Slide: NOT SET", fg="red")

    def toggle_ui_lock(self, lock=True):
        state = "disabled" if lock else "normal"
        readonly_state = "disabled" if lock else "readonly"
        
        self.folder_entry.config(state=state)
        self.name_combo.config(state=state)
        self.browse_btn.config(state=state)
        self.preset_combo.config(state=readonly_state)
        self.select_btn.config(state=state)
        self.load_btn.config(state=state)
        
        self.notebook.tab(1, state="disabled" if lock else "normal")
        
        mode = self.capture_mode.get()
        if mode == "Manual":
            self.hotkey_combo.config(state=state) 
            self.cont_cb.config(state=state)
        elif mode == "Auto":
            self.auto_count_entry.config(state=state)
            self.auto_delay_min_entry.config(state=state)
            self.auto_delay_max_entry.config(state=state)
            self.auto_key_combo.config(state=readonly_state)
        elif mode == "Smart":
            self.set_ref_btn.config(state=state)
            self.auto_delay_min_entry.config(state=state)
            self.auto_delay_max_entry.config(state=state)
            self.auto_key_combo.config(state=readonly_state)

        for child in self.start_btn.master.winfo_children():
             if isinstance(child, tk.LabelFrame) and child.cget("text") == "Capture Mode":
                 for rb in child.winfo_children():
                     if isinstance(rb, tk.Radiobutton): rb.config(state=state)

    def toggle_listening(self):
        save_folder = self.folder_var.get()
        if not os.path.exists(save_folder): os.makedirs(save_folder)

        if not self.is_listening:
            mode = self.capture_mode.get()
            
            if mode == "Smart" and self.reference_end_image is None:
                messagebox.showerror("Missing Target", "You must click 'Set Current Screen as Target' on the slide you want the macro to stop at before starting.")
                return

            if mode in ["Auto", "Smart"]:
                self.save_settings()

            self.is_listening = True
            self.toggle_ui_lock(lock=True)

            if mode == "Manual":
                custom_hotkey = self.hotkey_var.get().lower() 
                try:
                    keyboard.add_hotkey(custom_hotkey, self.take_screenshot)
                    self.current_hotkey = custom_hotkey 
                    self._key_locked = False
                except ValueError:
                    messagebox.showerror("Error", "Invalid key combination.")
                    self.toggle_ui_lock(lock=False)
                    self.is_listening = False
                    return
                self.start_btn.config(text=f"Stop Listening ({custom_hotkey})", fg="red")
            elif mode == "Auto":
                self.start_btn.config(text="Stop Auto-Capture (Abort)", fg="red")
                threading.Thread(target=self.run_auto_capture_thread, daemon=True).start()
            elif mode == "Smart":
                self.start_btn.config(text="Stop Smart Capture (Abort)", fg="red")
                threading.Thread(target=self.run_smart_capture_thread, daemon=True).start()

        else:
            self.is_listening = False
            self.toggle_ui_lock(lock=False)
            mode = self.capture_mode.get()
            if mode == "Manual":
                keyboard.remove_hotkey(self.current_hotkey)
                self.start_btn.config(text="2. Start Listening", fg="black")
            elif mode == "Auto":
                self.start_btn.config(text="2. Start Auto-Capture (3s Delay)", fg="black")
            elif mode == "Smart":
                self.start_btn.config(text="2. Start Smart Capture (3s Delay)", fg="black")

    def compare_images(self, img1, img2, tolerance=2.0):
        i1 = img1.resize((100, 100)).convert("L")
        i2 = img2.resize((100, 100)).convert("L")
        diff = ImageChops.difference(i1, i2)
        stat = ImageStat.Stat(diff)
        return stat.mean[0] < tolerance

    def run_smart_capture_thread(self):
        try:
            delay_min = self.auto_delay_min_var.get()
            delay_max = self.auto_delay_max_var.get()
            if delay_min > delay_max:
                delay_min, delay_max = delay_max, delay_min
        except tk.TclError:
            self.root.after(0, lambda: messagebox.showerror("Invalid Input", "Invalid delay times."))
            self.root.after(0, self.toggle_listening)
            return

        key = self.auto_key_var.get()

        for i in range(3, 0, -1):
            if not self.is_listening: return 
            self.root.after(0, lambda i=i: self.start_btn.config(text=f"Starting in {i}..."))
            time.sleep(1)

        if not self.is_listening: return

        max_slides = 200 
        suppress_detection = False 
        
        for i in range(max_slides):
            if not self.is_listening: break

            self.root.after(0, lambda curr=i+1: self.start_btn.config(text=f"Scanning Slide {curr} - Click to Stop"))
            current_img = ImageGrab.grab(bbox=self.capture_region)
            
            is_match = self.compare_images(current_img, self.reference_end_image)
            
            if suppress_detection:
                if not is_match:
                    suppress_detection = False 
            else:
                if is_match:
                    user_wants_to_stop = [None]
                    
                    def ask_user():
                        user_wants_to_stop[0] = messagebox.askyesno(
                            "Target Detected!", 
                            f"Target slide detected after {i} captures.\n\nDo you want to STOP the macro here?\n\n(Click 'No' to save this slide and continue capturing)."
                        )
                    
                    self.root.after(0, ask_user)
                    
                    while user_wants_to_stop[0] is None:
                        if not self.is_listening: 
                            break
                        time.sleep(0.1)
                    
                    if not self.is_listening or user_wants_to_stop[0]:
                        break
                    else:
                        suppress_detection = True

            self.root.after(0, lambda img=current_img: self.save_raw_image(img))
            time.sleep(0.3) 

            if i < max_slides - 1:
                keyboard.send(key)
                actual_delay = random.uniform(delay_min, delay_max)
                time.sleep(actual_delay)
            
        if i >= max_slides - 1:
            self.root.after(0, lambda: messagebox.showwarning("Max Slides Reached", "Stopped automatically after 200 slides to prevent infinite loop."))

        if self.is_listening:
            self.root.after(0, self.toggle_listening)

    def run_auto_capture_thread(self):
        try:
            slides = self.auto_count_var.get()
            delay_min = self.auto_delay_min_var.get()
            delay_max = self.auto_delay_max_var.get()
            if delay_min > delay_max:
                delay_min, delay_max = delay_max, delay_min
        except tk.TclError:
            self.root.after(0, lambda: messagebox.showerror("Error", "Invalid inputs"))
            self.root.after(0, self.toggle_listening)
            return

        key = self.auto_key_var.get()

        for i in range(3, 0, -1):
            if not self.is_listening: return 
            self.root.after(0, lambda i=i: self.start_btn.config(text=f"Starting in {i}..."))
            time.sleep(1)

        if not self.is_listening: return

        for i in range(slides):
            if not self.is_listening: break

            self.root.after(0, lambda curr=i+1, total=slides: self.start_btn.config(text=f"Capturing {curr}/{total} - Click to Stop"))
            self.root.after(0, lambda: self.take_screenshot(is_auto=True))
            time.sleep(0.3) 

            if i < slides - 1:
                keyboard.send(key)
                actual_delay = random.uniform(delay_min, delay_max)
                time.sleep(actual_delay)

        if self.is_listening:
            self.root.after(0, self.toggle_listening)
            self.root.after(0, lambda: messagebox.showinfo("Done", f"Captured {slides} slides!"))

    def check_key_release(self):
        try:
            if keyboard.is_pressed(self.current_hotkey):
                self.root.after(50, self.check_key_release)
            else:
                self._key_locked = False 
        except Exception:
             self._key_locked = False

    def save_raw_image(self, img_object):
        if not self.current_session_folder:
            base = self.name_var.get().strip("_")
            if not base: base = "Session"
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            self.current_session_folder = os.path.join(self.folder_var.get(), f"{base}_{timestamp}")
            os.makedirs(self.current_session_folder, exist_ok=True)
            
        save_folder = self.current_session_folder
        base_name = self.name_var.get()
        filename = f"{base_name}{self.counter}.png"
        filepath = os.path.join(save_folder, filename)
        
        img_object.save(filepath)
        self.session_images.append(filepath)
        self.show_toast(filename, len(self.session_images))
        self.session_label.config(text=f"Images in current session: {len(self.session_images)}")
        self.counter += 1

    def take_screenshot(self, is_auto=False):
        if not is_auto and not self.continuous_capture.get():
            if getattr(self, '_key_locked', False): return 
            self._key_locked = True
            self.check_key_release()

        if self.capture_region:
            img = ImageGrab.grab(bbox=self.capture_region)
            self.save_raw_image(img)

    def load_past_images(self):
        folder = self.folder_var.get()
        if not os.path.exists(folder):
            folder = os.path.expanduser("~")

        filepaths = filedialog.askopenfilenames(
            title="Select Past Captures",
            initialdir=folder,
            filetypes=[("PNG Images", "*.png"), ("JPEG Images", "*.jpg;*.jpeg"), ("All Files", "*.*")]
        )
        
        if filepaths:
            added_count = 0
            for path in filepaths:
                if path not in self.session_images:
                    self.session_images.append(path)
                    added_count += 1
            
            self.session_label.config(text=f"Images in current session: {len(self.session_images)}")
            
            if added_count > 0:
                messagebox.showinfo("Loaded", f"Successfully loaded {added_count} image(s) into the current session.")
            else:
                messagebox.showinfo("Notice", "No new images were loaded (they may already be in the session).")

    # ==========================================
    # --- CAPTURE REVIEW WINDOW ---
    # ==========================================
    def open_review_window(self):
        if not self.session_images:
            messagebox.showinfo("Empty Session", "You need to take some screenshots before you can review them!")
            return
            
        review_win = tk.Toplevel(self.root)
        review_win.title("Review Session Queue")
        review_win.geometry("800x550") 
        review_win.transient(self.root)
        if self.is_pinned.get():
            review_win.attributes('-topmost', True)
        review_win.grab_set() 
        
        left_frame = tk.Frame(review_win, width=300)
        left_frame.pack(side="left", fill="y", padx=10, pady=10)
        
        right_frame = tk.Frame(review_win, bg="#1e1e1e", width=400)
        right_frame.pack(side="right", fill="both", expand=True, padx=10, pady=10)
        right_frame.pack_propagate(False) 
        
        tk.Label(left_frame, text="Current Queue:", font=("Arial", 10, "bold")).pack(pady=(0, 5))
        
        list_frame = tk.Frame(left_frame)
        list_frame.pack(fill="both", expand=True)
        
        scrollbar = tk.Scrollbar(list_frame)
        scrollbar.pack(side="right", fill="y")
        
        # FIX: exportselection=False 
        listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, selectmode=tk.EXTENDED, font=("Arial", 10), exportselection=False)
        listbox.pack(side="left", fill="both", expand=True)
        scrollbar.config(command=listbox.yview)
        
        def refresh_lb():
            listbox.delete(0, tk.END)
            for filepath in self.session_images:
                listbox.insert(tk.END, os.path.basename(filepath))
                
        refresh_lb()
            
        preview_label = tk.Label(right_frame, text="Select an image to preview", bg="#1e1e1e", fg="white")
        preview_label.pack(expand=True)
        
        def update_preview(event=None):
            selection = listbox.curselection()
            if selection:
                idx = selection[0]
                path = self.session_images[idx]
                try:
                    img = Image.open(path)
                    img.thumbnail((500, 500)) 
                    photo = ImageTk.PhotoImage(img)
                    preview_label.config(image=photo, text="")
                    preview_label.image = photo 
                except Exception:
                    preview_label.config(image='', text="Image not found")
        
        listbox.bind("<<ListboxSelect>>", update_preview)

        # REORDER BUTTONS
        btn_frame = tk.Frame(left_frame)
        btn_frame.pack(fill="x", pady=10)
        
        def move_up():
            selection = listbox.curselection()
            if len(selection) != 1: return
            idx = selection[0]
            if idx > 0:
                item = self.session_images.pop(idx)
                self.session_images.insert(idx - 1, item)
                refresh_lb()
                listbox.select_set(idx - 1)
                update_preview()

        def move_down():
            selection = listbox.curselection()
            if len(selection) != 1: return
            idx = selection[0]
            if idx < listbox.size() - 1:
                item = self.session_images.pop(idx)
                self.session_images.insert(idx + 1, item)
                refresh_lb()
                listbox.select_set(idx + 1)
                update_preview()
                
        def move_top():
            sel = listbox.curselection()
            if len(sel) != 1: return
            idx = sel[0]
            if idx > 0:
                item = self.session_images.pop(idx)
                self.session_images.insert(0, item)
                refresh_lb()
                listbox.select_set(0)
                update_preview()
                
        def move_bot():
            sel = listbox.curselection()
            if len(sel) != 1: return
            idx = sel[0]
            if idx < len(self.session_images) - 1:
                item = self.session_images.pop(idx)
                self.session_images.append(item)
                refresh_lb()
                listbox.select_set(len(self.session_images)-1)
                update_preview()
                
        def reverse_list():
            self.session_images.reverse()
            refresh_lb()

        def remove_item():
            selection = listbox.curselection()
            if not selection: return
            for idx in reversed(selection):
                path = self.session_images[idx]
                try: os.remove(path)
                except OSError: pass 
                del self.session_images[idx]
            refresh_lb()
            self.session_label.config(text=f"Images in current session: {len(self.session_images)}")
            preview_label.config(image='', text="Select an image to preview")

        tk.Button(btn_frame, text="⬆ Up", command=move_up).grid(row=0, column=0, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="⬇ Down", command=move_down).grid(row=0, column=1, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="❌ Del", command=remove_item, fg="red").grid(row=0, column=2, sticky="ew", padx=2, pady=2)
        
        tk.Button(btn_frame, text="⇈ Top", command=move_top).grid(row=1, column=0, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="⇊ Bot", command=move_bot).grid(row=1, column=1, sticky="ew", padx=2, pady=2)
        tk.Button(btn_frame, text="⇄ Rev", command=reverse_list).grid(row=1, column=2, sticky="ew", padx=2, pady=2)

        btn_frame.columnconfigure(0, weight=1)
        btn_frame.columnconfigure(1, weight=1)
        btn_frame.columnconfigure(2, weight=1)

        def open_redaction_tool():
            selection = listbox.curselection()
            if not selection:
                messagebox.showinfo("Selection", "Please select at least one image to redact.")
                return
            filepaths = [self.session_images[idx] for idx in selection]
            self.launch_manual_redaction(filepaths, review_win, update_preview)

        redact_btn = tk.Button(right_frame, text="🖌️ Open Redaction Tool for Selected Image(s)", command=open_redaction_tool, bg="#ffd9b3")
        redact_btn.pack(pady=10)
        
        if self.session_images:
            listbox.selection_set(0)
            update_preview()

    def launch_manual_redaction(self, filepaths, parent_win, update_callback):
        try:
            original_img = Image.open(filepaths[0]).convert('RGB')
        except Exception as e:
            messagebox.showerror("Error", f"Could not open image for editing:\n{e}")
            return

        redact_win = tk.Toplevel(parent_win)
        title_text = f"Redaction Studio - {os.path.basename(filepaths[0])}"
        if len(filepaths) > 1:
            title_text += f" (+ {len(filepaths)-1} more)"
        redact_win.title(title_text)
        
        w, h = original_img.size
        screen_w = self.root.winfo_screenwidth() - 100
        screen_h = self.root.winfo_screenheight() - 100
        win_w = min(w + 20, screen_w)
        win_h = min(h + 80, screen_h)
        redact_win.geometry(f"{win_w}x{win_h}")
        redact_win.transient(parent_win)
        redact_win.grab_set()

        top_bar = tk.Frame(redact_win, bg="#e0e0e0", pady=5)
        top_bar.pack(fill="x")
        
        state = {'start_x': 0, 'start_y': 0, 'current_rect': None, 'rectangles': [], 'texts': [], 'history': [], 'zoom': 1.0, 'tool': 'box', 'color': 'red'}

        tool_var = tk.StringVar(value='box')
        def update_tool(): state['tool'] = tool_var.get()
        
        tk.Radiobutton(top_bar, text="⬛ Draw Box", variable=tool_var, value='box', command=update_tool, bg="#e0e0e0", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        tk.Radiobutton(top_bar, text="🔤 Add Text", variable=tool_var, value='text', command=update_tool, bg="#e0e0e0", font=("Arial", 9, "bold")).pack(side="left", padx=5)
        
        text_entry = tk.Entry(top_bar, width=20)
        text_entry.pack(side="left", padx=5)
        text_entry.insert(0, "Type here...")

        def choose_color():
            c = colorchooser.askcolor(color=state['color'], title="Choose Text Color")[1]
            if c:
                state['color'] = c
                color_btn.config(bg=c)
                
        color_btn = tk.Button(top_bar, text="Text Color", bg="red", fg="white", font=("Arial", 8, "bold"), command=choose_color)
        color_btn.pack(side="left", padx=5)

        tk.Label(top_bar, text="|  Scroll wheel to zoom", bg="#e0e0e0").pack(side="left", padx=10)

        canvas_frame = tk.Frame(redact_win)
        canvas_frame.pack(fill="both", expand=True)
        
        x_scroll = tk.Scrollbar(canvas_frame, orient="horizontal")
        x_scroll.pack(side="bottom", fill="x")
        y_scroll = tk.Scrollbar(canvas_frame, orient="vertical")
        y_scroll.pack(side="right", fill="y")
        
        canvas = tk.Canvas(canvas_frame, cursor="crosshair", xscrollcommand=x_scroll.set, yscrollcommand=y_scroll.set, bg="#333")
        canvas.pack(side="left", fill="both", expand=True)
        
        x_scroll.config(command=canvas.xview)
        y_scroll.config(command=canvas.yview)

        def refresh_canvas():
            canvas.delete("all")
            cur_w = int(original_img.width * state['zoom'])
            cur_h = int(original_img.height * state['zoom'])
            disp_img = original_img.resize((cur_w, cur_h), Image.Resampling.LANCZOS)
            redact_win.photo = ImageTk.PhotoImage(disp_img)
            canvas.create_image(0, 0, image=redact_win.photo, anchor="nw")
            canvas.config(scrollregion=(0, 0, cur_w, cur_h))
            
            for box in state['rectangles']:
                canvas.create_rectangle(
                    box[0] * state['zoom'], box[1] * state['zoom'], 
                    box[2] * state['zoom'], box[3] * state['zoom'], 
                    fill="black", outline="black"
                )
                
            for txt in state['texts']:
                x, y, text_str, color = txt
                canvas.create_text(x * state['zoom'], y * state['zoom'], text=text_str, fill=color, anchor="nw", font=("Arial", int(20 * state['zoom']), "bold"))

        refresh_canvas()

        def on_mousewheel(event):
            if event.delta > 0:
                state['zoom'] *= 1.1
            else:
                state['zoom'] /= 1.1
            refresh_canvas()

        canvas.bind("<MouseWheel>", on_mousewheel)

        def on_mouse_down(event):
            x = canvas.canvasx(event.x) / state['zoom']
            y = canvas.canvasy(event.y) / state['zoom']
            
            if state['tool'] == 'box':
                state['start_x'] = x
                state['start_y'] = y
                state['current_rect'] = canvas.create_rectangle(
                    x * state['zoom'], y * state['zoom'], 
                    x * state['zoom'], y * state['zoom'], 
                    fill="black", outline="red", width=2
                )
            elif state['tool'] == 'text':
                txt_val = text_entry.get().strip()
                if txt_val:
                    state['texts'].append((x, y, txt_val, state['color']))
                    state['history'].append('text')
                    refresh_canvas()

        def on_mouse_drag(event):
            if state['tool'] == 'box' and state['current_rect']:
                cur_x = canvas.canvasx(event.x) / state['zoom']
                cur_y = canvas.canvasy(event.y) / state['zoom']
                canvas.coords(
                    state['current_rect'], 
                    state['start_x'] * state['zoom'], state['start_y'] * state['zoom'], 
                    cur_x * state['zoom'], cur_y * state['zoom']
                )

        def on_mouse_up(event):
            if state['tool'] == 'box' and state['current_rect']:
                cur_x = canvas.canvasx(event.x) / state['zoom']
                cur_y = canvas.canvasy(event.y) / state['zoom']
                canvas.itemconfig(state['current_rect'], outline="black")
                
                coords = (min(state['start_x'], cur_x), min(state['start_y'], cur_y), 
                          max(state['start_x'], cur_x), max(state['start_y'], cur_y))
                          
                if (coords[2] - coords[0]) > 2 and (coords[3] - coords[1]) > 2:
                    state['rectangles'].append(coords)
                    state['history'].append('box')
                else:
                    canvas.delete(state['current_rect'])
                state['current_rect'] = None

        canvas.bind("<ButtonPress-1>", on_mouse_down)
        canvas.bind("<B1-Motion>", on_mouse_drag)
        canvas.bind("<ButtonRelease-1>", on_mouse_up)

        def save_redactions():
            if not state['rectangles'] and not state['texts']:
                redact_win.destroy()
                return
                
            try:
                font = ImageFont.truetype("arial.ttf", 24)
            except IOError:
                font = ImageFont.load_default()
                
            for fp in filepaths:
                try:
                    img = Image.open(fp).convert('RGB')
                    draw = ImageDraw.Draw(img)
                    
                    for box in state['rectangles']:
                        draw.rectangle(box, fill="black")
                        
                    for txt in state['texts']:
                        x, y, text_str, color = txt
                        draw.text((x, y), text_str, fill=color, font=font)
                        
                    img.save(fp)
                except Exception as e:
                    messagebox.showerror("Save Error", f"Could not save changes to {os.path.basename(fp)}:\n{e}", parent=redact_win)
                    
            update_callback() 
            redact_win.destroy()
            messagebox.showinfo("Saved", f"Changes have been permanently saved to {len(filepaths)} image(s).", parent=parent_win)

        def undo_last():
            if state['history']:
                last_action = state['history'].pop()
                if last_action == 'box' and state['rectangles']:
                    state['rectangles'].pop()
                elif last_action == 'text' and state['texts']:
                    state['texts'].pop()
                refresh_canvas()

        tk.Button(top_bar, text=f"💾 Save to {len(filepaths)} Image(s)", command=save_redactions, bg="#4CAF50", fg="white", font=("Arial", 10, "bold")).pack(side="right", padx=10)
        tk.Button(top_bar, text="↩️ Undo Last", command=undo_last).pack(side="right", padx=5)

    def show_toast(self, filename, session_index):
        toast = tk.Toplevel(self.root)
        toast.overrideredirect(True) 
        toast.attributes('-topmost', True) 
        
        label_text = f"📸 [#{session_index}] Captured: {filename}"
        label = tk.Label(toast, text=label_text, bg="#2b2b2b", fg="#4CAF50", font=("Arial", 11, "bold"), padx=15, pady=10, wraplength=400)
        label.pack()
        
        toast.update_idletasks()
        screen_width = toast.winfo_screenwidth()
        screen_height = toast.winfo_screenheight()
        toast_width = toast.winfo_width()
        toast_height = toast.winfo_height()
        
        x = screen_width - toast_width - 20
        y = screen_height - toast_height - 60 
        
        toast.geometry(f"+{x}+{y}")
        toast.after(1500, toast.destroy)

    def clear_session(self):
        if not self.session_images:
            messagebox.showinfo("Empty Session", "There are no images in the current session to clear.")
            return
            
        if messagebox.askyesno("Confirm Clear", "Are you sure you want to permanently delete all screenshots in the current session?\n\nThis will remove the files from your computer."):
            for path in self.session_images:
                try:
                    os.remove(path)
                except OSError:
                    pass 
            
            if self.current_session_folder and os.path.exists(self.current_session_folder):
                try:
                    if not os.listdir(self.current_session_folder): 
                        os.rmdir(self.current_session_folder)
                except OSError:
                    pass
            
            self.session_images.clear()
            self.counter = 1
            self.current_session_folder = None
            self.session_label.config(text="Images in current session: 0")
            messagebox.showinfo("Session Cleared", "All images in the current session have been deleted.")

    def export_to_pdf(self):
        if not self.session_images:
            messagebox.showwarning("Empty Session", "You haven't taken any screenshots yet!")
            return
            
        base_name = self.name_var.get()
        clean_name = base_name.strip("_") if base_name.endswith("_") else base_name
        if not clean_name: clean_name = "Compiled_Document"
        
        if self.current_session_folder:
            save_folder = self.current_session_folder
        else:
            timestamp = time.strftime("%Y%m%d_%H%M%S")
            save_folder = os.path.join(self.folder_var.get(), f"{clean_name}_Export_{timestamp}")
            os.makedirs(save_folder, exist_ok=True)
            
        pdf_filename = f"{clean_name}.pdf"
        pdf_path = os.path.join(save_folder, pdf_filename)
        
        while os.path.exists(pdf_path):
            action = messagebox.askyesnocancel(
                "File Already Exists",
                f"The file '{pdf_filename}' already exists.\n\n"
                "• Click 'Yes' to Overwrite it.\n"
                "• Click 'No' to Rename this new export.\n"
                "• Click 'Cancel' to abort."
            )
            if action is None:  
                return
            elif action is True:  
                break  
            else:  
                new_name = simpledialog.askstring("Rename File", "Enter a new name for the PDF:", initialvalue=clean_name)
                if not new_name: 
                    return 
                clean_name = new_name
                pdf_filename = f"{clean_name}.pdf"
                pdf_path = os.path.join(save_folder, pdf_filename)

        try:
            first_image = Image.open(self.session_images[0]).convert('RGB')
            other_images = [Image.open(filepath).convert('RGB') for filepath in self.session_images[1:]]
            
            first_image.save(pdf_path, format="PDF", resolution=100.0, save_all=True, append_images=other_images)
            
            if self.auto_open_pdf.get():
                messagebox.showinfo("Success", f"Saved {len(self.session_images)} images.\n\nFile Location:\n{pdf_path}\n\nOpening PDF now...")
            else:
                messagebox.showinfo("Success", f"Saved {len(self.session_images)} images to:\n{pdf_path}")
            
            self.session_images.clear()
            self.counter = 1
            self.current_session_folder = None 
            self.session_label.config(text="Images in current session: 0")
            
            self.scan_for_pdfs()
            
            if self.auto_open_pdf.get():
                try:
                    os.startfile(pdf_path)
                except AttributeError:
                    import subprocess, sys
                    opener = "open" if sys.platform == "darwin" else "xdg-open"
                    subprocess.call([opener, pdf_path])
                
        except PermissionError:
            messagebox.showerror(
                "File is Locked", 
                f"Cannot save '{pdf_filename}'.\n\n"
                "Windows is blocking the save because the file is currently OPEN in another program.\n\n"
                "Please close the PDF and try exporting again!"
            )
        except Exception as e:
            messagebox.showerror("Error", f"Could not create PDF:\n{str(e)}\n\nTry ensuring all images in the session still exist in the folder.")

if __name__ == "__main__":
    root = tk.Tk()
    app = ScreenCaptureApp(root)
    root.mainloop()