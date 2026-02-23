# Auto Z-Offset — Remastered

A Klipper plugin that uses the piezo-electric bed sensors found on QIDI printers to automatically determine the Z-offset correction needed for the inductive probe — compensating for the difference between where the probe thinks the bed is and where it actually is.

> [!IMPORTANT]
> This is a ground-up rewrite of the original `auto_z_offset` module. It does **not** inherit from Klipper's `probe.py`, making it resilient to upstream Klipper API changes. It is designed as a drop-in replacement — the same G-code commands and config section name are used.

## How It Works

1. **Probe the bed with the bed sensor** — the nozzle descends until the piezo bed sensor triggers. The raw toolhead Z position at the trigger point is recorded.
2. **Probe the bed with the inductive probe** — the toolhead repositions so the inductive probe is over bed center, then runs a standard `PROBE` command. The raw toolhead Z position is recorded.
3. **Calculate the correction** — the difference between the two Z readings, plus a configurable air-gap (`z_offset`), gives the error in the inductive probe's reference plane — i.e. how far off the probe's Z-offset is from reality.
4. **Average / median multiple runs** — when calibrating, multiple measure cycles are performed and reduced using the configured method to minimise noise.
5. **Save & apply** — the result is stored as `probe_z_correction` and applied as a G-code Z offset to compensate for the probe's error.

No probe result objects, `z_offset` arithmetic from Klipper's probe infrastructure, or class inheritance are involved. The module reads the toolhead position directly after each probe event.

## Command Reference

| Command | Description |
|---|---|
| `AUTO_Z_PROBE` | Probe Z-height at the current XY position using the bed sensors. Returns the raw trigger Z. |
| `AUTO_Z_HOME_Z` | Move to bed center, probe with the bed sensor, then reset the Z coordinate frame so the trigger point becomes `z_offset`. Lifts afterward. |
| `AUTO_Z_MEASURE_OFFSET` | Perform a full measurement cycle: bed-sensor probe → lift → inductive probe → compute offset. Returns the calculated offset. |
| `AUTO_Z_CALIBRATE` | Run `AUTO_Z_MEASURE_OFFSET` multiple times (`offset_samples`), reduce the results (average/median), apply and save the probe Z correction. |
| `AUTO_Z_LOAD_OFFSET` | Apply the saved `probe_z_correction` as a G-code Z offset. |
| `AUTO_Z_SAVE_GCODE_OFFSET` | Save the current live G-code Z offset (e.g. after baby-stepping) as the new `probe_z_correction`. |

## Basic Usage

> [!WARNING]
> `AUTO_Z_CALIBRATE` should **not** be called from a `PRINT_START` macro. On rare occasions the bed sensors can fail to trigger or trigger late, which could grind the nozzle into the bed. Calibrate separately, then use `AUTO_Z_LOAD_OFFSET` in `PRINT_START`.

### Calibration

1. Heat the nozzle to a non-oozing temperature (e.g. 160 °C).
2. Home all axes (`G28`).
3. Run `AUTO_Z_CALIBRATE`.
4. Move Z to 0 and verify a piece of paper slides under the nozzle with light resistance.
5. Adjust with baby-stepping if needed, then run `AUTO_Z_SAVE_GCODE_OFFSET`.
6. Run `SAVE_CONFIG` to persist.

### During Printing

Add `AUTO_Z_LOAD_OFFSET` to your `PRINT_START` macro to apply the saved offset each print. If you baby-step during a print, save the adjustment afterward with `AUTO_Z_SAVE_GCODE_OFFSET` and `SAVE_CONFIG`.

## Config Reference

