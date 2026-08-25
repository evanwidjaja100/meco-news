from __future__ import annotations

from datetime import datetime, timedelta, UTC
import unittest

from meco_news.config import load_config
from meco_news.models import NewsItem
from meco_news.ranking import deduplicate, rank_item, select_digest


class RankingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = load_config("config/watchlist.json")
        cls.now = datetime(2026, 8, 24, 3, 0, tzinfo=UTC)

    def item(self, title: str, source: str = "Petromindo", hours_old: int = 1) -> NewsItem:
        return NewsItem(
            title=title,
            url="https://example.com/" + str(abs(hash(title))),
            source=source,
            source_url="https://www.petromindo.com",
            published_at=self.now - timedelta(hours=hours_old),
        )

    def test_energy_project_scores_as_relevant(self) -> None:
        item = self.item("Gas infrastructure construction starts for Tanjung Enim project")
        ranked = rank_item(item, self.config, self.now)
        self.assertEqual(ranked.topic, "energy_projects")
        self.assertGreaterEqual(ranked.score, self.config["minimum_score"])

    def test_consumer_material_story_is_penalized(self) -> None:
        item = self.item("Peralatan masak stainless steel dan aluminium terbaru")
        ranked = rank_item(item, self.config, self.now)
        self.assertLess(ranked.score, self.config["fallback_score"])

    def test_customer_lane_requires_industrial_context(self) -> None:
        item = self.item("Transaksi non-tunai bisnis food and beverage tumbuh")
        ranked = rank_item(item, self.config, self.now)
        self.assertNotEqual(ranked.topic, "customer_industries")

    def test_near_duplicate_headlines_are_clustered(self) -> None:
        items = [
            self.item("Pertamina kerahkan 33 mobil tangki BBM ke Flores"),
            self.item("Pertamina kerahkan 38 mobil tangki BBM menuju NTT", source="Kompas"),
            self.item("Gas infrastructure construction starts for new project"),
        ]
        self.assertEqual(len(deduplicate(items)), 2)

    def test_selection_excludes_sent_items(self) -> None:
        items = [
            rank_item(self.item("Gas infrastructure construction starts for project A"), self.config, self.now),
            rank_item(self.item("TGI plans new gas delivery point in Riau"), self.config, self.now),
            rank_item(self.item("Pertamina builds bio-methanol value chain"), self.config, self.now),
        ]
        selected = select_digest(items, self.config, {items[0].fingerprint})
        self.assertNotIn(items[0], selected)


if __name__ == "__main__":
    unittest.main()
