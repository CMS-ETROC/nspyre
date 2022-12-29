"""
The nspyre data server transports arbitrary python objects over a TCP/IP socket
to a set of local or remote network clients, and keeps those objects up to date
as they are modified. For each data set on the data server, there is a single
data "source", and a set of data "sinks".

Objects are serialized by the source then pushed to the server. For local
clients, the data server sends the serialized data directly to the be
deserialized by the sink process. For sinks, the serialized object
data is diffed with any previously pushed data and the diff is sent rather than
the full object in order to minimize the required network bandwidth. The client
can then reconstruct the pushed data using a local copy of the last version of
the object, and the diff received from the server.

Example usage:

.. code-block:: console

   $ nspyre-dataserv -p 12345

"""
import asyncio
import logging
import selectors
import socket
from typing import Dict

logger = logging.getLogger(__name__)

# default port to host the data server on
DATASERV_PORT = 30000

# if no data is available, any socket sender should send an empty message with an
# interval given by KEEPALIVE_TIMEOUT (s)
KEEPALIVE_TIMEOUT = 3
# time (s) that the sender has to do work before it should give up
# in order to prevent a timeout on its associated receiver
OPS_TIMEOUT = 10
# timeout (s) for receive connections
TIMEOUT = (KEEPALIVE_TIMEOUT + OPS_TIMEOUT) + 1

# maximum size of the data queue
QUEUE_SIZE = 5

# indicates that the client is requesting some data about the server
NEGOTIATION_INFO = b'\xDE'
# TODO runtime control of the data server (maybe with rpyc?)
# NEGOTIATION_CMD = b'\xAD'
# indicates that the client will source data to the server
NEGOTIATION_SOURCE = b'\xBE'
# indicates that the client will sink data from the server
NEGOTIATION_SINK = b'\xEF'
# timeout (s) for send/recv operations during the client negotiation phase
NEGOTIATION_TIMEOUT = TIMEOUT

# timeout for relatively quick operations
FAST_TIMEOUT = 1.0

# custom recv_msg() and send_msg() use the following packet structure
# |                      HEADER       | PAYLOAD
# | message length (excluding header) | message
# |        HEADER_MSG_LEN             | variable length

# length (bytes) of the header section that identifies how large the payload is
HEADER_MSG_LEN = 8


class _CustomSock:
    """Tiny socket wrapper class that implements a custom messaging protocol"""

    def __init__(self, sock_reader, sock_writer):
        self.sock_reader = sock_reader
        self.sock_writer = sock_writer
        # (ip addr, port) of the client
        self.addr = sock_writer.get_extra_info('peername')

    async def recv_msg(self) -> bytes:
        """Receive a message through a socket by decoding the header then reading
        the rest of the message"""

        # the header bytes we receive from the client should identify
        # the length of the message payload
        msg_len_bytes = await self.sock_reader.readexactly(HEADER_MSG_LEN)
        msg_len = int.from_bytes(msg_len_bytes, byteorder='little')

        # get the payload
        msg = await self.sock_reader.readexactly(msg_len)

        logger.debug(f'received [{msg_len}] bytes from [{self.addr}]')

        return msg

    async def send_msg(self, msg: bytes):
        """Send a byte message through a socket interface by encoding the header
        then sending the rest of the message"""

        # calculate the payload length and package it into bytes
        msg_len_bytes = len(msg).to_bytes(HEADER_MSG_LEN, byteorder='little')

        # send the header + payload
        self.sock_writer.write(msg_len_bytes + msg)
        await self.sock_writer.drain()

        logger.debug(f'sent [{len(msg)}] bytes to {self.addr}')

    async def close(self):
        """Fully close a socket connection"""
        self.sock_writer.close()
        await self.sock_writer.wait_closed()
        logger.debug(f'closed socket [{self.addr}]')


def _queue_flush_and_put(queue, item):
    """Empty an asyncio queue then put a single item onto it"""
    for _ in range(queue.qsize()):
        queue.get_nowait()
        queue.task_done()
    queue.put_nowait(item)


