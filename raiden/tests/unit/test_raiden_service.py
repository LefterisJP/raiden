from raiden.blockchain.events import DecodedEvent
from raiden.tests.utils.factories import HOP1, UNIT_TOKEN_NETWORK_ADDRESS
from raiden.tests.utils.mocks import MockBlockchainEvents, MockRaidenService
from raiden_contracts.constants import ChannelEvent


def test_foo(chain_id):
    raiden = MockRaidenService()
    queried_block_number = 1
    events_to_return = [
        DecodedEvent(
            chain_id=chain_id,
            block_number=queried_block_number,
            block_hash="0x18056735e08706932904659d0114654578b9bf8b49e3a9a75076a5a879473b97",
            transaction_hash="0x1",
            originating_contract=UNIT_TOKEN_NETWORK_ADDRESS,
            event_data={
                "event": ChannelEvent.CLOSED,
                "block_number": queried_block_number,
                "transaction_hash": "0x1",
                "block_hash": "0x18056735e08706932904659d0114654578b9bf8b49e3a9a75076a5a879473b97",
                "args": {"closing_participant": HOP1, "channel_identifier": 1},
            },
        )
    ]
    block = {"number": queried_block_number}
    # patch_getBlock = patch.object(raiden.rpc_client.web3., "get_pfs_info", return_value=PFS_INFO):

    # raiden.handle_and_track_state_changes = raiden.handle_state_changes

    raiden.blockchain_events = MockBlockchainEvents(events_to_return)
    raiden._callback_new_block(block)
    import pdb

    pdb.set_trace()

    a = 1
