import asyncio
import inspect
import json
import math
import queue
import time
import traceback

import paho.mqtt.client as paho

from bmslib.bms import BmsSample, DeviceInfo, MIN_VALUE_EXPIRY
from bmslib.bt import BtBms
from bmslib.util import get_logger

logger = get_logger()


def round_to_n(x, n):
    if isinstance(x, str) or not math.isfinite(x) or not x:
        return x

    digits = -int(math.floor(math.log10(abs(x)))) + (n - 1)

    try:
        # return ('%.*f' % (digits, x))
        return str(round(x, digits or None))  # digits=0 will output 12.0, digits=None => 12
    except ValueError as e:
        print('error', x, n, e)
        raise e


def remove_none_values(fields: dict):
    for k in list(fields.keys()):
        v = fields[k]
        if v is None:
            del fields[k]
        elif isinstance(v, float):
            if math.isnan(v) or not math.isfinite(v):
                del fields[k]
        elif isinstance(v, str):
            if not v:
                del fields[k]


def build_mqtt_hass_config_discovery(base, topic):
    # Instead of daly_bms should be here added a proper name (unique), like serial or something
    # At this point it can be used only one daly_bms system with hass discovery

    hass_config_topic = f'homeassistant/sensor/{topic}/{base.replace("/", "_")}/config'
    hass_config_data = {}

    hass_config_data["unique_id"] = f'{topic}_{base.replace("/", "_")}'
    hass_config_data["name"] = f'{topic} {base.replace("/", " ")}'

    # see https://www.home-assistant.io/integrations/sensor/

    if 'soc_percent' in base or base.endswith('/soc'):
        hass_config_data["device_class"] = 'battery'
        hass_config_data["unit_of_measurement"] = '%'
    elif 'voltage' in base:
        hass_config_data["device_class"] = 'voltage'
        hass_config_data["unit_of_measurement"] = 'V'
    elif 'current' in base:
        hass_config_data["device_class"] = 'current'
        hass_config_data["unit_of_measurement"] = 'A'
    elif 'power' in base:
        hass_config_data["device_class"] = 'power'
        hass_config_data["unit_of_measurement"] = 'W'
    elif 'capacity' in base or base.endswith('/charge'):
        # hass_config_data["device_class"] = ''
        hass_config_data["unit_of_measurement"] = 'Ah'
    elif 'temperatures' in base:
        hass_config_data["device_class"] = 'temperature'
        hass_config_data["unit_of_measurement"] = '°C'
    else:
        pass

    hass_config_data["json_attributes_topic"] = f'{topic}{base}'
    hass_config_data["state_topic"] = f'{topic}{base}'

    hass_device = {
        "identifiers": [topic],  # daly_bms
        "manufacturer": topic,  # Daly
        "model": 'Currently not available',
        "name": topic,  # Daly BMS
        "sw_version": 'Currently not available'
    }
    hass_config_data["device"] = hass_device

    return hass_config_topic, json.dumps(hass_config_data)


_last_values = {}
_last_publish_time = 0.


def mqtt_single_out(client: paho.Client, topic, data, retain=False):
    # logger.debug(f'Send data: {data} on topic: {topic}, retain flag: {retain}')
    # print('mqtt: ' + topic, data)
    # return

    lv = _last_values.get(topic, None)
    if lv and lv[1] == data and (time.time() - lv[0]) < (MIN_VALUE_EXPIRY / 2):
        logger.debug('topic %s data not changed', topic)
        return False

    mqi: paho.MQTTMessageInfo = client.publish(topic, data, retain=retain)
    if mqi.rc != paho.MQTT_ERR_SUCCESS:
        logger.warning('mqtt publish %s failed: %s %s', topic, mqi.rc, mqi)
        return False

    now = time.time()
    _last_values[topic] = now, data
    global _last_publish_time
    _last_publish_time = now


def mqqt_last_publish_time():
    global _last_publish_time
    return _last_publish_time


def mqtt_iterator(client, result, topic, base='', hass=True):
    for key in result.keys():
        if type(result[key]) == dict:
            mqtt_iterator(client, result[key], topic, f'{base}/{key}', hass)
        else:
            if hass:
                # logger.debug('Sending out hass discovery message')
                topic_, output = build_mqtt_hass_config_discovery(f'{base}/{key}', topic=topic)
                mqtt_single_out(client, topic_, output, retain=True)

            if type(result[key]) == list:
                val = json.dumps(result[key])
            else:
                val = result[key]

            mqtt_single_out(client, f'{topic}{base}/{key}', val)


