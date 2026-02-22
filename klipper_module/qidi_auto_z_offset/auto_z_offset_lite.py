# QIDI Auto Z-Offset — Remastered
#
# Copyright (C) 2026  Nicholas Coe (BlueBell-XA)
#
# This file may be distributed under the terms of the GNU GPLv3 license.
#
# Credit goes to Joe Maples for the original Auto Z-Offset module, which inspired this rewrite.
# https://github.com/frap129/qidi_auto_z_offset/blob/main/auto_z_offset.py
#
# How it works:
#   1. Probe the bed with the bed sensor → read toolhead Z (raw trigger height)
#   2. Probe the bed with the inductive probe (via PROBE gcode) → read toolhead Z
#   3. Difference = true nozzle offset from the inductive probe's trigger plane
#   4. Average multiple runs, add in an air-gap offset, and save result
#
# No probe results or z_offset arithmetic involve.
# Inheritance minimised to avoid breakage when Klipper internals change.


# ---------------------------------------------------------------------------
# Z stepper current helper
# ---------------------------------------------------------------------------
class ZStepperCurrentHelper:
    """Printer-agnostic helper for temporarily reducing Z stepper motor currents.

    Discovers all TMC-driven Z steppers at init time (works with any TMC model)
    and provides methods to reduce and restore their run_current values.
    """
    def __init__(self, config, factor=0.33):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.factor = factor
        self._saved = {}      # stepper_name → nominal_run_current
        self._is_reduced = False
        self._hold = False    # when True, restore() is a no-op (held by outer loop)
        self.printer.register_event_handler('klippy:ready', self._on_ready)

    def _on_ready(self):
        """Discover all TMC-driven Z steppers and cache their default run_current."""
        for name, obj in self.printer.lookup_objects():
            parts = name.split()
            if (len(parts) == 2
                    and parts[0].startswith('tmc')
                    and parts[1].startswith('stepper_z')):
                cur = obj.get_status(None).get('run_current')
                if cur is not None:
                    self._saved[parts[1]] = cur

    def reduce(self):
        """Reduce all Z stepper run_currents by the configured factor."""
        if self._is_reduced:
            return
        for name, cur in self._saved.items():
            self.gcode.run_script_from_command(
                'SET_TMC_CURRENT STEPPER=%s CURRENT=%.3f' % (name, cur * self.factor))
        self._is_reduced = True

    def restore(self):
        """Restore all Z stepper run_currents to their original values."""
        if not self._is_reduced or self._hold:
            return
        for name, cur in self._saved.items():
            self.gcode.run_script_from_command(
                'SET_TMC_CURRENT STEPPER=%s CURRENT=%.3f' % (name, cur))
        self._is_reduced = False


