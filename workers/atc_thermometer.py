import time

from mqtt import MqttMessage

from workers.base import BaseWorker
from utils import booleanize
import logger

REQUIREMENTS = ["bluepy"]
_LOGGER = logger.get(__name__)


class AtcThermometerStatus:
    def __init__(
        self,
        worker,
        mac: str,
        name: str,
        available: bool = False,
        last_status_time: float = None,
        message_sent: bool = True,
    ):
        if last_status_time is None:
            last_status_time = time.time()

        self.worker = worker  # type: Atc_ThermometerWorker
        self.mac = mac.lower()
        self.name = name
        self.available = available
        self.last_status_time = last_status_time
        self.message_sent = message_sent

    def set_status(self, available):
        if available != self.available:
            self.available = available
            self.last_status_time = time.time()
            self.message_sent = False

    def _timeout(self):
        if self.available:
            return self.worker.available_timeout
        else:
            return self.worker.unavailable_timeout

    def has_time_elapsed(self):
        elapsed = time.time() - self.last_status_time
        return elapsed > self._timeout()

    def payload(self):
        if self.available:
            return self.worker.available_payload
        else:
            return self.worker.unavailable_payload

    def generate_messages(self, device):
        messages = []


        messages.append(
            MqttMessage(
                topic=self.worker.format_topic("{}/LWT".format(self.name)),
                    payload=self.payload(),
                    retain=True
                )
            )
        return messages


class Atc_ThermometerWorker(BaseWorker):
    # Default values
    devices = {}
    # Payload that should be send when device is available
    available_payload = "Online"  # type: str
    # Payload that should be send when device is unavailable
    unavailable_payload = "Offline"  # type: str
    # After what time (in seconds) we should inform that device is available (default: 0 seconds)
    available_timeout = 0  # type: float
    # After what time (in seconds) we should inform that device is unavailable (default: 60 seconds)
    unavailable_timeout = 60  # type: float
    scan_timeout = 10.0  # type: float
    scan_passive = True  # type: str or bool
    
    def _setup(self):
        self.autoconfCache = {}


    def __init__(self, command_timeout, global_topic_prefix, **kwargs):
        from bluepy.btle import Scanner, DefaultDelegate

        class ScanDelegate(DefaultDelegate):
            def __init__(self):
                DefaultDelegate.__init__(self)

            def handleDiscovery(self, dev, isNewDev, isNewData):
                if isNewDev:
                    _LOGGER.debug("Discovered new device: %s" % dev.addr)

        super(Atc_ThermometerWorker, self).__init__(
            command_timeout, global_topic_prefix, **kwargs
        )
        self.scanner = Scanner().withDelegate(ScanDelegate())
        self.last_status = [
            AtcThermometerStatus(self, mac, name) for name, mac in self.devices.items()
        ]
        _LOGGER.info("Adding %d %s devices", len(self.devices), repr(self))


    def parse_payload(self, device, name):
        messages = []
        if (device):
            data_str = str(device.getValueText(22))
            if data_str[0:4] == '1a18':
                mac = data_str[4:16]
                temp = int(data_str[16:20], 16) / 10
                hum = int(data_str[20:22], 16)
                batt = int(data_str[22:24], 16)
                _LOGGER.debug("Device: %s Temp: %sc Humidity: %s%% Batt: %s%%", mac, temp, hum, batt)
                payload = {"temperature": temp, "humidity": hum, "battery": batt, "rssi": device.rssi}
                messages.append(
                    MqttMessage(
                        topic=self.format_topic("{}/state".format(name)),
                        payload=payload,
                        retain=True
                    )
                )
                messages.append(
                    MqttMessage(
                        topic=self.format_topic("{}/LWT".format(name)),
                        payload=self.available_payload,
                        retain=True
                    )
                )
        return messages


    def get_base_autoconf_data(self, key, name, deviceClass, fieldName, unit):
        properName = name.replace("_", " ").title()
        base = {
            "~": self.format_prefixed_topic(name),
            "state_topic": "~/state",
            "avty_t": "~/LWT",
            "pl_avail": self.available_payload,
            "pl_not_avail": self.unavailable_payload,
            "device":{
                "identifiers":key,
                "name":properName,
                "mf":"Xiaomi",
                "mdl":"LYWSD03MMC",
                "sw":"1.0"
                  },
            "device_class": deviceClass,
            "name": properName + " " + fieldName.capitalize(),
            "uniq_id": key + "_" + fieldName,
            "value_template":"{{ value_json." + fieldName + " | round(1) }}",
            "unit_of_measurement": unit
            }
        return base

    def get_autoconf_data(self, key, name):
        if key in self.autoconfCache:
            return False
        else:
            self.autoconfCache[key] = True
            messages = []
            tempConfig = self.get_base_autoconf_data(key, name, "temperature", "temperature", "°C")
            messages.append(
                    MqttMessage(
                        topic=self.format_topic("{}/temperature/config".format(name)),
                        payload=tempConfig,
                        retain=True
                    )
                )

            humConfig = self.get_base_autoconf_data(key, name, "humidity", "humidity", "%")
            messages.append(
                    MqttMessage(
                        topic=self.format_topic("{}/humidity/config".format(name)),
                        payload=humConfig,
                        retain=True
                    )
                )


            batteryConfig = self.get_base_autoconf_data(key, name, "battery", "battery", "%")
            messages.append(
                    MqttMessage(
                        topic=self.format_topic("{}/battery/config".format(name)),
                        payload=batteryConfig,
                        retain=True
                    )
                )


            rssiConfig = self.get_base_autoconf_data(key, name, "signal_strength", "rssi", "dB")
            messages.append(
                    MqttMessage(
                        topic=self.format_topic("{}/rssi/config".format(name)),
                        payload=rssiConfig,
                        retain=True
                    )
                )

            return messages

    def status_update(self):
        from bluepy import btle

        _LOGGER.info("Updating %d %s devices", len(self.devices), repr(self))

        ret = []

        try:
            devices = self.scanner.scan(
               float(self.scan_timeout), passive=booleanize(self.scan_passive)
            )
            mac_addresses = {device.addr: device for device in devices}

            for status in self.last_status:
                device = mac_addresses.get(status.mac, None)
                status.set_status(device is not None)
                ret += status.generate_messages(device)
                ret += self.parse_payload(device, status.name)

                uniqueId = status.mac.replace(':', '', 5)

                autoconf_data = self.get_autoconf_data(uniqueId, status.name)
                if autoconf_data != False:
                    _LOGGER.info("Autoconfiguring %s %s", uniqueId, status.name)
                    ret += autoconf_data


        except btle.BTLEException as e:
            logger.log_exception(
                _LOGGER,
                "Error during update (%s)",
                repr(self),
                type(e).__name__,
                suppress=True,
            )

        return ret
