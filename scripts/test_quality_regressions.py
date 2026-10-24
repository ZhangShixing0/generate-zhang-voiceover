import unittest
from generate_voiceover import analyze_pacing


class PacingRegressionTests(unittest.TestCase):
    def test_short_phrase_acceleration_is_not_hidden_by_unit_average(self):
        # Both units average four chars/s, but the second phrase accelerates 20%.
        records = [{'char_count': 20, 'duration_seconds': 5, 'chars_per_second': 4,
                    'phrase_rates': [4.0, 4.8]}]
        self.assertEqual(analyze_pacing(records, {'generation': {}})['status'], 'needs-review')

    def test_slow_short_phrase_is_also_rejected(self):
        records = [{'char_count': 20, 'duration_seconds': 5, 'chars_per_second': 4,
                    'phrase_rates': [4.0, 3.5]}]
        self.assertEqual(analyze_pacing(records, {'generation': {}})['status'], 'needs-review')

    def test_missing_alignment_cannot_be_reported_as_a_pass(self):
        records = [{'char_count': 20, 'duration_seconds': 5, 'chars_per_second': 4}]
        self.assertEqual(analyze_pacing(records, {'generation': {}})['status'], 'needs-review')


if __name__ == '__main__':
    unittest.main()
