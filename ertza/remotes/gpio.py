# -*- coding: utf-8 -*-

import numpy as np

from ..config import ConfigRequest
from ..errors import RemoteError
from .modbus import ModbusRequest
from .temp_chart import NTCLE100E3103JB0 as temp_chart
from .event_watcher import EventWatcher

try:
    import Adafruit_BBIO.GPIO as GPIO
    import Adafruit_BBIO.PWM as PWM
except ImportError as e:
    pass
 
SWITCH_PINS = ("P8_10", "P8_11",)
TEMP_PINS = (
        '/sys/bus/iio/devices/iio:device0/in_voltage0_raw', #AIN0
        '/sys/bus/iio/devices/iio:device0/in_voltage1_raw', #AIN1
        )
TEMP_TARGET = (40.0, 40.0,) # in °C
FAN_PINS = ('P8_13', 'P8_19',)

TEMP_TABLE = temp_chart

class RemoteServer(object):
    def __init__(self, config, **kwargs):
        self.fake_mode = False
        self._modbus, self.restart_event = None, None
        self._config = config

        if 'fake_mode' in kwargs:
            self.fake_mode = kwargs['fake_mode']

        if 'modbus' in kwargs:
            self._modbus = kwargs['modbus']
            self.mdb_request = ModbusRequest(self._modbus)

        if 'logger' in kwargs:
            self.lg = kwargs['logger']
        else:
            import logging
            self.lg = logging.getLogger()

        if 'restart_event' in kwargs:
            self.restart_event = kwargs['restart_event']

        self.config_request = ConfigRequest(self._config)

        try:
            self.run(init=True)
        except RemoteError:
            self.fake_mode = True
            self.run(init=True)

    def run(self, interval=None, init=False):
        if (self.restart_event and self.restart_event.is_set()) or init:
            if not init:
                self.lg.info('Updating remote server…')
            try:
                self.create_switch_pins()
                self.create_temp_watchers()
                if self.restart_event:
                    self.restart_event.clear()
            except (NameError, RuntimeError) as e:
                raise RemoteError(
                        'Error configuring pins, am I on a beaglebone ?',
                        self.lg) from e
                if not self.fake_mode:
                    self.lg.warn('Starting failed. Fallback to fake mode.')
                    self.fake_mode = True
                    self.run()

        if not self.fake_mode:
            self.update_pid()
            self.detect_gpio_state()

    def create_switch_pins(self):
        if not self.fake_mode:
            sw = list()
            for i, p in enumerate(SWITCH_PINS):
                a = self.config_request.get('switch_'+str(i), 'action', None)
                r = self.config_request.get('switch_'+str(i), 'reverse', False)
                sw.append(SwitchHandler(i, p, a, r))
            self.switchs = tuple(sw)
            return True
        return False

    def create_temp_watchers(self):
        if TEMP_PINS and not self.fake_mode:
            ADC.setup()
            tw = list()
            for s, f, t in zip(TEMP_PINS, FAN_PINS, TEMP_TARGET):
                tw.append(TempWatcher(s, f, t))
            self.temp_watchers = tuple(tw)
            return True
        return False

    def update_pid(self):
        for tw in self.temp_watchers:
            tw.set_pid()

    def detect_gpio_state(self):
        for p in self.switch_pins:
            if p.update_state(): # if something change, trigger
                self.mdb_request.trigger(p.action, p.state)

    def __del__(self):
        if not self.fake_mode:
            GPIO.cleanup()

class SwitchHandler(EventWatcher):
    def __init__(self, name, pin, callback=None, invert=False):
        super(SwitchHandler, self).__init__(pin, key_code, name, invert)
        self.callback = callback

        self.setup_pin()

    def setup_pin(self):
        GPIO.setup(self.pin, GPIO.IN)
        GPIO.output(self.pin, GPIO.HIGH)
        GPIO.add_event_detect(self.pin, GPIO.BOTH)

    def update_state(self):
        if GPIO.event_detected(self.pin):
            self.state = GPIO.input(self.pin)
            return True
        return None


class TempWatcher(object):
    def __init__(self, sensor, fan, target_temp):
        self.sensor = sensor
        self.beta = 3977
        self.map_table = (
                (0, 423),
                (25, 900),
                (40, 1174),
                (55, 1386),
                (70, 1532),
                (85, 1599),
                (100, 1686))
        self.fan_pin = fan
        self.fan = PWM.start(fan, 0)
        self.target_temp = target_temp

        self.coeff_g = 1
        self.coeff_ti = 0.1
        self.coeff_td = 0.1

    def set_pid(self):
        self.get_error()
        _cmd = self.get_pid()
        self.fan.set_duty_cycle(_cmd)

    def get_temperature(self):
        """ Return the temperature in degrees celsius """
        with open(self.sensor, "r") as file:
            try:
                voltage = (float(file.read().rstrip()) / 4095.0) * 1.8
                res_val = self.voltage_to_resistance(voltage)  # Convert to resistance
                return self.resistance_to_degrees(res_val) # Convert to degrees
            except IOError as e:
                logging.error("Unable to get ADC value ({0}): {1}".format(e.errno, e.strerror))
                return -1.0

    def resistance_to_degrees(self, resistor_val):
        """ Return the temperature nearest to the resistor value """
        return resistor_val

    def voltage_to_resistance(self, v_sense):
        """ Convert the voltage to a resistance value """
        if v_sense == 0 or (abs(v_sense - 1.8) < 0.001):
            return 10000000.0
        return 4700.0 / ((1.8 / v_sense) - 1.0)

    def get_error(self):
        self.error_last = self.error
        self.error = self.target - self.get_temperature()
        self.error_sum += self.error
        self.error_delta = self.error - self.error_last

    def get_pid(self):
        return self.p() + self.i() + self.d()

    def get_p(self):
        return self.error * self.coeff_g

    def get_i(self):
        return self.error_sum * self.coeff_ti

    def get_d(self):
        return self.coef_td * self.error_delta

    def __del__(self):
        PWM.stop(self.fan_pin)
        PWM.cleanup()
