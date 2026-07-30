
# define an empty [switch_probe] to init the module
#
# Each tool has its own switch_probe section
# [switch_probe T0]
# pin: ^EBB0:PB6
# tool: 0
# z_offset: -0.95 # Needs to be calibrated. More positive = More Squish
# speed: 5.0
# samples: 3
# samples_result: median
# sample_retract_dist: 2.0
# samples_tolerance: 0.02
# samples_tolerance_retries: 3
# activate_gcode:
#     _TAP_PROBE_ACTIVATE HEATER=extruder
# flip_trigger: False
#     Set True for tools whose probe pin reads triggered while the tool
#     is seated, to flip the presence-detection logic.
#
# Crash detection is configured on the bare [switch_probe] section
# and is off until START_CRASH_DETECTION is called - see
# SwitchProbeBase for details.
# [switch_probe]
# crash_gcode:
#     M117 Crash detected!
# recover_gcode:
#     M117 Tool recovered, resuming.
# crash_debounce_delay: 0.05
#     Seconds a pin change must persist before it counts. This delays
#     the crash response by the same amount - the toolhead keeps moving
#     meanwhile - so raise it only as far as noise actually requires.
#
# scanner: True
#     Set when a scanning probe (Cartographer, Beacon, ...) is the
#     printer's probe. That scanner does all the probing; every
#     [switch_probe <tool>] section becomes detection-only, needing
#     just 'pin' (plus optional 'tool' and 'flip_trigger'). Any
#     leftover probing options are ignored with a warning in klippy.log.
#     Leave the scanner's own register_as_probe at its default.


import logging
from . import probe

# The only options a scanner-mode tool section actually uses. Everything
# else there is probing config the scanner has taken over.
_SCANNER_TOOL_OPTIONS = frozenset(('pin', 'tool', 'flip_trigger'))

# Feeds buttons.DebounceButton our 'crash_debounce_delay' value, since it
# always reads a config option literally named 'debounce_delay'.
class _DebounceConfig:
    def __init__(self, printer, debounce_delay):
        self.printer = printer
        self.debounce_delay = debounce_delay
    def get_printer(self):
        return self.printer
    def getfloat(self, option, default=0., minval=None):
        # Only stand in for the one option we mean to supply, so any
        # other option DebounceButton may read later still gets its own
        # default rather than silently receiving the debounce delay.
        if option == 'debounce_delay':
            return self.debounce_delay
        return default

