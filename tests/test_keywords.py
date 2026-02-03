import unittest


from police_tracker import detect_keywords


class TestDetectKeywords(unittest.TestCase):
    def test_exact_match_phrase(self):
        transcript = "Caller reports shots fired near the mall."
        keywords = ["shots fired", "officer down"]
        detected = detect_keywords(transcript, keywords, threshold=0.8)
        self.assertIn("shots fired", detected)
        self.assertNotIn("officer down", detected)

    def test_case_insensitive(self):
        transcript = "ACCIDENT on route 22"
        keywords = ["accident"]
        detected = detect_keywords(transcript, keywords, threshold=0.8)
        self.assertIn("accident", detected)

    def test_fuzzy_single_word(self):
        transcript = "Report of accdient on the highway"
        keywords = ["accident"]
        detected = detect_keywords(transcript, keywords, threshold=0.8)
        self.assertIn("accident", detected)

    def test_fuzzy_phrase(self):
        transcript = "Possible officer dwn at scene"
        keywords = ["officer down"]
        detected = detect_keywords(transcript, keywords, threshold=0.8)
        self.assertIn("officer down", detected)

    def test_no_match(self):
        transcript = "Routine traffic stop no incident"
        keywords = ["shots fired"]
        detected = detect_keywords(transcript, keywords, threshold=0.8)
        self.assertEqual(detected, [])


if __name__ == "__main__":
    unittest.main()

