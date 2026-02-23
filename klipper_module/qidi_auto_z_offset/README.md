# Auto Z-Offset — Remastered

A Klipper plugin that uses the piezo-electric bed sensors found on QIDI printers to automatically determine the Z-offset correction needed for the inductive probe — compensating for the difference between where the probe thinks the bed is and where it actually is.

> [!NOTE]
> This is a ground-up rewrite of the original `auto_z_offset` module. It does **not** inherit from Klipper's `probe.py`, making it resilient to upstream Klipper API changes. It is designed as a drop-in replacement — the same G-code commands and config section name are used.

> [!IMPORTANT]
> This module rewrite is ready for the early stages of **beta testing**. At the time of writing, it has only been tested on the QIDI Plus4. The code should be compatible with other printers, provided the assumptions below are met. **Please read this entire document before proceeding.** Ensure the assumptions are true for your printer, take caution during testing, and watch the printer closely at all times.

> [!CAUTION]
> **Disclaimer:** This software is provided as-is, without warranty of any kind. By installing and using this module, you accept full responsibility for any consequences, including but not limited to damage to your printer, print bed, nozzle, or other components. The author(s) are not responsible for any damage that may result from using this module. **Test at your own risk.**

---

## Prerequisites & Assumptions

All of the following must be true for this module to work correctly. **If any of these are not met, do not proceed.**

### Required

1. **`[probe]` section exists in your `printer.cfg`.**
   The module reads the inductive probe's XY and Z offsets at startup, and fires the `PROBE` G-code command during calibration. Without a `[probe]` section, Klipper will error on startup.

2. **The inductive probe is defined as the Z virtual endstop.**
   Your `[stepper_z]` section must contain:
   ```ini
   endstop_pin: probe:z_virtual_endstop
   ```
   This means Z homing (`G28 Z`) is performed via the inductive probe. The module's offset math assumes the Z coordinate system was established by homing through the inductive probe.

3. **Your printer has piezo-electric or load-cell bed sensors.**
   These are the force-sensing sensors built into the print bed on supported QIDI printers (Plus4, Q1 Pro, and similar models). The module uses these sensors as a separate endstop to detect when the nozzle physically contacts the bed.