# Tool registry and active-tool state machine shared by all backends.
class SwitchProbeBase:
    # Backends that consume every per-tool option leave this False, so an
    # option nothing reads is still reported as an invalid config option.
    # Scanner mode sets it True - see PrinterSwitchProbeScanner.
    _accepts_unused_options = False

    def __init__(self, config):
        self.printer = config.get_printer()
        self.tools = {}
        self.active_tool = None
        self.confirmed = False
        self.detected_tool_number = -1

        self.gcode = self.printer.lookup_object('gcode')
        self.gcode.register_command('SET_ACTIVE_SWITCH_PROBE',
                               self.cmd_SET_ACTIVE_SWITCH_PROBE,
                               desc=self.cmd_SET_ACTIVE_SWITCH_PROBE_help)
        self.gcode.register_command('DETECT_ACTIVE_SWITCH_PROBE',
                               self.cmd_DETECT_ACTIVE_SWITCH_PROBE,
                               desc=self.cmd_DETECT_ACTIVE_SWITCH_PROBE_help)
        self.printer.register_event_handler('klippy:connect',
                                            self._handle_connect)

        self.crash_detection_enabled = False
        self.buttons = self.printer.load_object(config, 'buttons')
        gcode_macro = self.printer.load_object(config, 'gcode_macro')
        self.crash_gcode = None
        if config.get('crash_gcode', None) is not None:
            self.crash_gcode = gcode_macro.load_template(
                config, 'crash_gcode')
        self.recover_gcode = None
        if config.get('recover_gcode', None) is not None:
            self.recover_gcode = gcode_macro.load_template(
                config, 'recover_gcode')
        # One shared shim - every tool's debounce config is identical.
        self.debounce_config = _DebounceConfig(
            self.printer,
            config.getfloat('crash_debounce_delay', 0.05, minval=0.))
        self.gcode.register_command('START_CRASH_DETECTION',
                               self.cmd_START_CRASH_DETECTION,
                               desc=self.cmd_START_CRASH_DETECTION_help)
        self.gcode.register_command('STOP_CRASH_DETECTION',
                               self.cmd_STOP_CRASH_DETECTION,
                               desc=self.cmd_STOP_CRASH_DETECTION_help)

    def _register_tool(self, tool_config, tool):
        if tool.key in self.tools:
            raise tool_config.error(
                "switch_probe: tool '%s' declared twice" % (tool.key,))
        tool.raw_config = {
            o: tool_config.get(o, note_valid=self._accepts_unused_options)
            for o in tool_config.get_prefix_options('')}
        # flip_trigger: True to flip detection logic
        tool.flip_trigger = tool_config.getboolean(
            'flip_trigger', False)
        tool.tool_number = tool_config.getint('tool', None, minval=0)
        tool.crashed = False
        self.tools[tool.key] = tool
        self._register_crash_watcher(tool_config, tool)
        return tool

    def _register_crash_watcher(self, tool_config, tool):
        self.buttons.register_debounce_button(
            tool_config.get('pin'), self._make_crash_callback(tool),
            self.debounce_config)

    def _make_crash_callback(self, tool):
        def callback(eventtime, state):
            self._handle_crash_event(tool, eventtime, state)
        return callback

    def _handle_crash_event(self, tool, eventtime, state):
        if not self.crash_detection_enabled:
            return
        if not self.confirmed or tool.key != self.active_tool:
            return
        present = bool(state) == tool.flip_trigger
        if tool.crashed and present:
            self._handle_recovery(tool)
        elif not tool.crashed and not present:
            self._handle_crash(tool)

    def _handle_crash(self, tool):
        tool.crashed = True
        msg = ("switch_probe: crash detected on tool '%s' (probe pin"
               " no longer reads as seated)" % (tool.key,))
        logging.warning(msg)
        if self.crash_gcode is not None:
            self._run_gcode(self.crash_gcode, 'crash_gcode')
        else:
            self.printer.invoke_shutdown(msg)

    def _handle_recovery(self, tool):
        tool.crashed = False
        logging.info(
            "switch_probe: tool '%s' probe re-seated after a crash"
            % (tool.key,))
        if self.recover_gcode is not None:
            self._run_gcode(self.recover_gcode, 'recover_gcode')

    def _run_gcode(self, template, option_name):
        try:
            self.gcode.run_script(template.render())
        except Exception:
            logging.exception(
                "switch_probe: error running %s" % (option_name,))

    def _default_tool_key(self):
        for tool in self.tools.values():
            if tool.tool_number == 0:
                return tool.key
        raise self.printer.config_error(
            "switch_probe: no tool with 'tool: 0' configured -"
            " a T0 probe is required")

    def _handle_connect(self):
        if not self.tools:
            raise self.printer.config_error(
                "switch_probe: no [switch_probe <tool>]"
                " sections configured")
        self._ensure_active()

    def _ensure_active(self):
        # Forces offsets to be present incase bedmesh, QGL, etc. get 
        # loaded before switch_probe
        if self.active_tool is None:
            self._activate(self._default_tool_key())

    def _get_active_tool(self):
        # No hardware touched, so confirmation isn't required here.
        self._ensure_active()
        return self.tools[self.active_tool]

    def _get_active(self):
        # Hardware accessor (querying/probing): requires the active tool
        # to be confirmed, since it may drive the wrong physical probe.
        tool = self._get_active_tool()
        if not self.confirmed:
            raise self.printer.command_error(
                "switch_probe: active switch probe not confirmed -"
                " call SET_ACTIVE_SWITCH_PROBE TOOL=%s before probing or"
                " homing" % (self.active_tool,))
        return tool

    def _check_can_activate(self, gcmd):
        pass

    def _tool_present(self, tool):
        toolhead = self.printer.lookup_object('toolhead')
        print_time = toolhead.get_last_move_time()
        triggered = bool(tool.mcu_probe.query_endstop(print_time))
        return triggered == tool.flip_trigger, triggered

    def _check_detected(self, gcmd, tool):
        present, triggered = self._tool_present(tool)
        if not present:
            raise gcmd.error(
                "switch_probe: tool '%s' probe not detected (pin"
                " reads %s)" % (
                    tool.key, "triggered" if triggered else "not triggered"))

    def _allow_pin_reuse(self, tool_config):
        # Allow reuse of the probe's pin for tool detection.
        pin_desc = tool_config.get('pin').strip()
        if pin_desc.startswith('^') or pin_desc.startswith('~'):
            pin_desc = pin_desc[1:].strip()
        if pin_desc.startswith('!'):
            pin_desc = pin_desc[1:].strip()
        self.printer.lookup_object('pins').allow_multi_use_pin(pin_desc)

    def get_status(self, eventtime):
        active = self.tools.get(self.active_tool)
        return {
            'active_probe': active.raw_config if active else {},
            'confirmed': self.confirmed,
            'detected_tool_number': self.detected_tool_number,
            'switch_probes': sorted(self.tools),
            'crash_detection_enabled': self.crash_detection_enabled,
            'crashed': active.crashed if active else False,
        }

    cmd_SET_ACTIVE_SWITCH_PROBE_help = (
        "Select which tool's switch probe should be used")
    def cmd_SET_ACTIVE_SWITCH_PROBE(self, gcmd):
        key = gcmd.get('TOOL')
        if key not in self.tools:
            raise gcmd.error(
                "switch_probe: unknown tool '%s' (known: %s)"
                % (key, ", ".join(sorted(self.tools.keys()))))
        self._check_can_activate(gcmd)
        self._check_detected(gcmd, self.tools[key])
        self._activate(key)
        self.confirmed = True

    cmd_DETECT_ACTIVE_SWITCH_PROBE_help = (
        "Scan every configured switch probe and activate whichever one is"
        " physically present")
    def cmd_DETECT_ACTIVE_SWITCH_PROBE(self, gcmd):
        present = [key for key in sorted(self.tools)
                  if self._tool_present(self.tools[key])[0]]
        if len(present) != 1:
            self.detected_tool_number = -1
            if not present:
                gcmd.respond_info("switch_probe: no tool detected")
                return
            raise gcmd.error(
                "switch_probe: multiple tools detected"
                " (%s)" % (", ".join(present),))
        key = present[0]
        self._check_can_activate(gcmd)
        self._activate(key)
        self.confirmed = True
        self.detected_tool_number = self.tools[key].tool_number
        gcmd.respond_info("switch_probe: detected tool '%s'" % (key,))

    cmd_START_CRASH_DETECTION_help = (
        "Treat probe pin transitions on the active, confirmed tool as a"
        " crash until disabled")
    def cmd_START_CRASH_DETECTION(self, gcmd):
        self.crash_detection_enabled = True

    cmd_STOP_CRASH_DETECTION_help = "Disable crash detection"
    def cmd_STOP_CRASH_DETECTION(self, gcmd):
        self.crash_detection_enabled = False


