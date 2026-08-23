import math
import unittest

from market_radar.canonical import canonical_json_bytes, sha256_hex


class CanonicalJsonTests(unittest.TestCase):
    def test_encoding_is_stable_and_sorted(self):
        first = canonical_json_bytes({"z": 1, "a": ["ç", True]})
        second = canonical_json_bytes({"a": ["ç", True], "z": 1})

        self.assertEqual(first, second)
        self.assertEqual(first, b'{"a":["\xc3\xa7",true],"z":1}\n')
        self.assertEqual(len(sha256_hex(first)), 64)

    def test_non_finite_numbers_fail_closed(self):
        with self.assertRaises(ValueError):
            canonical_json_bytes({"value": math.nan})
