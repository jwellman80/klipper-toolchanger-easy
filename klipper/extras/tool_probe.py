# Per-tool Z-Probe support
#
# Copyright (C) 2023 Viesturs Zarins <viesturz@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.
from . import probe

class ToolProbe:
    def __init__(self, config):
        self.tool = config.getint('tool')
        self.printer = config.get_printer()
        self.name = config.get_name()
        self.probe_offsets = probe.ProbeOffsetsHelper(config)
        self.param_helper = probe.ProbeParameterHelper(config)
        self.mcu_probe = probe.ProbeEndstopWrapper(
            config, self.probe_offsets, self.param_helper)
        self.probe_session = probe.SampleAveragingHelper(
            config, self.param_helper, self.mcu_probe.start_probe_session)

        # Crash detection stuff
        pin = config.get('pin')
        buttons = self.printer.load_object(config, 'buttons')
        ppins = self.printer.lookup_object('pins')
        ppins.allow_multi_use_pin(pin.replace('^', '').replace('!', ''))
        buttons.register_buttons([pin], self._button_handler)

        #Register with the endstop
        self.endstop = self.printer.load_object(config, "tool_probe_endstop")
        self.endstop.add_probe(config, self)

    def _button_handler(self, eventtime, is_triggered):
        self.endstop.note_probe_triggered(self, eventtime, is_triggered)

    def get_probe_params(self, gcmd=None):
        return self.param_helper.get_probe_params(gcmd)
    def get_offsets(self, gcmd=None):
        return self.probe_offsets.get_offsets(gcmd)
    def start_probe_session(self, gcmd):
        return self.probe_session.start_probe_session(gcmd)

def load_config_prefix(config):
    return ToolProbe(config)
