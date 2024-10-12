import json
import sys
import time
import threading
from dataclasses import dataclass
from datetime import timedelta, datetime
from time import sleep

import paho.mqtt.client as mqtt

## SETUP YOUR MQTT SERVER HERE
MQTT_SERVER = "hostname/ip"
MQTT_PORT = 1883
MQTT_USE_AUTH = True
MQTT_USER = "user"
MQTT_PASSWORD = "password"
MAX_CONCURRENT_UPDATES = 1

# DO NOT TOUCH!
otadict = {}
currently_updating = []
sent_request = []
possible_devices = []
init_done = False
nicer_output_flag = False
only_once = True
num_total = 0
last_message_time = time.time()
inactivity_timeout = 300


@dataclass
class OtaDevice:
    friendly_name: str
    ieee_addr: str
    supports_ota: bool
    checked_for_update: bool
    update_available: bool
    updating: bool


def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        client.subscribe("zigbee2mqtt/bridge/devices")
        client.subscribe("zigbee2mqtt/bridge/response/device/ota_update/check")
        client.subscribe("zigbee2mqtt/bridge/response/device/ota_update/update")
    else:
        print(f"Connection failed: {reason_code}")
        sys.exit(1)


def on_message(client, userdata, msg):
    global nicer_output_flag, only_once, otadict, last_message_time
    last_message_time = time.time()
    message = msg.payload.decode("utf-8")
    obj = json.loads(message)
    lower_topic = msg.topic.lower()
    if lower_topic == "zigbee2mqtt/bridge/devices":
        if only_once:
            handle_devicelist(obj)
            only_once = False
    elif lower_topic == "zigbee2mqtt/bridge/response/device/ota_update/check":
        if not nicer_output_flag:
            print("Fetching update responses:")
            nicer_output_flag = True
        handle_otacheck(obj)
    elif lower_topic == "zigbee2mqtt/bridge/response/device/ota_update/update":
        handle_otasuccess(obj)
    else:
        if "update" in obj and obj["update"]:
            device_fn = msg.topic.replace("zigbee2mqtt/", "")
            if "remaining" in obj["update"]:
                remaining_time = timedelta(seconds=obj["update"]["remaining"])
                percent = obj["update"]["progress"]
                current_time = datetime.now().strftime("%H:%M:%S")
                print(
                    f"  {current_time}: Updating '{device_fn}' - {percent:6.2f}%, {remaining_time} remaining"
                )
            elif obj["update"]["state"] == "idle":
                r = [
                    device
                    for device in otadict.values()
                    if device.updating and device.friendly_name == device_fn
                ]
                if r:
                    otacleanup(r[0])


def on_disconnect(client, userdata, reason_code, properties):
    if reason_code != 0:
        print("Connection lost, reconnecting")
        try:
            client.reconnect()
        except Exception as e:
            print(f"Failed to reconnect, error: {e}")
            sys.exit(1)


def handle_devicelist(devicelist):
    print("Looking for supported devices:")
    global otadict, num_total
    for device in devicelist:
        if device.get("definition"):
            dev = OtaDevice(
                device["friendly_name"],
                device["ieee_address"],
                device["definition"].get("supports_ota", False),
                False,
                False,
                False,
            )
            otadict[dev.ieee_addr] = dev
            if dev.supports_ota:
                print(
                    f"  '{dev.friendly_name}' supports OTA Updates, checking for new updates"
                )
                num_total += 1
                check_for_update(dev)


def handle_otacheck(obj):
    global otadict, sent_request, init_done, num_total
    ieee = obj["data"]["id"]
    device = otadict[ieee]
    if ieee in sent_request:
        sent_request.remove(ieee)
    progress = f"[{num_total - len(sent_request)}/{num_total}]"
    if obj["status"] == "ok":
        device.update_available = obj["data"]["updateAvailable"]
        if device.update_available:
            print(f"  {progress} Update is available for '{device.friendly_name}'")
        else:
            print(f"  {progress} No update available for '{device.friendly_name}'")
    else:
        print(f"  {progress} {obj['error']}")
        if obj["error"].startswith("Update or check"):
            start_update(otadict[obj["data"]["id"]])
    if not sent_request:
        init_done = True


def handle_otasuccess(obj):
    global otadict, currently_updating
    if obj["status"] == "error":
        print(obj["error"])
    else:
        name = obj["data"]["id"]
        res = [
            device
            for device in otadict.values()
            if device.friendly_name == name
        ]
        if res:
            otacleanup(res[0])


def otacleanup(dev: OtaDevice):
    global currently_updating, possible_devices
    dev.updating = False
    dev.update_available = False
    currently_updating.remove(dev.ieee_addr)
    print(
        f"Update for '{dev.friendly_name}' finished - {len(possible_devices)} more updates to go"
    )
    client.unsubscribe(f"zigbee2mqtt/{dev.friendly_name}")


def check_for_update(device: OtaDevice):
    global sent_request
    client.publish(
        "zigbee2mqtt/bridge/request/device/ota_update/check",
        payload=json.dumps({"id": device.ieee_addr}),
    )
    sent_request.append(device.ieee_addr)
    device.checked_for_update = True


def start_update(device: OtaDevice):
    global currently_updating
    print(f"Starting Update for '{device.friendly_name}'")
    client.subscribe(f"zigbee2mqtt/{device.friendly_name}")
    client.publish(
        "zigbee2mqtt/bridge/request/device/ota_update/update",
        payload=json.dumps({"id": device.ieee_addr}),
    )
    device.updating = True
    currently_updating.append(device.ieee_addr)


def monitor_inactivity():
    global last_message_time, client
    while True:
        time_since_last_message = time.time() - last_message_time
        if time_since_last_message > inactivity_timeout:
            print(f"Received no new messages in the last {inactivity_timeout} seconds, reconnecting")
            try:
                client.reconnect()
                last_message_time = time.time()
            except Exception as e:
                print(f"Failed to reconnect, error: {e}")
                client.loop_stop()
                client.disconnect()
                sys.exit(1)
        time.sleep(10)


client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
client.on_connect = on_connect
client.on_message = on_message
client.on_disconnect = on_disconnect
if MQTT_USE_AUTH:
    client.username_pw_set(MQTT_USER, MQTT_PASSWORD)

print("Starting initialization")
client.connect(MQTT_SERVER, MQTT_PORT, 60)
client.loop_start()

inactivity_thread = threading.Thread(target=monitor_inactivity, daemon=True)
inactivity_thread.start()

while not init_done:
    sleep(0.1)

print("Finished initialization")

possible_devices = [
    device
    for device in otadict.values()
    if not device.updating and device.update_available
]

print(f"There are updates for {len(possible_devices)} devices")

while possible_devices:
    if len(currently_updating) < MAX_CONCURRENT_UPDATES:
        device = possible_devices.pop()
        start_update(device)
    sleep(5)

while currently_updating:
    sleep(5)

print("Finished updating")