# -*- coding: utf-8 -*-
"""نظام إدارة المدرسة - تطبيق مكتبي عربي يعمل دون إنترنت.
Python 3.10+ | Tkinter + SQLite (بدون مكتبات خارجية)
"""
from __future__ import annotations

import csv
import hashlib
import html as html_lib
import hmac
import json
import re
import os
import secrets
import shutil
import sqlite3
import sys
import tempfile
import threading
import subprocess
import traceback
import uuid
import webbrowser
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
import tkinter as tk
import tkinter.font as tkfont
from tkinter import ttk, messagebox, filedialog, simpledialog, colorchooser
from functools import lru_cache

try:
    from PIL import Image, ImageTk
    PIL_AVAILABLE = True
except Exception:
    Image = ImageTk = None
    PIL_AVAILABLE = False

# Tk on some systems does not shape Arabic or apply its right-to-left display order.
# These small UI-only helpers fix display labels/tables while keeping database values logical.
try:
    import arabic_reshaper
    from bidi.algorithm import get_display as _bidi_get_display
    _ARABIC_DISPLAY_AVAILABLE = True
except Exception:
    arabic_reshaper = None
    _bidi_get_display = None
    _ARABIC_DISPLAY_AVAILABLE = False

_ARABIC_RANGE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\u08A0-\u08FF]")

@lru_cache(maxsize=8192)
def display_arabic_text(value):
    """Convert logical Arabic to a Tk-safe visual string exactly once.

    Tk widgets on Windows may not perform Arabic joining and bidirectional layout
    consistently. The reshaper creates the proper contextual glyph forms and
    python-bidi orders mixed RTL/LTR text for display. Database values must remain
    logical Unicode; use this helper only for display strings. A guard makes it
    idempotent when a display value passes through more than one UI wrapper.
    """
    if not isinstance(value, str) or not value or not _ARABIC_RANGE.search(value):
        return value
    # Windows/Tk uses the operating system's native text layout. Pre-reversing
    # strings there causes exactly the garbled/reversed words users reported.
    # Only shape/reorder on X11/Linux, where Tk commonly lacks full RTL shaping.
    if sys.platform.startswith("win") or sys.platform == "darwin":
        return value
    # Arabic Presentation Forms show that the value has already been reshaped.
    # Don't reshape it again when it passes a label/table wrapper a second time.
    if any(0xFB50 <= ord(ch) <= 0xFDFF or 0xFE70 <= ord(ch) <= 0xFEFF for ch in value):
        return value
    if not _ARABIC_DISPLAY_AVAILABLE:
        return value
    try:
        reshaped = arabic_reshaper.reshape(value)
        return _bidi_get_display(reshaped, base_dir="R")
    except Exception:
        return value


def _shape_widget_options(options):
    shaped = dict(options)
    if isinstance(shaped.get("text"), str):
        shaped["text"] = display_arabic_text(shaped["text"])
    return shaped


def _install_ttk_display_helpers():
    """Shape static labels/buttons and table displays without changing saved values."""
    if getattr(ttk.Label, "_madrasati_rtl_patched", False):
        return
    for cls in (ttk.Label, ttk.Button, ttk.LabelFrame, ttk.Checkbutton, ttk.Radiobutton):
        original_init = cls.__init__
        original_configure = cls.configure
        def make_init(orig):
            def _init(self, *args, **kwargs):
                return orig(self, *args, **_shape_widget_options(kwargs))
            return _init
        def make_configure(orig):
            def _configure(self, cnf=None, **kwargs):
                if isinstance(cnf, dict):
                    cnf = _shape_widget_options(cnf)
                kwargs = _shape_widget_options(kwargs)
                return orig(self, cnf, **kwargs)
            return _configure
        cls.__init__ = make_init(original_init)
        cls.configure = make_configure(original_configure)
        cls.config = cls.configure
        cls._madrasati_rtl_patched = True

    # Arabic forms should be right aligned; keep their underlying values unchanged.
    for cls in (ttk.Entry, ttk.Combobox):
        original_init = cls.__init__
        if getattr(cls, "_madrasati_rtl_aligned", False):
            continue
        def make_aligned_init(orig):
            def _init(self, *args, **kwargs):
                kwargs.setdefault("justify", "right")
                return orig(self, *args, **kwargs)
            return _init
        cls.__init__ = make_aligned_init(original_init)
        cls._madrasati_rtl_aligned = True

    original_heading = ttk.Treeview.heading
    def _heading(self, column, option=None, **kwargs):
        if isinstance(option, dict):
            option = _shape_widget_options(option)
        kwargs = _shape_widget_options(kwargs)
        return original_heading(self, column, option, **kwargs)
    ttk.Treeview.heading = _heading

    original_insert = ttk.Treeview.insert
    def _insert(self, parent, index, iid=None, **kwargs):
        values = kwargs.get("values")
        if values is not None:
            kwargs["values"] = tuple(display_arabic_text(str(v)) if v is not None else "" for v in values)
        if not kwargs.get("tags"):
            try:
                count = len(self.get_children(parent))
                kwargs["tags"] = ("row_even" if count % 2 == 0 else "row_odd",)
            except Exception:
                pass
        return original_insert(self, parent, index, iid, **kwargs)
    ttk.Treeview.insert = _insert

    # Splash/login windows also use classic tk.Label widgets.
    if not getattr(tk.Label, "_madrasati_rtl_patched", False):
        _tk_label_init = tk.Label.__init__
        _tk_label_configure = tk.Label.configure
        def _tk_label_init_rtl(self, *args, **kwargs):
            return _tk_label_init(self, *args, **_shape_widget_options(kwargs))
        def _tk_label_configure_rtl(self, cnf=None, **kwargs):
            if isinstance(cnf, dict):
                cnf = _shape_widget_options(cnf)
            kwargs = _shape_widget_options(kwargs)
            return _tk_label_configure(self, cnf, **kwargs)
        tk.Label.__init__ = _tk_label_init_rtl
        tk.Label.configure = _tk_label_configure_rtl
        tk.Label.config = _tk_label_configure_rtl
        tk.Label._madrasati_rtl_patched = True


_install_ttk_display_helpers()

from updater import UpdateService, UpdateError, is_newer

APP_NAME = "مدرستي Pro — نظام إدارة المدارس"
APP_VERSION = "1.3.0"
APP_VENDOR = "SchoolManagerPro"
APP_DIR = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR))


def _default_data_dir() -> Path:
    """Keep writable school records outside Program Files; support a portable USB mode."""
    if (APP_DIR / ".portable").exists():
        return APP_DIR / "SchoolData"
    if sys.platform.startswith("win"):
        root = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
        return root / APP_VENDOR / "SchoolData"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / APP_VENDOR / "SchoolData"
    root = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
    return root / APP_VENDOR / "SchoolData"


DATA_DIR = _default_data_dir()
ATTACHMENTS_DIR = DATA_DIR / "attachments"
BACKUPS_DIR = DATA_DIR / "backups"
DB_PATH = DATA_DIR / "school_manager.db"
LOGS_DIR = DATA_DIR / "logs"
for directory in (DATA_DIR, ATTACHMENTS_DIR, BACKUPS_DIR, LOGS_DIR):
    directory.mkdir(parents=True, exist_ok=True)


PRINT_DIR = DATA_DIR / "printouts"
PRINT_DIR.mkdir(parents=True, exist_ok=True)


def render_print_html(title, headers, rows, school_name="مدرستي", font_family="Tahoma"):
    # Render a proper Arabic/RTL print page from logical database values.
    safe_title = html_lib.escape(str(title or "تقرير المدرسة"))
    safe_school = html_lib.escape(str(school_name or "مدرستي"))
    safe_font = html_lib.escape(str(font_family or "Tahoma"), quote=True)
    head_html = "".join(f"<th>{html_lib.escape(str(h if h is not None else ''))}</th>" for h in headers)
    body_rows = []
    for row in rows:
        cells = "".join(f"<td>{html_lib.escape(str(value if value is not None else ''))}</td>" for value in row)
        body_rows.append(f"<tr>{cells}</tr>")
    if not body_rows:
        body_rows.append(f'<tr><td class="empty" colspan="{max(1, len(headers))}">لا توجد سجلات للطباعة.</td></tr>')
    return f'''<!doctype html>
<html lang="ar" dir="rtl">
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{safe_title}</title>
<style>
@page {{ size: A4 landscape; margin: 12mm; }}
* {{ box-sizing: border-box; }}
body {{ direction: rtl; text-align: right; font-family: "{safe_font}", Tahoma, Arial, sans-serif; color: #1b2d3d; margin: 0; font-size: 12pt; }}
header {{ border-bottom: 3px solid #168b88; margin-bottom: 14px; padding: 0 0 10px; }}
h1 {{ font-size: 20pt; margin: 0 0 5px; color: #123653; }}
.meta {{ color: #516578; font-size: 10pt; }}
table {{ direction: rtl; border-collapse: collapse; width: 100%; margin-top: 12px; }}
th, td {{ border: 1px solid #c9d7e2; padding: 7px 8px; text-align: right; vertical-align: top; overflow-wrap: anywhere; }}
th {{ background: #e2edf5; color: #123653; font-weight: bold; }}
tr:nth-child(even) td {{ background: #f4f8fb; }}
.empty {{ text-align: center; padding: 24px; color: #667; }}
.actions {{ margin: 10px 0; }}
button {{ border: 0; border-radius: 5px; padding: 8px 14px; background: #168b88; color: white; font-size: 11pt; cursor: pointer; }}
@media print {{ .actions {{ display: none; }} }}
</style>
</head>
<body>
<header><h1>{safe_school}</h1><div><strong>{safe_title}</strong></div><div class="meta">تاريخ الطباعة: {datetime.now():%Y-%m-%d %H:%M}</div></header>
<div class="actions"><button onclick="window.print()">طباعة</button></div>
<table><thead><tr>{head_html}</tr></thead><tbody>{''.join(body_rows)}</tbody></table>
<script>window.addEventListener('load', () => setTimeout(() => window.print(), 450));</script>
</body></html>'''


def open_print_preview(title, headers, rows, school_name="مدرستي", font_family="Tahoma", parent=None):
    # Write a persistent UTF-8 print view and open it in the default browser.
    PRINT_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^A-Za-z0-9_-]+", "_", str(title)).strip("_")[:36] or "report"
    path = PRINT_DIR / f"{slug}_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:5]}.html"
    path.write_text(render_print_html(title, headers, rows, school_name, font_family), encoding="utf-8")
    opened = webbrowser.open(path.resolve().as_uri())
    if not opened:
        messagebox.showinfo("تم إعداد معاينة الطباعة", f"تم إنشاء صفحة الطباعة هنا:\n{path}\nافتح الملف واضغط Ctrl+P للطباعة.", parent=parent)
    return path


def _migrate_legacy_data() -> None:
    """One-time, non-destructive move from older builds that stored data beside the source."""
    legacy = APP_DIR / "SchoolData"
    legacy_db = legacy / "school_manager.db"
    if DB_PATH.exists() or not legacy_db.exists() or legacy_db.resolve() == DB_PATH.resolve():
        return
    try:
        shutil.copy2(legacy_db, DB_PATH)
        for folder_name, destination in (("attachments", ATTACHMENTS_DIR), ("backups", BACKUPS_DIR)):
            source = legacy / folder_name
            if source.exists():
                for item in source.iterdir():
                    target = destination / item.name
                    if item.is_file() and not target.exists():
                        shutil.copy2(item, target)
                    elif item.is_dir() and not target.exists():
                        shutil.copytree(item, target)
    except OSError:
        # Keep the original data untouched if migration is blocked; normal startup will show the error.
        pass


_migrate_legacy_data()

DAYS = ["الأحد", "الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت"]
ATTENDANCE_STATUSES = ["حاضر", "غائب", "متأخر", "إجازة"]
PERSON_STATUSES = ["فعّال", "غير فعّال", "منقول", "متخرج"]
DOCUMENT_CATEGORIES = ["بطاقة وطنية / هوية", "شهادة ميلاد", "وثيقة دراسية", "درجات", "نقل مدرسي", "موافقة ولي الأمر", "شهادة أو كتاب رسمي", "صورة مدرسية / أرشيفية", "أخرى"]

SCHEMA = """
PRAGMA foreign_keys = ON;
CREATE TABLE IF NOT EXISTS school_info (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    name TEXT NOT NULL DEFAULT 'مدرستي', director TEXT DEFAULT '', phone TEXT DEFAULT '',
    email TEXT DEFAULT '', address TEXT DEFAULT '', academic_year TEXT DEFAULT '',
    morning_start TEXT DEFAULT '08:00', periods_count INTEGER NOT NULL DEFAULT 7,
    period_minutes INTEGER NOT NULL DEFAULT 40, break_minutes INTEGER NOT NULL DEFAULT 5,
    notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS classes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, grade TEXT NOT NULL, section TEXT NOT NULL,
    room TEXT DEFAULT '', capacity INTEGER DEFAULT 0, academic_year TEXT DEFAULT '',
    UNIQUE(grade, section, academic_year)
);
CREATE TABLE IF NOT EXISTS teachers (
    id INTEGER PRIMARY KEY AUTOINCREMENT, employee_no TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL, specialization TEXT DEFAULT '', phone TEXT DEFAULT '',
    email TEXT DEFAULT '', hire_date TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'فعّال', notes TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS subjects (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, grade TEXT DEFAULT '',
    weekly_lessons INTEGER DEFAULT 0, teacher_id INTEGER,
    FOREIGN KEY (teacher_id) REFERENCES teachers(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS students (
    id INTEGER PRIMARY KEY AUTOINCREMENT, student_no TEXT NOT NULL UNIQUE,
    full_name TEXT NOT NULL, gender TEXT DEFAULT '', birth_date TEXT DEFAULT '', class_id INTEGER,
    guardian_name TEXT DEFAULT '', guardian_phone TEXT DEFAULT '', address TEXT DEFAULT '',
    enrollment_date TEXT DEFAULT '', status TEXT NOT NULL DEFAULT 'فعّال', notes TEXT DEFAULT '',
    FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS attendance (
    id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
    attendance_date TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'حاضر', note TEXT DEFAULT '',
    UNIQUE(student_id, attendance_date),
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS timetable (
    id INTEGER PRIMARY KEY AUTOINCREMENT, class_id INTEGER NOT NULL, day TEXT NOT NULL,
    period_no INTEGER NOT NULL, start_time TEXT DEFAULT '', end_time TEXT DEFAULT '',
    subject_id INTEGER, teacher_id INTEGER, room TEXT DEFAULT '', notes TEXT DEFAULT '',
    UNIQUE(class_id, day, period_no),
    FOREIGN KEY (class_id) REFERENCES classes(id) ON DELETE CASCADE,
    FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL,
    FOREIGN KEY (teacher_id) REFERENCES teachers(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, owner_type TEXT NOT NULL DEFAULT 'school',
    owner_id INTEGER, title TEXT NOT NULL, category TEXT DEFAULT 'أخرى', document_date TEXT DEFAULT '',
    file_path TEXT DEFAULT '', notes TEXT DEFAULT '', created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS assessments (
    id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
    subject_id INTEGER, exam_name TEXT NOT NULL, exam_date TEXT DEFAULT '', term TEXT DEFAULT '',
    score REAL NOT NULL DEFAULT 0, max_score REAL NOT NULL DEFAULT 100, notes TEXT DEFAULT '',
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE,
    FOREIGN KEY (subject_id) REFERENCES subjects(id) ON DELETE SET NULL
);
CREATE TABLE IF NOT EXISTS fee_records (
    id INTEGER PRIMARY KEY AUTOINCREMENT, student_id INTEGER NOT NULL,
    academic_year TEXT DEFAULT '', fee_type TEXT NOT NULL DEFAULT 'أجور دراسية',
    amount REAL NOT NULL DEFAULT 0, paid_amount REAL NOT NULL DEFAULT 0,
    due_date TEXT DEFAULT '', payment_date TEXT DEFAULT '', notes TEXT DEFAULT '',
    FOREIGN KEY (student_id) REFERENCES students(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS app_users (
    id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
    full_name TEXT NOT NULL, password_salt TEXT NOT NULL, password_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0, is_active INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_students_class ON students(class_id);
CREATE INDEX IF NOT EXISTS idx_attendance_date ON attendance(attendance_date);
CREATE INDEX IF NOT EXISTS idx_timetable_class_day ON timetable(class_id, day);
CREATE INDEX IF NOT EXISTS idx_assessments_student ON assessments(student_id, exam_date);
CREATE INDEX IF NOT EXISTS idx_fees_student ON fee_records(student_id, academic_year);
"""


def today_text() -> str:
    return date.today().isoformat()


def resolve_document_path(raw_path: str | Path | None) -> Path | None:
    """Resolve legacy absolute or portable relative attachment paths."""
    if not raw_path:
        return None
    path = Path(str(raw_path))
    if path.is_absolute():
        if path.exists():
            return path
        # Recover attachments when a database is moved to another computer.
        candidate = ATTACHMENTS_DIR / path.name
        return candidate if candidate.exists() else path
    candidate = DATA_DIR / path
    if candidate.exists():
        return candidate
    fallback = ATTACHMENTS_DIR / path.name
    return fallback if fallback.exists() else candidate


def safe_int(value, default=0):
    try:
        return int(str(value).strip()) if str(value).strip() else default
    except (TypeError, ValueError):
        return default