# ---------------------------------------------------------------------------
# Main module
# ---------------------------------------------------------------------------
class AutoZOffset:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.gcode = self.printer.lookup_object('gcode')
        self.name = config.get_name()

        # ---- Config -------------------------------------------------------
        self.z_offset = config.getfloat('z_offset', 0.0)
        self.probe_hop = config.getfloat('probe_hop', 5.0, minval=4.0)
        self.probe_speed = config.getfloat('speed', 5.0, above=0.)
        self.lift_speed = config.getfloat('lift_speed', self.probe_speed, above=0.)
        self.probe_accel = config.getfloat('probe_accel', 0.0, minval=0.0)
        self.offset_samples = config.getint('offset_samples', 3, minval=1)
        self.calibrated_z_offset = config.getfloat('calibrated_z_offset', 0.0)
        self.xy_speed = 50.

        # Bed center
        self.center_x = config.getsection('stepper_x').getfloat(
            'position_max', note_valid=False) / 2.
        self.center_y = config.getsection('stepper_y').getfloat(
            'position_max', note_valid=False) / 2.

        # Minimum Z for probe moves (from stepper_z or [printer] config)
        self.z_min = self._lookup_z_min(config)

        # ---- Bed-sensor MCU endstop (direct, no ProbeEndstopWrapper) ------
        ppins = self.printer.lookup_object('pins')
        self.bed_endstop = ppins.setup_pin('endstop', config.get('pin'))
        self.printer.register_event_handler(
            'klippy:mcu_identify', self._attach_z_steppers)

        # ---- Prepare gcode (runs once before bed-sensor probing) ----------
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.prepare_gcode = gcode_macro.load_template(config, 'prepare_gcode')
        self._skip_prepare = False

        # ---- Helpers ------------------------------------------------------
        self.z_current = ZStepperCurrentHelper(config, factor=0.33)
        self.last_bed_z = 0.

        # ---- Inductive probe XY offsets (resolved at ready time) ----------
        self._ind_x_off = 0.
        self._ind_y_off = 0.
        self.printer.register_event_handler('klippy:ready', self._on_ready)

        # ---- G-code commands ----------------------------------------------
        for cmd, handler, desc in [
            ('AUTO_Z_PROBE',            self.cmd_probe,          'Probe Z with bed sensor'),
            ('AUTO_Z_HOME_Z',           self.cmd_home_z,         'Home Z via bed sensor'),
            ('AUTO_Z_MEASURE_OFFSET',   self.cmd_measure_offset, 'Measure true nozzle Z-offset'),
            ('AUTO_Z_CALIBRATE',        self.cmd_calibrate,      'Multi-sample calibration'),
            ('AUTO_Z_LOAD_OFFSET',      self.cmd_load_offset,    'Apply saved calibrated_z_offset'),
            ('AUTO_Z_SAVE_GCODE_OFFSET',self.cmd_save_offset,    'Save current gcode Z-offset'),
        ]:
            self.gcode.register_command(cmd, handler, desc=desc)

    # ---- Setup helpers ----------------------------------------------------
    @staticmethod
    def _lookup_z_min(config):
        """Return the minimum Z position from stepper or printer config."""
        try:
            from . import manual_probe
            zconfig = manual_probe.lookup_z_endstop_config(config)
            if zconfig is not None:
                return zconfig.getfloat('position_min', 0., note_valid=False)
        except (ImportError, AttributeError):
            pass
        return config.getsection('printer').getfloat(
            'minimum_z_position', 0., note_valid=False)

    def _attach_z_steppers(self):
        """Add every active Z stepper to the bed-sensor endstop."""
        kin = self.printer.lookup_object('toolhead').get_kinematics()
        for s in kin.get_steppers():
            if s.is_active_axis('z'):
                self.bed_endstop.add_stepper(s)

    def _on_ready(self):
        """Cache inductive probe XY offsets once all objects are available."""
        ind = self.printer.lookup_object('probe')
        if hasattr(ind, 'probe_offsets'):
            self._ind_x_off = ind.probe_offsets.x_offset
            self._ind_y_off = ind.probe_offsets.y_offset
        else:
            try:
                off = ind.get_offsets()
                self._ind_x_off, self._ind_y_off = off[0], off[1]
            except (TypeError, IndexError, AttributeError):
                pass

    # ---- Movement primitives ----------------------------------------------
    def _toolhead(self):
        return self.printer.lookup_object('toolhead')

    def _move(self, coord, speed):
        self._toolhead().manual_move(coord, speed)

    def _get_z(self):
        return self._toolhead().get_position()[2]

    def _move_to_center(self):
        z = max(self._get_z(), self.probe_hop)
        self._move([self.center_x, self.center_y, z], self.xy_speed)

    def _lift(self):
        self._move([None, None, self._get_z() + self.probe_hop], self.lift_speed)

    # ---- Low-level probing ------------------------------------------------
    def _probe_bed_sensor(self):
        """Fire bed sensor via probing_move and return raw trigger Z."""
        toolhead = self._toolhead()
        pos = toolhead.get_position()
        pos[2] = self.z_min
        phoming = self.printer.lookup_object('homing')

        old_accel = None
        if self.probe_accel > 0.:
            systime = self.printer.get_reactor().monotonic()
            old_accel = toolhead.get_status(systime)['max_accel']
            self.gcode.run_script_from_command('M204 S%.3f' % self.probe_accel)
        try:
            phoming.probing_move(self.bed_endstop, pos, self.probe_speed)
        finally:
            if old_accel is not None:
                self.gcode.run_script_from_command('M204 S%.3f' % old_accel)
        return self._get_z()

    def _probe_inductive(self):
        """Fire inductive probe via PROBE gcode and return raw trigger Z."""
        self.gcode.run_script_from_command('PROBE SAMPLES=1')
        return self._get_z()

    # ---- G-code commands --------------------------------------------------
    def cmd_probe(self, gcmd):
        """Probe with bed sensor and record raw trigger Z."""
        if not self._skip_prepare:
            self.gcode.run_script_from_command(self.prepare_gcode.render())
        self.z_current.reduce()
        try:
            self.last_bed_z = self._probe_bed_sensor()
        finally:
            self.z_current.restore()
        gcmd.respond_info('%s: bed sensor trigger Z: %.6f'
                         % (self.name, self.last_bed_z))

    def cmd_home_z(self, gcmd):
        """Home Z using bed sensor, then set Z=z_offset (small air-gap)."""
        self._move_to_center()
        self.cmd_probe(gcmd)
        toolhead = self._toolhead()
        p = toolhead.get_position()
        toolhead.set_position(
            [p[0], p[1], self.z_offset, p[3]], homing_axes=(0, 1, 2))
        self._lift()

    def cmd_measure_offset(self, gcmd):
        """Probe with both sensors and return the raw Z difference."""
        self._move_to_center()
        self.cmd_probe(gcmd)
        self._lift()

        # Move inductive probe over the same XY
        coord = self._toolhead().get_position()
        coord[0] = self.center_x - self._ind_x_off
        coord[1] = self.center_y - self._ind_y_off
        self._move(coord, self.xy_speed)

        inductive_z = self._probe_inductive()
        offset = self.last_bed_z - inductive_z
        gcmd.respond_info(
            '%s: true nozzle offset: %.6f  (bed_z=%.6f, inductive_z=%.6f)'
            % (self.name, offset, self.last_bed_z, inductive_z))
        self._lift()
        return offset

    def cmd_calibrate(self, gcmd):
        """Average multiple offset measurements and save result."""
        self.gcode.run_script_from_command('SET_GCODE_OFFSET Z=0 MOVE=0')

        # Prepare / reduce current once for entire loop
        self.gcode.run_script_from_command(self.prepare_gcode.render())
        self._skip_prepare = True
        self.z_current.reduce()
        self.z_current._hold = True
        try:
            total = 0.
            for _ in range(self.offset_samples):
                total += self.cmd_measure_offset(gcmd)
        finally:
            self.z_current._hold = False
            self.z_current.restore()
            self._skip_prepare = False

        self._move_to_center()
        self.calibrated_z_offset = -(total / self.offset_samples)
        self.cmd_load_offset(gcmd)
        self._store(gcmd)

    def cmd_load_offset(self, gcmd):
        """Apply calibrated_z_offset as a gcode Z offset."""
        self.gcode.run_script_from_command(
            'SET_GCODE_OFFSET Z=%f MOVE=0' % self.calibrated_z_offset)
        gcmd.respond_info('%s: applied calibrated_z_offset: %.6f'
                         % (self.name, self.calibrated_z_offset))

    def cmd_save_offset(self, gcmd):
        """Save the current gcode Z offset (e.g. after baby-stepping)."""
        gcode_move = self.printer.lookup_object('gcode_move')
        self.calibrated_z_offset = gcode_move.homing_position[2]
        self._store(gcmd)

    # ---- Persistence ------------------------------------------------------
    def _store(self, gcmd):
        configfile = self.printer.lookup_object('configfile')
        configfile.set(self.name, 'calibrated_z_offset',
                       '%.6f' % self.calibrated_z_offset)
        gcmd.respond_info(
            '%s: calibrated_z_offset: %.6f\n'
            'The SAVE_CONFIG command will update the printer config file\n'
            'with the above and restart the printer.'
            % (self.name, self.calibrated_z_offset))

    def get_status(self, eventtime):
        return {'last_bed_z': self.last_bed_z,
                'calibrated_z_offset': self.calibrated_z_offset}


def load_config(config):
    auto_z = AutoZOffset(config)
    config.get_printer().add_object('auto_z_offset', auto_z)
    return auto_z
