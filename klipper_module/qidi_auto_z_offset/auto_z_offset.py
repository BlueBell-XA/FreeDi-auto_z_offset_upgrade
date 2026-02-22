# QIDI Auto Z-Offset support
#
# Copyright (C) 2026  Nicholas Coe (BlueBell-XA)
# Copyright (C) 2024  Joe Maples <joe@maples.dev>
# Copyright (C) 2021  Dmitry Butyugin <dmbutyugin@google.com>
# Copyright (C) 2017-2021  Kevin O'Connor <kevin@koconnor.net>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

from . import probe
from . import manual_probe


# ---------------------------------------------------------------------------
# Cross-version compatibility helpers
# ---------------------------------------------------------------------------
# Klipper probe positions changed format between versions:
#   Old (-2025): plain 3-element lists/tuples [x, y, z]
#   New (2026+): ProbeResult namedtuple (bed_x, bed_y, bed_z,
#                                        test_x, test_y, test_z)
# These helpers let the rest of the code work with either format.

def _get_pos_z(position):
    """Extract the z / bed_z value from any probe position format."""
    if hasattr(position, 'bed_z'):
        return position.bed_z
    return position[2]


def _get_pos_xyz(position):
    """Extract (x, y, z) from any probe position format."""
    if hasattr(position, 'bed_x'):
        return position.bed_x, position.bed_y, position.bed_z
    return position[0], position[1], position[2]


def _adjust_pos_z_offset(position, z_offset):
    """
    Return a copy of *position* with *z_offset* subtracted from the z field.
    Preserves the original type where possible (ProbeResult, tuple, etc.).
    """
    # ProbeResult namedtuple — use _replace for a clean field update
    if hasattr(position, '_replace') and hasattr(position, 'bed_z'):
        try:
            return position._replace(bed_z=position.bed_z - z_offset)
        except (TypeError, ValueError):
            pass
    # Generic fallback — rebuild with modified index 2
    items = list(position)
    if len(items) > 2:
        items[2] -= z_offset
    try:
        return type(position)(*items)
    except TypeError:
        return tuple(items)


def _get_probe_xy_offsets(probe_obj):
    """Get (x_offset, y_offset) from a probe, handling API variations."""
    if hasattr(probe_obj, 'probe_offsets'):
        off = probe_obj.probe_offsets
        return off.x_offset, off.y_offset
    try:
        offsets = probe_obj.get_offsets()
        return offsets[0], offsets[1]
    except (TypeError, IndexError, AttributeError):
        return 0.0, 0.0


# ---------------------------------------------------------------------------
# Z stepper current helper
# ---------------------------------------------------------------------------
class ZStepperCurrentHelper:
    """Printer-agnostic helper for temporarily reducing Z stepper motor currents.

    Discovers all TMC-driven Z steppers at init time (works with any TMC model)
    and provides methods to reduce and restore their run_current values.
    """
    def __init__(self, config, probe_current_factor=0.5):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.probe_current_factor = probe_current_factor
        # Populated once the printer is ready and TMC objects are registered
        self._z_steppers = {}   # {stepper_name: tmc_object}
        self._saved_currents = {}  # {stepper_name: run_current}
        self._is_reduced = False  # Tracks whether current is currently reduced
        self._hold = False         # When True, restore() becomes a no-op (held by outer loop)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _handle_ready(self):
        """Discover all TMC-driven Z steppers and cache their default run_current."""
        for name, obj in self.printer.lookup_objects():
            parts = name.split()
            if (len(parts) == 2
                    and parts[0].startswith('tmc')
                    and parts[1].startswith('stepper_z')):
                stepper_name = parts[1]
                self._z_steppers[stepper_name] = obj
                run_current = obj.get_status(None).get('run_current', None)
                if run_current is not None:
                    self._saved_currents[stepper_name] = run_current

    def reduce(self):
        """Reduce all Z stepper run_currents by the configured factor.
        No-op if already reduced (i.e. managed by an outer calibration loop)."""
        if self._is_reduced:
            return
        for stepper_name, nominal in self._saved_currents.items():
            reduced = nominal * self.probe_current_factor
            self.gcode.run_script_from_command(
                "SET_TMC_CURRENT STEPPER=%s CURRENT=%.3f" % (stepper_name, reduced)
            )
        self._is_reduced = True

    def restore(self):
        """Restore all Z stepper run_currents to their original values.
        No-op if held by an outer calibration loop (skip_restore is set)."""
        if not self._is_reduced or self._hold:
            return
        for stepper_name, nominal in self._saved_currents.items():
            self.gcode.run_script_from_command(
                "SET_TMC_CURRENT STEPPER=%s CURRENT=%.3f" % (stepper_name, nominal)
            )
        self._is_reduced = False


