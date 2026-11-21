import unittest
from collections import defaultdict

from uci_to_mts_format import incident_to_mts_json


class CardPriorityTest(unittest.TestCase):
    def test_uci_priority_changes_only_card_priority(self):
        row = defaultdict(lambda: None, impact_n=1, urgency_n=3, sla_hours=24)
        other_fields = None
        for raw, expected_id, expected_label in (
            (1, 4, "Наивысший"), (2, 3, "Высокий"),
            (3, 2, "Средний"), (4, 1, "Низкий"),
        ):
            row["priority_n"] = raw
            card = incident_to_mts_json(row)
            self.assertEqual((card.pop("priorityId"), card.pop("priority")), (expected_id, expected_label))
            if other_fields is not None:
                self.assertEqual(card, other_fields)
            other_fields = card
