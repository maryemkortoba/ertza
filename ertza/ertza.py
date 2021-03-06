#!/usr/bin/env python
# -*- coding: utf-8 -*-

# Main class for ertza

import logging as lg
import os
import os.path
import sys
import signal
from threading import Thread
import queue

from .configparser import ConfigParser, ProfileError
from .machine import Machine
from .machine import AbstractMachineError

from .dispatch import Dispatcher

from .processors import OscProcessor, SerialProcessor

from .processors.osc.server import OscServer
from .processors.serial.server import SerialServer
from .processors.serial.message import SerialCommandString

from .commands import OscCommand, SerialCommand

from .pwm import PWM
from .thermistor import Thermistor

from .fan import Fan
from .switch import Switch, SwitchException
from .tempwatcher import TempWatcher
from .led import Led

from .network_utils import EthernetInterface

version = '0.1.0~Siderunner'

_DEFAULT_CONF = '/etc/ertza/default.conf'
_MACHINE_CONF = '/etc/ertza/machine.conf'
_CUSTOM_CONF = '/etc/ertza/custom.conf'

console_logger = lg.StreamHandler()
console_formatter = lg.Formatter('%(asctime)s %(name)-30s '
                                 '%(levelname)-8s %(message)s',
                                 datefmt='%Y%m%d %H:%M:%S')

logger = lg.getLogger('ertza')
logger.addHandler(console_logger)
console_logger.setFormatter(console_formatter)