# ---------------------------------------------------------------------------
# Main implementation - Modified from Joe's original source: https://github.com/frap129/qidi_auto_z_offset/blob/main/auto_z_offset.py
# ---------------------------------------------------------------------------
class AutoZOffsetCommandHelper:
    """Auto Z-Offset G-code commands for probing and calibration."""
    def __init__(self, config, mcu_probe, endstop_wrapper):
        self.printer = config.get_printer()
        self.mcu_probe = mcu_probe
        self.endstop_wrapper = endstop_wrapper
        self.name = config.get_name()
        self.gcode = self.printer.lookup_object("gcode")
        self.xy_move_speed = 50 # mm/s, for moving to bed center and probe XY compensation moves
        # Custom variable inits from printer.cfg
        self.z_offset = config.getfloat("z_offset", 0.0)
        self.probe_hop = config.getfloat("probe_hop", 5.0, minval=4.0)
        self.offset_samples = config.getint("offset_samples", 3, minval=1)
        self.calibrated_z_offset = config.getfloat("calibrated_z_offset", 0.0)
        # Derive bed center from stepper config
        self.bed_center_x = config.getsection('stepper_x').getfloat('position_max', note_valid=False) / 2.0
        self.bed_center_y = config.getsection('stepper_y').getfloat('position_max', note_valid=False) / 2.0
        # Z stepper current management (reduces current during bed-sensor probing)
        self.z_current_helper = ZStepperCurrentHelper(config, probe_current_factor=0.33)
        # Probe command data
        self.last_probe_position = self._make_coord(0., 0., 0.)
        self.last_z_result = 0.0

        # Steps we need to take to calculate the true Z offset:
        # 1. Home all axes normally. Whether done with inductive probe or bed sensor, the end result is the same
        #    (within the noise of the probes), provided Homing isn't redone during calibration.
        # 2. Probe Z at a given XY with bed sensor, and record the probe result.
        #    An offset (default 0.2mm) is added to the bed sensor probe result to ensure the nozzle is slightly above the bed.
        # 3. Move the toolhead so that the inductive probe is at the same XY position
        # 4. Probe Z with at same XY using the inductive probe, and record the probe result
        # 5. Subtract the inductive probe result from the bed sensor probe result to get the 
        #    true Z offset of the nozzle from the bed plane.
        # 6. Average multiple runs of the above to reduce noise, and save the result to config 
        #    for future application as a gcode offset. This resulting value should be inverted for Klipper's gcode offset feature.

        # Note: The 'probe result' is the the point at which the probe triggers.
        #       The value of this point is calculated as the kinematic Z height of the toolhead 
        #       'minus' the z_offset value of the [probe] config section.

        # Register G-code commands
        self.gcode.register_command(
            "AUTO_Z_PROBE",
            self.cmd_AUTO_Z_PROBE,
            desc=self.cmd_AUTO_Z_PROBE_help,
        )
        self.gcode.register_command(
            "AUTO_Z_HOME_Z",
            self.cmd_AUTO_Z_HOME_Z,
            desc=self.cmd_AUTO_Z_HOME_Z_help,
        )
        self.gcode.register_command(
            "AUTO_Z_MEASURE_OFFSET",
            self.cmd_AUTO_Z_MEASURE_OFFSET,
            desc=self.cmd_AUTO_Z_MEASURE_OFFSET_help,
        )
        self.gcode.register_command(
            "AUTO_Z_CALIBRATE",
            self.cmd_AUTO_Z_CALIBRATE,
            desc=self.cmd_AUTO_Z_CALIBRATE_help,
        )
        self.gcode.register_command(
            "AUTO_Z_LOAD_OFFSET",
            self.cmd_AUTO_Z_LOAD_OFFSET,
            desc=self.cmd_AUTO_Z_LOAD_OFFSET_help,
        )
        self.gcode.register_command(
            "AUTO_Z_SAVE_GCODE_OFFSET",
            self.cmd_AUTO_Z_SAVE_GCODE_OFFSET,
            desc=self.cmd_AUTO_Z_SAVE_GCODE_OFFSET_help,
        )

    def get_status(self, eventtime):
        """Return status dict for Klipper's object status reporting."""
        return {
            'name': self.name,
            'last_probe_position': self.last_probe_position,
            'last_z_result': self.last_z_result,
        }

    def _move(self, coord, speed):
        """Move the toolhead to a coordinate at the given speed."""
        self.printer.lookup_object('toolhead').manual_move(coord, speed)

    def _make_coord(self, x, y, z):
        """Create a coordinate, compatible across Klipper versions."""
        try:
            return self.gcode.Coord((x, y, z))
        except TypeError:
            return (x, y, z)

    def _move_to_center(self):
        """Move nozzle to the approximate center of the bed, with a safe Z height if currently low."""
        toolhead = self.printer.lookup_object("toolhead")
        current_z = toolhead.get_position()[2]
        target_coord = self._make_coord(self.bed_center_x, self.bed_center_y, max(current_z, self.probe_hop))
        self._move(target_coord, self.xy_move_speed)

    def _lift_probe(self, gcmd):
        toolhead = self.printer.lookup_object("toolhead")
        params = self.mcu_probe.get_probe_params(gcmd)
        current_position = toolhead.get_position()
        current_position[2] += self.probe_hop
        self._move(current_position, params["lift_speed"])

    def _store_z_offset(self, gcmd):
        configfile = self.printer.lookup_object("configfile")
        configfile.set(
            self.name, "calibrated_z_offset", "%.6f" % self.calibrated_z_offset
        )
        gcmd.respond_info(
            "%s: calibrated_z_offset: %.6f\n"
            "The SAVE_CONFIG command will update the printer config file\n"
            "with the above and restart the printer."
            % (self.name, self.calibrated_z_offset)
        )

    # Help commands for each implemented G-code command
    cmd_AUTO_Z_PROBE_help = "Probe Z-height at current XY position using the bed sensors"
    cmd_AUTO_Z_HOME_Z_help = "Home Z using the bed sensors as an endstop"
    cmd_AUTO_Z_MEASURE_OFFSET_help = "Z-Offset measured by the inductive probe after AUTO_Z_HOME_Z"
    cmd_AUTO_Z_CALIBRATE_help = "Set the Z-Offset by averaging multiple runs of AUTO_Z_MEASURE_OFFSET"
    cmd_AUTO_Z_LOAD_OFFSET_help = "Apply the calibrated_z_offset saved in the config file"
    cmd_AUTO_Z_SAVE_GCODE_OFFSET_help = "Save the current gcode offset for z as the new calibrated_z_offset"

    def cmd_AUTO_Z_PROBE(self, gcmd):
        """Probe Z-height at current XY position using the bed sensors"""
        self.z_current_helper.reduce()
        try:
            pos = probe.run_single_probe(self.mcu_probe, gcmd)
        finally:
            self.z_current_helper.restore()
        x, y, z = _get_pos_xyz(pos)
        self.last_z_result = z + self.z_offset
        self.last_probe_position = self._make_coord(x, y, z)
        gcmd.respond_info("%s: Bed sensor measured offset: z=%.6f" % (self.name, self.last_z_result*-1.0))

    def cmd_AUTO_Z_HOME_Z(self, gcmd):
        """Home Z using the bed sensors as an endstop, then apply z_offset config to set the new Z=0 plane."""
        self._move_to_center()
        self.cmd_AUTO_Z_PROBE(gcmd)
        toolhead = self.printer.lookup_object("toolhead")
        current_position = toolhead.get_position()
        toolhead.set_position(
            [current_position[0], current_position[1], self.z_offset, current_position[3]], homing_axes=(0, 1, 2)
        )
        self._lift_probe(gcmd)

    def cmd_AUTO_Z_MEASURE_OFFSET(self, gcmd):
        # Use bed sensors to find Z at bed center, with small z_offset added to ensure nozzle is above bed.
        self._move_to_center()
        self.cmd_AUTO_Z_PROBE(gcmd)
        self._lift_probe(gcmd)

        # Move inductive probe to position previously proved
        inductive_probe = self.printer.lookup_object("probe")
        toolhead = self.printer.lookup_object("toolhead")
        coord = toolhead.get_position()
        x_offset, y_offset = _get_probe_xy_offsets(inductive_probe)
        coord[0] = self.bed_center_x - x_offset
        coord[1] = self.bed_center_y - y_offset
        self._move(coord, self.xy_move_speed)

        # Find Z at same XY with inductive probe, and calculate true Z offset by subtracting from bed sensor result
        position = probe.run_single_probe(inductive_probe, gcmd)
        inductive_probe_result_z = _get_pos_z(position)
        true_offset = self.last_z_result - inductive_probe_result_z  # Subtract inductive probe measurement from bed sensor measurement
        gcmd.respond_info("%s: Calculated true nozzle offset: z=%.6f" % (self.name, true_offset*-1.0))
        self._lift_probe(gcmd)
        return true_offset

    def cmd_AUTO_Z_CALIBRATE(self, gcmd):
        """Set the Z-Offset by averaging multiple runs of AUTO_Z_MEASURE_OFFSET"""
        offset_total = 0.0
        self.gcode.run_script_from_command("SET_GCODE_OFFSET Z=0 MOVE=0")   # Clear any existing offsets to ensure clean measurements

        # Run prepare_gcode and reduce Z current once for the entire calibration
        self.endstop_wrapper.run_prepare_gcode()
        self.endstop_wrapper.skip_prepare = True
        self.z_current_helper.reduce()
        self.z_current_helper._hold = True
        try:
            # Measure true offset multiple times and average to reduce noise.
            for _ in range(self.offset_samples):
                offset_total += self.cmd_AUTO_Z_MEASURE_OFFSET(gcmd)
        finally:
            self.z_current_helper._hold = False
            self.z_current_helper.restore()
            self.endstop_wrapper.skip_prepare = False
        self._move_to_center()
        average_offset = offset_total / self.offset_samples
        self.calibrated_z_offset = average_offset * -1.0  # Invert the offset for Klipper's gcode offset convention

        # Apply calibrated offset and write to config
        self.cmd_AUTO_Z_LOAD_OFFSET(gcmd)
        self._store_z_offset(gcmd)

    def cmd_AUTO_Z_LOAD_OFFSET(self, gcmd):
        """Apply the calibrated_z_offset saved in the config file"""
        self.gcode.run_script_from_command("SET_GCODE_OFFSET Z=%f MOVE=0" % self.calibrated_z_offset)
        gcmd.respond_info("%s: Applied calibrated_z_offset: %.6f" % (self.name, self.calibrated_z_offset))

    def cmd_AUTO_Z_SAVE_GCODE_OFFSET(self, gcmd):
        """Save the current gcode offset if changed through baby-stepping as the new calibrated_z_offset"""
        gcode_move = self.printer.lookup_object("gcode_move")
        self.calibrated_z_offset = gcode_move.homing_position[2]
        self._store_z_offset(gcmd)