sample_desc = {
    "soc/total_voltage": {"field": "voltage", "class": "voltage", "unit_of_measurement": "V", "precision": 4,
                          "icon": "meter-electric"},
    "soc/current": {"field": "current", "class": "current", "unit_of_measurement": "A", "precision": 4},
    "soc/balance_current": {"field": "balance_current", "class": "current", "unit_of_measurement": "A", "precision": 4,
                            "icon": "scale-unbalanced"},
    "soc/soc_percent": {"field": "soc", "class": "battery", "unit_of_measurement": "%", "precision": 4,
                        "icon": "battery"},
    "soc/power": {"field": "power", "class": "power", "unit_of_measurement": "W", "precision": 4, "icon": "flash"},
    "soc/capacity": {"field": "capacity", "class": None, "unit_of_measurement": "Ah"},
    "soc/cycle_capacity": {"field": "cycle_capacity", "class": None, "unit_of_measurement": "Ah"},
    "mosfet_status/capacity_ah": {"field": "charge", "class": None, "unit_of_measurement": "Ah"},
    "mosfet_status/temperature": {"field": "mos_temperature", "class": "temperature", "unit_of_measurement": "°C",
                                  "icon": "thermometer"},
    # "switch/charge": # binary sensor
}


def publish_sample(client, device_topic, sample: BmsSample):
    for k, v in sample_desc.items():
        topic = f"{device_topic}/{k}"
        s = round_to_n(getattr(sample, v['field']), v.get('precision', 5))
        mqtt_single_out(client, topic, s)

    if sample.switches:
        for switch_name, switch_state in sample.switches.items():
            topic = f"{device_topic}/switch/{switch_name}"
            mqtt_single_out(client, topic, 'ON' if switch_state else 'OFF')


def publish_cell_voltages(client, device_topic, voltages):
    # "highest_voltage": parts[0] / 1000,
    # "highest_cell": parts[1],
    # "lowest_voltage": parts[2] / 1000,
    # "lowest_cell": parts[3],

    x = range(len(voltages))
    high_i = max(x, key=lambda i: voltages[i])
    low_i = min(x, key=lambda i: voltages[i])

    for i in range(0, len(voltages)):
        topic = f"{device_topic}/cell_voltages/{i + 1}"
        mqtt_single_out(client, topic, voltages[i] / 1000)


def publish_temperatures(client, device_topic, temperatures):
    for i in range(0, len(temperatures)):
        topic = f"{device_topic}/temperatures/{i + 1}"
        mqtt_single_out(client, topic, round_to_n(temperatures[i], 4))


def publish_hass_discovery(client, device_topic, num_cells, num_temp_sensors, expire_after_seconds: int,
                           device_info: DeviceInfo = None, switches=None):
    discovery_msg = {}

    device_json = {
        "identifiers": [(device_info and device_info.sn) or device_topic],
        # "manufacturer": device_topic,  # Daly
        "name": (device_info and device_info.name) or device_topic,
        "model": (device_info and device_info.model) or None,
        "sw_version": (device_info and device_info.sw_version) or None,
        "hw_version": (device_info and device_info.hw_version) or None,
    }

    def _hass_discovery(k, device_class, unit, icon=None):
        dm = {
            "unique_id": f"{device_topic}__{k.replace('/', '_')}",
            "name": f"{device_topic} {k.replace('/', ' ')}",
            "device_class": device_class or None,
            "unit_of_measurement": unit,
            "json_attributes_topic": f"{device_topic}/{k}",
            "state_topic": f"{device_topic}/{k}",
            "expire_after": expire_after_seconds,
            "device": device_json,
        }
        if icon:
            dm['icon'] = 'mdi:' + icon
        remove_none_values(dm)
        remove_none_values(dm['device'])
        discovery_msg[f"homeassistant/sensor/{device_topic}/_{k.replace('/', '_')}/config"] = dm

    for k, d in sample_desc.items():
        _hass_discovery(k, d["class"], unit=d["unit_of_measurement"], icon=d.get('icon', None))

    for i in range(0, num_cells):
        k = 'cell_voltages/%d' % (i + 1)
        _hass_discovery(k, "voltage", unit="V")

    for i in range(0, num_temp_sensors):
        k = 'temperatures/%d' % (i + 1)
        _hass_discovery(k, "temperature", unit="°C")

    if switches:
        for switch_name in switches:
            discovery_msg[f"homeassistant/switch/{device_topic}/{switch_name}/config"] = {
                "unique_id": f"{device_topic}__switch_{switch_name}",
                "name": f"{device_topic} {switch_name}",
                "device_class": 'outlet',
                "json_attributes_topic": f"{device_topic}/{switch_name}",
                "state_topic": f"{device_topic}/switch/{switch_name}",
                # "expire_after": expire_after_seconds,
                "device": device_json,
                "command_topic": f"homeassistant/switch/{device_topic}/{switch_name}/set",
            }

            discovery_msg[f"homeassistant/binary_sensor/{device_topic}/{switch_name}/config"] = {
                "unique_id": f"{device_topic}__switch_{switch_name}",
                "name": f"{device_topic} {switch_name}",
                "device_class": 'outlet',
                "json_attributes_topic": f"{device_topic}/{switch_name}",
                "state_topic": f"{device_topic}/switch/{switch_name}",
                # "expire_after": expire_after_seconds,
                "device": device_json,
                "command_topic": f"homeassistant/switch/{device_topic}/{switch_name}/set",
            }

    for topic, data in discovery_msg.items():
        mqtt_single_out(client, topic, json.dumps(data))


