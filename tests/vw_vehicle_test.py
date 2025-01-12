"""Vehicle class tests."""
import sys
from datetime import datetime

if sys.version_info >= (3, 8):
    # This won't work on python versions less than 3.8
    from unittest import IsolatedAsyncioTestCase
else:
    from unittest import TestCase

    class IsolatedAsyncioTestCase(TestCase):
        """Python 3.7 compatibility dummy class."""

        pass


from unittest.mock import MagicMock, patch

from aiohttp import ClientSession
from freezegun import freeze_time

from volkswagencarnet.vw_vehicle import Vehicle


class VehicleTest(IsolatedAsyncioTestCase):
    """Test Vehicle methods."""

    @freeze_time("2022-02-14 03:04:05")
    async def test_init(self):
        """Test that init does what it should."""
        async with ClientSession() as conn:
            target_date = datetime.fromisoformat("2022-02-14 03:04:05")
            url = "https://foo.bar"
            vehicle = Vehicle(conn, url)
            self.assertEqual(conn, vehicle._connection)
            self.assertEqual(url, vehicle._url)
            self.assertEqual("https://msg.volkswagen.de", vehicle._homeregion)
            self.assertFalse(vehicle._discovered)
            self.assertEqual({}, vehicle._states)
            self.assertEqual(30, vehicle._climate_duration)
            self.assertDictEqual(
                {
                    "batterycharge": {"status": "", "timestamp": target_date},
                    "climatisation": {"status": "", "timestamp": target_date},
                    # 'departuretimer': {'status': '', 'timestamp': datetime.now()}, # Not yet implemented
                    "latest": "",
                    "lock": {"status": "", "timestamp": target_date},
                    "preheater": {"status": "", "timestamp": target_date},
                    "refresh": {"status": "", "timestamp": target_date},
                    "remaining": -1,
                    "state": "",
                },
                vehicle._requests,
            )

            self.assertDictEqual(
                {
                    "carfinder_v1": {"active": False},
                    "rbatterycharge_v1": {"active": False},
                    "rclima_v1": {"active": False},
                    "rheating_v1": {"active": False},
                    "rhonk_v1": {"active": False},
                    "rlu_v1": {"active": False},
                    "statusreport_v1": {"active": False},
                    # 'timerprogramming_v1': {'active': False}, # Not yet implemented
                    "trip_statistic_v1": {"active": False},
                },
                vehicle._services,
            )

    def test_discover(self):
        """Test the discovery process."""
        pass

    async def test_update_deactivated(self):
        """Test that calling update on a deactivated Vehicle does nothing."""
        vehicle = MagicMock(spec=Vehicle, name="MockDeactivatedVehicle")
        vehicle.update = lambda: Vehicle.update(vehicle)
        vehicle._discovered = True
        vehicle._deactivated = True

        await vehicle.update()

        vehicle.discover.assert_not_called()
        # Verify that no other methods were called
        self.assertEqual(0, vehicle.method_calls.__len__(), f"xpected none, got {vehicle.method_calls}")

    async def test_update(self):
        """Test that update calls the wanted methods and nothing else."""
        vehicle = MagicMock(spec=Vehicle, name="MockUpdateVehicle")
        vehicle.update = lambda: Vehicle.update(vehicle)

        vehicle._discovered = False
        vehicle.deactivated = False
        await vehicle.update()

        vehicle.discover.assert_called_once()
        vehicle.get_preheater.assert_called_once()
        vehicle.get_climater.assert_called_once()
        vehicle.get_trip_statistic.assert_called_once()
        vehicle.get_position.assert_called_once()
        vehicle.get_statusreport.assert_called_once()
        vehicle.get_charger.assert_called_once()
        vehicle.get_timerprogramming.assert_called_once()

        # Verify that only the expected functions above were called
        self.assertEqual(
            8, vehicle.method_calls.__len__(), f"Wrong number of methods called. Expected 8, got {vehicle.method_calls}"
        )

    async def test_json(self):
        """Test that update calls the wanted methods and nothing else."""
        vehicle = Vehicle(conn=None, url="dummy34")

        vehicle._discovered = True
        dtstring = "2022-02-22T02:22:20+02:00"
        d = datetime.fromisoformat(dtstring)

        with patch.dict(vehicle.attrs, {"a string": "yay", "some date": d}):
            res = f"{vehicle.json}"
            self.assertEqual('{\n    "a string": "yay",\n    "some date": "2022-02-22T02:22:20+02:00"\n}', res)
