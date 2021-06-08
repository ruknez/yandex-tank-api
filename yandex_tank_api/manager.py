"""
Module that manages webserver and tank
"""
import os.path
import os
import signal
import multiprocessing
import logging
import logging.handlers
import traceback
import six
import time
import threading
import socket
import requests

import yandex_tank_api.common
import yandex_tank_api.worker
import yandex_tank_api.webserver

_log = logging.getLogger(__name__)


class TankRunner(object):
    """
    Manages the tank process and its working directory.
    """

    def __init__(
            self, cfg, manager_queue, session_id, tank_config, first_break):
        """
        Sets up working directory and tank queue
        Starts tank process
        """

        work_dir = os.path.join(cfg['tests_dir'], session_id)
        lock_dir = cfg['lock_dir']
        load_ini_path = os.path.join(work_dir, 'load.yaml')

        # Create load.yaml
        _log.info('Saving tank config to %s', load_ini_path)
        with open(load_ini_path, 'w') as tank_config_file:
            tank_config_file.write(six.ensure_str(tank_config))

        # Create tank queue and put first break there
        self.tank_queue = multiprocessing.Queue()
        self.set_break(first_break)

        ignore_machine_defaults = cfg['ignore_machine_defaults']
        configs_location = cfg['configs_location']

        # Start tank process
        self.tank_process = multiprocessing.Process(
            target=yandex_tank_api.worker.run,
            args=(
                self.tank_queue, manager_queue, work_dir, lock_dir, session_id,
                ignore_machine_defaults, configs_location))
        self.tank_process.start()

    def set_break(self, next_break):
        """Sends the next break to the tank process"""
        self.tank_queue.put({'break': next_break})

    def is_alive(self):
        """Check that the tank process didn't exit """
        return self.tank_process.exitcode is None

    def get_exitcode(self):
        """Return tank exitcode"""
        return self.tank_process.exitcode

    def join(self):
        """Joins the tank process"""
        _log.info('Waiting for tank exit...')
        return self.tank_process.join()

    def stop(self, remove_break):
        """Interrupts the tank process"""
        if self.is_alive():
            sig = signal.SIGTERM if remove_break else signal.SIGINT
            os.kill(self.tank_process.pid, sig)

    def __del__(self):
        self.stop(remove_break=True)