async def _cleanup_event_loop(loop):
    """End all tasks in an event loop and exit"""

    if not loop.is_running():
        logger.warning('Ignoring loop cleanup request because the loop isn\'t running.')
        return

    # gather all of the tasks except this one
    pending_tasks = []
    for task in asyncio.all_tasks(loop=loop):
        if task is not asyncio.current_task():
            pending_tasks.append(task)

    # cancel each pending task
    for task in pending_tasks:
        task.cancel()
    # wait for all tasks to exit
    await asyncio.gather(*pending_tasks, return_exceptions=True)

    # shut down the event loop
    loop.stop()


class _DataSet:
    """Class that wraps a pipeline consisting of a data source and a list of
    data sinks."""

    def __init__(self):
        # dict of the form
        # {'task': asyncio task object for source,
        # 'sock': socket for the source}
        self.source = None
        # dict of dicts of the form
        # {('127.0.0.1', 13445):
        #           {'task': asyncio task object for sink,
        #           'sock': socket for the sink,
        #           'queue': sink FIFO/queue},
        # ('192.168.1.5', 19859): ... }
        self.sinks = {}
        # object to store the most up-to-date data for safekeeping
        self.data = None

    async def run_sink(
        self,
        event_loop: asyncio.AbstractEventLoop,
        sock: _CustomSock,
    ):
        """run a new data sink until it closes

        Args:
            sock: socket for the sink
        """
        sink_id = sock.addr
        # sink connections should get unique ports, so this shouldn't happen
        assert sink_id not in self.sinks
        # create the sink task
        task = asyncio.create_task(self._sink_coro(event_loop, sink_id))
        # create a queue for the sink
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_SIZE)
        # add the sink data to the DataSet
        sink_dict = {
            'task': task,
            'sock': sock,
            'queue': queue,
        }
        self.sinks[sink_id] = sink_dict
        if self.data:
            # push the current data to the sink so it has a starting point
            queue.put_nowait(self.data)
        await task

    async def run_source(self, sock: _CustomSock):
        """run a data source until it closes"""
        task = asyncio.create_task(self._source_coro())
        self.source = {'task': task, 'sock': sock}
        await task

    async def _source_coro(self):
        """Receive data from a source client and transfer it to the client queues"""
        sock = self.source['sock']
        try:
            while True:
                try:
                    new_pickle = await asyncio.wait_for(
                        sock.recv_msg(), timeout=TIMEOUT
                    )
                except (asyncio.IncompleteReadError, asyncio.TimeoutError) as exc:
                    # if there was a timeout / problem receiving the message
                    # the source client is dead and will be terminated
                    logger.debug(
                        f'source [{sock.addr}] disconnected or hasn\'t sent a keepalive message - dropping connection'
                    )
                    raise asyncio.CancelledError from exc

                if len(new_pickle):
                    self.data = new_pickle
                    logger.debug(
                        f'source [{sock.addr}] received pickle of [{len(new_pickle)}] bytes'
                    )
                    for sink_id in self.sinks:
                        sink = self.sinks[sink_id]
                        queue = sink['queue']
                        try:
                            queue.put_nowait(new_pickle)
                        except asyncio.QueueFull:
                            # the sink isn't consuming data fast enough
                            # so we will empty the queue and place only this most recent
                            # piece of data on it
                            logger.debug(
                                f'sink [{sink["sock"].addr}] can\'t keep up with data source'
                            )
                            _queue_flush_and_put(queue, new_pickle)
                        logger.debug(
                            f'source [{sock.addr}] queued pickle of [{len(new_pickle)}] for sink [{sink["sock"].addr}]'
                        )
                else:
                    # the server just sent a keepalive signal
                    logger.debug(f'source [{sock.addr}] received keepalive')
        except asyncio.CancelledError as exc:
            raise asyncio.CancelledError from exc
        finally:
            logger.info(f'dropped source [{sock.addr}]')
            self.source = None

    async def _sink_coro(
        self,
        event_loop: asyncio.AbstractEventLoop,
        sink_id: tuple,
    ):
        """Receive source data from the queue"""
        sock = self.sinks[sink_id]['sock']
        queue = self.sinks[sink_id]['queue']
        try:
            while True:
                try:
                    # get pickle data from the queue
                    new_pickle = await asyncio.wait_for(
                        queue.get(), timeout=KEEPALIVE_TIMEOUT
                    )
                    queue.task_done()
                    logger.debug(
                        f'sink [{sock.addr}] got [{len(new_pickle)}] bytes from queue'
                    )
                except asyncio.TimeoutError:
                    # if there's no data available, send a keepalive message
                    logger.debug(
                        f'sink [{sock.addr}] no data available - sending keepalive'
                    )
                    new_pickle = b''

                try:
                    await asyncio.wait_for(
                        sock.send_msg(new_pickle),
                        timeout=OPS_TIMEOUT / 4,
                    )
                    logger.debug(f'sink [{sock.addr}] sent [{len(new_pickle)}] bytes')
                except (ConnectionError, asyncio.TimeoutError) as exc:
                    logger.info(
                        f'sink [{sock.addr}] disconnected or isn\'t accepting data - dropping connection'
                    )
                    raise asyncio.CancelledError from exc
        except asyncio.CancelledError as exc:
            raise asyncio.CancelledError from exc
        finally:
            self.sinks.pop(sink_id)
            logger.debug(f'dropped sink [{sock.addr}]')


