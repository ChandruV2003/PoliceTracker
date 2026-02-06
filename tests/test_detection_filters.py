import unittest


from police_tracker import apply_detection_filters


class TestDetectionFilters(unittest.TestCase):
    def test_ambiguous_single_word_gated_without_dispatch_context(self):
        transcript = "So we're shooting the show with our main camera."
        detected = ["shooting"]
        filtered = apply_detection_filters(transcript, detected, cfg={})
        self.assertEqual(filtered, [])

    def test_phrase_keywords_not_gated_without_dispatch_context(self):
        transcript = "Caller: shots fired at the mall."
        detected = ["shots fired"]
        filtered = apply_detection_filters(transcript, detected, cfg={})
        self.assertEqual(filtered, ["shots fired"])

    def test_obvious_ads_suppressed(self):
        transcript = "Download the app and use promo code SAVE20 for a bonus."
        detected = ["robbery"]
        filtered = apply_detection_filters(transcript, detected, cfg={})
        self.assertEqual(filtered, [])


if __name__ == "__main__":
    unittest.main()