######################################################################
# Klipper backend
######################################################################

# Per tool switch probe helper objects (offsets, params, endstop, session),
# built from one [switch_probe <tool>] section.
class SwitchProbeKlipper:
    def __init__(self, key, section_name, offsets, param_helper, mcu_probe,
                session):
        self.key = key
        self.section_name = section_name
        self.offsets = offsets
        self.param_helper = param_helper
        self.mcu_probe = mcu_probe
        self.session = session

# Registered as 'probe' in place of switch_probe itself
class _ProbePlaceholder:
    def __init__(self, tc_probe):
        self._tc_probe = tc_probe

    def get_status(self, eventtime):
        return self._tc_probe.cmd_helper.get_status(eventtime)

    def query_endstop(self, print_time):
        return self._tc_probe.query_endstop(print_time)

    def get_probe_params(self, gcmd=None):
        return self._tc_probe.get_probe_params(gcmd)

    def get_offsets(self, gcmd=None):
        return self._tc_probe.get_offsets(gcmd)

    def start_probe_session(self, gcmd):
        return self._tc_probe.start_probe_session(gcmd)

# Klipper "probe" interface.
# Delegates every call to whichever tool is currently active.
class PrinterSwitchProbeKlipper(SwitchProbeBase):
    def __init__(self, config):
        super().__init__(config)
        self.cmd_helper = probe.ProbeCommandHelper(
            config, self, self.query_endstop, can_set_z_offset=True)
        self.homing_helper = probe.HomingViaProbeHelper(
            config, 0., self.query_endstop)
        self.printer.add_object('probe', _ProbePlaceholder(self))

    def add_tool(self, tool_config):
        key = tool_config.get_name().split(None, 1)[1]
        self._allow_pin_reuse(tool_config)
        offsets = probe.ProbeOffsetsHelper(tool_config)
        param_helper = probe.ProbeParameterHelper(tool_config)
        mcu_probe = probe.ProbeEndstopWrapper(tool_config, offsets,
                                              param_helper)
        session = probe.SampleAveragingHelper(
            tool_config, param_helper, mcu_probe.start_probe_session)
        tool = SwitchProbeKlipper(key, tool_config.get_name(), offsets,
                                param_helper, mcu_probe, session)
        return self._register_tool(tool_config, tool)

    def _activate(self, key):
        self.active_tool = key
        self.cmd_helper.name = self.tools[key].section_name

    # Standard "probe" interface expected by whatever is registered as
    # 'probe' - see probe.ProbeCommandHelper/HomingViaProbeHelper.
    def query_endstop(self, print_time):
        return self._get_active().mcu_probe.query_endstop(print_time)

    def get_probe_params(self, gcmd=None):
        return self._get_active_tool().param_helper.get_probe_params(gcmd)

    def get_offsets(self, gcmd=None):
        return self._get_active_tool().offsets.get_offsets(gcmd)

    def start_probe_session(self, gcmd):
        return self._get_active().session.start_probe_session(gcmd)


