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


    def test_editable_student_teacher_and_timetable_records(self):
        class_id = self.db.execute(
            "INSERT INTO classes(grade,section,academic_year) VALUES(?,?,?)",
            ("الثاني المتوسط", "ب", "2026-2027"),
        )
        teacher_id = self.db.execute(
            "INSERT INTO teachers(employee_no,full_name,specialization) VALUES(?,?,?)",
            ("T-020", "مدرس تجريبي", "علوم"),
        )
        subject_id = self.db.execute(
            "INSERT INTO subjects(name,grade,teacher_id) VALUES(?,?,?)",
            ("العلوم", "الثاني المتوسط", teacher_id),
        )
        student_id = self.db.execute(
            "INSERT INTO students(student_no,full_name,class_id) VALUES(?,?,?)",
            ("S-020", "اسم أول", class_id),
        )
        timetable_id = self.db.execute(
            "INSERT INTO timetable(class_id,day,period_no,start_time,end_time,subject_id,teacher_id,room) VALUES(?,?,?,?,?,?,?,?)",
            (class_id, "الأحد", 1, "08:00", "08:40", subject_id, teacher_id, "1"),
        )
        # The editable forms update the selected records rather than creating duplicates.
        self.db.execute("UPDATE students SET full_name=? WHERE id=?", ("اسم معدل", student_id))
        self.db.execute("UPDATE teachers SET specialization=? WHERE id=?", ("فيزياء", teacher_id))
        self.db.execute("UPDATE timetable SET room=? WHERE id=?", ("مختبر 2", timetable_id))
        self.assertEqual(self.db.one("SELECT full_name FROM students WHERE id=?", (student_id,))["full_name"], "اسم معدل")
        self.assertEqual(self.db.one("SELECT specialization FROM teachers WHERE id=?", (teacher_id,))["specialization"], "فيزياء")
        self.assertEqual(self.db.one("SELECT room FROM timetable WHERE id=?", (timetable_id,))["room"], "مختبر 2")

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
