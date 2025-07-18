#!/usr/bin/env python
import datetime
import logging
import os
import pickle
import platform
import sys
import threading
import time
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, cast

import zmq
from sortedcontainers import SortedList

from parsl import curvezmq
from parsl.addresses import tcp_url
from parsl.app.errors import RemoteExceptionWrapper
from parsl.executors.high_throughput.errors import ManagerLost, VersionMismatch
from parsl.executors.high_throughput.manager_record import ManagerRecord
from parsl.executors.high_throughput.manager_selector import ManagerSelector
from parsl.monitoring.message_type import MessageType
from parsl.monitoring.radios.base import MonitoringRadioSender
from parsl.monitoring.radios.zmq import ZMQRadioSender
from parsl.process_loggers import wrap_with_logs
from parsl.serialize import serialize as serialize_object
from parsl.utils import setproctitle
from parsl.version import VERSION as PARSL_VERSION

PKL_HEARTBEAT_CODE = pickle.dumps((2 ** 32) - 1)
PKL_DRAINED_CODE = pickle.dumps((2 ** 32) - 2)

LOGGER_NAME = "interchange"
logger = logging.getLogger(LOGGER_NAME)


class Interchange:
    """ Interchange is a task orchestrator for distributed systems.

    1. Asynchronously queue large volume of tasks (>100K)
    2. Allow for workers to join and leave the union
    3. Detect workers that have failed using heartbeats
    """
    def __init__(self,
                 *,
                 client_address: str,
                 interchange_address: Optional[str],
                 client_ports: Tuple[int, int, int],
                 worker_port: Optional[int],
                 worker_port_range: Tuple[int, int],
                 hub_address: Optional[str],
                 hub_zmq_port: Optional[int],
                 heartbeat_threshold: int,
                 logdir: str,
                 logging_level: int,
                 poll_period: int,
                 cert_dir: Optional[str],
                 manager_selector: ManagerSelector,
                 run_id: str,
                 ) -> None:
        """
        Parameters
        ----------
        client_address : str
             The ip address at which the parsl client can be reached. Default: "127.0.0.1"

        interchange_address : Optional str
             If specified the interchange will only listen on this address for connections from workers
             else, it binds to all addresses.

        client_ports : tuple(int, int, int)
             The ports at which the client can be reached

        worker_port : int
             The specific port to which workers will connect to the Interchange.

        worker_port_range : tuple(int, int)
             The interchange picks ports at random from the range which will be used by workers.
             This is overridden when the worker_port option is set.

        hub_address : str
             The IP address at which the interchange can send info about managers to when monitoring is enabled.
             When None, monitoring is disabled.

        hub_zmq_port : str
             The port at which the interchange can send info about managers to when monitoring is enabled.
             When None, monitoring is disabled.

        heartbeat_threshold : int
             Number of seconds since the last heartbeat after which worker is considered lost.

        logdir : str
             Parsl log directory paths. Logs and temp files go here.

        logging_level : int
             Logging level as defined in the logging module.

        poll_period : int
             The main thread polling period, in milliseconds.

        cert_dir : str | None
            Path to the certificate directory.
        """
        self.cert_dir = cert_dir
        self.logdir = logdir
        os.makedirs(self.logdir, exist_ok=True)

        start_file_logger("{}/interchange.log".format(self.logdir), level=logging_level)
        logger.debug("Initializing Interchange process")

        self.client_address = client_address
        self.interchange_address: str = interchange_address or "*"
        self.poll_period = poll_period

        logger.info("Attempting connection to client at {} on ports: {},{},{}".format(
            client_address, client_ports[0], client_ports[1], client_ports[2]))
        self.zmq_context = curvezmq.ServerContext(self.cert_dir)
        self.task_incoming = self.zmq_context.socket(zmq.DEALER)
        self.task_incoming.set_hwm(0)
        self.task_incoming.connect(tcp_url(client_address, client_ports[0]))
        self.results_outgoing = self.zmq_context.socket(zmq.DEALER)
        self.results_outgoing.set_hwm(0)
        self.results_outgoing.connect(tcp_url(client_address, client_ports[1]))

        self.command_channel = self.zmq_context.socket(zmq.REP)
        self.command_channel.connect(tcp_url(client_address, client_ports[2]))
        logger.info("Connected to client")

        self.run_id = run_id

        self.hub_address = hub_address
        self.hub_zmq_port = hub_zmq_port

        self.pending_task_queue: SortedList[Any] = SortedList(key=lambda tup: (tup[0], tup[1]))

        # count of tasks that have been received from the submit side
        self.task_counter = 0

        # count of tasks that have been sent out to worker pools
        self.count = 0

        self.manager_sock = self.zmq_context.socket(zmq.ROUTER)
        self.manager_sock.set_hwm(0)

        if worker_port:
            task_addy = tcp_url(self.interchange_address, worker_port)
            self.manager_sock.bind(task_addy)

        else:
            worker_port = self.manager_sock.bind_to_random_port(
                tcp_url(self.interchange_address),
                min_port=worker_port_range[0],
                max_port=worker_port_range[1],
                max_tries=100,
            )
        self.worker_port = worker_port

        logger.info(f"Bound to port {worker_port} for incoming worker connections")

        self._ready_managers: Dict[bytes, ManagerRecord] = {}
        self.connected_block_history: List[str] = []

        self.heartbeat_threshold = heartbeat_threshold

        self.manager_selector = manager_selector

        self.current_platform = {'parsl_v': PARSL_VERSION,
                                 'python_v': "{}.{}.{}".format(sys.version_info.major,
                                                               sys.version_info.minor,
                                                               sys.version_info.micro),
                                 'os': platform.system(),
                                 'hostname': platform.node(),
                                 'dir': os.getcwd()}

        logger.info("Platform info: {}".format(self.current_platform))

    def get_tasks(self, count: int) -> Sequence[dict]:
        """ Obtains a batch of tasks from the internal pending_task_queue

        Parameters
        ----------
        count: int
            Count of tasks to get from the queue

        Returns
        -------
        List of upto count tasks. May return fewer than count down to an empty list
            eg. [{'task_id':<x>, 'buffer':<buf>} ... ]
        """
        tasks = []
        try:
            for _ in range(count):
                *_, task = self.pending_task_queue.pop()
                tasks.append(task)
        except IndexError:
            pass

        return tasks

    def _send_monitoring_info(self, monitoring_radio: Optional[MonitoringRadioSender], manager: ManagerRecord) -> None:
        if monitoring_radio:
            logger.info("Sending message {} to MonitoringHub".format(manager))

            d: Dict = cast(Dict, manager.copy())
            d['timestamp'] = datetime.datetime.now()
            d['last_heartbeat'] = datetime.datetime.fromtimestamp(d['last_heartbeat'])
            d['run_id'] = self.run_id

            monitoring_radio.send((MessageType.NODE_INFO, d))

    def process_command(self, monitoring_radio: Optional[MonitoringRadioSender]) -> None:
        """ Command server to run async command to the interchange
        """

        reply: Any  # the type of reply depends on the command_req received (aka this needs dependent types...)

        if self.command_channel in self.socks and self.socks[self.command_channel] == zmq.POLLIN:
            logger.debug("entering command_server section")

            command_req = self.command_channel.recv_pyobj()
            logger.debug("Received command request: {}".format(command_req))
            if command_req == "CONNECTED_BLOCKS":
                reply = self.connected_block_history

            elif command_req == "WORKERS":
                num_workers = 0
                for manager in self._ready_managers.values():
                    num_workers += manager['worker_count']
                reply = num_workers

            elif command_req == "MANAGERS":
                reply = []
                for manager_id in self._ready_managers:
                    m = self._ready_managers[manager_id]
                    idle_since = m['idle_since']
                    if idle_since is not None:
                        idle_duration = time.time() - idle_since
                    else:
                        idle_duration = 0.0
                    resp = {'manager': manager_id.decode('utf-8'),
                            'block_id': m['block_id'],
                            'worker_count': m['worker_count'],
                            'tasks': len(m['tasks']),
                            'idle_duration': idle_duration,
                            'active': m['active'],
                            'parsl_version': m['parsl_version'],
                            'python_version': m['python_version'],
                            'draining': m['draining']}
                    reply.append(resp)

            elif command_req == "MANAGERS_PACKAGES":
                reply = {}
                for manager_id in self._ready_managers:
                    m = self._ready_managers[manager_id]
                    manager_id_str = manager_id.decode('utf-8')
                    reply[manager_id_str] = m["packages"]

            elif command_req.startswith("HOLD_WORKER"):
                cmd, s_manager = command_req.split(';')
                manager_id = s_manager.encode('utf-8')
                logger.info("Received HOLD_WORKER for {!r}".format(manager_id))
                if manager_id in self._ready_managers:
                    m = self._ready_managers[manager_id]
                    m['active'] = False
                    self._send_monitoring_info(monitoring_radio, m)
                else:
                    logger.warning("Worker to hold was not in ready managers list")

                reply = None

            elif command_req == "WORKER_BINDS":
                reply = self.worker_port

            else:
                logger.error(f"Received unknown command: {command_req}")
                reply = None

            logger.debug("Reply: {}".format(reply))
            self.command_channel.send_pyobj(reply)

    @wrap_with_logs
    def start(self) -> None:
        """ Start the interchange
        """

        logger.info("Starting main interchange method")

        if self.hub_address is not None and self.hub_zmq_port is not None:
            logger.debug("Creating monitoring radio to %s:%s", self.hub_address, self.hub_zmq_port)
            monitoring_radio = ZMQRadioSender(self.hub_address, self.hub_zmq_port)
            logger.debug("Created monitoring radio")
        else:
            monitoring_radio = None

        poll_period = self.poll_period

        start = time.time()

        kill_event = threading.Event()

        poller = zmq.Poller()
        poller.register(self.manager_sock, zmq.POLLIN)
        poller.register(self.task_incoming, zmq.POLLIN)
        poller.register(self.command_channel, zmq.POLLIN)

        # These are managers which we should examine in an iteration
        # for scheduling a job (or maybe any other attention?).
        # Anything altering the state of the manager should add it
        # onto this list.
        interesting_managers: Set[bytes] = set()

        while not kill_event.is_set():
            self.socks = dict(poller.poll(timeout=poll_period))

            self.process_command(monitoring_radio)
            self.process_task_incoming()
            self.process_manager_socket_message(interesting_managers, monitoring_radio, kill_event)
            self.expire_bad_managers(interesting_managers, monitoring_radio)
            self.expire_drained_managers(interesting_managers, monitoring_radio)
            self.process_tasks_to_send(interesting_managers, monitoring_radio)

        self.zmq_context.destroy()
        delta = time.time() - start
        logger.info(f"Processed {self.count} tasks in {delta} seconds")
        logger.warning("Exiting")

    def process_task_incoming(self) -> None:
        """Process incoming task message(s).
        """

        if self.task_incoming in self.socks and self.socks[self.task_incoming] == zmq.POLLIN:
            logger.debug("start task_incoming section")
            msg = self.task_incoming.recv_pyobj()

            # Process priority, higher number = lower priority
            resource_spec = msg.get('resource_spec', {})
            priority = resource_spec.get('priority', float('inf'))
            queue_entry = (-priority, -self.task_counter, msg)

            logger.debug("putting message onto pending_task_queue")

            self.pending_task_queue.add(queue_entry)
            self.task_counter += 1
            logger.debug(f"Fetched {self.task_counter} tasks so far")

    def process_manager_socket_message(
        self,
        interesting_managers: Set[bytes],
        monitoring_radio: Optional[MonitoringRadioSender],
        kill_event: threading.Event,
    ) -> None:
        """Process one message from manager on the manager_sock channel."""
        if not self.socks.get(self.manager_sock) == zmq.POLLIN:
            return

        logger.debug('starting worker message section')
        msg_parts = self.manager_sock.recv_multipart()
        try:
            manager_id, meta_b, *msgs = msg_parts
            meta = pickle.loads(meta_b)
            mtype = meta['type']
        except Exception as e:
            logger.warning(
                f'Failed to read manager message ([{type(e).__name__}] {e})'
            )
            logger.debug('Message:\n   %r\n', msg_parts, exc_info=e)
            return

        logger.debug(
            'Processing message type %r from manager %r', mtype, manager_id
        )

        if mtype == 'registration':
            ix_minor_py = self.current_platform['python_v'].rsplit('.', 1)[0]
            ix_parsl_v = self.current_platform['parsl_v']
            mgr_minor_py = meta['python_v'].rsplit('.', 1)[0]
            mgr_parsl_v = meta['parsl_v']

            new_rec = ManagerRecord(
                block_id=None,
                start_time=meta['start_time'],
                tasks=[],
                worker_count=0,
                max_capacity=0,
                active=True,
                draining=False,
                last_heartbeat=time.time(),
                idle_since=time.time(),
                parsl_version=mgr_parsl_v,
                python_version=meta['python_v'],
            )

            # m is a ManagerRecord, but meta is a dict[Any,Any] and so can
            # contain arbitrary fields beyond those in ManagerRecord (and
            # indeed does - for example, python_v) which are then ignored
            # later.
            new_rec.update(meta)

            logger.info(f'Registration info for manager {manager_id!r}: {meta}')
            self._send_monitoring_info(monitoring_radio, new_rec)

            if (mgr_minor_py, mgr_parsl_v) != (ix_minor_py, ix_parsl_v):
                kill_event.set()
                vm_exc = VersionMismatch(
                    f"py.v={ix_minor_py} parsl.v={ix_parsl_v}",
                    f"py.v={mgr_minor_py} parsl.v={mgr_parsl_v}",
                )
                result_package = {
                    'type': 'result',
                    'task_id': -1,
                    'exception': serialize_object(vm_exc),
                }
                pkl_package = pickle.dumps(result_package)
                self.results_outgoing.send(pkl_package)
                logger.error(
                    'Manager has incompatible version info with the interchange;'
                    ' sending failure reports and shutting down:'
                    f'\n  Interchange: {vm_exc.interchange_version}'
                    f'\n  Manager:     {vm_exc.manager_version}'
                )

            else:
                # We really should update the associated data structure; but not
                # at this time.  *kicks can down the road*
                assert new_rec['block_id'] is not None, 'Verified externally'

                # set up entry only if we accept the registration
                self._ready_managers[manager_id] = new_rec
                self.connected_block_history.append(new_rec['block_id'])

                interesting_managers.add(manager_id)

                logger.info(
                    f"Registered manager {manager_id!r} (py{mgr_minor_py},"
                    f" {mgr_parsl_v}) and added to ready queue"
                )
                logger.debug("Manager %r -> %s", manager_id, new_rec)

            return

        if not (m := self._ready_managers.get(manager_id)):
            logger.warning(f"Ignoring message from unknown manager: {manager_id!r}")
            return

        if mtype == 'result':
            logger.debug("Number of results in batch: %d", len(msgs))
            b_messages_to_send = []

            for p_message in msgs:
                r = pickle.loads(p_message)
                r_type = r['type']
                if r_type == 'result':
                    # process this for task ID and forward to executor
                    tid = r['task_id']
                    logger.debug("Removing task %s from manager", tid)
                    try:
                        m['tasks'].remove(tid)
                        b_messages_to_send.append(p_message)
                    except Exception:
                        logger.exception(
                            'Ignoring exception removing task_id %s from manager'
                            ' task list %s',
                            tid,
                            m['tasks']
                        )
                elif r_type == 'monitoring':
                    # the monitoring code makes the assumption that no
                    # monitoring messages will be received if monitoring
                    # is not configured, and that monitoring_radio will only
                    # be None when monitoring is not configurated.
                    assert monitoring_radio is not None

                    monitoring_radio.send(r['payload'])

                else:
                    logger.error(
                        f'Discarding result message of unknown type: {r_type}'
                    )

            if b_messages_to_send:
                logger.debug(
                    'Sending messages (%d) on results_outgoing',
                    len(b_messages_to_send),
                )
                self.results_outgoing.send_multipart(b_messages_to_send)
                logger.debug('Sent messages on results_outgoing')

                # At least one result received, so manager now has idle capacity
                interesting_managers.add(manager_id)

                if len(m['tasks']) == 0 and m['idle_since'] is None:
                    m['idle_since'] = time.time()

                self._send_monitoring_info(monitoring_radio, m)

        elif mtype == 'heartbeat':
            m['last_heartbeat'] = time.time()
            self.manager_sock.send_multipart([manager_id, PKL_HEARTBEAT_CODE])

        elif mtype == 'drain':
            m['draining'] = True

        else:
            logger.error(f"Unexpected message type received from manager: {mtype}")

        logger.debug("leaving worker message section")

    def expire_drained_managers(self, interesting_managers: Set[bytes], monitoring_radio: Optional[MonitoringRadioSender]) -> None:

        for manager_id in list(interesting_managers):
            # is it always true that a draining manager will be in interesting managers?
            # i think so because it will have outstanding capacity?
            m = self._ready_managers[manager_id]
            if m['draining'] and len(m['tasks']) == 0:
                logger.info(f"Manager {manager_id!r} is drained - sending drained message to manager")
                self.manager_sock.send_multipart([manager_id, PKL_DRAINED_CODE])
                interesting_managers.remove(manager_id)
                self._ready_managers.pop(manager_id)

                m['active'] = False
                self._send_monitoring_info(monitoring_radio, m)

    def process_tasks_to_send(self, interesting_managers: Set[bytes], monitoring_radio: Optional[MonitoringRadioSender]) -> None:
        # Check if there are tasks that could be sent to managers

        logger.debug(
            "Managers count (interesting/total): %d/%d",
            len(interesting_managers),
            len(self._ready_managers)
        )

        if interesting_managers and self.pending_task_queue:
            shuffled_managers = self.manager_selector.sort_managers(self._ready_managers, interesting_managers)

            while shuffled_managers and self.pending_task_queue:  # cf. the if statement above...
                manager_id = shuffled_managers.pop()
                m = self._ready_managers[manager_id]
                tasks_inflight = len(m['tasks'])
                real_capacity = m['max_capacity'] - tasks_inflight

                if real_capacity and m["active"] and not m["draining"]:
                    tasks = self.get_tasks(real_capacity)
                    if tasks:
                        self.manager_sock.send_multipart([manager_id, pickle.dumps(tasks)])
                        task_count = len(tasks)
                        self.count += task_count
                        tids = [t['task_id'] for t in tasks]
                        m['tasks'].extend(tids)
                        m['idle_since'] = None
                        logger.debug("Sent tasks: %s to manager %r", tids, manager_id)
                        # recompute real_capacity after sending tasks
                        real_capacity -= task_count
                        if real_capacity > 0:
                            logger.debug("Manager %r has free capacity %s", manager_id, real_capacity)
                            # ... so keep it in the interesting_managers list
                        else:
                            logger.debug("Manager %r is now saturated", manager_id)
                            interesting_managers.remove(manager_id)
                    self._send_monitoring_info(monitoring_radio, m)
                else:
                    interesting_managers.remove(manager_id)
            logger.debug("leaving _ready_managers section, with %s managers still interesting", len(interesting_managers))

    def expire_bad_managers(self, interesting_managers: Set[bytes], monitoring_radio: Optional[MonitoringRadioSender]) -> None:
        bad_managers = [(manager_id, m) for (manager_id, m) in self._ready_managers.items() if
                        time.time() - m['last_heartbeat'] > self.heartbeat_threshold]
        for (manager_id, m) in bad_managers:
            logger.debug("Last: {} Current: {}".format(m['last_heartbeat'], time.time()))
            logger.warning(f"Too many heartbeats missed for manager {manager_id!r} - removing manager")
            if m['active']:
                m['active'] = False
                self._send_monitoring_info(monitoring_radio, m)

            logger.warning(f"Cancelling htex tasks {m['tasks']} on removed manager")
            for tid in m['tasks']:
                try:
                    raise ManagerLost(manager_id, m['hostname'])
                except Exception:
                    result_package = {'type': 'result', 'task_id': tid, 'exception': serialize_object(RemoteExceptionWrapper(*sys.exc_info()))}
                    pkl_package = pickle.dumps(result_package)
                    self.results_outgoing.send(pkl_package)
            logger.warning("Sent failure reports, unregistering manager")
            self._ready_managers.pop(manager_id, 'None')
            if manager_id in interesting_managers:
                interesting_managers.remove(manager_id)


def start_file_logger(filename: str, level: int = logging.DEBUG, format_string: Optional[str] = None) -> None:
    """Add a stream log handler.

    Parameters
    ---------

    filename: string
        Name of the file to write logs to. Required.
    level: logging.LEVEL
        Set the logging level. Default=logging.DEBUG
        - format_string (string): Set the format string
    format_string: string
        Format string to use.

    Returns
    -------
        None.
    """
    if format_string is None:
        format_string = (

            "%(asctime)s.%(msecs)03d %(name)s:%(lineno)d "
            "%(processName)s(%(process)d) %(threadName)s "
            "%(funcName)s [%(levelname)s] %(message)s"

        )

    logger.setLevel(level)
    handler = logging.FileHandler(filename)
    handler.setLevel(level)
    formatter = logging.Formatter(format_string, datefmt='%Y-%m-%d %H:%M:%S')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


if __name__ == "__main__":
    setproctitle("parsl: HTEX interchange")

    config = pickle.load(sys.stdin.buffer)

    ic = Interchange(**config)
    ic.start()
