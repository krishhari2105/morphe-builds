import unittest

from scripts.prune_actions_caches import caches_to_delete


class CacheRetentionTests(unittest.TestCase):
    def test_keeps_two_newest_matching_generations(self):
        caches = [
            {"id": 1, "key": "morphe-bases-Linux-morphe--1", "created_at": "2026-08-01"},
            {"id": 2, "key": "morphe-bases-Linux-morphe--2", "created_at": "2026-08-02"},
            {"id": 3, "key": "morphe-bases-Linux-morphe--3", "created_at": "2026-08-03"},
        ]
        deleted = caches_to_delete(caches, "morphe-bases-Linux-morphe--", 2)
        self.assertEqual([item["id"] for item in deleted], [1])

    def test_does_not_match_another_source_or_cache_type(self):
        caches = [
            {"id": 1, "key": "morphe-bases-Linux-morphe--1", "created_at": "2026-08-01"},
            {"id": 2, "key": "morphe-bases-Linux-morphe-dev--2", "created_at": "2026-08-02"},
            {"id": 3, "key": "morphe-tools-Linux-morphe--3", "created_at": "2026-08-03"},
        ]
        deleted = caches_to_delete(caches, "morphe-bases-Linux-morphe--", 0)
        self.assertEqual([item["id"] for item in deleted], [1])


if __name__ == "__main__":
    unittest.main()