4. **You know the MCU pin for the bed sensor signal** (see [Understanding the Pin Configuration](#understanding-the-pin-configuration) below).

5. **`position_max` is set on both `[stepper_x]` and `[stepper_y]`.**
   The module calculates bed center from these values. If they are missing or incorrect, probing will occur at the wrong location.

6. **The bed sensor has a power/enable control pin** (typically `[output_pin bed_sensor]`).
   Most QIDI printers require the bed sensor to be powered on before it can trigger. This is handled via `SET_PIN` commands in the `prepare_gcode`. If your bed sensor is always-on, adjust `prepare_gcode` accordingly.

### Recommended

7. **Z steppers use TMC drivers** (e.g. TMC2209, TMC2240).
   The module automatically discovers all TMC-driven Z steppers and temporarily reduces their run current during bed-sensor probing (controlled by `z_current_factor`). This makes the probe more sensitive and protects the bed. If no TMC drivers are found, the current reduction is silently skipped — probing still works, but with full motor force against the bed.

8. **The `[smart_effector]` section is not present** (or is commented out).
   `[smart_effector]` registers itself as the `probe` object in Klipper, which would replace the standard inductive probe. If present, the module would read offsets from the smart effector instead of your inductive probe, and the `PROBE` command behavior may differ.

### Before Every Calibration Run

9. **All axes must be homed** (`G28`) before running any `AUTO_Z_*` commands. The module does not check homing state — running it unhomed will produce garbage results or crash the toolhead.

10. **The nozzle must be clean.** Any filament stuck to the nozzle tip will change the trigger point of the bed sensor, giving an incorrect offset. Heat the nozzle to a non-oozing temperature (e.g. 140–160 °C) and wipe it before calibrating.

11. **The bed surface must be clear.** Remove any debris, filament scraps, or foreign objects from the center of the bed where probing takes place.

---

## Understanding the Pin Configuration

The `[auto_z_offset]` section requires **one pin** — the bed sensor **signal** pin. This is easily confused with the bed sensor **power** pin, so read this section carefully.

There are **two separate pins** involved:

| Pin | Purpose | Where it goes |
|---|---|---|
| **Bed sensor signal pin** | The endstop input the MCU monitors to detect when the bed sensor triggers. | `pin:` in `[auto_z_offset]` |
| **Bed sensor power pin** | Turns the bed sensor on/off. Controlled via `SET_PIN` in `prepare_gcode`. | `pin:` in `[output_pin bed_sensor]` |

### How to find your bed sensor signal pin

The easiest way is to look at the **existing** `[auto_z_offset]` section in your current `printer.cfg`. The `pin:` value from the old config is the same signal pin you need for the new config.

For stock FreeDi-based QIDI printers:

| Printer | Signal pin (`[auto_z_offset]`) | Power pin (`[output_pin bed_sensor]`) |
|---|---|---|
| Plus4 | `PC1` | `!PA14` |
| Q1 Pro | `PC1` | `!PA14` |

> [!NOTE]
> If your `printer.cfg` uses an MCU prefix (e.g. `U_1:PC1`), you must include the same prefix. The prefix depends on which MCU the bed sensor is wired to. Check your existing config for the correct prefix.

> [!WARNING]
> **Do not confuse these two pins.** Setting the signal pin incorrectly will cause the bed sensor to never trigger (or trigger immediately), which can result in the nozzle crashing into the bed. If in doubt, copy the `pin:` value directly from your existing `[auto_z_offset]` section.

### The `[output_pin bed_sensor]` section

This section controls the **power** to the bed sensor. It must also be present in your `printer.cfg` if your bed sensor requires power control (all known QIDI models do):

```ini
[output_pin bed_sensor]
pin: !PA14          # Power/enable pin — NOT the signal pin
pwm: false
shutdown_value: 0
value: 0
```

The `prepare_gcode` in `[auto_z_offset]` uses `SET_PIN PIN=bed_sensor VALUE=1` to power on the sensor before probing and `SET_PIN PIN=bed_sensor VALUE=0` to power it off. If this section is missing or the pin is wrong, the bed sensor will never activate.

---

## Installation & Testing

> [!CAUTION]
> **Read the entire [Prerequisites & Assumptions](#prerequisites--assumptions) section before starting.** Incorrect pin configuration or missing prerequisites can result in the nozzle crashing into the bed.

### Step 1 — Back Up Your Current Config

Before making any changes, save a copy of your current `printer.cfg` so you can revert if needed.

1. Open your web UI (Fluidd or Mainsail).
2. Navigate to your `printer.cfg`.
3. **Select all** the contents and paste them into a text file on your computer. Save it somewhere safe.

### Step 2 — Replace the Module File

1. SSH into your printer.
2. Navigate to the module directory and remove the original file:
   ```bash
   cd ~/FreeDi/klipper_module/qidi_auto_z_offset
   ```
   ```bash
   rm auto_z_offset.py
   ```
3. Download or paste the new module file:
   ```bash
   nano auto_z_offset.py
   ```
   Right-click in the terminal window to paste the full contents of the new `auto_z_offset.py` from this repo. Then press `Ctrl + O`, `Enter` to save, and `Ctrl + X` to exit.

> [!TIP]
> Alternatively, you can use `wget` or `scp` to transfer the file directly if you prefer not to copy-paste.

4. Restart Klipper so the new module file is picked up:
   ```bash
   sudo service klipper restart
   ```

### Step 3 — Update Your `printer.cfg`

1. Open your web UI and navigate to `printer.cfg`.
2. **Comment out** the entire old `[auto_z_offset]` section by adding `#` at the start of every line.
3. Add a new `[auto_z_offset]` section. See the [Example Configuration](#example-configuration) at the end of this document.

> [!IMPORTANT]
> **Critical items to get right:**
> - `pin:` — must be the **bed sensor signal pin** from your old config (see [Understanding the Pin Configuration](#understanding-the-pin-configuration)).
> - `[output_pin bed_sensor]` — must already exist in your config with the correct **power pin**. If it was part of your old setup, leave it as-is.
> - `prepare_gcode:` — this must power on the bed sensor and jog Z to settle the piezo sensors. Copy the pattern from the example, adjusting only if your printer requires different behavior.
> - Remove any old parameters that no longer exist (e.g. `calibrated_z_offset`). Refer to the [Config Reference](#config-reference) for the full list of valid parameters.

4. **Save** the config and **restart** the printer (firmware restart).

### Step 4 — Verify Klipper Starts Cleanly

After restarting, check the Klipper log for errors:
- In your web UI, check the console for any error messages.
- If Klipper fails to start, review the error message carefully.

### Step 5 — First Calibration (Supervised)

> [!CAUTION]
> **Keep your hand on the emergency stop (power switch or E-stop button) at all times during the first calibration.** If the nozzle moves toward the bed and the sensor does not trigger, you must kill power immediately to prevent damage.

1. **Heat the nozzle** to a non-oozing temperature (e.g. 160 °C) and **heat the bed** to 50 °C. Wait for both to stabilise.
2. **Clean the nozzle tip** — wipe away any filament residue.
3. **Clear the bed surface** — remove any debris from the center of the bed.
4. **Home all axes:**
   ```
   G28
   ```
5. **Run a single measurement first** (not the full calibration) to verify everything works:
   ```
   AUTO_Z_MEASURE_OFFSET
   ```
   Watch the printer closely and **be ready to test the bed sensor manually**:
   - After the `prepare_gcode` completes and the nozzle begins descending toward the bed, **quickly and forcefully tap the print bed with your finger**.
   - **If the bed retreats (or the nozzle rises)** — the bed sensor is working correctly. The tap simulated a trigger and the probe responded. Let the process continue.
   - **If nothing happens when you tap** — the bed sensor is not triggering. **Kill power immediately** before the nozzle crashes into the bed. Review your pin configuration (see [Understanding the Pin Configuration](#understanding-the-pin-configuration)).
   - If it errors with "bed sensor is already triggered" — the sensor is stuck on or the pin is inverted. Check wiring and pin polarity.
   - If it errors with "bed sensor did not trigger" — the sensor didn't fire before reaching the safety floor. Check that the bed sensor power pin is correct and that `prepare_gcode` is enabling it.

6. **If the single measurement succeeded**, run the full calibration:
   ```
   AUTO_Z_CALIBRATE
   ```

### Step 6 — Verify the Offset (Safely)

After calibration completes, you need to verify the offset is correct before printing.

> [!WARNING]
> **Do not move the nozzle directly to Z=0 in one fast move.** If the offset is wrong, the nozzle could crash into the bed.

1. **Lower the nozzle in small increments**, watching carefully:
   ```
   G1 Z5 F300
   G1 Z2 F300
   G1 Z1 F300
   G1 Z0.5 F300
   G1 Z0.2 F300
   G1 Z0 F300
   ```
   At each step, visually check the gap between the nozzle and the bed. **If the nozzle looks like it is going to contact the bed before reaching Z=0, stop immediately.**

2. At Z=0, slide a piece of paper under the nozzle. You should feel light resistance — the paper should move but with a slight drag.

3. If the offset needs fine-tuning, use baby-stepping in your web UI to adjust, then save:
   ```
   AUTO_Z_SAVE_GCODE_OFFSET
   SAVE_CONFIG
   ```

---

### Feedback

For any issues or bugs identified, please raise them here: [Issues](https://github.com/BlueBell-XA/FreeDi-auto_z_offset_upgrade/issues)

For general comments, feel free to message me on the FreeDi Discord group — @BlueBell-XA

### Reverting the Changes

If you need to go back to the original module:

1. Go to your update manager in the web UI, then **check for updates**.
2. The FreeDi repo should be reported as **DIRTY**, allowing you to perform a **soft recovery** on it.
3. Revert your `printer.cfg` to the backup you saved in Step 1.
4. Restart the printer.

---

## How It Works

1. **Probe the bed with the bed sensor** — the nozzle descends until the piezo bed sensor triggers. The raw toolhead Z position at the trigger point is recorded.
2. **Probe the bed with the inductive probe** — the toolhead repositions so the inductive probe is over bed center, then runs a standard `PROBE` command. The raw toolhead Z position is recorded.
3. **Calculate the correction** — the difference between the two Z readings, plus a configurable air-gap (`z_offset`), gives the error in the inductive probe's reference plane — i.e. how far off the probe's Z-offset is from reality.
4. **Average / median multiple runs** — when calibrating, multiple measure cycles are performed and reduced using the configured method to minimise noise.
5. **Save & apply** — the result is stored as `probe_z_correction` and applied as a G-code Z offset to compensate for the probe's error.

No probe result objects, `z_offset` arithmetic from Klipper's probe infrastructure, or class inheritance are involved. The module reads the toolhead position directly after each probe event.

---

## Command Reference

| Command | Description |
|---|---|
| `AUTO_Z_PROBE` | Probe Z-height at the current XY position using the bed sensors. Returns the raw trigger Z. |
| `AUTO_Z_HOME_Z` | Move to bed center, probe with the bed sensor, then reset the Z coordinate frame so the trigger point becomes `z_offset`. Lifts afterward. |
| `AUTO_Z_MEASURE_OFFSET` | Perform a full measurement cycle: bed-sensor probe → lift → inductive probe → compute offset. Returns the calculated offset. |
| `AUTO_Z_CALIBRATE` | Run `AUTO_Z_MEASURE_OFFSET` multiple times (`offset_samples`), reduce the results (average/median), apply and save the probe Z correction. |
| `AUTO_Z_LOAD_OFFSET` | Apply the saved `probe_z_correction` as a G-code Z offset. |
| `AUTO_Z_SAVE_GCODE_OFFSET` | Save the current live G-code Z offset (e.g. after baby-stepping) as the new `probe_z_correction`. |

---

## Basic Usage

> [!WARNING]
> `AUTO_Z_CALIBRATE` should **not** be called from a `PRINT_START` macro. On rare occasions the bed sensors can fail to trigger or trigger late, which could grind the nozzle into the bed. Calibrate separately, then use `AUTO_Z_LOAD_OFFSET` in `PRINT_START`.

### Calibration

1. Heat the nozzle to 160 °C and the bed to 50 °C.
2. Clean the nozzle tip and the bed surface.
3. Home all axes (`G28`).
4. Run `AUTO_Z_CALIBRATE`.
5. **Verify the offset safely** — lower the nozzle to Z=0 in small increments (see [Step 6](#step-6--verify-the-offset-safely) above). Check with a piece of paper.
6. Adjust with baby-stepping if needed, then run `AUTO_Z_SAVE_GCODE_OFFSET`.
7. Run `SAVE_CONFIG` to persist.

### During Printing

Add `AUTO_Z_LOAD_OFFSET` to your `PRINT_START` macro to apply the saved offset each print. If you baby-step during a print, save the adjustment afterward with `AUTO_Z_SAVE_GCODE_OFFSET` and `SAVE_CONFIG`.

---

## Config Reference

> [!WARNING]
> Setting `z_current_factor` too low or `speed` too high can cause the Z stepper motors to skip steps — particularly on the retract move after probing. If you hear grinding or notice Z position drift, increase `z_current_factor` or reduce `speed`.

```ini
[auto_z_offset]
pin:
#   MCU pin connected to the bed sensor SIGNAL output (endstop input).
#   This is the pin the MCU monitors to detect when the bed sensor triggers.
#   This is NOT the bed sensor power pin (that goes in [output_pin bed_sensor]).
#   Copy this value from your existing [auto_z_offset] section.
#   Required.

prepare_gcode:
#   G-code script to run before each bed-sensor probe (or once at the start
#   of a calibration run). Typically used to toggle the bed sensor power pin
#   and jog Z to settle the piezo sensors.
#   Required.

#z_offset: 0.2
#   Air-gap compensation added to every offset measurement. This is NOT the
#   inductive probe's z_offset — it accounts for the small distance between
#   the bed sensor trigger point and true bed contact.
#   Default: 0.2

#speed: 5.0
#   Speed (mm/s) for downward probing moves (both bed sensor and inductive).
#   Must be greater than 0.
#   Default: 5.0

#lift_speed: 20.0
#   Speed (mm/s) for upward retract/lift moves.
#   Must be greater than 0.
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
#   This is distinct from probe_hop. Minimum: 1.0
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

#z_current_factor: 0.6
#   Factor (0.3–1.0) to multiply Z stepper motor run_current by during
#   bed-sensor probing. Lower values make the probe more sensitive but
#   risk skipped steps on the retract. Automatically discovers all
#   TMC-driven Z steppers. If no TMC drivers are found this setting has
#   no effect.
#   Default: 0.6

#probe_z_correction: 0.0
#   The stored probe Z correction. This is the difference between where the
#   inductive probe thinks the bed is and where it actually is, as measured
#   by the bed sensor. Written automatically by AUTO_Z_CALIBRATE and
#   AUTO_Z_SAVE_GCODE_OFFSET. Do not edit manually unless you know what
#   you are doing.
#   Default: 0.0
```

---

## Example Configuration

> [!IMPORTANT]
> The pin names shown below are for the **QIDI Plus4** (and similar models like Q1 Pro). Your printer may use different pin names or MCU prefixes. **Always cross-reference with your existing `printer.cfg`.**

This example shows the three related config sections: the bed sensor power control, the inductive probe, and the auto z-offset section.

```ini
# ---- Bed sensor power control ----
# This pin turns the bed sensor on and off.
# The prepare_gcode in [auto_z_offset] uses SET_PIN to control this.
[output_pin bed_sensor]
pin: !PA14          # Power/enable pin for the bed sensor
pwm: false
shutdown_value: 0
value: 0

# ---- Inductive probe ----
# Standard Klipper probe section. The module reads offsets from here.
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

# ---- Auto Z-Offset ----
[auto_z_offset]
pin: PC1                # Bed sensor SIGNAL pin (endstop input, NOT the power pin)
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

> [!NOTE]
> The `prepare_gcode` above first powers off the bed sensor, jogs Z back and forth to settle the piezo sensors, then powers the sensor on. This sequence is printer-specific — if your printer behaves differently, adjust accordingly. The key requirement is that the bed sensor must be **powered on** by the end of `prepare_gcode`.
