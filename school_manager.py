# -*- coding: utf-8 -*-
"""نظام إدارة المدرسة - تطبيق مكتبي عربي يعمل دون إنترنت.
Python 3.10+ | Tkinter + SQLite (بدون مكتبات خارجية)
"""
from __future__ import annotations

import csv
import hashlib
import hmac
import json
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
from tkinter import ttk, messagebox, filedialog, simpledialog

from updater import UpdateService, UpdateError, is_newer

APP_NAME = "مدرستي Pro — نظام إدارة المدارس"
APP_VERSION = "1.1.0"
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
DOCUMENT_CATEGORIES = ["بطاقة وطنية / هوية", "شهادة ميلاد", "وثيقة دراسية", "درجات", "نقل مدرسي", "موافقة ولي الأمر", "شهادة أو كتاب رسمي", "أخرى"]

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
        toolbar = ttk.Frame(self.frame)
        toolbar.pack(fill="x", pady=(0, 8))
        ttk.Label(toolbar, text="بحث:").pack(side="right", padx=(5, 3))
        search = ttk.Entry(toolbar, textvariable=self.search_var, width=28)
        search.pack(side="right", padx=4)
        search.bind("<KeyRelease>", lambda _e: self.refresh())
        ttk.Button(toolbar, text="تحديث", command=self.refresh).pack(side="right", padx=3)
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
        ttk.Button(buttons, text="حفظ سجل جديد", command=self.add_record).pack(side="right", padx=3)
        ttk.Button(buttons, text="تحديث السجل المحدد", command=self.update_record).pack(side="right", padx=3)
        ttk.Button(buttons, text="حذف المحدد", command=self.delete_record).pack(side="right", padx=3)
        ttk.Button(buttons, text="مسح الحقول", command=self.clear_form).pack(side="right", padx=3)
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

    def _populate_combo(self, field):
        widget = self.widgets[field["name"]]
        try:
            options = field["options"]()
        except Exception:
            options = []
        labels = [str(label) for label, _value in options]
        self.combo_maps[field["name"]] = {str(label): value for label, value in options}
        self.combo_maps[field["name"] + "_reverse"] = {str(value): str(label) for label, value in options}
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
        for row in rows:
            vals = [row[col] if row[col] is not None else "" for col, _heading, _width in self.config["display_columns"]]
            self.tree.insert("", "end", iid=str(row["id"]), values=vals)

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
        sample = ["ST-001", "اسم الطالب", "ذكر", "2012-01-01", "الأول المتوسط", "أ", "اسم ولي الأمر", "07xxxxxxxx", "العنوان", today_text(), "فعّال", ""]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.writer(f)
            writer.writerow(headers)
            writer.writerow(sample)
        messagebox.showinfo("قالب جاهز", "املأ القالب واحفظه CSV UTF-8، ثم استخدم زر استيراد CSV. احذف صف المثال قبل استيراد بياناتك.", parent=self.frame)

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
                    class_id = None
                    grade, section = (item.get("class_grade") or "").strip(), (item.get("class_section") or "").strip()
                    if grade and section:
                        class_row = self.db.one("SELECT id FROM classes WHERE grade=? AND section=? ORDER BY id DESC LIMIT 1", (grade, section))
                        class_id = class_row["id"] if class_row else None
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
        self.status_var = tk.StringVar(value="حاضر")
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
        ttk.Button(filters, text="تسجيل الجميع حاضرين", command=self.mark_all_present).pack(side="left", padx=5)

        editor = ttk.LabelFrame(self.frame, text="📝 تسجيل حالة الطالب المحدد", padding=10)
        editor.pack(fill="x", pady=(0, 8))
        ttk.Label(editor, text="الحالة:").pack(side="right", padx=5)
        ttk.Combobox(editor, textvariable=self.status_var, values=ATTENDANCE_STATUSES, state="readonly", width=12).pack(side="right", padx=5)
        ttk.Label(editor, text="ملاحظة:").pack(side="right", padx=5)
        ttk.Entry(editor, textvariable=self.note_var, width=44).pack(side="right", padx=5)
        ttk.Button(editor, text="حفظ حالة الطالب", command=self.save_one).pack(side="right", padx=5)
        ttk.Label(editor, text="يمكن النقر المزدوج على سجل طالب لاختياره.", style="Hint.TLabel").pack(side="left", padx=8)

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
        self.summary = ttk.Label(self.frame, text="", style="Hint.TLabel")
        self.summary.pack(anchor="e", pady=(7, 0))

    def refresh_classes(self):
        rows = self.db.query("SELECT id,grade,section,academic_year FROM classes ORDER BY grade,section")
        self.class_map = {f"{r['grade']} - {r['section']}" + (f" ({r['academic_year']})" if r['academic_year'] else ""): r["id"] for r in rows}
        values = list(self.class_map)
        self.class_combo["values"] = values
        if self.class_var.get() not in values:
            self.class_var.set(values[0] if values else "")

    def load_roster(self):
        self.selected_student_id = None
        self.status_var.set("حاضر")
        self.note_var.set("")
        self.refresh_classes()
        if not self._valid_date():
            messagebox.showwarning("تاريخ غير صحيح", "اكتب التاريخ بصيغة YYYY-MM-DD.", parent=self.frame)
            return
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        class_id = self.class_map.get(self.class_var.get())
        if not class_id:
            self.summary.config(text="أضف صفاً وشعبة من تبويب الصفوف والشعب أولاً.")
            return
        rows = self.db.query("""SELECT s.id,s.student_no,s.full_name,c.grade,c.section,a.status,a.note
            FROM students s LEFT JOIN classes c ON c.id=s.class_id
            LEFT JOIN attendance a ON a.student_id=s.id AND a.attendance_date=?
            WHERE s.class_id=? AND s.status='فعّال' ORDER BY s.full_name""", (self.date_var.get().strip(), class_id))
        for r in rows:
            class_name = f"{r['grade'] or ''} / {r['section'] or ''}"
            self.tree.insert("", "end", iid=str(r["id"]), values=(r["student_no"], r["full_name"], class_name, r["status"] or "غير مسجل", r["note"] or ""))
        self._update_summary()

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
        vals = self.tree.item(selection[0], "values")
        if vals:
            self.status_var.set(vals[3] if vals[3] in ATTENDANCE_STATUSES else "حاضر")
            self.note_var.set(vals[4] or "")

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
                (self.selected_student_id, self.date_var.get().strip(), self.status_var.get(), self.note_var.get().strip()))
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
        vals = [self.tree.item(i, "values")[3] for i in self.tree.get_children()]
        total = len(vals)
        parts = [f"مجموع الطلبة: {total}"]
        parts.extend(f"{s}: {sum(1 for x in vals if x == s)}" for s in ATTENDANCE_STATUSES)
        parts.append(f"غير مسجل: {sum(1 for x in vals if x == 'غير مسجل')}")
        self.summary.config(text="   |   ".join(parts))


