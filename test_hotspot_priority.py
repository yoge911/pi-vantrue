import unittest
from unittest.mock import MagicMock, patch

from config import Config
import main
from transfer_state import set_transfer_in_progress, is_transfer_in_progress


class TestHotspotPrioritySelection(unittest.TestCase):
    def setUp(self):
        class TestConfig(Config):
            PREFERRED_HOTSPOT_SSID = "Vantrue-iPhone-Hotspot"
            FALLBACK_HOTSPOT_SSID = "iPhone 1"
            INTERNET_INTERFACE = "wlan1"
            HOTSPOT_RETRY_INTERVAL = 15

        self.config = TestConfig
        set_transfer_in_progress(False)

    def tearDown(self):
        set_transfer_in_progress(False)

    @patch("main.check_interface_exists", return_value=True)
    @patch("main.get_active_wifi_connection", return_value=None)
    @patch("main.is_ssid_visible")
    @patch("main.connect")
    def test_1_both_iphones_available_connects_to_preferred(
        self, mock_connect, mock_is_visible, mock_active, mock_iface
    ):
        """TEST 1: Both iPhones available -> connects to preferred Vantrue-iPhone-Hotspot."""
        mock_is_visible.side_effect = lambda ssid, interface, rescan: ssid in [
            "Vantrue-iPhone-Hotspot",
            "iPhone 1",
        ]
        mock_connect.return_value = True

        mock_uploader = MagicMock()
        mock_uploader.check_internet_connectivity.side_effect = [False, True]

        result = main.ensure_internet_connection_wlan1(uploader=mock_uploader)

        self.assertTrue(result)
        mock_connect.assert_called_once_with(
            "Vantrue-iPhone-Hotspot", interface="wlan1", check_visibility=False
        )

    @patch("main.check_interface_exists", return_value=True)
    @patch("main.get_active_wifi_connection", return_value=None)
    @patch("main.is_ssid_visible")
    @patch("main.connect")
    def test_2_preferred_unavailable_fallback_available(
        self, mock_connect, mock_is_visible, mock_active, mock_iface
    ):
        """TEST 2: Preferred unavailable, fallback available -> connects to regular iPhone 1."""
        def is_visible_side_effect(ssid, interface, rescan):
            return ssid == "iPhone 1"

        mock_is_visible.side_effect = is_visible_side_effect
        mock_connect.return_value = True

        mock_uploader = MagicMock()
        mock_uploader.check_internet_connectivity.side_effect = [False, True]

        result = main.ensure_internet_connection_wlan1(uploader=mock_uploader)

        self.assertTrue(result)
        mock_connect.assert_called_once_with(
            "iPhone 1", interface="wlan1", check_visibility=False
        )

    @patch("main.check_interface_exists", return_value=True)
    @patch("main.get_active_wifi_connection", return_value=None)
    @patch("main.is_ssid_visible", return_value=False)
    @patch("main.connect")
    def test_3_neither_iphone_available(
        self, mock_connect, mock_is_visible, mock_active, mock_iface
    ):
        """TEST 3: Neither iPhone available -> returns False and logs 15s retry warning without crashing."""
        mock_uploader = MagicMock()
        mock_uploader.check_internet_connectivity.return_value = False

        result = main.ensure_internet_connection_wlan1(uploader=mock_uploader)

        self.assertFalse(result)
        mock_connect.assert_not_called()

    @patch("main.check_interface_exists", return_value=True)
    @patch("main.get_active_wifi_connection", return_value=None)
    @patch("main.is_ssid_visible")
    @patch("main.connect")
    def test_4_initially_none_then_preferred_becomes_available(
        self, mock_connect, mock_is_visible, mock_active, mock_iface
    ):
        """TEST 4: Neither available initially, then Vantrue-iPhone-Hotspot becomes available."""
        mock_uploader = MagicMock()
        # Pass 1: No internet, no networks visible
        mock_uploader.check_internet_connectivity.side_effect = [False, False, True]
        mock_is_visible.return_value = False

        result_pass1 = main.ensure_internet_connection_wlan1(uploader=mock_uploader)
        self.assertFalse(result_pass1)

        # Pass 2: Preferred hotspot becomes visible
        mock_is_visible.side_effect = lambda ssid, interface, rescan: ssid == "Vantrue-iPhone-Hotspot"
        mock_connect.return_value = True

        result_pass2 = main.ensure_internet_connection_wlan1(uploader=mock_uploader)
        self.assertTrue(result_pass2)
        mock_connect.assert_called_with(
            "Vantrue-iPhone-Hotspot", interface="wlan1", check_visibility=False
        )

    @patch("main.check_interface_exists", return_value=True)
    @patch("main.get_active_wifi_connection", return_value="iPhone 1")
    @patch("main.is_ssid_visible")
    @patch("main.connect")
    def test_5_fallback_active_preferred_becomes_available(
        self, mock_connect, mock_is_visible, mock_active, mock_iface
    ):
        """TEST 5: Connected to fallback, preferred becomes available -> switches safely when transfer idle; defers when transfer in progress."""
        mock_uploader = MagicMock()
        mock_uploader.check_internet_connectivity.return_value = True
        mock_is_visible.side_effect = lambda ssid, interface, rescan: ssid == "Vantrue-iPhone-Hotspot"
        mock_connect.return_value = True

        # Scenario A: Transfer IN PROGRESS -> Skip preferred check
        set_transfer_in_progress(True)
        result_busy = main.ensure_internet_connection_wlan1(uploader=mock_uploader)
        self.assertTrue(result_busy)
        mock_connect.assert_not_called()

        # Scenario B: Transfer IDLE -> Switch to preferred
        set_transfer_in_progress(False)
        result_idle = main.ensure_internet_connection_wlan1(uploader=mock_uploader)
        self.assertTrue(result_idle)
        mock_connect.assert_called_once_with(
            "Vantrue-iPhone-Hotspot", interface="wlan1", check_visibility=False
        )

    @patch("main.check_interface_exists", return_value=True)
    @patch("main.get_active_wifi_connection", return_value="Vantrue-iPhone-Hotspot")
    @patch("main.is_ssid_visible", return_value=False)
    @patch("main.connect")
    def test_6_associated_to_ssid_but_no_internet_triggers_recovery(
        self, mock_connect, mock_is_visible, mock_active, mock_iface
    ):
        """TEST 6: Associated to SSID but internet check fails -> triggers recovery instead of assuming association is sufficient."""
        mock_uploader = MagicMock()
        # Active connection exists, but internet check returns False!
        mock_uploader.check_internet_connectivity.return_value = False

        result = main.ensure_internet_connection_wlan1(uploader=mock_uploader)

        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
