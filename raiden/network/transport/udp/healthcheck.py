from collections import namedtuple
from itertools import repeat

from gevent.event import Event
import structlog

from raiden.exceptions import (
    UnknownAddress,
    RaidenShuttingDown,
)
from raiden.utils import pex, typing
from raiden.transfer import views
from raiden.transfer.state import NodeNetworkStatus
from raiden.network.transport.udp import udp_utils
# type alias to avoid both circular dependencies and flake8 errors
UDPTransport = 'UDPTransport'

log = structlog.get_logger(__name__)  # pylint: disable=invalid-name


HealthEvents = namedtuple('HealthEvents', (
    'event_healthy',
    'event_unhealthy',
))


def healthcheck(
        transport: UDPTransport,
        recipient: typing.Address,
        event_stop: Event,
        event_healthy: Event,
        event_unhealthy: Event,
        nat_keepalive_retries: int,
        nat_keepalive_timeout: int,
        nat_invitation_timeout: int,
        ping_nonce: int,
):
    """ Sends a periodical Ping to `recipient` to check its health. """
    # pylint: disable=too-many-branches

    log.debug(
        'starting healthcheck for',
        node=pex(transport.raiden.address),
        to=pex(recipient),
    )

    # The state of the node is unknown, the events are set to allow the tasks
    # to do work.
    last_state = NodeNetworkStatus.UNKNOWN
    transport.set_node_network_state(
        recipient,
        last_state,
    )

    # Always call `clear` before `set`, since only `set` does context-switches
    # it's easier to reason about tasks that are waiting on both events.

    # Wait for the end-point registration or for the node to quit
    try:
        transport.get_host_port(recipient)
    except UnknownAddress:
        log.debug(
            'waiting for endpoint registration',
            node=pex(transport.raiden.address),
            to=pex(recipient),
        )

        event_healthy.clear()
        event_unhealthy.set()

        backoff = udp_utils.timeout_exponential_backoff(
            nat_keepalive_retries,
            nat_keepalive_timeout,
            nat_invitation_timeout,
        )
        sleep = next(backoff)

        while not event_stop.wait(sleep):
            try:
                transport.get_host_port(recipient)
            except UnknownAddress:
                sleep = next(backoff)
            else:
                break

    # Don't wait to send the first Ping and to start sending messages if the
    # endpoint is known
    sleep = 0
    event_unhealthy.clear()
    event_healthy.set()

    while not event_stop.wait(sleep):
        sleep = nat_keepalive_timeout

        ping_nonce['nonce'] += 1
        messagedata = transport.get_ping(ping_nonce['nonce'])
        message_id = ('ping', ping_nonce['nonce'], recipient)

        # Send Ping a few times before setting the node as unreachable
        try:
            acknowledged = udp_utils.retry(
                transport,
                messagedata,
                message_id,
                recipient,
                event_stop,
                [nat_keepalive_timeout] * nat_keepalive_retries,
            )
        except RaidenShuttingDown:  # For a clean shutdown process
            return

        if event_stop.is_set():
            return

        if not acknowledged:
            log.debug(
                'node is unresponsive',
                node=pex(transport.raiden.address),
                to=pex(recipient),
                current_state=last_state,
                new_state=NodeNetworkStatus.UNREACHABLE,
                retries=nat_keepalive_retries,
                timeout=nat_keepalive_timeout,
            )

            # The node is not healthy, clear the event to stop all queue
            # tasks
            last_state = NodeNetworkStatus.UNREACHABLE
            transport.set_node_network_state(
                recipient,
                last_state,
            )
            event_healthy.clear()
            event_unhealthy.set()

            # Retry until recovery, used for:
            # - Checking node status.
            # - Nat punching.
            try:
                acknowledged = udp_utils.retry(
                    transport,
                    messagedata,
                    message_id,
                    recipient,
                    event_stop,
                    repeat(nat_invitation_timeout),
                )
            except RaidenShuttingDown:  # For a clean shutdown process
                return

        if acknowledged:
            current_state = views.get_node_network_status(
                views.state_from_raiden(transport.raiden),
                recipient,
            )
            log.debug(
                'node answered',
                node=pex(transport.raiden.address),
                to=pex(recipient),
                current_state=current_state,
                new_state=NodeNetworkStatus.REACHABLE,
            )

            if last_state != NodeNetworkStatus.REACHABLE:
                last_state = NodeNetworkStatus.REACHABLE
                transport.set_node_network_state(
                    recipient,
                    last_state,
                )
                event_unhealthy.clear()
                event_healthy.set()
