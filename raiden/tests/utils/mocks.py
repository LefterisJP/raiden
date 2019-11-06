import json
import random
from collections import defaultdict
from unittest.mock import Mock, PropertyMock

from hexbytes import HexBytes

from raiden.blockchain.events import BlockchainEvents, DecodedEvent
from raiden.constants import Environment, RoutingMode
from raiden.raiden_event_handler import RaidenEventHandler
from raiden.raiden_service import RaidenService
from raiden.storage.serialization import JSONSerializer
from raiden.storage.sqlite import SerializedSQLiteStorage
from raiden.storage.wal import WriteAheadLog
from raiden.tests.utils import factories
from raiden.transfer import node
from raiden.transfer.architecture import StateManager
from raiden.transfer.identifiers import CanonicalIdentifier
from raiden.transfer.state_change import ActionInitChain
from raiden.ui.startup import setup_contracts_or_exit
from raiden.utils import privatekey_to_address
from raiden.utils.signer import LocalSigner
from raiden.utils.typing import (
    Address,
    Any,
    BlockNumber,
    BlockSpecification,
    ChannelID,
    Dict,
    Iterable,
    Optional,
    TokenAmount,
    TokenNetworkAddress,
    TokenNetworkRegistryAddress,
)
from raiden_contracts.utils.type_aliases import ChainID


class MockBlockchainEvents:
    def __init__(self, events_to_return):
        self.events_to_return = events_to_return

    def poll_blockchain_events(self, block_number: BlockNumber) -> Iterable[DecodedEvent]:
        return self.events_to_return


class MockJSONRPCClient:
    def __init__(self, privkey: bytes):
        # To be manually set by each test
        self.balances_mapping: Dict[Address, TokenAmount] = {}
        self.chain_id = ChainID(17)
        self.privkey = privkey
        self.address = privatekey_to_address(privkey)
        self.web3 = MockWeb3(self.chain_id)

    @staticmethod
    def can_query_state_for_block(block_identifier):  # pylint: disable=unused-argument
        # To be changed by each test
        return True

    def gas_price(self):  # pylint: disable=unused-argument, no-self-use
        # 1 gwei
        return 1000000000

    def balance(self, address):
        return self.balances_mapping[address]


class MockTokenNetworkProxy:
    def __init__(self, client: MockJSONRPCClient):
        self.client = client

    @staticmethod
    def detail_participants(  # pylint: disable=unused-argument
        participant1, participant2, block_identifier, channel_identifier
    ):
        # To be changed by each test
        return None


class MockPaymentChannel:
    def __init__(self, token_network, channel_id):  # pylint: disable=unused-argument
        self.token_network = token_network


class MockProxyManager:
    def __init__(self, privkey: bytes):
        # let's make a single mock token network for testing
        self.client = MockJSONRPCClient(privkey=privkey)
        self.token_network = MockTokenNetworkProxy(client=self.client)

    def payment_channel(self, canonical_identifier: CanonicalIdentifier):
        return MockPaymentChannel(self.token_network, canonical_identifier.channel_identifier)

    def token_network_registry(  # pylint: disable=unused-argument, no-self-use
        self, address: Address
    ):
        return Mock(address=address)

    def secret_registry(self, address: Address):  # pylint: disable=unused-argument, no-self-use
        return object()

    def user_deposit(self, address: Address):  # pylint: disable=unused-argument, no-self-use
        return object()

    def service_registry(self, address: Address):  # pylint: disable=unused-argument, no-self-use
        return object()


class MockChannelState:
    def __init__(self):
        self.settle_transaction = None
        self.close_transaction = None
        self.our_state = Mock()
        self.partner_state = Mock()


class MockTokenNetwork:
    def __init__(self):
        self.channelidentifiers_to_channels: dict = {}
        self.partneraddresses_to_channelidentifiers: dict = {}


class MockTokenNetworkRegistry:
    def __init__(self):
        self.tokennetworkaddresses_to_tokennetworks: dict = {}


class MockChainState:
    def __init__(self):
        self.identifiers_to_tokennetworkregistries: dict = {}