######################################################################
# Kalico backend
######################################################################

# Per tool switch probe endstop and offsets, built from one
# [switch_probe <tool>] section.
class SwitchProbeKalico:
    def __init__(self, key, section_name, mcu_probe, x_offset, y_offset,
                z_offset, probe_params):
        self.key = key
        self.section_name = section_name
        self.mcu_probe = mcu_probe
        self.x_offset = x_offset
        self.y_offset = y_offset
        self.z_offset = z_offset
        self.probe_params = probe_params

# Stand-in "mcu_probe" fed to Kalico's stock probe.PrinterProbe.
# A single shared instance that forwards every call to whichever tool's 
# ProbeEndstopWrapper is currently active.
class SwitchMcuProbe:
    def __init__(self, get_active_tool, get_assumed_tool):
        self._get_active_tool = get_active_tool
        self._get_assumed_tool = get_assumed_tool
    def _active(self):
        return self._get_active_tool().mcu_probe

    # MCU_endstop-like interface
    def get_mcu(self):
        return self._active().get_mcu()

    def add_stepper(self, stepper):
        pass

    def get_steppers(self):
        return self._active().get_steppers()

    def home_start(self, print_time, sample_time, sample_count, rest_time,
                   triggered=True):
        return self._active().home_start(
            print_time, sample_time, sample_count, rest_time, triggered)

    def home_wait(self, home_end_time):
        return self._active().home_wait(home_end_time)

    def query_endstop(self, print_time):
        return self._active().query_endstop(print_time)

    def get_position_endstop(self):
        # Kalico's stepper.py reads this once while [stepper_z] is being
        # constructed, before klippy:connect and any possible confirmation
        return self._get_assumed_tool().mcu_probe.get_position_endstop()

    # probe.ProbeEndstopWrapper interface used directly by PrinterProbe
    def probing_move(self, pos, speed, gcmd):
        return self._active().probing_move(pos, speed, gcmd)

    def probe_prepare(self, hmove):
        return self._active().probe_prepare(hmove)

    def probe_finish(self, hmove):
        return self._active().probe_finish(hmove)

    def multi_probe_begin(self, always_restore_toolhead=False):
        try:
            self._active().multi_probe_begin(always_restore_toolhead)
        except TypeError:
            self._active().multi_probe_begin()

    def multi_probe_end(self):
        self._active().multi_probe_end()

