"""C4.5 determinism proof: merge output is input-order and hash-seed independent."""

from __future__ import annotations

import copy
import itertools
import os
import subprocess
import sys
import unittest
from pathlib import Path

from meco_news.models import NewsItem
from meco_news.ranking import deduplicate_with_stats


def _item(title: str, url: str, source: str, summary: str) -> NewsItem:
    return NewsItem(
        title=title,
        url=url,
        source=source,
        source_url="",
        published_at=None,
        summary=summary,
        collector="probe",
        query_name="probe",
        source_id="probe",
        source_host="",
    )


def _fixture() -> list[NewsItem]:
    direct = _item(
        "Pertamina kerahkan 33 mobil tangki BBM ke Flores",
        "https://direct.example/flores-bbm",
        "DirectPublisher",
        "Direct publisher story with the longest summary body in this fixture.",
    )
    aggregator = _item(
        "Pertamina kerahkan 33 mobil tangki BBM ke Flores",
        "https://aggregator.example/flores-bbm",
        "NewsAggregator",
        "Short.",
    )
    fuzzy = _item(
        "Pertamina kerahkan 33 mobil tangki BBM menuju Flores",
        "https://other.example/flores-bbm-terkini",
        "OtherPublisher",
        "Reworded near duplicate sharing the core phrase.",
    )
    unrelated = _item(
        "PLN operasikan gardu induk baru di Kalimantan",
        "https://direct.example/gardu-kalimantan",
        "DirectPublisher",
        "Unrelated grid story that must always survive.",
    )
    return [direct, aggregator, fuzzy, unrelated]


class PermutationTests(unittest.TestCase):
    def test_all_24_permutations_agree(self) -> None:
        base = _fixture()
        reference_fps: tuple[str, ...] | None = None
        checked = 0
        for order in itertools.permutations(range(4)):
            items = [copy.deepcopy(base[i]) for i in order]
            before = [repr(item) for item in items]
            result, stats = deduplicate_with_stats(items, None)
            self.assertEqual([repr(item) for item in items], before)
            for produced in result:
                for owned in items:
                    self.assertIsNot(produced, owned)
            fingerprints = tuple(item.fingerprint for item in result)
            if reference_fps is None:
                reference_fps = fingerprints
                reference_stats = stats
                self.assertGreater(stats.fuzzy_comparisons, 0)
                self.assertEqual(stats.input_count, 4)
            else:
                self.assertEqual(fingerprints, reference_fps)
                self.assertEqual(stats, reference_stats)
            checked += 1
        self.assertEqual(checked, 24)
        self.assertIsNotNone(reference_fps)


class HashSeedTests(unittest.TestCase):
    def test_permutation_proof_passes_under_distinct_hash_seeds(self) -> None:
        root = Path(__file__).resolve().parents[1]
        for seed in ("0", "42"):
            env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": str(root)}
            completed = subprocess.run(
                [sys.executable, "-B", "-m", "unittest", "tests.test_c45_determinism.PermutationTests"],
                cwd=str(root),
                env=env,
                capture_output=True,
                text=True,
                timeout=120,
            )
            self.assertEqual(completed.returncode, 0, msg=completed.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