class HomingViaAutoZHelper(probe.HomingViaProbeHelper):
    """Helper class to manage homing using the bed sensors as a temporary virtual endstop."""
    # Can't use super() here due to multiple registration of chip (theorised, not tested), so re-implement init with necessary setup.
    def __init__(self, config, mcu_probe, param_helper):
        self.printer = config.get_printer()
        self.mcu_probe = mcu_probe
        self.param_helper = param_helper
        self.multi_probe_pending = False
        self.probe_offsets = AutoZOffsetOffsetsHelper(config)
        self.z_min_position = probe.lookup_minimum_z(config)
        self.results = []
        probe.LookupZSteppers(config, self.mcu_probe.add_stepper)
        # Register z_virtual_endstop pin to the one set within the probe config, so it can be used as a homing endstop
        self.printer.lookup_object("pins").register_chip("auto_z_offset", self)
        self.printer.register_event_handler(
            "homing:homing_move_begin", self._handle_homing_move_begin
        )
        self.printer.register_event_handler(
            "homing:homing_move_end", self._handle_homing_move_end
        )
        self.printer.register_event_handler(
            "homing:home_rails_begin", self._handle_home_rails_begin
        )
        self.printer.register_event_handler(
            "homing:home_rails_end", self._handle_home_rails_end
        )
        self.printer.register_event_handler(
            "gcode:command_error", self._handle_command_error
        )