# Kalico "probe" interface.
# Feeds a SwitchMcuProbe into Kalico's stock PrinterProbe.
class PrinterSwitchProbeKalico(SwitchProbeBase):
    def __init__(self, config):
        super().__init__(config)
        self.probe = None

    def add_tool(self, tool_config):
        key = tool_config.get_name().split(None, 1)[1]
        self._allow_pin_reuse(tool_config)
        mcu_probe = probe.ProbeEndstopWrapper(tool_config)
        x_offset = tool_config.getfloat('x_offset', 0.)
        y_offset = tool_config.getfloat('y_offset', 0.)
        z_offset = tool_config.getfloat('z_offset')
        probe_params = self._read_probe_params(tool_config)
        tool = SwitchProbeKalico(key, tool_config.get_name(), mcu_probe,
                               x_offset, y_offset, z_offset, probe_params)
        self._ensure_probe(tool_config)
        return self._register_tool(tool_config, tool)

    def _read_probe_params(self, tool_config):
        # Kalico's PrinterProbe treats these as fixed instance
        # attributes set once at construction, unlike mainline's
        # ProbeParameterHelper which reads them per-tool on demand.
        # Read them per-tool and push them onto self.probe on
        # every _activate(), so the behavior matches Klipper.
        speed = tool_config.getfloat('speed', 5.0, above=0.)
        return {
            'speed': speed,
            'retry_speed': tool_config.getfloat(
                'retry_speed', speed, above=0.),
            'lift_speed': tool_config.getfloat(
                'lift_speed', speed, above=0.),
            'drop_first_result': tool_config.getboolean(
                'drop_first_result', False),
            'sample_count': tool_config.getint('samples', 1, minval=1),
            'sample_retract_dist': tool_config.getfloat(
                'sample_retract_dist', 2., above=0.),
            'samples_result': tool_config.getchoice(
                'samples_result', ['median', 'average'], 'average'),
            'samples_tolerance': tool_config.getfloat(
                'samples_tolerance', 0.100, minval=0.),
            'samples_retries': tool_config.getint(
                'samples_tolerance_retries', 0, minval=0),
        }

    def _ensure_probe(self, tool_config):
        # This differs from Klipper in that it will use the settings 
        # for the first probe it loads rather than explicitly zero.
        if self.probe is not None:
            return
        switch_mcu_probe = SwitchMcuProbe(self._get_active,
                                           self._assumed_tool)
        self.probe = probe.PrinterProbe(tool_config, switch_mcu_probe)
        self.printer.add_object('probe', self.probe)

    def _assumed_tool(self):
        if self.active_tool in self.tools:
            return self.tools[self.active_tool]
        if not self.tools:
            raise self.printer.config_error(
                "switch_probe: no [switch_probe <tool>]"
                " sections configured")
        return self.tools[self._default_tool_key()]

    def _activate(self, key):
        tool = self.tools[key]
        self.active_tool = key
        self.probe.x_offset = tool.x_offset
        self.probe.y_offset = tool.y_offset
        self.probe.z_offset = tool.z_offset
        self.probe.name = tool.section_name
        for name, value in tool.probe_params.items():
            setattr(self.probe, name, value)

    def _check_can_activate(self, gcmd):
        if self.probe.multi_probe_pending:
            raise gcmd.error(
                "switch_probe: cannot switch active tool while a"
                " probe operation is still in progress")


######################################################################
# Scanner backend
######################################################################

# Per tool endstop, built from one [switch_probe <tool>] section.
# Only the pin matters here - scanner mode never probes with it.
class SwitchProbeDetect:
    def __init__(self, key, mcu_probe):
        self.key = key
        self.mcu_probe = mcu_probe

# Detection-only interface, used when a scanner (Cartographer, Beacon,
# ...) is the printer's probe. Deliberately never registers itself as
# 'probe' and never builds any probing machinery - the scanner owns all
# of that. Each tool's pin is used purely for presence detection and
# crash detection, so this backend needs none of the Klipper/Kalico
# probe-interface differences the other two backends have.
class PrinterSwitchProbeScanner(SwitchProbeBase):
    _accepts_unused_options = True

    def add_tool(self, tool_config):
        key = tool_config.get_name().split(None, 1)[1]
        self._allow_pin_reuse(tool_config)
        ppins = self.printer.lookup_object('pins')
        mcu_probe = ppins.setup_pin('endstop', tool_config.get('pin'))
        tool = self._register_tool(tool_config,
                                   SwitchProbeDetect(key, mcu_probe))
        ignored = sorted(set(tool.raw_config) - _SCANNER_TOOL_OPTIONS)
        if ignored:
            logging.warning(
                "switch_probe: '%s' ignoring option(s) %s - the"
                " scanner handles probing. Check for typos if you did not"
                " expect one of these."
                % (tool_config.get_name(), ", ".join(ignored)))
        return tool

    def _activate(self, key):
        # No offsets or probe params to apply
        self.active_tool = key

    def _handle_connect(self):
        # Both Cartographer and Beacon claim 'probe' during their own
        # config phase, so by now one of them must have.
        if self.printer.lookup_object('probe', None) is None:
            raise self.printer.config_error(
                "switch_probe: 'scanner: True' is set, but nothing"
                " registered as the printer's probe.")
        super()._handle_connect()


def _is_mainline_klipper():
    return hasattr(probe, 'HomingViaProbeHelper') and not hasattr(
        probe, 'RetryPolicy')

def load_config(config):
    if config.getboolean('scanner', False):
        return PrinterSwitchProbeScanner(config)
    if _is_mainline_klipper():
        return PrinterSwitchProbeKlipper(config)
    return PrinterSwitchProbeKalico(config)

def load_config_prefix(config):
    printer = config.get_printer()
    multi_probe = printer.load_object(config, 'switch_probe')
    return multi_probe.add_tool(config)