class Ertza(object):
    '''
    Main class for ertza.

    Handle log, configuration, startup and dispatch others tasks to
    various processes
    '''

    def __init__(self, *agrs, **kwargs):
        ''' Init '''
        logger.setLevel(15)
        logger.info('Ertza initializing. Version: {}'.format(version))

        machine = Machine()
        self.machine = machine
        machine.version = version

        if not os.path.isfile(_DEFAULT_CONF):
            logger.error('{} does not exist, exiting.'.format(_DEFAULT_CONF))
            sys.exit(1)

        c = None
        if 'config' in kwargs:
            c = kwargs['config']
        custom_conf = c[0] if c else _CUSTOM_CONF

        logger.info('Custom file: {}'.format(custom_conf))

        machine.config = ConfigParser(_DEFAULT_CONF,
                                      _MACHINE_CONF,
                                      custom_conf)

        self._config_leds()
        Led.set_status_leds('blink', 500)

        # Get loglevel from config file
        level = self.machine.config.getint('system', 'loglevel', fallback=10)
        if level > 0:
            lg.getLogger('').setLevel(level)
            logger.setLevel(level)
            logger.info('Log level set to {}'.format(level))

        machine.cape_infos = machine.config.find_cape('ARMAZCAPE')

        if machine.cape_infos:
            name = machine.cape_infos['name']
            logger.info('Found cape {} with S/N {}'.format(name, machine.serialnumber))
            SerialCommandString.SerialNumber = machine.serialnumber

        machine.config.load_variant()
        try:
            machine.config.load_profile()
        except ProfileError as e:
            logger.info('Unable to load profile: {!s}'.format(e))

        try:
            i = machine.config.get('machine', 'interface', fallback='eth1')
            logger.info('Configuring {} interface'.format(i))
            eth = EthernetInterface(i)
            try:
                logger.info('Setting interface {} to up'.format(i))
                eth.link_up()
                logger.info('Setting up default route'.format(i))
                eth.add_route('default')
            except Exception as e:
                logger.error(e)

            ip = machine.config.get('machine', 'ip_address', fallback=None)
            if not ip:
                ip = '10'
                for byte in eth.mac_address.split(':')[3:6]:
                    ip += '.{}'.format(int('0x{}'.format(byte), base=0))
                ip += '/8'
                logger.info('Generating default IP for MAC address: {}'
                            .format(ip))

            try:
                logger.info('Adding ip {} to {}'.format(ip, i))
                eth.add_ip(ip)
            except Exception as e:
                logger.error(e)
            machine.ethernet_interface = eth
        except IndexError:
            logger.warn('No IP address found')

        machine.init_driver()

        self._config_thermistors()
        self._config_fans()

        if not machine.config.get('switches', 'disable', fallback=False):
            self._config_external_switches()

        # Create dispatcher
        dispatcher = Dispatcher()

        if not machine.config.get('osc', 'disable', fallback=False):
            ident = OscServer.identifier
            osc_conf = machine.config['osc'] \
                if machine.config.has_section('osc') else None
            dispatcher.add_processor(OscProcessor(dispatcher.outlet))
            dispatcher.add_server(OscServer(dispatcher.inlet, osc_conf))
            OscProcessor.outlet = dispatcher.outlet(ident)
            OscCommand.machine = machine

        if not machine.config.get('serial', 'disable', fallback=False):
            ident = SerialServer.identifier
            serial_conf = machine.config['serial'] \
                if machine.config.has_section('serial') else None
            dispatcher.add_processor(SerialProcessor(dispatcher.outlet))
            dispatcher.add_server(SerialServer(dispatcher.inlet, serial_conf))
            SerialProcessor.outlet = dispatcher.outlet(ident)
            SerialCommand.machine = machine

        self.machine.dispatcher = dispatcher

    def start(self):
        ''' Start the processes '''
        self.running = True

        self.machine.start()

        try:
            if self.machine.switches is not None:
                Switch.start()
                logger.info('Switch thread started')
        except (SwitchException, RuntimeError) as e:
            logger.error('Error while starting switch thread: {!s}'.format(e))
            sys.exit(1)

        self.machine.dispatcher.start()

        try:
            self.machine.load_startup_mode()
        except AbstractMachineError as e:
            logger.error(str(e))

        Led.set_status_leds('blink', 50)

        logger.info('Ertza ready')

    def exit(self):
        self.machine.exit()

        self.running = False

        for f in self.machine.fans:
            f.set_value(0)

        Led.set_all_leds('on')

    def _config_thermistors(self):

        # Get available thermistors
        self.machine.thermistors = []
        if self.machine.config.getboolean('thermistors', 'got_thermistors'):
            th_p = 0

            while self.machine.config.has_option('thermistors',
                                                 'port_TH{}'.format(th_p)):
                th_n = 'TH{}'.format(th_p)
                adc_channel = int(self.machine.config.get(
                    'thermistors', 'port_{}'.format(th_n)))
                therm = Thermistor(adc_channel, th_n)
                self.machine.thermistors.append(therm)
                logger.debug('Found thermistor {} at ADC channel {}'
                             .format(th_n, adc_channel))
                th_p += 1

    def _config_fans(self):
        self.machine.fans = []

        # Get available fans
        if self.machine.config.getboolean('fans', 'got_fans'):
            PWM.set_frequency(1560)

            f_p = 0
            while self.machine.config.has_option('fans', 'address_F{}'.format(f_p)):
                f_n = 'F{}'.format(f_p)
                fan_channel = int(self.machine.config.get(
                    'fans', 'address_{}'.format(f_n)))
                fan = Fan(fan_channel)
                fan.min_speed = float(self.machine.config.get(
                    'fans', 'min_speed_{}'.format(f_n), fallback=0.0))
                self.machine.fans.append(fan)
                logger.debug(
                    'Found fan {} at channel {}'.format(f_n, fan_channel))
                f_p += 1

        for f in self.machine.fans:
            f.set_value(1)

        th_cf = self.machine.config['thermistors']
        tw_cf = self.machine.config['temperature_watchers']

        # Connect fans to thermistors
        if self.machine.fans:
            self.machine.temperature_watchers = []

            for t, therm in enumerate(self.machine.thermistors):
                for f, fan in enumerate(self.machine.fans):
                    if tw_cf.getboolean('connect_TH{}_to_F{}'.format(t, f),
                                        fallback=False):
                        tw = TempWatcher(therm, fan,
                                         'TempWatcher-{}-{}'.format(t, f))
                        tw.set_target_temperature(float(
                            th_cf.get('target_temperature_TH{}'.format(t))))
                        tw.set_max_temperature(float(
                            th_cf.get('max_temperature_TH{}'.format(t))))
                        tw.interval = float(th_cf.get(
                            'update_interval_TH{}'.format(t), fallback=5))
                        tw.enable()
                        self.machine.temperature_watchers.append(tw)
        elif self.machine.thermistors:
            self.machine.temperature_watchers = []
            for t, therm in enumerate(self.machine.thermistors):
                tw = TempWatcher(therm, None, 'TempLogger-{}'.format(t))
                tw.set_max_temperature(float(
                    th_cf.get('max_temperature_TH{}'.format(t))))
                tw.interval = float(th_cf.get('update_interval_TH{}'.format(t),
                                              fallback=5))
                tw.enable(mode=False)
                self.machine.temperature_watchers.append(tw)

    def _config_external_switches(self):
        Switch.callback = self.machine.switch_callback
        Switch.set_inputdev(self.machine.config.get(
            'switches', 'inputdev_path', fallback='/dev/input/event1'))

        # Create external switches
        self.machine.switches = []
        esw_p = 0
        while self.machine.config.has_option('switches',
                                             'keycode_ESW{}'.format(esw_p)):
            esw_n = 'ESW{}'.format(esw_p)
            esw_kc = int(self.machine.config.get(
                'switches', 'keycode_{}'.format(esw_n)))
            name = self.machine.config.get(
                'switches', 'name_{}'.format(esw_n), fallback=esw_n)
            esw_cf = {}
            esw_cf['invert'] = self.machine.config.getboolean(
                'switches', 'invert_{}'.format(esw_n))
            esw_cf['function'] = self.machine.config.get(
                'switches', 'function_{}'.format(esw_n))
            esw = Switch(esw_kc, name, **esw_cf)
            self.machine.switches.append(esw)
            logger.debug('Found switch {} at keycode {}'.format(name, esw_kc))
            esw_p += 1

    def _config_leds(self):

        # Create leds
        self.machine.leds = []
        if self.machine.config.getboolean('leds', 'got_leds'):
            led_i = 0
            while self.machine.config.has_option('leds', 'file_L{}'.format(led_i)):
                led_n = 'L{}'.format(led_i)
                led_f = self.machine.config.get('leds', 'file_{}'.format(led_n))
                led_fn = self.machine.config.get('leds', 'function_{}'.format(led_n),
                                                 fallback=None)
                led = Led(led_f, name=led_n, function=led_fn)
                led_t = self.machine.config.get('leds', 'trigger_{}'.format(led_n),
                                                fallback='none')
                led.set_trigger(led_t)
                if led_t == 'timer':
                    led.set_delays(int(self.machine.config.get(
                        'leds', 'blink_{}'.format(led_n), fallback='500')))
                self.machine.leds.append(led)
                logger.debug('Found led {}, trigger: {}'.format(led_n, led_t))
                led_i += 1

    def _execute(self, c, p):
        p.execute(c)


def main(parent_args=None):
    import argparse

    parser = argparse.ArgumentParser(prog='ertza')
    parser.add_argument('--config', nargs=1, help='use CONFIG as custom config file')

    if parent_args:
        args, args_remaining = parser.parse_known_args(parent_args)
    else:
        args, args_remaining = parser.parse_known_args()

    e = Ertza(**vars(args))

    def signal_handler(signal, frame):
        e.exit()

    signal.signal(signal.SIGINT, signal_handler)

    e.start()

    signal.pause()


def profile():
    import yappi
    yappi.start()
    main()
    yappi.get_func_stats().print_all()

if __name__ == '__main__':
    _DEFAULT_CONF = '../conf/default.conf'
    _MACHINE_CONF = '../conf/fake.conf'
    if len(sys.argv) > 1 and sys.argv[1] == 'profile':
        profile()
    else:
        main()