_switch_callbacks = {}
_message_queue = queue.Queue()

async def mqtt_process_action_queue():
    while not _message_queue.empty():
        callback, arg = _message_queue.get(block=False)
        try:
            await callback(arg)
        except Exception as e:
            logger.error('exception in action callback: %s', e)
            logger.error('Stack: %s', traceback.format_exc())


def subscribe_switches(mqtt_client: paho.Client, device_topic, bms: BtBms, switches):
    async def set_switch(switch_name, state):
        await bms.set_switch(switch_name, state)
        topic = f"{device_topic}/switch/{switch_name}"
        mqtt_single_out(mqtt_client, topic, 'ON' if state else 'OFF')


    for switch_name in switches:
        state_topic = f"homeassistant/switch/{device_topic}/{switch_name}/set"
        logger.info("subscribe %s", state_topic)
        mqtt_client.subscribe(state_topic, qos=2)
        _switch_callbacks[state_topic] = \
            lambda msg, switch_name=switch_name: set_switch(switch_name, msg.lower() == "on")


def mqtt_message_handler(client, userdata, message: paho.MQTTMessage):
    payload = message.payload.decode("utf-8")
    logger.info("new message %s: %s", message.topic, payload)
    callback = _switch_callbacks.get(message.topic, None)
    if callback:
        _message_queue.put((callback, payload))
    else:
        logger.warning("No callback for topic %s (payload %s)", message.topic, payload)

    """
    

mqtt: homeassistant/sensor/daly_bms/_status_temperature_sensors/config {"unique_id": "daly_bms__status_temperature_sensors", "name": "Daly BMS  status temperature_sensors", "json_attributes_topic": "daly_bms/status/temperature_sensors", "state_topic": "daly_bms/status/temperature_sensors", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/status/temperature_sensors 1
mqtt: homeassistant/sensor/daly_bms/_status_charger_running/config {"unique_id": "daly_bms__status_charger_running", "name": "Daly BMS  status charger_running", "json_attributes_topic": "daly_bms/status/charger_running", "state_topic": "daly_bms/status/charger_running", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/status/charger_running False
mqtt: homeassistant/sensor/daly_bms/_status_load_running/config {"unique_id": "daly_bms__status_load_running", "name": "Daly BMS  status load_running", "json_attributes_topic": "daly_bms/status/load_running", "state_topic": "daly_bms/status/load_running", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/status/load_running False
mqtt: homeassistant/sensor/daly_bms/_status_states_DI1/config {"unique_id": "daly_bms__status_states_DI1", "name": "Daly BMS  status states DI1", "json_attributes_topic": "daly_bms/status/states/DI1", "state_topic": "daly_bms/status/states/DI1", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/status/states/DI1 False
mqtt: homeassistant/sensor/daly_bms/_status_states_DI2/config {"unique_id": "daly_bms__status_states_DI2", "name": "Daly BMS  status states DI2", "json_attributes_topic": "daly_bms/status/states/DI2", "state_topic": "daly_bms/status/states/DI2", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/status/states/DI2 True
mqtt: homeassistant/sensor/daly_bms/_status_cycles/config {"unique_id": "daly_bms__status_cycles", "name": "Daly BMS  status cycles", "json_attributes_topic": "daly_bms/status/cycles", "state_topic": "daly_bms/status/cycles", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/status/cycles 5
mqtt: homeassistant/sensor/daly_bms/_cell_voltages_1/config {"unique_id": "daly_bms__cell_voltages_1", "name": "Daly BMS  cell_voltages 1", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/cell_voltages/1", "state_topic": "daly_bms/cell_voltages/1", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/cell_voltages/1 3.325
mqtt: homeassistant/sensor/daly_bms/_cell_voltages_2/config {"unique_id": "daly_bms__cell_voltages_2", "name": "Daly BMS  cell_voltages 2", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/cell_voltages/2", "state_topic": "daly_bms/cell_voltages/2", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/cell_voltages/2 3.331
mqtt: homeassistant/sensor/daly_bms/_cell_voltages_3/config {"unique_id": "daly_bms__cell_voltages_3", "name": "Daly BMS  cell_voltages 3", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/cell_voltages/3", "state_topic": "daly_bms/cell_voltages/3", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/cell_voltages/3 3.301
mqtt: homeassistant/sensor/daly_bms/_cell_voltages_4/config {"unique_id": "daly_bms__cell_voltages_4", "name": "Daly BMS  cell_voltages 4", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/cell_voltages/4", "state_topic": "daly_bms/cell_voltages/4", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/cell_voltages/4 3.331
mqtt: homeassistant/sensor/daly_bms/_cell_voltages_5/config {"unique_id": "daly_bms__cell_voltages_5", "name": "Daly BMS  cell_voltages 5", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/cell_voltages/5", "state_topic": "daly_bms/cell_voltages/5", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/cell_voltages/5 3.331
mqtt: homeassistant/sensor/daly_bms/_cell_voltages_6/config {"unique_id": "daly_bms__cell_voltages_6", "name": "Daly BMS  cell_voltages 6", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/cell_voltages/6", "state_topic": "daly_bms/cell_voltages/6", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/cell_voltages/6 3.324
mqtt: homeassistant/sensor/daly_bms/_cell_voltages_7/config {"unique_id": "daly_bms__cell_voltages_7", "name": "Daly BMS  cell_voltages 7", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/cell_voltages/7", "state_topic": "daly_bms/cell_voltages/7", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/cell_voltages/7 3.328
mqtt: homeassistant/sensor/daly_bms/_cell_voltages_8/config {"unique_id": "daly_bms__cell_voltages_8", "name": "Daly BMS  cell_voltages 8", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/cell_voltages/8", "state_topic": "daly_bms/cell_voltages/8", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/cell_voltages/8 3.323
mqtt: homeassistant/sensor/daly_bms/_balancing_status_error/config {"unique_id": "daly_bms__balancing_status_error", "name": "Daly BMS  balancing_status error", "json_attributes_topic": "daly_bms/balancing_status/error", "state_topic": "daly_bms/balancing_status/error", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/balancing_status/error not implemented
mqtt: homeassistant/sensor/daly_bms/_errors/config {"unique_id": "daly_bms__errors", "name": "Daly BMS  errors", "json_attributes_topic": "daly_bms/errors", "state_topic": "daly_bms/errors", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/errors []
    
    mqtt: homeassistant/sensor/daly_bms/_soc_total_voltage/config {"unique_id": "daly_bms__homeassistant/sensor/daly_bms/_soc_total_voltage/config {"unique_id": "daly_bms__soc_total_voltagesoc_total_voltage", "name": "Daly BMS  soc total_voltage", "device_class": "voltage", "unit_of_measurement": "V", "json_attributes_topic": "daly_bms/soc/total_voltage", "state_topic": "daly_bms/soc/total_voltage", "device": {"identifiers": ["daly_bms"], "manufacturer": "Daly", "model": "Currently not available", "name": "Daly BMS", "sw_version": "Currently not available"}}
mqtt: daly_bms/soc/total_voltage 26.5

    
    :return: 
    """


"""
homeassistant/sensor/jbd_bms/_soc_current/config
{"unique_id": "jbd_bms__soc_current", "name": "jbd current", "device_class": "current", "unit_of_measurement": "A", "json_attributes_topic": "jbd_bms/soc/current", "state_topic": "jbd_bms/soc/current", "device": {"identifiers": ["jbd_bms"], "manufacturer": "JBD", "model": "Currently not available", "name": "JBD BMS", "sw_version": "Currently not available"}}
mqtt: daly
"""
