import unittest
from http import HTTPStatus

import server


class SearchPayloadTest(unittest.TestCase):
    def test_trims_values_and_normalizes_infrastructure(self):
        query = server.search_payload({
            "incident_id": " INC-1 ",
            "symptom": "  Не пускает в Wi-Fi  ",
            "service": " Wi-Fi ",
            "site": " Ростов-на-Дону ",
            "infrastructure": [" Офисная сеть ", "Офисная сеть", ""],
            "current_group": " Первая линия ",
        })
        self.assertEqual(query, {
            "incident_id": "INC-1",
            "symptom": "Не пускает в Wi-Fi",
            "service": "Wi-Fi",
            "site": "Ростов-на-Дону",
            "infrastructure": ["Офисная сеть"],
            "current_group": "Первая линия",
        })

    def test_accepts_a_single_infrastructure_value_and_empty_site(self):
        query = server.search_payload({
            "incident_id": "INC-1",
            "symptom": "Не работает сеть",
            "service": "",
            "site": "",
            "infrastructure": "Офисная сеть",
        })
        self.assertEqual(query["service"], "")
        self.assertEqual(query["site"], "")
        self.assertEqual(query["infrastructure"], ["Офисная сеть"])

    def test_requires_incident_symptom_and_a_filter(self):
        for payload in (
            {"symptom": "текст", "service": "Wi-Fi"},
            {"incident_id": "INC-1", "service": "Wi-Fi"},
            {"incident_id": "INC-1", "symptom": "текст"},
            {"incident_id": "INC-1", "symptom": "текст", "service": "", "infrastructure": []},
        ):
            with self.subTest(payload=payload):
                with self.assertRaises(server.RequestError) as error:
                    server.search_payload(payload)
                self.assertEqual(error.exception.status, HTTPStatus.BAD_REQUEST)

    def test_rejects_unknown_fields_wrong_types_and_oversized_input(self):
        cases = (
            ({"incident_id": "INC-1", "symptom": "текст", "service": "Wi-Fi", "extra": 1},
             HTTPStatus.BAD_REQUEST),
            ({"incident_id": "INC-1", "symptom": 12, "service": "Wi-Fi"},
             HTTPStatus.BAD_REQUEST),
            ({"incident_id": "INC-1", "symptom": "x" * (server.MAX_SYMPTOM + 1),
              "service": "Wi-Fi"}, HTTPStatus.BAD_REQUEST),
            ({"incident_id": "INC-1", "symptom": "текст", "service": "Wi-Fi",
              "infrastructure": [f"infra-{i}" for i in range(server.MAX_INFRASTRUCTURE + 1)]},
             HTTPStatus.BAD_REQUEST),
            (["not", "an", "object"], HTTPStatus.BAD_REQUEST),
        )
        for payload, status in cases:
            with self.subTest(payload=payload):
                with self.assertRaises(server.RequestError) as error:
                    server.search_payload(payload)
                self.assertEqual(error.exception.status, status)

    def test_catalogue_validation_uses_database_values(self):
        catalogue = {"service": ["Wi-Fi"], "site": ["Ростов-на-Дону"], "infrastructure": ["Офисная сеть"]}
        server.validate_catalogue(
            {"service": "Wi-Fi", "site": "Ростов-на-Дону", "infrastructure": ["Офисная сеть"]},
            catalogue,
        )
        with self.assertRaises(server.RequestError):
            server.validate_catalogue(
                {"service": "Wi-Fi", "site": "Ростов", "infrastructure": []},
                catalogue,
            )


if __name__ == "__main__":
    unittest.main()