# TODO: Check and cleanup
class AutoZOffsetEndstopWrapper:
    """Wrapper class to adapt the probe as a temporary endstop for homing, with optional acceleration control."""
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object("gcode")
        self.probe_accel = config.getfloat("probe_accel", 0.0, minval=0.0)
        self.probe_wrapper = probe.ProbeEndstopWrapper(config)
        # Setup prepare_gcode
        gcode_macro = self.printer.load_object(config, "gcode_macro")
        self.prepare_gcode = gcode_macro.load_template(config, "prepare_gcode")
        self.skip_prepare = False  # When True, multi_probe_begin skips prepare_gcode
        # Wrappers
        self.get_mcu = self.probe_wrapper.get_mcu
        self.add_stepper = self.probe_wrapper.add_stepper
        self.get_steppers = self.probe_wrapper.get_steppers
        self.home_start = self.probe_wrapper.home_start
        self.home_wait = self.probe_wrapper.home_wait
        self.query_endstop = self.probe_wrapper.query_endstop
        self.multi_probe_end = self.probe_wrapper.multi_probe_end

    def run_prepare_gcode(self):
        """Explicitly run the prepare_gcode template."""
        self.gcode.run_script_from_command(self.prepare_gcode.render())

    def multi_probe_begin(self):
        if not self.skip_prepare:
            self.run_prepare_gcode()
        self.probe_wrapper.multi_probe_begin()

    def probe_prepare(self, hmove):
        toolhead = self.printer.lookup_object("toolhead")
        self.probe_wrapper.probe_prepare(hmove)
        if self.probe_accel > 0.0:
            systime = self.printer.get_reactor().monotonic()
            toolhead_info = toolhead.get_status(systime)
            self.old_max_accel = toolhead_info["max_accel"]
            self.gcode.run_script_from_command("M204 S%.3f" % self.probe_accel)

    def probe_finish(self, hmove):
        if self.probe_accel > 0.0:
            self.gcode.run_script_from_command("M204 S%.3f" % self.old_max_accel)
        self.probe_wrapper.probe_finish(hmove)