```ini
[auto_z_offset]
pin:
#   MCU pin connected to the bed sensor signal. Required.

prepare_gcode:
#   G-code script to run before each bed-sensor probe (or once at the start
#   of a calibration run). Typically used to toggle the bed sensor power pin
#   and jog Z to settle the piezo sensors. Required.

#z_offset: 0.2
#   Air-gap compensation added to every offset measurement. This is NOT the
#   inductive probe's z_offset — it accounts for the small distance between
#   the bed sensor trigger point and true bed contact.
#   Default: 0.2

#speed: 5.0
#   Speed (mm/s) for downward probing moves (both bed sensor and inductive).
#   Default: 5.0

#lift_speed: 20.0
#   Speed (mm/s) for upward retract/lift moves.
#   Default: 20.0

#probe_hop: 4.0
#   Distance (mm) to lift between the bed-sensor probe and the inductive
#   probe, and after each measurement cycle. Minimum 4.0 to avoid
#   prematurely triggering the inductive probe.
#   Default: 4.0

#probe_accel: 0
#   If set, temporarily limits acceleration (mm/s²) during bed-sensor
#   probing moves to reduce vibration-induced false triggers.
#   0 = disabled (use printer's current max_accel).
#   Default: 0

#probe_z_min:
#   Explicit safety floor (mm) for bed-sensor probing moves — the lowest Z
#   the toolhead is allowed to travel to. If not set, falls back to
#   [stepper_z] position_min. Clamped to an absolute minimum of -10 mm
#   regardless of the value provided.
#   Default: (stepper_z position_min)

#offset_samples: 3
#   Number of full measure cycles (bed sensor + inductive) to perform during
#   AUTO_Z_CALIBRATE. Results are reduced using samples_result.
#   Default: 3

#samples: 1
#   Number of times to probe each sensor per measurement point. When > 1,
#   tolerance checking and retract logic apply.
#   Default: 1

#sample_retract_dist: 3.0
#   Distance (mm) to retract between individual samples (when samples > 1).
#   This is distinct from probe_hop.
#   Default: 3.0

#samples_result: average
#   How to reduce multiple samples and multiple offset_samples: 'average'
#   or 'median'. Case-insensitive. An error is raised at startup for any
#   other value.
#   Default: average

#samples_tolerance: 0.1
#   Maximum allowed spread (mm) between samples in a single probe point.
#   If exceeded, the sample set is discarded and retried.
#   Default: 0.1

#samples_tolerance_retries: 0
#   Number of times to retry a sample set that exceeds samples_tolerance.
#   0 = error immediately on first tolerance failure.
#   Default: 0

#z_current_factor: 0.33
#   Factor (0.1–1.0) to multiply Z stepper motor run_current by during
#   bed-sensor probing. Lower values make the probe more sensitive but
#   risk skipped steps on the retract. Automatically discovers all
#   TMC-driven Z steppers.
#   Default: 0.33

#probe_z_correction: 0.0
#   The stored probe Z correction. This is the difference between where the
#   inductive probe thinks the bed is and where it actually is, as measured
#   by the bed sensor. Written automatically by AUTO_Z_CALIBRATE and
#   AUTO_Z_SAVE_GCODE_OFFSET. Do not edit manually unless you know what
#   you are doing.
#   Default: 0.0
```

## Example Configuration

This example includes the bed sensor control pin, the inductive probe, and the auto z-offset section. Adjust pin names and offsets for your specific printer model.

```ini
[output_pin bed_sensor]
pin: !U_1:PA14
value: 0

[probe]
pin: !gpio21
x_offset: 17.6
y_offset: 4.4
z_offset: 0.0
speed: 10
samples: 3
samples_result: average
sample_retract_dist: 4.0
samples_tolerance: 0.05
samples_tolerance_retries: 5

[auto_z_offset]
pin: U_1:PC1
z_offset: 0.2
speed: 5
lift_speed: 20
probe_accel: 50
probe_hop: 10
probe_z_min: -2
offset_samples: 4
samples: 1
sample_retract_dist: 5
samples_result: average
samples_tolerance: 0.025
samples_tolerance_retries: 1
z_current_factor: 0.33
prepare_gcode:
    G90
    G0 Z3
    G91
    SET_PIN PIN=bed_sensor VALUE=0
    M400
    {% set i = 30 %}
    {% for iteration in range(i|int) %}
        G1 Z0.6 F7000
        G1 Z-0.6 F7000
    {% endfor %}
    G1 Z3
    M400
    G90
    SET_PIN PIN=bed_sensor VALUE=1
```

## Key Differences from the Original

| Aspect | Original (`auto_z_offset.py`) | Remastered (`auto_z_offset.py`) |
|---|---|---|
| Klipper probe dependency | Inherits `ProbeEndstopWrapper`, `HomingViaProbeHelper`, `ProbeSessionHelper`, `ProbeParameterHelper` | None — uses `pins.setup_pin('endstop', ...)` and `homing.probing_move()` directly |
| Probe result handling | Uses Klipper's `ProbeResult` / position tuples with `z_offset` subtraction | Reads `toolhead.get_position()[2]` directly — no probe result objects |
| Z virtual endstop | Registers `auto_z_offset:z_virtual_endstop` chip | Not needed — bed sensor endstop is used only for probing, not homing rails |
| Compatibility | Breaks when Klipper changes probe internals (e.g. old→new `ProbeResult` format) | Resilient — only depends on stable Klipper primitives (`homing`, `pins`, `toolhead`) |
| New config options | — | `lift_speed`, `z_current_factor`, `probe_z_min` |
| Safety | Relies on `position_min` only | Pre-flight endstop query, hard -10 mm floor clamp, descriptive error messages |

## Credits

- **Joe** — original `auto_z_offset` concept ([frap129/qidi_auto_z_offset](https://github.com/frap129/qidi_auto_z_offset))
- **Nicholas (BlueBell-XA)** — remastered implementation
