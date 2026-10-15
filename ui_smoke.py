"""Headless smoke test: starts the UI, tests navigation and captures a dashboard preview."""
import os, tempfile
from pathlib import Path
os.environ["XDG_DATA_HOME"] = tempfile.mkdtemp(prefix="madrasati_ui_smoke_")
import tkinter as tk
import school_manager
root = tk.Tk()
app = school_manager.SchoolApp(root, {"full_name": "اختبار واجهة", "is_admin": True})
root.geometry("1200x800+10+10")
root.update_idletasks(); root.update()
assert len(app.nav_buttons) == 12, len(app.nav_buttons)
app.show_page(0); root.update_idletasks(); root.update()
assert app.notebook.select() == app.notebook.tabs()[0]
assert hasattr(app, "dashboard_canvas"), "dashboard should be scrollable"
settings_dialog = school_manager.AppearanceSettingsDialog(app)
assert {"font_family", "font_size", "text_color", "accent_color", "field_color", "panel_color"}.issubset(settings_dialog.vars)
settings_dialog.win.destroy()
for page in (1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11):
    app.show_page(page); root.update_idletasks(); root.update()
    assert app.notebook.select() == app.notebook.tabs()[page], page
app.show_page(0); root.update_idletasks(); root.update()
try:
    import pyautogui
    pyautogui.screenshot().save("/mnt/data/madrasati_ui_preview.png")
except Exception as exc:
    print("screenshot skipped:", exc)
print("UI smoke passed: nav buttons=", len(app.nav_buttons), "selected=", app.notebook.index(app.notebook.select()))
app.db.close(); root.destroy()
