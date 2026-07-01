# chihiros-ble-proxy

ESPHome firmware for an ESP32-S3 that bridges Chihiros aquarium devices to Home Assistant over BLE.

## Supported devices

| Device | BLE name prefix | YAML variable |
|---|---|---|
| WRGB II LED light | `DYNT90`, `DYSIL` | `light_mac` |
| CO2 controller | `DYPCO2` | `co2_mac` |
| Magnetic stirrer (4-channel) | `DYMIX` | `stirrer_mac` |
| Dosing pump (4-channel) | `DYDOSE` | `dosing_mac` |
| Cooling fan | `DYNFAN` | `fan_mac` |
| Doctor Mate (EC/TDS) | `DYNDOC` | `doctor_mac` |

## How it works

Chihiros devices store their own schedule and run fully autonomously — no permanent connection is needed. The ESP32 only connects over BLE to push a config update, then immediately disconnects. This keeps BLE stable and avoids connection conflicts.

```
Home Assistant  ──(ESPHome API)──▶  ESP32-S3  ──(BLE NUS)──▶  Chihiros device
```

## Hardware

- **Board:** ESP32-S3-DevKitC-1 (16 MB flash)
- **Framework:** ESP-IDF

Any ESP32-S3 with BLE should work; adjust `board:` in the YAML if needed.

## Setup

### 1. Configure secrets

```bash
cp secrets.yaml.example secrets.yaml
```

Edit `secrets.yaml`:
```yaml
wifi_ssid: "your-wifi-ssid"
wifi_password: "your-wifi-password"
encryption_key: ""   # generate: openssl rand -base64 32
ota_password: "your-ota-password"
```

### 2. Configure modules in `chihiros-ble-proxy.yaml`

Open the `substitutions` block at the top of the file. The only thing you need to change is the `*_enabled` flags — set modules you don't have to `"false"`:

```yaml
substitutions:
  co2_enabled:     "false"   # set "true" if you have a CO2 controller
  stirrer_enabled: "true"
  light_enabled:   "true"
  dosing_enabled:  "true"
  fan_enabled:     "true"
  doctor_enabled:  "false"   # set "true" if you have a Doctor Mate
```

MAC addresses default to `00:00:00:00:00:00`, which enables **auto-discovery**: the ESP32 connects to the first Chihiros device of each type it finds over BLE. This works out of the box as long as you have at most one device of each type within range.

**Pinning a MAC** is only necessary if you have multiple devices of the same type (e.g. two WRGB2 lights in the same room). In that case, find the MAC with `scan_ble.py` (see below) and set it:

```yaml
  light_mac: "EA:BC:4C:62:58:B8"   # pin to a specific unit
```

### 3. Flash

```bash
esphome run chihiros-ble-proxy.yaml
```

### Finding MAC addresses (if needed)

Install the BLE scanner and run it on your laptop while the devices are powered on:

```bash
sudo pacman -S python-bleak   # Arch/Manjaro — or: pip install bleak

sudo python3 scan_ble.py
```

The scanner shows a live table of nearby BLE devices. Chihiros devices are highlighted in green with the corresponding YAML variable name. Press `Enter` on a device to open a BLE sniffer that shows raw packets with decoded Chihiros protocol fields.

Alternatively, flash the firmware first and check the ESP32 logs — any Chihiros device found during scanning is logged with its MAC and type.

## Home Assistant entities

After adding the device in Home Assistant (Settings → Devices & Services → ESPHome), you get:

**Light (WRGB II)**
- Switch: `Light auto mode` — auto schedule vs. manual control
- Numbers: `Light schedule red/green/blue` (%), `Light schedule ramp` (min)
- Numbers: `Light red/green/blue (manual)` (%) — only used when auto mode is off
- Buttons: `Light apply schedule`, `Light RTC sync`
- Datetime: `Light period start / end`

**CO2 controller**
- Switch: `CO2 schedule enabled`
- Number: `CO2 pre-start (min)` — how many minutes before lights-on CO2 turns on
- Buttons: `CO2 apply schedule`, `CO2 RTC sync`

**Magnetic stirrer (4 channels)**
- Switches: `Stirrer channel 0–3` (on/off)
- Numbers per channel: speed, start hour/minute, lead time, duration
- Buttons: `Stirrer apply schedule`, `Stirrer RTC sync`

**Dosing pump (4 channels)**
- Switches: `Dosing pump 1–4 schedule` (enable/disable schedule)
- Numbers per pump: volume (mL), hour, minute, weekdays bitmask
- Buttons: `Dosing pump 1–4` (manual dose trigger)

**Cooling fan**
- Switch: `Fan silent mode`
- Numbers: `Fan start temperature`, `Fan max cooling temperature`, `Fan manual speed`
- Sensors: `Aquarium water temperature`, `Aquarium room temperature`, `Humidity`, `Fan speed`

**Doctor Mate (EC/TDS)**
- Select: `Doctor Mate profile` (Plant / Fish / Shrimp / Manual)
- Numbers: `Doctor Mate TDS`, `Doctor Mate tank volume`
- Sensor: `Doctor Mate EC`

## BLE protocol

All Chihiros devices use the **Nordic UART Service (NUS)**:

| Role | UUID |
|---|---|
| Service | `6e400001-b5a3-f393-e0a9-e50e24dcca9e` |
| TX (write) | `6e400002-b5a3-f393-e0a9-e50e24dcca9e` |
| RX (notify) | `6e400003-b5a3-f393-e0a9-e50e24dcca9e` |

Frame format: `[0x5a or 0xa5] 0x01 [len] 0x00 [seq] [cmd] [data...] [XOR-CRC]`

The full protocol implementation is in `components/chihiros_ble/chihiros_ble.h`.

## Troubleshooting

**Device not found / not connecting**
- Make sure the device is powered on and within BLE range (~5–10 m).
- Check the ESP32 logs (`esphome logs chihiros-ble-proxy.yaml`). On first boot with auto-discovery, you should see `Auto-discovered <type>: <MAC>` within a few seconds of the device advertising.
- If you see `Chihiros found: … -> set as light_mac` in the logs, the device is visible but the module is either disabled or auto-discovery has already latched onto a different device. Check the `*_enabled` flags and consider pinning the MAC.

**Wrong device picked up in auto-discovery**
- If two devices of the same type are in range, auto-discovery connects to whichever advertises first. Pin the correct one by setting its MAC.

**HCI 0x07 Memory Full errors in logs**
- Happens when multiple BLE connects are attempted simultaneously. The firmware staggers connects automatically. If it persists, increase the delays in `schedule_debounce_both`.

**Device not recognized / wrong BLE name prefix**
- Chihiros occasionally ships devices under new BLE name prefixes (e.g. a new WRGB2 variant advertising as `DYNEW` instead of `DYNT90`). If a device is powered on and in range but neither `scan_ble.py` nor the ESP32 logs identify it as a Chihiros device, check the raw BLE name shown in the scanner and compare it against the prefix lists in two places:
  1. `scan_ble.py` — the `CHIHIROS_PREFIXES` dict near the top of the file
  2. `chihiros-ble-proxy.yaml` — the `on_ble_advertise` lambda (one `rfind` check per device type)
- Add the new prefix to both, then re-flash and re-run the scanner.

## Credits

Protocol implementation and device knowledge based on [chihiros-esphome](https://github.com/BartdeJonge/chihiros-esphome) by Bart de Jonge.

## License

MIT