class Manager(object):
    """
    Implements the message processing logic
    """

    def __init__(self, cfg):
        """Sets up initial state of Manager"""

        self.cfg = cfg

        self.manager_queue = multiprocessing.Queue()
        self.webserver_queue = multiprocessing.Queue()

        self.webserver_process = multiprocessing.Process(
            target=yandex_tank_api.webserver.main,
            args=(
                self.webserver_queue, self.manager_queue, cfg['tests_dir'],
                cfg['tornado_debug']))
        self.webserver_process.daemon = True
        self.webserver_process.start()

        self.dockerized = bool(os.popen("awk -F/ '$2 == \"docker\"' /proc/self/cgroup").read())
        self.heartbeat_info = {
            'host': socket.gethostname(),
            'port': self._port if self.dockerized else 8123
        }
        self._send_info_timeout = 5
        self.info_sender = threading.Thread(target=self._send_heartbeat_info)
        self.info_sender.daemon = True

        self._timeout_thread = None

        self._reset_session(ignore_disposable=True)

    @property
    def _heartbeat_destination(self):
        return os.environ.get('HEARTBEAT_HANDLER')

    @property
    def disposable(self):
        return self.cfg['disposable'] or os.environ.get('DISPOSABLE') == "on"

    @property
    def _port(self):
        p = os.environ.get('EXPOSED_PORT', '0')
        if p.isdigit():
            return int(p)
        else:
            _log.warning("Invalid port in EXPOSED_PORT env var")
            return 0

    @property
    def _inactive_timeout(self):
        timeout = os.environ.get('TANKAPI_PREPARE_TIMEOUT')
        if timeout:
            if timeout.isdigit():
                return int(timeout)
            else:
                _log.warning("Invalid TANKAPI_PREPARE_TIMEOUT env value:%s", timeout)
        return None

    def _start_countdown(self):
        if self._inactive_timeout:
            if self._timeout_thread:
                self._timeout_thread.cancel()
                _log.info('Cancelled previous timeout %s', self._timeout_thread)
            self._timeout_thread = threading.Timer(
                    self._inactive_timeout,
                    self._stop_api,
                    args=['No task updates until timeout'])
            self._timeout_thread.daemon = True
            self._timeout_thread.start()
            _log.info('Set timeout thread %s to stop api after %ss',
                      self._timeout_thread,
                      self._inactive_timeout)

    def _stop_countdown(self):
        if self._timeout_thread:
            self._timeout_thread.cancel()
            _log.info('Cancel timeout thread %s', self._timeout_thread)

    def _stop_api(self, reason):
        _log.error("Stopping api because: %s", reason)
        if self.tank_runner:
            try:
                self.tank_runner.stop(remove_break=True)
            except Exception as ex:
                _log.error('Tank runner failed to stop: %s', ex)
        os.kill(os.getppid(), 2)
        os.kill(os.getpid(), 2)
        time.sleep(3)
        _log.error("Interrupt was not sufficient, killing myself")
        os.kill(os.getpid(), 15)
        os.kill(os.getppid(), 15)

    def _send_heartbeat_info(self):
        if self._heartbeat_destination is None:
            return
        else:
            while True:
                r = requests.post(self._heartbeat_destination, json=self.heartbeat_info)
                r.raise_for_status()
                time.sleep(self._send_info_timeout)

    def _reset_session(self, ignore_disposable=False):
        """
        Resets session state variables
        Should be called only when tank is not running
        """
        if self.disposable and not ignore_disposable:
            self.heartbeat_info['status'] = "disconnect"
            return self._stop_api("session reset")
        _log.info('Resetting current session variables')
        self.session_id = None
        self.heartbeat_info['session'] = None
        self.tank_runner = None
        self.last_tank_status = 'not started'

    def _handle_cmd_stop(self, msg):
        """Check running session and kill tank"""
        if msg['session'] == self.session_id:
            self.tank_runner.stop(remove_break=True)

        else:
            _log.error('Can stop only current session')

    def _handle_cmd_set_break(self, msg):
        """New break for running session"""
        if msg['session'] != self.session_id:
            raise RuntimeError(
                'Webserver requested to start session '
                'when another one is already running')
        elif 'break' in msg:
            self.tank_runner.set_break(msg['break'])
        else:
            # Internal protocol error
            _log.error(
                'Recieved run command without break:\n%s', msg)

    def _handle_cmd_new_session(self, msg):
        """Start new session"""
        if 'session' not in msg or 'config' not in msg:
            # Internal protocol error
            _log.critical(
                'Not enough data to start new session: '
                'both config and test should be present:%s\n', msg)
            return
        try:
            print(msg)
            self.tank_runner = TankRunner(
                cfg=self.cfg,
                manager_queue=self.manager_queue,
                session_id=msg['session'],
                tank_config=msg['config'],
                first_break=msg['break'])
        except KeyboardInterrupt:
            pass
        except Exception as ex:
            self.webserver_queue.put({
                'session': msg['session'],
                'status': 'failed',
                'break': msg['break'],
                'reason': 'Failed to start tank:\n' + traceback.format_exc(ex)
            })
        else:
            self.session_id = msg['session']
            self.heartbeat_info['session'] = msg['session']
            
    def _handle_cmd(self, msg):
        """Process command from webserver"""

        if 'session' not in msg:
            _log.error('Bad command: session id not specified')
            return

        cmd = msg['cmd']

        if cmd == 'stop':
            self._handle_cmd_stop(msg)
        elif cmd == 'run':
            if self.session_id is not None:
                self._handle_cmd_set_break(msg)
            else:
                self._handle_cmd_new_session(msg)
        else:
            _log.critical('Unknown command: %s', cmd)

    def _handle_tank_exit(self):
        """
        Empty manager queue.
        Report if tank died unexpectedly.
        Reset session.
        """
        logging.info('Tank exit, sleeping 1 s and handling remaining messages')
        time.sleep(1)
        while True:
            try:
                msg = self.manager_queue.get(block=False)
            except multiprocessing.queues.Empty:
                break
            self._handle_msg(msg)
        if self.last_tank_status == 'running'\
                or not self.tank_runner\
                or self.tank_runner.get_exitcode() != 0:
            # Report unexpected death
            self.webserver_queue.put({
                'session': self.session_id,
                'status': 'failed',
                'reason': 'Tank died unexpectedly. Last reported '
                'status: % s, worker exitcode: % s' % (
                    self.last_tank_status,
                    self.tank_runner.get_exitcode() if self.tank_runner else None)
            })
        # In any case, reset the session
        self._reset_session()
        self._start_countdown()

    def _handle_webserver_exit(self):
        """Stop tank and raise RuntimeError"""
        _log.error('Webserver died unexpectedly.')
        if self.tank_runner is not None:
            _log.warning('Stopping tank...')
            self.tank_runner.stop(remove_break=True)
            self.tank_runner.join()
        raise RuntimeError('Unexpected webserver exit')

    def run(self):
        """
        Manager event loop.
        Process message from self.manager_queue
        Check that tank is alive.
        Check that webserver is alive.
        """

        self.info_sender.start()
        self._start_countdown()

        while True:
            if self.session_id is not None and not self.tank_runner.is_alive():
                self._handle_tank_exit()
            if not self.webserver_process.is_alive():
                self._handle_webserver_exit()
            try:
                msg = self.manager_queue.get(
                    block=True, timeout=self.cfg['message_check_interval'])
            except multiprocessing.queues.Empty:
                continue
            self._handle_msg(msg)

    def _handle_msg(self, msg):
        """Handle message from manager queue"""
        _log.info('Recieved message:\n%s', msg)
        if 'cmd' in msg:
            # Recieved command from server
            self._handle_cmd(msg)
        elif 'status' in msg:
            # This is a status message from tank
            self._handle_tank_status(msg)
        else:
            _log.error('Strange message (not a command and not a status) ')

    def _handle_tank_status(self, msg):
        """
        Wait for tank exit if it stopped.
        Remember new status and notify webserver.
        """
        _log.info("Got tank status msg %s", msg)
        if msg['stage_completed']:
            self._start_countdown()
        elif msg['status'] == 'running':
            self._stop_countdown()
        else:
            _log.warning('Something wrong with current status %s', msg)

        new_status = msg['status']
        self.heartbeat_info['status'] = new_status

        if self.last_tank_status not in ['success', 'failed'] \
                and new_status in ['success', 'failed']:
            self.tank_runner.join()
            self._reset_session()

        self.last_tank_status = msg['status']

        self.webserver_queue.put(msg)


