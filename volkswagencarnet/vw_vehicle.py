#!/usr/bin/env python3
"""Vehicle class for We Connect."""
import asyncio
import logging
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from json import dumps as to_json
from typing import Optional

from .vw_utilities import find_path, is_valid_path

_LOGGER = logging.getLogger(__name__)

PRIMARY_RANGE = "0x0301030006"
PRIMARY_DRIVE = "0x0301030007"
SECONDARY_RANGE = "0x0301030008"
SECONDARY_DRIVE = "0x0301030009"
COMBINED_RANGE = "0x0301030005"

ENGINE_TYPE_ELECTRIC = "3"
ENGINE_TYPE_DIESEL = "5"
ENGINE_TYPE_GASOLINE = "6"
ENGINE_TYPE_COMBUSTION = [ENGINE_TYPE_DIESEL, ENGINE_TYPE_GASOLINE]

FUEL_LEVEL = "0x030103000A"
UNSUPPORTED = 0
NO_VALUE = -1


class Vehicle:
    """Vehicle contains the state of sensors and methods for interacting with the car."""

    def __init__(self, conn, url):
        """Initialize the Vehicle with default values."""
        self._connection = conn
        self._url = url
        self._homeregion = "https://msg.volkswagen.de"
        self._discovered = False
        self._states = {}
        self._requests: dict[str, any] = {
            # 'departuretimer': {'status': '', 'timestamp': datetime.now()}, # Not yet implemented
            "batterycharge": {"status": "", "timestamp": datetime.now()},
            "climatisation": {"status": "", "timestamp": datetime.now()},
            "refresh": {"status": "", "timestamp": datetime.now()},
            "lock": {"status": "", "timestamp": datetime.now()},
            "preheater": {"status": "", "timestamp": datetime.now()},
            "remaining": -1,
            "latest": "",
            "state": "",
        }
        self._climate_duration = 30

        # API Endpoints that might be enabled for car (that we support)
        self._services = {
            "rheating_v1": {"active": False},
            "rclima_v1": {"active": False},
            "rlu_v1": {"active": False},
            "trip_statistic_v1": {"active": False},
            "statusreport_v1": {"active": False},
            "rbatterycharge_v1": {"active": False},
            "rhonk_v1": {"active": False},
            "carfinder_v1": {"active": False},
            # 'timerprogramming_v1': {'active': False}, # Not yet implemented
        }

    # API get and set functions #
    # Init and update vehicle data
    async def discover(self):
        """Discover vehicle and initial data."""
        homeregion = await self._connection.getHomeRegion(self.vin)
        _LOGGER.debug(f"Get homeregion for VIN {self.vin}")
        if homeregion:
            self._homeregion = homeregion

        await asyncio.gather(self.get_carportdata(), self.get_realcardata(), return_exceptions=True)
        _LOGGER.info(f'Vehicle {self.vin} added. Homeregion is "{self._homeregion}"')

        _LOGGER.debug("Attempting discovery of supported API endpoints for vehicle.")
        operation_list = await self._connection.getOperationList(self.vin)
        if operation_list:
            service_info = operation_list["serviceInfo"]
            # Iterate over all endpoints in ServiceInfo list
            for service in service_info:
                try:
                    if service.get("serviceId", "Invalid") in self._services.keys():
                        data = {}
                        service_name = service.get("serviceId", None)
                        if service.get("serviceStatus", {}).get("status", "Disabled") == "Enabled":
                            _LOGGER.debug(f'Discovered enabled service: {service["serviceId"]}')
                            data["active"] = True
                            if service.get("cumulatedLicense", {}).get("expirationDate", False):
                                data["expiration"] = (
                                    service.get("cumulatedLicense", {}).get("expirationDate", None).get("content", None)
                                )
                            if service.get("operation", False):
                                data.update({"operations": []})
                                for operation in service.get("operation", []):
                                    data["operations"].append(operation.get("id", None))
                        elif service.get("serviceStatus", {}).get("status", None) == "Disabled":
                            reason = service.get("serviceStatus", {}).get("reason", "Unknown")
                            _LOGGER.debug(f"Service: {service_name} is disabled because of reason: {reason}")
                            data["active"] = False
                        else:
                            _LOGGER.warning(f"Could not determine status of service: {service_name}, assuming enabled")
                            data["active"] = True
                        self._services[service_name].update(data)
                except Exception as error:
                    _LOGGER.warning(f'Encountered exception: "{error}" while parsing service item: {service}')
        else:
            _LOGGER.warning(f"Could not determine available API endpoints for {self.vin}")
        _LOGGER.debug(f"API endpoints: {self._services}")
        self._discovered = True

    async def update(self):
        """Try to fetch data for all known API endpoints."""
        if not self._discovered:
            await self.discover()
        if not self.deactivated:
            await asyncio.gather(
                self.get_preheater(),
                self.get_climater(),
                self.get_trip_statistic(),
                self.get_position(),
                self.get_statusreport(),
                self.get_charger(),
                self.get_timerprogramming(),
                return_exceptions=True,
            )
        else:
            _LOGGER.info(f"Vehicle with VIN {self.vin} is deactivated.")

    # Data collection functions
    async def get_realcardata(self):
        """Fetch realcar data."""
        data = await self._connection.getRealCarData(self.vin)
        if data:
            self._states.update(data)

    async def get_carportdata(self):
        """Fetch carport data."""
        data = await self._connection.getCarportData(self.vin)
        if data:
            self._states.update(data)

    async def get_preheater(self):
        """Fetch pre-heater data if function is enabled."""
        if self._services.get("rheating_v1", {}).get("active", False):
            if not await self.expired("rheating_v1"):
                data = await self._connection.getPreHeater(self.vin)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug("Could not fetch preheater data")
        else:
            self._requests.pop("preheater", None)

    async def get_climater(self):
        """Fetch climater data if function is enabled."""
        if self._services.get("rclima_v1", {}).get("active", False):
            if not await self.expired("rclima_v1"):
                data = await self._connection.getClimater(self.vin)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug("Could not fetch climater data")
        else:
            self._requests.pop("climatisation", None)

    async def get_trip_statistic(self):
        """Fetch trip data if function is enabled."""
        if self._services.get("trip_statistic_v1", {}).get("active", False):
            if not await self.expired("trip_statistic_v1"):
                data = await self._connection.getTripStatistics(self.vin)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug("Could not fetch trip statistics")

    async def get_position(self):
        """Fetch position data if function is enabled."""
        if self._services.get("carfinder_v1", {}).get("active", False):
            if not await self.expired("carfinder_v1"):
                data = await self._connection.getPosition(self.vin)
                if data:
                    # Reset requests remaining to 15 if parking time has been updated
                    if data.get("findCarResponse", {}).get("parkingTimeUTC", False):
                        try:
                            new_time = data.get("findCarResponse").get("parkingTimeUTC")
                            old_time = self.attrs.get("findCarResponse").get("parkingTimeUTC")
                            if new_time > old_time:
                                _LOGGER.debug("Detected new parking time")
                                self.requests_remaining = 15
                        except Exception:
                            pass
                    self._states.update(data)
                else:
                    _LOGGER.debug("Could not fetch any positional data")

    async def get_statusreport(self):
        """Fetch status data if function is enabled."""
        if self._services.get("statusreport_v1", {}).get("active", False):
            if not await self.expired("statusreport_v1"):
                data = await self._connection.getVehicleStatusData(self.vin)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug("Could not fetch status report")

    async def get_charger(self):
        """Fetch charger data if function is enabled."""
        if self._services.get("rbatterycharge_v1", {}).get("active", False):
            if not await self.expired("rbatterycharge_v1"):
                data = await self._connection.getCharger(self.vin)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug("Could not fetch charger data")
        else:
            self._requests.pop("charger", None)

    async def get_timerprogramming(self):
        """Fetch timer data if function is enabled."""
        if self._services.get("timerprogramming_v1", {}).get("active", False):
            if not await self.expired("timerprogramming_v1"):
                data = await self._connection.getTimers(self.vin)
                if data:
                    self._states.update(data)
                else:
                    _LOGGER.debug("Could not fetch timers")
        else:
            self._requests.pop("departuretimer", None)

    async def wait_for_request(self, section, request, retry_count=36):
        """Update status of outstanding requests."""
        retry_count -= 1
        if retry_count == 0:
            _LOGGER.info(f"Timeout while waiting for result of {request.requestId}.")
            return "Timeout"
        try:
            status = await self._connection.get_request_status(self.vin, section, request)
            _LOGGER.debug(f"Request ID {request}: {status}")
            if status == "In progress":
                self._requests["state"] = "In progress"
                await asyncio.sleep(5)
                return await self.wait_for_request(section, request)
            else:
                self._requests["state"] = status
                return status
        except Exception as error:
            _LOGGER.warning(f"Exception encountered while waiting for request status: {error}")
            return "Exception"

    # Data set functions
    # Charging (BATTERYCHARGE)
    async def set_charger_current(self, value):
        """Set charger current."""
        if self.is_charging_supported:
            if 1 <= int(value) <= 255:
                data = {"action": {"settings": {"maxChargeCurrent": int(value)}, "type": "setSettings"}}
            else:
                _LOGGER.error(f"Set charger maximum current to {value} is not supported.")
                raise Exception(f"Set charger maximum current to {value} is not supported.")
            return await self.set_charger(data)
        else:
            _LOGGER.error("No charger support.")
            raise Exception("No charger support.")

    async def set_charger(self, action):
        """Charging actions."""
        if not self._services.get("rbatterycharge_v1", False):
            _LOGGER.info("Remote start/stop of charger is not supported.")
            raise Exception("Remote start/stop of charger is not supported.")
        if self._requests["batterycharge"].get("id", False):
            timestamp = self._requests.get("batterycharge", {}).get("timestamp", datetime.now())
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get("batterycharge", {}).pop("id")
            else:
                _LOGGER.debug("Charging action already in progress")
                return False
        if action in ["start", "stop"]:
            data = {"action": {"type": action}}
        elif action.get("action", {}).get("type", "") == "setSettings":
            data = action
        else:
            _LOGGER.error(f"Invalid charger action: {action}. Must be either start or stop")
            raise Exception(f"Invalid charger action: {action}. Must be either start or stop")
        try:
            self._requests["latest"] = "Charger"
            response = await self._connection.setCharger(self.vin, data)
            if not response:
                self._requests["batterycharge"] = {"status": "Failed"}
                _LOGGER.error(f"Failed to {action} charging")
                raise Exception(f"Failed to {action} charging")
            else:
                self._requests["remaining"] = response.get("rate_limit_remaining", -1)
                self._requests["batterycharge"] = {
                    "timestamp": datetime.now(),
                    "status": response.get("state", "Unknown"),
                    "id": response.get("id", 0),
                }
                if response.get("state", None) == "Throttled":
                    status = "Throttled"
                else:
                    status = await self.wait_for_request("batterycharge", response.get("id", 0))
                self._requests["batterycharge"] = {"status": status}
                return True
        except Exception as error:
            _LOGGER.warning(f"Failed to {action} charging - {error}")
            self._requests["batterycharge"] = {"status": "Exception"}
            raise Exception(f"Failed to {action} charging - {error}")

    # Climatisation electric/auxiliary/windows (CLIMATISATION)
    async def set_climatisation_temp(self, temperature=20):
        """Set climatisation target temp."""
        if self.is_electric_climatisation_supported or self.is_auxiliary_climatisation_supported:
            if 16 <= int(temperature) <= 30:
                temp = int((temperature + 273) * 10)
                data = {"action": {"settings": {"targetTemperature": temp}, "type": "setSettings"}}
            else:
                _LOGGER.error(f"Set climatisation target temp to {temperature} is not supported.")
                raise Exception(f"Set climatisation target temp to {temperature} is not supported.")
            return await self.set_climater(data)
        else:
            _LOGGER.error("No climatisation support.")
            raise Exception("No climatisation support.")

    async def set_window_heating(self, action="stop"):
        """Turn on/off window heater."""
        if self.is_window_heater_supported:
            if action in ["start", "stop"]:
                data = {"action": {"type": action + "WindowHeating"}}
            else:
                _LOGGER.error(f'Window heater action "{action}" is not supported.')
                raise Exception(f'Window heater action "{action}" is not supported.')
            return await self.set_climater(data)
        else:
            _LOGGER.error("No climatisation support.")
            raise Exception("No climatisation support.")

    async def set_battery_climatisation(self, mode=False):
        """Turn on/off electric climatisation from battery."""
        if self.is_electric_climatisation_supported:
            if mode in [True, False]:
                data = {"action": {"settings": {"climatisationWithoutHVpower": mode}, "type": "setSettings"}}
            else:
                _LOGGER.error(f'Set climatisation without external power to "{mode}" is not supported.')
                raise Exception(f'Set climatisation without external power to "{mode}" is not supported.')
            return await self.set_climater(data)
        else:
            _LOGGER.error("No climatisation support.")
            raise Exception("No climatisation support.")

    async def set_climatisation(self, mode="off", spin=False):
        """Turn on/off climatisation with electric/auxiliary heater."""
        if self.is_electric_climatisation_supported:
            if mode in ["electric", "auxiliary"]:
                target_temp = int((self.climatisation_target_temperature + 273) * 10)
                without_hv_power = self.climatisation_without_external_power
                data = {
                    "action": {
                        "settings": {
                            "climatisationWithoutHVpower": without_hv_power,
                            "targetTemperature": target_temp,
                            "heaterSource": mode,
                        },
                        "type": "startClimatisation",
                    }
                }
            elif mode == "off":
                data = {"action": {"type": "stopClimatisation"}}
            else:
                _LOGGER.error(f"Invalid climatisation type: {mode}")
                raise Exception(f"Invalid climatisation type: {mode}")
            return await self.set_climater(data, spin)
        else:
            _LOGGER.error("No climatisation support.")
            raise Exception("No climatisation support.")

    async def set_climater(self, data, spin=False):
        """Climater actions."""
        if not self._services.get("rclima_v1", False):
            _LOGGER.info("Remote control of climatisation functions is not supported.")
            raise Exception("Remote control of climatisation functions is not supported.")
        if self._requests["climatisation"].get("id", False):
            timestamp = self._requests.get("climatisation", {}).get("timestamp", datetime.now())
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get("climatisation", {}).pop("id")
            else:
                _LOGGER.debug("A climatisation action is already in progress")
                return False
        try:
            self._requests["latest"] = "Climatisation"
            response = await self._connection.setClimater(self.vin, data, spin)
            if not response:
                self._requests["climatisation"] = {"status": "Failed"}
                _LOGGER.error("Failed to execute climatisation request")
                raise Exception("Failed to execute climatisation request")
            else:
                self._requests["remaining"] = response.get("rate_limit_remaining", -1)
                self._requests["climatisation"] = {
                    "timestamp": datetime.now(),
                    "status": response.get("state", "Unknown"),
                    "id": response.get("id", 0),
                }
                if response.get("state", None) == "Throttled":
                    status = "Throttled"
                else:
                    status = await self.wait_for_request("climatisation", response.get("id", 0))
                self._requests["climatisation"] = {"status": status}
                return True
        except Exception as error:
            _LOGGER.warning(f"Failed to execute climatisation request - {error}")
            self._requests["climatisation"] = {"status": "Exception"}
        raise Exception("Climatisation action failed")

    # Parking heater heating/ventilation (RS)
    async def set_pheater(self, mode, spin):
        """Set the mode for the parking heater."""
        if not self.is_pheater_heating_supported:
            _LOGGER.error("No parking heater support.")
            raise Exception("No parking heater support.")
        if self._requests["preheater"].get("id", False):
            timestamp = self._requests.get("preheater", {}).get("timestamp", datetime.now())
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get("preheater", {}).pop("id")
            else:
                _LOGGER.debug("A parking heater action is already in progress")
                return False
        if mode not in ["heating", "ventilation", "off"]:
            _LOGGER.error(f"{mode} is an invalid action for parking heater")
            raise Exception(f"{mode} is an invalid action for parking heater")
        if mode == "off":
            data = {"performAction": {"quickstop": {"active": False}}}
        else:
            data = {
                "performAction": {
                    "quickstart": {"climatisationDuration": self.pheater_duration, "startMode": mode, "active": True}
                }
            }
        try:
            self._requests["latest"] = "Preheater"
            response = await self._connection.setPreHeater(self.vin, data, spin)
            if not response:
                self._requests["preheater"] = {"status": "Failed"}
                _LOGGER.error(f"Failed to set parking heater to {mode}")
                raise Exception(f'setPreHeater returned "{response}"')
            else:
                self._requests["remaining"] = response.get("rate_limit_remaining", -1)
                self._requests["preheater"] = {
                    "timestamp": datetime.now(),
                    "status": response.get("state", "Unknown"),
                    "id": response.get("id", 0),
                }
                if response.get("state", None) == "Throttled":
                    status = "Throttled"
                else:
                    status = await self.wait_for_request("rs", response.get("id", 0))
                self._requests["preheater"] = {"status": status}
                return True
        except Exception as error:
            _LOGGER.warning(f"Failed to set parking heater mode to {mode} - {error}")
            self._requests["preheater"] = {"status": "Exception"}
        raise Exception("Pre-heater action failed")

    # Lock (RLU)
    async def set_lock(self, action, spin):
        """Remote lock and unlock actions."""
        if not self._services.get("rlu_v1", False):
            _LOGGER.info("Remote lock/unlock is not supported.")
            raise Exception("Remote lock/unlock is not supported.")
        if self._requests["lock"].get("id", False):
            timestamp = self._requests.get("lock", {}).get("timestamp", datetime.now() - timedelta(minutes=5))
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get("lock", {}).pop("id")
            else:
                _LOGGER.debug("A lock action is already in progress")
                return False
        if action in ["lock", "unlock"]:
            data = '<rluAction xmlns="http://audi.de/connect/rlu"><action>' + action + "</action></rluAction>"
        else:
            _LOGGER.error(f"Invalid lock action: {action}")
            raise Exception(f"Invalid lock action: {action}")
        try:
            self._requests["latest"] = "Lock"
            response = await self._connection.setLock(self.vin, data, spin)
            if not response:
                self._requests["lock"] = {"status": "Failed"}
                _LOGGER.error(f"Failed to {action} vehicle")
                raise Exception(f"Failed to {action} vehicle")
            else:
                self._requests["remaining"] = response.get("rate_limit_remaining", -1)
                self._requests["lock"] = {
                    "timestamp": datetime.now(),
                    "status": response.get("state", "Unknown"),
                    "id": response.get("id", 0),
                }
                if response.get("state", None) == "Throttled":
                    status = "Throttled"
                else:
                    status = await self.wait_for_request("rlu", response.get("id", 0))
                self._requests["lock"] = {"status": status}
                return True
        except Exception as error:
            _LOGGER.warning(f"Failed to {action} vehicle - {error}")
            self._requests["lock"] = {"status": "Exception"}
        raise Exception("Lock action failed")

    # Refresh vehicle data (VSR)
    async def set_refresh(self):
        """Wake up vehicle and update status data."""
        if not self._services.get("statusreport_v1", {}).get("active", False):
            _LOGGER.info("Data refresh is not supported.")
            raise Exception("Data refresh is not supported.")
        if self._requests["refresh"].get("id", False):
            timestamp = self._requests.get("refresh", {}).get("timestamp", datetime.now() - timedelta(minutes=5))
            expired = datetime.now() - timedelta(minutes=3)
            if expired > timestamp:
                self._requests.get("refresh", {}).pop("id")
            else:
                _LOGGER.debug("A data refresh request is already in progress")
                return False
        try:
            self._requests["latest"] = "Refresh"
            response = await self._connection.setRefresh(self.vin)
            if not response:
                _LOGGER.error("Failed to request vehicle update")
                self._requests["refresh"] = {"status": "Failed"}
                raise Exception("Failed to execute data refresh")
            else:
                self._requests["remaining"] = response.get("rate_limit_remaining", -1)
                self._requests["refresh"] = {
                    "timestamp": datetime.now(),
                    "status": response.get("status", "Unknown"),
                    "id": response.get("id", 0),
                }
                if response.get("state", None) == "Throttled":
                    status = "Throttled"
                else:
                    status = await self.wait_for_request("vsr", response.get("id", 0))
                self._requests["refresh"] = {"status": status}
                return True
        except Exception as error:
            _LOGGER.warning(f"Failed to execute data refresh - {error}")
            self._requests["refresh"] = {"status": "Exception"}
        raise Exception("Data refresh failed")

    # Vehicle class helpers #
    # Vehicle info
    @property
    def attrs(self):
        """
        Return all attributes.

        :return:
        """
        return self._states

    def has_attr(self, attr) -> bool:
        """
        Return true if attribute exists.

        :param attr:
        :return:
        """
        return is_valid_path(self.attrs, attr)

    def get_attr(self, attr):
        """
        Return a specific attribute.

        :param attr:
        :return:
        """
        return find_path(self.attrs, attr)

    async def expired(self, service):
        """Check if access to service has expired."""
        try:
            now = datetime.utcnow()
            if self._services.get(service, {}).get("expiration", False):
                expiration = self._services.get(service, {}).get("expiration", False)
                if not expiration:
                    expiration = datetime.utcnow() + timedelta(days=1)
            else:
                _LOGGER.debug(f"Could not determine end of access for service {service}, assuming it is valid")
                expiration = datetime.utcnow() + timedelta(days=1)
            expiration = expiration.replace(tzinfo=None)
            if now >= expiration:
                _LOGGER.warning(f"Access to {service} has expired!")
                self._discovered = False
                return True
            else:
                return False
        except Exception:
            _LOGGER.debug(f"Exception. Could not determine end of access for service {service}, assuming it is valid")
            return False

    def dashboard(self, **config):
        """
        Return dashboard with specified configuraion.

        :param config:
        :return:
        """
        # Classic python notation
        from .vw_dashboard import Dashboard

        return Dashboard(self, **config)

    @property
    def vin(self) -> str:
        """
        Vehicle identification number.

        :return:
        """
        return self._url

    @property
    def unique_id(self) -> str:
        """
        Return unique id for the vehicle (vin).

        :return:
        """
        return self.vin

    # Information from vehicle states #
    # Car information
    @property
    def nickname(self) -> Optional[str]:
        """
        Return nickname of the vehicle.

        :return:
        """
        return self.attrs.get("carData", {}).get("nickname", None)

    @property
    def is_nickname_supported(self) -> bool:
        """
        Return true if naming the vehicle is supported.

        :return:
        """
        return self.attrs.get("carData", {}).get("nickname", False) is not False

    @property
    def deactivated(self) -> Optional[bool]:
        """
        Return true if service is deactivated.

        :return:
        """
        return self.attrs.get("carData", {}).get("deactivated", None)

    @property
    def is_deactivated_supported(self) -> bool:
        """
        Return true if service deactivation status is supported.

        :return:
        """
        return self.attrs.get("carData", {}).get("deactivated", False) is True

    @property
    def model(self) -> Optional[str]:
        """Return model."""
        return self.attrs.get("carportData", {}).get("modelName", None)

    @property
    def is_model_supported(self) -> bool:
        """Return true if model is supported."""
        return self.attrs.get("carportData", {}).get("modelName", False) is not False

    @property
    def model_year(self) -> Optional[bool]:
        """Return model year."""
        return self.attrs.get("carportData", {}).get("modelYear", None)

    @property
    def is_model_year_supported(self) -> bool:
        """Return true if model year is supported."""
        return self.attrs.get("carportData", {}).get("modelYear", False) is not False

    @property
    def model_image(self) -> str:
        # Not implemented
        """Return vehicle model image."""
        return self.attrs.get("imageUrl")

    @property
    def is_model_image_supported(self) -> bool:
        """
        Return true if vehicle model image is supported.

        :return:
        """
        # Not implemented
        return self.attrs.get("imageUrl", False) is not False

    # Lights
    @property
    def parking_light(self) -> bool:
        """Return true if parking light is on."""
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301010001"].get("value", 0))
        return response != 2

    @property
    def is_parking_light_supported(self) -> bool:
        """Return true if parking light is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            return "0x0301010001" in self.attrs.get("StoredVehicleDataResponseParsed")

    # Connection status
    @property
    def last_connected(self) -> str:
        """Return when vehicle was last connected to connect servers."""
        last_connected_utc = (
            self.attrs.get("StoredVehicleDataResponse")
            .get("vehicleData")
            .get("data")[0]
            .get("field")[0]
            .get("tsCarSentUtc")
        )
        last_connected = last_connected_utc.replace(tzinfo=timezone.utc).astimezone(tz=None)
        return last_connected.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def is_last_connected_supported(self) -> bool:
        """Return when vehicle was last connected to connect servers."""
        if next(
            iter(
                next(
                    iter(self.attrs.get("StoredVehicleDataResponse", {}).get("vehicleData", {}).get("data", {})), None
                ).get("field", {})
            ),
            None,
        ).get("tsCarSentUtc", []):
            return True
        return False

    # Service information
    @property
    def distance(self) -> Optional[int]:
        """Return vehicle odometer."""
        value = self.attrs.get("StoredVehicleDataResponseParsed")["0x0101010002"].get("value", 0)
        if value:
            return int(value)
        return None

    @property
    def is_distance_supported(self) -> bool:
        """Return true if odometer is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            return "0x0101010002" in self.attrs.get("StoredVehicleDataResponseParsed")

    @property
    def service_inspection(self):
        """Return time left for service inspection."""
        return -int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0203010004"].get("value"))

    @property
    def is_service_inspection_supported(self) -> bool:
        """
        Return true if days to service inspection is supported.

        :return:
        """
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            return "0x0203010004" in self.attrs.get("StoredVehicleDataResponseParsed")

    @property
    def service_inspection_distance(self):
        """Return time left for service inspection."""
        return -int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0203010003"].get("value", 0))

    @property
    def is_service_inspection_distance_supported(self):
        """
        Return true if distance to oil inspection is supported.

        :return:
        """
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0203010003" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def oil_inspection(self):
        """Return time left for service inspection."""
        return -int(self.attrs.get("StoredVehicleDataResponseParsed", {}).get("0x0203010002", {}).get("value", 0))

    @property
    def is_oil_inspection_supported(self):
        """
        Return true if days to oil inspection is supported.

        :return:
        """
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0203010002" in self.attrs.get("StoredVehicleDataResponseParsed"):
                if self.attrs.get("StoredVehicleDataResponseParsed").get("0x0203010002").get("value", None) is not None:
                    return True
        return False

    @property
    def oil_inspection_distance(self):
        """Return time left for service inspection."""
        return -int(self.attrs.get("StoredVehicleDataResponseParsed", {}).get("0x0203010001", {}).get("value", 0))

    @property
    def is_oil_inspection_distance_supported(self):
        """
        Return true if oil inspection distance is supported.

        :return:
        """
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0203010001" in self.attrs.get("StoredVehicleDataResponseParsed"):
                if self.attrs.get("StoredVehicleDataResponseParsed").get("0x0203010001").get("value", None) is not None:
                    return True
        return False

    @property
    def adblue_level(self):
        """Return adblue level."""
        return int(self.attrs.get("StoredVehicleDataResponseParsed", {}).get("0x02040C0001", {}).get("value", 0))

    @property
    def is_adblue_level_supported(self):
        """Return true if adblue level is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x02040C0001" in self.attrs.get("StoredVehicleDataResponseParsed"):
                if "value" in self.attrs.get("StoredVehicleDataResponseParsed")["0x02040C0001"]:
                    if self.attrs.get("StoredVehicleDataResponseParsed")["0x02040C0001"].get("value", 0) is not None:
                        return True
        return False

    # Charger related states for EV and PHEV
    @property
    def charging(self):
        """Return battery level."""
        cstate = (
            self.attrs.get("charger", {})
            .get("status", {})
            .get("chargingStatusData", {})
            .get("chargingState", {})
            .get("content", "")
        )
        return 1 if cstate == "charging" else 0

    @property
    def is_charging_supported(self):
        """Return true if charging is supported."""
        if self.attrs.get("charger", False):
            if "status" in self.attrs.get("charger", {}):
                if "chargingStatusData" in self.attrs.get("charger")["status"]:
                    if "chargingState" in self.attrs.get("charger")["status"]["chargingStatusData"]:
                        return True
        return False

    @property
    def battery_level(self):
        """Return battery level."""
        return int(
            self.attrs.get("charger").get("status").get("batteryStatusData").get("stateOfCharge").get("content", 0)
        )

    @property
    def is_battery_level_supported(self):
        """Return true if battery level is supported."""
        if self.attrs.get("charger", False):
            if "status" in self.attrs.get("charger"):
                if "batteryStatusData" in self.attrs.get("charger")["status"]:
                    if "stateOfCharge" in self.attrs.get("charger")["status"]["batteryStatusData"]:
                        return True
        return False

    @property
    def charge_max_ampere(self):
        """Return charger max ampere setting."""
        value = int(self.attrs.get("charger").get("settings").get("maxChargeCurrent").get("content"))
        if value == 254:
            return "Maximum"
        if value == 252:
            return "Reduced"
        if value == 0:
            return "Unknown"
        else:
            return value

    @property
    def is_charge_max_ampere_supported(self):
        """Return true if Charger Max Ampere is supported."""
        if self.attrs.get("charger", False):
            if "settings" in self.attrs.get("charger", {}):
                if "maxChargeCurrent" in self.attrs.get("charger", {})["settings"]:
                    return True
            else:
                return False

    @property
    def charging_cable_locked(self):
        """Return plug locked state."""
        response = self.attrs.get("charger")["status"]["plugStatusData"]["lockState"].get("content", 0)
        if response == "locked":
            return True
        else:
            return False

    @property
    def is_charging_cable_locked_supported(self):
        """Return true if plug locked state is supported."""
        if self.attrs.get("charger", False):
            if "status" in self.attrs.get("charger", {}):
                if "plugStatusData" in self.attrs.get("charger").get("status", {}):
                    if "lockState" in self.attrs.get("charger")["status"].get("plugStatusData", {}):
                        return True
        return False

    @property
    def charging_cable_connected(self):
        """Return plug locked state."""
        response = self.attrs.get("charger")["status"]["plugStatusData"]["plugState"].get("content", 0)
        if response == "connected":
            return False
        else:
            return True

    @property
    def is_charging_cable_connected_supported(self):
        """Return true if charging cable connected is supported."""
        if self.attrs.get("charger", False):
            if "status" in self.attrs.get("charger", {}):
                if "plugStatusData" in self.attrs.get("charger").get("status", {}):
                    if "plugState" in self.attrs.get("charger")["status"].get("plugStatusData", {}):
                        return True
        return False

    @property
    def charging_time_left(self):
        """Return minutes to charging complete."""
        if self.external_power:
            minutes = (
                self.attrs.get("charger", {})
                .get("status", {})
                .get("batteryStatusData", {})
                .get("remainingChargingTime", {})
                .get("content", 0)
            )
            if minutes:
                try:
                    if minutes == -1:
                        return "00:00"
                    if minutes == 65535:
                        return "00:00"
                    return "%02d:%02d" % divmod(minutes, 60)
                except Exception:
                    pass
        return 0

    @property
    def is_charging_time_left_supported(self):
        """Return true if charging is supported."""
        return self.is_charging_supported

    @property
    def external_power(self):
        """Return true if external power is connected."""
        check = (
            self.attrs.get("charger", {})
            .get("status", {})
            .get("chargingStatusData", {})
            .get("externalPowerSupplyState", {})
            .get("content", "")
        )
        if check in ["stationConnected", "available"]:
            return True
        else:
            return False

    @property
    def is_external_power_supported(self):
        """External power supported."""
        if (
            self.attrs.get("charger", {})
            .get("status", {})
            .get("chargingStatusData", {})
            .get("externalPowerSupplyState", False)
        ):
            return True

    @property
    def energy_flow(self):
        """Return true if energy is flowing through charging port."""
        check = (
            self.attrs.get("charger", {})
            .get("status", {})
            .get("chargingStatusData", {})
            .get("energyFlow", {})
            .get("content", "off")
        )
        if check == "on":
            return True
        else:
            return False

    @property
    def is_energy_flow_supported(self):
        """Energy flow supported."""
        if self.attrs.get("charger", {}).get("status", {}).get("chargingStatusData", {}).get("energyFlow", False):
            return True

    # Vehicle location states
    @property
    def position(self):
        """Return  position."""
        try:
            if self.vehicle_moving:
                output = {"lat": None, "lng": None, "timestamp": None}
            else:
                pos_obj = self.attrs.get("findCarResponse", {})
                lat = int(pos_obj.get("Position").get("carCoordinate").get("latitude")) / 1000000
                lng = int(pos_obj.get("Position").get("carCoordinate").get("longitude")) / 1000000
                parking_time = pos_obj.get("parkingTimeUTC")
                output = {"lat": lat, "lng": lng, "timestamp": parking_time}
        except Exception:
            output = {
                "lat": "?",
                "lng": "?",
            }
        return output

    @property
    def is_position_supported(self):
        """Return true if carfinder_v1 service is active."""
        if self._services.get("carfinder_v1", {}).get("active", False):
            # if self.attrs.get('findCarResponse', {}).get('Position', {}).get('carCoordinate', {}).get('latitude', False):
            return True
        elif self.attrs.get("isMoving", False):
            return True

    @property
    def vehicle_moving(self):
        """Return true if vehicle is moving."""
        return self.attrs.get("isMoving", False)

    @property
    def is_vehicle_moving_supported(self):
        """Return true if vehicle supports position."""
        if self.is_position_supported:
            return True

    @property
    def parking_time(self):
        """Return timestamp of last parking time."""
        park_time_utc: datetime = self.attrs.get("findCarResponse", {}).get("parkingTimeUTC", "Unknown")
        park_time = park_time_utc.replace(tzinfo=timezone.utc).astimezone(tz=None)
        return park_time.strftime("%Y-%m-%d %H:%M:%S")

    @property
    def is_parking_time_supported(self):
        """Return true if vehicle parking timestamp is supported."""
        if "parkingTimeUTC" in self.attrs.get("findCarResponse", {}):
            return True

    # Vehicle fuel level and range
    @property
    def electric_range(self):
        """
        Return electric range.

        :return:
        """
        value = -1
        if (
            PRIMARY_RANGE in self.attrs.get("StoredVehicleDataResponseParsed")
            and self.attrs.get("StoredVehicleDataResponseParsed")[PRIMARY_DRIVE].get("value", UNSUPPORTED)
            == ENGINE_TYPE_ELECTRIC
        ):
            value = self.attrs.get("StoredVehicleDataResponseParsed")[PRIMARY_RANGE].get("value", UNSUPPORTED)

        elif (
            SECONDARY_RANGE in self.attrs.get("StoredVehicleDataResponseParsed")
            and self.attrs.get("StoredVehicleDataResponseParsed")[SECONDARY_DRIVE].get("value", UNSUPPORTED)
            == ENGINE_TYPE_ELECTRIC
        ):
            value = self.attrs.get("StoredVehicleDataResponseParsed")[SECONDARY_RANGE].get("value", UNSUPPORTED)
        return int(value)

    @property
    def is_electric_range_supported(self):
        """
        Return true if electric range is supported.

        :return:
        """
        supported = False
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if (
                PRIMARY_RANGE in self.attrs.get("StoredVehicleDataResponseParsed")
                and self.attrs.get("StoredVehicleDataResponseParsed")[PRIMARY_DRIVE].get("value", UNSUPPORTED)
                == ENGINE_TYPE_ELECTRIC
            ):
                supported = True

            elif (
                SECONDARY_RANGE in self.attrs.get("StoredVehicleDataResponseParsed")
                and self.attrs.get("StoredVehicleDataResponseParsed")[SECONDARY_DRIVE].get("value", UNSUPPORTED)
                == ENGINE_TYPE_ELECTRIC
            ):
                supported = True
        return supported

    @property
    def combustion_range(self):
        """
        Return combustion engine range.

        :return:
        """
        value = -1
        if (
            PRIMARY_RANGE in self.attrs.get("StoredVehicleDataResponseParsed")
            and self.attrs.get("StoredVehicleDataResponseParsed")[PRIMARY_DRIVE].get("value", UNSUPPORTED)
            in ENGINE_TYPE_COMBUSTION
        ):
            value = self.attrs.get("StoredVehicleDataResponseParsed")[PRIMARY_RANGE].get("value", NO_VALUE)

        elif (
            SECONDARY_RANGE in self.attrs.get("StoredVehicleDataResponseParsed")
            and self.attrs.get("StoredVehicleDataResponseParsed")[SECONDARY_DRIVE].get("value", UNSUPPORTED)
            in ENGINE_TYPE_COMBUSTION
        ):
            value = self.attrs.get("StoredVehicleDataResponseParsed")[SECONDARY_RANGE].get("value", NO_VALUE)
        return int(value)

    @property
    def is_combustion_range_supported(self):
        """
        Return true if combustion range is supported, i.e. false for EVs.

        :return:
        """
        supported = False
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if (
                PRIMARY_RANGE in self.attrs.get("StoredVehicleDataResponseParsed")
                and self.attrs.get("StoredVehicleDataResponseParsed")[PRIMARY_DRIVE].get("value", UNSUPPORTED)
                in ENGINE_TYPE_COMBUSTION
            ):
                supported = True

            elif (
                SECONDARY_RANGE in self.attrs.get("StoredVehicleDataResponseParsed")
                and self.attrs.get("StoredVehicleDataResponseParsed")[PRIMARY_DRIVE].get("value", UNSUPPORTED)
                in ENGINE_TYPE_COMBUSTION
            ):
                supported = True
        return supported

    @property
    def combined_range(self):
        """
        Return combined range.

        :return:
        """
        value = -1
        if COMBINED_RANGE in self.attrs.get("StoredVehicleDataResponseParsed"):
            value = self.attrs.get("StoredVehicleDataResponseParsed")[COMBINED_RANGE].get("value", NO_VALUE)
        return int(value)

    @property
    def is_combined_range_supported(self):
        """
        Return true if combined range is supported.

        :return:
        """
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if COMBINED_RANGE in self.attrs.get("StoredVehicleDataResponseParsed"):
                return self.is_electric_range_supported and self.is_combustion_range_supported
        return False

    @property
    def fuel_level(self) -> int:
        """
        Return fuel level.

        :return:
        """
        value = -1
        if FUEL_LEVEL in self.attrs.get("StoredVehicleDataResponseParsed"):
            if "value" in self.attrs.get("StoredVehicleDataResponseParsed")[FUEL_LEVEL]:
                value = self.attrs.get("StoredVehicleDataResponseParsed")[FUEL_LEVEL].get("value", 0)
        return int(value)

    @property
    def is_fuel_level_supported(self):
        """
        Return true if fuel level reporting is supported.

        :return:
        """
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if FUEL_LEVEL in self.attrs.get("StoredVehicleDataResponseParsed"):
                return self.is_combustion_range_supported
        return False

    # Climatisation settings
    @property
    def climatisation_target_temperature(self):
        """Return the target temperature from climater."""
        value = self.attrs.get("climater").get("settings").get("targetTemperature").get("content")
        if value:
            reply = float((value / 10) - 273)
            self._climatisation_target_temperature = reply
            return reply

    @property
    def is_climatisation_target_temperature_supported(self):
        """Return true if climatisation target temperature is supported."""
        if self.attrs.get("climater", False):
            if "settings" in self.attrs.get("climater", {}):
                if "targetTemperature" in self.attrs.get("climater", {})["settings"]:
                    return True
            else:
                return False

    @property
    def climatisation_without_external_power(self):
        """Return state of climatisation from battery power."""
        return self.attrs.get("climater").get("settings").get("climatisationWithoutHVpower").get("content", False)

    @property
    def is_climatisation_without_external_power_supported(self):
        """Return true if climatisation on battery power is supported."""
        if self.attrs.get("climater", False):
            if "settings" in self.attrs.get("climater", {}):
                if "climatisationWithoutHVpower" in self.attrs.get("climater", {})["settings"]:
                    return True
            else:
                return False

    @property
    def outside_temperature(self):
        """Return outside temperature."""
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301020001"].get("value", 0))
        if response:
            return round(float((response / 10) - 273.15), 1)
        else:
            return False

    @property
    def is_outside_temperature_supported(self):
        """Return true if outside temp is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301020001" in self.attrs.get("StoredVehicleDataResponseParsed"):
                if "value" in self.attrs.get("StoredVehicleDataResponseParsed")["0x0301020001"]:
                    return True
                else:
                    return False
            else:
                return False

    # Climatisation, electric
    @property
    def electric_climatisation(self):
        """Return status of climatisation."""
        climatisation_type = (
            self.attrs.get("climater", {}).get("settings", {}).get("heaterSource", {}).get("content", "")
        )
        status = (
            self.attrs.get("climater", {})
            .get("status", {})
            .get("climatisationStatusData", {})
            .get("climatisationState", {})
            .get("content", "")
        )
        if status in ["heating", "on"] and climatisation_type == "electric":
            return True
        else:
            return False

    @property
    def is_electric_climatisation_supported(self):
        """Return true if vehichle has climater."""
        return self.is_climatisation_supported

    @property
    def auxiliary_climatisation(self):
        """Return status of auxiliary climatisation."""
        climatisation_type = (
            self.attrs.get("climater", {}).get("settings", {}).get("heaterSource", {}).get("content", "")
        )
        status = (
            self.attrs.get("climater", {})
            .get("status", {})
            .get("climatisationStatusData", {})
            .get("climatisationState", {})
            .get("content", "")
        )
        if status in ["heating", "heatingAuxiliary", "on"] and climatisation_type == "auxiliary":
            return True
        elif status in ["heatingAuxiliary"] and climatisation_type == "electric":
            return True
        else:
            return False

    @property
    def is_auxiliary_climatisation_supported(self):
        """Return true if vehicle has auxiliary climatisation."""
        if self._services.get("rclima_v1", False):
            functions = self._services.get("rclima_v1", {}).get("operations", [])
            # for operation in functions:
            #    if operation['id'] == 'P_START_CLIMA_AU':
            if "P_START_CLIMA_AU" in functions:
                return True
        return False

    @property
    def is_climatisation_supported(self):
        """Return true if climatisation has State."""
        response = (
            self.attrs.get("climater", {})
            .get("status", {})
            .get("climatisationStatusData", {})
            .get("climatisationState", {})
            .get("content", "")
        )
        if response != "":
            return True

    @property
    def window_heater(self):
        """Return status of window heater."""
        ret = False
        status_front = (
            self.attrs.get("climater", {})
            .get("status", {})
            .get("windowHeatingStatusData", {})
            .get("windowHeatingStateFront", {})
            .get("content", "")
        )
        if status_front == "on":
            ret = True

        status_rear = (
            self.attrs.get("climater", {})
            .get("status", {})
            .get("windowHeatingStatusData", {})
            .get("windowHeatingStateRear", {})
            .get("content", "")
        )
        if status_rear == "on":
            ret = True
        return ret

    @property
    def is_window_heater_supported(self):
        """Return true if vehichle has heater."""
        if self.is_electric_climatisation_supported:
            if self.attrs.get("climater", {}).get("status", {}).get("windowHeatingStatusData", {}).get(
                "windowHeatingStateFront", {}
            ).get("content", "") in ["on", "off"]:
                return True
            if self.attrs.get("climater", {}).get("status", {}).get("windowHeatingStatusData", {}).get(
                "windowHeatingStateRear", {}
            ).get("content", "") in ["on", "off"]:
                return True

    # Parking heater, "legacy" auxiliary climatisation
    @property
    def pheater_duration(self):
        """
        Return heating duration for legacy aux heater.

        :return:
        """
        return self._climate_duration

    @pheater_duration.setter
    def pheater_duration(self, value):
        if value in [10, 20, 30, 40, 50, 60]:
            self._climate_duration = value
        else:
            _LOGGER.warning(f"Invalid value for duration: {value}")

    @property
    def is_pheater_duration_supported(self):
        """
        Return true if legacy aux heater is supported.

        :return:
        """
        return self.is_pheater_heating_supported

    @property
    def pheater_ventilation(self):
        """Return status of combustion climatisation."""
        return (
            self.attrs.get("heating", {}).get("climatisationStateReport", {}).get("climatisationState", False)
            == "ventilation"
        )

    @property
    def is_pheater_ventilation_supported(self):
        """Return true if vehichle has combustion climatisation."""
        return self.is_pheater_heating_supported

    @property
    def pheater_heating(self):
        """Return status of combustion engine heating."""
        return (
            self.attrs.get("heating", {}).get("climatisationStateReport", {}).get("climatisationState", False)
            == "heating"
        )

    @property
    def is_pheater_heating_supported(self):
        """Return true if vehichle has combustion engine heating."""
        if self.attrs.get("heating", {}).get("climatisationStateReport", {}).get("climatisationState", False):
            return True

    @property
    def pheater_status(self):
        """Return status of combustion engine heating/ventilation."""
        return self.attrs.get("heating", {}).get("climatisationStateReport", {}).get("climatisationState", "Unknown")

    @property
    def is_pheater_status_supported(self):
        """Return true if vehichle has combustion engine heating/ventilation."""
        if self.attrs.get("heating", {}).get("climatisationStateReport", {}).get("climatisationState", False):
            return True

    # Windows
    @property
    def windows_closed(self):
        """
        Return true if all windows are closed.

        :return:
        """
        return (
            self.window_closed_left_front
            and self.window_closed_left_back
            and self.window_closed_right_front
            and self.window_closed_right_back
        )

    @property
    def is_windows_closed_supported(self):
        """Return true if window state is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301050001" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def window_closed_left_front(self):
        """
        Return left front window closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301050001"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_window_closed_left_front_supported(self):
        """Return true if window state is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301050001" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def window_closed_right_front(self):
        """
        Return right front window closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301050005"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_window_closed_right_front_supported(self):
        """Return true if window state is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301050005" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def window_closed_left_back(self):
        """
        Return left back window closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301050003"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_window_closed_left_back_supported(self):
        """Return true if window state is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301050003" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def window_closed_right_back(self):
        """
        Return right back window closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301050007"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_window_closed_right_back_supported(self):
        """Return true if window state is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301050007" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def sunroof_closed(self):
        """
        Return sunroof closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x030105000B"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_sunroof_closed_supported(self):
        """Return true if sunroof state is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x030105000B" in self.attrs.get("StoredVehicleDataResponseParsed"):
                if int(self.attrs.get("StoredVehicleDataResponseParsed")["0x030105000B"].get("value", 0)) == 0:
                    return False
                else:
                    return True
            else:
                return False

    # Locks
    @property
    def door_locked(self):
        """
        Return true if all doors are locked.

        :return:
        """
        # LEFT FRONT
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301040001"].get("value", 0))
        if response != 2:
            return False
        # LEFT REAR
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301040004"].get("value", 0))
        if response != 2:
            return False
        # RIGHT FRONT
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301040007"].get("value", 0))
        if response != 2:
            return False
        # RIGHT REAR
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x030104000A"].get("value", 0))
        if response != 2:
            return False

        return True

    @property
    def is_door_locked_supported(self):
        """
        Return true if supported.

        :return:
        """
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301040001" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def trunk_locked(self):
        """
        Return trunk locked state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x030104000D"].get("value", 0))
        if response == 2:
            return True
        else:
            return False

    @property
    def is_trunk_locked_supported(self):
        """
        Return true if supported.

        :return:
        """
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x030104000D" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    # Doors, hood and trunk
    @property
    def hood_closed(self):
        """Return true if hood is closed."""
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301040011"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_hood_closed_supported(self):
        """Return true if hood state is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301040011" in self.attrs.get("StoredVehicleDataResponseParsed", {}):
                if int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301040011"].get("value", 0)) == 0:
                    return False
                else:
                    return True
            else:
                return False

    @property
    def door_closed_left_front(self):
        """
        Return left front door closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301040002"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_door_closed_left_front_supported(self):
        """Return true if supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301040002" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def door_closed_right_front(self):
        """
        Return right front door closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301040008"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_door_closed_right_front_supported(self):
        """Return true if supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301040008" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def door_closed_left_back(self):
        """
        Return left back door closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x0301040005"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_door_closed_left_back_supported(self):
        """Return true if supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x0301040005" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def door_closed_right_back(self):
        """
        Return right back door closed state.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x030104000B"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_door_closed_right_back_supported(self):
        """Return true if supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x030104000B" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    @property
    def trunk_closed(self):
        """
        Return state of trunk closed.

        :return:
        """
        response = int(self.attrs.get("StoredVehicleDataResponseParsed")["0x030104000E"].get("value", 0))
        if response == 3:
            return True
        else:
            return False

    @property
    def is_trunk_closed_supported(self):
        """Return true if trunk closed state is supported."""
        if self.attrs.get("StoredVehicleDataResponseParsed", False):
            if "0x030104000E" in self.attrs.get("StoredVehicleDataResponseParsed"):
                return True
            else:
                return False

    # Departure timers
    # Not yet implemented
    @property
    def schedule1(self):
        """
        Return schedule #1.

        :return:
        """
        return False

    @property
    def is_schedule1_suppored(self):
        """
        Return true if supported.

        :return:
        """
        if self.attrs.get("timers", {}).get("timersAndProfiles", {}).get("timerList", {}).get("timer", False):
            return True
        return False

    @property
    def schedule2(self):
        """
        Return schedule #2.

        :return:
        """
        return False

    @property
    def is_schedule2_suppored(self):
        """
        Return true if supported.

        :return:
        """
        if self.attrs.get("timers", {}).get("timersAndProfiles", {}).get("timerList", {}).get("timer", False):
            return True
        return False

    @property
    def schedule3(self):
        """
        Return schedule #3.

        :return:
        """
        return False

    @property
    def is_schedule3_suppored(self):
        """
        Return true if supported.

        :return:
        """
        if self.attrs.get("timers", {}).get("timersAndProfiles", {}).get("timerList", {}).get("timer", False):
            return True
        return False

    # Trip data
    @property
    def trip_last_entry(self):
        """
        Return last trip data entry.

        :return:
        """
        return self.attrs.get("tripstatistics", {})

    @property
    def trip_last_average_speed(self):
        """
        Return last trip average speed.

        :return:
        """
        return self.trip_last_entry.get("averageSpeed")

    @property
    def is_trip_last_average_speed_supported(self):
        """
        Return true if supported.

        :return:
        """
        response = self.trip_last_entry
        if response and type(response.get("averageSpeed", None)) in (float, int):
            return True

    @property
    def trip_last_average_electric_engine_consumption(self):
        """
        Return last trip average electric consumption.

        :return:
        """
        value = self.trip_last_entry.get("averageElectricEngineConsumption")
        return float(value / 10)

    @property
    def is_trip_last_average_electric_engine_consumption_supported(self):
        """
        Return true if supported.

        :return:
        """
        response = self.trip_last_entry
        if response and type(response.get("averageElectricEngineConsumption", None)) in (float, int):
            return True

    @property
    def trip_last_average_fuel_consumption(self):
        """
        Return last trip average fuel consumption.

        :return:
        """
        return int(self.trip_last_entry.get("averageFuelConsumption")) / 10

    @property
    def is_trip_last_average_fuel_consumption_supported(self):
        """
        Return true if supported.

        :return:
        """
        response = self.trip_last_entry
        if response and type(response.get("averageFuelConsumption", None)) in (float, int):
            return True

    @property
    def trip_last_average_auxillary_consumption(self):
        """
        Return last trip average auxiliary consumption.

        :return:
        """
        return self.trip_last_entry.get("averageAuxiliaryConsumption")

    @property
    def is_trip_last_average_auxillary_consumption_supported(self) -> bool:
        """
        Return true if supported.

        :return:
        """
        response = self.trip_last_entry
        return response and type(response.get("averageAuxiliaryConsumption", None)) in (float, int)

    @property
    def trip_last_average_aux_consumer_consumption(self):
        """
        Return last trip average auxiliary consumer consumption.

        :return:
        """
        value = self.trip_last_entry.get("averageAuxConsumerConsumption")
        return float(value / 10)

    @property
    def is_trip_last_average_aux_consumer_consumption_supported(self) -> bool:
        """
        Return true if supported.

        :return:
        """
        response = self.trip_last_entry
        return response and type(response.get("averageAuxConsumerConsumption", None)) in (float, int)

    @property
    def trip_last_duration(self):
        """
        Return last trip duration in minutes(?).

        :return:
        """
        return self.trip_last_entry.get("traveltime")

    @property
    def is_trip_last_duration_supported(self) -> bool:
        """
        Return true if supported.

        :return:
        """
        response = self.trip_last_entry
        return response and type(response.get("traveltime", None)) in (float, int)

    @property
    def trip_last_length(self):
        """
        Return last trip length.

        :return:
        """
        return self.trip_last_entry.get("mileage")

    @property
    def is_trip_last_length_supported(self) -> bool:
        """
        Return true if supported.

        :return:
        """
        response = self.trip_last_entry
        return response and type(response.get("mileage", None)) in (float, int)

    @property
    def trip_last_recuperation(self):
        """
        Return last trip recuperation.

        :return:
        """
        # Not implemented
        return self.trip_last_entry.get("recuperation")

    @property
    def is_trip_last_recuperation_supported(self) -> bool:
        """
        Return true if supported.

        :return:
        """
        # Not implemented
        response = self.trip_last_entry
        return response and type(response.get("recuperation", None)) in (float, int)

    @property
    def trip_last_average_recuperation(self):
        """
        Return last trip total recuperation.

        :return:
        """
        value = self.trip_last_entry.get("averageRecuperation")
        return float(value / 10)

    @property
    def is_trip_last_average_recuperation_supported(self) -> bool:
        """
        Return true if supported.

        :return:
        """
        response = self.trip_last_entry
        return response and type(response.get("averageRecuperation", None)) in (float, int)

    @property
    def trip_last_total_electric_consumption(self):
        """
        Return last trip total electric consumption.

        :return:
        """
        # Not implemented
        return self.trip_last_entry.get("totalElectricConsumption")

    @property
    def is_trip_last_total_electric_consumption_supported(self) -> bool:
        """
        Return true if supported.

        :return:
        """
        # Not implemented
        response = self.trip_last_entry
        return response and type(response.get("totalElectricConsumption", None)) in (float, int)

    # Status of set data requests
    @property
    def refresh_action_status(self):
        """Return latest status of data refresh request."""
        return self._requests.get("refresh", {}).get("status", "None")

    @property
    def charger_action_status(self):
        """Return latest status of charger request."""
        return self._requests.get("batterycharge", {}).get("status", "None")

    @property
    def climater_action_status(self):
        """Return latest status of climater request."""
        return self._requests.get("climatisation", {}).get("status", "None")

    @property
    def pheater_action_status(self):
        """Return latest status of parking heater request."""
        return self._requests.get("preheater", {}).get("status", "None")

    @property
    def lock_action_status(self):
        """Return latest status of lock action request."""
        return self._requests.get("lock", {}).get("status", "None")

    # Requests data
    @property
    def refresh_data(self):
        """Get state of data refresh."""
        if self._requests.get("refresh", {}).get("id", False):
            return True

    @property
    def is_refresh_data_supported(self):
        """Return true, as data refresh is always supported."""
        return True

    @property
    def request_in_progress(self):
        """Request in progress is always supported."""
        try:
            for section in self._requests:
                if self._requests[section].get("id", False):
                    return True
        except Exception:
            pass
        return False

    @property
    def is_request_in_progress_supported(self):
        """Request in progress is always supported."""
        return True

    @property
    def request_results(self):
        """Get last request result."""
        data = {"latest": self._requests.get("latest", None), "state": self._requests.get("state", None)}
        for section in self._requests:
            if section in ["departuretimer", "batterycharge", "climatisation", "refresh", "lock", "preheater"]:
                data[section] = self._requests[section].get("status", "Unknown")
        return data

    @property
    def is_request_results_supported(self):
        """Request results is supported if in progress is supported."""
        return self.is_request_in_progress_supported

    @property
    def requests_remaining(self):
        """Get remaining requests before throttled."""
        if self.attrs.get("rate_limit_remaining", False):
            self.requests_remaining = self.attrs.get("rate_limit_remaining")
            self.attrs.pop("rate_limit_remaining")
        return self._requests["remaining"]

    @requests_remaining.setter
    def requests_remaining(self, value):
        self._requests["remaining"] = value

    @property
    def is_requests_remaining_supported(self):
        """
        Return true if requests remaining is supperted.

        :return:
        """
        return True if self._requests.get("remaining", False) else False

    # Helper functions #
    def __str__(self):
        """Return the vin."""
        return self.vin

    @property
    def json(self):
        """
        Return vehicle data in JSON format.

        :return:
        """

        def serialize(obj):
            """
            Convert datetime instances back to JSON compatible format.

            :param obj:
            :return:
            """
            return obj.isoformat() if isinstance(obj, datetime) else obj

        return to_json(OrderedDict(sorted(self.attrs.items())), indent=4, default=serialize)