def hash_password(password: str, salt: bytes | None = None) -> tuple[str, str]:
    """PBKDF2 password storage; never save the original password."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, digest_hex: str) -> bool:
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 310_000)
    return hmac.compare_digest(actual, expected)


class SchoolDB:
    def __init__(self, path: Path = DB_PATH):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=15)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA busy_timeout = 15000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.executescript(SCHEMA)
        self.conn.execute("INSERT OR IGNORE INTO school_info (id, name, academic_year) VALUES (1, 'مدرستي', '')")
        self.conn.commit()

    def query(self, sql, params=()):
        return self.conn.execute(sql, params).fetchall()

    def one(self, sql, params=()):
        return self.conn.execute(sql, params).fetchone()

    def execute(self, sql, params=()):
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.lastrowid

    def close(self):
        self.conn.close()

    def backup(self, target: Path):
        target.parent.mkdir(parents=True, exist_ok=True)
        dest = sqlite3.connect(target)
        try:
            self.conn.backup(dest)
            dest.commit()
        finally:
            dest.close()


class EntityManager:
    """واجهة CRUD قابلة لإعادة الاستخدام للطلاب والملاك والصفوف والمواد والجدول."""
    def __init__(self, app: "SchoolApp", parent, config: dict):
        self.app, self.db, self.config = app, app.db, config
        self.table = config["table"]
        self.current_id = None
        self.widgets = {}
        self.combo_maps = {}
        self.search_var = tk.StringVar()
        self.frame = ttk.Frame(parent, padding=12)
        self._build()
        self.refresh()

    def _build(self):
        cfg = self.config
        ttk.Label(self.frame, text=f"📘  إدارة {cfg['title']}", style="PageTitle.TLabel", anchor="e").pack(fill="x", pady=(0, 8))
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="🔎 بحث:").pack(side="right", padx=(5, 3))
        search = ttk.Entry(toolbar, textvariable=self.search_var, width=28)
        search.pack(side="right", padx=4)
        ttk.Label(toolbar, text="💡 انقر مرتين على السجل لتعديله في الحقول أعلاه", style="Hint.TLabel").pack(side="right", padx=12)
        search.bind("<KeyRelease>", lambda _e: self.refresh())
        ttk.Button(toolbar, text="تحديث", command=self.refresh).pack(side="right", padx=3)
        ttk.Button(toolbar, text="🖨️ طباعة القائمة", command=self.print_records).pack(side="left", padx=3)
        ttk.Button(toolbar, text="تصدير CSV", command=self.export_csv).pack(side="left", padx=3)
        if cfg.get("table") == "students":
            ttk.Button(toolbar, text="قالب CSV للطلاب", command=self.student_template).pack(side="left", padx=3)
            ttk.Button(toolbar, text="استيراد CSV", command=self.import_students_csv).pack(side="left", padx=3)

        form = ttk.LabelFrame(self.frame, text=f"بيانات {cfg['title']}", padding=10)
        form.pack(fill="x", pady=(0, 10))
        cols = 4
        for idx, field in enumerate(cfg["fields"]):
            r = idx // 2
            c = (idx % 2) * 2
            label_text = field["label"]
            label_icon = "✏️"
            label_key = label_text.lower()
            if any(k in label_key for k in ("اسم", "الطالب", "المدرس", "ولي الأمر")):
                label_icon = "👤"
            elif any(k in label_key for k in ("تاريخ", "السنة", "الفصل", "اليوم", "الدوام")):
                label_icon = "📅"
            elif any(k in label_key for k in ("هاتف", "بريد", "العنوان", "الغرفة", "القاعة")):
                label_icon = "📍"
            elif any(k in label_key for k in ("درجة", "امتحان", "مادة", "حصة", "صف", "شعبة")):
                label_icon = "📚"
            elif any(k in label_key for k in ("مبلغ", "رسم", "دفع", "قسط")):
                label_icon = "💰"
            elif any(k in label_key for k in ("وثيقة", "ملف", "ملاحظات")):
                label_icon = "🗂️"
            label = ttk.Label(form, text=label_icon + "  " + label_text + (" *" if field.get("required") else ""))
            label.grid(row=r, column=c, sticky="e", padx=(8, 4), pady=5)
            kind = field.get("kind", "entry")
            if kind == "combo":
                widget = ttk.Combobox(form, state="readonly", width=30)
                widget.bind("<<ComboboxSelected>>", lambda _e, n=field["name"]: self._field_changed(n))
            else:
                widget = ttk.Entry(form, width=33)
            widget.grid(row=r, column=c + 1, sticky="ew", padx=(0, 14), pady=5)
            self.widgets[field["name"]] = widget
            if kind == "combo":
                self._populate_combo(field)
        for c in range(cols):
            form.columnconfigure(c, weight=1 if c % 2 else 0)

        buttons = ttk.Frame(form)
        buttons.grid(row=(len(cfg["fields"]) + 1) // 2, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="➕ حفظ سجل جديد", command=self.add_record, style="Primary.TButton").pack(side="right", padx=3)
        ttk.Button(buttons, text="✏️ تحديث المحدد", command=self.update_record, style="Success.TButton").pack(side="right", padx=3)
        ttk.Button(buttons, text="🗑️ حذف المحدد", command=self.delete_record, style="Danger.TButton").pack(side="right", padx=3)
        ttk.Button(buttons, text="↺ مسح الحقول", command=self.clear_form).pack(side="right", padx=3)
        if cfg.get("table") == "timetable":
            ttk.Button(buttons, text="حساب وقت الحصة", command=self.autofill_period_times).pack(side="left", padx=3)

        grid_frame = ttk.Frame(self.frame)
        grid_frame.pack(fill="both", expand=True)
        col_names = [col[0] for col in cfg["display_columns"]]
        self.tree = ttk.Treeview(grid_frame, columns=col_names, show="headings", selectmode="browse")
        for col, heading, width in cfg["display_columns"]:
            self.tree.heading(col, text=heading)
            self.tree.column(col, width=width, minwidth=70, anchor="center", stretch=True)
        yscroll = ttk.Scrollbar(grid_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(grid_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        xscroll.grid(row=1, column=0, sticky="ew")
        grid_frame.rowconfigure(0, weight=1)
        grid_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.load_selected)
        self.tree.bind("<Double-1>", self.load_selected)
        self.tree.bind("<MouseWheel>", lambda event: self.tree.yview_scroll(-1 * int(event.delta / 120), "units"))
        self.empty_hint = ttk.Label(
            grid_frame, text=self._empty_message(), style="EmptyState.TLabel",
            justify="center", anchor="center"
        )
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

    def _empty_message(self):
        table = self.config.get("table")
        messages = {
            "classes": "لا توجد صفوف أو شعب مسجلة بعد.\nأدخل الصف والشعبة في النموذج أعلاه ثم اضغط «حفظ سجل جديد».",
            "students": "لا يوجد طلاب مسجلون بعد.\nأنشئ الصفوف والشعب أولاً، ثم أضف بيانات الطالب من النموذج أعلاه.",
            "teachers": "لا توجد بيانات مدرسين بعد.\nأدخل الرقم الوظيفي والاسم الكامل ثم احفظ السجل.",
            "subjects": "لا توجد مواد دراسية بعد.\nأضف اسم المادة وحدد الصف والمدرس عند الحاجة.",
            "assessments": "لا توجد درجات مسجلة بعد.\nأضف الطالب أولاً، ثم أدخل المادة واسم الامتحان والدرجة.",
            "timetable": "جدول الحصص فارغ حالياً.\nأنشئ الصفوف والمواد والمدرسين أولاً ثم أضف الحصص.",
            "fee_records": "لا توجد رسوم أو أقساط مسجلة بعد.\nأضف الطالب أولاً ثم أدخل المبلغ والمدفوع.",
        }
        return messages.get(table, "لا توجد سجلات بعد. استخدم النموذج أعلاه لإضافة أول سجل.")

    def _populate_combo(self, field):
        widget = self.widgets[field["name"]]
        try:
            options = field["options"]()
        except Exception:
            options = []
        labels = [display_arabic_text(str(label)) for label, _value in options]
        self.combo_maps[field["name"]] = {display_arabic_text(str(label)): value for label, value in options}
        self.combo_maps[field["name"] + "_reverse"] = {str(value): display_arabic_text(str(label)) for label, value in options}
        old = widget.get()
        widget["values"] = labels
        if old in labels:
            widget.set(old)
        elif labels:
            widget.set("")

    def refresh_dynamic_options(self):
        for field in self.config["fields"]:
            if field.get("kind") == "combo":
                self._populate_combo(field)

    def _field_changed(self, name):
        if self.table == "timetable" and name == "period_no":
            self.autofill_period_times()

    def _form_values(self, validate=True):
        out = {}
        for field in self.config["fields"]:
            name = field["name"]
            widget = self.widgets[name]
            raw = widget.get().strip()
            if validate and field.get("required") and not raw:
                messagebox.showwarning("بيانات ناقصة", f"يرجى إدخال: {field['label']}", parent=self.frame)
                widget.focus_set()
                return None
            if field.get("kind") == "combo":
                raw = self.combo_maps.get(name, {}).get(raw, None)
            if name in ("capacity", "weekly_lessons", "period_no"):
                raw = safe_int(raw, 0) if raw is not None else None
            elif name in ("class_id", "teacher_id", "subject_id", "student_id"):
                raw = safe_int(raw, 0) or None if raw not in (None, "") else None
            elif name in ("score", "max_score", "amount", "paid_amount"):
                try:
                    raw = float(raw) if raw not in (None, "") else 0.0
                except ValueError:
                    messagebox.showwarning("قيمة رقمية غير صحيحة", f"أدخل قيمة رقمية صحيحة في {field['label']}.", parent=self.frame)
                    return None
            elif raw == "":
                raw = None
            out[name] = raw
        return out

    def _check_extra(self, vals):
        if self.table == "timetable":
            if not vals.get("class_id"):
                messagebox.showwarning("بيانات ناقصة", "اختر الصف والشعبة أولاً.", parent=self.frame)
                return False
            if vals.get("start_time") and not self._valid_time(vals["start_time"]):
                messagebox.showwarning("وقت غير صحيح", "اكتب وقت البداية بصيغة 08:00.", parent=self.frame)
                return False
            if vals.get("end_time") and not self._valid_time(vals["end_time"]):
                messagebox.showwarning("وقت غير صحيح", "اكتب وقت النهاية بصيغة 08:40.", parent=self.frame)
                return False
            if vals.get("start_time") and vals.get("end_time") and vals["end_time"] <= vals["start_time"]:
                messagebox.showwarning("وقت غير صحيح", "يجب أن يكون وقت النهاية بعد وقت البداية.", parent=self.frame)
                return False
        if self.table == "students" and vals.get("birth_date") and not self._valid_date(vals["birth_date"]):
            messagebox.showwarning("تاريخ غير صحيح", "اكتب تاريخ الميلاد بصيغة YYYY-MM-DD.", parent=self.frame)
            return False
        if self.table == "assessments":
            if vals.get("exam_date") and not self._valid_date(vals["exam_date"]):
                messagebox.showwarning("تاريخ غير صحيح", "اكتب تاريخ الامتحان بصيغة YYYY-MM-DD.", parent=self.frame)
                return False
            if vals.get("score", 0) < 0 or vals.get("max_score", 0) <= 0 or vals.get("score", 0) > vals.get("max_score", 0):
                messagebox.showwarning("درجة غير صحيحة", "يجب أن تكون الدرجة من صفر إلى الدرجة الكاملة، والدرجة الكاملة أكبر من صفر.", parent=self.frame)
                return False
        if self.table == "fee_records":
            if vals.get("amount", 0) < 0 or vals.get("paid_amount", 0) < 0 or vals.get("paid_amount", 0) > vals.get("amount", 0):
                messagebox.showwarning("مبلغ غير صحيح", "المبلغ المدفوع يجب أن يكون بين صفر والمبلغ الكلي.", parent=self.frame)
                return False
            for date_key, label in (("due_date", "تاريخ الاستحقاق"), ("payment_date", "تاريخ الدفع")):
                if vals.get(date_key) and not self._valid_date(vals[date_key]):
                    messagebox.showwarning("تاريخ غير صحيح", f"{label}: استخدم YYYY-MM-DD.", parent=self.frame)
                    return False
        return True

    @staticmethod
    def _valid_time(value):
        try:
            datetime.strptime(value, "%H:%M")
            return True
        except ValueError:
            return False

    @staticmethod
    def _valid_date(value):
        try:
            datetime.strptime(value, "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def add_record(self):
        vals = self._form_values()
        if vals is None or not self._check_extra(vals):
            return
        try:
            names = list(vals.keys())
            self.db.execute(f"INSERT INTO {self.table} ({','.join(names)}) VALUES ({','.join('?' for _ in names)})", [vals[n] for n in names])
            messagebox.showinfo("تم الحفظ", "تمت إضافة السجل بنجاح.", parent=self.frame)
            self.clear_form()
            self.app.refresh_everything()
        except sqlite3.IntegrityError as exc:
            messagebox.showerror("تعذّر الحفظ", self._friendly_db_error(exc), parent=self.frame)
        except sqlite3.Error as exc:
            messagebox.showerror("خطأ قاعدة البيانات", str(exc), parent=self.frame)

    def update_record(self):
        if not self.current_id:
            messagebox.showwarning("اختيار مطلوب", "حدد سجلاً من الجدول أولاً.", parent=self.frame)
            return
        vals = self._form_values()
        if vals is None or not self._check_extra(vals):
            return
        try:
            names = list(vals.keys())
            self.db.execute(f"UPDATE {self.table} SET {','.join(n + '=?' for n in names)} WHERE id=?", [vals[n] for n in names] + [self.current_id])
            messagebox.showinfo("تم التحديث", "تم تحديث السجل بنجاح.", parent=self.frame)
            self.app.refresh_everything()
        except sqlite3.IntegrityError as exc:
            messagebox.showerror("تعذّر التحديث", self._friendly_db_error(exc), parent=self.frame)
        except sqlite3.Error as exc:
            messagebox.showerror("خطأ قاعدة البيانات", str(exc), parent=self.frame)

    def delete_record(self):
        if not self.current_id:
            messagebox.showwarning("اختيار مطلوب", "حدد السجل الذي تريد حذفه.", parent=self.frame)
            return
        if not messagebox.askyesno("تأكيد الحذف", "هل أنت متأكد من حذف السجل المحدد؟ قد تتأثر السجلات المرتبطة به.", parent=self.frame):
            return
        try:
            self.db.execute(f"DELETE FROM {self.table} WHERE id=?", (self.current_id,))
            self.clear_form()
            self.app.refresh_everything()
        except sqlite3.IntegrityError as exc:
            messagebox.showerror("تعذّر الحذف", self._friendly_db_error(exc), parent=self.frame)

    @staticmethod
    def _friendly_db_error(exc):
        msg = str(exc).lower()
        if "unique constraint failed" in msg:
            return "هذا الرقم أو السجل موجود مسبقاً. تحقق من الرقم التعريفي أو الصف والشعبة والسنة الدراسية."
        if "foreign key constraint failed" in msg:
            return "لا يمكن حذف هذا السجل لأنه مرتبط ببيانات أخرى."
        return str(exc)

    def clear_form(self):
        self.current_id = None
        for field in self.config["fields"]:
            widget = self.widgets[field["name"]]
            widget.set("") if isinstance(widget, ttk.Combobox) else widget.delete(0, "end")
        self.tree.selection_remove(self.tree.selection())

    def refresh(self):
        self.refresh_dynamic_options()
        for item in self.tree.get_children():
            self.tree.delete(item)
        search = self.search_var.get().strip()
        query = self.config["list_query"]
        params = []
        search_fields = self.config.get("search_fields", [])
        if search and search_fields:
            where = " OR ".join(f"{field} LIKE ?" for field in search_fields)
            # Query uses base aliases defined by each manager.
            query += f" WHERE {where}" if " WHERE " not in query.upper() else f" AND ({where})"
            params = [f"%{search}%"] * len(search_fields)
        order = self.config.get("order_by", "id DESC")
        query += f" ORDER BY {order}"
        try:
            rows = self.db.query(query, params)
        except sqlite3.Error:
            rows = []
        for row_index, row in enumerate(rows):
            vals = [row[col] if row[col] is not None else "" for col, _heading, _width in self.config["display_columns"]]
            self.tree.insert("", "end", iid=str(row["id"]), values=vals, tags=("row_even" if row_index % 2 == 0 else "row_odd",))
        if rows:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.configure(
                text=("لا توجد نتائج مطابقة للبحث. جرّب كلمة أخرى." if search else self._empty_message())
            )
            self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

    def load_selected(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        self.current_id = safe_int(selection[0])
        row = self.db.one(f"SELECT * FROM {self.table} WHERE id=?", (self.current_id,))
        if not row:
            return
        self.refresh_dynamic_options()
        for field in self.config["fields"]:
            name = field["name"]
            val = row[name]
            widget = self.widgets[name]
            if field.get("kind") == "combo":
                label = self.combo_maps.get(name + "_reverse", {}).get(str(val), "")
                widget.set(label)
            else:
                widget.delete(0, "end")
                if val is not None:
                    widget.insert(0, str(val))

    def print_records(self):
        """Print the same filtered list currently shown in this manager."""
        query = self.config["list_query"]
        params = []
        search = self.search_var.get().strip()
        search_fields = self.config.get("search_fields", [])
        if search and search_fields:
            where = " OR ".join(f"{field} LIKE ?" for field in search_fields)
            query += f" WHERE {where}" if " WHERE " not in query.upper() else f" AND ({where})"
            params = [f"%{search}%"] * len(search_fields)
        query += f" ORDER BY {self.config.get('order_by', 'id DESC')}"
        try:
            records = self.db.query(query, params)
            columns = self.config["display_columns"]
            headers = [heading for _col, heading, _width in columns]
            rows = [[record[col] if record[col] is not None else "" for col, _heading, _width in columns] for record in records]
            school = self.app.school_settings().get("name", "مدرستي")
            open_print_preview(f"قائمة {self.config['title']}", headers, rows, school, self.app.font_family, parent=self.frame)
        except Exception as exc:
            messagebox.showerror("تعذّرت معاينة الطباعة", str(exc), parent=self.frame)

    def export_csv(self):
        path = filedialog.asksaveasfilename(parent=self.frame, title="حفظ البيانات بصيغة CSV", defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile=f"{self.table}_{today_text()}.csv")
        if not path:
            return
        try:
            rows = self.db.query(self.config["list_query"] + f" ORDER BY {self.config.get('order_by', 'id DESC')}")
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow([heading for _col, heading, _width in self.config["display_columns"]])
                for row in rows:
                    writer.writerow([row[col] if row[col] is not None else "" for col, _heading, _width in self.config["display_columns"]])
            messagebox.showinfo("اكتمل التصدير", f"تم تصدير {len(rows)} سجل/سجلات.", parent=self.frame)
        except Exception as exc:
            messagebox.showerror("خطأ بالتصدير", str(exc), parent=self.frame)

    def student_template(self):
        path = filedialog.asksaveasfilename(parent=self.frame, title="حفظ قالب استيراد الطلاب", defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile="students_template.csv")
        if not path:
            return
        headers = ["student_no", "full_name", "gender", "birth_date", "class_grade", "class_section", "guardian_name", "guardian_phone", "address", "enrollment_date", "status", "notes"]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
        messagebox.showinfo("قالب جاهز", "تم إنشاء قالب فارغ بدون طلاب تجريبيين. أضف بيانات الطلبة أسفل أسماء الأعمدة، واكتب الصف والشعبة المطابقين لما هو مسجل في البرنامج، ثم استورد الملف.", parent=self.frame)

    def import_students_csv(self):
        path = filedialog.askopenfilename(parent=self.frame, title="اختيار ملف الطلاب", filetypes=[("CSV", "*.csv")])
        if not path:
            return
        added, skipped = 0, 0
        try:
            with open(path, "r", newline="", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                required = {"student_no", "full_name"}
                if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
                    messagebox.showerror("قالب غير صحيح", "يجب أن يتضمن الملف عمودَي student_no وfull_name على الأقل. استخدم قالب CSV للطلاب.", parent=self.frame)
                    return
                for item in reader:
                    student_no = (item.get("student_no") or "").strip()
                    full_name = (item.get("full_name") or "").strip()
                    if not student_no or not full_name:
                        skipped += 1
                        continue
                    grade, section = (item.get("class_grade") or "").strip(), (item.get("class_section") or "").strip()
                    if not grade or not section:
                        skipped += 1
                        continue
                    class_row = self.db.one("SELECT id FROM classes WHERE grade=? AND section=? ORDER BY id DESC LIMIT 1", (grade, section))
                    if not class_row:
                        skipped += 1
                        continue
                    class_id = class_row["id"]
                    try:
                        self.db.execute("""INSERT INTO students(student_no,full_name,gender,birth_date,class_id,guardian_name,guardian_phone,address,enrollment_date,status,notes)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?)""", (student_no, full_name, (item.get("gender") or "").strip(), (item.get("birth_date") or "").strip(), class_id,
                            (item.get("guardian_name") or "").strip(), (item.get("guardian_phone") or "").strip(), (item.get("address") or "").strip(),
                            (item.get("enrollment_date") or today_text()).strip(), (item.get("status") or "فعّال").strip(), (item.get("notes") or "").strip()))
                        added += 1
                    except sqlite3.IntegrityError:
                        skipped += 1
            self.app.refresh_everything()
            messagebox.showinfo("نتيجة الاستيراد", f"تم استيراد {added} طالباً. تم تجاوز {skipped} صف/صفوف (بيانات ناقصة أو رقم مكرر).", parent=self.frame)
        except Exception as exc:
            messagebox.showerror("فشل الاستيراد", str(exc), parent=self.frame)

    def autofill_period_times(self):
        if self.table != "timetable":
            return
        try:
            p = safe_int(self.widgets["period_no"].get(), 0)
            if p <= 0:
                return
            settings = self.app.school_settings()
            start = datetime.strptime(settings["morning_start"] or "08:00", "%H:%M")
            duration = max(1, safe_int(settings["period_minutes"], 40))
            pause = max(0, safe_int(settings["break_minutes"], 5))
            # استراحة قصيرة بعد كل حصة باستثناء الحصة الأخيرة.
            minutes_before = (p - 1) * duration + (p - 1) * pause
            st = start + timedelta(minutes=minutes_before)
            en = st + timedelta(minutes=duration)
            for key, value in (("start_time", st.strftime("%H:%M")), ("end_time", en.strftime("%H:%M"))):
                widget = self.widgets.get(key)
                if widget:
                    widget.delete(0, "end")
                    widget.insert(0, value)
        except (ValueError, KeyError):
            messagebox.showwarning("إعدادات الوقت", "راجع وقت بداية الدوام بصيغة HH:MM في بيانات المدرسة.", parent=self.frame)


class AttendanceTab:
    def __init__(self, app, parent):
        self.app, self.db = app, app.db
        self.frame = ttk.Frame(parent, padding=12)
        self.date_var = tk.StringVar(value=today_text())
        self.class_var = tk.StringVar()
        self.status_display_to_raw = {display_arabic_text(v): v for v in ATTENDANCE_STATUSES}
        self.status_var = tk.StringVar(value=display_arabic_text("حاضر"))
        self.note_var = tk.StringVar()
        self.class_map = {}
        self.selected_student_id = None
        self._build()
        self.refresh_classes()
        self.load_roster()

    def _build(self):
        filters = ttk.LabelFrame(self.frame, text="🗓️ تحديد اليوم والصف", padding=10)
        filters.pack(fill="x", pady=(0, 8))
        ttk.Label(filters, text="التاريخ (YYYY-MM-DD):").pack(side="right", padx=5)
        ttk.Entry(filters, textvariable=self.date_var, width=14).pack(side="right", padx=5)
        ttk.Label(filters, text="الصف والشعبة:").pack(side="right", padx=5)
        self.class_combo = ttk.Combobox(filters, textvariable=self.class_var, state="readonly", width=27)
        self.class_combo.pack(side="right", padx=5)
        self.class_combo.bind("<<ComboboxSelected>>", lambda _e: self.load_roster())
        ttk.Button(filters, text="عرض الطلبة", command=self.load_roster).pack(side="right", padx=5)
        ttk.Button(filters, text="🖨️ طباعة الكشف", command=self.print_roster).pack(side="left", padx=5)
        ttk.Button(filters, text="تسجيل الجميع حاضرين", command=self.mark_all_present).pack(side="left", padx=5)

        editor = ttk.LabelFrame(self.frame, text="📝 تسجيل حالة الطالب المحدد", padding=10)
        editor.pack(fill="x", pady=(0, 8))
        ttk.Label(editor, text="الحالة:").pack(side="right", padx=5)
        ttk.Combobox(editor, textvariable=self.status_var, values=[display_arabic_text(v) for v in ATTENDANCE_STATUSES], state="readonly", width=12).pack(side="right", padx=5)
        ttk.Label(editor, text="ملاحظة:").pack(side="right", padx=5)
        ttk.Entry(editor, textvariable=self.note_var, width=44).pack(side="right", padx=5)
        ttk.Button(editor, text="حفظ الحالة المحددة", command=self.save_one).pack(side="right", padx=5)
        ttk.Label(editor, text="اختر الطالب من الجدول، ثم احفظ حالته أو استخدم التسجيل السريع أدناه.", style="Hint.TLabel").pack(side="left", padx=8)

        quick = ttk.Frame(self.frame)
        quick.pack(fill="x", pady=(0, 8))
        ttk.Label(quick, text="تسجيل سريع للطالب المحدد:", style="Subtitle.TLabel").pack(side="right", padx=(4, 10))
        for label, status in [("✅ حاضر", "حاضر"), ("❌ غائب", "غائب"), ("⏰ متأخر", "متأخر"), ("📝 إجازة", "إجازة")]:
            ttk.Button(quick, text=label, command=lambda value=status: self.save_quick_status(value), style=("Success.TButton" if status == "حاضر" else ("Danger.TButton" if status == "غائب" else "Step.TButton"))).pack(side="right", padx=3)

        grid = ttk.Frame(self.frame)
        grid.pack(fill="both", expand=True)
        cols = ("student_no", "full_name", "class_name", "status", "note")
        self.tree = ttk.Treeview(grid, columns=cols, show="headings", selectmode="browse")
        labels = {"student_no": "رقم الطالب", "full_name": "اسم الطالب", "class_name": "الصف / الشعبة", "status": "الحالة", "note": "ملاحظة"}
        widths = {"student_no": 120, "full_name": 240, "class_name": 160, "status": 100, "note": 260}
        for col in cols:
            self.tree.heading(col, text=labels[col])
            self.tree.column(col, width=widths[col], anchor="center")
        y = ttk.Scrollbar(grid, orient="vertical", command=self.tree.yview)
        x = ttk.Scrollbar(grid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        grid.rowconfigure(0, weight=1)
        grid.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.select_student)
        self.tree.bind("<Double-1>", self.select_student)
        self.tree.bind("<MouseWheel>", lambda event: self.tree.yview_scroll(-1 * int(event.delta / 120), "units"))
        self.empty_hint = ttk.Label(
            grid, text="أضف الصفوف والشعب، ثم سجّل الطلاب حتى تظهر الأسماء هنا.",
            style="EmptyState.TLabel", justify="center", anchor="center"
        )
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")
        self.summary = ttk.Label(self.frame, text="", style="Hint.TLabel")
        self.summary.pack(anchor="e", pady=(7, 0))

    def print_roster(self):
        if not self._valid_date():
            messagebox.showwarning("تاريخ غير صحيح", "اكتب التاريخ بصيغة YYYY-MM-DD.", parent=self.frame)
            return
        class_id = self.class_map.get(self.class_var.get())
        if not class_id:
            messagebox.showinfo("اختر صفاً", "أضف الصف والشعبة ثم اخترهما قبل الطباعة.", parent=self.frame)
            return
        records = self.db.query("""SELECT s.student_no,s.full_name,COALESCE(c.grade,'') grade,COALESCE(c.section,'') section,
            COALESCE(a.status,'غير مسجل') status,COALESCE(a.note,'') note
            FROM students s LEFT JOIN classes c ON c.id=s.class_id
            LEFT JOIN attendance a ON a.student_id=s.id AND a.attendance_date=?
            WHERE s.class_id=? AND s.status='فعّال' ORDER BY s.full_name""", (self.date_var.get().strip(), class_id))
        rows = [[r['student_no'], r['full_name'], f"{r['grade']} / {r['section']}", r['status'], r['note']] for r in records]
        open_print_preview(f"كشف الحضور والغياب — {self.date_var.get().strip()}",
            ["رقم الطالب", "اسم الطالب", "الصف / الشعبة", "الحالة", "ملاحظة"], rows,
            self.app.school_settings().get("name", "مدرستي"), self.app.font_family, parent=self.frame)

    def refresh_classes(self):
        rows = self.db.query("SELECT id,grade,section,academic_year FROM classes ORDER BY grade,section")
        self.class_map = {display_arabic_text(f"{r['grade']} - {r['section']}" + (f" ({r['academic_year']})" if r['academic_year'] else "")): r["id"] for r in rows}
        values = list(self.class_map)
        self.class_combo["values"] = values
        if self.class_var.get() not in values:
            self.class_var.set(values[0] if values else "")

    def load_roster(self):
        self.selected_student_id = None
        self.status_var.set(display_arabic_text("حاضر"))
        self.note_var.set("")
        self.refresh_classes()
        if not self._valid_date():
            messagebox.showwarning("تاريخ غير صحيح", "اكتب التاريخ بصيغة YYYY-MM-DD.", parent=self.frame)
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        class_id = self.class_map.get(self.class_var.get())
        if not class_id:
            self.empty_hint.configure(text="لا توجد صفوف وشعب بعد. افتح «الصفوف والشعب» من القائمة الجانبية وأضف صفاً وشعبة أولاً.")
            self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")
            self.summary.config(text="الخطوة الأولى: إنشاء صف وشعبة، ثم إضافة الطلاب.")
            return
        rows = self.db.query("""SELECT s.id,s.student_no,s.full_name,c.grade,c.section,a.status,a.note
            FROM students s LEFT JOIN classes c ON c.id=s.class_id
            LEFT JOIN attendance a ON a.student_id=s.id AND a.attendance_date=?
            WHERE s.class_id=? AND s.status='فعّال' ORDER BY s.full_name""", (self.date_var.get().strip(), class_id))
        for r in rows:
            class_name = f"{r['grade'] or ''} / {r['section'] or ''}"
            self.tree.insert("", "end", iid=str(r["id"]), values=(r["student_no"], r["full_name"], class_name, r["status"] or "غير مسجل", r["note"] or ""))
        if rows:
            self.empty_hint.place_forget()
        else:
            self.empty_hint.configure(text="لا يوجد طلاب فعّالون في هذا الصف بعد. افتح «الطلاب» من القائمة الجانبية وأضف أسماءهم.")
            self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")
        self._update_summary()

    def save_quick_status(self, status):
        if not self.selected_student_id:
            messagebox.showwarning("اختيار مطلوب", "حدد طالباً من الجدول أولاً، ثم استخدم زر الحالة السريع.", parent=self.frame)
            return
        self.status_var.set(display_arabic_text(status))
        self.save_one()

    def _valid_date(self):
        try:
            datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d")
            return True
        except ValueError:
            return False

    def select_student(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        self.selected_student_id = safe_int(selection[0])
        # Read the original logical values from SQLite; table labels may be reshaped only for display.
        record = self.db.one(
            "SELECT status,note FROM attendance WHERE student_id=? AND attendance_date=?",
            (self.selected_student_id, self.date_var.get().strip()),
        )
        if record:
            self.status_var.set(display_arabic_text(record["status"] or "حاضر"))
            self.note_var.set(record["note"] or "")
        else:
            self.status_var.set(display_arabic_text("حاضر"))
            self.note_var.set("")

    def save_one(self):
        if not self.selected_student_id:
            messagebox.showwarning("اختيار مطلوب", "اختر طالباً من القائمة أولاً.", parent=self.frame)
            return
        if not self._valid_date():
            messagebox.showwarning("تاريخ غير صحيح", "اكتب التاريخ بصيغة YYYY-MM-DD.", parent=self.frame)
            return
        try:
            self.db.execute("""INSERT INTO attendance(student_id,attendance_date,status,note) VALUES(?,?,?,?)
                ON CONFLICT(student_id,attendance_date) DO UPDATE SET status=excluded.status,note=excluded.note""",
                (self.selected_student_id, self.date_var.get().strip(), self.status_display_to_raw.get(self.status_var.get(), self.status_var.get()), self.note_var.get().strip()))
            self.load_roster()
            self.app.refresh_dashboard()
        except sqlite3.Error as exc:
            messagebox.showerror("تعذّر حفظ الحضور", str(exc), parent=self.frame)

    def mark_all_present(self):
        if not self._valid_date():
            messagebox.showwarning("تاريخ غير صحيح", "اكتب التاريخ بصيغة YYYY-MM-DD.", parent=self.frame)
            return
        class_id = self.class_map.get(self.class_var.get())
        if not class_id:
            messagebox.showwarning("لا يوجد صف", "اختر صفاً وشعبة أولاً.", parent=self.frame)
            return
        rows = self.db.query("SELECT id FROM students WHERE class_id=? AND status='فعّال'", (class_id,))
        if not rows:
            messagebox.showinfo("لا توجد أسماء", "لا يوجد طلاب فعّالون في هذا الصف.", parent=self.frame)
            return
        if not messagebox.askyesno("تأكيد", f"سيتم تسجيل {len(rows)} طالباً بحالة حاضر في تاريخ {self.date_var.get()}. هل تريد المتابعة؟", parent=self.frame):
            return
        try:
            with self.db.conn:
                for r in rows:
                    self.db.conn.execute("""INSERT INTO attendance(student_id,attendance_date,status,note) VALUES(?,?,?, '')
                        ON CONFLICT(student_id,attendance_date) DO UPDATE SET status='حاضر',note=''""", (r["id"], self.date_var.get().strip()))
            self.load_roster()
            self.app.refresh_dashboard()
            messagebox.showinfo("تم", "تم تسجيل حضور جميع الطلبة في الصف المحدد.", parent=self.frame)
        except sqlite3.Error as exc:
            messagebox.showerror("خطأ", str(exc), parent=self.frame)

    def _update_summary(self):
        # Treeview values are display-shaped, so compute attendance counts from SQLite's logical values.
        class_id = self.class_map.get(self.class_var.get())
        if not class_id:
            self.summary.config(text="الخطوة الأولى: إنشاء صف وشعبة، ثم إضافة الطلاب.")
            return
        rows = self.db.query("""SELECT COALESCE(a.status,'غير مسجل') AS status, COUNT(*) AS n
            FROM students s LEFT JOIN attendance a
              ON a.student_id=s.id AND a.attendance_date=?
            WHERE s.class_id=? AND s.status='فعّال'
            GROUP BY COALESCE(a.status,'غير مسجل')""", (self.date_var.get().strip(), class_id))
        counts = {row["status"]: row["n"] for row in rows}
        total = sum(counts.values())
        parts = [f"مجموع الطلبة: {total}"]
        parts.extend(f"{status}: {counts.get(status, 0)}" for status in ATTENDANCE_STATUSES)
        parts.append(f"غير مسجل: {counts.get('غير مسجل', 0)}")
        self.summary.config(text="   |   ".join(parts))


class DocumentsTab:
    def __init__(self, app, parent):
        self.app, self.db = app, app.db
        self.frame = ttk.Frame(parent, padding=12)
        self.selected_id = None
        self.owner_type_values = ["المدرسة", "طالب", "مدرس"]
        self.category_values = list(DOCUMENT_CATEGORIES)
        self.owner_type_display_to_raw = {display_arabic_text(v): v for v in self.owner_type_values}
        self.category_display_to_raw = {display_arabic_text(v): v for v in self.category_values}
        self.owner_type = tk.StringVar(value=display_arabic_text("المدرسة"))
        self.owner_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.category_var = tk.StringVar(value=display_arabic_text("أخرى"))
        self.date_var = tk.StringVar(value=today_text())
        self.file_var = tk.StringVar()
        self.notes_var = tk.StringVar()
        self.owner_map = {}
        self._build()
        self.refresh_owners()
        self.refresh()

    def _build(self):
        form = ttk.LabelFrame(self.frame, text="🗂️ أرشفة وثائق المدرسة والطلبة والملاك", padding=10)
        form.pack(fill="x", pady=(0, 10))
        fields = [
            ("🏫 الجهة المالكة", ttk.Combobox(form, textvariable=self.owner_type, values=[display_arabic_text(v) for v in self.owner_type_values], state="readonly", width=24)),
            ("👤 اسم الطالب / المدرس", ttk.Combobox(form, textvariable=self.owner_var, state="readonly", width=42)),
            ("📄 عنوان الوثيقة *", ttk.Entry(form, textvariable=self.title_var, width=36)),
            ("🗂️ نوع الوثيقة", ttk.Combobox(form, textvariable=self.category_var, values=[display_arabic_text(v) for v in self.category_values], state="readonly", width=24)),
            ("📅 تاريخ الوثيقة", ttk.Entry(form, textvariable=self.date_var, width=18)),
            ("📎 مسار الملف", ttk.Entry(form, textvariable=self.file_var, width=56)),
            ("📝 ملاحظات", ttk.Entry(form, textvariable=self.notes_var, width=56)),
        ]
        for idx, (label, widget) in enumerate(fields):
            r, c = divmod(idx, 2)
            form.columnconfigure(c * 2 + 1, weight=1)
            ttk.Label(form, text=label).grid(row=r, column=c * 2, sticky="e", padx=6, pady=4)
            widget.grid(row=r, column=c * 2 + 1, sticky="ew", padx=6, pady=4)
        self.owner_combo = fields[1][1]
        self.owner_type.trace_add("write", lambda *_: self.refresh_owners())
        self.file_widget = fields[5][1]
        ttk.Button(form, text="اختيار ملف…", command=self.browse_file).grid(row=2, column=4, padx=4, pady=4, sticky="ew")
        ttk.Button(form, text="🖼️ إضافة صورة", command=self.browse_image).grid(row=2, column=5, padx=4, pady=4, sticky="ew")
        form.columnconfigure(5, weight=0)
        buttons = ttk.Frame(form)
        buttons.grid(row=4, column=0, columnspan=6, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="حفظ الوثيقة", command=self.save, style="Primary.TButton").pack(side="right", padx=3)
        ttk.Button(buttons, text="مسح الحقول", command=self.clear).pack(side="right", padx=3)
        ttk.Button(buttons, text="فتح الملف المحدد", command=self.open_selected).pack(side="left", padx=3)
        ttk.Button(buttons, text="معاينة صورة", command=self.preview_image).pack(side="left", padx=3)
        ttk.Button(buttons, text="🖨️ طباعة الأرشيف", command=self.print_archive).pack(side="left", padx=3)
        ttk.Button(buttons, text="حذف الوثيقة", command=self.delete_selected).pack(side="left", padx=3)
        ttk.Button(buttons, text="تصدير CSV", command=self.export_csv).pack(side="left", padx=3)

        archive_tools = ttk.Frame(self.frame)
        archive_tools.pack(fill="x", pady=(0, 6))
        ttk.Label(archive_tools, text="🔎 البحث في الأرشيف:").pack(side="right", padx=(5, 3))
        self.search_var = tk.StringVar()
        archive_search = ttk.Entry(archive_tools, textvariable=self.search_var, width=34)
        archive_search.pack(side="right", padx=4)
        archive_search.bind("<KeyRelease>", lambda _event: self.refresh())
        ttk.Button(archive_tools, text="مسح البحث", command=lambda: self.search_var.set("") or self.refresh()).pack(side="right", padx=3)
        ttk.Button(archive_tools, text="تحديث الأرشيف", command=self.refresh).pack(side="right", padx=3)

        grid = ttk.Frame(self.frame)
        grid.pack(fill="both", expand=True)
        cols = ("title", "owner_type", "owner_name", "category", "document_date", "file_name", "notes")
        self.tree = ttk.Treeview(grid, columns=cols, show="headings")
        labels = {"title": "عنوان الوثيقة", "owner_type": "الجهة", "owner_name": "الاسم", "category": "النوع", "document_date": "التاريخ", "file_name": "الملف", "notes": "ملاحظات"}
        widths = {"title": 180, "owner_type": 90, "owner_name": 190, "category": 150, "document_date": 110, "file_name": 220, "notes": 220}
        for col in cols:
            self.tree.heading(col, text=labels[col])
            self.tree.column(col, width=widths[col], anchor="center")
        y = ttk.Scrollbar(grid, orient="vertical", command=self.tree.yview)
        x = ttk.Scrollbar(grid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        grid.rowconfigure(0, weight=1)
        grid.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.select_row)
        self.tree.bind("<MouseWheel>", lambda event: self.tree.yview_scroll(-1 * int(event.delta / 120), "units"))
        self.empty_hint = ttk.Label(
            grid,
            text="الأرشيف فارغ حالياً. اختر ملفاً، واكتب عنوان الوثيقة، ثم اضغط حفظ الوثيقة.",
            style="EmptyState.TLabel", justify="center", anchor="center"
        )
        self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

    def refresh_owners(self):
        t = self.owner_type_display_to_raw.get(self.owner_type.get(), self.owner_type.get())
        self.owner_map = {}
        if t == "طالب":
            rows = self.db.query("SELECT id,student_no,full_name FROM students ORDER BY full_name")
            self.owner_map = {display_arabic_text(f"{r['full_name']} — {r['student_no']}"): r["id"] for r in rows}
        elif t == "مدرس":
            rows = self.db.query("SELECT id,employee_no,full_name FROM teachers ORDER BY full_name")
            self.owner_map = {display_arabic_text(f"{r['full_name']} — {r['employee_no']}"): r["id"] for r in rows}
        self.owner_combo = getattr(self, "owner_combo", None)
        if self.owner_combo:
            self.owner_combo["values"] = list(self.owner_map.keys())
        if t == "المدرسة":
            self.owner_var.set(display_arabic_text("المدرسة"))
        elif self.owner_var.get() not in self.owner_map:
            self.owner_var.set(next(iter(self.owner_map), ""))

    def browse_file(self):
        path = filedialog.askopenfilename(parent=self.frame, title="اختيار ملف الوثيقة", filetypes=[("كل الملفات", "*.*"), ("PDF", "*.pdf"), ("صور", "*.png *.jpg *.jpeg"), ("Word", "*.doc *.docx")])
        if path:
            self.file_var.set(path)

    def browse_image(self):
        path = filedialog.askopenfilename(parent=self.frame, title="إضافة صورة إلى الأرشيف",
            filetypes=[("صور", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"), ("كل الملفات", "*.*")])
        if not path:
            return
        self.file_var.set(path)
        if not self.title_var.get().strip():
            self.title_var.set(Path(path).stem)
        image_category = "صورة مدرسية / أرشيفية"
        self.category_var.set(display_arabic_text(image_category))

    def preview_image(self):
        raw = self.file_var.get().strip()
        path = Path(raw) if raw else None
        if path and not path.is_absolute():
            path = resolve_document_path(raw)
        if not path or not path.exists() or not path.is_file():
            sel = self.tree.selection()
            if sel:
                row = self.db.one("SELECT file_path FROM documents WHERE id=?", (safe_int(sel[0]),))
                path = resolve_document_path(row["file_path"]) if row and row["file_path"] else None
        if not path or not path.exists():
            messagebox.showinfo("معاينة الصورة", "اختر صورة من زر «إضافة صورة» أو حدد وثيقة تحتوي على صورة.", parent=self.frame)
            return
        if not PIL_AVAILABLE:
            messagebox.showwarning("مكتبة الصور غير متوفرة", "معاينة الصور تحتاج مكتبة Pillow. يمكنك استخدام «فتح الملف المحدد» لعرض الصورة في برنامج Windows.", parent=self.frame)
            return
        try:
            with Image.open(path) as source:
                img = source.convert("RGB")
                img.thumbnail((1000, 700))
                rendered = ImageTk.PhotoImage(img.copy())
            window = tk.Toplevel(self.frame)
            window.title(f"معاينة صورة — {path.name}")
            window.geometry("1050x790")
            window.configure(bg="#eaf0f5")
            ttk.Label(window, text=path.name, style="PageTitle.TLabel", anchor="center").pack(fill="x", padx=10, pady=8)
            panel = ttk.Frame(window, padding=8)
            panel.pack(fill="both", expand=True)
            label = ttk.Label(panel, image=rendered, anchor="center")
            label.image = rendered
            label.pack(fill="both", expand=True)
            ttk.Button(window, text="إغلاق المعاينة", command=window.destroy).pack(pady=8)
        except Exception as exc:
            messagebox.showerror("تعذّرت معاينة الصورة", f"لم يمكن فتح الصورة. يمكنك فتح الملف مباشرةً.\n{exc}", parent=self.frame)

    def print_archive(self):
        rows = self.db.query("""SELECT d.title,
            CASE d.owner_type WHEN 'school' THEN 'المدرسة' WHEN 'student' THEN 'طالب' WHEN 'teacher' THEN 'مدرس' ELSE d.owner_type END owner_type,
            CASE d.owner_type WHEN 'school' THEN COALESCE((SELECT name FROM school_info WHERE id=1),'المدرسة')
              WHEN 'student' THEN COALESCE((SELECT full_name FROM students WHERE id=d.owner_id),'طالب محذوف')
              WHEN 'teacher' THEN COALESCE((SELECT full_name FROM teachers WHERE id=d.owner_id),'مدرس محذوف') ELSE '' END owner_name,
            d.category,d.document_date,COALESCE(d.notes,'') notes FROM documents d ORDER BY d.id DESC""")
        data = [[r["title"], r["owner_type"], r["owner_name"], r["category"], r["document_date"], r["notes"]] for r in rows]
        open_print_preview("أرشيف وثائق المدرسة", ["العنوان", "الجهة", "الاسم", "التصنيف", "التاريخ", "ملاحظات"], data,
            self.app.school_settings().get("name", "مدرستي"), self.app.font_family, parent=self.frame)

    def _owner_db(self):
        t = self.owner_type_display_to_raw.get(self.owner_type.get(), self.owner_type.get())
        if t == "المدرسة":
            return "school", None
        if t == "طالب":
            return "student", self.owner_map.get(self.owner_var.get())
        return "teacher", self.owner_map.get(self.owner_var.get())

    def _stored_path(self):
        raw = self.file_var.get().strip()
        if not raw:
            return ""
        src = Path(raw)
        if not src.is_absolute():
            relative_candidate = DATA_DIR / src
            if relative_candidate.exists():
                src = relative_candidate
        elif not src.exists():
            recovered = resolve_document_path(raw)
            if recovered is not None and recovered.exists():
                src = recovered
        if not src.exists() or not src.is_file():
            raise FileNotFoundError("ملف الوثيقة غير موجود. اختر الملف من جديد.")
        try:
            resolved = src.resolve()
            attach_root = ATTACHMENTS_DIR.resolve()
            # Keep app-managed files as relative paths so a full backup can move between PCs.
            if resolved == attach_root or attach_root in resolved.parents:
                return resolved.relative_to(DATA_DIR.resolve()).as_posix()
        except (OSError, ValueError):
            pass
        dest = ATTACHMENTS_DIR / f"{uuid.uuid4().hex[:10]}_{src.name}"
        shutil.copy2(src, dest)
        return dest.relative_to(DATA_DIR).as_posix()

    def save(self):
        title = self.title_var.get().strip()
        if not title:
            messagebox.showwarning("بيانات ناقصة", "أدخل عنوان الوثيقة.", parent=self.frame)
            return
        owner_type, owner_id = self._owner_db()
        if owner_type != "school" and owner_id is None:
            messagebox.showwarning("اختيار مطلوب", "اختر الطالب أو المدرس صاحب الوثيقة.", parent=self.frame)
            return
        try:
            stored = self._stored_path()
            category = self.category_display_to_raw.get(self.category_var.get(), self.category_var.get())
            vals = (owner_type, owner_id, title, category, self.date_var.get().strip(), stored, self.notes_var.get().strip())
            if self.date_var.get().strip():
                datetime.strptime(self.date_var.get().strip(), "%Y-%m-%d")
            if self.selected_id:
                self.db.execute("UPDATE documents SET owner_type=?,owner_id=?,title=?,category=?,document_date=?,file_path=?,notes=? WHERE id=?", vals + (self.selected_id,))
            else:
                self.db.execute("INSERT INTO documents(owner_type,owner_id,title,category,document_date,file_path,notes) VALUES(?,?,?,?,?,?,?)", vals)
            self.refresh()
            self.clear()
            messagebox.showinfo("تم الحفظ", "تم حفظ الوثيقة في الأرشيف.", parent=self.frame)
        except ValueError:
            messagebox.showwarning("تاريخ غير صحيح", "اكتب التاريخ بصيغة YYYY-MM-DD.", parent=self.frame)
        except Exception as exc:
            messagebox.showerror("تعذّر الحفظ", str(exc), parent=self.frame)

    def refresh(self):
        if not hasattr(self, "tree"):
            return
        for item in self.tree.get_children():
            self.tree.delete(item)
        sql = """SELECT d.id,d.title,d.owner_type,d.owner_id,d.category,d.document_date,d.file_path,d.notes,
            CASE d.owner_type WHEN 'school' THEN COALESCE((SELECT name FROM school_info WHERE id=1),'المدرسة')
              WHEN 'student' THEN COALESCE((SELECT full_name FROM students WHERE id=d.owner_id),'طالب محذوف')
              WHEN 'teacher' THEN COALESCE((SELECT full_name FROM teachers WHERE id=d.owner_id),'مدرس محذوف') ELSE '' END AS owner_name
            FROM documents d"""
        term = self.search_var.get().strip() if hasattr(self, "search_var") else ""
        params = []
        if term:
            sql += " WHERE d.title LIKE ? OR d.category LIKE ? OR COALESCE(d.notes,'') LIKE ? OR COALESCE(d.file_path,'') LIKE ?"
            params = [f"%{term}%"] * 4
        sql += " ORDER BY d.id DESC"
        rows = self.db.query(sql, params)
        for r in rows:
            owner_label = {"school": "المدرسة", "student": "طالب", "teacher": "مدرس"}.get(r["owner_type"], r["owner_type"])
            fname = Path(r["file_path"]).name if r["file_path"] else ""
            self.tree.insert("", "end", iid=str(r["id"]), values=(r["title"], owner_label, r["owner_name"], r["category"], r["document_date"], fname, r["notes"] or ""))
        if rows:
            self.empty_hint.place_forget()
        else:
            text = "لا توجد نتائج مطابقة للبحث. جرّب كلمة أخرى." if term else "الأرشيف فارغ حالياً. أضف وثيقة أو صورة ثم احفظها لتظهر هنا."
            self.empty_hint.configure(text=text)
            self.empty_hint.place(relx=0.5, rely=0.5, anchor="center")

    def select_row(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.selected_id = safe_int(sel[0])
        r = self.db.one("SELECT * FROM documents WHERE id=?", (self.selected_id,))
        if not r:
            return
        self.owner_type.set(display_arabic_text({"school": "المدرسة", "student": "طالب", "teacher": "مدرس"}.get(r["owner_type"], "المدرسة")))
        self.refresh_owners()
        if r["owner_type"] == "student":
            owner = self.db.one("SELECT id,student_no,full_name FROM students WHERE id=?", (r["owner_id"],))
            if owner:
                self.owner_var.set(display_arabic_text(f"{owner['full_name']} — {owner['student_no']}"))
        elif r["owner_type"] == "teacher":
            owner = self.db.one("SELECT id,employee_no,full_name FROM teachers WHERE id=?", (r["owner_id"],))
            if owner:
                self.owner_var.set(display_arabic_text(f"{owner['full_name']} — {owner['employee_no']}"))
        self.title_var.set(r["title"] or "")
        self.category_var.set(display_arabic_text(r["category"] or "أخرى"))
        self.date_var.set(r["document_date"] or "")
        self.file_var.set(r["file_path"] or "")
        self.notes_var.set(r["notes"] or "")

    def open_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("اختيار مطلوب", "حدد وثيقة من القائمة.", parent=self.frame)
            return
        r = self.db.one("SELECT file_path FROM documents WHERE id=?", (safe_int(sel[0]),))
        if not r or not r["file_path"]:
            messagebox.showinfo("لا يوجد ملف", "هذه الوثيقة مسجلة من دون ملف مرفق.", parent=self.frame)
            return
        path = resolve_document_path(r["file_path"])
        if path is None or not path.exists():
            messagebox.showerror("الملف غير موجود", "تعذّر العثور على الملف المرفق في المسار المسجل. إذا نُقلت قاعدة البيانات يدوياً، انقل مجلد attachments معها أو استخدم النسخ الاحتياطي الكامل.", parent=self.frame)
            return
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(path))  # type: ignore[attr-defined]
            else:
                webbrowser.open(path.resolve().as_uri())
        except Exception as exc:
            messagebox.showerror("تعذّر فتح الملف", str(exc), parent=self.frame)

    def delete_selected(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showwarning("اختيار مطلوب", "حدد الوثيقة التي تريد حذفها.", parent=self.frame)
            return
        if messagebox.askyesno("تأكيد الحذف", "سيُحذف سجل الوثيقة من الأرشيف ولن يُحذف الملف الأصلي المرفق. متابعة؟", parent=self.frame):
            self.db.execute("DELETE FROM documents WHERE id=?", (safe_int(sel[0]),))
            self.clear()
            self.refresh()

    def clear(self):
        self.selected_id = None
        self.title_var.set("")
        self.category_var.set(display_arabic_text("أخرى"))
        self.date_var.set(today_text())
        self.file_var.set("")
        self.notes_var.set("")
        self.tree.selection_remove(self.tree.selection())

    def export_csv(self):
        path = filedialog.asksaveasfilename(parent=self.frame, title="تصدير الأرشيف", defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile=f"documents_{today_text()}.csv")
        if not path:
            return
        rows = self.db.query("""SELECT d.title,d.owner_type,d.category,d.document_date,d.file_path,d.notes,
            CASE d.owner_type WHEN 'school' THEN (SELECT name FROM school_info WHERE id=1)
            WHEN 'student' THEN (SELECT full_name FROM students WHERE id=d.owner_id)
            WHEN 'teacher' THEN (SELECT full_name FROM teachers WHERE id=d.owner_id) ELSE '' END owner_name FROM documents d ORDER BY d.id DESC""")
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.writer(f)
            w.writerow(["عنوان الوثيقة", "نوع الجهة", "الاسم", "التصنيف", "التاريخ", "مسار الملف", "ملاحظات"])
            for r in rows:
                w.writerow([r["title"], r["owner_type"], r["owner_name"], r["category"], r["document_date"], r["file_path"], r["notes"]])
        messagebox.showinfo("اكتمل التصدير", "تم تصدير أرشيف الوثائق.", parent=self.frame)


class AppearanceSettingsDialog:
    """User preferences for readable Arabic text, palette, and field appearance."""
    COLOR_KEYS = [
        ("text_color", "لون النص"),
        ("accent_color", "اللون الأساسي للأزرار والعناصر"),
        ("field_color", "خلفية خانات الإدخال"),
        ("panel_color", "خلفية البطاقات والجداول"),
    ]

    def __init__(self, app):
        self.app = app
        self.win = tk.Toplevel(app.root)
        self.win.title("إعدادات مظهر مدرستي Pro")
        self.win.transient(app.root)
        self.win.grab_set()
        self.win.geometry("700x610")
        self.win.minsize(650, 560)
        self.vars = {}
        self.swatches = {}
        prefs = app.appearance_preferences
        families = self._font_families()
        current_family = prefs.get("font_family", "Tahoma")
        if current_family not in families:
            families.insert(0, current_family)
        body = ttk.Frame(self.win, padding=18)
        body.pack(fill="both", expand=True)
        ttk.Label(body, text="⚙️ إعدادات الواجهة والخط والألوان", style="PageTitle.TLabel", anchor="e").pack(fill="x", pady=(0, 6))
        ttk.Label(body, text="اختر خطاً واضحاً للعربية وحجم النص وألوان الواجهة. تحفظ الاختيارات على هذا الجهاز.", style="Hint.TLabel", anchor="e", wraplength=620).pack(fill="x", pady=(0, 12))
        font_box = ttk.LabelFrame(body, text="🅰️ الخط وحجم الكتابة", padding=12)
        font_box.pack(fill="x", pady=(0, 10))
        ttk.Label(font_box, text="نوع الخط:").grid(row=0, column=3, sticky="e", padx=6, pady=7)
        self.vars["font_family"] = tk.StringVar(value=current_family)
        ttk.Combobox(font_box, textvariable=self.vars["font_family"], values=families, state="readonly", width=34).grid(row=0, column=2, columnspan=1, sticky="ew", padx=6, pady=7)
        ttk.Label(font_box, text="حجم الخط:").grid(row=0, column=1, sticky="e", padx=6, pady=7)
        self.vars["font_size"] = tk.StringVar(value=str(prefs.get("font_size", 11)))
        ttk.Combobox(font_box, textvariable=self.vars["font_size"], values=[str(x) for x in range(9, 19)], state="readonly", width=8).grid(row=0, column=0, sticky="w", padx=6, pady=7)
        font_box.columnconfigure(2, weight=1)
        colors_box = ttk.LabelFrame(body, text="🎨 ألوان البرنامج", padding=12)
        colors_box.pack(fill="both", expand=True, pady=(0, 12))
        for row, (key, label) in enumerate(self.COLOR_KEYS):
            ttk.Label(colors_box, text=label, anchor="e").grid(row=row, column=2, sticky="ew", padx=8, pady=8)
            default_color = app.colors.get({"text_color":"text", "accent_color":"accent", "field_color":"field", "panel_color":"panel"}[key], "#ffffff")
            stored_color = prefs.get(key, "") or ""
            color = stored_color or default_color
            self.vars[key] = tk.StringVar(value=stored_color)
            swatch = tk.Label(colors_box, text=color, bg=color, fg=self._contrast(color), relief="solid", bd=1, padx=9, pady=5, width=12)
            swatch.grid(row=row, column=1, sticky="ew", padx=6, pady=6)
            self.swatches[key] = swatch
            ttk.Button(colors_box, text="اختيار اللون…", command=lambda k=key: self.choose_color(k)).grid(row=row, column=0, padx=6, pady=6)
        colors_box.columnconfigure(2, weight=1)
        footer = ttk.Frame(body)
        footer.pack(fill="x")
        ttk.Button(footer, text="إعادة الافتراضي", command=self.reset_defaults).pack(side="left", padx=3)
        ttk.Button(footer, text="إلغاء", command=self.win.destroy).pack(side="left", padx=3)
        ttk.Button(footer, text="حفظ وتطبيق الإعدادات", command=self.save, style="Primary.TButton").pack(side="right", padx=3)

    @staticmethod
    def _contrast(hex_color):
        try:
            value = hex_color.lstrip("#")
            r, g, b = [int(value[i:i+2], 16) for i in (0, 2, 4)]
            return "#111827" if (0.299*r + 0.587*g + 0.114*b) > 150 else "#FFFFFF"
        except Exception:
            return "#111827"

    def _font_families(self):
        try:
            found = set(tkfont.families(self.app.root))
        except Exception:
            found = set()
        preferred = ["Tahoma", "Segoe UI", "Arial", "Calibri", "Noto Naskh Arabic UI", "Traditional Arabic"]
        result = [f for f in preferred if f in found]
        result.extend(sorted(f for f in found if f not in result and ("Arabic" in f or f in ("Verdana", "Microsoft Sans Serif")))[:20])
        return result or preferred[:4]

    def choose_color(self, key):
        palette_key = {"text_color":"text", "accent_color":"accent", "field_color":"field", "panel_color":"panel"}[key]
        current = self.vars[key].get() or self.app.colors.get(palette_key, "#ffffff")
        picked = colorchooser.askcolor(color=current, title="اختيار اللون", parent=self.win)[1]
        if picked:
            self.vars[key].set(picked.upper())
            self.swatches[key].configure(text=picked.upper(), bg=picked.upper(), fg=self._contrast(picked))

    def reset_defaults(self):
        self.vars["font_family"].set("Tahoma")
        self.vars["font_size"].set("11")
        # Empty color values mean use the coordinated defaults for the current theme.
        dark = self.app.theme_mode == "dark"
        defaults = ({"text":"#EFF5FB", "accent":"#24B6A6", "field":"#0F1D30", "panel":"#14253A"}
                    if dark else {"text":"#19324B", "accent":"#159E98", "field":"#FBFDFF", "panel":"#FFFFFF"})
        palette_map = {"text_color":"text", "accent_color":"accent", "field_color":"field", "panel_color":"panel"}
        for key, _label in self.COLOR_KEYS:
            self.vars[key].set("")
            color = defaults[palette_map[key]]
            self.swatches[key].configure(text=color, bg=color, fg=self._contrast(color))

    def save(self):
        family = self.vars["font_family"].get().strip() or "Tahoma"
        try:
            size = int(self.vars["font_size"].get())
        except (TypeError, ValueError):
            messagebox.showwarning("حجم الخط", "اختر حجم خط من القائمة.", parent=self.win)
            return
        if not 9 <= size <= 18:
            messagebox.showwarning("حجم الخط", "حجم الخط يجب أن يكون بين 9 و18.", parent=self.win)
            return
        prefs = dict(self.app.appearance_preferences)
        prefs["font_family"], prefs["font_size"] = family, size
        for key, _label in self.COLOR_KEYS:
            value = self.vars[key].get().strip()
            if value and not re.fullmatch(r"#[0-9A-Fa-f]{6}", value):
                messagebox.showwarning("لون غير صحيح", "اختر اللون من أداة اختيار الألوان.", parent=self.win)
                return
            # Store empty values for the theme defaults, otherwise a valid hex color.
            prefs[key] = value.upper() if value else ""
        self.app.appearance_preferences = prefs
        self.app.font_family = family
        self.app.font_size = size
        self.app._save_ui_preferences()
        self.app._style()
        self.app._update_tree_row_colors()
        self.app.status_var.set("تم حفظ إعدادات الخط والألوان")
        self.win.destroy()
        messagebox.showinfo("تم تطبيق الإعدادات", "تم تطبيق الخط والألوان وحفظها. ستبقى محفوظة عند فتح البرنامج مرة أخرى.", parent=self.app.root)


class SettingsTab:
    def __init__(self, app, parent):
        self.app, self.db = app, app.db
        self.frame = ttk.Frame(parent, padding=16)
        self.vars = {}
        self._build()
        self.load()

    def _build(self):
        details = ttk.LabelFrame(self.frame, text="🏫 معلومات المدرسة", padding=12)
        details.pack(fill="x", pady=(0, 12))
        settings_fields = [
            ("name", "🏫 اسم المدرسة", "entry"), ("director", "👤 اسم المدير/ة", "entry"),
            ("phone", "📞 رقم الهاتف", "entry"), ("email", "✉️ البريد الإلكتروني", "entry"),
            ("address", "📍 العنوان", "entry"), ("academic_year", "📅 السنة الدراسية", "entry"),
        ]
        for idx, (key, label, kind) in enumerate(settings_fields):
            r, c = divmod(idx, 2)
            var = tk.StringVar()
            self.vars[key] = var
            ttk.Label(details, text=label).grid(row=r, column=c * 2, sticky="e", padx=6, pady=6)
            ttk.Entry(details, textvariable=var, width=38).grid(row=r, column=c * 2 + 1, sticky="ew", padx=6, pady=6)
        for c in (1, 3):
            details.columnconfigure(c, weight=1)

        hours = ttk.LabelFrame(self.frame, text="🕘 ساعات الدوام وجدول الحصص", padding=12)
        hours.pack(fill="x", pady=(0, 12))
        hour_fields = [
            ("morning_start", "🕘 بداية الدوام (HH:MM)"), ("periods_count", "📚 عدد الحصص"),
            ("period_minutes", "⏱️ مدة الحصة بالدقائق"), ("break_minutes", "☕ الاستراحة بين الحصص بالدقائق"),
        ]
        for idx, (key, label) in enumerate(hour_fields):
            r, c = divmod(idx, 2)
            var = tk.StringVar()
            self.vars[key] = var
            ttk.Label(hours, text=label).grid(row=r, column=c * 2, sticky="e", padx=6, pady=6)
            ttk.Entry(hours, textvariable=var, width=20).grid(row=r, column=c * 2 + 1, sticky="w", padx=6, pady=6)
        self.vars["notes"] = tk.StringVar()
        ttk.Label(self.frame, text="ملاحظات عامة").pack(anchor="e")
        ttk.Entry(self.frame, textvariable=self.vars["notes"], width=110).pack(fill="x", pady=(4, 10))
        buttons = ttk.Frame(self.frame)
        buttons.pack(fill="x")
        ttk.Button(buttons, text="حفظ بيانات المدرسة والدوام", command=self.save).pack(side="right", padx=4)
        ttk.Button(buttons, text="معاينة أوقات الحصص", command=self.preview_periods).pack(side="right", padx=4)
        ttk.Button(buttons, text="نسخ احتياطي كامل ZIP", command=self.backup).pack(side="left", padx=4)
        ttk.Button(buttons, text="استعادة نسخة ZIP", command=self.restore_backup).pack(side="left", padx=4)
        ttk.Label(self.frame, text="تُحفظ البيانات محلياً على هذا الجهاز. احتفظ بنسخة احتياطية دورية، ولا تعتمد على هذا البرنامج وحده لحفظ النسخ الأصلية من الوثائق.", style="Hint.TLabel", wraplength=1050).pack(anchor="e", pady=12)

    def load(self):
        r = self.db.one("SELECT * FROM school_info WHERE id=1")
        if not r:
            return
        for key, var in self.vars.items():
            if key in r.keys():
                var.set("" if r[key] is None else str(r[key]))

    def save(self):
        try:
            datetime.strptime(self.vars["morning_start"].get().strip(), "%H:%M")
        except ValueError:
            messagebox.showwarning("وقت غير صحيح", "اكتب بداية الدوام بصيغة 08:00.", parent=self.frame)
            return
        count = safe_int(self.vars["periods_count"].get(), 0)
        duration = safe_int(self.vars["period_minutes"].get(), 0)
        pause = safe_int(self.vars["break_minutes"].get(), -1)
        if not (1 <= count <= 15 and 1 <= duration <= 180 and 0 <= pause <= 120):
            messagebox.showwarning("قيم غير صحيحة", "عدد الحصص من 1 إلى 15، مدة الحصة من 1 إلى 180 دقيقة، والاستراحة من 0 إلى 120 دقيقة.", parent=self.frame)
            return
        keys = ["name", "director", "phone", "email", "address", "academic_year", "morning_start", "periods_count", "period_minutes", "break_minutes", "notes"]
        vals = [self.vars[k].get().strip() for k in keys]
        vals[7], vals[8], vals[9] = count, duration, pause
        try:
            self.db.execute("UPDATE school_info SET " + ",".join(k + "=?" for k in keys) + " WHERE id=1", vals)
            self.app.update_window_title()
            self.app.refresh_everything()
            messagebox.showinfo("تم الحفظ", "تم حفظ معلومات المدرسة وساعات الدوام.", parent=self.frame)
        except sqlite3.Error as exc:
            messagebox.showerror("خطأ", str(exc), parent=self.frame)

    def preview_periods(self):
        try:
            start = datetime.strptime(self.vars["morning_start"].get().strip(), "%H:%M")
            count = max(1, min(15, safe_int(self.vars["periods_count"].get(), 7)))
            duration = max(1, safe_int(self.vars["period_minutes"].get(), 40))
            pause = max(0, safe_int(self.vars["break_minutes"].get(), 5))
        except ValueError:
            messagebox.showwarning("إعدادات غير صحيحة", "راجع الوقت وعدد الحصص ومدة الحصة.", parent=self.frame)
            return
        lines = []
        for i in range(1, count + 1):
            st = start + timedelta(minutes=(i - 1) * (duration + pause))
            en = st + timedelta(minutes=duration)
            lines.append(f"الحصة {i}: {st:%H:%M} - {en:%H:%M}")
        messagebox.showinfo("أوقات الحصص المقترحة", "\n".join(lines) + f"\n\nنهاية آخر حصة: {en:%H:%M}", parent=self.frame)

    def backup(self):
        default = f"SchoolManagerPro_Backup_{datetime.now():%Y%m%d_%H%M%S}.zip"
        path = filedialog.asksaveasfilename(parent=self.frame, title="إنشاء نسخة احتياطية كاملة", initialdir=str(BACKUPS_DIR), initialfile=default, defaultextension=".zip", filetypes=[("نسخة احتياطية كاملة ZIP", "*.zip")])
        if not path:
            return
        temp_db = None
        try:
            temp_db = BACKUPS_DIR / f".backup_db_{uuid.uuid4().hex}.db"
            self.db.backup(temp_db)
            school = self.db.one("SELECT name,academic_year FROM school_info WHERE id=1")
            manifest = {
                "product": APP_NAME,
                "version": APP_VERSION,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "school_name": school["name"] if school else "",
                "academic_year": school["academic_year"] if school else "",
                "format": "schoolmanagerpro-full-backup-v1",
            }
            with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
                archive.write(temp_db, "school_manager.db")
                archive.writestr("backup_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
                if ATTACHMENTS_DIR.exists():
                    for file_path in ATTACHMENTS_DIR.rglob("*"):
                        if file_path.is_file():
                            archive.write(file_path, "attachments/" + file_path.relative_to(ATTACHMENTS_DIR).as_posix())
            messagebox.showinfo("اكتمل النسخ الاحتياطي", f"تم حفظ قاعدة البيانات وملفات الوثائق في نسخة واحدة:\n{path}\n\nاحتفظ بهذه النسخة في مكان آمن.", parent=self.frame)
        except Exception as exc:
            messagebox.showerror("فشل النسخ الاحتياطي", str(exc), parent=self.frame)
        finally:
            if temp_db and temp_db.exists():
                try:
                    temp_db.unlink()
                except OSError:
                    pass

    def restore_backup(self):
        path = filedialog.askopenfilename(parent=self.frame, title="اختيار نسخة احتياطية كاملة", filetypes=[("نسخة احتياطية ZIP", "*.zip")])
        if not path:
            return
        if not messagebox.askyesno("تأكيد الاستعادة", "ستُستبدل قاعدة البيانات الحالية بمحتويات النسخة. احتفظ بنسخة آمنة أولاً. هل تريد المتابعة؟", parent=self.frame):
            return
        temp_root = Path(tempfile.mkdtemp(prefix="schoolmanager_restore_"))
        try:
            with zipfile.ZipFile(path, "r") as archive:
                members = archive.namelist()
                if "school_manager.db" not in members:
                    raise ValueError("هذه ليست نسخة كاملة صالحة: ملف قاعدة البيانات غير موجود.")
                # Extract only known, safe entries; never trust arbitrary ZIP paths.
                for member in members:
                    normalized = Path(member.replace("\\", "/"))
                    if normalized.is_absolute() or ".." in normalized.parts:
                        raise ValueError("ملف النسخة يحتوي مساراً غير آمن.")
                    if member == "school_manager.db" or member == "backup_manifest.json" or member.startswith("attachments/"):
                        target = temp_root.joinpath(*normalized.parts)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with archive.open(member) as source, open(target, "wb") as dest:
                            shutil.copyfileobj(source, dest)
            candidate_db = temp_root / "school_manager.db"
            check = sqlite3.connect(candidate_db)
            try:
                integrity = check.execute("PRAGMA integrity_check").fetchone()[0]
                if integrity != "ok":
                    raise ValueError("قاعدة البيانات داخل النسخة غير سليمة.")
                tables = {r[0] for r in check.execute("SELECT name FROM sqlite_master WHERE type='table'")}
                if "students" not in tables or "school_info" not in tables:
                    raise ValueError("قاعدة البيانات ليست من نظام إدارة المدرسة.")
            finally:
                check.close()
            # Keep a quick recovery copy of the currently active database before replacing it.
            safeguard = BACKUPS_DIR / f"before_restore_{datetime.now():%Y%m%d_%H%M%S}.db"
            self.db.backup(safeguard)
            try:
                self.db.conn.execute("PRAGMA wal_checkpoint(FULL)")
            except sqlite3.Error:
                pass
            self.db.close()
            for sidecar in (Path(str(DB_PATH) + "-wal"), Path(str(DB_PATH) + "-shm")):
                try:
                    sidecar.unlink(missing_ok=True)
                except OSError:
                    pass
            shutil.copy2(candidate_db, DB_PATH)
            restored_attachments = temp_root / "attachments"
            if restored_attachments.exists():
                for item in restored_attachments.rglob("*"):
                    if item.is_file():
                        relative = item.relative_to(restored_attachments)
                        destination = ATTACHMENTS_DIR / relative
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(item, destination)
            self.app.db = SchoolDB(DB_PATH)
            self.db = self.app.db
            for mgr in self.app.managers.values():
                mgr.db = self.app.db
            self.app.attendance_tab.db = self.app.db
            self.app.documents_tab.db = self.app.db
            self.app.reports_tab.db = self.app.db
            self.db = self.app.db
            self.app.update_window_title()
            self.app.refresh_everything()
            self.load()
            messagebox.showinfo("اكتملت الاستعادة", f"تمت استعادة النسخة بنجاح. تم حفظ قاعدة البيانات السابقة في:\n{safeguard}", parent=self.frame)
        except Exception as exc:
            # If the database connection was closed during a failed restore, reconnect every view.
            try:
                self.app.db.query("SELECT 1")
            except Exception:
                try:
                    self.app.db = SchoolDB(DB_PATH)
                    self.db = self.app.db
                    for mgr in self.app.managers.values():
                        mgr.db = self.app.db
                    self.app.attendance_tab.db = self.app.db
                    self.app.documents_tab.db = self.app.db
                    self.app.reports_tab.db = self.app.db
                except Exception:
                    pass
            messagebox.showerror("فشلت الاستعادة", f"لم تكتمل عملية الاستعادة: {exc}\n\nاحتفظ بالنسخة الاحتياطية الأصلية ولا تحذفها.", parent=self.frame)
        finally:
            shutil.rmtree(temp_root, ignore_errors=True)


class ReportsTab:
    REPORTS = ["ملخص الحضور لتاريخ", "قائمة الغياب والتأخر", "قائمة الطلاب", "قائمة المدرسين", "جدول الحصص", "درجات الطلاب", "كشف الرسوم والأقساط"]
    def __init__(self, app, parent):
        self.app, self.db = app, app.db
        self.frame = ttk.Frame(parent, padding=12)
        self.report_display_to_raw = {display_arabic_text(v): v for v in self.REPORTS}
        self.kind_var = tk.StringVar(value=display_arabic_text(self.REPORTS[0]))
        self.date_var = tk.StringVar(value=today_text())
        self.class_var = tk.StringVar()
        self.class_map = {}
        self.rows_export = []
        self.headers_export = []
        self._build()
        self.refresh_classes()
        self.generate()

    def _build(self):
        bar = ttk.LabelFrame(self.frame, text="📊 إعداد التقرير", padding=10)
        bar.pack(fill="x", pady=(0, 8))
        ttk.Label(bar, text="نوع التقرير:").pack(side="right", padx=5)
        ttk.Combobox(bar, textvariable=self.kind_var, values=[display_arabic_text(v) for v in self.REPORTS], state="readonly", width=25).pack(side="right", padx=5)
        ttk.Label(bar, text="التاريخ:").pack(side="right", padx=5)
        ttk.Entry(bar, textvariable=self.date_var, width=14).pack(side="right", padx=5)
        ttk.Label(bar, text="الصف (لجدول الحصص):").pack(side="right", padx=5)
        self.class_combo = ttk.Combobox(bar, textvariable=self.class_var, state="readonly", width=26)
        self.class_combo.pack(side="right", padx=5)
        ttk.Button(bar, text="إنشاء التقرير", command=self.generate, style="Primary.TButton").pack(side="right", padx=5)
        ttk.Button(bar, text="🖨️ طباعة التقرير", command=self.print_report).pack(side="left", padx=5)
        ttk.Button(bar, text="تصدير التقرير CSV", command=self.export_csv).pack(side="left", padx=5)
        grid = ttk.Frame(self.frame)
        grid.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(grid, show="headings")
        y = ttk.Scrollbar(grid, orient="vertical", command=self.tree.yview)
        x = ttk.Scrollbar(grid, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        grid.rowconfigure(0, weight=1)
        grid.columnconfigure(0, weight=1)
        self.tree.bind("<MouseWheel>", lambda event: self.tree.yview_scroll(-1 * int(event.delta / 120), "units"))

    def refresh_classes(self):
        rows = self.db.query("SELECT id,grade,section,academic_year FROM classes ORDER BY grade,section")
        self.class_map = {display_arabic_text(f"{r['grade']} - {r['section']}" + (f" ({r['academic_year']})" if r['academic_year'] else "")): r["id"] for r in rows}
        self.class_combo["values"] = list(self.class_map)
        if self.class_var.get() not in self.class_map:
            self.class_var.set(next(iter(self.class_map), ""))

    def _show(self, headers, rows):
        self.headers_export = list(headers)
        self.rows_export = [list(r) for r in rows]
        self.tree.delete(*self.tree.get_children())
        self.tree["columns"] = [f"c{i}" for i in range(len(headers))]
        self.tree["show"] = "headings"
        for i, header in enumerate(headers):
            col = f"c{i}"
            self.tree.heading(col, text=header)
            self.tree.column(col, width=max(110, min(250, len(str(header)) * 12)), anchor="center")
        for idx, row in enumerate(self.rows_export):
            self.tree.insert("", "end", iid=str(idx), values=["" if x is None else x for x in row])

    def generate(self):
        kind = self.report_display_to_raw.get(self.kind_var.get(), self.kind_var.get())
        dt = self.date_var.get().strip()
        if kind in (self.REPORTS[0], self.REPORTS[1]):
            try:
                datetime.strptime(dt, "%Y-%m-%d")
            except ValueError:
                messagebox.showwarning("تاريخ غير صحيح", "اكتب التاريخ بصيغة YYYY-MM-DD.", parent=self.frame)
                return
        if kind == self.REPORTS[0]:
            rows = self.db.query("SELECT status,COUNT(*) n FROM attendance WHERE attendance_date=? GROUP BY status ORDER BY status", (dt,))
            counts = {r["status"]: r["n"] for r in rows}
            total = sum(counts.values())
            data = [(s, counts.get(s, 0), (counts.get(s, 0) / total * 100 if total else 0)) for s in ATTENDANCE_STATUSES]
            data.append(("إجمالي السجلات", total, 100 if total else 0))
            self._show(["الحالة", "العدد", "النسبة %"], [(a, b, f"{c:.1f}%") for a, b, c in data])
        elif kind == self.REPORTS[1]:
            rows = self.db.query("""SELECT s.student_no,s.full_name,COALESCE(c.grade,'') grade,COALESCE(c.section,'') section,a.status,a.note
                FROM attendance a JOIN students s ON s.id=a.student_id LEFT JOIN classes c ON c.id=s.class_id
                WHERE a.attendance_date=? AND a.status IN ('غائب','متأخر','إجازة') ORDER BY c.grade,c.section,s.full_name""", (dt,))
            self._show(["رقم الطالب", "اسم الطالب", "الصف", "الشعبة", "الحالة", "ملاحظة"], [(r["student_no"],r["full_name"],r["grade"],r["section"],r["status"],r["note"]) for r in rows])
        elif kind == self.REPORTS[2]:
            rows = self.db.query("""SELECT s.student_no,s.full_name,s.gender,s.birth_date,COALESCE(c.grade,'') grade,COALESCE(c.section,'') section,s.guardian_name,s.guardian_phone,s.status
                FROM students s LEFT JOIN classes c ON c.id=s.class_id ORDER BY c.grade,c.section,s.full_name""")
            self._show(["رقم الطالب", "اسم الطالب", "الجنس", "تاريخ الميلاد", "الصف", "الشعبة", "ولي الأمر", "هاتف ولي الأمر", "الحالة"], [(r[k] for k in ["student_no","full_name","gender","birth_date","grade","section","guardian_name","guardian_phone","status"]) for r in rows])
        elif kind == self.REPORTS[3]:
            rows = self.db.query("SELECT employee_no,full_name,specialization,phone,email,status FROM teachers ORDER BY full_name")
            self._show(["الرقم الوظيفي", "اسم المدرس", "الاختصاص", "الهاتف", "البريد الإلكتروني", "الحالة"], [(r[k] for k in ["employee_no","full_name","specialization","phone","email","status"]) for r in rows])
        elif kind == self.REPORTS[4]:
            class_id = self.class_map.get(self.class_var.get())
            if not class_id:
                messagebox.showinfo("اختر صفاً", "أضف صفاً أو شعبة أولاً.", parent=self.frame)
                return
            rows = self.db.query("""SELECT t.day,t.period_no,t.start_time,t.end_time,COALESCE(s.name,'') subject,COALESCE(te.full_name,'') teacher,t.room
                FROM timetable t LEFT JOIN subjects s ON s.id=t.subject_id LEFT JOIN teachers te ON te.id=t.teacher_id
                WHERE t.class_id=? ORDER BY CASE t.day WHEN 'الأحد' THEN 1 WHEN 'الاثنين' THEN 2 WHEN 'الثلاثاء' THEN 3 WHEN 'الأربعاء' THEN 4 WHEN 'الخميس' THEN 5 WHEN 'الجمعة' THEN 6 ELSE 7 END,t.period_no""", (class_id,))
            self._show(["اليوم", "الحصة", "من", "إلى", "المادة", "المدرس", "القاعة"], [(r[k] for k in ["day","period_no","start_time","end_time","subject","teacher","room"]) for r in rows])
        elif kind == self.REPORTS[5]:
            rows = self.db.query("""SELECT s.student_no,s.full_name,COALESCE(c.grade,'') grade,COALESCE(c.section,'') section,
                a.exam_name,COALESCE(sub.name,'') subject,a.exam_date,a.term,a.score,a.max_score,
                ROUND(CASE WHEN a.max_score>0 THEN a.score*100.0/a.max_score ELSE 0 END,1) percentage
                FROM assessments a JOIN students s ON s.id=a.student_id LEFT JOIN classes c ON c.id=s.class_id
                LEFT JOIN subjects sub ON sub.id=a.subject_id ORDER BY c.grade,c.section,s.full_name,a.exam_date DESC""")
            self._show(["رقم الطالب", "اسم الطالب", "الصف", "الشعبة", "الامتحان", "المادة", "التاريخ", "الفصل", "الدرجة", "من", "%"],
                [(r[k] for k in ["student_no","full_name","grade","section","exam_name","subject","exam_date","term","score","max_score","percentage"]) for r in rows])
        elif kind == self.REPORTS[6]:
            rows = self.db.query("""SELECT s.student_no,s.full_name,f.academic_year,f.fee_type,f.amount,f.paid_amount,
                ROUND(f.amount-f.paid_amount,2) remaining,f.due_date,f.payment_date,f.notes
                FROM fee_records f JOIN students s ON s.id=f.student_id ORDER BY s.full_name,f.due_date""")
            self._show(["رقم الطالب", "اسم الطالب", "السنة", "نوع الرسم", "المبلغ الكلي", "المدفوع", "المتبقي", "الاستحقاق", "تاريخ الدفع", "ملاحظات"],
                [(r[k] for k in ["student_no","full_name","academic_year","fee_type","amount","paid_amount","remaining","due_date","payment_date","notes"]) for r in rows])

    def print_report(self):
        if not self.headers_export:
            messagebox.showinfo("لا يوجد تقرير", "أنشئ التقرير أولاً ثم اطبع النتيجة.", parent=self.frame)
            return
        kind = self.report_display_to_raw.get(self.kind_var.get(), self.kind_var.get())
        open_print_preview(kind, self.headers_export, self.rows_export,
            self.app.school_settings().get("name", "مدرستي"), self.app.font_family, parent=self.frame)

    def export_csv(self):
        if not self.headers_export:
            messagebox.showinfo("لا يوجد تقرير", "أنشئ التقرير أولاً.", parent=self.frame)
            return
        path = filedialog.asksaveasfilename(parent=self.frame, title="حفظ التقرير", defaultextension=".csv", filetypes=[("CSV", "*.csv")], initialfile=f"school_report_{today_text()}.csv")
        if not path:
            return
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(self.headers_export)
            writer.writerows(self.rows_export)
        messagebox.showinfo("تم التصدير", f"تم حفظ التقرير في:\n{path}", parent=self.frame)


class AuthenticationDialog:
    """First-run admin setup and local account sign-in dialog."""
    def __init__(self, root, db: SchoolDB):
        self.root, self.db = root, db
        self.result = None
        count = self.db.one("SELECT COUNT(*) n FROM app_users")
        self.first_run = not count or int(count["n"]) == 0
        self.win = tk.Toplevel(root)
        self.win.title("إعداد حساب المدير — مدرستي Pro" if self.first_run else "تسجيل الدخول — مدرستي Pro")
        self.win.resizable(False, False)
        self.win.configure(bg="#eef3f8")
        self.win.protocol("WM_DELETE_WINDOW", self.cancel)
        box = ttk.Frame(self.win, padding=24)
        box.pack(fill="both", expand=True)
        ttk.Label(box, text="مدرستي Pro", font=("Tahoma", 21, "bold"), foreground="#12365a").pack(pady=(0, 4))
        headline = "إنشاء حساب المدير لأول تشغيل" if self.first_run else "سجّل الدخول إلى نظام المدرسة"
        ttk.Label(box, text=headline, font=("Tahoma", 11, "bold")).pack(pady=(0, 5))
        hint = "أنشئ حساب المدير وكلمة مرور لا تقل عن 10 أحرف." if self.first_run else "أدخل اسم المستخدم وكلمة المرور للمتابعة."
        ttk.Label(box, text=hint, style="Hint.TLabel", wraplength=360, justify="center").pack(pady=(0, 16))
        form = ttk.Frame(box)
        form.pack(fill="x")
        self.username = tk.StringVar(value="admin" if self.first_run else "")
        self.full_name = tk.StringVar(value="مدير النظام" if self.first_run else "")
        self.password = tk.StringVar()
        self.confirm = tk.StringVar()
        row = 0
        self._add_field(form, row, "اسم المستخدم", self.username, False); row += 1
        if self.first_run:
            self._add_field(form, row, "الاسم الكامل", self.full_name, False); row += 1
        self._add_field(form, row, "كلمة المرور", self.password, True); row += 1
        if self.first_run:
            self._add_field(form, row, "تأكيد كلمة المرور", self.confirm, True); row += 1
        btn_text = "إنشاء الحساب" if self.first_run else "تسجيل الدخول"
        ttk.Button(box, text=btn_text, command=self.submit).pack(fill="x", pady=(16, 6))
        ttk.Label(box, text="تُحفظ بيانات البرنامج محلياً على هذا الجهاز.", style="Hint.TLabel").pack()
        self.win.bind("<Return>", lambda _event: self.submit())
        self.win.bind("<Escape>", lambda _event: self.cancel())
        self.win.update_idletasks()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        x = max(0, (self.win.winfo_screenwidth() - w) // 2)
        y = max(0, (self.win.winfo_screenheight() - h) // 2)
        self.win.geometry(f"{w}x{h}+{x}+{y}")
        self.win.transient(root)
        self.win.grab_set()
        self.win.focus_force()

    @staticmethod
    def _add_field(parent, row, label, variable, secret):
        ttk.Label(parent, text=label).grid(row=row, column=1, sticky="e", padx=(8, 0), pady=5)
        entry = ttk.Entry(parent, textvariable=variable, width=30, show="*" if secret else "")
        entry.grid(row=row, column=0, sticky="ew", pady=5)
        parent.columnconfigure(0, weight=1)
        if row == 0:
            entry.focus_set()

    def submit(self):
        username = self.username.get().strip()
        password = self.password.get()
        if not username:
            messagebox.showwarning("بيانات ناقصة", "أدخل اسم المستخدم.", parent=self.win)
            return
        if self.first_run:
            full_name = self.full_name.get().strip()
            if len(username) < 3 or any(ch.isspace() for ch in username):
                messagebox.showwarning("اسم المستخدم غير صالح", "استخدم 3 أحرف على الأقل من دون مسافات.", parent=self.win)
                return
            if not full_name:
                messagebox.showwarning("بيانات ناقصة", "أدخل الاسم الكامل لمدير النظام.", parent=self.win)
                return
            if len(password) < 10:
                messagebox.showwarning("كلمة المرور قصيرة", "اختر كلمة مرور لا تقل عن 10 أحرف.", parent=self.win)
                return
            if password != self.confirm.get():
                messagebox.showwarning("عدم تطابق", "كلمتا المرور غير متطابقتين.", parent=self.win)
                return
            salt, digest = hash_password(password)
            try:
                self.db.execute("INSERT INTO app_users(username,full_name,password_salt,password_hash,is_admin,is_active) VALUES(?,?,?,?,1,1)", (username, full_name, salt, digest))
            except sqlite3.IntegrityError:
                messagebox.showerror("تعذّر إنشاء الحساب", "اسم المستخدم موجود مسبقاً. اختر اسماً آخر.", parent=self.win)
                return
            self.result = {"username": username, "full_name": full_name, "is_admin": True}
        else:
            row = self.db.one("SELECT username,full_name,password_salt,password_hash,is_admin,is_active FROM app_users WHERE username=? COLLATE NOCASE", (username,))
            if not row or not row["is_active"] or not verify_password(password, row["password_salt"], row["password_hash"]):
                messagebox.showerror("تعذّر تسجيل الدخول", "اسم المستخدم أو كلمة المرور غير صحيحة، أو أن الحساب غير فعّال.", parent=self.win)
                self.password.set("")
                return
            self.result = {"username": row["username"], "full_name": row["full_name"], "is_admin": bool(row["is_admin"])}
        self.win.destroy()

    def cancel(self):
        self.result = None
        self.win.destroy()


class UserManagementDialog:
    """Simple local account management, available to administrator accounts."""
    def __init__(self, app):
        self.app, self.db = app, app.db
        self.win = tk.Toplevel(app.root)
        self.win.title("إدارة حسابات المستخدمين — مدرستي Pro")
        self.win.geometry("820x590")
        self.win.minsize(720, 500)
        self.win.transient(app.root)
        self.win.configure(bg="#eef3f8")
        self.selected_id = None
        self._build()
        self.refresh()
        self.win.grab_set()

    def _build(self):
        form = ttk.LabelFrame(self.win, text="إضافة حساب مستخدم", padding=12)
        form.pack(fill="x", padx=12, pady=12)
        self.username = tk.StringVar()
        self.full_name = tk.StringVar()
        self.password = tk.StringVar()
        self.confirm = tk.StringVar()
        self.is_admin = tk.BooleanVar(value=False)
        fields = [("اسم المستخدم", self.username, False), ("الاسم الكامل", self.full_name, False),
                  ("كلمة المرور المؤقتة (10 أحرف فأكثر)", self.password, True), ("تأكيد كلمة المرور", self.confirm, True)]
        for idx, (label, var, secret) in enumerate(fields):
            r, c = divmod(idx, 2)
            ttk.Label(form, text=label).grid(row=r, column=c * 2 + 1, sticky="e", padx=5, pady=5)
            ttk.Entry(form, textvariable=var, width=30, show="*" if secret else "").grid(row=r, column=c * 2, sticky="ew", padx=5, pady=5)
            form.columnconfigure(c * 2, weight=1)
        ttk.Checkbutton(form, text="منح صلاحية إدارة الحسابات", variable=self.is_admin).grid(row=2, column=0, columnspan=2, sticky="w", padx=5, pady=5)
        ttk.Button(form, text="إضافة الحساب", command=self.add_user).grid(row=2, column=2, columnspan=2, sticky="e", padx=5, pady=5)
        list_frame = ttk.LabelFrame(self.win, text="الحسابات المسجلة", padding=10)
        list_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        columns = ("username", "full_name", "role", "status", "created")
        self.tree = ttk.Treeview(list_frame, columns=columns, show="headings", selectmode="browse")
        labels = {"username": "اسم المستخدم", "full_name": "الاسم الكامل", "role": "الصلاحية", "status": "الحالة", "created": "تاريخ الإنشاء"}
        widths = {"username": 135, "full_name": 215, "role": 120, "status": 100, "created": 170}
        for col in columns:
            self.tree.heading(col, text=labels[col])
            self.tree.column(col, width=widths[col], anchor="center")
        y = ttk.Scrollbar(list_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=y.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        y.grid(row=0, column=1, sticky="ns")
        list_frame.rowconfigure(0, weight=1)
        list_frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", lambda _event: self._select())
        buttons = ttk.Frame(list_frame)
        buttons.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Button(buttons, text="تفعيل / إيقاف الحساب", command=self.toggle_user).pack(side="right", padx=4)
        ttk.Button(buttons, text="إعادة تعيين كلمة المرور", command=self.reset_password).pack(side="right", padx=4)
        ttk.Button(buttons, text="تحديث القائمة", command=self.refresh).pack(side="left", padx=4)
        ttk.Label(list_frame, text="ملاحظة: جميع الحسابات النشطة تستطيع استخدام وظائف البرنامج؛ الصلاحية الإدارية لإدارة الحسابات فقط.", style="Hint.TLabel", wraplength=720).grid(row=2, column=0, columnspan=2, sticky="e", pady=(8, 0))

    def refresh(self):
        self.tree.delete(*self.tree.get_children())
        rows = self.db.query("SELECT id,username,full_name,is_admin,is_active,created_at FROM app_users ORDER BY is_admin DESC,username")
        for row in rows:
            self.tree.insert("", "end", iid=str(row["id"]), values=(row["username"], row["full_name"], "مدير" if row["is_admin"] else "مستخدم", "فعّال" if row["is_active"] else "متوقف", row["created_at"]))

    def _select(self):
        selected = self.tree.selection()
        self.selected_id = safe_int(selected[0]) if selected else None

    def add_user(self):
        username, full_name = self.username.get().strip(), self.full_name.get().strip()
        password = self.password.get()
        if len(username) < 3 or any(ch.isspace() for ch in username):
            messagebox.showwarning("اسم غير صالح", "اسم المستخدم يجب أن يتكون من 3 أحرف على الأقل ومن دون مسافات.", parent=self.win); return
        if not full_name:
            messagebox.showwarning("بيانات ناقصة", "أدخل الاسم الكامل.", parent=self.win); return
        if len(password) < 10 or password != self.confirm.get():
            messagebox.showwarning("كلمة المرور غير صالحة", "تأكد من التطابق واستخدم 10 أحرف على الأقل.", parent=self.win); return
        salt, digest = hash_password(password)
        try:
            self.db.execute("INSERT INTO app_users(username,full_name,password_salt,password_hash,is_admin,is_active) VALUES(?,?,?,?,?,1)", (username, full_name, salt, digest, int(self.is_admin.get())))
            self.username.set(""); self.full_name.set(""); self.password.set(""); self.confirm.set(""); self.is_admin.set(False)
            self.refresh()
            messagebox.showinfo("تم", "تم إنشاء الحساب بنجاح.", parent=self.win)
        except sqlite3.IntegrityError:
            messagebox.showerror("اسم مستخدم مكرر", "يوجد حساب بهذا الاسم. اختر اسماً مختلفاً.", parent=self.win)

    def toggle_user(self):
        if not self.selected_id:
            messagebox.showwarning("اختيار مطلوب", "حدد حساباً من القائمة أولاً.", parent=self.win); return
        row = self.db.one("SELECT * FROM app_users WHERE id=?", (self.selected_id,))
        if not row:
            self.refresh(); return
        if row["username"].casefold() == self.app.current_user["username"].casefold() and row["is_active"]:
            messagebox.showwarning("غير مسموح", "لا يمكنك إيقاف الحساب الذي تستخدمه حالياً.", parent=self.win); return
        new_state = 0 if row["is_active"] else 1
        if not new_state and row["is_admin"]:
            count = self.db.one("SELECT COUNT(*) n FROM app_users WHERE is_admin=1 AND is_active=1")["n"]
            if count <= 1:
                messagebox.showwarning("غير مسموح", "يجب الإبقاء على حساب مدير فعّال واحد على الأقل.", parent=self.win); return
        self.db.execute("UPDATE app_users SET is_active=? WHERE id=?", (new_state, self.selected_id))
        self.refresh()

    def reset_password(self):
        if not self.selected_id:
            messagebox.showwarning("اختيار مطلوب", "حدد حساباً من القائمة أولاً.", parent=self.win); return
        row = self.db.one("SELECT username FROM app_users WHERE id=?", (self.selected_id,))
        if not row: return
        new_password = simpledialog.askstring("إعادة تعيين كلمة المرور", f"أدخل كلمة مرور جديدة للحساب {row['username']} (10 أحرف فأكثر):", show="*", parent=self.win)
        if new_password is None: return
        if len(new_password) < 10:
            messagebox.showwarning("كلمة المرور قصيرة", "استخدم 10 أحرف على الأقل.", parent=self.win); return
        confirm = simpledialog.askstring("تأكيد كلمة المرور", "أعد كتابة كلمة المرور الجديدة:", show="*", parent=self.win)
        if confirm != new_password:
            messagebox.showwarning("عدم تطابق", "كلمتا المرور غير متطابقتين.", parent=self.win); return
        salt, digest = hash_password(new_password)
        self.db.execute("UPDATE app_users SET password_salt=?,password_hash=? WHERE id=?", (salt, digest, self.selected_id))
        messagebox.showinfo("تم", "تم تحديث كلمة المرور.", parent=self.win)


class SchoolApp:
    def __init__(self, root, current_user):
        self.root = root
        self.current_user = current_user
        self.db = SchoolDB()
        self.managers = {}
        self.appearance_preferences = self._load_ui_preferences()
        self.theme_mode = self.appearance_preferences.get("theme_mode", "light")
        self.font_family = self.appearance_preferences.get("font_family", "Tahoma")
        self.font_size = int(self.appearance_preferences.get("font_size", 11) or 11)
        self._update_check_running = False
        self._update_download_running = False
        self.root.geometry("1440x900")
        self.root.minsize(1120, 720)
        self._style()
        # Use the full available screen on Windows; retain resizable window behavior.
        try:
            if sys.platform.startswith("win"):
                self.root.state("zoomed")
        except tk.TclError:
            pass
        self.header = ttk.Frame(root, padding=(18, 12), style="Header.TFrame")
        self.header.pack(fill="x")
        brand = ttk.Frame(self.header, style="Header.TFrame")
        brand.pack(side="right", anchor="e")
        brand_title = ttk.Frame(brand, style="Header.TFrame")
        brand_title.pack(anchor="e")
        self.brand_logo = None
        logo_path = RESOURCE_DIR / "assets" / "school_manager_logo.png"
        if logo_path.exists():
            try:
                self.brand_logo = tk.PhotoImage(file=str(logo_path))
                if self.brand_logo.width() > 72:
                    self.brand_logo = self.brand_logo.subsample(max(1, self.brand_logo.width() // 64), max(1, self.brand_logo.height() // 64))
                ttk.Label(brand_title, image=self.brand_logo).pack(side="right", padx=(0, 10))
            except tk.TclError:
                ttk.Label(brand_title, text="🎓", font=("Segoe UI Emoji", 25), foreground="#16866D").pack(side="right", padx=(0, 8))
        else:
            ttk.Label(brand_title, text="🎓", font=("Segoe UI Emoji", 25), foreground="#16866D").pack(side="right", padx=(0, 8))
        self.title_label = ttk.Label(brand_title, text="مدرستي Pro", style="HeaderTitle.TLabel")
        self.title_label.pack(side="right")
        ttk.Label(brand, text="نظام إدارة المدارس العراقية المتكامل", style="HeaderSubtitle.TLabel").pack(anchor="e")
        utilities = ttk.Frame(self.header, style="Header.TFrame")
        utilities.pack(side="left", anchor="w")
        ttk.Label(
            utilities,
            text=f"الإصدار {APP_VERSION}  •  المستخدم: {self.current_user['full_name']}",
            style="HeaderSubtitle.TLabel", anchor="w", justify="left"
        ).pack(side="top", anchor="w", pady=(0, 5))
        # Place actions on a separate full-width toolbar so all commands remain
        # visible on ordinary laptop screens instead of disappearing under the logo.
        self.action_toolbar = ttk.Frame(root, style="Header.TFrame", padding=(12, 4))
        self.action_toolbar.pack(fill="x")
        buttons = ttk.Frame(self.action_toolbar, style="Header.TFrame")
        buttons.pack(side="right", anchor="e")
        ttk.Button(buttons, text="🎨 إعدادات الواجهة", command=self.open_appearance_settings, style="Top.TButton").pack(side="left", padx=2)
        self.theme_button = ttk.Button(buttons, text=("☀️ الوضع الفاتح" if self.theme_mode == "dark" else "🌙 الوضع الداكن"), command=self.toggle_theme, style="Top.TButton")
        self.theme_button.pack(side="left", padx=2)
        self.update_button = ttk.Button(buttons, text="🔄 تحديث البرنامج", command=self.check_for_updates, style="Update.TButton")
        self.update_button.pack(side="left", padx=2)
        ttk.Button(buttons, text="🔐 تغيير كلمة المرور", command=self.change_password, style="Top.TButton").pack(side="left", padx=2)
        if self.current_user.get("is_admin"):
            ttk.Button(buttons, text="👥 إدارة الحسابات", command=self.manage_users, style="Top.TButton").pack(side="left", padx=2)
        ttk.Button(buttons, text="📁 مجلد البيانات", command=self.open_data_folder, style="Top.TButton").pack(side="left", padx=2)
        ttk.Button(buttons, text="ℹ️ حول البرنامج", command=self.show_about, style="Top.TButton").pack(side="left", padx=2)
        ttk.Separator(root).pack(fill="x")
        self.workspace = ttk.Frame(root)
        self.workspace.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        self.nav_frame = ttk.Frame(self.workspace, padding=10, style="Sidebar.TFrame", width=236)
        self.nav_frame.pack(side="right", fill="y", padx=(0, 8))
        self.nav_frame.pack_propagate(False)
        self.content_frame = ttk.Frame(self.workspace)
        self.content_frame.pack(side="left", fill="both", expand=True)
        self.notebook = ttk.Notebook(self.content_frame, style="Hidden.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.dashboard_frame = ttk.Frame(self.notebook, padding=12)
        self.notebook.add(self.dashboard_frame, text="🏠 الرئيسية")
        self._build_dashboard()

        # القوائم المرجعية تُنشأ قبل الطلاب والمقررات والجدول.
        self.classes_mgr = self._add_manager("الصفوف والشعب", self._class_config())
        self.teachers_mgr = self._add_manager("المدرسون", self._teacher_config())
        self.subjects_mgr = self._add_manager("المواد الدراسية", self._subject_config())
        self.students_mgr = self._add_manager("الطلاب", self._student_config())
        self.attendance_tab = AttendanceTab(self, self.notebook)
        self.notebook.add(self.attendance_tab.frame, text="📋 الحضور والغياب")
        self.assessments_mgr = self._add_manager("الامتحانات والدرجات", self._assessment_config())
        self.timetable_mgr = self._add_manager("جدول الحصص", self._timetable_config())
        self.fees_mgr = self._add_manager("الرسوم والأقساط", self._fees_config())
        self.documents_tab = DocumentsTab(self, self.notebook)
        self.notebook.add(self.documents_tab.frame, text="🗂️ الوثائق والأرشيف")
        self.reports_tab = ReportsTab(self, self.notebook)
        self.notebook.add(self.reports_tab.frame, text="📊 التقارير")
        self.settings_tab = SettingsTab(self, self.notebook)
        self.notebook.add(self.settings_tab.frame, text="🏫 بيانات المدرسة والدوام")
        self._build_sidebar_navigation()

        self.status_var = tk.StringVar(value="جاهز للعمل — تُحفظ البيانات محلياً على هذا الجهاز")
        self.status_display_var = tk.StringVar(value=display_arabic_text(self.status_var.get()))
        self.status_var.trace_add("write", lambda *_: self.status_display_var.set(display_arabic_text(self.status_var.get())))
        status = ttk.Label(root, textvariable=self.status_display_var, anchor="e", padding=(12, 6), style="Status.TLabel")
        status.pack(fill="x", side="bottom")
        self.update_window_title()
        self.refresh_everything()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # Check in the background, after the dashboard is visible; no startup wait if offline.
        self.root.after(2500, lambda: self.check_for_updates(automatic=True))

    def _style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        dark = getattr(self, "theme_mode", "light") == "dark"
        font_family = getattr(self, "font_family", "Tahoma") or "Tahoma"
        font_size = max(9, min(18, int(getattr(self, "font_size", 11) or 11)))
        def font_style(base=11, bold=False):
            size = max(8, min(36, font_size + (base - 11)))
            return (font_family, size, "bold" if bold else "normal")
        if dark:
            self.colors = {
                "bg": "#0C1626", "panel": "#14253A", "field": "#0F1D30",
                "text": "#EFF5FB", "muted": "#B3C4D7", "accent": "#24B6A6",
                "accent_dark": "#0C3446", "border": "#2C4059",
                "button": "#22364D", "selected": "#1B5960", "heading": "#20374F",
                "stripe": "#182B42", "header": "#0B2138", "sidebar": "#0B2138",
                "gold": "#F4C45E", "danger": "#D96B6B", "success": "#2DA77A",
            }
        else:
            self.colors = {
                "bg": "#F3F7FC", "panel": "#FFFFFF", "field": "#FBFDFF",
                "text": "#19324B", "muted": "#62788E", "accent": "#159E98",
                "accent_dark": "#143D5B", "border": "#D4E1ED",
                "button": "#E7F0F7", "selected": "#C9F1EB", "heading": "#DFEBF5",
                "stripe": "#F4F8FC", "header": "#123653", "sidebar": "#123653",
                "gold": "#F2BD4D", "danger": "#C94F5D", "success": "#16855B",
            }
        # Apply user-selected appearance colors without changing the theme's structural contrast.
        for preference_key, palette_key in (("text_color", "text"), ("accent_color", "accent"),
                                            ("field_color", "field"), ("panel_color", "panel")):
            custom = getattr(self, "appearance_preferences", {}).get(preference_key, "")
            if isinstance(custom, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", custom):
                self.colors[palette_key] = custom.upper()
        c = self.colors
        self.root.configure(bg=c["bg"])
        if hasattr(self, "dashboard_canvas"):
            try:
                self.dashboard_canvas.configure(background=c["bg"])
            except tk.TclError:
                pass
        if hasattr(self, "nav_canvas"):
            try:
                self.nav_canvas.configure(background=c["sidebar"])
            except tk.TclError:
                pass
        style.configure(".", font=font_style(11))
        style.configure("TFrame", background=c["bg"])
        style.configure("Header.TFrame", background=c["header"])
        style.configure("Hero.TFrame", background=c["panel"], bordercolor=c["border"], relief="solid")
        style.configure("Step.TFrame", background=c["panel"], bordercolor=c["border"], relief="solid")
        style.configure("TLabelframe", background=c["panel"], bordercolor=c["border"], relief="solid", padding=7)
        style.configure("TLabelframe.Label", background=c["panel"], foreground=c["accent_dark"], font=font_style(11, bold=True))
        style.configure("Card.TLabelframe", background=c["panel"], bordercolor=c["border"], relief="solid", padding=12)
        style.configure("Card.TLabelframe.Label", background=c["panel"], foreground=c["accent_dark"], font=font_style(11, bold=True))
        style.configure("TLabel", background=c["bg"], foreground=c["text"], font=font_style(11))
        style.configure("Panel.TLabel", background=c["panel"], foreground=c["text"])
        style.configure("Metric.TLabel", font=font_style(25, bold=True), foreground=c["accent"], background=c["panel"])
        style.configure("MetricBlue.TLabel", font=font_style(25, bold=True), foreground=("#76B7FF" if dark else "#2767AE"), background=c["panel"])
        style.configure("MetricTeal.TLabel", font=font_style(25, bold=True), foreground=c["accent"], background=c["panel"])
        style.configure("MetricGold.TLabel", font=font_style(25, bold=True), foreground=("#F4C45E" if dark else "#B77918"), background=c["panel"])
        style.configure("MetricPurple.TLabel", font=font_style(25, bold=True), foreground=("#C6A0FF" if dark else "#8051B7"), background=c["panel"])
        style.configure("PageTitle.TLabel", font=font_style(17, bold=True), foreground=("#F2C766" if dark else c["accent_dark"]), background=c["bg"])
        style.configure("DashboardIntro.TLabel", font=font_style(24, bold=True), foreground=("#F2C766" if dark else c["accent_dark"]), background=c["bg"])
        style.configure("Title.TLabel", font=font_style(26, bold=True), foreground="#FFFFFF", background=c["header"])
        style.configure("HeaderTitle.TLabel", font=font_style(25, bold=True), foreground="#FFFFFF", background=c["header"])
        style.configure("HeaderSubtitle.TLabel", font=font_style(10, bold=True), foreground="#D0E2F0", background=c["header"])
        style.configure("Subtitle.TLabel", font=font_style(11, bold=True), foreground=c["muted"], background=c["bg"])
        style.configure("Hint.TLabel", font=font_style(10), foreground=c["muted"], background=c["bg"])
        style.configure("StepTitle.TLabel", font=font_style(11, bold=True), foreground=c["accent_dark"], background=c["panel"])
        style.configure("StepHint.TLabel", font=font_style(9), foreground=c["muted"], background=c["panel"])
        style.configure("Status.TLabel", font=font_style(10, bold=True), foreground="#F3F8FF", background=c["accent_dark"])
        style.configure("TButton", font=font_style(10, bold=True), padding=(10, 8), background=c["button"], foreground=c["text"], bordercolor=c["border"])
        style.map("TButton", background=[("active", c["selected"]), ("pressed", c["selected"])], foreground=[("disabled", c["muted"])])
        style.configure("Primary.TButton", font=font_style(10, bold=True), padding=(12, 9), background=c["accent"], foreground="#FFFFFF", bordercolor=c["accent"])
        style.map("Primary.TButton", background=[("active", c["accent_dark"]), ("pressed", c["accent_dark"])], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("Success.TButton", font=font_style(10, bold=True), padding=(10, 8), background=c["success"], foreground="#FFFFFF", bordercolor=c["success"])
        style.map("Success.TButton", background=[("active", c["accent_dark"]), ("pressed", c["accent_dark"])], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("Danger.TButton", font=font_style(10, bold=True), padding=(10, 8), background=c["danger"], foreground="#FFFFFF", bordercolor=c["danger"])
        style.map("Danger.TButton", background=[("active", "#993B4A"), ("pressed", "#993B4A")], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("Top.TButton", font=font_style(9, bold=True), padding=(7, 6), background="#1D4D6D", foreground="#FFFFFF", bordercolor="#2B5B7C")
        style.map("Top.TButton", background=[("active", "#286181"), ("pressed", "#286181")], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("Update.TButton", font=font_style(9, bold=True), padding=(8, 6), background=c["gold"], foreground="#15324A", bordercolor=c["gold"])
        style.map("Update.TButton", background=[("active", "#FFD879"), ("pressed", "#E4AB35")], foreground=[("active", "#15324A"), ("pressed", "#15324A")])
        style.configure("Step.TButton", font=font_style(11, bold=True), padding=(8, 10), background=c["button"], foreground=c["text"], bordercolor=c["border"])
        style.map("Step.TButton", background=[("active", c["selected"]), ("pressed", c["selected"])])
        style.configure("TEntry", font=font_style(11), padding=8, fieldbackground=c["field"], foreground=c["text"])
        style.configure("TCombobox", font=font_style(11), padding=7, fieldbackground=c["field"], foreground=c["text"], arrowcolor=c["accent"])
        style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["text"])])
        style.configure("Treeview", font=font_style(11), rowheight=max(30, 36 + (font_size - 11) * 3), background=c["panel"], fieldbackground=c["panel"], foreground=c["text"])
        style.configure("Treeview.Heading", font=font_style(11, bold=True), background=c["heading"], foreground=c["accent_dark"], padding=(10, 10))
        style.map("Treeview", background=[("selected", c["selected"])], foreground=[("selected", c["text"])])
        style.configure("TNotebook", background=c["bg"], tabmargins=(4, 5, 4, 0))
        style.configure("TNotebook.Tab", font=font_style(11, bold=True), padding=(14, 11), background=c["button"], foreground=c["text"])
        style.map("TNotebook.Tab", background=[("selected", c["panel"]), ("active", c["selected"])], foreground=[("selected", c["accent_dark"] if not dark else "#93c5fd")])
        try:
            # The app has an intentional school-themed sidebar. Hide the duplicate
            # tab strip as well; styling only Hidden.TNotebook leaves tabs visible.
            style.layout("Hidden.TNotebook", [("Notebook.client", {"sticky": "nswe"})])
            # Tab elements use the child style, which must be hidden explicitly.
            style.layout("Hidden.TNotebook.Tab", [])
        except tk.TclError:
            pass
        # The navigation stays a dark, high-contrast school-themed rail in both themes.
        style.configure("Sidebar.TFrame", background=c["sidebar"], bordercolor=c["sidebar"], relief="flat")
        style.configure("SidebarTitle.TLabel", background=c["sidebar"], foreground="#FFFFFF", font=font_style(14, bold=True))
        style.configure("SidebarHint.TLabel", background=c["sidebar"], foreground="#C3D8E7", font=font_style(9))
        style.configure("Nav.TButton", anchor="e", font=font_style(10, bold=True), padding=(9, 8), background=c["sidebar"], foreground="#E9F3FA", bordercolor=c["sidebar"])
        style.map("Nav.TButton", background=[("active", "#1B4A69"), ("pressed", "#1B4A69")], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("NavActive.TButton", anchor="e", font=font_style(10, bold=True), padding=(9, 8), background=c["accent"], foreground="#FFFFFF", bordercolor=c["accent"])
        style.map("NavActive.TButton", background=[("active", "#0B807A"), ("pressed", "#0B807A")], foreground=[("active", "#FFFFFF"), ("pressed", "#FFFFFF")])
        style.configure("EmptyState.TLabel", font=font_style(12, bold=True), foreground=c["muted"], background=c["panel"], padding=14)
        self._update_tree_row_colors()

    def _update_tree_row_colors(self):
        if not hasattr(self, "root"):
            return
        colors = getattr(self, "colors", {})
        if not colors:
            return
        def visit(widget):
            try:
                if isinstance(widget, ttk.Treeview):
                    widget.tag_configure("row_even", background=colors.get("panel", "#FFFFFF"))
                    widget.tag_configure("row_odd", background=colors.get("stripe", "#F4F8FC"))
                for child in widget.winfo_children():
                    visit(child)
            except tk.TclError:
                pass
        visit(self.root)

    def _build_sidebar_navigation(self):
        ttk.Label(self.nav_frame, text="القائمة الرئيسية", style="SidebarTitle.TLabel", anchor="e").pack(fill="x", pady=(5, 3))
        ttk.Label(self.nav_frame, text="اختر القسم الذي تريد العمل به", style="SidebarHint.TLabel", anchor="e").pack(fill="x", pady=(0, 10))
        ttk.Separator(self.nav_frame).pack(fill="x", pady=(0, 8))

        # Keep all school sections accessible even when the user increases font size
        # or runs the app on a short laptop screen.
        list_area = ttk.Frame(self.nav_frame, style="Sidebar.TFrame")
        list_area.pack(fill="both", expand=True)
        self.nav_canvas = tk.Canvas(
            list_area, background=self.colors["sidebar"], highlightthickness=0, borderwidth=0
        )
        self.nav_scrollbar = ttk.Scrollbar(list_area, orient="vertical", command=self.nav_canvas.yview)
        self.nav_canvas.configure(yscrollcommand=self.nav_scrollbar.set)
        self.nav_canvas.pack(side="right", fill="both", expand=True)
        self.nav_scrollbar.pack(side="left", fill="y")
        self.nav_list_frame = ttk.Frame(self.nav_canvas, style="Sidebar.TFrame")
        self._nav_canvas_window = self.nav_canvas.create_window((0, 0), window=self.nav_list_frame, anchor="nw")
        self.nav_list_frame.bind(
            "<Configure>",
            lambda _event: self.nav_canvas.configure(scrollregion=self.nav_canvas.bbox("all"))
        )
        self.nav_canvas.bind(
            "<Configure>",
            lambda event: self.nav_canvas.itemconfigure(self._nav_canvas_window, width=event.width)
        )

        def nav_wheel(event):
            try:
                under = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
            except tk.TclError:
                return None
            inside = False
            while under is not None:
                if under == self.nav_list_frame or under == self.nav_canvas:
                    inside = True
                    break
                under = getattr(under, "master", None)
            if not inside:
                return None
            if getattr(event, "num", None) == 4:
                step = -3
            elif getattr(event, "num", None) == 5:
                step = 3
            else:
                delta = getattr(event, "delta", 0)
                step = -int(delta / 120) * 3 if delta else 0
            if step:
                self.nav_canvas.yview_scroll(step, "units")
                return "break"
            return None

        self.root.bind_all("<MouseWheel>", nav_wheel, add="+")
        self.root.bind_all("<Button-4>", nav_wheel, add="+")
        self.root.bind_all("<Button-5>", nav_wheel, add="+")

        self.nav_buttons = []
        self.nav_labels = [
            ("🏠", "الرئيسية"), ("🏫", "الصفوف والشعب"), ("👩‍🏫", "المدرسون"),
            ("📚", "المواد الدراسية"), ("🎓", "الطلاب"), ("📋", "الحضور والغياب"),
            ("📝", "الامتحانات والدرجات"), ("🗓️", "جدول الحصص"), ("💳", "الرسوم والأقساط"),
            ("🗂️", "الوثائق والأرشيف"), ("📊", "التقارير"), ("⚙️", "بيانات المدرسة والدوام"),
        ]
        for index, (icon, label) in enumerate(self.nav_labels):
            button = ttk.Button(
                self.nav_list_frame, text=f"{icon}   {label}", style="Nav.TButton",
                command=lambda page=index: self.show_page(page)
            )
            button.pack(fill="x", pady=2)
            self.nav_buttons.append(button)
        ttk.Separator(self.nav_frame).pack(fill="x", pady=(8, 5))
        ttk.Label(self.nav_frame, text="✓ تُحفظ السجلات على هذا الجهاز", style="SidebarHint.TLabel", anchor="e", wraplength=205).pack(fill="x", pady=(0, 3))
        self.show_page(0)

    def show_page(self, index):
        try:
            tabs = self.notebook.tabs()
            index = max(0, min(int(index), len(tabs) - 1))
            self.notebook.select(tabs[index])
            if hasattr(self, "nav_buttons"):
                for i, button in enumerate(self.nav_buttons):
                    button.configure(style="NavActive.TButton" if i == index else "Nav.TButton")
        except (tk.TclError, ValueError, TypeError):
            pass

    def toggle_theme(self):
        self.theme_mode = "dark" if self.theme_mode == "light" else "light"
        self._save_theme_mode()
        self._style()
        self._update_tree_row_colors()
        if hasattr(self, "theme_button"):
            self.theme_button.configure(text=("☀️ الوضع الفاتح" if self.theme_mode == "dark" else "🌙 الوضع الداكن"))
        self.status_var.set("تم تغيير مظهر البرنامج")

    def _build_dashboard(self):
        # The dashboard contains onboarding cards and quick actions. Make the page
        # scrollable so these controls remain accessible on shorter laptop displays.
        self.dashboard_canvas = tk.Canvas(
            self.dashboard_frame, background=self.colors["bg"],
            highlightthickness=0, borderwidth=0
        )
        self.dashboard_scrollbar = ttk.Scrollbar(
            self.dashboard_frame, orient="vertical", command=self.dashboard_canvas.yview
        )
        self.dashboard_canvas.configure(yscrollcommand=self.dashboard_scrollbar.set)
        self.dashboard_canvas.pack(side="left", fill="both", expand=True)
        self.dashboard_scrollbar.pack(side="right", fill="y")
        self.dashboard_inner = ttk.Frame(self.dashboard_canvas, padding=12)
        self._dashboard_canvas_window = self.dashboard_canvas.create_window(
            (0, 0), window=self.dashboard_inner, anchor="nw"
        )
        self.dashboard_inner.bind(
            "<Configure>",
            lambda _event: self.dashboard_canvas.configure(scrollregion=self.dashboard_canvas.bbox("all"))
        )
        self.dashboard_canvas.bind(
            "<Configure>",
            lambda event: self.dashboard_canvas.itemconfigure(self._dashboard_canvas_window, width=event.width)
        )

        def dashboard_wheel(event):
            if not self.dashboard_canvas.winfo_ismapped():
                return None
            try:
                under = self.root.winfo_containing(self.root.winfo_pointerx(), self.root.winfo_pointery())
            except tk.TclError:
                return None
            inside = False
            while under is not None:
                if under == self.dashboard_inner or under == self.dashboard_canvas:
                    inside = True
                    break
                under = getattr(under, "master", None)
            if not inside:
                return None
            if getattr(event, "num", None) == 4:
                step = -3
            elif getattr(event, "num", None) == 5:
                step = 3
            else:
                delta = getattr(event, "delta", 0)
                step = -int(delta / 120) * 3 if delta else 0
            if step:
                self.dashboard_canvas.yview_scroll(step, "units")
                return "break"
            return None

        self.root.bind_all("<MouseWheel>", dashboard_wheel, add="+")
        self.root.bind_all("<Button-4>", dashboard_wheel, add="+")
        self.root.bind_all("<Button-5>", dashboard_wheel, add="+")
        intro = ttk.Label(self.dashboard_inner, text="🎓  مدرستي Pro", style="DashboardIntro.TLabel")
        intro.pack(anchor="e", pady=(2, 4))
        ttk.Label(
            self.dashboard_inner,
            text="نظام عراقي متكامل لتنظيم المدرسة: سجلات الطلبة والملاك والصفوف والحضور والدرجات والجداول.",
            wraplength=850, style="Subtitle.TLabel", anchor="e", justify="right"
        ).pack(anchor="e", pady=(0, 12))

        self.cards_frame = ttk.Frame(self.dashboard_inner)
        self.cards_frame.pack(fill="x", pady=(0, 14))
        self.metric_vars = {}
        metrics = [
            ("students", "🎓 الطلاب", "MetricBlue.TLabel"),
            ("teachers", "👩‍🏫 المدرسون", "MetricTeal.TLabel"),
            ("classes", "🏫 الصفوف والشعب", "Metric.TLabel"),
            ("absent", "📋 غياب اليوم", "MetricGold.TLabel"),
            ("docs", "🗂️ الوثائق", "MetricPurple.TLabel"),
        ]
        for i, (key, label, metric_style) in enumerate(metrics):
            card = ttk.LabelFrame(self.cards_frame, text=label, padding=(10, 8), style="Card.TLabelframe")
            card.grid(row=0, column=len(metrics) - 1 - i, sticky="nsew", padx=4, pady=2)
            var = tk.StringVar(value="0")
            self.metric_vars[key] = var
            ttk.Label(card, textvariable=var, style=metric_style, anchor="center").pack(fill="x", pady=(2, 4))
            self.cards_frame.columnconfigure(i, weight=1, uniform="metric")

        setup = ttk.LabelFrame(self.dashboard_inner, text="🚀 ابدأ إعداد مدرستك", padding=(10, 9), style="Card.TLabelframe")
        setup.pack(fill="x", pady=(0, 12))
        steps = [
            ("🏫 بيانات المدرسة", "اسم المدرسة والسنة الدراسية", 11),
            ("🏠 الصفوف والشعب", "أنشئ الصفوف والشعب أولاً", 1),
            ("👩‍🏫 المدرسون", "أضف الملاك والاختصاصات", 2),
            ("📚 المواد", "اربط المواد بالصفوف والمدرسين", 3),
            ("🎓 الطلاب", "سجّل بيانات الطلبة وشُعبهم", 4),
            ("📋 الحضور والغياب", "حدد الصف والتاريخ وسجّل الحالة", 5),
            ("🗓️ جدول الحصص", "نظّم الأيام والحصص والمدرسين", 7),
            ("📝 الدرجات", "أدخل الامتحانات ونتائج الطلبة", 6),
        ]
        for i, (title, description, page_index) in enumerate(steps):
            card = ttk.Frame(setup, padding=(8, 7), style="Step.TFrame")
            row, col = divmod(i, 4)
            col = 3 - col
            card.grid(row=row, column=col, sticky="nsew", padx=4, pady=4)
            ttk.Label(card, text=title, style="StepTitle.TLabel", anchor="e").pack(fill="x", pady=(0, 3))
            ttk.Label(card, text=description, style="StepHint.TLabel", anchor="e", justify="right", wraplength=215).pack(fill="x", pady=(0, 6))
            ttk.Button(card, text="فتح القسم ←", style="Step.TButton", command=lambda idx=page_index: self.show_page(idx)).pack(fill="x")
            setup.columnconfigure(col, weight=1, uniform="setup")

        quick = ttk.LabelFrame(self.dashboard_inner, text="⚡ الوصول السريع", padding=(10, 8), style="Card.TLabelframe")
        quick.pack(fill="x", pady=(0, 10))
        shortcuts = [
            ("➕ إضافة طالب", 4), ("✅ تسجيل الحضور", 5), ("🗓️ عرض جدول الحصص", 7),
            ("📊 التقارير", 10), ("💾 النسخ الاحتياطي", 11),
        ]
        for text, idx in shortcuts:
            ttk.Button(quick, text=text, style="Primary.TButton", command=lambda page=idx: self._dashboard_shortcut(page)).pack(side="right", padx=4, pady=3)
        ttk.Label(
            self.dashboard_inner,
            text="🔒 بيانات المدرسة والطلاب تُحفظ محلياً على هذا الجهاز. استخدم النسخ الاحتياطي قبل نقل البرنامج أو استعادة البيانات.",
            style="Hint.TLabel", anchor="e", justify="right", wraplength=850
        ).pack(fill="x", pady=(1, 0))

    def _dashboard_shortcut(self, page_index):
        self.show_page(page_index)
        if page_index == 4:
            self.students_mgr.clear_form()
        elif page_index == 5:
            self.attendance_tab.load_roster()
        elif page_index == 11:
            self.settings_tab.backup()

    @staticmethod
    def _tab_label(tab_name):
        icons = {
            "الصفوف والشعب": "🏫", "المدرسون": "👩‍🏫", "المواد الدراسية": "📚",
            "الطلاب": "🎓", "الامتحانات والدرجات": "📝", "جدول الحصص": "🗓️",
            "الرسوم والأقساط": "💳", "الحضور والغياب": "📋",
            "الوثائق والأرشيف": "🗂️", "التقارير": "📊",
            "بيانات المدرسة والدوام": "🏫",
        }
        return f"{icons.get(tab_name, '📘')} {tab_name}"

    def _add_manager(self, tab_name, cfg):
        frame = ttk.Frame(self.notebook)
        self.notebook.add(frame, text=self._tab_label(tab_name))
        mgr = EntityManager(self, frame, cfg)
        self.managers[cfg["table"]] = mgr
        return mgr

    @staticmethod
    def _field(name, label, required=False, kind="entry", options=None):
        d = {"name": name, "label": label, "required": required, "kind": kind}
        if options is not None:
            d["options"] = options
        return d

    def _class_options(self):
        rows = self.db.query("SELECT id,grade,section,academic_year FROM classes ORDER BY grade,section")
        result = [("", None)]
        for r in rows:
            label = f"{r['grade']} - {r['section']}" + (f" ({r['academic_year']})" if r["academic_year"] else "")
            result.append((label, r["id"]))
        return result

    def _teacher_options(self):
        rows = self.db.query("SELECT id,employee_no,full_name FROM teachers ORDER BY full_name")
        return [("", None)] + [(f"{r['full_name']} — {r['employee_no']}", r["id"]) for r in rows]

    def _subject_options(self):
        rows = self.db.query("SELECT id,name,grade FROM subjects ORDER BY name")
        return [("", None)] + [(f"{r['name']}" + (f" ({r['grade']})" if r['grade'] else ""), r["id"]) for r in rows]

    def _period_options(self):
        count = safe_int(self.school_settings()["periods_count"], 7)
        return [("", None)] + [(str(i), i) for i in range(1, max(1, min(count, 15)) + 1)]

    def _student_options(self):
        rows = self.db.query("""SELECT s.id,s.student_no,s.full_name,COALESCE(c.grade,'') grade,COALESCE(c.section,'') section
            FROM students s LEFT JOIN classes c ON c.id=s.class_id ORDER BY s.full_name""")
        return [("", None)] + [
            (f"{r['full_name']} — {r['student_no']}" + (f" ({r['grade']} / {r['section']})" if r['grade'] or r['section'] else ""), r["id"])
            for r in rows
        ]

    def _assessment_config(self):
        return {
            "table": "assessments", "title": "الامتحانات والدرجات",
            "fields": [
                self._field("student_id", "الطالب", True, "combo", self._student_options),
                self._field("subject_id", "المادة", kind="combo", options=self._subject_options),
                self._field("exam_name", "اسم الامتحان", True),
                self._field("exam_date", "تاريخ الامتحان YYYY-MM-DD"),
                self._field("term", "الفصل / الكورس"),
                self._field("score", "الدرجة", True),
                self._field("max_score", "الدرجة الكاملة", True),
                self._field("notes", "ملاحظات"),
            ],
            "display_columns": [("id", "م", 48), ("student_name", "اسم الطالب", 210), ("exam_name", "الامتحان", 170), ("subject_name", "المادة", 150), ("exam_date", "التاريخ", 105), ("term", "الفصل", 100), ("score", "الدرجة", 80), ("max_score", "من", 70), ("percentage", "%", 70)],
            "list_query": "SELECT a.id,s.full_name student_name,a.exam_name,COALESCE(sub.name,'') subject_name,a.exam_date,a.term,a.score,a.max_score,ROUND(CASE WHEN a.max_score>0 THEN a.score*100.0/a.max_score ELSE 0 END,1) percentage FROM assessments a JOIN students s ON s.id=a.student_id LEFT JOIN subjects sub ON sub.id=a.subject_id",
            "search_fields": ["s.full_name", "s.student_no", "a.exam_name", "sub.name", "a.term"], "order_by": "a.exam_date DESC,a.exam_name,s.full_name"
        }

    def _fees_config(self):
        return {
            "table": "fee_records", "title": "الرسوم والأقساط",
            "fields": [
                self._field("student_id", "الطالب", True, "combo", self._student_options),
                self._field("academic_year", "السنة الدراسية"),
                self._field("fee_type", "نوع الرسم", True),
                self._field("amount", "المبلغ الكلي (د.ع)", True),
                self._field("paid_amount", "المبلغ المدفوع (د.ع)", True),
                self._field("due_date", "تاريخ الاستحقاق YYYY-MM-DD"),
                self._field("payment_date", "تاريخ الدفع YYYY-MM-DD"),
                self._field("notes", "ملاحظات"),
            ],
            "display_columns": [("id", "م", 48), ("student_name", "اسم الطالب", 220), ("academic_year", "السنة الدراسية", 115), ("fee_type", "نوع الرسم", 150), ("amount", "المبلغ", 100), ("paid_amount", "المدفوع", 100), ("remaining", "المتبقي", 100), ("due_date", "الاستحقاق", 105), ("payment_date", "الدفع", 105)],
            "list_query": "SELECT f.id,s.full_name student_name,f.academic_year,f.fee_type,f.amount,f.paid_amount,ROUND(f.amount-f.paid_amount,2) remaining,f.due_date,f.payment_date FROM fee_records f JOIN students s ON s.id=f.student_id",
            "search_fields": ["s.full_name", "s.student_no", "f.academic_year", "f.fee_type"], "order_by": "f.due_date DESC,s.full_name"
        }

    def _class_config(self):
        return {
            "table": "classes", "title": "الصفوف والشعب",
            "fields": [self._field("grade", "الصف الدراسي", True), self._field("section", "الشعبة", True), self._field("room", "القاعة / الغرفة"), self._field("capacity", "السعة القصوى"), self._field("academic_year", "السنة الدراسية")],
            "display_columns": [("id", "م", 55), ("grade", "الصف", 180), ("section", "الشعبة", 100), ("room", "القاعة", 120), ("capacity", "السعة", 90), ("academic_year", "السنة الدراسية", 140)],
            "list_query": "SELECT id,grade,section,room,capacity,academic_year FROM classes",
            "search_fields": ["grade", "section", "room", "academic_year"], "order_by": "grade,section"
        }

    def _teacher_config(self):
        return {
            "table": "teachers", "title": "المدرسين",
            "fields": [self._field("employee_no", "الرقم الوظيفي", True), self._field("full_name", "الاسم الكامل", True), self._field("specialization", "الاختصاص"), self._field("phone", "الهاتف"), self._field("email", "البريد الإلكتروني"), self._field("hire_date", "تاريخ المباشرة"), self._field("status", "الحالة", kind="combo", options=lambda: [(x, x) for x in PERSON_STATUSES]), self._field("notes", "ملاحظات")],
            "display_columns": [("id", "م", 50), ("employee_no", "الرقم الوظيفي", 120), ("full_name", "اسم المدرس", 220), ("specialization", "الاختصاص", 160), ("phone", "الهاتف", 130), ("email", "البريد الإلكتروني", 190), ("status", "الحالة", 100)],
            "list_query": "SELECT id,employee_no,full_name,specialization,phone,email,status FROM teachers",
            "search_fields": ["employee_no", "full_name", "specialization", "phone", "email"], "order_by": "full_name"
        }

    def _subject_config(self):
        return {
            "table": "subjects", "title": "المواد الدراسية",
            "fields": [self._field("name", "اسم المادة", True), self._field("grade", "الصف الدراسي"), self._field("weekly_lessons", "الحصص الأسبوعية"), self._field("teacher_id", "المدرس المسؤول", kind="combo", options=self._teacher_options)],
            "display_columns": [("id", "م", 50), ("name", "المادة", 220), ("grade", "الصف", 160), ("weekly_lessons", "حصص أسبوعية", 120), ("teacher_name", "المدرس المسؤول", 220)],
            "list_query": "SELECT s.id,s.name,s.grade,s.weekly_lessons,COALESCE(t.full_name,'') teacher_name FROM subjects s LEFT JOIN teachers t ON t.id=s.teacher_id",
            "search_fields": ["s.name", "s.grade"], "order_by": "s.name"
        }

    def _student_config(self):
        return {
            "table": "students", "title": "الطلاب",
            "fields": [self._field("student_no", "رقم الطالب", True), self._field("full_name", "اسم الطالب الكامل", True), self._field("gender", "الجنس", kind="combo", options=lambda: [("", None), ("ذكر", "ذكر"), ("أنثى", "أنثى")]), self._field("birth_date", "تاريخ الميلاد YYYY-MM-DD"), self._field("class_id", "الصف والشعبة", True, "combo", self._class_options), self._field("guardian_name", "اسم ولي الأمر"), self._field("guardian_phone", "هاتف ولي الأمر"), self._field("address", "العنوان"), self._field("enrollment_date", "تاريخ التسجيل"), self._field("status", "الحالة", kind="combo", options=lambda: [(x, x) for x in PERSON_STATUSES]), self._field("notes", "ملاحظات")],
            "display_columns": [("id", "م", 50), ("student_no", "رقم الطالب", 115), ("full_name", "اسم الطالب", 220), ("gender", "الجنس", 80), ("class_name", "الصف / الشعبة", 160), ("guardian_name", "ولي الأمر", 180), ("guardian_phone", "هاتف ولي الأمر", 140), ("status", "الحالة", 95)],
            "list_query": "SELECT s.id,s.student_no,s.full_name,s.gender,COALESCE(c.grade,'') || CASE WHEN c.section IS NULL OR c.section='' THEN '' ELSE ' / ' || c.section END AS class_name,s.guardian_name,s.guardian_phone,s.status FROM students s LEFT JOIN classes c ON c.id=s.class_id",
            "search_fields": ["s.student_no", "s.full_name", "s.guardian_name", "s.guardian_phone"], "order_by": "s.full_name"
        }

    def _timetable_config(self):
        return {
            "table": "timetable", "title": "جدول الحصص",
            "fields": [self._field("class_id", "الصف والشعبة", True, "combo", self._class_options), self._field("day", "اليوم", True, "combo", lambda: [(d, d) for d in DAYS]), self._field("period_no", "رقم الحصة", True, "combo", self._period_options), self._field("subject_id", "المادة", kind="combo", options=self._subject_options), self._field("teacher_id", "المدرس", kind="combo", options=self._teacher_options), self._field("start_time", "من (HH:MM)"), self._field("end_time", "إلى (HH:MM)"), self._field("room", "القاعة"), self._field("notes", "ملاحظات")],
            "display_columns": [("id", "م", 50), ("class_name", "الصف / الشعبة", 160), ("day", "اليوم", 100), ("period_no", "الحصة", 75), ("start_time", "من", 80), ("end_time", "إلى", 80), ("subject_name", "المادة", 180), ("teacher_name", "المدرس", 190), ("room", "القاعة", 100)],
            "list_query": "SELECT tt.id,COALESCE(c.grade,'') || ' - ' || COALESCE(c.section,'') class_name,tt.day,tt.period_no,tt.start_time,tt.end_time,COALESCE(s.name,'') subject_name,COALESCE(t.full_name,'') teacher_name,tt.room FROM timetable tt LEFT JOIN classes c ON c.id=tt.class_id LEFT JOIN subjects s ON s.id=tt.subject_id LEFT JOIN teachers t ON t.id=tt.teacher_id",
            "search_fields": ["c.grade", "c.section", "tt.day", "s.name", "t.full_name"], "order_by": "tt.day,tt.period_no,c.grade,c.section"
        }

    def school_settings(self):
        r = self.db.one("SELECT * FROM school_info WHERE id=1")
        return dict(r) if r else {"name": "مدرستي", "morning_start": "08:00", "periods_count": 7, "period_minutes": 40, "break_minutes": 5}

    def update_window_title(self):
        settings = self.school_settings()
        name = settings.get("name") or "مدرستي"
        self.root.title(f"{APP_NAME} — {name}")
        self.title_label.config(text=f"مدرستي Pro | {name}")

    def change_password(self):
        username = self.current_user["username"]
        row = self.db.one("SELECT * FROM app_users WHERE username=? COLLATE NOCASE", (username,))
        if not row:
            messagebox.showerror("خطأ", "تعذّر العثور على حساب المستخدم الحالي.", parent=self.root)
            return
        current = simpledialog.askstring("تغيير كلمة المرور", "أدخل كلمة المرور الحالية:", show="*", parent=self.root)
        if current is None:
            return
        if not verify_password(current, row["password_salt"], row["password_hash"]):
            messagebox.showerror("كلمة مرور غير صحيحة", "كلمة المرور الحالية غير صحيحة.", parent=self.root)
            return
        new_password = simpledialog.askstring("تغيير كلمة المرور", "أدخل كلمة المرور الجديدة (10 أحرف فأكثر):", show="*", parent=self.root)
        if new_password is None:
            return
        if len(new_password) < 10:
            messagebox.showwarning("كلمة المرور قصيرة", "استخدم 10 أحرف على الأقل.", parent=self.root)
            return
        confirm = simpledialog.askstring("تأكيد كلمة المرور", "أعد كتابة كلمة المرور الجديدة:", show="*", parent=self.root)
        if confirm != new_password:
            messagebox.showwarning("عدم تطابق", "كلمتا المرور غير متطابقتين.", parent=self.root)
            return
        salt, digest = hash_password(new_password)
        self.db.execute("UPDATE app_users SET password_salt=?,password_hash=? WHERE id=?", (salt, digest, row["id"]))
        messagebox.showinfo("تم", "تم تغيير كلمة المرور بنجاح.", parent=self.root)

    def manage_users(self):
        if not self.current_user.get("is_admin"):
            messagebox.showwarning("صلاحية غير كافية", "إدارة الحسابات متاحة لحساب المدير فقط.", parent=self.root)
            return
        UserManagementDialog(self)

    def open_data_folder(self):
        try:
            if sys.platform.startswith("win"):
                os.startfile(str(DATA_DIR))  # type: ignore[attr-defined]
            else:
                webbrowser.open(DATA_DIR.resolve().as_uri())
        except Exception as exc:
            messagebox.showerror("تعذّر فتح المجلد", str(exc), parent=self.root)

    def _load_ui_preferences(self):
        """Load per-Windows-user theme, font, and color choices safely."""
        defaults = {"theme_mode": "light", "font_family": "Tahoma", "font_size": 11,
                    "text_color": "", "accent_color": "", "field_color": "", "panel_color": ""}
        path = DATA_DIR / "ui_preferences.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                defaults.update({key: raw[key] for key in defaults if key in raw})
        except (OSError, ValueError, AttributeError, TypeError):
            pass
        if defaults.get("theme_mode") not in ("light", "dark"):
            defaults["theme_mode"] = "light"
        try:
            defaults["font_size"] = max(9, min(18, int(defaults.get("font_size", 11))))
        except (TypeError, ValueError):
            defaults["font_size"] = 11
        for key in ("text_color", "accent_color", "field_color", "panel_color"):
            value = defaults.get(key, "")
            if value and not (isinstance(value, str) and re.fullmatch(r"#[0-9A-Fa-f]{6}", value)):
                defaults[key] = ""
        return defaults

    def _load_theme_mode(self):
        return self.appearance_preferences.get("theme_mode", "light")

    def _save_ui_preferences(self):
        try:
            (DATA_DIR / "ui_preferences.json").write_text(
                json.dumps(self.appearance_preferences, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            messagebox.showwarning("حفظ الإعدادات", f"تم تطبيق الاختيار مؤقتاً، لكن تعذّر حفظه على القرص:\n{exc}", parent=self.root)

    def _save_theme_mode(self):
        self.appearance_preferences["theme_mode"] = self.theme_mode
        self._save_ui_preferences()

    def open_appearance_settings(self):
        AppearanceSettingsDialog(self)

    @staticmethod
    def _update_repository():
        """Read the repository setting packaged with the app or supplied beside it."""
        candidates = [APP_DIR / "update_config.json", RESOURCE_DIR / "update_config.json"]
        for path in candidates:
            try:
                config = json.loads(path.read_text(encoding="utf-8"))
                repository = str(config.get("repository", "")).strip()
                if repository and repository.upper() != "OWNER/REPOSITORY":
                    return repository
            except (OSError, ValueError, AttributeError):
                continue
        return ""

    def check_for_updates(self, automatic=False):
        """Check GitHub Releases without blocking the UI thread."""
        if self._update_check_running or self._update_download_running:
            if not automatic:
                messagebox.showinfo("التحديث", "عملية التحديث جارية بالفعل.", parent=self.root)
            return
        repository = self._update_repository()
        if not repository or repository.upper() == "OWNER/REPOSITORY":
            if not automatic:
                messagebox.showinfo(
                    "إعداد التحديث التلقائي",
                    "زر التحديث جاهز، لكن مصدر التحديث المركزي لم يُربط بعد.\n\n"
                    "يلزم نشر المشروع في مستودع GitHub عام، ثم تشغيل مسار Build and publish Madrasati Pro update.\n"
                    "سيُربط المستودع تلقائياً داخل الإصدارات المنشورة من خلال مسار البناء.",
                    parent=self.root,
                )
            return
        self._update_check_running = True
        if hasattr(self, "status_var"):
            self.status_var.set("🔄 جارٍ التحقق من وجود إصدار جديد...")
        if hasattr(self, "update_button"):
            self.update_button.configure(state="disabled", text="⏳ جارٍ التحقق...")

        def worker():
            try:
                service = UpdateService(repository, APP_VERSION)
                release = service.latest_release()
                result = (service, release, None)
            except Exception as exc:
                result = (None, None, exc)
            try:
                self.root.after(0, lambda: self._finish_update_check(result, automatic))
            except tk.TclError:
                pass
        threading.Thread(target=worker, name="MadrasatiUpdateCheck", daemon=True).start()

    def _finish_update_check(self, result, automatic=False):
        self._update_check_running = False
        if hasattr(self, "update_button"):
            self.update_button.configure(state="normal", text="🔄 تحديث البرنامج")
        service, release, error = result
        if error:
            if hasattr(self, "status_var"):
                self.status_var.set("تعذّر التحقق من التحديث — البرنامج يعمل كالمعتاد")
            if not automatic:
                messagebox.showwarning("تعذّر التحديث", str(error), parent=self.root)
            return
        latest_version = release["version"]
        if not is_newer(latest_version, APP_VERSION):
            if hasattr(self, "status_var"):
                self.status_var.set(f"✅ البرنامج محدّث — الإصدار {APP_VERSION}")
            if not automatic:
                messagebox.showinfo("البرنامج محدّث", f"أنت تستخدم أحدث إصدار متاح ({APP_VERSION}).", parent=self.root)
            return
        if hasattr(self, "status_var"):
            self.status_var.set(f"🆕 يتوفر تحديث جديد: {latest_version}")
        notes = release.get("notes") or "تحسينات وإصلاحات عامة."
        prompt = (
            f"يتوفر إصدار جديد من مدرستي Pro.\n\n"
            f"الإصدار الحالي: {APP_VERSION}\nالإصدار الجديد: {latest_version}\n\n"
            f"ملاحظات الإصدار:\n{notes[:1800]}\n\n"
            "هل تريد تنزيل التحديث وتثبيته الآن؟ سيُغلق البرنامج للحظات، وستبقى بيانات المدرسة محفوظة."
        )
        if messagebox.askyesno("تحديث جديد متوفر", prompt, parent=self.root):
            self._download_update(service, release)

    def _download_update(self, service, release):
        if self._update_download_running:
            return
        if sys.platform != "win32":
            messagebox.showwarning("غير مدعوم", "تثبيت تحديث Windows متاح من نسخة Windows المثبتة فقط.", parent=self.root)
            return
        self._update_download_running = True
        if hasattr(self, "update_button"):
            self.update_button.configure(state="disabled", text="⬇️ جارٍ التنزيل...")
        self.status_var.set(f"⬇️ جارٍ تنزيل الإصدار {release['version']} والتحقق منه...")

        def worker():
            try:
                installer = service.download_and_verify(release)
                result = (installer, None)
            except Exception as exc:
                result = (None, exc)
            try:
                self.root.after(0, lambda: self._finish_update_download(result, release))
            except tk.TclError:
                pass
        threading.Thread(target=worker, name="MadrasatiUpdateDownload", daemon=True).start()

    def _finish_update_download(self, result, release):
        self._update_download_running = False
        installer, error = result
        if hasattr(self, "update_button"):
            self.update_button.configure(state="normal", text="🔄 تحديث البرنامج")
        if error:
            self.status_var.set("فشل تنزيل التحديث — الإصدار الحالي لم يتغير")
            messagebox.showerror("فشل التحديث", str(error), parent=self.root)
            return
        try:
            # Explain the UAC prompt before starting the installer, then exit promptly.
            messagebox.showinfo(
                "بدء التحديث",
                "تم تنزيل التحديث والتحقق من سلامته. سيُغلق البرنامج الآن ليكمل مُثبّت Windows التحديث.\n"
                "قد يطلب Windows موافقة المسؤول، وهذا طبيعي عند تحديث برنامج مثبت داخل Program Files.",
                parent=self.root,
            )
            creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen(
                [str(installer), "/SP-", "/SILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
                cwd=str(installer.parent), creationflags=creation_flags,
            )
            self.on_close()
        except Exception as exc:
            messagebox.showerror("تعذّر تشغيل المُثبّت", str(exc), parent=self.root)

    def show_about(self):
        messagebox.showinfo(
            "حول مدرستي Pro",
            f"مدرستي Pro — نظام إدارة المدارس\nالإصدار: {APP_VERSION}\n\nإدارة الطلاب والمدرسين والحضور والدرجات والرسوم والجدول والوثائق والتقارير.\n\nتُحفظ البيانات محلياً في:\n{DATA_DIR}\n\nتذكير: أنشئ نسخة احتياطية دورية واحمِ بيانات الطلبة.",
            parent=self.root,
        )

    def refresh_everything(self):
        for mgr in self.managers.values():
            mgr.refresh()
        if hasattr(self, "attendance_tab"):
            self.attendance_tab.refresh_classes()
            self.attendance_tab.load_roster()
        if hasattr(self, "documents_tab"):
            self.documents_tab.refresh_owners()
            self.documents_tab.refresh()
        if hasattr(self, "reports_tab"):
            self.reports_tab.refresh_classes()
            self.reports_tab.generate()
        self.refresh_dashboard()

    def refresh_dashboard(self):
        if not hasattr(self, "metric_vars"):
            return
        values = {
            "students": self.db.one("SELECT COUNT(*) n FROM students WHERE status='فعّال'")["n"],
            "teachers": self.db.one("SELECT COUNT(*) n FROM teachers WHERE status='فعّال'")["n"],
            "classes": self.db.one("SELECT COUNT(*) n FROM classes")["n"],
            "absent": self.db.one("SELECT COUNT(*) n FROM attendance WHERE attendance_date=? AND status='غائب'", (today_text(),))["n"],
            "docs": self.db.one("SELECT COUNT(*) n FROM documents")["n"],
        }
        for key, val in values.items():
            self.metric_vars[key].set(str(val))

    def on_close(self):
        try:
            self.db.conn.commit()
            self.db.close()
        finally:
            self.root.destroy()


def show_splash_screen(root, duration_ms=2400):
    """Brief branded startup screen shown before login/setup."""
    splash = tk.Toplevel(root)
    splash.overrideredirect(True)
    splash.configure(bg="#102D49")
    width, height = 640, 380
    try:
        sw, sh = splash.winfo_screenwidth(), splash.winfo_screenheight()
        x, y = max(0, (sw - width) // 2), max(0, (sh - height) // 2)
        splash.geometry(f"{width}x{height}+{x}+{y}")
    except tk.TclError:
        splash.geometry(f"{width}x{height}")
    try:
        splash.attributes("-topmost", True)
    except tk.TclError:
        pass

    top_accent = tk.Frame(splash, bg="#20B7A7", height=7)
    top_accent.pack(fill="x")
    main = tk.Frame(splash, bg="#102D49", padx=28, pady=18)
    main.pack(fill="both", expand=True)
    logo_path = RESOURCE_DIR / "assets" / "school_manager_logo.png"
    splash.logo_image = None
    if logo_path.exists():
        try:
            image = tk.PhotoImage(file=str(logo_path))
            factor = max(1, image.width() // 112)
            if factor > 1:
                image = image.subsample(factor, factor)
            splash.logo_image = image
            tk.Label(main, image=splash.logo_image, bg="#102D49", borderwidth=0).pack(pady=(0, 8))
        except tk.TclError:
            tk.Label(main, text="🎓", font=("Segoe UI Emoji", 58), fg="#F4C45E", bg="#102D49").pack(pady=(0, 8))
    else:
        tk.Label(main, text="🎓", font=("Segoe UI Emoji", 58), fg="#F4C45E", bg="#102D49").pack(pady=(0, 8))
    tk.Label(main, text="مدرستي Pro", font=("Tahoma", 28, "bold"), fg="#FFFFFF", bg="#102D49").pack(pady=(0, 3))
    tk.Label(main, text="نظام إدارة المدارس العراقية", font=("Tahoma", 14, "bold"), fg="#D2E4F0", bg="#102D49").pack(pady=(0, 5))
    tk.Label(main, text=f"النسخة {APP_VERSION}  •  الطلاب  •  المدرسون  •  الحضور  •  الجداول", font=("Tahoma", 10), fg="#F4C45E", bg="#102D49").pack(pady=(0, 17))
    progress = tk.Canvas(main, width=380, height=7, bg="#1D4966", highlightthickness=0, bd=0)
    progress.pack()
    fill = progress.create_rectangle(0, 0, 0, 7, fill="#20B7A7", outline="")
    steps = 30
    delay = max(25, duration_ms // steps)
    def animate(index=0):
        try:
            progress.coords(fill, 0, 0, 380 * min(index, steps) / steps, 7)
            if index >= steps:
                splash.after(100, splash.destroy)
            else:
                splash.after(delay, lambda: animate(index + 1))
        except tk.TclError:
            pass
    splash.after(80, animate)
    try:
        root.wait_window(splash)
    except tk.TclError:
        pass


def main():
    root = tk.Tk()
    # Keep the Tk root mapped while the login/setup Toplevel is shown.
    # On some Windows/Tk builds, a transient dialog attached to a withdrawn
    # root can remain invisible while wait_window() blocks startup.
    root.title(APP_NAME)
    root.geometry("1x1+0+0")
    root.resizable(False, False)
    db = None
    try:
        icon_path = RESOURCE_DIR / "assets" / "school_manager.ico"
        if icon_path.exists():
            try:
                root.iconbitmap(str(icon_path))
            except tk.TclError:
                pass
        show_splash_screen(root, duration_ms=2400)
        db = SchoolDB()
        auth = AuthenticationDialog(root, db)
        root.wait_window(auth.win)
        db.close()
        db = None
        if not auth.result:
            root.destroy()
            return
        SchoolApp(root, auth.result)
        root.deiconify()
    except Exception as exc:
        if db is not None:
            try:
                db.close()
            except Exception:
                pass
        try:
            log_path = LOGS_DIR / "startup_errors.log"
            with open(log_path, "a", encoding="utf-8") as log_file:
                log_file.write(f"\n[{datetime.now().isoformat(timespec='seconds')}] {exc}\n")
                log_file.write(traceback.format_exc())
                log_file.write("\n")
        except OSError:
            pass
        try:
            root.deiconify()
            messagebox.showerror("خطأ بدء التشغيل", f"تعذّر تشغيل البرنامج:\n{exc}\n\nإذا تكرر الخطأ، شارك ملف startup_errors.log مع الدعم الفني.", parent=root)
        finally:
            root.destroy()
        return
    root.mainloop()


if __name__ == "__main__":
    main()
