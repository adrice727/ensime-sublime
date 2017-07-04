# coding: utf-8
import sublime

import time
import json
from threading import Thread

import websocket

from .protocol import ProtocolHandler
from .util import catch
from .errors import LaunchError
from .outgoing import ConnectionInfoRequest
from .config import gconfig


class EnsimeClient(ProtocolHandler):
    """An ENSIME client for a project configuration path (``.ensime``).

    This is a base class with an abstract ProtocolHandler – you will
    need to provide a concrete one.

    Once constructed, a client instance can either connect to an existing
    ENSIME server or launch a new one with a call to the ``setup()`` method.

    Communication with the server is done over a websocket (`self.ws`). Messages
    are sent to the server in the calling thread, while messages are received on
    a separate background thread and enqueued in `self.queue` upon receipt.

    Each call to the server contains a `callId` field with an integer ID,
    generated from `self.call_id`. Responses echo back the `callId` field so
    that appropriate handlers can be invoked.

    Responses also contain a `typehint` field in their `payload` field, which
    contains the type of the response. This is used to key into `self.handlers`,
    which stores the a handler per response type.
    """

    def __init__(self, parent_environment, launcher):
        super(EnsimeClient, self).__init__()
        self.launcher = launcher
        self.env = parent_environment
        self.env.logger.debug('__init__: in')

        self.ws = None
        self.ensime = None
        self.ensime_server = None

        self.call_id = 1
        self.call_options = {}
        self.connection_timeout = self.env.settings.get("timeout_connection", 20)

        # Map for messages received from the ensime server.
        self.responses = {}
        # By default, don't connect to server more than once
        self.number_try_connection = 1

        self.running = True  # queue poll is running
        self.connected = False  # connected to ensime server through websocket

        thread = Thread(name='queue-poller', target=self.queue_poll)
        thread.daemon = True
        thread.start()

    def queue_poll(self, sleep_t=0.5):
        """Put new messages in the map as they arrive.
        Since putting a value in a map is an atomic operation,
        existence of a certain key and retrieval can be done
        from a separate thread by the client.
        Value of sleep is low to improve responsiveness.
        """
        while self.running:
            if self.ws is not None:
                def log_and_close(msg):
                    if self.connected:
                        self.env.logger.error('Websocket exception', exc_info=True)
                        # Stop everything.
                        self.shutdown_server()
                        self._display_ws_warning()

                with catch(websocket.WebSocketException, log_and_close):
                    result = self.ws.recv()
                    _json = json.loads(result)
                    # Watch if it has a callId
                    call_id = _json.get("callId")
                    # TODO. check if a callback is registered for this call_id
                    # in call_options or the call_id is simply None
                    if call_id is not None:
                        self.responses[call_id] = _json
                    else:
                        if _json["payload"]:
                            self.handle_incoming_response(call_id, _json["payload"])
            time.sleep(sleep_t)

    def connect_when_ready(self, timeout, fallback):
        """Given a maximum timeout, waits for the http port to be written.
        Tries to connect to the websocket if it's written.
        If it fails cleans up by calling fallback. Ideally, should stop ensime
        process if connection wasn't established.
        """
        if not self.ws:
            while not self.ensime.is_ready() and (timeout > 0):
                time.sleep(1)
                timeout -= 1
            if self.ensime.is_ready():
                self.connected = self.connect_ensime_server()

            if self.connected:
                self.env.logger.info("Connected to the server.")
            else:
                fallback()
                self.env.logger.info("Couldn't connect to the server waited to long :(")
        else:
            self.env.logger.info("Already connected.")

    def setup(self):
        """Setup the client. Starts the enisme process using launcher
        and connects to it through websocket"""
        def initialize_ensime():
            if not self.ensime:
                self.env.logger.info("----Initialising server----")
                try:
                    self.ensime = self.launcher.launch()
                except LaunchError as err:
                    self.env.logger.error(err)
            return bool(self.ensime)

        # True if ensime is up, otherwise False
        self.running = initialize_ensime()
        if self.running:
            connect_when_ready_thread = Thread(target=self.connect_when_ready,
                                               args=(self.connection_timeout, self.shutdown_server))
            connect_when_ready_thread.daemon = True
            connect_when_ready_thread.start()

        return self.running

    def _display_ws_warning(self):
        warning = "A WS exception happened, 'ensime-sublime' has been disabled. " +\
            "For more information, have a look at the logs in `.ensime_cache`"
        sublime.error_message(warning)

    def send(self, msg):
        """Send something to the ensime server."""
        def reconnect(e):
            self.env.logger.error('send error, reconnecting...')
            self.connect_ensime_server()
            if self.ws:
                self.ws.send(msg + "\n")

        self.env.logger.debug('send: in')
        if self.ws is not None:
            with catch(websocket.WebSocketException, reconnect):
                self.env.logger.debug('send: sending JSON on WebSocket')
                self.ws.send(msg + "\n")

    def connect_ensime_server(self):
        """Start initial connection with the server.
        Return True if the connection info is received
        else returns False"""
        self.env.logger.debug('connect_ensime_server: in')

        def disable_completely(e):
            if e:
                self.env.logger.error('connection error: %s', e, exc_info=True)
            self.shutdown_server()
            self.env.logger.info("Server was shutdown.")
            self._display_ws_warning()

        if self.running and self.number_try_connection:
            if not self.ensime_server:
                port = self.ensime.http_port()
                uri = "websocket"
                self.ensime_server = gconfig['ensime_server'].format(port, uri)
            with catch(websocket.WebSocketException, disable_completely):
                # Use the default timeout (no timeout).
                options = {"subprotocols": ["jerky"]}
                options['enable_multithread'] = True
                self.env.logger.info("About to connect to %s with options %s",
                                     self.ensime_server, options)
                self.ws = websocket.create_connection(self.ensime_server, **options)
            self.number_try_connection -= 1
            call_id = ConnectionInfoRequest().run_in(self.env)
            received_response = self.get_response(call_id, timeout=30)  # confirm response
            return received_response
        else:
            # If it hits this, number_try_connection is 0
            disable_completely(None)
        return False

    def shutdown_server(self):
        """Shut down the ensime process if it is running and
        uncolorizes the open views in the editor.
        Does not change the client's running status."""
        self.env.logger.debug('shutdown_server: in')
        self.connected = False
        if self.ensime:
            self.ensime.stop()
        self.env.editor.uncolorize_all()

    def teardown(self):
        """Shutdown down the client. Stop the server if connected.
        This stops the loop receiving responses from the websocket."""
        self.env.logger.debug('teardown: in')
        self.running = False
        self.shutdown_server()

    def send_request(self, request):
        """Send a request to the server."""
        self.env.logger.debug('send_request: in')

        message = {'callId': self.call_id, 'req': request}
        self.env.logger.debug('send_request: %s', message)
        self.send(json.dumps(message))

        call_id = self.call_id
        self.call_id += 1
        return call_id

    def get_response(self, call_id, timeout=10, should_wait=True):
        """Gets a response with the specified call_id.
        If should_wait is set to true waits for the response to appear
        in the `responses` map for time specified by timeout.
        Returns True or False based on wether a response for that call_id was found."""
        start, now = time.time(), time.time()
        wait = should_wait and call_id not in self.responses
        while wait and (now - start) < timeout:
            if call_id not in self.responses:
                time.sleep(0.25)
                now = time.time()
            else:
                result = self.responses[call_id]
                self.env.logger.debug('unqueue: result received\n%s', result)
                if result and result != "nil":
                    wait = None
                    # Restart timeout
                    start, now = time.time(), time.time()
                    # Watch out, it may not have callId
                    call_id = result.get("callId")
                    if result["payload"]:
                        self.handle_incoming_response(call_id, result["payload"])
                    del self.responses[call_id]
                else:
                    self.env.logger.debug('unqueue: nil or None received')

        if (now - start) >= timeout:
            self.env.logger.warning('unqueue: no reply from server for %ss', timeout)
            return False
        return True
