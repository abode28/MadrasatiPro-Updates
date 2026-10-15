"""Non-GUI smoke tests for the school data model."""
from pathlib import Path
import sqlite3
import tempfile
import unittest

from school_manager import SchoolDB, hash_password, verify_password, SchoolApp


class SchoolDatabaseSmokeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="schoolmanager_test_")
        self.db_path = Path(self.temp.name) / "school.db"
        self.db = SchoolDB(self.db_path)

    def tearDown(self):
        self.db.close()
        self.temp.cleanup()

    def test_core_school_workflow(self):
        class_id = self.db.execute(
            "INSERT INTO classes(grade,section,academic_year) VALUES(?,?,?)",
            ("الأول المتوسط", "أ", "2026-2027"),
        )
        teacher_id = self.db.execute(
            "INSERT INTO teachers(employee_no,full_name,specialization) VALUES(?,?,?)",
            ("T-001", "مدرس تجريبي", "رياضيات"),
        )
        subject_id = self.db.execute(
            "INSERT INTO subjects(name,grade,teacher_id) VALUES(?,?,?)",
            ("الرياضيات", "الأول المتوسط", teacher_id),
        )
        student_id = self.db.execute(
            "INSERT INTO students(student_no,full_name,class_id) VALUES(?,?,?)",
            ("S-001", "طالب تجريبي", class_id),
        )
        self.db.execute(
            "INSERT INTO attendance(student_id,attendance_date,status) VALUES(?,?,?)",
            (student_id, "2026-10-15", "حاضر"),
        )
        self.db.execute(
            "INSERT INTO assessments(student_id,subject_id,exam_name,exam_date,score,max_score) VALUES(?,?,?,?,?,?)",
            (student_id, subject_id, "امتحان شهري", "2026-10-15", 85, 100),
        )
        self.db.execute(
            "INSERT INTO fee_records(student_id,academic_year,fee_type,amount,paid_amount) VALUES(?,?,?,?,?)",
            (student_id, "2026-2027", "أجور دراسية", 100000, 25000),
        )
        row = self.db.one("SELECT full_name FROM students WHERE id=?", (student_id,))
        self.assertEqual(row["full_name"], "طالب تجريبي")
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM attendance")["n"], 1)
        self.assertEqual(self.db.one("SELECT COUNT(*) n FROM assessments")["n"], 1)
        self.assertEqual(self.db.one("SELECT amount-paid_amount remaining FROM fee_records")["remaining"], 75000)


    def test_manager_queries_and_password_hashing(self):
        app = object.__new__(SchoolApp)
        app.db = self.db
        configs = [
            app._class_config(), app._teacher_config(), app._subject_config(),
            app._student_config(), app._timetable_config(),
            app._assessment_config(), app._fees_config(),
        ]
        for cfg in configs:
            query = cfg["list_query"] + " ORDER BY " + cfg.get("order_by", "id DESC")
            # All list queries must be valid before the UI is opened, including empty tables.
            self.db.query(query)
        salt, digest = hash_password("Strong-Test-Pass_2026")
        self.assertTrue(verify_password("Strong-Test-Pass_2026", salt, digest))
        self.assertFalse(verify_password("wrong-password", salt, digest))

    def test_database_backup_is_valid(self):
        backup_path = Path(self.temp.name) / "backup.db"
        self.db.backup(backup_path)
        connection = sqlite3.connect(backup_path)
        try:
            self.assertEqual(connection.execute("PRAGMA integrity_check").fetchone()[0], "ok")
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertIn("students", tables)
            self.assertIn("assessments", tables)
            self.assertIn("fee_records", tables)
        finally:
            connection.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