def run_server(options):
    """Runs the whole yandex-tank-api server """

    # Configure
    # TODO: un-hardcode cfg
    cfg = {
        'message_check_interval': 1.0,
        'tests_dir': options.work_dir + '/tests',
        'ignore_machine_defaults': options.ignore_machine_defaults,
        'tornado_debug': options.debug,
        'lock_dir': options.lock_dir,
        'configs_location': options.configs_location,
        'disposable': options.disposable,
    }

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)
    if options.log_file is None:
        handler = logging.StreamHandler()
    else:
        handler = logging.handlers.RotatingFileHandler(
            options.log_file, maxBytes=1000000, backupCount=16)

    handler.setFormatter(
        logging.Formatter('%(asctime)s [%(levelname)s] PID:%(process)d %(name)s %(filename)s:%(lineno)d  %(message)s'))

    # remove existing logging handlers to avoid message duplication
    for h in logging.root.handlers[:]:
        logging.root.removeHandler(h)
    root_logger.addHandler(handler)

    logger = logging.getLogger(__name__)
    try:
        logger.info('Starting server')
        Manager(cfg).run()
    except KeyboardInterrupt:
        logger.info('Interrupted, terminating')
    except Exception:
        logger.exception('Unhandled exception in manager.run_server:')
    except:
        logger.error('Caught something strange in manager.run_server')
