import unittest

from advisor import CANDIDATE_LIMIT, DEFAULT_THRESHOLD, baseline, summarize

QUERY = {
    "id": "new", "service": "Wi-Fi", "site": "Ростов",
    "title": "Не пускает в Wi-Fi", "description": "Сбой авторизации",
    "infrastructure": ["WLAN"], "current_group": "Первая линия",
}


def candidate(number=0, **changes):
    return dict(QUERY, incident_id=f"INC-{number:02}", site="Таганрог",
                outcome="Восстановили авторизацию", resolved_by="Сети Таганрога",
                first_assigned_group="Диспетчеры", similarity=0.92, **changes)


class AdvisorPolicyTest(unittest.TestCase):
    def test_cross_site_report_uses_current_directory(self):
        result, _ = summarize(QUERY, [candidate()], ["Сети Ростова"], DEFAULT_THRESHOLD)
        report = result["analogues"][0]
        self.assertEqual(report["resolved_by"], "Сети Таганрога")
        self.assertIn("Площадка аналога: Таганрог", report["differences"])
        self.assertNotIn("Диспетчеры", repr(result))
        self.assertEqual(result["recommendation"]["group"], "Сети Ростова")

    def test_conflict_missing_or_same_owner_preserves_history_only(self):
        for owners in ([], ["A", "B"], ["Первая линия"]):
            with self.subTest(owners=owners):
                result, _ = summarize(QUERY, [candidate()], owners, DEFAULT_THRESHOLD)
                self.assertEqual(len(result["analogues"]), 1)
                self.assertIsNone(result["recommendation"])

    def test_similar_unknown_outcome_and_low_similarity_are_excluded(self):
        unknown = dict(candidate(1), outcome=None)
        distant = dict(candidate(2), similarity=0.2)
        result, audit = summarize(QUERY, [unknown, distant], ["Владелец"], DEFAULT_THRESHOLD)
        self.assertEqual(result["analogues"], [])
        self.assertIsNone(result["recommendation"])
        self.assertEqual([item["decision"] for item in audit], ["unknown_outcome", "low_similarity"])

    def test_candidate_and_response_caps(self):
        result, audit = summarize(QUERY, [candidate(i) for i in range(12)], ["A"], DEFAULT_THRESHOLD)
        self.assertEqual(len(audit), CANDIDATE_LIMIT)
        self.assertEqual(len(result["analogues"]), 3)
        self.assertEqual(len(result["recommendation"]["incident_ids"]), 3)

    def test_baseline_filters_self_active_and_other_service(self):
        history = [
            dict(QUERY, status="Resolved", outcome="known"),
            dict(QUERY, id="active", status="Assigned", outcome="known"),
            dict(QUERY, id="other", service="VPN", status="Closed", outcome="known"),
            dict(QUERY, id="valid", status="Closed", outcome="known"),
        ]
        self.assertEqual(baseline(QUERY, history), ["valid"])


if __name__ == "__main__":
    unittest.main()
