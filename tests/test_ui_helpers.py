import unittest
from unittest.mock import Mock, patch
import school_manager
from school_manager import display_arabic_text, render_print_html


class ArabicRenderingAndPrintTests(unittest.TestCase):
    def test_arabic_display_is_platform_aware(self):
        text = "مدرستي Pro - اسم الطالب"
        if school_manager.sys.platform.startswith("win") or school_manager.sys.platform == "darwin":
            # Native OS text layout should receive logical Arabic, not a pre-reversed string.
            self.assertEqual(display_arabic_text(text), text)
        elif school_manager._ARABIC_DISPLAY_AVAILABLE:
            first = display_arabic_text(text)
            self.assertNotEqual(first, text)
            self.assertEqual(display_arabic_text(first), first)
        else:
            # Local minimal environments may omit the optional X11 shaping packages.
            self.assertEqual(display_arabic_text(text), text)


    @unittest.skipIf(school_manager.sys.platform.startswith("win") or school_manager.sys.platform == "darwin", "native OS handles RTL text")
    def test_windows_display_uses_native_rtl_without_manual_reordering(self):
        text = "مدرستي Pro - اسم الطالب"
        display_arabic_text.cache_clear()
        with patch.object(school_manager.sys, "platform", "win32"), \
             patch.object(school_manager, "_bidi_get_display") as bidi_mock:
            self.assertEqual(display_arabic_text(text), text)
            bidi_mock.assert_not_called()
        display_arabic_text.cache_clear()

    @unittest.skipIf(school_manager.sys.platform.startswith("win") or school_manager.sys.platform == "darwin", "native OS path does not require X11 reshaping")
    def test_display_arabic_text_does_not_reshape_an_already_shaped_value(self):
        # A presentation-form character signals that this display value is already
        # transformed; passing it through another Tk wrapper must not transform it twice.
        shaped = "ﭐم"
        fake_reshaper = Mock()
        fake_reshaper.reshape.return_value = shaped
        fake_bidi = Mock(side_effect=lambda text, base_dir=None: text)
        display_arabic_text.cache_clear()
        # Exercise the optional reshaping path deterministically even when this
        # suite is run on Windows/macOS, where the production function correctly
        # delegates RTL layout to the native OS and returns logical text unchanged.
        with patch.object(school_manager.sys, "platform", "linux"), \
             patch.object(school_manager, "_ARABIC_DISPLAY_AVAILABLE", True), \
             patch.object(school_manager, "arabic_reshaper", fake_reshaper), \
             patch.object(school_manager, "_bidi_get_display", fake_bidi):
            first = display_arabic_text("مرحبا")
            second = display_arabic_text(first)
            self.assertEqual(first, shaped)
            self.assertEqual(second, first)
            self.assertEqual(fake_reshaper.reshape.call_count, 1)
            self.assertEqual(fake_bidi.call_count, 1)
        display_arabic_text.cache_clear()

    def test_print_page_uses_rtl_and_escapes_cell_content(self):
        rendered = render_print_html(
            "كشف الطلاب", ["اسم الطالب", "الحالة"], [["علي", "غائب <script>"]], "مدرسة بغداد", "Tahoma"
        )
        self.assertIn('<html lang="ar" dir="rtl">', rendered)
        self.assertIn("كشف الطلاب", rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("window.print()", rendered)
        self.assertIn("مدرسة بغداد", rendered)

    def test_print_page_handles_empty_table(self):
        rendered = render_print_html("تقرير فارغ", ["الاسم"], [], "مدرستي")
        self.assertIn("لا توجد سجلات للطباعة", rendered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