class MockRaidenService(RaidenService):
    def __init__(self, message_handler=None, state_transition=None, private_key=None, config=None):
        if config is None:
            config = {}

        if private_key is None:
            private_key, self.address = factories.make_privkey_address()
        else:
            self.address = privatekey_to_address(private_key)

        rpc_client = MockJSONRPCClient(privkey=private_key)
        proxy_manager = MockProxyManager(privkey=private_key)

        config.setdefault("blockchain", {})
        config.setdefault("environment_type", Environment.DEVELOPMENT)
        config.setdefault("database_path", ":memory:")
        config["blockchain"].setdefault("query_interval", 10)
        config["blockchain"].setdefault("confirmation_blocks", 5)
        if "contracts_path" not in config:
            setup_contracts_or_exit(config, rpc_client.chain_id)

        super().__init__(
            rpc_client=rpc_client,
            proxy_manager=proxy_manager,
            query_start_block=0,
            default_registry=object(),
            default_secret_registry=object(),
            default_service_registry=object(),
            default_one_to_n_address=None,
            default_msc_address=factories.make_address(),
            transport=object(),
            raiden_event_handler=RaidenEventHandler(),
            message_handler=message_handler,
            routing_mode=RoutingMode.PFS,
            config=config,
            user_deposit=None,
        )

        # self.signer = LocalSigner(private_key)

        # self.message_handler = message_handler
        # self.routing_mode = RoutingMode.PRIVATE
        # self.config = config

        # self.user_deposit = Mock()
        # self.default_registry = Mock()
        # self.default_registry.address = factories.make_address()
        # self.default_one_to_n_address = factories.make_address()
        # self.default_msc_address = factories.make_address()

        # self.targets_to_identifiers_to_statuses: Dict[Address, dict] = defaultdict(dict)
        # self.route_to_feedback_token: dict = {}
        # self.event_poll_lock = gevent.lock.Semaphore()

        if state_transition is None:
            state_transition = node.state_transition

        serializer = JSONSerializer()
        state_manager = StateManager(state_transition, None)
        storage = SerializedSQLiteStorage(":memory:", serializer)
        self.wal = WriteAheadLog(state_manager, storage)

        self.blockchain_events = BlockchainEvents(chain_id=self.rpc_client.chain_id)

        state_change = ActionInitChain(
            pseudo_random_generator=random.Random(),
            block_number=BlockNumber(0),
            block_hash=factories.make_block_hash(),
            our_address=self.rpc_client.address,
            chain_id=self.rpc_client.chain_id,
        )

        self.wal.log_and_dispatch([state_change])

    def on_message(self, message):
        if self.message_handler:
            self.message_handler.on_message(self, message)

    def handle_and_track_state_changes(self, state_changes):
        pass

    def handle_state_changes(self, state_changes):
        pass

    def sign(self, message):
        message.sign(self.signer)


def make_raiden_service_mock(
    token_network_registry_address: TokenNetworkRegistryAddress,
    token_network_address: TokenNetworkAddress,
    channel_identifier: ChannelID,
    partner: Address,
):
    raiden_service = MockRaidenService(config={})
    chain_state = MockChainState()
    wal = Mock()
    wal.state_manager.current_state = chain_state
    raiden_service.wal = wal

    token_network = MockTokenNetwork()
    token_network.channelidentifiers_to_channels[channel_identifier] = MockChannelState()
    token_network.partneraddresses_to_channelidentifiers[partner] = [channel_identifier]

    token_network_registry = MockTokenNetworkRegistry()
    tokennetworkaddresses_to_tokennetworks = (
        token_network_registry.tokennetworkaddresses_to_tokennetworks
    )
    tokennetworkaddresses_to_tokennetworks[token_network_address] = token_network

    chain_state.identifiers_to_tokennetworkregistries = {
        token_network_registry_address: token_network_registry
    }

    return raiden_service


def mocked_failed_response(error: Exception, status_code: int = 200) -> Mock:
    m = Mock(json=Mock(side_effect=error), status_code=status_code)

    type(m).content = PropertyMock(side_effect=error)
    return m


def mocked_json_response(response_data: Optional[Dict] = None, status_code: int = 200) -> Mock:
    data = response_data or {}
    return Mock(json=Mock(return_value=data), content=json.dumps(data), status_code=status_code)


class MockEth:
    def getBlock(  # pylint: disable=unused-argument, no-self-use
        self, block_identifier: BlockSpecification
    ) -> Dict:
        return {
            "number": 1,
            "hash": HexBytes("0x3f683b65d412e00b068b7ee699d6765df8d5306a5c9b15b133e249adf4b7d456"),
            "gasLimit": 9990259,
        }


class MockWeb3Version:
    def __init__(self, netid):
        self.network = netid


class MockWeb3:
    def __init__(self, netid):
        self.version = MockWeb3Version(netid)
        self.eth = MockEth()