class DataServer:
    """
    The server has a set of DataSet objects. Each has 1 data source, and any
    number of data sinks. Pickled object data from the source is received on
    its socket, then transferred to the FIFO of every sink. The pickle is then
    sent out on the sink's socket.
    E.g.::

        self.datasets = {

        'dataset1' : _DataSet(
        socket (source) ----------> FIFO ------> socket (sink 1)
                           |
                            ------> FIFO ------> socket (sink 2)
        ),

        'dataset2' : _DataSet(
        socket (source) ----------> FIFO ------> socket (sink 1)
                           |
                            ------> FIFO ------> socket (sink 2)
                           |
                            ------> FIFO ------> socket (sink 3)
                           |
                            ------> FIFO ------> socket (sink 4)
        ),

        ... }

    """

    def __init__(self, port: int = DATASERV_PORT):
        """port: TCP/IP port of the data server"""
        self.port = port
        # a dictionary with string identifiers mapping to DataSet objects
        self.datasets: Dict[str, _DataSet] = {}
        # asyncio event loop for running all the server tasks
        # TODO for some reason there are performance issues on windows when using the ProactorEventLoop
        selector = selectors.SelectSelector()
        self.event_loop = asyncio.SelectorEventLoop(selector)

    def serve_forever(self):
        """Run the asyncio event loop - ayncio requires this be run in the main thread if
        processes are to be spawned from the event loop. See https://docs.python.org/3/library/asyncio-dev.html."""
        self.event_loop.set_debug(True)
        asyncio.set_event_loop(self.event_loop)
        try:
            self.event_loop.call_soon(self._main_helper)
            self.event_loop.run_forever()
        finally:
            self.event_loop.close()
            logger.info('data server closed')

    def stop(self):
        """Stop the asyncio event loop."""
        if self.event_loop.is_running():
            asyncio.run_coroutine_threadsafe(
                _cleanup_event_loop(self.event_loop), self.event_loop
            )
        else:
            raise RuntimeError('tried stopping the data server but it isn\'t running!')

    def _main_helper(self):
        """Callback function to start _main"""
        asyncio.create_task(self._main())

    async def _main(self):
        """Socket server listening coroutine"""

        # call self.negotiation when a new client connects
        # force ipv4
        server = await asyncio.start_server(
            self._negotiation, 'localhost', self.port, family=socket.AF_INET
        )

        addr = server.sockets[0].getsockname()
        logger.info(f'Serving on {addr}')

        async with server:
            await server.serve_forever()

    async def _negotiation(self, sock_reader, sock_writer):
        """Coroutine that determines what kind of client has connected, and deal
        with it accordingly"""

        # custom socket wrapper for sending / receiving structured messages
        sock = _CustomSock(sock_reader, sock_writer)

        logger.info(f'new client connection from [{sock.addr}]')

        try:
            try:
                # the first message we receive from the client should identify
                # what kind of client it is
                client_type = await asyncio.wait_for(
                    sock.recv_msg(), timeout=NEGOTIATION_TIMEOUT
                )
            except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                logger.warning(
                    f'connection with client [{sock.addr}] failed before it '
                    'identified itself during the negotiation phase'
                )
                try:
                    await sock.close()
                except IOError:
                    pass
                return

            # info client
            if client_type == NEGOTIATION_INFO:
                logger.info(f'client [{sock.addr}] is type [info]')
                # the client is requesting general info about the server
                # tell the client which datasets are available
                data = ','.join(list(self.datasets.keys())).encode()
                try:
                    await asyncio.wait_for(
                        sock.send_msg(data), timeout=NEGOTIATION_TIMEOUT
                    )
                except (ConnectionError, asyncio.TimeoutError):
                    logger.warning(
                        f'server failed sending data to [info] client [{sock.addr}]'
                    )
                try:
                    await sock.close()
                except IOError:
                    pass
                return

            # data source client
            elif client_type == NEGOTIATION_SOURCE:
                logger.info(f'client [{sock.addr}] is type [source]')
                # the client will be a data source for a dataset on the server
                # first we need know which dataset it will provide data for
                try:
                    dataset_name_bytes = await asyncio.wait_for(
                        sock.recv_msg(), timeout=NEGOTIATION_TIMEOUT
                    )
                except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                    logger.warning(
                        f'failed getting the data set name from client [{sock.addr}]'
                    )
                    try:
                        await sock.close()
                    except IOError:
                        pass
                    return

                dataset_name = dataset_name_bytes.decode()

                if dataset_name not in self.datasets:
                    # create a new DataSet
                    self.datasets[dataset_name] = _DataSet()

                # the server already contains a dataset with this name
                if self.datasets[dataset_name].source:
                    # the dataset already has a source
                    logger.warning(
                        f'client [{sock.addr}] wants to source data for data set [{dataset_name}], but it already has a source - dropping connection'
                    )
                    try:
                        await sock.close()
                    except IOError:
                        pass
                    return
                else:
                    logger.info(
                        f'client [{sock.addr}] sourcing data for data set [{dataset_name}]'
                    )
                    # the dataset exists and it's original source is gone, so the client
                    # will act as the new source
                    await self.datasets[dataset_name].run_source(sock)

            # data sink client
            elif client_type == NEGOTIATION_SINK:
                logger.info(f'client [{sock.addr}] is type [sink]')
                # get the dataset name
                try:
                    dataset_name_bytes = await asyncio.wait_for(
                        sock.recv_msg(), timeout=NEGOTIATION_TIMEOUT
                    )
                except (asyncio.IncompleteReadError, asyncio.TimeoutError):
                    logger.warning(
                        f'failed getting the data set name from client [{sock.addr}]'
                    )
                    try:
                        await sock.close()
                    except IOError:
                        pass
                    return

                dataset_name = dataset_name_bytes.decode()

                if dataset_name in self.datasets:
                    logger.info(
                        f'client [{sock.addr}] sinking data from data set [{dataset_name}]'
                    )
                    # add the client to the sinks for the requested dataset
                    await self.datasets[dataset_name].run_sink(self.event_loop, sock)
                else:
                    # the requested dataset isn't available on the server
                    logger.warning(
                        f'client [{sock.addr}] wants to sink data from data set [{dataset_name}], but it doesn\'t exist - dropping connection'
                    )
                    try:
                        await sock.close()
                    except IOError:
                        pass
                    return
            # unknown client type
            else:
                # the client gave an invalid connection type
                logger.error(
                    f'client [{sock.addr}] provided an invalid connection type [{client_type}] - dropping connection'
                )
                try:
                    await sock.close()
                except IOError:
                    pass
                return
        except ConnectionResetError:
            logger.debug(f'client [{sock.addr}] forcibly closed - closing connection')
        except asyncio.CancelledError as exc:
            logger.debug(
                f'communication with client [{sock.addr}] cancelled - closing connection'
            )
            try:
                await sock.close()
            except IOError:
                pass
            raise asyncio.CancelledError from exc

    def __enter__(self):
        """Python context manager setup"""
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        """Python context manager teardown"""
        self.stop()
