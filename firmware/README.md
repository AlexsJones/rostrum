# TX-26 firmware

The TX-26 voice unit: a Teensy 4.0 on a PJRC Audio Adaptor Rev D, with an electret capsule and a
push-to-talk switch. It plugs in as a standard USB microphone called **TX-26** (no drivers on
Linux, macOS or Windows) and is silent unless the switch is held. The switch is also reported over
USB serial, for Rostrum to turn into key presses.

## Wiring

| From | To | Notes |
|---|---|---|
| Teensy 4.0 | Audio Adaptor Rev D | Stacked: the Teensy on top, its headers soldered through the adaptor. The adaptor needs 3V, GND, 7, 8, 18, 19, 20, 21, 23. |
| Capsule (SparkFun COM-28383) | Adaptor MIC and GND pads | The leg whose pad has traces to the metal can goes to GND. Reversed, it gives near silence. |
| Switch | Teensy pins 2 and 3 | Pin 3 is driven low as the switch's ground. |
| Lamp | pin 4 → 330 Ω → LED long leg; short leg → GND | Glows dimly when on, bright while transmitting. |

## Build and flash

Needs `arduino-cli` with PJRC's Teensy core, and PJRC's udev rule to flash (once, with sudo:
`00-teensy.rules` from the core's `tools/teensy-tools/<version>/` into `/etc/udev/rules.d/`).

```sh
arduino-cli config add board_manager.additional_urls https://www.pjrc.com/teensy/package_teensy_index.json
arduino-cli core install teensy:avr
arduino-cli compile --fqbn teensy:avr:teensy40:usb=serialmidiaudio firmware/tx26
arduino-cli upload  --fqbn teensy:avr:teensy40:usb=serialmidiaudio -p usb3/3-5 firmware/tx26   # port: arduino-cli board list
```

USB type **Serial + MIDI + Audio** is the only official Teensy mode with both audio and serial;
none has audio and a keyboard.

## Serial protocol

| Line | Meaning |
|---|---|
| `PTT 1` / `PTT 0` | switch pressed / released (debounced 5 ms) |
| `LEVEL 0.123` | the mic's peak level over the last 250 ms, 0..1, switch held or not |
| `GAIN 44` (sent) | set the mic preamp, 0..63 dB; answered with `GAIN 44`. Back to 44 at power-off. |

## Bench diagnostics

For checking a new build, each prints a line a second:

- `diag`: whether the audio chip answers, audio blocks and level, and how pin 2 behaves
- `diag2`: continuity of the chip's control lines (18, 19) through the adaptor's pull-ups, and an I2C scan
- `diag3`: loops a tone through the chip's digital lines (tests 7, 8, 20, 21, 23), alternating with the mic
