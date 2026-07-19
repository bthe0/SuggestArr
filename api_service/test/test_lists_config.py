"""Config-default tests for the monitored-list integration keys."""
import unittest

from api_service.config.config import get_config_values, get_default_values


class TestListConfigDefaults(unittest.TestCase):
    def test_keys_present(self):
        defaults = get_default_values()
        self.assertIn("TRAKT_CLIENT_ID", defaults)
        self.assertIn("FLARESOLVERR_URL", defaults)

    def test_trakt_client_id_defaults_empty(self):
        self.assertEqual(get_config_values()["TRAKT_CLIENT_ID"], "")

    def test_flaresolverr_defaults_to_container_endpoint(self):
        self.assertEqual(
            get_config_values()["FLARESOLVERR_URL"],
            "http://flaresolverr:8191/v1",
        )


if __name__ == "__main__":
    unittest.main()
