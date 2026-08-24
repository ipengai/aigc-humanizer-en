import unittest
from unittest.mock import patch

from app.humanizer.ai_text_humanizer import AITextHumanizer


class AITextHumanizerMinimumLengthTests(unittest.TestCase):
    def test_short_input_is_rejected_before_network_call(self):
        humanizer = AITextHumanizer(email="test@example.com", password="secret")

        with patch(
            "app.humanizer.ai_text_humanizer._cfg", return_value=300
        ), patch(
            "app.humanizer.ai_text_humanizer.urllib_request.urlopen"
        ) as urlopen:
            success, message = humanizer._call_api("short input")

        self.assertFalse(success)
        self.assertIn("minimum 300", message)
        urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