class DocumentsTab:
    def __init__(self, app, parent):
        self.app, self.db = app, app.db
        self.frame = ttk.Frame(parent, padding=12)
        self.selected_id = None
        self.owner_type = tk.StringVar(value="المدرسة")
        self.owner_var = tk.StringVar()
        self.title_var = tk.StringVar()
        self.category_var = tk.StringVar(value="أخرى")
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
            ("🏫 الجهة المالكة", ttk.Combobox(form, textvariable=self.owner_type, values=["المدرسة", "طالب", "مدرس"], state="readonly", width=24)),
            ("👤 اسم الطالب / المدرس", ttk.Combobox(form, textvariable=self.owner_var, state="readonly", width=42)),
            ("📄 عنوان الوثيقة *", ttk.Entry(form, textvariable=self.title_var, width=36)),
            ("🗂️ نوع الوثيقة", ttk.Combobox(form, textvariable=self.category_var, values=DOCUMENT_CATEGORIES, state="readonly", width=24)),
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
        ttk.Button(form, text="اختيار ملف…", command=self.browse_file).grid(row=2, column=4, padx=6, pady=4, sticky="ew")
        buttons = ttk.Frame(form)
        buttons.grid(row=4, column=0, columnspan=5, sticky="ew", pady=(8, 0))
        ttk.Button(buttons, text="حفظ الوثيقة", command=self.save).pack(side="right", padx=3)
        ttk.Button(buttons, text="مسح الحقول", command=self.clear).pack(side="right", padx=3)
        ttk.Button(buttons, text="فتح الملف المحدد", command=self.open_selected).pack(side="left", padx=3)
        ttk.Button(buttons, text="حذف الوثيقة", command=self.delete_selected).pack(side="left", padx=3)
        ttk.Button(buttons, text="تصدير CSV", command=self.export_csv).pack(side="left", padx=3)

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

    def refresh_owners(self):
        t = self.owner_type.get()
        self.owner_map = {}
        if t == "طالب":
            rows = self.db.query("SELECT id,student_no,full_name FROM students ORDER BY full_name")
            self.owner_map = {f"{r['full_name']} — {r['student_no']}": r["id"] for r in rows}
        elif t == "مدرس":
            rows = self.db.query("SELECT id,employee_no,full_name FROM teachers ORDER BY full_name")
            self.owner_map = {f"{r['full_name']} — {r['employee_no']}": r["id"] for r in rows}
        self.owner_combo = getattr(self, "owner_combo", None)
        if self.owner_combo:
            self.owner_combo["values"] = list(self.owner_map.keys())
        if t == "المدرسة":
            self.owner_var.set("المدرسة")
        elif self.owner_var.get() not in self.owner_map:
            self.owner_var.set(next(iter(self.owner_map), ""))

    def browse_file(self):
        path = filedialog.askopenfilename(parent=self.frame, title="اختيار ملف الوثيقة", filetypes=[("كل الملفات", "*.*"), ("PDF", "*.pdf"), ("صور", "*.png *.jpg *.jpeg"), ("Word", "*.doc *.docx")])
        if path:
            self.file_var.set(path)

    def _owner_db(self):
        t = self.owner_type.get()
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
            vals = (owner_type, owner_id, title, self.category_var.get(), self.date_var.get().strip(), stored, self.notes_var.get().strip())
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
        rows = self.db.query("""SELECT d.id,d.title,d.owner_type,d.owner_id,d.category,d.document_date,d.file_path,d.notes,
            CASE d.owner_type WHEN 'school' THEN COALESCE((SELECT name FROM school_info WHERE id=1),'المدرسة')
              WHEN 'student' THEN COALESCE((SELECT full_name FROM students WHERE id=d.owner_id),'طالب محذوف')
              WHEN 'teacher' THEN COALESCE((SELECT full_name FROM teachers WHERE id=d.owner_id),'مدرس محذوف') ELSE '' END AS owner_name
            FROM documents d ORDER BY d.id DESC""")
        for r in rows:
            owner_label = {"school": "المدرسة", "student": "طالب", "teacher": "مدرس"}.get(r["owner_type"], r["owner_type"])
            fname = Path(r["file_path"]).name if r["file_path"] else ""
            self.tree.insert("", "end", iid=str(r["id"]), values=(r["title"], owner_label, r["owner_name"], r["category"], r["document_date"], fname, r["notes"] or ""))

    def select_row(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        self.selected_id = safe_int(sel[0])
        r = self.db.one("SELECT * FROM documents WHERE id=?", (self.selected_id,))
        if not r:
            return
        self.owner_type.set({"school": "المدرسة", "student": "طالب", "teacher": "مدرس"}.get(r["owner_type"], "المدرسة"))
        self.refresh_owners()
        if r["owner_type"] == "student":
            owner = self.db.one("SELECT id,student_no,full_name FROM students WHERE id=?", (r["owner_id"],))
            if owner:
                self.owner_var.set(f"{owner['full_name']} — {owner['student_no']}")
        elif r["owner_type"] == "teacher":
            owner = self.db.one("SELECT id,employee_no,full_name FROM teachers WHERE id=?", (r["owner_id"],))
            if owner:
                self.owner_var.set(f"{owner['full_name']} — {owner['employee_no']}")
        self.title_var.set(r["title"] or "")
        self.category_var.set(r["category"] or "أخرى")
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
        self.category_var.set("أخرى")
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
        self.kind_var = tk.StringVar(value=self.REPORTS[0])
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
        ttk.Combobox(bar, textvariable=self.kind_var, values=self.REPORTS, state="readonly", width=25).pack(side="right", padx=5)
        ttk.Label(bar, text="التاريخ:").pack(side="right", padx=5)
        ttk.Entry(bar, textvariable=self.date_var, width=14).pack(side="right", padx=5)
        ttk.Label(bar, text="الصف (لجدول الحصص):").pack(side="right", padx=5)
        self.class_combo = ttk.Combobox(bar, textvariable=self.class_var, state="readonly", width=26)
        self.class_combo.pack(side="right", padx=5)
        ttk.Button(bar, text="إنشاء التقرير", command=self.generate).pack(side="right", padx=5)
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

    def refresh_classes(self):
        rows = self.db.query("SELECT id,grade,section,academic_year FROM classes ORDER BY grade,section")
        self.class_map = {f"{r['grade']} - {r['section']}" + (f" ({r['academic_year']})" if r['academic_year'] else ""): r["id"] for r in rows}
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
        kind = self.kind_var.get()
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
        ttk.Label(box, text="مدرستي Pro", font=("Segoe UI", 21, "bold"), foreground="#12365a").pack(pady=(0, 4))
        headline = "إنشاء حساب المدير لأول تشغيل" if self.first_run else "سجّل الدخول إلى نظام المدرسة"
        ttk.Label(box, text=headline, font=("Segoe UI", 11, "bold")).pack(pady=(0, 5))
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
        self.theme_mode = self._load_theme_mode()
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
        self.header = ttk.Frame(root, padding=(18, 12))
        self.header.pack(fill="x")
        brand = ttk.Frame(self.header)
        brand.pack(side="right", anchor="e")
        brand_title = ttk.Frame(brand)
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
        self.title_label = ttk.Label(brand_title, text="مدرستي Pro", style="Title.TLabel")
        self.title_label.pack(side="right")
        ttk.Label(brand, text="نظام إدارة المدارس العراقية المتكامل", style="Subtitle.TLabel").pack(anchor="e")
        utilities = ttk.Frame(self.header)
        utilities.pack(side="left", anchor="w")
        ttk.Label(utilities, text=f"الإصدار {APP_VERSION}  •  المستخدم: {self.current_user['full_name']}", style="Subtitle.TLabel").pack(side="top", anchor="w", pady=(0, 5))
        buttons = ttk.Frame(utilities)
        buttons.pack(side="left", anchor="w")
        self.theme_button = ttk.Button(buttons, text=("☀️ الوضع الفاتح" if self.theme_mode == "dark" else "🌙 الوضع الداكن"), command=self.toggle_theme)
        self.theme_button.pack(side="left", padx=2)
        self.update_button = ttk.Button(buttons, text="🔄 تحديث البرنامج", command=self.check_for_updates)
        self.update_button.pack(side="left", padx=2)
        ttk.Button(buttons, text="🔐 تغيير كلمة المرور", command=self.change_password).pack(side="left", padx=2)
        if self.current_user.get("is_admin"):
            ttk.Button(buttons, text="إدارة الحسابات", command=self.manage_users).pack(side="left", padx=2)
        ttk.Button(buttons, text="مجلد البيانات", command=self.open_data_folder).pack(side="left", padx=2)
        ttk.Button(buttons, text="حول البرنامج", command=self.show_about).pack(side="left", padx=2)
        ttk.Separator(root).pack(fill="x")
        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True, padx=10, pady=10)
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

        self.status_var = tk.StringVar(value="جاهز للعمل — تُحفظ البيانات محلياً على هذا الجهاز")
        status = ttk.Label(root, textvariable=self.status_var, anchor="e", padding=(12, 6), style="Status.TLabel")
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
        if dark:
            self.colors = {
                "bg": "#111827", "panel": "#1f2937", "field": "#111827",
                "text": "#f3f4f6", "muted": "#cbd5e1", "accent": "#60a5fa",
                "accent_dark": "#1e3a5f", "border": "#374151",
                "button": "#273449", "selected": "#24476b", "heading": "#273449",
            }
        else:
            self.colors = {
                "bg": "#edf3f9", "panel": "#ffffff", "field": "#ffffff",
                "text": "#203044", "muted": "#526a82", "accent": "#1462a0",
                "accent_dark": "#12365a", "border": "#cbd8e6",
                "button": "#e1ebf5", "selected": "#c4def8", "heading": "#d5e4f2",
            }
        c = self.colors
        self.root.configure(bg=c["bg"])
        style.configure(".", font=("Segoe UI", 11))
        style.configure("TFrame", background=c["bg"])
        style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"], relief="solid")
        style.configure("TLabelframe.Label", background=c["bg"], foreground=c["accent_dark"], font=("Segoe UI", 11, "bold"))
        style.configure("TLabel", background=c["bg"], foreground=c["text"], font=("Segoe UI", 12))
        style.configure("Metric.TLabel", font=("Segoe UI", 24, "bold"), foreground=c["accent"], background=c["panel"])
        style.configure("DashboardIntro.TLabel", font=("Segoe UI", 24, "bold"), foreground=c["accent_dark"] if not dark else "#93c5fd", background=c["bg"])
        style.configure("Title.TLabel", font=("Segoe UI", 26, "bold"), foreground=c["accent_dark"] if not dark else "#93c5fd", background=c["bg"])
        style.configure("Subtitle.TLabel", font=("Segoe UI", 11, "bold"), foreground=c["muted"], background=c["bg"])
        style.configure("Hint.TLabel", font=("Segoe UI", 10), foreground=c["muted"], background=c["bg"])
        style.configure("Status.TLabel", font=("Segoe UI", 10, "bold"), foreground="#f3f8ff", background=c["accent_dark"])
        style.configure("TButton", font=("Segoe UI", 12, "bold"), padding=(12, 9), background=c["button"], foreground=c["text"])
        style.map("TButton", background=[("active", c["selected"]), ("pressed", c["selected"])], foreground=[("disabled", c["muted"])])
        style.configure("TEntry", font=("Segoe UI", 12), padding=8, fieldbackground=c["field"], foreground=c["text"], insertcolor=c["text"])
        style.configure("TCombobox", font=("Segoe UI", 12), padding=8, fieldbackground=c["field"], foreground=c["text"], arrowcolor=c["text"])
        style.map("TCombobox", fieldbackground=[("readonly", c["field"])], foreground=[("readonly", c["text"])])
        style.configure("Treeview", font=("Segoe UI", 12), rowheight=40, background=c["panel"], fieldbackground=c["panel"], foreground=c["text"])
        style.configure("Treeview.Heading", font=("Segoe UI", 12, "bold"), background=c["heading"], foreground=c["text"], padding=10)
        style.map("Treeview", background=[("selected", c["selected"])], foreground=[("selected", c["text"])])
        style.configure("TNotebook", background=c["bg"], tabmargins=(4, 5, 4, 0))
        style.configure("TNotebook.Tab", font=("Segoe UI", 11, "bold"), padding=(14, 11), background=c["button"], foreground=c["text"])
        style.map("TNotebook.Tab", background=[("selected", c["panel"]), ("active", c["selected"])], foreground=[("selected", c["accent_dark"] if not dark else "#93c5fd")])

    def toggle_theme(self):
        self.theme_mode = "dark" if self.theme_mode == "light" else "light"
        self._save_theme_mode()
        self._style()
        if hasattr(self, "theme_button"):
            self.theme_button.configure(text=("☀️ الوضع الفاتح" if self.theme_mode == "dark" else "🌙 الوضع الداكن"))
        self.status_var.set("تم تغيير مظهر البرنامج")

    def _build_dashboard(self):
        intro = ttk.Label(self.dashboard_frame, text="🎓  مرحباً بك في مدرستي Pro", style="DashboardIntro.TLabel")
        intro.pack(anchor="e", pady=(4, 6))
        ttk.Label(self.dashboard_frame, text="نظّم بيانات المدرسة من مكان واحد، وسجّل الحضور والغياب، وأدرج الجداول والوثائق، ثم صدّر التقارير عند الحاجة.", wraplength=1200).pack(anchor="e", pady=(0, 18))
        self.cards_frame = ttk.Frame(self.dashboard_frame)
        self.cards_frame.pack(fill="x", pady=(0, 18))
        self.metric_vars = {}
        metrics = [("students", "الطلاب"), ("teachers", "المدرسون"), ("classes", "الصفوف والشعب"), ("absent", "غياب اليوم"), ("docs", "الوثائق المؤرشفة")]
        for i, (key, label) in enumerate(metrics):
            card = ttk.LabelFrame(self.cards_frame, text=label, padding=16)
            card.grid(row=0, column=i, sticky="nsew", padx=5)
            var = tk.StringVar(value="0")
            self.metric_vars[key] = var
            ttk.Label(card, textvariable=var, style="Metric.TLabel").pack()
            self.cards_frame.columnconfigure(i, weight=1)
        actions = ttk.LabelFrame(self.dashboard_frame, text="اختصارات سريعة", padding=14)
        actions.pack(fill="x", pady=(0, 18))
        for label, tab_index in [("🎓 إدارة الطلاب", 4), ("📋 تسجيل الحضور", 5), ("📝 الامتحانات والدرجات", 6), ("🗓️ جدول الحصص", 7), ("💳 الرسوم والأقساط", 8), ("🗂️ أرشفة وثيقة", 9), ("📊 التقارير", 10), ("🏫 بيانات المدرسة والدوام", 11)]:
            ttk.Button(actions, text=label, command=lambda i=tab_index: self.notebook.select(i)).pack(side="right", padx=4, pady=4)
        helpbox = ttk.LabelFrame(self.dashboard_frame, text="ملاحظات البدء", padding=12)
        helpbox.pack(fill="both", expand=True)
        tips = [
            "1. أدخل بيانات المدرسة والسنة الدراسية من تبويب (بيانات المدرسة والدوام).",
            "2. أنشئ الصفوف والشعب، ثم سجّل أسماء المدرسين والمواد الدراسية.",
            "3. أضف الطلاب يدوياً أو استورد قائمة كبيرة من ملف CSV باستخدام قالب الطلاب.",
            "4. من تبويب الحضور والغياب اختر الصف والتاريخ ثم سجّل الحالات واحفظها.",
            "5. سجّل نتائج الامتحانات من تبويب الامتحانات والدرجات، وأدخل الرسوم والأقساط من تبويب الرسوم والأقساط.",
            "6. أرفق ملفات PDF أو صور الوثائق في تبويب الوثائق والأرشيف، وأنشئ نسخة احتياطية كاملة بصيغة ZIP دورياً.",
            "7. تُحفظ البيانات في ملف المستخدم على هذا الجهاز؛ استخدم النسخ الاحتياطي الكامل لنقل المدرسة إلى حاسبة أخرى.",
        ]
        for tip in tips:
            ttk.Label(helpbox, text=tip, anchor="e", justify="right", wraplength=1200).pack(fill="x", anchor="e", pady=4)

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
            "fields": [self._field("student_no", "رقم الطالب", True), self._field("full_name", "اسم الطالب الكامل", True), self._field("gender", "الجنس", kind="combo", options=lambda: [("", None), ("ذكر", "ذكر"), ("أنثى", "أنثى")]), self._field("birth_date", "تاريخ الميلاد YYYY-MM-DD"), self._field("class_id", "الصف والشعبة", kind="combo", options=self._class_options), self._field("guardian_name", "اسم ولي الأمر"), self._field("guardian_phone", "هاتف ولي الأمر"), self._field("address", "العنوان"), self._field("enrollment_date", "تاريخ التسجيل"), self._field("status", "الحالة", kind="combo", options=lambda: [(x, x) for x in PERSON_STATUSES]), self._field("notes", "ملاحظات")],
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

    def _load_theme_mode(self):
        """Remember the selected theme for this Windows user."""
        path = DATA_DIR / "ui_preferences.json"
        try:
            mode = json.loads(path.read_text(encoding="utf-8")).get("theme_mode", "light")
            return mode if mode in ("light", "dark") else "light"
        except (OSError, ValueError, AttributeError):
            return "light"

    def _save_theme_mode(self):
        try:
            (DATA_DIR / "ui_preferences.json").write_text(
                json.dumps({"theme_mode": self.theme_mode}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

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