class AutoZOffsetParameterHelper(probe.ProbeParameterHelper):
    """Inherits all probe parameter handling from upstream Klipper probe.py."""
    def __init__(self, config):
        super().__init__(config)


# TODO: Check and cleanup
class AutoZOffsetSessionHelper(probe.ProbeSessionHelper):
    """Helper class to manage the state and logic of a multi-sample probing session for a single command"""
    def __init__(self, config, param_helper, start_session_cb):
        self.printer = config.get_printer()
        self.probe_z_offset = self.printer.lookup_object("probe").get_offsets()[2]
        self.param_helper = param_helper
        self.start_session_cb = start_session_cb
        # Session state
        self.hw_probe_session = None
        self.results = []
        # Register event handlers
        self.printer.register_event_handler(
            "gcode:command_error", self._handle_command_error
        )

    def run_probe(self, gcmd):
        if self.hw_probe_session is None:
            self._probe_state_error()
        params = self.param_helper.get_probe_params(gcmd)
        toolhead = self.printer.lookup_object("toolhead")
        probexy = toolhead.get_position()[:2]
        retries = 0
        positions = []
        sample_count = params["samples"]
        while len(positions) < sample_count:
            # Probe position
            pos = self._probe(gcmd)
            positions.append(pos)
            # Check samples tolerance (use helper for z access)
            z_positions = [_get_pos_z(p) for p in positions]
            if max(z_positions) - min(z_positions) > params["samples_tolerance"]:
                if retries >= params["samples_tolerance_retries"]:
                    raise gcmd.error("Probe samples exceed samples_tolerance")
                gcmd.respond_info("Probe samples exceed tolerance. Retrying...")
                retries += 1
                positions = []
            # Retract using actual toolhead position (not probe result z)
            if len(positions) < sample_count:
                cur_z = toolhead.get_position()[2]
                toolhead.manual_move(
                    probexy + [cur_z + params["sample_retract_dist"]],
                    params["lift_speed"],
                )
        # Discard highest and lowest values (sort by z explicitly)
        positions.sort(key=_get_pos_z)
        positions = positions[1:-1]
        # Subtract main probe z_offset (preserves position type)
        positions = [_adjust_pos_z_offset(p, self.probe_z_offset)
                     for p in positions]
        # Calculate result
        epos = probe.calc_probe_z_average(positions, params["samples_result"])
        self.results.append(epos)

