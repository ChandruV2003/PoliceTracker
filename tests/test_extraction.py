import os
import tempfile
import unittest
from pathlib import Path


# Isolate DB + avoid network before importing web_server
_tmp_dir = tempfile.TemporaryDirectory()
_db_path = Path(_tmp_dir.name) / "policetracker_extraction_test.db"
os.environ["DATABASE_PATH"] = str(_db_path)
os.environ["ENABLE_GEOCODING"] = "0"
os.environ["DASHBOARD_PIN"] = ""
os.environ["DASHBOARD_USER"] = ""
os.environ["DASHBOARD_PASS"] = ""

import importlib
import web_server  # noqa: E402

importlib.reload(web_server)


class TestExtraction(unittest.TestCase):
    def test_extract_unit_word_digits(self):
        units = web_server.extract_unit_mentions("Unit five, zero, three responding")
        self.assertIn("unit 503", units)

    def test_extract_unit_hyphen_code(self):
        units = web_server.extract_unit_mentions("LF-109 Fire 1 responding")
        self.assertIn("LF-109", units)

    def test_extract_location_hint_address(self):
        hint = web_server.extract_location_hint(
            "In front of 606 Foxrun Road",
            context="Gloucester County, NJ",
        )
        self.assertIsNotNone(hint)
        self.assertIn("Foxrun Road", hint)
        self.assertIn("NJ", hint)

    def test_extract_location_hint_rejects_generic_off_the_road(self):
        hint = web_server.extract_location_hint("right off the road", context="South NJ")
        self.assertIsNone(hint)


if __name__ == "__main__":
    unittest.main()