# TODO: Check and cleanup
class AutoZOffsetOffsetsHelper:
    """Helper class to provide consistent probe offsets across different Klipper versions."""
    def __init__(self, config):
        self.z_offset = config.getfloat("z_offset", 0.0)

    def get_offsets(self, gcmd=None):
        return 0.0, 0.0, self.z_offset

    # TODO: This is the only time manual_probe is used, but the function doesn't seem to be called. Investigate further.
    def create_probe_result(self, test_pos):
        """Create a probe result, with fallback for older Klipper versions."""
        try:
            return manual_probe.ProbeResult(
                test_pos[0], test_pos[1],
                test_pos[2]-self.z_offset,
                test_pos[0], test_pos[1], test_pos[2])
        except (AttributeError, TypeError):
            # Fallback: older Klipper without ProbeResult
            return (test_pos[0], test_pos[1],
                    test_pos[2]-self.z_offset)


class AutoZOffsetProbe:
    """Main class for the AUTO_Z_OFFSET module, responsible for managing the probing and commands."""
    def __init__(self, config):
        self.printer = config.get_printer()
        self.mcu_probe = AutoZOffsetEndstopWrapper(config)
        self.cmd_helper = AutoZOffsetCommandHelper(config, self, self.mcu_probe)
        self.probe_offsets = AutoZOffsetOffsetsHelper(config)
        self.param_helper = AutoZOffsetParameterHelper(config)
        self.homing_helper = HomingViaAutoZHelper(
            config, self.mcu_probe, self.param_helper
        )
        self.probe_session = AutoZOffsetSessionHelper(
            config, self.param_helper, self.homing_helper.start_probe_session
        )

    def get_probe_params(self, gcmd=None):
        return self.param_helper.get_probe_params(gcmd)

    def get_offsets(self, gcmd=None):
        return self.probe_offsets.get_offsets(gcmd)

    def get_status(self, eventtime):
        return self.cmd_helper.get_status(eventtime)

    def start_probe_session(self, gcmd):
        return self.probe_session.start_probe_session(gcmd)


def load_config(config):
    """Klipper module load function. The entry point for this plugin."""
    auto_z_offset = AutoZOffsetProbe(config)
    config.get_printer().add_object("auto_z_offset", auto_z_offset)
    return auto_z_offset